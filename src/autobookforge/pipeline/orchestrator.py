"""流水线编排器。

串联完整链路：编码检测 -> 文本清洗 -> 章节识别 -> 双层纠错 -> 封面生成
-> EPUB 生成 -> MOBI/AZW3 转换。支持进度回调、纠错缓存与配置驱动。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..core.boilerplate import AdConfig
from ..core.cleaner import DEFAULT_AD_PATTERNS, clean_text, prepare
from ..core.encoding import detect_and_convert
from ..core.epub_builder import build_epub, check_epub_structure, normalize_style
from ..core.fonts import FontRegistry
from ..core.mobi_converter import convert_formats, is_available as calibre_available
from ..core.splitter import BookSplit, infer_title, split_chapters
from ..corrector.engine import create_engine_from_config
from ..corrector.report import build_segments
from ..cover.design_render import design_summary, render_design_cover
from ..cover.generator import generate_cover
from ..llm.runner import LLMRunner
from ..llm.settings import LLMSettings, load_settings as load_llm_settings
from .cache import CorrectionCache
from .config import (
    PipelineConfig,
    default_ad_config_path,
    default_chapter_config_path,
    default_config_path,
    load_config,
)

ProgressCB = Callable[[str, int, int, str], None]

STAGES = [
    ("encoding", "编码检测"),
    ("cleaning", "文本清洗"),
    ("ad_filter_llm", "广告兜底判定"),
    ("splitting", "章节识别"),
    ("title_llm", "章节名生成"),
    ("correcting", "纠错处理"),
    ("proof_llm", "大模型校对"),
    ("toc_llm", "目录修正"),
    ("cover", "封面生成"),
    ("epub", "EPUB 生成"),
    ("convert", "格式转换"),
]

_BOOKNAME_JUNK = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|（[^）]*）|《|》")
_AUTHOR_RE = re.compile(r"作\s*者\s*[:：]\s*([^\s，,。；;、/【】\[\]（）()｜|]{1,20})")
_ENCODING_SUFFIX = re.compile(r"[\s_\-]*(utf-?8|utf8|gbk|gb2312|gb18030|big5|ansi|unicode)$", re.IGNORECASE)
_NAME_AUTHOR_CUT = re.compile(r"(作\s*者|著者|by|BY|By)\s*[:：]?\s*.*$")
_AUTHOR_TAG_SUFFIX = re.compile(
    r"(科幻|言情|都市|玄幻|武侠|历史|悬疑|灵异|游戏|竞技|同人|完结|番外|上部|中部|下部|全集|全本|原文|翻译|校对|精校)$"
)


def _noop(stage: str, cur: int, total: int, msg: str) -> None:  # pragma: no cover
    return None


def clean_bookname(name: str) -> str:
    """清洗文件名得到书名。"""
    name = re.sub(r"\.(txt|text)$", "", name, flags=re.IGNORECASE)
    name = _BOOKNAME_JUNK.sub("", name)
    name = _NAME_AUTHOR_CUT.sub("", name)
    name = _ENCODING_SUFFIX.sub("", name)
    name = name.replace("_", " ").strip().strip(" -·|｜")
    return name or "未命名书籍"


def extract_author(text: str, default: str = "") -> str:
    """从正文首部提取作者，并剥离题材 / 状态标签。"""
    head = text[:3000]
    m = _AUTHOR_RE.search(head)
    if not m:
        return default
    author = m.group(1).strip()
    for _ in range(4):
        stripped = _AUTHOR_TAG_SUFFIX.sub("", author)
        if stripped == author:
            break
        author = stripped
    author = author.strip(" /|-·、｜")
    return author or default


@dataclass
class PipelineOptions:
    """单次运行参数。"""

    output_dir: Optional[str] = None
    formats: list[str] = field(default_factory=lambda: ["epub"])
    style: str = "literary"
    correct_level: int = 2
    cover: bool = True
    cover_style: str = "auto"
    title: str = ""
    author: str = ""
    language: str = "zh-Hans"
    dry_run: bool = False
    use_cache: bool = True
    # LLM 增强层（可选；未配置时整层静默跳过）
    llm: bool = False
    llm_budget: str = ""
    llm_tasks: Optional[dict] = None
    llm_max_chunks: int = 400
    llm_settings: Optional[LLMSettings] = None
    # 精排：字体与版式（留空走 configs/fonts.yaml 的默认值）
    font_embed: str = ""            # none / body / body+title / full
    font_format: str = ""           # woff2 / ttf / both
    font_body: str = ""             # 正文字体 id
    font_title: str = ""            # 章题字体 id
    font_subset: Optional[bool] = None
    indent_mode: str = "space"      # space 最兼容 / css 正确 / spacing 西式
    title_page: bool = True
    page_ornament: str = "chapter"  # none / chapter / segment
    vertical: bool = False


@dataclass
class PipelineResult:
    """单次运行结果。"""

    success: bool
    input_path: str
    book_title: str
    author: str
    output_files: dict = field(default_factory=dict)
    chapter_count: int = 0
    total_chars: int = 0
    encoding: str = ""
    clean_stats: dict = field(default_factory=dict)
    correct_stats: dict = field(default_factory=dict)
    split_warnings: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    epub_check: dict = field(default_factory=dict)
    message: str = ""
    cover_path: str = ""
    cover_design: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    typography: dict = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return round(sum(self.timings.values()), 3)

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "input_path": self.input_path,
            "book_title": self.book_title,
            "author": self.author,
            "output_files": self.output_files,
            "chapter_count": self.chapter_count,
            "total_chars": self.total_chars,
            "encoding": self.encoding,
            "clean_stats": self.clean_stats,
            "correct_stats": self.correct_stats,
            "split_warnings": self.split_warnings,
            "timings": {k: round(v, 4) for k, v in self.timings.items()},
            "elapsed": self.elapsed,
            "report": self.report,
            "epub_check": self.epub_check,
            "message": self.message,
            "cover_path": self.cover_path,
            "cover_design": self.cover_design,
            "llm": self.llm,
            "typography": self.typography,
        }


class AutoBookPipeline:
    """电子书生产流水线。"""

    def __init__(self, config: Optional[PipelineConfig] = None, *, cache_dir: Optional[str] = None) -> None:
        self.config = config or load_config()
        self.cache_dir = cache_dir or os.path.join(os.getcwd(), ".abf_cache")
        self.cache = CorrectionCache(self.cache_dir, enabled=True)
        self.runner: Optional[LLMRunner] = None

    # -- LLM 增强层装配 --
    def _setup_runner(self, opts: "PipelineOptions") -> Optional["LLMRunner"]:
        """按运行参数装配 LLM 运行器；未启用或未配置时返回 None。"""
        if self.runner is not None:
            return self.runner
        if not opts.llm:
            return None
        settings = opts.llm_settings or load_llm_settings()
        if not settings.configured:
            return None
        settings.enabled = True
        if opts.llm_budget:
            settings.budget = opts.llm_budget
        if opts.llm_tasks:
            tasks = dict(settings.tasks)
            for k, v in opts.llm_tasks.items():
                if k in tasks:
                    tasks[k] = bool(v)
            settings.tasks = tasks
        self.runner = LLMRunner(settings, cache_dir=self.cache_dir)
        return self.runner

    # -- 配置读取辅助 --
    def _clean_options(self, indent_mode: str = "space") -> dict:
        sc = self.config.step("text_clean")
        opt = dict(sc.options) if sc else {}
        ad_path = opt.get("ad_config") or default_ad_config_path()
        # 缩进由 indent_mode 统一决定：只有 space 模式才注入全角空格
        indent_on = bool(opt.get("paragraph_indent", True)) and (indent_mode or "space") == "space"
        return {
            "smart_linebreak": bool(opt.get("smart_linebreak", True)),
            "paragraph_indent": indent_on,
            "remove_ads": bool(opt.get("remove_ads", True)),
            "normalize_spaces": bool(opt.get("normalize_spaces", True)),
            "ad_config": AdConfig.load(ad_path),
        }

    def _split_options(self) -> dict:
        sc = self.config.step("chapter_split")
        opt = dict(sc.options) if sc else {}
        from ..core.markers import load_families_from_yaml

        families = load_families_from_yaml(default_chapter_config_path())
        params: dict = {
            "min_chapter_length": int(opt.get("min_chapter_length", 100)),
            "max_chapter_length": int(opt.get("max_chapter_length", 50000)),
            "detect_volume": bool(opt.get("detect_volume", True)),
            "detect_section": bool(opt.get("detect_section", True)),
        }
        if families:
            params["families"] = families
        return params

    # -- 字体与版式 --
    def _font_plan(self, opts: "PipelineOptions", book: BookSplit, title: str, author: str):
        """按风格与用户覆盖子集化内嵌字体；失败返回 None，不影响主链路。"""
        try:
            reg = FontRegistry.load()
        except Exception:
            return None
        style = normalize_style(opts.style)
        overrides: dict = {}
        if opts.font_body:
            overrides["body"] = opts.font_body
        if opts.font_title:
            overrides["title"] = opts.font_title
        roles = reg.roles_for_style(style, overrides or None)
        texts: list = [title, author]
        for ch in book.chapters:
            texts.append(ch.title or "")
            texts.append(ch.content or "")
        return reg.plan(
            texts,
            level=opts.font_embed or None,
            fmt=opts.font_format or None,
            subset=opts.font_subset,
            roles=roles,
            tmp_dir=os.path.join(self.cache_dir, "fonts"),
        )

    # -- 主流程 --
    def run(
        self,
        input_path: str,
        options: Optional[PipelineOptions] = None,
        progress: Optional[ProgressCB] = None,
    ) -> PipelineResult:
        opts = options or PipelineOptions()
        cb = progress or _noop
        timings: dict[str, float] = {}

        input_path = os.path.abspath(input_path)
        if not os.path.exists(input_path):
            return PipelineResult(False, input_path, "", "", message=f"输入文件不存在: {input_path}")

        # 1) 编码检测
        cb("encoding", 0, 1, "检测文件编码")
        t = time.perf_counter()
        decoded = detect_and_convert(input_path)
        timings["encoding"] = time.perf_counter() - t

        # 1.5) LLM 去广告兜底（规则前置：仅规则未判定的可疑行进入模型）
        runner = self._setup_runner(opts)
        clean_opts = self._clean_options(opts.indent_mode)
        raw_text = decoded.text
        _ad_rule_dropped = 0
        _ad_llm_dropped = 0
        if runner is not None and runner.task_on("ad_filter"):
            cb("ad_filter_llm", 0, 1, "语境判定可疑行")
            t = time.perf_counter()
            raw_text, _ad_result = runner.filter_ads(raw_text, cfg=clean_opts.get("ad_config"))
            _ad_rule_dropped = int(_ad_result.data.get("rule_dropped", 0))
            _ad_llm_dropped = int(_ad_result.data.get("llm_dropped", 0))
            timings["ad_filter_llm"] = time.perf_counter() - t

        # 2) 文本清洗（含元信息解析与站点水印清除）
        cb("cleaning", 0, 1, "清洗文本")
        t = time.perf_counter()
        src_name = os.path.basename(input_path)
        prepared = prepare(raw_text, filename=src_name, **clean_opts)
        cleaned = prepared.text
        clean_stats = prepared.stats
        # 前置过滤阶段删除的行计入清洗统计，避免 ads_removed 显示为 0 的困惑
        clean_stats.ads_removed += _ad_rule_dropped
        clean_stats.llm_ads_removed = _ad_llm_dropped
        meta = prepared.meta
        timings["cleaning"] = time.perf_counter() - t

        # 3) 章节识别
        cb("splitting", 0, 1, "识别章节结构")
        t = time.perf_counter()
        fn_title = clean_bookname(os.path.splitext(src_name)[0])
        if opts.title:
            title = opts.title
        elif meta.title:
            title = meta.title
        else:
            title = fn_title if len(fn_title) >= 2 else infer_title(cleaned)
        author = opts.author or meta.author or extract_author(cleaned) or "佚名"
        split_opts = self._split_options()
        book = split_chapters(cleaned, book_title=title, **split_opts)
        book.title = title
        book.meta = meta.as_dict()
        timings["splitting"] = time.perf_counter() - t

        # 3.5) LLM 章节名生成与空白补全（仅有问题的标题进入候选）
        if runner is not None and (runner.task_on("chapter_titles") or runner.task_on("blank_titles")):
            cb("title_llm", 0, 1, "生成章节名")
            t = time.perf_counter()
            try:
                runner.generate_titles(book, author=author)
            except Exception as exc:  # 增强层失败不得阻断主流程
                runner.notes.append(f"章节名生成失败（已跳过）：{exc}")
            timings["title_llm"] = time.perf_counter() - t

        # 4) 双层纠错
        correction_dicts: list[dict] = []
        chapter_reports: list[dict] = []
        correct_stats: dict = {}
        base_texts: dict = {}          # 规则纠错前的章节原文快照
        base_corrections: dict = {}    # 规则层的修正（用于 LLM 修改点坐标回映射）
        t = time.perf_counter()
        correct_step = self.config.step("correct")
        correct_enabled = correct_step.enabled if correct_step else True
        if correct_enabled and opts.correct_level >= 1:
            engine = create_engine_from_config(
                default_config_path(),
                level=opts.correct_level,
                cache=self.cache if opts.use_cache else None,
            )
            n = len(book.chapters)
            for i, ch in enumerate(book.chapters):
                cb("correcting", i, n, f"纠错 {i + 1}/{n} 章")
                original = ch.content
                base_texts[i] = original
                res = engine.correct(original)
                base_corrections[i] = list(res.corrections)
                ch.content = res.text
                t_res = engine.correct(ch.title)
                ch.title = t_res.text
                for c in res.corrections:
                    d = c.as_dict()
                    d["chapter"] = i
                    d["chapter_title"] = ch.title
                    correction_dicts.append(d)
                if res.corrections:
                    chapter_reports.append(
                        {
                            "chapter": i,
                            "title": ch.title,
                            "segments": build_segments(original, res.corrections),
                            "count": len(res.corrections),
                        }
                    )
            cb("correcting", n, n, "纠错完成")

        # 4.5) LLM 精准校对（修改点映射回原文坐标，与规则层修正合并展示）
        if runner is not None:
            mode = ""
            for cand in ("proofread", "spelling", "grammar"):
                if runner.task_on(cand):
                    mode = cand
                    break
            if mode:
                cb("proof_llm", 0, 1, "大模型校对")
                t = time.perf_counter()
                _pr, outcome = runner.proofread(
                    book,
                    base_corrections=base_corrections,
                    base_texts=base_texts,
                    mode=mode,
                    max_chunks=opts.llm_max_chunks,
                )
                for ci, corr_list in ((outcome.corrections_by_chapter if outcome else None) or {}).items():
                    ch_title = book.chapters[ci].title if 0 <= ci < len(book.chapters) else ""
                    for c in corr_list:
                        d = c.as_dict()
                        d["chapter"] = ci
                        d["chapter_title"] = ch_title
                        correction_dicts.append(d)
                timings["proof_llm"] = time.perf_counter() - t

        # 4.6) LLM 目录层级修正（依需求默认关闭）
        if runner is not None and runner.task_on("toc"):
            cb("toc_llm", 0, 1, "目录层级修正")
            t = time.perf_counter()
            try:
                runner.fix_toc(book)
            except Exception as exc:  # 增强层失败不得阻断主流程
                runner.notes.append(f"目录修正失败（已跳过）：{exc}")
            timings["toc_llm"] = time.perf_counter() - t

        by_source: dict = {}
        by_category: dict = {}
        for d in correction_dicts:
            by_source[d["source"]] = by_source.get(d["source"], 0) + 1
            by_category[d["category"]] = by_category.get(d["category"], 0) + 1
        correct_stats = {"total": len(correction_dicts), "by_source": by_source, "by_category": by_category}
        timings["correcting"] = time.perf_counter() - t

        # 输出目录
        out_dir = opts.output_dir or os.path.join(os.getcwd(), "output")
        os.makedirs(out_dir, exist_ok=True)
        base = re.sub(r'[\\/:*?"<>|]', "_", title)[:60] or "book"

        # 5) 封面生成
        cover_path = ""
        t = time.perf_counter()
        cover_design: dict = {}
        if opts.cover and not opts.dry_run:
            cb("cover", 0, 1, "生成封面")
            cover_path = os.path.join(out_dir, f"{base}_cover.png")
            if runner is not None and runner.task_on("cover_params"):
                cover_design, _ = runner.cover_design(
                    title=title,
                    author=author,
                    chapters=book.chapters,
                    tags=meta.tags,
                    style_hint="" if opts.cover_style in ("", "auto") else opts.cover_style,
                )
            try:
                if cover_design:
                    render_design_cover(title, author, cover_path, design=cover_design)
                else:
                    generate_cover(title, author, cover_path, style=opts.cover_style)
            except Exception:  # pragma: no cover
                cover_path = ""
        timings["cover"] = time.perf_counter() - t

        # 5.5) 字体子集化（可选增强；失败不影响主链路）
        font_plan = None
        font_report: dict = {}
        t = time.perf_counter()
        if not opts.dry_run:
            cb("fonts", 0, 1, "准备内嵌字体")
            try:
                font_plan = self._font_plan(opts, book, title, author)
                font_report = dict(getattr(font_plan, "report", {}) or {})
            except Exception as exc:
                font_report = {"error": str(exc)[:200]}
                font_plan = None
        timings["fonts"] = time.perf_counter() - t

        # 6) EPUB 生成
        output_files: dict = {}
        epub_check: dict = {}
        t = time.perf_counter()
        if not opts.dry_run:
            cb("epub", 0, 1, "生成 EPUB")
            epub_path = os.path.join(out_dir, f"{base}.epub")
            build_epub(
                book,
                epub_path,
                style=opts.style,
                author=author,
                cover_path=cover_path or None,
                language=opts.language,
                font_plan=font_plan,
                title_page=bool(opts.title_page),
                page_ornament=opts.page_ornament,
                indent_mode=opts.indent_mode,
                vertical=bool(opts.vertical),
                subtitle=str(getattr(meta, "subtitle", "") or ""),
            )
            output_files["epub"] = epub_path
            epub_check = check_epub_structure(epub_path)
        timings["epub"] = time.perf_counter() - t

        # 7) 格式转换
        t = time.perf_counter()
        if not opts.dry_run:
            cb("convert", 0, 1, "格式转换")
            extra_formats = [f for f in opts.formats if f.lower() != "epub"]
            if extra_formats:
                results = convert_formats(output_files.get("epub", ""), out_dir, base, extra_formats)
                for fmt, r in results.items():
                    if r.success and r.output:
                        output_files[fmt] = r.output
        timings["convert"] = time.perf_counter() - t

        llm_report: dict = {}
        if runner is not None:
            runner.flush()
            llm_report = runner.report().as_dict()
            llm_report["task_usage"] = runner.task_usage_dict()
        elif opts.llm:
            llm_report = {
                "enabled": True,
                "configured": False,
                "active": False,
                "notes": ["未检测到有效的 LLM 配置（API 地址 / 密钥 / 模型名），已跳过 LLM 增强"],
            }

        report = {
            "title": title,
            "author": author,
            "meta": meta.as_dict(),
            "summary": {
                "total_corrections": len(correction_dicts),
                "by_source": by_source,
                "by_category": by_category,
                "original_chars": len(cleaned),
                "chapter_count": len(book.chapters),
                "split_quality": book.quality,
            },
            "corrections": correction_dicts,
            "chapters": chapter_reports,
            "chapter_index": [
                {"index": c.index, "title": c.title, "level": c.level,
                 "volume": c.volume_title, "chars": len(c.content)}
                for c in book.chapters
            ],
            "clean_stats": clean_stats.as_dict(),
            "split_quality": book.quality,
            "typography": {
                "style": normalize_style(opts.style),
                "indent_mode": opts.indent_mode,
                "page_ornament": opts.page_ornament,
                "title_page": bool(opts.title_page),
                "vertical": bool(opts.vertical),
                "fonts": font_report,
            },
            "split_warnings": book.warnings,
            "cover_design": cover_design,
            "cover_design_summary": design_summary(cover_design),
            "llm": llm_report,
        }

        message = "处理完成"
        if any(f in opts.formats for f in ("mobi", "azw3")) and not calibre_available():
            message = "处理完成（未检测到 Calibre，MOBI/AZW3 已跳过）"
        if opts.llm and not llm_report.get("active"):
            message += "（LLM 增强未启用）"

        return PipelineResult(
            success=True,
            input_path=input_path,
            book_title=title,
            author=author,
            output_files=output_files,
            chapter_count=len(book.chapters),
            total_chars=book.total_chars,
            encoding=decoded.encoding,
            clean_stats=clean_stats.as_dict(),
            correct_stats=correct_stats,
            split_warnings=book.warnings,
            timings=timings,
            report=report,
            epub_check=epub_check,
            message=message,
            cover_path=cover_path,
            cover_design=cover_design,
            llm=llm_report,
            typography=report.get("typography", {}),
        )


def run_pipeline(
    input_path: str,
    *,
    output_dir: Optional[str] = None,
    formats: Optional[Sequence[str]] = None,
    style: str = "literary",
    correct_level: int = 2,
    cover: bool = True,
    cover_style: str = "auto",
    title: str = "",
    author: str = "",
    config_path: Optional[str] = None,
    progress: Optional[ProgressCB] = None,
    llm: bool = False,
    llm_budget: str = "",
    llm_tasks: Optional[dict] = None,
) -> PipelineResult:
    """便捷函数：一次性跑完整流水线。"""
    config = load_config(config_path)
    options = PipelineOptions(
        output_dir=output_dir,
        formats=list(formats or ["epub"]),
        style=style,
        correct_level=correct_level,
        cover=cover,
        cover_style=cover_style,
        title=title,
        author=author,
        llm=llm,
        llm_budget=llm_budget,
        llm_tasks=llm_tasks,
    )
    return AutoBookPipeline(config).run(input_path, options, progress)
