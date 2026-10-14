"""Tests for billing.py (written first, TDD RED)."""

import unittest

from starfield import billing


class ExportStatusTest(unittest.TestCase):
    def test_missing_dataset_reports_setup_not_error(self):
        import starfield.core as core

        real = core.run
        core.run = lambda *a, **k: (_ for _ in ()).throw(
            core.CliError("bq: Not found: Dataset proj:billing_export"))
        try:
            out = billing.export_status("proj")
        finally:
            core.run = real
        self.assertFalse(out["enabled"])
        self.assertIn("Billing export", out["setup"])

    def test_present_export_table_reports_enabled(self):
        import starfield.core as core

        real = core.run
        core.run = lambda *a, **k: '[{"table_id": "gcp_billing_export_v1_ABCDEF"}]'
        try:
            out = billing.export_status("proj")
        finally:
            core.run = real
        self.assertTrue(out["enabled"])


class CostSummaryTest(unittest.TestCase):
    def test_sums_cost_and_credits(self):
        import starfield.core as core

        real = core.run
        rows = [{"service": "Cloud Storage", "cost": 3.5, "credits": -0.5},
                {"service": "BigQuery", "cost": 1.0, "credits": 0.0}]

        def fake(cmd, timeout=300):
            if "INFORMATION_SCHEMA" in cmd[-1]:
                return '[{"table_id": "gcp_billing_export_v1_ABCDEF"}]'
            import json
            return json.dumps(rows)

        core.run = fake
        try:
            out = billing.cost_summary("proj", days=30)
        finally:
            core.run = real
        self.assertAlmostEqual(out["total_cost"], 4.5)
        self.assertAlmostEqual(out["total_credits"], -0.5)
        self.assertAlmostEqual(out["net"], 4.0)
        self.assertIn("source", out)


class PricingTest(unittest.TestCase):
    def test_extracts_tier_rates(self):
        import starfield.billing as bmod

        real = bmod.gcp._fetch_json
        bmod.gcp._fetch_json = lambda url, token, timeout=60: (
            {"services": [{"name": "services/6F81-5844-456A",
                           "displayName": "Cloud Storage"}]}
            if url.endswith("/services?pageSize=100") else
            {
            "skus": [{
                "description": "Download Worldwide test",
                "category": {"resourceGroup": "NetworkEgress"},
                "serviceRegions": ["global"],
                "pricingInfo": [{
                    "pricingExpression": {
                        "usageUnit": "GiBy",
                        "tieredRates": [{"unitPrice": {"units": "0", "nanos": 120000000}}],
                    }
                }],
            }]
        })
        try:
            out = billing.pricing("download worldwide")
        finally:
            bmod.gcp._fetch_json = real
        self.assertEqual(len(out["skus"]), 1)
        self.assertAlmostEqual(out["skus"][0]["price_usd_per_unit"], 0.12)
        self.assertEqual(out["skus"][0]["unit"], "GiBy")


class ForecastTest(unittest.TestCase):
    def test_projects_bytes_and_cost(self):
        import starfield.billing as bmod

        real_traffic = bmod.analytics.traffic_history
        bmod.analytics.traffic_history = lambda *a, **k: {
            "daily": [{"day": "2026-10-01", "sent_bytes": 100.0, "requests": 1.0},
                      {"day": "2026-10-02", "sent_bytes": 300.0, "requests": 1.0}]}
        try:
            out = billing.forecast("proj", days_history=30, days_ahead=30,
                                   rate_usd_per_gib=0.12, free_gib=0.0)
        finally:
            bmod.analytics.traffic_history = real_traffic
        # avg 200 B/day * 30 d = 6000 B projected
        self.assertAlmostEqual(out["projected_bytes"], 6000.0)
        self.assertIn("rate", out)


if __name__ == "__main__":
    unittest.main()
