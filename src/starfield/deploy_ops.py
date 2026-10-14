"""Read-only deploy checks: diff, health, public access, IAM.

Nothing here mutates cloud state. `deploy_diff` previews what an upload
would change; `deploy_health` probes URLs; `public_check`/`iam_inspect`
read bucket policy.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import time

from . import core, gcs_ops, verify


def deploy_diff(
    folder: str, bucket: str, prefix: str = "", timeout: float = 300
) -> dict:
    """Compare a local folder against bucket objects.

    Returns names (bucket-relative) in `missing` (local-only), `changed`
    (md5 differs), `unchanged`, and `extra` (bucket-only).
    """
    staged = {s.name: s for s in gcs_ops.stage(folder)}
    out = core.run(
        ["gcloud", "storage", "objects", "list", f"gs://{bucket}/{prefix}",
         "--format=json(name,size,md5_hash)"],
        timeout=timeout,
    )
    try:
        remote = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(f"objects list: unparseable output: {exc}") from exc
    if not isinstance(remote, list):
        remote = []
    pre = prefix.strip("/")
    missing, changed, unchanged = [], [], []
    seen = set()
    for item in staged.values():
        key = f"{pre}/{item.name}" if pre else item.name
        seen.add(key)
        want = base64.b64encode(hashlib.md5(item.data).digest()).decode()
        match = next((o for o in remote if o.get("name") == key), None)
        if match is None:
            missing.append(key)
        elif match.get("md5_hash") != want:
            changed.append(key)
        else:
            unchanged.append(key)
    extra = sorted(
        o.get("name", "") for o in remote
        if o.get("name") not in seen and o.get("name")
    )
    return {
        "bucket": bucket,
        "prefix": pre,
        "missing": sorted(missing),
        "changed": sorted(changed),
        "unchanged": sorted(unchanged),
        "extra": extra,
        "source": "local stage vs gcloud objects list md5 (live)",
    }


def deploy_health(
    urls: list,
    sha256: str | None = None,
    timeout: float = 60,
    workers: int = 8,
) -> dict:
    """Probe URLs concurrently: status, latency, optional hash compare."""
    def probe(url: str) -> dict:
        start = time.monotonic()
        try:
            out = verify.check(url, sha256=sha256, timeout=timeout)
        except Exception as exc:
            return {"url": url, "ok": False, "status": 0, "ms": 0.0,
                    "error": str(exc)[:200]}
        out = dict(out)
        out["url"] = url
        out["ms"] = round((time.monotonic() - start) * 1000, 1)
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, urls))
    failing = [r["url"] for r in results if not r.get("ok")]
    return {
        "results": results,
        "passing": len(results) - len(failing),
        "failing": failing,
        "source": "live HTTPS fetch per URL",
    }


def public_check(bucket: str, timeout: float = 60) -> dict:
    """Report whether anonymous users can read this bucket's objects."""
    out = core.run(
        ["gcloud", "storage", "buckets", "get-iam-policy", f"gs://{bucket}",
         "--format=json"],
        timeout=timeout,
    )
    try:
        policy = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(f"get-iam-policy: unparseable output: {exc}") from exc
    bindings = policy.get("bindings", []) if isinstance(policy, dict) else []
    public_roles = sorted(
        b.get("role", "") for b in bindings if "allUsers" in b.get("members", [])
    )
    return {
        "bucket": bucket,
        "public": bool(public_roles),
        "public_roles": public_roles,
        "bindings": bindings,
        "source": "bucket IAM policy (live)",
    }


def iam_inspect(bucket: str, timeout: float = 60) -> dict:
    """Dump a bucket's IAM bindings (read-only)."""
    return {
        "bucket": bucket,
        **public_check(bucket, timeout=timeout),
    }
