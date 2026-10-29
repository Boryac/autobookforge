"""模块 3 测试：章节识别与分章。"""
from autobookforge.core.splitter import infer_title, split_chapters
from autobookforge.core.textutil import cn_to_arabic, extract_number


def _make_book(n=3, body_len=200):
    parts = ["测试书名", ""]
    for i in range(1, n + 1):
        parts.append(f"第{i}章　章节标题{i}")
        parts.append("正文内容。" * (body_len // 5))
        parts.append("")
    return "\n".join(parts)


def test_split_multi_chapters():
    book = split_chapters(_make_book(3), min_chapter_length=50)
    assert len(book.chapters) == 3
    assert book.chapters[0].title.startswith("第1章")


def test_no_heading_fallback():
    book = split_chapters("这是一段没有任何章节标记的文字。" * 5, min_chapter_length=10)
    assert len(book.chapters) == 1
    assert any("未识别" in w for w in book.warnings)


def test_short_chapter_preserved():
    """短章不再被强行合并——旧版把标题行并进上一章，反而污染了标题与正文边界。"""
    text = "第一章　A\n" + "内容。" * 20 + "\n第二章　B\n这段很短但有效。\n"
    book = split_chapters(text, min_chapter_length=100)
    assert len(book.chapters) == 2
    assert "这段很短但有效。" in book.chapters[1].content


def test_empty_chapter_dropped():
    """空壳章（目录页 / 重复标注）被丢弃，不产生噪声章节。"""
    text = (
        "第一章　甲\n第二章　乙\n第三章　丙\n\n"
        "第一章　甲\n" + "内容。" * 30 + "\n"
        "第二章　乙\n" + "内容。" * 30 + "\n"
        "第三章　丙\n" + "内容。" * 30 + "\n"
    )
    book = split_chapters(text, min_chapter_length=50)
    assert len(book.chapters) == 3
    assert book.chapters[0].title.startswith("第一章")


def test_bare_number_chapters():
    """裸数字占一行（分卷下载器常见写法）应被识别为章。"""
    text = "".join(f"\n{i}\n" + "正文内容。" * 30 + "\n" for i in range(1, 6))
    book = split_chapters(text, min_chapter_length=50)
    assert len(book.chapters) == 5


def test_num_dun_scheme():
    """手打体「1、」「10、标题」应被识别，且不误吞下一段正文。"""
    text = (
        "1、\n" + "内容。" * 30 + "\n"
        "2、重逢危机\n" + "内容。" * 30 + "\n"
        "3、\n" + "内容。" * 30 + "\n"
    )
    book = split_chapters(text, min_chapter_length=50)
    assert len(book.chapters) == 3
    assert "重逢危机" in book.chapters[1].title


def test_mixed_marker_families():
    """同一本书内混用多种标记族（裸数字 + 站点前缀式）也应正确切分。"""
    text = (
        "\n1\n" + "内容。" * 30 + "\n"
        "\n2\n" + "内容。" * 30 + "\n"
        "\n测试小说--正章1\n" + "内容。" * 30 + "\n"
        "\n测试小说--正章2\n" + "内容。" * 30 + "\n"
    )
    book = split_chapters(text, min_chapter_length=50)
    assert len(book.chapters) == 4


def test_scene_title_fallback():
    """无序号评书体：按书内场景短标题自适应分章。"""
    text = (
        "胡子与他的熊熊集团\n\n" + "内容。" * 30 + "\n\n"
        "胡子与农民工德贵的庄园\n\n" + "内容。" * 30 + "\n\n"
        "胡子和他的第一位小车司机熊\n\n" + "内容。" * 30 + "\n\n"
        "胡子与背军挎的神秘熊(上\n\n" + "内容。" * 30 + "\n\n"
        "胡子与背军挎的神秘熊（下\n\n" + "内容。" * 30 + "\n\n"
        "胡子接老伴 众熊认干妈1\n\n" + "内容。" * 30 + "\n"
    )
    book = split_chapters(text, min_chapter_length=50)
    assert len(book.chapters) == 6
    assert any("场景短标题" in w for w in book.warnings)


def test_quality_metrics():
    book = split_chapters(_make_book(4), min_chapter_length=50)
    q = book.quality
    assert q["chapters"] == 4
    assert 0.0 < q["coverage"] <= 1.0
    assert q["median_len"] > 0


def test_cn_to_arabic():
    assert cn_to_arabic("一百二十三") == 123
    assert cn_to_arabic("十二") == 12
    assert cn_to_arabic("十") == 10
    assert cn_to_arabic("2024") == 2024
    assert cn_to_arabic("零") == 0


def test_extract_number():
    assert extract_number("第12章　测试") == 12
    assert extract_number("第一百二十章") == 120


def test_volume_grouping():
    text = (
        "第一卷　上\n第一章　a\n" + "内容。" * 40 + "\n"
        "第二章　b\n" + "内容。" * 40 + "\n"
        "第二卷　下\n第三章　c\n" + "内容。" * 40 + "\n"
    )
    book = split_chapters(text, min_chapter_length=50)
    assert len(book.chapters) == 3
    assert book.chapters[0].volume_title == "第一卷　上"
    assert book.chapters[2].volume_title == "第二卷　下"


def test_serialization_roundtrip():
    book = split_chapters(_make_book(2), min_chapter_length=50)
    data = book.as_dict()
    from autobookforge.core.splitter import BookSplit

    restored = BookSplit.from_dict(data)
    assert len(restored.chapters) == len(book.chapters)
    assert restored.chapters[0].title == book.chapters[0].title
