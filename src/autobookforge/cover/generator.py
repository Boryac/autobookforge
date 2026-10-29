"""封面生成模块。

采用与 Web UI 同源的视觉系统（米白纸张 / 玻璃质感 / 酸橙绿强调 / 细腻噪点），
用 Pillow 直接绘制 1600x2560 竖版封面，零浏览器依赖、零 API 成本。

同时提供 HTML/CSS 模板（templates/），供需要 Playwright 高保真截图时使用。
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

COVER_W, COVER_H = 1600, 2560

_ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_LOGO = _ASSETS_DIR / "logo.jpg"

# --- 字体候选（Windows / macOS / Linux） ---
_FONT_BOLD = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)
_FONT_REGULAR = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
_FONT_SERIF = (
    r"C:\Windows\Fonts\simkai.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
)

# --- 风格配色 ---
COVER_STYLES: dict[str, dict] = {
    "minimal": {
        "bg": ((253, 253, 252), (248, 248, 245)),
        "title": (32, 34, 32),
        "author": (122, 124, 118),
        "accent": (200, 216, 60),
        "band": (224, 242, 71),
        "font": "regular",
        "dark": False,
    },
    "literary": {
        "bg": ((251, 250, 246), (240, 233, 216)),
        "title": (43, 41, 34),
        "author": (110, 100, 74),
        "accent": (178, 158, 98),
        "band": (224, 242, 71),
        "font": "serif",
        "dark": False,
    },
    "business": {
        "bg": ((255, 255, 255), (238, 240, 241)),
        "title": (18, 20, 24),
        "author": (96, 100, 106),
        "accent": (32, 34, 32),
        "band": (224, 242, 71),
        "font": "regular",
        "dark": False,
    },
    "classic": {
        "bg": ((247, 241, 225), (235, 225, 198)),
        "title": (90, 74, 44),
        "author": (134, 116, 80),
        "accent": (179, 154, 99),
        "band": (179, 154, 99),
        "font": "serif",
        "dark": False,
    },
    "tech": {
        "bg": ((20, 25, 22), (38, 48, 40)),
        "title": (244, 247, 238),
        "author": (176, 186, 168),
        "accent": (224, 242, 71),
        "band": (224, 242, 71),
        "font": "regular",
        "dark": True,
    },
    "design": {
        "bg": ((250, 247, 242), (232, 224, 210)),
        "title": (38, 36, 32),
        "author": (120, 112, 100),
        "accent": (224, 242, 71),
        "band": (60, 62, 56),
        "font": "regular",
        "dark": False,
    },
}
DEFAULT_STYLE = "literary"


def _pick_font_path(kind: str) -> Optional[str]:
    table = {"bold": _FONT_BOLD, "regular": _FONT_REGULAR, "serif": _FONT_SERIF}
    for path in table.get(kind, _FONT_REGULAR):
        if os.path.exists(path):
            return path
    return None


def load_font(size: int, kind: str = "regular") -> ImageFont.FreeTypeFont:
    path = _pick_font_path(kind) or _pick_font_path("regular") or _pick_font_path("bold")
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(size: tuple[int, int], top: tuple, bottom: tuple, angle_vertical: bool = True) -> Image.Image:
    w, h = size
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line((0, y, w, y), fill=_lerp(top, bottom, t))
    return img


def _add_grain(img: Image.Image, strength: float = 10.0, opacity: float = 0.08) -> Image.Image:
    """叠加细腻噪点（高频微颗粒）。"""
    w, h = img.size
    small = (max(2, w // 4), max(2, h // 4))
    noise = Image.effect_noise(small, strength).convert("L")
    noise = noise.resize((w, h), Image.NEAREST)
    noise_rgb = Image.merge("RGB", (noise, noise, noise))
    return Image.blend(img, noise_rgb, opacity)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """按像素宽度做中文友好换行。"""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        cur = ""
        for ch in paragraph:
            trial = cur + ch
            if draw.textlength(trial, font=font) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines


def _resolve_style(style: str) -> dict:
    return COVER_STYLES.get(style, COVER_STYLES[DEFAULT_STYLE])


def generate_cover(
    title: str,
    author: str = "",
    output_path: str = "cover.png",
    *,
    style: str = "auto",
    subtitle: str = "",
    logo_path: Optional[str] = None,
    width: int = COVER_W,
    height: int = COVER_H,
    seed: Optional[int] = None,
) -> str:
    """生成竖版封面 PNG，返回输出路径。"""
    if style in ("auto", "", None):
        style = DEFAULT_STYLE
    cfg = _resolve_style(style)

    if seed is not None:
        random.seed(seed)

    base = _gradient((width, height), cfg["bg"][0], cfg["bg"][1])
    base = _add_grain(base, strength=8.0, opacity=0.075)

    draw = ImageDraw.Draw(base)
    margin = int(width * 0.12)
    inner = width - margin * 2

    title_font = load_font(int(width * 0.088), cfg["font"])
    author_font = load_font(int(width * 0.038), "regular")
    sub_font = load_font(int(width * 0.03), "regular")
    brand_font = load_font(int(width * 0.024), "regular")

    # --- 顶部装饰带（酸橙绿渐变条） ---
    band_h = int(height * 0.012)
    draw.rectangle((0, 0, width, band_h), fill=cfg["band"])

    # --- 装饰圆点（呼应参考页 pulsepoint） ---
    dot_r = int(width * 0.012)
    draw.ellipse(
        (margin, int(height * 0.11), margin + dot_r * 2, int(height * 0.11) + dot_r * 2),
        fill=cfg["band"],
    )

    # --- 书名 ---
    title_lines = _wrap_text(title or "未命名书籍", title_font, inner, draw)
    line_h = int(width * 0.088 * 1.28)
    block_h = line_h * len(title_lines)
    start_y = int(height * 0.30)
    for idx, line in enumerate(title_lines):
        y = start_y + idx * line_h
        draw.text((width // 2, y), line, font=title_font, fill=cfg["title"], anchor="ma")

    # --- 分隔线 ---
    rule_y = start_y + block_h + int(height * 0.045)
    rule_w = int(inner * 0.34)
    draw.line(
        ((width - rule_w) // 2, rule_y, (width + rule_w) // 2, rule_y),
        fill=cfg["accent"],
        width=max(2, int(width * 0.0035)),
    )

    # --- 副标题 ---
    cur_y = rule_y + int(height * 0.035)
    if subtitle:
        for line in _wrap_text(subtitle, sub_font, inner, draw):
            draw.text((width // 2, cur_y), line, font=sub_font, fill=cfg["author"], anchor="ma")
            cur_y += int(width * 0.044)

    # --- 作者 ---
    if author:
        draw.text((width // 2, cur_y + int(height * 0.012)), author, font=author_font, fill=cfg["author"], anchor="ma")

    # --- 底部品牌区 ---
    brand_y = int(height * 0.885)
    draw.line(
        (margin, brand_y - int(height * 0.03), width - margin, brand_y - int(height * 0.03)),
        fill=cfg["accent"],
        width=1,
    )

    # Logo（可选，右下角）
    lp = Path(logo_path) if logo_path else DEFAULT_LOGO
    logo_size = int(width * 0.12)
    if lp.exists():
        try:
            logo = Image.open(lp).convert("RGBA")
            logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
            lx = width - margin - logo_size
            ly = height - int(height * 0.075) - logo_size // 2
            base.paste(logo, (lx, ly), logo)
        except Exception:
            pass

    draw.text((margin, brand_y), "AutoBookForge", font=brand_font, fill=cfg["author"], anchor="la")
    draw.text(
        (margin, brand_y + int(width * 0.034)),
        "TXT 精排电子书引擎",
        font=load_font(int(width * 0.02), "regular"),
        fill=cfg["author"],
        anchor="la",
    )

    # --- 底部装饰带 ---
    draw.rectangle((0, height - band_h, width, height), fill=cfg["accent"])

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    base.save(output_path, "PNG", optimize=True)
    return output_path


def generate_cover_from_template(
    title: str,
    author: str,
    output_path: str,
    *,
    style: str = "literary",
    template: str = "cover_default.html",
) -> Optional[str]:
    """可选：HTML 模板 + Playwright 截图（需安装 playwright 与 chromium）。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return None

    tpl = _TEMPLATE_DIR / template
    if not tpl.exists():
        return None
    html_text = tpl.read_text(encoding="utf-8")
    html_text = (
        html_text.replace("{{TITLE}}", title or "未命名书籍")
        .replace("{{AUTHOR}}", author or "")
        .replace("{{STYLE}}", style)
    )
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": COVER_W // 2, "height": COVER_H // 2}, device_scale_factor=2)
            page.set_content(html_text)
            page.wait_for_timeout(200)
            page.screenshot(path=output_path, clip={"x": 0, "y": 0, "width": COVER_W // 2, "height": COVER_H // 2})
            browser.close()
        return output_path
    except Exception:
        return None


def available_styles() -> Sequence[str]:
    return tuple(COVER_STYLES.keys())
