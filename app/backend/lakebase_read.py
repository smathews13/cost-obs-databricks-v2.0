"""Phase B — dual-read cutover for dashboard aggregate reads (serve Delta, shadow Lakebase).

This is the reader half of Phase B (docs/LAKEBASE_MIGRATION_DESIGN.md §13/§14). The
headline dashboard reads the `daily_*` aggregates through billing._exec_mv; this module
lets that choke point ALSO run the Postgres-dialect equivalent against Lakebase and log a
parity verdict — WITHOUT changing what the app serves — so the §13 burn-in accumulates
during real usage before any cutover.

Modes (env `COST_OBS_LAKEBASE_READ`):
  - off               — serve Delta only.
  - shadow           — serve Delta; fire-and-forget the Lakebase read + parity log.
  - on      (default) — serve Lakebase, falling back to warehouse gold on a miss.

Dialect notes (Databricks SQL → Postgres) for these specific aggregate queries:
  - backtick `{catalog}`.`{schema}`.`t` → unqualified `t` (the pool sets search_path).
  - `:named` params → psycopg `%(named)s`.
  - `CAST(workspace_id AS STRING)` (Delta) → `workspace_id` (already text in Lakebase).
  - workspace_id NULL (account-level) was stored as the '__account__' sentinel by the
    sync, so reads map it back with NULLIF to match Delta's NULL semantics (COUNT DISTINCT
    and the workspace grain).
The templates are hand-written (not auto-translated) so parity can't be faked by a lossy
translator — §14.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

_WS_SENTINEL = "__account__"

# Small pool for fire-and-forget shadow reads so they never block the served Delta path.
_shadow_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lb-shadow")


def lakebase_read_mode() -> str:
    """'off' | 'shadow' | 'on' — default 'on'."""
    m = os.getenv("COST_OBS_LAKEBASE_READ", "on").strip().lower()
    return m if m in ("off", "shadow", "on") else "on"


def warehouse_view_fallback() -> bool:
    """True when a viewer may hit warehouse SQL. False when Lakebase serving is on."""
    if lakebase_read_mode() != "on":
        return True
    try:
        from backend.db import lakebase_available, source_labels_active
        return bool(source_labels_active() or not lakebase_available())
    except Exception:
        return True


def pg_ws_filter(delta_ws_filter: str) -> str:
    """Translate the Delta workspace-filter clause to Postgres.

    The Delta clause is `AND CAST(workspace_id AS STRING) IN ('a','b')`; workspace_id is
    already text in Lakebase, so drop the CAST. Values are pre-validated + quoted upstream.
    """
    if not delta_ws_filter:
        return ""
    out = re.sub(r"CAST\(\s*(?:\w+\.)?workspace_id\s+AS\s+STRING\s*\)", "workspace_id",
                 delta_ws_filter, flags=re.IGNORECASE)
    # Unified-view source_label is Delta-only; app-managed Lakebase copies have no such column.
    out = re.sub(r"\s+AND\s+source_label\s+IN\s*\([^)]*\)", "", out, flags=re.IGNORECASE)
    return out


# ── Postgres-dialect templates for the dashboard-bundle headline reads ────────────
# `{ws_filter}` is the pg_ws_filter() output. Params: %(start_date)s / %(end_date)s.

PG_BILLING_SUMMARY = """
SELECT
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count,
  COUNT(DISTINCT usage_date) AS days_in_range,
  MIN(usage_date) AS first_date,
  MAX(usage_date) AS last_date
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_BILLING_BY_PRODUCT = """
SELECT
  product_category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count
FROM daily_product_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY product_category
ORDER BY total_spend DESC
"""

PG_BILLING_TIMESERIES = """
SELECT
  usage_date,
  product_category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_product_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date, product_category
ORDER BY usage_date, product_category
"""

PG_BILLING_BY_WORKSPACE = """
SELECT
  NULLIF(workspace_id, '__account__') AS workspace_id,
  MAX(workspace_name) AS workspace_name,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_workspace_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(workspace_id, '__account__')
ORDER BY total_spend DESC
"""

PG_ETL_BREAKDOWN = """
SELECT
  CASE
    WHEN product_category = 'ETL - Streaming' THEN 'Streaming (SDP)'
    WHEN product_category = 'ETL - Batch' THEN 'Batch Jobs'
  END AS etl_type,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_product_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND product_category IN ('ETL - Streaming', 'ETL - Batch')
GROUP BY product_category
ORDER BY total_spend DESC
"""

PG_SQL_TOOL_ATTRIBUTION = """
SELECT
  sql_product,
  SUM(attributed_dbus) AS total_dbus,
  SUM(attributed_spend) AS total_spend
FROM sql_tool_attribution
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY sql_product
ORDER BY total_spend DESC
"""

PG_PLATFORM_KPIS = """
SELECT
  SUM(total_queries) AS total_queries,
  MAX(unique_query_users) AS unique_query_users,
  SUM(total_rows_read) AS total_rows_read,
  SUM(total_bytes_read) AS total_bytes_read,
  SUM(total_compute_seconds) AS total_compute_seconds
FROM daily_query_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_TAG_STATS = """
WITH per_day AS (
  SELECT
    usage_date,
    SUM(total_spend) AS daily_spend,
    COUNT(DISTINCT CONCAT(tag_key, ':', tag_value)) AS daily_tag_count
  FROM daily_tag_summary
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
)
SELECT
  (SELECT COUNT(DISTINCT CONCAT(tag_key, ':', tag_value))
     FROM daily_tag_summary
     WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
       {ws_filter}
  ) AS total_tag_count,
  AVG(CASE WHEN daily_tag_count > 0 THEN daily_spend / daily_tag_count ELSE NULL END) AS avg_cost_per_tag
FROM per_day
"""

PG_AVG_DAILY_WORKSPACES = """
SELECT ROUND(AVG(daily_count)) AS avg_daily_workspaces
FROM (
  SELECT usage_date, COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS daily_count
  FROM daily_workspace_breakdown
  WHERE usage_date >= %(start_date)s
    AND usage_date <= %(end_date)s
    {ws_filter}
  GROUP BY usage_date
) t
"""

PG_AVG_DAILY_QUERY_USERS = """
SELECT ROUND(AVG(daily_users)) AS avg_daily_query_users
FROM (
  SELECT usage_date, SUM(unique_query_users) AS daily_users
  FROM daily_query_stats
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
) t
"""

PG_KPI_TREND_SPEND = """
SELECT usage_date AS date, SUM(total_spend) AS value
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_DBUS = """
SELECT usage_date AS date, SUM(total_dbus) AS value
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_WORKSPACES = """
SELECT usage_date AS date, COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS value
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_USER_SPEND = """
SELECT usage_date AS date, SUM(user_attributed_spend) AS value
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_APPS_SUMMARY = """
WITH apps_by_day AS (
  SELECT usage_date, workspace_id, app_id,
    SUM(total_dbus) AS daily_dbus,
    SUM(total_spend) AS daily_spend
  FROM daily_apps_summary
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
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

PG_APPS_BY_APP_FULL = """
SELECT
  app_id,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT workspace_id) AS workspace_count,
  COUNT(DISTINCT usage_date) AS days_active,
  MAX(usage_date) AS last_usage_date
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY app_id
ORDER BY total_spend DESC
"""

PG_APPS_TIMESERIES = """
SELECT
  usage_date,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {app_filter}
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_APPS_BY_APP_SKU = """
SELECT
  app_id,
  sku_name,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY app_id, sku_name
ORDER BY app_id, total_spend DESC
"""

PG_APPS_FILTERED_AVG = """
SELECT COALESCE(AVG(daily_apps), 0) AS avg_daily_apps
FROM (
  SELECT usage_date, COUNT(DISTINCT app_id) AS daily_apps
  FROM daily_apps_summary
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  {app_filter}
  GROUP BY usage_date
) t
"""

PG_SKU_BREAKDOWN = """
SELECT
  sku_name AS product,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspaces_using,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  ROUND(100.0 * SUM(total_spend) / NULLIF(SUM(SUM(total_spend)) OVER (), 0), 2) AS percentage
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY sku_name
ORDER BY total_spend DESC
LIMIT 100
"""

PG_PIPELINE_OBJECTS = """
SELECT
  object_type,
  object_id,
  MAX(object_name) AS object_name,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  NULL AS object_state,
  MAX(owner) AS owner,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  SUM(total_runs) AS total_runs
FROM daily_job_pipeline_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY object_type, object_id
ORDER BY total_spend DESC
LIMIT 200
"""

PG_INTERACTIVE_BREAKDOWN = """
SELECT
  cluster_id,
  MAX(cluster_name) AS cluster_name,
  notebook_path,
  run_as_user,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  NULL AS cluster_state,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active,
  SUM(notebook_count) AS notebook_count
FROM daily_interactive_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY cluster_id, notebook_path, run_as_user
ORDER BY total_spend DESC
LIMIT 100
"""

PG_TAGGING_COVERAGE = """
SELECT
  SUM(total_spend) AS total_spend,
  SUM(tagged_spend) AS tagged_spend,
  SUM(total_spend) - SUM(tagged_spend) AS untagged_spend,
  COUNT(DISTINCT CASE WHEN tagged_spend > 0 THEN NULLIF(workspace_id, '__account__') END) AS tagged_workspaces,
  COUNT(DISTINCT CASE WHEN total_spend > tagged_spend THEN NULLIF(workspace_id, '__account__') END) AS untagged_workspaces
FROM daily_tag_coverage
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_AIML_SUMMARY = """
SELECT
  aiml_category AS category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING')
    OR sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%'
    OR sku_name ILIKE '%INFERENCE%' OR sku_name ILIKE '%FINE_TUNING%' OR sku_name ILIKE '%VECTOR_SEARCH%'
    OR aiml_category IN (
      'FMAPI - Anthropic', 'FMAPI - OpenAI', 'FMAPI - Gemini',
      'Serverless Inference', 'Batch Inference', 'AI Search',
      'Fine Tuning', 'Model Serving'
    )
  )
GROUP BY aiml_category
ORDER BY total_spend DESC
"""

PG_GENIE_BY_WORKSPACE = """
SELECT
  NULLIF(workspace_id, '__account__') AS workspace_id,
  SUM(total_spend) AS total_usd,
  SUM(CASE WHEN charge_class = 'charged' THEN total_spend ELSE 0 END) AS charged_usd,
  SUM(CASE WHEN charge_class = 'free_tier' THEN total_spend ELSE 0 END) AS free_tier_usd,
  SUM(CASE WHEN charge_class = 'other' THEN total_spend ELSE 0 END) AS other_usage_type_usd,
  SUM(CASE WHEN genie_surface = 'genie_code' THEN total_spend ELSE 0 END) AS total_usd__genie_code,
  SUM(CASE WHEN genie_surface = 'genie_code' AND charge_class = 'charged' THEN total_spend ELSE 0 END) AS charged_usd__genie_code,
  SUM(CASE WHEN genie_surface = 'genie_code' AND charge_class = 'free_tier' THEN total_spend ELSE 0 END) AS free_tier_usd__genie_code,
  SUM(CASE WHEN genie_surface = 'genie_one' THEN total_spend ELSE 0 END) AS total_usd__genie_one,
  SUM(CASE WHEN genie_surface = 'genie_one' AND charge_class = 'charged' THEN total_spend ELSE 0 END) AS charged_usd__genie_one,
  SUM(CASE WHEN genie_surface = 'genie_one' AND charge_class = 'free_tier' THEN total_spend ELSE 0 END) AS free_tier_usd__genie_one,
  SUM(CASE WHEN genie_surface = 'genie_agents' THEN total_spend ELSE 0 END) AS total_usd__genie_agents,
  SUM(CASE WHEN genie_surface = 'genie_agents' AND charge_class = 'charged' THEN total_spend ELSE 0 END) AS charged_usd__genie_agents,
  SUM(CASE WHEN genie_surface = 'genie_agents' AND charge_class = 'free_tier' THEN total_spend ELSE 0 END) AS free_tier_usd__genie_agents,
  SUM(CASE WHEN genie_surface = 'unknown' THEN total_spend ELSE 0 END) AS total_usd__genie_unknown,
  SUM(CASE WHEN genie_surface = 'unknown' AND charge_class = 'charged' THEN total_spend ELSE 0 END) AS charged_usd__genie_unknown,
  SUM(CASE WHEN genie_surface = 'unknown' AND charge_class = 'free_tier' THEN total_spend ELSE 0 END) AS free_tier_usd__genie_unknown,
  MIN(usage_date) AS earliest_date_with_billing,
  MAX(usage_date) AS latest_date_with_billing
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND genie_surface IS NOT NULL
  AND genie_surface NOT IN ('__na__', '')
GROUP BY NULLIF(workspace_id, '__account__')
HAVING SUM(total_spend) > 0
"""

PG_ACCOUNT_TOTAL = """
SELECT COALESCE(SUM(total_spend), 0) AS databricks_total_usd
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_AIML_TOTALS = """
SELECT
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count,
  0 AS endpoint_count,
  COUNT(DISTINCT usage_date) AS days_in_range,
  MIN(usage_date) AS first_date,
  MAX(usage_date) AS last_date,
  0 AS avg_endpoints_per_day,
  0 AS avg_cost_per_endpoint
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING')
    OR sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%'
    OR sku_name ILIKE '%INFERENCE%' OR sku_name ILIKE '%FINE_TUNING%' OR sku_name ILIKE '%VECTOR_SEARCH%'
    OR aiml_category IN (
      'FMAPI - Anthropic', 'FMAPI - OpenAI', 'FMAPI - Gemini',
      'Serverless Inference', 'Batch Inference', 'AI Search',
      'Fine Tuning', 'Model Serving'
    )
  )
"""

PG_GENIE_TIMESERIES = """
SELECT
  usage_date,
  SUM(total_spend) AS total_usd,
  SUM(CASE WHEN charge_class = 'charged' THEN total_spend ELSE 0 END) AS charged_usd,
  SUM(CASE WHEN charge_class = 'free_tier' THEN total_spend ELSE 0 END) AS free_tier_usd,
  SUM(CASE WHEN genie_surface = 'genie_code' THEN total_spend ELSE 0 END) AS genie_code_usd,
  SUM(CASE WHEN genie_surface = 'genie_one' THEN total_spend ELSE 0 END) AS genie_one_usd,
  SUM(CASE WHEN genie_surface = 'genie_agents' THEN total_spend ELSE 0 END) AS genie_agents_usd
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND genie_surface IS NOT NULL
  AND genie_surface NOT IN ('__na__', '')
GROUP BY usage_date
ORDER BY usage_date
"""

PG_DBSQL_SUMMARY = """
SELECT
  SUM(query_count) AS total_queries,
  COUNT(DISTINCT executed_by) AS unique_users,
  COUNT(DISTINCT warehouse_id) AS unique_warehouses,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus,
  CASE WHEN SUM(query_count) > 0 THEN SUM(total_spend) / SUM(query_count) ELSE 0 END AS avg_cost_per_query,
  AVG(avg_duration_seconds) AS avg_duration_seconds
FROM daily_dbsql_source
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_DBSQL_BY_SOURCE = """
SELECT
  query_source_type,
  SUM(query_count) AS query_count,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus,
  CASE WHEN SUM(query_count) > 0 THEN SUM(total_spend) / SUM(query_count) ELSE 0 END AS avg_cost_per_query
FROM daily_dbsql_source
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY query_source_type
ORDER BY total_spend DESC
"""

PG_DBSQL_BY_USER = """
SELECT
  executed_by,
  query_source_type,
  SUM(query_count) AS query_count,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus
FROM daily_dbsql_source
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY executed_by, query_source_type
ORDER BY total_spend DESC
LIMIT 100
"""

PG_DBSQL_BY_WAREHOUSE = """
SELECT
  warehouse_id,
  SUM(query_count) AS query_count,
  COUNT(DISTINCT executed_by) AS unique_users,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus
FROM daily_dbsql_source
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY warehouse_id
ORDER BY total_spend DESC
LIMIT 50
"""

PG_DBSQL_TIMESERIES = """
SELECT
  usage_date AS date,
  query_source_type,
  SUM(query_count) AS query_count,
  SUM(total_spend) AS daily_spend,
  SUM(total_dbus) AS daily_dbus
FROM daily_dbsql_source
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date, query_source_type
ORDER BY date
"""

PG_DBSQL_TOP = """
SELECT
  statement_id,
  query_source_type,
  executed_by,
  warehouse_id,
  NULLIF(workspace_id, '__account__') AS workspace_id,
  statement_preview,
  duration_seconds,
  cost,
  dbus,
  query_profile_url,
  source_url,
  start_time,
  end_time
FROM daily_dbsql_top
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
ORDER BY cost DESC
LIMIT %(limit)s
"""

PG_AIML_PROVIDERS = """
SELECT
  CASE
    WHEN sku_name ILIKE '%ANTHROPIC%' THEN 'Anthropic'
    WHEN sku_name ILIKE '%OPENAI%' THEN 'OpenAI'
    WHEN sku_name ILIKE '%GEMINI%' THEN 'Gemini'
    ELSE 'Other'
  END AS provider,
  sku_name,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND (sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%')
GROUP BY 1, sku_name
ORDER BY total_spend DESC
"""

PG_AIML_CATEGORIES = """
SELECT
  aiml_category AS category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING')
    OR sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%'
    OR sku_name ILIKE '%INFERENCE%' OR sku_name ILIKE '%FINE_TUNING%' OR sku_name ILIKE '%VECTOR_SEARCH%'
    OR aiml_category IN (
      'FMAPI - Anthropic', 'FMAPI - OpenAI', 'FMAPI - Gemini',
      'Serverless Inference', 'Batch Inference', 'AI Search',
      'Fine Tuning', 'Model Serving'
    )
  )
GROUP BY aiml_category
ORDER BY total_spend DESC
"""

PG_AIML_TIMESERIES = """
SELECT
  usage_date,
  CASE
    WHEN aiml_category = 'FMAPI - Anthropic' THEN 'Anthropic'
    WHEN aiml_category = 'FMAPI - OpenAI' THEN 'OpenAI'
    WHEN aiml_category = 'FMAPI - Gemini' THEN 'Gemini'
    WHEN aiml_category = 'Model Serving' THEN 'Other Model Serving'
    ELSE aiml_category
  END AS category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING')
    OR sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%'
    OR sku_name ILIKE '%INFERENCE%' OR sku_name ILIKE '%FINE_TUNING%' OR sku_name ILIKE '%VECTOR_SEARCH%'
    OR aiml_category IN (
      'FMAPI - Anthropic', 'FMAPI - OpenAI', 'FMAPI - Gemini',
      'Serverless Inference', 'Batch Inference', 'AI Search',
      'Fine Tuning', 'Model Serving'
    )
  )
GROUP BY usage_date, 2
ORDER BY usage_date, 2
"""

PG_AIML_SKU_CATALOG = """
SELECT
  sku_name,
  billing_origin_product,
  usage_type,
  SUM(1) AS usage_records,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING', 'AGENT_BRICKS', 'SUPERVISOR_AGENT', 'KNOWLEDGE_AGENT')
    OR sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%'
    OR sku_name ILIKE '%INFERENCE%' OR sku_name ILIKE '%FINE_TUNING%' OR sku_name ILIKE '%VECTOR_SEARCH%'
  )
GROUP BY sku_name, billing_origin_product, usage_type
ORDER BY total_spend DESC
"""

PG_AIML_ENDPOINTS = """
SELECT
  endpoint_name,
  NULLIF(workspace_id, '__account__') AS workspace_id,
  sku_name,
  cost_type,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active
FROM daily_aiml_endpoints
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY endpoint_name, NULLIF(workspace_id, '__account__'), sku_name, cost_type
ORDER BY total_spend DESC
LIMIT 500
"""

PG_TAG_COVERAGE_TIMESERIES = """
SELECT
  usage_date,
  SUM(tagged_spend) AS tagged_spend,
  SUM(total_spend) - SUM(tagged_spend) AS untagged_spend
FROM daily_tag_coverage
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_COST_BY_TAG = """
SELECT
  tag_key,
  tag_value,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus
FROM daily_tag_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY tag_key, tag_value
ORDER BY total_spend DESC
LIMIT 200
"""

PG_COST_BY_TAG_KEY = """
SELECT
  tag_key,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT tag_value) AS value_count
FROM daily_tag_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY tag_key
ORDER BY total_spend DESC
LIMIT 100
"""

PG_CLOUD_SUMMARY = """
SELECT
  SUM(unblended_cost) AS total_unblended,
  SUM(net_unblended_cost) AS total_net_unblended,
  SUM(amortized_cost) AS total_amortized,
  SUM(net_amortized_cost) AS total_net_amortized,
  COUNT(DISTINCT NULLIF(cluster_id, '__na__')) AS cluster_count,
  COUNT(DISTINCT NULLIF(warehouse_id, '__na__')) AS warehouse_count,
  COUNT(DISTINCT usage_date) AS days_in_range
FROM daily_cloud_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
"""

PG_CLOUD_BY_CLUSTER = """
SELECT
  NULLIF(cluster_id, '__na__') AS cluster_id,
  SUM(CASE WHEN charge_type = 'Compute' THEN net_unblended_cost ELSE 0 END) AS compute_cost,
  SUM(CASE WHEN charge_type = 'Storage' THEN net_unblended_cost ELSE 0 END) AS storage_cost,
  SUM(CASE WHEN charge_type = 'Networking' THEN net_unblended_cost ELSE 0 END) AS network_cost,
  SUM(net_unblended_cost) AS total_cost,
  COUNT(DISTINCT usage_date) AS days_active
FROM daily_cloud_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
  AND cluster_id IS NOT NULL AND cluster_id <> '__na__'
GROUP BY cluster_id
ORDER BY total_cost DESC
LIMIT 100
"""

PG_CLOUD_BY_CHARGE_TYPE = """
SELECT
  charge_type,
  SUM(unblended_cost) AS unblended_cost,
  SUM(net_unblended_cost) AS net_unblended_cost,
  SUM(amortized_cost) AS amortized_cost,
  SUM(net_amortized_cost) AS net_amortized_cost
FROM daily_cloud_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY charge_type
ORDER BY net_unblended_cost DESC
"""

PG_CLOUD_TIMESERIES = """
SELECT
  usage_date AS date,
  charge_type,
  SUM(net_unblended_cost) AS daily_cost
FROM daily_cloud_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY usage_date, charge_type
ORDER BY date
"""

PG_APPS_AVG_COST_PER_APP = """
SELECT COALESCE(AVG(daily_cost_per_app), 0) AS avg_cost_per_app
FROM (
  SELECT usage_date,
    SUM(total_spend) / NULLIF(COUNT(DISTINCT app_id), 0) AS daily_cost_per_app
  FROM daily_apps_summary
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  {app_filter}
  GROUP BY usage_date
) t
"""

PG_USERS_SUMMARY = """
SELECT COUNT(DISTINCT NULLIF(user_email, '__na__')) AS user_count,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count,
  SUM(total_spend) AS total_spend, SUM(total_dbus) AS total_dbus,
  SUM(total_spend) / NULLIF(COUNT(DISTINCT NULLIF(user_email, '__na__')), 0) AS avg_spend_per_user
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_USERS_TOP = """
SELECT NULLIF(user_email, '__na__') AS user_email, SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus, COUNT(DISTINCT usage_date) AS active_days,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(user_email, '__na__')
ORDER BY total_spend DESC
"""

PG_USERS_PRODUCT = """
SELECT NULLIF(user_email, '__na__') AS user_email, product_category, SUM(total_spend) AS spend
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(user_email, '__na__'), product_category
ORDER BY spend DESC
LIMIT 5000
"""

PG_USERS_TIMESERIES = """
SELECT usage_date AS date, NULLIF(user_email, '__na__') AS user_email, SUM(total_spend) AS daily_spend
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND user_email IN (
    SELECT user_email FROM (
      SELECT user_email, SUM(total_spend) AS s FROM daily_user_spend
      WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
      GROUP BY user_email ORDER BY s DESC LIMIT 6
    ) t
  )
GROUP BY usage_date, NULLIF(user_email, '__na__')
ORDER BY date, daily_spend DESC
"""

PG_USERS_BY_WORKSPACE = """
SELECT NULLIF(workspace_id, '__account__') AS workspace_id,
  COUNT(DISTINCT NULLIF(user_email, '__na__')) AS user_count,
  SUM(total_spend) AS total_spend
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(workspace_id, '__account__')
ORDER BY total_spend DESC
LIMIT 20
"""

PG_USERS_SPEND_GROWTH = """
SELECT CASE WHEN usage_date <= %(mid_date)s THEN 'first_half' ELSE 'second_half' END AS period,
  SUM(total_spend) AS total_spend
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY 1
"""

PG_USERS_BY_WORKSPACE_DETAIL = """
SELECT NULLIF(workspace_id, '__account__') AS workspace_id,
  NULLIF(user_email, '__na__') AS user_email,
  product_category, SUM(total_spend) AS spend
FROM daily_user_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(workspace_id, '__account__'), NULLIF(user_email, '__na__'), product_category
ORDER BY spend DESC
LIMIT 2000
"""

PG_USERS_GROWTH = """
WITH base AS (
  SELECT date_trunc('month', usage_date)::date AS month, NULLIF(user_email, '__na__') AS user_id
  FROM daily_user_spend
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    AND user_email IS NOT NULL AND user_email <> '__na__'
),
user_first AS (SELECT user_id, MIN(month) AS first_month FROM base GROUP BY user_id)
SELECT to_char(b.month, 'YYYY-MM') AS month,
  COUNT(DISTINCT b.user_id) AS active_users,
  COUNT(DISTINCT CASE WHEN b.month = f.first_month THEN b.user_id END) AS new_users
FROM base b JOIN user_first f ON b.user_id = f.user_id
GROUP BY b.month
ORDER BY month
"""

PG_UNTAGGED = """
SELECT object_id AS {id_col}, MAX(NULLIF(object_name, '__na__')) AS {name_col},
  MAX(NULLIF(owner, '__na__')) AS owner, MAX(NULLIF(extra, '__na__')) AS cluster_source,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active
FROM daily_untagged_objects
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  AND object_type = %(object_type)s
  {ws_filter}
GROUP BY object_id
ORDER BY total_spend DESC
LIMIT 1000
"""

PG_AIML_MODELS = """
SELECT NULLIF(model_name, '__na__') AS model_name, model_type,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count
FROM daily_aiml_models
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(model_name, '__na__'), model_type
ORDER BY total_spend DESC
LIMIT 50
"""

PG_AIML_ML_CLUSTERS = """
SELECT MAX(NULLIF(cluster_name, '__na__')) AS cluster_name,
  NULLIF(cluster_id, '__na__') AS cluster_id,
  MAX(NULLIF(runtime_version, '__na__')) AS runtime_version,
  MAX(NULLIF(owner, '__na__')) AS owner,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active
FROM daily_aiml_ml_clusters
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(cluster_id, '__na__')
ORDER BY total_spend DESC
"""

PG_AIML_AGENTS = """
SELECT NULLIF(agent_name, '__na__') AS agent_name,
  MAX(NULLIF(endpoint_id, '__na__')) AS endpoint_id,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  MAX(billing_origin_product) AS billing_origin_product,
  MAX(NULLIF(served_entity_name, '__na__')) AS served_entity_name,
  MAX(agent_type) AS agent_type,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active,
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS workspace_count,
  MIN(usage_date) AS first_seen, MAX(usage_date) AS last_seen,
  SUM(total_spend) / NULLIF(COUNT(DISTINCT usage_date), 0) AS avg_daily_spend
FROM daily_aiml_agents
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(agent_name, '__na__')
ORDER BY total_spend DESC
"""

PG_INFRA_CLUSTERS = """
SELECT NULLIF(cluster_id, '__na__') AS cluster_id,
  MAX(NULLIF(cluster_name, '__na__')) AS cluster_name,
  MAX(NULLIF(driver_instance_type, '__na__')) AS driver_instance_type,
  MAX(NULLIF(worker_instance_type, '__na__')) AS worker_instance_type,
  MAX(NULLIF(cluster_source, '__na__')) AS cluster_source,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  MAX(NULLIF(workspace_name, '__na__')) AS workspace_name,
  MAX(cloud) AS cloud,
  SUM(total_dbu_hours) AS total_dbu_hours,
  COUNT(DISTINCT usage_date) AS days_active
FROM daily_infra_cluster_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(cluster_id, '__na__')
ORDER BY total_dbu_hours DESC
LIMIT 100
"""

PG_INFRA_TIMESERIES = """
SELECT usage_date, MAX(cloud) AS cloud, SUM(total_dbu_hours) AS total_dbu_hours
FROM daily_infra_cluster_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_INFRA_SUMMARY = """
SELECT SUM(daily_cost) AS total_cost, AVG(daily_clusters) AS avg_clusters_per_day,
  CASE WHEN AVG(daily_clusters) > 0 THEN AVG(daily_cost / daily_clusters) ELSE 0 END AS avg_cost_per_cluster,
  COUNT(*) AS days_in_range
FROM (
  SELECT usage_date, SUM(total_dbu_hours) AS daily_cost,
    COUNT(DISTINCT NULLIF(cluster_id, '__na__')) AS daily_clusters
  FROM daily_infra_cluster_spend
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
) d
"""

PG_WH_META = """
SELECT NULLIF(warehouse_id, '__na__') AS warehouse_id,
  MAX(NULLIF(warehouse_name, '__na__')) AS warehouse_name,
  MAX(warehouse_type) AS warehouse_type,
  MAX(warehouse_size) AS warehouse_size,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  MAX(NULLIF(workspace_name, '__na__')) AS workspace_name
FROM daily_dbsql_warehouse_type
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(warehouse_id, '__na__')
"""

PG_WH_TYPE_BILLING = """
SELECT usage_date AS date, NULLIF(warehouse_id, '__na__') AS warehouse_id, SUM(daily_spend) AS daily_spend
FROM daily_dbsql_warehouse_type
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date, NULLIF(warehouse_id, '__na__')
"""

PG_REGION_SCOPE = """
SELECT billing_ws_count AS billing_ws, compute_ws_count AS compute_ws
FROM daily_dbsql_region_scope
ORDER BY snapshot_date DESC
LIMIT 1
"""

PG_WAREHOUSE_OPTIMIZER = """
SELECT * FROM warehouse_optimizer_findings
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM warehouse_optimizer_findings)
"""

PG_OBJECT_SPEND = """
SELECT usage_date, object_type, NULLIF(object_id, '__na__') AS object_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend
FROM daily_object_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date, object_type, NULLIF(object_id, '__na__')
"""

PG_AVAILABLE_OBJECTS = """
SELECT object_type, NULLIF(object_id, '__na__') AS object_id,
  MAX(NULLIF(object_name, '__na__')) AS object_name,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id
FROM daily_object_spend
WHERE usage_date >= CURRENT_DATE - INTERVAL '90 days'
  AND object_type = %(object_type)s
GROUP BY object_type, NULLIF(object_id, '__na__')
ORDER BY object_id
LIMIT 100
"""

PG_MONTHLY_SPEND = """
SELECT date_trunc('month', usage_date)::date AS month,
  SUM(total_spend) AS total_spend, SUM(total_dbus) AS total_dbus
FROM daily_usage_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY 1
ORDER BY 1
"""

PG_AI_GATEWAY_ROWS = """
SELECT usage_date, NULLIF(workspace_id, '__account__') AS workspace_id,
  NULLIF(route_name, '__na__') AS route_name,
  NULLIF(model_name, '__na__') AS model_name,
  client_user, request_count, input_tokens, output_tokens, total_tokens,
  avg_latency_ms, error_count, success_count
FROM daily_ai_gateway
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_WORKSPACE_LIST = """
SELECT NULLIF(workspace_id, '__account__') AS workspace_id,
  MAX(NULLIF(workspace_name, '__na__')) AS workspace_name
FROM daily_workspace_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  AND workspace_id IS NOT NULL AND workspace_id <> '__account__'
  {ws_filter}
GROUP BY NULLIF(workspace_id, '__account__')
ORDER BY COALESCE(MAX(NULLIF(workspace_name, '__na__')), NULLIF(workspace_id, '__account__'))
"""

PG_WORKSPACE_COUNT_LATEST = """
SELECT daily_ws AS workspace_count FROM (
  SELECT usage_date, COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS daily_ws
  FROM daily_workspace_breakdown
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
  ORDER BY usage_date DESC
  LIMIT 1
) t
"""

PG_WAREHOUSE_IDLE = """
SELECT NULLIF(warehouse_id, '__na__') AS warehouse_id,
  MAX(NULLIF(warehouse_name, '__na__')) AS warehouse_name,
  MAX(warehouse_size) AS warehouse_size,
  MAX(warehouse_type) AS warehouse_type,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id,
  CASE WHEN MAX(CASE WHEN uptime_source = 'events' THEN 1 ELSE 0 END) = 1 THEN 'events' ELSE 'billing' END AS uptime_source,
  SUM(total_running_minutes) AS total_running_minutes,
  SUM(busy_union_minutes) AS busy_union_minutes,
  SUM(idle_minutes) AS idle_minutes,
  SUM(warm_hold_minutes) AS warm_hold_minutes,
  MAX(auto_stop_mins) AS auto_stop_mins,
  MAX(max_num_clusters) AS max_num_clusters,
  SUM(total_spend) AS total_spend,
  SUM(estimated_idle_spend) AS estimated_idle_spend
FROM daily_warehouse_idle
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(warehouse_id, '__na__')
HAVING SUM(total_running_minutes) >= 10
ORDER BY COALESCE(SUM(estimated_idle_spend), -1) DESC, SUM(warm_hold_minutes) DESC, SUM(total_spend) DESC
LIMIT 25
"""

PG_WAREHOUSE_IDLE_SERVERLESS = """
SELECT COUNT(DISTINCT NULLIF(warehouse_id, '__na__')) AS serverless_count
FROM daily_warehouse_idle
WHERE warehouse_type = 'SERVERLESS'
"""

PG_KPI_BILLING = """
SELECT
  COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS active_workspaces,
  SUM(distinct_jobs) AS total_jobs,
  SUM(job_run_rows) AS total_job_runs,
  SUM(distinct_job_owners) AS unique_job_owners,
  SUM(distinct_clusters) AS total_clusters,
  SUM(distinct_sql_warehouses) AS sql_warehouses,
  SUM(sql_dbus) AS sql_dbus,
  SUM(distinct_models) AS models_served,
  SUM(serving_dbus) AS total_serving_dbus
FROM daily_kpi_facts
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_KPI_LAKEFLOW = """
SELECT SUM(total_runs) AS total_runs, SUM(successful_runs) AS successful_runs,
  SUM(total_run_hours) AS total_run_hours
FROM daily_lakeflow_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_TOTAL_WORKSPACES = """
SELECT COUNT(DISTINCT NULLIF(workspace_id, '__account__')) AS total_workspaces
FROM daily_workspace_breakdown
"""

PG_AVG_DAILY_MODELS = """
SELECT COALESCE(ROUND(AVG(daily_models)), 0) AS avg_daily_models
FROM (
  SELECT usage_date, SUM(distinct_models) AS daily_models
  FROM daily_kpi_facts
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
) t
"""

PG_QUERY_USER_COUNT = """
SELECT COUNT(DISTINCT NULLIF(user_id, '__na__')) AS unique_query_users
FROM daily_query_users
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
"""

PG_STICKINESS = """
WITH total_users AS (
  SELECT COUNT(DISTINCT NULLIF(user_id, '__na__')) AS total
  FROM daily_query_users
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
),
daily_users AS (
  SELECT usage_date, COUNT(DISTINCT NULLIF(user_id, '__na__')) AS dau
  FROM daily_query_users
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
)
SELECT ROUND(AVG(100.0 * d.dau / NULLIF(t.total, 0))::numeric, 1) AS stickiness_pct
FROM daily_users d CROSS JOIN total_users t
"""

PG_SPEND_ANOMALIES = """
WITH daily_stats AS (
  SELECT usage_date, SUM(total_spend) AS daily_spend
  FROM daily_usage_summary
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
  GROUP BY usage_date
),
with_lag AS (
  SELECT usage_date, daily_spend,
    LAG(daily_spend) OVER (ORDER BY usage_date) AS prev_day_spend
  FROM daily_stats
)
SELECT usage_date, daily_spend, prev_day_spend,
  daily_spend - prev_day_spend AS change_amount,
  ROUND((100.0 * (daily_spend - prev_day_spend) / NULLIF(prev_day_spend, 0))::numeric, 2) AS change_percent
FROM with_lag
WHERE prev_day_spend IS NOT NULL AND prev_day_spend > 0
ORDER BY ABS(change_percent) DESC
LIMIT 20
"""

PG_APPS_IDENTITIES = """
SELECT NULLIF(app_id, '__na__') AS app_id, NULLIF(run_as, '__na__') AS run_as
FROM daily_apps_identities
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY NULLIF(app_id, '__na__'), NULLIF(run_as, '__na__')
"""

PG_AVAILABLE_QUERIES = """
SELECT NULLIF(object_id, '__na__') AS object_id,
  MAX(NULLIF(object_name, '__na__')) AS object_name,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id
FROM use_case_query_objects
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM use_case_query_objects)
GROUP BY NULLIF(object_id, '__na__')
LIMIT 100
"""

PG_AVAILABLE_DASHBOARDS = """
SELECT NULLIF(object_id, '__na__') AS object_id,
  MAX(NULLIF(object_name, '__na__')) AS object_name,
  MAX(NULLIF(workspace_id, '__account__')) AS workspace_id
FROM use_case_dashboard_objects
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM use_case_dashboard_objects)
GROUP BY NULLIF(object_id, '__na__')
LIMIT 100
"""

PG_AZURE_SUMMARY = """
SELECT SUM(total_cost) AS total_cost, SUM(total_cost_usd) AS total_cost_usd,
  COUNT(DISTINCT NULLIF(cluster_id, '__na__')) AS cluster_count,
  COUNT(DISTINCT NULLIF(warehouse_id, '__na__')) AS warehouse_count,
  COUNT(DISTINCT usage_date) AS days_in_range
FROM daily_azure_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
"""

PG_AZURE_BY_CLUSTER = """
SELECT NULLIF(cluster_id, '__na__') AS cluster_id,
  SUM(CASE WHEN charge_type = 'Compute' THEN total_cost ELSE 0 END) AS compute_cost,
  SUM(CASE WHEN charge_type = 'Storage' THEN total_cost ELSE 0 END) AS storage_cost,
  SUM(CASE WHEN charge_type = 'Networking' THEN total_cost ELSE 0 END) AS network_cost,
  SUM(total_cost) AS total_cost, COUNT(DISTINCT usage_date) AS days_active
FROM daily_azure_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
  AND cluster_id IS NOT NULL AND cluster_id <> '__na__'
GROUP BY NULLIF(cluster_id, '__na__')
ORDER BY total_cost DESC
LIMIT 100
"""

PG_AZURE_BY_CHARGE_TYPE = """
SELECT charge_type, SUM(total_cost) AS total_cost, SUM(total_cost_usd) AS total_cost_usd
FROM daily_azure_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY charge_type
ORDER BY total_cost DESC
"""

PG_AZURE_TIMESERIES = """
SELECT usage_date AS date, charge_type, SUM(total_cost) AS daily_cost
FROM daily_azure_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY usage_date, charge_type
ORDER BY date
"""

PG_GCP_SUMMARY = """
SELECT SUM(total_cost) AS total_cost, MAX(currency) AS currency,
  COUNT(DISTINCT NULLIF(project_id, '__na__')) AS project_count,
  COUNT(DISTINCT service) AS service_count,
  COUNT(DISTINCT usage_date) AS days_in_range
FROM daily_gcp_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
"""

PG_GCP_BY_SERVICE = """
SELECT service, SUM(total_cost) AS total_cost, COUNT(DISTINCT usage_date) AS days_active
FROM daily_gcp_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY service
ORDER BY total_cost DESC
LIMIT 50
"""

PG_GCP_BY_PROJECT = """
SELECT NULLIF(project_id, '__na__') AS project_id, MAX(project_name) AS project_name,
  SUM(total_cost) AS total_cost, COUNT(DISTINCT service) AS service_count
FROM daily_gcp_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY NULLIF(project_id, '__na__')
ORDER BY total_cost DESC
LIMIT 50
"""

PG_GCP_TIMESERIES = """
SELECT usage_date AS date, service, SUM(total_cost) AS daily_cost
FROM daily_gcp_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY usage_date, service
ORDER BY date
"""

PG_GCP_BY_SKU = """
SELECT service, sku, SUM(total_cost) AS total_cost
FROM daily_gcp_actuals
WHERE usage_date >= %(start_date)s AND usage_date < %(end_date)s
GROUP BY service, sku
ORDER BY total_cost DESC
LIMIT 100
"""

PG_KPI_TREND_QUERIES = """
SELECT usage_date AS date, SUM(total_queries) AS value
FROM daily_query_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_ROWS = """
SELECT usage_date AS date, SUM(total_rows_read) AS value
FROM daily_query_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_BYTES = """
SELECT usage_date AS date, SUM(total_bytes_read) AS value
FROM daily_query_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_COMPUTE = """
SELECT usage_date AS date, SUM(total_compute_seconds) AS value
FROM daily_query_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_USERS = """
SELECT usage_date AS date, COUNT(DISTINCT NULLIF(user_id, '__na__')) AS value
FROM daily_query_users
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_JOBS = """
SELECT usage_date AS date, SUM(distinct_jobs) AS value
FROM daily_kpi_facts
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_JOB_RUNS = """
SELECT usage_date AS date, SUM(job_run_rows) AS value
FROM daily_kpi_facts
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_CLUSTERS = """
SELECT usage_date AS date, SUM(distinct_clusters) AS value
FROM daily_kpi_facts
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_MODELS = """
SELECT usage_date AS date, SUM(distinct_models) AS value
FROM daily_kpi_facts
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_LAKEFLOW_SUCCESS = """
SELECT usage_date AS date, SUM(successful_runs) AS value
FROM daily_lakeflow_stats
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
"""

PG_KPI_TREND_STICKINESS = """
WITH total_users AS (
  SELECT COUNT(DISTINCT NULLIF(user_id, '__na__')) AS total
  FROM daily_query_users
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
),
daily_users AS (
  SELECT usage_date AS date, COUNT(DISTINCT NULLIF(user_id, '__na__')) AS dau
  FROM daily_query_users
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
  GROUP BY usage_date
)
SELECT d.date, 100.0 * d.dau / NULLIF(t.total, 0) AS value
FROM daily_users d CROSS JOIN total_users t
ORDER BY d.date
"""

PLATFORM_KPI_TREND_PG = {
    "total_queries": PG_KPI_TREND_QUERIES,
    "total_rows_read": PG_KPI_TREND_ROWS,
    "total_bytes_read": PG_KPI_TREND_BYTES,
    "total_compute_seconds": PG_KPI_TREND_COMPUTE,
    "total_users": PG_KPI_TREND_USERS,
    "total_jobs": PG_KPI_TREND_JOBS,
    "total_job_runs": PG_KPI_TREND_JOB_RUNS,
    "active_notebooks": PG_KPI_TREND_CLUSTERS,
    "active_workspaces": PG_KPI_TREND_WORKSPACES,
    "models_served": PG_KPI_TREND_MODELS,
    "successful_runs": PG_KPI_TREND_LAKEFLOW_SUCCESS,
    "stickiness": PG_KPI_TREND_STICKINESS,
    "unique_warehouses": """
SELECT usage_date AS date, COUNT(DISTINCT NULLIF(warehouse_id, '__na__')) AS value
FROM daily_dbsql_warehouse_type
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
}

_AIML_KPI_WHERE = """
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING')
    OR sku_name ILIKE '%ANTHROPIC%' OR sku_name ILIKE '%OPENAI%' OR sku_name ILIKE '%GEMINI%'
    OR sku_name ILIKE '%INFERENCE%' OR sku_name ILIKE '%FINE_TUNING%' OR sku_name ILIKE '%VECTOR_SEARCH%'
    OR aiml_category IN (
      'FMAPI - Anthropic', 'FMAPI - OpenAI', 'FMAPI - Gemini',
      'Serverless Inference', 'Batch Inference', 'AI Search',
      'Fine Tuning', 'Model Serving'
    )
  )
"""

PG_KPI_TREND_TAGGED = """
SELECT usage_date AS date, SUM(tagged_spend) AS value
FROM daily_tag_coverage
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_UNTAGGED = """
SELECT usage_date AS date, SUM(total_spend) - SUM(tagged_spend) AS value
FROM daily_tag_coverage
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_INFRA_COST = """
SELECT usage_date AS date, SUM(total_spend) AS value
FROM daily_product_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
  AND product_category IN (
    'Interactive', 'ETL - Batch', 'ETL - Streaming', 'SQL - DBSQL', 'SQL - Genie'
  )
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_INFRA_CLUSTERS = """
SELECT usage_date AS date, COUNT(DISTINCT NULLIF(cluster_id, '__na__')) AS value
FROM daily_infra_cluster_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_INFRA_DBUS = """
SELECT usage_date AS date, SUM(total_dbu_hours) AS value
FROM daily_infra_cluster_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_AVG_CLUSTER = """
SELECT usage_date AS date,
  SUM(total_dbu_hours) * 0.50 / NULLIF(COUNT(DISTINCT NULLIF(cluster_id, '__na__')), 0) AS value
FROM daily_infra_cluster_spend
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_AIML_SPEND = """
SELECT usage_date AS date, SUM(total_spend) AS value
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
""" + _AIML_KPI_WHERE + """
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_AIML_DBUS = """
SELECT usage_date AS date, SUM(total_dbus) AS value
FROM daily_sku_breakdown
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
""" + _AIML_KPI_WHERE + """
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_AIML_ENDPOINTS = """
SELECT usage_date AS date, COUNT(DISTINCT NULLIF(endpoint_name, '__na__')) AS value
FROM daily_aiml_endpoints
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_AIML_AVG_EP = """
SELECT usage_date AS date,
  SUM(total_spend) / NULLIF(COUNT(DISTINCT NULLIF(endpoint_name, '__na__')), 0) AS value
FROM daily_aiml_endpoints
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_APPS_SPEND = """
SELECT usage_date AS date, SUM(total_spend) AS value
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_APPS_DBUS = """
SELECT usage_date AS date, SUM(total_dbus) AS value
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_APPS_COUNT = """
SELECT usage_date AS date, COUNT(DISTINCT NULLIF(app_id, 'Unknown')) AS value
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_APPS_AVG = """
SELECT usage_date AS date,
  SUM(total_spend) / NULLIF(COUNT(DISTINCT NULLIF(app_id, 'Unknown')), 0) AS value
FROM daily_apps_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_KPI_TREND_TOTAL_TAGS = """
SELECT usage_date AS date, COUNT(DISTINCT tag_key || ':' || tag_value) AS value
FROM daily_tag_summary
WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
  {ws_filter}
GROUP BY usage_date
ORDER BY usage_date
"""

PG_AVAILABLE_TAGS = """
SELECT tag_key, ARRAY_AGG(DISTINCT tag_value) AS tag_values
FROM daily_tag_summary
WHERE tag_key IS NOT NULL
GROUP BY tag_key
"""

PG_APPS_WORKSPACES = """
SELECT NULLIF(a.app_id, 'Unknown') AS app_id,
  NULLIF(a.workspace_id, '__account__') AS workspace_id,
  MAX(NULLIF(w.workspace_name, '__na__')) AS workspace_name
FROM (
  SELECT app_id, workspace_id
  FROM daily_apps_summary
  WHERE usage_date BETWEEN %(start_date)s AND %(end_date)s
    {ws_filter}
) a
LEFT JOIN (
  SELECT workspace_id, MAX(NULLIF(workspace_name, '__na__')) AS workspace_name
  FROM daily_workspace_breakdown
  GROUP BY workspace_id
) w ON w.workspace_id = a.workspace_id
GROUP BY NULLIF(a.app_id, 'Unknown'), NULLIF(a.workspace_id, '__account__')
"""

KPI_TREND_PG = {
    "total_spend": PG_KPI_TREND_SPEND,
    "avg_daily_spend": PG_KPI_TREND_SPEND,
    "total_dbus": PG_KPI_TREND_DBUS,
    "workspace_count": PG_KPI_TREND_WORKSPACES,
    "user_spend": PG_KPI_TREND_USER_SPEND,
    "tagged_spend": PG_KPI_TREND_TAGGED,
    "untagged_spend": PG_KPI_TREND_UNTAGGED,
    "infra_cost": PG_KPI_TREND_INFRA_COST,
    "infra_clusters": PG_KPI_TREND_INFRA_CLUSTERS,
    "infra_dbu_hours": PG_KPI_TREND_INFRA_DBUS,
    "avg_cost_per_cluster": PG_KPI_TREND_AVG_CLUSTER,
    "aiml_spend": PG_KPI_TREND_AIML_SPEND,
    "aiml_dbus": PG_KPI_TREND_AIML_DBUS,
    "aiml_endpoints": PG_KPI_TREND_AIML_ENDPOINTS,
    "aiml_avg_endpoint_cost": PG_KPI_TREND_AIML_AVG_EP,
    "apps_spend": PG_KPI_TREND_APPS_SPEND,
    "apps_dbus": PG_KPI_TREND_APPS_DBUS,
    "apps_count": PG_KPI_TREND_APPS_COUNT,
    "apps_avg_cost_per_app": PG_KPI_TREND_APPS_AVG,
    "total_tags": PG_KPI_TREND_TOTAL_TAGS,
}

# Registry: maps a Databricks MV template (by identity) → (parity name, PG template, key cols).
# Built lazily to avoid an import cycle with materialized_views.
_REGISTRY: dict[str, tuple[str, str, list[str]]] | None = None


def _registry() -> dict[str, tuple[str, str, list[str]]]:
    global _REGISTRY
    if _REGISTRY is None:
        from backend.materialized_views import (
            MV_BILLING_BY_PRODUCT,
            MV_BILLING_BY_WORKSPACE,
            MV_BILLING_SUMMARY,
            MV_BILLING_TIMESERIES,
            MV_ETL_BREAKDOWN,
            MV_PLATFORM_KPIS,
            MV_SQL_TOOL_ATTRIBUTION,
            MV_TAG_STATS,
        )
        from backend.materialized_views_extra import (
            MV_INTERACTIVE_BREAKDOWN,
            MV_PIPELINE_OBJECTS,
            MV_SKU_BREAKDOWN,
            MV_TAGGING_COVERAGE,
        )
        from backend.queries import AVG_DAILY_QUERY_USERS_MV, AVG_DAILY_WORKSPACES
        from backend.routers.apps import (
            MV_APPS_AVG_COST_PER_APP,
            MV_APPS_BY_APP_FULL,
            MV_APPS_BY_APP_SKU,
            MV_APPS_FILTERED_AVG,
            MV_APPS_SUMMARY,
            MV_APPS_TIMESERIES,
        )
        _REGISTRY = {
            MV_BILLING_SUMMARY:     ("billing_summary",  PG_BILLING_SUMMARY,     []),
            MV_BILLING_BY_PRODUCT:  ("billing_products", PG_BILLING_BY_PRODUCT,  ["product_category"]),
            MV_BILLING_TIMESERIES:  ("billing_timeseries", PG_BILLING_TIMESERIES, ["usage_date", "product_category"]),
            MV_BILLING_BY_WORKSPACE: ("billing_workspaces", PG_BILLING_BY_WORKSPACE, ["workspace_id"]),
            MV_ETL_BREAKDOWN:       ("billing_etl",      PG_ETL_BREAKDOWN,       ["etl_type"]),
            MV_SQL_TOOL_ATTRIBUTION: ("sql_tool",        PG_SQL_TOOL_ATTRIBUTION, ["sql_product"]),
            MV_PLATFORM_KPIS:       ("platform_kpis",    PG_PLATFORM_KPIS,       []),
            MV_TAG_STATS:           ("tag_stats",        PG_TAG_STATS,           []),
            AVG_DAILY_WORKSPACES:   ("avg_daily_workspaces", PG_AVG_DAILY_WORKSPACES, []),
            AVG_DAILY_QUERY_USERS_MV: ("avg_daily_query_users", PG_AVG_DAILY_QUERY_USERS, []),
            MV_APPS_SUMMARY:        ("apps_summary",     PG_APPS_SUMMARY,        []),
            MV_APPS_BY_APP_FULL:    ("apps_by_app",      PG_APPS_BY_APP_FULL,    ["app_id"]),
            MV_APPS_TIMESERIES:     ("apps_timeseries",  PG_APPS_TIMESERIES,     ["usage_date"]),
            MV_APPS_BY_APP_SKU:     ("apps_sku",         PG_APPS_BY_APP_SKU,     ["app_id", "sku_name"]),
            MV_APPS_FILTERED_AVG:   ("apps_filtered_avg", PG_APPS_FILTERED_AVG,  []),
            MV_APPS_AVG_COST_PER_APP: ("apps_avg_cost",  PG_APPS_AVG_COST_PER_APP, []),
            MV_SKU_BREAKDOWN:         ("sku_breakdown",  PG_SKU_BREAKDOWN,        ["product"]),
            MV_PIPELINE_OBJECTS:      ("pipeline_objects", PG_PIPELINE_OBJECTS,   ["object_type", "object_id"]),
            MV_INTERACTIVE_BREAKDOWN: ("interactive",    PG_INTERACTIVE_BREAKDOWN, ["cluster_id", "notebook_path", "run_as_user"]),
            MV_TAGGING_COVERAGE:      ("tagging_coverage", PG_TAGGING_COVERAGE,   []),
        }
    return _REGISTRY


class _Fmt(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _resolve_delta(delta_result: list[dict] | None, fetch_delta) -> list[dict]:
    if delta_result is not None:
        return delta_result
    if fetch_delta is not None:
        try:
            return fetch_delta() or []
        except Exception as e:
            from backend.db import SourceScopeUnsupportedError
            if isinstance(e, SourceScopeUnsupportedError):
                raise
            logger.warning("warehouse gold fallback failed: %s", e)
            return []
    return []


_PG_TABLE_RE = re.compile(r'\b(?:FROM|JOIN)\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', re.I)
_loading_cache: dict[str, Any] = {"at": 0.0, "tables": frozenset()}


def _tables_still_loading() -> frozenset[str]:
    """Gold copies mid full reload (sync cleared backfilled_at). Cached 30s."""
    now = time.monotonic()
    if now - _loading_cache["at"] < 30:
        return _loading_cache["tables"]
    tables: frozenset[str] = frozenset()
    try:
        from backend.db import lakebase_query
        from backend.lakebase_sync import _SYNC_STATE_TABLE
        rows = lakebase_query(f"SELECT table_name FROM {_SYNC_STATE_TABLE} WHERE backfilled_at IS NULL")
        tables = frozenset(str(r["table_name"]) for r in rows)
    except Exception as e:
        logger.debug("lakebase_read: sync-state check failed: %s", e)
    _loading_cache.update(at=now, tables=tables)
    return tables


def dual_read(
    name: str,
    pg_tmpl: str,
    params: dict[str, Any],
    delta_ws_filter: str,
    delta_result: list[dict] | None = None,
    key_cols: list[str] | None = None,
    extra_format: dict[str, str] | None = None,
    fetch_delta=None,
) -> list[dict]:
    """Serve Lakebase in 'on' mode without touching the warehouse unless Postgres misses.

    Pass either a precomputed `delta_result` or a `fetch_delta` callable. In 'on'
    mode the callable is invoked only when Lakebase errors, returns no rows, or
    source labels are on.
    """
    mode = lakebase_read_mode()
    if mode == "off":
        return _resolve_delta(delta_result, fetch_delta)
    try:
        from backend.db import lakebase_available, source_labels_active
        if not lakebase_available() or source_labels_active():
            return _resolve_delta(delta_result, fetch_delta)
        fmt = _Fmt(ws_filter=pg_ws_filter(delta_ws_filter), app_filter="")
        if extra_format:
            fmt.update(extra_format)
        pg_sql = pg_tmpl.format_map(fmt)
        pg_params = {
            "start_date": params.get("start_date"),
            "end_date": params.get("end_date"),
            **{k: v for k, v in params.items() if k not in ("start_date", "end_date")},
        }
        keys = key_cols or []

        if mode == "on":
            from backend.db import lakebase_query
            loading = _tables_still_loading()
            if loading and loading.intersection(_PG_TABLE_RE.findall(pg_sql)):
                logger.info("lakebase_read[%s]: gold copy is reloading, serving warehouse gold", name)
                return _resolve_delta(delta_result, fetch_delta)
            try:
                pg_rows = lakebase_query(pg_sql, pg_params)
            except Exception as e:
                logger.warning("lakebase_read[%s]: 'on' read failed, falling back to Delta: %s", name, e)
                return _resolve_delta(delta_result, fetch_delta)
            if pg_rows:
                return pg_rows
            gold = _resolve_delta(delta_result, fetch_delta)
            if gold:
                logger.info("lakebase_read[%s]: Postgres empty, serving warehouse gold", name)
                return gold
            return pg_rows

        delta = _resolve_delta(delta_result, fetch_delta)
        _shadow_pool.submit(_run_shadow, name, pg_sql, pg_params, keys, delta)
        return delta
    except Exception as e:
        from backend.db import SourceScopeUnsupportedError
        if isinstance(e, SourceScopeUnsupportedError):
            raise
        logger.debug("dual_read error (non-fatal, serving Delta): %s", e)
        return _resolve_delta(delta_result, fetch_delta)


def dual_read_mv(
    mv_template: str,
    params: dict[str, Any],
    delta_ws_filter: str,
    delta_result: list[dict] | None = None,
    extra_format: dict[str, str] | None = None,
    fetch_delta=None,
) -> list[dict]:
    """Cutover choke point. 'on' serves Lakebase first; warehouse only on fallback.

    In shadow mode a background task reads Lakebase and logs a parity verdict.
    """
    mode = lakebase_read_mode()
    if mode == "off":
        return _resolve_delta(delta_result, fetch_delta)
    try:
        from backend.db import lakebase_available
        if not lakebase_available():
            return _resolve_delta(delta_result, fetch_delta)
        reg = _registry().get(mv_template)
        if reg is None:
            return _resolve_delta(delta_result, fetch_delta)
        name, pg_tmpl, key_cols = reg
        return dual_read(
            name, pg_tmpl, params, delta_ws_filter, delta_result, key_cols, extra_format,
            fetch_delta=fetch_delta,
        )
    except Exception as e:
        from backend.db import SourceScopeUnsupportedError
        if isinstance(e, SourceScopeUnsupportedError):
            raise
        logger.debug("dual_read_mv error (non-fatal, serving Delta): %s", e)
        return _resolve_delta(delta_result, fetch_delta)


def gold_read(
    name: str,
    pg_tmpl: str,
    params: dict[str, Any],
    ws_clause: str = "",
    live_sql: str | None = None,
    extra_format: dict[str, str] | None = None,
) -> list[dict]:
    """Postgres first, warehouse gold on miss, live system SQL only when fallback is allowed."""
    from backend.db import apply_mv_overrides, execute_query, get_catalog_schema
    from backend.materialized_views_remaining import REMAINING_GOLD_SQL

    def _fetch():
        gold = REMAINING_GOLD_SQL.get(name)
        if gold:
            from backend.db import source_label_filter_clause
            cat, sch = get_catalog_schema()
            sql = gold.format(
                catalog=cat, schema=sch,
                ws_filter=ws_clause + source_label_filter_clause(gold),
            )
            return execute_query(apply_mv_overrides(sql, cat, sch), params)
        if warehouse_view_fallback() and live_sql:
            return execute_query(live_sql, params)
        return []

    return dual_read(name, pg_tmpl, params, ws_clause, extra_format=extra_format, fetch_delta=_fetch)


def _run_shadow(name: str, pg_sql: str, pg_params: dict, key_cols: list[str], delta_result: list[dict]) -> None:
    """Background: read Lakebase, compare to the served Delta rows, log the verdict."""
    try:
        from backend.db import lakebase_query
        from backend.lakebase_parity import _write_parity_log, compare_result_sets
        lb_rows = lakebase_query(pg_sql, pg_params)
        report = compare_result_sets(delta_result, lb_rows, key_cols=key_cols)
        report["name"] = name
        report["checked_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        level = logging.INFO if report["verdict"] == "PASS" else logging.WARNING
        logger.log(level, "lakebase_read shadow[%s]: %s delta=%d lakebase=%d hard=%d miss=%d extra=%d",
                   name, report["verdict"], report["delta_rows"], report["lakebase_rows"],
                   report["hard_mismatches"], report["missing_in_lakebase"], report["extra_in_lakebase"])
        _write_parity_log(f"read:{name}", report)
    except Exception as e:
        logger.debug("lakebase_read shadow[%s] failed (non-fatal): %s", name, e)
