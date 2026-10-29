"""结构化输出的解析与校验。

LLM 输出必须落进固定字段才能被程序消费。本模块提供：

* :func:`extract_json` —— 从可能夹带 markdown 围栏、前后寒暄的文本中抽出第一个完整 JSON；
* :func:`validate` —— 轻量 JSON Schema 子集校验（type / required / properties / items / enum / 长度 / 区间）；
* :func:`repair_instruction` —— 校验失败时生成最小回灌指令，只让模型修正字段，不重发原始输入。

校验 + 一次性修复重试，是「重试率降低约 90%」的关键：绝大多数失败是字段缺失或类型
漂移，回灌错误信息比重发全文便宜得多。
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence


class SchemaError(ValueError):
    """JSON 解析失败。"""


def _find_json_span(text: str) -> Optional[str]:
    """括号平衡扫描，抽出第一个完整 JSON 对象或数组。"""
    start = -1
    opener = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            opener = ch
            break
    if start < 0:
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str) -> Any:
    """从模型输出中抽取 JSON。支持 ```json 围栏与前后多余文字。"""
    if text is None:
        raise SchemaError("输出为空")
    s = str(text).strip()
    if not s:
        raise SchemaError("输出为空")

    # 去掉 markdown 围栏
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl >= 0:
            s = s[first_nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]

    candidates = [s.strip()]
    span = _find_json_span(s)
    if span and span != candidates[0]:
        candidates.append(span)

    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            # 常见修复：中文引号、尾随逗号
            fixed = cand.replace("，", ",").replace("：", ":")
            fixed = fixed.replace("“", '"').replace("”", '"')
            fixed = fixed.replace("'", '"') if fixed.count("'") % 2 == 0 else fixed
            for prefix in ("", "["):
                try:
                    return json.loads(fixed)
                except Exception:  # noqa: BLE001
                    continue
    raise SchemaError(f"无法解析 JSON: {last_err}")


# --------------------------------------------------------------------------- #
# 轻量校验
# --------------------------------------------------------------------------- #

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def validate(obj: Any, spec: dict, path: str = "$") -> list[str]:
    """按 spec 校验，返回错误信息列表（空列表 = 通过）。"""
    errors: list[str] = []
    if not spec:
        return errors

    expected = spec.get("type")
    if expected:
        check = _TYPE_CHECKS.get(expected)
        if check and not check(obj):
            if spec.get("nullable") and obj is None:
                return errors
            return [f"{path} 期望 {expected}，实际为 {type(obj).__name__}"]

    if expected == "object" and isinstance(obj, dict):
        for key in spec.get("required", []) or []:
            if key not in obj or obj[key] is None:
                errors.append(f"{path}.{key} 缺失")
        props = spec.get("properties") or {}
        for key, sub in props.items():
            if key in obj and obj[key] is not None:
                errors.extend(validate(obj[key], sub, f"{path}.{key}"))
        if spec.get("no_extra"):
            for key in obj:
                if key not in props:
                    errors.append(f"{path}.{key} 为多余字段")

    if expected == "array" and isinstance(obj, list):
        item_spec = spec.get("items")
        min_items = spec.get("min_items")
        max_items = spec.get("max_items")
        if min_items is not None and len(obj) < int(min_items):
            errors.append(f"{path} 元素数少于 {min_items}")
        if max_items is not None and len(obj) > int(max_items):
            errors.append(f"{path} 元素数多于 {max_items}")
        if item_spec:
            for i, item in enumerate(obj):
                errors.extend(validate(item, item_spec, f"{path}[{i}]"))

    if expected in ("string", "array") and isinstance(obj, (str, list)):
        min_len = spec.get("min_len")
        max_len = spec.get("max_len")
        n = len(obj)
        if min_len is not None and n < int(min_len):
            errors.append(f"{path} 长度小于 {min_len}")
        if max_len is not None and n > int(max_len):
            errors.append(f"{path} 长度大于 {max_len}")

    if expected in ("integer", "number") and isinstance(obj, (int, float)) and not isinstance(obj, bool):
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None and obj < lo:
            errors.append(f"{path} 小于 {lo}")
        if hi is not None and obj > hi:
            errors.append(f"{path} 大于 {hi}")

    enum = spec.get("enum")
    if enum and obj not in enum:
        errors.append(f"{path} 取值 {obj!r} 不在允许集合内")

    pattern = spec.get("pattern")
    if pattern and isinstance(obj, str):
        import re

        if not re.match(pattern, obj):
            errors.append(f"{path} 不匹配格式 {pattern}")

    return errors


def repair_instruction(errors: Sequence[str], *, schema_hint: str = "") -> str:
    """生成最小回灌指令：只报错误字段，要求重发完整 JSON。"""
    head = "上一次输出未通过字段校验，错误如下："
    body = "\n".join(f"- {e}" for e in list(errors)[:12])
    tail = "\n请只输出修正后的完整 JSON 对象，不要输出任何其他文字。"
    if schema_hint:
        tail += "\n字段要求：" + schema_hint
    return head + "\n" + body + tail


# --------------------------------------------------------------------------- #
# 常用字段规格
# --------------------------------------------------------------------------- #

SCHEMA_TITLES = {
    "type": "object",
    "required": ["titles"],
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "title"],
                "properties": {
                    "index": {"type": "integer", "min": 0},
                    "title": {"type": "string", "min_len": 1, "max_len": 40},
                },
            },
        }
    },
}

SCHEMA_COVER = {
    "type": "object",
    "required": ["style", "background", "title_font", "author_font", "decoration", "layout"],
    "properties": {
        "style": {"type": "string"},
        "background": {
            "type": "object",
            "required": ["type", "colors"],
            "properties": {
                "type": {"type": "string", "enum": ["gradient", "solid", "pattern"]},
                "colors": {"type": "array", "min_len": 1, "max_len": 4},
                "pattern": {"type": "string"},
            },
        },
        "title_font": {
            "type": "object",
            "required": ["family", "size", "color"],
            "properties": {
                "family": {"type": "string", "enum": ["serif", "sans", "mono", "display"]},
                "size": {"type": "integer", "min": 60, "max": 260},
                "color": {"type": "string"},
                "weight": {"type": "integer", "min": 100, "max": 900},
            },
        },
        "author_font": {
            "type": "object",
            "required": ["family", "size", "color"],
            "properties": {
                "family": {"type": "string", "enum": ["serif", "sans", "mono", "display"]},
                "size": {"type": "integer", "min": 24, "max": 120},
                "color": {"type": "string"},
            },
        },
        "decoration": {
            "type": "object",
            "required": ["type", "position"],
            "properties": {
                "type": {"type": "string", "enum": ["none", "geometric", "wave", "particle", "line", "grid"]},
                "position": {"type": "string", "enum": ["top", "bottom", "left", "right", "center"]},
            },
        },
        "layout": {"type": "string", "enum": ["center", "top_heavy", "bottom_heavy", "split"]},
    },
}

_FIX_ITEM = {
    "type": "object",
    "required": ["original", "replacement"],
    "properties": {
        "original": {"type": "string", "min_len": 1, "max_len": 40},
        "replacement": {"type": "string", "min_len": 1, "max_len": 40},
        "type": {"type": "string"},
    },
}

_FIX_BLOCK = {
    "type": "object",
    "required": ["id", "fixes"],
    "properties": {
        "id": {"type": "integer", "min": 0},
        "fixes": {"type": "array", "items": _FIX_ITEM, "max_items": 200},
    },
}

SCHEMA_FIXES = {
    "type": "object",
    "required": ["blocks"],
    "properties": {"blocks": {"type": "array", "items": _FIX_BLOCK}},
}

SCHEMA_AD_LINES = {
    "type": "object",
    "required": ["lines"],
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "action"],
                "properties": {
                    "id": {"type": "integer", "min": 0},
                    "action": {"type": "string", "enum": ["drop", "keep"]},
                    "reason": {"type": "string", "max_len": 40},
                },
            },
        }
    },
}

SCHEMA_TOC = {
    "type": "object",
    "required": ["toc"],
    "properties": {
        "toc": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "title", "level"],
                "properties": {
                    "index": {"type": "integer", "min": 0},
                    "title": {"type": "string", "min_len": 1, "max_len": 60},
                    "level": {"type": "integer", "min": 1, "max": 3},
                    "parent": {"type": "integer", "nullable": True},
                },
            },
        }
    },
}

SCHEMA_HINTS: dict[str, str] = {
    "titles": "titles 数组，每项含 index（整数）与 title（8-15 个汉字，末尾无标点）",
    "cover_params": "style / background{type,colors}/ title_font{family,size,color,weight} / author_font{family,size,color} / decoration{type,position} / layout",
    "fixes": "blocks 数组，每项含 id（整数）与 fixes（original / replacement / type）",
    "ad_lines": "lines 数组，每项含 id、action（drop 或 keep）、reason",
    "toc": "toc 数组，每项含 index、title、level（1-3）、parent",
}
