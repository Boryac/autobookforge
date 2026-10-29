"""按 LLM 输出的程序化设计参数渲染封面。

与 :mod:`autobookforge.cover.generator` 同源（同一套字体候选、噪点质感与纸张色系），
区别在于这里的一切都由设计参数驱动：背景类型与配色、标题 / 作者字体族与字重、
装饰元素与位置、版面布局，全部来自任务二的模型输出。全程本地绘制，零图片 API 调用。

设计参数结构见 :func:`autobookforge.llm.tasks.cover_params.normalize_design`。
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw

from .generator import (
    COVER_H,
    COVER_W,
    DEFAULT_LOGO,
    _add_grain,
    _lerp,
    _wrap_text,
    load_font,
)

_FONT_MONO = (
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)
_FONT_DISPLAY = (
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)


def hex_to_rgb(value: str, fallback: tuple = (32, 34, 32)) -> tuple:
    v = (value or "").strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return fallback
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return fallback


def _pick(kind: str) -> str:
    table = {"mono": _FONT_MONO, "display": _FONT_DISPLAY, "serif": (), "sans": ()}
    if kind in ("mono", "display"):
        for path in table[kind]:
            if os.path.exists(path):
                return path
    if kind == "serif":
        return "serif"
    if kind == "sans":
        return "regular"
    return "regular"


def _font(size: int, family: str, weight: int = 500):
    """按字体族与字重取字体。"""
    kind = _pick(family)
    if kind in ("serif", "regular"):
        if weight >= 700 and kind == "regular":
            return load_font(size, "bold")
        return load_font(size, kind)
    try:
        from PIL import ImageFont

        return ImageFont.truetype(kind, size)
    except Exception:
        return load_font(size, "regular")


# --------------------------------------------------------------------------- #
# 背景
# --------------------------------------------------------------------------- #


def _multicolor_gradient(size: tuple[int, int], colors: Sequence[tuple], vertical: bool = True) -> Image.Image:
    """多段线性渐变。"""
    w, h = size
    if not colors:
        colors = [(251, 250, 246), (240, 233, 216)]
    if len(colors) == 1:
        colors = [colors[0], _lerp(colors[0], (255, 255, 255), 0.12)]
    img = Image.new("RGB", (w, h), colors[0])
    draw = ImageDraw.Draw(img)
    segments = len(colors) - 1
    span = (h if vertical else w) // segments or 1
    for y in range(h):
        seg = min(segments - 1, y // span)
        t = (y - seg * span) / max(1, span)
        color = _lerp(colors[seg], colors[seg + 1], t)
        draw.line((0, y, w, y), fill=color)
    return img


def _decorate(img: Image.Image, kind: str, position: str, accent: tuple, ink: tuple, seed: int = 7) -> None:
    """绘制装饰元素（几何 / 波浪 / 粒子 / 线条 / 网格）。"""
    if kind in ("", "none", None):
        return
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    band = int(h * 0.22)
    if position == "top":
        top, bottom = 0, band
    elif position == "bottom":
        top, bottom = h - band, h
    elif position in ("left", "right"):
        top, bottom = 0, h
    else:
        top, bottom = int(h * 0.34), int(h * 0.66)

    soft = (accent[0], accent[1], accent[2], 46)
    mid = (accent[0], accent[1], accent[2], 90)
    deep = (ink[0], ink[1], ink[2], 26)

    if kind == "line":
        for i in range(9):
            y = top + int((bottom - top) * (i + 1) / 10)
            inset = int(w * (0.10 + 0.035 * i))
            draw.line((inset, y, w - inset, y), fill=soft if i % 3 else mid, width=max(1, int(w * 0.0016)))
    elif kind == "wave":
        for k in range(5):
            amp = h * (0.012 + 0.006 * k)
            phase = rnd.uniform(0, 6.28)
            base_y = top + (bottom - top) * (k + 1) / 6
            pts = []
            for x in range(0, w + 1, max(2, w // 220)):
                y = base_y + amp * math.sin(2 * math.pi * x / (w * 0.62) + phase)
                pts.append((x, y))
            draw.line(pts, fill=mid if k < 2 else soft, width=max(1, int(w * 0.0018)))
    elif kind == "particle":
        for _ in range(190):
            x = rnd.uniform(0, w)
            y = rnd.uniform(top, bottom)
            r = rnd.uniform(w * 0.0018, w * 0.0062)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=mid if rnd.random() < 0.25 else deep)
    elif kind == "grid":
        step = max(28, w // 26)
        for x in range(0, w + 1, step):
            draw.line((x, top, x, bottom), fill=deep, width=1)
        for y in range(top, bottom + 1, step):
            draw.line((0, y, w, y), fill=deep, width=1)
    elif kind == "geometric":
        for _ in range(6):
            cx = rnd.uniform(w * 0.1, w * 0.9)
            cy = rnd.uniform(top + band * 0.1, max(top + 1, bottom - band * 0.1))
            r = rnd.uniform(w * 0.03, w * 0.1)
            if rnd.random() < 0.5:
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=mid, width=max(2, int(w * 0.0022)))
            else:
                draw.polygon(
                    [(cx, cy - r), (cx + r, cy + r * 0.75), (cx - r, cy + r * 0.75)],
                    outline=mid,
                    width=max(2, int(w * 0.0022)),
                )


# --------------------------------------------------------------------------- #
# 主渲染
# --------------------------------------------------------------------------- #

_LAYOUTS = {
    "center": (0.36, "center"),
    "top_heavy": (0.20, "left"),
    "bottom_heavy": (0.54, "center"),
    "split": (0.30, "center"),
}


def render_design_cover(
    title: str,
    author: str = "",
    output_path: str = "cover.png",
    *,
    design: Optional[dict] = None,
    subtitle: str = "",
    logo_path: Optional[str] = None,
    width: int = COVER_W,
    height: int = COVER_H,
    seed: Optional[int] = None,
) -> str:
    """按设计参数渲染封面，返回输出路径。"""
    d = design or {}
    bg = d.get("background") or {}
    tf = d.get("title_font") or {}
    af = d.get("author_font") or {}
    dec = d.get("decoration") or {}

    colors = [hex_to_rgb(c) for c in (bg.get("colors") or ["#fbfaf6", "#f0e9d8"])]
    accent = hex_to_rgb(d.get("accent") or "#e0f247", (224, 242, 71))
    ink = hex_to_rgb(tf.get("color") or "#202220", (32, 34, 32))

    rnd_seed = seed if seed is not None else 7
    random.seed(rnd_seed)

    base = _multicolor_gradient((width, height), colors)

    # 图案打底（pattern 背景）
    pattern = str(bg.get("pattern") or "none")
    if str(bg.get("type")) == "pattern" and pattern not in ("none", ""):
        _decorate(base, pattern, "center", accent, ink, seed=rnd_seed + 3)

    base = _add_grain(base, strength=8.0, opacity=0.07)

    _decorate(base, str(dec.get("type") or "none"), str(dec.get("position") or "top"), accent, ink, seed=rnd_seed)

    draw = ImageDraw.Draw(base, "RGBA")
    margin = int(width * 0.12)
    inner = width - margin * 2

    layout = str(d.get("layout") or "center")
    start_frac, align = _LAYOUTS.get(layout, _LAYOUTS["center"])

    title_size = int(tf.get("size") or 148)
    title_size = max(70, min(260, int(title_size * width / 1600)))
    title_weight = int(tf.get("weight") or 600)
    title_font = _font(title_size, str(tf.get("family") or "serif"), title_weight)

    author_size = int(af.get("size") or 54)
    author_size = max(28, min(120, int(author_size * width / 1600)))
    author_font = _font(author_size, str(af.get("family") or "sans"), 500)
    sub_font = _font(int(author_size * 0.62), "sans", 400)
    brand_font = _font(int(width * 0.023), "sans", 400)

    # 顶部强调条
    band_h = max(4, int(height * 0.009))
    if layout in ("top_heavy", "split"):
        draw.rectangle((0, 0, width, band_h), fill=accent)

    title_lines = _wrap_text(title or "未命名书籍", title_font, inner, draw)
    line_h = int(title_size * 1.26)
    block_h = line_h * len(title_lines)
    start_y = int(height * start_frac)
    if layout == "bottom_heavy":
        start_y = int(height * start_frac) - block_h // 3

    for idx, line in enumerate(title_lines):
        y = start_y + idx * line_h
        if align == "left":
            draw.text((margin, y), line, font=title_font, fill=ink + (255,))
        else:
            draw.text((width // 2, y), line, font=title_font, fill=ink + (255,), anchor="ma")

    # 分隔线
    rule_y = start_y + block_h + int(height * 0.035)
    rule_w = int(inner * (0.46 if layout == "split" else 0.30))
    rx = margin if align == "left" else (width - rule_w) // 2
    draw.line((rx, rule_y, rx + rule_w, rule_y), fill=accent + (255,), width=max(2, int(width * 0.003)))

    cur_y = rule_y + int(height * 0.028)
    if subtitle:
        for line in _wrap_text(subtitle, sub_font, inner, draw):
            draw.text((margin if align == "left" else width // 2, cur_y), line, font=sub_font,
                      fill=hex_to_rgb(af.get("color") or "#6e644a") + (255,),
                      anchor="la" if align == "left" else "ma")
            cur_y += int(author_size * 0.72)

    if author:
        ay = cur_y + int(height * 0.008)
        draw.text(
            (margin if align == "left" else width // 2, ay),
            author,
            font=author_font,
            fill=hex_to_rgb(af.get("color") or "#6e644a") + (255,),
            anchor="la" if align == "left" else "ma",
        )

    # 底部品牌区
    brand_y = int(height * 0.905)
    draw.line((margin, brand_y - int(height * 0.025), width - margin, brand_y - int(height * 0.025)),
              fill=accent + (90,), width=1)
    lp = Path(logo_path) if logo_path else DEFAULT_LOGO
    logo_size = int(width * 0.10)
    if lp.exists():
        try:
            logo = Image.open(lp).convert("RGBA")
            logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
            base.paste(logo, (width - margin - logo_size, brand_y - logo_size // 2), logo)
        except Exception:
            pass
    brand_ink = hex_to_rgb(af.get("color") or "#6e644a") + (255,)
    draw.text((margin, brand_y), "AutoBookForge", font=brand_font, fill=brand_ink, anchor="la")
    draw.text((margin, brand_y + int(width * 0.032)), "TXT 精排电子书引擎",
              font=_font(int(width * 0.019), "sans", 400), fill=brand_ink, anchor="la")

    if layout in ("bottom_heavy", "split"):
        draw.rectangle((0, height - band_h, width, height), fill=accent)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    base.save(output_path, "PNG", optimize=True)
    return output_path


def design_summary(design: Optional[dict]) -> str:
    """生成一行设计摘要，用于报告与 UI 展示。"""
    if not design:
        return ""
    bg = design.get("background") or {}
    tf = design.get("title_font") or {}
    dec = design.get("decoration") or {}
    return (
        f"{design.get('style', '')} / {bg.get('type', '')} "
        f"{'-'.join(bg.get('colors') or [])} / 标题 {tf.get('family', '')} {tf.get('size', '')}px / "
        f"装饰 {dec.get('type', '')}@{dec.get('position', '')} / 布局 {design.get('layout', '')}"
    )
