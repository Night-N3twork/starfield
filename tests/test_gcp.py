"""Tests for gcp.py shared cloud helpers (written first, TDD RED)."""

import json
import unittest

from starfield import gcp


class GcpHelpersTest(unittest.TestCase):
    def test_sum_series_adds_int_and_double_values(self):
        series = [
            {"points": [{"value": {"int64Value": "10"}},
                        {"value": {"doubleValue": 2.5}}]},
            {"points": [{"value": {"int64Value": "7"}}]},
        ]
        self.assertEqual(gcp.sum_series(series), 19.5)

    def test_timeseries_sends_bearer_and_returns_series(self):
        seen = {}

        def fake_fetch(url, token, timeout=60):
            seen["auth"] = token
            seen["metric"] = "m" in url
            return {"timeSeries": [{"resource": {"labels": {"bucket_name": "b"}}} ]}

        real = gcp._fetch_json
        gcp._fetch_json = fake_fetch
        try:
            out = gcp.timeseries("proj", "metric.type=\"m\"", "s", "e", token="tok")
        finally:
            gcp._fetch_json = real
        self.assertEqual(seen["auth"], "tok")
        self.assertTrue(seen["metric"])
        self.assertEqual(len(out), 1)

    def test_bq_query_parses_json_rows(self):
        import starfield.core as core

        real = core.run
        core.run = lambda *a, **k: json.dumps([{"d": 1}])
        try:
            self.assertEqual(gcp.bq_query("SELECT 1", timeout=60), [{"d": 1}])
        finally:
            core.run = real


if __name__ == "__main__":
    unittest.main()
