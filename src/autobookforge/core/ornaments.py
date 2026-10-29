"""结构组件：扉页、章节饰条、逻辑页纹样。

全部输出 XHTML 片段，由 ``epub_builder`` 组装进书籍。本模块不依赖 EbookLib，
可单独测试。

关于「每页顶部纹样」的现实边界
------------------------------
EPUB（reflowable）没有分页概念——页码与页眉由阅读器实时计算并绘制，作者无法
在每一「页」顶部放置元素。可落地的三层替代：

1. ``@page`` 边距框（见 guoxue.css）：CSS Paged Media 规范内的写法，
   Vivliostyle / Prince 等排版引擎会渲染，主流阅读器忽略。零成本保留。
2. ``ornament="chapter"``：每章开头一个纹样（内容层，一定生效）。
3. ``ornament="segment"``：按字数把章节切成「逻辑页」，每段顶部插入纹样。
   滚动阅读时形成接近书页的节奏，但切分点与阅读器实际分页不重合。

真正的逐页纹样只能靠固定版式（pre-paginated）导出，代价是失去字号调节能力。
"""

from __future__ import annotations

import html
from typing import Optional, Sequence

# 装饰符号：全部取自 Unicode 几何符号 / Dingbats 区，默认文本呈现，
# 不是 Emoji，中文字体字形覆盖良好。
ORNAMENTS = {
    "diamond": "\u2756",    # ❖
    "lozenge": "\u25c8",    # ◈
    "asterisk": "\u2217",   # ∗
    "florette": "\u2740",   # ❀
    "bar": "\u2014",        # —
    "none": "",
}

DEFAULT_ORNAMENT = "diamond"

# 逻辑页切分的目标字数（中文按字符计，约等于一屏正文）
DEFAULT_SEGMENT_CHARS = 650


def ornament_mark(kind: str = DEFAULT_ORNAMENT) -> str:
    return ORNAMENTS.get((kind or "").strip().lower(), ORNAMENTS[DEFAULT_ORNAMENT])


def page_rule(kind: str = DEFAULT_ORNAMENT, repeat: int = 1) -> str:
    """逻辑页纹样：一行居中的装饰符号。"""
    ch = ornament_mark(kind)
    if not ch:
        return ""
    repeat = max(1, min(int(repeat or 1), 5))
    body = ("\u2003".join([ch] * repeat)) if repeat > 1 else ch
    return '<p class="page-rule">%s</p>' % html.escape(body)


def section_break(kind: str = "lozenge") -> str:
    """章节内的场景分隔符。"""
    ch = ornament_mark(kind)
    if not ch:
        return ""
    return '<p class="section-break">%s</p>' % html.escape(ch)


def title_page_xhtml(
    book_title: str,
    *,
    author: str = "",
    subtitle: str = "",
    imprint: str = "AutoBookForge 精排",
    ornament: str = DEFAULT_ORNAMENT,
) -> str:
    """生成扉页 XHTML 片段。"""
    title = (book_title or "未命名书籍").strip()
    ch = ornament_mark(ornament)
    rows: list[str] = ['<section class="title-page" epub:type="titlepage">', '  <div class="tp-inner">']
    if ch:
        rows.append('    <p class="tp-ornament">%s</p>' % html.escape(ch))
    rows.append('    <h1 class="tp-title">%s</h1>' % html.escape(title))
    rows.append('    <hr class="tp-rule" />')
    if subtitle:
        rows.append('    <p class="tp-sub">%s</p>' % html.escape(subtitle.strip()))
    if author:
        rows.append('    <p class="tp-author">%s</p>' % html.escape(author.strip()))
    if imprint:
        rows.append('    <p class="tp-imprint">%s</p>' % html.escape(imprint.strip()))
    rows.append("  </div>")
    rows.append("</section>")
    return "\n".join(rows)


def chapter_xhtml(
    chapter,
    *,
    ornament: str = "chapter",
    segment_chars: int = DEFAULT_SEGMENT_CHARS,
    ornament_kind: str = DEFAULT_ORNAMENT,
) -> str:
    """把 Chapter 转为 XHTML 片段。

    ornament: none / chapter / segment
    """
    level = int(getattr(chapter, "level", 2) or 2)
    title = (getattr(chapter, "title", "") or "").strip()
    content = getattr(chapter, "content", "") or ""

    parts: list[str] = []
    if level == 1:
        parts.append('<h1 class="volume-title">%s</h1>' % html.escape(title))
    elif level == 3:
        parts.append('<h2 class="section-title">%s</h2>' % html.escape(title))
    else:
        parts.append('<h1 class="chapter-title">%s</h1>' % html.escape(title))

    mode = (ornament or "chapter").strip().lower()
    parts.append('<div class="chapter-body">')

    acc = 0
    seen = False
    for raw in content.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if mode == "segment" and seen and segment_chars > 0 and acc >= segment_chars:
            mark = page_rule(ornament_kind)
            if mark:
                parts.append(mark)
                acc = 0
        parts.append("<p>%s</p>" % html.escape(line))
        acc += len(line)
        seen = True

    parts.append("</div>")
    return "\n".join(parts)


def typography_css(family_by_role: dict, *, vertical: bool = False) -> str:
    """运行时字体覆盖样式。

    直接输出「选择器 + 字面字体栈」，不依赖 CSS 变量——老 Kindle 与部分
    国产阅读器不支持 var()，直接给值才能生效。同一批变量也写入 :root，
    供 _tokens.css 中带 var() 的规则使用。
    """
    by = {k: v for k, v in (family_by_role or {}).items() if v}
    groups = [
        ("body", "body, p, li, td, th"),
        ("title", "h1, h2, h3, h4, .chapter-title, .volume-title, .section-title,\n.tp-title, .tp-author, .tp-ornament, .fn-head"),
        ("quote", "blockquote, blockquote p, blockquote cite, .tp-sub, figcaption"),
        ("note", ".footnote, .footnotes li, nav#toc, .toc-item"),
    ]
    vars_lines: list = []
    blocks: list = []
    for role, selector in groups:
        fam = by.get(role)
        if not fam:
            continue
        stack = _css_stack(fam)
        vars_lines.append("  --abf-font-%s: %s;" % (role, stack))
        blocks.append("%s {\n  font-family: %s;\n}" % (selector, stack))

    lines = ["/* 运行时生成的字体覆盖层（由 AutoBookForge 写入，勿手改） */"]
    if vars_lines:
        lines.append(":root {")
        lines.extend(vars_lines)
        lines.append("}")
        lines.append("")
    lines.extend(blocks)
    if vertical:
        lines += ["", "html, body { -webkit-writing-mode: vertical-rl; writing-mode: vertical-rl; }"]
    return "\n".join(lines).strip() + "\n"


def _css_stack(value: str) -> str:
    """把「A, B, serif」这类回退链规整为 CSS 可用的字符串。"""
    v = (value or "").strip()
    if not v:
        return "serif"
    # 家族名含空格但未加引号时补引号
    out: list[str] = []
    for part in v.split(","):
        p = part.strip()
        if not p:
            continue
        if p.startswith('"') or p.startswith("'") or " " not in p:
            out.append(p)
        else:
            out.append('"%s"' % p)
    return ", ".join(out) if out else "serif"
