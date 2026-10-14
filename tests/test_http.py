"""HTTP server tests (Task 6). Stdlib unittest only.

Real local HTTP server in-thread on an ephemeral port, exercised via
`urllib`: /healthz 200, POST /v1/links md text, unknown path 404 JSON,
ensure-bucket gate, plus list/verify passthrough with fakes.
"""

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from starfield import cli, http_server


def _start_server(allow_create=False):
    server = http_server.build_server(
        "127.0.0.1", 0, allow_create=allow_create
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}
    )
    thread.daemon = True
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _request(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class HttpFixture(unittest.TestCase):
    allow_create = False

    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base = _start_server(
            allow_create=cls.allow_create
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()


class TestHealthz(HttpFixture):
    def test_healthz_200_ok(self):
        status, payload = _request(self.base, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"], payload)


class TestLinks(HttpFixture):
    def test_links_post_returns_md_text(self):
        status, payload = _request(
            self.base,
            "POST",
            "/v1/links",
            {
                "org": "acme",
                "repo": "lyra",
                "ref": "abc1234",
                "file": "index.svg",
                "format": "md",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertIn(
            "- [cdn.jsdelivr.net]"
            "(https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg)",
            payload["text"],
        )


class TestUnknownRoute(HttpFixture):
    def test_unknown_path_404_json(self):
        status, payload = _request(self.base, "GET", "/nope")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)

    def test_wrong_method_404_json(self):
        status, payload = _request(self.base, "GET", "/v1/links")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])


class TestCreateGate(HttpFixture):
    def test_ensure_bucket_refused_without_allow_create(self):
        status, payload = _request(
            self.base, "POST", "/v1/ensure-bucket", {"bucket": "bkt"}
        )
        self.assertEqual(status, 403, payload)
        self.assertFalse(payload["ok"], payload)
        self.assertIn("allow-create", payload["error"].lower())

    def test_ensure_repo_refused_without_allow_create(self):
        status, payload = _request(
            self.base,
            "POST",
            "/v1/ensure-repo",
            {"org": "acme", "repo": "lyra"},
        )
        self.assertEqual(status, 403, payload)
        self.assertFalse(payload["ok"], payload)


class TestAllowedCreate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base = _start_server(allow_create=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_ensure_bucket_allowed_calls_through(self):
        with mock.patch(
            "starfield.http_server.gcs_ops.ensure_bucket",
            return_value={"created": True},
        ) as m:
            status, payload = _request(
                self.base, "POST", "/v1/ensure-bucket", {"bucket": "bkt"}
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["created"])
        m.assert_called_once()


class TestPassthrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base = _start_server(allow_create=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_verify_passes_through(self):
        with mock.patch(
            "starfield.http_server.verify.check",
            return_value={
                "ok": True,
                "status": 200,
                "bytes": 3,
                "content_type": "text/plain",
            },
        ):
            status, payload = _request(
                self.base,
                "POST",
                "/v1/verify",
                {"url": "http://example.invalid/ok"},
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], 200)

    def test_gh_list_get(self):
        with mock.patch(
            "starfield.http_server.gh_ops.list_repos",
            return_value=[{"name": "lyra"}],
        ):
            status, payload = _request(
                self.base, "GET", "/v1/gh-list?org=acme"
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["repos"], [{"name": "lyra"}])

    def test_gcs_list_buckets_get(self):
        with mock.patch(
            "starfield.http_server.gcs_ops.list_buckets",
            return_value=[{"name": "bkt"}],
        ):
            status, payload = _request(self.base, "GET", "/v1/gcs-list")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["buckets"], [{"name": "bkt"}])

    def test_gh_sync_passthrough_forwards_kwargs(self):
        with mock.patch(
            "starfield.http_server.gh_ops.sync",
            return_value={"sha": "abc", "changed": True, "pushed": True},
        ) as m:
            status, payload = _request(
                self.base,
                "POST",
                "/v1/gh-sync",
                {
                    "org": "acme",
                    "repo": "lyra",
                    "folder": "/tmp/site",
                    "branch": "main",
                },
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["sha"], "abc")
        m.assert_called_once_with(
            org="acme",
            repo="lyra",
            folder="/tmp/site",
            branch="main",
            message=None,
            delete=True,
            dry_run=False,
            workdir=None,
            timeout=300,
        )

    def test_gcs_upload_passthrough_forwards_kwargs(self):
        with mock.patch(
            "starfield.http_server.gcs_ops.upload",
            return_value={"raw_bytes": 10, "stored_bytes": 12, "files": []},
        ) as m:
            status, payload = _request(
                self.base,
                "POST",
                "/v1/gcs-upload",
                {"bucket": "bkt", "folder": "/tmp/site", "prefix": "p"},
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["raw_bytes"], 10)
        m.assert_called_once_with(
            bucket="bkt",
            folder="/tmp/site",
            prefix="p",
            cache_control=None,
            create_only=False,
            dry_run=False,
            timeout=300,
        )

    def test_gh_list_invalid_timeout_falls_back(self):
        with mock.patch(
            "starfield.http_server.gh_ops.list_repos",
            return_value=[],
        ) as m:
            status, payload = _request(
                self.base, "GET", "/v1/gh-list?org=acme&timeout=abc"
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        m.assert_called_once_with(org="acme", timeout=300)

    def test_gcs_list_invalid_timeout_falls_back(self):
        with mock.patch(
            "starfield.http_server.gcs_ops.list_buckets",
            return_value=[],
        ) as m:
            status, payload = _request(
                self.base, "GET", "/v1/gcs-list?timeout=abc"
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        m.assert_called_once_with(timeout=300)

    def test_missing_param_400_json(self):
        status, payload = _request(self.base, "POST", "/v1/verify", {})
        self.assertEqual(status, 400, payload)
        self.assertFalse(payload["ok"], payload)


class TestServeCli(unittest.TestCase):
    def test_serve_help_lists_port(self):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                cli.main(["serve", "--help"])
        except SystemExit:
            pass
        self.assertIn("--port", buf.getvalue())

    def test_serve_invokes_http_server(self):
        with mock.patch(
            "starfield.http_server.serve", return_value=None
        ) as m:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                code = cli.main(["serve", "--port", "8765"])
        self.assertEqual(code, 0)
        m.assert_called_once()
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("port", m.call_args[0][0] if m.call_args[0] else None), 8765)


class TestReadRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base = _start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, payload):
        import urllib.error

        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_public_check_passthrough(self):
        with mock.patch(
            "starfield.http_server.deploy_ops.public_check",
            return_value={"bucket": "b", "public": True},
        ):
            status, body = self._post("/v1/gcs-public-check", {"bucket": "b"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["public"])

    def test_deploy_health_passthrough(self):
        with mock.patch(
            "starfield.http_server.deploy_ops.deploy_health",
            return_value={"results": [], "passing": 0, "failing": []},
        ):
            status, body = self._post(
                "/v1/deploy-health", {"urls": ["https://x/y.svg"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_read_route_missing_field_is_400(self):
        status, body = self._post("/v1/gcs-public-check", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class TestReadCliWiring(unittest.TestCase):
    def test_new_commands_have_help(self):
        for cmd in ("gcs-usage", "gcp-pricing", "deploy-diff", "deploy-health",
                    "gcs-public-check"):
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main([cmd, "--help"])
            self.assertEqual(ctx.exception.code, 0, cmd)

    def test_public_check_wiring(self):
        with mock.patch(
            "starfield.cli.deploy_ops.public_check",
            return_value={"bucket": "b", "public": False},
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(["gcs-public-check", "--bucket", "b"])
        self.assertEqual(code, 0)
        self.assertIn('"public": false', buf.getvalue())


if __name__ == "__main__":
    unittest.main()
