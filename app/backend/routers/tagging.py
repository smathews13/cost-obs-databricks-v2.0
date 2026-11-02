"""Tagging Hub API endpoints for untagged resource discovery and tag cost attribution."""

import asyncio
import logging
import threading as _threading
import time as _time
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
    get_workspace_client,
    local_source_is_selected,
    source_scope_unavailable,
)
from backend.materialized_views import MV_TAG_STATS, MV_TAGGING_SUMMARY
from backend.materialized_views_extra import MV_TAGGING_COVERAGE
from backend.routers.billing import _check_mv_available, _exec_mv

router = APIRouter()
logger = logging.getLogger(__name__)

# Guard: skip system.lakeflow enriched queries for 2h after a timeout (matches TTLCache TTL).
# Prevents every request from burning 45s waiting for a timeout when lakeflow is unavailable.
# Circuit-breaker state is shared across worker threads — guard with a lock so we never
# read a torn (available, last_failure) pair or flip-flop the flag on interleaved writes.
_lakeflow_lock = _threading.Lock()
_lakeflow_available: bool = True
_lakeflow_last_failure: float = 0.0
_LAKEFLOW_RETRY_INTERVAL: float = 7200.0


def _lakeflow_in_cooldown() -> bool:
    with _lakeflow_lock:
        return (not _lakeflow_available) and (_time.time() - _lakeflow_last_failure) < _LAKEFLOW_RETRY_INTERVAL


def _lakeflow_mark_success() -> None:
    global _lakeflow_available
    with _lakeflow_lock:
        _lakeflow_available = True


def _lakeflow_mark_failure() -> None:
    global _lakeflow_available, _lakeflow_last_failure
    with _lakeflow_lock:
        _lakeflow_available = False
        _lakeflow_last_failure = _time.time()


def get_default_start_date() -> str:
    """Get default start date (last 30 days)."""
    return (date.today() - timedelta(days=30)).isoformat()


def get_default_end_date() -> str:
    """Get default end date (today)."""
    return date.today().isoformat()


# SQL Queries for Tagging Hub

TAGGING_SUMMARY = """
WITH usage_with_tags AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    u.usage_metadata,
    u.custom_tags,
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
  SUM(CASE WHEN has_tags THEN usage_quantity * price_per_dbu ELSE 0 END) as tagged_spend,
  SUM(CASE WHEN NOT has_tags THEN usage_quantity * price_per_dbu ELSE 0 END) as untagged_spend,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT CASE WHEN has_tags THEN workspace_id END) as tagged_workspaces,
  COUNT(DISTINCT CASE WHEN NOT has_tags THEN workspace_id END) as untagged_workspaces
FROM usage_with_tags
"""

UNTAGGED_CLUSTERS = """
SELECT
  u.usage_metadata.cluster_id AS cluster_id,
  CAST(NULL AS STRING) AS cluster_name,
  CAST(NULL AS STRING) AS cluster_source,
  CAST(NULL AS STRING) AS owner,
  MAX(u.workspace_id) as workspace_id,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.usage_metadata.cluster_id IS NOT NULL
  AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
GROUP BY u.usage_metadata.cluster_id
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_CLUSTERS_ENRICHED = """
WITH cluster_info AS (
  SELECT
    cluster_id,
    MAX(cluster_name) as cluster_name,
    MAX(cluster_source) as cluster_source,
    MAX(owned_by) as owned_by
  FROM system.compute.clusters
  WHERE delete_time IS NULL
  GROUP BY cluster_id
)
SELECT
  u.usage_metadata.cluster_id AS cluster_id,
  COALESCE(ci.cluster_name, u.usage_metadata.cluster_id) AS cluster_name,
  ci.cluster_source,
  ci.owned_by as owner,
  MAX(u.workspace_id) as workspace_id,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
LEFT JOIN cluster_info ci ON u.usage_metadata.cluster_id = ci.cluster_id
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.usage_metadata.cluster_id IS NOT NULL
  AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
GROUP BY u.usage_metadata.cluster_id, ci.cluster_name, ci.cluster_source, ci.owned_by
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_JOBS = """
WITH job_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.usage_metadata.job_id AS job_id,
    u.usage_metadata.job_name AS job_name,
    u.sku_name,
    u.usage_quantity,
    u.custom_tags,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.usage_metadata.job_id IS NOT NULL
    AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
)
SELECT
  job_id,
  MAX(job_name) as job_name,
  MAX(workspace_id) as workspace_id,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT usage_date) as days_active
FROM job_usage
GROUP BY job_id
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_JOBS_ENRICHED = """
WITH job_usage AS (
  SELECT
    u.usage_metadata.job_id AS job_id,
    MAX(u.workspace_id) AS workspace_id,
    MAX(u.usage_metadata.job_name) AS usage_job_name,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
    COUNT(DISTINCT u.usage_date) AS days_active
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.usage_metadata.job_id IS NOT NULL
    AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
  GROUP BY u.usage_metadata.job_id
),
-- system.lakeflow.jobs is SCD Type 2 (~2.9x rows per job). Dedupe to latest live
-- row per job so the LEFT JOIN doesn't fan out. job_id is STRING on both sides;
-- avoid TRY_CAST-to-BIGINT which broke data-skipping and forced a shuffle-cast.
-- LEFT SEMI JOIN restricts the dedup to jobs that actually show up in the usage
-- window — on accounts with a large historical catalog but few active jobs,
-- this drops the window sort from 100k+ rows to a few thousand.
jobs_latest AS (
  SELECT lj.job_id, lj.name
  FROM system.lakeflow.jobs lj
  LEFT SEMI JOIN job_usage ju ON lj.job_id = ju.job_id
  WHERE lj.delete_time IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY lj.job_id ORDER BY lj.change_time DESC) = 1
)
SELECT
  ju.job_id,
  COALESCE(jl.name, ju.usage_job_name, ju.job_id) AS job_name,
  ju.workspace_id,
  ju.total_dbus,
  ju.total_spend,
  ju.days_active
FROM job_usage ju
LEFT JOIN jobs_latest jl ON ju.job_id = jl.job_id
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_PIPELINES = """
SELECT
  u.usage_metadata.dlt_pipeline_id AS pipeline_id,
  CAST(NULL AS STRING) AS pipeline_name,
  MAX(u.workspace_id) as workspace_id,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
  AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
GROUP BY u.usage_metadata.dlt_pipeline_id
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_PIPELINES_ENRICHED = """
WITH pipeline_usage AS (
  SELECT
    u.usage_metadata.dlt_pipeline_id AS pipeline_id,
    MAX(u.workspace_id) AS workspace_id,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
    COUNT(DISTINCT u.usage_date) AS days_active
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
  GROUP BY u.usage_metadata.dlt_pipeline_id
),
-- Dedupe SCD2 pipelines (~2x rows per pipeline) to latest live row per pipeline.
-- LEFT SEMI JOIN restricts the dedup to pipelines that actually show up in the
-- usage window — same optimization as jobs_latest above.
pipelines_latest AS (
  SELECT lp.pipeline_id, lp.name
  FROM system.lakeflow.pipelines lp
  LEFT SEMI JOIN pipeline_usage pu ON lp.pipeline_id = pu.pipeline_id
  WHERE lp.delete_time IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY lp.pipeline_id ORDER BY lp.change_time DESC) = 1
)
SELECT
  pu.pipeline_id,
  COALESCE(pl.name, pu.pipeline_id) AS pipeline_name,
  pu.workspace_id,
  pu.total_dbus,
  pu.total_spend,
  pu.days_active
FROM pipeline_usage pu
LEFT JOIN pipelines_latest pl ON pu.pipeline_id = pl.pipeline_id
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_WAREHOUSES = """
SELECT
  u.usage_metadata.warehouse_id AS warehouse_id,
  CAST(NULL AS STRING) AS warehouse_name,
  MAX(u.workspace_id) as workspace_id,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.usage_metadata.warehouse_id IS NOT NULL
  AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
GROUP BY u.usage_metadata.warehouse_id
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_WAREHOUSES_ENRICHED = """
WITH warehouse_info AS (
  SELECT
    warehouse_id,
    MAX(warehouse_name) as warehouse_name
  FROM system.compute.warehouses
  WHERE delete_time IS NULL
  GROUP BY warehouse_id
)
SELECT
  u.usage_metadata.warehouse_id AS warehouse_id,
  COALESCE(wi.warehouse_name, u.usage_metadata.warehouse_id) AS warehouse_name,
  MAX(u.workspace_id) as workspace_id,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
LEFT JOIN warehouse_info wi ON u.usage_metadata.warehouse_id = wi.warehouse_id
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.usage_metadata.warehouse_id IS NOT NULL
  AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
GROUP BY u.usage_metadata.warehouse_id, wi.warehouse_name
ORDER BY total_spend DESC
LIMIT 1000
"""

UNTAGGED_ENDPOINTS = """
WITH endpoint_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.usage_metadata.endpoint_name AS endpoint_name,
    u.sku_name,
    u.usage_quantity,
    u.custom_tags,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.usage_metadata.endpoint_name IS NOT NULL
    AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
)
SELECT
  endpoint_name,
  MAX(workspace_id) as workspace_id,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT usage_date) as days_active
FROM endpoint_usage
GROUP BY endpoint_name
ORDER BY total_spend DESC
LIMIT 1000
"""

COST_BY_TAG = """
WITH tagged_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    u.custom_tags,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.custom_tags IS NOT NULL
    AND size(u.custom_tags) > 0
),
exploded_tags AS (
  SELECT
    usage_date,
    workspace_id,
    usage_quantity,
    price_per_dbu,
    tag_key,
    tag_value
  FROM tagged_usage
  LATERAL VIEW EXPLODE(custom_tags) t AS tag_key, tag_value
)
SELECT
  tag_key,
  tag_value,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT workspace_id) as workspace_count,
  COUNT(DISTINCT usage_date) as days_active
FROM exploded_tags
GROUP BY tag_key, tag_value
ORDER BY total_spend DESC
LIMIT 1000
"""

TAG_STATS = """
WITH tagged_usage AS (
  SELECT
    u.usage_date,
    u.usage_quantity,
    u.custom_tags,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.custom_tags IS NOT NULL
    AND size(u.custom_tags) > 0
),
exploded_tags AS (
  SELECT
    usage_date,
    usage_quantity,
    price_per_dbu,
    tag_key,
    tag_value
  FROM tagged_usage
  LATERAL VIEW EXPLODE(custom_tags) t AS tag_key, tag_value
),
daily_agg AS (
  SELECT
    usage_date,
    SUM(usage_quantity * price_per_dbu) AS daily_spend,
    COUNT(DISTINCT CONCAT(tag_key, ':', tag_value)) AS daily_tag_count
  FROM exploded_tags
  GROUP BY usage_date
)
SELECT
  (SELECT COUNT(DISTINCT CONCAT(tag_key, ':', tag_value)) FROM exploded_tags) AS total_tag_count,
  AVG(CASE WHEN daily_tag_count > 0 THEN daily_spend / daily_tag_count ELSE NULL END) AS avg_cost_per_tag
FROM daily_agg
"""

COST_BY_TAG_KEY = """
WITH tagged_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    u.custom_tags,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.custom_tags IS NOT NULL
    AND size(u.custom_tags) > 0
),
exploded_tags AS (
  SELECT
    usage_date,
    workspace_id,
    usage_quantity,
    price_per_dbu,
    tag_key
  FROM tagged_usage
  LATERAL VIEW EXPLODE(custom_tags) t AS tag_key, tag_value
)
SELECT
  tag_key,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT workspace_id) as workspace_count
FROM exploded_tags
GROUP BY tag_key
ORDER BY total_spend DESC
LIMIT 50
"""

TOP_OBJECTS_BY_TAG = """
WITH tagged_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.billing_origin_product,
    u.usage_quantity,
    u.usage_metadata,
    u.custom_tags,
    COALESCE(p.pricing.default, 0) as price_per_dbu
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.custom_tags IS NOT NULL
    AND size(u.custom_tags) > 0
),
filtered AS (
  SELECT
    usage_date,
    workspace_id,
    sku_name,
    billing_origin_product,
    usage_quantity,
    usage_metadata,
    price_per_dbu
  FROM tagged_usage
  LATERAL VIEW EXPLODE(custom_tags) t AS tk, tv
  WHERE tk = :tag_key AND tv = :tag_value
),
objects AS (
  SELECT
    COALESCE(
      usage_metadata.cluster_id,
      usage_metadata.warehouse_id,
      usage_metadata.endpoint_name,
      usage_metadata.dlt_pipeline_id,
      CAST(usage_metadata.job_id AS STRING),
      sku_name
    ) as object_id,
    CASE
      WHEN usage_metadata.cluster_id IS NOT NULL THEN 'Cluster'
      WHEN usage_metadata.warehouse_id IS NOT NULL THEN 'SQL Warehouse'
      WHEN usage_metadata.endpoint_name IS NOT NULL THEN 'Serving Endpoint'
      WHEN usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'Pipeline'
      WHEN usage_metadata.job_id IS NOT NULL THEN 'Job'
      ELSE 'Other'
    END as object_type,
    COALESCE(
      usage_metadata.cluster_id,
      usage_metadata.warehouse_id,
      usage_metadata.endpoint_name,
      usage_metadata.dlt_pipeline_id,
      usage_metadata.job_name,
      sku_name
    ) as object_name,
    MAX(workspace_id) as workspace_id,
    SUM(usage_quantity) as total_dbus,
    SUM(usage_quantity * price_per_dbu) as total_spend,
    COUNT(DISTINCT usage_date) as days_active
  FROM filtered
  GROUP BY 1, 2, 3
)
SELECT * FROM objects
ORDER BY total_spend DESC
LIMIT 5
"""

AVAILABLE_TAGS = """
WITH tagged_usage AS (
  SELECT
    u.custom_tags
  FROM system.billing.usage u
  WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), 90)
    AND u.custom_tags IS NOT NULL
    AND size(u.custom_tags) > 0
),
exploded_tags AS (
  SELECT DISTINCT
    tag_key,
    tag_value
  FROM tagged_usage
  LATERAL VIEW EXPLODE(custom_tags) t AS tag_key, tag_value
)
SELECT
  tag_key,
  COLLECT_SET(tag_value) as tag_values
FROM exploded_tags
GROUP BY tag_key
ORDER BY tag_key
"""

TAG_COVERAGE_TIMESERIES = """
WITH usage_with_tags AS (
  SELECT
    u.usage_date,
    u.usage_quantity,
    u.custom_tags,
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
  usage_date,
  SUM(CASE WHEN has_tags THEN usage_quantity * price_per_dbu ELSE 0 END) as tagged_spend,
  SUM(CASE WHEN NOT has_tags THEN usage_quantity * price_per_dbu ELSE 0 END) as untagged_spend
FROM usage_with_tags
GROUP BY usage_date
ORDER BY usage_date
"""


@router.get("/summary")
async def get_tagging_summary(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get tagging coverage summary."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(_exec_mv, MV_TAGGING_COVERAGE, params, "")
    if not results:
        from backend.routers.billing import _live_if_allowed
        results = await asyncio.to_thread(_live_if_allowed, TAGGING_SUMMARY, params)

    if not results:
        return {
            "tagged_spend": 0,
            "untagged_spend": 0,
            "total_spend": 0,
            "tagged_percentage": 0,
            "untagged_percentage": 0,
            "tagged_workspaces": 0,
            "untagged_workspaces": 0,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    row = results[0]
    total_spend = float(row.get("total_spend") or 0)
    tagged_spend = float(row.get("tagged_spend") or 0)
    untagged_spend = float(row.get("untagged_spend") or 0)

    return {
        "tagged_spend": tagged_spend,
        "untagged_spend": untagged_spend,
        "total_spend": total_spend,
        "tagged_percentage": (tagged_spend / total_spend * 100) if total_spend > 0 else 0,
        "untagged_percentage": (untagged_spend / total_spend * 100) if total_spend > 0 else 0,
        "tagged_workspaces": row.get("tagged_workspaces") or 0,
        "untagged_workspaces": row.get("untagged_workspaces") or 0,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/untagged/clusters")
async def get_untagged_clusters(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get untagged clusters with spend."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_UNTAGGED, gold_read
    results = await asyncio.to_thread(
        gold_read, "untagged_clusters", PG_UNTAGGED, {**params, "object_type": "cluster"}, "",
        UNTAGGED_CLUSTERS, extra_format={"id_col": "cluster_id", "name_col": "cluster_name"},
    )

    clusters = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        clusters.append(
            {
                "cluster_id": row.get("cluster_id"),
                "cluster_name": row.get("cluster_name"),
                "cluster_source": row.get("cluster_source"),
                "owner": row.get("owner"),
                "workspace_id": str(row.get("workspace_id")),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "days_active": row.get("days_active") or 0,
            }
        )

    return {
        "clusters": clusters,
        "total_spend": total_spend,
        "count": len(clusters),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/untagged/jobs")
async def get_untagged_jobs(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get untagged jobs with spend."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_UNTAGGED, gold_read
    results = await asyncio.to_thread(
        gold_read, "untagged_jobs", PG_UNTAGGED, {**params, "object_type": "job"}, "",
        UNTAGGED_JOBS, extra_format={"id_col": "job_id", "name_col": "job_name"},
    )

    jobs = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        jobs.append(
            {
                "job_id": row.get("job_id"),
                "job_name": row.get("job_name"),
                "workspace_id": str(row.get("workspace_id")),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "days_active": row.get("days_active") or 0,
            }
        )

    return {
        "jobs": jobs,
        "total_spend": total_spend,
        "count": len(jobs),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


_tagging_pipeline_names_cache: dict[str, str] | None = None
_tagging_pipeline_names_ts: float = 0


def _get_tagging_pipeline_names() -> dict[str, str]:
    """Get pipeline ID → name mapping. Tries system.lakeflow.pipelines, falls back to SDK. Cached 1 hour."""
    global _tagging_pipeline_names_cache, _tagging_pipeline_names_ts
    now = _time.monotonic()
    ttl = cache_ttls.PIPELINE_NAMES
    if _tagging_pipeline_names_cache is not None and (now - _tagging_pipeline_names_ts) < ttl:
        return _tagging_pipeline_names_cache

    try:
        from backend.lakebase_read import warehouse_view_fallback
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
            if names:
                logger.info(f"Tagging pipeline names from system table: {len(names)} found")
                _tagging_pipeline_names_cache = names
                _tagging_pipeline_names_ts = now
                return names
    except Exception as e:
        logger.warning(f"system.lakeflow.pipelines not accessible for tagging enrichment: {type(e).__name__}: {e}")

    try:
        w = get_workspace_client()
        pipeline_names: dict[str, str] = {}
        for p in w.pipelines.list_pipelines():
            if p.pipeline_id and p.name:
                pipeline_names[p.pipeline_id] = p.name
        logger.info(f"Tagging pipeline names from SDK: {len(pipeline_names)} found")
        _tagging_pipeline_names_cache = pipeline_names
        _tagging_pipeline_names_ts = now
        return pipeline_names
    except Exception as e:
        logger.warning(f"Could not list pipelines via SDK for tagging: {type(e).__name__}: {e}")
        return {}


def _enrich_pipeline_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Fill in pipeline_name where it is NULL or equals the UUID pipeline_id."""
    if not rows:
        return rows
    try:
        needs_enrichment = any(
            not r.get("pipeline_name") or r.get("pipeline_name") == r.get("pipeline_id")
            for r in rows
        )
        if not needs_enrichment:
            return rows
        names = _get_tagging_pipeline_names()
        if not names:
            return rows
        for row in rows:
            pid = row.get("pipeline_id")
            if pid and pid in names:
                row["pipeline_name"] = names[pid]
    except Exception as e:
        logger.warning(f"Pipeline name enrichment failed: {type(e).__name__}: {e}")
    return rows


@router.get("/untagged/pipelines")
async def get_untagged_pipelines(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get untagged SDP pipelines with spend."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_UNTAGGED, gold_read
    results = await asyncio.to_thread(
        gold_read, "untagged_pipelines", PG_UNTAGGED, {**params, "object_type": "pipeline"}, "",
        UNTAGGED_PIPELINES, extra_format={"id_col": "pipeline_id", "name_col": "pipeline_name"},
    )
    results = await asyncio.to_thread(_enrich_pipeline_rows, results)

    pipelines = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        pipelines.append(
            {
                "pipeline_id": row.get("pipeline_id"),
                "pipeline_name": row.get("pipeline_name"),
                "workspace_id": str(row.get("workspace_id")),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "days_active": row.get("days_active") or 0,
            }
        )

    return {
        "pipelines": pipelines,
        "total_spend": total_spend,
        "count": len(pipelines),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/untagged/warehouses")
async def get_untagged_warehouses(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get untagged SQL warehouses with spend."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_UNTAGGED, gold_read
    results = await asyncio.to_thread(
        gold_read, "untagged_warehouses", PG_UNTAGGED, {**params, "object_type": "warehouse"}, "",
        UNTAGGED_WAREHOUSES, extra_format={"id_col": "warehouse_id", "name_col": "warehouse_name"},
    )

    warehouses = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        warehouses.append(
            {
                "warehouse_id": row.get("warehouse_id"),
                "warehouse_name": row.get("warehouse_name"),
                "workspace_id": str(row.get("workspace_id")),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "days_active": row.get("days_active") or 0,
            }
        )

    return {
        "warehouses": warehouses,
        "total_spend": total_spend,
        "count": len(warehouses),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/untagged/endpoints")
async def get_untagged_endpoints(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get untagged model serving endpoints with spend."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_UNTAGGED, gold_read
    results = await asyncio.to_thread(
        gold_read, "untagged_endpoints", PG_UNTAGGED, {**params, "object_type": "endpoint"}, "",
        UNTAGGED_ENDPOINTS, extra_format={"id_col": "endpoint_name", "name_col": "endpoint_name"},
    )

    endpoints = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        endpoints.append(
            {
                "endpoint_name": row.get("endpoint_name"),
                "workspace_id": str(row.get("workspace_id")),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "days_active": row.get("days_active") or 0,
            }
        )

    return {
        "endpoints": endpoints,
        "total_spend": total_spend,
        "count": len(endpoints),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/cost-by-tag")
async def get_cost_by_tag(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get cost breakdown by tag key/value pairs."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(execute_query, COST_BY_TAG, params)

    tags = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        tags.append(
            {
                "tag_key": row.get("tag_key"),
                "tag_value": row.get("tag_value"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "workspace_count": row.get("workspace_count") or 0,
                "days_active": row.get("days_active") or 0,
            }
        )

    # Calculate percentages
    for tag in tags:
        tag["percentage"] = (tag["total_spend"] / total_spend * 100) if total_spend > 0 else 0

    return {
        "tags": tags,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/top-objects-by-tag")
async def get_top_objects_by_tag(
    tag_key: str = Query(description="Tag key to drill into"),
    tag_value: str = Query(description="Tag value to drill into"),
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get top 5 most expensive objects for a specific tag key/value pair."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
        "tag_key": tag_key,
        "tag_value": tag_value,
    }

    if not local_source_is_selected():
        return {
            "available": False,
            "reason": "shared_scope_unsupported",
            "reason_detail": "The selected shared source does not publish tag object detail.",
            "error_code": "SOURCE_SCOPE_UNSUPPORTED",
            "objects": [],
            "tag_key": tag_key,
            "tag_value": tag_value,
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }

    results = await asyncio.to_thread(execute_query, TOP_OBJECTS_BY_TAG, params)

    objects = []
    for row in results:
        objects.append(
            {
                "object_id": row.get("object_id"),
                "object_type": row.get("object_type"),
                "object_name": row.get("object_name"),
                "workspace_id": str(row.get("workspace_id")) if row.get("workspace_id") else None,
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": float(row.get("total_spend") or 0),
                "days_active": row.get("days_active") or 0,
            }
        )

    return {
        "objects": objects,
        "tag_key": tag_key,
        "tag_value": tag_value,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/cost-by-tag-key")
async def get_cost_by_tag_key(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get cost breakdown by tag key (aggregated across all values)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    results = await asyncio.to_thread(execute_query, COST_BY_TAG_KEY, params)

    tag_keys = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        tag_keys.append(
            {
                "tag_key": row.get("tag_key"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "workspace_count": row.get("workspace_count") or 0,
            }
        )

    # Calculate percentages
    for key in tag_keys:
        key["percentage"] = (key["total_spend"] / total_spend * 100) if total_spend > 0 else 0

    return {
        "tag_keys": tag_keys,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/available-tags")
async def get_available_tags() -> dict[str, Any]:
    """Get all available tag keys and their values from the Databricks account.

    Returns tag keys with their associated values for use in tagging resources.
    """
    try:
        from backend.lakebase_read import PG_AVAILABLE_TAGS, gold_read
        params = {
            "start_date": get_default_start_date(),
            "end_date": get_default_end_date(),
        }
        results = await asyncio.to_thread(
            gold_read, "available_tags", PG_AVAILABLE_TAGS, params, "", AVAILABLE_TAGS
        )

        tags = {}
        for row in results:
            tag_key = row.get("tag_key")
            raw_values = row.get("tag_values")
            tag_values = list(raw_values) if raw_values is not None else []
            if tag_key:
                tags[tag_key] = sorted(tag_values)

        return {
            "tags": tags,
            "count": len(tags),
        }
    except Exception as e:
        logger.error(f"Error fetching available tags: {e}")
        return {"tags": {}, "count": 0}


@router.get("/coverage-timeseries")
async def get_tag_coverage_timeseries(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get tag coverage over time (tagged vs untagged spend)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_TAG_COVERAGE_TIMESERIES, dual_read, warehouse_view_fallback

    def _tag_ts():
        live = (lambda: execute_query(TAG_COVERAGE_TIMESERIES, params)) if warehouse_view_fallback() else None
        return dual_read(
            "tag_coverage_timeseries", PG_TAG_COVERAGE_TIMESERIES, params, "",
            fetch_delta=live,
        )

    results = await asyncio.to_thread(_tag_ts)

    timeseries = []
    for row in results:
        timeseries.append(
            {
                "date": str(row.get("usage_date")),
                "Tagged": float(row.get("tagged_spend") or 0),
                "Untagged": float(row.get("untagged_spend") or 0),
            }
        )

    return {
        "timeseries": timeseries,
        "categories": ["Tagged", "Untagged"],
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/dashboard-bundle")
async def get_tagging_dashboard_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """Get all tagging dashboard data in a single request."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    blocked = source_scope_unavailable(
        {"daily_tag_summary", "daily_tag_coverage"},
        "The selected shared source does not publish the Tagging aggregates.",
    )
    if blocked:
        empty_untagged = {"items": [], "total_spend": 0, "count": 0}
        return {
            **blocked,
            "summary": {},
            "untagged": {
                "clusters": empty_untagged,
                "jobs": empty_untagged,
                "pipelines": empty_untagged,
                "warehouses": empty_untagged,
                "endpoints": empty_untagged,
            },
            "cost_by_tag": {"tags": [], "total_spend": 0},
            "timeseries": {"timeseries": [], "categories": ["Tagged", "Untagged"]},
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
    _dkey = bundle_cache_key("tagging:dashboard-bundle", params["start_date"], params["end_date"], id_list)
    if (_dcached := delta_cache_get(_dkey)) is not None:
        return _dcached
    ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    def _ws(sql: str) -> str:
        return wf.inject_ws_filter(sql, ws_clause)

    jobs_ok = [True]
    pipelines_ok = [True]

    def query_with_fallback(enriched_sql: str, fallback_sql: str, query_params: dict) -> list[dict[str, Any]]:
        """Try enriched query (with system.compute tables), fall back to billing-only."""
        try:
            return execute_query(enriched_sql, query_params)
        except Exception as e:
            logger.warning(f"Enriched query failed ({e}), falling back to billing-only")
            return execute_query(fallback_sql, query_params)

    def summary_query(query_params: dict) -> list[dict[str, Any]]:
        """MV fast path for TAGGING_SUMMARY. Falls back to raw system.billing.usage
        scan on failure.

        The raw TAGGING_SUMMARY scans every row in system.billing.usage across the
        window and computes has_tags per row — expensive on large accounts and the
        source of the 75s bundle timeout we saw. The MV path uses daily_usage_summary
        (already aggregated per day/workspace) for the total and only scans billing.usage
        rows that have custom_tags for the tagged portion.
        """
        if _check_mv_available():
            try:
                mv_ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
                rows = _exec_mv(MV_TAGGING_COVERAGE, query_params, mv_ws_clause)
                if rows:
                    return rows
                from backend.lakebase_read import warehouse_view_fallback
                if not warehouse_view_fallback():
                    return rows or []
                catalog, schema = get_catalog_schema()
                mv_ws_filter = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
                mv_ws_clause_billing = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
                mv_sql = MV_TAGGING_SUMMARY.format(
                    catalog=catalog,
                    schema=schema,
                    ws_filter=mv_ws_filter,
                    ws_clause_billing=mv_ws_clause_billing,
                )
                mv_sql = apply_mv_overrides(mv_sql, catalog, schema)
                return execute_query(mv_sql, query_params)
            except Exception as e:
                logger.warning("tagging_summary MV path failed (%s); falling back to raw scan", type(e).__name__)
        from backend.lakebase_read import warehouse_view_fallback
        if not warehouse_view_fallback():
            return []
        return execute_query(_ws(TAGGING_SUMMARY), query_params)

    def tag_stats_query(query_params: dict) -> list[dict[str, Any]]:
        """MV fast path for tag_stats. Falls back to raw system.billing.usage scan on failure.

        The raw TAG_STATS query re-explodes custom_tags across the full billing date range
        on every request, which was saturating the 90s bundle deadline on large accounts.
        daily_tag_summary already stores per-day (tag_key, tag_value, spend) rows so we can
        derive both KPIs in milliseconds when the MV is available.
        """
        if _check_mv_available():
            try:
                mv_ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
                return _exec_mv(MV_TAG_STATS, query_params, mv_ws_clause)
            except Exception as e:
                logger.warning("tag_stats MV path failed (%s); falling back to raw scan", type(e).__name__)
        from backend.lakebase_read import warehouse_view_fallback
        if not warehouse_view_fallback():
            return []
        return execute_query(_ws(TAG_STATS), query_params)

    def lakeflow_query(name: str, enriched_sql: str, fallback_sql: str, query_params: dict, flag: list) -> list[dict[str, Any]]:
        """Try lakeflow-enriched query with 45s timeout; fall back to billing-only on failure.

        `name` (e.g. "lakeflow_jobs" / "lakeflow_pipelines") is used as the label in
        execute_queries_parallel so the timeout log message identifies which query stalled.
        """
        if _lakeflow_in_cooldown():
            flag[0] = False
            return execute_query(fallback_sql, query_params)
        try:
            result = execute_queries_parallel([(name, lambda: execute_query(enriched_sql, query_params))], timeout=45.0)
            if result.get(name) is not None:
                _lakeflow_mark_success()
                return result[name]
        except Exception as e:
            logger.warning(f"{name} failed/timed out ({type(e).__name__}); falling back to billing-only")
        _lakeflow_mark_failure()
        logger.warning("system.lakeflow unavailable; skipping enriched queries for %.0fs", _LAKEFLOW_RETRY_INTERVAL)
        flag[0] = False
        return execute_query(fallback_sql, query_params)

    from backend.lakebase_read import (
        PG_COST_BY_TAG,
        PG_COST_BY_TAG_KEY,
        PG_TAG_COVERAGE_TIMESERIES,
        PG_UNTAGGED,
        dual_read,
        gold_read,
        warehouse_view_fallback,
    )
    live_ok = warehouse_view_fallback()
    mv_ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)

    def _tag_gold(name: str, pg: str, query_params: dict, live_sql: str, ws_pg: str) -> list[dict[str, Any]]:
        live = (lambda: execute_query(live_sql, query_params)) if live_ok else None
        return dual_read(name, pg, query_params, ws_pg, fetch_delta=live)

    def _untagged(gold_name: str, kind: str, id_col: str, name_col: str, live_sql: str) -> list[dict[str, Any]]:
        p = {**params, "object_type": kind}
        return gold_read(
            gold_name,
            PG_UNTAGGED,
            p,
            mv_ws_clause,
            live_sql,
            extra_format={"id_col": id_col, "name_col": name_col},
        )

    queries = [
        ("summary", lambda: summary_query(params)),
        ("tag_stats", lambda: tag_stats_query(params)),
        ("cost_by_tag", lambda: _tag_gold("cost_by_tag", PG_COST_BY_TAG, params, _ws(COST_BY_TAG), mv_ws_clause)),
        ("tag_keys", lambda: _tag_gold("cost_by_tag_key", PG_COST_BY_TAG_KEY, params, _ws(COST_BY_TAG_KEY), mv_ws_clause)),
        ("timeseries", lambda: _tag_gold("tag_coverage_timeseries", PG_TAG_COVERAGE_TIMESERIES, params, _ws(TAG_COVERAGE_TIMESERIES), mv_ws_clause)),
        ("clusters", lambda: _untagged("untagged_clusters", "cluster", "cluster_id", "cluster_name", _ws(UNTAGGED_CLUSTERS))),
        ("jobs", lambda: _untagged("untagged_jobs", "job", "job_id", "job_name", _ws(UNTAGGED_JOBS))),
        ("pipelines", lambda: _untagged("untagged_pipelines", "pipeline", "pipeline_id", "pipeline_name", _ws(UNTAGGED_PIPELINES))),
        ("warehouses", lambda: _untagged("untagged_warehouses", "warehouse", "warehouse_id", "warehouse_name", _ws(UNTAGGED_WAREHOUSES))),
        ("endpoints", lambda: _untagged("untagged_endpoints", "endpoint", "endpoint_name", "endpoint_name", _ws(UNTAGGED_ENDPOINTS))),
    ]

    try:
        results = await asyncio.to_thread(execute_queries_parallel, queries, timeout=90.0)
    except Exception as e:
        logger.error("tagging dashboard-bundle failed: %s", e)
        empty_untagged = {"items": [], "total_spend": 0, "count": 0}
        code = str(getattr(e, "code", "TAGGING_BUNDLE_FAILED"))
        source_unsupported = code == "SOURCE_SCOPE_UNSUPPORTED"
        return {
            "available": False,
            "reason": "shared_scope_unsupported" if source_unsupported else "producer_failed",
            "reason_detail": (
                "The selected shared source does not publish the Tagging aggregates."
                if source_unsupported
                else "Tag coverage data is temporarily unavailable. Retry shortly."
            ),
            "error_code": code,
            "summary": {"tagged_spend": 0, "untagged_spend": 0, "total_spend": 0, "tagged_percentage": 0, "untagged_percentage": 0},
            "untagged": {"clusters": empty_untagged, "jobs": empty_untagged, "pipelines": empty_untagged, "warehouses": empty_untagged, "endpoints": empty_untagged},
            "cost_by_tag": {"tags": [], "total_spend": 0},
            "timeseries": {"timeseries": [], "categories": ["Tagged", "Untagged"]},
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "error": str(e),
        }

    # Format summary
    summary_data = results.get("summary", [])
    if summary_data:
        row = summary_data[0]
        total_spend = float(row.get("total_spend") or 0)
        tagged_spend = float(row.get("tagged_spend") or 0)
        untagged_spend = float(row.get("untagged_spend") or 0)
        summary = {
            "tagged_spend": tagged_spend,
            "untagged_spend": untagged_spend,
            "total_spend": total_spend,
            "tagged_percentage": (tagged_spend / total_spend * 100) if total_spend > 0 else 0,
            "untagged_percentage": (untagged_spend / total_spend * 100) if total_spend > 0 else 0,
        }
    else:
        summary = {"tagged_spend": 0, "untagged_spend": 0, "total_spend": 0, "tagged_percentage": 0, "untagged_percentage": 0}

    # Enrich pipeline names via system table / SDK fallback (handles NULL names and UUID-only rows)
    pipeline_rows = results.get("pipelines")
    if pipeline_rows:
        results["pipelines"] = await asyncio.to_thread(_enrich_pipeline_rows, pipeline_rows)

    # Format untagged resources
    def format_untagged(data, key):
        items = []
        total = 0
        for row in (data or []):
            spend = float(row.get("total_spend") or 0)
            total += spend
            item = {k: row.get(k) for k in row.keys()}
            item["total_spend"] = spend
            item["total_dbus"] = float(row.get("total_dbus") or 0)
            # Ensure workspace_id is a string (may come as int from SQL)
            if "workspace_id" in item and item["workspace_id"] is not None:
                item["workspace_id"] = str(item["workspace_id"])
            items.append(item)
        return {"items": items, "total_spend": total, "count": len(items)}

    # Format tag costs
    tag_data = results.get("cost_by_tag", []) or []
    tag_total = sum(float(r.get("total_spend") or 0) for r in tag_data)
    tags = [
        {
            "tag_key": r.get("tag_key"),
            "tag_value": r.get("tag_value"),
            "total_dbus": float(r.get("total_dbus") or 0),
            "total_spend": float(r.get("total_spend") or 0),
            "percentage": (float(r.get("total_spend") or 0) / tag_total * 100) if tag_total > 0 else 0,
        }
        for r in tag_data
    ]

    # Format timeseries
    ts_data = results.get("timeseries", []) or []
    timeseries = [
        {
            "date": str(r.get("usage_date")),
            "Tagged": float(r.get("tagged_spend") or 0),
            "Untagged": float(r.get("untagged_spend") or 0),
        }
        for r in ts_data
    ]

    _resp = {
        "summary": summary,
        "untagged": {
            "clusters": format_untagged(results.get("clusters"), "clusters"),
            "jobs": format_untagged(results.get("jobs"), "jobs"),
            "pipelines": format_untagged(results.get("pipelines"), "pipelines"),
            "warehouses": format_untagged(results.get("warehouses"), "warehouses"),
            "endpoints": format_untagged(results.get("endpoints"), "endpoints"),
        },
        "cost_by_tag": {"tags": tags, "total_spend": tag_total},
        "timeseries": {"timeseries": timeseries, "categories": ["Tagged", "Untagged"]},
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "avg_cost_per_tag": (lambda v: float(v) if v is not None else None)((results.get("tag_stats") or [{}])[0].get("avg_cost_per_tag")),
        "total_tag_count": (lambda v: int(v) if v is not None else None)((results.get("tag_stats") or [{}])[0].get("total_tag_count")),
        "lakeflow_available": jobs_ok[0] and pipelines_ok[0],
        "enrichment_note": (
            None if (jobs_ok[0] and pipelines_ok[0]) else
            "Job names may be incomplete — Lakeflow enrichment was unavailable." if (not jobs_ok[0] and pipelines_ok[0]) else
            "Pipeline names may be incomplete — Lakeflow enrichment was unavailable." if (jobs_ok[0] and not pipelines_ok[0]) else
            "Job and pipeline names may be incomplete — Lakeflow enrichment was unavailable."
        ),
    }
    delta_cache_put(_dkey, "tagging:dashboard-bundle", _resp, ttl_seconds=600 if id_list else 1800)
    return _resp
