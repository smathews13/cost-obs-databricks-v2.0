"""
Genie Cost Reporting Router (customer-facing, static dashboard tab).

Adapts the internal cost-reporting query pattern (which lives on
`main.data_commercialization.genie_daily_workspace_usage_by_attribute` +
`eng_billable_usage.billable_usage` — neither of which customers have) to
what customers *do* have: `system.billing.usage`, `system.billing.list_prices`,
and `system.access.workspaces_latest`.

Two dimensions of uncertainty against real customer data:
 1. **Genie sub-surface detection.** `system.billing.usage` carries it in
    `usage_metadata.genie.surface` (GENIE_CODE / GENIE_ONE / GENIE_AGENTS).
    The SKU is no help: Genie bills as GENIE_FREE_USAGE or
    ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_<region> for every surface.
    SKU-name patterns remain as a fallback for rows without the field.
 2. **Free-tier attribution.** Customers don't have a direct `free_tier_usd`
    column. Approximated from `usage_type`: `BILLABLE` → charged, everything
    else (`NON_BILLABLE`, `RELATED`, promotional variants) → free-tier.

Both mappings are centralized in `_SURFACE_CASE_SQL` and `_CHARGE_CLASS_CASE_SQL`
so they can be updated in one place. The `/diagnostics` endpoint dumps the
distinct `sku_name` × `usage_type` × `billing_origin_product` combinations
actually present in the customer's data so field engineers can validate the
mapping in each deployment.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query

from backend import cache_ttls
from backend import workspace_filter as wf
from backend.db import (
    apply_mv_overrides,
    bundle_cache_key,
    delta_cache_get,
    delta_cache_put,
    execute_queries_parallel,
    execute_query,
    get_catalog_schema,
    source_label_filter_clause,
    source_scope_unavailable,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _with_genie_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from backend.routers.conversational import configured_space_id
        from backend.unity_gateway import cached_inventory, list_genie_space_inventory
        space_id = configured_space_id()
        payload["inventory"] = cached_inventory(
            f"genie:{space_id}",
            lambda: list_genie_space_inventory(space_id),
            {"spaces": [], "space_count": 0},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Genie space inventory failed: %s", exc)
        payload["inventory"] = {"available": False, "error": str(exc), "spaces": [], "space_count": 0}
    return payload


# ---------------------------------------------------------------------------
# SKU mapping — centralized so it's easy to update once we see real data.
# ---------------------------------------------------------------------------
# "Any Genie" filter used to constrain the initial scan of system.billing.usage.
# Broad by design — a false-positive here only means we do a bit more work in
# the CASE expressions below, not that we surface non-Genie spend as Genie.
# billing_origin_product = 'GENIE' is the reliable signal: real Genie spend is
# token-billed serverless inference (sku_name like ENTERPRISE_SERVERLESS_REAL_TIME_
# INFERENCE_*) which does NOT contain 'GENIE' — the sku_name patterns alone missed
# all of it. Validated against cmegdemos billing via the Diagnostics endpoint.
_GENIE_INCLUSION_WHERE = (
    "(u.billing_origin_product = 'GENIE' "
    "OR u.sku_name ILIKE '%GENIE%' OR u.sku_name ILIKE '%AI_BI_GENIE%')"
)

# The surface field on a billing row. Read through to_json so a workspace whose
# usage_metadata struct predates the `genie` field gets NULL instead of failing
# analysis of the whole query.
GENIE_SURFACE_FIELD_SQL = "get_json_object(to_json({alias}usage_metadata), '$.genie.surface')"

# Genie sub-surface classification. `usage_metadata.genie.surface` is the real
# signal (validated against billing on 2026-10-30: GENIE_CODE, GENIE_ONE and
# GENIE_AGENTS, with the same SKUs across all three). The SKU-name patterns are a
# fallback for rows that don't carry the field.
# The `ELSE 'unknown'` bucket is intentional: any row that passes the Genie
# inclusion filter but matches neither still contributes to `total_usd` and
# `charged_usd__genie_unknown` — never dropped silently. The Diagnostics endpoint
# shows the surface values and SKUs behind any unknown spend.
def genie_surface_case_sql(surface: str, sku: str) -> str:
    """CASE mapping a row to genie_code / genie_one / genie_agents / unknown."""
    return f"""
    CASE
        WHEN {surface} = 'GENIE_CODE'   OR {sku} ILIKE '%GENIE_CODE%'   THEN 'genie_code'
        WHEN {surface} = 'GENIE_ONE'    OR {sku} ILIKE '%GENIE_ONE%'    THEN 'genie_one'
        WHEN {surface} = 'GENIE_AGENTS' OR {sku} ILIKE '%GENIE_AGENTS%' THEN 'genie_agents'
        ELSE 'unknown'
    END
"""


# Over the genie_usage CTEs below, which project the surface field as genie_surface_raw.
_SURFACE_CASE_SQL = genie_surface_case_sql("genie_surface_raw", "sku_name")

# Free-tier attribution per V2.1_SPEC §5.2 convention, refined after validating
# against real billing (Diagnostics endpoint): Genie serverless inference bills as
# usage_type = 'TOKEN' (metered token consumption) — that is charged spend, so it
# joins BILLABLE. Without this, all Genie token spend fell into 'other' and the
# dashboard showed $0.
#   BILLABLE, TOKEN     → charged
#   NON_BILLABLE        → free_tier
#   RELATED             → free_tier
#   anything else       → excluded from Genie buckets AND tracked in
#                         `other_usage_type_usd` diagnostic so new/unknown
#                         values surface if Databricks starts emitting them.
_CHARGE_CLASS_CASE_SQL = """
    CASE
        WHEN usage_type IN ('BILLABLE', 'TOKEN') THEN 'charged'
        WHEN usage_type IN ('NON_BILLABLE', 'RELATED') THEN 'free_tier'
        ELSE 'other'
    END
"""


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------

# Per-workspace Genie breakdown. Analogous to the internal account_level CTE
# in the source SQL, but keyed by workspace_id (customers don't have SFDC
# account IDs; workspace is the finest available grain).
_GENIE_BY_WORKSPACE_SQL = f"""
WITH genie_usage AS (
    SELECT
        u.usage_date,
        u.workspace_id,
        u.sku_name,
        u.usage_type,
        {GENIE_SURFACE_FIELD_SQL.format(alias="u.")} AS genie_surface_raw,
        u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
        ON u.sku_name = p.sku_name
       AND u.cloud = p.cloud
       AND p.price_end_time IS NULL
    WHERE u.usage_date BETWEEN :start_date AND :end_date
      AND u.usage_quantity > 0
      AND {_GENIE_INCLUSION_WHERE}
      {{ws_clause}}
),
priced AS (
    SELECT
        usage_date,
        workspace_id,
        spend,
        {_SURFACE_CASE_SQL} AS surface,
        {_CHARGE_CLASS_CASE_SQL} AS charge_class
    FROM genie_usage
)
SELECT
    workspace_id,
    -- Overall
    SUM(spend) AS total_usd,
    SUM(CASE WHEN charge_class = 'charged'   THEN spend ELSE 0 END) AS charged_usd,
    SUM(CASE WHEN charge_class = 'free_tier' THEN spend ELSE 0 END) AS free_tier_usd,
    -- Other usage_type diagnostic — surfaces spend under unknown usage_type
    -- values (anything not BILLABLE / NON_BILLABLE / RELATED). If non-zero
    -- in a customer deploy, run the /diagnostics endpoint to see which
    -- usage_type strings Databricks is emitting.
    SUM(CASE WHEN charge_class = 'other'     THEN spend ELSE 0 END) AS other_usage_type_usd,
    -- Genie Code
    SUM(CASE WHEN surface = 'genie_code' THEN spend ELSE 0 END) AS total_usd__genie_code,
    SUM(CASE WHEN surface = 'genie_code' AND charge_class = 'charged'   THEN spend ELSE 0 END) AS charged_usd__genie_code,
    SUM(CASE WHEN surface = 'genie_code' AND charge_class = 'free_tier' THEN spend ELSE 0 END) AS free_tier_usd__genie_code,
    -- Genie One
    SUM(CASE WHEN surface = 'genie_one' THEN spend ELSE 0 END) AS total_usd__genie_one,
    SUM(CASE WHEN surface = 'genie_one' AND charge_class = 'charged'   THEN spend ELSE 0 END) AS charged_usd__genie_one,
    SUM(CASE WHEN surface = 'genie_one' AND charge_class = 'free_tier' THEN spend ELSE 0 END) AS free_tier_usd__genie_one,
    -- Genie Agents
    SUM(CASE WHEN surface = 'genie_agents' THEN spend ELSE 0 END) AS total_usd__genie_agents,
    SUM(CASE WHEN surface = 'genie_agents' AND charge_class = 'charged'   THEN spend ELSE 0 END) AS charged_usd__genie_agents,
    SUM(CASE WHEN surface = 'genie_agents' AND charge_class = 'free_tier' THEN spend ELSE 0 END) AS free_tier_usd__genie_agents,
    -- Genie Unknown — passes the Genie inclusion filter but no sub-surface
    -- pattern matches. Non-zero here means the SKU set needs mapping updates.
    SUM(CASE WHEN surface = 'unknown' THEN spend ELSE 0 END) AS total_usd__genie_unknown,
    SUM(CASE WHEN surface = 'unknown' AND charge_class = 'charged'   THEN spend ELSE 0 END) AS charged_usd__genie_unknown,
    SUM(CASE WHEN surface = 'unknown' AND charge_class = 'free_tier' THEN spend ELSE 0 END) AS free_tier_usd__genie_unknown,
    -- Time bounds
    MIN(CASE WHEN spend > 0 THEN usage_date END) AS earliest_date_with_billing,
    MAX(CASE WHEN spend > 0 THEN usage_date END) AS latest_date_with_billing
FROM priced
GROUP BY workspace_id
HAVING SUM(spend) > 0
"""


# Databricks-total denominator for the same window. Used to compute
# `genie_pct_of_databricks_usd`. Scoped to the same workspace filter as the
# Genie query so the ratio is apples-to-apples.
_DATABRICKS_TOTAL_SQL = """
SELECT COALESCE(SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)), 0.0) AS databricks_total_usd
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
   AND u.cloud = p.cloud
   AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  {ws_clause}
"""


# Timeseries — daily Genie spend by charge class, for the trend chart.
_GENIE_TIMESERIES_SQL = f"""
WITH genie_usage AS (
    SELECT
        u.usage_date,
        u.workspace_id,
        u.sku_name,
        u.usage_type,
        {GENIE_SURFACE_FIELD_SQL.format(alias="u.")} AS genie_surface_raw,
        u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
        ON u.sku_name = p.sku_name
       AND u.cloud = p.cloud
       AND p.price_end_time IS NULL
    WHERE u.usage_date BETWEEN :start_date AND :end_date
      AND u.usage_quantity > 0
      AND {_GENIE_INCLUSION_WHERE}
      {{ws_clause}}
)
SELECT
    usage_date,
    SUM(spend) AS total_usd,
    SUM(CASE WHEN {_CHARGE_CLASS_CASE_SQL} = 'charged'   THEN spend ELSE 0 END) AS charged_usd,
    SUM(CASE WHEN {_CHARGE_CLASS_CASE_SQL} = 'free_tier' THEN spend ELSE 0 END) AS free_tier_usd,
    SUM(CASE WHEN {_SURFACE_CASE_SQL} = 'genie_code'   THEN spend ELSE 0 END) AS genie_code_usd,
    SUM(CASE WHEN {_SURFACE_CASE_SQL} = 'genie_one'    THEN spend ELSE 0 END) AS genie_one_usd,
    SUM(CASE WHEN {_SURFACE_CASE_SQL} = 'genie_agents' THEN spend ELSE 0 END) AS genie_agents_usd
FROM genie_usage
GROUP BY usage_date
ORDER BY usage_date
"""


# Diagnostics — dump the distinct SKU / usage_type / billing_origin_product
# combos actually present in the customer's data so field engineers can
# validate the sub-surface + free-tier mappings. Returned by the
# /diagnostics endpoint.
_GENIE_DIAGNOSTICS_SQL = f"""
SELECT
    u.sku_name,
    u.usage_type,
    u.billing_origin_product,
    {GENIE_SURFACE_FIELD_SQL.format(alias="u.")} AS genie_surface,
    COUNT(*) AS row_count,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_usd,
    MIN(u.usage_date) AS earliest_date,
    MAX(u.usage_date) AS latest_date
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
   AND u.cloud = p.cloud
   AND p.price_end_time IS NULL
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), 90)
  AND (
        u.sku_name ILIKE '%GENIE%'
     OR u.sku_name ILIKE '%AI_BI%'
     OR u.billing_origin_product ILIKE '%GENIE%'
     OR u.billing_origin_product ILIKE '%AI_BI%'
  )
GROUP BY ALL
ORDER BY total_usd DESC
LIMIT 100
"""


def _defaults(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    return start_date, end_date


def _try_div(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _resolve_ws_name(ws_id: str, name_map: dict[str, str]) -> str:
    return name_map.get(str(ws_id)) or str(ws_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/dashboard-bundle")
def get_genie_dashboard_bundle(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str | None = Query(default=None, description="Comma-separated workspace IDs to scope"),
) -> dict[str, Any]:
    """Return the payload for the static Genie cost dashboard tab.

    Shape:
      {
        "summary": { total_usd, charged_usd, free_tier_usd, pct_free,
                     databricks_total_usd, genie_pct_of_databricks_usd,
                     <per-surface> totals },
        "workspaces": [{ workspace_id, workspace_name, ...per-row metrics... }],
        "timeseries": [{ usage_date, total_usd, charged_usd, free_tier_usd,
                         genie_code_usd, genie_one_usd, genie_agents_usd }],
        "surface_breakdown": [{ surface, total_usd, charged_usd, free_tier_usd }],
      }
    """
    sd, ed = _defaults(start_date, end_date)
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    # Genie gold is daily_sku_breakdown (genie_surface). Missing AIML grain is
    # not a reason to mark this tab unsupported.
    blocked = source_scope_unavailable(
        {"daily_sku_breakdown"},
        "The selected shared source does not publish Genie cost detail.",
    )
    if blocked:
        return _with_genie_inventory({
            **blocked,
            "summary": {},
            "workspaces": [],
            "timeseries": [],
            "surface_breakdown": [],
        })
    dkey = bundle_cache_key("genie:dashboard-bundle", sd, ed, id_list)
    if (cached := delta_cache_get(dkey)) is not None:
        return _with_genie_inventory(cached)

    params = {"start_date": sd, "end_date": ed}
    ws_clause_u = wf.build_ws_filter_clause(col="u.workspace_id", id_list=id_list)

    by_ws_sql = _GENIE_BY_WORKSPACE_SQL.format(ws_clause=ws_clause_u)
    dbx_total_sql = _DATABRICKS_TOTAL_SQL.format(ws_clause=ws_clause_u)
    ts_sql = _GENIE_TIMESERIES_SQL.format(ws_clause=ws_clause_u)

    def _gold_sql(pg_tmpl: str) -> list[dict[str, Any]]:
        cat, sch = get_catalog_schema()
        ws_pg = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
        delta = (
            pg_tmpl.replace("%(start_date)s", ":start_date")
            .replace("%(end_date)s", ":end_date")
            .replace("daily_sku_breakdown", f"`{cat}`.`{sch}`.`daily_sku_breakdown`")
            .replace("daily_usage_summary", f"`{cat}`.`{sch}`.`daily_usage_summary`")
            .replace("NULLIF(workspace_id, '__account__')", "workspace_id")
        )
        sql = delta.format(ws_filter=ws_pg + source_label_filter_clause(delta))
        return execute_query(apply_mv_overrides(sql, cat, sch), params)

    def _fetch_delta(pg_tmpl: str, live_sql: str):
        from backend.db import selected_source_labels
        from backend.lakebase_read import warehouse_view_fallback
        if not warehouse_view_fallback():
            return None
        if selected_source_labels():
            return lambda: _gold_sql(pg_tmpl)
        return lambda: execute_query(live_sql, params)

    def _q_ws() -> list[dict[str, Any]]:
        from backend.lakebase_read import PG_GENIE_BY_WORKSPACE, dual_read
        ws_pg = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
        return dual_read(
            "genie_by_workspace", PG_GENIE_BY_WORKSPACE, params, ws_pg,
            fetch_delta=_fetch_delta(PG_GENIE_BY_WORKSPACE, by_ws_sql),
        )

    def _q_total() -> list[dict[str, Any]]:
        from backend.lakebase_read import PG_ACCOUNT_TOTAL, dual_read
        ws_pg = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
        return dual_read(
            "genie_account_total", PG_ACCOUNT_TOTAL, params, ws_pg,
            fetch_delta=_fetch_delta(PG_ACCOUNT_TOTAL, dbx_total_sql),
        )

    def _q_ts() -> list[dict[str, Any]]:
        from backend.lakebase_read import PG_GENIE_TIMESERIES, dual_read
        ws_pg = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
        return dual_read(
            "genie_timeseries", PG_GENIE_TIMESERIES, params, ws_pg,
            fetch_delta=_fetch_delta(PG_GENIE_TIMESERIES, ts_sql),
        )

    try:
        results = execute_queries_parallel([
            ("by_workspace", _q_ws),
            ("dbx_total", _q_total),
            ("timeseries", _q_ts),
        ], timeout=60.0)
    except Exception as e:
        logger.error("genie dashboard-bundle failed: %s", e, exc_info=True)
        code = str(getattr(e, "code", "GENIE_BUNDLE_FAILED"))
        source_unsupported = code == "SOURCE_SCOPE_UNSUPPORTED"
        return {
            "available": False,
            "reason": "shared_scope_unsupported" if source_unsupported else "producer_failed",
            "reason_detail": (
                "The selected shared source does not publish Genie cost detail."
                if source_unsupported
                else "Genie cost data is temporarily unavailable. Retry shortly."
            ),
            "error_code": code,
            "summary": {},
            "workspaces": [],
            "timeseries": [],
            "surface_breakdown": [],
        }

    ws_rows = results.get("by_workspace") or []
    dbx_total_rows = results.get("dbx_total") or []
    ts_rows = results.get("timeseries") or []

    dbx_total = float((dbx_total_rows[0] if dbx_total_rows else {}).get("databricks_total_usd") or 0.0)

    # Resolve workspace names (best-effort). The account workspace list (cached
    # 1h, shared with the top-nav filter) works on every read path;
    # system.access.workspaces_latest fills gaps where the warehouse is used and
    # may not be granted in every deployment.
    ws_name_map: dict[str, str] = {}
    from backend.lakebase_read import warehouse_view_fallback
    from backend.routers.settings import workspace_names_enabled
    show_names = workspace_names_enabled()
    if ws_rows and show_names:
        try:
            from backend.routers.billing import _get_account_workspace_names
            ws_name_map.update(_get_account_workspace_names())
        except Exception as e:
            logger.debug("account workspace names unavailable (non-fatal): %s", e)
    if ws_rows and show_names and warehouse_view_fallback() and any(
        str(r.get("workspace_id") or "") not in ws_name_map for r in ws_rows
    ):
        try:
            name_rows = execute_query(
                "SELECT workspace_id, workspace_name FROM system.access.workspaces_latest",
                None,
            )
            for r in name_rows or []:
                wid = r.get("workspace_id")
                wname = r.get("workspace_name")
                if wid and wname:
                    ws_name_map.setdefault(str(wid), str(wname))
        except Exception as e:
            logger.debug("workspace name resolution failed (non-fatal): %s", e)

    workspaces: list[dict[str, Any]] = []
    agg = {
        "total_usd": 0.0,
        "charged_usd": 0.0,
        "free_tier_usd": 0.0,
        "other_usage_type_usd": 0.0,
        "total_usd__genie_code": 0.0,
        "charged_usd__genie_code": 0.0,
        "free_tier_usd__genie_code": 0.0,
        "total_usd__genie_one": 0.0,
        "charged_usd__genie_one": 0.0,
        "free_tier_usd__genie_one": 0.0,
        "total_usd__genie_agents": 0.0,
        "charged_usd__genie_agents": 0.0,
        "free_tier_usd__genie_agents": 0.0,
        "total_usd__genie_unknown": 0.0,
        "charged_usd__genie_unknown": 0.0,
        "free_tier_usd__genie_unknown": 0.0,
    }

    for r in ws_rows:
        row = {
            "workspace_id": str(r.get("workspace_id") or ""),
            "workspace_name": _resolve_ws_name(r.get("workspace_id") or "", ws_name_map),
            "total_usd": float(r.get("total_usd") or 0.0),
            "charged_usd": float(r.get("charged_usd") or 0.0),
            "free_tier_usd": float(r.get("free_tier_usd") or 0.0),
            "other_usage_type_usd": float(r.get("other_usage_type_usd") or 0.0),
            "total_usd__genie_code": float(r.get("total_usd__genie_code") or 0.0),
            "charged_usd__genie_code": float(r.get("charged_usd__genie_code") or 0.0),
            "free_tier_usd__genie_code": float(r.get("free_tier_usd__genie_code") or 0.0),
            "total_usd__genie_one": float(r.get("total_usd__genie_one") or 0.0),
            "charged_usd__genie_one": float(r.get("charged_usd__genie_one") or 0.0),
            "free_tier_usd__genie_one": float(r.get("free_tier_usd__genie_one") or 0.0),
            "total_usd__genie_agents": float(r.get("total_usd__genie_agents") or 0.0),
            "charged_usd__genie_agents": float(r.get("charged_usd__genie_agents") or 0.0),
            "free_tier_usd__genie_agents": float(r.get("free_tier_usd__genie_agents") or 0.0),
            "total_usd__genie_unknown": float(r.get("total_usd__genie_unknown") or 0.0),
            "charged_usd__genie_unknown": float(r.get("charged_usd__genie_unknown") or 0.0),
            "free_tier_usd__genie_unknown": float(r.get("free_tier_usd__genie_unknown") or 0.0),
            "earliest_date_with_billing": str(r.get("earliest_date_with_billing")) if r.get("earliest_date_with_billing") else None,
            "latest_date_with_billing": str(r.get("latest_date_with_billing")) if r.get("latest_date_with_billing") else None,
        }
        # Per-row derived pct_free (charged vs free-tier ratio for this workspace).
        row["pct_free"] = _try_div(row["free_tier_usd"], row["charged_usd"] + row["free_tier_usd"])
        row["pct_free__genie_code"] = _try_div(row["free_tier_usd__genie_code"], row["charged_usd__genie_code"] + row["free_tier_usd__genie_code"])
        row["pct_free__genie_one"] = _try_div(row["free_tier_usd__genie_one"], row["charged_usd__genie_one"] + row["free_tier_usd__genie_one"])
        row["pct_free__genie_agents"] = _try_div(row["free_tier_usd__genie_agents"], row["charged_usd__genie_agents"] + row["free_tier_usd__genie_agents"])
        row["pct_free__genie_unknown"] = _try_div(row["free_tier_usd__genie_unknown"], row["charged_usd__genie_unknown"] + row["free_tier_usd__genie_unknown"])
        row["genie_pct_of_databricks_usd"] = _try_div(row["charged_usd"], dbx_total)
        workspaces.append(row)

        for k in agg:
            agg[k] += row[k]

    summary = {
        **agg,
        "workspace_count": len(workspaces),
        "databricks_total_usd": dbx_total,
        "pct_free": _try_div(agg["free_tier_usd"], agg["charged_usd"] + agg["free_tier_usd"]),
        "pct_free__genie_code": _try_div(agg["free_tier_usd__genie_code"], agg["charged_usd__genie_code"] + agg["free_tier_usd__genie_code"]),
        "pct_free__genie_one": _try_div(agg["free_tier_usd__genie_one"], agg["charged_usd__genie_one"] + agg["free_tier_usd__genie_one"]),
        "pct_free__genie_agents": _try_div(agg["free_tier_usd__genie_agents"], agg["charged_usd__genie_agents"] + agg["free_tier_usd__genie_agents"]),
        "pct_free__genie_unknown": _try_div(agg["free_tier_usd__genie_unknown"], agg["charged_usd__genie_unknown"] + agg["free_tier_usd__genie_unknown"]),
        "genie_pct_of_databricks_usd": _try_div(agg["charged_usd"], dbx_total),
        "start_date": sd,
        "end_date": ed,
    }

    surface_breakdown = [
        {
            "surface": "Genie Code",
            "total_usd": agg["total_usd__genie_code"],
            "charged_usd": agg["charged_usd__genie_code"],
            "free_tier_usd": agg["free_tier_usd__genie_code"],
        },
        {
            "surface": "Genie One",
            "total_usd": agg["total_usd__genie_one"],
            "charged_usd": agg["charged_usd__genie_one"],
            "free_tier_usd": agg["free_tier_usd__genie_one"],
        },
        {
            "surface": "Genie Agents",
            "total_usd": agg["total_usd__genie_agents"],
            "charged_usd": agg["charged_usd__genie_agents"],
            "free_tier_usd": agg["free_tier_usd__genie_agents"],
        },
    ]
    # "Genie Unknown" — Genie-inclusion-filter matches with no known surface.
    # Non-zero means the mapping is out of date; the Diagnostics endpoint shows
    # the surface values and SKUs to add to genie_surface_case_sql.
    if (agg["total_usd__genie_unknown"] > 0
            or agg["charged_usd__genie_unknown"] > 0
            or agg["free_tier_usd__genie_unknown"] > 0):
        surface_breakdown.append({
            "surface": "Genie Unknown",
            "total_usd": agg["total_usd__genie_unknown"],
            "charged_usd": agg["charged_usd__genie_unknown"],
            "free_tier_usd": agg["free_tier_usd__genie_unknown"],
        })

    timeseries = [
        {
            "usage_date": str(r.get("usage_date")),
            "total_usd": float(r.get("total_usd") or 0.0),
            "charged_usd": float(r.get("charged_usd") or 0.0),
            "free_tier_usd": float(r.get("free_tier_usd") or 0.0),
            "genie_code_usd": float(r.get("genie_code_usd") or 0.0),
            "genie_one_usd": float(r.get("genie_one_usd") or 0.0),
            "genie_agents_usd": float(r.get("genie_agents_usd") or 0.0),
        }
        for r in ts_rows
    ]

    payload = _with_genie_inventory({
        "summary": summary,
        "workspaces": sorted(workspaces, key=lambda w: w["total_usd"], reverse=True),
        "timeseries": timeseries,
        "surface_breakdown": surface_breakdown,
    })
    # An empty result is cheap to recompute and is what a read returns while the
    # Lakebase copy of daily_sku_breakdown is being reloaded; caching it would
    # show "No Genie spend" for the whole TTL after the reload finishes.
    if ws_rows:
        try:
            delta_cache_put(dkey, "genie:dashboard-bundle", payload, ttl_seconds=cache_ttls.AIML_BUNDLE)
        except Exception as e:
            logger.debug("Delta cache write failed for genie bundle: %s", e)
    return payload


@router.get("/diagnostics")
def get_genie_diagnostics() -> dict[str, Any]:
    """Dump the distinct (sku_name, usage_type, billing_origin_product, genie_surface)
    combinations that pass the Genie inclusion filter, so field engineers can
    validate the sub-surface + free-tier CASE mappings against real customer data.

    Look for:
      - genie_surface values other than GENIE_CODE / GENIE_ONE / GENIE_AGENTS.
        Add them to `genie_surface_case_sql`.
      - Distinct `usage_type` values. If any promotional/free-tier variant
        exists beyond `NON_BILLABLE` and `RELATED`, add it to
        `_CHARGE_CLASS_CASE_SQL`.
    """
    try:
        rows = execute_query(_GENIE_DIAGNOSTICS_SQL, None)
    except Exception as e:
        logger.warning("Genie diagnostics query failed: %s", e)
        return {"available": False, "error": str(e), "rows": []}

    return {
        "available": True,
        "rows": [
            {
                "sku_name": r.get("sku_name"),
                "usage_type": r.get("usage_type"),
                "billing_origin_product": r.get("billing_origin_product"),
                "genie_surface": r.get("genie_surface"),
                "row_count": int(r.get("row_count") or 0),
                "total_usd": float(r.get("total_usd") or 0.0),
                "earliest_date": str(r.get("earliest_date")) if r.get("earliest_date") else None,
                "latest_date": str(r.get("latest_date")) if r.get("latest_date") else None,
            }
            for r in (rows or [])
        ],
        "note": (
            "This dump surfaces distinct SKU / usage_type / billing_origin_product / genie_surface "
            "combinations for any row matching 'GENIE' or 'AI_BI' in sku_name or "
            "billing_origin_product over the last 90 days. Use it to validate the "
            "sub-surface and free-tier mappings in genie_cost.py."
        ),
    }
