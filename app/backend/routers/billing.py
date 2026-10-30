"""Billing API endpoints for cost observability."""

import asyncio
import logging
import os
import time
from datetime import date, timedelta
from typing import Any

from cachetools import TTLCache

from fastapi import APIRouter, HTTPException, Query

from backend.db import (
    execute_query,
    execute_queries_parallel,
    get_catalog_schema,
    get_host_url,
    get_workspace_client,
    bundle_cache_key,
    delta_cache_get,
    delta_cache_put,
    apply_mv_overrides,
    source_scope_unavailable,
    SourceScopeUnsupportedError,
)
from backend import cache_ttls
from backend.queries import (
    ACCOUNT_INFO,
    AWS_COST_ESTIMATE,
    AWS_COST_TIMESERIES,
    BILLING_BY_PRODUCT,
    BILLING_BY_PRODUCT_FAST,
    BILLING_BY_PRODUCT_WORKSPACE,
    BILLING_BY_WORKSPACE,
    BILLING_SUMMARY,
    BILLING_TIMESERIES,
    BILLING_TIMESERIES_FAST,
    ETL_BREAKDOWN,
    INFRA_COST_ESTIMATE,
    INFRA_COST_TIMESERIES,
    INTERACTIVE_BREAKDOWN,
    PIPELINE_OBJECTS,
    BILLING_KPIS_FAST,
    AVG_DAILY_WORKSPACES,
    AVG_DAILY_QUERY_USERS_MV,
    STICKINESS_PCT_QH,
    TOTAL_WORKSPACES_ALLTIME,
    AVG_DAILY_MODELS,
    LAKEFLOW_JOB_STATS,
    PLATFORM_KPIS,
    PLATFORM_KPIS_FAST,
    SKU_BREAKDOWN,
    SPEND_ANOMALIES,
    SQL_TOOL_ATTRIBUTION,
)
from backend.cloud_pricing import (
    get_instance_family,
    get_instance_pricing,
    get_pricing_disclaimer,
    get_cloud_display_name,
)
from backend.materialized_views import (
    MV_BILLING_BY_PRODUCT,
    MV_BILLING_BY_WORKSPACE,
    MV_BILLING_SUMMARY,
    MV_BILLING_TIMESERIES,
    MV_ETL_BREAKDOWN,
    MV_PLATFORM_KPIS,
    MV_SQL_TOOL_ATTRIBUTION,
    check_materialized_views_exist,
)
from backend.materialized_views_extra import (
    MV_INTERACTIVE_BREAKDOWN,
    MV_PIPELINE_OBJECTS,
    MV_SKU_BREAKDOWN,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Stale fallback: stores the last successful non-zero kpis_response per date-range key so
# transient failures (warehouse cold-start, lakeflow replication gap) never show 0s.
_kpis_stale: TTLCache = TTLCache(maxsize=500, ttl=cache_ttls.STALE_KPI)


def _ensure_list(val: Any) -> list:
    """Convert COLLECT_LIST results to a proper Python list.

    Databricks COLLECT_LIST may return Java arrays or stringified arrays
    that don't serialize to JSON properly.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        import json
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        # Try comma-separated string
        stripped = val.strip("[]")
        if stripped:
            return [s.strip().strip("'\"") for s in stripped.split(",")]
        return []
    # Try converting iterable types (Java arrays, etc.)
    try:
        return list(val)
    except (TypeError, ValueError):
        return []

# Cache for MV availability check — re-check after 30 min once confirmed available.
# MVs don't disappear except during an explicit rebuild; 5-min polling was wasteful.
_mv_cache: dict[str, Any] = {"available": None, "checked_at": 0}
_MV_CHECK_INTERVAL = 1800  # 30 minutes


def _check_mv_available() -> bool:
    """Check if materialized views are available (with caching).

    Runs the SQL check in a thread with a 30-second timeout so a slow or
    starting warehouse never blocks a bundle endpoint indefinitely.
    """
    now = time.time()
    if _mv_cache["available"] is not None and (now - _mv_cache["checked_at"]) < _MV_CHECK_INTERVAL:
        return _mv_cache["available"]

    import threading
    from concurrent.futures import Future, wait as _cfwait

    def _check():
        catalog, schema = get_catalog_schema()
        tables = check_materialized_views_exist(catalog, schema)
        core_tables = ["daily_usage_summary", "daily_product_breakdown", "daily_workspace_breakdown"]
        return all(tables.get(t, False) for t in core_tables)

    future: Future = Future()

    def _daemon_run(f=future, fn=_check):
        try:
            f.set_result(fn())
        except Exception as e:
            f.set_exception(e)

    threading.Thread(target=_daemon_run, daemon=True, name="sql-mv-check").start()
    done, _ = _cfwait([future], timeout=30.0)
    if not done:
        # Don't lock out MVs for 30 min on a transient timeout (e.g. cold SDK init after
        # restart). Cache the negative result for only 15s so the next request retries.
        logger.debug("MV availability check timed out after 30s — retrying in 15s")
        _mv_cache["available"] = False
        _mv_cache["checked_at"] = now - (_MV_CHECK_INTERVAL - 15)
        return False
    try:
        available = future.result()
        _mv_cache["available"] = available
        _mv_cache["checked_at"] = now
        if available:
            logger.info("Materialized views available - using optimized queries")
        return available
    except Exception as e:
        # Treat exceptions the same as timeouts: retry in 15s, not 30 min.
        # A transient SDK error on startup should not lock out MVs for half an hour.
        logger.debug("MV check failed (retrying in 15s): %s", e)
        _mv_cache["available"] = False
        _mv_cache["checked_at"] = now - (_MV_CHECK_INTERVAL - 15)
        return False


def _get_mv_query(mv_query: str, ws_filter: str = "") -> str:
    """Format a materialized view query with the correct catalog/schema and apply table overrides.

    The active source-label selection (if any) is appended into the same
    `{ws_filter}` slot the workspace filter uses, so MV reads against the unified
    views are narrowed to the chosen sources. No-op when nothing is selected or
    no additional sources are configured.
    """
    from backend.db import apply_mv_overrides, source_label_filter_clause
    catalog, schema = get_catalog_schema()
    sql = mv_query.format(
        catalog=catalog, schema=schema,
        ws_filter=ws_filter + source_label_filter_clause(mv_query),
    )
    return apply_mv_overrides(sql, catalog, schema)


def _optional_breakdown_unavailable(
    kind: str,
    collection: str,
    params: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    """Return a safe, settled 200 contract for non-critical DBU detail."""
    code = str(getattr(exc, "code", "QUERY_FAILED"))
    source_unsupported = code == "SOURCE_SCOPE_UNSUPPORTED"
    logger.warning("%s breakdown unavailable (%s): %s", kind, code, exc)
    return {
        "available": False,
        "reason": "shared_scope_unsupported" if source_unsupported else "producer_failed",
        "reason_detail": (
            f"The selected shared source does not publish {kind} detail."
            if source_unsupported
            else f"{kind.capitalize()} detail is temporarily unavailable. Retry shortly."
        ),
        "error_code": code,
        collection: [],
        "total_spend": None if source_unsupported else 0,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _exec_mv(mv_template: str, params: dict, ws_filter: str = "") -> list[dict]:
    """Serve a gold aggregate. Postgres first when COST_OBS_LAKEBASE_READ=on.

    On Postgres failure, read the warehouse *gold copy* (daily_* tables), never
    live system.* scans. Live system SQL stays behind `_live_if_allowed`.
    """
    from backend.lakebase_read import dual_read_mv
    return dual_read_mv(
        mv_template,
        params,
        ws_filter,
        fetch_delta=lambda: execute_query(_get_mv_query(mv_template, ws_filter), params),
    )


def _live_if_allowed(sql: str, params: dict) -> list[dict]:
    """Hit live warehouse SQL only when Lakebase serving is not on."""
    from backend.lakebase_read import warehouse_view_fallback
    if not warehouse_view_fallback():
        return []
    return execute_query(sql, params)


def _mv_ws_clause(id_list: list[str] | None) -> str:
    """Build a workspace filter clause for MV queries (plain workspace_id column, no table alias)."""
    from backend import workspace_filter as wf
    return wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)


def get_workspace_name() -> str | None:
    """Get workspace name from Databricks SDK."""
    try:
        w = get_workspace_client()
        host = w.config.host or ""
        if host:
            # Extract workspace name from host
            # e.g., https://e2-demo-field-eng.cloud.databricks.com
            parts = host.replace("https://", "").replace("http://", "").split(".")
            if parts:
                return parts[0]
        return None
    except Exception:
        return None


@router.get("/account")
async def get_account_info() -> dict[str, Any]:
    """Get account information — returns instantly from host URL, no SQL query needed."""
    result: dict[str, Any] = {
        "account_id": None,
        "account_name": None,
        "cloud": None,
        "host": None,
    }

    # Instant: detect everything from host URL
    host = get_host_url()
    if host:
        result["host"] = host
        parts = host.replace("https://", "").replace("http://", "").split(".")
        if parts:
            result["account_name"] = parts[0]
        host_lower = host.lower()
        if "azuredatabricks.net" in host_lower:
            result["cloud"] = "AZURE"
            # Use Azure subscription ID (not available from host URL, set via env)
            result["account_id"] = os.environ.get("AZURE_SUBSCRIPTION_ID", None)
        elif "gcp.databricks.com" in host_lower:
            result["cloud"] = "GCP"
        elif "cloud.databricks.com" in host_lower:
            result["cloud"] = "AWS"

    return result


@router.get("/account-details")
async def get_account_details() -> dict[str, Any]:
    """Get account_id from billing data — may be slow, called separately."""
    try:
        results = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, lambda: execute_query(ACCOUNT_INFO)),
            timeout=10.0
        )
        if results:
            row = results[0]
            return {
                "account_id": row.get("account_id"),
                "cloud": row.get("cloud"),
            }
    except Exception as e:
        logger.warning(f"Could not query account details from billing tables: {e}")
    return {"account_id": None, "cloud": None}


def get_default_start_date() -> str:
    """Get default start date (last 30 days)."""
    return (date.today() - timedelta(days=30)).isoformat()


def get_default_end_date() -> str:
    """Get default end date (today)."""
    return date.today().isoformat()


@router.get("/summary")
async def get_billing_summary(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Get overall billing summary (total spend, DBUs, etc.)."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)
    use_mv = await asyncio.to_thread(_check_mv_available)

    if use_mv:
        results = await asyncio.to_thread(_exec_mv, MV_BILLING_SUMMARY, params, _mv_ws_clause(id_list))
        if not results:
            results = await asyncio.to_thread(
                _live_if_allowed, _inject_ws_filter(BILLING_SUMMARY, ws_clause), params
            )
    else:
        results = await asyncio.to_thread(execute_query, _inject_ws_filter(BILLING_SUMMARY, ws_clause), params)

    if not results:
        return {
            "total_dbus": 0,
            "total_spend": 0,
            "workspace_count": 0,
            "days_in_range": 0,
            "avg_daily_spend": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    row = results[0]
    days = row.get("days_in_range") or 1
    total_spend = float(row.get("total_spend") or 0)

    return {
        "total_dbus": float(row.get("total_dbus") or 0),
        "total_spend": total_spend,
        "workspace_count": row.get("workspace_count") or 0,
        "days_in_range": days,
        "avg_daily_spend": total_spend / days if days > 0 else 0,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "first_date": str(row.get("first_date")) if row.get("first_date") else None,
        "last_date": str(row.get("last_date")) if row.get("last_date") else None,
    }


@router.get("/by-product")
async def get_billing_by_product(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_id: str = Query(default=None, description="Filter by workspace ID"),
) -> dict[str, Any]:
    """Get billing breakdown by product category."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend import workspace_filter as wf
    id_list = [workspace_id] if workspace_id else None
    if workspace_id:
        params["workspace_id"] = workspace_id
    ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
    results = await asyncio.to_thread(_exec_mv, MV_BILLING_BY_PRODUCT, params, ws_clause)
    if not results:
        live = BILLING_BY_PRODUCT_WORKSPACE if workspace_id else BILLING_BY_PRODUCT
        results = await asyncio.to_thread(_live_if_allowed, live, params)

    products = []
    total_spend = 0

    for row in (results or []):
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        products.append(
            {
                "category": row.get("product_category"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "workspace_count": row.get("workspace_count") or 0,
            }
        )

    # Calculate percentages
    for product in products:
        product["percentage"] = (
            (product["total_spend"] / total_spend * 100) if total_spend > 0 else 0
        )

    return {
        "products": products,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/by-workspace")
async def get_billing_by_workspace(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get billing breakdown by workspace."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    # Always use the live query here — the MV lacks top_products and top_users columns.
    results = await asyncio.to_thread(execute_query, BILLING_BY_WORKSPACE, params)
    return _format_workspaces(results, params)


@router.get("/timeseries")
async def get_billing_timeseries(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get daily billing time series by product category."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(execute_query, BILLING_TIMESERIES, params)

    # Transform to chart-friendly format: [{date, SQL, ETL, Interactive, ...}, ...]
    date_data: dict[str, dict[str, float]] = {}

    for row in (results or []):
        date_str = str(row.get("usage_date"))
        category = row.get("product_category")
        spend = float(row.get("total_spend") or 0)

        if date_str not in date_data:
            date_data[date_str] = {"date": date_str}

        date_data[date_str][category] = spend

    # Convert to list sorted by date
    timeseries = sorted(date_data.values(), key=lambda x: x["date"])

    # Get all categories
    categories = set()
    for row in results:
        categories.add(row.get("product_category"))

    return {
        "timeseries": timeseries,
        "categories": sorted(list(categories)),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/sql-breakdown")
async def get_sql_breakdown(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """Get SQL breakdown by tool (DBSQL vs Genie).

    Uses materialized views when available for fast queries.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    _dkey = bundle_cache_key("billing:sql-breakdown", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached

    try:
        from backend import workspace_filter as wf
        ws_clause = wf.build_ws_filter_clause(id_list=id_list)
        use_mv = await asyncio.to_thread(_check_mv_available)
        if use_mv:
            results = await asyncio.to_thread(_exec_mv, MV_SQL_TOOL_ATTRIBUTION, params, _mv_ws_clause(id_list))
        else:
            results = await asyncio.to_thread(execute_query, _inject_ws_filter(SQL_TOOL_ATTRIBUTION, ws_clause), params)

        products = []
        total_spend = 0

        for row in results:
            spend = float(row.get("total_spend") or 0)
            total_spend += spend
            products.append(
                {
                    "product": row.get("sql_product"),
                    "total_dbus": float(row.get("total_dbus") or 0),
                    "total_spend": spend,
                }
            )

        # Calculate percentages
        for product in products:
            product["percentage"] = (
                (product["total_spend"] / total_spend * 100) if total_spend > 0 else 0
            )

        _resp = {
            "products": products,
            "total_spend": total_spend,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "using_materialized_views": use_mv,
        }
        delta_cache_put(_dkey, "billing:sql-breakdown", _resp, ttl_seconds=cache_ttls.BUNDLE)
        return _resp
    except Exception as e:
        # If query.history is not available, return empty result
        return {
            "products": [],
            "total_spend": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "error": f"SQL breakdown not available: {str(e)}",
        }


@router.get("/etl-breakdown")
async def get_etl_breakdown(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get ETL breakdown (Batch vs Streaming)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(_exec_mv, MV_ETL_BREAKDOWN, params, "")
    if not results:
        results = await asyncio.to_thread(_live_if_allowed, ETL_BREAKDOWN, params)

    products = []
    total_spend = 0

    for row in (results or []):
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        products.append(
            {
                "product": row.get("etl_type"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
            }
        )

    # Calculate percentages
    for product in products:
        product["percentage"] = (
            (product["total_spend"] / total_spend * 100) if total_spend > 0 else 0
        )

    return {
        "products": products,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/pipeline-objects")
async def get_pipeline_objects(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Get spend breakdown by pipeline objects (Jobs and SDP pipelines)."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    _dkey = bundle_cache_key("billing:pipeline-objects", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    blocked = source_scope_unavailable(
        {"daily_job_pipeline_spend"},
        "The selected shared source does not publish Jobs and pipeline detail.",
    )
    if blocked:
        return {**blocked, "objects": [], "total_spend": 0,
                "start_date": params["start_date"], "end_date": params["end_date"]}

    try:
        raw = await asyncio.to_thread(_exec_mv, MV_PIPELINE_OBJECTS, params, _mv_ws_clause(id_list))
        if not raw:
            raw = await asyncio.to_thread(
                _live_if_allowed, _inject_ws_filter(PIPELINE_OBJECTS, ws_clause), params
            )
        raw_copy = [dict(r) for r in (raw or [])]
        try:
            results = await asyncio.wait_for(asyncio.to_thread(_enrich_pipeline_results, raw_copy), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning("Pipeline name enrichment timed out; serving raw results")
            results = raw

        objects = []
        total_spend = 0

        for row in (results or []):
            spend = float(row.get("total_spend") or 0)
            total_spend += spend
            obj_name = row.get("object_name")
            obj_id = row.get("object_id")
            objects.append(
                {
                    "object_type": row.get("object_type"),
                    "object_id": obj_id,
                    "object_name": obj_name,
                    "workspace_id": str(row.get("workspace_id") or ""),
                    "object_state": row.get("object_state"),
                    "owner": row.get("owner"),
                    "total_dbus": float(row.get("total_dbus") or 0),
                    "total_spend": spend,
                    "total_runs": int(row.get("total_runs") or 0),
                }
            )

        # Calculate percentages
        for obj in objects:
            obj["percentage"] = (
                (obj["total_spend"] / total_spend * 100) if total_spend > 0 else 0
            )

        _resp = {
            "objects": objects,
            "total_spend": total_spend,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
        delta_cache_put(_dkey, "billing:pipeline-objects", _resp, ttl_seconds=cache_ttls.BUNDLE)
        return _resp
    except Exception as e:
        return _optional_breakdown_unavailable("pipeline", "objects", params, e)


@router.get("/interactive-breakdown")
async def get_interactive_breakdown(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Get Interactive compute breakdown by notebook, user, and cluster."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    _dkey = bundle_cache_key("billing:interactive-breakdown", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    blocked = source_scope_unavailable(
        {"daily_interactive_spend"},
        "The selected shared source does not publish Interactive compute detail.",
    )
    if blocked:
        return {**blocked, "items": [], "total_spend": 0,
                "start_date": params["start_date"], "end_date": params["end_date"]}

    try:
        results = await asyncio.to_thread(_exec_mv, MV_INTERACTIVE_BREAKDOWN, params, _mv_ws_clause(id_list))
        if not results:
            results = await asyncio.to_thread(
                _live_if_allowed, _inject_ws_filter(INTERACTIVE_BREAKDOWN, ws_clause), params
            )

        items = []
        total_spend = 0

        for row in (results or []):
            spend = float(row.get("total_spend") or 0)
            total_spend += spend
            items.append(
                {
                    "cluster_id": row.get("cluster_id"),
                    "cluster_name": row.get("cluster_name"),
                    "notebook_path": row.get("notebook_path"),
                    "user": row.get("run_as_user"),
                    "workspace_id": row.get("workspace_id"),
                    "cluster_state": row.get("cluster_state"),
                    "total_dbus": float(row.get("total_dbus") or 0),
                    "total_spend": spend,
                    "days_active": row.get("days_active") or 0,
                    "notebook_count": row.get("notebook_count") or 0,
                }
            )

        # Calculate percentages
        for item in items:
            item["percentage"] = (
                (item["total_spend"] / total_spend * 100) if total_spend > 0 else 0
            )

        _resp = {
            "items": items,
            "total_spend": total_spend,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
        delta_cache_put(_dkey, "billing:interactive-breakdown", _resp, ttl_seconds=cache_ttls.BUNDLE)
        return _resp
    except Exception as e:
        return _optional_breakdown_unavailable("interactive", "items", params, e)


@router.get("/infra-costs")
async def get_infra_costs(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get estimated infrastructure costs based on cluster instance types.

    Automatically detects the cloud provider (AWS or Azure) and uses appropriate pricing.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    blocked = source_scope_unavailable(
        {"daily_infra_cluster_spend"},
        "Classic infrastructure metadata is unavailable for the selected shared sources.",
    )
    if blocked:
        return {
            **blocked,
            "cloud": "UNKNOWN",
            "cloud_display_name": "Cloud",
            "clusters": [],
            "instance_families": [],
            "total_estimated_cost": None,
            "total_databricks_spend": 0,
            "total_dbu_hours": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    try:
        # Single query — instance families are derived in Python from cluster results
        from backend.lakebase_read import PG_INFRA_CLUSTERS, gold_read
        cluster_results = await asyncio.to_thread(
            gold_read, "infra_clusters", PG_INFRA_CLUSTERS, params, "", INFRA_COST_ESTIMATE
        )

        # Detect cloud from results, fall back to host URL detection
        host = get_host_url()
        cloud = "AWS"
        if host:
            h = host.lower()
            if "azuredatabricks.net" in h:
                cloud = "AZURE"
            elif "gcp.databricks.com" in h:
                cloud = "GCP"
        if cluster_results:
            for row in cluster_results:
                if row.get("cloud"):
                    cloud = row.get("cloud")
                    break

        clusters = []
        total_estimated_cost = 0
        total_dbu_hours = 0
        family_agg: dict[str, dict] = {}

        for row in cluster_results:
            dbu_hours = float(row.get("total_dbu_hours") or 0)
            driver_type = row.get("driver_instance_type")
            worker_type = row.get("worker_instance_type")

            driver_cost = get_instance_pricing(driver_type, cloud)
            worker_cost = get_instance_pricing(worker_type, cloud)
            estimated_cost = dbu_hours * (driver_cost + worker_cost * 2) / 2

            total_estimated_cost += estimated_cost
            total_dbu_hours += dbu_hours

            clusters.append({
                "cluster_id": row.get("cluster_id"),
                "cluster_name": row.get("cluster_name"),
                "driver_instance_type": driver_type,
                "worker_instance_type": worker_type,
                "cluster_source": row.get("cluster_source"),
                "workspace_id": str(row.get("workspace_id") or ""),
                "workspace_name": row.get("workspace_name"),
                "total_dbu_hours": dbu_hours,
                "estimated_cost": estimated_cost,
                "days_active": row.get("days_active") or 0,
            })

            # Aggregate instance families from cluster data — no second query needed
            for itype in [driver_type, worker_type]:
                if itype:
                    family = get_instance_family(itype, cloud)
                    days = row.get("days_active") or 0
                    if family in family_agg:
                        family_agg[family]["total_dbu_hours"] += dbu_hours
                        family_agg[family]["days_active"] = max(family_agg[family]["days_active"], days)
                    else:
                        family_agg[family] = {"instance_family": family, "total_dbu_hours": dbu_hours, "days_active": days}

        for cluster in clusters:
            cluster["percentage"] = (
                (cluster["estimated_cost"] / total_estimated_cost * 100)
                if total_estimated_cost > 0 else 0
            )
        instance_families = sorted(family_agg.values(), key=lambda f: f["total_dbu_hours"], reverse=True)

        return {
            "cloud": cloud,
            "cloud_display_name": get_cloud_display_name(cloud),
            "clusters": clusters,
            "instance_families": instance_families,
            "total_estimated_cost": total_estimated_cost,
            "total_dbu_hours": total_dbu_hours,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "disclaimer": get_pricing_disclaimer(cloud),
        }
    except Exception as e:
        return {
            "cloud": "UNKNOWN",
            "cloud_display_name": "Cloud",
            "clusters": [],
            "instance_families": [],
            "total_estimated_cost": 0,
            "total_dbu_hours": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "error": f"Infrastructure cost estimation not available: {str(e)}",
        }


@router.get("/infra-costs-timeseries")
async def get_infra_costs_timeseries(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get estimated infrastructure costs over time (daily).

    Automatically detects cloud provider and uses appropriate pricing.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    blocked = source_scope_unavailable(
        {"daily_infra_cluster_spend"},
        "Infrastructure history is unavailable for the selected shared sources.",
    )
    if blocked:
        return {
            **blocked,
            "cloud": "UNKNOWN",
            "cloud_display_name": "Cloud",
            "timeseries": [],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    try:
        from backend.lakebase_read import PG_INFRA_TIMESERIES, gold_read
        results = await asyncio.to_thread(
            gold_read, "infra_timeseries", PG_INFRA_TIMESERIES, params, "", INFRA_COST_TIMESERIES
        )

        # Detect cloud from host URL, override with billing data if available
        host = get_host_url()
        cloud = "AWS"
        if host:
            h = host.lower()
            if "azuredatabricks.net" in h:
                cloud = "AZURE"
            elif "gcp.databricks.com" in h:
                cloud = "GCP"
        if results:
            for row in results:
                if row.get("cloud"):
                    cloud = row.get("cloud")
                    break

        timeseries = []
        for row in results:
            dbu_hours = float(row.get("total_dbu_hours") or 0)
            # Use average pricing for timeseries (rough estimate)
            avg_cost_per_hour = 0.50  # Reasonable average
            estimated_cost = dbu_hours * avg_cost_per_hour

            timeseries.append(
                {
                    "date": str(row.get("usage_date")),
                    "Infrastructure Cost": estimated_cost,
                    "total_dbu_hours": dbu_hours,
                }
            )

        return {
            "cloud": cloud,
            "cloud_display_name": get_cloud_display_name(cloud),
            "timeseries": timeseries,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
    except Exception as e:
        return {
            "cloud": "UNKNOWN",
            "cloud_display_name": "Cloud",
            "timeseries": [],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "error": f"Infrastructure cost timeseries not available: {str(e)}",
        }


@router.get("/infra-bundle")
async def get_infra_bundle(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Bundled infra endpoint: runs cluster costs, instance families, and timeseries in parallel."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    blocked = source_scope_unavailable(
        {"daily_infra_cluster_spend"},
        "Classic infrastructure metadata is unavailable for the selected shared sources.",
    )
    if blocked:
        return {
            **blocked,
            "cloud": "UNKNOWN",
            "cloud_display_name": "Cloud",
            "clusters": [],
            "instance_families": [],
            "timeseries": [],
            "total_estimated_cost": None,
            "total_dbu_hours": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    # Delta cross-worker cache
    _dkey = bundle_cache_key("billing:infra-bundle", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached

    _ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    # Billing-based summary query — matches KPI trend drill-downs exactly
    BILLING_INFRA_SUMMARY = """
    WITH usage_with_price AS (
      SELECT
        u.usage_date,
        u.usage_quantity,
        u.usage_metadata.cluster_id as cluster_id,
        COALESCE(p.pricing.default, 0) as price_per_dbu
      FROM system.billing.usage u
      LEFT JOIN system.billing.list_prices p
        ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
      WHERE u.usage_date BETWEEN :start_date AND :end_date
        AND u.usage_quantity > 0
        AND u.usage_metadata.cluster_id IS NOT NULL
        AND (u.sku_name LIKE '%ALL_PURPOSE%' OR u.sku_name LIKE '%JOBS%' OR u.sku_name LIKE '%DLT%')
    ),
    daily_stats AS (
      SELECT
        usage_date,
        SUM(usage_quantity * price_per_dbu) as daily_cost,
        COUNT(DISTINCT cluster_id) as daily_clusters
      FROM usage_with_price
      GROUP BY usage_date
    )
    SELECT
      SUM(daily_cost) as total_cost,
      AVG(daily_clusters) as avg_clusters_per_day,
      CASE WHEN AVG(daily_clusters) > 0 THEN AVG(daily_cost / daily_clusters) ELSE 0 END as avg_cost_per_cluster,
      COUNT(*) as days_in_range
    FROM daily_stats
    """

    _infra_sql = _inject_ws_filter(BILLING_INFRA_SUMMARY, _ws_clause)
    _clusters_sql = _inject_ws_filter(INFRA_COST_ESTIMATE, _ws_clause)
    _ts_sql = _inject_ws_filter(INFRA_COST_TIMESERIES, _ws_clause)
    try:
        from backend.lakebase_read import PG_INFRA_CLUSTERS, PG_INFRA_SUMMARY, PG_INFRA_TIMESERIES, gold_read
        query_results = await asyncio.to_thread(execute_queries_parallel, [
            ("clusters", lambda: gold_read("infra_clusters", PG_INFRA_CLUSTERS, params, _ws_clause, _clusters_sql)),
            ("timeseries", lambda: gold_read("infra_timeseries", PG_INFRA_TIMESERIES, params, _ws_clause, _ts_sql)),
            ("billing_summary", lambda: gold_read("infra_summary", PG_INFRA_SUMMARY, params, _ws_clause, _infra_sql)),
        ], timeout=90.0)

        cluster_results = query_results.get("clusters") or []
        ts_results = query_results.get("timeseries") or []
        billing_summary_results = query_results.get("billing_summary") or []

        # Detect cloud from host URL, override with billing data if available
        host = get_host_url()
        cloud = "AWS"
        if host:
            h = host.lower()
            if "azuredatabricks.net" in h:
                cloud = "AZURE"
            elif "gcp.databricks.com" in h:
                cloud = "GCP"
        if cluster_results:
            for row in cluster_results:
                if row.get("cloud"):
                    cloud = row.get("cloud")
                    break

        # --- Build clusters and instance families in one pass ---
        # Pre-build a pricing map for all unique instance types to avoid repeated lookups
        all_types = {row.get("driver_instance_type") for row in cluster_results} | {row.get("worker_instance_type") for row in cluster_results}
        pricing_map = {t: get_instance_pricing(t, cloud) for t in all_types if t}
        family_map = {t: get_instance_family(t, cloud) for t in all_types if t}

        clusters = []
        total_estimated_cost = 0
        total_dbu_hours = 0
        family_agg: dict[str, dict] = {}

        for row in cluster_results:
            dbu_hours = float(row.get("total_dbu_hours") or 0)
            driver_type = row.get("driver_instance_type")
            worker_type = row.get("worker_instance_type")
            driver_cost = pricing_map.get(driver_type, 0.0) if driver_type else 0.0
            worker_cost = pricing_map.get(worker_type, 0.0) if worker_type else 0.0
            estimated_cost = dbu_hours * (driver_cost + worker_cost * 2) / 2
            total_estimated_cost += estimated_cost
            total_dbu_hours += dbu_hours
            clusters.append({
                "cluster_id": row.get("cluster_id"),
                "cluster_name": row.get("cluster_name"),
                "driver_instance_type": driver_type,
                "worker_instance_type": worker_type,
                "cluster_source": row.get("cluster_source"),
                "workspace_id": str(row.get("workspace_id") or ""),
                "workspace_name": row.get("workspace_name"),
                "total_dbu_hours": dbu_hours,
                "estimated_cost": estimated_cost,
                "days_active": row.get("days_active") or 0,
            })
            # Derive instance families from cluster data — no second query needed
            for itype in [driver_type, worker_type]:
                if itype:
                    family = family_map.get(itype) or get_instance_family(itype, cloud)
                    days = row.get("days_active") or 0
                    if family in family_agg:
                        family_agg[family]["total_dbu_hours"] += dbu_hours
                        family_agg[family]["days_active"] = max(family_agg[family]["days_active"], days)
                    else:
                        family_agg[family] = {"instance_family": family, "total_dbu_hours": dbu_hours, "days_active": days}

        for cluster in clusters:
            cluster["percentage"] = (
                (cluster["estimated_cost"] / total_estimated_cost * 100)
                if total_estimated_cost > 0 else 0
            )
        instance_families = sorted(family_agg.values(), key=lambda f: f["total_dbu_hours"], reverse=True)

        # --- Build timeseries ---
        timeseries = []
        for row in ts_results:
            dbu_hours = float(row.get("total_dbu_hours") or 0)
            avg_cost_per_hour = 0.50
            estimated_cost = dbu_hours * avg_cost_per_hour
            timeseries.append({
                "date": str(row.get("usage_date")),
                "Infrastructure Cost": estimated_cost,
                "total_dbu_hours": dbu_hours,
            })

        # Extract billing-based summary (matches KPI drill-downs)
        billing_summary = {}
        if billing_summary_results:
            bs = billing_summary_results[0]
            billing_summary = {
                "total_cost": float(bs.get("total_cost") or 0),
                "avg_clusters_per_day": round(float(bs.get("avg_clusters_per_day") or 0)),
                "avg_cost_per_cluster": float(bs.get("avg_cost_per_cluster") or 0),
                "days_in_range": int(bs.get("days_in_range") or 0),
            }

        _resp = {
            "infra_costs": {
                "cloud": cloud,
                "cloud_display_name": get_cloud_display_name(cloud),
                "clusters": clusters,
                "instance_families": instance_families,
                "total_estimated_cost": total_estimated_cost,
                "total_dbu_hours": total_dbu_hours,
                "billing_summary": billing_summary,
                "start_date": params["start_date"],
                "end_date": params["end_date"],
                "disclaimer": get_pricing_disclaimer(cloud),
            },
            "infra_timeseries": {
                "cloud": cloud,
                "cloud_display_name": get_cloud_display_name(cloud),
                "timeseries": timeseries,
                "start_date": params["start_date"],
                "end_date": params["end_date"],
            },
        }
        delta_cache_put(_dkey, "billing:infra-bundle", _resp, ttl_seconds=cache_ttls.BUNDLE_FILTERED if id_list else cache_ttls.BUNDLE)
        return _resp
    except Exception as e:
        logger.error(f"Infra bundle error: {e}")
        host = get_host_url()
        err_cloud = "AWS"
        if host:
            h = host.lower()
            if "azuredatabricks.net" in h: err_cloud = "AZURE"
            elif "gcp.databricks.com" in h: err_cloud = "GCP"
        empty = {
            "cloud": err_cloud, "cloud_display_name": get_cloud_display_name(err_cloud),
            "start_date": params["start_date"], "end_date": params["end_date"],
        }
        return {
            "infra_costs": {**empty, "clusters": [], "instance_families": [], "total_estimated_cost": 0, "total_dbu_hours": 0, "error": str(e)},
            "infra_timeseries": {**empty, "timeseries": [], "error": str(e)},
        }


async def _compute_cloud_costs_bundle(
    start_date: str,
    end_date: str,
    workspace_ids: str | None,
) -> dict[str, Any]:
    """Compute Cloud Costs: required infra totals plus optional invoice actuals."""
    from backend.routers import aws_actual, azure_actual, gcp_actual

    infra = await get_infra_bundle(
        start_date=start_date,
        end_date=end_date,
        workspace_ids=workspace_ids,
    )
    infra_error = (infra.get("infra_costs") or {}).get("error")
    if infra_error:
        raise HTTPException(status_code=503, detail=str(infra_error))

    provider_calls = (
        ("aws_actual", aws_actual.get_aws_actual_dashboard_bundle),
        ("azure_actual", azure_actual.get_azure_actual_dashboard_bundle),
        ("gcp_actual", gcp_actual.get_gcp_actual_dashboard_bundle),
    )
    providers: dict[str, Any] = {}
    partial_reasons: dict[str, str] = {}
    for name, call in provider_calls:
        try:
            providers[name] = await call(start_date, end_date)
            if providers[name].get("availability") == "partial":
                partial_reasons[name] = "OPTIONAL_DETAIL_FAILED"
        except Exception as exc:
            logger.warning("Optional %s Cloud bundle failed: %s", name, exc)
            code = str(getattr(exc, "code", "QUERY_FAILED"))
            partial_reasons[name] = code
            providers[name] = {
                "available": False,
                "availability": "unavailable",
                "transient_error": True,
                "error_code": code,
                "message": (
                    f"{name.replace('_', ' ').title()} is temporarily unavailable. "
                    "Usage & Metadata remains available; retry Cloud Costs shortly."
                ),
                "start_date": start_date,
                "end_date": end_date,
            }

    return {
        "availability": "partial" if partial_reasons else "available",
        "partial_reasons": partial_reasons,
        "infra_bundle": infra,
        **providers,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/cloud-costs-bundle")
async def get_cloud_costs_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """One Cloud Costs payload: infra totals plus optional cloud invoice actuals."""
    params_start = start_date or get_default_start_date()
    params_end = end_date or get_default_end_date()
    cache_key = bundle_cache_key(
        "billing:cloud-costs-bundle",
        params_start,
        params_end,
        [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None,
    )
    if (cached := delta_cache_get(cache_key)) is not None:
        return cached

    payload = await _compute_cloud_costs_bundle(params_start, params_end, workspace_ids)
    delta_cache_put(
        cache_key,
        "billing:cloud-costs-bundle",
        payload,
        ttl_seconds=cache_ttls.BUNDLE_FILTERED if workspace_ids else cache_ttls.BUNDLE,
    )
    return payload


@router.get("/aws-costs")
async def get_aws_costs(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get estimated AWS infrastructure costs based on cluster instance types.

    DEPRECATED: Use /infra-costs instead for multi-cloud support.
    This endpoint is maintained for backwards compatibility.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    blocked = source_scope_unavailable(
        {"daily_infra_cluster_spend"},
        "Classic infrastructure metadata is unavailable for the selected shared sources.",
    )
    if blocked:
        return {
            **blocked,
            "clusters": [],
            "instance_families": [],
            "total_estimated_cost": None,
            "total_databricks_spend": 0,
            "total_dbu_hours": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    try:
        # Get detailed cluster costs
        from backend.lakebase_read import PG_INFRA_CLUSTERS, gold_read
        cluster_results = await asyncio.to_thread(
            gold_read, "infra_clusters", PG_INFRA_CLUSTERS, params, "", AWS_COST_ESTIMATE
        )

        clusters = []
        total_estimated_cost = 0
        total_dbu_hours = 0

        for row in cluster_results:
            cost = _cluster_vm_cost(row, "AWS")
            dbu_hours = float(row.get("total_dbu_hours") or 0)
            total_estimated_cost += cost
            total_dbu_hours += dbu_hours
            clusters.append(
                {
                    "cluster_id": row.get("cluster_id"),
                    "cluster_name": row.get("cluster_name"),
                    "driver_instance_type": row.get("driver_instance_type"),
                    "worker_instance_type": row.get("worker_instance_type"),
                    "cluster_source": row.get("cluster_source"),
                    "total_dbu_hours": dbu_hours,
                    "estimated_aws_cost": cost,
                    "days_active": row.get("days_active") or 0,
                }
            )

        # Calculate percentages
        for cluster in clusters:
            cluster["percentage"] = (
                (cluster["estimated_aws_cost"] / total_estimated_cost * 100)
                if total_estimated_cost > 0
                else 0
            )

        instance_families = _families_from_cluster_rows(cluster_results, "AWS")

        return {
            "clusters": clusters,
            "instance_families": instance_families,
            "total_estimated_cost": total_estimated_cost,
            "total_dbu_hours": total_dbu_hours,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "disclaimer": "AWS costs are estimated based on EC2 On-Demand pricing (US East). Actual costs may vary based on region, reserved instances, and spot pricing.",
        }
    except Exception as e:
        return {
            "clusters": [],
            "instance_families": [],
            "total_estimated_cost": 0,
            "total_dbu_hours": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "error": f"AWS cost estimation not available: {str(e)}",
        }


@router.get("/aws-costs-timeseries")
async def get_aws_costs_timeseries(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get estimated AWS infrastructure costs over time (daily rolling aggregate)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    blocked = source_scope_unavailable(
        {"daily_infra_cluster_spend"},
        "Infrastructure history is unavailable for the selected shared sources.",
    )
    if blocked:
        return {
            **blocked,
            "timeseries": [],
            "instance_families": [],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    try:
        from backend.lakebase_read import PG_INFRA_TIMESERIES, gold_read
        results = await asyncio.to_thread(
            gold_read, "infra_timeseries", PG_INFRA_TIMESERIES, params, "", AWS_COST_TIMESERIES
        )
        return _format_aws_timeseries(results, params)
    except Exception as e:
        return {
            "timeseries": [],
            "instance_families": [],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "error": f"AWS cost timeseries not available: {str(e)}",
        }

_pipeline_names_cache: dict[str, str] | None = None
_pipeline_names_cache_ts: float = 0
_PIPELINE_CACHE_TTL = cache_ttls.PIPELINE_NAMES


def _get_pipeline_names() -> dict[str, str]:
    """Get pipeline ID → name mapping. Try system table first, fall back to SDK. Cached for 1 hour."""
    global _pipeline_names_cache, _pipeline_names_cache_ts
    import time as _time
    now = _time.monotonic()
    if _pipeline_names_cache is not None and (now - _pipeline_names_cache_ts) < _PIPELINE_CACHE_TTL:
        return _pipeline_names_cache

    from backend.lakebase_read import warehouse_view_fallback
    # Try system.lakeflow.pipelines (cross-workspace) only when viewers may hit the warehouse.
    try:
        if not warehouse_view_fallback():
            raise RuntimeError("skip warehouse pipeline-name lookup")
        results = execute_query("""
            SELECT pipeline_id, MAX(name) as pipeline_name
            FROM system.lakeflow.pipelines
            WHERE name IS NOT NULL
            GROUP BY pipeline_id
        """)
        if results:
            names = {r["pipeline_id"]: r["pipeline_name"] for r in results if r.get("pipeline_id") and r.get("pipeline_name")}
            logger.info(f"Pipeline names from system table: {len(names)} found")
            if names:
                _pipeline_names_cache = names
                _pipeline_names_cache_ts = now
                return names
    except Exception as e:
        logger.warning(f"system.lakeflow.pipelines not accessible: {type(e).__name__}: {e}")

    # Fall back to SDK (current workspace only)
    try:
        w = get_workspace_client()
        pipeline_names: dict[str, str] = {}
        for p in w.pipelines.list_pipelines():
            if p.pipeline_id and p.name:
                pipeline_names[p.pipeline_id] = p.name
        logger.info(f"Pipeline names from SDK: {len(pipeline_names)} found")
        _pipeline_names_cache = pipeline_names
        _pipeline_names_cache_ts = now
        return pipeline_names
    except Exception as e:
        logger.warning(f"Could not list pipelines via SDK: {type(e).__name__}: {e}")
        return {}


def _enrich_pipeline_results(results: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Enrich billing-only pipeline results with names from system table or SDK."""
    if not results:
        return results
    try:
        sdp_rows = [r for r in results if r.get("object_type") == "SDP Pipeline"]
        unresolved = [r for r in sdp_rows if r.get("object_name") == r.get("object_id")]
        if not unresolved:
            return results
        pipeline_names = _get_pipeline_names()
        if not pipeline_names:
            return results
        for row in results:
            if row.get("object_type") == "SDP Pipeline":
                pid = row.get("object_id")
                if pid and pid in pipeline_names:
                    row["object_name"] = pipeline_names[pid]
    except Exception as e:
        logger.warning(f"Pipeline enrichment failed: {type(e).__name__}: {e}")
    return results


@router.get("/dashboard-bundle")
async def get_dashboard_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get all dashboard data in a single request with parallel execution.
    
    This endpoint executes all dashboard queries in parallel to minimize latency.
    Expected speedup: 6-12x faster than making individual requests.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_INFRA_CLUSTERS, PG_INFRA_TIMESERIES, gold_read

    # Execute all queries in parallel
    queries = [
        ("summary", lambda: execute_query(BILLING_SUMMARY, params)),
        ("products", lambda: execute_query(BILLING_BY_PRODUCT, params)),
        ("workspaces", lambda: execute_query(BILLING_BY_WORKSPACE, params)),
        ("timeseries", lambda: execute_query(BILLING_TIMESERIES, params)),
        ("sql_breakdown", lambda: execute_query(SQL_TOOL_ATTRIBUTION, params)),
        ("etl_breakdown", lambda: execute_query(ETL_BREAKDOWN, params)),
        ("pipeline_objects", lambda: _enrich_pipeline_results(execute_query(PIPELINE_OBJECTS, params))),
        ("interactive", lambda: execute_query(INTERACTIVE_BREAKDOWN, params)),
        ("aws_clusters", lambda: gold_read("infra_clusters", PG_INFRA_CLUSTERS, params, "", AWS_COST_ESTIMATE)),
        ("aws_timeseries", lambda: gold_read("infra_timeseries", PG_INFRA_TIMESERIES, params, "", AWS_COST_TIMESERIES)),
    ]

    try:
        results = await asyncio.to_thread(execute_queries_parallel, queries, timeout=60.0)

        # Format responses to match existing endpoint structures
        response = {
            "summary": _format_summary(results.get("summary"), params),
            "products": _format_products(results.get("products"), params),
            "workspaces": _format_workspaces(results.get("workspaces"), params),
            "timeseries": _format_timeseries(results.get("timeseries"), params),
            "sql_breakdown": _format_sql_breakdown(results.get("sql_breakdown"), params),
            "etl_breakdown": _format_etl_breakdown(results.get("etl_breakdown"), params),
            "pipeline_objects": _format_pipeline_objects(results.get("pipeline_objects"), params),
            "interactive": _format_interactive(results.get("interactive"), params),
            "aws": {
                "clusters": _format_aws_clusters(results.get("aws_clusters"), None, params),
                "timeseries": _format_aws_timeseries(results.get("aws_timeseries"), params),
            },
        }

        return response
    except Exception as e:
        logger.error("dashboard-bundle failed: %s", e)
        return {
            "summary": _format_summary(None, params),
            "products": _format_products(None, params),
            "workspaces": _format_workspaces(None, params),
            "timeseries": _format_timeseries(None, params),
            "sql_breakdown": _format_sql_breakdown(None, params),
            "etl_breakdown": _format_etl_breakdown(None, params),
            "pipeline_objects": _format_pipeline_objects(None, params),
            "interactive": _format_interactive(None, params),
            "aws": {
                "clusters": _format_aws_clusters(None, None, params),
                "timeseries": _format_aws_timeseries(None, params),
            },
        }


def _inject_ws_filter(sql: str, clause: str) -> str:
    """Append a workspace filter clause after the usage_quantity guard in a SQL string."""
    if not clause:
        return sql
    for anchor in ("AND u.usage_quantity > 0", "AND usage_quantity > 0"):
        if anchor in sql:
            return sql.replace(anchor, f"{anchor}\n    {clause}", 1)
    return sql


def _inject_qh_ws_filter(sql: str, clause: str) -> str:
    """Append a workspace filter clause to a system.query.history query."""
    if not clause:
        return sql
    anchor = "AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)"
    if anchor in sql:
        return sql.replace(anchor, f"{anchor}\n    {clause}", 1)
    return sql


# Module-level cache for account workspace names (1h TTL).
# Populated by AccountClient.workspaces.list() when DATABRICKS_ACCOUNT_ID is set.
_account_ws_names: dict[str, str] = {}
_account_ws_names_ts: float = 0.0
_background_tasks: set = set()  # keeps fire-and-forget tasks alive


def _get_account_workspace_names() -> dict[str, str]:
    """Return workspace_id → workspace_name for ALL account workspaces. Cached 1h; {} on failure.

    Uses db.get_account_client() (account-console host + OAuth M2M). Previously this built a
    bare AccountClient(account_id=...) which has NO account host, so it hit the workspace host,
    failed, and returned {} — that's why the top-nav workspace filter showed raw IDs for the
    ~thousands of non-billing account workspaces (names resolve elsewhere via the
    billing.usage ⋈ workspaces_latest join, which only covers spending workspaces).
    """
    global _account_ws_names, _account_ws_names_ts
    if time.time() - _account_ws_names_ts < 3600:
        return _account_ws_names

    try:
        from backend.db import get_account_client
        a = get_account_client()  # correct account-console host; raises if no account_id
        names = {str(w.workspace_id): w.workspace_name for w in a.workspaces.list() if w.workspace_name}
        _account_ws_names = names
        _account_ws_names_ts = time.time()
        logger.info("AccountClient: fetched %d workspace names", len(names))
        return names
    except Exception as e:
        logger.warning("AccountClient workspace list failed: %s", e)
        _account_ws_names_ts = time.time()  # backoff: don't retry for another hour
        return {}


async def _refresh_account_ws_names_bg() -> None:
    """Fire-and-forget: refresh workspace name cache without blocking the request path."""
    await asyncio.to_thread(_get_account_workspace_names)


@router.get("/workspaces")
async def get_workspace_list(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """All workspaces with billing activity, scoped to COST_OBS_WORKSPACES when set."""
    return await workspace_list(start_date, end_date, apply_pool=True)


async def workspace_list(start_date: str | None, end_date: str | None, *, apply_pool: bool) -> dict[str, Any]:
    """Workspaces with billing activity. apply_pool=False lists every workspace, so the
    setup wizard can offer workspaces outside the current pool when rescoping."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    configured_ids = wf.get_configured_workspace_ids() if apply_pool else []
    ws_clause = wf.build_ws_filter_clause() if configured_ids else ""
    sql_with_names = f"""
        SELECT
            CAST(u.workspace_id AS STRING) as workspace_id,
            MAX(wsl.workspace_name) as workspace_name
        FROM system.billing.usage u
        LEFT JOIN system.access.workspaces_latest wsl
            ON CAST(u.workspace_id AS BIGINT) = CAST(wsl.workspace_id AS BIGINT)
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.workspace_id IS NOT NULL
          {ws_clause}
        GROUP BY u.workspace_id
        ORDER BY COALESCE(MAX(wsl.workspace_name), CAST(u.workspace_id AS STRING))
    """
    sql_ids_only = f"""
        SELECT
            CAST(workspace_id AS STRING) as workspace_id,
            CAST(NULL AS STRING) as workspace_name
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND workspace_id IS NOT NULL
          {ws_clause}
        GROUP BY workspace_id
        ORDER BY workspace_id
    """
    def _fetch_rows() -> list:
        from backend.lakebase_read import PG_WORKSPACE_LIST, gold_read, warehouse_view_fallback
        rows = gold_read("workspace_list", PG_WORKSPACE_LIST, params, ws_clause)
        if rows:
            if _account_ws_names:
                rows = [{**r, "workspace_name": _account_ws_names.get(str(r["workspace_id"])) or r["workspace_name"]} for r in rows]
            return rows
        if not warehouse_view_fallback():
            return []
        try:
            rows = execute_query(sql_with_names, params)
        except Exception as e:
            logger.warning("get_workspace_list: system.access.workspaces_latest unavailable (%s: %s), falling back to IDs only", type(e).__name__, e)
            rows = execute_query(sql_ids_only, params)
        # AccountClient names (authoritative for CURRENT workspaces).
        if _account_ws_names:
            rows = [{**r, "workspace_name": _account_ws_names.get(r["workspace_id"]) or r["workspace_name"]} for r in rows]
        # Fill any STILL-unresolved names from the app's own workspace MV. The MV retains
        # names for workspaces that were later deleted/renamed — system.access.workspaces_latest
        # drops those and AccountClient only lists current workspaces, so the long tail
        # otherwise renders as raw IDs in the top-nav filter (names resolve elsewhere in the
        # app precisely because those surfaces read this MV). (The old fallback queried
        # system.access.workspaces, which isn't present in every workspace, and only ran when
        # EVERY row was null — so partial gaps like this never got filled.)
        if any(r["workspace_name"] is None for r in rows):
            try:
                from backend.db import get_catalog_schema
                _cat, _sch = get_catalog_schema()
                mv = execute_query(
                    f"SELECT CAST(workspace_id AS STRING) AS workspace_id, MAX(workspace_name) AS workspace_name "
                    f"FROM `{_cat}`.`{_sch}`.`daily_workspace_breakdown` "
                    f"WHERE workspace_name IS NOT NULL GROUP BY workspace_id",
                    no_cache=True,
                )
                mv_map = {r["workspace_id"]: r["workspace_name"] for r in mv}
                if mv_map:
                    rows = [{**r, "workspace_name": r["workspace_name"] or mv_map.get(r["workspace_id"])} for r in rows]
            except Exception as e:
                logger.debug("get_workspace_list: MV workspace-name fill skipped: %s", e)
        return rows

    # Kick off background refresh if stale — never blocks the request path
    if time.time() - _account_ws_names_ts >= 3600:
        _t = asyncio.create_task(_refresh_account_ws_names_bg())
        _background_tasks.add(_t)
        _t.add_done_callback(_background_tasks.discard)

    # Admin pref: when workspace display names are off, return IDs (name=null) even
    # where a name resolved. `historical` still reflects true name availability so the
    # toggle never mislabels a live workspace.
    try:
        from backend.routers.settings import workspace_names_enabled
        show_names = workspace_names_enabled()
    except Exception:
        show_names = True

    try:
        rows = await asyncio.wait_for(asyncio.to_thread(_fetch_rows), timeout=30.0)
        return {
            "workspaces": [
                {
                    "id": r["workspace_id"],
                    "name": (r["workspace_name"] if show_names else None),
                    # No name resolvable from any source => workspace no longer exists.
                    "historical": not (r.get("workspace_name") and str(r["workspace_name"]).strip()),
                }
                for r in rows
                if r.get("workspace_id") is not None
                and str(r["workspace_id"]).strip().lower() not in ("", "none", "null")
            ],
            "is_scoped": bool(configured_ids),
            "env_var": "COST_OBS_WORKSPACES",
            "env_var_value": ",".join(configured_ids),
        }
    except asyncio.TimeoutError:
        logger.warning("get_workspace_list timed out after 30s")
        return {"workspaces": [], "is_scoped": bool(configured_ids), "error": "timeout"}
    except Exception as e:
        logger.warning("get_workspace_list failed: %s", e)
        return {"workspaces": [], "is_scoped": bool(configured_ids), "error": str(e)}


@router.get("/dashboard-bundle-fast")
async def get_dashboard_bundle_fast(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """Get essential dashboard data FAST.

    This endpoint is optimized for fast initial page load by:
    1. Using materialized views when available (sub-second queries)
    2. Falling back to optimized queries that skip system.query.history
    3. Running queries in parallel

    Expected load time: <1 second with MVs, 2-5 seconds without.
    """
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None

    blocked = source_scope_unavailable(
        {"daily_usage_summary", "daily_product_breakdown", "daily_workspace_breakdown"},
        "The selected shared source does not publish usage summary gold.",
    )
    if blocked:
        return {
            "summary": {**_format_summary(None, params), **blocked},
            "products": {**_format_products_fast(None, params), **blocked},
            "workspaces": {**_format_workspaces(None, params), **blocked},
            "timeseries": {**_format_timeseries_fast(None, params), **blocked},
            "etl_breakdown": {**_format_etl_breakdown(None, params), **blocked},
            "is_fast_mode": True,
            "using_materialized_views": False,
            **blocked,
        }

    # Delta cross-worker cache — check before running any warehouse queries
    _dkey = bundle_cache_key("billing:dashboard-bundle-fast", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached

    # Build workspace filter — dropdown selection overrides env/file config.
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)
    mv_ws = _mv_ws_clause(id_list)

    use_mv = await asyncio.to_thread(_check_mv_available)

    if use_mv:
        # Use materialized views — much faster than live system.billing.usage scans.
        # MVs now include workspace_id so they support workspace filtering directly.
        logger.info("Using materialized views for dashboard bundle")

        def _mv_summary():
            r = _exec_mv(MV_BILLING_SUMMARY, params, mv_ws)
            if r:
                return r
            return _live_if_allowed(_inject_ws_filter(BILLING_SUMMARY, ws_clause), params)

        def _mv_timeseries():
            r = _exec_mv(MV_BILLING_TIMESERIES, params, mv_ws)
            return r if r else _live_if_allowed(_inject_ws_filter(BILLING_TIMESERIES_FAST, ws_clause), params)

        def _mv_products():
            r = _exec_mv(MV_BILLING_BY_PRODUCT, params, mv_ws)
            return r if r else _live_if_allowed(_inject_ws_filter(BILLING_BY_PRODUCT_FAST, ws_clause), params)

        def _mv_workspaces():
            r = _exec_mv(MV_BILLING_BY_WORKSPACE, params, mv_ws)
            return r if r else _live_if_allowed(_inject_ws_filter(BILLING_BY_WORKSPACE, ws_clause), params)

        # Also fetch most-recent-day workspace count; MV summary gives period-total DISTINCT
        # which is always >= any single day and mismatches the daily trend chart.
        _mv_ws_filter = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
        from backend.lakebase_read import PG_WORKSPACE_COUNT_LATEST, gold_read
        queries = [
            ("summary", _mv_summary),
            ("products", _mv_products),
            ("workspaces", _mv_workspaces),
            ("timeseries", _mv_timeseries),
            ("etl_breakdown", lambda: _exec_mv(MV_ETL_BREAKDOWN, params, mv_ws)),
            ("workspace_count", lambda: gold_read(
                "workspace_count_latest", PG_WORKSPACE_COUNT_LATEST, params, _mv_ws_filter,
            )),
        ]
    else:
        # Fall back to fast queries without MVs; inject workspace filter when active.
        _ws = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
        WORKSPACE_COUNT_QUERY = f"""
        SELECT daily_ws as workspace_count FROM (
          SELECT usage_date, COUNT(DISTINCT workspace_id) as daily_ws
          FROM system.billing.usage
          WHERE usage_date BETWEEN :start_date AND :end_date AND usage_quantity > 0
          {_ws}
          GROUP BY usage_date
          ORDER BY usage_date DESC
          LIMIT 1
        )
        """
        _s = _inject_ws_filter(BILLING_SUMMARY, ws_clause)
        _p = _inject_ws_filter(BILLING_BY_PRODUCT_FAST, ws_clause)
        _w = _inject_ws_filter(BILLING_BY_WORKSPACE, ws_clause)
        _t = _inject_ws_filter(BILLING_TIMESERIES_FAST, ws_clause)
        _e = _inject_ws_filter(ETL_BREAKDOWN, ws_clause)
        queries = [
            ("summary", lambda: execute_query(_s, params)),
            ("products", lambda: execute_query(_p, params)),
            ("workspaces", lambda: execute_query(_w, params)),
            ("timeseries", lambda: execute_query(_t, params)),
            ("etl_breakdown", lambda: execute_query(_e, params)),
            ("workspace_count", lambda: execute_query(WORKSPACE_COUNT_QUERY, params)),
        ]

    try:
        results = await asyncio.to_thread(execute_queries_parallel, queries, timeout=90.0)

        # Format responses
        response = {
            "summary": _format_summary(results.get("summary"), params),
            "products": _format_products_fast(results.get("products"), params),
            "workspaces": _format_workspaces(results.get("workspaces"), params),
            "timeseries": _format_timeseries_fast(results.get("timeseries"), params),
            "etl_breakdown": _format_etl_breakdown(results.get("etl_breakdown"), params),
            "is_fast_mode": True,
            "using_materialized_views": use_mv,
        }

        # Override workspace_count with most-recent-day count (both MV and non-MV paths).
        # MV summary computes COUNT(DISTINCT) over the full period which is always larger
        # than any daily count and mismatches the "Daily Active Workspaces" trend chart.
        wc_results = results.get("workspace_count")
        if wc_results and len(wc_results) > 0:
            accurate_count = int(wc_results[0].get("workspace_count") or 0)
            if accurate_count > 0:
                response["summary"]["workspace_count"] = accurate_count

        delta_cache_put(_dkey, "billing:dashboard-bundle-fast", response, ttl_seconds=cache_ttls.BUNDLE_FILTERED if id_list else cache_ttls.BUNDLE)
        return response
    except SourceScopeUnsupportedError as e:
        blocked = {
            "available": False,
            "reason": "shared_scope_unsupported",
            "reason_detail": str(e),
            "error_code": e.code,
        }
        return {
            "summary": {**_format_summary(None, params), **blocked},
            "products": {**_format_products_fast(None, params), **blocked},
            "workspaces": {**_format_workspaces(None, params), **blocked},
            "timeseries": {**_format_timeseries_fast(None, params), **blocked},
            "etl_breakdown": {**_format_etl_breakdown(None, params), **blocked},
            "is_fast_mode": True,
            "using_materialized_views": use_mv,
            **blocked,
        }
    except Exception as e:
        logger.error("dashboard-bundle-fast failed: %s", e)
        return {
            "summary": _format_summary(None, params),
            "products": _format_products_fast(None, params),
            "workspaces": _format_workspaces(None, params),
            "timeseries": _format_timeseries_fast(None, params),
            "etl_breakdown": _format_etl_breakdown(None, params),
            "is_fast_mode": True,
            "using_materialized_views": use_mv,
            "error": str(e),
        }


def _format_products_fast(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format fast products query results."""
    if not results:
        return {"products": [], "total_spend": 0, "start_date": params["start_date"], "end_date": params["end_date"]}

    total_spend = sum(float(row.get("total_spend") or 0) for row in results)
    products = []
    for row in results:
        spend = float(row.get("total_spend") or 0)
        products.append({
            "category": row.get("product_category"),
            "total_dbus": float(row.get("total_dbus") or 0),
            "total_spend": spend,
            "workspace_count": int(row.get("workspace_count") or 0),
            "percentage": (spend / total_spend * 100) if total_spend > 0 else 0,
        })

    return {
        "products": products,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_timeseries_fast(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format fast timeseries query results."""
    if not results:
        return {"timeseries": [], "categories": [], "start_date": params["start_date"], "end_date": params["end_date"]}

    categories = set()
    timeseries_map: dict[str, dict[str, Any]] = {}

    for row in results:
        date = str(row.get("usage_date"))
        category = row.get("product_category") or "Other"
        spend = float(row.get("total_spend") or 0)

        categories.add(category)

        if date not in timeseries_map:
            timeseries_map[date] = {"date": date}

        timeseries_map[date][category] = spend

    timeseries = sorted(timeseries_map.values(), key=lambda x: x["date"])

    return {
        "timeseries": timeseries,
        "categories": sorted(list(categories)),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_summary(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format summary query results."""
    if not results:
        from backend.lakebase_read import warehouse_view_fallback
        if not warehouse_view_fallback():
            return {
                "total_dbus": 0,
                "total_spend": 0,
                "workspace_count": 0,
                "days_in_range": 0,
                "avg_daily_spend": 0,
                "start_date": params["start_date"],
                "end_date": params["end_date"],
                "first_date": None,
                "last_date": None,
            }
        return {"error": "Summary data not available"}

    row = results[0] if results else {}
    total_dbus = float(row.get("total_dbus") or 0)
    total_spend = float(row.get("total_spend") or 0)
    workspace_count = int(row.get("workspace_count") or 0)
    days_in_range = int(row.get("days_in_range") or 1)
    avg_daily_spend = total_spend / days_in_range if days_in_range > 0 else 0

    return {
        "total_dbus": total_dbus,
        "total_spend": total_spend,
        "workspace_count": workspace_count,
        "days_in_range": days_in_range,
        "avg_daily_spend": avg_daily_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "first_date": str(row.get("first_date")) if row.get("first_date") else None,
        "last_date": str(row.get("last_date")) if row.get("last_date") else None,
    }


def _format_products(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format products query results."""
    if not results:
        return {"products": [], "total_spend": 0, "start_date": params["start_date"], "end_date": params["end_date"]}

    total_spend = sum(float(row.get("total_spend") or 0) for row in results)
    products = []
    for row in results:
        spend = float(row.get("total_spend") or 0)
        products.append({
            "category": row.get("category"),
            "total_dbus": float(row.get("total_dbus") or 0),
            "total_spend": spend,
            "workspace_count": int(row.get("workspace_count") or 0),
            "percentage": (spend / total_spend * 100) if total_spend > 0 else 0,
        })

    return {
        "products": products,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_workspaces(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format workspaces query results."""
    if not results:
        return {"workspaces": [], "total_spend": 0, "start_date": params["start_date"], "end_date": params["end_date"]}

    total_spend = sum(float(row.get("total_spend") or 0) for row in results)
    # Admin pref: when workspace display names are turned off, show IDs even where a
    # name resolved. `historical` still reflects true name availability (computed from
    # the resolved name), so the toggle never mislabels a live workspace as historical.
    try:
        from backend.routers.settings import workspace_names_enabled
        show_names = workspace_names_enabled()
    except Exception:
        show_names = True
    workspaces = []
    for row in results:
        wid = row.get("workspace_id")
        # Skip rows with no workspace id — str(None) would otherwise render a bogus
        # "None" entry in every workspace dropdown across the app.
        if wid is None or str(wid).strip().lower() in ("", "none", "null"):
            continue
        wid = str(wid)
        raw_name = row.get("workspace_name")
        resolved = raw_name if (raw_name and str(raw_name).strip()) else None
        spend = float(row.get("total_spend") or 0)
        workspaces.append({
            "workspace_id": wid,
            "workspace_name": resolved if show_names else None,
            # No display name in billing history => the workspace no longer exists
            # in the account (deleted); surface it as "historical" in dropdowns.
            "historical": resolved is None,
            "total_dbus": float(row.get("total_dbus") or 0),
            "total_spend": spend,
            "top_products": _ensure_list(row.get("top_products")),
            "top_users": _ensure_list(row.get("top_users")),
            "percentage": (spend / total_spend * 100) if total_spend > 0 else 0,
        })

    return {
        "workspaces": workspaces,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_timeseries(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format timeseries query results."""
    if not results:
        return {"timeseries": [], "categories": [], "start_date": params["start_date"], "end_date": params["end_date"]}

    categories = set()
    timeseries_map: dict[str, dict[str, Any]] = {}

    for row in results:
        date = str(row.get("usage_date"))
        category = row.get("category") or "Other"
        spend = float(row.get("total_spend") or 0)

        categories.add(category)

        if date not in timeseries_map:
            timeseries_map[date] = {"date": date}

        timeseries_map[date][category] = spend

    timeseries = sorted(timeseries_map.values(), key=lambda x: x["date"])

    return {
        "timeseries": timeseries,
        "categories": sorted(list(categories)),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_sql_breakdown(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format SQL breakdown query results."""
    if not results:
        return {"products": [], "total_spend": 0, "start_date": params["start_date"], "end_date": params["end_date"]}

    total_spend = sum(float(row.get("total_spend") or 0) for row in results)
    products = []
    for row in results:
        spend = float(row.get("total_spend") or 0)
        products.append({
            "product": row.get("product"),
            "total_dbus": float(row.get("total_dbus") or 0),
            "total_spend": spend,
            "percentage": (spend / total_spend * 100) if total_spend > 0 else 0,
        })

    return {
        "products": products,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_etl_breakdown(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format ETL breakdown query results."""
    return _format_sql_breakdown(results, params)  # Same format


def _format_pipeline_objects(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format pipeline objects query results."""
    if not results:
        return {"objects": [], "total_spend": 0, "start_date": params["start_date"], "end_date": params["end_date"]}

    total_spend = sum(float(row.get("total_spend") or 0) for row in results)
    objects = []
    for row in results:
        spend = float(row.get("total_spend") or 0)
        objects.append({
            "object_type": row.get("object_type"),
            "object_id": row.get("object_id"),
            "object_name": row.get("object_name"),
            "workspace_id": str(row.get("workspace_id") or ""),
            "object_state": row.get("object_state"),
            "owner": row.get("owner"),
            "total_dbus": float(row.get("total_dbus") or 0),
            "total_spend": spend,
            "total_runs": int(row.get("total_runs") or 0),
            "percentage": (spend / total_spend * 100) if total_spend > 0 else 0,
        })

    return {
        "objects": objects,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _format_interactive(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format interactive breakdown query results."""
    if not results:
        return {"items": [], "total_spend": 0, "start_date": params["start_date"], "end_date": params["end_date"]}

    total_spend = sum(float(row.get("total_spend") or 0) for row in results)
    items = []
    for row in results:
        spend = float(row.get("total_spend") or 0)
        items.append({
            "cluster_id": row.get("cluster_id"),
            "notebook_path": row.get("notebook_path"),
            "user": row.get("run_as_user"),
            "total_dbus": float(row.get("total_dbus") or 0),
            "total_spend": spend,
            "days_active": int(row.get("days_active") or 0),
            "notebook_count": int(row.get("notebook_count") or 0),
            "percentage": (spend / total_spend * 100) if total_spend > 0 else 0,
        })

    return {
        "items": items,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _cluster_vm_cost(row: dict[str, Any], cloud: str = "AWS") -> float:
    """Price a gold cluster row. Gold copies store DBU hours, not a precomputed dollar estimate."""
    if row.get("estimated_aws_cost") is not None:
        return float(row.get("estimated_aws_cost") or 0)
    dbu_hours = float(row.get("total_dbu_hours") or 0)
    driver_cost = get_instance_pricing(row.get("driver_instance_type"), cloud)
    worker_cost = get_instance_pricing(row.get("worker_instance_type"), cloud)
    return dbu_hours * (driver_cost + worker_cost * 2) / 2


def _families_from_cluster_rows(
    cluster_results: list[dict[str, Any]] | None,
    cloud: str = "AWS",
) -> list[dict[str, Any]]:
    """Roll instance families from gold cluster rows — no warehouse family query."""
    if not cluster_results:
        return []
    family_agg: dict[str, dict[str, Any]] = {}
    for row in cluster_results:
        dbu_hours = float(row.get("total_dbu_hours") or 0)
        days = int(row.get("days_active") or 0)
        for itype in (row.get("driver_instance_type"), row.get("worker_instance_type")):
            if not itype:
                continue
            family = get_instance_family(itype, cloud)
            existing = family_agg.get(family)
            if existing:
                existing["total_dbu_hours"] += dbu_hours
                existing["days_active"] = max(existing["days_active"], days)
            else:
                family_agg[family] = {
                    "instance_family": family,
                    "total_dbu_hours": dbu_hours,
                    "days_active": days,
                }
    return sorted(family_agg.values(), key=lambda f: f["total_dbu_hours"], reverse=True)


def _format_aws_clusters(
    cluster_results: list[dict[str, Any]] | None,
    instance_results: list[dict[str, Any]] | None,
    params: dict[str, str]
) -> dict[str, Any]:
    """Format AWS costs query results."""
    if not cluster_results:
        return {"clusters": [], "instance_families": [], "total_estimated_cost": 0, "total_dbu_hours": 0,
                "start_date": params["start_date"], "end_date": params["end_date"]}

    total_cost = sum(_cluster_vm_cost(row, "AWS") for row in cluster_results)
    total_dbu_hours = sum(float(row.get("total_dbu_hours") or 0) for row in cluster_results)

    clusters = []
    for row in cluster_results:
        cost = _cluster_vm_cost(row, "AWS")
        clusters.append({
            "cluster_id": row.get("cluster_id"),
            "cluster_name": row.get("cluster_name"),
            "driver_instance_type": row.get("driver_instance_type"),
            "worker_instance_type": row.get("worker_instance_type"),
            "cluster_source": row.get("cluster_source"),
            "total_dbu_hours": float(row.get("total_dbu_hours") or 0),
            "estimated_aws_cost": cost,
            "days_active": int(row.get("days_active") or 0),
            "percentage": (cost / total_cost * 100) if total_cost > 0 else 0,
        })

    instance_families = []
    if instance_results:
        for row in instance_results:
            instance_families.append({
                "instance_family": row.get("instance_family"),
                "total_dbu_hours": float(row.get("total_dbu_hours") or 0),
                "days_active": int(row.get("days_active") or 0),
            })
    else:
        instance_families = _families_from_cluster_rows(cluster_results, "AWS")

    return {
        "clusters": clusters,
        "instance_families": instance_families,
        "total_estimated_cost": total_cost,
        "total_dbu_hours": total_dbu_hours,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "disclaimer": "Estimated AWS costs based on standard EC2 pricing. Actual costs may vary.",
    }


def _format_aws_timeseries(results: list[dict[str, Any]] | None, params: dict[str, str]) -> dict[str, Any]:
    """Format AWS timeseries query results with instance family breakdown."""
    if not results:
        return {"timeseries": [], "instance_families": [], "start_date": params["start_date"], "end_date": params["end_date"]}

    # Aggregate by date, with per-family breakdown
    from collections import defaultdict
    date_totals: dict[str, float] = defaultdict(float)
    date_family: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    all_families: set[str] = set()

    for row in results:
        d = str(row.get("usage_date"))
        family = row.get("instance_family") or "Infrastructure"
        if row.get("estimated_aws_cost") is not None:
            cost = float(row.get("estimated_aws_cost") or 0)
        else:
            cost = float(row.get("total_dbu_hours") or 0) * 0.50
        date_totals[d] += cost
        date_family[d][family] += cost
        all_families.add(family)

    # Build timeseries with total + per-family columns
    timeseries = []
    for d in sorted(date_totals.keys()):
        entry: dict[str, Any] = {"date": d, "AWS Cost": round(date_totals[d], 2)}
        for family in all_families:
            entry[family] = round(date_family[d].get(family, 0), 2)
        timeseries.append(entry)

    # Sort families by total spend descending
    family_totals = {f: sum(date_family[d].get(f, 0) for d in date_totals) for f in all_families}
    sorted_families = sorted(all_families, key=lambda f: family_totals[f], reverse=True)

    return {
        "timeseries": timeseries,
        "instance_families": sorted_families,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/sku-breakdown")
async def get_sku_breakdown(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Get breakdown by SKU/product type.

    Returns spend and usage metrics grouped by SKU name.
    """
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    _dkey = bundle_cache_key("billing:sku-breakdown", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)
    blocked = source_scope_unavailable(
        {"daily_sku_breakdown"},
        "The selected shared source does not publish SKU detail.",
    )
    if blocked:
        return {**blocked, "skus": [], "total_spend": 0,
                "start_date": params["start_date"], "end_date": params["end_date"]}
    try:
        results = await asyncio.to_thread(_exec_mv, MV_SKU_BREAKDOWN, params, _mv_ws_clause(id_list))
        if not results:
            results = await asyncio.to_thread(
                _live_if_allowed, _inject_ws_filter(SKU_BREAKDOWN, ws_clause), params
            )

        skus = []
        total_spend = 0.0

        for row in results:
            sku = {
                "product": row.get("product"),
                "workspaces_using": int(row.get("workspaces_using") or 0),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": float(row.get("total_spend") or 0),
                "percentage": float(row.get("percentage") or 0),
            }
            skus.append(sku)
            total_spend += sku["total_spend"]

        _resp = {
            "skus": skus,
            "total_spend": total_spend,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
        delta_cache_put(_dkey, "billing:sku-breakdown", _resp, ttl_seconds=cache_ttls.BUNDLE)
        return _resp
    except Exception as e:
        return _optional_breakdown_unavailable("sku", "skus", params, e)


_group_membership_cache: dict[str, list[str]] | None = None
_group_membership_cache_ts: float = 0
_GROUP_CACHE_TTL = cache_ttls.GROUP_MEMBERSHIP


def _get_cached_group_membership(w) -> dict[str, list[str]]:
    """Get user→groups mapping from SDK, cached for 1 hour."""
    global _group_membership_cache, _group_membership_cache_ts
    import time as _time
    now = _time.monotonic()
    if _group_membership_cache is not None and (now - _group_membership_cache_ts) < _GROUP_CACHE_TTL:
        return _group_membership_cache

    user_groups: dict[str, list[str]] = {}
    for g in w.groups.list(attributes="displayName,members", filter='displayName co ""'):
        if not g.display_name or not g.members:
            continue
        for m in g.members:
            if m.display and "@" in m.display:
                user_groups.setdefault(m.display, []).append(g.display_name)

    _group_membership_cache = user_groups
    _group_membership_cache_ts = now
    return user_groups


@router.get("/spend-by-user-group")
async def get_spend_by_user_group(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get spend breakdown by user group (falls back to top users)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    # Try user groups first via Databricks SDK (cached for 1 hour)
    groups = []
    total_spend = 0.0
    source = "users"

    try:
        w = get_workspace_client()
        # Build group membership map: user_name -> list of group names
        user_groups = _get_cached_group_membership(w)

        if user_groups:
            source = "groups"
            # Get per-user spend
            user_query = """
            SELECT
              COALESCE(u.identity_metadata.run_as, 'Unknown') as user_identity,
              SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
              SUM(u.usage_quantity) as total_dbus
            FROM system.billing.usage u
            LEFT JOIN system.billing.list_prices p
              ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
            WHERE u.usage_date BETWEEN :start_date AND :end_date
              AND u.usage_quantity > 0
              AND u.identity_metadata.run_as IS NOT NULL
              AND u.identity_metadata.run_as != 'Unknown'
            GROUP BY 1
            HAVING SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) > 0
            """
            user_results = await asyncio.to_thread(execute_query, user_query, params)

            # Aggregate spend by group
            group_spend: dict[str, dict] = {}
            for row in (user_results or []):
                user = row.get("user_identity") or ""
                spend = float(row.get("total_spend") or 0)
                dbus = float(row.get("total_dbus") or 0)
                matched_groups = user_groups.get(user, ["No Group"])
                for gname in matched_groups:
                    if gname not in group_spend:
                        group_spend[gname] = {"total_spend": 0, "total_dbus": 0, "user_count": set()}
                    group_spend[gname]["total_spend"] += spend
                    group_spend[gname]["total_dbus"] += dbus
                    group_spend[gname]["user_count"].add(user)

            for gname, data in sorted(group_spend.items(), key=lambda x: x[1]["total_spend"], reverse=True)[:15]:
                groups.append({
                    "group_name": gname,
                    "total_spend": data["total_spend"],
                    "total_dbus": data["total_dbus"],
                    "user_count": len(data["user_count"]),
                })
                total_spend += data["total_spend"]
    except Exception as e:
        logger.warning(f"Group lookup failed, falling back to users: {e}")

    # Fallback: top users by spend
    if not groups:
        source = "users"
        query = """
        SELECT
          u.identity_metadata.run_as as group_name,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
          SUM(u.usage_quantity) as total_dbus,
          1 as user_count
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.identity_metadata.run_as IS NOT NULL
        GROUP BY 1
        HAVING SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) > 0
        ORDER BY total_spend DESC
        LIMIT 15
        """
        try:
            results = await asyncio.to_thread(execute_query, query, params)
        except Exception as e:
            logger.warning(f"User spend query failed: {e}")
            return {"groups": [], "total_spend": 0, "error": str(e)}

        for row in (results or []):
            spend = float(row.get("total_spend") or 0)
            total_spend += spend
            name = row.get("group_name") or ""
            if not name or name == "Unknown":
                continue
            groups.append({
                "group_name": name.split("@")[0] if "@" in name else name,
                "total_spend": spend,
                "total_dbus": float(row.get("total_dbus") or 0),
                "user_count": int(row.get("user_count") or 0),
            })

    # Calculate percentages
    for g in groups:
        g["percentage"] = round((g["total_spend"] / total_spend * 100) if total_spend > 0 else 0, 1)

    return {
        "groups": groups,
        "total_spend": total_spend,
        "source": source,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/spend-anomalies")
async def get_spend_anomalies(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get days with largest day-over-day spend changes.

    Returns top 20 days with biggest absolute percentage changes in spend.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(execute_query, SPEND_ANOMALIES, params)

    # Effective spike threshold folds in the admin's anomaly-sensitivity setting
    # (low/medium/high → base threshold × 1.5/1.0/0.5). Additive: each row is flagged
    # is_spike so consumers can highlight true anomalies without a contract change.
    try:
        from backend.routers.settings import anomaly_spike_threshold
        spike_threshold = anomaly_spike_threshold()
    except Exception:
        spike_threshold = 20.0

    anomalies = []

    for row in results:
        change_percent = float(row.get("change_percent") or 0)
        anomaly = {
            "usage_date": str(row.get("usage_date")),
            "daily_spend": float(row.get("daily_spend") or 0),
            "prev_day_spend": float(row.get("prev_day_spend") or 0),
            "change_amount": float(row.get("change_amount") or 0),
            "change_percent": change_percent,
            "is_spike": abs(change_percent) >= spike_threshold,
        }
        anomalies.append(anomaly)

    return {
        "anomalies": anomalies,
        "spike_threshold_percent": spike_threshold,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/platform-kpis")
async def get_platform_kpis(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    fast: bool = Query(default=True, description="Use fast mode (skips query.history)"),
) -> dict[str, Any]:
    """Get platform KPIs showing value and accomplishments.

    Returns key metrics like total queries, jobs, data processed,
    unique users, and other metrics demonstrating platform value.

    Set fast=true (default) to skip slow query.history joins.
    When materialized views are available, query stats are included even in fast mode.
    """
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    # Initialize response with defaults
    response = {
        "total_queries": 0,
        "unique_query_users": 0,
        "total_rows_read": 0,
        "total_bytes_read": 0,
        "total_compute_seconds": 0,
        "total_jobs": 0,
        "total_job_runs": 0,
        "successful_runs": 0,
        "total_job_run_hours": 0,
        "unique_job_owners": 0,
        "active_workspaces": 0,
        "active_notebooks": 0,
        "models_served": 0,
        "total_serving_dbus": 0,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }

    # Check if we can use materialized views for query stats
    use_mv = await asyncio.to_thread(_check_mv_available)

    if use_mv:
        # Try to get query stats from materialized view (fast!)
        try:
            mv_results = await asyncio.to_thread(_exec_mv, MV_PLATFORM_KPIS, params)
            if mv_results and len(mv_results) > 0:
                mv_row = mv_results[0]
                response["total_queries"] = int(mv_row.get("total_queries") or 0)
                response["unique_query_users"] = int(mv_row.get("unique_query_users") or 0)
                response["total_rows_read"] = int(mv_row.get("total_rows_read") or 0)
                response["total_bytes_read"] = int(mv_row.get("total_bytes_read") or 0)
                response["total_compute_seconds"] = float(mv_row.get("total_compute_seconds") or 0)
                logger.info("Platform KPIs: Using materialized views for query stats")
        except Exception as e:
            logger.debug(f"MV query stats failed: {e}")

    from backend.lakebase_read import PG_KPI_BILLING, gold_read, warehouse_view_fallback
    results = await asyncio.to_thread(
        gold_read, "kpi_billing", PG_KPI_BILLING, params, "",
        PLATFORM_KPIS_FAST if (fast or use_mv) and warehouse_view_fallback() else (
            PLATFORM_KPIS if warehouse_view_fallback() else None
        ),
    )

    if results and len(results) > 0:
        row = results[0]

        # If we didn't get query stats from MV, try to get them from the query results
        if not use_mv and not fast:
            response["total_queries"] = int(row.get("total_queries") or 0)
            response["unique_query_users"] = int(row.get("unique_query_users") or 0)
            response["total_rows_read"] = int(row.get("total_rows_read") or 0)
            response["total_bytes_read"] = int(row.get("total_bytes_read") or 0)
            response["total_compute_seconds"] = float(row.get("total_compute_seconds") or 0)

        # Always get billing-based metrics (including unique_job_owners and lakeflow stats)
        response["total_jobs"] = int(row.get("total_jobs") or 0)
        response["total_job_runs"] = int(row.get("total_job_runs") or 0)
        response["successful_runs"] = int(row.get("successful_runs") or 0)
        response["unique_job_owners"] = int(row.get("unique_job_owners") or 0)
        response["active_workspaces"] = int(row.get("active_workspaces") or 0)
        response["active_notebooks"] = int(row.get("active_notebooks") or 0)
        response["models_served"] = int(row.get("models_served") or 0)
        response["total_serving_dbus"] = float(row.get("total_serving_dbus") or 0)

    return response


@router.get("/kpis-bundle")
async def get_kpis_bundle(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Bundled KPIs endpoint: runs platform KPIs and spend anomalies in parallel."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None

    # Delta cross-worker cache
    _dkey = bundle_cache_key("billing:kpis-bundle", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached

    ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    # Determine which KPI query to run
    use_mv = await asyncio.to_thread(_check_mv_available)

    # Supplemental query for accurate user count (MV uses MAX of daily counts which under-counts)
    catalog, schema = get_catalog_schema()
    USER_COUNT_QUERY = f"""
    SELECT COUNT(DISTINCT executed_by) as unique_query_users
    FROM `{catalog}`.`{schema}`.`dbsql_cost_per_query`
    WHERE start_time >= :start_date AND start_time < DATE_ADD(CAST(:end_date AS DATE), 1)
    """

    # Direct Delta query for query stats — used as fallback if Lakebase daily_query_stats is empty
    mv_ws = _mv_ws_clause(id_list)
    delta_query_stats_sql = MV_PLATFORM_KPIS.format(catalog=catalog, schema=schema, ws_filter=mv_ws)

    anomalies_sql = SPEND_ANOMALIES

    from backend.lakebase_read import (
        PG_AVG_DAILY_MODELS, PG_KPI_BILLING, PG_KPI_LAKEFLOW, PG_QUERY_USER_COUNT,
        PG_SPEND_ANOMALIES, PG_STICKINESS, PG_TOTAL_WORKSPACES, gold_read,
        warehouse_view_fallback,
    )
    billing_ws_clause = wf.build_ws_filter_clause(id_list=id_list, col="workspace_id")
    live_ok = warehouse_view_fallback()
    billing_kpis_sql = _inject_ws_filter(BILLING_KPIS_FAST, billing_ws_clause) if live_ok else None

    parallel_queries: list[tuple[str, Any]] = [
        ("billing_kpis", lambda: gold_read("kpi_billing", PG_KPI_BILLING, params, billing_ws_clause, billing_kpis_sql)),
        ("lakeflow_kpis", lambda: gold_read(
            "kpi_lakeflow", PG_KPI_LAKEFLOW, params, billing_ws_clause,
            LAKEFLOW_JOB_STATS if live_ok else None,
        )),
        ("anomalies", lambda: gold_read(
            "spend_anomalies", PG_SPEND_ANOMALIES, params, ws_clause,
            _inject_ws_filter(anomalies_sql, ws_clause) if live_ok else None,
        )),
    ]
    if use_mv:
        parallel_queries.append(("avg_daily_ws", lambda: _exec_mv(AVG_DAILY_WORKSPACES, params, mv_ws)))
        parallel_queries.append(("avg_daily_query_users", lambda: _exec_mv(AVG_DAILY_QUERY_USERS_MV, params, mv_ws)))

    # delta_query_stats is a Delta-direct fallback; skip it when MV is available to avoid redundant scan
    if use_mv:
        parallel_queries.append(("mv_kpis", lambda: _exec_mv(MV_PLATFORM_KPIS, params, mv_ws)))
    else:
        parallel_queries.append(("delta_query_stats", lambda: execute_query(delta_query_stats_sql, params)))

    parallel_queries.append(("user_count", lambda: gold_read(
        "query_user_count", PG_QUERY_USER_COUNT, params, billing_ws_clause,
        USER_COUNT_QUERY if live_ok else None,
    )))
    parallel_queries.append(("total_workspaces", lambda: gold_read(
        "total_workspaces_alltime", PG_TOTAL_WORKSPACES, {}, "",
        TOTAL_WORKSPACES_ALLTIME if live_ok else None,
    )))
    avg_daily_models_sql = _inject_ws_filter(AVG_DAILY_MODELS, billing_ws_clause) if live_ok else None
    parallel_queries.append(("avg_daily_models", lambda: gold_read(
        "avg_daily_models", PG_AVG_DAILY_MODELS, params, billing_ws_clause, avg_daily_models_sql,
    )))
    _qh_ws = ws_clause if ws_clause else ""
    stickiness_sql = STICKINESS_PCT_QH.format(ws_filter=_qh_ws) if live_ok else None
    parallel_queries.append(("stickiness_pct", lambda: gold_read(
        "stickiness_pct", PG_STICKINESS, params, billing_ws_clause, stickiness_sql,
    )))

    try:
        query_results = await asyncio.to_thread(execute_queries_parallel, parallel_queries, timeout=90.0)
    except Exception as e:
        logger.error("kpis-bundle query execution failed: %s", e)
        return {"kpis": {
            "total_queries": 0, "unique_query_users": 0,
            "total_rows_read": 0, "total_bytes_read": 0, "total_compute_seconds": 0,
            "total_jobs": 0, "total_job_runs": 0, "successful_runs": 0, "total_job_run_hours": 0,
            "unique_job_owners": 0, "active_workspaces": 0, "avg_daily_workspaces": 0, "active_notebooks": 0,
            "models_served": 0, "total_serving_dbus": 0, "avg_daily_models": 0,
            "avg_daily_query_users": 0, "total_workspace_count": 0, "stickiness_pct": 0,
            "start_date": params["start_date"], "end_date": params["end_date"],
            "error": str(e),
        }, "anomalies": {"anomalies": [], "start_date": params["start_date"], "end_date": params["end_date"]}}

    # --- Build KPIs response ---
    kpis_response = {
        "total_queries": 0, "unique_query_users": 0,
        "total_rows_read": 0, "total_bytes_read": 0, "total_compute_seconds": 0,
        "total_jobs": 0, "total_job_runs": 0, "successful_runs": 0, "total_job_run_hours": 0,
        "unique_job_owners": 0, "active_workspaces": 0, "avg_daily_workspaces": 0, "active_notebooks": 0,
        "models_served": 0, "total_serving_dbus": 0, "avg_daily_models": 0,
        "avg_daily_query_users": 0, "total_workspace_count": 0, "stickiness_pct": 0,
        "start_date": params["start_date"], "end_date": params["end_date"],
    }

    # Apply query stats: MV (Lakebase) first, fall back to Delta if result is empty/zero
    def _apply_query_stats(row: dict) -> None:
        kpis_response["total_queries"] = int(row.get("total_queries") or 0)
        kpis_response["unique_query_users"] = int(row.get("unique_query_users") or 0)
        kpis_response["total_rows_read"] = int(row.get("total_rows_read") or 0)
        kpis_response["total_bytes_read"] = int(row.get("total_bytes_read") or 0)
        kpis_response["total_compute_seconds"] = float(row.get("total_compute_seconds") or 0)

    mv_results = query_results.get("mv_kpis")
    mv_has_data = mv_results and len(mv_results) > 0 and int(mv_results[0].get("total_queries") or 0) > 0
    if mv_has_data:
        _apply_query_stats(mv_results[0])
    else:
        # Lakebase daily_query_stats may be empty — use Delta copy directly
        delta_qs = query_results.get("delta_query_stats")
        if delta_qs and len(delta_qs) > 0:
            _apply_query_stats(delta_qs[0])

    # Billing-backed KPIs: total_jobs, total_job_runs, active_workspaces, clusters, models
    billing_results = query_results.get("billing_kpis")
    if billing_results and len(billing_results) > 0:
        row = billing_results[0]
        kpis_response["total_jobs"] = int(row.get("total_jobs") or 0)
        kpis_response["total_job_runs"] = int(row.get("total_job_runs") or 0)
        kpis_response["unique_job_owners"] = int(row.get("unique_job_owners") or 0)
        kpis_response["active_workspaces"] = int(row.get("active_workspaces") or 0)
        kpis_response["active_notebooks"] = int(row.get("total_clusters") or 0)
        kpis_response["models_served"] = int(row.get("models_served") or 0)
        kpis_response["total_serving_dbus"] = float(row.get("total_serving_dbus") or 0)

    avg_daily_ws_results = query_results.get("avg_daily_ws")
    if avg_daily_ws_results and len(avg_daily_ws_results) > 0:
        val = avg_daily_ws_results[0].get("avg_daily_workspaces")
        if val is not None:
            kpis_response["avg_daily_workspaces"] = int(val)

    # Lakeflow-backed KPI: successful_runs (may be missing if lakeflow is inaccessible)
    lakeflow_results = query_results.get("lakeflow_kpis")
    if lakeflow_results and len(lakeflow_results) > 0:
        kpis_response["successful_runs"] = int(lakeflow_results[0].get("successful_runs") or 0)
        kpis_response["total_job_run_hours"] = int(lakeflow_results[0].get("total_run_hours") or 0)

    avg_daily_users_results = query_results.get("avg_daily_query_users")
    if avg_daily_users_results and len(avg_daily_users_results) > 0:
        val = avg_daily_users_results[0].get("avg_daily_query_users")
        if val is not None:
            kpis_response["avg_daily_query_users"] = int(val)

    total_ws_results = query_results.get("total_workspaces")
    if total_ws_results and len(total_ws_results) > 0:
        kpis_response["total_workspace_count"] = int(total_ws_results[0].get("total_workspaces") or 0)

    avg_daily_models_results = query_results.get("avg_daily_models")
    if avg_daily_models_results and len(avg_daily_models_results) > 0:
        kpis_response["avg_daily_models"] = int(avg_daily_models_results[0].get("avg_daily_models") or 0)

    stickiness_results = query_results.get("stickiness_pct")
    if stickiness_results and len(stickiness_results) > 0:
        val = stickiness_results[0].get("stickiness_pct")
        if val is not None:
            kpis_response["stickiness_pct"] = float(val)

    # Override unique_query_users with accurate cross-range distinct count from prpr table
    uc_results = query_results.get("user_count")
    if uc_results and len(uc_results) > 0:
        accurate_users = int(uc_results[0].get("unique_query_users") or 0)
        if accurate_users > 0:
            kpis_response["unique_query_users"] = accurate_users

    # Stale-fallback: if billing-backed KPIs are all zero (query failed or warehouse cold),
    # serve the last known-good result so the UI never shows 0s for a healthy account.
    from backend.workspace_filter import pool_cache_tag
    stale_key = f"{params['start_date']}/{params['end_date']}/{workspace_ids or 'pool:' + pool_cache_tag()}"
    billing_has_data = (
        kpis_response.get("total_jobs", 0) > 0
        or kpis_response.get("active_workspaces", 0) > 0
        or kpis_response.get("total_job_runs", 0) > 0
    )
    if billing_has_data:
        _kpis_stale[stale_key] = {k: v for k, v in kpis_response.items()}
    elif stale_key in _kpis_stale:
        logger.warning("Billing KPIs are all zero; serving stale cached response to prevent zero-flash")
        kpis_response = dict(_kpis_stale[stale_key])
        kpis_response["data_stale"] = True

    # --- Build anomalies response ---
    anomaly_results = query_results.get("anomalies") or []
    anomalies = []
    for row in anomaly_results:
        anomalies.append({
            "usage_date": str(row.get("usage_date")),
            "daily_spend": float(row.get("daily_spend") or 0),
            "prev_day_spend": float(row.get("prev_day_spend") or 0),
            "change_amount": float(row.get("change_amount") or 0),
            "change_percent": float(row.get("change_percent") or 0),
        })

    _kpis_resp = {
        "kpis": kpis_response,
        "anomalies": {
            "anomalies": anomalies,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        },
    }
    # Anomaly surfaces need fresher data — cap at 5 min regardless of scope
    delta_cache_put(_dkey, "billing:kpis-bundle", _kpis_resp, ttl_seconds=cache_ttls.KPI)
    return _kpis_resp


def _kpi_trend_response(
    kpi: str,
    granularity: str,
    results: list[dict[str, Any]] | None,
    _resp,
) -> dict[str, Any]:
    """Turn daily {date, value} rows into the sparkline payload."""
    daily_points = []
    for row in results or []:
        daily_points.append({
            "date": str(row["date"])[:10],
            "value": float(row.get("value") or 0),
        })

    AVG_KPIS = {"avg_cost_per_cluster", "avg_daily_spend", "avg_spend_per_user"}

    if granularity == "weekly" and daily_points:
        from datetime import datetime, timedelta
        buckets: dict[str, list[float]] = {}
        for dp in daily_points:
            d = datetime.strptime(dp["date"], "%Y-%m-%d")
            week_start = d - timedelta(days=d.weekday())
            key = week_start.strftime("%Y-%m-%d")
            buckets.setdefault(key, []).append(dp["value"])
        data_points = []
        for key in sorted(buckets.keys()):
            vals = buckets[key]
            agg = sum(vals) / len(vals) if kpi in AVG_KPIS else sum(vals)
            data_points.append({"date": key, "value": agg})
    elif granularity == "monthly" and daily_points:
        buckets_m: dict[str, list[float]] = {}
        for dp in daily_points:
            key = dp["date"][:7] + "-01"
            buckets_m.setdefault(key, []).append(dp["value"])
        data_points = []
        for key in sorted(buckets_m.keys()):
            vals = buckets_m[key]
            agg = sum(vals) / len(vals) if kpi in AVG_KPIS else sum(vals)
            data_points.append({"date": key, "value": agg})
    else:
        data_points = daily_points

    all_values = [dp["value"] for dp in data_points]
    if not data_points:
        return _resp({
            "kpi": kpi,
            "granularity": granularity,
            "data_points": [],
            "summary": {
                "period_start_value": 0,
                "period_end_value": 0,
                "change_amount": 0,
                "change_percent": 0,
                "min_value": 0,
                "max_value": 0,
                "avg_value": 0,
                "trend": "flat",
            },
        })

    period_start_value = all_values[0]
    period_end_value = all_values[-1]
    change_amount = period_end_value - period_start_value
    change_percent = (change_amount / period_start_value * 100) if period_start_value > 0 else 0
    if abs(change_percent) < 5:
        trend = "flat"
    elif change_percent > 0:
        trend = "increasing"
    else:
        trend = "decreasing"

    return _resp({
        "kpi": kpi,
        "granularity": granularity,
        "data_points": data_points,
        "summary": {
            "period_start_value": round(period_start_value, 2),
            "period_end_value": round(period_end_value, 2),
            "change_amount": round(change_amount, 2),
            "change_percent": round(change_percent, 2),
            "min_value": round(min(all_values), 2),
            "max_value": round(max(all_values), 2),
            "avg_value": round(sum(all_values) / len(all_values), 2),
            "trend": trend,
        },
    })


@router.get("/kpi-trend")
async def get_kpi_trend(
    kpi: str = Query(..., description="KPI to fetch trend for: total_spend, total_dbus, avg_daily_spend, workspace_count, aiml_spend, aiml_dbus, aiml_endpoints, tagged_spend, untagged_spend, infra_cost, infra_clusters, infra_dbu_hours"),
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    granularity: str = Query("daily", description="Granularity: daily, weekly, monthly"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Get trend data for a specific KPI over time."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None

    _dkey = bundle_cache_key(f"billing:kpi-trend:{kpi}:{granularity}", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    def _resp(data: dict) -> dict:
        delta_cache_put(_dkey, "billing:kpi-trend", data, ttl_seconds=cache_ttls.TREND)
        return data

    ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)

    from backend.lakebase_read import KPI_TREND_PG, gold_read, warehouse_view_fallback
    pg = KPI_TREND_PG.get(kpi)
    if pg:
        gold_rows = await asyncio.to_thread(gold_read, f"kpi_trend_{kpi}", pg, params, ws_clause)
        if gold_rows or not warehouse_view_fallback():
            return _kpi_trend_response(kpi, granularity, gold_rows, _resp)

    use_mv = await asyncio.to_thread(_check_mv_available)
    # MV tables are pre-aggregated and cannot be filtered by individual workspace
    if id_list:
        use_mv = False

    # Build query based on KPI type — use MVs when available for daily-aggregation KPIs
    # mv_fallback_query is set when using an MV so we can fall back to live if MV is empty
    mv_fallback_query = None

    if kpi == "total_spend" or kpi == "avg_daily_spend":
        if use_mv:
            catalog, schema = get_catalog_schema()
            query = f"SELECT usage_date as date, SUM(total_spend) as value FROM `{catalog}`.`{schema}`.`daily_usage_summary` WHERE usage_date BETWEEN :start_date AND :end_date GROUP BY usage_date ORDER BY usage_date"
            mv_fallback_query = """
        WITH usage_with_price AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
        else:
            query = """
        WITH usage_with_price AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "total_dbus":
        if use_mv:
            catalog, schema = get_catalog_schema()
            query = f"SELECT usage_date as date, SUM(total_dbus) as value FROM `{catalog}`.`{schema}`.`daily_usage_summary` WHERE usage_date BETWEEN :start_date AND :end_date GROUP BY usage_date ORDER BY usage_date"
            mv_fallback_query = """
        SELECT
          usage_date as date,
          SUM(usage_quantity) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
        else:
            query = """
        SELECT
          usage_date as date,
          SUM(usage_quantity) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "workspace_count":
        if use_mv:
            catalog, schema = get_catalog_schema()
            query = f"SELECT usage_date as date, COUNT(DISTINCT workspace_id) as value FROM `{catalog}`.`{schema}`.`daily_usage_summary` WHERE usage_date BETWEEN :start_date AND :end_date GROUP BY usage_date ORDER BY usage_date"
            mv_fallback_query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT workspace_id) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
        else:
            query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT workspace_id) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "aiml_spend":
        query = """
        WITH usage_with_price AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND (
              u.billing_origin_product = 'MODEL_SERVING'
              OR u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
              OR u.sku_name LIKE '%ANTHROPIC%'
              OR u.sku_name LIKE '%OPENAI%'
              OR u.sku_name LIKE '%GEMINI%'
              OR u.sku_name LIKE '%INFERENCE%'
            )
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "aiml_dbus":
        query = """
        SELECT
          usage_date as date,
          SUM(usage_quantity) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND (
            billing_origin_product = 'MODEL_SERVING'
            OR sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
            OR sku_name LIKE '%ANTHROPIC%'
            OR sku_name LIKE '%OPENAI%'
            OR sku_name LIKE '%GEMINI%'
            OR sku_name LIKE '%INFERENCE%'
          )
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "aiml_endpoints":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT usage_metadata.endpoint_name) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND (
            billing_origin_product = 'MODEL_SERVING'
            OR sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
            OR sku_name LIKE '%INFERENCE%'
          )
          AND usage_metadata.endpoint_name IS NOT NULL
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "aiml_avg_endpoint_cost":
        query = """
        SELECT
          u.usage_date as date,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0))
            / NULLIF(COUNT(DISTINCT u.usage_metadata.endpoint_name), 0) as value
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name
          AND u.cloud = p.cloud
          AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND (
            u.billing_origin_product = 'MODEL_SERVING'
            OR u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
            OR u.sku_name LIKE '%INFERENCE%'
          )
          AND u.usage_metadata.endpoint_name IS NOT NULL
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
    elif kpi == "tagged_spend":
        query = """
        WITH usage_with_tags AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu,
            CASE WHEN u.custom_tags IS NOT NULL AND size(u.custom_tags) > 0 THEN true ELSE false END as has_tags
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
        )
        SELECT
          usage_date as date,
          SUM(CASE WHEN has_tags THEN usage_quantity * price_per_dbu ELSE 0 END) as value
        FROM usage_with_tags
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "untagged_spend":
        query = """
        WITH usage_with_tags AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu,
            CASE WHEN u.custom_tags IS NOT NULL AND size(u.custom_tags) > 0 THEN true ELSE false END as has_tags
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
        )
        SELECT
          usage_date as date,
          SUM(CASE WHEN NOT has_tags THEN usage_quantity * price_per_dbu ELSE 0 END) as value
        FROM usage_with_tags
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "infra_cost":
        query = """
        WITH usage_with_price AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND (u.sku_name LIKE '%ALL_PURPOSE%' OR u.sku_name LIKE '%JOBS%' OR u.sku_name LIKE '%SQL%' OR u.sku_name LIKE '%DLT%')
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "infra_clusters":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT usage_metadata.cluster_id) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND usage_metadata.cluster_id IS NOT NULL
          AND (sku_name LIKE '%ALL_PURPOSE%' OR sku_name LIKE '%JOBS%' OR sku_name LIKE '%DLT%')
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "infra_dbu_hours":
        query = """
        SELECT
          usage_date as date,
          SUM(usage_quantity) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND (sku_name LIKE '%ALL_PURPOSE%' OR sku_name LIKE '%JOBS%' OR sku_name LIKE '%DLT%')
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "avg_cost_per_cluster":
        query = """
        WITH usage_with_price AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            u.usage_metadata.cluster_id as cluster_id,
            COALESCE(p.pricing.default, 0) as price_per_dbu
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.usage_metadata.cluster_id IS NOT NULL
            AND (u.sku_name LIKE '%ALL_PURPOSE%' OR u.sku_name LIKE '%JOBS%' OR u.sku_name LIKE '%DLT%')
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu) / NULLIF(COUNT(DISTINCT cluster_id), 0) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "apps_spend":
        query = """
        WITH usage_with_price AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.billing_origin_product = 'APPS'
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "apps_dbus":
        query = """
        SELECT
          usage_date as date,
          SUM(usage_quantity) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND billing_origin_product = 'APPS'
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "apps_count":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT COALESCE(usage_metadata.app_id, 'unknown')) as value
        FROM system.billing.usage
        WHERE usage_date BETWEEN :start_date AND :end_date
          AND usage_quantity > 0
          AND billing_origin_product = 'APPS'
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "apps_avg_cost_per_app":
        query = """
        SELECT
          usage_date as date,
          SUM(usage_quantity * COALESCE(p.pricing.default, 0))
            / NULLIF(COUNT(DISTINCT COALESCE(u.usage_metadata.app_id, 'unknown')), 0) as value
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.billing_origin_product = 'APPS'
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "total_tags":
        query = """
        WITH tagged AS (
          SELECT u.usage_date, t.key as tag_key, t.value as tag_value
          FROM system.billing.usage u
          LATERAL VIEW EXPLODE(u.custom_tags) t AS key, value
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.custom_tags IS NOT NULL
            AND size(u.custom_tags) > 0
        )
        SELECT
          usage_date as date,
          COUNT(DISTINCT CONCAT(tag_key, ':', tag_value)) as value
        FROM tagged
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "cost_per_tag":
        query = """
        WITH tagged AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu,
            t.key as tag_key,
            t.value as tag_value
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
          LATERAL VIEW EXPLODE(u.custom_tags) t AS key, value
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.custom_tags IS NOT NULL
            AND size(u.custom_tags) > 0
        )
        SELECT
          usage_date as date,
          SUM(usage_quantity * price_per_dbu)
            / NULLIF(COUNT(DISTINCT CONCAT(tag_key, ':', tag_value)), 0) as value
        FROM tagged
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "sql_spend":
        _cat, _sch = get_catalog_schema()
        query = f"""
        SELECT
          DATE(start_time) as date,
          SUM(query_attributed_dollars_estimation) as value
        FROM `{_cat}`.`{_sch}`.`dbsql_cost_per_query`
        WHERE DATE(start_time) >= :start_date
          AND DATE(start_time) <= :end_date
        GROUP BY DATE(start_time)
        ORDER BY date
        """
    elif kpi == "user_spend":
        if use_mv:
            catalog, schema = get_catalog_schema()
            query = f"SELECT usage_date as date, SUM(user_attributed_spend) as value FROM `{catalog}`.`{schema}`.`daily_usage_summary` WHERE usage_date BETWEEN :start_date AND :end_date GROUP BY usage_date ORDER BY usage_date"
            mv_fallback_query = """
        SELECT
          u.usage_date as date,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as value
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name
          AND u.cloud = p.cloud
          AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.identity_metadata.run_as IS NOT NULL
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
        else:
            query = """
        SELECT
          u.usage_date as date,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as value
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name
          AND u.cloud = p.cloud
          AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.identity_metadata.run_as IS NOT NULL
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
    elif kpi == "avg_spend_per_user":
        query = """
        SELECT
          u.usage_date as date,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) / NULLIF(COUNT(DISTINCT u.identity_metadata.run_as), 0) as value
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name
          AND u.cloud = p.cloud
          AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.identity_metadata.run_as IS NOT NULL
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
    elif kpi == "tag_coverage_pct":
        query = """
        WITH usage_with_tags AS (
          SELECT
            u.usage_date,
            u.usage_quantity,
            COALESCE(p.pricing.default, 0) as price_per_dbu,
            CASE WHEN u.custom_tags IS NOT NULL AND size(u.custom_tags) > 0 THEN true ELSE false END as has_tags
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
        )
        SELECT
          usage_date as date,
          100.0 * SUM(CASE WHEN has_tags THEN usage_quantity * price_per_dbu ELSE 0 END)
            / NULLIF(SUM(usage_quantity * price_per_dbu), 0) as value
        FROM usage_with_tags
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "power_user_spend":
        # ws_clause is embedded inline via f-string; null it out so _inject_ws_filter below is a no-op
        query = f"""
        WITH user_totals AS (
          SELECT
            u.identity_metadata.run_as AS user_email,
            SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.identity_metadata.run_as IS NOT NULL
            {ws_clause}
          GROUP BY u.identity_metadata.run_as
        ),
        period_total AS (SELECT SUM(total_spend) AS grand_total FROM user_totals),
        power_users AS (
          SELECT ut.user_email
          FROM user_totals ut, period_total pt
          WHERE ut.total_spend / NULLIF(pt.grand_total, 0) >= 0.10
        )
        SELECT
          u.usage_date AS date,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS value
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name
          AND u.cloud = p.cloud
          AND p.price_end_time IS NULL
        JOIN power_users pu ON u.identity_metadata.run_as = pu.user_email
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          {ws_clause}
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
        ws_clause = ""  # already embedded above; prevent double-injection
    else:
        return {"error": f"Unknown KPI: {kpi}"}

    if not warehouse_view_fallback():
        return _kpi_trend_response(kpi, granularity, [], _resp)

    try:
        results = await asyncio.to_thread(execute_query, apply_mv_overrides(_inject_ws_filter(query, ws_clause), *get_catalog_schema()), params)
        if use_mv:
            from backend.lakebase_read import dual_read
            pg_live = KPI_TREND_PG.get(kpi)
            if pg_live:
                results = dual_read(f"kpi_trend_{kpi}", pg_live, params, ws_clause, results or [], ["date"])
        if not results and mv_fallback_query:
            from backend.lakebase_read import warehouse_view_fallback
            if warehouse_view_fallback():
                logger.info(f"KPI trend MV returned empty for {kpi}, falling back to live query")
                results = await asyncio.to_thread(execute_query, _inject_ws_filter(mv_fallback_query, ws_clause), params)
    except Exception as e:
        logger.error(f"KPI trend query failed for {kpi}: {e}")
        if mv_fallback_query:
            from backend.lakebase_read import warehouse_view_fallback
            if warehouse_view_fallback():
                try:
                    results = await asyncio.to_thread(execute_query, _inject_ws_filter(mv_fallback_query, ws_clause), params)
                except Exception as fallback_e:
                    logger.warning(f"KPI trend fallback query also failed for {kpi}: {fallback_e}")
                    results = []
        if not results:
            return {
                "kpi": kpi,
                "granularity": granularity,
                "data_points": [],
                "summary": {
                    "period_start_value": 0,
                    "period_end_value": 0,
                    "change_amount": 0,
                    "change_percent": 0,
                    "min_value": 0,
                    "max_value": 0,
                    "avg_value": 0,
                    "trend": "flat",
                },
            }

    # Process results into daily data points
    daily_points = []
    for row in results:
        daily_points.append({
            "date": str(row["date"]),
            "value": float(row["value"] or 0)
        })

    # KPIs that represent averages/rates — use AVG when grouping into buckets
    AVG_KPIS = {"avg_cost_per_cluster", "avg_daily_spend", "avg_spend_per_user"}

    # Group into weekly/monthly buckets if needed
    if granularity == "weekly" and daily_points:
        from datetime import datetime, timedelta
        buckets: dict[str, list[float]] = {}
        for dp in daily_points:
            d = datetime.strptime(dp["date"], "%Y-%m-%d")
            week_start = d - timedelta(days=d.weekday())
            key = week_start.strftime("%Y-%m-%d")
            buckets.setdefault(key, []).append(dp["value"])
        data_points = []
        for key in sorted(buckets.keys()):
            vals = buckets[key]
            agg = sum(vals) / len(vals) if kpi in AVG_KPIS else sum(vals)
            data_points.append({"date": key, "value": agg})
    elif granularity == "monthly" and daily_points:
        buckets_m: dict[str, list[float]] = {}
        for dp in daily_points:
            key = dp["date"][:7] + "-01"
            buckets_m.setdefault(key, []).append(dp["value"])
        data_points = []
        for key in sorted(buckets_m.keys()):
            vals = buckets_m[key]
            agg = sum(vals) / len(vals) if kpi in AVG_KPIS else sum(vals)
            data_points.append({"date": key, "value": agg})
    else:
        data_points = daily_points

    # Calculate summary statistics
    all_values = [dp["value"] for dp in data_points]

    if not data_points:
        return {
            "kpi": kpi,
            "granularity": granularity,
            "data_points": [],
            "summary": {
                "period_start_value": 0,
                "period_end_value": 0,
                "change_amount": 0,
                "change_percent": 0,
                "min_value": 0,
                "max_value": 0,
                "avg_value": 0,
                "trend": "flat"
            }
        }

    period_start_value = all_values[0]
    period_end_value = all_values[-1]
    change_amount = period_end_value - period_start_value
    change_percent = (change_amount / period_start_value * 100) if period_start_value > 0 else 0

    # Determine trend
    if abs(change_percent) < 5:
        trend = "flat"
    elif change_percent > 0:
        trend = "increasing"
    else:
        trend = "decreasing"

    return _resp({
        "kpi": kpi,
        "granularity": granularity,
        "data_points": data_points,
        "summary": {
            "period_start_value": round(period_start_value, 2),
            "period_end_value": round(period_end_value, 2),
            "change_amount": round(change_amount, 2),
            "change_percent": round(change_percent, 2),
            "min_value": round(min(all_values), 2),
            "max_value": round(max(all_values), 2),
            "avg_value": round(sum(all_values) / len(all_values), 2),
            "trend": trend
        }
    })

def _build_platform_kpi_response(kpi: str, granularity: str, data_points: list[dict]) -> dict[str, Any]:
    """Build the standard platform KPI trend response from a list of {date, value} points."""
    PLATFORM_AVG_KPIS = {"avg_query_duration"}
    all_values = [dp["value"] for dp in data_points]
    if not data_points:
        return {"kpi": kpi, "granularity": granularity, "data_points": [], "summary": {
            "period_start_value": 0, "period_end_value": 0, "change_amount": 0,
            "change_percent": 0, "min_value": 0, "max_value": 0, "avg_value": 0, "trend": "flat"
        }}
    period_start_value = all_values[0]
    period_end_value = all_values[-1]
    change_amount = period_end_value - period_start_value
    change_percent = (change_amount / period_start_value * 100) if period_start_value > 0 else 0
    trend = "flat" if abs(change_percent) < 5 else ("increasing" if change_percent > 0 else "decreasing")
    # For avg metrics that carry a query_count weight, use a weighted mean so the summary
    # matches the KPI card (which computes AVG over all individual queries, not daily avgs).
    if kpi in PLATFORM_AVG_KPIS and any("query_count" in dp for dp in data_points):
        total_weight = sum(dp.get("query_count", 1) for dp in data_points)
        avg_value = sum(dp["value"] * dp.get("query_count", 1) for dp in data_points) / total_weight if total_weight > 0 else 0
    else:
        avg_value = sum(all_values) / len(all_values)
    # Strip internal weight fields before returning to the client
    clean_points = [{"date": dp["date"], "value": dp["value"]} for dp in data_points]
    return {
        "kpi": kpi, "granularity": granularity, "data_points": clean_points,
        "summary": {
            "period_start_value": round(period_start_value, 2),
            "period_end_value": round(period_end_value, 2),
            "change_amount": round(change_amount, 2),
            "change_percent": round(change_percent, 2),
            "min_value": round(min(all_values), 2),
            "max_value": round(max(all_values), 2),
            "avg_value": round(avg_value, 2),
            "trend": trend,
        }
    }


@router.get("/platform-kpi-trend")
async def get_platform_kpi_trend(
    kpi: str = Query(..., description="Platform KPI: total_queries, total_rows_read, total_bytes_read, total_compute_seconds, total_jobs, total_job_runs, successful_runs, active_notebooks, active_workspaces, models_served, total_users, sql_queries, sql_users"),
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
    granularity: str = Query("daily", description="Granularity: daily, weekly, monthly"),
    workspace_ids: str = Query(default=None, description="Comma-separated workspace IDs to filter"),
) -> dict[str, Any]:
    """Get trend data for platform KPIs over time."""
    from backend import workspace_filter as wf
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None

    _dkey = bundle_cache_key(f"billing:platform-kpi-trend:{kpi}:{granularity}", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    def _resp(data: dict) -> dict:
        delta_cache_put(_dkey, "billing:platform-kpi-trend", data, ttl_seconds=cache_ttls.TREND)
        return data

    ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
    from backend.lakebase_read import PLATFORM_KPI_TREND_PG, gold_read, warehouse_view_fallback
    pg_tmpl = PLATFORM_KPI_TREND_PG.get(kpi)
    if pg_tmpl:
        rows = await asyncio.to_thread(
            gold_read, f"platform_kpi_trend_{kpi}", pg_tmpl, params, ws_clause,
        )
        if rows or not warehouse_view_fallback():
            daily_points = [{"date": str(r["date"])[:10], "value": float(r.get("value") or 0)} for r in rows]
            PLATFORM_AVG_KPIS = {"avg_query_duration"}
            if granularity == "weekly" and daily_points:
                from datetime import datetime, timedelta
                buckets: dict[str, list[float]] = {}
                for dp in daily_points:
                    d = datetime.strptime(dp["date"], "%Y-%m-%d")
                    week_start = d - timedelta(days=d.weekday())
                    buckets.setdefault(week_start.strftime("%Y-%m-%d"), []).append(dp["value"])
                data_points = [
                    {"date": key, "value": (sum(v) / len(v) if kpi in PLATFORM_AVG_KPIS else sum(v))}
                    for key, v in sorted(buckets.items())
                ]
            elif granularity == "monthly" and daily_points:
                buckets_m: dict[str, list[float]] = {}
                for dp in daily_points:
                    buckets_m.setdefault(str(dp["date"])[:7] + "-01", []).append(dp["value"])
                data_points = [
                    {"date": key, "value": (sum(v) / len(v) if kpi in PLATFORM_AVG_KPIS else sum(v))}
                    for key, v in sorted(buckets_m.items())
                ]
            else:
                data_points = daily_points
            return _resp(_build_platform_kpi_response(kpi, granularity, data_points))

    # KPIs sourced from system.query.history (workspace_id filter via _inject_qh_ws_filter)
    _QH_KPIS = frozenset({"total_queries", "total_rows_read", "total_bytes_read",
                           "total_compute_seconds", "total_users", "avg_query_duration",
                           "unique_warehouses"})

    # For DISTINCT COUNT KPIs, monthly/weekly rollup must be done in SQL — summing
    # daily distinct counts in Python overcounts (user active 30 days = 30x, not 1x).
    # We re-query with DATE_TRUNC grouping so the DB computes true monthly/weekly uniques.
    DATE_TRUNC_MAP = {"weekly": "WEEK", "monthly": "MONTH"}
    if granularity in DATE_TRUNC_MAP:
        trunc = DATE_TRUNC_MAP[granularity]
        if kpi == "total_users":
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', DATE(start_time)) as date,
              COUNT(DISTINCT COALESCE(executed_by, executed_as_user_id)) as value
            FROM system.query.history
            WHERE start_time >= CAST(:start_date AS TIMESTAMP)
              AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
              AND (executed_by IS NOT NULL OR executed_as_user_id IS NOT NULL)
            GROUP BY DATE_TRUNC('{trunc}', DATE(start_time))
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, _inject_qh_ws_filter(query, ws_clause), params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))
        elif kpi == "active_workspaces":
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', usage_date) as date,
              COUNT(DISTINCT workspace_id) as value
            FROM system.billing.usage
            WHERE usage_date >= :start_date AND usage_date <= :end_date AND usage_quantity > 0
            GROUP BY DATE_TRUNC('{trunc}', usage_date)
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, apply_mv_overrides(_inject_ws_filter(query, ws_clause), *get_catalog_schema()), params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))
        elif kpi == "total_jobs":
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', usage_date) as date,
              COUNT(DISTINCT usage_metadata.job_id) as value
            FROM system.billing.usage
            WHERE usage_date >= :start_date AND usage_date <= :end_date
              AND usage_metadata.job_id IS NOT NULL AND usage_quantity > 0
            GROUP BY DATE_TRUNC('{trunc}', usage_date)
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, apply_mv_overrides(_inject_ws_filter(query, ws_clause), *get_catalog_schema()), params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))
        elif kpi == "models_served":
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', usage_date) as date,
              COUNT(DISTINCT usage_metadata.endpoint_name) as value
            FROM system.billing.usage
            WHERE usage_date >= :start_date AND usage_date <= :end_date
              AND sku_name LIKE '%INFERENCE%' AND usage_quantity > 0
            GROUP BY DATE_TRUNC('{trunc}', usage_date)
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, apply_mv_overrides(_inject_ws_filter(query, ws_clause), *get_catalog_schema()), params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))
        elif kpi == "unique_warehouses":
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', DATE(start_time)) as date,
              COUNT(DISTINCT warehouse_id) as value
            FROM system.query.history
            WHERE start_time >= CAST(:start_date AS TIMESTAMP)
              AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
              AND warehouse_id IS NOT NULL
            GROUP BY DATE_TRUNC('{trunc}', DATE(start_time))
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, _inject_qh_ws_filter(query, ws_clause), params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))
        elif kpi == "sql_users":
            _cat, _sch = get_catalog_schema()
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', DATE(start_time)) as date,
              COUNT(DISTINCT executed_by) as value
            FROM `{_cat}`.`{_sch}`.`dbsql_cost_per_query`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            GROUP BY DATE_TRUNC('{trunc}', DATE(start_time))
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, query, params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))
        elif kpi == "sql_queries":
            _cat, _sch = get_catalog_schema()
            query = f"""
            SELECT
              DATE_TRUNC('{trunc}', DATE(start_time)) as date,
              COUNT(*) as value
            FROM `{_cat}`.`{_sch}`.`dbsql_cost_per_query`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            GROUP BY DATE_TRUNC('{trunc}', DATE(start_time))
            ORDER BY date
            """
            results = await asyncio.to_thread(execute_query, query, params)
            daily_points = [{"date": str(r["date"])[:10], "value": float(r["value"] or 0)} for r in results]
            return _resp(_build_platform_kpi_response(kpi, granularity, daily_points))

    # Build query based on KPI type
    # Use explicit TIMESTAMP casts for partition-aware date filtering on system.query.history
    if kpi == "total_queries":
        query = """
        SELECT
          DATE(start_time) as date,
          COUNT(*) as value
        FROM system.query.history
        WHERE start_time >= CAST(:start_date AS TIMESTAMP)
          AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "total_rows_read":
        query = """
        SELECT
          DATE(start_time) as date,
          SUM(COALESCE(read_rows, 0)) as value
        FROM system.query.history
        WHERE start_time >= CAST(:start_date AS TIMESTAMP)
          AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "total_bytes_read":
        query = """
        SELECT
          DATE(start_time) as date,
          SUM(COALESCE(read_bytes, 0)) as value
        FROM system.query.history
        WHERE start_time >= CAST(:start_date AS TIMESTAMP)
          AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "total_compute_seconds":
        query = """
        SELECT
          DATE(start_time) as date,
          SUM(COALESCE(total_task_duration_ms, 0)) / 1000.0 as value
        FROM system.query.history
        WHERE start_time >= CAST(:start_date AS TIMESTAMP)
          AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "total_jobs":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT usage_metadata.job_id) as value
        FROM system.billing.usage
        WHERE usage_date >= :start_date
          AND usage_date <= :end_date
          AND usage_metadata.job_id IS NOT NULL
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "total_job_runs":
        query = """
        SELECT
          usage_date as date,
          COUNT(*) as value
        FROM system.billing.usage
        WHERE usage_date >= :start_date
          AND usage_date <= :end_date
          AND usage_metadata.job_id IS NOT NULL
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "successful_runs":
        query = """
        SELECT
          DATE(period_start_time) as date,
          COUNT(CASE WHEN result_state = 'SUCCEEDED' THEN 1 END) as value
        FROM system.lakeflow.job_run_timeline
        WHERE period_start_time >= :start_date
          AND period_start_time < DATE_ADD(CAST(:end_date AS DATE), 1)
        GROUP BY DATE(period_start_time)
        ORDER BY DATE(period_start_time)
        """
    elif kpi == "active_notebooks":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT usage_metadata.cluster_id) as value
        FROM system.billing.usage
        WHERE usage_date >= :start_date
          AND usage_date <= :end_date
          AND usage_metadata.cluster_id IS NOT NULL
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "active_workspaces":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT workspace_id) as value
        FROM system.billing.usage
        WHERE usage_date >= :start_date
          AND usage_date <= :end_date
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "models_served":
        query = """
        SELECT
          usage_date as date,
          COUNT(DISTINCT usage_metadata.endpoint_name) as value
        FROM system.billing.usage
        WHERE usage_date >= :start_date
          AND usage_date <= :end_date
          AND sku_name LIKE '%INFERENCE%'
          AND usage_quantity > 0
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "total_users":
        query = """
        SELECT
          DATE(start_time) as date,
          COUNT(DISTINCT COALESCE(executed_by, executed_as_user_id)) as value
        FROM system.query.history
        WHERE start_time >= CAST(:start_date AS TIMESTAMP)
          AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
          AND (executed_by IS NOT NULL OR executed_as_user_id IS NOT NULL)
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "avg_query_duration":
        _cat, _sch = get_catalog_schema()
        query = f"""
        SELECT
          DATE(start_time) as date,
          AVG(duration_seconds) as value,
          COUNT(*) as query_count
        FROM `{_cat}`.`{_sch}`.`dbsql_cost_per_query`
        WHERE DATE(start_time) >= :start_date
          AND DATE(start_time) <= :end_date
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "unique_warehouses":
        query = """
        SELECT
          DATE(start_time) as date,
          COUNT(DISTINCT warehouse_id) as value
        FROM system.query.history
        WHERE start_time >= CAST(:start_date AS TIMESTAMP)
          AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
          AND warehouse_id IS NOT NULL
        GROUP BY DATE(start_time)
        ORDER BY DATE(start_time)
        """
    elif kpi == "sql_queries":
        _cat, _sch = get_catalog_schema()
        query = f"""
        SELECT
          DATE(start_time) as date,
          COUNT(*) as value
        FROM `{_cat}`.`{_sch}`.`dbsql_cost_per_query`
        WHERE DATE(start_time) >= :start_date
          AND DATE(start_time) <= :end_date
        GROUP BY DATE(start_time)
        ORDER BY date
        """
    elif kpi == "sql_users":
        _cat, _sch = get_catalog_schema()
        query = f"""
        SELECT
          DATE(start_time) as date,
          COUNT(DISTINCT executed_by) as value
        FROM `{_cat}`.`{_sch}`.`dbsql_cost_per_query`
        WHERE DATE(start_time) >= :start_date
          AND DATE(start_time) <= :end_date
        GROUP BY DATE(start_time)
        ORDER BY date
        """
    elif kpi == "stickiness":
        _qh_ws = ws_clause if ws_clause else ""
        stickiness_query = f"""
        WITH total_users AS (
          SELECT COUNT(DISTINCT COALESCE(executed_by, executed_as_user_id)) AS total
          FROM system.query.history
          WHERE start_time >= CAST(:start_date AS TIMESTAMP)
            AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
            {_qh_ws}
            AND (executed_by IS NOT NULL OR executed_as_user_id IS NOT NULL)
        ),
        daily_users AS (
          SELECT
            DATE(start_time) AS date,
            COUNT(DISTINCT COALESCE(executed_by, executed_as_user_id)) AS dau
          FROM system.query.history
          WHERE start_time >= CAST(:start_date AS TIMESTAMP)
            AND start_time < CAST(DATE_ADD(CAST(:end_date AS DATE), 1) AS TIMESTAMP)
            {_qh_ws}
            AND (executed_by IS NOT NULL OR executed_as_user_id IS NOT NULL)
          GROUP BY DATE(start_time)
        )
        SELECT d.date, 100.0 * d.dau / NULLIF(t.total, 0) AS value
        FROM daily_users d CROSS JOIN total_users t
        ORDER BY d.date
        """
        try:
            results = await asyncio.to_thread(execute_query, stickiness_query, params)
        except Exception as e:
            logger.error(f"Platform KPI trend query failed for {kpi}: {e}")
            return {"kpi": kpi, "granularity": granularity, "data_points": [],
                    "summary": {"period_start_value": 0, "period_end_value": 0, "change_amount": 0,
                                "change_percent": 0, "min_value": 0, "max_value": 0, "avg_value": 0, "trend": "flat"}}
        s_points = [{"date": str(r["date"]), "value": float(r["value"] or 0)} for r in results]
        return _resp(_build_platform_kpi_response(kpi, granularity, s_points))
    else:
        return {"error": f"Unknown platform KPI: {kpi}"}

    try:
        filtered_query = _inject_qh_ws_filter(query, ws_clause) if kpi in _QH_KPIS else _inject_ws_filter(query, ws_clause)
        results = await asyncio.to_thread(execute_query, filtered_query, params)
    except Exception as e:
        logger.error(f"Platform KPI trend query failed for {kpi}: {e}")
        return {
            "kpi": kpi,
            "granularity": granularity,
            "data_points": [],
            "summary": {
                "period_start_value": 0,
                "period_end_value": 0,
                "change_amount": 0,
                "change_percent": 0,
                "min_value": 0,
                "max_value": 0,
                "avg_value": 0,
                "trend": "flat"
            }
        }

    # Process results into daily data points
    daily_points = []
    for row in results:
        daily_points.append({
            "date": str(row["date"]),
            "value": float(row["value"] or 0)
        })

    # KPIs that represent averages/rates — use AVG when grouping into buckets
    PLATFORM_AVG_KPIS = {"avg_query_duration"}

    # Group into weekly/monthly buckets if needed
    if granularity == "weekly" and daily_points:
        from datetime import datetime, timedelta
        buckets: dict[str, list[float]] = {}
        for dp in daily_points:
            d = datetime.strptime(dp["date"], "%Y-%m-%d")
            week_start = d - timedelta(days=d.weekday())
            key = week_start.strftime("%Y-%m-%d")
            buckets.setdefault(key, []).append(dp["value"])
        data_points = []
        for key in sorted(buckets.keys()):
            vals = buckets[key]
            agg = sum(vals) / len(vals) if kpi in PLATFORM_AVG_KPIS else sum(vals)
            data_points.append({"date": key, "value": agg})
    elif granularity == "monthly" and daily_points:
        buckets_m: dict[str, list[float]] = {}
        for dp in daily_points:
            key = dp["date"][:7] + "-01"
            buckets_m.setdefault(key, []).append(dp["value"])
        data_points = []
        for key in sorted(buckets_m.keys()):
            vals = buckets_m[key]
            agg = sum(vals) / len(vals) if kpi in PLATFORM_AVG_KPIS else sum(vals)
            data_points.append({"date": key, "value": agg})
    else:
        data_points = daily_points

    # Calculate summary statistics
    # Use all values (including zeros) for data points, but filter for summary
    all_values = [dp["value"] for dp in data_points]
    positive_values = [v for v in all_values if v > 0]

    if not data_points:
        return {
            "kpi": kpi,
            "granularity": granularity,
            "data_points": [],
            "summary": {
                "period_start_value": 0,
                "period_end_value": 0,
                "change_amount": 0,
                "change_percent": 0,
                "min_value": 0,
                "max_value": 0,
                "avg_value": 0,
                "trend": "flat"
            }
        }

    # Use positive_values for start/end/min/max/trend; all_values for avg so it matches card computation
    values = positive_values if positive_values else all_values

    period_start_value = values[0] if values else 0
    period_end_value = values[-1] if values else 0
    change_amount = period_end_value - period_start_value
    change_percent = (change_amount / period_start_value * 100) if period_start_value > 0 else 0

    # Determine trend
    if abs(change_percent) < 5:
        trend = "flat"
    elif change_percent > 0:
        trend = "increasing"
    else:
        trend = "decreasing"

    return _resp({
        "kpi": kpi,
        "granularity": granularity,
        "data_points": data_points,
        "summary": {
            "period_start_value": round(period_start_value, 2),
            "period_end_value": round(period_end_value, 2),
            "change_amount": round(change_amount, 2),
            "change_percent": round(change_percent, 2),
            "min_value": round(min(values), 2),
            "max_value": round(max(values), 2),
            "avg_value": round(sum(all_values) / len(all_values), 2) if all_values else 0,
            "trend": trend
        }
    })


