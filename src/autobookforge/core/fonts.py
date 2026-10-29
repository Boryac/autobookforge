"""字体系统：发现 / 角色映射 / 子集化 / @font-face 生成 / 许可随包。

设计要点
--------
1. **发现优先于登记**：扫描 ``ttf/`` 目录，读 name 表得到家族名。新增字体
   只需把文件丢进去，无需改代码；``configs/fonts.yaml`` 只做稳定 id 与角色映射。
2. **按本书字符集子集化**：CJK 字体动辄 20-40 MB，整份嵌入不可接受。
   实测子集后单款落到 0.6-1.1 MB（woff2）。
3. **三层回退链**：内嵌字体 -> 系统同风格字体 -> generic 族。阅读器拒绝
   内嵌字体时版面气质不崩。
4. **无 fontTools 时优雅降级**：返回空 plan 并说明原因，主链路不受影响。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FONT_ROOT = PROJECT_ROOT / "ttf"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "fonts.yaml"

# 兜底字符集：标点、圈码、罗马数字、饰条符号、数字与拉丁字母。
# 缺了这些，饰条与注号会显示成方框。
FALLBACK_CHARS = (
    "　"
    "、。〃〈〉《》「」『』【】〔〕〖〗’‘“”"
    "！？，．：；、…—－～·※"
    "（）［］｛｝＋－＝×÷％＃＆＊／＼｜"
    "0123456789０１２３４５６７８９"
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
    "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "§¶†‡•○●◇◆□■△▲▽▼☆★"
    "❖◈∗❀〇〓"
)

# SIL OFL 1.1 全文（许可本身允许自由复制与随字体分发）
OFL_TEXT = """SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.
The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply to any
document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may include
source files, build scripts and documentation.
"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).
"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).
"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.
"Author" refers to any designer, engineer, programmer, technical writer or
other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining a
copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components, in
Original or Modified Versions, may be sold by itself.
2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or in
the appropriate machine-readable metadata fields within text or binary
files as long as those fields can be easily viewed by the user.
3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.
4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any Modified
Version, except to acknowledge the contribution(s) of the Copyright
Holder(s) and the Author(s) or with their explicit written permission.
5) The Font Software, modified or unmodified, in part or in whole, must be
distributed entirely under this license, and must not be distributed under
any other license. The requirement for fonts to remain under this license
does not apply to any document created using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are not
met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER
DEALINGS IN THE FONT SOFTWARE.
"""

# 角色中文名（报告用）
ROLE_LABELS = {
    "body": "正文",
    "title": "章题",
    "quote": "引文",
    "note": "注文",
    "unassigned": "未分配",
}

EMBED_LEVELS = ("none", "body", "body+title", "full")
FORMATS = ("woff2", "ttf", "both")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class FontFace:
    font_id: str
    path: str
    family: str
    label: str = ""
    style: str = "normal"
    weight: int = 400
    role: str = "unassigned"
    note: str = ""
    glyphs: int = 0
    source_size: int = 0
    registered: bool = False

    @property
    def display(self) -> str:
        return self.label or self.family or self.font_id

    def as_dict(self) -> dict:
        return {
            "id": self.font_id,
            "family": self.family,
            "label": self.display,
            "style": self.style,
            "weight": self.weight,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "note": self.note,
            "glyphs": self.glyphs,
            "size_mb": round(self.source_size / 1048576, 2),
            "registered": self.registered,
            "path": os.path.relpath(self.path, PROJECT_ROOT).replace("\\", "/"),
        }


@dataclass
class FontPlan:
    """一次排版所需的字体产物。"""

    css: str = ""
    files: list = field(default_factory=list)          # [(arcname, bytes)]
    licenses: list = field(default_factory=list)       # [(arcname, text)]
    report: dict = field(default_factory=dict)
    family_by_role: dict = field(default_factory=dict)  # {role: CSS 家族名}

    @property
    def ok(self) -> bool:
        return bool(self.files)


# --------------------------------------------------------------------------
# 字体元信息读取
# --------------------------------------------------------------------------
def _slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "-", text or "").strip("-").lower()
    return s or "font"


def _read_face_meta(path: Path) -> dict:
    """读 name / OS2 表，失败时返回空 dict（不抛异常）。"""
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return {}
    tt = None
    try:
        tt = TTFont(str(path), fontNumber=0, lazy=True)
        family = ""
        copyright_line = ""
        try:
            name = tt["name"]
            family = name.getDebugName(16) or name.getDebugName(1) or ""
            copyright_line = name.getDebugName(0) or ""
        except Exception:
            pass
        weight = 400
        try:
            weight = int(tt["OS/2"].usWeightClass)
        except Exception:
            pass
        style = "normal"
        try:
            mac_style = tt["head"].macStyle
            if mac_style & 0b10:
                style = "italic"
        except Exception:
            pass
        glyphs = 0
        try:
            glyphs = len(tt.getGlyphOrder())
        except Exception:
            pass
        return {
            "family": family or path.stem,
            "weight": weight,
            "style": style,
            "glyphs": glyphs,
            "copyright": copyright_line,
        }
    except Exception:
        return {}
    finally:
        if tt is not None:
            try:
                tt.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 字体库
# --------------------------------------------------------------------------
class FontRegistry:
    """扫描 ttf/ 目录并合并 configs/fonts.yaml 的登记信息。"""

    def __init__(self, root: Path, config: dict):
        self.root = Path(root)
        self.config = config or {}
        self.faces: list[FontFace] = []
        self.roles: dict[str, str] = dict((config or {}).get("roles") or {})
        self.embed: dict = dict((config or {}).get("embed") or {})
        self.fallbacks: dict = dict((config or {}).get("fallbacks") or {})
        self._scan()

    # -- 构建 --
    @classmethod
    def load(cls, root: Optional[str] = None, config_path: Optional[str] = None) -> "FontRegistry":
        cfg = load_font_config(config_path)
        base = Path(root) if root else _config_root(cfg)
        return cls(base, cfg)

    def _scan(self) -> None:
        disc = self.config.get("discovery") or {}
        raw_roots = disc.get("roots") or ["ttf"]
        ignore_dirs = set(disc.get("ignore_dirs") or ["__MACOSX"])
        ignore_prefixes = tuple(disc.get("ignore_prefixes") or ["._"])
        exts = tuple(str(e).lower() for e in (disc.get("extensions") or [".ttf", ".otf", ".ttc"]))
        entries = list(self.config.get("fonts") or [])

        found: list[Path] = []
        for r in raw_roots:
            base = Path(r)
            if not base.is_absolute():
                base = PROJECT_ROOT / r
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
                for fn in filenames:
                    if not fn.lower().endswith(exts):
                        continue
                    if ignore_prefixes and fn.startswith(ignore_prefixes):
                        continue
                    found.append(Path(dirpath) / fn)

        used_ids: set[str] = set()
        for path in sorted(found):
            entry = _match_entry(entries, path, self.root)
            meta = _read_face_meta(path)
            if entry:
                font_id = str(entry.get("id") or _slug(path.stem))
                family = str(entry.get("family") or meta.get("family") or path.stem)
                label = str(entry.get("label") or "")
                role = str(entry.get("role") or "unassigned")
                note = str(entry.get("note") or "")
                style = str(entry.get("style") or meta.get("style") or "normal")
                weight = int(entry.get("weight") or meta.get("weight") or 400)
                registered = True
            else:
                font_id = _slug(path.stem)
                family = str(meta.get("family") or path.stem)
                label = ""
                role = "unassigned"
                note = ""
                style = str(meta.get("style") or "normal")
                weight = int(meta.get("weight") or 400)
                registered = False

            base_id = font_id
            n = 2
            while font_id in used_ids:
                font_id = "%s-%d" % (base_id, n)
                n += 1
            used_ids.add(font_id)

            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            self.faces.append(
                FontFace(
                    font_id=font_id,
                    path=str(path),
                    family=family,
                    label=label,
                    style=style,
                    weight=weight,
                    role=role,
                    note=note,
                    glyphs=int(meta.get("glyphs") or 0),
                    source_size=size,
                    registered=registered,
                )
            )

    # -- 查询 --
    def get(self, font_id: str) -> Optional[FontFace]:
        if not font_id:
            return None
        for f in self.faces:
            if f.font_id == font_id:
                return f
        low = font_id.strip().lower()
        for f in self.faces:
            if f.family.lower() == low or f.display.lower() == low:
                return f
        return None

    def by_role(self, role: str, roles: Optional[dict] = None) -> Optional[FontFace]:
        table = self.roles if roles is None else roles
        return self.get(table.get(role, ""))

    def roles_for_style(self, style: str, overrides: Optional[dict] = None) -> dict:
        """风格默认角色 -> 用户覆盖。overrides 中值为 None 表示显式取消该角色。"""
        sd = (self.config.get("style_defaults") or {}).get(style or "")
        merged = dict(self.roles) if sd is None else dict(sd)
        for k, v in (overrides or {}).items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        return merged

    def available(self, role: Optional[str] = None) -> list[FontFace]:
        if role:
            return [f for f in self.faces if f.role == role or f.role == "unassigned"]
        return list(self.faces)

    def options(self) -> dict:
        """供 Web / CLI 展示的字体清单。"""
        return {
            "fonts": [f.as_dict() for f in self.faces],
            "roles": dict(self.roles),
            "embed": dict(self.embed),
            "levels": list(EMBED_LEVELS),
            "formats": list(FORMATS),
            "fallbacks": dict(self.fallbacks),
        }

    # -- 嵌入编排 --
    def plan(
        self,
        texts: Iterable[str],
        *,
        level: Optional[str] = None,
        fmt: Optional[str] = None,
        subset: Optional[bool] = None,
        overrides: Optional[dict] = None,
        tmp_dir: Optional[str] = None,
        roles: Optional[dict] = None,
    ) -> FontPlan:
        """生成一次排版的字体产物。

        level: none / body / body+title / full
        fmt:   woff2 / ttf / both
        """
        ov = dict(overrides or {})
        role_table = dict(roles) if roles is not None else dict(self.roles)
        level = str(level or ov.get("level") or self.embed.get("level") or "body").strip().lower()
        fmt = str(fmt or ov.get("format") or self.embed.get("format") or "woff2").strip().lower()
        if subset is None:
            subset = bool(self.embed.get("subset", True))
        if level not in EMBED_LEVELS:
            level = "body"
        if fmt not in FORMATS:
            fmt = "woff2"

        notes: list[str] = []
        report: dict = {
            "level": level,
            "format": fmt,
            "subset": bool(subset),
            "faces": [],
            "total_source_mb": 0.0,
            "total_out_mb": 0.0,
            "elapsed": 0.0,
            "notes": notes,
        }
        plan = FontPlan(report=report)

        if level == "none":
            notes.append("嵌入档位为 none，使用系统回退链")
            report["family_by_role"] = {}
            report["fallback_families"] = self._fallback_families()
            return plan

        wanted_roles = self._roles_for_level(level)
        if not wanted_roles:
            notes.append("未配置任何字体角色，跳过嵌入")
            return plan

        # 字符集：本书全部文本 + 兜底
        charset = collect_charset(texts)
        report["charset_size"] = len(charset)

        out_dir = tmp_dir or _default_tmp_dir()
        os.makedirs(out_dir, exist_ok=True)
        t0 = time.perf_counter()
        license_lines: list[str] = []
        family_by_role: dict[str, str] = {}

        for role in wanted_roles:
            face = self.by_role(role, role_table)
            if face is None:
                notes.append("角色 %s 未配置可用字体" % ROLE_LABELS.get(role, role))
                continue
            if not os.path.exists(face.path):
                notes.append("字体文件缺失：%s" % face.path)
                continue

            base_name = "%s-%s" % (_slug(face.family), role)
            variants: list[tuple[str, str]] = []  # [(format, filename)]
            wanted = ["woff2", "truetype"] if fmt == "both" else (
                ["woff2"] if fmt == "woff2" else ["truetype"]
            )

            face_report = {
                "role": role,
                "role_label": ROLE_LABELS.get(role, role),
                "id": face.font_id,
                "family": face.family,
                "source_mb": round(face.source_size / 1048576, 2),
                "variants": [],
                "ok": False,
                "error": "",
            }

            entries_files: list[tuple[str, bytes]] = []
            for kind in wanted:
                ext = ".woff2" if kind == "woff2" else _src_ext(face.path)
                fname = base_name + ext
                dst = os.path.join(out_dir, fname)
                flavor = "woff2" if kind == "woff2" else None
                if subset:
                    ok, err = cached_subset_font(face.path, charset, dst, flavor=flavor)
                else:
                    ok, err = _copy_raw(face.path, dst, flavor=flavor)
                if not ok:
                    face_report["error"] = err
                    notes.append("字体 %s 处理失败：%s" % (face.display, err))
                    continue
                try:
                    with open(dst, "rb") as fh:
                        blob = fh.read()
                except Exception as exc:
                    face_report["error"] = repr(exc)[:160]
                    continue
                entries_files.append(("fonts/" + fname, blob))
                format_label = "woff2" if kind == "woff2" else ("opentype" if ext == ".otf" else "truetype")
                variants.append((format_label, fname))
                face_report["variants"].append(
                    {"format": format_label, "file": fname, "size_mb": round(len(blob) / 1048576, 3)}
                )

            if not variants:
                report["faces"].append(face_report)
                continue

            face_report["ok"] = True
            report["faces"].append(face_report)
            report["total_source_mb"] += face.source_size / 1048576
            for _, blob in entries_files:
                report["total_out_mb"] += len(blob) / 1048576
            plan.files.extend(entries_files)
            family_by_role[role] = face.family
            license_lines.append(
                "%-28s  %s  (%s)" % (face.family, face.display, os.path.basename(face.path))
            )
            plan.css += _font_face_block(face, variants) + "\n"

        report["elapsed"] = round(time.perf_counter() - t0, 2)
        report["total_source_mb"] = round(report["total_source_mb"], 2)
        report["total_out_mb"] = round(report["total_out_mb"], 2)

        # 体积超限自动降级
        limit = float(self.embed.get("max_size_mb") or 3.0)
        if report["total_out_mb"] > limit and level != "body":
            notes.append(
                "嵌入体积 %.2f MB 超出上限 %.1f MB，已降级为 body 档" % (report["total_out_mb"], limit)
            )
            return self.plan(texts, level="body", fmt=fmt, subset=subset, tmp_dir=tmp_dir, roles=role_table)

        if plan.files:
            plan.licenses = self._license_files(license_lines)
        # 只为「实际嵌入」的角色输出字体覆盖；未嵌入的角色交给风格 CSS 的回退链
        plan.family_by_role = dict(family_by_role)
        report["family_by_role"] = dict(family_by_role)
        report["embedded_roles"] = sorted(family_by_role.keys())
        report["fallback_families"] = self._fallback_families()
        return plan

    # -- 内部 --
    def _roles_for_level(self, level: str) -> list[str]:
        if level == "body":
            return ["body"]
        if level == "body+title":
            return ["body", "title"]
        if level == "full":
            return ["body", "title", "quote", "note"]
        return []

    def _fallback_families(self) -> dict:
        return {
            "body": self.fallbacks.get("serif", "serif"),
            "title": self.fallbacks.get("sans", "sans-serif"),
            "quote": self.fallbacks.get("fangsong", "serif"),
            "note": self.fallbacks.get("kai", "serif"),
        }

    def _license_files(self, lines: list[str]) -> list:
        notice = (
            "本电子书内嵌的字体均为 SIL Open Font License 1.1 授权，\n"
            "允许嵌入、再分发与商业使用。字体未作改名的修改，仅按本书\n"
            "字符集做了子集化裁剪。\n\n"
            "内嵌字体清单：\n" + "\n".join("  " + l for l in lines) + "\n"
        )
        return [("fonts/OFL.txt", OFL_TEXT), ("fonts/FONT-LICENSES.txt", notice)]


# --------------------------------------------------------------------------
# 配置读取
# --------------------------------------------------------------------------
def load_font_config(path: Optional[str] = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        return {}
    try:
        import yaml

        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _config_root(cfg: dict) -> Path:
    disc = (cfg or {}).get("discovery") or {}
    roots = disc.get("roots") or ["ttf"]
    first = str(roots[0])
    base = Path(first)
    if not base.is_absolute():
        base = PROJECT_ROOT / first
    return base


def _match_entry(entries: list, path: Path, root: Path) -> Optional[dict]:
    try:
        rel = path.relative_to(root).as_posix().lower()
    except Exception:
        rel = path.name.lower()
    name_low = path.name.lower()
    stem_low = path.stem.lower()
    # 1) 精确相对路径
    for e in entries:
        f = str(e.get("file") or "").replace("\\", "/").lower()
        if f and f == rel:
            return e
    # 2) 文件名
    for e in entries:
        f = str(e.get("file") or "").replace("\\", "/").lower()
        if f and Path(f).name == name_low:
            return e
    # 3) 去扩展名的 basename
    for e in entries:
        f = str(e.get("file") or "").replace("\\", "/").lower()
        if f and Path(f).stem == stem_low:
            return e
    return None


def _src_ext(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in (".otf", ".ttf", ".ttc") else ".ttf"


def _default_tmp_dir() -> str:
    import tempfile

    return os.path.join(tempfile.gettempdir(), "abf_fonts_%d" % int(time.time() * 1000))


# --------------------------------------------------------------------------
# 字符集
# --------------------------------------------------------------------------
def collect_charset(texts: Iterable[str], extras: str = "") -> str:
    chars = set(FALLBACK_CHARS)
    for t in texts:
        if t:
            chars.update(t)
    if extras:
        chars.update(extras)
    chars.discard("\x00")
    return "".join(sorted(chars))


# --------------------------------------------------------------------------
# 子集化
# --------------------------------------------------------------------------
def fonttools_available() -> bool:
    try:
        import fontTools  # noqa: F401

        return True
    except Exception:
        return False


def subset_font(src: str, chars: str, dst: str, flavor: Optional[str] = "woff2") -> tuple[bool, str]:
    """按字符集裁剪字体并写出。flavor 为 None 时输出 ttf/otf 原格式。"""
    try:
        from fontTools import subset as ftsubset
    except Exception as exc:
        return False, "fonttools 未安装: %r" % (exc,)

    opts = ftsubset.Options()
    if flavor:
        opts.flavor = flavor
    opts.layout_features = ["*"]
    opts.drop_tables = list(opts.drop_tables) + ["DSIG"]
    opts.notdef_outline = True
    opts.recalc_bounds = True
    opts.recalc_timestamp = False
    opts.name_IDs = ["*"]
    opts.name_legacy = True
    opts.name_languages = ["*"]
    opts.desubroutinize = True

    font = None
    try:
        font = ftsubset.load_font(str(src), opts)
        sub = ftsubset.Subsetter(options=opts)
        sub.populate(unicodes=sorted({ord(c) for c in chars}))
        sub.subset(font)
        ftsubset.save_font(font, str(dst), opts)
        return True, ""
    except Exception as exc:
        return False, repr(exc)[:220]
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


_FONT_CACHE: Optional[str] = None


def font_cache_dir() -> str:
    """跨进程复用的字体子集缓存目录。"""
    global _FONT_CACHE
    if _FONT_CACHE is None:
        import tempfile

        d = os.path.join(tempfile.gettempdir(), "abf_font_cache")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        _FONT_CACHE = d
    return _FONT_CACHE


def cached_subset_font(src: str, chars: str, dst: str, flavor: Optional[str] = "woff2") -> tuple[bool, str]:
    """带磁盘缓存的子集化。

    CJK 字形的 woff2 压缩是全链路最慢的一步（11 MB 源约 3-10 秒），而同一本书
    重复转换、或不同书共用字符集时结果完全一致，缓存收益明显。
    """
    import hashlib
    import shutil

    key = hashlib.sha1(
        ("%s|%s|%s" % (os.path.abspath(src), flavor or "raw", chars)).encode("utf-8")
    ).hexdigest()[:20]
    ext = ".woff2" if flavor == "woff2" else _src_ext(src)
    cache = os.path.join(font_cache_dir(), key + ext)
    if os.path.exists(cache) and os.path.getsize(cache) > 1024:
        try:
            shutil.copyfile(cache, dst)
            return True, ""
        except Exception:
            pass
    ok, err = subset_font(src, chars, dst, flavor=flavor)
    if ok:
        try:
            shutil.copyfile(dst, cache)
        except Exception:
            pass
    return ok, err


def _copy_raw(src: str, dst: str, flavor: Optional[str] = None) -> tuple[bool, str]:
    """不子集化时的原样复制（flavor 仅支持 None）。"""
    try:
        import shutil

        shutil.copyfile(src, dst)
        return True, ""
    except Exception as exc:
        return False, repr(exc)[:180]


# --------------------------------------------------------------------------
# CSS 生成
# --------------------------------------------------------------------------
def _font_face_block(face: FontFace, variants: list[tuple[str, str]], url_prefix: str = "fonts/") -> str:
    srcs = ",\n       ".join(
        'url("%s%s") format("%s")' % (url_prefix, fname, fmt) for fmt, fname in variants
    )
    return (
        "@font-face {\n"
        '  font-family: "%s";\n'
        "  font-style: %s;\n"
        "  font-weight: %d;\n"
        "  font-display: swap;\n"
        "  src: %s;\n"
        "}" % (face.family, face.style, face.weight, srcs)
    )


def build_font_face_css(blobs: list[tuple[FontFace, list[tuple[str, str]]]]) -> str:
    return "\n\n".join(_font_face_block(f, v) for f, v in blobs)


# --------------------------------------------------------------------------
# 与 pipeline 配置合并
# --------------------------------------------------------------------------
def resolve_typography(pipeline_cfg: dict, registry: Optional[FontRegistry] = None) -> dict:
    """合并 configs/pipeline.yaml 的 typography 段与字体库默认值。"""
    reg = registry or FontRegistry.load()
    typo = dict((pipeline_cfg or {}).get("typography") or {})
    roles = dict(reg.roles)
    for k, v in (typo.get("roles") or {}).items():
        if v is not None:
            roles[k] = v
    return {
        "level": str(typo.get("embed") or reg.embed.get("level") or "body"),
        "format": str(typo.get("format") or reg.embed.get("format") or "woff2"),
        "subset": bool(typo.get("subset", reg.embed.get("subset", True))),
        "roles": roles,
        "fallbacks": dict(reg.fallbacks),
        "indent": str(typo.get("indent") or "space"),
        "vertical": bool(typo.get("vertical", False)),
        "title_page": bool(typo.get("title_page", True)),
        "page_ornament": str(typo.get("page_ornament") or "chapter"),
    }


def list_font_options(config_path: Optional[str] = None) -> dict:
    """供 API / CLI 输出可用字体清单。"""
    try:
        reg = FontRegistry.load(config_path=config_path)
        data = reg.options()
        data["fonttools"] = fonttools_available()
        return data
    except Exception as exc:
        return {"fonts": [], "error": repr(exc)[:200], "fonttools": fonttools_available()}
