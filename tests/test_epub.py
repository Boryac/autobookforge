"""模块 5/6 测试：样式注入与 EPUB 生成。"""
import os

from autobookforge.core.epub_builder import (
    STYLE_ALIASES,
    build_epub,
    check_epub_structure,
    chapter_to_xhtml,
    resolve_style_css,
)
from autobookforge.core.splitter import BookSplit, Chapter


def _sample_book():
    return BookSplit(
        "测试书籍",
        [
            Chapter(0, "第一章　开端", "正文内容。" * 80, 2, None, 1),
            Chapter(1, "第二章　发展", "更多内容。" * 80, 2, None, 2),
        ],
    )


def test_all_styles_resolve():
    for name in ("minimal", "literary", "business", "classic"):
        css = resolve_style_css(name)
        assert len(css) > 200
        assert "body" in css


def test_chinese_style_alias():
    assert STYLE_ALIASES["文艺"] == "literary.css"
    assert STYLE_ALIASES["古籍"] == "classic.css"


def test_chapter_to_xhtml():
    ch = Chapter(0, "第一章", "第一段。\n第二段。")
    html = chapter_to_xhtml(ch)
    assert "<h1" in html
    assert html.count("<p>") == 2


def test_build_epub_and_structure(tmp_path):
    out = tmp_path / "book.epub"
    build_epub(_sample_book(), str(out), style="literary", author="测试作者")
    assert out.exists()
    assert out.stat().st_size > 1000
    chk = check_epub_structure(str(out))
    assert chk["ok"] is True
    assert chk["missing"] == []
    assert chk["chapters"] >= 2


def test_build_epub_with_cover(tmp_path):
    from autobookforge.cover.generator import generate_cover

    cover = tmp_path / "cover.png"
    generate_cover("测试书籍", "测试作者", str(cover), style="literary")
    assert cover.exists()
    assert cover.stat().st_size > 2000

    out = tmp_path / "book2.epub"
    build_epub(_sample_book(), str(out), style="minimal", cover_path=str(cover))
    chk = check_epub_structure(str(out))
    assert chk["ok"] is True
    assert chk["has_cover"] is True


def test_all_cover_styles(tmp_path):
    from autobookforge.cover.generator import generate_cover

    for style in ("minimal", "literary", "business", "classic", "tech", "design"):
        p = tmp_path / f"cover_{style}.png"
        generate_cover("封面测试", "作者", str(p), style=style)
        assert p.exists() and p.stat().st_size > 2000
