"""Layer 1：规则引擎（YoungCorrector 等价实现）。

纯规则驱动，不依赖深度学习模型或外部 API。加载 JSON 规则库，
定位并修正同音错别字、标点误用、词语搭配不当、重复字词。
毫秒级完成绝大多数常见中文拼写与搭配纠错，并逐条记录修正来源。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_RULES_PATH = _DATA_DIR / "rules.json"


@dataclass
class Correction:
    """单条修正记录。"""

    start: int
    end: int
    original: str
    replacement: str
    source: str
    category: str
    confidence: float
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "original": self.original,
            "replacement": self.replacement,
            "source": self.source,
            "category": self.category,
            "confidence": round(self.confidence, 4),
            "note": self.note,
        }


@dataclass
class CorrectionResult:
    """一次纠错的完整结果。"""

    text: str
    corrections: list[Correction] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.corrections)

    def stats(self) -> dict:
        by_source: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for c in self.corrections:
            by_source[c.source] = by_source.get(c.source, 0) + 1
            by_category[c.category] = by_category.get(c.category, 0) + 1
        return {"total": self.count, "by_source": by_source, "by_category": by_category}

    def as_dict(self) -> dict:
        return {"stats": self.stats(), "corrections": [c.as_dict() for c in self.corrections]}


# ----------------------------------------------------------------------
# 规则装载
# ----------------------------------------------------------------------

@dataclass
class _CompiledRule:
    regex: re.Pattern
    replacement: str
    confidence: float
    category: str
    note: str
    is_regex: bool  # True 用 expand，False 用字面量


def load_rules(rules_path: Optional[str] = None) -> tuple[list[_CompiledRule], dict]:
    """加载规则库，返回 (编译规则列表, 原始数据)。"""
    path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
    if not path.exists():
        return [], {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    compiled: list[_CompiledRule] = []
    for item in data.get("literal", []):
        wrong = str(item.get("wrong", ""))
        if not wrong:
            continue
        try:
            rx = re.compile(re.escape(wrong))
        except re.error:
            continue
        compiled.append(
            _CompiledRule(
                rx,
                str(item.get("right", "")),
                float(item.get("confidence", 0.9)),
                str(item.get("type", "homophone")),
                str(item.get("note", "")),
                False,
            )
        )
    for item in data.get("regex", []):
        pattern = str(item.get("pattern", ""))
        if not pattern:
            continue
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        compiled.append(
            _CompiledRule(
                rx,
                str(item.get("right", "")),
                float(item.get("confidence", 0.9)),
                str(item.get("type", "punctuation")),
                str(item.get("note", "")),
                True,
            )
        )
    return compiled, data


class RuleEngine:
    """规则层纠错引擎。"""

    source_name = "rule"

    def __init__(
        self,
        rules_path: Optional[str] = None,
        *,
        min_confidence: float = 0.90,
        categories: Optional[Sequence[str]] = None,
        custom_rule_files: Optional[Sequence[str]] = None,
    ) -> None:
        self.rules, self.raw = load_rules(rules_path)
        self.min_confidence = min_confidence
        self.categories = set(categories) if categories else None
        for extra in custom_rule_files or []:
            self._load_custom(extra)

    def _load_custom(self, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            extra_rules, _ = load_rules(path)
            self.rules.extend(extra_rules)
        except Exception:
            pass

    # -- 核心 --
    def _collect(self, text: str) -> list[tuple]:
        found: list[tuple] = []
        for rule in self.rules:
            if rule.confidence < self.min_confidence:
                continue
            if self.categories is not None and rule.category not in self.categories:
                continue
            for m in rule.regex.finditer(text):
                original = m.group(0)
                try:
                    repl = m.expand(rule.replacement) if rule.is_regex else rule.replacement
                except (re.error, IndexError):
                    repl = rule.replacement
                if repl == original:
                    continue
                found.append((m.start(), m.end(), repl, rule.confidence, rule.category, rule.note, original))
        return found

    @staticmethod
    def _resolve(found: list[tuple]) -> list[tuple]:
        """按起点贪心解决重叠：起点相同的取置信度高、长度长者。"""
        found.sort(key=lambda x: (x[0], -x[3], -(x[1] - x[0])))
        chosen: list[tuple] = []
        last_end = -1
        for item in found:
            if item[0] >= last_end:
                chosen.append(item)
                last_end = item[1]
        return chosen

    def correct(self, text: str) -> CorrectionResult:
        if not text:
            return CorrectionResult(text, [])
        chosen = self._resolve(self._collect(text))

        out: list[str] = []
        pos = 0
        corrections: list[Correction] = []
        for start, end, repl, conf, cat, note, original in chosen:
            out.append(text[pos:start])
            out.append(repl)
            corrections.append(
                Correction(start, end, original, repl, self.source_name, cat, conf, note)
            )
            pos = end
        out.append(text[pos:])
        return CorrectionResult("".join(out), corrections)
