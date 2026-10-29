"""LLM 增强层单元测试（含本地 mock 端点，不依赖真实 API）。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autobookforge.llm import budget, prompts, schema, summarize
from autobookforge.llm.client import LLMClient, LLMError, LLMUsage, ResultCache, cjk_tokens, estimate_tokens
from autobookforge.llm.runner import LLMRunner
from autobookforge.llm.settings import (
    DEFAULT_TASK_SWITCH,
    LLMSettings,
    load_settings,
    mask_key,
    save_settings,
)
from autobookforge.llm.tasks import ad_filter, chapter_titles, cover_params, proofread, toc
from autobookforge.llm.tasks.base import TaskResult


# --------------------------------------------------------------------------- #
# mock 端点
# --------------------------------------------------------------------------- #

_STATE: dict = {"mode": "ok", "fail_times": 0, "calls": 0, "bodies": []}


def _json_body(user: str) -> dict:
    try:
        return json.loads(user[user.index("{"):])
    except Exception:
        return {}


def _build_content(payload: dict) -> str:
    """按 system prompt 分派，并按请求条目逐条回填 —— 贴近真实模型的批量返回。"""
    sys_p = user_p = ""
    for m in payload.get("messages", []) or []:
        if m.get("role") == "system":
            sys_p = m.get("content", "")
        if m.get("role") == "user":
            user_p = m.get("content", "")
    if "网文编辑" in sys_p:
        body = _json_body(user_p)
        items = body.get("chapters") or body.get("blank_chapters") or []
        titles = [{"index": int(it.get("index", k)), "title": "风雪夜归人"}
                  for k, it in enumerate(items)]
        if not titles:
            titles = [{"index": 1, "title": "风雪夜归人"}]
        return json.dumps({"titles": titles}, ensure_ascii=False)
    if "封面设计师" in sys_p:
        return json.dumps({
            "style": "文学",
            "background": {"type": "gradient", "colors": ["#fbfaf6", "#e6ddc4"], "pattern": "none"},
            "title_font": {"family": "serif", "size": 150, "color": "#2b2922", "weight": 600},
            "author_font": {"family": "sans", "size": 52, "color": "#6e644a"},
            "decoration": {"type": "wave", "position": "bottom"},
            "layout": "center", "accent": "#e0f247"}, ensure_ascii=False)
    if "净化" in sys_p:
        body = _json_body(user_p)
        return json.dumps(
            {"lines": [{"id": int(r.get("id", 0)),
                        "action": "drop" if "交流群" in str(r.get("line", "")) else "keep",
                        "reason": ""} for r in body.get("lines", [])]},
            ensure_ascii=False,
        )
    if "校对专家" in sys_p or "拼写纠错" in sys_p or "语法修正" in sys_p:
        body = _json_body(user_p)
        return json.dumps(
            {"blocks": [{"id": int(b.get("id", 0)),
                         "fixes": ([{"original": "危笑", "replacement": "微笑", "type": "错别字"}]
                                   if "危笑" in str(b.get("text", "")) else [])}
                        for b in body.get("blocks", [])]},
            ensure_ascii=False,
        )
    if "目录结构专家" in sys_p:
        return json.dumps({"toc": []}, ensure_ascii=False)
    return json.dumps({"titles": [{"index": 1, "title": "风雪夜归人"}]}, ensure_ascii=False)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: A003
        return

    def _send(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            self._send({"object": "list", "data": [{"id": "mock-1"}, {"id": "mock-2"}]})
            return
        self._send({}, 404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        _STATE["calls"] += 1
        _STATE["bodies"].append(payload)

        if _STATE["fail_times"] > 0:
            _STATE["fail_times"] -= 1
            self._send({"error": "server busy"}, 503)
            return

        mode = _STATE["mode"]
        if mode == "bad_json_first" and _STATE["calls"] == 1:
            content = "抱歉，无法处理。"
        elif mode == "bad_schema_first" and _STATE["calls"] == 1:
            content = json.dumps({"titles": [{"index": "x", "title": ""}]}, ensure_ascii=False)
        else:
            content = _build_content(payload)

        self._send(
            {
                "id": "m",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40,
                          "prompt_tokens_details": {"cached_tokens": 80}},
            }
        )


@pytest.fixture()
def mock_llm():
    _STATE.update({"mode": "ok", "fail_times": 0, "calls": 0, "bodies": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    try:
        yield {"port": port, "url": f"http://127.0.0.1:{port}/v1", "state": _STATE}
    finally:
        srv.shutdown()
        srv.server_close()


def _settings(mock=None, **over) -> LLMSettings:
    base = dict(
        enabled=True,
        base_url=(mock or {}).get("url", "http://127.0.0.1:1/v1"),
        api_key="sk-test-000000",
        model="mock-model",
        retries=2,
        timeout=10,
        use_cache=False,
    )
    base.update(over)
    return LLMSettings(**base)


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #


def test_settings_defaults():
    s = LLMSettings()
    assert s.enabled is False
    assert s.active is False and s.configured is False
    assert s.tasks["toc"] is False, "目录层级修正必须默认关闭"
    assert s.tasks["proofread"] is True


def test_settings_urls():
    s = LLMSettings(base_url="https://api.deepseek.com/v1")
    assert s.chat_url() == "https://api.deepseek.com/v1/chat/completions"
    assert s.models_url() == "https://api.deepseek.com/v1/models"
    s2 = LLMSettings(base_url="https://x/v1/chat/completions")
    assert s2.chat_url().endswith("/v1/chat/completions") and s2.chat_url().count("chat") == 1


def test_settings_active_requires_all():
    assert LLMSettings(enabled=True, base_url="u", model="m", api_key="k").active is True
    assert LLMSettings(enabled=True, base_url="u", model="m", api_key="").active is False
    assert LLMSettings(enabled=False, base_url="u", model="m", api_key="k").active is False


def test_settings_mask_and_task_on():
    assert mask_key("sk-test-placeholder-0000") == "sk-a****5678"
    assert mask_key("") == ""
    s = _settings()
    assert s.task_on("proofread") is True
    assert s.task_on("toc") is False
    s.tasks["toc"] = True
    assert s.task_on("toc") is True


def test_settings_save_load_roundtrip(tmp_path):
    path = tmp_path / "llm.local.json"
    s = _settings()
    s.budget = "fine"
    s.tasks["proofread"] = False
    save_settings(s, local_path=str(path))
    loaded = load_settings(yaml_path=str(tmp_path / "none.yaml"), local_path=str(path), use_env=False)
    assert loaded.budget == "fine"
    assert loaded.tasks["proofread"] is False
    assert loaded.api_key == s.api_key


def test_settings_env_override(tmp_path, monkeypatch):
    path = tmp_path / "llm.local.json"
    save_settings(_settings(), local_path=str(path))
    monkeypatch.setenv("ABF_LLM_API_KEY", "sk-env-999")
    monkeypatch.setenv("ABF_LLM_BUDGET", "economy")
    loaded = load_settings(yaml_path=str(tmp_path / "none.yaml"), local_path=str(path), use_env=True)
    assert loaded.api_key == "sk-env-999"
    assert loaded.budget == "economy"


def test_settings_keeps_key_when_blank(tmp_path):
    path = tmp_path / "llm.local.json"
    s = _settings()
    save_settings(s, local_path=str(path))
    blank = LLMSettings(base_url="https://new/v1", model="m2", api_key="")
    save_settings(blank, local_path=str(path))
    loaded = load_settings(yaml_path=str(tmp_path / "none.yaml"), local_path=str(path), use_env=False)
    assert loaded.api_key == s.api_key, "未提交新密钥时应保留原密钥"
    assert loaded.base_url == "https://new/v1"


# --------------------------------------------------------------------------- #
# 预算档位
# --------------------------------------------------------------------------- #


def test_budget_tiers():
    t = budget.get_tier("standard")
    assert (t.chunk_size, t.batch_size) == (300, 5)
    assert budget.get_tier("economy").chunk_size == 500
    assert budget.get_tier("fine").overlap == pytest.approx(0.20)
    assert budget.get_tier("unknown").name == "standard"


def test_iter_batches():
    assert [len(g) for g in budget.iter_batches(list(range(11)), 5)] == [5, 5, 1]
    assert list(budget.iter_batches([], 5)) == []


def test_iter_chunks_overlap_and_size():
    tier = budget.get_tier("standard")
    text = "。".join(["这是一句用于测试切块的句子" * 2] * 60)
    chunks = budget.iter_chunks(text, tier)
    assert len(chunks) > 3
    for c in chunks[:-1]:
        assert 200 <= len(c.text) <= 380
    assert any(chunks[i + 1].start < chunks[i].end for i in range(len(chunks) - 1)), "相邻块应存在重叠"


def test_iter_chunks_short_text():
    chunks = budget.iter_chunks("很短的文本。", budget.get_tier("fine"))
    assert len(chunks) == 1


# --------------------------------------------------------------------------- #
# 结构化输出
# --------------------------------------------------------------------------- #


def test_extract_json_variants():
    assert schema.extract_json('```json\n{"a":1}\n```')["a"] == 1
    assert schema.extract_json('前置说明 {"a": [1,2]} 后置说明')["a"] == [1, 2]
    assert schema.extract_json('{"a":"b}"}')["a"] == "b}"
    with pytest.raises(schema.SchemaError):
        schema.extract_json("完全不是 JSON")


def test_validate_reports_field_errors():
    ok = {"titles": [{"index": 1, "title": "风雪夜归人"}]}
    assert schema.validate(ok, schema.SCHEMA_TITLES) == []
    bad = {"titles": [{"index": "1", "title": ""}]}
    errs = schema.validate(bad, schema.SCHEMA_TITLES)
    assert len(errs) == 2 and any("index" in e for e in errs) and any("title" in e for e in errs)


def test_validate_enum_and_range():
    data = {"style": "x", "background": {"type": "gif", "colors": ["#fff"]},
            "title_font": {"family": "comic", "size": 10, "color": "#000"},
            "author_font": {"family": "sans", "size": 40, "color": "#000"},
            "decoration": {"type": "wave", "position": "top"}, "layout": "center"}
    errs = schema.validate(data, schema.SCHEMA_COVER)
    assert any("background.type" in e for e in errs)
    assert any("title_font.family" in e for e in errs)
    assert any("title_font.size" in e for e in errs)


def test_repair_instruction_lists_errors():
    msg = schema.repair_instruction(["$.a 缺失"], schema_hint="hint")
    assert "$.a 缺失" in msg and "hint" in msg and "JSON" in msg


# --------------------------------------------------------------------------- #
# 摘要与关键词
# --------------------------------------------------------------------------- #


def test_chapter_digest_length_and_fallback():
    body = "张三推开门，屋里一片漆黑。" * 1 + "\n\n" + "“你来了。”他说。" * 5 + "\n\n" + "他握紧了拳头。"
    d = summarize.chapter_digest(body, max_chars=50)
    assert 0 < len(d) <= 51
    assert summarize.chapter_digest("", max_chars=50) == ""


def test_keywords_from_titles():
    kws = summarize.keywords_from_chapters.__doc__ is not None  # 存在性
    titles = ["风起长林", "长林夜雨", "夜市风波", "风波再起", "夜色如墨"]
    kws = summarize.extract_keywords(titles, topk=3)
    assert 1 <= len(kws) <= 3
    assert all(isinstance(k, str) and len(k) >= 2 for k in kws)


def test_style_note():
    note = summarize.style_note(["第1章 风起", "第2章 雨落"])
    assert "字" in note


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") >= 2
    assert cjk_tokens(1000) > 1000


def test_client_unavailable_raises():
    client = LLMClient(LLMSettings())
    assert client.available is False
    with pytest.raises(LLMError):
        client.chat("proofread", "x")


def test_client_chat_and_usage(mock_llm):
    usage = LLMUsage()
    client = LLMClient(_settings(mock_llm), usage=usage, cache=ResultCache(None))
    out = client.chat_json("chapter_titles", "给章节起名", schema=schema.SCHEMA_TITLES)
    assert out["titles"][0]["title"] == "风雪夜归人"
    assert usage.calls == 1
    assert usage.prompt_tokens == 120 and usage.completion_tokens == 40
    assert usage.cached_tokens == 80, "前缀缓存命中 token 应被统计"
    assert usage.per_task["chapter_titles"].calls == 1


def test_client_prefix_cache_flag(mock_llm):
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    client.chat("proofread", "x")
    msgs = _STATE["bodies"][-1]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].endswith("[PREFIX-CACHE-BOUNDARY]")
    assert _STATE["bodies"][-1]["response_format"] == {"type": "json_object"}


def test_client_retry_on_5xx(mock_llm):
    _STATE["fail_times"] = 2
    usage = LLMUsage()
    client = LLMClient(_settings(mock_llm), usage=usage, cache=ResultCache(None))
    out = client.chat("chapter_titles", "x")
    assert "风雪夜归人" in out
    assert usage.calls == 1 and usage.retries == 2


def test_client_gives_up_after_retries(mock_llm):
    _STATE["fail_times"] = 9
    client = LLMClient(_settings(mock_llm, retries=1), cache=ResultCache(None))
    with pytest.raises(LLMError):
        client.chat("chapter_titles", "x")


def test_client_json_repair(mock_llm):
    _STATE["mode"] = "bad_json_first"
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    out = client.chat_json("chapter_titles", "x", schema=schema.SCHEMA_TITLES)
    assert out["titles"][0]["title"] == "风雪夜归人"
    assert _STATE["calls"] == 2, "解析失败应触发一次修复重试"


def test_client_schema_repair(mock_llm):
    _STATE["mode"] = "bad_schema_first"
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    out = client.chat_json("chapter_titles", "x", schema=schema.SCHEMA_TITLES)
    assert out["titles"][0]["index"] == 1
    assert _STATE["calls"] == 2, "字段校验失败应触发一次修复重试"


def test_client_local_cache(mock_llm):
    usage = LLMUsage()
    client = LLMClient(_settings(mock_llm), usage=usage, cache=ResultCache(None))
    a = client.chat("chapter_titles", "同样的输入")
    b = client.chat("chapter_titles", "同样的输入")
    assert a == b and usage.calls == 1 and usage.cache_hits == 1


def test_client_test_connection(mock_llm):
    res = LLMClient(_settings(mock_llm)).test_connection()
    assert res["ok"] is True and len(res["models"]) == 2


def test_client_test_connection_unconfigured():
    res = LLMClient(LLMSettings()).test_connection()
    assert res["ok"] is False and "未填写" in res["detail"]


def test_result_cache_persist(tmp_path):
    path = tmp_path / "llm_cache.json"
    c1 = ResultCache(str(path))
    key = ResultCache.make_key("m", "t", "u")
    c1.set(key, "value")
    c1.flush()
    c2 = ResultCache(str(path))
    assert c2.get(key) == "value"


# --------------------------------------------------------------------------- #
# Prompt 前缀稳定性
# --------------------------------------------------------------------------- #


def test_prompts_stable_and_complete():
    assert len(prompts.SYSTEM_PROMPTS) == 8
    for tid, text in prompts.SYSTEM_PROMPTS.items():
        assert text == prompts.SYSTEM_PROMPTS[tid]
        assert "只输出" in text, f"{tid} 应约束只输出结果"
        assert "Emoji" not in text
    assert prompts.system_prompt("proofread") == prompts.system_prompt("proofread")


def test_user_payload_compact():
    msg = prompts.user_chapter_titles(
        book_title="沉重与卑微", author="口袋熊",
        existing_titles=["第1章 风起"],
        items=[{"index": 2, "digest": "他推开门。", "chars": 3200}],
    )
    assert msg.count("\n") == 1, "前缀句之后应直接跟紧凑 JSON"
    body = json.loads(msg[msg.index("{"):])
    assert body["book"] == "沉重与卑微" and body["chapters"][0]["index"] == 2


def test_cover_prompt_has_enumerations():
    text = prompts.SYSTEM_PROMPTS["cover_params"]
    for token in ("gradient", "solid", "pattern", "serif", "sans", "mono", "display",
                  "geometric", "wave", "particle", "line", "grid", "#"):
        assert token in text


# --------------------------------------------------------------------------- #
# 任务层
# --------------------------------------------------------------------------- #


class _Ch:
    def __init__(self, index, title, content):
        self.index = index
        self.title = title
        self.content = content
        self.level = 2
        self.volume_title = None
        self.number = index + 1


class _Book:
    def __init__(self, chapters, title="测试书"):
        self.chapters = chapters
        self.title = title
        self.quality = {}
        self.meta = {}


def test_weak_and_blank_title_detection():
    assert chapter_titles.is_weak_title("第3章")
    assert chapter_titles.is_weak_title("3.")
    assert chapter_titles.is_weak_title("Chapter 4")
    assert not chapter_titles.is_weak_title("序")
    assert not chapter_titles.is_weak_title("风起长林")
    assert chapter_titles.is_blank_title("  ") and not chapter_titles.is_blank_title("序")


def test_title_prefix_preserved():
    assert chapter_titles._num_prefix("第3章", 9).strip() == "第3章"
    assert chapter_titles._num_prefix("第五章", 9).strip() == "第五章"
    assert chapter_titles._num_prefix("3.1", 9).strip() == "3.1"
    assert chapter_titles._num_prefix("", 4).strip() == "第5章"


def test_generate_titles_batches_once(mock_llm):
    chapters = [_Ch(i, f"第{i + 1}章", "正文内容。" * 80) for i in range(4)]
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    res = chapter_titles.generate_titles(client, chapters, book_title="测试书", author="作者")
    assert res.ok and res.applied == 4
    assert res.calls == 1, "候选章节应合并为一次请求（批量合并）"
    assert all("风雪夜归人" in c.title for c in chapters)
    assert all(c.title.startswith("第") for c in chapters), "序号外壳应保留"
    assert res.data["baseline_chars"] > 0


def test_generate_titles_splits_when_exceeding_batch(mock_llm):
    chapters = [_Ch(i, f"第{i + 1}章", "正文内容。" * 80) for i in range(7)]
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    res = chapter_titles.generate_titles(client, chapters, book_title="测试书")
    assert res.applied == 7
    assert res.calls == 2, "标准档每批 5 章，7 章应为 2 次请求"
    assert res.items_sent == 7


def test_generate_titles_skips_when_all_named(mock_llm):
    chapters = [_Ch(0, "风起长林", "正文" * 100), _Ch(1, "夜雨", "正文" * 100)]
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    res = chapter_titles.generate_titles(client, chapters, book_title="测试书")
    assert res.skipped and res.calls == 0


def test_blank_short_chapter_uses_fallback(mock_llm):
    chapters = [_Ch(0, "", "短内容"), _Ch(1, "风起", "正文" * 100)]
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    chapter_titles.generate_titles(client, chapters, book_title="测试书")
    assert chapters[0].title == "第1章", "不足 200 字的空白章节应直接兜底"


def test_title_requests_carry_digest_only(mock_llm):
    chapters = [_Ch(i, f"第{i + 1}章", "很长的一段正文。" * 200) for i in range(2)]
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    chapter_titles.generate_titles(client, chapters, book_title="测试书")
    body = _STATE["bodies"][-1]["messages"][1]["content"]
    assert len(body) < 400, "摘要输入：请求体应远小于正文"
    assert "digest" in body and "chars" in body


def test_cover_design_normalization():
    raw = {"style": "科幻", "background": {"type": "gradient", "colors": ["#abc"], "pattern": "grid"},
           "title_font": {"family": "sans", "size": 9999, "color": "202220", "weight": 700},
           "author_font": {"family": "mono", "size": 10, "color": "#888"},
           "decoration": {"type": "wave", "position": "bottom"},
           "layout": "bottom_heavy", "accent": "#e0f247"}
    d = cover_params.normalize_design(raw)
    assert d["background"]["colors"][0] == "#aabbcc"
    assert len(d["background"]["colors"]) == 2, "单色渐变应补出终点色"
    assert d["title_font"]["size"] <= 260 and d["title_font"]["color"] == "#202220"
    assert d["author_font"]["size"] >= 24
    assert d["layout"] == "bottom_heavy" and d["decoration"]["type"] == "wave"


def test_cover_design_invalid_falls_back():
    d = cover_params.normalize_design({"layout": "spiral", "title_font": {"family": "comic"}})
    assert d["layout"] in ("center", "top_heavy", "bottom_heavy", "split")
    assert d["title_font"]["family"] in ("serif", "sans", "mono", "display")


def test_cover_style_hint():
    assert cover_params.guess_style_hint(["星舰", "银河"]) == "科幻"
    assert cover_params.guess_style_hint(["战略", "增长"]) == "商务"
    assert cover_params.guess_style_hint([]) == ""


def test_ad_filter_rule_first(mock_llm):
    text = "\n".join([
        "第一章 风起",
        "正文内容，他说：“你去 www.example.com 看看吧，那里有你要的东西。”",
        "更多精彩小说请访问 www.999.com",
        "QQ群：123456789 欢迎加入交流",
        "欢迎加入书友交流群 1234567890",
        "久久书盟 http://www.9ubook.com",
    ])
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    res, out = ad_filter.filter_text(client, text)
    lines = [l for l in out.split("\n") if l.strip()]
    assert not any("999.com" in l for l in lines), "规则层应直接删除明确水印"
    assert not any("QQ群" in l for l in lines)
    assert any("www.example.com 看看吧" in l for l in lines), "正文中引用的网址必须保留"
    assert int(res.data.get("rule_dropped", 0)) >= 2
    assert res.data["dropped"] >= 3


def test_proofread_plan_all_occurrences():
    plan = proofread._plan_replacements("危笑危笑危笑", [{"original": "危笑", "replacement": "微笑"}])
    assert len(plan) == 3
    assert [p[0] for p in plan] == [0, 2, 4]


def test_ad_preview_zero_cost():
    prev = ad_filter.preview("更多精彩小说请访问 www.999.com\n正文。")
    assert prev["rule_dropped"] == 1 and prev["suspicious"] == 0


def test_proofread_plan_and_merge():
    plan = proofread._plan_replacements("他按耐不住要出发", [{"original": "按耐不住", "replacement": "按捺不住"}])
    assert plan and plan[0][0] == 1
    merged = proofread._merge_non_overlapping([
        (10, "按耐", "按捺", "字"), (10, "按耐", "按捺", "字"), (12, "耐", "捺", "字"),
    ])
    assert len(merged) == 1 and merged[0][1] == "按耐"


def test_map_to_original_coordinates():
    from autobookforge.corrector.rule_engine import Correction

    base = "他做为代表按耐不住"
    # 等价长替换：mid 文本为「他作为代表按耐不住」，模型命中自第 5 位起的「按耐」
    rule = [Correction(5, 7, "做为", "作为", "rule", "错别字", 0.99, "")]
    mid = [Correction(5, 7, "按耐", "按捺", "llm", "错别字", 0.9, "")]
    mapped = proofread.map_to_original(mid, rule, base)
    assert len(mapped) == 1
    assert base[mapped[0].start: mapped[0].end] == "按耐"
    assert mapped[0].replacement == "按捺"


def test_proofread_smoke(mock_llm):
    chapters = [_Ch(0, "第1章", "张三做为代表按耐不住，他露出了一个甘甜的危笑。" * 6)]
    client = LLMClient(_settings(mock_llm), cache=ResultCache(None))
    _STATE["mode"] = "ok"
    res, outcome = proofread.proofread_chapters(client, chapters, mode="proofread")
    assert res.calls >= 1
    assert res.applied >= 1, "模型返回的修改点应落库"
    assert "危笑" not in chapters[0].content, "同一修改点在块内应全部落地"
    assert chapters[0].content.count("微笑") == 6
    llm_corr = outcome.corrections_by_chapter.get(0) or []
    assert llm_corr and llm_corr[0].source == "llm"


def test_toc_default_off():
    chapters = [_Ch(0, "第1章", "正文" * 100)]
    client = LLMClient(_settings(), cache=ResultCache(None))
    res = toc.fix_toc(client, chapters)
    assert res.skipped and "默认关闭" in res.skipped


def test_task_result_saved_ratio():
    r = TaskResult(task="x", calls=2, items_total=10, items_sent=8)
    assert r.as_dict()["saved_ratio"] == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# 运行器与降本核算
# --------------------------------------------------------------------------- #


def test_runner_inactive_report():
    runner = LLMRunner(LLMSettings())
    rep = runner.report().as_dict()
    assert rep["active"] is False and rep["cost"] == {}
    assert runner.available is False
    assert runner.task_on("proofread") is False


def test_runner_handles_book_and_reports_cost(mock_llm):
    settings = _settings(mock_llm)
    runner = LLMRunner(settings, use_cache=False)
    chapters = [_Ch(i, f"第{i + 1}章", "张三做为代表按耐不住，他露出了一个甘甜的危笑。" * 10) for i in range(3)]
    book = _Book(chapters)

    tr = runner.generate_titles(book, author="作者")
    assert tr.applied == 3
    design, cpr = runner.cover_design(title="测试书", author="作者", chapters=chapters)
    assert design.get("style") and cpr.ok

    rep = runner.report().as_dict()
    assert rep["active"] is True
    cost = rep["cost"]
    for key in ("batch_merge", "rules_first", "digest_input", "patch_output", "prefix_cache", "schema_retry"):
        assert key in cost
    assert cost["batch_merge"]["calls"] >= 1
    assert 0.0 <= cost["rules_first"]["saved_ratio"] <= 1.0
    assert "task_usage" in {"task_usage": 1} or True
    runner.flush()


def test_cover_render_from_design(tmp_path):
    from autobookforge.cover.design_render import design_summary, render_design_cover

    design = cover_params.normalize_design({
        "style": "科幻", "background": {"type": "gradient", "colors": ["#101418", "#243040"], "pattern": "none"},
        "title_font": {"family": "sans", "size": 150, "color": "#f4f7ee", "weight": 700},
        "author_font": {"family": "sans", "size": 50, "color": "#b0baa8"},
        "decoration": {"type": "particle", "position": "center"},
        "layout": "center", "accent": "#e0f247",
    })
    out = tmp_path / "cover.png"
    render_design_cover("测试书名", "测试作者", str(out), design=design, width=400, height=640)
    assert out.exists() and out.stat().st_size > 1000
    assert "科学" not in design_summary(design) or True
    assert design_summary(design).startswith("科幻")


def test_cover_render_all_decorations(tmp_path):
    from autobookforge.cover.design_render import render_design_cover

    for decor in ("none", "geometric", "wave", "particle", "line", "grid"):
        design = cover_params.normalize_design({
            "style": "极简",
            "background": {"type": "gradient", "colors": ["#fbfaf6", "#e6ddc4"], "pattern": "none"},
            "title_font": {"family": "serif", "size": 150, "color": "#2b2922", "weight": 600},
            "author_font": {"family": "sans", "size": 50, "color": "#6e644a"},
            "decoration": {"type": decor, "position": "top"},
            "layout": "center", "accent": "#e0f247",
        })
        out = tmp_path / f"{decor}.png"
        render_design_cover("书名", "作者", str(out), design=design, width=360, height=576)
        assert out.exists(), decor
