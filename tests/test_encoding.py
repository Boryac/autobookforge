"""模块 1 测试：编码检测与转换。"""
import os

import pytest

from autobookforge.core.encoding import EncodingDetectionError, detect_and_convert

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def test_utf8_detection():
    d = detect_and_convert(os.path.join(FIXTURES, "凡人小传.txt"))
    assert "凡人" in d.text
    assert d.fallback_used is False
    assert len(d.text) > 100


def test_gbk_detection():
    d = detect_and_convert(os.path.join(FIXTURES, "凡人小传_GBK.txt"))
    assert "凡人" in d.text
    assert len(d.text) > 100


def test_missing_file_raises():
    with pytest.raises(EncodingDetectionError):
        detect_and_convert(os.path.join(FIXTURES, "不存在的文件.txt"))


def test_as_dict():
    d = detect_and_convert(os.path.join(FIXTURES, "凡人小传.txt"))
    info = d.as_dict()
    assert {"encoding", "confidence", "fallback_used", "length", "path"} <= set(info)
