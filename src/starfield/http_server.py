"""Stdlib HTTP adapter (Task 6).

Thin adapter over `gh_ops`, `gcs_ops`, `verify`, and `links`: JSON
in/out with a `{"ok": bool, ...}` envelope, unknown routes/methods
answered 404 JSON (never an HTML stack).
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gcs_ops, gh_ops, links, verify

#: Mutating routes that refuse without `allow_create=True`.
_CREATE_ROUTES = ("/v1/ensure-repo", "/v1/ensure-bucket")


def _missing(body: dict, *names: str) -> str | None:
    for name in names:
        if not body.get(name):
            return name
    return None


def _handler_class(allow_create: bool, default_timeout: float):
    """Build a request-handler class bound to server settings."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "starfield/1"

        def log_message(self, *args):  # silence output
            pass

        # -- response helpers -------------------------------------

        def _send_json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _not_found(self) -> None:
            self._send_json(
                404,
                {"ok": False, "error": f"not found: {self.command} {self.path}"},
            )

        def _read_json(self) -> tuple[dict | None, str | None]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}, None
            try:
                body = json.loads(raw.decode())
            except Exception:
                return None, "invalid JSON body"
            if not isinstance(body, dict):
                return None, "JSON body must be an object"
            return body, None

        # -- entry points -----------------------------------------

        def do_GET(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                query = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(parsed.query).items()
                }
                if parsed.path == "/healthz":
                    self._send_json(200, {"ok": True})
                elif parsed.path == "/v1/gh-list":
                    self._handle_gh_list(query)
                elif parsed.path == "/v1/gcs-list":
                    self._handle_gcs_list(query)
                else:
                    self._not_found()
            except BrokenPipeError:
                pass
            except Exception as exc:  # never leak an HTML stack
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/v1/gh-sync":
                    body, err = self._read_json()
                    if err is not None:
                        self._send_json(400, {"ok": False, "error": err})
                    else:
                        self._handle_gh_sync(body)
                elif path == "/v1/gcs-upload":
                    body, err = self._read_json()
                    if err is not None:
                        self._send_json(400, {"ok": False, "error": err})
                    else:
                        self._handle_gcs_upload(body)
                elif path in _CREATE_ROUTES:
                    body, err = self._read_json()
                    if err is not None:
                        self._send_json(400, {"ok": False, "error": err})
                    elif not allow_create:
                        name = path.rsplit("/", 1)[-1]
                        self._send_json(
                            403,
                            {
                                "ok": False,
                                "error": f"{name} requires --allow-create",
                            },
                        )
                    elif path == "/v1/ensure-repo":
                        self._handle_ensure_repo(body)
                    else:
                        self._handle_ensure_bucket(body)
                elif path == "/v1/verify":
                    body, err = self._read_json()
                    if err is not None:
                        self._send_json(400, {"ok": False, "error": err})
                    else:
                        self._handle_verify(body)
                elif path == "/v1/links":
                    body, err = self._read_json()
                    if err is not None:
                        self._send_json(400, {"ok": False, "error": err})
                    else:
                        self._handle_links(body)
                else:
                    self._not_found()
            except BrokenPipeError:
                pass
            except Exception as exc:  # never leak an HTML stack
                self._send_json(500, {"ok": False, "error": str(exc)})

        # quiet non-GET/POST verbs with the same JSON 404
        do_PUT = _not_found
        do_DELETE = _not_found
        do_PATCH = _not_found
        do_HEAD = _not_found
        do_OPTIONS = _not_found

        # -- route handlers ---------------------------------------

        def _timeout(self, body: dict) -> float:
            try:
                return float(body.get("timeout", default_timeout))
            except (TypeError, ValueError):
                return float(default_timeout)

        def _qtimeout(self, query: dict) -> float:
            try:
                return float(query.get("timeout", default_timeout))
            except (TypeError, ValueError):
                return float(default_timeout)

        def _handle_gh_sync(self, body: dict) -> None:
            missing = _missing(body, "org", "repo", "folder")
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            try:
                out = gh_ops.sync(
                    org=body["org"],
                    repo=body["repo"],
                    folder=body["folder"],
                    branch=body.get("branch", "main"),
                    message=body.get("message"),
                    delete=body.get("delete", True),
                    dry_run=body.get("dry_run", False),
                    workdir=body.get("workdir"),
                    timeout=self._timeout(body),
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, **out})

        def _handle_gcs_upload(self, body: dict) -> None:
            missing = _missing(body, "bucket", "folder")
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            try:
                out = gcs_ops.upload(
                    bucket=body["bucket"],
                    folder=body["folder"],
                    prefix=body.get("prefix", ""),
                    cache_control=body.get("cache_control"),
                    create_only=body.get("create_only", False),
                    dry_run=body.get("dry_run", False),
                    timeout=self._timeout(body),
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, **out})

        def _handle_ensure_repo(self, body: dict) -> None:
            missing = _missing(body, "org", "repo")
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            try:
                out = gh_ops.ensure_repo(
                    org=body["org"],
                    repo=body["repo"],
                    public=body.get("public", False),
                    timeout=self._timeout(body),
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, **out})

        def _handle_ensure_bucket(self, body: dict) -> None:
            missing = _missing(body, "bucket")
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            try:
                out = gcs_ops.ensure_bucket(
                    bucket=body["bucket"],
                    location=body.get("location", "us-central1"),
                    public=body.get("public", False),
                    timeout=self._timeout(body),
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, **out})

        def _handle_verify(self, body: dict) -> None:
            missing = _missing(body, "url")
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            out = verify.check(
                url=body["url"],
                sha256=body.get("sha256"),
                mime=body.get("mime"),
                min_bytes=body.get("min_bytes", 1),
                timeout=self._timeout(body),
            )
            self._send_json(200, dict(out))

        def _handle_links(self, body: dict) -> None:
            missing = _missing(body, "org", "repo", "ref")
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            fmt = body.get("format", "plain")
            try:
                text = links.mirror_urls(
                    body["org"],
                    body["repo"],
                    body["ref"],
                    body.get("file", "index.svg"),
                    format=fmt,
                    check=bool(body.get("check", False)),
                    timeout=self._timeout(body),
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, "text": text, "format": fmt})

        def _handle_gh_list(self, query: dict) -> None:
            if not query.get("org"):
                self._send_json(400, {"ok": False, "error": "missing org"})
                return
            timeout = self._qtimeout(query)
            try:
                repos = gh_ops.list_repos(
                    org=query["org"], timeout=timeout
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, "repos": repos})

        def _handle_gcs_list(self, query: dict) -> None:
            timeout = self._qtimeout(query)
            try:
                if query.get("bucket"):
                    objects = gcs_ops.list_objects(
                        query["bucket"], timeout=timeout
                    )
                    self._send_json(200, {"ok": True, "objects": objects})
                else:
                    buckets = gcs_ops.list_buckets(timeout=timeout)
                    self._send_json(200, {"ok": True, "buckets": buckets})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return

    Handler.allow_create = allow_create  # introspection aid for tests
    Handler.default_timeout = default_timeout
    return Handler


def build_server(
    host: str,
    port: int,
    allow_create: bool = False,
    timeout: float = 300,
    concurrency: int = 8,  # accepted for CLI parity; server threads per conn
) -> ThreadingHTTPServer:
    """Create (but do not start) a threaded HTTP server."""
    _ = concurrency
    server = ThreadingHTTPServer(
        (host, port), _handler_class(allow_create, timeout)
    )
    server.daemon_threads = True
    return server


def serve(
    port: int,
    allow_create: bool = False,
    timeout: float = 300,
    concurrency: int = 8,
) -> None:
    """Serve forever on `port` (blocks until interrupted)."""
    server = build_server(
        "0.0.0.0", port, allow_create=allow_create, timeout=timeout,
        concurrency=concurrency,
    )
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
