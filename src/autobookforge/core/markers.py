"""章节标记识别引擎（书内自适应）。

设计动因
--------
真实校园 / 网文 TXT 的分章写法高度不统一，穷举正则必然漏判：

* ``第12章 标题`` / ``第十二回``        —— 标准体
* ``1`` / ``2`` / ``3``（裸数字占一行）  —— 分卷下载器常见
* ``1、`` / ``10、标题``               —— 手打体
* ``《一》标题`` / ``【三】标题``        —— 古风 / 台版
* ``熊熊小说--正章7``                  —— 站点水印式
* ``Chapter 3.标题`` + ``3.1 标题``    —— 学术 / 日志体
* ``胡子与他的熊熊集团``（无序号短题）  —— 评书体

因此本模块不再依赖单一正则，而是：

1. **多族候选生成**：把上述写法建模为若干「标记族」，每族带优先级 / 位数捕获组 / 结构约束。
2. **逐族单调性过滤**：章节序号必须非递减，丢弃回卷与噪声（站点水印、正文数字）。
3. **族间并集 + 邻近合并**：同一章被两族重复标注时合并为一个（保留信息量更大的标题）。
4. **主族评分与兜底**：章族不足时依次回退到书内「场景短标题族」（自适应学习共同前缀，
   用于评书体 / 无序号分段文本）。
5. **层级组装**：卷 / 章 / 节三级，节归属于其前面的章。

模块对外只暴露 ``discover``，返回 :class:`DiscoverResult`，其余为内部实现。
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .textutil import cn_to_arabic

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CN = "0-9〇零一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾佰仟"
CN_CLS = f"[{CN}]"

SENTENCE_END = set("。！？…!?；;")
PROSE_TAIL = set("，、：,:")
LEAD_JUNK = set("　 \t:：、.．-—～~·|｜")

MAX_LINE_LEN = 64
MERGE_GAP_SAME = 6  # 同号重复标注的最大行距。
# 目录页里的同号标记之间必然夹着其它章标记（因此永远不会成为「相邻对」），
# 本窗口只会命中正文中真正的重复标注，不会把目录标题并到正文首章上。

CHAPTER = "chapter"
SECTION = "section"
VOLUME = "volume"
SCENE = "scene"


@dataclass
class MarkerFamily:
    """一类章节标记写法。"""

    name: str
    kind: str
    priority: int
    pattern: re.Pattern
    number_group: int = 1
    number_group2: int = 0        # 复合序号（如 3.1 的次序号），用于排序
    title_group: int = 2
    weak: bool = False            # 弱族：必须被空行夹住才可信
    max_title_len: int = 44
    label: str = ""               # 标题缺失时的回退模板，{} 为序号

    def __post_init__(self) -> None:
        if not self.label:
            self.label = "第{}章"


@dataclass
class Marker:
    """一行被识别出的章节标记。"""

    line: int
    family: MarkerFamily
    number: Optional[int]
    raw: str
    title: str
    framed: bool = False
    display: Optional[int] = None   # 对外展示序号（复合序号取主序号）

    @property
    def kind(self) -> str:
        return self.family.kind

    @property
    def num(self) -> Optional[int]:
        """用于章节落库的序号：优先展示序号。"""
        return self.display if self.display is not None else self.number

    @property
    def priority(self) -> int:
        return self.family.priority


@dataclass
class DiscoverResult:
    """标记发现结果。"""

    markers: list[Marker] = field(default_factory=list)
    strategy: str = "none"
    stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 标记族定义
# --------------------------------------------------------------------------- #

def default_families() -> list[MarkerFamily]:
    """内置标记族，按优先级降序返回。"""
    fams = [
        # ---- 卷 ----
        MarkerFamily("vol_di", VOLUME, 100,
                     re.compile(rf"^第\s*({CN_CLS}{{1,8}})\s*[卷部篇]\s*(?:[　\s:：、.．\-—]*(.{{0,40}}))?$"),
                     label="第{}卷"),
        MarkerFamily("vol_scope", VOLUME, 96,
                     re.compile(r"^([上中下])\s*[篇部卷]\s*(.{0,36})$"),
                     number_group=0, title_group=0, label="{}篇"),
        # ---- 章 ----
        MarkerFamily("chap_special", CHAPTER, 94,
                     re.compile(r"^(序章|序言|序|楔子|引子|尾声|终章|后记|後記|番外|完本感言|作者的话|作者的話|完结感言)\s*[:：、.．]?\s*(.{0,34})$"),
                     number_group=0, title_group=0, label="{}"),
        MarkerFamily("chap_di", CHAPTER, 92,
                     re.compile(rf"^第\s*({CN_CLS}{{1,8}})\s*[章回]\s*(?:[　\s:：、.．\-—]*(.{{0,40}}))?$"),
                     label="第{}章"),
        MarkerFamily("chap_zhang_en", CHAPTER, 88,
                     re.compile(r"^(?:Chapter|CHAPTER|chapter)\s*([0-9]{1,4})\s*[.、:：\-]?\s*(.{0,56})$"),
                     label="Chapter {}"),
        MarkerFamily("chap_site_prefix", CHAPTER, 86,
                     re.compile(r"^(?:[【\[][^】\]]{0,12}[】\]]|[^\s]{1,12}(?:小说|書屋|书屋|书院|書院|书盟|文学|文学网)"
                                r"\s*(?:--|——|－|—|-))\s*(正章|第)?\s*([0-9]{1,4})\s*章?\s*(.{0,40})$"),
                     number_group=2, title_group=3, label="正章{}"),
        MarkerFamily("chap_book_num", CHAPTER, 84,
                     re.compile(rf"^《\s*({CN_CLS}{{1,6}})\s*》\s*(.{{0,40}})$"),
                     label="第{}章"),
        MarkerFamily("chap_bracket_num", CHAPTER, 82,
                     re.compile(rf"^[【〔〖\[（(]\s*({CN_CLS}{{1,6}})\s*[】〕〗\]）)]\s*(.{{0,40}})$"),
                     label="第{}章"),
        MarkerFamily("chap_num_dun", CHAPTER, 74,
                     re.compile(r"^([0-9]{1,3})\s*[、.．]\s*(?!\d)(.{0,40})$"),
                     label="第{}章"),
        MarkerFamily("chap_num_cn_dun", CHAPTER, 72,
                     re.compile(rf"^([〇零一二三四五六七八九十]{{1,4}})\s*[、.．]\s*(.{{0,40}})$"),
                     label="第{}章"),
        # ---- 节 ----
        MarkerFamily("sec_dot", SECTION, 62,
                     re.compile(r"^([0-9]{1,2})\.([0-9]{1,2})(?!\d)\s+(.{0,50})$"),
                     number_group=1, number_group2=2, title_group=3, label="{}"),
        MarkerFamily("sec_bracket", SECTION, 58,
                     re.compile(rf"^[（(]\s*({CN_CLS}{{1,4}})\s*[）)]\s*(.{{0,40}})$"),
                     label="第{}节"),
        # ---- 弱族（需空行夹住）----
        MarkerFamily("bare_num", CHAPTER, 44,
                     re.compile(r"^([0-9]{1,4})$"),
                     number_group=1, title_group=0, weak=True, label="第{}章"),
        MarkerFamily("num_space", CHAPTER, 40,
                     re.compile(r"^([0-9]{1,4})\s+([^\s0-9].{0,36})$"),
                     number_group=1, title_group=2, weak=True, label="第{}章"),
    ]
    fams.sort(key=lambda f: -f.priority)
    return fams


SCENE_FAMILY = MarkerFamily(
    "scene_title", SCENE, 20,
    re.compile(r"^(.{2,26})$"),
    number_group=0, title_group=0, weak=True, label="{}",
)


def load_families_from_yaml(path: str) -> Optional[list[MarkerFamily]]:
    """从 chapter_patterns.yaml 的 families 段加载自定义族；缺失返回 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return None
    raw = data.get("families")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[MarkerFamily] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pat = item.get("pattern")
        if not pat:
            continue
        try:
            compiled = re.compile(str(pat))
        except re.error:
            continue
        out.append(
            MarkerFamily(
                name=str(item.get("name", f"custom_{len(out)}")),
                kind=str(item.get("kind", CHAPTER)),
                priority=int(item.get("priority", 50)),
                pattern=compiled,
                number_group=int(item.get("number_group", 1)),
                number_group2=int(item.get("number_group2", 0)),
                title_group=int(item.get("title_group", 2)),
                weak=bool(item.get("weak", False)),
                max_title_len=int(item.get("max_title_len", 44)),
                label=str(item.get("label", "第{}章")),
            )
        )
    if not out:
        return None
    out.sort(key=lambda f: -f.priority)
    return out


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _strip_line(line: str) -> str:
    # 仅去掉首尾空白（含全角空格），保留标题内部的全角空格，避免改写作者写法
    return line.strip()


def _bare(line: str) -> str:
    """用于空行判定的轻量 strip（保留全角空格语义，仅判空）。"""
    return line.strip().strip("\u3000 \t")


def _to_int(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return cn_to_arabic(token)


def _clean_title(text: str) -> str:
    t = (text or "").strip()
    t = t.strip("\u3000 \t")
    t = t.lstrip("".join(LEAD_JUNK))
    t = re.sub(r"[\s:：、.．\-—]+$", "", t)
    return t.strip()


def _looks_like_prose(text: str) -> bool:
    """标题位置却写成句子的情况（正文被误判）。"""
    if not text:
        return False
    n = len(text)
    if n >= 14 and text[-1] in SENTENCE_END:
        return True
    if n >= 14 and text[-1] in PROSE_TAIL:
        return True
    if n >= 22 and ("。" in text or "，" in text):
        return True
    return False


def _title_richness(m: Marker) -> int:
    """标题信息量：带文字标题 > 纯序号。"""
    if m.number is None:
        return len(m.title) + 6
    base = 0 if not m.title else len(m.title) + 4
    return base


# 本身已是完整标题文本的族：保留作者的原始写法
# （不把「第一卷　上」改写成「第1卷 上」，也不丢失全角空格）
PRESERVE_FAMILIES = frozenset({
    "chap_di", "chap_special", "sec_dot", "sec_bracket",
    "vol_di", "vol_scope", "scene_title",
    "custom_vol", "custom_chap", "custom_sec",
})


def render_title(m: Marker) -> str:
    """生成最终章节标题。

    区分两种来源：
      * 原文已含标题文字（第X章 / 卷 / 场景题） -> 原样保留
      * 原文只是序号或异形包裹（裸数字 / 1、 / 《一》 / 站点前缀） -> 归一化为规范标题
    """
    if m.family.name in PRESERVE_FAMILIES:
        return m.raw.strip()
    fam = m.family
    if m.number is None:
        return m.title or m.raw.strip()
    head = fam.label.replace("{}", str(m.number))
    return f"{head} {m.title}".strip() if m.title else head


# --------------------------------------------------------------------------- #
# 候选发现
# --------------------------------------------------------------------------- #

def _iter_candidates(
    lines: Sequence[str],
    families: Sequence[MarkerFamily],
) -> list[Marker]:
    out: list[Marker] = []
    n = len(lines)
    for i, raw in enumerate(lines):
        s = _strip_line(raw)
        if not s or len(s) > MAX_LINE_LEN:
            continue
        prev_blank = i == 0 or not _bare(lines[i - 1])
        next_blank = i == n - 1 or not _bare(lines[i + 1])
        # 弱族（裸数字 / 数字+标题）在被空行夹住时可信；真实语料里标记行常是
        # "上文空行 + 下文紧接正文"，故只要一侧为空行即可。
        framed = prev_blank or next_blank
        for fam in families:
            m = fam.pattern.match(s)
            if not m:
                continue
            ng = fam.number_group
            raw_num = m.group(ng) if 0 < ng <= m.re.groups else None
            number = _to_int(raw_num)
            display = number
            ng2 = fam.number_group2
            if ng2 and 0 < ng2 <= m.re.groups:
                minor = _to_int(m.group(ng2))
                if number is not None and minor is not None:
                    # 复合排序键：保证 3.1 < 3.2 < 3.4 < 4.1 严格递增
                    number = number * 1000 + minor
            tg = fam.title_group
            raw_title = m.group(tg) if 0 < tg <= m.re.groups else ""
            title = _clean_title(raw_title or "")
            if fam.weak and not framed:
                continue
            if title and _looks_like_prose(title):
                continue
            if number is not None and number <= 0:
                continue
            out.append(Marker(i, fam, number, s, title, framed=framed, display=display))
            break
    return out


def discover_scene_titles(lines: Sequence[str], *, min_count: int = 5) -> list[Marker]:
    """评书体 / 无序号文本：自适应学习短标题行。"""
    n = len(lines)
    cands: list[str] = []
    idxs: list[int] = []
    for i, raw in enumerate(lines):
        s = _strip_line(raw)
        if not (2 <= len(s) <= 26):
            continue
        if re.search(r"[。！？…；：，、]$", s):
            continue
        if re.search(r"[。！？…；]", s):
            continue
        if s[0] in "。，、：；！？…（）()［］【】〔〕“”\"'《》〈〉":
            continue
        if not re.search(r"[\u4e00-\u9fff]", s):
            continue
        prev_blank = i == 0 or not _bare(lines[i - 1])
        next_blank = i == n - 1 or not _bare(lines[i + 1])
        if not (prev_blank or next_blank):
            continue
        cands.append(s)
        idxs.append(i)

    if len(cands) < min_count:
        return []

    prefix_hits = Counter(c[:2] for c in cands if len(c) >= 2)
    top_prefix, top_n = prefix_hits.most_common(1)[0] if prefix_hits else ("", 0)
    use_prefix = top_n >= max(2, int(len(cands) * 0.35))

    marks: list[Marker] = []
    for i, s in zip(idxs, cands):
        if use_prefix and not s.startswith(top_prefix):
            continue
        if re.search(r"(如下|请看|欲知|且听|结果|于是|因此|所以)", s):
            continue
        marks.append(Marker(i, SCENE_FAMILY, None, s, s, framed=True))

    if len(marks) < min_count:
        return []
    marks.append(Marker(len(lines), SCENE_FAMILY, None, "", "", framed=True))  # 收尾哨兵
    return marks


# --------------------------------------------------------------------------- #
# 序列优选
# --------------------------------------------------------------------------- #

def _monotonic(marks: Sequence[Marker]) -> list[Marker]:
    """保留序号非递减的子序列；允许一次「编号重开」（目录页之后 / 分卷重编号）。

    仅按 = 接受紧邻的同号标记（重复标注），远端同号一律视为回卷噪声；
    若序号回卷到 1，则判为编号重新开始，从该点起继续累计。
    """
    kept: list[Marker] = []
    last: Optional[int] = None
    last_line: Optional[int] = None
    for m in marks:
        if m.number is None:
            kept.append(m)
            continue
        if last is None or m.number > last:
            kept.append(m)
            last, last_line = m.number, m.line
            continue
        if m.number == last and last_line is not None and m.line - last_line <= MERGE_GAP_SAME:
            kept.append(m)
            last_line = m.line
            continue
        if m.number == 1 and last is not None and m.number < last:
            kept.append(m)
            last, last_line = 1, m.line
    return kept


def _merge_close(marks: Sequence[Marker], *, gap: int = MERGE_GAP_SAME) -> list[Marker]:
    """合并同一章被重复标注的邻近标记。"""
    ordered = sorted(marks, key=lambda m: m.line)
    out: list[Marker] = []
    for m in ordered:
        if out:
            p = out[-1]
            d = m.line - p.line
            if d <= 3 and p.number is None and m.number is None:
                if _title_richness(m) > _title_richness(p):
                    out[-1] = m
                continue
            same = (p.number is not None and m.number is not None and p.number == m.number)
            if (same and d <= gap) or (d <= 3 and (p.number is None or m.number is None)):
                if _title_richness(m) > _title_richness(p):
                    out[-1] = m
                continue
        out.append(m)
    return out


def _family_scores(cands: Sequence[Marker], total_lines: int) -> dict[str, float]:
    by_fam: dict[str, list[Marker]] = {}
    for m in cands:
        by_fam.setdefault(m.family.name, []).append(m)
    scores: dict[str, float] = {}
    for name, group in by_fam.items():
        group = sorted(group, key=lambda m: m.line)
        kept = _monotonic(group)
        if len(kept) < 3:
            continue
        span = (kept[-1].line - kept[0].line + 1) / max(1, total_lines)
        mono = len(kept) / max(1, len(group))
        fam = group[0].family
        scores[name] = len(kept) * (0.4 + 0.6 * mono) * (0.25 + 0.75 * span) * (fam.priority / 100.0)
    return scores


def _subdivide(lines: Sequence[str], marks: list[Marker], families: Sequence[MarkerFamily],
               *, max_chars: int) -> list[Marker]:
    """超长章内部用更强证据族二次切分。"""
    extra_fams = [f for f in families if f.kind in (SECTION, CHAPTER)]
    out = list(marks)
    changed = True
    guard = 0
    while changed and guard < 2:
        changed = False
        guard += 1
        out.sort(key=lambda m: m.line)
        spans: list[tuple[int, int]] = []
        for i, m in enumerate(out):
            start = m.line + 1
            end = out[i + 1].line if i + 1 < len(out) else len(lines)
            body = sum(len(lines[k].strip()) for k in range(start, end))
            if body > max_chars:
                spans.append((start, end))
        for start, end in spans:
            window = lines[start:end]
            inner = _iter_candidates(window, extra_fams)
            inner = [Marker(m.line + start, m.family, m.number, m.raw, m.title, m.framed, m.display) for m in inner]
            inner = [m for m in inner if m.kind in (CHAPTER, SECTION)]
            inner = _monotonic(inner)
            inner = _merge_close(inner)
            if len(inner) >= 2:
                out.extend(inner)
                changed = True
    out.sort(key=lambda m: m.line)
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def discover(
    lines: Sequence[str],
    *,
    families: Optional[Sequence[MarkerFamily]] = None,
    max_chapter_length: int = 50000,
    allow_scene_fallback: bool = True,
) -> DiscoverResult:
    """在给定行序列上发现章节标记。"""
    fams = list(families or default_families())
    cands = _iter_candidates(lines, fams)
    notes: list[str] = []

    chap_cands = [m for m in cands if m.kind == CHAPTER]
    scores = _family_scores(chap_cands, len(lines))

    chosen: list[Marker] = []
    strategy = "none"

    # 章族并集：每族先各自做序号单调过滤，再合并去重。
    # 不设"每族至少 N 个"的门槛——短篇与分卷文件里同一族常常只有 2 个标记，
    # 一旦按门槛丢弃整族，链路就会整体退化为单章。
    per_family: list[Marker] = []
    by_fam: dict[str, list[Marker]] = {}
    for m in chap_cands:
        by_fam.setdefault(m.family.name, []).append(m)
    for name, group in by_fam.items():
        group = sorted(group, key=lambda m: m.line)
        per_family.extend(_monotonic(group))
    per_family.sort(key=lambda m: m.line)
    chosen = _merge_close(per_family)
    if chosen:
        strategy = "family"

    if len(chosen) < 2 and allow_scene_fallback:
        scene = discover_scene_titles(lines)
        if len(scene) >= 4:
            chosen = scene
            strategy = "scene"
            notes.append("未发现序号式章节标记，改用书内场景短标题自适应分章")

    if len(chosen) < 2:
        return DiscoverResult([], strategy, {"candidates": len(cands)}, notes + ["未发现任何可用章节标记"])

    chosen = _subdivide(lines, chosen, fams, max_chars=max_chapter_length)

    # 附加卷 / 节标记（层级用）
    volumes = _merge_close(_monotonic([m for m in cands if m.kind == VOLUME]))
    sections = _merge_close(_monotonic([m for m in cands if m.kind == SECTION]))

    all_marks = sorted(chosen + volumes + sections, key=lambda m: m.line)
    dedup: list[Marker] = []
    for m in all_marks:
        if dedup and dedup[-1].line == m.line:
            keep = m if (m.kind == CHAPTER and dedup[-1].kind != CHAPTER) else dedup[-1]
            dedup[-1] = keep
            continue
        dedup.append(m)

    stats = {
        "candidates": len(cands),
        "chapter_markers": len(chosen),
        "volume_markers": len(volumes),
        "section_markers": len(sections),
        "family_scores": {k: round(v, 3) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])},
    }
    return DiscoverResult(dedup, strategy, stats, notes)
