"""
Unity Gateway Cost Intelligence Router

system.ai_gateway.usage is account-admin only — the app SP cannot query it directly.
An account admin must create and refresh a derived table:

    CREATE OR REPLACE TABLE <catalog>.<schema>.ai_gateway_usage_shared
    CLUSTER BY (CAST(event_time AS DATE))
    AS SELECT * FROM system.ai_gateway.usage
    WHERE workspace_id IN (...);   -- optional workspace filter

Then grant the app SP SELECT on it:
    GRANT SELECT ON TABLE <catalog>.<schema>.ai_gateway_usage_shared TO `<sp_client_id>`;

Set the env var:
    AI_GATEWAY_USAGE_TABLE=<catalog>.<schema>.ai_gateway_usage_shared

The table name is unchanged (`system.ai_gateway.usage`). Column names follow the
Unity Gateway schema, with a fallback to the legacy Mosaic AI Gateway names.

Cost model:
- 1P models (Databricks-hosted): correlated with system.billing.usage via workspace_id + date
  at aggregate level (no 1:1 join — billing aggregates usage from all consumers).
- External models (OpenAI, Anthropic, Mistral, etc.): token-based estimation using
  per-model price constants below.  No DBU cost in ai_gateway.usage — this is an estimate.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query

from backend import cache_ttls
from backend.db import (
    bundle_cache_key,
    delta_cache_get,
    delta_cache_put,
    execute_queries_parallel,
    execute_query,
    source_scope_unavailable,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# External model pricing constants (USD per 1 000 tokens, input / output)
# These are estimates — update as model pricing changes.
# ---------------------------------------------------------------------------
_EXTERNAL_MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o":                  {"input": 0.005,   "output": 0.015},
    "gpt-4o-mini":             {"input": 0.00015,  "output": 0.0006},
    "gpt-4-turbo":             {"input": 0.01,    "output": 0.03},
    "gpt-3.5-turbo":           {"input": 0.0005,  "output": 0.0015},
    # Anthropic
    "claude-3-5-sonnet":       {"input": 0.003,   "output": 0.015},
    "claude-3-5-haiku":        {"input": 0.0008,  "output": 0.004},
    "claude-3-opus":           {"input": 0.015,   "output": 0.075},
    # Mistral
    "mistral-large":           {"input": 0.003,   "output": 0.009},
    "mistral-small":           {"input": 0.001,   "output": 0.003},
    # Gemini (Google)
    "gemini-1.5-pro":          {"input": 0.00125, "output": 0.005},
    "gemini-1.5-flash":        {"input": 0.000075,"output": 0.0003},
    # Meta Llama (Databricks-hosted — 0 cost, DBU cost attributed via billing.usage)
    "databricks-meta-llama":   {"input": 0.0,     "output": 0.0},
    "meta-llama":              {"input": 0.0,     "output": 0.0},
}

_BUNDLE_TTL = cache_ttls.AIML_BUNDLE


def _get_usage_table() -> str:
    """Return the fully-qualified Unity Gateway derived table path from env."""
    tbl = os.environ.get("AI_GATEWAY_USAGE_TABLE", "").strip()
    return tbl


def _defaults(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    return start_date, end_date


def _model_price(model_name: str) -> dict[str, float]:
    """Look up per-1K-token pricing for a model. Best-effort prefix match."""
    if not model_name:
        return {"input": 0.0, "output": 0.0}
    lower = model_name.lower()
    for key, prices in _EXTERNAL_MODEL_PRICING.items():
        if key in lower:
            return prices
    return {"input": 0.0, "output": 0.0}


# ---------------------------------------------------------------------------
# SQL Templates
# ---------------------------------------------------------------------------

def _usage_exprs(table: str) -> dict[str, str]:
    from backend.unity_gateway import pick_usage_columns
    return pick_usage_columns(table)


def _render_usage_sql(template: str, table: str) -> str:
    exprs = _usage_exprs(table)
    return template.format(table=table, **exprs)


_AI_GW_STATUS_SQL = """
SELECT
    MIN(CAST({event_ts} AS DATE)) AS earliest_date,
    MAX(CAST({event_ts} AS DATE)) AS latest_date,
    COUNT(*)                      AS total_rows,
    COUNT(DISTINCT workspace_id)  AS workspace_count
FROM {table}
"""

_AI_GW_SUMMARY_SQL = """
SELECT
    COUNT(*)                                         AS total_requests,
    COUNT(DISTINCT {service})                       AS route_count,
    COUNT(DISTINCT {model})                       AS model_count,
    COUNT(DISTINCT {requester})                      AS user_count,
    SUM({input_tokens})                           AS total_input_tokens,
    SUM({output_tokens})                          AS total_output_tokens,
    SUM({total_tokens})                           AS total_tokens,
    AVG({latency})                         AS avg_latency_ms,
    SUM(CASE WHEN CAST({status_code} AS INT) >= 400 THEN 1 ELSE 0 END)  AS error_count,
    SUM(CASE WHEN CAST({status_code} AS INT) < 400 THEN 1 ELSE 0 END)   AS success_count
FROM {table}
WHERE CAST({event_ts} AS DATE) BETWEEN :start_date AND :end_date
  {{workspace_filter}}
"""

_AI_GW_BY_MODEL_SQL = """
SELECT
    {model} AS model_name,
    COUNT(*)                 AS request_count,
    SUM({input_tokens})   AS input_tokens,
    SUM({output_tokens})  AS output_tokens,
    SUM({total_tokens})   AS total_tokens,
    AVG({latency}) AS avg_latency_ms,
    SUM(CASE WHEN CAST({status_code} AS INT) >= 400 THEN 1 ELSE 0 END) AS error_count
FROM {table}
WHERE CAST({event_ts} AS DATE) BETWEEN :start_date AND :end_date
  {{workspace_filter}}
GROUP BY {model}
ORDER BY request_count DESC
LIMIT 50
"""

_AI_GW_BY_ROUTE_SQL = """
SELECT
    {service} AS route_name,
    {model} AS model_name,
    {service_type} AS service_type,
    COUNT(*)                 AS request_count,
    SUM({input_tokens})   AS input_tokens,
    SUM({output_tokens})  AS output_tokens,
    SUM({total_tokens})   AS total_tokens,
    AVG({latency}) AS avg_latency_ms,
    SUM(CASE WHEN CAST({status_code} AS INT) >= 400 THEN 1 ELSE 0 END) AS error_count
FROM {table}
WHERE CAST({event_ts} AS DATE) BETWEEN :start_date AND :end_date
  {{workspace_filter}}
GROUP BY {service}, {model}, {service_type}
ORDER BY request_count DESC
LIMIT 100
"""

_AI_GW_BY_USER_SQL = """
SELECT
    COALESCE({requester}, '(unknown)')  AS client_user,
    COUNT(*)                            AS request_count,
    COUNT(DISTINCT {service})          AS routes_used,
    SUM({input_tokens})              AS input_tokens,
    SUM({output_tokens})             AS output_tokens,
    SUM({total_tokens})              AS total_tokens,
    AVG({latency})            AS avg_latency_ms
FROM {table}
WHERE CAST({event_ts} AS DATE) BETWEEN :start_date AND :end_date
  {{workspace_filter}}
GROUP BY {requester}
ORDER BY request_count DESC
LIMIT 100
"""

_AI_GW_TIMESERIES_SQL = """
SELECT
    CAST({event_ts} AS DATE)  AS usage_date,
    {model} AS model_name,
    COUNT(*)                 AS request_count,
    SUM({input_tokens})   AS input_tokens,
    SUM({output_tokens})  AS output_tokens,
    SUM({total_tokens})   AS total_tokens,
    AVG({latency}) AS avg_latency_ms
FROM {table}
WHERE CAST({event_ts} AS DATE) BETWEEN :start_date AND :end_date
  {{workspace_filter}}
GROUP BY CAST({event_ts} AS DATE), {model}
ORDER BY usage_date, model_name
"""

_AI_GW_BILLING_CORRELATE_SQL = """
SELECT
    u.usage_date,
    u.workspace_id,
    SUM(COALESCE(p.pricing.default, 0) * u.usage_quantity) AS estimated_cost_usd,
    SUM(u.usage_quantity)                                   AS dbu_quantity
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND (
    u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
    OR u.billing_origin_product = 'MODEL_SERVING'
    OR u.sku_name LIKE '%ANTHROPIC%'
    OR u.sku_name LIKE '%OPENAI%'
    OR u.sku_name LIKE '%GEMINI%'
    OR u.sku_name LIKE '%LLAMA%'
    OR u.sku_name LIKE '%FOUNDATION_MODEL%'
  )
  {workspace_filter}
GROUP BY u.usage_date, u.workspace_id
ORDER BY u.usage_date
"""


def _with_inventory(bundle: dict[str, Any], tbl: str | None) -> dict[str, Any]:
    from backend.unity_gateway import (
        cached_inventory,
        list_unity_gateway_inventory,
        usage_schema_label,
    )
    try:
        bundle["inventory"] = cached_inventory(
            "unity-gateway", list_unity_gateway_inventory, {"model_services": [], "mcp_services": []},
        )
        bundle["usage_schema"] = usage_schema_label(tbl) if tbl else "unity"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unity Gateway inventory failed: %s", exc)
        bundle.setdefault(
            "inventory",
            {"available": False, "error": str(exc), "model_services": [], "mcp_services": []},
        )
    return bundle


def _ws_ids(csv: str | None, repeated: list[str] | None) -> list[str] | None:
    """Merge `workspace_ids=a,b` (what the UI sends) and repeated `workspace_id`, dropping unsafe values.

    Falls back to the app's configured workspace scope (COST_OBS_WORKSPACES).
    """
    from backend.workspace_filter import _is_safe_id, get_configured_workspace_ids
    raw = [*(csv.split(",") if csv else []), *(repeated or [])]
    ids = sorted({w.strip() for w in raw if w.strip() and _is_safe_id(w.strip())})
    return ids or get_configured_workspace_ids() or None


def _apply_workspace_filter(sql: str, workspace_ids: list[str] | None, table_alias: str = "") -> tuple[str, dict]:
    """Inject workspace filter and return (sql, extra_params)."""
    prefix = f"{table_alias}." if table_alias else ""
    if workspace_ids:
        placeholders = ", ".join(f":ws_{i}" for i in range(len(workspace_ids)))
        clause = f"AND CAST({prefix}workspace_id AS STRING) IN ({placeholders})"
        params = {f"ws_{i}": str(wid) for i, wid in enumerate(workspace_ids)}
        return sql.replace("{workspace_filter}", clause), params
    return sql.replace("{workspace_filter}", ""), {}


def _aggregate_gateway_rows(rows: list[dict]) -> dict[str, Any]:
    """Turn daily_ai_gateway grain into the dashboard's summary / model / route / user / ts views."""
    total_req = sum(int(r.get("request_count") or 0) for r in rows)
    errors = sum(int(r.get("error_count") or 0) for r in rows)
    successes = sum(int(r.get("success_count") or 0) for r in rows)
    routes = {r.get("route_name") for r in rows if r.get("route_name")}
    models = {r.get("model_name") for r in rows if r.get("model_name")}
    users = {r.get("client_user") for r in rows if r.get("client_user")}
    lat_w = sum(float(r.get("avg_latency_ms") or 0) * int(r.get("request_count") or 0) for r in rows)
    summary = {
        "total_requests": total_req,
        "success_count": successes,
        "error_count": errors,
        "error_rate_pct": round(errors / total_req * 100, 1) if total_req else 0.0,
        "route_count": len(routes),
        "model_count": len(models),
        "user_count": len(users),
        "total_input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "total_output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
        "avg_latency_ms": round(lat_w / total_req, 1) if total_req else 0.0,
    }
    by_model: dict[str, dict] = {}
    by_route: dict[tuple, dict] = {}
    by_user: dict[str, dict] = {}
    ts: dict[tuple, dict] = {}
    for r in rows:
        n = int(r.get("request_count") or 0)
        model = r.get("model_name")
        route = r.get("route_name")
        user = r.get("client_user") or "(unknown)"
        day = str(r.get("usage_date"))
        bm = by_model.setdefault(model, {"model_name": model, "request_count": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "avg_latency_ms": 0.0, "error_count": 0, "_lat": 0.0})
        bm["request_count"] += n
        bm["input_tokens"] += int(r.get("input_tokens") or 0)
        bm["output_tokens"] += int(r.get("output_tokens") or 0)
        bm["total_tokens"] += int(r.get("total_tokens") or 0)
        bm["error_count"] += int(r.get("error_count") or 0)
        bm["_lat"] += float(r.get("avg_latency_ms") or 0) * n
        br = by_route.setdefault((route, model), {"route_name": route, "model_name": model, "request_count": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "avg_latency_ms": 0.0, "error_count": 0, "_lat": 0.0})
        br["request_count"] += n
        br["input_tokens"] += int(r.get("input_tokens") or 0)
        br["output_tokens"] += int(r.get("output_tokens") or 0)
        br["total_tokens"] += int(r.get("total_tokens") or 0)
        br["error_count"] += int(r.get("error_count") or 0)
        br["_lat"] += float(r.get("avg_latency_ms") or 0) * n
        bu = by_user.setdefault(user, {"client_user": user, "request_count": 0, "routes_used": set(), "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "avg_latency_ms": 0.0, "_lat": 0.0})
        bu["request_count"] += n
        bu["routes_used"].add(route)
        bu["input_tokens"] += int(r.get("input_tokens") or 0)
        bu["output_tokens"] += int(r.get("output_tokens") or 0)
        bu["total_tokens"] += int(r.get("total_tokens") or 0)
        bu["_lat"] += float(r.get("avg_latency_ms") or 0) * n
        t = ts.setdefault((day, model), {"usage_date": day, "model_name": model, "request_count": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "avg_latency_ms": 0.0, "_lat": 0.0})
        t["request_count"] += n
        t["input_tokens"] += int(r.get("input_tokens") or 0)
        t["output_tokens"] += int(r.get("output_tokens") or 0)
        t["total_tokens"] += int(r.get("total_tokens") or 0)
        t["_lat"] += float(r.get("avg_latency_ms") or 0) * n
    def _fin(d):
        req = d.get("request_count") or 0
        d["avg_latency_ms"] = round(d.pop("_lat", 0) / req, 1) if req else 0.0
        if "routes_used" in d and isinstance(d["routes_used"], set):
            d["routes_used"] = len(d["routes_used"])
        return d
    model_rows = _enrich_model_costs(sorted((_fin(v) for v in by_model.values()), key=lambda x: x["request_count"], reverse=True)[:50])
    route_rows = _enrich_model_costs(sorted((_fin(v) for v in by_route.values()), key=lambda x: x["request_count"], reverse=True)[:100])
    user_rows = sorted((_fin(v) for v in by_user.values()), key=lambda x: x["request_count"], reverse=True)[:100]
    ts_rows = _annotate_costs(sorted((_fin(v) for v in ts.values()), key=lambda x: (x["usage_date"], x["model_name"])), {})
    summary["total_estimated_cost_usd"] = round(sum(r.get("estimated_cost_usd", 0) for r in model_rows), 2)
    return {
        "summary": summary,
        "by_model": model_rows,
        "by_route": route_rows,
        "top_users": user_rows[:25],
        "timeseries": ts_rows,
    }


def _billing_by_date(billing_rows: list[dict]) -> dict[str, float]:
    """Sum billing-correlated serving cost per day across workspaces."""
    out: dict[str, float] = {}
    for r in billing_rows:
        d = str(r.get("usage_date"))
        out[d] = out.get(d, 0.0) + float(r.get("estimated_cost_usd") or 0)
    return out


def _annotate_costs(rows: list[dict], billing_by_date: dict) -> list[dict]:
    """Attach estimated_cost_usd to timeseries rows from billing correlation + token pricing.

    A day's billed serving cost is split across that day's model rows by token
    share (request share when tokens are absent), so the chart sums to the bill.
    """
    day_tokens: dict[str, float] = {}
    day_requests: dict[str, float] = {}
    for row in rows:
        d = str(row.get("usage_date", ""))
        day_tokens[d] = day_tokens.get(d, 0.0) + float(row.get("total_tokens") or 0)
        day_requests[d] = day_requests.get(d, 0.0) + float(row.get("request_count") or 0)
    for row in rows:
        date_str = str(row.get("usage_date", ""))
        model = str(row.get("model_name", "")).lower()
        if day_tokens.get(date_str):
            share = float(row.get("total_tokens") or 0) / day_tokens[date_str]
        elif day_requests.get(date_str):
            share = float(row.get("request_count") or 0) / day_requests[date_str]
        else:
            share = 0.0
        billing_cost = billing_by_date.get(date_str, 0.0) * share
        prices = _model_price(model)
        input_k = (row.get("input_tokens") or 0) / 1000.0
        output_k = (row.get("output_tokens") or 0) / 1000.0
        token_est = input_k * prices["input"] + output_k * prices["output"]
        # If billing data is available use it; if token pricing is > 0 add it for external models
        if billing_cost > 0:
            row["estimated_cost_usd"] = round(billing_cost, 4)
            row["cost_source"] = "billing"
        elif token_est > 0:
            row["estimated_cost_usd"] = round(token_est, 4)
            row["cost_source"] = "token_estimate"
        else:
            row["estimated_cost_usd"] = 0.0
            row["cost_source"] = "unavailable"
    return rows


def _enrich_model_costs(rows: list[dict]) -> list[dict]:
    """Add estimated cost to per-model rows using token pricing."""
    for row in rows:
        model = str(row.get("model_name", "")).lower()
        prices = _model_price(model)
        input_k = (row.get("input_tokens") or 0) / 1000.0
        output_k = (row.get("output_tokens") or 0) / 1000.0
        token_est = input_k * prices["input"] + output_k * prices["output"]
        row["estimated_cost_usd"] = round(token_est, 4)
        row["cost_source"] = "token_estimate" if token_est > 0 else "unavailable"
    return rows


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
async def get_ai_gateway_status() -> dict[str, Any]:
    """Check whether the Unity Gateway usage table is configured and accessible."""
    tbl = _get_usage_table()
    if not tbl:
        return {
            "configured": False,
            "table": None,
            "message": (
                "AI_GATEWAY_USAGE_TABLE env var not set. "
                "An account admin must create and share a derived table from system.ai_gateway.usage."
            ),
        }
    import asyncio
    loop = asyncio.get_running_loop()

    def _probe():
        try:
            rows = execute_query(
                _render_usage_sql(_AI_GW_STATUS_SQL, tbl),
                no_cache=True,
            )
            if rows:
                r = rows[0]
                return {
                    "configured": True,
                    "accessible": True,
                    "table": tbl,
                    "earliest_date": str(r.get("earliest_date") or ""),
                    "latest_date": str(r.get("latest_date") or ""),
                    "total_rows": r.get("total_rows") or 0,
                    "workspace_count": r.get("workspace_count") or 0,
                }
            return {"configured": True, "accessible": True, "table": tbl, "total_rows": 0}
        except Exception as e:
            return {
                "configured": True,
                "accessible": False,
                "table": tbl,
                "error": str(e),
                "message": (
                    "Table configured but not accessible. Check that the app SP has SELECT grant on the table, "
                    "and that the table has been populated by the admin refresh job."
                ),
            }

    return await loop.run_in_executor(None, _probe)


@router.get("/summary")
async def get_ai_gateway_summary(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    workspace_ids: str | None = Query(None, description="Comma-separated workspace IDs"),
    workspace_id: list[str] | None = Query(None),
) -> dict[str, Any]:
    """Overall request counts, token consumption, and latency for the period."""
    workspace_ids = _ws_ids(workspace_ids, workspace_id)
    tbl = _get_usage_table()
    if not tbl:
        return {"error": "AI_GATEWAY_USAGE_TABLE not configured"}

    start_date, end_date = _defaults(start_date, end_date)
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}

    sql, ws_params = _apply_workspace_filter(_render_usage_sql(_AI_GW_SUMMARY_SQL, tbl), workspace_ids)
    params.update(ws_params)

    import asyncio
    loop = asyncio.get_running_loop()

    def _run():
        rows = execute_query(sql, params)
        if not rows:
            return {}
        r = rows[0]
        total = r.get("total_tokens") or 0
        errors = r.get("error_count") or 0
        successes = r.get("success_count") or 0
        total_req = r.get("total_requests") or 0
        return {
            "total_requests": total_req,
            "success_count": successes,
            "error_count": errors,
            "error_rate_pct": round(errors / total_req * 100, 1) if total_req else 0.0,
            "route_count": r.get("route_count") or 0,
            "model_count": r.get("model_count") or 0,
            "user_count": r.get("user_count") or 0,
            "total_input_tokens": r.get("total_input_tokens") or 0,
            "total_output_tokens": r.get("total_output_tokens") or 0,
            "total_tokens": total,
            "avg_latency_ms": round(float(r.get("avg_latency_ms") or 0), 1),
            "start_date": start_date,
            "end_date": end_date,
        }

    return await loop.run_in_executor(None, _run)


@router.get("/by-model")
async def get_ai_gateway_by_model(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    workspace_ids: str | None = Query(None, description="Comma-separated workspace IDs"),
    workspace_id: list[str] | None = Query(None),
) -> list[dict[str, Any]]:
    """Token consumption and request counts broken down by model."""
    workspace_ids = _ws_ids(workspace_ids, workspace_id)
    tbl = _get_usage_table()
    if not tbl:
        return []

    start_date, end_date = _defaults(start_date, end_date)
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    sql, ws_params = _apply_workspace_filter(_render_usage_sql(_AI_GW_BY_MODEL_SQL, tbl), workspace_ids)
    params.update(ws_params)

    import asyncio
    loop = asyncio.get_running_loop()

    def _run():
        rows = execute_query(sql, params)
        return _enrich_model_costs(rows)

    return await loop.run_in_executor(None, _run)


@router.get("/by-route")
async def get_ai_gateway_by_route(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    workspace_ids: str | None = Query(None, description="Comma-separated workspace IDs"),
    workspace_id: list[str] | None = Query(None),
) -> list[dict[str, Any]]:
    """Token consumption and request counts broken down by Unity Gateway service."""
    workspace_ids = _ws_ids(workspace_ids, workspace_id)
    tbl = _get_usage_table()
    if not tbl:
        return []

    start_date, end_date = _defaults(start_date, end_date)
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    sql, ws_params = _apply_workspace_filter(_render_usage_sql(_AI_GW_BY_ROUTE_SQL, tbl), workspace_ids)
    params.update(ws_params)

    import asyncio
    loop = asyncio.get_running_loop()

    def _run():
        rows = execute_query(sql, params)
        return _enrich_model_costs(rows)

    return await loop.run_in_executor(None, _run)


@router.get("/by-user")
async def get_ai_gateway_by_user(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    workspace_ids: str | None = Query(None, description="Comma-separated workspace IDs"),
    workspace_id: list[str] | None = Query(None),
) -> list[dict[str, Any]]:
    """Token consumption and request counts broken down by calling user/service principal."""
    workspace_ids = _ws_ids(workspace_ids, workspace_id)
    tbl = _get_usage_table()
    if not tbl:
        return []

    start_date, end_date = _defaults(start_date, end_date)
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    sql, ws_params = _apply_workspace_filter(_render_usage_sql(_AI_GW_BY_USER_SQL, tbl), workspace_ids)
    params.update(ws_params)

    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: execute_query(sql, params))


@router.get("/timeseries")
async def get_ai_gateway_timeseries(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    workspace_ids: str | None = Query(None, description="Comma-separated workspace IDs"),
    workspace_id: list[str] | None = Query(None),
) -> list[dict[str, Any]]:
    """Daily token usage and request counts by model for the time series chart."""
    workspace_ids = _ws_ids(workspace_ids, workspace_id)
    tbl = _get_usage_table()
    if not tbl:
        return []

    start_date, end_date = _defaults(start_date, end_date)
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    sql, ws_params = _apply_workspace_filter(_render_usage_sql(_AI_GW_TIMESERIES_SQL, tbl), workspace_ids)
    params.update(ws_params)

    billing_sql, billing_ws = _apply_workspace_filter(
        _AI_GW_BILLING_CORRELATE_SQL, workspace_ids, table_alias="u"
    )
    billing_params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    billing_params.update(billing_ws)

    import asyncio
    loop = asyncio.get_running_loop()

    def _run():
        results = execute_queries_parallel([
            ("usage", lambda: execute_query(sql, params)),
            ("billing", lambda: execute_query(billing_sql, billing_params)),
        ], timeout=60)
        usage_rows = results.get("usage") or []
        billing_rows = results.get("billing") or []
        billing_by_date = _billing_by_date(billing_rows)
        return _annotate_costs(usage_rows, billing_by_date)

    return await loop.run_in_executor(None, _run)


@router.get("/dashboard-bundle")
def get_ai_gateway_dashboard_bundle(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    workspace_ids: str | None = Query(None, description="Comma-separated workspace IDs"),
    workspace_id: list[str] | None = Query(None),
) -> dict[str, Any]:
    """All Unity Gateway data in one call for the dashboard tab.

    Returns: status, summary, by_model, by_route, top_users, timeseries, inventory.
    """
    workspace_ids = _ws_ids(workspace_ids, workspace_id)
    tbl = _get_usage_table()
    if not tbl:
        return _with_inventory({
            "configured": False,
            "message": (
                "AI_GATEWAY_USAGE_TABLE not configured. "
                "Set this env var in your Databricks App to the admin-managed derived table."
            ),
        }, None)

    start_date, end_date = _defaults(start_date, end_date)
    # Unity Gateway gold is daily_ai_gateway. Missing AIML grain is not a reason
    # to mark this tab unsupported.
    blocked = source_scope_unavailable(
        {"daily_ai_gateway"},
        "The selected shared source does not publish Unity Gateway detail.",
    )
    if blocked:
        return _with_inventory({
            **blocked,
            "configured": True,
            "table": tbl,
            "start_date": start_date,
            "end_date": end_date,
            "summary": {},
            "by_model": [],
            "by_route": [],
            "top_users": [],
            "timeseries": [],
        }, tbl)

    # Delta cache check
    ws_list = sorted(workspace_ids) if workspace_ids else []
    cache_key = bundle_cache_key("ai-gateway-bundle", start_date, end_date, ws_list or None)
    cached = delta_cache_get(cache_key)
    if cached is not None:
        logger.info("ai-gateway-bundle: Delta cache hit")
        return _with_inventory(cached, tbl)

    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    from backend import workspace_filter as wf
    ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=workspace_ids) if workspace_ids else ""

    from backend.lakebase_read import PG_AI_GATEWAY_ROWS, gold_read

    def _gold_run():
        rows = gold_read("ai_gateway_rows", PG_AI_GATEWAY_ROWS, params, ws_clause)
        if rows:
            agg = _aggregate_gateway_rows(rows)
            bundle = {
                "configured": True,
                "table": tbl,
                "start_date": start_date,
                "end_date": end_date,
                **agg,
            }
            delta_cache_put(cache_key, "ai-gateway-bundle", bundle, ttl_seconds=_BUNDLE_TTL)
            return bundle
        return None

    def _run():
        gold_bundle = _gold_run()
        if gold_bundle:
            return gold_bundle
        from backend.lakebase_read import warehouse_view_fallback
        if not warehouse_view_fallback():
            empty = _aggregate_gateway_rows([])
            return {
                "configured": True,
                "table": tbl,
                "start_date": start_date,
                "end_date": end_date,
                **empty,
            }

        def _make_sql(template: str) -> tuple[str, dict]:
            sql, ws_p = _apply_workspace_filter(_render_usage_sql(template, tbl), workspace_ids)
            return sql, {**params, **ws_p}

        summary_sql, summary_p = _make_sql(_AI_GW_SUMMARY_SQL)
        model_sql, model_p = _make_sql(_AI_GW_BY_MODEL_SQL)
        route_sql, route_p = _make_sql(_AI_GW_BY_ROUTE_SQL)
        user_sql, user_p = _make_sql(_AI_GW_BY_USER_SQL)
        ts_sql, ts_p = _make_sql(_AI_GW_TIMESERIES_SQL)
        billing_sql, billing_ws = _apply_workspace_filter(
            _AI_GW_BILLING_CORRELATE_SQL, workspace_ids, table_alias="u"
        )
        billing_p: dict[str, Any] = {"start_date": start_date, "end_date": end_date, **billing_ws}

        results = execute_queries_parallel([
            ("summary",  lambda: execute_query(summary_sql, summary_p)),
            ("by_model", lambda: execute_query(model_sql, model_p)),
            ("by_route", lambda: execute_query(route_sql, route_p)),
            ("by_user",  lambda: execute_query(user_sql, user_p)),
            ("timeseries", lambda: execute_query(ts_sql, ts_p)),
            ("billing",  lambda: execute_query(billing_sql, billing_p)),
        ], timeout=90)

        summary_rows = results.get("summary") or []
        model_rows = _enrich_model_costs(results.get("by_model") or [])
        route_rows = _enrich_model_costs(results.get("by_route") or [])
        user_rows = results.get("by_user") or []
        billing_rows = results.get("billing") or []
        billing_by_date = _billing_by_date(billing_rows)
        ts_rows = _annotate_costs(results.get("timeseries") or [], billing_by_date)

        # Build summary dict
        s = summary_rows[0] if summary_rows else {}
        total_req = s.get("total_requests") or 0
        errors = s.get("error_count") or 0
        summary = {
            "total_requests": total_req,
            "success_count": s.get("success_count") or 0,
            "error_count": errors,
            "error_rate_pct": round(errors / total_req * 100, 1) if total_req else 0.0,
            "route_count": s.get("route_count") or 0,
            "model_count": s.get("model_count") or 0,
            "user_count": s.get("user_count") or 0,
            "total_input_tokens": s.get("total_input_tokens") or 0,
            "total_output_tokens": s.get("total_output_tokens") or 0,
            "total_tokens": s.get("total_tokens") or 0,
            "avg_latency_ms": round(float(s.get("avg_latency_ms") or 0), 1),
            "total_estimated_cost_usd": round(sum(r.get("estimated_cost_usd", 0) for r in model_rows), 2),
        }

        bundle = {
            "configured": True,
            "table": tbl,
            "start_date": start_date,
            "end_date": end_date,
            "summary": summary,
            "by_model": model_rows,
            "by_route": route_rows,
            "top_users": user_rows[:25],
            "timeseries": ts_rows,
        }
        delta_cache_put(cache_key, "ai-gateway-bundle", bundle, ttl_seconds=_BUNDLE_TTL)
        return bundle

    return _with_inventory(_run(), tbl)
