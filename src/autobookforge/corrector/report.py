"""纠错报告：逐条修正记录、来源标注、diff 分段与多格式导出。

满足 PRD 验收标准「每处修正标注来源层级（规则 / 统计）」与「纠错报告可追溯」。
"""

from __future__ import annotations

import html
import json
from typing import Iterable, Optional, Sequence

from .rule_engine import Correction


def build_segments(original: str, corrections: Sequence[Correction]) -> list[dict]:
    """把原文按修正位置切分为 diff 分段，供左右分栏 / 高亮渲染。"""
    ordered = sorted(corrections, key=lambda c: c.start)
    segments: list[dict] = []
    pos = 0
    for c in ordered:
        if c.start < pos:  # 跳过重叠
            continue
        if c.start > pos:
            segments.append({"text": original[pos:c.start], "changed": False})
        segments.append(
            {
                "text": original[c.start:c.end],
                "changed": True,
                "replacement": c.replacement,
                "source": c.source,
                "category": c.category,
                "confidence": round(c.confidence, 4),
                "note": c.note,
                "start": c.start,
                "end": c.end,
            }
        )
        pos = c.end
    if pos < len(original):
        segments.append({"text": original[pos:], "changed": False})
    return segments


def build_report(
    original: str,
    corrected: str,
    corrections: Sequence[Correction],
    *,
    title: str = "",
    extra: Optional[dict] = None,
    with_segments: bool = True,
) -> dict:
    """构建报告数据结构。"""
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for c in corrections:
        by_source[c.source] = by_source.get(c.source, 0) + 1
        by_category[c.category] = by_category.get(c.category, 0) + 1

    report = {
        "title": title,
        "summary": {
            "total_corrections": len(corrections),
            "by_source": by_source,
            "by_category": by_category,
            "original_chars": len(original),
            "corrected_chars": len(corrected),
        },
        "corrections": [c.as_dict() for c in corrections],
    }
    if with_segments:
        report["segments"] = build_segments(original, corrections)
    if extra:
        report.update(extra)
    return report


def to_json(report: dict, *, indent: int = 2) -> str:
    return json.dumps(report, ensure_ascii=False, indent=indent)


def to_markdown(report: dict) -> str:
    s = report.get("summary", {})
    src = s.get("by_source", {})
    heading = "# 纠错报告"
    if report.get("title"):
        heading += f"：{report['title']}"
    lines = [
        heading,
        "",
        "## 统计",
        "",
        f"- 修正总数：{s.get('total_corrections', 0)} 处",
        f"- 规则层（rule）：{src.get('rule', 0)} 处",
        f"- 统计层（ngram）：{src.get('ngram', 0)} 处",
        f"- 原文字数：{s.get('original_chars', 0)}",
        "",
        "## 逐条明细",
        "",
        "| 序号 | 位置 | 原文 | 修正 | 来源 | 类型 | 置信度 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for idx, c in enumerate(report.get("corrections", []), 1):
        lines.append(
            "| {i} | {p} | {o} | {r} | {s} | {t} | {c} | {n} |".format(
                i=idx,
                p=c.get("start", 0),
                o=(c.get("original") or "").replace("|", "\\|"),
                r=(c.get("replacement") or "").replace("|", "\\|"),
                s=c.get("source", ""),
                t=c.get("category", ""),
                c=c.get("confidence", ""),
                n=(c.get("note") or "").replace("|", "\\|"),
            )
        )
    return "\n".join(lines)


def segments_to_html(segments: Iterable[dict]) -> str:
    """把 diff 分段渲染为高亮 HTML（<del>/<ins> 标注修正，含来源徽标）。"""
    out: list[str] = []
    for seg in segments:
        if not seg.get("changed"):
            out.append(html.escape(seg.get("text", "")).replace("\n", "<br>"))
            continue
        src = seg.get("source", "")
        badge = "规则" if src == "rule" else "统计"
        note = html.escape(seg.get("note", ""))
        original_text = html.escape(seg.get("text", ""))
        replacement = html.escape(seg.get("replacement", ""))
        out.append(
            f'<del class="abf-del" title="{note}">{original_text}</del>'
            f'<ins class="abf-ins" data-source="{src}" title="{note}">{replacement}'
            f'<span class="abf-badge">{badge}</span></ins>'
        )
    return "".join(out)


def to_html_fragment(report: dict) -> str:
    """生成纠错报告 HTML 片段（供 Web UI diff 视图使用）。"""
    return segments_to_html(report.get("segments", []))
