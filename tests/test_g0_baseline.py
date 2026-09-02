"""G0 离线回归：不访问真实 SearXNG、网页或 AI 服务。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import json


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import collector  # noqa: E402
import config  # noqa: E402
import pipeline  # noqa: E402
import url_check  # noqa: E402
import hotlists  # noqa: E402
from searx_client import SearxClient  # noqa: E402


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
        self.assertEqual(second[0]["url"], "https://example.test/a")
        self.assertEqual(get.call_count, 1)

    def test_paid_provider_stays_off_without_explicit_switch(self):
        with patch.dict(hotlists.os.environ, {"TOPHUBDATA_KEY": "test-key"}, clear=False):
            with patch.object(hotlists, "_paid_apis_enabled", return_value=False):
                with patch.object(hotlists.requests, "get") as get:
                    self.assertIsNone(hotlists._thd("/hot"))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
