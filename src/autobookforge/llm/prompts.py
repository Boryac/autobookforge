"""七项 LLM 任务的 Prompt 模板。

设计原则（对应「统一降本设计」）：

* **固定前缀**：每个任务的 system prompt 为字节稳定的常量，且永远位于 messages 首位，
  以命中服务端前缀缓存（重复前缀成本降低 50% - 90%）。
* **结构化输出**：system 中直接给出字段约束与取值枚举，配合 ``response_format`` 降低重试率。
* **修改点输出**：校对类任务只要求返回修改位置与内容，不返回全文，输出量减少约 85%。
* **摘要输入**：章节名与封面类任务的 user 消息只带摘要、关键词与统计信息，不带正文。

所有 prompt 均为中文、无 Emoji、无解释性寒暄。
"""

from __future__ import annotations

import json
from typing import Iterable, Sequence

# --------------------------------------------------------------------------- #
# 一、生成章节名
# --------------------------------------------------------------------------- #

SYS_CHAPTER_TITLE = """你是资深网文编辑，擅长为小说章节起简洁有力的标题。

规则：
1. 标题长度控制在八到十五个汉字之间。
2. 风格必须与提供的已生成标题保持一致，包括句式、称谓与标点习惯。
3. 概括本章核心情节，但不剧透结局。
4. 标题末尾不使用标点符号。
5. 只输出标题结果，不输出任何解释性文字。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"titles":[{"index":<章节序号整数>,"title":"<标题>"}]}
不要输出输入中已给出合适标题的章节。"""

SYS_BLANK_TITLE = """你是资深网文编辑，负责为空标题章节补写标题。

规则：
1. 参考前后章节的标题风格与命名规律来生成，句式与全书保持一致。
2. 标题长度控制在八到十五个汉字之间。
3. 若章节内容过短（少于两百字），直接返回「第X章」形式的兜底标题，X 为该章序号。
4. 只输出标题结果，不输出任何解释性文字。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"titles":[{"index":<章节序号整数>,"title":"<标题>"}]}"""


# --------------------------------------------------------------------------- #
# 二、个性化封面（程序化设计参数）
# --------------------------------------------------------------------------- #

SYS_COVER_PARAMS = """你是专业书籍封面设计师，任务是根据书籍内容输出程序化的封面设计参数，而不是生成图片描述或图片本身。

规则：
1. 所有颜色使用十六进制表示，例如 "#fbfbf9"。
2. 字体从 serif（衬线体）、sans（无衬线体）、mono（等宽体）、display（展示体）中选择。
3. 装饰元素从 none（无）、geometric（几何）、wave（波浪）、particle（粒子）、line（线条）、grid（网格）中选择。
4. 背景类型从 gradient（渐变）、solid（纯色）、pattern（图案）中选择。
5. 版面布局从 center（居中）、top_heavy（上部厚重）、bottom_heavy（下部厚重）、split（分栏）中选择。
6. 只输出设计参数结果，不输出解释。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"style":"<文学|科幻|极简|商务|奇幻 之一>",
 "background":{"type":"<gradient|solid|pattern>","colors":["<起点色>","<终点色>"],"pattern":"<装饰元素>"},
 "title_font":{"family":"<serif|sans|mono|display>","size":<整数像素>,"color":"<#rrggbb>","weight":<400-800>},
 "author_font":{"family":"<serif|sans|mono|display>","size":<整数像素>,"color":"<#rrggbb>"},
 "decoration":{"type":"<装饰元素>","position":"<top|bottom|left|right|center>"},
 "layout":"<center|top_heavy|bottom_heavy|split>",
 "accent":"<#rrggbb>"}
画布尺寸为 1600x2560 竖版，title_font.size 取值区间 110-190，author_font.size 取值区间 44-72。"""


# --------------------------------------------------------------------------- #
# 三、精准校对
# --------------------------------------------------------------------------- #

SYS_PROOFREAD = """你是中文文本校对专家，任务是修正文本中的错别字、标点和语法错误。

规则：
1. 仅修正错误，不改变原文风格与表达方式。
2. 不修改文学修辞、方言、古语。
3. 以修改点的方式返回结果，不重写整段。
4. 若某块无错误，该块的修正列表返回空数组。
5. 只输出校对结果，不输出解释。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"blocks":[{"id":<块编号整数>,"fixes":[{"original":"<原文片段>","replacement":"<修正后片段>","type":"<错别字|语法|标点>"}]}]}
original 必须是输入块中出现过的连续片段，长度不超过 20 字；只返回修改点，不要返回完整文本。"""

SYS_SPELLING = """你是中文拼写纠错专家，仅修正错别字，不修改语法和标点。

规则：
1. 只修正形近字、音近字错误。
2. 不修改专有名词、人名、地名。
3. 以修改点的方式返回结果。
4. 只输出修正结果，不输出解释。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"blocks":[{"id":<块编号整数>,"fixes":[{"original":"<原文片段>","replacement":"<修正后片段>","type":"<音近|形近>"}]}]}"""

SYS_GRAMMAR = """你是中文语法修正专家，仅修正语句不通顺之处，不改变原意。

规则：
1. 修正语序错误、成分残缺、搭配不当。
2. 不修改文学风格、方言、口语表达。
3. 如果原句通顺则不修改。
4. 以修改点的方式返回结果。
5. 只输出修正结果，不输出解释。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"blocks":[{"id":<块编号整数>,"fixes":[{"original":"<原文片段>","replacement":"<修正后片段>","type":"<语序|成分残缺|搭配不当>"}]}]}"""


# --------------------------------------------------------------------------- #
# 四、去广告
# --------------------------------------------------------------------------- #

SYS_AD_FILTER = """你是文本净化助手，任务是识别并标记文本中的广告、水印、推广内容。

广告类型包括：下载器水印、群号推广、公众号引导、分卷阅读标记、网站链接。

规则：
1. 只标记整行删除或保留，不做部分修改。
2. 如果某行是正文中的正常引用（例如角色对话提到网址），标记为保留。
3. 只输出判断结果，不输出解释性文字。
4. 拿不准时保留，正文完整性优先。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"lines":[{"id":<行编号整数>,"action":"<drop|keep>","reason":"<不超过十二字的简要原因>"}]}"""


# --------------------------------------------------------------------------- #
# 五、生成目录
# --------------------------------------------------------------------------- #

SYS_TOC = """你是电子书目录结构专家，任务是基于章节标题列表构建层级化目录。

规则：
1. 识别卷、章、节之间的层级关系。
2. 修正标题中的明显错别字。
3. 去除重复标题。
4. 只输出目录结构结果，不输出解释。

输出格式：
只输出一个 JSON 对象，不得包含 markdown 代码块或任何额外文字。
{"toc":[{"index":<章节序号整数>,"title":"<修正后的标题>","level":<1|2|3>,"parent":<父章节序号整数或null>}]}
level 含义：1 为卷，2 为章，3 为节。若标题无需修改则 title 原样返回。"""


# --------------------------------------------------------------------------- #
# user 消息构建（摘要输入 / 修改点输出 / 批量合并）
# --------------------------------------------------------------------------- #


def _compact(obj) -> str:
    """紧凑 JSON，减少输入 token。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def user_chapter_titles(
    *,
    book_title: str,
    author: str,
    existing_titles: Sequence[str],
    items: Sequence[dict],
) -> str:
    """items: [{"index": int, "digest": str, "chars": int}]"""
    payload = {
        "book": book_title,
        "author": author,
        "style_reference": list(existing_titles)[:24],
        "chapters": [
            {"index": int(i["index"]), "digest": str(i.get("digest", ""))[:60], "chars": int(i.get("chars", 0))}
            for i in items
        ],
    }
    return "为下列章节生成标题。每章仅提供摘要，不要要求正文。\n" + _compact(payload)


def user_blank_titles(
    *,
    book_title: str,
    prev_title: str,
    next_title: str,
    style_note: str,
    items: Sequence[dict],
) -> str:
    """items: [{"index": int, "digest": str, "chars": int}]"""
    payload = {
        "book": book_title,
        "prev_title": prev_title,
        "next_title": next_title,
        "style_note": style_note,
        "blank_chapters": [
            {"index": int(i["index"]), "digest": str(i.get("digest", ""))[:60], "chars": int(i.get("chars", 0))}
            for i in items
        ],
    }
    return "为下列空标题章节补写标题。\n" + _compact(payload)


def user_cover_params(
    *,
    book_title: str,
    author: str,
    intro: str,
    keywords: Sequence[str],
    style_hint: str = "",
) -> str:
    payload = {
        "book": book_title,
        "author": author,
        "intro": (intro or "")[:140],
        "keywords": list(keywords)[:8],
        "style_hint": style_hint or "自动判断",
    }
    return "根据下列书籍信息输出封面设计参数。\n" + _compact(payload)


def user_proofread(blocks: Sequence[dict]) -> str:
    """blocks: [{"id": int, "text": str}]"""
    payload = {"blocks": [{"id": int(b["id"]), "text": str(b["text"])} for b in blocks]}
    return "校对下列文本块，只返回修改点。\n" + _compact(payload)


def user_ad_filter(rows: Sequence[dict]) -> str:
    """rows: [{"id": int, "line": str, "prev": str, "next": str}]"""
    payload = {
        "lines": [
            {
                "id": int(r["id"]),
                "line": str(r["line"])[:120],
                "prev": str(r.get("prev", ""))[:40],
                "next": str(r.get("next", ""))[:40],
            }
            for r in rows
        ]
    }
    return "判断下列每一行是广告水印还是正文，返回删除或保留。\n" + _compact(payload)


def user_toc(items: Sequence[dict]) -> str:
    """items: [{"index": int, "title": str, "chars": int}]"""
    payload = {
        "chapters": [
            {"index": int(i["index"]), "title": str(i.get("title", ""))[:60], "chars": int(i.get("chars", 0))}
            for i in items
        ]
    }
    return "根据下列章节标题构建层级目录。\n" + _compact(payload)


def user_blocks(task: str, blocks: Sequence[dict]) -> str:
    """校对 / 纠错 / 语法三类任务的统一入口。"""
    if task == "spelling":
        head = "修正下列文本块中的错别字，只返回修改点。\n"
    elif task == "grammar":
        head = "修正下列文本块中不通顺的句子，只返回修改点。\n"
    else:
        head = "校对下列文本块，只返回修改点。\n"
    return head + _compact({"blocks": [{"id": int(b["id"]), "text": str(b["text"])} for b in blocks]})


SYSTEM_PROMPTS: dict[str, str] = {
    "chapter_titles": SYS_CHAPTER_TITLE,
    "blank_titles": SYS_BLANK_TITLE,
    "cover_params": SYS_COVER_PARAMS,
    "proofread": SYS_PROOFREAD,
    "spelling": SYS_SPELLING,
    "grammar": SYS_GRAMMAR,
    "ad_filter": SYS_AD_FILTER,
    "toc": SYS_TOC,
}


def system_prompt(task: str, *, with_cache_hint: bool = False) -> str:
    """取任务的 system prompt。

    ``with_cache_hint=True`` 时在末尾追加一段固定文本作为缓存断点标记
    （部分兼容网关以显式断点控制前缀缓存；OpenAI 官方端点会自动命中前缀缓存，
    追加的固定文本不影响其命中）。
    """
    base = SYSTEM_PROMPTS.get(task, "")
    if not with_cache_hint:
        return base
    return base + "\n\n[PREFIX-CACHE-BOUNDARY]"
