"""Token 预算档位与文本切块。

三档预算（对应 PRD 的降本策略）：

========  ==========  ==========  ========  ====================================
档位       分块字数     批量块数     重叠比例   适用文本
========  ==========  ==========  ========  ====================================
economy   500         10          10%       网文、通俗小说
standard  300         5           15%       一般文学作品（默认）
fine      200         3           20%       严肃文学、学术文本
========  ==========  ==========  ========  ====================================

切块严格按句子边界进行，块之间保留重叠区用于上下文校验；重叠区不重复输出修改点，
由 :mod:`autobookforge.llm.tasks.proofread` 负责按坐标去重。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Sequence

# 句末标点（含中英文）
_SENT_END = "。！？…；!?;"
_PARA_SPLIT = re.compile(r"\n+")


@dataclass(frozen=True)
class BudgetTier:
    """单档预算参数。"""

    name: str
    label: str
    chunk_size: int
    batch_size: int
    overlap: float
    description: str = ""

    @property
    def overlap_chars(self) -> int:
        return max(20, int(self.chunk_size * self.overlap))

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "chunk_size": self.chunk_size,
            "batch_size": self.batch_size,
            "overlap": self.overlap,
            "overlap_chars": self.overlap_chars,
            "description": self.description,
        }


TIERS: dict[str, BudgetTier] = {
    "economy": BudgetTier(
        "economy", "经济", 500, 10, 0.10, "适合网文与通俗小说，调用次数最少"
    ),
    "standard": BudgetTier(
        "standard", "标准", 300, 5, 0.15, "适合一般文学作品，质量与成本均衡"
    ),
    "fine": BudgetTier(
        "fine", "精细", 200, 3, 0.20, "适合严肃文学与学术文本，上下文校验最充分"
    ),
}

DEFAULT_TIER = "standard"


def get_tier(name: str | None) -> BudgetTier:
    """取档位；未知名称回退到标准档。"""
    return TIERS.get((name or "").strip().lower(), TIERS[DEFAULT_TIER])


def tier_names() -> tuple[str, ...]:
    return tuple(TIERS.keys())


# --------------------------------------------------------------------------- #
# 切块
# --------------------------------------------------------------------------- #


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句，保留标点；视换行为强边界。"""
    out: list[str] = []
    for para in _PARA_SPLIT.split(text or ""):
        if not para:
            continue
        buf = ""
        for ch in para:
            buf += ch
            if ch in _SENT_END:
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return out


@dataclass
class TextChunk:
    """一个待处理的文本块。"""

    index: int
    text: str
    start: int = 0  # 相对所属章节正文的起始偏移
    end: int = 0


def iter_chunks(text: str, tier: BudgetTier, *, base_offset: int = 0) -> list[TextChunk]:
    """把文本按档位切成带重叠的块，块边界对齐句子。

    ``base_offset`` 用于把块坐标换算到更大范围（例如整章）的绝对偏移。
    """
    text = text or ""
    if not text:
        return []
    if len(text) <= tier.chunk_size:
        return [TextChunk(0, text, base_offset, base_offset + len(text))]

    sentences = _split_sentences(text)
    chunks: list[TextChunk] = []
    cur: list[str] = []
    cur_len = 0
    pos = 0
    idx = 0
    for s in sentences:
        cur.append(s)
        cur_len += len(s)
        if cur_len >= tier.chunk_size:
            body = "".join(cur)
            chunks.append(TextChunk(idx, body, base_offset + pos, base_offset + pos + len(body)))
            idx += 1
            pos += len(body) - 0  # 下一块起点（随后回退重叠量）
            # 构造重叠：保留尾部若干句作为下一块前缀
            overlap: list[str] = []
            olen = 0
            for prev in reversed(cur):
                if olen >= tier.overlap_chars:
                    break
                overlap.insert(0, prev)
                olen += len(prev)
            cur = list(overlap)
            cur_len = olen
            pos = max(0, pos - olen)
    if cur:
        body = "".join(cur)
        if body.strip():
            chunks.append(TextChunk(idx, body, base_offset + pos, base_offset + pos + len(body)))
    return chunks


def iter_batches(items: Sequence, batch_size: int) -> Iterator[list]:
    """把列表按批量大小分组（批量合并：一次请求处理多块）。"""
    size = max(1, int(batch_size))
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def merge_small_chapters(chapters: Sequence, tier: BudgetTier) -> list[list[int]]:
    """把若干相邻小章节合并到一次请求，避免逐章调用。

    返回分组后的章节下标列表；分组以「总字数不超过 batch_size 倍块长」为上限。
    """
    limit = tier.chunk_size * tier.batch_size
    groups: list[list[int]] = []
    cur: list[int] = []
    cur_len = 0
    for i, ch in enumerate(chapters):
        n = len(getattr(ch, "content", "") or "")
        if cur and (cur_len + n > limit or len(cur) >= tier.batch_size):
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(i)
        cur_len += n
    if cur:
        groups.append(cur)
    return groups
