"""Shared Google Cloud helpers: auth token, Monitoring, BigQuery.

Thin wrappers over `core.run` (gcloud/bq CLIs) and `urllib` (JSON APIs).
`_fetch_json` is the module seam tests monkeypatch; everything else is
pure composition on top of it.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from . import core

_MONITORING_BASE = "https://monitoring.googleapis.com/v3/projects"


def access_token(timeout: float = 60) -> str:
    """Return a GCP access token via `gcloud auth print-access-token`."""
    return core.run(
        ["gcloud", "auth", "print-access-token"], timeout=timeout
    ).strip()


def _fetch_json(url: str, token: str, timeout: float = 60) -> dict:
    """GET `url` with a bearer token; parse the JSON body."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise core.CliError(f"GET {url}: {exc}") from exc


def timeseries(
    project: str,
    metric_filter: str,
    start_iso: str,
    end_iso: str,
    token: str | None = None,
    timeout: float = 60,
) -> list:
    """List Monitoring timeSeries matching `metric_filter` in [start, end]."""
    token = token if token is not None else access_token(timeout=timeout)
    params = urllib.parse.urlencode(
        {
            "filter": metric_filter,
            "interval.startTime": start_iso,
            "interval.endTime": end_iso,
            "pageSize": "1000",
        }
    )
    data = _fetch_json(f"{_MONITORING_BASE}/{project}/timeSeries?{params}", token, timeout)
    if "error" in data:
        raise core.CliError(f"Monitoring API: {data['error']}")
    return data.get("timeSeries", [])


def sum_series(series: list) -> float:
    """Sum int64/double values across every point of every series."""
    total = 0.0
    for entry in series:
        for point in entry.get("points", []):
            value = point.get("value", {})
            total += float(value.get("int64Value", value.get("doubleValue", 0)))
    return total


def bq_query(sql: str, timeout: float = 300) -> list:
    """Run Standard SQL via `bq query`; return parsed JSON rows."""
    out = core.run(
        ["bq", "query", "--format=json", "--nouse_legacy_sql", sql],
        timeout=timeout,
    )
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(f"bq query: unparseable output: {exc}") from exc
    return rows if isinstance(rows, list) else []
