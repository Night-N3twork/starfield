"""GCS usage analytics from Monitoring (+ usage logs where enabled).

Every function takes an injectable seam (`_q`, `_describe`, `_logs`,
`_run`) defaulting to the real backend, so unit tests never touch the
network. Returned dicts always carry units (bytes, counts) and a
`source` note distinguishing measured data from estimates.
"""

from __future__ import annotations

import csv
import datetime
import io
import json

from . import core, gcp

SENT = "storage.googleapis.com/network/sent_bytes_count"
RECEIVED = "storage.googleapis.com/network/received_bytes_count"
REQUESTS = "storage.googleapis.com/api/request_count"
STORED = "storage.googleapis.com/storage/total_bytes"


def _window(days: int) -> tuple[str, str]:
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _filter(metric: str, bucket: str | None) -> str:
    filt = f'metric.type="{metric}" AND resource.type="gcs_bucket"'
    if bucket:
        filt += f' AND resource.labels.bucket_name="{bucket}"'
    return filt


def _per_bucket(series: list, key: str = "sum") -> dict:
    """Aggregate series values per bucket: {bucket: total}."""
    out: dict[str, float] = {}
    for entry in series:
        bucket = entry.get("resource", {}).get("labels", {}).get("bucket_name", "?")
        out[bucket] = out.get(bucket, 0.0) + gcp.sum_series([entry])
    return out


def usage(
    project: str,
    bucket: str | None = None,
    days: int = 30,
    timeout: float = 300,
    _q=None,
) -> dict:
    """Stored/download/upload bytes + request counts, per bucket and total."""
    query = _q or gcp.timeseries
    start, end = _window(days)
    sent = _per_bucket(query(project, _filter(SENT, bucket), start, end, timeout=timeout))
    received = _per_bucket(query(project, _filter(RECEIVED, bucket), start, end, timeout=timeout))
    requests = _per_bucket(query(project, _filter(REQUESTS, bucket), start, end, timeout=timeout))
    buckets: dict[str, dict] = {}
    for name in sorted(set(sent) | set(received) | set(requests)):
        if bucket is not None and name != bucket:
            continue
        buckets[name] = {
            "sent_bytes": sent.get(name, 0.0),
            "received_bytes": received.get(name, 0.0),
            "requests": requests.get(name, 0.0),
        }
    return {
        "project": project,
        "bucket": bucket,
        "days": days,
        "buckets": buckets,
        "totals": {
            "sent_bytes": sum(sent.values()),
            "received_bytes": sum(received.values()),
            "requests": sum(requests.values()),
        },
        "source": "Google Cloud Monitoring (per-minute deltas; ~3-4 min ingest delay)",
    }


def bucket_stats(
    project: str, bucket: str, days: int = 30, timeout: float = 300, _q=None
) -> dict:
    """usage() for one bucket plus current stored bytes and object count."""
    data = usage(project, bucket=bucket, days=days, timeout=timeout, _q=_q)
    stored_series = (_q or gcp.timeseries)(
        project, _filter(STORED, bucket), *_window(days), timeout=timeout
    )
    stored = _per_bucket(stored_series).get(bucket, 0.0)
    try:
        listing = core.run(
            ["gcloud", "storage", "objects", "list", f"gs://{bucket}/",
             "--format=value(name)"],
            timeout=timeout,
        )
        objects = len([line for line in listing.splitlines() if line.strip()])
    except core.CliError:
        objects = -1  # unknown (e.g. list permission); storage still reported
    data["buckets"].setdefault(bucket, {"sent_bytes": 0.0, "received_bytes": 0.0, "requests": 0.0})
    data["buckets"][bucket]["stored_bytes"] = stored
    data["buckets"][bucket]["object_count"] = objects
    data["source"] += "; stored bytes are daily samples"
    return data


def object_stats(bucket: str, obj: str, timeout: float = 60) -> dict:
    """Size, metadata, and cache config for one object."""
    out = core.run(
        ["gcloud", "storage", "objects", "describe", f"gs://{bucket}/{obj}",
         "--format=json"],
        timeout=timeout,
    )
    try:
        meta = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(f"objects describe: unparseable output: {exc}") from exc
    return {
        "bucket": bucket,
        "object": obj,
        "size_bytes": int(meta.get("size", 0)),
        "content_type": meta.get("content_type"),
        "content_encoding": meta.get("content_encoding"),
        "cache_control": meta.get("cache_control"),
        "content_disposition": meta.get("content_disposition"),
        "storage_class": meta.get("storage_class"),
        "updated": meta.get("update_time") or meta.get("updated"),
        "source": "gcloud storage objects describe (live metadata)",
    }


def traffic_history(
    project: str,
    bucket: str | None = None,
    days: int = 30,
    timeout: float = 300,
    _q=None,
) -> dict:
    """Daily sent bytes (and request counts) across the window."""
    query = _q or gcp.timeseries
    start, end = _window(days)
    by_day: dict[str, dict] = {}

    def add(metric: str, field: str) -> None:
        for entry in query(project, _filter(metric, bucket), start, end, timeout=timeout):
            for point in entry.get("points", []):
                interval = point.get("interval", {})
                day = (interval.get("endTime") or "")[:10]
                if not day:
                    continue
                row = by_day.setdefault(day, {"day": day, "sent_bytes": 0.0, "requests": 0.0})
                value = point.get("value", {})
                row[field] += float(value.get("int64Value", value.get("doubleValue", 0)))

    add(SENT, "sent_bytes")
    add(REQUESTS, "requests")
    return {
        "project": project,
        "bucket": bucket,
        "days": days,
        "daily": [by_day[day] for day in sorted(by_day)],
        "source": "Google Cloud Monitoring (per-minute deltas grouped by day)",
    }


def request_stats(
    project: str,
    bucket: str | None = None,
    days: int = 30,
    timeout: float = 300,
    _q=None,
) -> dict:
    """Request counts grouped by method and response code, plus error total."""
    query = _q or gcp.timeseries
    start, end = _window(days)
    by_method: dict[str, dict] = {}
    errors = 0.0
    total = 0.0
    for entry in query(project, _filter(REQUESTS, bucket), start, end, timeout=timeout):
        labels = entry.get("metric", {}).get("labels", {})
        method = labels.get("method", "?")
        code = labels.get("response_code", "?")
        value = gcp.sum_series([entry])
        by_method.setdefault(method, {})[code] = by_method.get(method, {}).get(code, 0.0) + value
        total += value
        if not str(code).startswith("2") and code not in ("OK",):
            # Numeric 4xx/5xx are errors; string codes (OK/NOT_FOUND/...) are
            # API result names, counted separately below.
            try:
                if int(code) >= 400:
                    errors += value
            except (TypeError, ValueError):
                pass
    return {
        "project": project,
        "bucket": bucket,
        "days": days,
        "by_method": by_method,
        "totals": {"requests": total, "errors": errors},
        "source": "Google Cloud Monitoring api/request_count",
    }


def top_objects(
    project: str,
    bucket: str,
    days: int = 7,
    limit: int = 10,
    timeout: float = 300,
    _describe=None,
    _logs=None,
    max_log_bytes: int = 5 * 1024 * 1024,
) -> dict:
    """Most-downloaded objects from Cloud Storage usage logs.

    Needs usage logging enabled on the bucket. Without it, returns
    `available: False` with the setup command instead of failing.
    """
    describe = _describe or (
        lambda b: json.loads(
            core.run(
                ["gcloud", "storage", "buckets", "describe", f"gs://{b}",
                 "--format=json"],
                timeout=timeout,
            )
        )
    )
    try:
        meta = describe(bucket)
    except core.CliError as exc:
        raise core.CliError(f"buckets describe: {exc.message}") from exc
    logging_cfg = (meta or {}).get("logging_config") or {}
    log_bucket = (logging_cfg.get("logBucket") or "").replace("gs://", "").rstrip("/")
    prefix = logging_cfg.get("logObjectPrefix", "")
    if not log_bucket:
        return {
            "bucket": bucket,
            "available": False,
            "reason": "usage logging is not enabled on this bucket",
            "setup": (
                f"gcloud storage buckets update gs://{bucket} "
                f"--log-bucket=gs://<LOG-BUCKET> --log-object-prefix={bucket}"
            ),
            "source": "bucket logging_config (live)",
        }
    if _logs is not None:
        csv_texts = _logs(bucket, log_bucket, prefix, days)
    else:
        csv_texts = _fetch_log_csvs(log_bucket, prefix, days, timeout, max_log_bytes)
    counts: dict[str, int] = {}
    bytes_out: dict[str, int] = {}
    for text in csv_texts:
        for obj, size in _parse_usage_log(text):
            counts[obj] = counts.get(obj, 0) + 1
            bytes_out[obj] = bytes_out.get(obj, 0) + size
    ranked = sorted(counts, key=lambda o: counts[o], reverse=True)[:limit]
    return {
        "bucket": bucket,
        "available": True,
        "log_bucket": log_bucket,
        "top": [
            {"object": o, "downloads": counts[o], "bytes_sent": bytes_out[o]}
            for o in ranked
        ],
        "source": "Cloud Storage usage logs (hourly CSV batches)",
    }


# Column layout of GCS usage-log CSVs (no header row in the files).
_CS_URI, _SC_STATUS, _SC_BYTES = 4, 6, 8


def _parse_usage_log(text: str) -> list:
    """Yield (object_path, bytes_sent) for successful GETs in one CSV batch."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) <= _SC_BYTES:
            continue
        method = row[3].strip().upper() if len(row) > 3 else ""
        if method != "GET":
            continue
        try:
            if int(row[_SC_STATUS]) >= 400:
                continue
            size = int(row[_SC_BYTES])
        except (TypeError, ValueError):
            continue
        uri = row[_CS_URI]
        # cs_uri looks like "/bucket/object"; strip the leading bucket part.
        parts = uri.split("/", 2)
        obj = parts[2] if len(parts) > 2 else uri.lstrip("/")
        if obj:
            out.append((obj, size))
    return out


def _fetch_log_csvs(
    log_bucket: str, prefix: str, days: int, timeout: float, cap: int
) -> list:
    """Download recent usage-log CSVs up to `cap` total bytes."""
    try:
        listing = core.run(
            ["gcloud", "storage", "objects", "list", f"gs://{log_bucket}/{prefix}",
             "--format=json(name,size,updated)"],
            timeout=timeout,
        )
        objs = json.loads(listing)
    except (core.CliError, json.JSONDecodeError):
        return []
    if not isinstance(objs, list):
        return []
    objs.sort(key=lambda o: o.get("updated", ""), reverse=True)
    texts, total = [], 0
    for obj in objs[:50]:
        name = obj.get("name", "")
        try:
            size = int(obj.get("size", 0))
        except (TypeError, ValueError):
            continue
        if total + size > cap:
            break
        try:
            text = core.run(
                ["gcloud", "storage", "cat", f"gs://{log_bucket}/{name}"],
                timeout=timeout,
            )
        except core.CliError:
            continue
        total += size
        texts.append(text)
    return texts


def audit_logs(
    project: str,
    bucket: str | None = None,
    days: int = 7,
    limit: int = 50,
    timeout: float = 300,
    _run=None,
) -> dict:
    """Recent storage audit/admin events via Cloud Logging.

    Data Access audit logs are off by default, so an empty result is
    normal and reported gracefully rather than as an error.
    """
    run = _run or core.run
    filt = 'resource.type="gcs_bucket"'
    if bucket:
        filt += f' AND resource.labels.bucket_name="{bucket}"'
    out = run(
        ["gcloud", "logging", "read", filt, f"--freshness={days}d",
         f"--limit={limit}", "--format=json", f"--project={project}"],
        timeout=timeout,
    )
    try:
        entries = json.loads(out)
    except json.JSONDecodeError:
        entries = []
    if not isinstance(entries, list):
        entries = []
    return {
        "project": project,
        "bucket": bucket,
        "entries": [
            {
                "timestamp": e.get("timestamp"),
                "method": e.get("protoPayload", {}).get("methodName"),
                "principal": e.get("protoPayload", {}).get("authenticationInfo", {}).get(
                    "principalEmail"
                ),
                "resource": e.get("resource", {}).get("labels", {}).get("bucket_name"),
            }
            for e in entries
        ],
        "note": (
            "Empty is normal: Data Access audit logs for Cloud Storage are "
            "disabled by default; enable them in IAM > Audit Logs for coverage."
        ),
        "source": "Cloud Logging (live query)",
    }
