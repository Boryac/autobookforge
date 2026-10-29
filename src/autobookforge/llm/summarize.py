"""规则层摘要与关键词提取（零成本，不调用任何模型）。

「摘要输入」是七大降本机制之一：章节名生成与封面参数任务只需摘要、关键词与统计信息，
把整章正文送进模型会让输入量高出十倍以上。本模块用纯规则把章节压成 50 字左右的
摘要（首段 + 尾段 + 对话密集段），并从章节标题做 n-gram 词频统计抽取核心关键词，
全部在本地毫秒级完成。

摘要质量由 :func:`chapter_digest` 的结构保证——网文章节的信息量高度集中在开头场景、
结尾钩子与会话密集处，取这三处即可近似概括本章。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional, Sequence

_FULLWIDTH = "\u3000"
_PARA_SPLIT = re.compile(r"\n\s*\n")
_QUOTE_RE = re.compile(r"[“”\"『』「」]")
_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[0-9零〇一二三四五六七八九十百千万两]+\s*[章回节卷部篇]|"
    r"[0-9]{1,4}\s*[、.．]|Chapter\s*\d+|卷[0-9一二三四五六七八九十]+)"
)

# 高频虚词 / 停用字：出现于 n-gram 中含这些字即过滤
_STOP_CHARS = set(
    "的了是在和就都而及与着或一个上也很到说要去你会最不之其为把被这那我你他她它们"
    "有些只可没还但所从对能下里中前后内外时年月日天说道问看想知觉得过起来出地得"
    "么呢吧啊呀哦嗯啦哈之乎者也都又再很太更只便才即如此多少"
)
_PUNCT = set("，。、；：！？…—－·「」『』“”\"'（）()《》〈〉【】[]{}<>~`!@#$%^&*_+=|\\/：;,.\t\r\n ")


def _clean(text: str) -> str:
    s = (text or "").replace(_FULLWIDTH, " ").strip()
    s = re.sub(r"[ \t]+", " ", s)
    return s


def _paragraphs(text: str) -> list[str]:
    raw = _PARA_SPLIT.split(text or "")
    out: list[str] = []
    for p in raw:
        s = _clean(p)
        if s:
            out.append(s)
    return out


def _is_heading_like(s: str) -> bool:
    return bool(_HEADING_RE.match(s)) and len(s) <= 40


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def chapter_digest(text: str, *, max_chars: int = 50, index: Optional[int] = None) -> str:
    """把章节正文压成 50 字左右的摘要。

    取首段（场景）、尾段（钩子）、对话最密集段三者拼接；不足则逐级降级。
    """
    paras = _paragraphs(text)
    if not paras:
        return ""

    head = ""
    for p in paras[:3]:
        if _is_heading_like(p) and len(p) <= 24:
            continue
        head = p
        break
    if not head:
        head = paras[0]

    tail = paras[-1] if len(paras) > 1 else ""
    if _is_heading_like(tail) and len(tail) <= 24:
        tail = paras[-2] if len(paras) > 2 else ""

    quote_best = ""
    if len(paras) > 2:
        best = max(paras[1:-1], key=lambda p: len(_QUOTE_RE.findall(p)), default="")
        if len(_QUOTE_RE.findall(best)) >= 2:
            quote_best = best

    budget_each = max(10, max_chars // 3)
    parts = [_truncate(head, budget_each)]
    if tail and tail != head:
        parts.append(_truncate(tail, budget_each))
    if quote_best and quote_best not in (head, tail):
        parts.append(_truncate(quote_best, budget_each))

    digest = " ".join(p for p in parts if p)
    if len(digest) > max_chars:
        digest = digest[:max_chars].rstrip() + "…"
    return digest


def book_intro(chapters: Sequence, *, max_chars: int = 100) -> str:
    """从章节标题与首章内容生成约 100 字的内容简介（封面参数任务的输入）。"""
    if not chapters:
        return ""
    titles = [getattr(c, "title", "") for c in chapters[:40]]
    titles = [t for t in titles if t]
    first_body = getattr(chapters[0], "content", "") if chapters else ""
    body_digest = chapter_digest(first_body, max_chars=60)
    head = "、".join(titles[:8])
    intro = f"{head}。{body_digest}"
    if len(intro) > max_chars:
        intro = intro[:max_chars].rstrip() + "…"
    return intro


def _ngrams(sentence: str, n: int) -> Iterable[str]:
    for i in range(len(sentence) - n + 1):
        yield sentence[i : i + n]


def _is_valid_gram(g: str) -> bool:
    if any(ch in _STOP_CHARS for ch in g):
        return False
    if any(ch in _PUNCT for ch in g):
        return False
    if not re.search(r"[\u4e00-\u9fff]", g):
        return False
    if re.fullmatch(r"[0-9]+", g):
        return False
    return True


def extract_keywords(texts: Sequence[str], *, topk: int = 5, min_len: int = 2, max_len: int = 4) -> list[str]:
    """按 n-gram 词频抽关键词（无分词依赖）。texts 推荐传章节标题集合。"""
    counter: Counter[str] = Counter()
    for raw in texts:
        for sentence in re.split(r"[，。、；：！？…\s「」『』“”\"'（）()《》【】]+", raw or ""):
            s = _clean(sentence)
            if len(s) < min_len:
                continue
            for n in range(max_len, min_len - 1, -1):
                if len(s) < n:
                    continue
                for g in _ngrams(s, n):
                    if _is_valid_gram(g):
                        counter[g] += 1

    if not counter:
        return []

    # 打分：频次为主，长度略加权（长词信息量更高）
    scored = sorted(counter.items(), key=lambda kv: (kv[1] * (1.0 + 0.15 * len(kv[0])), len(kv[0])), reverse=True)

    picked: list[str] = []
    for word, _count in scored:
        if any(word in p or p in word for p in picked):
            continue
        picked.append(word)
        if len(picked) >= topk:
            break
    return picked


def keywords_from_chapters(chapters: Sequence, *, topk: int = 5) -> list[str]:
    """从章节标题（优先）与首章摘要中抽关键词。"""
    titles = [getattr(c, "title", "") or "" for c in chapters]
    kws = extract_keywords(titles, topk=topk)
    if len(kws) < topk:
        extra: list[str] = []
        for ch in list(chapters)[:6]:
            extra.append(chapter_digest(getattr(ch, "content", ""), max_chars=80))
        kws = list(dict.fromkeys(kws + extract_keywords(extra, topk=topk)))
    return kws[:topk]


def style_note(titles: Sequence[str], *, limit: int = 20) -> str:
    """描述全书标题风格，供空白章节补全任务使用。"""
    ts = [t for t in (titles or []) if t][:limit]
    if not ts:
        return "无参考标题，采用简洁的四到八字场景标题"
    lens = [len(t) for t in ts]
    avg = sum(lens) / len(lens)
    has_bracket = sum(1 for t in ts if re.match(r"^[第卷章节回]", t))
    pattern = "以序号开头" if has_bracket > len(ts) * 0.6 else "以场景短语为主"
    tail_punct = sum(1 for t in ts if t and t[-1] in "。，、！？…")
    suffix = "末尾带标点" if tail_punct > len(ts) * 0.4 else "末尾不带标点"
    return f"平均 {avg:.0f} 字，{pattern}，{suffix}"
