"""MCP stdio server (Task 7).

Speaks JSON-RPC 2.0 over line-delimited stdio: one JSON request per
line on stdin, one JSON response per line on stdout. Thin adapter
over `gh_ops`, `gcs_ops`, `verify`, and `links`, mirroring the HTTP
adapter's envelopes and gates:

- `initialize` answers the handshake (server info + tool capability).
- `tools/list` names the eight tools with JSON input schemas.
- `tools/call` runs one tool; the outcome is an MCP content
  envelope (`{"content": [{"type": "text", ...}], "isError": bool}`).
  Tool-level failures (missing args, `CliError`, gates) are
  `isError` results, not JSON-RPC errors; only protocol problems
  (unknown method/tool, malformed JSON) are JSON-RPC errors.
- Mutating tools (`gh_sync`, `gcs_upload`, `ensure_repo`,
  `ensure_bucket`) refuse with an error result under `read_only`.
- `ensure_repo`/`ensure_bucket` refuse without `allow_create`.
- `org`/`bucket` server defaults fill in per-call gaps.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from . import __version__, gcs_ops, gh_ops, links, verify

#: MCP protocol version this server handshakes with.
PROTOCOL_VERSION = "2024-11-05"

#: Tools that change remote state; refused under `read_only=True`.
MUTATING_TOOLS = frozenset(
    {"gh_sync", "gcs_upload", "ensure_repo", "ensure_bucket"}
)

#: Tools that create repos/buckets; refused unless `allow_create=True`.
CREATE_GATED_TOOLS = frozenset({"ensure_repo", "ensure_bucket"})


def _schema(
    properties: dict[str, dict[str, Any]], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


#: Tool catalogue, in stable presentation order.
TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "gh_sync",
        "description": "Push a folder to org/repo@branch via gh/git.",
        "inputSchema": _schema(
            {
                "org": {"type": "string"},
                "repo": {"type": "string"},
                "folder": {"type": "string"},
                "branch": {"type": "string"},
                "message": {"type": "string"},
                "delete": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "workdir": {"type": "string"},
                "timeout": {"type": "number"},
            },
            ["org", "repo", "folder"],
        ),
    },
    {
        "name": "gcs_upload",
        "description": "Gzip-upload a folder to a GCS bucket.",
        "inputSchema": _schema(
            {
                "bucket": {"type": "string"},
                "folder": {"type": "string"},
                "prefix": {"type": "string"},
                "cache_control": {"type": "string"},
                "create_only": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "timeout": {"type": "number"},
            },
            ["bucket", "folder"],
        ),
    },
    {
        "name": "ensure_repo",
        "description": "Create org/repo when missing (needs --allow-create).",
        "inputSchema": _schema(
            {
                "org": {"type": "string"},
                "repo": {"type": "string"},
                "public": {"type": "boolean"},
                "timeout": {"type": "number"},
            },
            ["org", "repo"],
        ),
    },
    {
        "name": "ensure_bucket",
        "description": "Create a GCS bucket when missing (needs --allow-create).",
        "inputSchema": _schema(
            {
                "bucket": {"type": "string"},
                "location": {"type": "string"},
                "public": {"type": "boolean"},
                "timeout": {"type": "number"},
            },
            ["bucket"],
        ),
    },
    {
        "name": "gcs_list",
        "description": "List buckets, or objects when bucket is given.",
        "inputSchema": _schema(
            {
                "bucket": {"type": "string"},
                "timeout": {"type": "number"},
            }
        ),
    },
    {
        "name": "gh_list",
        "description": "List repos in a GitHub org.",
        "inputSchema": _schema(
            {
                "org": {"type": "string"},
                "timeout": {"type": "number"},
            },
            ["org"],
        ),
    },
    {
        "name": "verify_deploy",
        "description": "GET a URL and compare sha256/mime/size.",
        "inputSchema": _schema(
            {
                "url": {"type": "string"},
                "sha256": {"type": "string"},
                "mime": {"type": "string"},
                "min_bytes": {"type": "integer"},
                "timeout": {"type": "number"},
            },
            ["url"],
        ),
    },
    {
        "name": "list_mirrors",
        "description": "Render mirror URLs for org/repo@ref (check=True verifies each live).",
        "inputSchema": _schema(
            {
                "org": {"type": "string"},
                "repo": {"type": "string"},
                "ref": {"type": "string"},
                "file": {"type": "string"},
                "format": {"type": "string"},
                "check": {"type": "boolean"},
                "timeout": {"type": "number"},
            },
            ["org", "repo", "ref"],
        ),
    },
]

_TOOL_NAMES = frozenset(t["name"] for t in TOOL_DEFS)


def _ok(data: Any) -> dict[str, Any]:
    """Wrap tool output in a success MCP content envelope."""
    return {
        "content": [{"type": "text", "text": json.dumps(data)}],
        "isError": False,
    }


def _err(message: str) -> dict[str, Any]:
    """Wrap a tool failure in an error MCP content envelope."""
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _timeout(args: dict[str, Any], default: float) -> float:
    try:
        return float(args.get("timeout", default))
    except (TypeError, ValueError):
        return float(default)


def _call_tool(
    name: str,
    args: dict[str, Any],
    *,
    org: str | None,
    bucket: str | None,
    allow_create: bool,
    read_only: bool,
    timeout: float,
) -> dict[str, Any]:
    """Run tool `name` with `args`; never raises (errors become results)."""
    if name in MUTATING_TOOLS and read_only:
        return _err(f"{name} is disabled under --read-only")
    if name in CREATE_GATED_TOOLS and not allow_create:
        return _err(f"{name.replace('_', '-')} requires --allow-create")
    try:
        if name == "gh_sync":
            for key in ("org", "repo", "folder"):
                if not (args.get(key) or (org if key == "org" else None)):
                    return _err(f"missing {key}")
            out = gh_ops.sync(
                org=args.get("org") or org,
                repo=args["repo"],
                folder=args["folder"],
                branch=args.get("branch", "main"),
                message=args.get("message"),
                delete=args.get("delete", True),
                dry_run=args.get("dry_run", False),
                workdir=args.get("workdir"),
                timeout=_timeout(args, timeout),
            )
            return _ok(out)
        if name == "gcs_upload":
            resolved_bucket = args.get("bucket") or bucket
            if not resolved_bucket:
                return _err("missing bucket")
            if not args.get("folder"):
                return _err("missing folder")
            out = gcs_ops.upload(
                bucket=resolved_bucket,
                folder=args["folder"],
                prefix=args.get("prefix", ""),
                cache_control=args.get("cache_control"),
                create_only=args.get("create_only", False),
                dry_run=args.get("dry_run", False),
                timeout=_timeout(args, timeout),
            )
            return _ok(out)
        if name == "ensure_repo":
            resolved_org = args.get("org") or org
            if not resolved_org:
                return _err("missing org")
            if not args.get("repo"):
                return _err("missing repo")
            out = gh_ops.ensure_repo(
                org=resolved_org,
                repo=args["repo"],
                public=args.get("public", False),
                timeout=_timeout(args, timeout),
            )
            return _ok(out)
        if name == "ensure_bucket":
            resolved_bucket = args.get("bucket") or bucket
            if not resolved_bucket:
                return _err("missing bucket")
            out = gcs_ops.ensure_bucket(
                bucket=resolved_bucket,
                location=args.get("location", "us-central1"),
                public=args.get("public", False),
                timeout=_timeout(args, timeout),
            )
            return _ok(out)
        if name == "gcs_list":
            resolved_bucket = args.get("bucket") or bucket
            if resolved_bucket:
                return _ok(
                    gcs_ops.list_objects(
                        resolved_bucket, timeout=_timeout(args, timeout)
                    )
                )
            return _ok(gcs_ops.list_buckets(timeout=_timeout(args, timeout)))
        if name == "gh_list":
            resolved_org = args.get("org") or org
            if not resolved_org:
                return _err("missing org")
            return _ok(
                gh_ops.list_repos(
                    org=resolved_org, timeout=_timeout(args, timeout)
                )
            )
        if name == "verify_deploy":
            if not args.get("url"):
                return _err("missing url")
            return _ok(
                verify.check(
                    url=args["url"],
                    sha256=args.get("sha256"),
                    mime=args.get("mime"),
                    min_bytes=args.get("min_bytes", 1),
                    timeout=_timeout(args, timeout),
                )
            )
        if name == "list_mirrors":
            resolved_org = args.get("org") or org
            if not resolved_org:
                return _err("missing org")
            for key in ("repo", "ref"):
                if not args.get(key):
                    return _err(f"missing {key}")
            fmt = args.get("format", "plain")
            try:
                text = links.mirror_urls(
                    resolved_org,
                    args["repo"],
                    args["ref"],
                    args.get("file", "index.html"),
                    format=fmt,
                    check=bool(args.get("check", False)),
                    timeout=_timeout(args, timeout),
                )
            except ValueError as exc:
                return _err(str(exc))
            return _ok({"text": text, "format": fmt})
    except Exception as exc:
        return _err(str(exc))
    return _err(f"unknown tool: {name}")


def _respond(
    stdout: TextIO, id_: Any, payload: dict[str, Any]
) -> None:
    msg = {"jsonrpc": "2.0", "id": id_, **payload}
    stdout.write(json.dumps(msg) + "\n")
    stdout.flush()


def _handle_message(
    msg: Any,
    *,
    org: str | None,
    bucket: str | None,
    allow_create: bool,
    read_only: bool,
    timeout: float,
) -> tuple[bool, dict[str, Any] | None]:
    """Handle one parsed message.

    Returns `(respond, payload)`; `respond` is False for notifications
    (no `id`), which get no reply.
    """
    if not isinstance(msg, dict):
        return True, {
            "error": {"code": -32600, "message": "invalid request"}
        }
    id_ = msg.get("id")
    if id_ is None:
        return False, None  # notification: no response
    method = msg.get("method")
    if method == "initialize":
        return True, {
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "starfield", "version": __version__},
            }
        }
    if method == "tools/list":
        return True, {"result": {"tools": TOOL_DEFS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return True, {
                "error": {"code": -32602, "message": "params must be an object"}
            }
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return True, {
                "error": {
                    "code": -32602,
                    "message": "arguments must be an object",
                }
            }
        if name not in _TOOL_NAMES:
            return True, {
                "error": {"code": -32602, "message": f"unknown tool: {name}"}
            }
        result = _call_tool(
            name,
            arguments,
            org=org,
            bucket=bucket,
            allow_create=allow_create,
            read_only=read_only,
            timeout=timeout,
        )
        return True, {"result": result}
    return True, {
        "error": {"code": -32601, "message": f"unknown method: {method}"}
    }


def run(
    org: str | None = None,
    bucket: str | None = None,
    allow_create: bool = False,
    read_only: bool = False,
    timeout: float = 300,
    concurrency: int = 8,  # accepted for CLI parity; stdio is serial
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Serve JSON-RPC over stdio until EOF (one request per line).

    `stdin`/`stdout` default to the process streams; tests pass
    in-memory pipes instead.
    """
    _ = concurrency
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    for line in inp:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _respond(
                out,
                None,
                {"error": {"code": -32700, "message": f"parse error: {exc}"}},
            )
            continue
        respond, payload = _handle_message(
            msg,
            org=org,
            bucket=bucket,
            allow_create=allow_create,
            read_only=read_only,
            timeout=float(timeout),
        )
        if respond and payload is not None:
            _respond(out, msg.get("id") if isinstance(msg, dict) else None,
                     payload)
