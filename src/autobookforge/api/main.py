"""FastAPI 应用入口。

托管 REST API 与单文件 Web UI（web/index.html），
并静态挂载 vendor 资源与项目 assets（Logo / 封面）。
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .routes import router

ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = ROOT / "web"
ASSETS_DIR = ROOT / "assets"

app = FastAPI(
    title="AutoBookForge",
    version=__version__,
    description="全自动 TXT 精排电子书生产引擎（零 API 成本，纯本地 CPU）",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

logger = logging.getLogger("autobookforge")


@app.exception_handler(Exception)
def _unhandled(request: Request, exc: Exception):  # noqa: ANN201
    """未捕获异常：落日志并返回结构化 detail，避免前端只看到一个空 500。"""
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    try:
        logger.error("Unhandled error on %s\n%s", request.url.path, "".join(tb))
    except Exception:
        pass
    return JSONResponse(
        {
            "detail": "%s: %s" % (type(exc).__name__, exc),
            "path": request.url.path,
            "traceback": "".join(tb[-8:]),
        },
        status_code=500,
    )


@app.get("/app.css")
def app_css():
    return FileResponse(str(WEB_DIR / "app.css"), media_type="text/css")


@app.get("/app.js")
def app_js():
    return FileResponse(str(WEB_DIR / "app.js"), media_type="application/javascript")


@app.get("/")
def index():
    page = WEB_DIR / "index.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"service": "AutoBookForge", "version": __version__, "hint": "web/index.html 缺失"})


@app.get("/logo.jpg")
def logo():
    for cand in (WEB_DIR / "logo.jpg", ASSETS_DIR / "logo.jpg"):
        if cand.exists():
            return FileResponse(str(cand))
    return JSONResponse({}, status_code=204)


@app.get("/favicon.ico")
def favicon():
    fav = ASSETS_DIR / "logo.jpg"
    if fav.exists():
        return FileResponse(str(fav))
    return JSONResponse({}, status_code=204)


if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
