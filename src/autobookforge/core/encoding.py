"""模块 1：编码检测与转换。

封装 charset-normalizer，提供多级 fallback（UTF-8 -> GBK -> GB18030 -> BIG5），
检测或解码失败时抛出明确异常，供 CLI / Web UI 提示用户手动指定编码。
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from typing import Optional, Sequence

try:  # charset-normalizer 为必装依赖，容错以便单测环境降级
    from charset_normalizer import from_path as _cn_from_path

    _HAS_CHARSET_NORMALIZER = True
except Exception:  # pragma: no cover
    _HAS_CHARSET_NORMALIZER = False


DEFAULT_FALLBACKS: tuple[str, ...] = ("utf-8", "gbk", "gb18030", "big5", "utf-16")


class EncodingDetectionError(Exception):
    """无法检测或解码文件编码时抛出。"""


@dataclass
class DecodeResult:
    """编码检测结果。"""

    text: str
    encoding: str
    confidence: float
    fallback_used: bool
    path: str

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "confidence": round(self.confidence, 4),
            "fallback_used": self.fallback_used,
            "length": len(self.text),
            "path": self.path,
        }


_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _strip_bom(text: str) -> str:
    return text[1:] if text and text[0] == "\ufeff" else text


def _try_decode(raw: bytes, encoding: str) -> Optional[str]:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def read_bytes(filepath: str) -> bytes:
    if not os.path.exists(filepath):
        raise EncodingDetectionError(f"文件不存在: {filepath}")
    if os.path.isdir(filepath):
        raise EncodingDetectionError(f"路径是目录而非文件: {filepath}")
    with open(filepath, "rb") as fh:
        return fh.read()


def detect_and_convert(
    filepath: str,
    fallbacks: Sequence[str] = DEFAULT_FALLBACKS,
) -> DecodeResult:
    """读取文件并返回解码后的文本与所用编码。

    顺序：BOM 嗅探 -> charset-normalizer 最佳猜测 -> 逐级 fallback。
    """
    raw = read_bytes(filepath)
    if not raw:
        return DecodeResult("", "utf-8", 1.0, False, filepath)

    # 1) BOM 优先
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            text = _try_decode(raw, enc)
            if text is not None:
                return DecodeResult(_strip_bom(text), enc, 1.0, False, filepath)

    # 2) charset-normalizer 猜测
    if _HAS_CHARSET_NORMALIZER:
        try:
            best = _cn_from_path(filepath).best()
            if best is not None:
                text = str(best)
                if text:
                    coherence = float(getattr(best, "percent_coherence", 0.0) or 0.0) / 100.0
                    return DecodeResult(
                        _strip_bom(text),
                        best.encoding,
                        max(coherence, 0.5),
                        False,
                        filepath,
                    )
        except Exception:
            pass

    # 3) 逐级 fallback
    for enc in fallbacks:
        text = _try_decode(raw, enc)
        if text is not None:
            return DecodeResult(_strip_bom(text), enc, 0.4, True, filepath)

    raise EncodingDetectionError(
        f"无法检测文件编码，请手动指定 --encoding 参数。文件: {filepath} "
        f"（已尝试: {', '.join(fallbacks)}）"
    )


def decode_with_encoding(filepath: str, encoding: str) -> DecodeResult:
    """按用户指定编码解码，失败时回退到自动检测。"""
    raw = read_bytes(filepath)
    text = _try_decode(raw, encoding)
    if text is None:
        return detect_and_convert(filepath)
    return DecodeResult(_strip_bom(text), encoding, 1.0, False, filepath)
