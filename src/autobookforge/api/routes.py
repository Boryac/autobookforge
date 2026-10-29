"""REST API 路由。

提供上传、任务状态、纠错报告、下载、纠错预览等端点。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .. import __version__
from ..core.epub_builder import INDENT_MODES, ORNAMENT_MODES, STYLE_ALIASES, style_choices
from ..core.fonts import list_font_options
from ..core.mobi_converter import find_calibre
from ..core.boilerplate import AdConfig
from ..corrector.engine import create_engine_from_config
from ..corrector.report import build_report, to_json, to_markdown
from ..cover.generator import available_styles as cover_styles
from ..llm.budget import TIERS
from ..llm.client import LLMClient
from ..llm.runner import LLMRunner
from ..llm.settings import (
    COST_STRATEGIES,
    TASK_DESCRIPTIONS,
    TASK_IDS,
    TASK_LABELS,
    THINKING_HINT,
    THINKING_LABELS,
    THINKING_MODES,
    LLMSettings,
    load_settings as load_llm_settings,
    save_settings as save_llm_settings,
)
from ..llm.tasks import ad_filter as llm_ad_filter
from ..pipeline.config import default_ad_config_path, default_config_path, load_config
from .jobs import OPTIONAL_STAGES, STAGE_ORDER, manager

router = APIRouter(prefix="/api")

ROOT = Path(__file__).resolve().parents[3]
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "output"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_runtime_dirs() -> None:
    """运行时目录可能被外部清理，任何写文件动作前按需重建。"""
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


class PreviewRequest(BaseModel):
    text: str
    level: int = 2


class ConvertLocalRequest(BaseModel):
    path: str
    style: str = "literary"
    correct_level: int = 2
    cover: bool = True
    cover_style: str = "auto"
    title: str = ""
    author: str = ""
    formats: list[str] = ["epub"]
    llm: bool = False
    llm_budget: str = ""
    font_embed: str = ""
    font_format: str = ""
    font_body: str = ""
    font_title: str = ""
    indent_mode: str = "space"
    page_ornament: str = "chapter"
    title_page: bool = True
    vertical: bool = False


class LLMSettingsRequest(BaseModel):
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: int = 60
    retries: int = 2
    concurrency: int = 4
    budget: str = "standard"
    json_mode: bool = True
    prefix_cache: bool = True
    use_cache: bool = True
    thinking: str = "auto"
    tasks: dict = {}


class LLMTestRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 60
    thinking: str = "auto"


class AdPreviewRequest(BaseModel):
    text: str
    use_llm: bool = False


@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "service": "AutoBookForge"}


@router.get("/config")
def get_config() -> dict:
    cfg = load_config()
    return {
        "version": __version__,
        "styles": sorted(set(STYLE_ALIASES.keys())),
        "style_choices": style_choices(),
        "fonts": list_font_options(),
        "indent_modes": list(INDENT_MODES),
        "ornament_modes": list(ORNAMENT_MODES),
        "cover_styles": list(cover_styles()),
        "stages": STAGE_ORDER,
        "calibre_available": find_calibre() is not None,
        "pipeline": cfg.as_dict(),
        "config_path": default_config_path(),
        "llm": {
            "task_ids": list(TASK_IDS),
            "task_labels": TASK_LABELS,
            "task_descriptions": TASK_DESCRIPTIONS,
            "budgets": {k: v.as_dict() for k, v in TIERS.items()},
            "strategies": list(COST_STRATEGIES),
            "optional_stages": list(OPTIONAL_STAGES),
        },
    }


@router.get("/fonts")
def get_fonts() -> dict:
    """可用字体清单、角色映射与嵌入档位（供 Web 下拉使用）。"""
    return list_font_options()


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    style: str = Form("literary"),
    correct_level: int = Form(2),
    cover: bool = Form(True),
    cover_style: str = Form("auto"),
    title: str = Form(""),
    author: str = Form(""),
    formats: str = Form("epub"),
    llm: bool = Form(False),
    llm_budget: str = Form(""),
    font_embed: str = Form(""),
    font_format: str = Form(""),
    font_body: str = Form(""),
    font_title: str = Form(""),
    indent_mode: str = Form("space"),
    page_ornament: str = Form("chapter"),
    title_page: bool = Form(True),
    vertical: bool = Form(False),
) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if not file.filename.lower().endswith((".txt", ".text")):
        raise HTTPException(status_code=400, detail="仅支持 TXT 文件")

    _ensure_runtime_dirs()
    safe_name = os.path.basename(file.filename)
    saved = UPLOAD_DIR / safe_name
    content = await file.read()
    saved.write_bytes(content)

    opts = {
        "output_dir": str(OUTPUT_DIR),
        "style": style,
        "correct_level": int(correct_level),
        "cover": bool(cover),
        "cover_style": cover_style,
        "title": title,
        "author": author,
        "formats": [f.strip().lower() for f in formats.split(",") if f.strip()],
        "llm": bool(llm),
        "llm_budget": llm_budget,
        "font_embed": font_embed,
        "font_format": font_format,
        "font_body": font_body,
        "font_title": font_title,
        "indent_mode": indent_mode,
        "page_ornament": page_ornament,
        "title_page": bool(title_page),
        "vertical": bool(vertical),
    }
    job = manager.create(safe_name, str(saved), opts)
    manager.run_async(job)
    return {"job_id": job.id, "filename": safe_name, "size": len(content)}


@router.post("/convert-local")
def convert_local(req: ConvertLocalRequest) -> dict:
    _ensure_runtime_dirs()
    if not os.path.exists(req.path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.path}")
    opts = {
        "output_dir": str(OUTPUT_DIR),
        "style": req.style,
        "correct_level": req.correct_level,
        "cover": req.cover,
        "cover_style": req.cover_style,
        "title": req.title,
        "author": req.author,
        "formats": req.formats,
        "llm": bool(req.llm),
        "llm_budget": req.llm_budget,
        "font_embed": req.font_embed,
        "font_format": req.font_format,
        "font_body": req.font_body,
        "font_title": req.font_title,
        "indent_mode": req.indent_mode,
        "page_ornament": req.page_ornament,
        "title_page": bool(req.title_page),
        "vertical": bool(req.vertical),
    }
    job = manager.create(os.path.basename(req.path), req.path, opts)
    manager.run_async(job)
    return {"job_id": job.id, "filename": os.path.basename(req.path)}


@router.get("/jobs")
def list_jobs() -> dict:
    jobs = manager.list()
    return {"jobs": [j.as_dict(with_result=False) for j in jobs]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.as_dict(with_result=True)


@router.get("/jobs/{job_id}/report")
def get_report(job_id: str, format: str = "json"):
    job = manager.get(job_id)
    if job is None or job.result is None:
        raise HTTPException(status_code=404, detail="任务不存在或尚未完成")
    report = job.result.get("report", {})
    if format == "md":
        return PlainTextResponse(to_markdown(report), media_type="text/markdown; charset=utf-8")
    return report


@router.get("/jobs/{job_id}/download")
def download(job_id: str, fmt: str = "epub"):
    job = manager.get(job_id)
    if job is None or job.result is None:
        raise HTTPException(status_code=404, detail="任务不存在或尚未完成")
    files = job.result.get("output_files", {})
    path = files.get(fmt.lower())
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"未生成 {fmt} 格式")
    return FileResponse(path, filename=os.path.basename(path))


@router.get("/jobs/{job_id}/cover")
def get_cover(job_id: str):
    job = manager.get(job_id)
    if job is None or job.result is None:
        raise HTTPException(status_code=404, detail="任务不存在或尚未完成")
    cover = job.result.get("cover_path")
    if not cover or not os.path.exists(cover):
        raise HTTPException(status_code=404, detail="未生成封面")
    return FileResponse(cover, filename=os.path.basename(cover))


@router.post("/preview-correct")
def preview_correct(req: PreviewRequest) -> dict:
    """对话式纠错预览：返回 diff 分段与统计，用于 UI 左右分栏。"""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="文本为空")
    engine = create_engine_from_config(default_config_path(), level=max(1, min(2, req.level)))
    res = engine.correct(req.text)
    report = build_report(req.text, res.text, res.corrections, title="预览", with_segments=True)
    return report


@router.get("/report/{job_id}.json", response_class=PlainTextResponse)
def report_json(job_id: str) -> str:
    job = manager.get(job_id)
    if job is None or job.result is None:
        raise HTTPException(status_code=404, detail="任务不存在或尚未完成")
    return to_json(job.result.get("report", {}))


# --------------------------------------------------------------------------- #
# LLM 设置与诊断
# --------------------------------------------------------------------------- #


def _llm_payload(settings: LLMSettings) -> dict:
    data = settings.as_dict(masked=True)
    data["task_ids"] = list(TASK_IDS)
    data["task_labels"] = TASK_LABELS
    data["task_descriptions"] = TASK_DESCRIPTIONS
    data["budgets"] = {k: v.as_dict() for k, v in TIERS.items()}
    data["strategies"] = list(COST_STRATEGIES)
    data["thinking_modes"] = [
        {"id": m, "name": THINKING_LABELS.get(m, m)} for m in THINKING_MODES
    ]
    data["thinking_hint"] = THINKING_HINT
    data["saved_path"] = str(load_llm_settings_path())
    return data


def load_llm_settings_path() -> str:
    from ..llm.settings import LOCAL_JSON_PATH

    return str(LOCAL_JSON_PATH)


@router.get("/llm/settings")
def llm_settings_get() -> dict:
    return _llm_payload(load_llm_settings())


@router.post("/llm/settings")
def llm_settings_post(req: LLMSettingsRequest) -> dict:
    settings = load_llm_settings()
    settings.enabled = bool(req.enabled)
    if req.base_url.strip():
        settings.base_url = req.base_url.strip()
    # 界面回显的是打码串，只有用户真正输入新密钥时才覆盖
    if req.api_key.strip() and "****" not in req.api_key:
        settings.api_key = req.api_key.strip()
    if req.model.strip():
        settings.model = req.model.strip()
    settings.temperature = max(0.0, min(2.0, float(req.temperature)))
    settings.max_tokens = max(64, min(32000, int(req.max_tokens)))
    settings.timeout = max(5, min(600, int(req.timeout)))
    settings.retries = max(0, min(8, int(req.retries)))
    settings.concurrency = max(1, min(16, int(req.concurrency)))
    settings.budget = req.budget if req.budget in TIERS else "standard"
    settings.json_mode = bool(req.json_mode)
    settings.prefix_cache = bool(req.prefix_cache)
    settings.use_cache = bool(req.use_cache)
    settings.thinking = req.thinking if req.thinking in THINKING_MODES else "auto"
    if isinstance(req.tasks, dict) and req.tasks:
        tasks = dict(settings.tasks)
        for tid in TASK_IDS:
            if tid in req.tasks:
                tasks[tid] = bool(req.tasks[tid])
        settings.tasks = tasks
    saved = save_llm_settings(settings)
    data = _llm_payload(load_llm_settings())
    data["saved_path"] = saved
    return data


def _apply_overrides(req: LLMTestRequest) -> LLMSettings:
    settings = load_llm_settings()
    if req.base_url.strip():
        settings.base_url = req.base_url.strip()
    if req.api_key.strip() and "****" not in req.api_key:
        settings.api_key = req.api_key.strip()
    if req.model.strip():
        settings.model = req.model.strip()
    if req.timeout:
        settings.timeout = max(5, min(600, int(req.timeout)))
    if req.thinking in THINKING_MODES:
        settings.thinking = req.thinking
    return settings


@router.post("/llm/test")
def llm_test(req: LLMTestRequest) -> dict:
    """连通性测试：可直接使用界面当前填写的参数，无需先保存。"""
    return LLMClient(_apply_overrides(req)).test_connection()


@router.post("/llm/models")
def llm_models(req: LLMTestRequest) -> dict:
    res = LLMClient(_apply_overrides(req)).test_connection()
    return {"ok": res.get("ok"), "models": res.get("models", []), "detail": res.get("detail", ""),
            "latency_ms": res.get("latency_ms", 0)}


@router.post("/llm/preview-ad")
def llm_preview_ad(req: AdPreviewRequest) -> dict:
    """去广告预览：规则层零成本统计；可选择叠加模型的语境判定。"""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="文本为空")
    cfg = AdConfig.load(default_ad_config_path())
    data: dict = dict(llm_ad_filter.preview(req.text, cfg))
    data["use_llm"] = bool(req.use_llm)
    data["suspicious_lines"] = [
        {"index": i, "line": line} for i, line in llm_ad_filter.suspicious_lines(req.text, cfg)[:30]
    ]
    settings = load_llm_settings()
    if req.use_llm:
        if not settings.active:
            data["llm"] = {"skipped": "LLM 未配置或未启用"}
        else:
            runner = LLMRunner(settings)
            _out, res = runner.filter_ads(req.text, cfg=cfg)
            detail = res.as_dict()
            detail["data"] = res.data
            data["llm"] = detail
    return data
