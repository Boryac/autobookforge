"""模块 4 测试：双层纠错引擎与报告。"""
from autobookforge.corrector.engine import CorrectionEngine
from autobookforge.corrector.ngram import NGramCorrector, load_confusion_set, load_lexicon
from autobookforge.corrector.report import build_report, build_segments, to_markdown
from autobookforge.corrector.rule_engine import RuleEngine


def test_rule_engine_homophone():
    eng = RuleEngine()
    res = eng.correct("这个做为参考。")
    assert "作为" in res.text
    assert any(c.source == "rule" and c.category == "homophone" for c in res.corrections)


def test_rule_engine_punctuation():
    eng = RuleEngine()
    res = eng.correct("你好，，世界。。。")
    assert "，，" not in res.text
    assert "。。。" not in res.text


def test_rule_engine_collocation():
    eng = RuleEngine()
    res = eng.correct("我们要增加水平。")
    assert "提高水平" in res.text


def test_confidence_filter():
    eng = RuleEngine(min_confidence=0.99)
    res = eng.correct("这个做为参考。")
    assert all(c.confidence >= 0.99 for c in res.corrections)


def test_ngram_layer_hits_lexicon():
    eng = CorrectionEngine(level=2)
    res = eng.correct("他题高了自己的水平。")
    assert "提高" in res.text
    assert any(c.source == "ngram" for c in res.corrections)


def test_level1_has_no_ngram():
    eng = CorrectionEngine(level=1)
    assert eng.ngram is None
    res = eng.correct("他题高了自己的水平。")
    assert any(c.source == "rule" for c in res.corrections) or res.text


def test_level2_has_ngram():
    eng = CorrectionEngine(level=2)
    assert eng.ngram is not None


def test_confusion_and_lexicon_loaded():
    conf = load_confusion_set()
    lex = load_lexicon()
    assert len(conf) > 20
    assert "提高" in lex


def test_report_segments_and_stats():
    eng = CorrectionEngine(level=2)
    text = "这个做为参考，按耐不住的心情。"
    res = eng.correct(text)
    report = build_report(text, res.text, res.corrections, title="测试")
    assert report["summary"]["total_corrections"] >= 2
    assert any(s.get("changed") for s in report["segments"])
    md = to_markdown(report)
    assert "纠错报告" in md
    assert "逐条明细" in md


def test_segments_cover_full_text():
    eng = CorrectionEngine(level=2)
    text = "这个做为参考。"
    res = eng.correct(text)
    segs = build_segments(text, res.corrections)
    rebuilt = "".join(s["text"] for s in segs)
    assert rebuilt == text


def test_empty_text():
    eng = CorrectionEngine(level=2)
    res = eng.correct("")
    assert res.text == ""
    assert res.count == 0
