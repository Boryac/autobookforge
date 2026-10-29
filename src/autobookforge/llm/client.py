"""OpenAI 兼容的 LLM 客户端（零第三方依赖）。

只用标准库 ``urllib`` 发请求，兼容 OpenAI / DeepSeek / 通义 / Kimi / 本地 vLLM / Ollama
等一切遵循 ``POST {base_url}/chat/completions`` 的服务。API 地址、密钥、模型名由用户在
Web 设置页填写。

内置的降本与稳健机制：

* **前缀缓存**：system prompt 恒为 messages 首条且字节稳定，命中服务端前缀缓存；
  同时解析响应中的缓存命中字段（``prompt_tokens_details.cached_tokens``、
  ``prompt_cache_hit_tokens``、``cache_read_input_tokens``）用于成本核算。
* **结构化输出**：``response_format={"type":"json_object"}`；端点不支持时自动降级，
  后续请求不再携带该字段（避免反复失败）。
* **重试**：网络错误 / 429 / 5xx 指数退避重试，尊重 ``Retry-After``；
  输出未通过字段校验时用最小回灌指令做一次修复重试。
* **本地缓存**：同一份输入（同模型、同 prompt）只计费一次。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .prompts import system_prompt
from .schema import SchemaError, extract_json, repair_instruction, validate
from .settings import LLMSettings

USER_AGENT = "AutoBookForge/1.0 (+local)"


# --------------------------------------------------------------------------- #
# 用量统计
# --------------------------------------------------------------------------- #


@dataclass
class TaskUsage:
    calls: int = 0
    retries: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "elapsed": round(self.elapsed, 3),
        }


@dataclass
class LLMUsage:
    """全局用量与降本统计。"""

    calls: int = 0
    retries: int = 0
    failures: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    estimated_prompt_tokens: int = 0   # 若本地估算（服务端未返回 usage）
    elapsed: float = 0.0
    per_task: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    def task(self, task_id: str) -> TaskUsage:
        if task_id not in self.per_task:
            self.per_task[task_id] = TaskUsage()
        return self.per_task[task_id]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "elapsed": round(self.elapsed, 3),
            "per_task": {k: v.as_dict() for k, v in self.per_task.items()},
        }


class LLMError(RuntimeError):
    """调用失败（含状态码与原因）。"""

    def __init__(self, message: str, *, status: int = 0, kind: str = "error", retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# 本地结果缓存
# --------------------------------------------------------------------------- #


class ResultCache:
    """按 (model + task + user) 哈希缓存模型输出，避免重复计费。"""

    def __init__(self, path: Optional[str] = None, *, max_entries: int = 4000) -> None:
        self.path = path
        self.max_entries = max_entries
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()
        self.hits = 0
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self._data = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                self._data = {}

    @staticmethod
    def make_key(model: str, task: str, user: str) -> str:
        h = hashlib.sha1(f"{model}\u0000{task}\u0000{user}".encode("utf-8")).hexdigest()
        return h

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            v = self._data.get(key)
            if v is not None:
                self.hits += 1
            return v

    def set(self, key: str, value: str) -> None:
        with self._lock:
            if len(self._data) >= self.max_entries:
                for k in list(self._data.keys())[: self.max_entries // 10]:
                    self._data.pop(k, None)
            self._data[key] = value

    def flush(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 估算
# --------------------------------------------------------------------------- #


def cjk_tokens(n_chars: int) -> int:
    """按字数估算中文 token（CJK 约 1 字 1 token，另加少量结构开销）。"""
    return int(max(0, n_chars) * 1.05) + 8


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 按 1 字 1 token，其余按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk + other / 4) + 4


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class LLMClient:
    """对话补全客户端。线程安全。"""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        usage: Optional[LLMUsage] = None,
        cache: Optional[ResultCache] = None,
    ) -> None:
        self.settings = settings
        self.usage = usage or LLMUsage()
        self.cache = cache
        self._json_mode_supported = True
        self._thinking_supported = True
        self._lock = threading.Lock()
        self._local = threading.local()
        self._fail_streak: dict[str, int] = {}
        self._circuit_limit = 3

    # -- 状态 --
    @property
    def available(self) -> bool:
        return self.settings.active

    def _circuit_open(self, task: str) -> bool:
        """同一任务连续失败达到阈值后熔断，避免整本书都被拖在无效重试上。"""
        with self._lock:
            return self._fail_streak.get(task, 0) >= self._circuit_limit

    def _record_ok(self, task: str) -> None:
        with self._lock:
            self._fail_streak[task] = 0

    def _record_fail(self, task: str) -> None:
        with self._lock:
            self._fail_streak[task] = self._fail_streak.get(task, 0) + 1

    def _cache_key(self, task: str, user: str) -> str:
        return ResultCache.make_key(self.settings.model, task, user)

    # -- 组装请求 --
    def _build_messages(self, task: str, user: str) -> list[dict]:
        return [
            {"role": "system", "content": system_prompt(task, with_cache_hint=self.settings.prefix_cache)},
            {"role": "user", "content": user},
        ]

    def _payload(
        self,
        messages: Sequence[dict],
        *,
        json_mode: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> dict:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": list(messages),
            "temperature": self.settings.temperature if temperature is None else float(temperature),
            "max_tokens": int(max_tokens or self.settings.max_tokens),
            "stream": False,
        }
        if json_mode and self.settings.json_mode and self._json_mode_supported:
            payload["response_format"] = {"type": "json_object"}
        if self._thinking_supported:
            mode = str(getattr(self.settings, "thinking", "auto") or "auto")
            if mode == "off_all":
                payload["enable_thinking"] = False
                payload["chat_template_kwargs"] = {"enable_thinking": False}
                payload["thinking"] = {"type": "disabled"}
            elif mode == "off_vllm":
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            elif mode == "off_openai":
                payload["enable_thinking"] = False
                payload["thinking"] = {"type": "disabled"}
            elif mode == "off_reasoning":
                payload["reasoning_effort"] = "none"
        return payload

    def _bump_max_tokens(self) -> bool:
        """输出被推理过程耗尽时放大输出预算（上限 16384），返回是否生效。"""
        with self._lock:
            cur = int(self.settings.max_tokens or 0)
            if cur <= 0:
                return False
            nxt = 16384 if cur < 16384 else cur
            if nxt <= cur:
                return False
            self.settings.max_tokens = nxt
            return True

    # -- 低层 HTTP --
    def _post(self, payload: dict) -> dict:
        url = self.settings.chat_url()
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.api_key.strip()}",
            "User-Agent": USER_AGENT,
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        timeout = max(5, int(self.settings.timeout))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except Exception:
                retry_after = 0.0
            kind = "http"
            retryable = exc.code in (408, 409, 425, 429, 500, 502, 503, 504, 522, 524)
            detail = raw[:400].replace("\n", " ")
            # 端点不支持 response_format：降级重试
            if exc.code == 400 and "response_format" in raw:
                raise LLMError(
                    "端点不支持 response_format，已自动降级", status=400, kind="json_mode_unsupported", retryable=True
                )
            if exc.code == 400 and any(
                w in raw for w in ("enable_thinking", "chat_template_kwargs", "reasoning_effort", "'thinking'", '"thinking"')
            ):
                raise LLMError(
                    "端点不认识关闭思考的字段，已自动降级", status=400, kind="thinking_unsupported", retryable=True
                )
            raise LLMError(f"HTTP {exc.code}: {detail}", status=exc.code, kind=kind, retryable=retryable) from None
        except urllib.error.URLError as exc:
            raise LLMError(f"网络错误: {exc.reason}", kind="network", retryable=True) from None
        except TimeoutError:
            raise LLMError("请求超时", kind="timeout", retryable=True) from None

        try:
            return json.loads(body)
        except Exception:
            raise LLMError(f"响应非 JSON: {body[:200]}", kind="bad_response") from None

    @staticmethod
    def _content_of(data: dict) -> str:
        try:
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("响应缺少 choices", kind="bad_response")
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if content is None:
                # 部分端点把内容放在 reasoning_content / text
                content = msg.get("text") or choices[0].get("text") or ""
            return str(content)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"响应结构异常: {exc}", kind="bad_response") from exc

    @staticmethod
    def _usage_of(data: dict) -> tuple[int, int, int]:
        u = data.get("usage") or {}
        prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        cached = 0
        details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or details.get("cache_hit_tokens") or 0)
        cached = int(
            cached
            or u.get("prompt_cache_hit_tokens")
            or u.get("cache_read_input_tokens")
            or 0
        )
        return prompt, completion, cached

    # -- 主调用 --
    def chat(
        self,
        task: str,
        user: str,
        *,
        json_mode: bool = True,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        use_cache: bool = True,
    ) -> str:
        """发送一次对话补全，返回文本内容。失败抛 :class:`LLMError`。"""
        if self._circuit_open(task):
            raise LLMError("该任务连续失败已熔断，跳过剩余请求", kind="circuit", retryable=False)
        if not self.available:
            raise LLMError("LLM 未配置或未启用", kind="unavailable")

        cache_key = self._cache_key(task, user) if (use_cache and self.cache is not None) else None
        if cache_key:
            hit = self.cache.get(cache_key)
            if hit is not None:
                self.usage.cache_hits += 1
                return hit

        messages = self._build_messages(task, user)
        payload = self._payload(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)

        attempts = max(1, int(self.settings.retries) + 1)
        last: Optional[LLMError] = None
        bumped = False
        started = time.perf_counter()
        for i in range(attempts):
            try:
                data = self._post(payload)
                content = self._content_of(data)
                if not content.strip():
                    reason = ""
                    try:
                        reason = str((data.get("choices") or [{}])[0].get("finish_reason") or "")
                    except Exception:
                        reason = ""
                    raise LLMError(
                        "模型返回空内容"
                        + ("（finish_reason=length，输出预算被推理过程耗尽）" if reason == "length" else ""),
                        kind="empty",
                        retryable=True,
                    )
                prompt_t, comp_t, cached_t = self._usage_of(data)
                if not prompt_t:
                    prompt_t = sum(estimate_tokens(m.get("content", "")) for m in messages)
                    self.usage.estimated_prompt_tokens += prompt_t
                u = self.usage
                t = u.task(task)
                with self._lock:
                    u.calls += 1
                    u.prompt_tokens += prompt_t
                    u.completion_tokens += comp_t
                    u.cached_tokens += cached_t
                    u.elapsed += time.perf_counter() - started
                    t.calls += 1
                    t.prompt_tokens += prompt_t
                    t.completion_tokens += comp_t
                    t.cached_tokens += cached_t
                    t.elapsed += time.perf_counter() - started
                    if i > 0:
                        u.retries += i
                        t.retries += i
                self._record_ok(task)
                if cache_key:
                    self.cache.set(cache_key, content)
                return content
            except LLMError as exc:
                last = exc
                if exc.kind == "json_mode_unsupported":
                    with self._lock:
                        self._json_mode_supported = False
                    payload = self._payload(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)
                    continue
                if exc.kind == "thinking_unsupported":
                    with self._lock:
                        self._thinking_supported = False
                    payload = self._payload(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)
                    continue
                if (
                    exc.kind == "empty"
                    and "输出预算被推理过程耗尽" not in str(exc)
                    and not bumped
                    and self._bump_max_tokens()
                ):
                    bumped = True
                    payload = self._payload(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)
                    continue
                if exc.kind == "empty":
                    self._record_fail(task)
                    self.usage.failures += 1
                    raise LLMError(
                        "模型持续返回空内容：该端点把推理过程计入输出预算。"
                        "请在设置里把 max_tokens 提高到 8192 以上，或改用非推理模型",
                        kind="empty",
                        retryable=False,
                    )
                if not exc.retryable or i == attempts - 1:
                    self._record_fail(task)
                    self.usage.failures += 1
                    self.usage.task(task).failures += 1
                    raise
                backoff = min(12.0, (0.8 * (2**i)) + random.uniform(0, 0.3))
                time.sleep(backoff)
        assert last is not None
        raise last

    def chat_json(
        self,
        task: str,
        user: str,
        *,
        schema: Optional[dict] = None,
        schema_hint: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        use_cache: bool = True,
    ) -> Any:
        """请求并解析结构化输出；校验失败时做一次最小回灌修复。"""
        raw = self.chat(
            task,
            user,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
            use_cache=use_cache,
        )
        try:
            obj = extract_json(raw)
        except SchemaError as exc:
            obj = self._repair(task, user, raw, [str(exc)], schema_hint, temperature, max_tokens)
            if obj is None:
                raise
        errors = validate(obj, schema) if schema else []
        if errors:
            fixed = self._repair(task, user, raw, errors, schema_hint, temperature, max_tokens)
            if fixed is not None:
                obj = fixed
        return obj

    def _repair(
        self,
        task: str,
        user: str,
        previous: str,
        errors: Sequence[str],
        schema_hint: str,
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> Optional[Any]:
        """把校验错误回灌，要求模型只重发修正后的 JSON。"""
        try:
            messages = self._build_messages(task, user) + [
                {"role": "assistant", "content": previous[:2000]},
                {"role": "user", "content": repair_instruction(errors, schema_hint=schema_hint)},
            ]
            payload = self._payload(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
            data = self._post(payload)
            content = self._content_of(data)
            prompt_t, comp_t, cached_t = self._usage_of(data)
            u = self.usage
            t = u.task(task)
            with self._lock:
                u.calls += 1
                u.retries += 1
                u.prompt_tokens += prompt_t
                u.completion_tokens += comp_t
                u.cached_tokens += cached_t
                t.calls += 1
                t.retries += 1
                t.prompt_tokens += prompt_t
                t.completion_tokens += comp_t
                t.cached_tokens += cached_t
            return extract_json(content)
        except Exception:
            return None

    # -- 连通性 --
    def test_connection(self) -> dict:
        """测试连通性：优先列模型，失败则发一条最小请求。"""
        result: dict = {
            "base_url": self.settings.base_url,
            "model": self.settings.model,
            "ok": False,
            "latency_ms": 0,
            "models": [],
            "detail": "",
        }
        if not self.settings.configured:
            result["detail"] = "API 地址、密钥或模型名未填写完整"
            return result

        started = time.perf_counter()
        try:
            url = self.settings.models_url()
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key.strip()}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=max(5, int(self.settings.timeout))) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            items = data.get("data") or data.get("models") or []
            names = []
            for it in items:
                if isinstance(it, dict):
                    names.append(str(it.get("id") or it.get("name") or ""))
            result["models"] = [n for n in names if n][:200]
            result["ok"] = True
            result["detail"] = f"模型列表可用，共 {len(result['models'])} 个"
        except Exception as exc:  # noqa: BLE001
            try:
                messages = self._build_messages("connectivity", "只回复两个字符：ok")
                payload = self._payload(messages, json_mode=False, temperature=0, max_tokens=8)
                data = self._post(payload)
                content = self._content_of(data)
                result["ok"] = True
                result["detail"] = f"对话接口可用，返回: {content.strip()[:40]}"
            except Exception as exc2:  # noqa: BLE001
                result["ok"] = False
                result["detail"] = f"模型列表失败({exc})；对话接口失败({exc2})"
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    # -- 批量并行 --
    def map_parallel(self, fn: Callable, items: Sequence, *, workers: Optional[int] = None) -> list:
        """对多个独立请求做受限并发（批量合并仍是首选，并发用于剩余多批）。"""
        items = list(items)
        if not items:
            return []
        w = max(1, min(int(workers or self.settings.concurrency), len(items)))
        if w == 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=w) as pool:
            return list(pool.map(fn, items))
