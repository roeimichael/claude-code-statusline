import json
import unittest

import statusline as sl


class NonfiniteNumbers(unittest.TestCase):
    def test_cache_overflow_is_rejected_without_hiding_payload_usage(self):
        cache = sl._clean_usage_cache(json.loads(
            '{"fetched_at":1e309,"usage":{"five_hour":{"utilization":70,"resets_at":1e309}}}'))
        self.assertNotIn("fetched_at", cache)
        usage = sl.merge_usage({"five_hour": {"used_percentage": 42, "resets_at": 3700}},
                               cache.get("usage"), 100)
        line = sl.format_statusline({}, usage, 100, True)
        self.assertIn("42%", sl.ANSI_RE.sub("", line))

    def test_nonfinite_percentages_are_missing(self):
        for value in (float("inf"), float("-inf"), float("nan"), "Infinity", "NaN"):
            with self.subTest(value=value):
                usage = sl.merge_usage({"five_hour": {"used_percentage": value}}, None, 100)
                self.assertIsNone(usage["five_hour"])
                self.assertEqual(sl.context_pct({"used_percentage": value,
                                                "context_window_size": 1000}), 0)

    def test_nonfinite_reset_cannot_crash_countdown(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                self.assertEqual(sl.fmt_reset(value), "")


if __name__ == "__main__":
    unittest.main()
