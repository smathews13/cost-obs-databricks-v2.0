"""Users & Groups — spend attribution by user and weekly report management."""

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from backend import workspace_filter as wf
from backend.db import (
    bundle_cache_key,
    delta_cache_get,
    delta_cache_put,
    execute_queries_parallel,
    get_local_source_label,
    get_workspace_client,
    selected_source_labels,
)
from backend.email_service import send_alert_email
from backend.routers.user import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

SETTINGS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".settings")
USER_REPORTS_FILE = os.path.join(SETTINGS_DIR, "user_reports.json")


def _users_failure_payload(
    start_date: str,
    end_date: str,
    error_code: str = "USERS_BUNDLE_FAILED",
) -> dict[str, Any]:
    return {
        "available": False,
        "reason": "producer_failed",
        "reason_detail": "User summary data is temporarily unavailable. Retry shortly.",
        "error_code": error_code,
        "summary": {},
        "top_users": [],
        "timeseries": [],
        "timeseries_users": [],
        "by_workspace": [],
        "user_growth": [],
        "start_date": start_date,
        "end_date": end_date,
    }


def _identity_scope(
    labels: list[str],
    workspace_ids: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Describe which identity detail is safe for the selected source scope."""
    del workspace_ids
    if not labels:
        return True, {"available": True}
    local_label = get_local_source_label()
    if local_label not in labels:
        return False, {
            "available": False,
            "reason": "identity_detail_unavailable_for_shared_sources",
            "reason_detail": (
                "User and group identities are not included in shared cost sources."
            ),
        }
    shared_labels = [label for label in labels if label != local_label]
    if shared_labels:
        return True, {
            "available": True,
            "reason": "shared_identity_detail_omitted",
            "reason_detail": (
                "Identity detail includes only the selected local source; shared "
                "sources do not expose user or group identities."
            ),
        }
    return True, {"available": True}

# ── SQL Queries ───────────────────────────────────────────────────────────────

USERS_SUMMARY = """
WITH usage_with_price AS (
  SELECT
    u.identity_metadata.run_as AS user_email,
    u.workspace_id,
    u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend,
    u.usage_quantity AS dbus
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
)
SELECT
  COUNT(DISTINCT user_email)   AS user_count,
  COUNT(DISTINCT workspace_id) AS workspace_count,
  SUM(spend)                   AS total_spend,
  SUM(dbus)                    AS total_dbus,
  SUM(spend) / NULLIF(COUNT(DISTINCT user_email), 0) AS avg_spend_per_user
FROM usage_with_price
"""

USERS_TOP_SPEND = """
WITH usage_with_price AS (
  SELECT
    u.identity_metadata.run_as AS user_email,
    u.billing_origin_product   AS product,
    u.usage_date,
    u.workspace_id,
    u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend,
    u.usage_quantity                                   AS dbus
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
)
SELECT
  user_email,
  SUM(spend)                   AS total_spend,
  SUM(dbus)                    AS total_dbus,
  COUNT(DISTINCT usage_date)   AS active_days,
  COUNT(DISTINCT workspace_id) AS workspace_count
FROM usage_with_price
GROUP BY user_email
ORDER BY total_spend DESC
"""

USERS_PRODUCT_BREAKDOWN = """
WITH usage_with_price AS (
  SELECT
    u.identity_metadata.run_as AS user_email,
    CASE
      WHEN u.billing_origin_product = 'SQL' THEN 'SQL'
      WHEN u.billing_origin_product = 'DLT' OR u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'ETL - Streaming'
      WHEN u.billing_origin_product = 'JOBS' THEN 'ETL - Batch'
      WHEN u.sku_name LIKE '%ALL_PURPOSE%' THEN 'Interactive'
      WHEN u.billing_origin_product IN ('SERVING', 'MODEL_SERVING')
           OR u.sku_name LIKE '%SERVING%' OR u.sku_name LIKE '%INFERENCE%'
           OR u.sku_name LIKE '%PROVISIONED_THROUGHPUT%' THEN 'Model Serving'
      WHEN u.sku_name LIKE '%VECTOR_SEARCH%' THEN 'AI Search'
      WHEN u.sku_name LIKE '%FOUNDATION_MODEL%' OR u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine-Tuning'
      WHEN u.sku_name LIKE '%AI_BI%' OR u.sku_name LIKE '%AI_QUERY%'
           OR u.sku_name LIKE '%AI_FUNCTIONS%' THEN 'AI Functions'
      WHEN u.sku_name LIKE '%SERVERLESS%'
           AND u.billing_origin_product NOT IN ('SQL', 'JOBS', 'DLT') THEN 'Serverless'
      ELSE 'Other'
    END AS product_category,
    u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
)
SELECT
  user_email,
  product_category,
  SUM(spend) AS spend
FROM usage_with_price
GROUP BY user_email, product_category
ORDER BY spend DESC
LIMIT 5000
"""

USERS_TIMESERIES = """
WITH top_users AS (
  SELECT u.identity_metadata.run_as AS user_email,
         SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
  GROUP BY u.identity_metadata.run_as
  ORDER BY total_spend DESC
  LIMIT 6
),
daily AS (
  SELECT
    u.usage_date,
    u.identity_metadata.run_as AS user_email,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS daily_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  INNER JOIN top_users tu ON u.identity_metadata.run_as = tu.user_email
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
  GROUP BY u.usage_date, u.identity_metadata.run_as
)
SELECT usage_date AS date, user_email, daily_spend
FROM daily
ORDER BY date, daily_spend DESC
"""

USERS_BY_WORKSPACE = """
WITH usage_with_price AS (
  SELECT
    u.workspace_id,
    u.identity_metadata.run_as AS user_email,
    u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
)
SELECT
  workspace_id,
  COUNT(DISTINCT user_email) AS user_count,
  SUM(spend)                 AS total_spend
FROM usage_with_price
GROUP BY workspace_id
ORDER BY total_spend DESC
LIMIT 20
"""


USERS_BY_WORKSPACE_DETAIL = """
WITH usage_with_price AS (
  SELECT
    u.workspace_id,
    u.identity_metadata.run_as AS user_email,
    CASE
      WHEN u.billing_origin_product = 'DLT' OR u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'ETL - Streaming'
      WHEN u.billing_origin_product = 'JOBS' THEN 'ETL - Batch'
      WHEN u.sku_name LIKE '%ALL_PURPOSE%' THEN 'Interactive'
      WHEN u.billing_origin_product IN ('SERVING', 'MODEL_SERVING')
           OR u.sku_name LIKE '%SERVING%' THEN 'Model Serving'
      WHEN u.billing_origin_product = 'SQL' THEN 'SQL'
      ELSE 'Other'
    END AS product_category,
    u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
)
SELECT
  workspace_id,
  user_email,
  product_category,
  SUM(spend) AS spend
FROM usage_with_price
GROUP BY workspace_id, user_email, product_category
ORDER BY spend DESC
LIMIT 2000
"""


USERS_SPEND_GROWTH = """
WITH usage_with_price AS (
  SELECT
    u.usage_date,
    u.usage_quantity * COALESCE(p.pricing.default, 0) AS spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
)
SELECT
  CASE WHEN usage_date <= :mid_date THEN 'first_half' ELSE 'second_half' END AS period,
  SUM(spend) AS total_spend
FROM usage_with_price
GROUP BY CASE WHEN usage_date <= :mid_date THEN 'first_half' ELSE 'second_half' END
"""

USERS_GROWTH = """
WITH base AS (
  SELECT
    date_trunc('month', usage_date) AS month,
    identity_metadata.run_as AS user_id
  FROM system.billing.usage
  WHERE usage_date BETWEEN :start_date AND :end_date
    AND usage_quantity > 0
    AND identity_metadata.run_as IS NOT NULL
),
user_first AS (
  SELECT user_id, MIN(month) AS first_month FROM base GROUP BY user_id
)
SELECT
  date_format(b.month, 'yyyy-MM') AS month,
  COUNT(DISTINCT b.user_id) AS active_users,
  COUNT(DISTINCT CASE WHEN b.month = f.first_month THEN b.user_id END) AS new_users
FROM base b
JOIN user_first f ON b.user_id = f.user_id
GROUP BY b.month
ORDER BY month
"""

_SCIM_TIMEOUT = 12.0  # seconds per HTTP request


def _scim_get_all_pages(host: str, path: str, auth_header: str, params: dict) -> list[dict]:
    """Paginate through a SCIM endpoint and return all resources."""
    results = []
    start_index = 1
    while True:
        resp = httpx.get(
            f"{host}{path}",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params={**params, "startIndex": start_index},
            timeout=_SCIM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        resources = data.get("Resources") or []
        results.extend(resources)
        total = int(data.get("totalResults") or 0)
        if not resources or len(results) >= total:
            break
        start_index += len(resources)
    return results


def _fetch_workspace_groups(user_token: str | None = None) -> dict[str, list[str]]:
    """Return {user_email: [group_names]} via direct SCIM HTTP calls with a hard timeout.

    Uses httpx instead of the SDK to avoid unbounded retry/backoff that causes
    the endpoint to hang on rate-limited workspaces (especially Azure AD-backed).

    Auth priority:
      1. user_token (X-Forwarded-Access-Token from Databricks Apps) — the
         logged-in user is typically a workspace admin with SCIM read access.
      2. SP token from WorkspaceClient (w.config.authenticate()) — works when
         the app's service principal has been granted SCIM read permissions.
    """
    try:
        w = get_workspace_client()
        host = w.config.host.rstrip("/")

        # Resolve auth header
        if user_token:
            auth_header = f"Bearer {user_token}"
            logger.info("Group mapping: using forwarded user token for SCIM calls")
        else:
            sp_headers = w.config.authenticate()
            auth_header = sp_headers.get("Authorization", "")
            logger.info("Group mapping: using service principal token for SCIM calls")

        # ── Primary: user-side lookup (one SCIM call per page) ────────────
        user_groups: dict[str, list[str]] = {}
        try:
            users = _scim_get_all_pages(
                host, "/api/2.0/preview/scim/v2/Users", auth_header,
                {"attributes": "id,userName,groups", "count": 200},
            )
            logger.info(f"Group mapping: loaded {len(users)} users from workspace")
            for u in users:
                email = u.get("userName") or ""
                if not email:
                    continue
                for g in (u.get("groups") or []):
                    gname = g.get("display") or ""
                    if gname:
                        user_groups.setdefault(email, []).append(gname)
            if user_groups:
                logger.info(f"Group mapping (user-side): {len(user_groups)} users with groups")
                return user_groups
            logger.info("Group mapping: user-side returned 0 users with groups, trying group-side fallback")
        except Exception as e:
            logger.warning(f"Group mapping: user-side lookup failed: {e}")

        # ── Fallback: group-side lookup ────────────────────────────────────
        id_to_email: dict[str, str] = {}
        try:
            for u in _scim_get_all_pages(
                host, "/api/2.0/preview/scim/v2/Users", auth_header,
                {"attributes": "id,userName", "count": 200},
            ):
                uid = str(u.get("id") or "")
                email = u.get("userName") or ""
                if uid and email:
                    id_to_email[uid] = email
        except Exception as e:
            logger.warning(f"Could not fetch user list for group mapping: {e}")

        groups = []
        try:
            groups = _scim_get_all_pages(
                host, "/api/2.0/preview/scim/v2/Groups", auth_header,
                {"attributes": "id,displayName,members", "count": 100},
            )
            logger.info(f"Group mapping (group-side): found {len(groups)} groups")
        except Exception as e:
            logger.error(f"Group mapping: failed to list groups: {e}", exc_info=True)
            return {}

        group_count = 0
        member_resolved = 0
        member_unresolved = 0
        for group in groups:
            gname = group.get("displayName") or ""
            if not gname:
                continue
            members = group.get("members") or []
            if not members:
                continue
            group_count += 1
            for member in members:
                ref = member.get("$ref") or ""
                if ref and "/Groups/" in ref:
                    continue  # skip nested groups
                display = member.get("display") or ""
                if "@" in display:
                    user_groups.setdefault(display, []).append(gname)
                    member_resolved += 1
                    continue
                member_id = str(member.get("value") or "")
                if member_id:
                    email = id_to_email.get(member_id) or ""
                    if email:
                        user_groups.setdefault(email, []).append(gname)
                        member_resolved += 1
                        continue
                member_unresolved += 1

        logger.info(
            f"Group mapping (group-side): {group_count} groups, "
            f"{member_resolved} resolved, {member_unresolved} unresolved "
            f"→ {len(user_groups)} users with groups"
        )
        return user_groups
    except Exception as e:
        logger.warning(f"Could not fetch workspace groups: {e}", exc_info=True)
        return {}


# ── Report config helpers ─────────────────────────────────────────────────────

def _load_report_config() -> dict:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    if os.path.exists(USER_REPORTS_FILE):
        try:
            with open(USER_REPORTS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"weekly_reports": [], "user_alerts": []}


def _save_report_config(config: dict) -> None:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(USER_REPORTS_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── Pydantic models ───────────────────────────────────────────────────────────

class WeeklyReportConfig(BaseModel):
    email: str
    name: str | None = None
    send_day: str = "monday"  # monday–sunday
    enabled: bool = True


class UserAlertConfig(BaseModel):
    email: str
    name: str | None = None
    threshold_amount: float | None = None   # alert when user spend > X in period
    spike_percent: float | None = None      # alert when user spend spikes > X%
    enabled: bool = True


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/bundle")
async def get_users_groups_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """Get all user/group spend data in a single parallel request."""
    if not end_date:
        end_date = (date.today() - timedelta(days=1)).isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    params = {"start_date": start_date, "end_date": end_date}
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    allowed, scope = _identity_scope(selected_source_labels(), id_list)
    if not allowed:
        return {**_users_failure_payload(start_date, end_date), **scope}
    _dkey = bundle_cache_key("users:dashboard-bundle", start_date, end_date, id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    def _ws(sql: str) -> str:
        return wf.inject_ws_filter(sql, ws_clause)

    # Compute mid-point for spend growth comparison
    from datetime import datetime as _dt
    start_dt = _dt.strptime(start_date, "%Y-%m-%d").date()
    end_dt = _dt.strptime(end_date, "%Y-%m-%d").date()
    mid_dt = start_dt + (end_dt - start_dt) / 2
    growth_params = {**params, "mid_date": mid_dt.isoformat()}

    growth_date_params = {
        "start_date": (date.today() - timedelta(days=182)).isoformat(),
        "end_date": (date.today() - timedelta(days=1)).isoformat(),
    }

    from backend.lakebase_read import (
        PG_USERS_BY_WORKSPACE,
        PG_USERS_GROWTH,
        PG_USERS_PRODUCT,
        PG_USERS_SPEND_GROWTH,
        PG_USERS_SUMMARY,
        PG_USERS_TIMESERIES,
        PG_USERS_TOP,
        gold_read,
    )
    queries = [
        ("summary", lambda: gold_read("users_summary", PG_USERS_SUMMARY, params, ws_clause, _ws(USERS_SUMMARY))),
        ("top_users", lambda: gold_read("users_top", PG_USERS_TOP, params, ws_clause, _ws(USERS_TOP_SPEND))),
        ("product_breakdown", lambda: gold_read("users_product", PG_USERS_PRODUCT, params, ws_clause, _ws(USERS_PRODUCT_BREAKDOWN))),
        ("timeseries", lambda: gold_read("users_timeseries", PG_USERS_TIMESERIES, params, ws_clause, _ws(USERS_TIMESERIES))),
        ("by_workspace", lambda: gold_read("users_by_workspace", PG_USERS_BY_WORKSPACE, params, ws_clause, _ws(USERS_BY_WORKSPACE))),
        ("spend_growth", lambda: gold_read("users_spend_growth", PG_USERS_SPEND_GROWTH, growth_params, ws_clause, _ws(USERS_SPEND_GROWTH))),
        ("user_growth", lambda: gold_read("users_growth", PG_USERS_GROWTH, growth_date_params, "", USERS_GROWTH)),
    ]
    try:
        results = await asyncio.to_thread(execute_queries_parallel, queries, timeout=90.0)
    except Exception as e:
        logger.error("users dashboard-bundle failed: %s", e)
        return {**_users_failure_payload(start_date, end_date, str(getattr(e, "code", "USERS_BUNDLE_FAILED"))), "error": str(e)}

    # Summary
    summary_rows = results.get("summary") or []
    summary = {}
    if summary_rows:
        r = summary_rows[0]
        summary = {
            "user_count": int(r.get("user_count") or 0),
            "workspace_count": int(r.get("workspace_count") or 0),
            "total_spend": float(r.get("total_spend") or 0),
            "total_dbus": float(r.get("total_dbus") or 0),
            "avg_spend_per_user": float(r.get("avg_spend_per_user") or 0),
        }

    # Spend growth: compare first half vs second half of date range
    growth_rows = {r.get("period"): float(r.get("total_spend") or 0) for r in (results.get("spend_growth") or [])}
    first_half = growth_rows.get("first_half", 0)
    second_half = growth_rows.get("second_half", 0)
    if first_half > 0:
        summary["spend_growth_pct"] = round((second_half - first_half) / first_half * 100, 1)
    else:
        summary["spend_growth_pct"] = None

    # Top users
    top_users_rows = results.get("top_users") or []
    total_spend = summary.get("total_spend") or 1
    top_users = [
        {
            "user_email": r.get("user_email"),
            "total_spend": float(r.get("total_spend") or 0),
            "total_dbus": float(r.get("total_dbus") or 0),
            "active_days": int(r.get("active_days") or 0),
            "workspace_count": int(r.get("workspace_count") or 0),
            "percentage": float(r.get("total_spend") or 0) / total_spend * 100,
        }
        for r in top_users_rows
    ]

    # Product breakdown per user → dict[user_email, list[{product, spend}]]
    prod_rows = results.get("product_breakdown") or []
    user_products: dict[str, list] = {}
    for r in prod_rows:
        email = r.get("user_email")
        if email:
            user_products.setdefault(email, []).append({
                "product": r.get("product_category") or "Other",
                "spend": float(r.get("spend") or 0),
            })

    # Attach product breakdown to top users
    for u in top_users:
        u["products"] = user_products.get(u["user_email"], [])
        # Primary product = highest spend category
        if u["products"]:
            u["primary_product"] = max(u["products"], key=lambda x: x["spend"])["product"]
        else:
            u["primary_product"] = "Unknown"

    # Timeseries — pivot to {date, user1_spend, user2_spend, ...}
    ts_rows = results.get("timeseries") or []
    ts_by_date: dict[str, dict] = {}
    ts_users: set[str] = set()
    for r in ts_rows:
        d = str(r.get("date"))
        user = r.get("user_email", "")
        spend = float(r.get("daily_spend") or 0)
        ts_users.add(user)
        if d not in ts_by_date:
            ts_by_date[d] = {"date": d}
        ts_by_date[d][user] = spend
    timeseries = sorted(ts_by_date.values(), key=lambda x: x["date"])

    # Workspace breakdown
    ws_rows = results.get("by_workspace") or []
    by_workspace = [
        {
            "workspace_id": r.get("workspace_id"),
            "user_count": int(r.get("user_count") or 0),
            "total_spend": float(r.get("total_spend") or 0),
        }
        for r in ws_rows
    ]

    growth_rows = results.get("user_growth") or []
    user_growth = [
        {
            "month": r.get("month"),
            "active_users": int(r.get("active_users") or 0),
            "new_users": int(r.get("new_users") or 0),
        }
        for r in growth_rows
    ]

    _resp = {
        "summary": summary,
        "top_users": top_users,
        "timeseries": timeseries,
        "timeseries_users": sorted(list(ts_users)),
        "by_workspace": by_workspace,
        "user_growth": user_growth,
        "start_date": start_date,
        "end_date": end_date,
        **scope,
    }
    delta_cache_put(_dkey, "users:dashboard-bundle", _resp, ttl_seconds=600 if id_list else 1800)
    return _resp


@router.get("/user-growth")
async def get_user_growth() -> dict[str, Any]:
    """Monthly active users and new users trend — always last 6 months, ignores date filter."""
    end_date = (date.today() - timedelta(days=1)).isoformat()
    start_date = (date.today() - timedelta(days=182)).isoformat()

    params = {"start_date": start_date, "end_date": end_date}
    from backend.lakebase_read import PG_USERS_GROWTH, gold_read
    rows = await asyncio.to_thread(gold_read, "users_growth", PG_USERS_GROWTH, params, "", USERS_GROWTH)
    return {
        "data": [
            {
                "month": r.get("month"),
                "active_users": int(r.get("active_users") or 0),
                "new_users": int(r.get("new_users") or 0),
            }
            for r in (rows or [])
        ]
    }


@router.get("/debug-groups")
async def debug_groups(request: Request) -> dict[str, Any]:
    """Debug endpoint: tests SCIM connectivity and returns raw group data."""
    user_token = request.headers.get("X-Forwarded-Access-Token")
    forwarded_email = request.headers.get("X-Forwarded-Email")
    forwarded_user = request.headers.get("X-Forwarded-User")
    try:
        w = get_workspace_client()
        host = w.config.host.rstrip("/")
        if user_token:
            auth_header = f"Bearer {user_token}"
            auth_method = "user_token"
        else:
            sp_headers = w.config.authenticate()
            auth_header = sp_headers.get("Authorization", "")
            auth_method = "service_principal"

        users_resp = httpx.get(
            f"{host}/api/2.0/preview/scim/v2/Users",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params={"attributes": "id,userName,groups", "count": 5},
            timeout=_SCIM_TIMEOUT,
        )
        users_data = users_resp.json() if users_resp.status_code == 200 else {"error": users_resp.text}

        groups_resp = httpx.get(
            f"{host}/api/2.0/preview/scim/v2/Groups",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params={"attributes": "id,displayName,members", "count": 5},
            timeout=_SCIM_TIMEOUT,
        )
        groups_data = groups_resp.json() if groups_resp.status_code == 200 else {"error": groups_resp.text}

        return {
            "auth_method": auth_method,
            "forwarded_headers_present": {
                "X-Forwarded-Access-Token": bool(user_token),
                "X-Forwarded-Email": bool(forwarded_email),
                "X-Forwarded-User": bool(forwarded_user),
            },
            "users_scim_status": users_resp.status_code,
            "users_total": users_data.get("totalResults"),
            "users_sample": [
                {"id": u.get("id"), "userName": u.get("userName"), "groups": [g.get("display") for g in (u.get("groups") or [])]}
                for u in (users_data.get("Resources") or [])[:5]
            ],
            "groups_scim_status": groups_resp.status_code,
            "groups_total": groups_data.get("totalResults"),
            "groups_sample": [
                {"displayName": g.get("displayName"), "memberCount": len(g.get("members") or [])}
                for g in (groups_data.get("Resources") or [])[:5]
            ],
        }
    except Exception as e:
        return {"error": str(e), "auth_method": "user_token" if user_token else "service_principal"}


@router.get("/groups-bundle")
async def get_groups_bundle(
    request: Request,
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Return group-level spend by joining billing data with workspace group membership."""
    if not end_date:
        end_date = (date.today() - timedelta(days=1)).isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    params = {"start_date": start_date, "end_date": end_date}
    user_token = request.headers.get("X-Forwarded-Access-Token")

    # Fetch group membership and billing data in parallel without blocking the event loop
    import asyncio as _asyncio
    _loop = _asyncio.get_running_loop()
    from backend.lakebase_read import PG_USERS_BY_WORKSPACE_DETAIL, gold_read
    billing_fut = _loop.run_in_executor(
        None, gold_read, "users_workspace_detail", PG_USERS_BY_WORKSPACE_DETAIL, params, "", USERS_BY_WORKSPACE_DETAIL
    )
    groups_fut = _loop.run_in_executor(None, _fetch_workspace_groups, user_token)
    _done, _ = await _asyncio.wait([billing_fut, groups_fut], timeout=30.0)
    try:
        billing_rows = billing_fut.result() if billing_fut in _done else []
    except Exception as e:
        logger.warning(f"Billing query failed: {e}")
        billing_rows = []
    try:
        user_groups = groups_fut.result() if groups_fut in _done else {}
    except Exception as e:
        logger.warning(f"Group mapping timed out or failed: {e}")
        user_groups = {}

    # Build group → workspace → product spend
    group_workspace_spend: dict[str, dict[str, dict[str, float]]] = {}
    group_users: dict[str, set] = {}
    ungrouped_spend: dict[str, float] = {}  # workspace_id → spend

    all_groups: set[str] = set()
    for groups in user_groups.values():
        all_groups.update(groups)

    for row in billing_rows:
        user = row.get("user_email") or ""
        workspace = row.get("workspace_id") or "unknown"
        product = row.get("product_category") or "Other"
        spend = float(row.get("spend") or 0)

        groups_for_user = user_groups.get(user, [])
        if not groups_for_user:
            ungrouped_spend[workspace] = ungrouped_spend.get(workspace, 0) + spend
            continue
        for gname in groups_for_user:
            group_workspace_spend.setdefault(gname, {})
            group_workspace_spend[gname].setdefault(workspace, {})
            group_workspace_spend[gname][workspace][product] = (
                group_workspace_spend[gname][workspace].get(product, 0) + spend
            )
            group_users.setdefault(gname, set()).add(user)

    # Build flat group list
    groups_out = []
    for gname, ws_data in group_workspace_spend.items():
        total = sum(sum(products.values()) for products in ws_data.values())
        product_totals: dict[str, float] = {}
        for ws_products in ws_data.values():
            for p, s in ws_products.items():
                product_totals[p] = product_totals.get(p, 0) + s
        primary = max(product_totals, key=lambda x: product_totals[x]) if product_totals else "Other"
        workspaces_out = [
            {
                "workspace_id": ws,
                "spend": sum(products.values()),
                "products": [{"product": p, "spend": s} for p, s in sorted(products.items(), key=lambda x: -x[1])],
            }
            for ws, products in sorted(ws_data.items(), key=lambda x: -sum(x[1].values()))
        ]
        groups_out.append({
            "group_name": gname,
            "total_spend": total,
            "member_count": len(group_users.get(gname, set())),
            "primary_product": primary,
            "workspaces": workspaces_out,
        })

    groups_out.sort(key=lambda x: -x["total_spend"])
    grand_total = sum(g["total_spend"] for g in groups_out)
    for g in groups_out:
        g["percentage"] = g["total_spend"] / grand_total * 100 if grand_total > 0 else 0

    # Ungrouped users
    ungrouped_total = sum(ungrouped_spend.values())

    return {
        "groups": groups_out,
        "total_spend": grand_total,
        "ungrouped_spend": ungrouped_total,
        "ungrouped_workspaces": [{"workspace_id": ws, "spend": s} for ws, s in sorted(ungrouped_spend.items(), key=lambda x: -x[1])],
        "groups_available": len(all_groups) > 0,
        "start_date": start_date,
        "end_date": end_date,
    }


# ── User Detail (groups + permissions) ───────────────────────────────────────

@router.get("/user-detail/{email:path}")
async def get_user_detail(request: Request, email: str) -> dict[str, Any]:
    """Return group memberships and permission grants for a specific user."""
    user_token = request.headers.get("X-Forwarded-Access-Token")
    try:
        user_groups = _fetch_workspace_groups(user_token)
        groups = user_groups.get(email, [])
    except Exception as e:
        logger.warning(f"Failed to fetch groups for user {email}: {e}")
        groups = []

    permission_grants: list[dict[str, str]] = []
    try:
        w = get_workspace_client()
        host = w.config.host.rstrip("/")
        auth_header = f"Bearer {user_token}" if user_token else w.config.authenticate().get("Authorization", "")
        # Find the user's SCIM ID and fetch entitlements/roles
        filter_param = f'userName eq "{email}"'
        resp = httpx.get(
            f"{host}/api/2.0/preview/scim/v2/Users",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params={"attributes": "id,userName,roles,entitlements", "filter": filter_param},
            timeout=_SCIM_TIMEOUT,
        )
        if resp.status_code == 200:
            resources = resp.json().get("Resources") or []
            if resources:
                u = resources[0]
                for role in (u.get("roles") or []):
                    val = role.get("value") or ""
                    if val:
                        permission_grants.append({"type": "workspace_role", "value": val})
                for ent in (u.get("entitlements") or []):
                    val = ent.get("value") or ""
                    if val:
                        permission_grants.append({"type": "entitlement", "value": val})
    except Exception as e:
        logger.warning(f"Could not look up SCIM info for {email}: {e}")

    return {
        "email": email,
        "groups": groups,
        "permission_grants": permission_grants,
    }


# ── Report Config CRUD ────────────────────────────────────────────────────────

@router.get("/report-config")
async def get_report_config() -> dict[str, Any]:
    return _load_report_config()


@router.post("/report-config/weekly-report", dependencies=[Depends(require_admin)])
async def add_weekly_report(data: WeeklyReportConfig) -> dict[str, Any]:
    config = _load_report_config()
    # Prevent duplicates
    existing = [r for r in config["weekly_reports"] if r["email"] == data.email]
    if existing:
        # Update existing
        for r in config["weekly_reports"]:
            if r["email"] == data.email:
                r.update({"name": data.name, "send_day": data.send_day, "enabled": data.enabled})
    else:
        config["weekly_reports"].append({
            "id": str(uuid.uuid4())[:8],
            "email": data.email,
            "name": data.name or data.email.split("@")[0],
            "send_day": data.send_day,
            "enabled": data.enabled,
            "created_at": datetime.utcnow().isoformat(),
        })
    _save_report_config(config)
    return {"success": True, "config": config}


@router.delete("/report-config/weekly-report/{email_or_id}", dependencies=[Depends(require_admin)])
async def delete_weekly_report(email_or_id: str) -> dict[str, Any]:
    config = _load_report_config()
    config["weekly_reports"] = [
        r for r in config["weekly_reports"]
        if r.get("email") != email_or_id and r.get("id") != email_or_id
    ]
    _save_report_config(config)
    return {"success": True}


@router.post("/report-config/user-alert", dependencies=[Depends(require_admin)])
async def add_user_alert(data: UserAlertConfig) -> dict[str, Any]:
    config = _load_report_config()
    existing = [a for a in config["user_alerts"] if a["email"] == data.email]
    if existing:
        for a in config["user_alerts"]:
            if a["email"] == data.email:
                a.update({
                    "name": data.name,
                    "threshold_amount": data.threshold_amount,
                    "spike_percent": data.spike_percent,
                    "enabled": data.enabled,
                })
    else:
        config["user_alerts"].append({
            "id": str(uuid.uuid4())[:8],
            "email": data.email,
            "name": data.name or data.email.split("@")[0],
            "threshold_amount": data.threshold_amount,
            "spike_percent": data.spike_percent,
            "enabled": data.enabled,
            "created_at": datetime.utcnow().isoformat(),
        })
    _save_report_config(config)
    return {"success": True, "config": config}


@router.delete("/report-config/user-alert/{email_or_id}", dependencies=[Depends(require_admin)])
async def delete_user_alert(email_or_id: str) -> dict[str, Any]:
    config = _load_report_config()
    config["user_alerts"] = [
        a for a in config["user_alerts"]
        if a.get("email") != email_or_id and a.get("id") != email_or_id
    ]
    _save_report_config(config)
    return {"success": True}


@router.post("/send-test-report", dependencies=[Depends(require_admin)])
async def send_test_report(
    email: str = Query(...),
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Send a test weekly spend report to the given email."""
    if not end_date:
        end_date = (date.today() - timedelta(days=1)).isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=7)).isoformat()

    params = {"start_date": start_date, "end_date": end_date}

    try:
        from backend.lakebase_read import PG_USERS_TOP, gold_read
        top_users = await asyncio.to_thread(gold_read, "users_top", PG_USERS_TOP, params, "", USERS_TOP_SPEND)
    except Exception as e:
        return {"success": False, "error": str(e)}

    rows_html = ""
    for i, u in enumerate(top_users[:10]):
        spend = float(u.get("total_spend") or 0)
        rows_html += f"""
        <tr style="background:{('#f9f9f9' if i % 2 else 'white')}">
          <td style="padding:8px 12px;font-size:13px;color:#333">{u.get('user_email','—')}</td>
          <td style="padding:8px 12px;font-size:13px;color:#333;text-align:right">${spend:,.2f}</td>
          <td style="padding:8px 12px;font-size:13px;color:#333;text-align:right">{int(u.get('active_days') or 0)}d</td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
      <div style="background:#FF3621;padding:20px 24px;border-radius:8px 8px 0 0">
        <h1 style="color:white;margin:0;font-size:20px">Weekly Spend Report</h1>
        <p style="color:rgba(255,255,255,0.8);margin:4px 0 0;font-size:13px">{start_date} → {end_date}</p>
      </div>
      <div style="background:white;padding:24px;border:1px solid #e5e5e5;border-top:none;border-radius:0 0 8px 8px">
        <h2 style="font-size:15px;color:#111;margin:0 0 12px">Top Users by Spend</h2>
        <table style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="background:#f0f0f0">
              <th style="padding:8px 12px;text-align:left;font-size:12px;color:#666">User</th>
              <th style="padding:8px 12px;text-align:right;font-size:12px;color:#666">Spend</th>
              <th style="padding:8px 12px;text-align:right;font-size:12px;color:#666">Active</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        <p style="font-size:11px;color:#999;margin-top:20px">
          This report was sent from Cost Observability &amp; Control.
        </p>
      </div>
    </div>"""

    result = send_alert_email(
        to_email=email,
        subject=f"Weekly Spend Report — {start_date} to {end_date}",
        html_body=html_body,
    )
    return result
