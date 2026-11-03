"""Databricks Apps cost analysis API endpoints."""

import asyncio
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response

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
    get_workspace_client,
    source_label_filter_clause,
    source_scope_unavailable,
)
from backend.routers.billing import _check_mv_available

# Matches standard Databricks service principal UUID format
_SP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8,12}$",
    re.IGNORECASE,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_apps_bundle_inflight: set[str] = set()
_apps_bundle_inflight_lock = threading.Lock()


def get_default_start_date() -> str:
    """Get default start date (last 30 days)."""
    return (date.today() - timedelta(days=30)).isoformat()


def get_default_end_date() -> str:
    """Get default end date (today)."""
    return date.today().isoformat()


# The active_days param controls the "active" filter: apps with usage in
# the last N days of the date range are considered active.
ACTIVE_DAYS = 7

# ── App name resolution (UUID → human-readable name) ────────────────────

_app_name_cache: dict[str, dict[str, str]] = {}  # uuid → {name, url}
_app_name_cache_time: float = 0
APP_NAME_CACHE_TTL = 3600  # 1 hour - app list rarely changes


def _get_app_registry() -> dict[str, dict[str, str]]:
    """Fetch and cache the UUID → {name, url} mapping from Databricks Apps API.

    Returns a dict keyed by app UUID with values like:
      {"name": "cost-observability", "url": "https://cost-observability-xxx.aws.databricksapps.com"}
    """
    global _app_name_cache, _app_name_cache_time

    now = time.time()
    if _app_name_cache and (now - _app_name_cache_time) < APP_NAME_CACHE_TTL:
        return _app_name_cache

    try:
        w = get_workspace_client()
        registry: dict[str, dict[str, str]] = {}
        for app in w.apps.list():
            app_id = getattr(app, "id", None)
            app_name = getattr(app, "name", None)
            app_url = getattr(app, "url", None) or ""
            # Try to get icon/thumbnail info from the app object
            app_description = getattr(app, "description", None) or ""
            if app_id and app_name:
                registry[app_id] = {
                    "name": app_name,
                    "url": app_url,
                    "description": app_description,
                }
        _app_name_cache = registry
        _app_name_cache_time = now
        logger.info("Refreshed app name cache: %d apps", len(registry))
        return registry
    except Exception as e:
        logger.warning("Failed to fetch app registry: %s", e)
        return _app_name_cache  # return stale cache on error


_APP_URL_WS_RE = re.compile(r"-(\d{6,})\.[a-z0-9.-]*databricksapps\.com", re.IGNORECASE)
_host_ws_id_cache: str | None = None


def _host_workspace_id(registry: dict[str, dict[str, str]]) -> str | None:
    """The workspace the Apps registry describes: the one this app runs in.

    The Apps API only lists apps in the calling workspace, so the registry says
    nothing about apps billed to any other workspace. App URLs carry the
    workspace id (<name>-<workspace-id>.<cloud>.databricksapps.com).
    """
    global _host_ws_id_cache
    if _host_ws_id_cache:
        return _host_ws_id_cache
    for entry in registry.values():
        m = _APP_URL_WS_RE.search(entry.get("url") or "")
        if m:
            _host_ws_id_cache = m.group(1)
            return _host_ws_id_cache
    env_id = (os.getenv("DATABRICKS_WORKSPACE_ID") or "").strip()
    if env_id:
        _host_ws_id_cache = env_id
        return env_id
    try:
        ws_id = get_workspace_client().get_workspace_id()
        if ws_id:
            _host_ws_id_cache = str(ws_id)
    except Exception as e:
        logger.debug("Could not resolve the host workspace id: %s", e)
    return _host_ws_id_cache


# Names for apps outside the host workspace come from billing, which records
# the app name on every APPS usage row.
APPS_BILLING_NAMES = """
SELECT
  u.usage_metadata.app_id AS app_id,
  MAX_BY(u.usage_metadata.app_name, u.usage_date) AS app_name
FROM system.billing.usage u
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.billing_origin_product = 'APPS'
  AND u.usage_metadata.app_id IS NOT NULL
  AND u.usage_metadata.app_name IS NOT NULL
GROUP BY u.usage_metadata.app_id
"""

BILLING_NAME_TTL = 3600
BILLING_NAME_LOOKBACK_DAYS = 365
BILLING_NAME_RETRY_SECONDS = 300
_billing_names: dict[str, str] = {}
_billing_names_time: float = 0
_billing_names_refreshing = False
_billing_names_lock = threading.Lock()


def _refresh_billing_names() -> None:
    global _billing_names_time, _billing_names_refreshing
    try:
        today = date.today()
        rows = execute_query(APPS_BILLING_NAMES, {
            "start_date": (today - timedelta(days=BILLING_NAME_LOOKBACK_DAYS)).isoformat(),
            "end_date": today.isoformat(),
        })
        names = {
            str(r["app_id"]): str(r["app_name"])
            for r in rows or []
            if r.get("app_id") and r.get("app_name")
        }
        with _billing_names_lock:
            _billing_names.update(names)
            _billing_names_time = time.time()
        logger.info("Refreshed billing app names: %d apps", len(names))
    except Exception as e:
        logger.warning("Failed to fetch billing app names: %s", e)
        with _billing_names_lock:
            _billing_names_time = time.time() - BILLING_NAME_TTL + BILLING_NAME_RETRY_SECONDS
    finally:
        with _billing_names_lock:
            _billing_names_refreshing = False


def _billing_app_names(*, wait: bool) -> dict[str, str]:
    """app_id → name from billing, refreshed hourly.

    Lakebase serving must not wait on the warehouse, so callers pass wait=False
    there: the refresh runs in the background and names fill in on a later read.
    """
    global _billing_names_refreshing
    with _billing_names_lock:
        if _billing_names_time and time.time() - _billing_names_time < BILLING_NAME_TTL:
            return dict(_billing_names)
        start = not _billing_names_refreshing
        if start:
            _billing_names_refreshing = True
    if start:
        if wait:
            _refresh_billing_names()
        else:
            threading.Thread(target=_refresh_billing_names, daemon=True, name="apps-billing-names").start()
    with _billing_names_lock:
        return dict(_billing_names)


def _apply_billing_names(bundle: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """Replace app ids still shown as names, including in already-cached bundles."""
    if not names or not isinstance(bundle, dict):
        return bundle
    apps_block = bundle.get("apps")
    apps = apps_block.get("apps") if isinstance(apps_block, dict) else None
    artifacts = bundle.get("connected_artifacts")

    def _named(items: Any) -> Any:
        if not isinstance(items, list):
            return items
        out = []
        for item in items:
            app_id = item.get("app_id") if isinstance(item, dict) else None
            if app_id and item.get("app_name") == app_id and app_id in names:
                item = {**item, "app_name": names[app_id]}
            out.append(item)
        return out

    named_apps = _named(apps)
    named_artifacts = _named(artifacts)
    if named_apps is apps and named_artifacts is artifacts:
        return bundle
    result = {**bundle, "connected_artifacts": named_artifacts}
    if isinstance(apps_block, dict):
        result["apps"] = {**apps_block, "apps": named_apps}
    return result


# ── Connected artifacts cache ────────────────────────────────────────
_app_resources_cache: dict[str, list[dict[str, str]]] = {}
_app_resources_cache_time: float = 0
APP_RESOURCES_CACHE_TTL = 1800  # 30 minutes — SDK calls are expensive


def _fetch_one_app_resources(w: Any, app_name: str) -> tuple[str, list[dict[str, str]]]:
    """Fetch resources for a single app. Runs in a thread-pool worker."""
    try:
        app_detail = w.apps.get(app_name)
        resources = getattr(app_detail, "resources", None) or []
        app_resources: list[dict[str, str]] = []
        for r in resources:
            res_name = getattr(r, "name", None) or ""
            res_description = getattr(r, "description", None) or ""
            res_type = ""
            if getattr(r, "serving_endpoint", None):
                res_type = "SERVING_ENDPOINT"
                ep = r.serving_endpoint
                res_name = res_name or getattr(ep, "name", "") or getattr(ep, "endpoint_name", "") or ""
                res_description = res_description or getattr(ep, "permission", "") or ""
            elif getattr(r, "sql_warehouse", None):
                res_type = "SQL_WAREHOUSE"
                wh = r.sql_warehouse
                res_name = res_name or getattr(wh, "name", "") or getattr(wh, "id", "") or ""
                res_description = res_description or getattr(wh, "permission", "") or ""
            elif getattr(r, "secret", None):
                res_type = "SECRET"
                sec = r.secret
                res_name = res_name or getattr(sec, "key", "") or ""
                res_description = res_description or getattr(sec, "scope", "") or ""
            elif getattr(r, "job", None):
                res_type = "JOB"
                job = r.job
                res_name = res_name or getattr(job, "id", "") or ""
                res_description = res_description or getattr(job, "permission", "") or ""
            else:
                res_type = getattr(r, "type", None) or "UNKNOWN"
            app_resources.append({"name": res_name, "type": res_type, "description": res_description})

        sp_name = getattr(app_detail, "service_principal_name", None) or ""
        sp_id = getattr(app_detail, "service_principal_id", None)
        if not sp_name and sp_id:
            sp_name = str(sp_id)
        if sp_name:
            app_resources.append({"name": sp_name, "type": "SERVICE_PRINCIPAL", "description": "Run-as identity"})
        return app_name, app_resources
    except Exception as e:
        logger.debug("Failed to get resources for app %s: %s", app_name, e)
        return app_name, []


def _get_app_resources() -> dict[str, list[dict[str, str]]]:
    """Fetch connected artifacts/resources for each app via w.apps.get().

    Sequential fetch with per-app error isolation — the SDK client is not safe
    to share across concurrent threads, so we iterate serially with a 30s
    total budget enforced by the asyncio.wait_for() wrapper in the endpoint.

    Returns a dict keyed by app name with lists of resource dicts:
      {"cost-obs": [{"name": "sql-wh", "type": "SQL_WAREHOUSE", "description": "..."}]}
    """
    global _app_resources_cache, _app_resources_cache_time

    now = time.time()
    if _app_resources_cache and (now - _app_resources_cache_time) < APP_RESOURCES_CACHE_TTL:
        return _app_resources_cache

    registry = _get_app_registry()
    if not registry:
        return _app_resources_cache

    try:
        w = get_workspace_client()
        resources_by_app: dict[str, list[dict[str, str]]] = {}
        for entry in registry.values():
            app_name, app_res = _fetch_one_app_resources(w, entry["name"])
            resources_by_app[app_name] = app_res

        _app_resources_cache = resources_by_app
        _app_resources_cache_time = now
        logger.info("Refreshed app resources cache: %d apps", len(resources_by_app))
        return resources_by_app
    except Exception as e:
        logger.warning("Failed to fetch app resources: %s", e)
        return _app_resources_cache


def _resolve_app_name(app_id: str, registry: dict[str, dict[str, str]]) -> str:
    """Resolve a billing app_id (UUID) to a human-readable name."""
    entry = registry.get(app_id)
    if entry:
        return entry["name"]
    return app_id  # fall back to raw UUID


# ── SQL Queries ──────────────────────────────────────────────────────────

APPS_SUMMARY = """
WITH apps_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    u.usage_metadata,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.billing_origin_product = 'APPS'
),
apps_by_day AS (
  SELECT
    usage_date,
    workspace_id,
    COALESCE(usage_metadata.app_id, 'unknown') as app_id,
    SUM(usage_quantity) as daily_dbus,
    SUM(usage_quantity * price_per_dbu) as daily_spend
  FROM apps_usage
  GROUP BY usage_date, workspace_id, COALESCE(usage_metadata.app_id, 'unknown')
),
apps_totals AS (
  SELECT
    SUM(daily_dbus) as total_dbus,
    SUM(daily_spend) as total_spend,
    COUNT(DISTINCT workspace_id) as workspace_count,
    COUNT(DISTINCT app_id) as app_count,
    COUNT(DISTINCT usage_date) as days_in_range,
    MIN(usage_date) as first_date,
    MAX(usage_date) as last_date
  FROM apps_by_day
),
apps_avg AS (
  SELECT COALESCE(AVG(daily_apps), 0) as avg_daily_apps
  FROM (
    SELECT usage_date, COUNT(DISTINCT app_id) as daily_apps
    FROM apps_by_day
    GROUP BY usage_date
  ) t
)
SELECT t.*, a.avg_daily_apps
FROM apps_totals t, apps_avg a
"""

# Returns per-app breakdown with last_usage_date for active filtering.
# app_id here is the raw UUID from billing; names are resolved in Python.
APPS_BY_APP_FULL = """
WITH apps_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.usage_quantity,
    COALESCE(u.usage_metadata.app_id, 'Unknown') as app_id,
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
  app_id,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT workspace_id) as workspace_count,
  COUNT(DISTINCT usage_date) as days_active,
  MAX(usage_date) as last_usage_date
FROM apps_usage
GROUP BY app_id
ORDER BY total_spend DESC
"""

# Distinct workspaces per app (for workspace filtering) with name resolution
APPS_WORKSPACES = """
WITH apps_usage AS (
  SELECT DISTINCT
    COALESCE(u.usage_metadata.app_id, 'Unknown') as app_id,
    u.workspace_id
  FROM system.billing.usage u
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.billing_origin_product = 'APPS'
)
SELECT
  a.app_id,
  a.workspace_id,
  COALESCE(ws.workspace_name, CAST(a.workspace_id AS STRING)) as workspace_name
FROM apps_usage a
LEFT JOIN system.access.workspaces_latest ws ON a.workspace_id = ws.workspace_id
"""

# Service principals used as run_as identity for each app
APPS_SERVICE_PRINCIPALS = """
SELECT
  COALESCE(u.usage_metadata.app_id, 'Unknown') as app_id,
  u.identity_metadata.run_as as run_as
FROM system.billing.usage u
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.billing_origin_product = 'APPS'
  AND u.identity_metadata.run_as IS NOT NULL
GROUP BY 1, 2
"""

# Fallback: workspace IDs only (no name resolution)
APPS_WORKSPACES_FALLBACK = """
WITH apps_usage AS (
  SELECT DISTINCT
    COALESCE(u.usage_metadata.app_id, 'Unknown') as app_id,
    u.workspace_id
  FROM system.billing.usage u
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.billing_origin_product = 'APPS'
)
SELECT
  app_id,
  workspace_id,
  CAST(workspace_id AS STRING) as workspace_name
FROM apps_usage
"""


def _query_app_workspaces(params: dict[str, Any], ws_clause: str = "") -> list[dict[str, Any]]:
    """Query workspace info per app from Lakebase gold; live system tables only as fallback."""
    from backend.lakebase_read import PG_APPS_WORKSPACES, gold_read, warehouse_view_fallback
    rows = gold_read("apps_workspaces", PG_APPS_WORKSPACES, params, ws_clause)
    if rows:
        return rows
    if not warehouse_view_fallback():
        return []
    sql = wf.inject_ws_filter(APPS_WORKSPACES, ws_clause)
    fallback = wf.inject_ws_filter(APPS_WORKSPACES_FALLBACK, ws_clause)
    try:
        rows = execute_query(sql, params)
        if rows:
            sample = rows[0]
            logger.info("App workspace sample: workspace_id=%s, workspace_name=%s",
                        sample.get("workspace_id"), sample.get("workspace_name"))
        return rows
    except Exception as e:
        logger.warning("Could not query workspace names (system.access.workspaces_latest may not be accessible): %s", e)
        try:
            return execute_query(fallback, params)
        except Exception:
            return []


APPS_TIMESERIES = """
SELECT
  u.usage_date,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.billing_origin_product = 'APPS'
GROUP BY u.usage_date
ORDER BY u.usage_date
"""

# Per-app SKU breakdown — what cost categories make up each app's total
APPS_BY_APP_SKU = """
WITH apps_usage AS (
  SELECT
    COALESCE(u.usage_metadata.app_id, 'Unknown') as app_id,
    u.sku_name,
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
  app_id,
  sku_name,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend
FROM apps_usage
GROUP BY app_id, sku_name
ORDER BY app_id, total_spend DESC
"""


# ── MV fast paths — daily_apps_summary ───────────────────────────────────
# When the daily_apps_summary MV is available, these read from it directly
# instead of scanning the full system.billing.usage table. Output shapes
# match their raw counterparts so _process_apps and the frontend don't need
# to change. Fallback to raw on any exception.

MV_APPS_SUMMARY = """
WITH apps_by_day AS (
  SELECT usage_date, workspace_id, app_id,
    SUM(total_dbus) AS daily_dbus,
    SUM(total_spend) AS daily_spend
  FROM `{catalog}`.`{schema}`.`daily_apps_summary`
  WHERE usage_date BETWEEN :start_date AND :end_date
    {ws_filter}
  GROUP BY usage_date, workspace_id, app_id
),
apps_totals AS (
  SELECT
    SUM(daily_dbus) AS total_dbus,
    SUM(daily_spend) AS total_spend,
    COUNT(DISTINCT workspace_id) AS workspace_count,
    COUNT(DISTINCT app_id) AS app_count,
    COUNT(DISTINCT usage_date) AS days_in_range,
    MIN(usage_date) AS first_date,
    MAX(usage_date) AS last_date
  FROM apps_by_day
),
apps_avg AS (
  SELECT COALESCE(AVG(daily_apps), 0) AS avg_daily_apps
  FROM (
    SELECT usage_date, COUNT(DISTINCT app_id) AS daily_apps
    FROM apps_by_day
    GROUP BY usage_date
  ) t
)
SELECT t.*, a.avg_daily_apps
FROM apps_totals t, apps_avg a
"""

MV_APPS_BY_APP_FULL = """
SELECT
  app_id,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT workspace_id) AS workspace_count,
  COUNT(DISTINCT usage_date) AS days_active,
  MAX(usage_date) AS last_usage_date
FROM `{catalog}`.`{schema}`.`daily_apps_summary`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY app_id
ORDER BY total_spend DESC
"""

MV_APPS_TIMESERIES = """
SELECT
  usage_date,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_apps_summary`
WHERE usage_date BETWEEN :start_date AND :end_date
  {app_filter}
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

MV_APPS_BY_APP_SKU = """
SELECT
  app_id,
  sku_name,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_apps_summary`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY app_id, sku_name
ORDER BY app_id, total_spend DESC
"""

MV_APPS_FILTERED_AVG = """
SELECT COALESCE(AVG(daily_apps), 0) AS avg_daily_apps
FROM (
  SELECT usage_date, COUNT(DISTINCT app_id) AS daily_apps
  FROM `{catalog}`.`{schema}`.`daily_apps_summary`
  WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
  {app_filter}
  GROUP BY usage_date
) t
"""

MV_APPS_AVG_COST_PER_APP = """
SELECT COALESCE(AVG(daily_cost_per_app), 0) AS avg_cost_per_app
FROM (
  SELECT usage_date,
    SUM(total_spend) / NULLIF(COUNT(DISTINCT app_id), 0) AS daily_cost_per_app
  FROM `{catalog}`.`{schema}`.`daily_apps_summary`
  WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
  {app_filter}
  GROUP BY usage_date
) t
"""


def _process_apps(
    raw_apps: list[dict[str, Any]],
    active_only: bool,
    end_date_str: str,
    registry: dict[str, dict[str, str]],
    sku_rows: list[dict[str, Any]] | None = None,
    *,
    app_workspaces: dict[str, list[str]] | None = None,
    host_ws_id: str | None = None,
    host_in_scope: bool = True,
    billing_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Turn billing rows into app tiles.

    The Apps API registry only covers the workspace this app runs in (the host).
    So an app billed to another workspace is a live app there, not a deleted one;
    it gets a tile named from billing. Only an unregistered app billed solely to
    the host workspace is historical (deleted). Registry apps with no billing are
    shown only when the host workspace is in the selected scope.

    Returns a dict with keys: apps, inactive_summary, total_app_count,
    active_count, inactive_count, total_spend, unregistered_summary.
    """
    app_workspaces = app_workspaces or {}
    billing_names = billing_names or {}
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    active_cutoff = end_dt - timedelta(days=ACTIVE_DAYS)

    active_rows: list[dict[str, Any]] = []
    inactive_rows: list[dict[str, Any]] = []

    for r in raw_apps:
        last = r.get("last_usage_date")
        is_active = last is not None and last >= active_cutoff
        r["_is_active"] = is_active
        if is_active:
            active_rows.append(r)
        else:
            inactive_rows.append(r)

    source = active_rows if active_only else raw_apps
    total_spend_all = sum(float(r.get("total_spend") or 0) for r in source)

    def _elsewhere(app_id: str) -> bool:
        if not host_ws_id:
            return app_id in billing_names
        return any(ws != host_ws_id for ws in app_workspaces.get(app_id, []))

    def _tile(r: dict[str, Any], raw_id: str, **extra: Any) -> dict[str, Any]:
        spend = float(r.get("total_spend") or 0)
        return {
            "app_id": raw_id,
            "app_name": raw_id,
            "app_url": "",
            "total_dbus": float(r.get("total_dbus") or 0),
            "total_spend": spend,
            "workspace_count": r.get("workspace_count") or 0,
            "days_active": r.get("days_active") or 0,
            "last_usage_date": str(r.get("last_usage_date")) if r.get("last_usage_date") else None,
            "percentage": (spend / total_spend_all * 100) if total_spend_all > 0 else 0,
            "status": "active" if r.get("_is_active") else "inactive",
            "is_registered": False,
            **extra,
        }

    apps: list[dict[str, Any]] = []
    historical_rows: list[dict[str, Any]] = []
    for r in source:
        raw_id = r.get("app_id") or r.get("app_name") or "Unknown"
        if raw_id in registry:
            reg_entry = registry[raw_id]
            apps.append(_tile(
                r, raw_id,
                app_name=reg_entry.get("name", raw_id),
                app_url=reg_entry.get("url", ""),
                is_registered=True,
            ))
        elif _elsewhere(raw_id):
            apps.append(_tile(
                r, raw_id,
                app_name=billing_names.get(raw_id, raw_id),
                deployed_elsewhere=True,
            ))
        else:
            historical_rows.append(r)

    apps.sort(key=lambda a: a["total_spend"], reverse=True)

    if not active_only:
        apps_in_list = {a["app_id"] for a in apps}

        # Deployed in the host workspace but idle for the whole window.
        if host_in_scope:
            for uid, entry in registry.items():
                if uid in apps_in_list:
                    continue
                apps.append({
                    "app_id": uid,
                    "app_name": entry.get("name", uid),
                    "app_url": entry.get("url", ""),
                    "total_dbus": 0,
                    "total_spend": 0,
                    "workspace_count": 0,
                    "days_active": 0,
                    "last_usage_date": None,
                    "percentage": 0,
                    "is_registered": True,
                    "status": "inactive",
                })
                if host_ws_id:
                    app_workspaces.setdefault(uid, [host_ws_id])
                apps_in_list.add(uid)

        # Deleted apps that still have billing in the window.
        for r in historical_rows:
            raw_id = r.get("app_id") or r.get("app_name") or "Unknown"
            if raw_id in apps_in_list or r.get("_is_active"):
                continue
            apps.append(_tile(
                r, raw_id,
                app_name=billing_names.get(raw_id, raw_id),
                historical=True,
            ))
            apps_in_list.add(raw_id)

    unreg_spend = sum(float(r.get("total_spend") or 0) for r in historical_rows)
    unreg_dbus = sum(float(r.get("total_dbus") or 0) for r in historical_rows)

    inactive_spend = sum(float(r.get("total_spend") or 0) for r in inactive_rows)
    inactive_dbus = sum(float(r.get("total_dbus") or 0) for r in inactive_rows)

    registered_spend = sum(a["total_spend"] for a in apps)

    if sku_rows:
        sku_by_app: dict[str, list[dict[str, Any]]] = {}
        for row in sku_rows:
            aid = row.get("app_id") or "Unknown"
            sku_by_app.setdefault(aid, []).append({
                "sku_name": row.get("sku_name") or "Unknown",
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": float(row.get("total_spend") or 0),
            })
        for app in apps:
            breakdown = sku_by_app.get(app["app_id"], [])
            app_spend = app["total_spend"]
            for item in breakdown:
                item["percentage"] = (item["total_spend"] / app_spend * 100) if app_spend > 0 else 0
            app["sku_breakdown"] = breakdown

    for app in apps:
        app["workspace_names"] = list(app_workspaces.get(app["app_id"], []))

    live = [a for a in apps if not a.get("historical")]
    return {
        "apps": apps,
        "total_spend": registered_spend,
        "total_app_count": len(apps),
        "active_count": sum(1 for a in live if a.get("status") == "active"),
        "inactive_count": sum(1 for a in live if a.get("status") == "inactive"),
        "inactive_summary": {
            "count": len(inactive_rows),
            "total_spend": inactive_spend,
            "total_dbus": inactive_dbus,
            "percentage": (inactive_spend / (total_spend_all + inactive_spend) * 100)
            if (total_spend_all + inactive_spend) > 0
            else 0,
        },
        "unregistered_summary": {
            "count": len(historical_rows),
            "total_spend": unreg_spend,
            "total_dbus": unreg_dbus,
            "percentage": (unreg_spend / total_spend_all * 100) if total_spend_all > 0 else 0,
        },
    }


@router.get("/summary")
async def get_apps_summary(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get Databricks Apps cost summary."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(execute_query, APPS_SUMMARY, params)

    if not results:
        return {
            "total_dbus": 0,
            "total_spend": 0,
            "workspace_count": 0,
            "app_count": 0,
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
        "app_count": row.get("app_count") or 0,
        "avg_daily_apps": round(float(row.get("avg_daily_apps") or 0)),
        "days_in_range": days,
        "avg_daily_spend": total_spend / days if days > 0 else 0,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "first_date": str(row.get("first_date")) if row.get("first_date") else None,
        "last_date": str(row.get("last_date")) if row.get("last_date") else None,
    }


def _empty_bundle(params: dict, active_only: bool) -> dict[str, Any]:
    """Return a valid zero-data bundle — used when workspace filter finds no Apps rows."""
    return {
        "summary": {"total_dbus": 0, "total_spend": 0, "workspace_count": 0, "app_count": 0, "days_in_range": 1, "avg_daily_spend": 0, "avg_daily_apps": 0, "avg_cost_per_app": 0},
        "apps": {"apps": [], "total_spend": 0, "total_app_count": 0, "active_count": 0, "inactive_count": 0,
                 "inactive_summary": {"count": 0, "total_spend": 0, "total_dbus": 0, "percentage": 0},
                 "unregistered_summary": {"count": 0, "total_spend": 0, "total_dbus": 0, "percentage": 0}},
        "timeseries": {"timeseries": [], "categories": ["Total"]},
        "connected_artifacts": [],
        "workspaces": [],
        "active_only": active_only,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _apps_failure_payload(
    params: dict[str, Any],
    code: str = "APPS_BUNDLE_FAILED",
) -> dict[str, Any]:
    source_unsupported = code == "SOURCE_SCOPE_UNSUPPORTED"
    return {
        **_empty_bundle(params, False),
        "available": False,
        "reason": "shared_scope_unsupported" if source_unsupported else "producer_failed",
        "reason_detail": (
            "The selected shared source does not publish Apps cost detail."
            if source_unsupported
            else "Apps cost data is temporarily unavailable. Retry shortly."
        ),
        "error_code": code,
    }


def _compute_apps_bundle(params: dict, id_list: list | None, active_only: bool, dkey: str) -> None:
    """Background worker: run all Apps queries, build response, write to Delta cache."""
    import time as _time
    _endpoint = f"apps:dashboard-bundle:v2:{'active' if active_only else 'all'}"
    _start = _time.time()

    try:
        ws_clause = wf.build_ws_filter_clause(id_list=id_list)

        def _ws(sql: str) -> str:
            return wf.inject_ws_filter(sql, ws_clause)

        registry = _get_app_registry()  # TTL-cached (1h)
        host_ws_id = _host_workspace_id(registry)
        # The registry only lists the host workspace's apps, so it can't be used to
        # filter billing: that would drop every app deployed anywhere else.
        app_filter = ""
        scope_ids = id_list or wf.get_configured_workspace_ids() or None
        host_in_scope = not scope_ids or (host_ws_id is not None and host_ws_id in scope_ids)

        filtered_timeseries = f"""
        SELECT
          u.usage_date,
          SUM(u.usage_quantity) as total_dbus,
          SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
          ON u.sku_name = p.sku_name
          AND u.cloud = p.cloud
          AND p.price_end_time IS NULL
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.billing_origin_product = 'APPS'
          {app_filter}
          {ws_clause}
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """

        filtered_avg_apps_query = f"""
        SELECT COALESCE(AVG(daily_apps), 0) as avg_daily_apps
        FROM (
          SELECT usage_date, COUNT(DISTINCT u.usage_metadata.app_id) as daily_apps
          FROM system.billing.usage u
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.billing_origin_product = 'APPS'
            {app_filter}
            {ws_clause}
          GROUP BY usage_date
        ) t
        """

        # Mean-of-daily-ratios: matches the kpi-trend drilldown methodology exactly.
        avg_cost_per_app_query = f"""
        SELECT COALESCE(AVG(daily_cost_per_app), 0) as avg_cost_per_app
        FROM (
          SELECT u.usage_date,
            SUM(u.usage_quantity * COALESCE(p.pricing.default, 0))
              / NULLIF(COUNT(DISTINCT u.usage_metadata.app_id), 0) as daily_cost_per_app
          FROM system.billing.usage u
          LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
          WHERE u.usage_date BETWEEN :start_date AND :end_date
            AND u.usage_quantity > 0
            AND u.billing_origin_product = 'APPS'
            {app_filter}
            {ws_clause}
          GROUP BY u.usage_date
        ) t
        """

        # MV fast paths — daily_apps_summary avoids the raw system.billing.usage
        # scan for the four heavy slots when the MV is available. Each helper
        # falls back to its raw counterpart on any exception so a broken MV
        # never blocks a page load.
        from backend.lakebase_read import (
            PG_APPS_IDENTITIES,
            dual_read_mv,
            gold_read,
            warehouse_view_fallback,
        )
        live_ok = warehouse_view_fallback()
        # Lakebase-on viewers skip the warehouse DESCRIBE; dual_read still serves Postgres first.
        _use_mv = True if not live_ok else _check_mv_available()
        _mv_ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list) if _use_mv else ""
        # MV column is bare `app_id`; the raw filter uses `u.usage_metadata.app_id`
        _mv_app_filter = ""

        def _mv_query(name: str, mv_sql_tpl: str, raw_fallback: Any, **fmt_kwargs: str) -> list[dict[str, Any]]:
            """Postgres first. Warehouse gold only if Postgres misses."""
            if _use_mv:
                def _gold() -> list[dict[str, Any]]:
                    cat, sch = get_catalog_schema()
                    mv_sql = mv_sql_tpl.format(
                        catalog=cat, schema=sch,
                        ws_filter=_mv_ws_clause + source_label_filter_clause(mv_sql_tpl),
                        **fmt_kwargs,
                    )
                    mv_sql = apply_mv_overrides(mv_sql, cat, sch)
                    return execute_query(mv_sql, params)

                try:
                    return dual_read_mv(
                        mv_sql_tpl, params, _mv_ws_clause,
                        extra_format={"app_filter": fmt_kwargs.get("app_filter", "")},
                        fetch_delta=_gold,
                    )
                except Exception as e:
                    logger.warning("apps %s MV path failed (%s); falling back to raw scan", name, type(e).__name__)
            return raw_fallback() if live_ok else []

        def _gold_sps():
            live = _ws(APPS_SERVICE_PRINCIPALS) if warehouse_view_fallback() else None
            return gold_read("apps_identities", PG_APPS_IDENTITIES, params, ws_clause, live)

        queries = [
            ("summary", lambda: _mv_query("summary", MV_APPS_SUMMARY, lambda: execute_query(_ws(APPS_SUMMARY), params))),
            ("apps", lambda: _mv_query("apps", MV_APPS_BY_APP_FULL, lambda: execute_query(_ws(APPS_BY_APP_FULL), params))),
            ("timeseries", lambda: _mv_query("timeseries", MV_APPS_TIMESERIES, lambda: execute_query(filtered_timeseries, params), app_filter=_mv_app_filter)),
            ("filtered_avg_apps", lambda: _mv_query("filtered_avg_apps", MV_APPS_FILTERED_AVG, lambda: execute_query(filtered_avg_apps_query, params), app_filter=_mv_app_filter)),
            ("avg_cost_per_app", lambda: _mv_query("avg_cost_per_app", MV_APPS_AVG_COST_PER_APP, lambda: execute_query(avg_cost_per_app_query, params), app_filter=_mv_app_filter)),
            ("sku_breakdown", lambda: _mv_query("sku_breakdown", MV_APPS_BY_APP_SKU, lambda: execute_query(_ws(APPS_BY_APP_SKU), params))),
            ("workspaces", lambda: _query_app_workspaces(params, ws_clause)),
            ("service_principals", lambda: _gold_sps()),
            ("app_names", lambda: _billing_app_names(wait=live_ok)),
        ]

        results = execute_queries_parallel(queries, 75.0)

        # Build workspace lookup per app_id
        workspace_rows = results.get("workspaces", []) or []
        app_workspace_map: dict[str, list[str]] = {}
        all_workspaces: dict[str, str] = {}  # ws_id -> ws_name
        for row in workspace_rows:
            app_id = row.get("app_id", "")
            ws_name = str(row.get("workspace_name", ""))
            ws_id = str(row.get("workspace_id", ""))
            if app_id not in app_workspace_map:
                app_workspace_map[app_id] = []
            if ws_id not in app_workspace_map[app_id]:
                app_workspace_map[app_id].append(ws_id)
            all_workspaces[ws_id] = ws_name

        summary_data = results.get("summary", []) or []
        # Calendar days so the KPI subtitle matches the selected range regardless of billing table lag
        _start_dt = datetime.strptime(params["start_date"], "%Y-%m-%d")
        _end_dt = datetime.strptime(params["end_date"], "%Y-%m-%d")
        days_in_range = (_end_dt - _start_dt).days + 1
        avg_daily_apps = 0
        filtered_avg_data = results.get("filtered_avg_apps", []) or []
        if filtered_avg_data:
            avg_daily_apps = round(float(filtered_avg_data[0].get("avg_daily_apps") or 0))

        raw_apps = results.get("apps", []) or []
        sku_rows = results.get("sku_breakdown", []) or []
        billing_names = results.get("app_names") or {}
        apps_result = _process_apps(
            raw_apps, active_only, params["end_date"], registry, sku_rows,
            app_workspaces=app_workspace_map, host_ws_id=host_ws_id,
            host_in_scope=host_in_scope, billing_names=billing_names,
        )

        summary_row = summary_data[0] if summary_data else {}
        total_spend_all = float(summary_row.get("total_spend") or 0)
        total_dbus_all = float(summary_row.get("total_dbus") or 0)
        avg_daily_spend = total_spend_all / days_in_range if days_in_range > 0 else 0

        avg_cost_per_app_data = results.get("avg_cost_per_app", []) or []
        avg_cost_per_app = float(avg_cost_per_app_data[0].get("avg_cost_per_app") or 0) if avg_cost_per_app_data else 0

        summary = {
            "total_dbus": total_dbus_all,
            "total_spend": total_spend_all,
            "workspace_count": len(all_workspaces),
            "app_count": int(apps_result.get("total_app_count") or 0),
            "avg_daily_apps": avg_daily_apps,
            "days_in_range": days_in_range,
            "avg_daily_spend": avg_daily_spend,
            "avg_cost_per_app": avg_cost_per_app,
        }

        timeseries_data = results.get("timeseries", []) or []
        timeseries = sorted(
            [{"date": str(row.get("usage_date")), "Total": float(row.get("total_spend") or 0)} for row in timeseries_data],
            key=lambda x: x["date"],
        )

        # Use stale resources; refresh in background daemon thread
        resources_by_app = _app_resources_cache
        threading.Thread(target=_get_app_resources, daemon=True, name="apps-resources-bg").start()
        connected_artifacts: list[dict[str, Any]] = []
        for uid, entry in registry.items():
            app_name = entry.get("name", uid)
            for res in resources_by_app.get(app_name, []):
                connected_artifacts.append({
                    "app_id": uid,
                    "app_name": app_name,
                    "artifact_name": str(res.get("name", "")),
                    "artifact_type": str(res.get("type", "")),
                    "artifact_description": str(res.get("description", "")),
                })

        sp_rows = results.get("service_principals", []) or []
        seen_sp: set[tuple[str, str]] = set()
        for row in sp_rows:
            app_id = str(row.get("app_id", ""))
            run_as = str(row.get("run_as", ""))
            if not app_id or not run_as:
                continue
            key = (app_id, run_as)
            if key in seen_sp:
                continue
            seen_sp.add(key)
            app_name = registry.get(app_id, {}).get("name") or billing_names.get(app_id, app_id)
            connected_artifacts.append({
                "app_id": app_id,
                "app_name": app_name,
                "artifact_name": run_as,
                "artifact_type": "SERVICE_PRINCIPAL",
                "artifact_description": "Run-as identity",
            })

        _resp = {
            "summary": summary,
            "apps": apps_result,
            "timeseries": {"timeseries": timeseries, "categories": ["Total"]},
            "connected_artifacts": connected_artifacts,
            "workspaces": [{"id": ws_id, "name": ws_name} for ws_id, ws_name in sorted(all_workspaces.items(), key=lambda x: x[1])],
            "active_only": active_only,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
        # Guard against locking in a zero-value bundle for 30 min when a critical
        # query timed out (execute_queries_parallel sets that slot to None).
        # A partial-timeout would otherwise cache the empty response with full TTL
        # and users see $0 on the KPIs until the cache expires. Short-cache
        # (60s) so the next request retries against a hopefully-warm warehouse.
        _summary_missing = results.get("summary") is None
        _apps_missing = results.get("apps") is None
        _timeseries_missing = results.get("timeseries") is None
        _bundle_empty = (
            total_spend_all == 0
            and total_dbus_all == 0
            and not apps_result.get("apps")
        )
        _degraded = _summary_missing or _apps_missing or _timeseries_missing or _bundle_empty
        if _degraded:
            cache_ttl = 60
            logger.info(
                "apps dashboard-bundle degraded response — short-caching (60s): "
                "summary_missing=%s apps_missing=%s timeseries_missing=%s empty=%s",
                _summary_missing, _apps_missing, _timeseries_missing, _bundle_empty,
            )
        else:
            cache_ttl = 60 if not registry else (600 if id_list else 1800)
        try:
            delta_cache_put(dkey, _endpoint, _resp, ttl_seconds=cache_ttl)
        except Exception as _ce:
            logger.debug("Delta cache write failed for apps/dashboard-bundle: %s", _ce)
        logger.info(
            "apps dashboard-bundle background compute complete: %.1fs workspaces=%s apps=%d",
            _time.time() - _start, id_list or "all", apps_result.get("total_app_count", 0),
        )
    except Exception as e:
        logger.error("apps dashboard-bundle background compute failed: %s", e, exc_info=True)
        code = str(getattr(e, "code", "APPS_BUNDLE_FAILED"))
        try:
            delta_cache_put(
                dkey, _endpoint, _apps_failure_payload(params, code), ttl_seconds=60
            )
        except Exception:
            pass
    finally:
        with _apps_bundle_inflight_lock:
            _apps_bundle_inflight.discard(dkey)


@router.get("/dashboard-bundle")
async def get_apps_dashboard_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    active_only: bool = Query(default=False, description="Show only apps active in last 7 days"),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """Get all Apps dashboard data in a single request (submit-and-poll: 202 on cache miss)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    blocked = source_scope_unavailable(
        {"daily_apps_summary"},
        "The selected shared source does not publish Apps cost detail.",
    )
    if blocked:
        return {**_apps_failure_payload(params, "SOURCE_SCOPE_UNSUPPORTED"), **blocked, "active_only": active_only}
    _endpoint = f"apps:dashboard-bundle:v2:{'active' if active_only else 'all'}"
    _dkey = bundle_cache_key(_endpoint, params["start_date"], params["end_date"], id_list)

    if (_dcached := delta_cache_get(_dkey)) is not None:
        if isinstance(_dcached, dict) and "_error" in _dcached:
            raise HTTPException(status_code=500, detail=_dcached.get("_error", "Bundle compute failed"))
        return _apply_billing_names(_dcached, _billing_app_names(wait=False))

    with _apps_bundle_inflight_lock:
        if _dkey not in _apps_bundle_inflight:
            _apps_bundle_inflight.add(_dkey)
            threading.Thread(
                target=_compute_apps_bundle,
                args=(params, id_list, active_only, _dkey),
                daemon=True,
                name="apps-bundle-bg",
            ).start()
            logger.info("apps dashboard-bundle: started background compute for %s", _dkey)
        else:
            logger.debug("apps dashboard-bundle: already inflight for %s", _dkey)

    return JSONResponse(
        status_code=202,
        content={"status": "pending", "cache_key": _dkey},
        headers={"Retry-After": "2"},
    )


# ── KPI Trend ────────────────────────────────────────────────────────


@router.get("/kpi-trend")
async def get_apps_kpi_trend(
    kpi: str = Query(..., description="KPI: apps_spend, apps_dbus, apps_count"),
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    granularity: str = Query("daily", description="daily, weekly, monthly"),
) -> dict[str, Any]:
    """KPI trend over every app billed in scope, matching the dashboard KPIs."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    _dkey = bundle_cache_key(f"apps:kpi-trend:{kpi}:{granularity}", params["start_date"], params["end_date"], None)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached

    app_filter = ""

    from backend.lakebase_read import KPI_TREND_PG, gold_read, warehouse_view_fallback
    pg = KPI_TREND_PG.get(kpi)
    if pg:
        gold_rows = await asyncio.to_thread(gold_read, f"apps_kpi_trend_{kpi}", pg, params)
        if gold_rows or not warehouse_view_fallback():
            results = gold_rows or []
            daily_points = [{"date": str(r["date"])[:10], "value": float(r.get("value") or 0)} for r in results]
            if granularity == "weekly" and daily_points:
                buckets: dict[str, list[float]] = {}
                for dp in daily_points:
                    d = datetime.strptime(dp["date"], "%Y-%m-%d")
                    week_start = d - timedelta(days=d.weekday())
                    key = week_start.strftime("%Y-%m-%d")
                    buckets.setdefault(key, []).append(dp["value"])
                data_points = [{"date": k, "value": sum(v)} for k, v in sorted(buckets.items())]
            elif granularity == "monthly" and daily_points:
                buckets_m: dict[str, list[float]] = {}
                for dp in daily_points:
                    key = dp["date"][:7] + "-01"
                    buckets_m.setdefault(key, []).append(dp["value"])
                data_points = [{"date": k, "value": sum(v)} for k, v in sorted(buckets_m.items())]
            else:
                data_points = daily_points
            if not data_points:
                empty = {
                    "kpi": kpi, "granularity": granularity, "data_points": [],
                    "summary": {"period_start_value": 0, "period_end_value": 0,
                                 "change_amount": 0, "change_percent": 0,
                                 "min_value": 0, "max_value": 0, "avg_value": 0,
                                 "trend": "flat"},
                }
                delta_cache_put(_dkey, "apps:kpi-trend", empty, ttl_seconds=cache_ttls.TREND)
                return empty
            all_values = [dp["value"] for dp in data_points]
            start_val = all_values[0]
            end_val = all_values[-1]
            change = end_val - start_val
            change_pct = (change / start_val * 100) if start_val > 0 else 0
            trend = "flat" if abs(change_pct) < 5 else ("increasing" if change_pct > 0 else "decreasing")
            payload = {
                "kpi": kpi,
                "granularity": granularity,
                "data_points": data_points,
                "summary": {
                    "period_start_value": round(start_val, 2),
                    "period_end_value": round(end_val, 2),
                    "change_amount": round(change, 2),
                    "change_percent": round(change_pct, 2),
                    "min_value": round(min(all_values), 2),
                    "max_value": round(max(all_values), 2),
                    "avg_value": round(sum(all_values) / len(all_values), 2),
                    "trend": trend,
                },
            }
            delta_cache_put(_dkey, "apps:kpi-trend", payload, ttl_seconds=cache_ttls.TREND)
            return payload

    if kpi == "apps_spend":
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
        SELECT usage_date as date, SUM(usage_quantity * price_per_dbu) as value
        FROM usage_with_price
        GROUP BY usage_date
        ORDER BY usage_date
        """
    elif kpi == "apps_dbus":
        query = """
        SELECT u.usage_date as date, SUM(u.usage_quantity) as value
        FROM system.billing.usage u
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.billing_origin_product = 'APPS'
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
    elif kpi == "apps_count":
        query = f"""
        SELECT u.usage_date as date,
               COUNT(DISTINCT u.usage_metadata.app_id) as value
        FROM system.billing.usage u
        WHERE u.usage_date BETWEEN :start_date AND :end_date
          AND u.usage_quantity > 0
          AND u.billing_origin_product = 'APPS'
          {app_filter}
        GROUP BY u.usage_date
        ORDER BY u.usage_date
        """
    elif kpi == "apps_avg_cost_per_app":
        query = f"""
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
          {app_filter}
        GROUP BY usage_date
        ORDER BY usage_date
        """
    else:
        return {"error": f"Unknown KPI: {kpi}"}

    if not warehouse_view_fallback():
        return {
            "kpi": kpi, "granularity": granularity, "data_points": [],
            "summary": {"period_start_value": 0, "period_end_value": 0,
                         "change_amount": 0, "change_percent": 0,
                         "min_value": 0, "max_value": 0, "avg_value": 0,
                         "trend": "flat"},
        }

    try:
        results = await asyncio.to_thread(execute_query, query, params)
    except Exception as e:
        logger.error("Apps KPI trend query failed for %s: %s", kpi, e)
        return {
            "kpi": kpi, "granularity": granularity, "data_points": [],
            "summary": {"period_start_value": 0, "period_end_value": 0,
                         "change_amount": 0, "change_percent": 0,
                         "min_value": 0, "max_value": 0, "avg_value": 0,
                         "trend": "flat"},
        }

    daily_points = [{"date": str(r["date"]), "value": float(r["value"] or 0)} for r in results]

    # Aggregate into weekly/monthly buckets
    if granularity == "weekly" and daily_points:
        buckets: dict[str, list[float]] = {}
        for dp in daily_points:
            d = datetime.strptime(dp["date"], "%Y-%m-%d")
            week_start = d - timedelta(days=d.weekday())
            key = week_start.strftime("%Y-%m-%d")
            buckets.setdefault(key, []).append(dp["value"])
        data_points = [{"date": k, "value": sum(v)} for k, v in sorted(buckets.items())]
    elif granularity == "monthly" and daily_points:
        buckets_m: dict[str, list[float]] = {}
        for dp in daily_points:
            key = dp["date"][:7] + "-01"
            buckets_m.setdefault(key, []).append(dp["value"])
        data_points = [{"date": k, "value": sum(v)} for k, v in sorted(buckets_m.items())]
    else:
        data_points = daily_points

    if not data_points:
        return {
            "kpi": kpi, "granularity": granularity, "data_points": [],
            "summary": {"period_start_value": 0, "period_end_value": 0,
                         "change_amount": 0, "change_percent": 0,
                         "min_value": 0, "max_value": 0, "avg_value": 0,
                         "trend": "flat"},
        }

    all_values = [dp["value"] for dp in data_points]
    start_val = all_values[0]
    end_val = all_values[-1]
    change = end_val - start_val
    change_pct = (change / start_val * 100) if start_val > 0 else 0
    trend = "flat" if abs(change_pct) < 5 else ("increasing" if change_pct > 0 else "decreasing")

    _resp = {
        "kpi": kpi,
        "granularity": granularity,
        "data_points": data_points,
        "summary": {
            "period_start_value": round(start_val, 2),
            "period_end_value": round(end_val, 2),
            "change_amount": round(change, 2),
            "change_percent": round(change_pct, 2),
            "min_value": round(min(all_values), 2),
            "max_value": round(max(all_values), 2),
            "avg_value": round(sum(all_values) / len(all_values), 2),
            "trend": trend,
        },
    }
    delta_cache_put(_dkey, "apps:kpi-trend", _resp, ttl_seconds=cache_ttls.TREND)
    return _resp


# ── Thumbnail proxy ──────────────────────────────────────────────────

# Cache thumbnails in memory to avoid repeated HTTP calls
_thumbnail_cache: dict[str, bytes | None] = {}
_thumbnail_cache_time: dict[str, float] = {}
THUMBNAIL_CACHE_TTL = 600  # 10 minutes

# Paths to try for app thumbnails (order matters)
_THUMBNAIL_PATHS = [
    "/static/thumbnail.png",
    "/static/dbfavicon.png",
    "/favicon.ico",
]


@router.get("/thumbnail")
async def get_app_thumbnail(
    app_id: str = Query(..., description="App UUID"),
) -> Response:
    """Proxy an app's thumbnail image to avoid CORS/auth issues."""
    import os

    now = time.time()

    # Check cache
    if app_id in _thumbnail_cache:
        cached_time = _thumbnail_cache_time.get(app_id, 0)
        if (now - cached_time) < THUMBNAIL_CACHE_TTL:
            data = _thumbnail_cache[app_id]
            if data:
                content_type = "image/png" if not data[:4] == b"\x00\x00\x01\x00" else "image/x-icon"
                return Response(content=data, media_type=content_type)
            return Response(status_code=404)

    registry = _get_app_registry()
    entry = registry.get(app_id)
    if not entry or not entry.get("url"):
        _thumbnail_cache[app_id] = None
        _thumbnail_cache_time[app_id] = now
        return Response(status_code=404)

    app_url = entry["url"].rstrip("/")

    # Build multiple auth approaches to try
    auth_headers_list: list[dict[str, str]] = []

    # 1) No auth (some apps serve static files publicly)
    auth_headers_list.append({})

    # 2) Workspace token auth
    try:
        w = get_workspace_client()
        token = getattr(w.config, "token", None)
        if token:
            auth_headers_list.append({"Authorization": f"Bearer {token}"})
    except Exception:
        pass

    # 3) App-level token from environment (if available)
    app_token = os.environ.get("DATABRICKS_TOKEN")
    if app_token:
        auth_headers_list.append({"Authorization": f"Bearer {app_token}"})

    # Try each thumbnail path with each auth approach
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        for path in _THUMBNAIL_PATHS:
            for headers in auth_headers_list:
                try:
                    resp = await client.get(f"{app_url}{path}", headers=headers)
                    if resp.status_code == 200 and len(resp.content) > 100:
                        ct = resp.headers.get("content-type", "")
                        if "image" in ct or "icon" in ct or path.endswith((".png", ".ico")):
                            _thumbnail_cache[app_id] = resp.content
                            _thumbnail_cache_time[app_id] = now
                            media = ct if "image" in ct else "image/png"
                            logger.info("Thumbnail found for app %s at %s%s", entry.get("name"), app_url, path)
                            return Response(content=resp.content, media_type=media)
                except Exception as e:
                    logger.debug("Thumbnail fetch failed for %s%s: %s", app_url, path, e)
                    continue

    logger.info("No thumbnail found for app %s (%s)", entry.get("name"), app_url)
    _thumbnail_cache[app_id] = None
    _thumbnail_cache_time[app_id] = now
    return Response(status_code=404)


# ── Connected artifacts ──────────────────────────────────────────────

@router.get("/connected-artifacts")
async def get_connected_artifacts() -> dict[str, Any]:
    """Get connected artifacts (serving endpoints, warehouses, etc.) for all apps."""
    registry = _get_app_registry()
    resources_by_app = _get_app_resources()

    artifacts: list[dict[str, Any]] = []
    for uid, entry in registry.items():
        app_name = entry["name"]
        app_resources = resources_by_app.get(app_name, [])
        for res in app_resources:
            artifacts.append({
                "app_id": uid,
                "app_name": app_name,
                "artifact_name": res["name"],
                "artifact_type": res["type"],
                "artifact_description": res["description"],
            })

    return {
        "artifacts": artifacts,
        "count": len(artifacts),
    }
