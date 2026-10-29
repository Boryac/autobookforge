"""模块 6：EPUB 生成。

基于 EbookLib 从零构建 EPUB3，注入精排 CSS 与内嵌字体，生成章节 XHTML、
嵌套目录、扉页与封面。运行时调用（不静态捆绑），许可合规。

设计要点
--------
- **稳定标识符**：``dc:identifier`` 用书名+作者派生的 UUID5。同一本书重复转换
  得到同一个 id，阅读器不会在书架里出现重复条目。调用方可显式传入覆盖。
- **正文不设底色**：底色交给阅读器主题，避免深色模式下的浅底黑字。
- **缩进模式**：容器类 ``abf-indent-*`` 控制，风格 CSS 不再各自处理。
- **字体为可选增强**：``font_plan`` 为 None 时走系统回退链，体积零增量。
"""

from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path
from typing import Optional

from ebooklib import epub

from . import ornaments as orn
from .splitter import BookSplit, Chapter

_STYLE_DIR = Path(__file__).resolve().parent.parent / "styles"

STYLE_ALIASES = {
    "minimal": "minimal.css",
    "literary": "literary.css",
    "business": "business.css",
    "classic": "classic.css",
    "guoxue": "guoxue.css",
    "简约": "minimal.css",
    "文艺": "literary.css",
    "商务": "business.css",
    "古籍": "classic.css",
    "国学": "guoxue.css",
}

STYLE_LABELS = {
    "minimal": "简约",
    "literary": "文艺",
    "business": "商务",
    "classic": "古籍",
    "guoxue": "国学精排",
}

DEFAULT_STYLE = "literary"

INDENT_MODES = ("space", "css", "spacing")
ORNAMENT_MODES = ("none", "chapter", "segment")

_FONT_MEDIA = {
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ttc": "font/ttf",
}


# ---------------------------------------------------------------------------
# 风格
# ---------------------------------------------------------------------------
def normalize_style(style: str) -> str:
    raw = (style or "").strip()
    if raw in STYLE_ALIASES:
        fname = STYLE_ALIASES[raw]
        return fname[:-4]
    return DEFAULT_STYLE


def style_choices() -> list:
    seen: list = []
    for key, fname in STYLE_ALIASES.items():
        if len(key) > 3:  # 跳过中文别名
            continue
        sid = fname[:-4]
        if sid in [s["id"] for s in seen]:
            continue
        seen.append({"id": sid, "label": STYLE_LABELS.get(sid, sid), "file": fname})
    return seen


def resolve_style_css(style: str) -> str:
    """按顺序拼装：令牌 -> 兼容层 -> 风格。"""
    parts: list[str] = []
    for name in ("_tokens.css", "base.css"):
        p = _STYLE_DIR / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    fname = STYLE_ALIASES.get((style or "").strip(), None)
    if fname is None:
        fname = STYLE_ALIASES[DEFAULT_STYLE]
    sp = _STYLE_DIR / fname
    if sp.exists():
        parts.append(sp.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _link_css(item: epub.EpubItem) -> None:
    item.add_link(href="style/main.css", rel="stylesheet", type="text/css")
    item.add_link(href="style/typography.css", rel="stylesheet", type="text/css")


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
def build_toc(book: BookSplit, items: list) -> list:
    """构建目录：有卷时按卷分组，否则扁平。"""
    if not book.volume_titles:
        return list(items)

    toc: list = []
    bucket: list = []
    current: Optional[str] = None

    def flush() -> None:
        nonlocal bucket, current
        if not bucket:
            return
        if current:
            toc.append((epub.Section(current), tuple(bucket)))
        else:
            toc.extend(bucket)
        bucket = []

    for ch, item in zip(book.chapters, items):
        vol = ch.volume_title or ""
        if vol != current:
            flush()
            current = vol
        bucket.append(item)
    flush()
    return toc


# ---------------------------------------------------------------------------
# 元数据辅助
# ---------------------------------------------------------------------------
_ID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # DNS 命名空间


def stable_identifier(title: str, author: str) -> str:
    """由书名与作者派生稳定 UUID，重复转换不产生新书。"""
    key = "%s|%s|autobookforge" % ((title or "").strip(), (author or "").strip())
    return "urn:uuid:" + str(uuid.uuid5(_ID_NS, key))


def _has_modified(eb: epub.EpubBook) -> bool:
    for entry in getattr(eb, "metadata", []) or []:
        try:
            ns, name = entry[0], entry[1]
        except Exception:
            continue
        if str(name) == "dcterms:modified":
            return True
        try:
            others = entry[3]
        except Exception:
            others = None
        if isinstance(others, dict) and others.get("property") == "dcterms:modified":
            return True
    return False


def _body_class(indent_mode: str, vertical: bool) -> str:
    classes = ["abf-indent-%s" % (indent_mode if indent_mode in INDENT_MODES else "space")]
    if vertical:
        classes.append("abf-vertical")
    return " ".join(classes)


def _wrap(content: str, *, indent_mode: str, vertical: bool) -> str:
    cls = _body_class(indent_mode, vertical)
    return '<div class="%s">\n%s\n</div>' % (cls, content)


def chapter_to_xhtml(ch: Chapter, *, ornament: str = "chapter", indent_mode: str = "space",
                     vertical: bool = False) -> str:
    """把章节文本转为 XHTML 片段（保留旧签名，内部走 ornaments）。"""
    inner = orn.chapter_xhtml(ch, ornament=ornament)
    return _wrap(inner, indent_mode=indent_mode, vertical=vertical)


# ---------------------------------------------------------------------------
# 主构建
# ---------------------------------------------------------------------------
def build_epub(
    book: BookSplit,
    output_path: str,
    *,
    style: str = DEFAULT_STYLE,
    author: str = "",
    cover_path: Optional[str] = None,
    identifier: Optional[str] = None,
    language: str = "zh-Hans",
    publisher: str = "AutoBookForge",
    font_plan=None,
    title_page: bool = True,
    page_ornament: str = "chapter",
    indent_mode: str = "space",
    vertical: bool = False,
    subtitle: str = "",
    description: str = "",
    imprint: str = "AutoBookForge 精排",
) -> str:
    """把 BookSplit 写为 EPUB 文件，返回输出路径。"""
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    style = normalize_style(style)
    if indent_mode not in INDENT_MODES:
        indent_mode = "space"
    if page_ornament not in ORNAMENT_MODES:
        page_ornament = "chapter"

    title = book.title or "未命名书籍"
    author = author or "佚名"

    eb = epub.EpubBook()
    eb.set_identifier(identifier or stable_identifier(title, author))
    eb.set_title(title)
    eb.set_language(language)
    eb.add_author(author)
    eb.add_metadata("DC", "publisher", publisher)
    eb.add_metadata("DC", "description", description or f"由 AutoBookForge 生成，共 {len(book.chapters)} 章")
    eb.add_metadata("DC", "rights", "公共领域或已授权内容")
    if not _has_modified(eb):
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        eb.add_metadata("meta", now, {"property": "dcterms:modified"})
    if subtitle:
        eb.add_metadata("DC", "title", subtitle, {"id": "subtitle"})

    # ---- 样式 ----
    eb.add_item(
        epub.EpubItem(
            uid="style_main",
            file_name="style/main.css",
            media_type="text/css",
            content=resolve_style_css(style),
        )
    )

    # ---- 字体（可选增强）----
    face_css = (getattr(font_plan, "css", "") or "").strip()
    typo_css = orn.typography_css(
        (getattr(font_plan, "family_by_role", None) or {}), vertical=vertical
    )
    if face_css:
        typo_css = face_css + "\n\n" + typo_css
    eb.add_item(
        epub.EpubItem(
            uid="style_typography",
            file_name="style/typography.css",
            media_type="text/css",
            content=typo_css,
        )
    )
    font_count = 0
    if font_plan is not None:
        for arcname, blob in getattr(font_plan, "files", []) or []:
            ext = os.path.splitext(arcname)[1].lower()
            eb.add_item(
                epub.EpubItem(
                    uid="font_%d" % font_count,
                    file_name=arcname,
                    media_type=_FONT_MEDIA.get(ext, "application/octet-stream"),
                    content=blob,
                )
            )
            font_count += 1
        for arcname, text in getattr(font_plan, "licenses", []) or []:
            eb.add_item(
                epub.EpubItem(
                    uid="lic_%s" % os.path.basename(arcname).replace(".", "_"),
                    file_name=arcname,
                    media_type="text/plain",
                    content=text.encode("utf-8"),
                )
            )

    # ---- 封面 ----
    spine: list = ["nav"]
    if cover_path and os.path.exists(cover_path):
        with open(cover_path, "rb") as fh:
            eb.set_cover("cover.jpg", fh.read())
        spine = ["cover", "nav"]

    # ---- 扉页 ----
    front: list = []
    if title_page:
        tp = epub.EpubHtml(title="扉页", file_name="titlepage.xhtml", lang=language)
        tp.content = orn.title_page_xhtml(title, author=author, subtitle=subtitle, imprint=imprint)
        _link_css(tp)
        eb.add_item(tp)
        front.append(tp)

    # ---- 章节 ----
    items: list = []
    for ch in book.chapters:
        item = epub.EpubHtml(
            title=ch.title or f"第{ch.index + 1}章",
            file_name=f"chap_{ch.index:04d}.xhtml",
            lang=language,
        )
        item.content = chapter_to_xhtml(
            ch, ornament=page_ornament, indent_mode=indent_mode, vertical=vertical
        )
        _link_css(item)
        eb.add_item(item)
        items.append(item)

    eb.toc = build_toc(book, items)
    eb.spine = spine + front + items
    eb.add_item(epub.EpubNcx())
    eb.add_item(epub.EpubNav())

    epub.write_epub(output_path, eb, {})
    return output_path


# ---------------------------------------------------------------------------
# 结构自检
# ---------------------------------------------------------------------------
def check_epub_structure(epub_path: str) -> dict:
    """轻量结构自检（不依赖 epubcheck）：校验 zip 内的关键条目。"""
    import zipfile

    result = {
        "ok": True,
        "missing": [],
        "chapters": 0,
        "xhtml_files": 0,
        "has_cover": False,
        "has_nav": False,
        "has_ncx": False,
        "has_fonts": 0,
        "has_typography": False,
        "has_titlepage": False,
        "modified_ok": False,
        "mimetype_stored": False,
        "epub_version": "",
    }
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names = zf.namelist()
            for req in ("mimetype", "META-INF/container.xml"):
                if req not in names:
                    result["missing"].append(req)
            result["chapters"] = sum(
                1 for n in names if n.startswith("EPUB/chap_") and n.endswith(".xhtml")
            )
            result["xhtml_files"] = sum(1 for n in names if n.startswith("EPUB/") and n.endswith(".xhtml"))
            result["has_cover"] = any("cover" in n.lower() for n in names)
            result["has_nav"] = any(n.endswith("nav.xhtml") for n in names)
            result["has_ncx"] = any(n.endswith(".ncx") for n in names)
            result["has_typography"] = any(n.endswith("typography.css") for n in names)
            result["has_titlepage"] = any(n.endswith("titlepage.xhtml") for n in names)
            result["has_fonts"] = sum(
                1 for n in names if n.startswith("EPUB/fonts/") and n.lower().endswith((".woff2", ".ttf", ".otf"))
            )
            if names and zf.read("mimetype") != b"application/epub+zip":
                result["ok"] = False
                result["missing"].append("mimetype-content")
            try:
                info = zf.getinfo("mimetype")
                result["mimetype_stored"] = info.compress_type == zipfile.ZIP_STORED
            except Exception:
                pass
            opf = next((n for n in names if n.endswith(".opf")), None)
            if opf:
                try:
                    text = zf.read(opf).decode("utf-8", "replace")
                except Exception:
                    text = ""
                result["modified_ok"] = "dcterms:modified" in text
                if "version=\"3.0\"" in text or "version='3.0'" in text:
                    result["epub_version"] = "3.0"
                elif "version=\"2.0\"" in text:
                    result["epub_version"] = "2.0"
                if not result["modified_ok"]:
                    result["missing"].append("dcterms:modified")
    except Exception as exc:  # pragma: no cover
        result["ok"] = False
        result["error"] = str(exc)
    if result["missing"]:
        result["ok"] = False
    return result
