"""任务五：生成目录（层级化结构）。

目录本身由章节识别模块直接产出，**默认不调用模型**（成本为零）。只有用户显式开启
「目录层级修正」时，才把章节标题列表送模型，用于三类明显错乱：

* 卷 / 章 / 节层级识别错误；
* 标题中存在显而易见的错别字；
* 目录页残留导致同一标题重复出现。

降本要点：输入仅章节标题与字数（不含正文），且一次请求返回全部层级。
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..budget import get_tier, iter_batches
from ..client import LLMClient, LLMError
from ..prompts import user_toc
from ..schema import SCHEMA_HINTS, SCHEMA_TOC
from .base import TaskResult

TITLE_LIMIT = 300


def fix_toc(
    client: LLMClient,
    chapters: Sequence,
    *,
    limit: int = TITLE_LIMIT,
    budget: Optional[str] = None,
) -> TaskResult:
    """修正目录层级与标题错别字，就地写回 ``ch.title`` / ``ch.level``。"""
    res = TaskResult(task="toc", items_total=len(chapters))
    if not chapters:
        res.skipped = "无章节"
        return res
    if not client.available:
        res.skipped = "LLM 未配置"
        return res
    if not client.settings.task_on("toc"):
        res.skipped = "目录层级修正默认关闭（章节识别结果已直接构建目录）"
        return res

    tier = get_tier(budget or client.settings.budget)
    items = [
        {
            "index": int(getattr(ch, "index", i)),
            "title": str(getattr(ch, "title", "") or "")[:60],
            "chars": len(getattr(ch, "content", "") or ""),
        }
        for i, ch in enumerate(chapters)
    ][:limit]

    pos_by_index: dict[int, int] = {}
    for i, ch in enumerate(chapters):
        pos_by_index[int(getattr(ch, "index", i))] = i

    applied = 0
    toc_out: list[dict] = []
    for group in iter_batches(items, tier.batch_size):
        user = user_toc(group)
        try:
            obj = client.chat_json(
                "toc",
                user,
                schema=SCHEMA_TOC,
                schema_hint=SCHEMA_HINTS["toc"],
                temperature=0.1,
                max_tokens=min(client.settings.max_tokens, 2600),
            )
        except LLMError as exc:
            res.notes.append(f"目录修正失败（保留原目录）：{exc}")
            continue
        res.calls += 1
        res.items_sent += len(group)
        for row in (obj or {}).get("toc", []) or []:
            try:
                idx = int(row.get("index"))
                level = int(row.get("level", 2))
            except (TypeError, ValueError):
                continue
            pos = pos_by_index.get(idx)
            if pos is None:
                continue
            ch = chapters[pos]
            title = str(row.get("title") or "").strip()
            if title and title != ch.title:
                ch.title = title[:60]
                applied += 1
            if level in (1, 2, 3) and level != ch.level:
                ch.level = level
                applied += 1
            toc_out.append(
                {
                    "index": idx,
                    "title": ch.title,
                    "level": ch.level,
                    "parent": row.get("parent"),
                }
            )

    res.applied = applied
    res.ok = applied > 0
    res.data["toc"] = toc_out
    return res
