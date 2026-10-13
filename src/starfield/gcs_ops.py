"""GCS operations via the `gcloud storage` CLI (Task 4).

Staging gzips compressible web assets in place-names (mtime=0,
level 9); uploads preserve filenames, set `Content-Encoding: gzip`
only on compressed objects, and verify every object with a public
GET (status/MIME/encoding/disposition/cache headers, byte length,
md5, gunzip-compare). All subprocess work goes through `core.run`
so unit tests can fake it; the verification HTTP layer goes through
the module-level `_http_get` hook for the same reason.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import core

#: Extensions gzipped during staging (level 9, mtime=0, in place-names).
COMPRESSIBLE_EXTS = frozenset(
    {".html", ".svg", ".js", ".mjs", ".css", ".json", ".wasm", ".map"}
)

#: Entry docs: always fresh, never transformed.
ENTRY_DOCS = frozenset({"index.html", "index.svg", "sw.js", "bootstrap-sw.js"})

#: Worker docs upload before entries but after dependencies. (They are a
#: subset of ENTRY_DOCS for cache purposes; ordering treats them apart so
#: an entry referencing the worker never goes live first.)
WORKER_DOCS = frozenset({"sw.js", "bootstrap-sw.js"})

#: `name-HASH.ext` (hash = 8+ alphanumerics) marks a hashed asset.
_HASHED_RE = re.compile(r"-[0-9A-Za-z]{8,}\.[^./]+$")

_MIME_TABLE = {
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".wasm": "application/wasm",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}

#: Substrings (lowercased) of a `gcloud storage` error meaning "missing".
_NOT_FOUND_HINTS = ("not found", "404", "does not exist", "no such")


def _is_not_found(exc: core.CliError) -> bool:
    msg = exc.message.lower()
    return any(hint in msg for hint in _NOT_FOUND_HINTS)


def content_type(path: str) -> str:
    """Return the GCS `Content-Type` for `path` by extension."""
    return _MIME_TABLE.get(Path(path).suffix.lower(), "application/octet-stream")


def cache_control(name: str) -> str:
    """Return the `Cache-Control` value for an object `name`.

    Entry docs (by basename) are never cached; hashed assets
    (`*-HASH.*`) are immutable for a year; everything else is public
    for a day. Transforms are always disabled.
    """
    base = Path(name).name
    if base in ENTRY_DOCS:
        return "no-cache,no-transform"
    if _HASHED_RE.search(base):
        return "public,max-age=31536000,immutable,no-transform"
    return "public,max-age=86400,no-transform"


@dataclass
class Staged:
    """One file ready for upload: `data` is the exact stored bytes."""

    name: str  # object-relative POSIX path (filename preserved, no .gz)
    source: Path  # original file on disk
    content_type: str
    content_encoding: str | None  # "gzip" iff gzipped, else None
    cache_control: str
    data: bytes  # bytes as stored on GCS
    raw: bytes  # original source bytes (gunzip-compare reference)


def stage(folder: str) -> list[Staged]:
    """Walk `folder` and stage every file; compressible assets are
    gzip-9 (mtime=0) encoded, filenames preserved."""
    src = Path(folder)
    if not src.is_dir():
        raise core.CliError(f"folder not found: {folder}")
    staged: list[Staged] = []
    files = sorted(
        p for p in src.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(src).parts)
    )
    for path in files:
        rel = path.relative_to(src).as_posix()
        raw = path.read_bytes()
        ctype = content_type(path.name)
        encoding: str | None = None
        data = raw
        if path.suffix.lower() in COMPRESSIBLE_EXTS:
            data = gzip.compress(raw, compresslevel=9, mtime=0)
            encoding = "gzip"
        staged.append(
            Staged(
                name=rel,
                source=path,
                content_type=ctype,
                content_encoding=encoding,
                cache_control=cache_control(rel),
                data=data,
                raw=raw,
            )
        )
    return staged


def _rank(name: str) -> int:
    """Upload rank: dependencies (0), worker (1), entry (2)."""
    base = Path(name).name
    if base in WORKER_DOCS:
        return 1
    if base in ENTRY_DOCS:
        return 2
    return 0


def _object_name(prefix: str, name: str) -> str:
    clean = prefix.strip("/")
    return f"{clean}/{name}" if clean else name


def _public_url(bucket: str, obj: str) -> str:
    return f"https://storage.googleapis.com/{bucket}/{urllib.parse.quote(obj, safe='/')}"


def _http_get(url: str, timeout: float = 60) -> tuple[int, dict, bytes]:
    """GET `url` with identity encoding; return (status, headers, body).

    Lowercases header names. HTTP errors surface as their status/body;
    connection-level failures raise `core.CliError`.
    """
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body
    except Exception as exc:
        raise core.CliError(f"GET {url}: {exc}") from exc


def _verify_object(
    bucket: str,
    obj: str,
    item: Staged,
    cache: str,
    timeout: float,
) -> None:
    """Public-GET `obj` and compare every header/byte against `item`."""
    url = _public_url(bucket, obj)
    status, headers, body = _http_get(url, timeout=timeout)
    if status != 200:
        raise core.CliError(f"verify {url}: HTTP {status}")
    mime = headers.get("content-type", "").split(";")[0].strip()
    if mime != item.content_type:
        raise core.CliError(
            f"verify {url}: content-type {mime!r} != {item.content_type!r}"
        )
    encoding = headers.get("content-encoding", "") or None
    if encoding != item.content_encoding:
        raise core.CliError(
            f"verify {url}: content-encoding {encoding!r} != "
            f"{item.content_encoding!r}"
        )
    if headers.get("content-disposition", "") != "inline":
        raise core.CliError(
            f"verify {url}: content-disposition "
            f"{headers.get('content-disposition', '')!r} != 'inline'"
        )
    if headers.get("cache-control", "") != cache:
        raise core.CliError(
            f"verify {url}: cache-control {headers.get('cache-control', '')!r} "
            f"!= {cache!r}"
        )
    if "content-length" in headers and int(headers["content-length"]) != len(
        item.data
    ):
        raise core.CliError(
            f"verify {url}: content-length {headers['content-length']} "
            f"!= {len(item.data)}"
        )
    if hashlib.md5(body).digest() != hashlib.md5(item.data).digest():
        raise core.CliError(f"verify {url}: md5 mismatch")
    try:
        decoded = gzip.decompress(body) if item.content_encoding == "gzip" else body
    except Exception as exc:
        raise core.CliError(f"verify {url}: gunzip failed: {exc}") from exc
    if decoded != item.raw:
        raise core.CliError(f"verify {url}: body bytes differ from source")


def _bucket_describe(bucket: str, timeout: float) -> dict | None:
    """Return bucket metadata, or None when the bucket does not exist."""
    try:
        out = core.run(
            ["gcloud", "storage", "buckets", "describe", f"gs://{bucket}",
             "--format=json"],
            timeout=timeout,
        )
    except core.CliError as exc:
        if _is_not_found(exc):
            return None
        raise
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(
            f"gcloud storage buckets describe: unparseable output: {exc}"
        ) from exc


def _object_generation(bucket: str, obj: str, timeout: float) -> str | None:
    """Return the live generation of `obj`, or None when missing."""
    try:
        out = core.run(
            ["gcloud", "storage", "objects", "describe", f"gs://{bucket}/{obj}",
             "--format=json"],
            timeout=timeout,
        )
    except core.CliError as exc:
        if _is_not_found(exc):
            return None
        raise
    try:
        return str(json.loads(out).get("generation", ""))
    except json.JSONDecodeError as exc:
        raise core.CliError(
            f"gcloud storage objects describe: unparseable output: {exc}"
        ) from exc


def upload(
    bucket: str,
    folder: str,
    prefix: str = "",
    cache_control: str | None = None,
    create_only: bool = False,
    dry_run: bool = False,
    timeout: float = 300,
) -> dict:
    """Gzip-upload `folder` to `gs://bucket/[prefix]`; return byte counts.

    Objects upload in dependency/worker/entry order with
    `if-generation-match` preconditions; every object is verified with
    a public GET. `cache_control` overrides per-file values;
    `create_only` refuses to overwrite; `dry_run` stages without any
    subprocess or network call. Raises `core.CliError` on empty
    folders, missing buckets, existing objects under `create_only`,
    and verification mismatches.
    """
    src = Path(folder)
    if not src.is_dir():
        raise core.CliError(f"folder not found: {folder}")
    staged = stage(folder)
    if not staged:
        raise core.CliError(f"refusing to upload empty folder: {folder}")
    if cache_control is not None:
        for item in staged:
            item.cache_control = cache_control
    ordered = sorted(staged, key=lambda s: (_rank(s.name), s.name))
    objects = [_object_name(prefix, item.name) for item in ordered]

    def summary() -> dict:
        files = [
            {
                "name": obj,
                "content_type": item.content_type,
                "content_encoding": item.content_encoding,
                "cache_control": item.cache_control,
                "raw_bytes": len(item.raw),
                "stored_bytes": len(item.data),
                "md5": hashlib.md5(item.data).hexdigest(),
            }
            for obj, item in zip(objects, ordered)
        ]
        return {
            "raw_bytes": sum(len(item.raw) for item in ordered),
            "stored_bytes": sum(len(item.data) for item in ordered),
            "files": files,
        }

    if dry_run:
        return summary()

    # Fail before uploading anything when the bucket is missing.
    if _bucket_describe(bucket, timeout) is None:
        raise core.CliError(f"bucket not found: gs://{bucket}")

    # Resolve generation-match preconditions up front so `create_only`
    # refuses before any upload starts.
    generations: list[str] = []
    for obj in objects:
        gen = _object_generation(bucket, obj, timeout)
        if create_only and gen is not None:
            raise core.CliError(
                f"gs://{bucket}/{obj} already exists "
                "(create-only: refusing to overwrite)"
            )
        generations.append(gen if gen else "0")

    with tempfile.TemporaryDirectory(prefix="starfield-gcs-upload-") as tmp:
        for index, (obj, item, gen) in enumerate(
            zip(objects, ordered, generations)
        ):
            local = Path(tmp) / f"{index}.bin"
            local.write_bytes(item.data)
            cmd = [
                "gcloud", "storage", "cp", str(local),
                f"gs://{bucket}/{obj}",
                f"--content-type={item.content_type}",
                f"--cache-control={item.cache_control}",
                "--content-disposition=inline",
                f"--if-generation-match={gen}",
            ]
            if item.content_encoding:
                cmd.append(f"--content-encoding={item.content_encoding}")
            core.run(cmd, timeout=timeout)
            _verify_object(bucket, obj, item, item.cache_control, timeout)
    return summary()


def ensure_bucket(
    bucket: str,
    location: str = "us-central1",
    public: bool = False,
    timeout: float = 60,
) -> dict:
    """Create `gs://bucket` when missing; return `{"created": bool}`.

    New buckets are regional (`location`), Standard class, uniform
    bucket-level access, public-access-prevention off, soft-delete
    disabled, no versioning/Autoclass. Public `allUsers:objectViewer`
    access is granted ONLY with `public=True`.
    """
    if _bucket_describe(bucket, timeout) is not None:
        return {"created": False}
    core.run(
        [
            "gcloud", "storage", "buckets", "create", f"gs://{bucket}",
            f"--location={location}",
            "--default-storage-class=Standard",
            "--uniform-bucket-level-access",
            "--no-public-access-prevention",
            "--soft-delete-duration=0",
            "--format=json",
        ],
        timeout=timeout,
    )
    if public:
        core.run(
            [
                "gcloud", "storage", "buckets", "add-iam-policy-binding",
                f"gs://{bucket}",
                "--member=allUsers",
                "--role=roles/storage.objectViewer",
            ],
            timeout=timeout,
        )
    return {"created": True}


def list_buckets(timeout: float = 60) -> list[dict]:
    """List buckets as parsed `gcloud storage buckets list --format=json`."""
    out = core.run(
        ["gcloud", "storage", "buckets", "list", "--format=json"],
        timeout=timeout,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(
            f"gcloud storage buckets list: unparseable output: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise core.CliError(
            f"gcloud storage buckets list: unexpected output: {out[:200]}"
        )
    return data


def list_objects(bucket: str, timeout: float = 60) -> list[dict]:
    """List objects in `bucket` as parsed `--format=json` dicts."""
    out = core.run(
        ["gcloud", "storage", "objects", "list", "--format=json",
         f"gs://{bucket}"],
        timeout=timeout,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(
            f"gcloud storage objects list: unparseable output: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise core.CliError(
            f"gcloud storage objects list: unexpected output: {out[:200]}"
        )
    return data
