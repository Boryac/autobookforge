"""任务层公共结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskResult:
    """单个 LLM 任务的执行结果与降本核算。"""

    task: str
    ok: bool = False
    skipped: str = ""            # 跳过原因（未配置 / 规则已覆盖 / 无候选）
    calls: int = 0               # 实际请求次数
    items_sent: int = 0          # 送入模型的条目数（章节 / 文本块 / 行）
    items_total: int = 0         # 全量条目数，用于核算节省比例
    applied: int = 0             # 实际生效的改写条数
    data: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def saved_ratio(self) -> float:
        """相对「逐条调用」的调用量节省比例。"""
        if not self.items_total:
            return 0.0
        return max(0.0, 1.0 - (self.calls / self.items_total)) if self.items_total else 0.0

    def as_dict(self) -> dict:
        return {
            "task": self.task,
            "ok": self.ok,
            "skipped": self.skipped,
            "calls": self.calls,
            "items_sent": self.items_sent,
            "items_total": self.items_total,
            "applied": self.applied,
            "saved_ratio": round(self.saved_ratio, 4),
            "notes": list(self.notes),
        }
