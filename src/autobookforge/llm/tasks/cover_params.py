"""任务二：个性化封面（程序化生成参数）。

模型只输出**设计参数**，由本地渲染器画图，全程零图片 API 调用。

降本要点：

* **摘要输入**：只送书名、作者、约 100 字简介、5 个核心关键词与可选风格提示；
  关键词由规则层从章节标题做 n-gram 词频统计得出，不发送正文。
* **结构化输出**：字段与取值集合在 prompt 中固定，配合字段校验把重试率压到极低。
* **本地渲染**：参数落到 :mod:`autobookforge.cover.generator` 的程序化渲染器，
  支持渐变 / 纯色 / 图案背景与几何 / 波浪 / 粒子 / 线条 / 网格装饰。
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from ..client import LLMClient, LLMError
from ..prompts import user_cover_params
from ..schema import SCHEMA_COVER, SCHEMA_HINTS
from ..summarize import book_intro, keywords_from_chapters
from .base import TaskResult

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_FAMILIES = ("serif", "sans", "mono", "display")
_DECOR = ("none", "geometric", "wave", "particle", "line", "grid")
_LAYOUTS = ("center", "top_heavy", "bottom_heavy", "split")
_BG_TYPES = ("gradient", "solid", "pattern")

# 与内置封面风格同源的中性默认值（LLM 输出不合法时使用）
DEFAULT_DESIGN: dict = {
    "style": "literary",
    "background": {"type": "gradient", "colors": ["#fbfaf6", "#f0e9d8"], "pattern": "none"},
    "title_font": {"family": "serif", "size": 148, "color": "#2b2922", "weight": 600},
    "author_font": {"family": "sans", "size": 54, "color": "#6e644a"},
    "decoration": {"type": "line", "position": "top"},
    "layout": "center",
    "accent": "#e0f247",
}

# 风格关键词 → 内置风格，供封面参数缺失时回退
_STYLE_HINTS = (
    ("科幻", ("星", "舰", "机甲", "宇宙", "银河", "未来", "纳米", "人工智能", "太空", "文明")),
    ("奇幻", ("魔", "龙", "剑", "魔法", "精灵", "咒", "神", "仙", "妖", "秘境")),
    ("商务", ("管理", "经济", "市场", "职场", "投资", "战略", "商业", "营销", "团队", "增长")),
    ("极简", ("笔记", "随想", "札记", "杂文", "语录", "短篇")),
)


def normalize_color(value: str, fallback: str = "#202220") -> str:
    """把颜色规整为 #rrggbb。"""
    v = str(value or "").strip()
    m = _HEX_RE.match(v)
    if not m:
        return fallback
    hexpart = m.group(1)
    if len(hexpart) == 3:
        hexpart = "".join(c * 2 for c in hexpart)
    return "#" + hexpart.lower()


def normalize_design(raw: dict) -> dict:
    """校验并规范化设计参数；缺失或非法字段回落到默认值。"""
    d = dict(DEFAULT_DESIGN)
    if not isinstance(raw, dict):
        return d

    style = str(raw.get("style") or d["style"]).strip()
    d["style"] = style or d["style"]

    bg = raw.get("background") if isinstance(raw.get("background"), dict) else {}
    bg_type = str(bg.get("type") or d["background"]["type"]).lower()
    colors = bg.get("colors")
    norm_colors: list[str] = []
    if isinstance(colors, list):
        for c in colors[:4]:
            if isinstance(c, str) and c.strip():
                norm_colors.append(normalize_color(c, d["background"]["colors"][0]))
    if not norm_colors:
        norm_colors = list(d["background"]["colors"])
    if len(norm_colors) == 1 and bg_type == "gradient":
        norm_colors.append(_shift(norm_colors[0], 0.12))
    pattern = str(bg.get("pattern") or "none").lower()
    d["background"] = {
        "type": bg_type if bg_type in _BG_TYPES else d["background"]["type"],
        "colors": norm_colors,
        "pattern": pattern if pattern in _DECOR else "none",
    }

    for key, default in (("title_font", d["title_font"]), ("author_font", d["author_font"])):
        src = raw.get(key) if isinstance(raw.get(key), dict) else {}
        fam = str(src.get("family") or default["family"]).lower()
        try:
            size = int(src.get("size") or default["size"])
        except (TypeError, ValueError):
            size = int(default["size"])
        size = max(60, min(260, size)) if key == "title_font" else max(24, min(120, size))
        out = {
            "family": fam if fam in _FAMILIES else default["family"],
            "size": size,
            "color": normalize_color(str(src.get("color") or default["color"]), default["color"]),
        }
        if key == "title_font":
            try:
                weight = int(src.get("weight") or default.get("weight", 600))
            except (TypeError, ValueError):
                weight = 600
            out["weight"] = max(300, min(900, weight))
        d[key] = out

    dec = raw.get("decoration") if isinstance(raw.get("decoration"), dict) else {}
    dec_type = str(dec.get("type") or d["decoration"]["type"]).lower()
    dec_pos = str(dec.get("position") or d["decoration"]["position"]).lower()
    d["decoration"] = {
        "type": dec_type if dec_type in _DECOR else d["decoration"]["type"],
        "position": dec_pos if dec_pos in ("top", "bottom", "left", "right", "center") else d["decoration"]["position"],
    }

    layout = str(raw.get("layout") or d["layout"]).lower()
    d["layout"] = layout if layout in _LAYOUTS else d["layout"]

    d["accent"] = normalize_color(str(raw.get("accent") or d["accent"]), d["accent"])
    return d


def _shift(hex_color: str, amount: float) -> str:
    """把颜色微调（用于只有单色时生成渐变终点）。"""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except Exception:
        return "#f2f2ef"
    k = 1.0 - abs(amount)
    r, g, b = (int(v * k) for v in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def guess_style_hint(keywords: Sequence[str], tags: Sequence[str] = ()) -> str:
    """规则层风格判断：从关键词与标签推断风格提示，避免让模型瞎猜。"""
    bag = " ".join(list(keywords or []) + list(tags or []))
    for style, words in _STYLE_HINTS:
        if any(w in bag for w in words):
            return style
    return ""


def generate_design(
    client: LLMClient,
    *,
    title: str,
    author: str = "",
    chapters: Sequence = (),
    tags: Sequence[str] = (),
    style_hint: str = "",
) -> TaskResult:
    """生成封面设计参数。返回 ``data['design']``（已规范化）。"""
    res = TaskResult(task="cover_params", items_total=1)
    if not client.available:
        res.skipped = "LLM 未配置"
        return res
    if not client.settings.task_on("cover_params"):
        res.skipped = "封面参数任务已关闭"
        return res

    keywords = keywords_from_chapters(chapters, topk=5) if chapters else []
    intro = book_intro(chapters, max_chars=100) if chapters else ""
    hint = style_hint or guess_style_hint(keywords, tags)

    user = user_cover_params(
        book_title=title,
        author=author,
        intro=intro,
        keywords=keywords,
        style_hint=hint,
    )
    try:
        obj = client.chat_json(
            "cover_params",
            user,
            schema=SCHEMA_COVER,
            schema_hint=SCHEMA_HINTS["cover_params"],
            temperature=0.7,
            max_tokens=min(client.settings.max_tokens, 700),
        )
    except LLMError as exc:
        res.notes.append(f"封面参数生成失败，回退内置风格：{exc}")
        res.data["design"] = dict(DEFAULT_DESIGN)
        return res

    res.calls = 1
    res.items_sent = 1
    design = normalize_design(obj or {})
    design["keywords"] = keywords
    design["style_hint"] = hint
    res.data["design"] = design
    res.applied = 1
    res.ok = True
    return res
