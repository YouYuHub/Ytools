"""附接 marker 计时起点回归：round_started_at 独立于轮次号下发，注册即有近似值。"""
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factory.chat_factory as cf


class AttachMarkerStartedAtTests(unittest.TestCase):
    def _stream(self, **overrides):
        base = {
            "question_text": "hi",
            "question_parts": None,
            "question_quotes": None,
            "live_round_no": None,
            "live_round_started_at_ms": None,
            "live_round_model": None,
            "live_round_usage_total": None,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_registration_seeds_started_at(self):
        before = int(time.time() * 1000)
        stream = cf._SessionStream("sid-marker-seed")
        after = int(time.time() * 1000)
        self.assertIsInstance(stream.live_round_started_at_ms, int)
        self.assertGreaterEqual(stream.live_round_started_at_ms, before)
        self.assertLessEqual(stream.live_round_started_at_ms, after)

    def test_marker_carries_started_at_without_round(self):
        """确认前附接：无轮次号也有可用起点（前端计时不从附接时刻重计）。"""
        marker = cf._build_attach_marker(
            self._stream(live_round_started_at_ms=1720000000000), False
        )
        self.assertTrue(marker.get("replay"))
        self.assertNotIn("round", marker)
        self.assertEqual(marker.get("round_started_at"), 1720000000000)

    def test_marker_full_fields_with_round(self):
        """确认后附接：既有轮次号也有精确起点（既有行为不变）。"""
        marker = cf._build_attach_marker(
            self._stream(
                live_round_no=3,
                live_round_started_at_ms=1720000000000,
                live_round_usage_total={"total_tokens": 10},
                live_round_model={"provider": "p", "name": "m"},
            ),
            True,
        )
        self.assertEqual(marker.get("round"), 3)
        self.assertEqual(marker.get("round_started_at"), 1720000000000)
        self.assertEqual(marker.get("round_usage_total"), {"total_tokens": 10})
        self.assertEqual(marker.get("model"), {"provider": "p", "name": "m"})
        self.assertTrue(marker.get("replay_complete"))

    def test_marker_skips_invalid_started_at(self):
        marker = cf._build_attach_marker(
            self._stream(live_round_no=2, live_round_started_at_ms=-5), False
        )
        self.assertEqual(marker.get("round"), 2)
        self.assertNotIn("round_started_at", marker)


if __name__ == "__main__":
    unittest.main()
