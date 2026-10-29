"""任务一：生成章节名（含空白章节补全）。

降本要点：

* **规则前置**：只有「标题为空」或「标题仅有序号」的章节进入候选，已有合格标题的章节
  一律不送模型。真实语料里这类章节通常只占少数，调用量因此大幅下降。
* **摘要输入**：每章只送 50 字摘要（首段 + 尾段 + 对话密集段），不送正文，
  输入量减少约 90%。
* **批量合并**：候选章节按档位批量合并为一次请求，而不是逐章调用。
* **兜底短路**：空白且正文不足 200 字的章节直接返回「第X章」，无需模型参与。
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from ..budget import BudgetTier, get_tier, iter_batches
from ..client import LLMClient, LLMError
from ..prompts import user_blank_titles, user_chapter_titles
from ..schema import SCHEMA_HINTS, SCHEMA_TITLES
from ..summarize import chapter_digest, style_note
from .base import TaskResult

# 仅有序号的标题：需要补写真正的标题
_WEAK_TITLE_RE = re.compile(
    r"^(?:第\s*[0-9零〇一二三四五六七八九十百千万两]{1,8}\s*[章回节部篇]"
    r"|[0-9]{1,4}\s*[、.．]?"
    r"|卷\s*[0-9一二三四五六七八九十]{1,4}"
    r"|Chapter\s*[0-9]{1,4}\s*[.．]?"
    r"|[0-9]{1,4}\.[0-9]{1,4})$",
    re.IGNORECASE,
)

# 有意义但极短的固定标题，不算弱标题
_MEANINGFUL_SHORT = {"序", "序章", "序言", "前言", "楔子", "引子", "尾声", "后记", "番外", "终章", "尾声"}

SHORT_CHAPTER_CHARS = 200  # 少于该字数时使用兜底标题
_TITLE_TAIL_PUNCT = re.compile(r"[。，、；：！？…\-\s]+$")
_TITLE_MAX = 34


def is_weak_title(title: str) -> bool:
    """标题是否仅有序号、缺少实义。"""
    t = (title or "").strip().strip("\u3000")
    if not t:
        return False
    if t in _MEANINGFUL_SHORT:
        return False
    if _WEAK_TITLE_RE.match(t):
        return True
    # 纯数字 / 纯符号
    if re.fullmatch(r"[0-9０-９]{1,4}", t):
        return True
    return False


def is_blank_title(title: str) -> bool:
    t = (title or "").strip().strip("\u3000")
    return not t


def _num_prefix(title: str, index: int) -> str:
    """从原标题里提取序号外壳，保证目录编号不丢。"""
    t = (title or "").strip()
    m = re.match(r"^(第\s*[0-9零〇一二三四五六七八九十百千万两]{1,8}\s*[章回节部篇])\s*", t)
    if m:
        return m.group(1) + " "
    m = re.match(r"^(卷\s*[0-9一二三四五六七八九十]{1,4})\s*", t)
    if m:
        return m.group(1) + " "
    m = re.match(r"^(Chapter\s*[0-9]{1,4}[.．]?)\s*", t, re.IGNORECASE)
    if m:
        return m.group(1) + " "
    m = re.match(r"^([0-9]{1,4}\.[0-9]{1,4})\s*", t)
    if m:
        return m.group(1) + " "
    return f"第{index + 1}章 "


def _normalize_title(raw: str) -> str:
    t = (raw or "").strip().strip("\u3000")
    t = t.strip("\"'“”‘’《》〈〉【】[]")
    t = _TITLE_TAIL_PUNCT.sub("", t)
    if len(t) > _TITLE_MAX:
        t = t[:_TITLE_MAX]
    return t


def _target_items(chapters: Sequence, indexes: Sequence[int]) -> list[dict]:
    items: list[dict] = []
    for i in indexes:
        ch = chapters[i]
        body = getattr(ch, "content", "") or ""
        items.append(
            {
                "index": int(getattr(ch, "index", i)),
                "digest": chapter_digest(body, max_chars=50),
                "chars": len(body),
            }
        )
    return items


def _style_reference(chapters: Sequence, *, limit: int = 24) -> list[str]:
    out: list[str] = []
    for ch in chapters:
        t = (getattr(ch, "title", "") or "").strip()
        if t and not is_weak_title(t) and not is_blank_title(t):
            out.append(t)
        if len(out) >= limit:
            break
    return out


def generate_titles(
    client: LLMClient,
    chapters: Sequence,
    *,
    book_title: str,
    author: str = "",
    tier: Optional[BudgetTier] = None,
    limit: int = 400,
) -> TaskResult:
    """为缺名 / 仅有序号的章节批量生成标题，就地写回 ``ch.title``。"""
    res = TaskResult(task="chapter_titles", items_total=len(chapters))
    if not chapters:
        res.skipped = "无章节"
        return res
    if not client.available:
        res.skipped = "LLM 未配置"
        return res

    settings = client.settings
    tier = tier or get_tier(settings.budget)

    weak_idx: list[int] = []
    blank_idx: list[int] = []
    for i, ch in enumerate(chapters):
        t = getattr(ch, "title", "") or ""
        if is_blank_title(t):
            blank_idx.append(i)
        elif is_weak_title(t):
            weak_idx.append(i)
    weak_idx = weak_idx[:limit]
    blank_idx = blank_idx[:limit]

    if not weak_idx and not blank_idx:
        res.skipped = "所有章节标题均已具备实义标题"
        res.ok = True
        return res

    ref = _style_reference(chapters)
    note = style_note(ref or [getattr(c, "title", "") for c in chapters])
    applied = 0

    # -- 空白章节：过短的直接兜底，不调用模型 --
    short_blank: list[int] = []
    real_blank: list[int] = []
    for i in blank_idx:
        body = getattr(chapters[i], "content", "") or ""
        if len(body) < SHORT_CHAPTER_CHARS:
            short_blank.append(i)
        else:
            real_blank.append(i)
    for i in short_blank:
        chapters[i].title = f"第{i + 1}章"
        applied += 1
    if short_blank:
        res.notes.append(f"{len(short_blank)} 个空白章节正文不足 {SHORT_CHAPTER_CHARS} 字，直接使用兜底标题")

    # -- 弱标题章节：生成实义标题 --
    if weak_idx and settings.task_on("chapter_titles"):
        items = _target_items(chapters, weak_idx)
        batches = list(iter_batches(items, tier.batch_size))
        res.items_sent += len(items)
        for group in batches:
            user = user_chapter_titles(
                book_title=book_title,
                author=author,
                existing_titles=ref,
                items=group,
            )
            try:
                obj = client.chat_json(
                    "chapter_titles",
                    user,
                    schema=SCHEMA_TITLES,
                    schema_hint=SCHEMA_HINTS["titles"],
                    temperature=0.6,
                    max_tokens=min(settings.max_tokens, 1200),
                )
            except LLMError as exc:
                res.notes.append(f"章节名生成失败（已跳过该批）：{exc}")
                continue
            res.calls += 1
            for row in (obj or {}).get("titles", []) or []:
                try:
                    idx = int(row.get("index"))
                except (TypeError, ValueError):
                    continue
                title = _normalize_title(row.get("title", ""))
                if not title:
                    continue
                pos = _index_to_pos(chapters, idx)
                if pos is None:
                    continue
                ch = chapters[pos]
                ch.title = _num_prefix(ch.title, pos) + title
                applied += 1
    elif weak_idx:
        res.notes.append("章节名生成任务已关闭，跳过 %d 个仅有序号的章节" % len(weak_idx))

    # -- 空白章节：走补全指令 --
    if real_blank and settings.task_on("blank_titles"):
        items = _target_items(chapters, real_blank)
        batches = list(iter_batches(items, tier.batch_size))
        res.items_sent += len(items)
        for bi, group in enumerate(batches):
            first_pos = real_blank[0]
            prev_title = ""
            next_title = ""
            if first_pos > 0:
                prev_title = getattr(chapters[first_pos - 1], "title", "") or ""
            if first_pos + 1 < len(chapters):
                next_title = getattr(chapters[first_pos + 1], "title", "") or ""
            user = user_blank_titles(
                book_title=book_title,
                prev_title=prev_title,
                next_title=next_title,
                style_note=note,
                items=group,
            )
            try:
                obj = client.chat_json(
                    "blank_titles",
                    user,
                    schema=SCHEMA_TITLES,
                    schema_hint=SCHEMA_HINTS["titles"],
                    temperature=0.5,
                    max_tokens=min(settings.max_tokens, 1200),
                )
            except LLMError as exc:
                res.notes.append(f"空白章节补全失败（已跳过该批）：{exc}")
                continue
            res.calls += 1
            for row in (obj or {}).get("titles", []) or []:
                try:
                    idx = int(row.get("index"))
                except (TypeError, ValueError):
                    continue
                pos = _index_to_pos(chapters, idx)
                if pos is None:
                    continue
                title = _normalize_title(row.get("title", ""))
                if not title:
                    continue
                ch = chapters[pos]
                ch.title = _num_prefix(ch.title or f"第{pos + 1}章", pos) + title
                applied += 1
    elif real_blank:
        res.notes.append("空白章节补全任务已关闭，跳过 %d 个空白章节" % len(real_blank))

    # 仍未拿到标题的空白章节兜底
    for i in blank_idx:
        if is_blank_title(getattr(chapters[i], "title", "")):
            chapters[i].title = f"第{i + 1}章"

    res.applied = applied
    res.ok = applied > 0
    res.data["generated"] = applied
    res.data["candidates"] = len(weak_idx) + len(blank_idx)
    # 若改送候选章节全文的估算字数（用于摘要输入的降本核算）
    res.data["baseline_chars"] = sum(
        len(getattr(chapters[i], "content", "") or "") for i in (weak_idx + blank_idx)
    )
    return res


def _index_to_pos(chapters: Sequence, index: int) -> Optional[int]:
    """按 ``Chapter.index`` 反查列表下标；越界时按位置回退。"""
    for i, ch in enumerate(chapters):
        if int(getattr(ch, "index", -1)) == index:
            return i
    if 0 <= index < len(chapters):
        return index
    return None
