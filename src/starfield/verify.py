"""Deployment verification via public GET + hash compare (Task 5).

`check` fetches a URL with `urllib` (advertising gzip), gunzips when the
response says `Content-Encoding: gzip`, then asserts HTTP 200, an
optional MIME prefix, an optional minimum decoded size, and an optional
sha256 of the decoded body. Used for entry docs, post-upload checks,
and mirror spot-checks.

Failures are returned as `{"ok": False, ..., "error": ...}` dicts, not
raised, so the HTTP/MCP adapters can surface them unchanged.
"""

from __future__ import annotations

import gzip
import hashlib
import urllib.error
import urllib.request


def check(
    url: str,
    sha256: str | None = None,
    mime: str | None = None,
    min_bytes: int = 1,
    timeout: float = 60,
) -> dict:
    """GET `url` and compare against expectations.

    Returns `{"ok", "status", "bytes", "content_type"}`, plus `"error"`
    describing the first mismatch when `ok` is False. `bytes` counts
    decoded (post-gunzip) body bytes. `status` is 0 when no HTTP
    response was received at all.
    """
    try:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
                encoding = resp.headers.get("Content-Encoding", "")
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            status = exc.code
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            return {
                "ok": False,
                "status": status,
                "bytes": 0,
                "content_type": content_type,
                "error": f"GET {url}: HTTP {status}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "bytes": 0,
            "content_type": "",
            "error": f"GET {url}: {exc}",
        }
    if (encoding or "").lower() == "gzip":
        try:
            body = gzip.decompress(raw)
        except Exception as exc:
            return {
                "ok": False,
                "status": status,
                "bytes": 0,
                "content_type": content_type,
                "error": f"GET {url}: gunzip failed: {exc}",
            }
    else:
        body = raw
    if status != 200:
        return {
            "ok": False,
            "status": status,
            "bytes": len(body),
            "content_type": content_type,
            "error": f"GET {url}: HTTP {status}",
        }
    if len(body) < min_bytes:
        return {
            "ok": False,
            "status": status,
            "bytes": len(body),
            "content_type": content_type,
            "error": f"GET {url}: too few bytes: {len(body)} < {min_bytes}",
        }
    if mime is not None:
        base = content_type.split(";")[0].strip()
        if not base.startswith(mime):
            return {
                "ok": False,
                "status": status,
                "bytes": len(body),
                "content_type": content_type,
                "error": f"GET {url}: content-type {base!r} != {mime!r}",
            }
    if sha256 is not None:
        actual = hashlib.sha256(body).hexdigest()
        if actual.lower() != sha256.lower():
            return {
                "ok": False,
                "status": status,
                "bytes": len(body),
                "content_type": content_type,
                "error": f"GET {url}: sha256 mismatch: got {actual} want {sha256}",
            }
    return {
        "ok": True,
        "status": status,
        "bytes": len(body),
        "content_type": content_type,
    }
