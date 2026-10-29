"""模块 9：CLI 入口（Typer）。

基于类型注解，减少样板代码。提供 convert / info / watch / serve 子命令。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .llm.settings import (
    TASK_IDS,
    TASK_LABELS,
    load_settings as load_llm_settings,
    mask_key,
    save_settings as save_llm_settings,
)
from .pipeline.config import default_config_path, load_config
from .pipeline.orchestrator import AutoBookPipeline, PipelineOptions
from .core.epub_builder import STYLE_ALIASES
from .cover.generator import available_styles as cover_styles

app = typer.Typer(
    help="AutoBookForge - 全自动 TXT 精排电子书生产引擎",
    add_completion=False,
    no_args_is_help=True,
)

_STYLE_CHOICES = "minimal(简约) / literary(文艺) / business(商务) / classic(古籍)"


def _parse_llm_tasks(spec: Optional[str]) -> Optional[dict]:
    """解析 --llm-task 参数：逗号分隔，前缀 ! 表示关闭该任务。"""
    if not spec:
        return None
    out: dict = {}
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        flag = True
        if item.startswith("!"):
            flag = False
            item = item[1:]
        if item in TASK_IDS:
            out[item] = flag
    return out or None


def _print_llm_summary(rep: dict) -> None:
    """打印 LLM 增强摘要与降本核算。"""
    if not rep:
        return
    if not rep.get("active"):
        notes = rep.get("notes") or ["未配置 API 地址 / 密钥 / 模型名"]
        typer.echo(f"LLM 增强: 未启用（{notes[0]}）")
        return
    usage = rep.get("usage") or {}
    typer.echo(
        f"LLM 增强: {rep.get('model')}  档位 {rep.get('budget_label')}  调用 {usage.get('calls', 0)} 次  "
        f"输入 {usage.get('prompt_tokens', 0)} tok / 输出 {usage.get('completion_tokens', 0)} tok / "
        f"缓存命中 {usage.get('cached_tokens', 0)} tok"
    )
    for tid, t in (rep.get("tasks") or {}).items():
        label = TASK_LABELS.get(tid, tid)
        if t.get("skipped"):
            typer.echo(f"  - {label}: 跳过（{t['skipped']}）")
        else:
            typer.echo(
                f"  - {label}: 调用 {t.get('calls', 0)} 次，"
                f"送检 {t.get('items_sent', 0)}/{t.get('items_total', 0)}，生效 {t.get('applied', 0)}"
            )
    cost = rep.get("cost") or {}
    parts = []
    for key, name in (("batch_merge", "批量合并"), ("rules_first", "规则前置"),
                      ("digest_input", "摘要输入"), ("patch_output", "修改点输出"),
                      ("prefix_cache", "前缀缓存")):
        c = cost.get(key) or {}
        ratio = c.get("saved_ratio", c.get("hit_ratio"))
        if ratio is not None:
            parts.append(f"{name} {ratio:.0%}")
    if parts:
        typer.echo("  降本: " + "　".join(parts))


def _progress_printer(stage: str, cur: int, total: int, msg: str) -> None:
    total = max(1, total)
    pct = int(cur / total * 100) if total > 1 else None
    if pct is None:
        typer.echo(f"  [{stage}] {msg}")
    else:
        bar_len = 24
        filled = int(bar_len * cur / total)
        bar = "#" * filled + "-" * (bar_len - filled)
        typer.echo(f"  [{stage}] [{bar}] {msg}")


@app.command()
def convert(
    input_file: Path = typer.Argument(..., help="输入 TXT 文件路径"),
    output: Path = typer.Option(Path("./output"), "--output", "-o", help="输出目录"),
    format: str = typer.Option("epub", "--format", "-f", help="输出格式，逗号分隔: epub,mobi,azw3"),
    style: str = typer.Option("literary", "--style", "-s", help=f"排版风格: {_STYLE_CHOICES}"),
    correct_level: int = typer.Option(2, "--correct-level", "-c", help="纠错强度: 1=仅规则, 2=规则+统计"),
    cover: bool = typer.Option(True, "--cover/--no-cover", help="是否生成封面"),
    cover_style: str = typer.Option("auto", "--cover-style", help="封面风格: auto/minimal/literary/business/classic/tech/design"),
    title: str = typer.Option("", "--title", help="书名（默认自动推断）"),
    author: str = typer.Option("", "--author", help="作者（默认自动提取）"),
    config: Optional[Path] = typer.Option(None, "--config", help="YAML 配置文件路径"),
    report: bool = typer.Option(False, "--report", help="输出纠错报告 (JSON + Markdown)"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="精简输出"),
    llm: bool = typer.Option(False, "--llm/--no-llm", help="启用 LLM 增强（需先填写 API 地址与密钥）"),
    llm_budget: str = typer.Option("", "--llm-budget", help="Token 预算档位: economy / standard / fine"),
    llm_max_chunks: int = typer.Option(400, "--llm-max-chunks", help="单次校对最多送检的文本块数"),
    llm_task: Optional[str] = typer.Option(None, "--llm-task", help="临时开关任务，逗号分隔，! 前缀表示关闭"),
    llm_thinking: str = typer.Option(
        "", "--llm-thinking",
        help="推理型模型思考开关: auto / off_all / off_vllm / off_openai / off_reasoning",
    ),
    font_embed: str = typer.Option("", "--font-embed", help="内嵌字体档位: none / body / body+title / full"),
    font_format: str = typer.Option("", "--font-format", help="字体格式: woff2 体积最优 / ttf 老 Kindle / both"),
    font_body: str = typer.Option("", "--font-body", help="正文字体 id（autobookforge fonts 可列出）"),
    font_title: str = typer.Option("", "--font-title", help="章题字体 id"),
    indent_mode: str = typer.Option("space", "--indent", help="缩进模式: space 最兼容 / css 随字号缩放 / spacing 段距"),
    ornament: str = typer.Option("chapter", "--ornament", help="纹样: none / chapter 章首 / segment 逻辑页"),
    title_page: bool = typer.Option(True, "--title-page/--no-title-page", help="是否生成扉页"),
    vertical: bool = typer.Option(False, "--vertical/--no-vertical", help="竖排（仅 Apple Books 等少数阅读器支持良好）"),
) -> None:
    """将 TXT 文件转换为精排电子书。"""
    formats = [f.strip().lower() for f in format.split(",") if f.strip()]
    cfg = load_config(str(config) if config else None)
    pipeline = AutoBookPipeline(cfg)

    llm_overrides: Optional[dict] = None
    if llm and llm_thinking.strip():
        from .llm.settings import load_settings as _load_llm

        st = _load_llm()
        st.thinking = llm_thinking.strip()
        llm_overrides = {"llm_settings": st}

    if not quiet:
        typer.echo(f"AutoBookForge v{__version__}")
        typer.echo(f"输入: {input_file}")
        typer.echo(f"输出: {output}  格式: {','.join(formats)}  风格: {style}  纠错档位: {correct_level}")
        typer.echo("")

    opts = PipelineOptions(
        output_dir=str(output),
        formats=formats,
        style=style,
        correct_level=correct_level,
        cover=cover,
        cover_style=cover_style,
        title=title,
        author=author,
        llm=llm,
        llm_budget=llm_budget,
        llm_tasks=_parse_llm_tasks(llm_task),
        llm_max_chunks=llm_max_chunks,
        llm_settings=(llm_overrides or {}).get("llm_settings"),
        font_embed=font_embed,
        font_format=font_format,
        font_body=font_body,
        font_title=font_title,
        indent_mode=indent_mode,
        title_page=title_page,
        page_ornament=ornament,
        vertical=vertical,
    )
    result = pipeline.run(str(input_file), opts, progress=None if quiet else _progress_printer)

    if not result.success:
        typer.secho(f"处理失败: {result.message}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if not quiet:
        typer.echo("")
        typer.echo(f"书名: {result.book_title}    作者: {result.author}")
        typer.echo(f"章节: {result.chapter_count} 章    总字数: {result.total_chars}")
        typer.echo(f"编码: {result.encoding}")
        _typo = (result.report or {}).get("typography") or {}
        _fonts = _typo.get("fonts") or {}
        _faces = _fonts.get("faces") or []
        if _faces:
            typer.echo(
                "精排: 风格 %s  缩进 %s  纹样 %s"
                % (_typo.get("style"), _typo.get("indent_mode"), _typo.get("page_ornament"))
            )
            typer.echo(
                "字体: %s  合计 %.2f MB（%s，源 %.2f MB，%.1fs）"
                % (
                    "、".join(f.get("family", "") for f in _faces),
                    _fonts.get("total_out_mb", 0),
                    _fonts.get("format"),
                    _fonts.get("total_source_mb", 0),
                    _fonts.get("elapsed", 0),
                )
            )
        typer.echo(
            f"清洗: 去广告 {result.clean_stats.get('ads_removed', 0)} 行, "
            f"合并断行 {result.clean_stats.get('lines_merged', 0)} 次"
        )
        typer.echo(
            f"纠错: 共 {result.correct_stats.get('total', 0)} 处 "
            f"(规则 {result.correct_stats.get('by_source', {}).get('rule', 0)} / "
            f"统计 {result.correct_stats.get('by_source', {}).get('ngram', 0)})"
        )
        for fmt, path in result.output_files.items():
            typer.secho(f"  [OK] {fmt.upper()}: {path}", fg=typer.colors.GREEN)
        if result.epub_check:
            ok = result.epub_check.get("ok")
            if ok:
                typer.echo(f"EPUB 结构自检: 通过 ({result.epub_check.get('chapters', 0)} 个 XHTML)")
            else:
                typer.secho(f"EPUB 结构自检: 异常 {result.epub_check.get('missing')}", fg=typer.colors.YELLOW)
        if result.split_warnings:
            typer.echo("分章提示:")
            for w in result.split_warnings[:6]:
                typer.echo(f"  - {w}")
        _print_llm_summary(result.report.get("llm") or {})
        typer.echo(f"耗时: {result.elapsed:.2f}s")
        if result.message and result.message != "处理完成":
            typer.secho(result.message, fg=typer.colors.YELLOW)

    if report:
        from .corrector.report import to_json, to_markdown

        out_dir = Path(output)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in result.book_title if ch not in '\\/:*?"<>|')[:50] or "book"
        (out_dir / f"{safe}_correct_report.json").write_text(
            to_json(result.report), encoding="utf-8"
        )
        (out_dir / f"{safe}_correct_report.md").write_text(
            to_markdown(result.report), encoding="utf-8"
        )
        typer.echo(f"纠错报告已写入: {out_dir / (safe + '_correct_report.json')}")

    raise typer.Exit(code=0)


def _print_split_diagnosis(input_file: Path) -> None:
    """打印单个文件的分章诊断：编码 / 元信息 / 标记族 / 质量指标。

    这是排查「分章不理想」最快的入口：能直接看到走了哪个标记族、命中多少标记、
    族得分排名，以及最终章长分布与覆盖率。
    """
    from .core.boilerplate import AdConfig
    from .core.cleaner import prepare
    from .core.encoding import detect_and_convert
    from .core.markers import discover, load_families_from_yaml
    from .core.splitter import split_chapters
    from .pipeline.config import default_ad_config_path, default_chapter_config_path

    path = str(input_file)
    dec = detect_and_convert(path)
    prep = prepare(
        dec.text,
        filename=os.path.basename(path),
        ad_config=AdConfig.load(default_ad_config_path()),
    )
    families = load_families_from_yaml(default_chapter_config_path())
    found = discover(prep.text.split("\n"), families=families)
    book = split_chapters(
        prep.text,
        book_title=prep.meta.title or os.path.basename(path),
        families=families,
    )

    typer.echo("")
    typer.echo("-- 分章诊断 " + "-" * 44)
    typer.echo(f"编码      : {dec.encoding}")
    typer.echo(f"书名/作者 : {prep.meta.title or '(未识别)'} / {prep.meta.author or '(未识别)'}")
    typer.echo(f"题材标签  : {'、'.join(prep.meta.tags) or '(无)'}")
    typer.echo(
        f"清洗明细  : 删除水印 {prep.stats.ads_removed} 行 / 段内剥离 {prep.stats.inline_ads_stripped} 处 / "
        f"元信息 {prep.stats.meta_removed} 行 / 标题保护 {prep.stats.headings_protected} 行"
    )
    typer.echo(f"分章策略  : {found.strategy}")
    scores = found.stats.get("family_scores") or {}
    if scores:
        typer.echo("族得分    : " + ", ".join(f"{k}={v}" for k, v in list(scores.items())[:6]))
    typer.echo(
        f"标记命中  : 章 {found.stats.get('chapter_markers', 0)} / "
        f"卷 {found.stats.get('volume_markers', 0)} / "
        f"节 {found.stats.get('section_markers', 0)}"
    )
    q = book.quality or {}
    typer.echo(
        f"分章结果  : {q.get('chapters', 0)} 章"
        f"（卷 {q.get('volumes', 0)} / 节 {q.get('sections', 0)}）　"
        f"章长 中位 {q.get('median_len', 0)} / 最长 {q.get('max_len', 0)}　"
        f"覆盖 {q.get('coverage', 0):.0%}　单调 {q.get('monotonic_ratio', 0):.0%}"
    )
    for i, ch in enumerate(book.chapters[:15]):
        typer.echo(f"  [{i:>3}] L{ch.level} {len(ch.content):>7} 字  {ch.title[:44]}")
    if len(book.chapters) > 15:
        typer.echo(f"  ... 另有 {len(book.chapters) - 15} 章")
    for w in book.warnings[:6]:
        typer.echo(f"  注意: {w}")


@app.command()
def info(
    input_file: Optional[Path] = typer.Argument(None, help="可选：TXT 文件，附加分章诊断"),
) -> None:
    """显示环境与配置信息；给出文件时附加该文件的分章诊断。"""
    from .core.mobi_converter import find_calibre

    typer.echo(f"AutoBookForge v{__version__}")
    typer.echo(f"配置文件: {default_config_path()}")
    typer.echo(f"排版风格: {', '.join(sorted(set(STYLE_ALIASES.keys())))}")
    typer.echo(f"封面风格: {', '.join(cover_styles())}")
    calibre = find_calibre()
    typer.echo(f"Calibre: {calibre if calibre else '未检测到（MOBI/AZW3 不可用）'}")
    ls = load_llm_settings()
    if ls.active:
        typer.echo(f"LLM: {ls.model} @ {ls.base_url}　密钥 {mask_key(ls.api_key)}　档位 {ls.budget}　已启用")
    elif ls.configured:
        typer.echo("LLM: 已填写配置但总开关为关闭状态（autobookforge llm --enable）")
    else:
        typer.echo("LLM: 未配置（Web UI 的「LLM 增强 → 模型与任务设置」可填写 API 地址、密钥与模型名）")

    if input_file is not None:
        if not input_file.exists():
            typer.secho(f"文件不存在: {input_file}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        _print_split_diagnosis(input_file)


@app.command()
def watch(
    directory: Path = typer.Argument(..., help="监听目录"),
    output: Path = typer.Option(Path("./output"), "--output", "-o"),
    style: str = typer.Option("literary", "--style", "-s"),
    correct_level: int = typer.Option(2, "--correct-level", "-c"),
) -> None:
    """监听目录，自动处理新增的 TXT 文件。"""
    from .pipeline.watcher import watch_directory

    watch_directory(str(directory), str(output), style=style, correct_level=correct_level)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """启动 Web UI 服务。"""
    try:
        import uvicorn
    except Exception:
        typer.secho("未安装 uvicorn，请先 pip install uvicorn[standard]", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"启动 Web UI: http://{host}:{port}")
    uvicorn.run("autobookforge.api.main:app", host=host, port=port, reload=reload)


@app.command()
def fonts(
    style: str = typer.Option("", "--style", "-s", help="只显示某个风格的默认角色字体"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出（供脚本消费）"),
) -> None:
    """列出 ttf/ 目录下可用的字体与角色映射。"""
    import json as _json

    from .core.fonts import FontRegistry, fonttools_available, list_font_options

    if json_out:
        typer.echo(_json.dumps(list_font_options(), ensure_ascii=False, indent=2))
        return

    reg = FontRegistry.load()
    typer.echo(f"字体目录: {reg.root}")
    if fonttools_available():
        typer.echo("fontTools: 可用（支持按本书字符集子集化）")
    else:
        typer.echo("fontTools: 未安装（无法子集化，只能整份嵌入或走系统回退）")
    typer.echo("")
    typer.echo("%-26s %-24s %-6s %-8s %-8s %s" % ("id", "家族名", "字重", "体积MB", "角色", "说明"))
    for f in sorted(reg.faces, key=lambda x: (x.role == "unused", x.role, x.font_id)):
        note = f.note or ("（自动发现）" if not f.registered else "")
        typer.echo(
            "%-26s %-24s %-6d %-8.2f %-8s %s"
            % (f.font_id, f.family[:24], f.weight, f.source_size / 1048576, f.role, note)
        )
    typer.echo("")
    typer.echo("风格默认角色字体：")
    for sid in ("minimal", "literary", "business", "classic", "guoxue"):
        mark = " <=" if style and style == sid else ""
        typer.echo("  %-9s %s%s" % (sid, reg.roles_for_style(sid), mark))
    typer.echo("")
    typer.echo(
        "嵌入档位: %s（none/body/body+title/full）   格式: %s（woff2/ttf/both）   上限: %s MB"
        % (reg.embed.get("level"), reg.embed.get("format"), reg.embed.get("max_size_mb"))
    )


@app.command()
def llm(
    base_url: str = typer.Option("", "--base-url", help="写入 API 地址，如 https://api.deepseek.com/v1"),
    api_key: str = typer.Option("", "--api-key", help="写入 API Key（仅保存在本机 configs/llm.local.json）"),
    model: str = typer.Option("", "--model", help="写入模型名称"),
    budget: str = typer.Option("", "--budget", help="写入预算档位: economy / standard / fine"),
    enable: bool = typer.Option(False, "--enable", help="启用 LLM 增强"),
    disable: bool = typer.Option(False, "--disable", help="停用 LLM 增强"),
    task: Optional[str] = typer.Option(None, "--task", help="开关任务，逗号分隔，! 前缀表示关闭"),
    thinking: str = typer.Option(
        "", "--thinking",
        help="思考模式: auto / off_all / off_vllm / off_openai / off_reasoning",
    ),
    test: bool = typer.Option(False, "--test", help="测试连通性"),
) -> None:
    """查看或修改 LLM 设置（与 Web 设置页等价）。"""
    settings = load_llm_settings()
    changed = False
    if base_url:
        settings.base_url = base_url.strip()
        changed = True
    if api_key:
        settings.api_key = api_key.strip()
        changed = True
    if model:
        settings.model = model.strip()
        changed = True
    if budget:
        settings.budget = budget.strip()
        changed = True
    if thinking:
        from .llm.settings import THINKING_MODES as _MODES

        tv = thinking.strip()
        if tv in _MODES:
            settings.thinking = tv
            changed = True
        else:
            typer.secho(f"思考模式无效（可选: {', '.join(_MODES)}）", fg=typer.colors.YELLOW)
    if enable:
        settings.enabled = True
        changed = True
    if disable:
        settings.enabled = False
        changed = True
    tasks = _parse_llm_tasks(task)
    if tasks:
        merged = dict(settings.tasks)
        merged.update(tasks)
        settings.tasks = merged
        changed = True

    if changed:
        path = save_llm_settings(settings)
        typer.echo(f"设置已写入: {path}")

    typer.echo("")
    typer.echo(f"API 地址 : {settings.base_url}")
    typer.echo(f"模型     : {settings.model}")
    typer.echo(f"密钥     : {mask_key(settings.api_key) or '(未填写)'}")
    typer.echo(f"预算档位 : {settings.budget}")
    typer.echo(f"思考模式 : {settings.thinking}")
    typer.echo(f"总开关   : {'启用' if settings.enabled else '关闭'}")
    typer.echo(f"可用状态 : {'已就绪' if settings.active else ('已配置但未启用' if settings.configured else '未配置')}")
    typer.echo("任务开关 :")
    for tid in TASK_IDS:
        flag = "开" if settings.tasks.get(tid, False) else "关"
        typer.echo(f"  [{flag}] {tid:<16} {TASK_LABELS.get(tid, '')}")

    if test:
        from .llm.client import LLMClient

        typer.echo("")
        result = LLMClient(settings).test_connection()
        if result.get("ok"):
            typer.secho(f"连通性测试通过（{result.get('latency_ms')} ms）：{result.get('detail')}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"连通性测试失败：{result.get('detail')}", fg=typer.colors.RED)
            raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
