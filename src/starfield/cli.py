"""Argparse CLI skeleton (Task 1). Subcommands are stubs until later tasks."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import analytics, billing, core, deploy_ops, gcs_ops, gh_ops, http_server, links, mcp_server, verify


def _run_read_op(args: argparse.Namespace) -> int:
    """Run a read-only op described by `args._op(name, params)`; print JSON."""
    try:
        func = getattr(args._mod, args._fname)
        out = func(**args._params(args), timeout=args.timeout)
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


def _read_parser(
    sub: argparse._SubParsersAction,
    name: str,
    help_text: str,
    mod,
    fname: str,
    required: tuple,
    options: tuple,
    renames: dict | None = None,
) -> None:
    """Register a read-only subcommand from a spec row.

    `options` is ((flag, type, default), ...); `--timeout` always added.
    `renames` maps CLI flags to function parameter names. The op function
    is resolved from `(mod, fname)` at call time so tests can patch ops.
    """
    renames = renames or {}
    p = sub.add_parser(name, help=help_text)
    for field in required:
        if field == "urls":
            p.add_argument("--urls", nargs="+", required=True)
        else:
            p.add_argument("--" + field.replace("_", "-"), required=True)
    for flag, typ, default in options:
        if isinstance(default, bool):
            p.add_argument("--" + flag, action="store_true")
        else:
            p.add_argument("--" + flag, type=typ, default=default)
    p.add_argument("--timeout", type=int, default=300)

    def params(args: argparse.Namespace, _req=required,
               _opts=options, _ren=renames) -> dict:
        kwargs = {field: getattr(args, field) for field in _req}
        for flag, _typ, default in _opts:
            kwargs[flag] = getattr(args, flag)
        return { _ren.get(k, k): v for k, v in kwargs.items() }

    p.set_defaults(func=_run_read_op, _mod=mod, _fname=fname,
                   _params=lambda args, _p=params: _p(args))


#: Read-only inspection commands: (name, help, module, func, required,
#: options[, renames]).
_READ_COMMANDS = (
    ("gcs-usage", "bucket usage and traffic", analytics, "usage",
     ("project",), (("bucket", str, None), ("days", int, 30))),
    ("gcs-bucket-stats", "one bucket plus stored bytes/object count",
     analytics, "bucket_stats", ("project", "bucket"), (("days", int, 30),)),
    ("gcs-object-stats", "size/metadata/cache config for one object",
     analytics, "object_stats", ("bucket", "object"), ()),
    ("gcs-traffic-history", "daily sent bytes and requests",
     analytics, "traffic_history", ("project",),
     (("bucket", str, None), ("days", int, 30))),
    ("gcs-request-stats", "requests by method and response code",
     analytics, "request_stats", ("project",),
     (("bucket", str, None), ("days", int, 30))),
    ("gcs-top-objects", "most-downloaded objects from usage logs",
     analytics, "top_objects", ("project", "bucket"),
     (("days", int, 7), ("limit", int, 10))),
    ("gcs-audit-logs", "recent storage audit/admin events",
     analytics, "audit_logs", ("project",),
     (("bucket", str, None), ("days", int, 7), ("limit", int, 50))),
    ("gcs-billing", "billing export status plus cost summary",
     billing, "gcs_billing", ("project",),
     (("dataset", str, "billing_export"), ("days", int, 30))),
    ("gcp-cost-summary", "total cost/credits/net per service",
     billing, "cost_summary", ("project",),
     (("dataset", str, "billing_export"), ("days", int, 30))),
    ("gcp-cost-breakdown", "cost grouped by service/sku/project/day",
     billing, "cost_breakdown", ("project",),
     (("dataset", str, "billing_export"), ("days", int, 30),
      ("group_by", str, "service"))),
    ("gcp-pricing", "search the public billing catalog",
     billing, "pricing", ("filter",), (), {"filter": "filter_text"}),
    ("gcp-cost-forecast", "project traffic forward and price it",
     billing, "forecast", ("project",),
     (("bucket", str, None), ("days_history", int, 30),
      ("days_ahead", int, 30), ("rate_usd_per_gib", float, None),
      ("free_gib", float, billing.FREE_EGRESS_GIB))),
    ("deploy-diff", "preview local folder vs bucket differences",
     deploy_ops, "deploy_diff", ("folder", "bucket"), (("prefix", str, ""),)),
    ("deploy-health", "probe URLs for status/latency/hash",
     deploy_ops, "deploy_health", ("urls",),
     (("sha256", str, None),)),
    ("gcs-public-check", "report anonymous read access for a bucket",
     deploy_ops, "public_check", ("bucket",), ()),
    ("gcs-iam-inspect", "dump a bucket's IAM bindings",
     deploy_ops, "iam_inspect", ("bucket",), ()),
)


def _stub(command: str) -> int:
    result = core.not_implemented(command)
    print(result.error, file=sys.stderr)
    return 1


def _run_gh_sync(args: argparse.Namespace) -> int:
    try:
        out = gh_ops.sync(
            org=args.org,
            repo=args.repo,
            folder=args.folder,
            branch=args.branch,
            message=args.message,
            delete=not args.no_delete,
            dry_run=args.dry_run,
            workdir=args.workdir,
            timeout=args.timeout,
        )
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _run_ensure_repo(args: argparse.Namespace) -> int:
    try:
        out = gh_ops.ensure_repo(
            org=args.org, repo=args.repo, public=args.public, timeout=args.timeout
        )
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _run_gcs_upload(args: argparse.Namespace) -> int:
    try:
        out = gcs_ops.upload(
            bucket=args.bucket,
            folder=args.folder,
            prefix=args.prefix,
            cache_control=args.cache_control,
            create_only=args.create_only,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _run_ensure_bucket(args: argparse.Namespace) -> int:
    try:
        out = gcs_ops.ensure_bucket(
            bucket=args.bucket,
            location=args.location,
            public=args.public,
            timeout=args.timeout,
        )
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _run_gcs_list(args: argparse.Namespace) -> int:
    try:
        if args.bucket is None:
            out = gcs_ops.list_buckets(timeout=args.timeout)
        else:
            out = gcs_ops.list_objects(args.bucket, timeout=args.timeout)
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    out = verify.check(
        url=args.url,
        sha256=args.sha256,
        mime=args.mime,
        min_bytes=args.min_bytes if args.min_bytes is not None else 1,
    )
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 1


def _run_gh_list(args: argparse.Namespace) -> int:
    try:
        out = gh_ops.list_repos(org=args.org, timeout=args.timeout)
    except core.CliError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _run_links(args: argparse.Namespace) -> int:
    try:
        text = links.mirror_urls(
            args.org,
            args.repo,
            args.ref,
            args.file,
            format=args.format,
            check=args.check,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(text)
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    http_server.serve(
        port=args.port,
        allow_create=args.allow_create,
        timeout=args.timeout,
        concurrency=args.concurrency,
    )
    return 0


def _run_mcp(args: argparse.Namespace) -> int:
    if getattr(args, "transport", "stdio") == "http":
        from . import mcp_http

        mcp_http.serve_mcp_http(
            port=args.port,
            org=args.org,
            bucket=args.bucket,
            allow_create=args.allow_create,
            read_only=args.read_only,
            timeout=args.timeout,
        )
        return 0
    mcp_server.run(
        org=args.org,
        bucket=args.bucket,
        allow_create=args.allow_create,
        read_only=args.read_only,
        timeout=args.timeout,
        concurrency=args.concurrency,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="starfield",
        description="Push folders to GitHub repos and/or GCS buckets, "
        "then verify and list mirror links.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("gh-sync", help="push a folder to org/repo@branch")
    p.add_argument("--org", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--message", default=None)
    p.add_argument("--no-delete", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--workdir", default=None)
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=_run_gh_sync)

    p = sub.add_parser("gcs-upload", help="gzip-upload a folder to a bucket")
    p.add_argument("--bucket", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--prefix", default="")
    p.add_argument("--cache-control", default=None)
    p.add_argument("--create-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=_run_gcs_upload)

    p = sub.add_parser("ensure-repo", help="create org/repo if missing")
    p.add_argument("--org", required=True)
    p.add_argument("--repo", required=True)
    vis = p.add_mutually_exclusive_group()
    vis.add_argument("--public", action="store_true")
    vis.add_argument("--private", action="store_true")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=_run_ensure_repo)

    p = sub.add_parser("ensure-bucket", help="create bucket if missing")
    p.add_argument("--bucket", required=True)
    p.add_argument("--location", default="us-central1")
    p.add_argument("--public", action="store_true")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=_run_ensure_bucket)

    p = sub.add_parser("gcs-list", help="list buckets or object summary")
    p.add_argument("--bucket", default=None)
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=_run_gcs_list)

    p = sub.add_parser("gh-list", help="list repos in an org")
    p.add_argument("--org", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=_run_gh_list)

    p = sub.add_parser("verify", help="GET a URL and compare sha256")
    p.add_argument("--url", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--mime", default=None)
    p.add_argument("--min-bytes", type=int, default=None)
    p.set_defaults(func=_run_verify)

    p = sub.add_parser("links", help="mirror URLs for org/repo@ref")
    p.add_argument("--org", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--file", default="index.svg")
    p.add_argument("--format", default="plain", choices=("plain", "md", "json"))
    p.add_argument("--check", action="store_true",
                   help="fetch each URL and drop unreachable hosts")
    p.set_defaults(func=_run_links)

    p = sub.add_parser("serve", help="run the HTTP server")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allow-create", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=8)
    p.set_defaults(func=_run_serve)

    p = sub.add_parser("mcp", help="run the MCP server (stdio or HTTP)")
    p.add_argument("--transport", default="stdio", choices=("stdio", "http"))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--org", default=None)
    p.add_argument("--bucket", default=None)
    p.add_argument("--allow-create", action="store_true")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=8)
    p.set_defaults(func=_run_mcp)

    for _name, _help, _mod, _fname, _required, _options, *_rest in _READ_COMMANDS:
        _read_parser(sub, _name, _help, _mod, _fname, _required, _options,
                     renames=_rest[0] if _rest else None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return int(args.func(args))
