"""精排与字体系统测试。

覆盖：字体发现与角色映射、子集化、@font-face 注入、缩进模式容器类、
扉页、饰条/纹样、稳定标识符、结构自检。
"""
from __future__ import annotations

import os
import zipfile

import pytest

from autobookforge.core import ornaments
from autobookforge.core.epub_builder import (
    INDENT_MODES,
    ORNAMENT_MODES,
    build_epub,
    check_epub_structure,
    normalize_style,
    resolve_style_css,
    stable_identifier,
    style_choices,
)
from autobookforge.core.fonts import (
    FALLBACK_CHARS,
    FontRegistry,
    collect_charset,
    fonttools_available,
    list_font_options,
)
from autobookforge.core.splitter import BookSplit, Chapter

# --------------------------------------------------------------------------
# 风格
# --------------------------------------------------------------------------


def test_style_choices_include_guoxue():
    ids = [s["id"] for s in style_choices()]
    assert ids == ["minimal", "literary", "business", "classic", "guoxue"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("guoxue", "guoxue"),
        ("国学", "guoxue"),
        ("古籍", "classic"),
        ("文艺", "literary"),
        ("", "literary"),
        ("unheard-of", "literary"),
    ],
)
def test_normalize_style(raw, expected):
    assert normalize_style(raw) == expected


@pytest.mark.parametrize("style", ["minimal", "literary", "business", "classic", "guoxue"])
def test_resolve_style_css_has_tokens_and_style(style):
    css = resolve_style_css(style)
    assert "--abf-paper" in css          # 令牌层
    assert ".abf-indent-spacing" in css  # 缩进模式类
    assert "text-indent" in css


def test_body_background_removed_from_styles():
    """正文不得设底色，否则与阅读器深色主题冲突。"""
    for style in ("minimal", "literary", "business", "classic", "guoxue"):
        css = resolve_style_css(style)
        head = css.split("/*")[0]
        assert "background: #f" not in css or "body" not in head
    base = resolve_style_css("guoxue")
    assert "background: #fbfbf9" not in base


# --------------------------------------------------------------------------
# 字符集与字体库
# --------------------------------------------------------------------------


def test_collect_charset_includes_fallback():
    cs = collect_charset(["第一章 试炼"])
    assert "第" in cs and "章" in cs
    assert "　" in cs                      # 全角空格（space 缩进模式必须）
    assert "\u2756" in cs                  # 饰条菱形
    assert "①" in cs                       # 圈码
    for ch in "0123456789":
        assert ch in cs


def test_collect_charset_dedups():
    a = collect_charset(["abc", "abd"])
    b = collect_charset(["abcabd"])
    assert set(a) == set(b)


def test_font_registry_discovers_ttf_dir():
    reg = FontRegistry.load()
    assert reg.faces, "未发现任何字体，请确认 ttf/ 目录存在"
    ids = {f.font_id for f in reg.faces}
    assert "source-han-serif" in ids
    assert "zhuque-fangsong" in ids
    # 全部为可嵌入的开源字体（OFL），体积可读
    for f in reg.faces:
        assert f.source_size > 100_000
        assert f.family


def test_roles_for_style_overrides():
    reg = FontRegistry.load()
    g = reg.roles_for_style("guoxue")
    assert g.get("body") == "source-han-serif"
    assert g.get("title") == "source-han-serif-bold"
    minimal = reg.roles_for_style("minimal")
    assert minimal.get("body") is None      # 走系统无衬线
    custom = reg.roles_for_style("minimal", {"body": "lxgw-wenkai"})
    assert custom["body"] == "lxgw-wenkai"


def test_font_options_payload():
    data = list_font_options()
    assert data["fonts"]
    assert set(data["levels"]) == set(("none", "body", "body+title", "full"))
    assert set(data["formats"]) == set(("woff2", "ttf", "both"))
    assert "fonttools" in data


@pytest.mark.skipif(not fonttools_available(), reason="未安装 fontTools")
def test_plan_body_level_subset(tmp_path):
    reg = FontRegistry.load()
    plan = reg.plan(
        ["他站在窗前，第一章 试炼。"],
        level="body",
        fmt="woff2",
        roles=reg.roles_for_style("guoxue"),
        tmp_dir=str(tmp_path),
    )
    assert plan.ok
    assert plan.css.count("@font-face") == 1
    assert plan.family_by_role.get("body")
    # 子集化后远小于源文件
    assert plan.report["total_out_mb"] < plan.report["total_source_mb"] / 5
    assert len(plan.files) == 1
    assert plan.files[0][0].startswith("fonts/")
    # 许可随包
    names = [n for n, _ in plan.licenses]
    assert "fonts/OFL.txt" in names


@pytest.mark.skipif(not fonttools_available(), reason="未安装 fontTools")
def test_plan_level_none_has_no_faces(tmp_path):
    reg = FontRegistry.load()
    plan = reg.plan(["测试"], level="none", tmp_dir=str(tmp_path))
    assert not plan.files
    assert plan.css == ""
    assert plan.family_by_role == {}


@pytest.mark.skipif(not fonttools_available(), reason="未安装 fontTools")
def test_subset_cache_reuses(tmp_path):
    reg = FontRegistry.load()
    face = reg.get("zhuque-fangsong")
    if not face:
        pytest.skip("缺少朱雀仿宋")
    from autobookforge.core.fonts import cached_subset_font

    a = tmp_path / "a.woff2"
    b = tmp_path / "b.woff2"
    ok1, _ = cached_subset_font(face.path, "测试文字", str(a), flavor="woff2")
    ok2, _ = cached_subset_font(face.path, "测试文字", str(b), flavor="woff2")
    assert ok1 and ok2
    assert a.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------
# 结构组件
# --------------------------------------------------------------------------


def test_title_page_xhtml():
    html = ornaments.title_page_xhtml("测试书名", author="某某", imprint="AutoBookForge 精排")
    assert "title-page" in html
    assert "测试书名" in html
    assert "某某" in html
    assert 'epub:type="titlepage"' in html
    assert "<hr class=\"tp-rule\" />" in html


def test_page_rule_uses_non_emoji_symbol():
    mark = ornaments.page_rule("diamond")
    assert "\u2756" in mark
    assert "page-rule" in mark
    # 不得出现 Emoji 区字符
    assert all(ord(c) < 0x1F000 for c in mark)


def test_chapter_xhtml_levels():
    v = ornaments.chapter_xhtml(Chapter(0, "卷一 上", "正文一", 1, None, None))
    c = ornaments.chapter_xhtml(Chapter(1, "第一章", "正文二", 2, None, None))
    s = ornaments.chapter_xhtml(Chapter(2, "1.1 小节", "正文三", 3, None, None))
    assert "volume-title" in v
    assert "chapter-title" in c
    assert "section-title" in s
    for frag in (v, c, s):
        assert 'class="chapter-body"' in frag


def test_chapter_xhtml_segment_ornament():
    text = "\n".join("第%d段正文内容，用来累计字符数。" % i for i in range(1, 61))
    frag = ornaments.chapter_xhtml(
        Chapter(0, "第一章", text, 2, None, None), ornament="segment", segment_chars=100
    )
    assert frag.count('class="page-rule"') >= 5
    none_frag = ornaments.chapter_xhtml(
        Chapter(0, "第一章", text, 2, None, None), ornament="none"
    )
    assert "page-rule" not in none_frag


def test_typography_css_only_embedded_roles():
    css = ornaments.typography_css({"body": "Source Han Serif CN"})
    assert "Source Han Serif CN" in css
    assert "h1, h2, h3" not in css      # 未嵌入 title，不应输出覆盖
    css2 = ornaments.typography_css({})
    assert css2.strip().startswith("/*")
    assert "@font-face" not in css2


# --------------------------------------------------------------------------
# EPUB 组装
# --------------------------------------------------------------------------


def _book() -> BookSplit:
    chapters = [
        Chapter(0, "卷一 风起", "卷一正文。" * 30, 1, "卷一 风起", 1),
        Chapter(1, "第一章 试炼", "第一章正文。" * 40, 2, "卷一 风起", 1),
        Chapter(2, "第二章 归途", "第二章正文。" * 40, 2, "卷一 风起", 2),
    ]
    b = BookSplit("测试书", chapters)
    b.quality = {"chapters": 3, "volumes": 1, "sections": 0, "median_len": 120,
                 "coverage": 1.0, "monotonic_ratio": 1.0}
    return b


def test_stable_identifier_is_deterministic():
    a = stable_identifier("某书", "某人")
    b = stable_identifier("某书", "某人")
    c = stable_identifier("某书", "另一人")
    assert a == b
    assert a != c
    assert a.startswith("urn:uuid:")


@pytest.mark.parametrize("mode", INDENT_MODES)
def test_build_epub_indent_mode(tmp_path, mode):
    out = str(tmp_path / ("%s.epub" % mode))
    build_epub(_book(), out, style="guoxue", author="某人", indent_mode=mode,
               title_page=False, page_ornament="none")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        opf = next(n for n in names if n.endswith(".opf"))
        assert "dcterms:modified" in zf.read(opf).decode("utf-8")
        chap = sorted(n for n in names if n.startswith("EPUB/chap_"))[0]
        body = zf.read(chap).decode("utf-8")
        assert ("abf-indent-%s" % mode) in body


@pytest.mark.parametrize("mode", ORNAMENT_MODES)
def test_build_epub_ornament_modes(tmp_path, mode):
    out = str(tmp_path / ("orn_%s.epub" % mode))
    build_epub(_book(), out, style="guoxue", author="某人", page_ornament=mode)
    chk = check_epub_structure(out)
    assert chk["ok"], chk
    assert chk["has_nav"] and chk["has_ncx"]
    assert chk["modified_ok"]
    assert chk["mimetype_stored"]
    assert chk["chapters"] == 3


def test_build_epub_titlepage_and_vertical(tmp_path):
    out = str(tmp_path / "tp.epub")
    build_epub(_book(), out, style="guoxue", author="某人", title_page=True, vertical=True)
    chk = check_epub_structure(out)
    assert chk["has_titlepage"]
    with zipfile.ZipFile(out) as zf:
        tp = zf.read("EPUB/titlepage.xhtml").decode("utf-8")
        assert "tp-title" in tp and "测试书" in tp


@pytest.mark.skipif(not fonttools_available(), reason="未安装 fontTools")
def test_build_epub_with_fonts(tmp_path):
    reg = FontRegistry.load()
    plan = reg.plan(
        ["卷一 风起", "第一章 试炼", "第一章正文。" * 20],
        level="body+title",
        fmt="woff2",
        roles=reg.roles_for_style("guoxue"),
        tmp_dir=str(tmp_path),
    )
    out = str(tmp_path / "fonts.epub")
    build_epub(_book(), out, style="guoxue", author="某人", font_plan=plan, page_ornament="none")
    chk = check_epub_structure(out)
    assert chk["ok"], chk
    assert chk["has_fonts"] >= 1
    assert chk["has_typography"]
    with zipfile.ZipFile(out) as zf:
        typo = zf.read("EPUB/style/typography.css").decode("utf-8")
        assert "@font-face" in typo
        assert "fonts/" in typo
        names = zf.namelist()
        assert any(n.endswith("OFL.txt") for n in names)


def test_check_epub_structure_reports_missing(tmp_path):
    bogus = tmp_path / "bogus.epub"
    bogus.write_bytes(b"not a zip")
    chk = check_epub_structure(str(bogus))
    assert chk["ok"] is False
