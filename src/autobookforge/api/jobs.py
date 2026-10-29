"""后台任务管理与进度上报。

FastAPI 请求创建任务后立即返回 job_id，后台线程执行流水线，
前端轮询 /api/jobs/{id} 获取阶段与百分比进度。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..pipeline.config import load_config
from ..pipeline.orchestrator import AutoBookPipeline, PipelineOptions, PipelineResult

# 各阶段在总进度中的权重
STAGE_WEIGHTS: dict[str, tuple[float, float]] = {
    "encoding": (0.00, 0.02),
    "ad_filter_llm": (0.02, 0.05),
    "cleaning": (0.05, 0.10),
    "splitting": (0.10, 0.16),
    "title_llm": (0.16, 0.22),
    "correcting": (0.22, 0.52),
    "proof_llm": (0.52, 0.74),
    "toc_llm": (0.74, 0.77),
    "cover": (0.77, 0.82),
    "fonts": (0.82, 0.87),
    "epub": (0.87, 0.95),
    "convert": (0.95, 1.00),
}

# 未启用 LLM 时不会出现的阶段（用于前端跳过展示）
OPTIONAL_STAGES = ("ad_filter_llm", "title_llm", "proof_llm", "toc_llm")

STAGE_LABELS = {
    "encoding": "编码检测",
    "ad_filter_llm": "广告兜底判定",
    "cleaning": "文本清洗",
    "splitting": "章节识别",
    "title_llm": "章节名生成",
    "correcting": "纠错处理",
    "proof_llm": "大模型校对",
    "toc_llm": "目录层级修正",
    "cover": "封面生成",
    "fonts": "内嵌字体",
    "epub": "EPUB 生成",
    "convert": "格式转换",
}

STAGE_ORDER = list(STAGE_WEIGHTS.keys())


@dataclass
class Job:
    id: str
    filename: str
    input_path: str
    status: str = "pending"  # pending / running / done / error
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    result: Optional[dict] = None
    error: str = ""
    options: dict = field(default_factory=dict)

    def as_dict(self, *, with_result: bool = True) -> dict:
        data = {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "progress": round(self.progress, 4),
            "message": self.message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "options": self.options,
        }
        if with_result and self.result is not None:
            data["result"] = self.result
        return data


class JobManager:
    """进程内任务管理器（线程安全）。"""

    def __init__(self, *, max_jobs: int = 200) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.max_jobs = max_jobs

    def create(self, filename: str, input_path: str, options: dict) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            filename=filename,
            input_path=input_path,
            options=options,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()
        return job

    def _evict_locked(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at)
        for old in ordered[: len(self._jobs) - self.max_jobs]:
            if old.status in ("done", "error"):
                self._jobs.pop(old.id, None)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    # -- 执行 --
    def _update_progress(self, job: Job):
        def cb(stage: str, cur: int, total: int, msg: str) -> None:
            job.stage = stage
            job.message = msg
            lo, hi = STAGE_WEIGHTS.get(stage, (job.progress, job.progress))
            if total > 1:
                frac = min(1.0, cur / total)
            else:
                frac = 1.0 if "完成" in msg or "生成" in msg or "转换" in msg else 0.5
            job.progress = lo + (hi - lo) * frac

        return cb

    def run_async(self, job: Job) -> None:
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.stage = "encoding"
        job.message = "开始处理"
        try:
            config = load_config()
            pipeline = AutoBookPipeline(config)
            opts = job.options
            options = PipelineOptions(
                output_dir=opts.get("output_dir"),
                formats=opts.get("formats", ["epub"]),
                style=opts.get("style", "literary"),
                correct_level=int(opts.get("correct_level", 2)),
                cover=bool(opts.get("cover", True)),
                cover_style=opts.get("cover_style", "auto"),
                title=opts.get("title", ""),
                author=opts.get("author", ""),
                llm=bool(opts.get("llm", False)),
                llm_budget=str(opts.get("llm_budget", "") or ""),
                llm_tasks=opts.get("llm_tasks") or None,
                font_embed=str(opts.get("font_embed") or ""),
                font_format=str(opts.get("font_format") or ""),
                font_body=str(opts.get("font_body") or ""),
                font_title=str(opts.get("font_title") or ""),
                indent_mode=str(opts.get("indent_mode") or "space"),
                title_page=bool(opts.get("title_page", True)),
                page_ornament=str(opts.get("page_ornament") or "chapter"),
                vertical=bool(opts.get("vertical", False)),
            )
            result: PipelineResult = pipeline.run(job.input_path, options, self._update_progress(job))
            if result.success:
                job.status = "done"
                job.progress = 1.0
                job.message = result.message
                job.result = result.as_dict()
            else:
                job.status = "error"
                job.error = result.message
                job.message = result.message
        except Exception as exc:  # pragma: no cover
            job.status = "error"
            job.error = str(exc)
            job.message = f"处理异常: {exc}"
        finally:
            job.finished_at = time.time()


manager = JobManager()
