"""模块 2 测试：文本清洗与智能排版。"""
from autobookforge.core.cleaner import FULLWIDTH_SPACE, clean_text


def test_ads_removed():
    text = "正常段落一。\n更多精彩请访问 www.example.com\nQQ群：123456789\n正常段落二。"
    cleaned, stats = clean_text(text)
    assert stats.ads_removed >= 2
    assert "example.com" not in cleaned
    assert "正常段落一。" in cleaned


def test_broken_line_merged():
    text = "他站在山巅，俯瞰着脚下\n的云海。"
    cleaned, stats = clean_text(text)
    assert stats.lines_merged >= 1
    assert "脚下的云海。" in cleaned.replace(FULLWIDTH_SPACE, "")


def test_hardcoded_indent():
    text = "这是一个普通段落。"
    cleaned, _ = clean_text(text)
    assert cleaned.startswith(FULLWIDTH_SPACE * 2)


def test_heading_not_indented():
    text = "第一章　开端\n正文内容在此。"
    cleaned, _ = clean_text(text)
    assert "第一章" in cleaned
    assert not cleaned.startswith(FULLWIDTH_SPACE)


def test_blank_lines_collapsed():
    text = "第一段。\n\n\n\n第二段。"
    cleaned, stats = clean_text(text)
    assert "\n\n\n" not in cleaned
    assert stats.blanks_collapsed >= 1


def test_smart_linebreak_disabled():
    text = "他站在山巅，俯瞰着脚下\n的云海。"
    cleaned, stats = clean_text(text, smart_linebreak=False)
    assert stats.lines_merged == 0
