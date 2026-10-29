"""双层纠错管线编排。

Layer 1 规则引擎 -> Layer 2 统计层。提供 correct-level 1/2 两档：
  1 = 仅规则引擎（最快，零模型依赖，毫秒级）
  2 = 规则引擎 + 统计层（推荐，CPU 即可，秒级）
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

from .ngram import NGramCorrector
from .rule_engine import Correction, CorrectionResult, RuleEngine


def _map_back(pos_in_mid: int, rule_corrections: Sequence[Correction]) -> int:
    """把统计层输出坐标映射回原文坐标（近似，用于报告可追溯）。"""
    offset = 0
    for c in rule_corrections:
        orig_len = c.end - c.start
        new_len = len(c.replacement)
        mid_start = c.start + offset
        mid_end = mid_start + new_len
        if pos_in_mid <= mid_start:
            break
        if pos_in_mid <= mid_end:
            return c.start
        offset += new_len - orig_len
    return max(0, pos_in_mid - offset)


class CorrectionEngine:
    """双层纠错引擎。"""

    def __init__(
        self,
        level: int = 2,
        *,
        rules_path: Optional[str] = None,
        confusion_path: Optional[str] = None,
        lexicon_path: Optional[str] = None,
        min_confidence: float = 0.90,
        categories: Optional[Sequence[str]] = None,
        custom_rule_files: Optional[Sequence[str]] = None,
        ngram_threshold: float = 0.82,
        ngram_context_window: int = 3,
        cache=None,
    ) -> None:
        self.level = max(1, min(2, int(level)))
        self.rule = RuleEngine(
            rules_path,
            min_confidence=min_confidence,
            categories=categories,
            custom_rule_files=custom_rule_files,
        )
        self.ngram = (
            NGramCorrector(
                confusion_path,
                lexicon_path,
                threshold=ngram_threshold,
                context_window=ngram_context_window,
            )
            if self.level >= 2
            else None
        )
        self.cache = cache

    # -- 对外 --
    def correct(self, text: str) -> CorrectionResult:
        if not text:
            return CorrectionResult(text, [])

        cache_key = None
        if self.cache is not None:
            cache_key = self.cache.make_key(text, self.level)
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        rule_result = self.rule.correct(text)
        corrections: list[Correction] = list(rule_result.corrections)
        mid_text = rule_result.text
        final_text = mid_text

        if self.ngram is not None:
            ngram_result = self.ngram.correct(mid_text)
            for c in ngram_result.corrections:
                start = _map_back(c.start, rule_result.corrections)
                end = _map_back(c.end, rule_result.corrections)
                corrections.append(
                    Correction(
                        start,
                        end,
                        c.original,
                        c.replacement,
                        "ngram",
                        c.category,
                        c.confidence,
                        c.note,
                    )
                )
            final_text = ngram_result.text

        corrections.sort(key=lambda c: c.start)
        result = CorrectionResult(final_text, corrections)

        if self.cache is not None and cache_key is not None:
            self.cache.set(cache_key, result)
        return result


def create_engine_from_config(config_path: Optional[str] = None, *, level: int = 2, cache=None) -> CorrectionEngine:
    """从 correct_rules.yaml 创建引擎。"""
    params: dict = {}
    if config_path and os.path.exists(config_path):
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            rule_cfg = data.get("rule_layer", {}) or {}
            stat_cfg = data.get("statistical_layer", {}) or {}
            params["min_confidence"] = float(rule_cfg.get("min_confidence", 0.90))
            params["categories"] = rule_cfg.get("categories")
            params["custom_rule_files"] = rule_cfg.get("custom_rule_files")
            params["ngram_threshold"] = float(stat_cfg.get("threshold", 0.82))
            params["ngram_context_window"] = int(stat_cfg.get("context_window", 3))
        except Exception:
            params = {}
    return CorrectionEngine(level=level, cache=cache, **params)
