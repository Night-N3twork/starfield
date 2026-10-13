"""Streamable-HTTP transport for the MCP server.

Thin adapter over `mcp_server._handle_message`: POST a JSON-RPC message
(or batch) to /mcp, get the JSON-RPC response back as plain JSON
(no SSE streaming — allowed by the Streamable HTTP spec). Same tools,
same gates; only the transport differs from stdio.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import mcp_server


def _error(status: int, code: int, message: str) -> tuple[int, dict[str, Any]]:
    return status, {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": code, "message": message},
    }


def _dispatch(
    msg: Any,
    *,
    org: str | None,
    bucket: str | None,
    allow_create: bool,
    read_only: bool,
    timeout: float,
) -> tuple[int, Any]:
    """Route one POST body; returns (http_status, response_json)."""
    if isinstance(msg, list):
        out = []
        for item in msg:
            respond, payload = mcp_server._handle_message(
                item, org=org, bucket=bucket, allow_create=allow_create,
                read_only=read_only, timeout=timeout,
            )
            if respond and payload is not None:
                out.append({"jsonrpc": "2.0", "id": item.get("id") if isinstance(item, dict) else None, **payload})
        return 200, out
    respond, payload = mcp_server._handle_message(
        msg, org=org, bucket=bucket, allow_create=allow_create,
        read_only=read_only, timeout=timeout,
    )
    if not respond or payload is None:
        return 202, {}
    id_ = msg.get("id") if isinstance(msg, dict) else None
    return 200, {"jsonrpc": "2.0", "id": id_, **payload}


def build_server(
    port: int = 8765,
    *,
    org: str | None = None,
    bucket: str | None = None,
    allow_create: bool = False,
    read_only: bool = False,
    timeout: float = 300,
) -> ThreadingHTTPServer:
    """Build (not start) the MCP HTTP server."""
    opts = {
        "org": org, "bucket": bucket, "allow_create": allow_create,
        "read_only": read_only, "timeout": float(timeout),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "starfield-mcp-http"

        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/mcp":
                status, payload = _error(404, -32000, f"unknown path: {self.path}")
                self._send(status, payload)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                length = 0
            try:
                msg = json.loads(self.rfile.read(length) or b"null")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                status, payload = _error(200, -32700, f"parse error: {exc}")
                self._send(status, payload)
                return
            status, payload = _dispatch(msg, **opts)
            self._send(status, payload)

        def do_GET(self) -> None:
            if self.path == "/mcp":
                status, payload = _error(405, -32000, "method not allowed: use POST")
            else:
                status, payload = _error(404, -32000, f"unknown path: {self.path}")
            self._send(status, payload)

        def do_PUT(self) -> None:
            status, payload = _error(404, -32000, f"unknown path: {self.path}")
            self._send(status, payload)

        do_DELETE = do_PUT
        do_PATCH = do_PUT
        do_OPTIONS = do_PUT

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_mcp_http(
    port: int = 8765,
    *,
    org: str | None = None,
    bucket: str | None = None,
    allow_create: bool = False,
    read_only: bool = False,
    timeout: float = 300,
) -> ThreadingHTTPServer:
    """Build and serve the MCP HTTP endpoint forever (blocks)."""
    server = build_server(
        port, org=org, bucket=bucket, allow_create=allow_create,
        read_only=read_only, timeout=timeout,
    )
    server.serve_forever()
    return server
