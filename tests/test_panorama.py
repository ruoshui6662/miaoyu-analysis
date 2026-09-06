from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from panorama import classify_scope  # noqa: E402


class PanoramaScopeTests(unittest.TestCase):
    def test_clear_domestic_keeps_fast_path(self):
        result = classify_scope("某市社区物业收费调整")
        self.assertEqual(result["decision"], "domestic")
        self.assertEqual(result["scope_type"], "domestic")
        self.assertFalse(result["requires_gap_check"])

    def test_cross_border_strong_signal_enters_panorama(self):
        result = classify_scope("美国监管机构调查中国企业跨境数据业务")
        self.assertEqual(result["decision"], "panorama")
        self.assertEqual(result["scope_type"], "cross_border")
        self.assertIn("foreign_entity", {item["code"] for item in result["reasons"]})

    def test_medium_signal_defers_to_gap_check(self):
        result = classify_scope("某品牌在海外市场的舆论变化")
        self.assertEqual(result["decision"], "first_pass_then_gap_check")
        self.assertTrue(result["requires_gap_check"])

    def test_manual_overrides_are_recorded(self):
        result = classify_scope("本地社区事件", requested_mode="panorama")
        self.assertEqual(result["decision"], "panorama")
        self.assertEqual(result["trigger"], "user_override")
        self.assertEqual(result["requested_mode"], "panorama")

    def test_invalid_mode_falls_back_to_auto(self):
        result = classify_scope("本地社区事件", requested_mode="all-sources")
        self.assertEqual(result["requested_mode"], "auto")
        self.assertEqual(result["decision"], "domestic")
