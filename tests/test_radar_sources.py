import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import db
from radar_sources import RadarFeedError, fetch_feed, parse_feed, validate_endpoint_url
from radar import RadarService


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

    def test_invalid_or_unsafe_feed_is_rejected(self):
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("ftp://example.test/feed.xml")
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("https://user:password@example.test/feed.xml")
        with self.assertRaises(RadarFeedError):
            validate_endpoint_url("http://127.0.0.1/feed.xml")

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
                self.assertEqual(db.radar_stats("radar-feed-1")["total"], 1)
                self.assertEqual(db.radar_endpoint_state(endpoint_id)["status"], "healthy")
                self.assertEqual(fetch.call_count, 1)

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
