"""书籍元信息解析与站点水印 / 广告清除。

真实 TXT 的"噪声"分三类，处理策略完全不同：

============  ==========================================  ==========================
类别          典型形态                                    策略
============  ==========================================  ==========================
元信息        《书名》作者：xxx【完结+番外】               解析为结构化元数据，行本身移除
站点水印      久游书盟http://www.9ubook.com                整行删除 / 行内子串剥离
推广导流语    读书，下载电子图书就来"久游书盟"，每天更新…  整行删除（多词共现判定）
============  ==========================================  ==========================

关键设计：**先解析元信息，再做水印判定**。否则 ``《[熊熊小说]爱有天意》`` 会因为含站点名
而被当成广告删掉——它其实是书名行。元信息行一旦被消费，就退出后续所有删除候选。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .markers import default_families, discover_scene_titles

# --------------------------------------------------------------------------- #
# 站点字典与推广词
# --------------------------------------------------------------------------- #

DEFAULT_SITE_NAMES: tuple[str, ...] = (
    "久游书盟", "9ubook", "笔趣阁", "顶点小说", "无错小说", "全本小说", "熊猫小说",
    "熊熊小说", "书包网", "久久小说", "天天书吧", "言情小说吧", "潇湘书院", "起点中文网",
    "创世中文网", "纵横中文网", "17k", "晋江文学城", "飞卢小说", "刺猬猫", "塔读文学",
    "掌阅", "云起书院", "红袖添香", "逐浪小说", "看书网", "小说阅读网", "txt之家",
    "奇书网", "贼吧网", "糯米小说", "书友群", "爱书网", "八零电子书", "傻鸟小说网",
    "去读读", "56书库", "棉花糖小说", "新笔趣", "思路客", "燃文小说", "辣文小说",
)

DEFAULT_PROMO_FRAGMENTS: tuple[str, ...] = (
    "下载电子图书", "每天更新", "最快最全", "免费图书", "包您一爽", "请记住本书",
    "最新章节", "手机阅读", "全文免费阅读", "无弹窗", "加入书架", "推荐票",
    "txt下载", "电子书下载", "全集下载", "免费阅读", "在线阅读", "全本免费",
    "更新最快", "请收藏本站", "记住本站", "首发", "独家首发", "本站网址",
    "分卷阅读", "更多精彩", "下载器", "内容简介", "全文字",
)

DEFAULT_AD_KEYWORDS: tuple[str, ...] = (
    "广告", "推广", "赞助", "免责声明", "版权归", "扫码", "二维码",
)

URL_RE = re.compile(r"(?:https?://|www\.)[^\s\u4e00-\u9fff，。、；：！？）】”』]{3,}")
_BARE_HOST_RE = re.compile(r"\b[a-z0-9\-]{2,30}\.(?:com|net|cn|org|cc|me|top|xyz|info|tv)\b", re.I)
_QQ_RE = re.compile(r"(?:QQ|qq|企鹅)\s*群?\s*[:：]?\s*\d{5,}")
_WECHAT_RE = re.compile(r"(?:微信|weixin|公众号|WeChat)\s*(?:号|id)?\s*[:：]?\s*[A-Za-z0-9_\-]{4,}")
_SEP_LINE_RE = re.compile(r"^[\s\-=*_~·—─━#=]{4,}$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cjk_count(s: str) -> int:
    return len(_CJK_RE.findall(s))


@dataclass
class AdConfig:
    """广告清理配置。"""

    site_names: list[str] = field(default_factory=lambda: list(DEFAULT_SITE_NAMES))
    promo_fragments: list[str] = field(default_factory=lambda: list(DEFAULT_PROMO_FRAGMENTS))
    ad_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_AD_KEYWORDS))
    extra_patterns: list[str] = field(default_factory=list)
    protect_words: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "AdConfig":
        cfg = cls()
        if isinstance(data.get("site_names"), list):
            cfg.site_names = [str(x) for x in data["site_names"]]
        if isinstance(data.get("promo_fragments"), list):
            cfg.promo_fragments = [str(x) for x in data["promo_fragments"]]
        if isinstance(data.get("ad_keywords"), list):
            cfg.ad_keywords = [str(x) for x in data["ad_keywords"]]
        if isinstance(data.get("extra_patterns"), list):
            cfg.extra_patterns = [str(x) for x in data["extra_patterns"]]
        if isinstance(data.get("protect_words"), list):
            cfg.protect_words = [str(x) for x in data["protect_words"]]
        return cfg

    @classmethod
    def load(cls, path: Optional[str]) -> "AdConfig":
        if not path or not os.path.exists(path):
            return cls()
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:
            return cls()
        return cls.from_dict(data if isinstance(data, dict) else {})


def find_site(s: str, cfg: AdConfig) -> str:
    low = s.lower()
    for name in cfg.site_names:
        if name.lower() in low:
            return name
    return ""


def looks_like_watermark(s: str, cfg: AdConfig) -> bool:
    """整行是否为站点水印 / 推广导流语。"""
    if not s:
        return False
    n = len(s)
    if _SEP_LINE_RE.match(s):
        return True
    if any(w and w in s for w in cfg.protect_words):
        return False

    if URL_RE.search(s) or _BARE_HOST_RE.search(s):
        # 纯水印行（中文极少）整行删；正文夹带 URL 的行只做就地剥离
        return _cjk_count(s) <= 10 or n <= 24

    if _QQ_RE.search(s) or _WECHAT_RE.search(s):
        return True

    site = find_site(s, cfg)
    if site and n <= 60 and "《" not in s and "》" not in s:
        promo_hits = sum(1 for f in cfg.promo_fragments if f in s)
        if promo_hits >= 1:
            return True
        # 行内几乎只有站点名本身
        residue = s
        for token in (site, "手机站", "手机版", "网", "书盟", "小说", "阅读"):
            residue = residue.replace(token, "")
        residue = re.sub(r"[\[\]【】()（）\s\-—·|｜:：]+", "", residue)
        if len(residue) <= 2:
            return True

    promo_hits = sum(1 for f in cfg.promo_fragments if f in s)
    if promo_hits >= 2 and n <= 120:
        return True

    kw_hits = sum(1 for f in cfg.ad_keywords if f in s)
    if kw_hits >= 2 and n <= 80:
        return True

    for pat in cfg.extra_patterns:
        try:
            if re.search(pat, s):
                return True
        except re.error:
            continue
    return False


def strip_inline_watermarks(s: str, cfg: AdConfig) -> str:
    """剥离段落内部的 URL 与句尾站点名，保留正文。"""
    if not s:
        return s
    out = URL_RE.sub("", s)
    out = _BARE_HOST_RE.sub("", out)
    out = _QQ_RE.sub("", out)
    out = _WECHAT_RE.sub("", out)
    # 句尾站点名（最多剥两层，避免误伤正文中的正常提及）
    for _ in range(2):
        out2 = out.rstrip()
        for name in cfg.site_names:
            if out2.lower().endswith(name.lower()):
                out2 = out2[: len(out2) - len(name)]
                break
        else:
            break
        out = out2
    out = re.sub(r"[\s\u3000]+$", "", out)
    out = re.sub(r"[（(【\[]\s*[）)】\]]$", "", out)
    return out


# --------------------------------------------------------------------------- #
# 元信息解析
# --------------------------------------------------------------------------- #

_TAG_ALLOW = re.compile(
    r"(现代|都市|古代|未来|科幻|奇幻|玄幻|武侠|仙侠|历史|军事|悬疑|推理|灵异|恐怖|"
    r"校园|青春|言情|纯爱|耽美|百合|生子|虐心|甜文|爽文|HE|BE|高H|清水|"
    r"日志体|书信体|第一人称|第三人称|同人|原创|完结|连载|短篇|长篇|中篇|"
    r"番外|上部|中部|下部|全集|全本|真人|明星|校园|职场|种田|系统|快穿)"
)

_TITLE_AUTHOR_RE = re.compile(
    r"^[《〈【<\[]?\s*(?P<title>[^》〉】>\]]{1,48}?)\s*[》〉】>\]]?\s*"
    r"(?:作者|著者|原著|作\s*者)\s*[:：]?\s*(?P<author>[^（(\[【,，。;；]{1,30})"
)
_BRACKET_TITLE_RE = re.compile(r"^[《〈]\s*(?P<title>[^》〉]{1,48})\s*[》〉]\s*(?P<rest>.*)$")
_ANGLE_TITLE_RE = re.compile(r"^[<＜]\s*(?P<title>[^>＞]{1,48})\s*[>＞]\s*(?P<rest>.*)$")
_AUTHOR_ONLY_RE = re.compile(r"^(?:作者|著者|原\s*著|BY|by|By|作\s*者)\s*[:：]?\s*(?P<author>.{1,36})$")
_TAG_LINE_RE = re.compile(r"^(?:属性分类|标签|题材|类型|分类|关键词|内容标签|风格)\s*[:：]\s*(?P<tags>.{1,90})$")
_PAREN_TAG_RE = re.compile(r"^[（(]\s*(?P<tags>[^)）]{1,80})\s*[)）]\s*$")
_SUMMARY_RE = re.compile(r"^(?:简介|内容简介|作品简介|文案|摘要|题记)\s*[:：]?\s*(?P<text>.{0,300})$")
_BY_TAIL_RE = re.compile(r"\s*(?:BY|by|By)\s*[:：]?\s*(?P<author>[^（(\[【]{1,30}).*$")
_JUNK_BRACKET_RE = re.compile(r"\[[^\]]{0,20}\]|【[^】]{0,20}】|（[^）]{0,20}）|\([^)]{0,20}\)")


@dataclass
class BookMeta:
    """书籍元信息。"""

    title: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    site: str = ""
    consumed: list[int] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "author": self.author,
            "tags": list(self.tags),
            "description": self.description,
            "site": self.site,
            "consumed_lines": len(self.consumed),
        }


def split_tags(text: str) -> list[str]:
    parts = re.split(r"[/／|｜,，、;；\s]+", text or "")
    out: list[str] = []
    for p in parts:
        p = p.strip("　 ·、.．-—()（）【】[]")
        if not p:
            continue
        p = re.sub(r"^(?:BY|by|By)\s*", "", p)
        if len(p) > 8:
            m = _TAG_ALLOW.search(p)
            if m:
                p = m.group(1)
            else:
                continue
        if p and p not in out:
            out.append(p)
    return out


def _clean_title(raw: str) -> str:
    t = (raw or "").strip()
    t = t.strip("　 \t")
    # 先去括号内容（[站点] / 【完结】 / （备注）），再去外包裹符号，顺序不可颠倒，
    # 否则 "[熊熊小说]爱有天意" 会残留 "熊熊小说]爱有天意"。
    t = _JUNK_BRACKET_RE.sub("", t)
    t = re.sub(r"^[《〈【<\[\s]+", "", t)
    t = re.sub(r"[》〉】>\]]+$", "", t)
    t = _BY_TAIL_RE.sub("", t)
    t = re.sub(r"(?:作者|著者|原著)\s*[:：]?\s*$", "", t)
    t = re.sub(r"[\s_\-]*(?:utf-?8|utf8|gbk|gb2312|gb18030|big5|ansi|unicode)$", "", t, flags=re.I)
    t = re.sub(r"[\s\-_·|｜]+$", "", t)
    t = re.sub(r"\s*\d{1,3}$", "", t)
    return t.strip() or ""


def _clean_author(raw: str) -> str:
    a = (raw or "").strip()
    a = _JUNK_BRACKET_RE.sub("", a)
    a = re.split(r"[/／|｜,，、;；]", a)[0]
    a = re.sub(r"[\s\-_·]+$", "", a)
    a = a.strip("　 :：")
    a = re.sub(r"^(?:BY|by|By)\s*", "", a)
    for _ in range(3):
        stripped = re.sub(
            r"(科幻|言情|都市|玄幻|武侠|历史|悬疑|灵异|同人|完结|番外|上部|中部|下部|全集|全本|"
            r"现代|古代|未来|校园|日志体|高H|HE|BE|短篇|长篇)$",
            "", a,
        )
        if stripped == a:
            break
        a = stripped
    return a.strip("　 -_·/|") or ""


def _filename_meta(filename: str) -> tuple[str, str, list[str]]:
    """从文件名抽取 (书名, 作者, 标签)。"""
    stem = re.sub(r"\.(txt|text)$", "", os.path.basename(filename or ""), flags=re.I)
    title = ""
    author = ""
    tags: list[str] = []

    # 1) 书本名：优先书名号，其次「作者」之前的部分
    mb = _BRACKET_TITLE_RE.match(stem)
    body = stem
    if mb:
        title = _clean_title(mb.group("title"))
        body = mb.group("rest") or ""

    mta = _TITLE_AUTHOR_RE.match(stem)
    if not title and mta:
        title = _clean_title(mta.group("title"))

    # 2) 作者：作者：xxx / BY xxx
    am = re.search(r"(?:作者|著者|原著|作\s*者|BY|by|By)\s*[:：]?\s*(?P<a>[^（(\[【,，。;；]{1,30})", body or stem)
    if am:
        author = _clean_author(am.group("a"))
    elif mta:
        author = _clean_author(mta.group("author"))

    # 3) 标签：只取圆括号内的备注（题材 / 状态 / 说明）
    pm = re.search(r"[（(](?P<tags>[^)）]{1,90})[)）]", stem)
    if pm:
        tags = split_tags(pm.group("tags"))

    # 4) 书名兜底
    if not title:
        head = re.split(r"(?:作者|著者|原著|BY|by)\s*[:：]?", stem)[0]
        head = re.sub(r"[（(][^)）]{0,90}[)）]", "", head)
        title = _clean_title(head)

    return title, author, tags


def parse_meta(
    lines: Sequence[str],
    filename: str = "",
    *,
    cfg: Optional[AdConfig] = None,
    head_window: int = 48,
) -> BookMeta:
    """解析书籍元信息；返回被消费的行号（这些行应从正文移除）。"""
    cfg = cfg or AdConfig()
    meta = BookMeta()
    fn_title, fn_author, fn_tags = _filename_meta(filename)
    meta.title = fn_title
    meta.author = fn_author
    meta.tags = list(fn_tags)
    meta.source = "filename"

    fams = default_families()
    scanned = 0
    for i, raw in enumerate(lines[:head_window]):
        s = raw.replace("\u3000", " ").strip()
        if not s:
            continue
        if len(s) > 200:
            break
        scanned += 1
        if scanned > 30:
            break

        consumed_here = False

        m = _TITLE_AUTHOR_RE.match(s)
        if m:
            t = _clean_title(m.group("title"))
            a = _clean_author(m.group("author"))
            if t and 1 <= len(t) <= 48:
                meta.title = t
                meta.source = "content"
            if a:
                meta.author = a
            consumed_here = True
        else:
            # 命中强章节标记族 -> 正文已开始，立即结束元信息扫描
            # （必须先于书名号兜底判断，否则《一》课堂风云 会被当成书名吃掉）
            if any(f.pattern.match(s) for f in fams if not f.weak):
                break
            mb = _BRACKET_TITLE_RE.match(s) or _ANGLE_TITLE_RE.match(s)
            if mb and len(s) <= 60:
                t = _clean_title(mb.group("title"))
                # 仅在尚无更可信标题时采用，避免正文里的《...》覆盖真实书名
                if t and 1 <= len(t) <= 48 and not meta.title:
                    meta.title = t
                    meta.source = "content"
                rest = mb.group("rest") or ""
                am = re.search(r"(?:作者|著者|BY|by|By)\s*[:：]?\s*(?P<a>[^（(\[【]{1,30})", rest)
                if am:
                    meta.author = _clean_author(am.group("a")) or meta.author
                tags = split_tags(rest)
                meta.tags = list(dict.fromkeys(meta.tags + tags))
                consumed_here = True

        if not consumed_here:
            ma = _AUTHOR_ONLY_RE.match(s)
            if ma and len(s) <= 44:
                a = _clean_author(ma.group("author"))
                if a:
                    meta.author = a
                consumed_here = True

        if not consumed_here:
            mt = _TAG_LINE_RE.match(s)
            if mt:
                meta.tags = list(dict.fromkeys(meta.tags + split_tags(mt.group("tags"))))
                consumed_here = True

        if not consumed_here:
            mp = _PAREN_TAG_RE.match(s)
            if mp:
                tags = split_tags(mp.group("tags"))
                if tags and len(tags) >= 2:
                    meta.tags = list(dict.fromkeys(meta.tags + tags))
                    consumed_here = True

        if not consumed_here:
            ms = _SUMMARY_RE.match(s)
            if ms:
                text = ms.group("text").strip()
                if text:
                    meta.description = (meta.description + " " + text).strip()
                consumed_here = True

        if not consumed_here and meta.title and len(s) <= 40:
            core = re.sub(r"[\s\d_\-·|｜《》<>〈〉]+", "", s)
            if core and core == re.sub(r"[\s\d_\-·|｜]+", "", meta.title):
                consumed_here = True

        if consumed_here:
            meta.consumed.append(i)
            continue

        if s in ("`", "=", "-", "·"):
            continue

        # 遇到第一批正文即停止扫描
        if len(s) >= 60:
            break
        if any(f.pattern.match(s) for f in fams if not f.weak):
            break

    site = ""
    for line in lines[:head_window]:
        found = find_site(line, cfg)
        if found:
            site = found
            break
    if site and site.lower() not in meta.title.lower():
        meta.site = site

    meta.tags = [t for t in dict.fromkeys(meta.tags) if t and t != meta.title]
    return meta
