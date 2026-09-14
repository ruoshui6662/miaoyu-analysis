import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import db
import radar_sources
from radar_sources import RadarFeedError, _check_resolved_target, fetch_feed, parse_feed, validate_endpoint_url
from radar import RadarService, _retry_seconds


RSS_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>示例媒体</title>
<item><title>小米汽车发布新消息</title><link>https://example.test/a?utm_source=x</link>
<guid>a-1</guid><pubDate>Sat, 05 Sep 2026 09:00:00 GMT</pubDate><description>摘要内容</description></item>
</channel></rss>'''.encode("utf-8")

ATOM_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom 示例</title>
<entry><title>公开更新</title><id>tag:example.test,2026:item-1</id>
<link href="https://example.test/atom-1"/><updated>2026-09-05T09:00:00Z</updated>
<summary>Atom 摘要</summary></entry></feed>'''.encode("utf-8")


class _Response:
    def __init__(self, status_code=200, body=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


class _LocalFeedServer:
    """让 RSS 测试走真实 requests/HTTP 边界，不访问互联网。"""

    def __init__(self, responses: list[tuple[int, bytes, dict[str, str]]]):
        self._responses = list(responses)
        self.requests: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                fixture.requests.append({key: value for key, value in self.headers.items()})
                if not fixture._responses:
                    self.send_response(500)
                    self.end_headers()
                    return
                status, body, headers = fixture._responses.pop(0)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}/feed.xml"

    def __exit__(self, _exc_type, _exc, _traceback):
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=5)


class RadarSourceTests(unittest.TestCase):
    def test_parse_rss_and_atom(self):
        rss = parse_feed(RSS_XML)
        atom = parse_feed(ATOM_XML)
        self.assertEqual(rss["feed_title"], "示例媒体")
        self.assertEqual(rss["items"][0]["url"], "https://example.test/a?utm_source=x")
        self.assertEqual(atom["feed_title"], "Atom 示例")
        self.assertEqual(atom["items"][0]["url"], "https://example.test/atom-1")

    def test_fetch_uses_conditional_headers_and_handles_304(self):
        endpoint = {"url": "https://example.test/feed.xml"}
        state = {"etag": '"v1"', "last_modified": "Sat, 05 Sep 2026 08:00:00 GMT"}
        with patch("radar_sources._check_resolved_target"), patch(
            "radar_sources.requests.get", return_value=_Response(
                304, headers={"ETag": '"v1"'})) as request:
            result = fetch_feed(endpoint, state)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["http_status"], 304)
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["If-None-Match"], '"v1"')
        self.assertEqual(headers["If-Modified-Since"], state["last_modified"])

    def test_fetch_cursor_filters_seen_ids_even_when_feed_order_changes(self):
        payload_one = '''<rss version="2.0"><channel><title>示例</title>
        <item><title>旧条目</title><link>https://example.test/old</link><guid>old-1</guid></item>
        <item><title>中间条目</title><link>https://example.test/mid</link><guid>mid-1</guid></item>
        </channel></rss>'''.encode("utf-8")
        payload_two = '''<rss version="2.0"><channel><title>示例</title>
        <item><title>新条目</title><link>https://example.test/new</link><guid>new-1</guid></item>
        <item><title>旧条目</title><link>https://example.test/old</link><guid>old-1</guid></item>
        <item><title>中间条目</title><link>https://example.test/mid</link><guid>mid-1</guid></item>
        </channel></rss>'''.encode("utf-8")
        endpoint = {"url": "https://example.test/feed.xml"}
        with patch("radar_sources._check_resolved_target"), patch(
                "radar_sources.requests.get", side_effect=[
                    _Response(200, payload_one, {"ETag": '"v1"'}),
                    _Response(200, payload_two, {"ETag": '"v2"'}),
                ]):
            first = fetch_feed(endpoint)
            second = fetch_feed(endpoint, {"cursor_value": first["cursor_after"]})
        self.assertEqual([item["external_id"] for item in first["items"]], ["old-1", "mid-1"])
        self.assertEqual([item["external_id"] for item in second["items"]], ["new-1"])
        self.assertTrue(first["cursor_after"].startswith('{"version":1'))

    def test_real_http_invalid_xml_keeps_cursor_and_records_response_status(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-real-http-invalid-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.dict(os.environ, {"MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES": "1"}, clear=False), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 _LocalFeedServer([(200, b"<rss>", {
                     "Content-Type": "application/rss+xml", "ETag": '"broken-v1"',
                 })]) as server:
                source_id = db.radar_source_identity_get_or_create("本地验收源", "127.0.0.1")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", server.url)
                now = datetime.now(timezone.utc).isoformat()
                db.topic_create("radar-real-http-invalid", "本地验收", ["验收"], [], now,
                                kind="radar", source_scope=["L1"])
                db.radar_topic_endpoint_bind("radar-real-http-invalid", endpoint_id)
                db.radar_endpoint_state_upsert(
                    endpoint_id, status="healthy", checked_at=now, etag='"old"',
                    cursor_value="stable-cursor", last_success_at=now,
                )
                sub_id = db.subscription_upsert("radar-real-http-invalid", 900, True, now)

                result = RadarService()._collect_for_subscriptions([db.subscription_get(sub_id)])
                state = db.radar_endpoint_state(endpoint_id)

                self.assertEqual(result[0]["status"], "partial")
                self.assertEqual(state["cursor_value"], "stable-cursor")
                self.assertEqual(state["consecutive_failures"], 1)
                self.assertEqual(state["last_http_status"], 200)
                self.assertTrue(state["cooldown_until"])
                self.assertEqual(len(server.requests), 1)

    def test_real_http_feed_keeps_cursor_through_304_after_service_restart(self):
        last_modified = "Sat, 05 Sep 2026 09:00:00 GMT"
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-real-http-304-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.dict(os.environ, {"MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES": "1"}, clear=False), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 _LocalFeedServer([
                     (200, RSS_XML, {
                         "Content-Type": "application/rss+xml", "ETag": '"v1"',
                         "Last-Modified": last_modified,
                     }),
                     (304, b"", {"ETag": '"v1"', "Last-Modified": last_modified}),
                 ]) as server:
                source_id = db.radar_source_identity_get_or_create("本地重启源", "127.0.0.1")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", server.url)
                now = datetime.now(timezone.utc).isoformat()
                db.topic_create("radar-real-http-304", "小米", ["小米"], [], now,
                                kind="radar", source_scope=["L1"])
                db.radar_topic_endpoint_bind("radar-real-http-304", endpoint_id)
                sub_id = db.subscription_upsert("radar-real-http-304", 900, True, now)

                first = RadarService()._collect_for_subscriptions([db.subscription_get(sub_id)])
                before_restart = db.radar_endpoint_state(endpoint_id)
                db.radar_endpoint_state_upsert(
                    endpoint_id, status=before_restart["status"],
                    checked_at=before_restart["last_checked_at"], etag=before_restart["etag"],
                    last_modified=before_restart["last_modified"],
                    cursor_value=before_restart["cursor_value"],
                    last_success_at=before_restart["last_success_at"], next_fetch_at="",
                    consecutive_failures=before_restart["consecutive_failures"],
                    cooldown_until=before_restart["cooldown_until"],
                    average_update_seconds=before_restart["average_update_seconds"],
                    last_http_status=before_restart["last_http_status"],
                    item_count=before_restart["item_count"], error_message=before_restart["error_message"],
                )

                second = RadarService()._collect_for_subscriptions([db.subscription_get(sub_id)])
                after_restart = db.radar_endpoint_state(endpoint_id)

                self.assertEqual(first[0]["result_code"], "matched")
                self.assertEqual(second[0]["result_code"], "no_match")
                self.assertEqual(db.radar_stats("radar-real-http-304")["total"], 1)
                self.assertEqual(after_restart["status"], "unchanged")
                self.assertEqual(after_restart["cursor_value"], before_restart["cursor_value"])
                self.assertEqual(server.requests[1]["If-None-Match"], '"v1"')
                self.assertEqual(server.requests[1]["If-Modified-Since"], last_modified)

    def test_real_http_503_keeps_cursor_and_enters_backoff(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-real-http-503-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch.dict(os.environ, {"MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES": "1"}, clear=False), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 _LocalFeedServer([(503, b"temporarily unavailable", {
                     "Content-Type": "text/plain",
                 })]) as server:
                source_id = db.radar_source_identity_get_or_create("本地退避源", "127.0.0.1")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", server.url)
                now = datetime.now(timezone.utc).isoformat()
                db.topic_create("radar-real-http-503", "退避", ["退避"], [], now,
                                kind="radar", source_scope=["L1"])
                db.radar_topic_endpoint_bind("radar-real-http-503", endpoint_id)
                db.radar_endpoint_state_upsert(
                    endpoint_id, status="healthy", checked_at=now, cursor_value="stable-cursor",
                    last_success_at=now,
                )
                sub_id = db.subscription_upsert("radar-real-http-503", 900, True, now)

                result = RadarService()._collect_for_subscriptions([db.subscription_get(sub_id)])
                state = db.radar_endpoint_state(endpoint_id)

                self.assertEqual(result[0]["status"], "partial")
                self.assertEqual(state["cursor_value"], "stable-cursor")
                self.assertEqual(state["consecutive_failures"], 1)
                self.assertEqual(state["last_http_status"], 503)
                self.assertEqual(state["status"], "degraded")
                self.assertTrue(state["cooldown_until"])

    def test_backoff_schedule_has_documented_caps(self):
        with patch("radar.random.uniform", return_value=0):
            self.assertEqual([_retry_seconds(i) for i in range(1, 7)],
                             [30, 60, 120, 300, 900, 900])

    def test_endpoint_identity_binding_and_state_are_persistent(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-source-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                source_id = db.radar_source_identity_get_or_create("示例媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/feed.xml")
                db.topic_create("radar-source-1", "小米", ["小米"], [], "2026-09-05T09:00:00+00:00", kind="radar")
                self.assertTrue(db.radar_topic_endpoint_bind("radar-source-1", endpoint_id))
                self.assertEqual(db.radar_topic_endpoint_ids(["radar-source-1"]), {endpoint_id})
                db.radar_endpoint_state_upsert(
                    endpoint_id, status="healthy", checked_at="2026-09-05T09:00:00+00:00",
                    etag='"v1"', last_modified="Sat, 05 Sep 2026 09:00:00 GMT",
                    cursor_value="a-1", last_success_at="2026-09-05T09:00:00+00:00",
                    next_fetch_at="2026-09-05T09:15:00+00:00", item_count=1,
                )
                self.assertEqual(db.radar_endpoint_state(endpoint_id)["etag"], '"v1"')
                self.assertEqual(db.radar_endpoints()[0]["source_name"], "示例媒体")

    def test_endpoint_lease_prevents_duplicate_workers_and_can_expire(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-lease-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                source_id = db.radar_source_identity_get_or_create("租约媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/lease.xml")
                now = "2026-09-07T09:00:00+00:00"
                future = "2026-09-07T09:02:00+00:00"
                self.assertTrue(db.radar_endpoint_lease_acquire(endpoint_id, "worker-a", now, future))
                self.assertFalse(db.radar_endpoint_lease_acquire(endpoint_id, "worker-b", now, future))
                self.assertEqual(db.radar_endpoint_state(endpoint_id)["lease_owner"], "worker-a")
                self.assertTrue(db.radar_endpoint_lease_release(endpoint_id, "worker-a", now))
                self.assertTrue(db.radar_endpoint_lease_acquire(endpoint_id, "worker-b", now, future))
                self.assertFalse(db.radar_endpoint_lease_release(endpoint_id, "worker-a", now))
                self.assertTrue(db.radar_endpoint_lease_acquire(
                    endpoint_id, "worker-c", "2026-09-07T10:00:00+00:00", future,
                ))

    def test_invalid_or_unsafe_feed_is_rejected(self):
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("ftp://example.test/feed.xml")
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("https://user:password@example.test/feed.xml")
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("http://127.0.0.1/feed.xml")
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("http://198.18.0.204/feed.xml")

    @patch("radar_sources.socket.getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.204", 0)),
    ])
    def test_fake_ip_proxy_result_is_allowed_for_hostname(self, _getaddrinfo):
        _check_resolved_target("https://example.test/feed.xml")

    @patch("radar_sources.socket.getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0)),
    ])
    def test_private_dns_result_is_still_rejected(self, _getaddrinfo):
        with self.assertRaises(RadarFeedError):
            _check_resolved_target("https://example.test/feed.xml")

    def test_private_allowlist_accepts_only_matching_cidr(self):
        env = {
            "MIAOYU_RADAR_PRIVATE_SOURCE_ALLOWLIST": "10.0.0.0/24",
            "MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES": "",
        }
        with patch.dict(os.environ, env, clear=False):
            try:
                allowed_url = validate_endpoint_url("http://10.0.0.9/feed.xml")
            except RadarFeedError:
                allowed_url = ""
            self.assertEqual(allowed_url, "http://10.0.0.9/feed.xml")
            with self.assertRaisesRegex(RadarFeedError, "内网"):
                validate_endpoint_url("http://10.0.1.9/feed.xml")
            allowed = getattr(radar_sources, "_private_target_allowed", lambda *_args: False)
            self.assertTrue(allowed("10.0.0.9", ["10.0.0.9"]))
            self.assertFalse(allowed("10.0.1.9", ["10.0.1.9"]))

    def test_fetch_revalidates_redirect_before_requesting_it(self):
        endpoint = {"url": "https://public.example/feed.xml"}
        redirect = _Response(302, headers={"Location": "http://127.0.0.1/private.xml"})
        with patch("radar_sources._check_resolved_target", side_effect=[
            None, RadarFeedError("private_address_blocked", "重定向目标被拒绝"),
        ]), patch("radar_sources.requests.get", return_value=redirect) as get:
            with self.assertRaises(RadarFeedError) as raised:
                fetch_feed(endpoint)
        self.assertEqual(raised.exception.code, "private_address_blocked")
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs.get("allow_redirects", True))

    def test_fetch_limits_redirect_chain_to_three_hops(self):
        endpoint = {"url": "https://public.example/feed.xml"}
        redirects = [_Response(302, headers={"Location": "/next.xml"}) for _ in range(4)]
        with patch("radar_sources._check_resolved_target"), patch(
            "radar_sources.requests.get", side_effect=redirects) as get:
            with self.assertRaises(RadarFeedError) as raised:
                fetch_feed(endpoint)
        self.assertEqual(raised.exception.code, "redirect_limit")
        self.assertEqual(get.call_count, 4)
        self.assertTrue(all(
            call.kwargs.get("allow_redirects") is False for call in get.call_args_list
        ))

    def test_radar_service_ingests_bound_feed_once(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-feed-run-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 patch("radar.fetch_feed", return_value={
                     "status": "success", "items": [{
                         "title": "小米汽车发布新消息", "url": "https://example.test/a",
                         "snippet": "公开摘要", "published": "2026-09-05T09:00:00+00:00",
                         "external_id": "a-1",
                     }], "feed_title": "示例媒体", "cursor_after": "a-1",
                     "etag": '"v1"', "last_modified": "",
                     "http_status": 200,
                 }) as fetch:
                source_id = db.radar_source_identity_get_or_create("示例媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/feed.xml")
                db.topic_create("radar-feed-1", "小米", ["小米"], [],
                                datetime.now(timezone.utc).isoformat(), kind="radar")
                db.radar_topic_endpoint_bind("radar-feed-1", endpoint_id)
                sub = db.subscription_upsert("radar-feed-1", 900, True,
                                              datetime.now(timezone.utc).isoformat())
                result = RadarService()._collect_for_subscriptions([db.subscription_get(sub)])
                self.assertEqual(result[0]["items"], 1)
                self.assertEqual(result[0]["result_code"], "matched")
                self.assertEqual(db.radar_stats("radar-feed-1")["total"], 1)
                self.assertEqual(db.radar_endpoint_state(endpoint_id)["status"], "healthy")
                self.assertEqual(fetch.call_count, 1)

    def test_radar_service_respects_persisted_next_fetch_at(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-next-fetch-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 patch("radar.fetch_feed") as fetch:
                source_id = db.radar_source_identity_get_or_create("未到期媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/feed.xml")
                db.topic_create("radar-next-fetch", "小米", ["小米"], [],
                                datetime.now(timezone.utc).isoformat(), kind="radar")
                db.radar_topic_endpoint_bind("radar-next-fetch", endpoint_id)
                now = datetime.now(timezone.utc).isoformat()
                db.radar_endpoint_state_upsert(
                    endpoint_id, status="healthy", checked_at=now,
                    next_fetch_at="2999-01-01T00:00:00+00:00", cursor_value="old-cursor",
                )
                sub = db.subscription_upsert("radar-next-fetch", 900, True, now)
                result = RadarService()._collect_for_subscriptions([db.subscription_get(sub)])
                self.assertEqual(result[0]["status"], "success")
                self.assertEqual(result[0]["result_code"], "deferred")
                fetch.assert_not_called()
                self.assertEqual(db.radar_endpoint_state(endpoint_id)["cursor_value"], "old-cursor")

    def test_manual_refresh_does_not_bypass_cooldown(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-cooldown-run-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 patch("radar.fetch_feed") as fetch:
                source_id = db.radar_source_identity_get_or_create("冷却媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/cooldown.xml")
                now = datetime.now(timezone.utc).isoformat()
                db.topic_create("radar-cooldown", "冷却", ["冷却"], [], now,
                                kind="radar", source_scope=["L1"])
                db.radar_topic_endpoint_bind("radar-cooldown", endpoint_id)
                db.radar_endpoint_state_upsert(
                    endpoint_id, status="cooldown", checked_at=now,
                    next_fetch_at="2999-01-01T00:00:00+00:00",
                    cooldown_until="2999-01-01T00:00:00+00:00", consecutive_failures=5,
                )
                sub_id = db.subscription_upsert("radar-cooldown", 900, True, now)
                run_id = db.monitor_run_create(sub_id, "radar-cooldown", now, "")
                result = RadarService().run_topic("radar-cooldown", run_id)
                self.assertEqual(result["result_code"], "deferred")
                self.assertEqual(db.monitor_runs("radar-cooldown", 1)[0]["result_code"], "deferred")
                fetch.assert_not_called()

    def test_radar_service_skips_endpoint_owned_by_another_worker(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-lease-run-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 patch("radar.fetch_feed") as fetch, \
                 patch.object(db, "radar_endpoint_lease_acquire", return_value=False):
                source_id = db.radar_source_identity_get_or_create("占用媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/feed.xml")
                db.topic_create("radar-lease-run", "小米", ["小米"], [],
                                datetime.now(timezone.utc).isoformat(), kind="radar")
                db.radar_topic_endpoint_bind("radar-lease-run", endpoint_id)
                now = datetime.now(timezone.utc).isoformat()
                sub = db.subscription_upsert("radar-lease-run", 900, True, now)
                RadarService()._collect_for_subscriptions([db.subscription_get(sub)])
                fetch.assert_not_called()

    def test_manual_run_reuses_precreated_monitor_run(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-run-status-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch("radar.fetch_for_sources", return_value=([], [])):
                now = datetime.now(timezone.utc).isoformat()
                db.topic_create("radar-run-status", "状态", ["状态"], [], now, kind="radar")
                sub_id = db.subscription_upsert("radar-run-status", 900, True, now)
                run_id = db.monitor_run_create(sub_id, "radar-run-status", now, "before")
                result = RadarService().run_topic("radar-run-status", run_id)
                runs = db.monitor_runs("radar-run-status", 10)
                self.assertEqual(result["run_id"], run_id)
                self.assertEqual(result["result_code"], "no_sources")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["status"], "success")
                self.assertEqual(runs[0]["result_code"], "no_sources")

    def test_feed_failure_preserves_cursor_and_enters_backoff(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-feed-fail-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"), \
                 patch("radar.fetch_for_sources", return_value=([], [])), \
                 patch("radar.fetch_feed", side_effect=RadarFeedError("http_error", "上游不可用", http_status=503)):
                source_id = db.radar_source_identity_get_or_create("失败媒体", "example.test")
                endpoint_id = db.radar_endpoint_create(source_id, "rss", "https://example.test/feed.xml")
                db.topic_create("radar-feed-2", "小米", ["小米"], [],
                                datetime.now(timezone.utc).isoformat(), kind="radar")
                db.radar_topic_endpoint_bind("radar-feed-2", endpoint_id)
                now = datetime.now(timezone.utc).isoformat()
                db.radar_endpoint_state_upsert(endpoint_id, status="healthy", checked_at=now,
                                                cursor_value="old-cursor", etag='"old"')
                sub = db.subscription_upsert("radar-feed-2", 900, True, now)
                result = RadarService()._collect_for_subscriptions([db.subscription_get(sub)])
                self.assertEqual(result[0]["items"], 0)
                state = db.radar_endpoint_state(endpoint_id)
                self.assertEqual(state["cursor_value"], "old-cursor")
                self.assertEqual(state["status"], "degraded")
                self.assertEqual(state["consecutive_failures"], 1)


if __name__ == "__main__":
    unittest.main()
