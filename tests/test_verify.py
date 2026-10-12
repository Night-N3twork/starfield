"""Verify tests (Task 5). Stdlib unittest only.

`verify.check` is exercised against a real local `http.server` fixture
(ephemeral port, in-thread): 200+match PASS, wrong-hash FAIL, 404 FAIL
with status recorded, plus MIME prefix, min-bytes, and gzip decoding.
The real network is never touched.
"""

import gzip
import hashlib
import io
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from starfield import cli, verify

BODY = b"hello starfield verify"
GZIPPED = gzip.compress(BODY, compresslevel=9, mtime=0)
SHA = hashlib.sha256(BODY).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass

    def do_GET(self):
        if self.path == "/ok":
            data = BODY
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/gzip":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(GZIPPED)))
            self.end_headers()
            self.wfile.write(GZIPPED)
        else:
            data = b"nope"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


class VerifyFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        cls.thread.daemon = True
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()


class TestCheck(VerifyFixture):
    def test_match_passes(self):
        out = verify.check(f"{self.base}/ok", sha256=SHA)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["bytes"], len(BODY))
        self.assertIn("text/plain", out["content_type"])

    def test_no_sha_still_checks_status(self):
        out = verify.check(f"{self.base}/ok")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], 200)

    def test_wrong_hash_fails_with_detail(self):
        out = verify.check(f"{self.base}/ok", sha256="0" * 64)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["status"], 200)
        self.assertIn("sha256", out["error"].lower())
        self.assertIn("0" * 8, out["error"])

    def test_404_fails_with_status_recorded(self):
        out = verify.check(f"{self.base}/missing", sha256=SHA)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["status"], 404)
        self.assertIn("404", out["error"])

    def test_mime_prefix_match(self):
        out = verify.check(f"{self.base}/ok", sha256=SHA, mime="text/plain")
        self.assertTrue(out["ok"], out)

    def test_mime_mismatch_fails(self):
        out = verify.check(f"{self.base}/ok", sha256=SHA, mime="image/svg+xml")
        self.assertFalse(out["ok"], out)
        self.assertIn("image/svg+xml", out["error"])

    def test_min_bytes_failure(self):
        out = verify.check(f"{self.base}/ok", sha256=SHA, min_bytes=len(BODY) + 1)
        self.assertFalse(out["ok"], out)
        self.assertIn("bytes", out["error"].lower())

    def test_gzip_body_is_decoded_before_hashing(self):
        out = verify.check(f"{self.base}/gzip", sha256=SHA)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["bytes"], len(BODY))

    def test_connection_refused_reports_error(self):
        out = verify.check("http://127.0.0.1:1/unreachable", timeout=2)
        self.assertFalse(out["ok"], out)
        self.assertIn("error", out)


class TestVerifyCli(VerifyFixture):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_cli_verify_pass(self):
        code, _ = self._run(
            ["verify", "--url", f"{self.base}/ok", "--sha256", SHA]
        )
        self.assertEqual(code, 0)

    def test_cli_verify_fail(self):
        code, _ = self._run(
            ["verify", "--url", f"{self.base}/missing", "--sha256", SHA]
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
