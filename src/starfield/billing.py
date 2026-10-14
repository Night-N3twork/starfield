"""Billing, pricing, and forecasting (all read-only; no budget tools).

`gcs_billing`/`cost_summary`/`cost_breakdown` read the Standard usage-cost
BigQuery export when present, and otherwise return `enabled: False` with
setup steps instead of failing. `pricing` reads the public Cloud Billing
catalog. `forecast` projects Monitoring traffic forward and prices it.
Every number carries units and a `source` note; estimates are labeled.
"""

from __future__ import annotations

from . import analytics, core, gcp

CATALOG_BASE = "https://cloudbilling.googleapis.com/v1"
EXPORT_TABLE_PREFIX = "gcp_billing_export_v1_"
FREE_EGRESS_GIB = 100.0


def _export_tables(project: str, dataset: str, timeout: float) -> list:
    try:
        rows = gcp.bq_query(
            f"SELECT table_id FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES`",
            timeout=timeout,
        )
    except core.CliError:
        return []
    if isinstance(rows, list):
        return [r.get("table_id", "") for r in rows if isinstance(r, dict)]
    return []


def export_status(
    project: str, dataset: str = "billing_export", timeout: float = 120
) -> dict:
    """Report whether the Standard cost export exists, plus setup steps."""
    try:
        out = gcp.bq_query(
            f"SELECT table_id FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
            f"WHERE table_id LIKE '{EXPORT_TABLE_PREFIX}%' LIMIT 1",
            timeout=timeout,
        )
    except core.CliError:
        out = []
    enabled = bool(out)
    result: dict = {
        "project": project,
        "dataset": dataset,
        "enabled": enabled,
        "source": "BigQuery INFORMATION_SCHEMA (live)",
    }
    if not enabled:
        result["setup"] = (
            "Billing export is not enabled. In console: Billing > Billing "
            f"export > Standard usage cost export > project {project}, dataset "
            f"{dataset} > Save. Tables appear as "
            f"{dataset}.{EXPORT_TABLE_PREFIX}* (data starts flowing within ~24h)."
        )
    return result


def _require_export(project: str, dataset: str, timeout: float) -> None:
    tables = _export_tables(project, dataset, timeout)
    if not any(t.startswith(EXPORT_TABLE_PREFIX) for t in tables):
        raise core.CliError(
            f"billing export not enabled for {project}.{dataset} "
            f"(want tables like {EXPORT_TABLE_PREFIX}*; see export_status setup)"
        )


def gcs_billing(
    project: str,
    dataset: str = "billing_export",
    days: int = 30,
    timeout: float = 300,
) -> dict:
    """Export status plus cost summary when the export exists."""
    status = export_status(project, dataset=dataset, timeout=timeout)
    if not status.get("enabled"):
        return status
    return {**status, **cost_summary(
        project, dataset=dataset, days=days, timeout=timeout)}


def cost_summary(
    project: str,
    dataset: str = "billing_export",
    days: int = 30,
    timeout: float = 300,
) -> dict:
    """Total cost/credits/net per service over the window."""
    _require_export(project, dataset, timeout)
    rows = gcp.bq_query(
        "SELECT service.description AS service, "
        "SUM(cost) AS cost, "
        "SUM((SELECT SUM(c.amount) FROM UNNEST(credits) AS c)) AS credits "
        f"FROM `{project}.{dataset}.{EXPORT_TABLE_PREFIX}*` "
        f"WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY)) "
        "AND FORMAT_DATE('%Y%m%d', CURRENT_DATE()) "
        "GROUP BY service ORDER BY cost DESC",
        timeout=timeout,
    )
    services = [
        {"service": r.get("service"), "cost": r.get("cost") or 0.0,
         "credits": r.get("credits") or 0.0}
        for r in rows
    ]
    total_cost = sum(s["cost"] for s in services)
    total_credits = sum(s["credits"] for s in services)
    return {
        "project": project,
        "days": days,
        "services": services,
        "total_cost": total_cost,
        "total_credits": total_credits,
        "net": total_cost + total_credits,
        "source": "BigQuery standard usage-cost export (billed amounts, USD)",
    }


def cost_breakdown(
    project: str,
    dataset: str = "billing_export",
    days: int = 30,
    group_by: str = "service",
    timeout: float = 300,
) -> dict:
    """Cost grouped by service, sku, project, or day."""
    groups = {
        "service": "service.description",
        "sku": "sku.description",
        "project": "project.id",
        "day": "DATE(usage_start_time)",
    }
    if group_by not in groups:
        raise core.CliError(
            f"unknown group_by: {group_by!r} (want service|sku|project|day)"
        )
    _require_export(project, dataset, timeout)
    col = groups[group_by]
    rows = gcp.bq_query(
        f"SELECT {col} AS grp, SUM(cost) AS cost "
        f"FROM `{project}.{dataset}.{EXPORT_TABLE_PREFIX}*` "
        f"WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY)) "
        "AND FORMAT_DATE('%Y%m%d', CURRENT_DATE()) "
        "GROUP BY grp ORDER BY cost DESC LIMIT 100",
        timeout=timeout,
    )
    return {
        "project": project,
        "days": days,
        "group_by": group_by,
        "groups": [{"group": r.get("grp"), "cost": r.get("cost") or 0.0} for r in rows],
        "source": "BigQuery standard usage-cost export (billed amounts, USD)",
    }


def pricing(filter_text: str, timeout: float = 120) -> dict:
    """Search the public Cloud Billing catalog; return matching SKUs."""
    token = gcp.access_token(timeout=timeout)
    needle = filter_text.lower()
    found = []
    services = gcp._fetch_json(
        f"{CATALOG_BASE}/services?pageSize=100", token, timeout
    ).get("services", [])
    for service in services:
        sid = service.get("name", "").split("/")[-1]
        skus = gcp._fetch_json(
            f"{CATALOG_BASE}/services/{sid}/skus?pageSize=200", token, timeout
        ).get("skus", [])
        for sku in skus:
            text = f"{service.get('displayName', '')} {sku.get('description', '')}".lower()
            if needle not in text:
                continue
            price, unit = None, ""
            for info in sku.get("pricingInfo", []):
                expr = info.get("pricingExpression", {})
                unit = expr.get("usageUnit", "") or unit
                for tier in expr.get("tieredRates", [])[:1]:
                    nanos = tier.get("unitPrice", {})
                    try:
                        price = float(nanos.get("units", 0)) + float(
                            nanos.get("nanos", 0)) / 1e9
                    except (TypeError, ValueError):
                        continue
            found.append({
                "service": service.get("displayName"),
                "sku": sku.get("description"),
                "unit": unit,
                "price_usd_per_unit": price,
                "regions": sku.get("serviceRegions", []),
            })
            if len(found) >= 50:
                return {"filter": filter_text, "skus": found,
                        "source": "Cloud Billing Catalog API (list prices, USD)"}
    return {"filter": filter_text, "skus": found,
            "source": "Cloud Billing Catalog API (list prices, USD)"}


def forecast(
    project: str,
    bucket: str | None = None,
    days_history: int = 30,
    days_ahead: int = 30,
    rate_usd_per_gib: float | None = None,
    free_gib: float = FREE_EGRESS_GIB,
    timeout: float = 300,
) -> dict:
    """Project traffic forward (daily average) and price the projection.

    `rate_usd_per_gib` defaults to None, meaning "look it up is out of
    scope here": pass an explicit rate (e.g. from `pricing`) or accept
    the documented $0.12/GiB fallback, which the output labels as such.
    """
    history = analytics.traffic_history(
        project, bucket=bucket, days=days_history, timeout=timeout
    )
    daily = history.get("daily", [])
    avg = sum(r.get("sent_bytes", 0.0) for r in daily) / len(daily) if daily else 0.0
    projected_bytes = avg * days_ahead
    projected_gib = projected_bytes / (1024 ** 3)
    assumed_rate = rate_usd_per_gib is None
    rate = 0.12 if assumed_rate else float(rate_usd_per_gib)
    billable = max(0.0, projected_gib - free_gib)
    return {
        "project": project,
        "bucket": bucket,
        "days_history": days_history,
        "days_ahead": days_ahead,
        "avg_daily_bytes": avg,
        "projected_bytes": projected_bytes,
        "projected_gib": projected_gib,
        "billable_gib": billable,
        "estimated_usd": billable * rate,
        "rate": {
            "usd_per_gib": rate,
            "assumed": assumed_rate,
            "note": ("documented $0.12/GiB list fallback; pass rate_usd_per_gib "
                     "from pricing() for catalog-grounded estimates")
            if assumed_rate else "caller-supplied rate",
        },
        "free_gib": free_gib,
        "source": "Monitoring history averaged forward (estimate, not a bill)",
    }
