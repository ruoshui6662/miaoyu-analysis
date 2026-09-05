"""G0 离线回归：不访问真实 SearXNG、网页或 AI 服务。"""
from __future__ import annotations

import sys
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import json


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.environ.setdefault("MIAOYU_ADMIN_TOKEN", "test-admin-token-0123456789abcdef")

import collector  # noqa: E402
import config  # noqa: E402
import pipeline  # noqa: E402
import url_check  # noqa: E402
import hotlists  # noqa: E402
import app as app_module  # noqa: E402
import db  # noqa: E402
import evidence  # noqa: E402
import events  # noqa: E402
import alerts  # noqa: E402
import backup  # noqa: E402
import monitor_report  # noqa: E402
import monitor  # noqa: E402
import preflight  # noqa: E402
import security  # noqa: E402
from ai_client import AIClient, AIClientError  # noqa: E402
from searx_client import SearxClient  # noqa: E402
from search_providers import BraveSearchClient, SearchRouter, TavilySearchClient  # noqa: E402


def make_client():
    client = app_module.app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + os.environ["MIAOYU_ADMIN_TOKEN"]
    return client


class JsonResponseTests(unittest.TestCase):
    def test_chat_json_extracts_object_after_think_block(self):
        text = '<think>先分析素材，花括号 { 不能当作 JSON。</think>\n{"points": [{"id": "r1"}]}'
        with patch.object(AIClient, "chat", return_value=text):
            result = AIClient().chat_json([])
        self.assertEqual(result, {"points": [{"id": "r1"}]})

    def test_chat_json_extracts_fenced_object_with_surrounding_text(self):
        text = '下面是结果：\n```json\n{"intro":"含 } 字符", "timeline": []}\n```\n以上。'
        with patch.object(AIClient, "chat", return_value=text):
            result = AIClient().chat_json([])
        self.assertEqual(result["intro"], "含 } 字符")

    def test_chat_json_rejects_array_instead_of_object(self):
        with patch.object(AIClient, "chat", return_value='[{"id":"r1"}]'):
            with self.assertRaisesRegex(AIClientError, "JSON 不是对象"):
                AIClient().chat_json([])

    def test_chat_json_reports_invalid_json_without_guessing(self):
        with patch.object(AIClient, "chat", return_value='<think>思考</think>{"points": [}'):
            with self.assertRaisesRegex(AIClientError, "未返回合法 JSON"):
                AIClient().chat_json([])

    def test_chat_json_retries_once_with_strict_output_instruction(self):
        responses = iter([
            "<think>只输出思考</think>",
            '{"keywords": ["山西", "十五五"]}',
        ])
        with patch.object(AIClient, "chat", side_effect=lambda messages, **_kw: next(responses)) as call:
            result = AIClient().chat_json([{"role": "user", "content": "输出关键词"}])
        self.assertEqual(result["keywords"], ["山西", "十五五"])
        self.assertEqual(call.call_count, 2)
        self.assertIn("禁止输出 <think>", call.call_args_list[1].args[0][-1]["content"])


class RuntimeConfigTests(unittest.TestCase):
    def test_new_searx_client_reads_current_runtime_config(self):
        old_url, old_timeout = config.SEARXNG_URL, config.SEARXNG_TIMEOUT
        try:
            config.SEARXNG_URL = "http://runtime.example/"
            config.SEARXNG_TIMEOUT = 17
            client = SearxClient()
            self.assertEqual(client.base_url, "http://runtime.example")
            self.assertEqual(client.timeout, 17)
        finally:
            config.SEARXNG_URL, config.SEARXNG_TIMEOUT = old_url, old_timeout

    def test_collector_reads_current_groups_and_timeout(self):
        old_groups, old_timeout = config.ENABLED_GROUPS, config.HTTP_TIMEOUT
        try:
            config.ENABLED_GROUPS = ["wechat"]
            self.assertEqual([group["name"] for group in collector._enabled_groups()], ["wechat"])
            config.HTTP_TIMEOUT = 19

            class Response:
                status_code = 404

            with patch.object(collector.requests, "get", return_value=Response()) as get:
                self.assertEqual(collector.fetch_page("https://example.test/article"), ("", 404))
            self.assertEqual(get.call_args.kwargs["timeout"], 19)
        finally:
            config.ENABLED_GROUPS, config.HTTP_TIMEOUT = old_groups, old_timeout

    def test_system_environment_wins_over_dotenv_after_reload(self):
        """Compose 注入的内网地址不能在任务热重载时被 .env 覆盖。"""
        old_url = config.SEARXNG_URL
        old_env_url = os.environ.get("SEARXNG_URL")
        try:
            with patch.object(config, "_ENV_BASELINE", {
                "SEARXNG_URL": "http://searxng:8080",
            }), patch.object(
                config, "_load_env",
                side_effect=lambda force=False: os.environ.__setitem__(
                    "SEARXNG_URL", "https://stale.example"
                ),
            ), patch.object(config, "_load_db", return_value={}):
                config.reload()
                self.assertEqual(config.SEARXNG_URL, "http://searxng:8080")
        finally:
            config.SEARXNG_URL = old_url
            if old_env_url is None:
                os.environ.pop("SEARXNG_URL", None)
            else:
                os.environ["SEARXNG_URL"] = old_env_url


class SearchProviderTests(unittest.TestCase):
    class Response:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(f"status={self.status_code}")

        def json(self):
            return self.payload

    def test_brave_normalizes_official_web_results(self):
        client = BraveSearchClient(api_key="brave-test")
        response = self.Response({"web": {"results": [{
            "title": "测试新闻", "url": "https://example.test/a",
            "description": "摘要", "page_age": "2026-09-03T10:20:00Z",
        }]}})
        with patch.object(client.session, "get", return_value=response) as get:
            data = client.search("测试", time_range="day")
        self.assertEqual(data["results"][0]["provider"], "brave")
        self.assertEqual(data["results"][0]["title"], "测试新闻")
        self.assertEqual(get.call_args.kwargs["timeout"], config.SEARCH_TIMEOUT)
        self.assertEqual(get.call_args.kwargs["params"]["freshness"], "pd")

    def test_tavily_normalizes_results_and_uses_bearer_session_header(self):
        client = TavilySearchClient(api_key="tvly-test")
        response = self.Response({"results": [{
            "title": "Tavily 结果", "url": "https://example.test/b",
            "content": "正文摘要", "score": 0.8,
        }], "usage": {"credits": 1}})
        with patch.object(client.session, "post", return_value=response) as post:
            data = client.search("测试")
        self.assertEqual(data["results"][0]["provider"], "tavily")
        self.assertEqual(data["usage"]["credits"], 1)
        self.assertEqual(post.call_args.kwargs["json"]["search_depth"], config.TAVILY_SEARCH_DEPTH)
        self.assertTrue(client.session.headers["Authorization"].startswith("Bearer "))

    def test_router_skips_unconfigured_external_providers(self):
        old_order, old_mode = config.SEARCH_PROVIDER_ORDER, config.SEARCH_PROVIDER_MODE
        old_url = config.SEARXNG_URL
        try:
            config.SEARCH_PROVIDER_ORDER = ["searxng", "brave", "tavily"]
            config.SEARCH_PROVIDER_MODE = "failover"
            router = SearchRouter()
            with patch.object(router.providers["searxng"], "search", return_value={
                "query": "q", "results": [{"url": "https://example.test", "title": "命中"}],
                "suggestions": [], "unresponsive": [],
            }) as primary, patch.object(router.providers["brave"], "search") as brave, patch.object(
                router.providers["tavily"], "search"
            ) as tavily:
                data = router.search("q")
            primary.assert_called_once()
            brave.assert_not_called()
            tavily.assert_not_called()
            self.assertEqual(data["providers_used"], ["searxng"])
        finally:
            config.SEARCH_PROVIDER_ORDER, config.SEARCH_PROVIDER_MODE = old_order, old_mode
            config.SEARXNG_URL = old_url

    def test_router_skips_unconfigured_searxng_and_uses_configured_brave(self):
        old_order, old_mode, old_url, old_brave = (
            config.SEARCH_PROVIDER_ORDER, config.SEARCH_PROVIDER_MODE,
            config.SEARXNG_URL, config.BRAVE_API_KEY,
        )
        try:
            config.SEARCH_PROVIDER_ORDER = ["searxng", "brave", "tavily"]
            config.SEARCH_PROVIDER_MODE = "failover"
            config.SEARXNG_URL = ""
            config.BRAVE_API_KEY = "brave-test"
            router = SearchRouter()
            with patch.object(router.providers["searxng"], "search") as searxng, patch.object(
                router.providers["brave"], "search", return_value={
                    "query": "q", "results": [{"url": "https://brave.test", "title": "备用命中"}],
                }
            ) as brave:
                data = router.search("q")
            searxng.assert_not_called()
            brave.assert_called_once()
            self.assertEqual(data["providers_used"], ["brave"])
            self.assertEqual(data["results"][0]["title"], "备用命中")
        finally:
            config.SEARCH_PROVIDER_ORDER, config.SEARCH_PROVIDER_MODE = old_order, old_mode
            config.SEARXNG_URL, config.BRAVE_API_KEY = old_url, old_brave

    def test_router_returns_actionable_error_when_no_provider_is_configured(self):
        old_order, old_mode, old_url, old_brave, old_tavily = (
            config.SEARCH_PROVIDER_ORDER, config.SEARCH_PROVIDER_MODE,
            config.SEARXNG_URL, config.BRAVE_API_KEY, config.TAVILY_API_KEY,
        )
        try:
            config.SEARCH_PROVIDER_ORDER = ["searxng", "brave", "tavily"]
            config.SEARCH_PROVIDER_MODE = "failover"
            config.SEARXNG_URL = ""
            config.BRAVE_API_KEY = ""
            config.TAVILY_API_KEY = ""
            data = SearchRouter().search("q")
            self.assertEqual(data["providers_used"], [])
            self.assertIn("至少配置", data["error"])
        finally:
            config.SEARCH_PROVIDER_ORDER, config.SEARCH_PROVIDER_MODE = old_order, old_mode
            config.SEARXNG_URL = old_url
            config.BRAVE_API_KEY, config.TAVILY_API_KEY = old_brave, old_tavily

class UrlCacheTests(unittest.TestCase):
    def setUp(self):
        url_check._cache.clear()

    def test_cache_isolated_per_url_and_reuses_only_overlap(self):
        calls: list[str] = []

        def check_one(url: str) -> str:
            calls.append(url)
            return {"https://a.test": "ok", "https://b.test": "gone", "https://c.test": "unreachable"}[url]

        with patch.object(url_check, "_check_one", side_effect=check_one):
            first = url_check.check_urls(["https://a.test", "https://b.test"], workers=1)
            second = url_check.check_urls(["https://b.test", "https://c.test"], workers=1)

        self.assertEqual(first, {"https://a.test": "ok", "https://b.test": "gone"})
        self.assertEqual(second, {"https://b.test": "gone", "https://c.test": "unreachable"})
        self.assertEqual(calls, ["https://a.test", "https://b.test", "https://c.test"])


class ArtifactAndProgressTests(unittest.TestCase):
    def test_safe_filename_component_handles_illegal_and_reserved_names(self):
        safe = pipeline.safe_filename_component('G0 / : * ? < > | " 文件名')
        self.assertTrue(safe)
        self.assertFalse(any(char in safe for char in '<>:"/\\|?*'))
        self.assertEqual(pipeline.safe_filename_component("CON.txt"), "_CON.txt")
        self.assertLessEqual(len(pipeline.safe_filename_component("测" * 120)), 80)

    def test_ai_progress_uses_stage_status_not_impossible_stream_character_count(self):
        class FakeAI:
            calls = 0
            keyword_args: list[dict] = []

            def chat_json(self, _messages, **kwargs):
                type(self).calls += 1
                type(self).keyword_args.append(kwargs)
                data = [
                    {"intro": "概况", "timeline": [{"date": "2026-09-02", "event": "事件", "cross_checked": True}]},
                    {"points": [{"title": "原因", "body": "说明"}]},
                    {"points": [{"id": "r1", "title": "风险", "body": "说明"}]},
                    {"points": [{"for_id": "r1", "title": "建议", "body": "说明"}]},
                ][type(self).calls - 1]
                return data

        progress: list[tuple[str, str]] = []
        with patch.object(pipeline, "AIClient", FakeAI):
            result = pipeline._run_ai_stage(
                "离线样例", [{"title": "样例", "url": "https://example.test", "body": "正文"}], None,
                lambda step, detail: progress.append((step, detail)),
            )

        self.assertEqual(len(result["advice"]), 1)
        self.assertTrue(all("on_chunk" not in kwargs for kwargs in FakeAI.keyword_args))
        self.assertTrue(any("生成中" in detail for _step, detail in progress))
        self.assertFalse(any("已生成" in detail and "字" in detail for _step, detail in progress))

    def test_markdown_and_docx_export_with_special_topic(self):
        report = {
            "title": 'G0 / : * ? < > | 文件名',
            "intro": "离线验收样例",
            "sections": [{"heading": "事件概况",
                          "paragraphs": [{"lead": "事实：", "body": "样例内容"}]}],
            "stats": {"total_raw": 1, "total_after_dedupe": 1,
                      "body_fetched": 1,
                      "credibility_dist": {"high": 1, "mid": 0, "low": 0}},
        }
        with tempfile.TemporaryDirectory(prefix="miaoyu-g0-") as tmp:
            md = Path(tmp) / "safe.md"
            docx = Path(tmp) / "safe.docx"
            self.assertTrue(pipeline.gen_markdown(report, str(md)))
            self.assertTrue(md.exists())
            self.assertTrue(pipeline.gen_docx(report, str(docx)))
            self.assertTrue(docx.exists())
            self.assertGreater(docx.stat().st_size, 0)

    def test_reference_materials_are_shared_and_rendered_at_export_tail(self):
        items = [
            {"title": "有正文的资料", "url": "https://example.test/article",
             "source_name": "示例媒体", "published": "2026-09-03", "credibility": "high", "body": "正文"},
            {"title": "只有摘要的资料", "url": "https://example.test/search",
             "source_name": "搜索结果", "credibility": "mid", "body": ""},
        ]
        refs = pipeline._build_references(items, {"detail": {"https://example.test/article": "ok"}})
        report = {
            "title": "参考资料验收",
            "sections": [{"heading": "正文", "paragraphs": [{"lead": "结论：", "body": "内容"}]}],
            "stats": {"total_raw": 2, "total_after_dedupe": 2, "body_fetched": 1,
                      "credibility_dist": {"high": 1, "mid": 1, "low": 0}},
            "references": refs,
        }
        markdown = pipeline.render_markdown(report)
        self.assertIn("## 参考资料", markdown)
        self.assertIn("### 资料说明", markdown)
        self.assertNotIn("数据附录", markdown)
        self.assertLess(markdown.index("## 参考资料"), markdown.index("### 资料说明"))
        self.assertEqual([ref["title"] for ref in refs], ["有正文的资料", "只有摘要的资料"])


class PublicHotlistTests(unittest.TestCase):
    def setUp(self):
        hotlists._newsnow_cache.clear()
        hotlists._newsnow_health.clear()
        hotlists._aggregate_cache = None

    def test_newsnow_payload_is_normalized_and_cached(self):
        payload = {"status": "cache", "updatedTime": 123,
                   "items": [{"title": "公开热点", "url": "https://example.test/a",
                              "mobileUrl": "https://m.example.test/a", "extra": {"info": "1.2万"}}]}

        class Response:
            status_code = 200
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        with patch.object(hotlists._session, "get", return_value=Response()) as get:
            first = hotlists.fetch_newsnow_board("weibo")
            second = hotlists.fetch_newsnow_board("weibo")

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["title"], "公开热点")
        self.assertEqual(first[0]["hot"], "1.2万")
        self.assertEqual(first[0]["provider"], "newsnow")
        self.assertTrue(first[0]["captured_at"])
        self.assertEqual(second[0]["url"], "https://example.test/a")
        self.assertEqual(get.call_count, 1)

    def test_hot_boards_api_exposes_observation_and_first_seen_times(self):
        boards = [{
            "name": "微博热搜", "source_id": "weibo", "provider": "newsnow",
            "items": [{"title": "带时间的热点", "url": "https://example.test/time"}],
        }]
        with tempfile.TemporaryDirectory(prefix="miaoyu-hot-time-api-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.object(hotlists, "fetch_aggregated", return_value=boards), \
                 patch.object(hotlists, "quota_state", return_value={"provider": "newsnow"}), \
                 patch.object(hotlists, "source_health", return_value={}):
                response = make_client().get("/api/hot/boards")
        item = response.get_json()["boards"][0]["items"][0]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(item["captured_at"])
        self.assertEqual(item["first_seen_at"], item["captured_at"])

    def test_frontend_formats_hot_time_as_relative_observation_time(self):
        frontend = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function hotRelativeTime", frontend)
        self.assertIn("function hotTimeCell", frontend)
        self.assertIn("最近观测", frontend)
        self.assertIn("setInterval(() => {\n  if (HOT.boards && HOT.boards.length) renderHomeHotTable();", frontend)

    def test_heat_annotation_uses_uniform_rank_label_and_preserves_native_metric(self):
        annotated = hotlists.annotate_hot_items([
            {"title": "原生热度", "hot": "1.2万", "rank": 1},
            {"title": "榜内中段", "hot": "", "rank": 2},
            {"title": "榜内末位", "hot": "—", "rank": 3},
        ])

        self.assertEqual(annotated[0]["heat"]["basis"], "rank")
        self.assertEqual(annotated[0]["heat"]["label"], "高热")
        self.assertEqual(annotated[0]["heat"]["raw"], "1.2万")
        self.assertEqual(annotated[0]["heat"]["relative_score"], 100)
        self.assertEqual(annotated[1]["heat"]["basis"], "rank")
        self.assertEqual(annotated[1]["heat"]["label"], "中热")
        self.assertEqual(annotated[2]["heat"]["label"], "在榜")
        self.assertIn("来源未提供原生热度值", annotated[2]["heat"]["tooltip"])
        self.assertIn("来源原生热度：1.2万", annotated[0]["heat"]["tooltip"])

        fifteen = hotlists.annotate_hot_items([{"title": str(i)} for i in range(15)])
        self.assertEqual(fifteen[2]["heat"]["label"], "高热")
        self.assertEqual(fifteen[3]["heat"]["label"], "中热")
        self.assertEqual(fifteen[8]["heat"]["label"], "中热")
        self.assertEqual(fifteen[9]["heat"]["label"], "在榜")

    def test_paid_provider_stays_off_without_explicit_switch(self):
        with patch.dict(hotlists.os.environ, {"TOPHUBDATA_KEY": "test-key"}, clear=False):
            with patch.object(hotlists, "_paid_apis_enabled", return_value=False):
                with patch.object(hotlists.requests, "get") as get:
                    self.assertIsNone(hotlists._thd("/hot"))
        get.assert_not_called()

    def test_public_board_merge_keeps_fixed_six_and_normalizes_aliases(self):
        merged = hotlists._merge_public_boards(
            [{"name": "广告榜", "items": [{"title": "不要展示"}]},
             {"name": "微博", "items": [{"title": "微博条目"}], "provider": "rebang"}],
            [{"name": "知乎热榜", "items": [{"title": "知乎条目"}], "provider": "newsnow"}],
        )
        self.assertEqual([item["source_id"] for item in merged], ["weibo", "zhihu"])
        self.assertEqual([item["name"] for item in merged], ["微博热搜", "知乎热榜"])

    def test_one_public_board_failure_does_not_drop_other_boards(self):
        def board_result(source_id):
            if source_id == "bilibili":
                return []
            return [{"title": source_id, "url": "https://example.test/" + source_id}]

        with patch.object(hotlists, "fetch_newsnow_board", side_effect=board_result):
            boards = hotlists.fetch_newsnow_aggregated()

        self.assertEqual(len(boards), 5)
        self.assertNotIn("bilibili", [board["source_id"] for board in boards])
        self.assertEqual({board["source_id"] for board in boards},
                         set(hotlists.NEWSNOW_BOARDS) - {"bilibili"})


class TaskRouteTests(unittest.TestCase):
    def test_analyze_route_persists_done_state_with_ai_stub(self):
        state = {}
        finished = threading.Event()

        def fake_create(tid, topic, provider="", verify=False, created_at=""):
            state[tid] = {"id": tid, "topic": topic, "provider": provider,
                          "verify": verify, "status": "pending", "step": "",
                          "detail": "", "created_at": created_at,
                          "finished_at": "", "report_file": "",
                          "report_summary": None, "error": ""}

        def fake_update(tid, **fields):
            state[tid].update(fields)
            if fields.get("status") in {"done", "error"}:
                finished.set()

        def fake_get(tid):
            item = state.get(tid)
            if not item:
                return None
            result = dict(item)
            if isinstance(result.get("report_summary"), str):
                result["report_summary"] = json.loads(result["report_summary"])
            return result

        def fake_analysis(topic, provider=None, verify=False, progress=None):
            progress("collect", "采集完成")
            progress("facts", "事实生成中")
            return {
                "title": topic,
                "sections": [{"heading": "事件概况", "paragraphs": []}],
                "docx": "", "md": "", "json": "", "elapsed_sec": 0.01,
                "ai_ready": True,
            }

        with patch.object(app_module, "db_task_create", side_effect=fake_create), \
             patch.object(app_module, "db_task_update", side_effect=fake_update), \
             patch.object(app_module, "db_task_get", side_effect=fake_get), \
             patch.object(app_module, "run_analysis", side_effect=fake_analysis):
            client = make_client()
            response = client.post("/api/analyze", json={"topic": "离线验收主题"})
            self.assertEqual(response.status_code, 200)
            task_id = response.get_json()["task_id"]
            self.assertTrue(finished.wait(timeout=2), "后台任务未在限定时间内结束")
            detail = client.get(f"/api/tasks/{task_id}")

        self.assertEqual(detail.status_code, 200)
        payload = detail.get_json()
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["step"], "done")
        self.assertEqual(payload["report"]["title"], "离线验收主题")
        self.assertEqual(payload["report"]["sections"], 1)

    def test_history_groups_runs_and_keeps_exports_under_report(self):
        tasks = [
            {
                "id": "done-new", "topic": "开局之年看山西", "status": "done",
                "step": "done", "detail": "完成，耗时 42.0s",
                "created_at": "2026-09-03 12:00:00", "finished_at": "2026-09-03 12:00:42",
                "provider": "auto", "verify": False, "error": "",
                "report_summary": {
                    "title": "开局之年看山西舆情分析报告", "sections": 5,
                    "elapsed_sec": 42, "docx": "C:/reports/shanxi.docx",
                    "md": "C:/reports/shanxi.md",
                },
            },
            {
                "id": "failed-old", "topic": "  开局之年看山西  ", "status": "error",
                "step": "error", "detail": "任务失败: internal",
                "created_at": "2026-09-02 12:00:00", "finished_at": "2026-09-02 12:00:01",
                "provider": "auto", "verify": False,
                "error": "'gbk' codec can't encode character '\u26a0'",
                "report_summary": None,
            },
            {
                "id": "running", "topic": "另一主题", "status": "running",
                "step": "collect", "detail": "正在采集公开信源",
                "created_at": "2026-09-03 13:00:00", "finished_at": "",
                "provider": "router", "verify": True, "error": "",
                "report_summary": None,
            },
        ]
        with patch.object(app_module, "db_task_list", return_value=tasks):
            response = make_client().get("/api/history?per=8")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["total_runs"], 3)
        group = next(item for item in payload["items"] if item["topic"] == "开局之年看山西")
        self.assertEqual(group["run_count"], 2)
        self.assertEqual(group["status"], "done")
        self.assertEqual([item["label"] for item in group["latest"]["report"]["exports"]],
                         ["Word", "Markdown"])
        self.assertEqual(group["latest"]["report"]["exports"][0]["url"],
                         "/api/reports/shanxi.docx")
        self.assertNotIn("gbk", group["runs"][1]["detail"].lower())
        self.assertIn("字符兼容", group["runs"][1]["detail"])

    def test_history_filters_by_latest_status_and_search(self):
        tasks = [
            {"id": "a", "topic": "品牌发布", "status": "done", "step": "done",
             "detail": "完成", "created_at": "2026-09-03 12:00:00", "finished_at": "",
             "provider": "auto", "verify": False, "error": "", "report_summary": None},
            {"id": "b", "topic": "政策讨论", "status": "running", "step": "collect",
             "detail": "采集中", "created_at": "2026-09-03 13:00:00", "finished_at": "",
             "provider": "auto", "verify": False, "error": "", "report_summary": None},
        ]
        with patch.object(app_module, "db_task_list", return_value=tasks):
            response = make_client().get("/api/history?status=active&q=政策")

        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["topic"], "政策讨论")


class RiskNormalizationTests(unittest.TestCase):
    def test_missing_risk_ids_are_generated_and_advice_can_be_matched(self):
        class FakeAI:
            calls = 0

            def chat_json(self, _messages, **_kwargs):
                type(self).calls += 1
                return [
                    {"intro": "背景", "timeline": []},
                    {"points": [{"title": "原因", "body": "说明"}]},
                    {"points": [{"title": "风险", "body": "说明"}]},
                    {"points": [{"title": "建议", "body": "说明"}]},
                ][type(self).calls - 1]

        with patch.object(pipeline, "AIClient", FakeAI):
            result = pipeline._run_ai_stage(
                "离线主题", [{"title": "素材", "url": "https://example.test", "body": "正文"}],
                None, lambda _step, _detail: None,
            )
        self.assertEqual(result["risk_advice_check"]["risk_count"], 1)
        self.assertEqual(result["risk_advice_check"]["advice_count"], 1)
        self.assertEqual(result["advice"][0]["for_id"], "r1")
        self.assertTrue(any("未标注对应风险" in warning
                            for warning in result["risk_advice_check"]["warnings"]))


class SecurityTests(unittest.TestCase):
    def test_private_api_requires_token_and_settings_never_return_api_key(self):
        unauthenticated = app_module.app.test_client().get("/api/settings")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertNotIn("WWW-Authenticate", unauthenticated.headers)
        self.assertEqual(unauthenticated.get_json()["auth"], "Bearer token required")
        with patch.object(config, "AI_PROVIDERS", {
            "test": {"name": "测试", "endpoint": "https://example.test/v1",
                      "apiKey": "super-secret-key", "model": "test", "custom": True},
        }):
            response = make_client().get("/api/settings")
        self.assertEqual(response.status_code, 200)
        payload = response.get_data(as_text=True)
        self.assertNotIn("super-secret-key", payload)
        self.assertIn("apiKeyConfigured", payload)

        with patch.object(config, "BRAVE_API_KEY", "brave-secret"), patch.object(
            config, "TAVILY_API_KEY", "tavily-secret"
        ):
            search_settings = make_client().get("/api/settings")
        search_payload = search_settings.get_data(as_text=True)
        self.assertNotIn("brave-secret", search_payload)
        self.assertNotIn("tavily-secret", search_payload)
        self.assertTrue(search_settings.get_json()["settings"]["BRAVE_API_KEY_CONFIGURED"])
        self.assertTrue(search_settings.get_json()["settings"]["TAVILY_API_KEY_CONFIGURED"])

        edit = app_module.app.test_client().post("/api/report/edit", json={})
        self.assertEqual(edit.status_code, 401)

    def test_frontend_does_not_use_browser_prompt_for_auth(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("prompt(", frontend)
        self.assertIn("authRequired", frontend)
        self.assertIn("authGate", frontend)
        self.assertIn("authUsername", frontend)
        self.assertIn("authPassword", frontend)
        self.assertIn("panel-account", frontend)
        self.assertIn("/api/auth/password", frontend)
        self.assertIn(".auth-gate.hidden { display: none; }", frontend)
        self.assertNotIn("sessionStorage.getItem(\"miaoyu_admin_token\")", frontend)

    def test_product_logo_uses_canonical_c_mark(self):
        root = Path(app_module.ROOT)
        frontend = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        logo = root / "frontend" / "assets" / "miaoyu-logo.svg"
        self.assertTrue(logo.is_file())
        self.assertIn("assets/miaoyu-logo.svg", frontend)
        self.assertIn("brand-name-lead", frontend)
        self.assertIn("auth-brand-mark", frontend)
        self.assertIn('transform="rotate(-9 32 32)"', logo.read_text(encoding="utf-8"))
        self.assertNotIn('<span class="logo">舆</span>', frontend)
        self.assertNotIn("brand-subtitle", frontend)
        self.assertNotIn("healthDot", frontend)
        self.assertIn(".brand .logo { display: flex; width: 28px; height: 28px; }", frontend)

    def test_report_actions_share_icon_and_button_geometry(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("--icon-md: 16px", frontend)
        self.assertIn(".actions button.txt", frontend)
        self.assertIn("${IC.download}下载 Markdown", frontend)
        self.assertIn(".actions .btn-with-icon svg { width: var(--icon-md);", frontend)

    def test_pdf_export_uses_playwright_backend_renderer(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        renderer = (Path(app_module.ROOT) / "backend" / "scripts" / "render_pdf.mjs").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/report/export-pdf"', frontend)
        self.assertIn("document.fonts?.ready", renderer)
        self.assertIn("page.pdf({", renderer)
        self.assertIn("printBackground: true", renderer)
        self.assertIn(".report .sec p,.report .q-card,.report .ov-card", frontend)
        self.assertIn(".report .references", frontend)
        self.assertIn("reference-list", frontend)
        self.assertNotIn("数据附录", frontend)
        self.assertIn("break-inside:avoid;page-break-inside:avoid", frontend)
        self.assertIn("orphans:3;widows:3", frontend)
        self.assertNotIn("html2pdf()", frontend)

    def test_docker_image_installs_playwright_browser_runtime(self):
        dockerfile = (Path(app_module.ROOT) / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright", dockerfile)
        self.assertIn("npx playwright install --with-deps chromium", dockerfile)
        self.assertIn("chmod -R a+rX /ms-playwright", dockerfile)

    def test_pdf_export_endpoint_rejects_invalid_renderer_output(self):
        client = make_client()
        with patch.object(app_module.subprocess, "run") as run:
            def write_invalid_pdf(command, **_kwargs):
                Path(command[-1]).write_bytes(b"%PDF-1.7\n" + b"x" * 1500)
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run.side_effect = write_invalid_pdf
            response = client.post("/api/report/export-pdf", json={"html": "<p>test</p>"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("无效", response.get_json()["error"])

    def test_ai_action_buttons_share_height_but_keep_primary_hierarchy(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("#view-settings #panel-ai .ai-actions > button {", frontend)
        self.assertIn("height: 40px; min-height: 40px", frontend)
        self.assertIn("#view-settings #panel-ai .ai-actions > .primary { min-width: 128px;", frontend)
        self.assertIn("#view-settings #panel-ai .ai-actions > button { height: 44px; min-height: 44px; }", frontend)

    def test_password_reminder_buttons_keep_mobile_horizontal_geometry(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".auth-reminder-actions { flex-direction: row;", frontend)
        self.assertIn("#passwordReminder .auth-reminder-actions button { width: auto;", frontend)

    def test_radar_uses_board_scoped_diff_and_round_robin(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("buildRadarNewByBoard", frontend)
        self.assertIn("it.is_new === true", frontend)
        self.assertIn("hot_prev_by_board", frontend)
        self.assertIn("for (let offset = 0; fresh.length < 10; offset++)", frontend)
        self.assertNotIn('localStorage.getItem("hot_prev")', frontend)
        self.assertNotIn("fresh.sort((a, b) => hotNum(b.hot) - hotNum(a.hot));", frontend)

    def test_frontend_uses_explainable_heat_annotation(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function heatMeta", frontend)
        self.assertIn("统一榜内热度", frontend)
        self.assertNotIn("basis: \"native\"", frontend)
        self.assertIn("跨平台覆盖优先，榜内位置其次", frontend)
        self.assertNotIn(".sort((a, b) => hotNum(b.hot) - hotNum(a.hot))", frontend)

    def test_home_platform_marks_use_canonical_local_assets(self):
        frontend = (Path(app_module.ROOT) / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("HOME_PLATFORM_ASSETS", frontend)
        for asset in (
            "source-56.svg", "source-57.svg", "source-82.svg", "b.svg", "source-81.svg", "source-51.ico",
        ):
            self.assertIn(f"assets/source-logos/library/{asset}", frontend)
        self.assertIn("function homeBoardId(name, sourceId)", frontend)
        self.assertIn("homePlatformMark(source.name, meta, source.name, source.source_id)", frontend)
        self.assertIn(".home-platform-mark:not(.logo).weibo", frontend)
        self.assertIn(".home-platform-mark.logo { overflow: hidden; background: var(--surface);", frontend)
        self.assertNotIn("const HOME_BOARD_LOGOS", frontend)

    def test_auth_login_sets_cookie_and_logout_revokes_session(self):
        client = app_module.app.test_client()
        token = os.environ["MIAOYU_ADMIN_TOKEN"]
        status = client.get("/api/auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.get_json()["authenticated"])

        bad = client.post("/api/auth/login", json={"token": "12345"})
        self.assertEqual(bad.status_code, 401)
        self.assertIn("6", bad.get_json()["error"])
        good = client.post("/api/auth/login", json={"token": token})
        self.assertEqual(good.status_code, 200)
        self.assertIn("miaoyu_session=", good.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", good.headers.get("Set-Cookie", ""))
        self.assertTrue(client.get("/api/auth/status").get_json()["authenticated"])
        self.assertEqual(client.get("/api/settings").status_code, 200)

        self.assertEqual(client.post("/api/auth/logout").status_code, 200)
        self.assertFalse(client.get("/api/auth/status").get_json()["authenticated"])

    def test_account_password_login_and_change_requires_admin_username(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-admin-account-") as tmp:
            with patch.object(security, "ACCOUNT_FILE", Path(tmp) / "admin_account.json"):
                with security._SESSIONS_LOCK:
                    security._SESSIONS.clear()
                client = app_module.app.test_client()
                self.assertEqual(client.post("/api/auth/login", json={
                    "username": "operator", "password": "password",
                }).status_code, 401)
                first = client.post("/api/auth/login", json={
                    "username": "admin", "password": "password",
                })
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first.get_json()["must_change_password"])
                self.assertEqual(client.get("/api/auth/status").get_json()["username"], "admin")

                changed = client.post("/api/auth/password", json={
                    "current_password": "password",
                    "new_password": "local-pass-2026",
                    "confirm_password": "local-pass-2026",
                })
                self.assertEqual(changed.status_code, 200)
                self.assertFalse(changed.get_json()["must_change_password"])
                self.assertFalse(security.verify_admin_password("admin", "password"))
                self.assertTrue(security.verify_admin_password("admin", "local-pass-2026"))

                old_login = app_module.app.test_client().post("/api/auth/login", json={
                    "username": "admin", "password": "password",
                })
                new_login = app_module.app.test_client().post("/api/auth/login", json={
                    "username": "admin", "password": "local-pass-2026",
                })
                self.assertEqual(old_login.status_code, 401)
                self.assertEqual(new_login.status_code, 200)
                raw = (Path(tmp) / "admin_account.json").read_text(encoding="utf-8")
                self.assertNotIn("local-pass-2026", raw)
                self.assertIn("password_hash", raw)
                with security._SESSIONS_LOCK:
                    security._SESSIONS.clear()

    def test_settings_save_keeps_redacted_existing_api_key(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-security-settings-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.object(config, "AI_PROVIDERS", {
                     "test": {"name": "测试", "endpoint": "https://example.test/v1",
                               "apiKey": "super-secret-key", "model": "test", "custom": True},
                 }):
                response = make_client().post("/api/settings", json={
                    "settings": {"AI_PROVIDERS": json.dumps({
                        "test": {"name": "测试", "endpoint": "https://new.example/v1",
                                  "apiKey": "", "model": "test", "custom": True},
                    }, ensure_ascii=False)},
                })
                saved = json.loads(db.get_all()["AI_PROVIDERS"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved["test"]["apiKey"], "super-secret-key")

    def test_metrics_endpoint_is_protected_and_contains_only_counts(self):
        self.assertEqual(app_module.app.test_client().get("/api/metrics").status_code, 401)
        response = make_client().get("/api/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("database", response.get_json())
        self.assertNotIn("apiKey", response.get_data(as_text=True))

    def test_security_headers_and_fixed_window_limiter(self):
        response = app_module.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        limiter = security.FixedWindowLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("x")[0])
        self.assertTrue(limiter.allow("x")[0])
        blocked, retry = limiter.allow("x")
        self.assertFalse(blocked)
        self.assertGreaterEqual(retry, 1)

    def test_missing_environment_token_is_generated_once_and_reused(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-token-") as tmp:
            with patch.dict(os.environ, {"MIAOYU_ADMIN_TOKEN": ""}), \
                 patch.object(security, "TOKEN_FILE", Path(tmp) / "admin_token"):
                first = security.admin_token()
                second = security.admin_token()
        self.assertGreaterEqual(len(first), 24)
        self.assertEqual(first, second)

    def test_backup_restore_requires_confirmation_and_preserves_report_files(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-backup-") as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            db_path = data_dir / "settings.db"
            data_dir.mkdir()
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("CREATE TABLE sample(value TEXT)")
                conn.execute("INSERT INTO sample VALUES('kept')")
                conn.commit()
            finally:
                conn.close()
            report = data_dir / "reports" / "sample.md"
            report.parent.mkdir(parents=True)
            report.write_text("报告证据", encoding="utf-8")
            archive = backup.create_backup(root / "snapshot.zip", data_dir=data_dir, db_path=db_path)
            with self.assertRaises(ValueError):
                backup.restore_backup(archive, data_dir=root / "restored", db_path=root / "restored.db")
            restored_db = root / "restored" / "settings.db"
            backup.restore_backup(archive, data_dir=root / "restored", db_path=restored_db, confirm=True)
            restored = sqlite3.connect(str(restored_db))
            try:
                self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "kept")
            finally:
                restored.close()
            self.assertEqual((root / "restored" / "reports" / "sample.md").read_text(encoding="utf-8"), "报告证据")

    def test_deployment_preflight_reports_missing_docker_without_exposing_secrets(self):
        result = preflight.run(require_docker=False)
        self.assertTrue(result["ok"])
        self.assertTrue(any(item["name"] == "compose_files" and item["ok"]
                            for item in result["checks"]))
        self.assertNotIn("apiKey", json.dumps(result, ensure_ascii=False))


class MonitorServiceTests(unittest.TestCase):
    def _subscription(self, db_path):
        now = "2026-09-02T00:00:00+00:00"
        db.topic_create("topic-1", "监测主题", ["主题", "主题事件"], [], now)
        return db.subscription_upsert("topic-1", 600, True, now)

    def test_success_commits_cursor_and_mention(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-monitor-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                subscription_id = self._subscription(Path(tmp) / "settings.db")
                service = monitor.MonitorService(collect_fn=lambda *_args: {
                    "items": [{"title": "监测到的内容", "url": "https://example.test/a",
                                "source_name": "测试媒体"}],
                })
                result = service.run_subscription(subscription_id)
                self.assertEqual(result["status"], "success")
                self.assertTrue(db.cursor_get("topic:topic-1"))
                self.assertEqual(db.monitor_runs("topic-1")[0]["status"], "success")
                conn = sqlite3.connect(str(Path(tmp) / "settings.db"))
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM mention_topics").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_mentions").fetchone()[0], 1)
                finally:
                    conn.close()

    def test_failure_does_not_advance_cursor_and_schedules_backoff(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-monitor-fail-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                subscription_id = self._subscription(Path(tmp) / "settings.db")
                service = monitor.MonitorService(collect_fn=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("模拟来源失败")))
                result = service.run_subscription(subscription_id)
                self.assertEqual(result["status"], "error")
                self.assertEqual(db.cursor_get("topic:topic-1"), "")
                run = db.monitor_runs("topic-1")[0]
                self.assertEqual(run["status"], "error")
                self.assertEqual(run["cursor_after"], "")
                sub = db.subscription_get(subscription_id)
                self.assertEqual(sub["consecutive_failures"], 1)
                self.assertTrue(sub["cooldown_until"])

    def test_topic_api_persists_subscription_configuration(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-monitor-api-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.object(monitor.monitor_service, "start"):
                client = make_client()
                created = client.post("/api/monitor/topics", json={
                    "name": "山西舆情", "keywords": ["山西", "山西事件"],
                    "exclude_keywords": ["招聘"], "interval_seconds": 120,
                })
                self.assertEqual(created.status_code, 201)
                topic_id = created.get_json()["topic_id"]
                listed = client.get("/api/monitor/topics")

        self.assertEqual(listed.status_code, 200)
        item = listed.get_json()["items"][0]
        self.assertEqual(item["id"], topic_id)
        self.assertEqual(item["keywords"], ["山西", "山西事件"])
        self.assertEqual(item["subscription"]["interval_seconds"], 120)

    def test_topic_api_keeps_topic_and_subscription_enabled_state_in_sync(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-monitor-api-state-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.object(monitor.monitor_service, "start"):
                client = make_client()
                created = client.post("/api/monitor/topics", json={
                    "name": "暂停监测", "keywords": ["暂停监测"], "enabled": False,
                })
                self.assertEqual(created.status_code, 201)
                item = client.get("/api/monitor/topics").get_json()["items"][0]
        self.assertFalse(item["enabled"])
        self.assertFalse(item["subscription"]["enabled"])


class EvidenceModelTests(unittest.TestCase):
    def test_url_canonicalization_removes_tracking_parameters(self):
        url = evidence.canonicalize_url(
            "HTTPS://Example.TEST/story/?utm_source=x&id=7&spm=abc"
        )
        self.assertEqual(url, "https://example.test/story?id=7")


class EventAggregationTests(unittest.TestCase):
    def test_cross_platform_title_variants_share_one_explainable_event(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-events-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                now = "2026-09-02T00:00:00+00:00"
                db.topic_create("topic-event", "山西舆情", ["山西"], [], now)
                first = evidence.normalize_mention(
                    {"title": "山西煤矿事故救援进展", "url": "https://a.example/1", "hot": "1000"},
                    source_id="媒体甲", source_type="search", captured_at=now,
                    topic_id="topic-event",
                )
                first_id, _ = db.mention_upsert(first)
                db.mention_topic_touch(first_id, "topic-event", now)
                one = events.assign_mention("topic-event", first_id, first, now)

                later = "2026-09-02T01:00:00+00:00"
                second = evidence.normalize_mention(
                    {"title": "山西煤矿事故最新救援进展", "url": "https://b.example/2", "hot": "1万"},
                    source_id="媒体乙", source_type="search", captured_at=later,
                    topic_id="topic-event",
                )
                second_id, _ = db.mention_upsert(second)
                db.mention_topic_touch(second_id, "topic-event", later)
                two = events.assign_mention("topic-event", second_id, second, later)

                self.assertEqual(one["event_id"], two["event_id"])
                self.assertEqual(two["method"], "title_overlap")
                item = db.event_list("topic-event")[0]
                self.assertEqual(item["mention_count"], 2)
                self.assertEqual(item["platform_count"], 2)
                self.assertGreater(item["heat_score"], 0)
                self.assertGreater(two["heat_score"], one["heat_score"])

    def test_events_api_reads_aggregated_events(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-events-api-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.object(monitor.monitor_service, "start"):
                now = "2026-09-02T00:00:00+00:00"
                db.topic_create("topic-api-event", "测试主题", ["测试"], [], now)
                client = make_client()
                missing = client.get("/api/monitor/events?topic_id=missing")
                listed = client.get("/api/monitor/events?topic_id=topic-api-event")
                signals = client.get("/api/monitor/signals?topic_id=topic-api-event")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["items"], [])
        self.assertEqual(signals.status_code, 200)
        self.assertEqual(signals.get_json()["items"], [])

    def test_alerts_are_deterministic_and_deduplicated(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-alerts-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                now = "2026-09-02T00:00:00+00:00"
                db.topic_create("topic-alert", "告警主题", ["告警"], [], now)
                subscription_id = db.subscription_upsert("topic-alert", 600, True, now)
                items = [{
                    "title": "同一事件发生新进展", "url": f"https://p{i}.example/story",
                    "source_name": f"平台{i}", "sentiment": "negative", "hot": "1万",
                } for i in range(3)]
                service = monitor.MonitorService(collect_fn=lambda *_args: {"items": items})
                result = service.run_subscription(subscription_id)
                signals = alerts.evaluate_topic("topic-alert", "2026-09-02T00:01:00+00:00")
                persisted = db.signal_list("topic-alert")
        self.assertEqual(result["status"], "success")
        self.assertEqual({s["signal_type"] for s in signals},
                         {"cross_platform", "surge", "negative_ratio"})
        self.assertEqual(len(persisted), 3)

    def test_periodic_report_contains_window_events_signals_and_download_files(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-periodic-report-") as tmp:
            db_path = Path(tmp) / "settings.db"
            report_dir = Path(tmp) / "reports"
            with patch.object(db, "SETTINGS_DB", db_path), \
                 patch.object(monitor.monitor_service, "start"), \
                 patch.object(monitor_report, "gen_docx",
                              side_effect=lambda _report, path: (Path(path).write_bytes(b"docx") or True)):
                now = "2026-09-02T00:00:00+00:00"
                db.topic_create("topic-report", "报告主题", ["报告"], [], now)
                subscription_id = db.subscription_upsert("topic-report", 600, True, now)
                service = monitor.MonitorService(collect_fn=lambda *_args: {"items": [
                    {"title": "报告主题出现新进展", "url": "https://one.example/a",
                     "source_name": "平台甲", "sentiment": "negative"},
                    {"title": "报告主题出现新进展", "url": "https://two.example/b",
                     "source_name": "平台乙", "sentiment": "negative"},
                ]})
                self.assertEqual(service.run_subscription(subscription_id)["status"], "success")
                report = monitor_report.generate_periodic_report(
                    "topic-report", start=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    end=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), output_dir=report_dir,
                )
                saved = json.loads(Path(report["json"]).read_text(encoding="utf-8"))
                json_exists = Path(report["json"]).exists()
                md_exists = Path(report["md"]).exists()
                docx_exists = Path(report["docx"]).exists()
        self.assertEqual(saved["monitor"]["summary"]["event_count"], 1)
        self.assertEqual(saved["monitor"]["summary"]["mention_count"], 2)
        self.assertEqual(len(saved["events"][0]["mentions"]), 2)
        self.assertTrue(json_exists)
        self.assertTrue(md_exists)
        self.assertTrue(docx_exists)

    def test_hot_snapshot_is_persistent_and_mentions_are_idempotent(self):
        boards = [{
            "source_id": "weibo", "provider": "newsnow",
            "items": [{"title": "同一热点", "url": "https://example.test/a?utm_source=x",
                        "rank": 1, "hot": "100"}],
        }]
        changed = [{
            "source_id": "weibo", "provider": "newsnow",
            "items": [{"title": "同一热点", "url": "https://example.test/a?utm_source=x",
                        "rank": 3, "hot": "200"}],
        }]
        with tempfile.TemporaryDirectory(prefix="miaoyu-evidence-") as tmp:
            db_path = Path(tmp) / "settings.db"
            with patch.object(db, "SETTINGS_DB", db_path):
                first = evidence.record_hot_boards(boards, captured_at="2026-09-02T00:00:00+00:00")
                second = evidence.record_hot_boards(changed, captured_at="2026-09-02T00:05:00+00:00")
                repeated = evidence.record_hot_boards(changed, captured_at="2026-09-02T00:10:00+00:00")
                rank_data = db.hot_rank_changes("weibo")
                history = db.hot_history("weibo")
                conn = sqlite3.connect(str(db_path))
                try:
                    counts = {
                        "runs": conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0],
                        "mentions": conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0],
                        "hot_items": conn.execute("SELECT COUNT(*) FROM hot_items").fetchone()[0],
                    }
                finally:
                    conn.close()

        self.assertEqual(first["mentions_new"], 1)
        self.assertEqual(second["mentions_new"], 0)
        self.assertEqual(repeated["items"], 0)
        self.assertEqual(counts, {"runs": 2, "mentions": 1, "hot_items": 2})
        self.assertEqual(rank_data["items"][next(iter(rank_data["items"]))]["rank_change"], -2)
        item = rank_data["items"][next(iter(rank_data["items"]))]
        self.assertEqual(item["captured_at"], "2026-09-02T00:05:00+00:00")
        self.assertEqual(item["first_seen_at"], "2026-09-02T00:00:00+00:00")
        self.assertEqual(len(history), 2)

    def test_hot_history_api_reads_persisted_snapshot_without_fetching(self):
        boards = [{"source_id": "zhihu", "provider": "newsnow",
                   "items": [{"title": "历史条目", "url": "https://example.test/h"}]}]
        with tempfile.TemporaryDirectory(prefix="miaoyu-evidence-api-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                evidence.record_hot_boards(boards, captured_at=datetime.now(timezone.utc).isoformat())
                response = make_client().get(
                    "/api/hot/history?board_id=zhihu&hours=24"
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["title"], "历史条目")

    def test_ui2_container_hierarchy_flattens_secondary_sections(self):
        frontend = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/* UI-3：冷白底 + 白色内容区 + 极弱分隔。", frontend)
        self.assertIn("--bg-page: #f7f8fa", frontend)
        self.assertIn(".home-focus-metric { min-width: 0; padding: 0; }", frontend)
        self.assertIn(".home-hot-table thead { border-bottom: 1px solid var(--separator); }", frontend)
        self.assertIn(".home-hot-table td { padding: 14px 10px; border: 0;", frontend)
        self.assertIn("#view-history .history-group { border: 0;", frontend)
        self.assertIn("#view-settings #panel-groups .src-item + .src-item { border-top: 0;", frontend)

    def test_home_actions_have_distinct_visual_and_semantic_roles(self):
        frontend = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="primary" onclick="startHome()">开始研判</button>', frontend)
        self.assertIn('class="home-focus-action" ${analyzeAction}>分析此热点 →</button>', frontend)
        self.assertNotIn('class="home-link-button" ${analyzeAction}>查看完整分析 →</button>', frontend)
        self.assertNotIn('aria-label="要点速览"', frontend)


if __name__ == "__main__":
    unittest.main(verbosity=2)
