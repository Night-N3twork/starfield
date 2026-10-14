"""Streamable-HTTP transport for the MCP server (TDD RED first)."""

import json
import threading
import unittest
import urllib.request

from starfield import mcp_http


def _start(**kwargs):
    server = mcp_http.build_server(port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/mcp"


def _post(base, payload):
    req = urllib.request.Request(
        base,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.status, json.loads(res.read().decode())


class McpHttpTest(unittest.TestCase):
    def test_initialize_lists_eight_tools(self):
        server, base = _start()
        try:
            status, body = _post(base, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            self.assertEqual(status, 200)
            self.assertIn("protocolVersion", body["result"])
            _, listed = _post(base, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(listed["result"]["tools"]), 24)
        finally:
            server.shutdown()
            server.server_close()

    def test_call_list_mirrors_and_gates(self):
        server, base = _start(read_only=True)
        try:
            _, body = _post(
                base,
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "list_mirrors",
                            "arguments": {"org": "o", "repo": "r", "ref": "abc", "format": "plain"}}},
            )
            self.assertFalse(body["result"]["isError"])
            _, gated = _post(
                base,
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "gh_sync",
                            "arguments": {"org": "o", "repo": "r", "folder": "."}}},
            )
            self.assertTrue(gated["result"]["isError"])
        finally:
            server.shutdown()
            server.server_close()

    def test_protocol_errors_are_json(self):
        server, base = _start()
        try:
            _, body = _post(base, {"jsonrpc": "2.0", "id": 5, "method": "nope"})
            self.assertIn("error", body)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base.replace("/mcp", "/nope"), timeout=30)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
