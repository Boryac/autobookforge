"""模块 2：文本清洗与智能排版。

处理顺序（严格）：

  1. 统一换行符          ``\\r\\n`` / ``\\r`` -> ``\\n``
  2. 去噪              去除零宽字符、行内多余空白与行尾空白
  3. 元信息摘除        《书名》作者：xxx / 属性分类：xxx 等解析为元数据后移除
  4. 水印清除          站点行、URL 行、推广导流语整行删除；段内 URL / 句尾站点名就地剥离
  5. 标题保护          章节标记行与场景短标题行不参与断行合并、不加缩进
  6. 智能换行合并      以中文句末标点判定句子边界，合并被硬换行拆断的段落
  7. 首行缩进硬编码    段首写入两个全角空格（规避 Kindle 覆盖 CSS text-indent）

第 5 步是本模块与旧实现最关键的差异：旧版只按静态正则判断标题，导致 ``10、`` 这类
手打体标记被当作"断句"与下一段正文合并，标题因此被污染、章节随之错位。新版把标题判定
统一交给 :mod:`markers` 的标记族，与分章阶段使用同一套判据，两侧结论不会打架。

首行缩进硬编码的关键性：Kindle 转码会接管 CSS ``text-indent`` 导致缩进缩水，
而真实字符无法被吞掉。该方案已在 txt2ebook 等项目中被验证有效。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .boilerplate import AdConfig, BookMeta, looks_like_watermark, parse_meta, strip_inline_watermarks
from .markers import default_families
from .textutil import (
    DEFAULT_CHAPTER_PATTERNS,
    DEFAULT_VOLUME_PATTERNS,
    SENTENCE_END_CHARS,
    ZERO_WIDTH_CHARS,
    compile_patterns,
    matches_any,
)

FULLWIDTH_SPACE = "\u3000"
DEFAULT_INDENT = FULLWIDTH_SPACE * 2

# 兼容旧配置来源；实际水印判定已交由 boilerplate.looks_like_watermark
DEFAULT_AD_PATTERNS: tuple[str, ...] = (
    r".*下载器.*",
    r".*分卷阅读.*",
    r".*更多精彩.*",
    r".*请记住本书.*",
    r".*最新章节.*",
    r".*(www|http|https)\S*\..*",
    r".*[Qq][Qq]群?\s*[:：]?\s*\d+.*",
    r".*(笔趣阁|顶点小说|无错小说|全本小说|txt免费|手机阅读).*",
    r"^\s*[-=*_]{3,}\s*$",
)

_MULTI_SPACE = re.compile(r"[ \t\u00a0\u2000-\u200a]{2,}")
_MULTI_BLANK = re.compile(r"\n{3,}")
_SCENE_BAD_TAIL = re.compile(r"[。！？…；：，、]$")
_SCENE_BAD_ANY = re.compile(r"[。！？…；]")

_SCENE_FAMILY_NAME = "scene_title"


@dataclass
class CleanStats:
    """清洗过程统计，用于报告与测试断言。"""

    lines_in: int = 0
    lines_out: int = 0
    ads_removed: int = 0
    llm_ads_removed: int = 0
    inline_ads_stripped: int = 0
    meta_removed: int = 0
    headings_protected: int = 0
    blanks_collapsed: int = 0
    lines_merged: int = 0
    spaces_normalized: int = 0

    def as_dict(self) -> dict:
        return {
            "lines_in": self.lines_in,
            "lines_out": self.lines_out,
            "ads_removed": self.ads_removed,
            "llm_ads_removed": self.llm_ads_removed,
            "inline_ads_stripped": self.inline_ads_stripped,
            "meta_removed": self.meta_removed,
            "headings_protected": self.headings_protected,
            "blanks_collapsed": self.blanks_collapsed,
            "lines_merged": self.lines_merged,
            "spaces_normalized": self.spaces_normalized,
        }


def _ends_sentence(line: str) -> bool:
    s = line.rstrip()
    return bool(s) and s[-1] in SENTENCE_END_CHARS


def _normalize_inline_spaces(line: str, stats: CleanStats) -> str:
    new = _MULTI_SPACE.sub(" ", line)
    if new != line:
        stats.spaces_normalized += 1
    return new.strip()


def _looks_like_scene_title(s: str, prev_blank: bool, next_blank: bool) -> bool:
    """评书体 / 无序号分段的短标题行。"""
    if not (2 <= len(s) <= 26):
        return False
    if _SCENE_BAD_TAIL.search(s) or _SCENE_BAD_ANY.search(s):
        return False
    if s[0] in "。，、：；！？…（）()［］【】〔〕“”\"'《》〈〉":
        return False
    if not re.search(r"[\u4e00-\u9fff]", s):
        return False
    # 必须被空行完整夹住，否则会把硬换行拆出的半句当成标题（历史上曾导致标题污染）
    return prev_blank and next_blank


class HeadingJudge:
    """统一标题判据（与分章阶段同源）。"""

    def __init__(self, families=None) -> None:
        self._fams = list(families or default_families())

    def is_heading(self, line: str, prev_blank: bool, next_blank: bool) -> bool:
        s = line.strip()
        if not s or len(s) > 64:
            return False
        for fam in self._fams:
            if not fam.pattern.match(s):
                continue
            if fam.weak and not (prev_blank or next_blank):
                continue
            return True
        return _looks_like_scene_title(s, prev_blank, next_blank)


@dataclass
class PreparedText:
    """清洗结果（含元信息与标题标记）。"""

    text: str
    stats: CleanStats
    meta: BookMeta
    heading_lines: list[int] = field(default_factory=list)


def prepare(
    text: str,
    *,
    filename: str = "",
    smart_linebreak: bool = True,
    paragraph_indent: bool = True,
    remove_ads: bool = True,
    normalize_spaces: bool = True,
    ad_config: Optional[AdConfig] = None,
    meta: Optional[BookMeta] = None,
    indent: str = DEFAULT_INDENT,
    keep_all: bool = False,
) -> PreparedText:
    """完整清洗，返回文本 + 统计 + 元信息 + 标题行号。"""
    stats = CleanStats()
    cfg = ad_config or AdConfig()

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")

    raw_lines = text.split("\n")
    stats.lines_in = len(raw_lines)

    if meta is None:
        meta = parse_meta(raw_lines, filename, cfg=cfg)
    consumed = set(meta.consumed)

    # ---- 逐行筛选 ------------------------------------------------------- #
    kept: list[list] = []  # [orig_index, text]
    for i, raw in enumerate(raw_lines):
        if i in consumed:
            stats.meta_removed += 1
            continue
        s = raw.replace(FULLWIDTH_SPACE, FULLWIDTH_SPACE).strip()
        s = s.strip("\u3000 \t")
        if not s:
            kept.append([i, ""])
            continue
        if remove_ads and looks_like_watermark(s, cfg):
            stats.ads_removed += 1
            continue
        if remove_ads:
            stripped = strip_inline_watermarks(s, cfg)
            if stripped != s:
                stats.inline_ads_stripped += 1
                s = stripped
                if not s:
                    stats.ads_removed += 1
                    continue
        if normalize_spaces:
            s = _normalize_inline_spaces(s, stats)
        kept.append([i, s])

    # ---- 标题判定（基于筛选后的空行结构）-------------------------------- #
    judge = HeadingJudge()
    flags: list[bool] = []
    n = len(kept)
    for j in range(n):
        s = kept[j][1]
        if not s:
            flags.append(False)
            continue
        prev_blank = j == 0 or not kept[j - 1][1]
        next_blank = j == n - 1 or not kept[j + 1][1]
        flags.append(judge.is_heading(s, prev_blank, next_blank))
    stats.headings_protected = sum(1 for f in flags if f)

    # ---- 断行合并（标题行不参与）---------------------------------------- #
    merged: list[list] = []  # [text, is_heading]
    i = 0
    while i < n:
        s = kept[i][1]
        if not s:
            merged.append(["", False])
            i += 1
            continue
        if flags[i] or not smart_linebreak:
            merged.append([s, flags[i]])
            i += 1
            continue
        buf = s
        while (
            not _ends_sentence(buf)
            and i + 1 < n
            and kept[i + 1][1]
            and not flags[i + 1]
        ):
            i += 1
            buf = buf + kept[i][1]
            stats.lines_merged += 1
        merged.append([buf, False])
        i += 1

    # ---- 空行折叠 ------------------------------------------------------- #
    collapsed: list[list] = []
    prev_blank = True
    for s, is_head in merged:
        if s == "":
            if prev_blank:
                stats.blanks_collapsed += 1
                continue
            collapsed.append(["", False])
            prev_blank = True
        else:
            collapsed.append([s, is_head])
            prev_blank = False

    # ---- 首行缩进硬编码 ------------------------------------------------- #
    out_lines: list[str] = []
    heading_lines: list[int] = []
    for s, is_head in collapsed:
        if not s:
            out_lines.append("")
            continue
        if is_head:
            heading_lines.append(len(out_lines))
            out_lines.append(s)
        elif paragraph_indent and not keep_all:
            out_lines.append(indent + s)
        else:
            out_lines.append(s)

    result = _MULTI_BLANK.sub("\n\n", "\n".join(out_lines)).strip("\n")
    stats.lines_out = len([x for x in result.split("\n") if x])
    return PreparedText(result, stats, meta, heading_lines)


def clean_text(
    text: str,
    *,
    smart_linebreak: bool = True,
    paragraph_indent: bool = True,
    remove_ads: bool = True,
    normalize_spaces: bool = True,
    ad_patterns: Optional[Sequence[str]] = None,
    heading_patterns: Optional[Sequence[str]] = None,
    indent: str = DEFAULT_INDENT,
    filename: str = "",
    ad_config: Optional[AdConfig] = None,
    meta: Optional[BookMeta] = None,
) -> tuple[str, CleanStats]:
    """清洗文本，返回 (清洗后文本, 统计)。兼容旧签名。"""
    if ad_config is None and ad_patterns:
        ad_config = AdConfig(extra_patterns=[p for p in ad_patterns if isinstance(p, str)])
    prepared = prepare(
        text,
        filename=filename,
        smart_linebreak=smart_linebreak,
        paragraph_indent=paragraph_indent,
        remove_ads=remove_ads,
        normalize_spaces=normalize_spaces,
        ad_config=ad_config,
        meta=meta,
        indent=indent,
    )
    return prepared.text, prepared.stats


def clean_file(
    filepath: str,
    *,
    output_path: Optional[str] = None,
    **kwargs,
) -> tuple[str, CleanStats]:
    """读取文件（自动编码）并清洗，可选写回。"""
    from .encoding import detect_and_convert

    decoded = detect_and_convert(filepath)
    kwargs.setdefault("filename", os.path.basename(filepath))
    cleaned, stats = clean_text(decoded.text, **kwargs)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(cleaned)
    return cleaned, stats
