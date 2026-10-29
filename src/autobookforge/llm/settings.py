"""LLM 设置：读取、校验、持久化。

配置来源与优先级（后者覆盖前者）：

  1. 代码内置默认值
  2. ``configs/llm.yaml``         项目默认模板（可提交、不含密钥）
  3. ``configs/llm.local.json``   用户在 Web 设置页填写并保存（含 API Key，勿提交）
  4. 环境变量 ``ABF_LLM_*``       容器 / CI 场景的最终覆盖

API 地址、API Key、模型名称全部由用户自行填写；未填写时 LLM 层整体不启用，
流水线回落到纯本地 CPU 路径（零成本模式不受影响）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .budget import DEFAULT_TIER, tier_names

# 配置路径
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = _PROJECT_ROOT / "configs"
DEFAULT_YAML_PATH = CONFIG_DIR / "llm.yaml"
LOCAL_JSON_PATH = CONFIG_DIR / "llm.local.json"

# 七项任务（chapter_titles 内含空白补全；spelling / grammar 为精准校对的单独调用模式）
TASK_IDS: tuple[str, ...] = (
    "chapter_titles",
    "blank_titles",
    "cover_params",
    "proofread",
    "ad_filter",
    "toc",
    "spelling",
    "grammar",
)

TASK_LABELS: dict[str, str] = {
    "chapter_titles": "生成章节名",
    "blank_titles": "空白章节名补全",
    "cover_params": "个性化封面参数",
    "proofread": "精准校对",
    "ad_filter": "去广告兜底",
    "toc": "目录层级修正",
    "spelling": "错别字单独修正",
    "grammar": "语句通顺性修正",
}

TASK_DESCRIPTIONS: dict[str, str] = {
    "chapter_titles": "为缺失或仅有序号的章节补写标题（批量一次请求）",
    "blank_titles": "章节标题为空时按前后章节风格补全",
    "cover_params": "分析内容后输出程序化封面设计参数，本地渲染，不消耗图片 API",
    "proofread": "修正错别字、标点、语法，以修改点方式返回，不重写整段",
    "ad_filter": "仅对规则层无法判定的可疑行做语境判定",
    "toc": "默认关闭。仅当章节标题明显错乱时启用，修正层级与错别字",
    "spelling": "仅修正形近 / 音近错别字，不动语法标点",
    "grammar": "仅修正语序错误、成分残缺、搭配不当",
}

# 默认任务开关（toc 依需求默认关闭）
DEFAULT_TASK_SWITCH: dict[str, bool] = {
    "chapter_titles": True,
    "blank_titles": True,
    "cover_params": True,
    "proofread": True,
    "ad_filter": True,
    "toc": False,
    "spelling": False,
    "grammar": False,
}

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# 推理型模型的思考开关写法（不同服务端字段名不同，auto 表示不干预）
THINKING_MODES: tuple[str, ...] = (
    "auto",
    "off_all",
    "off_vllm",
    "off_openai",
    "off_reasoning",
)

THINKING_LABELS: dict[str, str] = {
    "auto": "不干预（默认）",
    "off_all": "关闭思考：全部写法都发（推荐先试这个）",
    "off_vllm": "关闭思考：chat_template_kwargs（vLLM / Ollama / LM Studio）",
    "off_openai": "关闭思考：enable_thinking + thinking.type（OpenAI 兼容 / Qwen）",
    "off_reasoning": "关闭思考：reasoning_effort=none（部分 OpenAI 兼容端点）",
}

THINKING_HINT = (
    "若模型把思考过程计入 max_tokens 导致返回空内容，请选择一种关闭思考的写法；"
    "不确定端点类型时先试『全部写法都发』，报 400 时会自动降级重试。"
)

_ENV_MAP: dict[str, str] = {
    "ABF_LLM_ENABLED": "enabled",
    "ABF_LLM_BASE_URL": "base_url",
    "ABF_LLM_API_KEY": "api_key",
    "ABF_LLM_MODEL": "model",
    "ABF_LLM_BUDGET": "budget",
    "ABF_LLM_TEMPERATURE": "temperature",
    "ABF_LLM_TIMEOUT": "timeout",
    "ABF_LLM_MAX_TOKENS": "max_tokens",
    "ABF_LLM_CONCURRENCY": "concurrency",
    "ABF_LLM_THINKING": "thinking",
}

# 需求文档中的「统一降本设计」，用于设置页与报告展示
COST_STRATEGIES: tuple[dict, ...] = (
    {"id": "batch", "name": "批量合并", "effect": "调用次数减少 50% - 80%"},
    {"id": "schema", "name": "结构化输出", "effect": "重试率降低约 90%"},
    {"id": "patches", "name": "修改点输出", "effect": "输出量减少约 85%"},
    {"id": "digest", "name": "摘要输入", "effect": "输入量减少约 90%"},
    {"id": "rules_first", "name": "规则前置", "effect": "LLM 调用量减少约 80%"},
    {"id": "prefix", "name": "前缀缓存", "effect": "重复前缀成本降低 50% - 90%"},
)


def _as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class LLMSettings:
    """LLM 层设置。"""

    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: int = 60
    retries: int = 2
    concurrency: int = 4
    budget: str = DEFAULT_TIER
    json_mode: bool = True          # 请求 response_format=json_object
    prefix_cache: bool = True       # 固定 system 前缀并标注缓存断点
    use_cache: bool = True          # 本地结果缓存
    thinking: str = "auto"          # 推理型模型的思考开关写法
    tasks: dict = field(default_factory=lambda: dict(DEFAULT_TASK_SWITCH))

    # -- 派生 --
    @property
    def configured(self) -> bool:
        """是否具备可调用条件（地址 + 密钥 + 模型）。"""
        return bool(self.api_key.strip() and self.base_url.strip() and self.model.strip())

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.configured)

    def task_on(self, task_id: str) -> bool:
        if not self.active:
            return False
        return bool(self.tasks.get(task_id, DEFAULT_TASK_SWITCH.get(task_id, False)))

    def chat_url(self) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def models_url(self) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        return base + "/models"

    # -- 序列化 --
    def as_dict(self, *, masked: bool = True) -> dict:
        data = asdict(self)
        data["tasks"] = {k: bool(self.tasks.get(k, DEFAULT_TASK_SWITCH.get(k, False))) for k in TASK_IDS}
        if masked:
            data["api_key"] = mask_key(self.api_key)
            data["api_key_set"] = bool(self.api_key)
        data["active"] = self.active
        data["configured"] = self.configured
        data["chat_url"] = self.chat_url()
        data["budget_label"] = self.budget
        return data

    def as_saved_dict(self) -> dict:
        """落盘用的字典（含明文密钥，仅写入本地配置）。"""
        data = asdict(self)
        data["tasks"] = {k: bool(self.tasks.get(k, DEFAULT_TASK_SWITCH.get(k, False))) for k in TASK_IDS}
        return data


def mask_key(key: str) -> str:
    """密钥打码，供界面回显。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return k[0] + "****"
    return f"{k[:4]}****{k[-4:]}"


def _from_mapping(data: dict) -> dict:
    """把任意字典规整为合法字段值。"""
    out: dict = {}
    if not isinstance(data, dict):
        return out
    llm = data.get("llm") if isinstance(data.get("llm"), dict) else data
    if not isinstance(llm, dict):
        return out

    if "enabled" in llm:
        out["enabled"] = _as_bool(llm.get("enabled"), False)
    for key in ("base_url", "api_key", "model", "budget"):
        if llm.get(key) is not None:
            out[key] = str(llm.get(key))
    for key, cast, lo, hi in (
        ("temperature", float, 0.0, 2.0),
        ("timeout", int, 5, 600),
        ("max_tokens", int, 64, 32000),
        ("retries", int, 0, 8),
        ("concurrency", int, 1, 16),
    ):
        if llm.get(key) is not None:
            try:
                out[key] = max(lo, min(hi, cast(llm[key])))
            except (TypeError, ValueError):
                continue
    for key in ("json_mode", "prefix_cache", "use_cache"):
        if llm.get(key) is not None:
            out[key] = _as_bool(llm.get(key))
    if llm.get("thinking") is not None:
        tv = str(llm.get("thinking")).strip()
        out["thinking"] = tv if tv in THINKING_MODES else "auto"
    tasks = llm.get("tasks")
    if isinstance(tasks, dict):
        cur = dict(DEFAULT_TASK_SWITCH)
        for tid in TASK_IDS:
            if tid in tasks:
                cur[tid] = _as_bool(tasks[tid], cur[tid])
        out["tasks"] = cur
    return out


def load_settings(
    *,
    yaml_path: Optional[str] = None,
    local_path: Optional[str] = None,
    use_env: bool = True,
) -> LLMSettings:
    """按优先级合并配置。"""
    merged: dict = {}

    yp = Path(yaml_path) if yaml_path else DEFAULT_YAML_PATH
    if yp.exists():
        try:
            import yaml

            with open(yp, "r", encoding="utf-8") as fh:
                merged.update(_from_mapping(yaml.safe_load(fh) or {}))
        except Exception:
            pass

    lp = Path(local_path) if local_path else LOCAL_JSON_PATH
    if lp.exists():
        try:
            with open(lp, "r", encoding="utf-8") as fh:
                merged.update(_from_mapping(json.load(fh)))
        except Exception:
            pass

    if use_env:
        env_over: dict = {}
        for env_key, field_name in _ENV_MAP.items():
            raw = os.environ.get(env_key)
            if raw is None or raw == "":
                continue
            env_over[field_name] = raw
        if env_over:
            merged.update(_from_mapping(env_over))

    settings = LLMSettings()
    for k, v in merged.items():
        setattr(settings, k, v)

    if settings.budget not in tier_names():
        settings.budget = DEFAULT_TIER
    if settings.thinking not in THINKING_MODES:
        settings.thinking = "auto"
    return settings


def save_settings(settings: LLMSettings, *, local_path: Optional[str] = None) -> str:
    """把设置写入本地配置文件（含密钥），返回文件路径。"""
    lp = Path(local_path) if local_path else LOCAL_JSON_PATH
    lp.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if lp.exists():
        try:
            with open(lp, "r", encoding="utf-8") as fh:
                existing = json.load(fh) or {}
        except Exception:
            existing = {}
    payload = settings.as_saved_dict()
    # 若本次未提交新密钥（界面只回显打码串），保留原有密钥
    if not payload.get("api_key") and isinstance(existing.get("api_key"), str):
        payload["api_key"] = existing["api_key"]
    if not isinstance(existing.get("llm"), dict):
        existing = {"llm": existing.get("llm", {})} if "llm" in existing else {}
    existing_out = dict(existing)
    existing_out["llm"] = payload
    # 兼容两种读取方式：顶层字段与 llm 子节点
    for k, v in payload.items():
        existing_out[k] = v
    with open(lp, "w", encoding="utf-8") as fh:
        json.dump(existing_out, fh, ensure_ascii=False, indent=2)
    return str(lp)
