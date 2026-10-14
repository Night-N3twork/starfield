"""Stdlib HTTP adapter (Task 6).

Thin adapter over `gh_ops`, `gcs_ops`, `verify`, and `links`: JSON
in/out with a `{"ok": bool, ...}` envelope, unknown routes/methods
answered 404 JSON (never an HTML stack).
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import analytics, billing, deploy_ops, gcs_ops, gh_ops, links, verify

#: Mutating routes that refuse without `allow_create=True`.
_CREATE_ROUTES = ("/v1/ensure-repo", "/v1/ensure-bucket")

#: Read-only routes as (path, module, func-name, required, defaults, renames).
#: Modules (not function objects) so tests can patch op functions.
_READ_ROUTES: dict[str, tuple] = {
    "/v1/gcs-usage": (analytics, "usage", ("project",),
                      {"bucket": None, "days": 30}, {}),
    "/v1/gcs-bucket-stats": (analytics, "bucket_stats", ("project", "bucket"),
                             {"days": 30}, {}),
    "/v1/gcs-object-stats": (analytics, "object_stats", ("bucket", "object"),
                             {}, {}),
    "/v1/gcs-traffic-history": (analytics, "traffic_history", ("project",),
                                {"bucket": None, "days": 30}, {}),
    "/v1/gcs-request-stats": (analytics, "request_stats", ("project",),
                              {"bucket": None, "days": 30}, {}),
    "/v1/gcs-top-objects": (analytics, "top_objects", ("project", "bucket"),
                            {"days": 7, "limit": 10}, {}),
    "/v1/gcs-audit-logs": (analytics, "audit_logs", ("project",),
                           {"bucket": None, "days": 7, "limit": 50}, {}),
    "/v1/gcs-billing": (billing, "export_status", ("project",),
                        {"dataset": "billing_export"}, {}),
    "/v1/gcp-cost-summary": (billing, "cost_summary", ("project",),
                             {"dataset": "billing_export", "days": 30}, {}),
    "/v1/gcp-cost-breakdown": (billing, "cost_breakdown", ("project",),
                               {"dataset": "billing_export", "days": 30,
                                "group_by": "service"}, {}),
    "/v1/gcp-pricing": (billing, "pricing", ("filter",), {},
                        {"filter": "filter_text"}),
    "/v1/gcp-cost-forecast": (billing, "forecast", ("project",),
                              {"bucket": None, "days_history": 30,
                               "days_ahead": 30, "rate_usd_per_gib": None,
                               "free_gib": billing.FREE_EGRESS_GIB}, {}),
    "/v1/deploy-diff": (deploy_ops, "deploy_diff", ("folder", "bucket"),
                        {"prefix": ""}, {}),
    "/v1/deploy-health": (deploy_ops, "deploy_health", ("urls",),
                          {"sha256": None}, {}),
    "/v1/gcs-public-check": (deploy_ops, "public_check", ("bucket",), {}, {}),
    "/v1/gcs-iam-inspect": (deploy_ops, "iam_inspect", ("bucket",), {}, {}),
}


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
                elif path in _READ_ROUTES:
                    body, err = self._read_json()
                    if err is not None:
                        self._send_json(400, {"ok": False, "error": err})
                    else:
                        self._handle_read(path, body)
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

        def _handle_read(self, path: str, body: dict) -> None:
            mod, fname, required, defaults, renames = _READ_ROUTES[path]
            func = getattr(mod, fname)  # resolve late so tests can patch ops
            missing = _missing(body, *required)
            if missing:
                self._send_json(
                    400, {"ok": False, "error": f"missing {missing}"}
                )
                return
            kwargs = dict(defaults)
            for key in required:
                kwargs[renames.get(key, key)] = body[key]
            for key, value in body.items():
                if key in ("timeout",):
                    continue
                kwargs[renames.get(key, key)] = value
            try:
                out = func(**kwargs, timeout=self._timeout(body))
            except TypeError as exc:
                self._send_json(
                    400, {"ok": False, "error": f"bad arguments: {exc}"}
                )
                return
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, **out})

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
