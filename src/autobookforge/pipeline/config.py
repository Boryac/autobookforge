"""YAML 配置加载与校验。

加载 configs/pipeline.yaml，把各步骤参数化为可选项，无需改动代码即可切换行为。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "pipeline.yaml"
DEFAULT_AD_CONFIG_PATH = DEFAULT_CONFIG_PATH.parent / "ad_patterns.yaml"
DEFAULT_CHAPTER_CONFIG_PATH = DEFAULT_CONFIG_PATH.parent / "chapter_patterns.yaml"


@dataclass
class StepConfig:
    name: str
    module: str = ""
    enabled: bool = True
    options: dict = field(default_factory=dict)
    layers: list[dict] = field(default_factory=list)


@dataclass
class PipelineConfig:
    input: str = "./novels/*.txt"
    output: str = "./output/{bookname}.{ext}"
    title: str = ""
    author: str = ""
    language: str = "zh"
    steps: list[StepConfig] = field(default_factory=list)
    source_path: str = ""
    raw: dict = field(default_factory=dict)

    def step(self, name: str) -> Optional[StepConfig]:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def step_enabled(self, name: str, default: bool = True) -> bool:
        s = self.step(name)
        return s.enabled if s is not None else default

    def option(self, step_name: str, key: str, default: Any = None) -> Any:
        s = self.step(step_name)
        if s is None:
            return default
        return s.options.get(key, default)

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "meta": {"title": self.title, "author": self.author, "language": self.language},
            "steps": [
                {"name": s.name, "module": s.module, "enabled": s.enabled, "options": s.options}
                for s in self.steps
            ],
        }


def _parse_steps(raw_steps: list) -> list[StepConfig]:
    steps: list[StepConfig] = []
    for item in raw_steps or []:
        if not isinstance(item, dict):
            continue
        steps.append(
            StepConfig(
                name=str(item.get("name", "")),
                module=str(item.get("module", "")),
                enabled=bool(item.get("enabled", True)),
                options=dict(item.get("options", {}) or {}),
                layers=list(item.get("layers", []) or []),
            )
        )
    return steps


def config_from_dict(data: dict) -> PipelineConfig:
    meta = dict(data.get("meta", {}) or {})
    return PipelineConfig(
        input=str(data.get("input", "./novels/*.txt")),
        output=str(data.get("output", "./output/{bookname}.{ext}")),
        title=str(meta.get("title", "") or ""),
        author=str(meta.get("author", "") or ""),
        language=str(meta.get("language", "zh") or "zh"),
        steps=_parse_steps(data.get("steps", [])),
        raw=data,
    )


def load_config(path: Optional[str] = None) -> PipelineConfig:
    """加载 YAML 配置；失败时返回默认配置。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return PipelineConfig(
            steps=[
                StepConfig("encoding_detect", "charset-normalizer"),
                StepConfig("text_clean", "clear-book"),
                StepConfig("chapter_split", "txt-to-epub-converter"),
                StepConfig("correct", "young-corrector"),
                StepConfig("typeset", "vivliostyle", options={"style": "literary"}),
                StepConfig("cover", "mondo-poster", options={"style": "auto"}),
                StepConfig("epub_build", "ebooklib"),
                StepConfig("format_convert", "calibre", options={"formats": ["epub"]}),
            ]
        )
    try:
        import yaml

        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return PipelineConfig()
    cfg = config_from_dict(data)
    cfg.source_path = str(cfg_path)
    return cfg


def default_config_path() -> str:
    return str(DEFAULT_CONFIG_PATH)


def default_ad_config_path() -> str:
    """广告 / 水印字典路径。"""
    return str(DEFAULT_AD_CONFIG_PATH)


def default_chapter_config_path() -> str:
    """章节标记族配置路径。"""
    return str(DEFAULT_CHAPTER_CONFIG_PATH)
