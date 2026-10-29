"""任务三 / 六 / 七：精准校对、错别字修正、语句通顺性修正。

三个任务共用同一条管线，只有 system prompt 不同：

* ``proofread`` —— 错别字 + 标点 + 语法（默认，覆盖第六、七项能力）
* ``spelling``  —— 仅错别字（音近 / 形近）
* ``grammar``   —— 仅语句通顺性

降本要点：

* **修改点输出**：模型只回 ``original`` / ``replacement`` / ``type``，
  不重写整段，输出量减少约 85%。
* **分块 + 重叠**：按句子边界切块（档位决定块长），相邻块保留 10% - 20% 重叠用于
  上下文校验；重叠区产生的重复修改点在落库前按坐标去重。
* **批量合并**：一次请求处理多个文本块（档位决定批量数）。
* **坐标可追溯**：LLM 修改点被映射回「规则纠错前」的原文坐标，
  因此能与规则层 / 统计层修正一起进入同一份 diff 报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ...corrector.rule_engine import Correction
from ..budget import BudgetTier, get_tier, iter_batches, iter_chunks
from ..client import LLMClient, LLMError
from ..prompts import user_blocks, user_proofread
from ..schema import SCHEMA_FIXES, SCHEMA_HINTS
from .base import TaskResult

DEFAULT_MIN_CHARS = 40
DEFAULT_MAX_CHUNKS = 400
MAX_FIX_PER_BLOCK = 40
# 同一修改点在单个块内的最大落地处数：错别字常在同一段重复出现，
# 全部替换更符合「这个片段是错的」的语义，但设上限以避免异常放大。
MAX_SAME_FIX = 20


@dataclass
class _ChunkRef:
    gid: int
    chapter: int
    start: int
    end: int
    text: str


@dataclass
class Outcome:
    corrections_by_chapter: dict = field(default_factory=dict)
    applied: int = 0
    chunks: int = 0


# --------------------------------------------------------------------------- #
# 坐标映射（mid -> 原文）
# --------------------------------------------------------------------------- #


def _mid_to_orig(pos: int, rule_corrections: Sequence[Correction]) -> int:
    """把「规则纠错后」的偏移映射回「规则纠错前」的偏移。"""
    if not rule_corrections:
        return pos
    delta = 0
    for c in sorted(rule_corrections, key=lambda c: c.start):
        mid_start = c.start + delta
        mid_end = mid_start + len(c.replacement)
        if pos <= mid_start:
            break
        if pos <= mid_end:
            return c.start
        delta += len(c.replacement) - (c.end - c.start)
    return max(0, pos - delta)


def map_to_original(
    corrections: Sequence[Correction],
    rule_corrections: Sequence[Correction],
    base_text: str,
) -> list[Correction]:
    """把 LLM 修改点映射到规则纠错前的原文坐标，使 diff 报告可对齐。"""
    out: list[Correction] = []
    for c in corrections:
        s = _mid_to_orig(c.start, rule_corrections)
        e = _mid_to_orig(c.end, rule_corrections)
        if e <= s:
            e = s + len(c.original)
        e = min(len(base_text), max(s, e))
        original = base_text[s:e] if 0 <= s < len(base_text) else c.original
        if not original:
            continue
        out.append(
            Correction(
                s,
                s + len(original),
                original,
                c.replacement,
                c.source,
                c.category,
                c.confidence,
                c.note,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 块选择
# --------------------------------------------------------------------------- #


def _select_chunks(chunks: list[_ChunkRef], *, per_chapter_limit: int, global_limit: int) -> list[_ChunkRef]:
    """块数超预算时按章均匀抽样（保持顺序），避免只处理全书开头。"""
    grouped: dict[int, list[_ChunkRef]] = {}
    for c in chunks:
        grouped.setdefault(c.chapter, []).append(c)
    picked: list[_ChunkRef] = []
    for _ci, group in grouped.items():
        if len(group) <= per_chapter_limit:
            picked.extend(group)
            continue
        step = len(group) / per_chapter_limit
        idxs = sorted({min(len(group) - 1, int(i * step)) for i in range(per_chapter_limit)})
        picked.extend(group[i] for i in idxs)
    picked.sort(key=lambda c: (c.chapter, c.start))
    return picked[:global_limit]


def _plan_replacements(
    chunk_text: str, fixes: Sequence[dict]
) -> list[tuple[int, str, str, str]]:
    """在一个块内定位修改点，返回 (块内偏移, 原文, 修正, 类型)。区间互不重叠。"""
    used: list[tuple[int, int]] = []
    out: list[tuple[int, str, str, str]] = []
    for f in list(fixes)[:MAX_FIX_PER_BLOCK]:
        orig = str(f.get("original") or "")
        repl = str(f.get("replacement") or "")
        ctype = str(f.get("type") or "校对")
        if not orig or orig == repl:
            continue
        search_from = 0
        hits = 0
        while hits < MAX_SAME_FIX:
            pos = chunk_text.find(orig, search_from)
            if pos < 0:
                break
            end = pos + len(orig)
            if all(pos >= ue or end <= us for us, ue in used):
                used.append((pos, end))
                out.append((pos, orig, repl, ctype))
                hits += 1
            search_from = pos + 1
    out.sort(key=lambda x: x[0])
    return out


def _merge_non_overlapping(
    items: Sequence[tuple[int, str, str, str]]
) -> list[tuple[int, str, str, str]]:
    """跨块合并：同一修改点去重，重叠区间只保留靠前者。"""
    seen: set[tuple[int, str, str]] = set()
    kept: list[tuple[int, str, str, str]] = []
    last_end = -1
    for pos, orig, repl, ctype in sorted(items, key=lambda x: (x[0], -len(x[1]))):
        key = (pos, orig, repl)
        if key in seen:
            continue
        seen.add(key)
        if pos < last_end:
            continue
        # 同一处被拆成多条（如「按耐」与「耐」）时，只保留最完整的一条；
        # 判断限定在相邻位置窗口内，避免把不同位置的同类修改一并丢弃。
        if any(
            orig in k_orig and abs(pos - k_pos) <= max(len(orig), len(k_orig))
            for k_pos, k_orig, _, _ in kept
        ):
            continue
        kept.append((pos, orig, repl, ctype))
        last_end = pos + len(orig)
    return kept


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def proofread_chapters(
    client: LLMClient,
    chapters: Sequence,
    *,
    base_corrections: Optional[dict] = None,
    base_texts: Optional[dict] = None,
    tier: Optional[BudgetTier] = None,
    mode: str = "proofread",
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> tuple[TaskResult, Outcome]:
    """对章节正文做校对，就地写回 ``ch.content``。

    ``base_corrections`` / ``base_texts`` 为规则层的修正与原文快照（按章节下标索引），
    用于把 LLM 修改点映射回原文坐标。
    """
    res = TaskResult(task=mode, items_total=len(chapters))
    outcome = Outcome()
    if not chapters:
        res.skipped = "无章节"
        return res, outcome
    if not client.available:
        res.skipped = "LLM 未配置"
        return res, outcome
    if not client.settings.task_on(mode):
        res.skipped = f"{mode} 任务已关闭"
        return res, outcome

    settings = client.settings
    tier = tier or get_tier(settings.budget)

    # 1) 建块
    all_chunks: list[_ChunkRef] = []
    gid = 0
    candidates = 0
    for ci, ch in enumerate(chapters):
        body = getattr(ch, "content", "") or ""
        if len(body.strip()) < min_chars:
            continue
        candidates += 1
        for ck in iter_chunks(body, tier):
            all_chunks.append(_ChunkRef(gid, ci, ck.start, ck.end, ck.text))
            gid += 1

    if not all_chunks:
        res.skipped = "无可校对内容"
        res.items_total = candidates
        return res, outcome

    per_chapter_limit = max(1, max_chunks // max(1, candidates))
    picked = _select_chunks(all_chunks, per_chapter_limit=per_chapter_limit, global_limit=max_chunks)
    if len(picked) < len(all_chunks):
        res.notes.append(
            f"块数超出预算（{len(all_chunks)} 块），已按章均匀抽样 {len(picked)} 块送检"
        )

    res.items_total = len(all_chunks)          # 全量块数
    res.items_sent = len(picked)
    # 若重写整段的估算字数（用于修改点输出的降本核算）
    res.data["baseline_chars"] = sum(len(c.text) for c in picked)

    # 2) 批量请求
    fixes_by_gid: dict[int, list[dict]] = {}

    def _run_group(group):
        payload = [{"id": c.gid, "text": c.text} for c in group]
        user = user_proofread(payload) if mode == "proofread" else user_blocks(mode, payload)
        try:
            return client.chat_json(
                mode,
                user,
                schema=SCHEMA_FIXES,
                schema_hint=SCHEMA_HINTS["fixes"],
                temperature=0.1,
                max_tokens=min(settings.max_tokens, 2400),
            )
        except Exception as exc:  # noqa: BLE001
            return {"__error__": str(exc)}

    groups = list(iter_batches(picked, tier.batch_size))
    fail_streak = 0
    for obj in client.map_parallel(_run_group, groups):
        if not isinstance(obj, dict):
            continue
        if obj.get("__error__"):
            res.notes.append(f"批次校对失败（已跳过）：{obj['__error__']}")
            fail_streak += 1
            if fail_streak >= 3:
                res.notes.append("连续多个批次失败，已中止该任务的剩余请求")
                break
            continue
        fail_streak = 0
        res.calls += 1
        for blk in (obj.get("blocks", []) or []):
            try:
                bid = int(blk.get("id"))
            except (TypeError, ValueError):
                continue
            fixes = blk.get("fixes")
            if isinstance(fixes, list) and fixes:
                fixes_by_gid.setdefault(bid, []).extend(fixes)

    if not fixes_by_gid:
        res.ok = True
        res.notes.append("模型未返回需要修正的内容")
        return res, outcome

    # 3) 按块定位 -> 章内绝对坐标
    per_chapter_items: dict[int, list[tuple[int, str, str, str]]] = {}
    for c in picked:
        fixes = fixes_by_gid.get(c.gid)
        if not fixes:
            continue
        for pos, orig, repl, ctype in _plan_replacements(c.text, fixes):
            per_chapter_items.setdefault(c.chapter, []).append((c.start + pos, orig, repl, ctype))

    # 4) 逐章落库
    total_applied = 0
    for ci, items in per_chapter_items.items():
        ch = chapters[ci]
        merged = _merge_non_overlapping(items)
        if not merged:
            continue
        text = ch.content or ""
        corrections: list[Correction] = []
        for pos, orig, repl, ctype in merged:
            if pos >= len(text) or text[pos : pos + len(orig)] != orig:
                # 文本已被前一处替换影响（理论上不会，替换按倒序执行），保守跳过
                found = text.find(orig)
                if found < 0:
                    continue
                pos = found
            corrections.append(
                Correction(
                    pos,
                    pos + len(orig),
                    orig,
                    repl,
                    "llm",
                    ctype or "校对",
                    0.9,
                    f"大模型{'校对' if mode == 'proofread' else ('纠错' if mode == 'spelling' else '语法修正')}",
                )
            )
        if not corrections:
            continue
        # 倒序替换，避免偏移
        new_text = text
        for c in sorted(corrections, key=lambda c: c.start, reverse=True):
            new_text = new_text[: c.start] + c.replacement + new_text[c.end :]
        ch.content = new_text

        # 5) 坐标映射回规则纠错前的原文，统一进报告
        rule_corr = (base_corrections or {}).get(ci) or []
        base_text = (base_texts or {}).get(ci) or text
        mapped = map_to_original(corrections, rule_corr, base_text) if rule_corr else corrections
        outcome.corrections_by_chapter[ci] = mapped
        total_applied += len(mapped)

    outcome.applied = total_applied
    outcome.chunks = len(picked)
    res.applied = total_applied
    res.ok = total_applied > 0
    return res, outcome
