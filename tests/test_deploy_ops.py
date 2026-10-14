"""Tests for deploy_ops.py (written first, TDD RED)."""

import base64
import hashlib
import json
import unittest


def _gzip_bytes(data: bytes) -> bytes:
    import gzip

    return gzip.compress(data, compresslevel=9, mtime=0)


class DeployDiffTest(unittest.TestCase):
    def test_reports_missing_changed_and_extra(self):
        import starfield.core as core
        from starfield import deploy_ops

        real = core.run
        staged_gz = _gzip_bytes(b"<html>new</html>")
        listing = [
            {"name": "index.html", "size": "999",
             "md5_hash": base64.b64encode(hashlib.md5(b"old").digest()).decode()},
            {"name": "stale.js", "size": "3",
             "md5_hash": base64.b64encode(hashlib.md5(b"old").digest()).decode()},
        ]

        def fake(cmd, timeout=300):
            if cmd[:3] == ["gcloud", "storage", "objects"]:
                return json.dumps(listing)
            raise AssertionError(f"unexpected: {cmd}")

        core.run = fake
        try:
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                Path(d, "index.html").write_bytes(b"<html>new</html>")
                Path(d, "fresh.js").write_text("x")
                out = deploy_ops.deploy_diff(d, "bkt")
        finally:
            core.run = real
        self.assertIn("index.html", out["changed"])
        self.assertIn("fresh.js", out["missing"])
        self.assertIn("stale.js", out["extra"])
        self.assertEqual(out["unchanged"], [])


class PublicCheckTest(unittest.TestCase):
    def test_public_bucket_named(self):
        import starfield.core as core
        from starfield import deploy_ops

        real = core.run
        core.run = lambda *a, **k: json.dumps({"bindings": [
            {"role": "roles/storage.objectViewer", "members": ["allUsers"]}]})
        try:
            out = deploy_ops.public_check("bkt")
        finally:
            core.run = real
        self.assertTrue(out["public"])
        self.assertIn("allUsers", str(out["bindings"]))

    def test_private_bucket_flagged(self):
        import starfield.core as core
        from starfield import deploy_ops

        real = core.run
        core.run = lambda *a, **k: json.dumps({"bindings": []})
        try:
            out = deploy_ops.public_check("bkt")
        finally:
            core.run = real
        self.assertFalse(out["public"])


class DeployHealthTest(unittest.TestCase):
    def test_reports_status_and_latency_per_url(self):
        from starfield import deploy_ops

        real_check = deploy_ops.verify.check
        deploy_ops.verify.check = lambda url, **k: (
            {"ok": True, "status": 200, "bytes": 10, "content_type": "image/svg+xml"}
            if "good" in url else
            {"ok": False, "status": 404, "bytes": 0, "content_type": "",
             "error": "GET x: HTTP 404"})
        try:
            out = deploy_ops.deploy_health(
                ["https://x/good.svg", "https://x/bad.svg"])
        finally:
            deploy_ops.verify.check = real_check
        by_url = {r["url"]: r for r in out["results"]}
        self.assertTrue(by_url["https://x/good.svg"]["ok"])
        self.assertIn("ms", by_url["https://x/good.svg"])
        self.assertFalse(by_url["https://x/bad.svg"]["ok"])
        self.assertEqual(out["failing"], ["https://x/bad.svg"])


if __name__ == "__main__":
    unittest.main()
