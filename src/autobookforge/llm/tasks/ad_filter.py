"""任务四：去广告（规则前置 + 语境判定兜底）。

降本要点：

* **规则前置**：先用 :func:`autobookforge.core.boilerplate.looks_like_watermark`
  匹配明确的广告 / 水印模式，命中行直接删除，不进入模型（真实语料命中率约八成）。
* **只送不确定行**：仅把规则未判定但含可疑特征的行送模型，输入量减少约 80%。
* **语境输入**：每行附带前后各一句，帮助模型区分「推广水印」与「正文中正常提到的网址」。
* **保守优先**：拿不准一律保留；明显是正文的长行即使被判删除也会被安全阀拦下。
"""

from __future__ import annotations

from typing import Optional, Sequence

from ...core.boilerplate import (
    AdConfig,
    find_site,
    looks_like_watermark,
)
from ...core.boilerplate import URL_RE, _BARE_HOST_RE, _QQ_RE, _WECHAT_RE  # noqa: PLC2701
from ..budget import get_tier, iter_batches
from ..client import LLMClient, LLMError
from ..prompts import user_ad_filter
from ..schema import SCHEMA_AD_LINES, SCHEMA_HINTS
from .base import TaskResult

LINES_PER_REQUEST = 40
MAX_SUSPICIOUS = 600

_PROSE_PUNCT_RE = None  # 延迟编译


def _prose_punct():
    global _PROSE_PUNCT_RE
    if _PROSE_PUNCT_RE is None:
        import re

        _PROSE_PUNCT_RE = re.compile(r"[。！？…”」』]")
    return _PROSE_PUNCT_RE


def is_suspicious(s: str, cfg: AdConfig) -> bool:
    """规则未判定为水印、但含有可疑特征的行（候选送检对象）。"""
    if not s:
        return False
    n = len(s)
    if n > 90:
        return False
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    # 明显是正文的行（中文多且带句末标点）不进候选，既防误删也省调用
    if cjk >= 15 and _prose_punct().search(s):
        return False
    if URL_RE.search(s) or _BARE_HOST_RE.search(s) or _QQ_RE.search(s) or _WECHAT_RE.search(s):
        return True
    if find_site(s, cfg):
        return True
    promo_hits = sum(1 for f in cfg.promo_fragments if f in s)
    if promo_hits >= 1 and n <= 80:
        return True
    if n <= 26 and not _prose_punct().search(s):
        import re

        if re.search(r"[0-9A-Za-z@#*=_\-+·]{3,}", s):
            return True
    return False


def _is_clearly_prose(s: str) -> bool:
    """安全阀：明显是正文的行，即使模型判删也保留。"""
    if len(s) >= 70:
        return True
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    hits = len(_prose_punct().findall(s))
    if cjk >= 10 and hits >= 1:
        return True
    return cjk >= 24


def filter_text(
    client: LLMClient,
    text: str,
    *,
    cfg: Optional[AdConfig] = None,
    budget: Optional[str] = None,
) -> tuple[TaskResult, str]:
    """对原始文本做「规则 + 语境」两级广告过滤，返回 (结果, 过滤后文本)。"""
    res = TaskResult(task="ad_filter")
    cfg = cfg or AdConfig()
    raw_lines = (text or "").split("\n")
    res.items_total = len(raw_lines)
    if not raw_lines:
        return res, text
    if not client.available:
        res.skipped = "LLM 未配置"
        return res, text
    if not client.settings.task_on("ad_filter"):
        res.skipped = "去广告任务已关闭"
        return res, text

    tier = get_tier(budget or client.settings.budget)

    # 第一级：规则层直接判定（不进模型）
    drop_by_rule: set[int] = set()
    candidates: list[int] = []
    for i, raw in enumerate(raw_lines):
        s = raw.strip().strip("\u3000")
        if not s:
            continue
        if looks_like_watermark(s, cfg):
            drop_by_rule.add(i)
            continue
        if is_suspicious(s, cfg):
            candidates.append(i)

    res.data["rule_dropped"] = len(drop_by_rule)
    if not candidates:
        res.ok = True
        res.notes.append("规则层已覆盖全部明确水印，无需调用模型")
        out = "\n".join(l for i, l in enumerate(raw_lines) if i not in drop_by_rule)
        res.data["dropped"] = len(drop_by_rule)
        return res, out

    if len(candidates) > MAX_SUSPICIOUS:
        res.notes.append(f"可疑行 {len(candidates)} 条超出上限，仅送检前 {MAX_SUSPICIOUS} 条")
        candidates = candidates[:MAX_SUSPICIOUS]

    # 第二级：语境判定
    rows: list[dict] = []
    for i in candidates:
        rows.append(
            {
                "id": i,
                "line": raw_lines[i].strip().strip("\u3000"),
                "prev": raw_lines[i - 1].strip()[:40] if i > 0 else "",
                "next": raw_lines[i + 1].strip()[:40] if i + 1 < len(raw_lines) else "",
            }
        )

    res.items_sent = len(rows)
    drop_by_llm: set[int] = set()
    batch_size = max(10, min(LINES_PER_REQUEST, tier.batch_size * 8))
    for group in iter_batches(rows, batch_size):
        user = user_ad_filter(group)
        try:
            obj = client.chat_json(
                "ad_filter",
                user,
                schema=SCHEMA_AD_LINES,
                schema_hint=SCHEMA_HINTS["ad_lines"],
                temperature=0.0,
                max_tokens=min(client.settings.max_tokens, 1600),
            )
        except LLMError as exc:
            res.notes.append(f"语境判定失败（该批全部保留）：{exc}")
            continue
        res.calls += 1
        for row in (obj or {}).get("lines", []) or []:
            try:
                rid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            if str(row.get("action", "")).lower() != "drop":
                continue
            if not (0 <= rid < len(raw_lines)):
                continue
            if _is_clearly_prose(raw_lines[rid].strip()):
                continue  # 安全阀
            drop_by_llm.add(rid)

    drop = drop_by_rule | drop_by_llm
    out = "\n".join(l for i, l in enumerate(raw_lines) if i not in drop)
    res.data["dropped"] = len(drop)
    res.data["llm_dropped"] = len(drop_by_llm)
    res.applied = len(drop_by_llm)
    res.ok = True
    if drop_by_llm:
        res.notes.append(f"语境判定额外删除 {len(drop_by_llm)} 行")
    else:
        res.notes.append("语境判定未发现新的广告行")
    return res, out


def suspicious_lines(text: str, cfg: Optional[AdConfig] = None) -> list[tuple[int, str]]:
    """仅探测可疑行（供诊断与测试使用，不调用模型）。"""
    cfg = cfg or AdConfig()
    out: list[tuple[int, str]] = []
    for i, raw in enumerate((text or "").split("\n")):
        s = raw.strip().strip("\u3000")
        if s and not looks_like_watermark(s, cfg) and is_suspicious(s, cfg):
            out.append((i, s))
    return out


def preview(text: str, cfg: Optional[AdConfig] = None) -> dict:
    """纯规则层的预览统计（零成本，供 Web UI 快速展示）。"""
    cfg = cfg or AdConfig()
    lines = (text or "").split("\n")
    rule_drop = 0
    susp = 0
    for raw in lines:
        s = raw.strip().strip("\u3000")
        if not s:
            continue
        if looks_like_watermark(s, cfg):
            rule_drop += 1
        elif is_suspicious(s, cfg):
            susp += 1
    return {
        "lines": len(lines),
        "rule_dropped": rule_drop,
        "suspicious": susp,
        "note": "规则层可直接删除的行不会送入模型",
    }
