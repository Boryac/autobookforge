"""七项 LLM 任务的实现。

========  ==============================  ============================================
序号      任务                            模块
========  ==============================  ============================================
一       生成章节名（含空白章节补全）      :mod:`.chapter_titles`
二       个性化封面（程序化参数）          :mod:`.cover_params`
三 / 六 / 七  精准校对 / 错别字 / 通顺性    :mod:`.proofread`
四       去广告                            :mod:`.ad_filter`
五       生成目录                          :mod:`.toc`
========  ==============================  ============================================
"""

from __future__ import annotations

from .base import TaskResult
from . import ad_filter, chapter_titles, cover_params, proofread, toc

__all__ = [
    "TaskResult",
    "ad_filter",
    "chapter_titles",
    "cover_params",
    "proofread",
    "toc",
]
