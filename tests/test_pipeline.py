"""端到端测试：完整流水线。"""
import json
import os

from autobookforge.pipeline.config import load_config
from autobookforge.pipeline.orchestrator import AutoBookPipeline, PipelineOptions, clean_bookname, extract_author

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def test_clean_bookname():
    assert clean_bookname("凡人小传") == "凡人小传"
    assert clean_bookname("小说【精校版】(1)") == "小说"
    assert clean_bookname("") == "未命名书籍"


def test_extract_author():
    assert extract_author("作者：李四\n正文") == "李四"
    assert extract_author("作者: 王五\n正文") == "王五"
    assert extract_author("没有作者信息") == ""


def test_load_config_has_steps():
    cfg = load_config()
    assert cfg.step("encoding_detect") is not None
    assert cfg.step("correct") is not None


def test_full_pipeline_epub(tmp_path):
    res = AutoBookPipeline(load_config()).run(
        os.path.join(FIXTURES, "凡人小传.txt"),
        PipelineOptions(output_dir=str(tmp_path), formats=["epub"], style="literary",
                        correct_level=2, cover=True, cover_style="literary"),
    )
    assert res.success, res.message
    assert res.chapter_count >= 2
    assert res.total_chars > 200
    assert "epub" in res.output_files
    assert os.path.exists(res.output_files["epub"])
    assert res.epub_check.get("ok") is True
    assert res.author == "李四"
    assert res.correct_stats["total"] > 0


def test_pipeline_level1(tmp_path):
    res = AutoBookPipeline(load_config()).run(
        os.path.join(FIXTURES, "凡人小传.txt"),
        PipelineOptions(output_dir=str(tmp_path), style="minimal", correct_level=1, cover=False),
    )
    assert res.success
    assert res.correct_stats.get("by_source", {}).get("ngram", 0) == 0


def test_pipeline_classic_ancient(tmp_path):
    res = AutoBookPipeline(load_config()).run(
        os.path.join(FIXTURES, "山海经异闻.txt"),
        PipelineOptions(output_dir=str(tmp_path), style="classic", correct_level=2, cover=True),
    )
    assert res.success
    assert res.chapter_count >= 3
    assert "epub" in res.output_files


def test_report_has_chapter_index(tmp_path):
    res = AutoBookPipeline(load_config()).run(
        os.path.join(FIXTURES, "凡人小传.txt"),
        PipelineOptions(output_dir=str(tmp_path), correct_level=2, cover=False),
    )
    idx = res.report.get("chapter_index", [])
    assert len(idx) == res.chapter_count
    assert all("title" in c and "chars" in c for c in idx)


def test_missing_input():
    res = AutoBookPipeline(load_config()).run(
        os.path.join(FIXTURES, "不存在.txt"), PipelineOptions(dry_run=True)
    )
    assert res.success is False
    assert "不存在" in res.message


def test_report_json_serializable(tmp_path):
    res = AutoBookPipeline(load_config()).run(
        os.path.join(FIXTURES, "凡人小传.txt"),
        PipelineOptions(output_dir=str(tmp_path), correct_level=2, cover=False),
    )
    text = json.dumps(res.as_dict(), ensure_ascii=False)
    assert "book_title" in text
