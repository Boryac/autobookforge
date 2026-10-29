"""文本处理公共工具：中文数字归一化、标题判定、正则装载。"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional, Sequence

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
    "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {
    "十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
    "万": 10000, "亿": 100000000,
}

# 内置章节标题正则（当外部 YAML 缺失时使用）
DEFAULT_VOLUME_PATTERNS: tuple[str, ...] = (
    r"^第[0-9零〇一二三四五六七八九十百千两]+[卷部篇][\s　:：].{0,40}$",
    r"^Part\s+[0-9IVXLC]+[:：\s].{0,60}$",
)
DEFAULT_CHAPTER_PATTERNS: tuple[str, ...] = (
    r"^第[0-9零〇一二三四五六七八九十百千两]+[章回][\s　:：]?.{0,60}$",
    r"^[0-9]+[、.．][\s　]?.{1,40}$",
    r"^Chapter\s+[0-9]+[:：\s].{0,80}$",
    r"^序[章言]?[\s　:：].{0,40}$",
    r"^后记$",
    r"^尾声$",
)
DEFAULT_SECTION_PATTERNS: tuple[str, ...] = (
    r"^[0-9]+\.[0-9]+[\s　].{0,50}$",
    r"^[（(][0-9零〇一二三四五六七八九十]+[）)][\s　]?.{0,50}$",
)

# 句末标点（决定“断行是否可合并”）
SENTENCE_END_CHARS = set("。！？…!?；;") | set("”』」》】）)\"'")
# 零宽与不可见字符
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u00ad"


def cn_to_arabic(text: str) -> Optional[int]:
    """把中文数字（含简繁、单位）转为整数；无法解析返回 None。"""
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in ("零", "〇"):
        return 0

    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                if number == 0:
                    number = 1  # 「十三」= 1*10 + 3
                section += number * unit
            number = 0
        else:
            return None
    return total + section + number


def extract_number(text: str) -> Optional[int]:
    """从标题中提取首个数字（阿拉伯或中文）。"""
    m = re.search(r"\d+", text)
    if m:
        return int(m.group())
    m = re.search(r"[零〇一二三四五六七八九十百千两壹贰叁肆伍陆柒捌玖拾佰仟萬万]+", text)
    if m:
        return cn_to_arabic(m.group())
    return None


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern]:
    out: list[re.Pattern] = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out


def matches_any(line: str, patterns: Sequence[re.Pattern]) -> bool:
    s = line.strip()
    if not s:
        return False
    return any(pat.match(s) for pat in patterns)


def is_heading(line: str, patterns: Sequence[re.Pattern]) -> bool:
    return matches_any(line, patterns)


def load_patterns_from_yaml(path: str) -> dict[str, list[str]]:
    """从 chapter_patterns.yaml 读取三类正则。失败时返回空字典。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for key in ("volume_patterns", "chapter_patterns", "section_patterns"):
        val = data.get(key)
        if isinstance(val, list):
            result[key] = [str(x) for x in val]
    constraints = data.get("constraints")
    if isinstance(constraints, dict):
        result["constraints"] = constraints  # type: ignore[assignment]
    return result
