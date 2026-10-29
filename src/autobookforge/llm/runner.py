"""LLM 增强层的统一编排与降本核算。

:class:`LLMRunner` 是流水线与七项任务之间的唯一接口：负责按任务开关调度、
收集用量、汇总降本指标，并在未配置时静默跳过（流水线回落到纯本地路径）。

降本核算的口径在 :meth:`LLMRunner.cost_report` 中逐项写明来源，不做无依据的推算：

* 批量合并 —— 实际请求次数 vs 逐条目调用的次数；
* 规则前置 —— 规则层直接处理、未进入模型的条目占比；
* 摘要输入 —— 若送全文的估算 token vs 实际 prompt token；
* 修改点输出 —— 若重写整段的估算 token vs 实际 completion token；
* 前缀缓存 —— 服务端返回的缓存命中 token 占比；
* 结构化输出 —— 校验重试次数占比。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..core.boilerplate import AdConfig
from .budget import BudgetTier, get_tier
from .client import LLMClient, LLMUsage, ResultCache, cjk_tokens, estimate_tokens
from .settings import LLMSettings, load_settings
from .tasks import ad_filter, chapter_titles, cover_params, proofread, toc
from .tasks.base import TaskResult


@dataclass
class LLMRunReport:
    """一次流水线运行的 LLM 汇总。"""

    enabled: bool = False
    configured: bool = False
    active: bool = False
    model: str = ""
    base_url: str = ""
    budget: str = "standard"
    budget_label: str = "标准"
    tasks: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    design: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "active": self.active,
            "model": self.model,
            "base_url": self.base_url,
            "budget": self.budget,
            "budget_label": self.budget_label,
            "tasks": self.tasks,
            "usage": self.usage,
            "cost": self.cost,
            "design": self.design,
            "notes": self.notes,
        }


class LLMRunner:
    """七项任务的编排器。"""

    def __init__(
        self,
        settings: Optional[LLMSettings] = None,
        *,
        cache_dir: Optional[str] = None,
        use_cache: Optional[bool] = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.usage = LLMUsage()
        cache_path = os.path.join(cache_dir or os.path.join(os.getcwd(), ".abf_cache"), "llm_cache.json")
        enabled_cache = self.settings.use_cache if use_cache is None else bool(use_cache)
        self.cache = ResultCache(cache_path) if enabled_cache else None
        self.client = LLMClient(self.settings, usage=self.usage, cache=self.cache)
        self.tier: BudgetTier = get_tier(self.settings.budget)
        self.results: dict[str, TaskResult] = {}
        self.notes: list[str] = []
        self.design: dict = {}

    # -- 状态 --
    @property
    def available(self) -> bool:
        return self.client.available

    def task_on(self, task_id: str) -> bool:
        return self.settings.task_on(task_id)

    # -- 各任务入口 --
    def filter_ads(self, text: str, cfg: Optional[AdConfig] = None) -> tuple[str, TaskResult]:
        try:
            res, out = ad_filter.filter_text(self.client, text, cfg=cfg, budget=self.settings.budget)
        except Exception as exc:  # 增强层失败不得阻断主流程
            res = TaskResult(task="ad_filter", ok=False, notes=[f"广告兜底判定失败（已跳过）：{exc}"])
            out = text
        self.results["ad_filter"] = res
        return out, res

    def generate_titles(self, book, *, author: str = "") -> TaskResult:
        try:
            res = chapter_titles.generate_titles(
                self.client,
                book.chapters,
                book_title=getattr(book, "title", "") or "",
                author=author,
                tier=self.tier,
            )
        except Exception as exc:  # 增强层失败不得阻断主流程
            res = TaskResult(
                task="chapter_titles",
                ok=False,
                items_total=len(getattr(book, "chapters", []) or []),
                notes=[f"章节名生成失败（已跳过）：{exc}"],
            )
        res.data.setdefault("baseline_chars", sum(len(getattr(c, "content", "") or "") for c in book.chapters))
        self.results["chapter_titles"] = res
        return res

    def proofread(
        self,
        book,
        *,
        base_corrections: Optional[dict] = None,
        base_texts: Optional[dict] = None,
        mode: str = "proofread",
        max_chunks: int = proofread.DEFAULT_MAX_CHUNKS,
    ):
        outcome = None
        try:
            res, outcome = proofread.proofread_chapters(
                self.client,
                book.chapters,
                base_corrections=base_corrections,
                base_texts=base_texts,
                tier=self.tier,
                mode=mode,
                max_chunks=max_chunks,
            )
        except Exception as exc:  # 增强层失败不得阻断主流程
            res = TaskResult(
                task=mode if mode in ("proofread", "spelling", "grammar") else "proofread",
                ok=False,
                items_total=len(getattr(book, "chapters", []) or []),
                notes=[f"大模型校对失败（已跳过）：{exc}"],
            )
        res.data.setdefault(
            "baseline_chars",
            sum(
                len(getattr(c, "content", "") or "")
                for i, c in enumerate(book.chapters)
                if outcome is not None and i in outcome.corrections_by_chapter
            )
            or sum(len(getattr(c, "content", "") or "") for c in book.chapters),
        )
        key = mode if mode in ("proofread", "spelling", "grammar") else "proofread"
        self.results[key] = res
        return res, outcome

    def fix_toc(self, book) -> TaskResult:
        try:
            res = toc.fix_toc(self.client, book.chapters, budget=self.settings.budget)
        except Exception as exc:  # 增强层失败不得阻断主流程
            res = TaskResult(
                task="toc",
                ok=False,
                items_total=len(getattr(book, "chapters", []) or []),
                notes=[f"目录修正失败（已跳过）：{exc}"],
            )
        self.results["toc"] = res
        return res

    def cover_design(
        self,
        *,
        title: str,
        author: str = "",
        chapters: Sequence = (),
        tags: Sequence[str] = (),
        style_hint: str = "",
    ) -> tuple[dict, TaskResult]:
        try:
            res = cover_params.generate_design(
                self.client,
                title=title,
                author=author,
                chapters=chapters,
                tags=tags,
                style_hint=style_hint,
            )
        except Exception as exc:  # 增强层失败不得阻断主流程
            res = TaskResult(task="cover_params", ok=False, notes=[f"封面设计参数生成失败（已回退内置封面）：{exc}"])
        res.data.setdefault("baseline_chars", sum(len(getattr(c, "content", "") or "") for c in chapters))
        self.results["cover_params"] = res
        design = res.data.get("design") or {}
        if design:
            self.design = design
        return design, res

    # -- 汇总 --
    def _task_dicts(self) -> dict:
        return {k: v.as_dict() for k, v in self.results.items()}

    def cost_report(self) -> dict:
        """按统一降本设计逐项核算。"""
        usage = self.usage
        task_dicts = self._task_dicts()

        # 1) 批量合并：实际调用 vs 逐条目调用
        batch_items = sum(t.get("items_sent", 0) for t in task_dicts.values())
        batch_calls = sum(t.get("calls", 0) for t in task_dicts.values())
        batch_saved = max(0.0, 1.0 - (batch_calls / batch_items)) if batch_items else 0.0

        # 2) 规则前置：规则层消化掉的条目占比
        ad = self.results.get("ad_filter")
        rules_total = 0
        rules_skipped = 0
        if ad is not None:
            rules_total = int(ad.data.get("rule_dropped", 0)) + int(ad.items_sent)
            rules_skipped = int(ad.data.get("rule_dropped", 0))
        ct = self.results.get("chapter_titles")
        title_total = int(ct.items_total) if ct else 0
        title_skipped = max(0, title_total - int(ct.items_sent)) if ct else 0
        rules_total += title_total
        rules_skipped += title_skipped
        rules_saved = (rules_skipped / rules_total) if rules_total else 0.0

        # 3) 摘要输入：若送全文的估算 token vs 实际 prompt token
        digest_baseline = 0
        for t in (self.results.get("chapter_titles"), self.results.get("cover_params")):
            if t is not None:
                digest_baseline += cjk_tokens(int(t.data.get("baseline_chars", 0)))
        digest_actual = sum(
            v.prompt_tokens
            for k, v in usage.per_task.items()
            if k in ("chapter_titles", "blank_titles", "cover_params")
        )
        digest_saved = max(0.0, 1.0 - (digest_actual / digest_baseline)) if digest_baseline else 0.0

        # 4) 修改点输出：若重写整段的估算 token vs 实际 completion token
        patch_baseline = 0
        for key in ("proofread", "spelling", "grammar"):
            t = self.results.get(key)
            if t is not None:
                patch_baseline += cjk_tokens(int(t.data.get("baseline_chars", 0)))
        patch_actual = sum(
            v.completion_tokens
            for k, v in usage.per_task.items()
            if k in ("proofread", "spelling", "grammar")
        )
        patch_saved = max(0.0, 1.0 - (patch_actual / patch_baseline)) if patch_baseline else 0.0

        # 5) 前缀缓存
        cache_hit_ratio = (usage.cached_tokens / usage.prompt_tokens) if usage.prompt_tokens else 0.0

        # 6) 结构化输出：重试占比
        retry_ratio = (usage.retries / usage.calls) if usage.calls else 0.0

        return {
            "batch_merge": {
                "calls": batch_calls,
                "sequential_baseline": batch_items,
                "saved_ratio": round(batch_saved, 4),
                "note": "实际请求次数与逐条目调用次数之比",
            },
            "rules_first": {
                "handled_by_rules": rules_skipped,
                "total": rules_total,
                "saved_ratio": round(rules_saved, 4),
                "note": "广告行与章节标题中由规则层直接消化、未进入模型的比例",
            },
            "digest_input": {
                "baseline_tokens": digest_baseline,
                "actual_prompt_tokens": digest_actual,
                "saved_ratio": round(digest_saved, 4),
                "note": "章节名与封面任务：若送正文的估算 token 与实际输入 token 之比"
                        + ("（本次候选章节较少，固定前缀开销占比偏高，长文本下该比例会显著提高）"
                           if digest_baseline and digest_actual >= digest_baseline * 0.6 else ""),
            },
            "patch_output": {
                "baseline_tokens": patch_baseline,
                "actual_completion_tokens": patch_actual,
                "saved_ratio": round(patch_saved, 4),
                "note": "校对任务：若重写整段的估算 token 与实际输出 token 之比"
                        + ("（本次送检文本较少，比例参考意义有限）"
                           if patch_baseline and patch_actual >= patch_baseline * 0.6 else ""),
            },
            "prefix_cache": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_tokens": usage.cached_tokens,
                "hit_ratio": round(cache_hit_ratio, 4),
                "note": "服务端返回的前缀缓存命中 token 占比（依赖服务端支持）",
            },
            "schema_retry": {
                "calls": usage.calls,
                "retries": usage.retries,
                "retry_ratio": round(retry_ratio, 4),
                "note": "字段校验失败引发的修复重试占比",
            },
            "local_cache": {
                "hits": usage.cache_hits,
                "note": "本地结果缓存命中次数（同输入不重复计费）",
            },
        }

    def report(self) -> LLMRunReport:
        rep = LLMRunReport(
            enabled=bool(self.settings.enabled),
            configured=bool(self.settings.configured),
            active=bool(self.settings.active),
            model=self.settings.model,
            base_url=self.settings.base_url,
            budget=self.tier.name,
            budget_label=self.tier.label,
            tasks=self._task_dicts(),
            usage=self.usage.as_dict(),
            cost=self.cost_report() if self.settings.active else {},
            design=self.design,
            notes=list(self.notes),
        )
        for t in self.results.values():
            rep.notes.extend(t.notes)
        return rep

    def task_usage_dict(self) -> dict:
        """按任务拆分的 token 用量，供 UI 展示。"""
        return {k: v.as_dict() for k, v in self.usage.per_task.items()}

    def flush(self) -> None:
        if self.cache is not None:
            self.cache.flush()
