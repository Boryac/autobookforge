"""模块 3：章节识别与分章。

分章不再依赖单一静态正则，而是委托 :mod:`markers` 的「书内自适应标记族引擎」：

* 多族候选（第X章 / 裸数字 / ``1、`` / ``《一》`` / ``Chapter N.`` / 站点水印式 / 场景短题）
* 逐族单调性过滤 + 族间并集 + 邻近重复合并
* 卷 / 章 / 节三级层级组装
* 超长章内部二次切分

输出 :class:`BookSplit`，并附带质量指标（章长分布、覆盖率、序号单调率），
供报告与 UI 做异常预警。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from statistics import median
from typing import Optional, Sequence

from .markers import (
    CHAPTER,
    SECTION,
    VOLUME,
    Marker,
    MarkerFamily,
    default_families,
    discover,
    load_families_from_yaml,
    render_title,
)
from .textutil import (
    DEFAULT_CHAPTER_PATTERNS,
    DEFAULT_SECTION_PATTERNS,
    DEFAULT_VOLUME_PATTERNS,
    compile_patterns,
    matches_any,
)

MAX_TITLE_LEN = 90
EMPTY_BODY_EPS = 8


@dataclass
class Chapter:
    """单个章节。"""

    index: int
    title: str
    content: str
    level: int = 2  # 1=卷 2=章 3=节
    volume_title: Optional[str] = None
    number: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "content": self.content,
            "level": self.level,
            "volume_title": self.volume_title,
            "number": self.number,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Chapter":
        return cls(
            index=int(data.get("index", 0)),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            level=int(data.get("level", 2)),
            volume_title=data.get("volume_title"),
            number=data.get("number"),
        )


@dataclass
class BookSplit:
    """分章结果。"""

    title: str
    chapters: list[Chapter]
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)

    @property
    def total_chars(self) -> int:
        return sum(len(c.content) for c in self.chapters)

    @property
    def volume_titles(self) -> list[str]:
        seen: list[str] = []
        for c in self.chapters:
            if c.volume_title and c.volume_title not in seen:
                seen.append(c.volume_title)
        return seen

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "warnings": self.warnings,
            "meta": self.meta,
            "quality": self.quality,
            "chapters": [c.as_dict() for c in self.chapters],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BookSplit":
        return cls(
            title=str(data.get("title", "")),
            chapters=[Chapter.from_dict(c) for c in data.get("chapters", [])],
            warnings=[str(w) for w in data.get("warnings", [])],
            meta=dict(data.get("meta") or {}),
            quality=dict(data.get("quality") or {}),
        )

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "BookSplit":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def infer_title(text: str, fallback: str = "未命名书籍") -> str:
    """从正文前若干行推断书名（标记行与超长行跳过）。"""
    fams = compile_patterns(
        list(DEFAULT_VOLUME_PATTERNS) + list(DEFAULT_CHAPTER_PATTERNS) + list(DEFAULT_SECTION_PATTERNS)
    )
    for line in text.split("\n")[:12]:
        s = line.strip().strip("\u3000")
        if not s or len(s) > 40:
            continue
        if matches_any(s, fams):
            continue
        if re.fullmatch(r"[\d\s、.．]+", s):
            continue
        return s
    return fallback


def _split_marks(
    lines: Sequence[str],
    marks: Sequence[Marker],
) -> tuple[list[tuple[int, Marker]], str]:
    """把标记列表规整为 (行号, 标记) 列表，并返回首标记之前的正文。"""
    ordered = sorted(marks, key=lambda m: m.line)
    pairs = [(m.line, m) for m in ordered]
    first_line = pairs[0][0] if pairs else len(lines)
    preface = "\n".join(lines[:first_line]).strip()
    return pairs, preface


def _build_chapters(
    lines: Sequence[str],
    marks: Sequence[Marker],
    book_title: str,
    *,
    min_chapter_length: int,
    warnings: list[str],
) -> list[Chapter]:
    pairs, preface = _split_marks(lines, marks)
    pairs = [(ln, m) for ln, m in pairs if ln < len(lines)]
    if not pairs:
        return []

    chapters: list[Chapter] = []
    cur_volume: Optional[str] = None

    for idx, (line_no, mark) in enumerate(pairs):
        end = pairs[idx + 1][0] if idx + 1 < len(pairs) else len(lines)
        body = "\n".join(lines[line_no + 1 : end]).strip()

        if mark.kind == VOLUME:
            title = render_title(mark)
            cur_volume = title
            if body:
                chapters.append(Chapter(len(chapters), title, body, 1, None, mark.num))
            continue

        title = render_title(mark)
        if mark.kind == SECTION:
            chapters.append(Chapter(len(chapters), title, body, 3, cur_volume, mark.num))
        else:
            chapters.append(Chapter(len(chapters), title, body, 2, cur_volume, mark.num))

    # 前言归位：短前言独立成章（元信息已剥离，剩余多为序 / 致辞）
    if preface:
        _stripped = preface.strip()
        if len(_stripped) < 40 and not re.search(r"[。，、；：！？…,.!?;:]", _stripped):
            preface = ""
    if preface:
        rest_len = sum(len(c.content) for c in chapters)
        pref_title = book_title if (rest_len and len(preface) >= rest_len) else "前言"
        chapters.insert(0, Chapter(0, pref_title, preface, 2, None, None))
    return chapters


def _drop_empty_chapters(chapters: list[Chapter], warnings: list[str]) -> list[Chapter]:
    """丢弃空壳章（重复标注残留），把其标题补回上一章。"""
    out: list[Chapter] = []
    for ch in chapters:
        body_len = len(ch.content.strip())
        if body_len < EMPTY_BODY_EPS:
            # 空壳章（目录页 / 重复标注 / 站点分隔）直接丢弃。
            # 刻意不做「标题接续给下一章」——目录里的标题与被接章的标题常不同名，
            # 接续会把错误的标题按到正文首章上。
            if ch.content.strip() and out:
                prev = out[-1]
                prev.content = (prev.content + "\n\n" + ch.content).strip()
                warnings.append(f"章节《{ch.title}》正文过短，已并入上一章")
            elif ch.content.strip():
                out.append(ch)
            continue
        out.append(ch)
    return out


def _compute_quality(chapters: list[Chapter], total_chars: int, body_chars: int) -> dict:
    lengths = [len(c.content) for c in chapters]
    cov = min(1.0, (sum(lengths) / body_chars)) if body_chars else 0.0
    numbers = [c.number for c in chapters if c.number is not None]
    mono = 1.0
    if len(numbers) >= 2:
        ok = sum(1 for a, b in zip(numbers, numbers[1:]) if b >= a)
        mono = ok / (len(numbers) - 1)
    return {
        "chapters": len(chapters),
        "volumes": sum(1 for c in chapters if c.level == 1),
        "sections": sum(1 for c in chapters if c.level == 3),
        "min_len": min(lengths) if lengths else 0,
        "median_len": int(median(lengths)) if lengths else 0,
        "max_len": max(lengths) if lengths else 0,
        "coverage": round(cov, 4),
        "monotonic_ratio": round(mono, 4),
        "body_chars": body_chars,
    }


def _quality_warnings(quality: dict, warnings: list[str], *, max_chapter_length: int) -> None:
    chapters = quality.get("chapters", 0)
    if chapters <= 1 and quality.get("body_chars", 0) > 20000:
        warnings.append("仅识别到 1 章且体量较大，可能未发现该书的章节标记格式")
    if quality.get("max_len", 0) > max_chapter_length:
        warnings.append(
            f"存在超长章节（{quality['max_len']} 字），可能仍有未识别的章节标题"
        )
    if chapters >= 6 and quality.get("coverage", 1) < 0.6:
        warnings.append(f"章节覆盖率偏低（{quality['coverage']:.0%}），部分正文可能落在章节之外")
    if chapters >= 10 and quality.get("monotonic_ratio", 1) < 0.7:
        warnings.append("章节序号单调性偏低，可能存在目录页或重复标注")


def split_chapters(
    text: str,
    *,
    volume_patterns: Optional[Sequence[str]] = None,
    chapter_patterns: Optional[Sequence[str]] = None,
    section_patterns: Optional[Sequence[str]] = None,
    min_chapter_length: int = 100,
    max_chapter_length: int = 50000,
    detect_volume: bool = True,
    detect_section: bool = True,
    book_title: Optional[str] = None,
    families: Optional[Sequence[MarkerFamily]] = None,
) -> BookSplit:
    """把整书文本切分为章节结构。"""
    lines = text.split("\n")
    body_chars = len(text)
    title = book_title or infer_title(text)
    warnings: list[str] = []

    fams = list(families) if families else None
    if fams is None and (volume_patterns or chapter_patterns or section_patterns):
        custom: list[MarkerFamily] = []
        for pat in (volume_patterns or []):
            custom.append(MarkerFamily("custom_vol", VOLUME, 80, re.compile(pat), label="第{}卷"))
        for pat in (chapter_patterns or []):
            custom.append(MarkerFamily("custom_chap", CHAPTER, 76, re.compile(pat), label="第{}章"))
        for pat in (section_patterns or []):
            custom.append(MarkerFamily("custom_sec", SECTION, 56, re.compile(pat), label="第{}节"))
        if custom:
            custom.sort(key=lambda f: -f.priority)
            fams = custom

    found = discover(lines, families=fams, max_chapter_length=max_chapter_length)

    if not found.markers:
        warnings.append("未识别到章级标记，整书作为单章处理")
        chapters = [Chapter(0, title, text.strip(), 2)] if text.strip() else []
        quality = _compute_quality(chapters, len(text.strip()), body_chars)
        return BookSplit(title, chapters, warnings, quality=quality)

    chapters = _build_chapters(
        lines,
        found.markers,
        title,
        min_chapter_length=min_chapter_length,
        warnings=warnings,
    )
    chapters = _drop_empty_chapters(chapters, warnings)

    if not chapters:
        warnings.append("分章结果为空，整书作为单章处理")
        chapters = [Chapter(0, title, text.strip(), 2)]

    for i, ch in enumerate(chapters):
        ch.index = i

    quality = _compute_quality(chapters, body_chars, body_chars)
    if len(chapters) == 1 and quality["body_chars"] > 20000:
        warnings.append("仅识别到单一章节，建议检查章节标题格式")
    _quality_warnings(quality, warnings, max_chapter_length=max_chapter_length)
    warnings.extend(found.notes)

    return BookSplit(title, chapters, warnings, quality=quality)


def split_file(
    filepath: str,
    *,
    config_path: Optional[str] = None,
    book_title: Optional[str] = None,
    **overrides,
) -> BookSplit:
    """读取文件并分章；可从 YAML 配置加载正则族。"""
    from .cleaner import prepare
    from .encoding import detect_and_convert

    params: dict = {}
    fams: Optional[list[MarkerFamily]] = None
    if config_path:
        fams = load_families_from_yaml(config_path)
        loaded = _load_constraints(config_path)
        if isinstance(loaded, dict):
            params.setdefault("min_chapter_length", int(loaded.get("min_chapter_length", 100)))
            params.setdefault("max_chapter_length", int(loaded.get("max_chapter_length", 50000)))
    if fams:
        params["families"] = fams

    params.update(overrides)

    decoded = detect_and_convert(filepath)
    prepared = prepare(decoded.text, filename=os.path.basename(filepath))
    book = split_chapters(prepared.text, book_title=book_title, **params)
    book.meta = prepared.meta.as_dict()
    return book


def _load_constraints(config_path: str) -> dict:
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        import yaml

        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    constraints = data.get("constraints")
    return constraints if isinstance(constraints, dict) else {}
