import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import db
from evidence import normalize_mention
from radar import match_keywords


class RadarTests(unittest.TestCase):
    def test_match_all_keywords_and_excludes(self):
        item = {"title": "品牌A 发布新品", "snippet": "公开发布会消息"}
        hit = match_keywords(item, ["品牌A", "新品"], ["招聘"])
        self.assertEqual(hit["matched_keywords"], ["品牌A", "新品"])
        self.assertEqual(hit["match_location"], "title")
        self.assertIsNone(match_keywords({"title": "品牌A 招聘"}, ["品牌A"], ["招聘"]))

    def test_topic_timeline_and_unread_state(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                first = "2026-09-05T08:00:00+00:00"
                second = "2026-09-05T09:00:00+00:00"
                db.topic_create("radar-1", "品牌观察", ["品牌A", "新品"], [], first, kind="radar")
                for title, stamp in (("品牌A 新品预告", first), ("品牌A 新品正式发布", second)):
                    mention = normalize_mention(
                        {"title": title, "url": f"https://example.test/{title[-2:]}", "published": stamp},
                        source_id="示例媒体", source_type="feed", captured_at=stamp,
                    )
                    mention_id, _ = db.mention_upsert(mention)
                    db.mention_topic_touch(mention_id, "radar-1", stamp, ["品牌A", "新品"], "title")
                items = db.radar_timeline("radar-1")
                self.assertEqual([item["title"] for item in items], ["品牌A 新品正式发布", "品牌A 新品预告"])
                self.assertEqual(db.radar_stats("radar-1")["unread"], 2)
                db.radar_mark_read("radar-1", second)
                self.assertEqual(db.radar_stats("radar-1")["unread"], 0)

    def test_delete_radar_topic_removes_subscription_edges(self):
        with tempfile.TemporaryDirectory(prefix="miaoyu-radar-delete-") as tmp:
            with patch.object(db, "SETTINGS_DB", Path(tmp) / "settings.db"):
                now = datetime.now(timezone.utc).isoformat()
                db.topic_create("radar-2", "可删除", ["删除"], [], now, kind="radar")
                db.subscription_upsert("radar-2", 900, True, now)
                self.assertTrue(db.radar_delete_topic("radar-2"))
                self.assertIsNone(db.topic_get("radar-2"))
                self.assertFalse(db.radar_delete_topic("radar-2"))


if __name__ == "__main__":
    unittest.main()
