"""MCP stdio server tests (Task 7). Stdlib unittest only.

Drives the JSON-RPC-over-stdio server in-process over pipes: feed
newline-delimited requests on a fake stdin, collect response lines
from a fake stdout. Covers tools/list names, list_mirrors content,
the read-only refusal, and the allow-create gate, plus passthrough
kwargs, unknown tool/method errors, and `mcp` CLI wiring.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from starfield import cli, mcp_server

EXPECTED_TOOLS = (
    "gh_sync",
    "gcs_upload",
    "ensure_repo",
    "ensure_bucket",
    "gcs_list",
    "gh_list",
    "verify_deploy",
    "list_mirrors",
)


def _req(id_, method, params=None):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _drive(payloads, **kwargs):
    """Run the server over in-memory pipes; return response dicts."""
    stdin = io.StringIO("".join(json.dumps(p) + "\n" for p in payloads))
    stdout = io.StringIO()
    mcp_server.run(stdin=stdin, stdout=stdout, **kwargs)
    return [
        json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()
    ]


def _call(name, arguments):
    return _req(1, "tools/call", {"name": name, "arguments": arguments})


def _text(result):
    return result["result"]["content"][0]["text"]


class TestInitialize(unittest.TestCase):
    def test_initialize_returns_server_info(self):
        (resp,) = _drive([_req(1, "initialize")])
        self.assertEqual(resp["id"], 1)
        self.assertIn("serverInfo", resp["result"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "starfield")
        self.assertIn("tools", resp["result"].get("capabilities", {}))


class TestToolsList(unittest.TestCase):
    def test_tools_list_names(self):
        (resp,) = _drive([_req(1, "tools/list")])
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, list(EXPECTED_TOOLS))

    def test_tools_list_entries_have_schemas(self):
        (resp,) = _drive([_req(1, "tools/list")])
        for tool in resp["result"]["tools"]:
            self.assertIn("description", tool, tool)
            self.assertIn("inputSchema", tool, tool)
            self.assertEqual(tool["inputSchema"].get("type"), "object")


class TestListMirrors(unittest.TestCase):
    def test_list_mirrors_content(self):
        (resp,) = _drive(
            [
                _call(
                    "list_mirrors",
                    {
                        "org": "acme",
                        "repo": "lyra",
                        "ref": "abc1234",
                        "file": "index.svg",
                    },
                )
            ]
        )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertIn(
            "https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg",
            payload["text"],
        )

    def test_list_mirrors_uses_default_org(self):
        (resp,) = _drive(
            [_call("list_mirrors", {"repo": "lyra", "ref": "abc1234"})],
            org="acme",
        )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertIn("acme/lyra@abc1234", payload["text"])

    def test_list_mirrors_missing_param_is_error_result(self):
        (resp,) = _drive([_call("list_mirrors", {"repo": "lyra"})])
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("missing", _text(resp).lower())


class TestReadOnly(unittest.TestCase):
    def test_mutating_tools_refused_under_read_only(self):
        calls = [
            _call("gh_sync", {"org": "a", "repo": "r", "folder": "/tmp/x"}),
            _call("gcs_upload", {"bucket": "b", "folder": "/tmp/x"}),
            _call("ensure_repo", {"org": "a", "repo": "r"}),
            _call("ensure_bucket", {"bucket": "b"}),
        ]
        reqs = [dict(c, id=i + 1) for i, c in enumerate(calls)]
        with (
            mock.patch(
                "starfield.mcp_server.gh_ops.sync",
                side_effect=AssertionError("must not run"),
            ),
            mock.patch(
                "starfield.mcp_server.gcs_ops.upload",
                side_effect=AssertionError("must not run"),
            ),
        ):
            resps = _drive(reqs, read_only=True, allow_create=True)
        self.assertEqual(len(resps), 4)
        for resp in resps:
            self.assertTrue(resp["result"]["isError"], resp)
            self.assertIn("read-only", _text(resp).lower(), resp)

    def test_read_tools_still_work_under_read_only(self):
        (resp,) = _drive(
            [_call("list_mirrors", {"org": "a", "repo": "r", "ref": "x"})],
            read_only=True,
        )
        self.assertFalse(resp["result"]["isError"])


class TestAllowCreateGate(unittest.TestCase):
    def test_ensure_tools_refused_without_allow_create(self):
        reqs = [
            dict(_call("ensure_repo", {"org": "a", "repo": "r"}), id=1),
            dict(_call("ensure_bucket", {"bucket": "b"}), id=2),
        ]
        with (
            mock.patch(
                "starfield.mcp_server.gh_ops.ensure_repo",
                side_effect=AssertionError("must not run"),
            ),
            mock.patch(
                "starfield.mcp_server.gcs_ops.ensure_bucket",
                side_effect=AssertionError("must not run"),
            ),
        ):
            resps = _drive(reqs)
        self.assertEqual(len(resps), 2)
        for resp in resps:
            self.assertTrue(resp["result"]["isError"], resp)
            self.assertIn("allow-create", _text(resp).lower(), resp)

    def test_ensure_repo_allowed_calls_through(self):
        with mock.patch(
            "starfield.mcp_server.gh_ops.ensure_repo",
            return_value={"created": True},
        ) as m:
            (resp,) = _drive(
                [_call("ensure_repo", {"org": "acme", "repo": "lyra"})],
                allow_create=True,
            )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertTrue(payload["created"])
        m.assert_called_once_with(
            org="acme", repo="lyra", public=False, timeout=300
        )

    def test_ensure_bucket_allowed_calls_through(self):
        with mock.patch(
            "starfield.mcp_server.gcs_ops.ensure_bucket",
            return_value={"created": False},
        ) as m:
            (resp,) = _drive(
                [_call("ensure_bucket", {"bucket": "bkt"})],
                allow_create=True,
            )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertFalse(payload["created"])
        m.assert_called_once_with(
            bucket="bkt", location="us-central1", public=False, timeout=300
        )


class TestPassthrough(unittest.TestCase):
    def test_gh_sync_forwards_kwargs(self):
        with mock.patch(
            "starfield.mcp_server.gh_ops.sync",
            return_value={"sha": "abc", "changed": True, "pushed": True},
        ) as m:
            (resp,) = _drive(
                [
                    _call(
                        "gh_sync",
                        {
                            "org": "acme",
                            "repo": "lyra",
                            "folder": "/tmp/site",
                        },
                    )
                ]
            )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
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

    def test_gcs_upload_forwards_kwargs(self):
        with mock.patch(
            "starfield.mcp_server.gcs_ops.upload",
            return_value={"raw_bytes": 1, "stored_bytes": 2, "files": []},
        ) as m:
            (resp,) = _drive(
                [
                    _call(
                        "gcs_upload",
                        {"bucket": "bkt", "folder": "/tmp/site", "prefix": "p"},
                    )
                ]
            )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertEqual(payload["raw_bytes"], 1)
        m.assert_called_once_with(
            bucket="bkt",
            folder="/tmp/site",
            prefix="p",
            cache_control=None,
            create_only=False,
            dry_run=False,
            timeout=300,
        )

    def test_verify_deploy_passes_through(self):
        with mock.patch(
            "starfield.mcp_server.verify.check",
            return_value={
                "ok": True,
                "status": 200,
                "bytes": 3,
                "content_type": "text/plain",
            },
        ) as m:
            (resp,) = _drive(
                [_call("verify_deploy", {"url": "http://example.invalid/x"})]
            )
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertTrue(payload["ok"])
        m.assert_called_once_with(
            url="http://example.invalid/x",
            sha256=None,
            mime=None,
            min_bytes=1,
            timeout=300,
        )

    def test_gh_list_uses_default_org(self):
        with mock.patch(
            "starfield.mcp_server.gh_ops.list_repos",
            return_value=[{"name": "lyra"}],
        ) as m:
            (resp,) = _drive([_call("gh_list", {})], org="acme")
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertEqual(payload, [{"name": "lyra"}])
        m.assert_called_once_with(org="acme", timeout=300)

    def test_gcs_list_buckets_when_no_bucket(self):
        with mock.patch(
            "starfield.mcp_server.gcs_ops.list_buckets",
            return_value=[{"name": "bkt"}],
        ) as m:
            (resp,) = _drive([_call("gcs_list", {})])
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertEqual(payload, [{"name": "bkt"}])
        m.assert_called_once_with(timeout=300)

    def test_gcs_list_objects_with_bucket(self):
        with mock.patch(
            "starfield.mcp_server.gcs_ops.list_objects",
            return_value=[{"name": "index.html"}],
        ) as m:
            (resp,) = _drive([_call("gcs_list", {"bucket": "bkt"})])
        payload = json.loads(_text(resp))
        self.assertFalse(resp["result"]["isError"], payload)
        self.assertEqual(payload, [{"name": "index.html"}])
        m.assert_called_once_with("bkt", timeout=300)

    def test_tool_exception_becomes_error_result(self):
        with mock.patch(
            "starfield.mcp_server.gh_ops.list_repos",
            side_effect=Exception("boom"),
        ):
            (resp,) = _drive([_call("gh_list", {"org": "acme"})])
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("boom", _text(resp))


class TestProtocolErrors(unittest.TestCase):
    def test_unknown_tool_is_jsonrpc_error(self):
        (resp,) = _drive([_call("nope", {})])
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method_is_jsonrpc_error(self):
        (resp,) = _drive([_req(1, "bogus/method")])
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_malformed_line_is_parse_error(self):
        stdin = io.StringIO("not json\n")
        stdout = io.StringIO()
        mcp_server.run(stdin=stdin, stdout=stdout)
        (resp,) = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(resp["error"]["code"], -32700)

    def test_notification_gets_no_response(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "tools/list"}) + "\n"
        )
        stdout = io.StringIO()
        mcp_server.run(stdin=stdin, stdout=stdout)
        self.assertEqual(stdout.getvalue().strip(), "")


class TestMcpCli(unittest.TestCase):
    def test_mcp_help_lists_read_only(self):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                cli.main(["mcp", "--help"])
        except SystemExit:
            pass
        self.assertIn("--read-only", buf.getvalue())

    def test_mcp_invokes_server(self):
        with mock.patch("starfield.cli.mcp_server.run") as m:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                code = cli.main(["mcp", "--org", "acme", "--read-only"])
        self.assertEqual(code, 0)
        m.assert_called_once()
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("org"), "acme")
        self.assertTrue(kwargs.get("read_only"))


if __name__ == "__main__":
    unittest.main()
