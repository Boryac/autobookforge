"""LLM 增强层。

对外入口：

* :class:`~autobookforge.llm.settings.LLMSettings` —— 用户填写的 API 地址 / 密钥 / 模型与任务开关
* :class:`~autobookforge.llm.client.LLMClient` —— OpenAI 兼容客户端（零第三方依赖）
* :class:`~autobookforge.llm.runner.LLMRunner` —— 七项任务的统一编排与降本核算

设计前提：LLM 是**可选的增强层**。未配置时整层静默跳过，流水线仍以纯本地 CPU 完成
清洗、分章、双层纠错、排版与打包，零 API 成本这一基础能力不受影响。
"""

from __future__ import annotations

from .budget import DEFAULT_TIER, TIERS, BudgetTier, get_tier, tier_names
from .client import LLMClient, LLMError, LLMUsage, ResultCache, estimate_tokens
from .settings import (
    COST_STRATEGIES,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LOCAL_JSON_PATH,
    TASK_DESCRIPTIONS,
    TASK_IDS,
    TASK_LABELS,
    LLMSettings,
    load_settings,
    mask_key,
    save_settings,
)

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMSettings",
    "LLMUsage",
    "ResultCache",
    "BudgetTier",
    "COST_STRATEGIES",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIER",
    "LOCAL_JSON_PATH",
    "TASK_DESCRIPTIONS",
    "TASK_IDS",
    "TASK_LABELS",
    "TIERS",
    "estimate_tokens",
    "get_tier",
    "load_settings",
    "mask_key",
    "save_settings",
    "tier_names",
]
