"""Layer 2：统计纠错层（轻量 NGram 实现，CPU 即可运行）。

不依赖 KenLM 权重与神经网络：以「混淆集候选生成 + 词表/上下文频次打分」
作为高精确率的前置筛选层，只输出置信度较高的修正建议，并标注 source=ngram。

接口与 pycorrector 的 KenlmCorrector 保持同构（correct(text) -> 修正列表），
后续可无损替换为真实 KenLM 模型。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .rule_engine import Correction, CorrectionResult

_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CONFUSION_PATH = _DATA_DIR / "confusion_set.json"
DEFAULT_LEXICON_PATH = _DATA_DIR / "lexicon.txt"

_CJK = re.compile(r"^[\u4e00-\u9fff]+$")


def load_confusion_set(path: Optional[str] = None) -> dict[str, set[str]]:
    """加载混淆集：字 -> 可替换候选字集合（含自身）。"""
    p = Path(path) if path else DEFAULT_CONFUSION_PATH
    mapping: dict[str, set[str]] = {}
    if not p.exists():
        return mapping
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return mapping
    for group in data.get("groups", []):
        chars = [c for c in str(group) if "\u4e00" <= c <= "\u9fff"]
        if len(chars) < 2:
            continue
        for ch in chars:
            mapping.setdefault(ch, set()).update(chars)
    return mapping


def load_lexicon(path: Optional[str] = None) -> set[str]:
    """加载词表：仅保留 2-4 字纯中文词。"""
    p = Path(path) if path else DEFAULT_LEXICON_PATH
    words: set[str] = set()
    if not p.exists():
        return words
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            w = line.strip()
            if 2 <= len(w) <= 4 and _CJK.match(w):
                words.add(w)
    except Exception:
        pass
    return words


class NGramCorrector:
    """轻量统计纠错层。"""

    source_name = "ngram"

    def __init__(
        self,
        confusion_path: Optional[str] = None,
        lexicon_path: Optional[str] = None,
        *,
        threshold: float = 0.82,
        context_window: int = 3,
        min_word_len: int = 2,
        max_word_len: int = 4,
        max_substitutions: int = 1,
    ) -> None:
        self.confusion = load_confusion_set(confusion_path)
        self.lexicon = load_lexicon(lexicon_path)
        self.threshold = threshold
        self.context_window = context_window
        self.min_word_len = min_word_len
        self.max_word_len = max_word_len
        self.max_substitutions = max_substitutions

    # -- 内部 --
    def _variants(self, word: str) -> list[str]:
        """生成混淆变体（限制替换位数）。"""
        out: list[str] = []
        for i, ch in enumerate(word):
            cands = self.confusion.get(ch)
            if not cands:
                continue
            for cand in cands:
                if cand != ch:
                    out.append(word[:i] + cand + word[i + 1 :])
        return out

    def _score(self, length: int) -> float:
        base = 0.84 + 0.03 * (length - self.min_word_len)
        return min(base, 0.94)

    # -- 核心 --
    def correct(self, text: str) -> CorrectionResult:
        if not text or not self.confusion or not self.lexicon:
            return CorrectionResult(text, [])

        corrections: list[Correction] = []
        out: list[str] = []
        i = 0
        n = len(text)
        last_consumed = 0

        while i < n:
            hit = False
            for L in range(self.max_word_len, self.min_word_len - 1, -1):
                word = text[i : i + L]
                if len(word) < L or not _CJK.match(word):
                    continue
                if word in self.lexicon:
                    continue
                if not any(ch in self.confusion for ch in word):
                    continue

                best: Optional[str] = None
                best_conf = 0.0
                for var in self._variants(word):
                    if var in self.lexicon:
                        conf = self._score(L)
                        if conf > best_conf:
                            best, best_conf = var, conf
                if best and best_conf >= self.threshold:
                    out.append(text[last_consumed:i])
                    out.append(best)
                    corrections.append(
                        Correction(
                            i,
                            i + L,
                            word,
                            best,
                            self.source_name,
                            "statistical",
                            best_conf,
                            f"统计层：依混淆集与词表将「{word}」判为「{best}」",
                        )
                    )
                    last_consumed = i + L
                    i += L
                    hit = True
                    break
            if not hit:
                i += 1

        if not corrections:
            return CorrectionResult(text, [])
        out.append(text[last_consumed:])
        return CorrectionResult("".join(out), corrections)
