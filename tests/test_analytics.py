"""Tests for analytics.py (written first, TDD RED)."""

import unittest

from starfield import analytics


def _series(bucket, method="ReadObject", code="OK", values=(10, 20)):
    return [{
        "resource": {"labels": {"bucket_name": bucket}},
        "metric": {"labels": {"method": method, "response_code": code}},
        "points": [{"value": {"int64Value": str(v)},
                    "interval": {"endTime": "2026-10-11T00:00:00Z"}} for v in values],
    }]


def _fake_q(mapping):
    def q(project, metric_filter, start, end, timeout=300):
        for key, series in mapping.items():
            if key in metric_filter:
                return series
        return []

    return q


class UsageTest(unittest.TestCase):
    def test_usage_aggregates_bytes_and_requests_per_bucket(self):
        q = _fake_q({
            "sent_bytes_count": _series("b1") + _series("b2", values=(5,)),
            "received_bytes_count": _series("b1", values=(1,)),
            "request_count": _series("b1", values=(2, 3)),
        })
        out = analytics.usage("proj", days=1, _q=q)
        self.assertEqual(out["buckets"]["b1"]["sent_bytes"], 30)
        self.assertEqual(out["buckets"]["b2"]["sent_bytes"], 5)
        self.assertEqual(out["buckets"]["b1"]["received_bytes"], 1)
        self.assertEqual(out["buckets"]["b1"]["requests"], 5)
        self.assertEqual(out["totals"]["sent_bytes"], 35)
        self.assertIn("source", out)

    def test_usage_single_bucket_filter(self):
        q = _fake_q({"sent_bytes_count": _series("b1") + _series("b2")})
        out = analytics.usage("proj", bucket="b1", days=1, _q=q)
        self.assertEqual(set(out["buckets"]), {"b1"})


class TrafficHistoryTest(unittest.TestCase):
    def test_groups_points_by_day(self):
        series = [{
            "resource": {"labels": {"bucket_name": "b1"}},
            "metric": {"labels": {}},
            "points": [
                {"value": {"int64Value": "10"},
                 "interval": {"endTime": "2026-10-10T12:00:00Z"}},
                {"value": {"int64Value": "20"},
                 "interval": {"endTime": "2026-10-11T12:00:00Z"}},
                {"value": {"int64Value": "5"},
                 "interval": {"endTime": "2026-10-11T13:00:00Z"}},
            ],
        }]
        out = analytics.traffic_history("proj", days=2, _q=_fake_q({"sent_bytes_count": series}))
        by_day = {row["day"]: row["sent_bytes"] for row in out["daily"]}
        self.assertEqual(by_day, {"2026-10-10": 10, "2026-10-11": 25})


class RequestStatsTest(unittest.TestCase):
    def test_groups_by_method_and_code(self):
        q = _fake_q({"request_count": _series("b1", values=(4,))
                     + _series("b1", method="WriteObject", values=(1,))})
        out = analytics.request_stats("proj", days=1, _q=q)
        self.assertEqual(out["by_method"]["ReadObject"]["OK"], 4)
        self.assertEqual(out["by_method"]["WriteObject"]["OK"], 1)
        self.assertEqual(out["totals"]["requests"], 5)
        self.assertEqual(out["totals"]["errors"], 0)


class TopObjectsTest(unittest.TestCase):
    def test_no_logging_configured_reports_unavailable(self):
        out = analytics.top_objects("proj", "bkt", _describe=lambda b: {})
        self.assertFalse(out["available"])
        self.assertIn("--log-bucket", out["setup"])


class AuditLogsTest(unittest.TestCase):
    def test_empty_log_read_is_graceful(self):
        out = analytics.audit_logs("proj", _run=lambda *a, **k: "[]")
        self.assertEqual(out["entries"], [])
        self.assertIn("Data Access", out["note"])


if __name__ == "__main__":
    unittest.main()
