"""AI/ML 360 API endpoints."""

import asyncio
import logging
import threading
import time as _time
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from backend import cache_ttls
from backend import workspace_filter as wf
from backend.db import (
    bundle_cache_key,
    delta_cache_get,
    delta_cache_put,
    execute_queries_parallel,
    execute_query,
    source_label_filter_clause,
    source_scope_unavailable,
)


def _aiml_failure_payload(
    params: dict[str, str],
    error_code: str,
) -> dict[str, Any]:
    source_unsupported = error_code == "SOURCE_SCOPE_UNSUPPORTED"
    unavailable = {
        "available": False,
        "reason": "shared_scope_unsupported" if source_unsupported else "producer_failed",
        "reason_detail": (
            "The selected shared source does not publish AI/ML cost detail."
            if source_unsupported
            else "AI/ML data is temporarily unavailable. Retry shortly."
        ),
        "error_code": error_code,
    }
    return {
        **unavailable,
        "summary": {**unavailable, "total_dbus": 0, "total_spend": 0},
        "providers": {**unavailable, "providers": [], "total_spend": 0},
        "endpoints": {**unavailable, "endpoints": [], "total_spend": 0},
        "categories": {**unavailable, "categories": [], "total_spend": 0},
        "timeseries": {**unavailable, "timeseries": [], "categories": []},
        "models": {**unavailable, "models": []},
        "ml_clusters": {**unavailable, "clusters": []},
        "agent_bricks": {**unavailable, "agents": []},
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _aiml_rows_empty(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    if len(rows) != 1:
        return False
    row = rows[0]
    spend = row.get("total_spend")
    if spend is None:
        spend = row.get("total_usd")
    try:
        return float(spend or 0) == 0.0
    except (TypeError, ValueError):
        return True


def _gold_then_live(name: str, pg_tmpl: str, params: dict, live_sql: str, ws_clause: str = "") -> list[dict[str, Any]]:
    """Postgres first; warehouse *gold* (daily_sku / daily_aiml_endpoints) on miss.

    `live_sql` is only used when Lakebase serving is off — those queries scan
    system.billing.usage and must not run for ordinary dashboard views.
    """
    from backend.db import apply_mv_overrides, get_catalog_schema
    from backend.lakebase_read import dual_read, warehouse_view_fallback
    from backend.materialized_views_extra import AIML_GOLD_SQL

    def _fetch():
        gold = AIML_GOLD_SQL.get(name)
        if gold:
            cat, sch = get_catalog_schema()
            sql = gold.format(
                catalog=cat, schema=sch,
                ws_filter=ws_clause + source_label_filter_clause(gold),
            )
            return execute_query(apply_mv_overrides(sql, cat, sch), params)
        if warehouse_view_fallback():
            return execute_query(live_sql, params)
        return []

    rows = dual_read(name, pg_tmpl, params, ws_clause, fetch_delta=_fetch)
    if _aiml_rows_empty(rows):
        gold_rows = _fetch()
        if gold_rows and not _aiml_rows_empty(gold_rows):
            return gold_rows
    return rows


def query_with_fallback(enriched_sql: str, fallback_sql: str, query_params: dict, label: str = "query") -> list[dict[str, Any]]:
    """Try enriched query first, fall back to simpler query if it fails."""
    try:
        result = execute_query(enriched_sql, query_params)
        if result is not None:
            logger.info(f"{label}: enriched query returned {len(result)} rows")
            return result
        logger.warning(f"{label}: enriched query returned None, falling back")
    except Exception as e:
        logger.warning(f"{label}: enriched query failed ({e}), falling back to billing-only")
    fallback_result = execute_query(fallback_sql, query_params)
    logger.info(f"{label}: fallback query returned {len(fallback_result or [])} rows")
    return fallback_result or []

router = APIRouter()
logger = logging.getLogger(__name__)

_aiml_bundle_inflight: set[str] = set()
_aiml_bundle_inflight_lock = threading.Lock()
_aiml_available: bool = True
_aiml_last_failure: float = 0.0
_AIML_RETRY_INTERVAL: float = 1800.0  # skip new computes for 30 min after a mass timeout


def get_default_start_date() -> str:
    """Get default start date (last 30 days)."""
    return (date.today() - timedelta(days=30)).isoformat()


def get_default_end_date() -> str:
    """Get default end date (today)."""
    return date.today().isoformat()


# SQL Queries for AI/ML cost analysis

AIML_SUMMARY = """
WITH aiml_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    u.usage_metadata,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    CASE
      WHEN u.sku_name LIKE '%ANTHROPIC%' THEN 'FMAPI - Anthropic'
      WHEN u.sku_name LIKE '%OPENAI%' THEN 'FMAPI - OpenAI'
      WHEN u.sku_name LIKE '%GEMINI%' THEN 'FMAPI - Gemini'
      WHEN u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%' THEN 'Serverless Inference'
      WHEN u.product_features.model_serving.offering_type = 'BATCH_INFERENCE' THEN 'Batch Inference'
      WHEN u.billing_origin_product = 'VECTOR_SEARCH' THEN 'AI Search'
      WHEN u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine Tuning'
      WHEN u.billing_origin_product = 'MODEL_SERVING' THEN 'Model Serving'
      ELSE 'Other AI/ML'
    END as category
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND (
      u.billing_origin_product = 'MODEL_SERVING'
      OR u.billing_origin_product = 'VECTOR_SEARCH'
      OR u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
      OR u.sku_name LIKE '%ANTHROPIC%'
      OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%'
      OR u.sku_name LIKE '%INFERENCE%'
      OR u.sku_name LIKE '%FINE_TUNING%'
    )
),
aiml_by_day AS (
  SELECT
    usage_date,
    workspace_id,
    usage_metadata.endpoint_name as endpoint_name,
    SUM(usage_quantity) as daily_dbus,
    SUM(usage_quantity * price_per_dbu) as daily_spend
  FROM aiml_usage
  GROUP BY usage_date, workspace_id, usage_metadata.endpoint_name
),
aiml_summary AS (
  SELECT
    SUM(daily_dbus) as total_dbus,
    SUM(daily_spend) as total_spend,
    COUNT(DISTINCT workspace_id) as workspace_count,
    COUNT(DISTINCT endpoint_name) as endpoint_count,
    COUNT(DISTINCT usage_date) as days_in_range,
    MIN(usage_date) as first_date,
    MAX(usage_date) as last_date
  FROM aiml_by_day
),
aiml_daily_totals AS (
  SELECT usage_date,
    SUM(daily_spend) as daily_cost,
    COUNT(DISTINCT endpoint_name) as daily_endpoints
  FROM aiml_by_day
  WHERE endpoint_name IS NOT NULL
  GROUP BY usage_date
),
aiml_avg_endpoints AS (
  SELECT COALESCE(AVG(daily_endpoints), 0) as avg_endpoints_per_day FROM aiml_daily_totals
),
aiml_avg_cost AS (
  SELECT COALESCE(AVG(daily_cost / NULLIF(daily_endpoints, 0)), 0) as avg_cost_per_endpoint FROM aiml_daily_totals
)
SELECT
  s.total_dbus, s.total_spend, s.workspace_count, s.endpoint_count,
  s.days_in_range, s.first_date, s.last_date,
  e.avg_endpoints_per_day,
  c.avg_cost_per_endpoint
FROM aiml_summary s, aiml_avg_endpoints e, aiml_avg_cost c
"""

FMAPI_PROVIDER_COSTS = """
WITH provider_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    CASE
      WHEN u.sku_name LIKE '%ANTHROPIC%' THEN 'Anthropic'
      WHEN u.sku_name LIKE '%OPENAI%' THEN 'OpenAI'
      WHEN u.sku_name LIKE '%GEMINI%' THEN 'Gemini'
      ELSE 'Other'
    END as provider
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND (
      u.sku_name LIKE '%ANTHROPIC%'
      OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%'
    )
)
SELECT
  provider,
  sku_name,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT workspace_id) as workspace_count
FROM provider_usage
GROUP BY provider, sku_name
ORDER BY total_spend DESC
"""

SERVERLESS_INFERENCE_BY_ENDPOINT = """
WITH inference_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    u.usage_metadata.endpoint_name as endpoint_name,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    CASE
      WHEN u.sku_name LIKE '%LAUNCH%' THEN 'Launch (Scale-from-Zero)'
      ELSE 'Steady State'
    END as cost_type
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
)
SELECT
  COALESCE(iu.endpoint_name, 'UNKNOWN') as endpoint_name,
  iu.workspace_id,
  MAX(wsl.workspace_name) as workspace_name,
  iu.sku_name,
  iu.cost_type,
  SUM(iu.usage_quantity) as total_dbus,
  SUM(iu.usage_quantity * iu.price_per_dbu) as total_spend,
  COUNT(DISTINCT iu.usage_date) as days_active
FROM inference_usage iu
LEFT JOIN system.access.workspaces_latest wsl
  ON CAST(iu.workspace_id AS BIGINT) = CAST(wsl.workspace_id AS BIGINT)
GROUP BY iu.endpoint_name, iu.workspace_id, iu.sku_name, iu.cost_type
ORDER BY total_spend DESC
LIMIT 500
"""

AIML_BY_CATEGORY = """
WITH aiml_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    CASE
      WHEN u.sku_name LIKE '%ANTHROPIC%' THEN 'FMAPI - Anthropic'
      WHEN u.sku_name LIKE '%OPENAI%' THEN 'FMAPI - OpenAI'
      WHEN u.sku_name LIKE '%GEMINI%' THEN 'FMAPI - Gemini'
      WHEN u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%' THEN 'Serverless Inference'
      WHEN u.product_features.model_serving.offering_type = 'BATCH_INFERENCE' THEN 'Batch Inference'
      WHEN u.billing_origin_product = 'VECTOR_SEARCH' THEN 'AI Search'
      WHEN u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine Tuning'
      WHEN u.billing_origin_product = 'MODEL_SERVING' THEN 'Model Serving'
      ELSE 'Other AI/ML'
    END as category
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND (
      u.billing_origin_product = 'MODEL_SERVING'
      OR u.billing_origin_product = 'VECTOR_SEARCH'
      OR u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
      OR u.sku_name LIKE '%ANTHROPIC%'
      OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%'
      OR u.sku_name LIKE '%INFERENCE%'
      OR u.sku_name LIKE '%FINE_TUNING%'
    )
)
SELECT
  category,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT workspace_id) as workspace_count
FROM aiml_usage
GROUP BY category
ORDER BY total_spend DESC
"""

AIML_TIMESERIES = """
WITH aiml_usage AS (
  SELECT
    u.usage_date,
    u.sku_name,
    u.usage_quantity,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    CASE
      WHEN u.sku_name LIKE '%ANTHROPIC%' THEN 'Anthropic'
      WHEN u.sku_name LIKE '%OPENAI%' THEN 'OpenAI'
      WHEN u.sku_name LIKE '%GEMINI%' THEN 'Gemini'
      WHEN u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%' THEN 'Serverless Inference'
      WHEN u.billing_origin_product = 'VECTOR_SEARCH' THEN 'AI Search'
      WHEN u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine Tuning'
      ELSE 'Other Model Serving'
    END as category
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND (
      u.billing_origin_product = 'MODEL_SERVING'
      OR u.billing_origin_product = 'VECTOR_SEARCH'
      OR u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
      OR u.sku_name LIKE '%ANTHROPIC%'
      OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%'
      OR u.sku_name LIKE '%INFERENCE%'
      OR u.sku_name LIKE '%FINE_TUNING%'
    )
)
SELECT
  usage_date,
  category,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend
FROM aiml_usage
GROUP BY usage_date, category
ORDER BY usage_date, category
"""

AIML_TOP_MODELS_ENRICHED = """
WITH cluster_names AS (
  SELECT cluster_id, MAX(cluster_name) as cluster_name
  FROM system.compute.clusters
  GROUP BY cluster_id
),
model_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    CASE
      WHEN u.sku_name LIKE '%FEATURE%' THEN 'Feature Store'
      WHEN u.sku_name LIKE '%TRAINING%' THEN 'Model Training'
      WHEN u.sku_name LIKE '%ANTHROPIC%' OR u.sku_name LIKE '%OPENAI%'
           OR u.sku_name LIKE '%GEMINI%' OR u.sku_name LIKE '%META%'
           OR u.sku_name LIKE '%COHERE%' OR u.sku_name LIKE '%MISTRAL%'
           OR u.sku_name LIKE '%AI21%' OR u.sku_name LIKE '%MOSAIC%'
           OR u.sku_name LIKE '%DBRX%' OR u.sku_name LIKE '%LLAMA%'
           OR u.billing_origin_product = 'FOUNDATION_MODEL_SERVING' THEN 'Foundation Model API'
      ELSE NULL
    END as model_type,
    COALESCE(
      u.usage_metadata.endpoint_name,
      cn.cluster_name,
      CASE WHEN u.usage_metadata.job_id IS NOT NULL
        THEN CONCAT('Job #', CAST(u.usage_metadata.job_id AS STRING))
        ELSE NULL END,
      CAST(u.usage_metadata.cluster_id AS STRING),
      CASE WHEN u.sku_name LIKE '%FEATURE%' THEN 'Feature Store Run'
           WHEN u.sku_name LIKE '%TRAINING%' THEN 'Serverless Fine-tuning'
           ELSE NULL END,
      'Unknown'
    ) as model_name
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  LEFT JOIN cluster_names cn
    ON u.usage_metadata.cluster_id = cn.cluster_id
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND (
      u.sku_name LIKE '%FEATURE%'
      OR u.sku_name LIKE '%TRAINING%'
      OR u.sku_name LIKE '%ANTHROPIC%'
      OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%'
      OR u.sku_name LIKE '%META%'
      OR u.sku_name LIKE '%COHERE%'
      OR u.sku_name LIKE '%MISTRAL%'
      OR u.sku_name LIKE '%AI21%'
      OR u.sku_name LIKE '%MOSAIC%'
      OR u.sku_name LIKE '%DBRX%'
      OR u.sku_name LIKE '%LLAMA%'
      OR u.billing_origin_product = 'FOUNDATION_MODEL_SERVING'
    )
)
SELECT
  model_name,
  model_type,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT usage_date) as days_active,
  COUNT(DISTINCT workspace_id) as workspace_count
FROM model_usage
WHERE model_type IS NOT NULL
GROUP BY model_name, model_type
ORDER BY total_spend DESC
LIMIT 50
"""

AIML_TOP_MODELS_AND_FEATURE_STORES = """
WITH model_usage AS (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.usage_quantity,
    COALESCE(p.pricing.default, 0) as price_per_dbu,
    COALESCE(
      u.usage_metadata.endpoint_name,
      CASE WHEN u.usage_metadata.job_id IS NOT NULL
        THEN CONCAT('Job #', CAST(u.usage_metadata.job_id AS STRING))
        ELSE NULL END,
      CAST(u.usage_metadata.cluster_id AS STRING),
      CASE WHEN u.sku_name LIKE '%FEATURE%' THEN 'Feature Store Run'
           WHEN u.sku_name LIKE '%TRAINING%' THEN 'Serverless Fine-tuning'
           ELSE NULL END,
      'Unknown'
    ) as model_name,
    CASE
      WHEN u.sku_name LIKE '%FEATURE%' THEN 'Feature Store'
      WHEN u.sku_name LIKE '%TRAINING%' THEN 'Model Training'
      WHEN u.sku_name LIKE '%ANTHROPIC%' OR u.sku_name LIKE '%OPENAI%'
           OR u.sku_name LIKE '%GEMINI%' OR u.sku_name LIKE '%META%'
           OR u.sku_name LIKE '%COHERE%' OR u.sku_name LIKE '%MISTRAL%'
           OR u.sku_name LIKE '%AI21%' OR u.sku_name LIKE '%MOSAIC%'
           OR u.sku_name LIKE '%DBRX%' OR u.sku_name LIKE '%LLAMA%'
           OR u.billing_origin_product = 'FOUNDATION_MODEL_SERVING' THEN 'Foundation Model API'
      ELSE NULL
    END as model_type
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
    AND u.cloud = p.cloud
    AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    AND (
      u.sku_name LIKE '%FEATURE%'
      OR u.sku_name LIKE '%TRAINING%'
      OR u.sku_name LIKE '%ANTHROPIC%'
      OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%'
      OR u.sku_name LIKE '%META%'
      OR u.sku_name LIKE '%COHERE%'
      OR u.sku_name LIKE '%MISTRAL%'
      OR u.sku_name LIKE '%AI21%'
      OR u.sku_name LIKE '%MOSAIC%'
      OR u.sku_name LIKE '%DBRX%'
      OR u.sku_name LIKE '%LLAMA%'
      OR u.billing_origin_product = 'FOUNDATION_MODEL_SERVING'
    )
)
SELECT
  model_name,
  model_type,
  SUM(usage_quantity) as total_dbus,
  SUM(usage_quantity * price_per_dbu) as total_spend,
  COUNT(DISTINCT usage_date) as days_active,
  COUNT(DISTINCT workspace_id) as workspace_count
FROM model_usage
WHERE model_type IS NOT NULL
GROUP BY model_name, model_type
ORDER BY total_spend DESC
LIMIT 50
"""

## ML Runtime Clusters
#
# IMPORTANT: ML runtime (data science) clusters CANNOT be identified from billing
# data alone. The SKU name is identical whether the cluster uses a standard runtime
# or an ML runtime (e.g., both use ENTERPRISE_ALL_PURPOSE_COMPUTE). The only way
# to identify ML clusters is via spark_version in system.compute.clusters, which
# contains '-ml-' or '-gpu-' for ML/GPU runtimes.
#
# The enriched query drives FROM system.compute.clusters to find ML clusters first,
# then joins billing for spend. If system.compute.clusters is unavailable, the
# fallback returns nothing (because it's genuinely impossible to identify ML clusters
# from billing alone).

AIML_ML_RUNTIME_CLUSTERS_ENRICHED = """
WITH ml_cluster_ids AS (
  SELECT DISTINCT cluster_id
  FROM system.compute.clusters
  WHERE dbr_version IS NOT NULL
    AND (
      LOWER(dbr_version) LIKE '%-ml-%'
      OR LOWER(dbr_version) LIKE '%-gpu-%'
    )
),
cluster_meta AS (
  SELECT
    c.cluster_id,
    MAX(c.cluster_name) as cluster_name,
    MAX(c.dbr_version) as spark_version,
    MAX(c.owned_by) as owned_by
  FROM system.compute.clusters c
  WHERE c.cluster_id IN (SELECT cluster_id FROM ml_cluster_ids)
  GROUP BY c.cluster_id
)
SELECT
  cm.cluster_name,
  cm.cluster_id,
  cm.spark_version as runtime_version,
  cm.owned_by as owner,
  MAX(u.workspace_id) as workspace_id,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active
FROM cluster_meta cm
INNER JOIN system.billing.usage u
  ON u.usage_metadata.cluster_id = cm.cluster_id
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
GROUP BY cm.cluster_name, cm.cluster_id, cm.spark_version, cm.owned_by
ORDER BY total_spend DESC
"""

# Fallback: ML clusters cannot be identified from billing alone.
# Return empty result set with correct schema.
AIML_ML_RUNTIME_CLUSTERS_FALLBACK = """
SELECT
  CAST(NULL AS STRING) as cluster_name,
  CAST(NULL AS STRING) as cluster_id,
  CAST(NULL AS STRING) as runtime_version,
  CAST(NULL AS STRING) as owner,
  CAST(0 AS DOUBLE) as total_dbus,
  CAST(0 AS DOUBLE) as total_spend,
  CAST(0 AS INT) as days_active
WHERE 1=0
"""

AIML_AGENT_BRICKS_ENRICHED = """
WITH serving_endpoints AS (
  SELECT
    endpoint_id as serving_endpoint_id,
    MAX(endpoint_name) as endpoint_name,
    MAX(served_entity_name) as served_entity_name
  FROM system.serving.served_entities
  WHERE endpoint_id IS NOT NULL
  GROUP BY endpoint_id
)
SELECT
  COALESCE(
    se.endpoint_name,
    u.usage_metadata.endpoint_name,
    u.usage_metadata.endpoint_id,
    'Unknown'
  ) as agent_name,
  MAX(u.usage_metadata.endpoint_id) as endpoint_id,
  MAX(u.workspace_id) as workspace_id,
  MAX(u.billing_origin_product) as billing_origin_product,
  MAX(se.served_entity_name) as served_entity_name,
  MAX(CASE
    WHEN u.billing_origin_product = 'SUPERVISOR_AGENT'
      OR LOWER(COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '')) LIKE '%supervisor%'
      OR LOWER(COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '')) LIKE '%multi-agent%'
      OR LOWER(COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '')) LIKE '%orchestrator%'
      THEN 'Supervisor Agent'
    WHEN u.billing_origin_product = 'KNOWLEDGE_AGENT'
      OR (u.billing_origin_product = 'AGENT_BRICKS'
          AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_KNOWLEDGE_ASSISTANT')
      OR COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '') LIKE 'ka-%'
      OR COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '') LIKE 'kie-%'
      OR LOWER(COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '')) LIKE '%knowledge%assistant%'
      THEN 'Knowledge Assistant'
    WHEN u.billing_origin_product = 'AGENT_BRICKS'
      AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_AI_CLASSIFY'
      THEN 'AI Classify'
    WHEN u.billing_origin_product = 'AGENT_BRICKS'
      AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_AI_EXTRACT'
      THEN 'AI Extract'
    WHEN LOWER(COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, '')) LIKE '%genie%'
      THEN 'Genie Space'
    ELSE 'Agent'
  END) as agent_type,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active,
  COUNT(DISTINCT u.workspace_id) as workspace_count,
  MIN(u.usage_date) as first_seen,
  MAX(u.usage_date) as last_seen,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) / NULLIF(COUNT(DISTINCT u.usage_date), 0) as avg_daily_spend
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
LEFT JOIN serving_endpoints se
  ON u.usage_metadata.endpoint_id = se.serving_endpoint_id
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.billing_origin_product IN ('AGENT_BRICKS', 'SUPERVISOR_AGENT', 'KNOWLEDGE_AGENT')
GROUP BY COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id, 'Unknown')
ORDER BY total_spend DESC
"""

AIML_AGENT_BRICKS_FALLBACK = """
SELECT
  COALESCE(u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id, 'Unknown') as agent_name,
  MAX(u.usage_metadata.endpoint_id) as endpoint_id,
  MAX(u.workspace_id) as workspace_id,
  MAX(u.billing_origin_product) as billing_origin_product,
  CAST(NULL AS STRING) as served_entity_name,
  MAX(CASE
    WHEN u.billing_origin_product = 'SUPERVISOR_AGENT'
      OR LOWER(COALESCE(u.usage_metadata.endpoint_name, '')) LIKE '%supervisor%'
      OR LOWER(COALESCE(u.usage_metadata.endpoint_name, '')) LIKE '%multi-agent%'
      OR LOWER(COALESCE(u.usage_metadata.endpoint_name, '')) LIKE '%orchestrator%'
      THEN 'Supervisor Agent'
    WHEN u.billing_origin_product = 'KNOWLEDGE_AGENT'
      OR (u.billing_origin_product = 'AGENT_BRICKS'
          AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_KNOWLEDGE_ASSISTANT')
      OR COALESCE(u.usage_metadata.endpoint_name, '') LIKE 'ka-%'
      OR COALESCE(u.usage_metadata.endpoint_name, '') LIKE 'kie-%'
      OR LOWER(COALESCE(u.usage_metadata.endpoint_name, '')) LIKE '%knowledge%assistant%'
      THEN 'Knowledge Assistant'
    WHEN u.billing_origin_product = 'AGENT_BRICKS'
      AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_AI_CLASSIFY'
      THEN 'AI Classify'
    WHEN u.billing_origin_product = 'AGENT_BRICKS'
      AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_AI_EXTRACT'
      THEN 'AI Extract'
    WHEN LOWER(COALESCE(u.usage_metadata.endpoint_name, '')) LIKE '%genie%'
      THEN 'Genie Space'
    ELSE 'Agent'
  END) as agent_type,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend,
  COUNT(DISTINCT u.usage_date) as days_active,
  COUNT(DISTINCT u.workspace_id) as workspace_count,
  MIN(u.usage_date) as first_seen,
  MAX(u.usage_date) as last_seen,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) / NULLIF(COUNT(DISTINCT u.usage_date), 0) as avg_daily_spend
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND p.price_end_time IS NULL
WHERE u.usage_date BETWEEN :start_date AND :end_date
  AND u.usage_quantity > 0
  AND u.billing_origin_product IN ('AGENT_BRICKS', 'SUPERVISOR_AGENT', 'KNOWLEDGE_AGENT')
GROUP BY COALESCE(u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id, 'Unknown')
ORDER BY total_spend DESC
"""

AIML_SKU_CATALOG = """
SELECT DISTINCT
  u.sku_name,
  u.billing_origin_product,
  u.usage_type,
  COUNT(*) as usage_records,
  SUM(u.usage_quantity) as total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as total_spend
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
GROUP BY u.sku_name, u.billing_origin_product, u.usage_type
ORDER BY total_spend DESC
"""


@router.get("/debug-ml-clusters")
async def debug_ml_clusters() -> dict[str, Any]:
    """Debug: diagnose why ML runtime clusters may not be showing up."""
    diagnostics: dict[str, Any] = {}

    # Step 1: Can we access system.compute.clusters at all?
    try:
        cluster_table_check = await asyncio.to_thread(execute_query, """
            SELECT COUNT(*) as total_rows, COUNT(DISTINCT cluster_id) as unique_clusters
            FROM system.compute.clusters
        """)
        diagnostics["clusters_table_accessible"] = True
        diagnostics["clusters_table_stats"] = dict(cluster_table_check[0]) if cluster_table_check else {}
    except Exception as e:
        diagnostics["clusters_table_accessible"] = False
        diagnostics["clusters_table_error"] = str(e)
        return diagnostics

    # Step 2: What spark_versions exist? Any with -ml- or -gpu-?
    spark_versions = await asyncio.to_thread(execute_query, """
        SELECT
          spark_version,
          COUNT(*) as cluster_count,
          CASE
            WHEN LOWER(spark_version) LIKE '%-ml-%' OR LOWER(spark_version) LIKE '%-gpu-%' THEN 'ML/GPU'
            ELSE 'Standard'
          END as runtime_type
        FROM system.compute.clusters
        WHERE spark_version IS NOT NULL
        GROUP BY spark_version
        ORDER BY cluster_count DESC
        LIMIT 50
    """)
    diagnostics["spark_versions"] = [dict(r) for r in (spark_versions or [])]

    ml_versions = [r for r in (spark_versions or []) if r.get("runtime_type") == "ML/GPU"]
    diagnostics["ml_gpu_versions_found"] = len(ml_versions)
    diagnostics["ml_gpu_versions"] = [dict(r) for r in ml_versions]

    # Step 3: If ML clusters exist, do they appear in billing?
    if ml_versions:
        ml_cluster_billing = await asyncio.to_thread(execute_query, """
            WITH ml_clusters AS (
              SELECT DISTINCT cluster_id
              FROM system.compute.clusters
              WHERE spark_version IS NOT NULL
                AND (LOWER(spark_version) LIKE '%%-ml-%%' OR LOWER(spark_version) LIKE '%%-gpu-%%')
            )
            SELECT
              COUNT(DISTINCT mc.cluster_id) as ml_clusters_with_billing,
              COUNT(*) as billing_rows,
              SUM(u.usage_quantity) as total_dbus
            FROM ml_clusters mc
            INNER JOIN system.billing.usage u
              ON u.usage_metadata.cluster_id = mc.cluster_id
            WHERE u.usage_date >= CURRENT_DATE - 30
              AND u.usage_quantity > 0
        """)
        diagnostics["ml_clusters_in_billing"] = dict(ml_cluster_billing[0]) if ml_cluster_billing else {}
    else:
        diagnostics["ml_clusters_in_billing"] = "No ML/GPU spark versions found — nothing to match"

    # Step 4: What SKUs have GPU/ML in the name? (for reference)
    gpu_skus = await asyncio.to_thread(execute_query, """
        SELECT DISTINCT sku_name
        FROM system.billing.usage
        WHERE usage_date >= CURRENT_DATE - 30
          AND (LOWER(sku_name) LIKE '%%gpu%%' OR LOWER(sku_name) LIKE '%%\\_ml%%' ESCAPE '\\\\')
        LIMIT 20
    """)
    diagnostics["gpu_ml_sku_names"] = [dict(r) for r in (gpu_skus or [])]

    # Step 5: Sample billing cluster_ids to verify format matches
    billing_cluster_sample = await asyncio.to_thread(execute_query, """
        SELECT DISTINCT usage_metadata.cluster_id as cluster_id
        FROM system.billing.usage
        WHERE usage_date >= CURRENT_DATE - 7
          AND usage_metadata.cluster_id IS NOT NULL
        LIMIT 5
    """)
    compute_cluster_sample = await asyncio.to_thread(execute_query, """
        SELECT DISTINCT cluster_id
        FROM system.compute.clusters
        LIMIT 5
    """)
    diagnostics["billing_cluster_id_samples"] = [dict(r) for r in (billing_cluster_sample or [])]
    diagnostics["compute_cluster_id_samples"] = [dict(r) for r in (compute_cluster_sample or [])]

    return diagnostics


@router.get("/summary")
async def get_aiml_summary(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get AI/ML cost summary."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    def _aiml_summary():
        from backend.lakebase_read import PG_AIML_TOTALS
        return _gold_then_live("aiml_summary", PG_AIML_TOTALS, params, AIML_SUMMARY)

    results = await asyncio.to_thread(_aiml_summary)

    if not results:
        return {
            "total_dbus": 0,
            "total_spend": 0,
            "workspace_count": 0,
            "endpoint_count": 0,
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
        "endpoint_count": round(float(row.get("avg_endpoints_per_day") or row.get("endpoint_count") or 0)),
        "days_in_range": days,
        "avg_daily_spend": total_spend / days if days > 0 else 0,
        "avg_cost_per_endpoint": float(row.get("avg_cost_per_endpoint") or 0),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "first_date": str(row.get("first_date")) if row.get("first_date") else None,
        "last_date": str(row.get("last_date")) if row.get("last_date") else None,
    }


@router.get("/providers")
async def get_fmapi_providers(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get FMAPI provider costs (Anthropic, OpenAI, Gemini)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_AIML_PROVIDERS
    results = await asyncio.to_thread(_gold_then_live, "aiml_providers", PG_AIML_PROVIDERS, params, FMAPI_PROVIDER_COSTS)

    providers = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        providers.append(
            {
                "provider": row.get("provider"),
                "sku_name": row.get("sku_name"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "workspace_count": row.get("workspace_count") or 0,
            }
        )

    # Calculate percentages
    for provider in providers:
        provider["percentage"] = (
            (provider["total_spend"] / total_spend * 100) if total_spend > 0 else 0
        )

    return {
        "providers": providers,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/endpoints")
async def get_serverless_endpoints(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get serverless inference costs by endpoint."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_AIML_ENDPOINTS
    results = await asyncio.to_thread(
        _gold_then_live, "aiml_endpoints", PG_AIML_ENDPOINTS, params, SERVERLESS_INFERENCE_BY_ENDPOINT
    )

    endpoints = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        endpoints.append(
            {
                "endpoint_name": row.get("endpoint_name"),
                "sku_name": row.get("sku_name"),
                "cost_type": row.get("cost_type"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "days_active": row.get("days_active") or 0,
                "workspace_id": str(row.get("workspace_id")) if row.get("workspace_id") else None,
                "workspace_name": row.get("workspace_name"),
            }
        )

    # Calculate percentages
    for endpoint in endpoints:
        endpoint["percentage"] = (
            (endpoint["total_spend"] / total_spend * 100) if total_spend > 0 else 0
        )

    return {
        "endpoints": endpoints,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/by-category")
async def get_aiml_by_category(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get AI/ML costs by category."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_AIML_CATEGORIES
    results = await asyncio.to_thread(_gold_then_live, "aiml_categories", PG_AIML_CATEGORIES, params, AIML_BY_CATEGORY)

    categories = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        categories.append(
            {
                "category": row.get("category"),
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
                "workspace_count": row.get("workspace_count") or 0,
            }
        )

    # Calculate percentages
    for category in categories:
        category["percentage"] = (
            (category["total_spend"] / total_spend * 100) if total_spend > 0 else 0
        )

    return {
        "categories": categories,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/timeseries")
async def get_aiml_timeseries(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get AI/ML cost timeseries by category."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_AIML_TIMESERIES
    results = await asyncio.to_thread(_gold_then_live, "aiml_timeseries", PG_AIML_TIMESERIES, params, AIML_TIMESERIES)

    # Transform to chart-friendly format
    date_data: dict[str, dict[str, float]] = {}
    categories = set()

    for row in results:
        date_str = str(row.get("usage_date"))
        category = row.get("category")
        spend = float(row.get("total_spend") or 0)

        if date_str not in date_data:
            date_data[date_str] = {"date": date_str}

        date_data[date_str][category] = spend
        categories.add(category)

    timeseries = sorted(date_data.values(), key=lambda x: x["date"])

    return {
        "timeseries": timeseries,
        "categories": sorted(list(categories)),
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


@router.get("/sku-catalog")
async def get_aiml_sku_catalog(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get catalog of all AI/ML related SKUs."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }

    from backend.lakebase_read import PG_AIML_SKU_CATALOG
    results = await asyncio.to_thread(_gold_then_live, "aiml_sku_catalog", PG_AIML_SKU_CATALOG, params, AIML_SKU_CATALOG)

    skus = []
    total_spend = 0

    for row in results:
        spend = float(row.get("total_spend") or 0)
        total_spend += spend
        skus.append(
            {
                "sku_name": row.get("sku_name"),
                "billing_origin_product": row.get("billing_origin_product"),
                "usage_type": row.get("usage_type"),
                "usage_records": row.get("usage_records") or 0,
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": spend,
            }
        )

    return {
        "skus": skus,
        "total_spend": total_spend,
        "start_date": params["start_date"],
        "end_date": params["end_date"],
    }


def _compute_aiml_bundle(params: dict, id_list: list | None, ws_clause: str, dkey: str) -> None:
    """Background worker: run all 8 AIML queries, build response, write to Delta cache."""
    global _aiml_available, _aiml_last_failure
    try:
        def _ws(sql: str) -> str:
            return wf.inject_ws_filter(sql, ws_clause)

        from backend.lakebase_read import (
            PG_AIML_AGENTS,
            PG_AIML_CATEGORIES,
            PG_AIML_ENDPOINTS,
            PG_AIML_ML_CLUSTERS,
            PG_AIML_MODELS,
            PG_AIML_PROVIDERS,
            PG_AIML_TIMESERIES,
            PG_AIML_TOTALS,
            gold_read,
        )
        queries = [
            ("summary", lambda: _gold_then_live("aiml_summary", PG_AIML_TOTALS, params, _ws(AIML_SUMMARY), ws_clause)),
            ("providers", lambda: _gold_then_live("aiml_providers", PG_AIML_PROVIDERS, params, _ws(FMAPI_PROVIDER_COSTS), ws_clause)),
            ("endpoints", lambda: _gold_then_live("aiml_endpoints", PG_AIML_ENDPOINTS, params, _ws(SERVERLESS_INFERENCE_BY_ENDPOINT), ws_clause)),
            ("categories", lambda: _gold_then_live("aiml_categories", PG_AIML_CATEGORIES, params, _ws(AIML_BY_CATEGORY), ws_clause)),
            ("timeseries", lambda: _gold_then_live("aiml_timeseries", PG_AIML_TIMESERIES, params, _ws(AIML_TIMESERIES), ws_clause)),
            ("models", lambda: gold_read("aiml_models", PG_AIML_MODELS, params, ws_clause, _ws(AIML_TOP_MODELS_AND_FEATURE_STORES))),
            ("ml_clusters", lambda: gold_read("aiml_ml_clusters", PG_AIML_ML_CLUSTERS, params, ws_clause, _ws(AIML_ML_RUNTIME_CLUSTERS_FALLBACK))),
            ("agent_bricks", lambda: gold_read("aiml_agent_bricks", PG_AIML_AGENTS, params, ws_clause, _ws(AIML_AGENT_BRICKS_FALLBACK))),
        ]

        results = execute_queries_parallel(queries, timeout=90.0)

        timed_out = sum(1 for v in results.values() if v is None)
        if timed_out >= 4:
            _aiml_available = False
            _aiml_last_failure = _time.time()
            logger.warning(
                "aiml bundle: %d/%d queries timed out; skipping new computes for %.0fs",
                timed_out, len(queries), _AIML_RETRY_INTERVAL,
            )
        else:
            _aiml_available = True

        # Format summary
        summary_data = results.get("summary", [])
        ep_raw = results.get("endpoints") or []
        ep_names = {
            r.get("endpoint_name")
            for r in ep_raw
            if r.get("endpoint_name") and r.get("endpoint_name") != "UNKNOWN"
        }
        if summary_data:
            row = summary_data[0]
            days = row.get("days_in_range") or 1
            total_spend = float(row.get("total_spend") or 0)
            endpoint_count = round(float(row.get("avg_endpoints_per_day") or row.get("endpoint_count") or 0))
            if endpoint_count == 0:
                endpoint_count = len(ep_names)
            avg_cost = float(row.get("avg_cost_per_endpoint") or 0)
            if avg_cost == 0 and endpoint_count > 0 and days:
                avg_cost = total_spend / endpoint_count / days
            summary = {
                "total_dbus": float(row.get("total_dbus") or 0),
                "total_spend": total_spend,
                "workspace_count": row.get("workspace_count") or 0,
                "endpoint_count": endpoint_count,
                "days_in_range": days,
                "avg_daily_spend": total_spend / days if days > 0 else 0,
                "avg_cost_per_endpoint": avg_cost,
            }
        else:
            ts_raw = results.get("timeseries") or []
            ts_dates = {str(r.get("usage_date")) for r in ts_raw}
            days = max(len(ts_dates), 1)
            total_dbus = sum(float(r.get("total_dbus") or 0) for r in ts_raw)
            total_spend = sum(float(r.get("total_spend") or 0) for r in ts_raw)
            endpoint_count = len(ep_names)
            summary = {
                "total_dbus": total_dbus,
                "total_spend": total_spend,
                "workspace_count": 0,
                "endpoint_count": endpoint_count,
                "days_in_range": days,
                "avg_daily_spend": total_spend / days if days > 0 else 0,
                "avg_cost_per_endpoint": total_spend / endpoint_count if endpoint_count > 0 else 0,
            }

        # Format providers
        providers_data = results.get("providers", []) or []
        providers_total = sum(float(r.get("total_spend") or 0) for r in providers_data)
        providers = [
            {
                "provider": r.get("provider"),
                "sku_name": r.get("sku_name"),
                "total_dbus": float(r.get("total_dbus") or 0),
                "total_spend": float(r.get("total_spend") or 0),
                "percentage": (float(r.get("total_spend") or 0) / providers_total * 100) if providers_total > 0 else 0,
            }
            for r in providers_data
        ]

        # Format endpoints
        endpoints_data = results.get("endpoints", []) or []
        endpoints_total = sum(float(r.get("total_spend") or 0) for r in endpoints_data)
        endpoints = [
            {
                "endpoint_name": r.get("endpoint_name"),
                "sku_name": r.get("sku_name"),
                "cost_type": r.get("cost_type"),
                "total_dbus": float(r.get("total_dbus") or 0),
                "total_spend": float(r.get("total_spend") or 0),
                "days_active": r.get("days_active") or 0,
                "workspace_id": str(r.get("workspace_id")) if r.get("workspace_id") else None,
                "workspace_name": r.get("workspace_name"),
                "percentage": (float(r.get("total_spend") or 0) / endpoints_total * 100) if endpoints_total > 0 else 0,
            }
            for r in endpoints_data
        ]

        # Format categories
        categories_data = results.get("categories", []) or []
        categories_total = sum(float(r.get("total_spend") or 0) for r in categories_data)
        categories = [
            {
                "category": r.get("category"),
                "total_dbus": float(r.get("total_dbus") or 0),
                "total_spend": float(r.get("total_spend") or 0),
                "percentage": (float(r.get("total_spend") or 0) / categories_total * 100) if categories_total > 0 else 0,
            }
            for r in categories_data
        ]

        # Format timeseries
        timeseries_data = results.get("timeseries", []) or []
        date_map: dict[str, dict[str, Any]] = {}
        ts_categories = set()
        for row in timeseries_data:
            date_str = str(row.get("usage_date"))
            category = row.get("category")
            spend = float(row.get("total_spend") or 0)
            if date_str not in date_map:
                date_map[date_str] = {"date": date_str}
            date_map[date_str][category] = spend
            ts_categories.add(category)
        timeseries = sorted(date_map.values(), key=lambda x: x["date"])

        # Format models & feature stores
        models_data = results.get("models", []) or []
        models = [
            {
                "model_name": r.get("model_name") or "Unknown",
                "model_type": r.get("model_type") or "Other",
                "total_dbus": float(r.get("total_dbus") or 0),
                "total_spend": float(r.get("total_spend") or 0),
                "days_active": r.get("days_active") or 0,
                "workspace_count": r.get("workspace_count") or 0,
            }
            for r in models_data
        ]

        # Format ML runtime clusters — aggregate by cluster (may have multiple SKU rows)
        ml_clusters_data = results.get("ml_clusters", []) or []
        ml_clusters_by_id: dict[str, dict[str, Any]] = {}
        for r in ml_clusters_data:
            cid = r.get("cluster_id") or "Unknown"
            if cid not in ml_clusters_by_id:
                ml_clusters_by_id[cid] = {
                    "cluster_name": r.get("cluster_name") or "Unknown",
                    "cluster_id": cid,
                    "workspace_id": str(r.get("workspace_id")) if r.get("workspace_id") else None,
                    "runtime_version": r.get("runtime_version") or "Unknown",
                    "owner": r.get("owner") or "Unknown",
                    "total_dbus": 0.0,
                    "total_spend": 0.0,
                    "days_active": r.get("days_active") or 0,
                }
            ml_clusters_by_id[cid]["total_dbus"] += float(r.get("total_dbus") or 0)
            ml_clusters_by_id[cid]["total_spend"] += float(r.get("total_spend") or 0)
            ml_clusters_by_id[cid]["days_active"] = max(
                ml_clusters_by_id[cid]["days_active"], r.get("days_active") or 0
            )
        ml_clusters = sorted(ml_clusters_by_id.values(), key=lambda x: x["total_spend"], reverse=True)

        # Format agent bricks
        agent_bricks_data = results.get("agent_bricks", []) or []
        agent_bricks = [
            {
                "agent_name": r.get("agent_name") or "Unknown",
                "agent_type": r.get("agent_type") or "Agent",
                "billing_origin_product": r.get("billing_origin_product"),
                "served_entity_name": r.get("served_entity_name"),
                "endpoint_id": r.get("endpoint_id"),
                "workspace_id": str(r.get("workspace_id")) if r.get("workspace_id") else None,
                "total_dbus": float(r.get("total_dbus") or 0),
                "total_spend": float(r.get("total_spend") or 0),
                "days_active": r.get("days_active") or 0,
                "workspace_count": r.get("workspace_count") or 0,
                "first_seen": str(r.get("first_seen")) if r.get("first_seen") else None,
                "last_seen": str(r.get("last_seen")) if r.get("last_seen") else None,
                "avg_daily_spend": float(r.get("avg_daily_spend") or 0),
            }
            for r in agent_bricks_data
        ]

        _resp = {
            "summary": summary,
            "providers": {"providers": providers, "total_spend": providers_total},
            "endpoints": {"endpoints": endpoints, "total_spend": endpoints_total},
            "categories": {"categories": categories, "total_spend": categories_total},
            "timeseries": {"timeseries": timeseries, "categories": sorted(list(ts_categories))},
            "models": {"models": models},
            "ml_clusters": {"clusters": ml_clusters},
            "agent_bricks": {"agents": agent_bricks},
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
        delta_cache_put(dkey, "aiml:dashboard-bundle", _resp, ttl_seconds=cache_ttls.BUNDLE_FILTERED if id_list else cache_ttls.BUNDLE)
        logger.info("aiml dashboard-bundle background compute complete: %s", dkey)
    except Exception as e:
        logger.error("aiml dashboard-bundle background compute failed: %s", e, exc_info=True)
        code = str(getattr(e, "code", "AIML_PRODUCER_FAILED"))
        try:
            delta_cache_put(
                dkey, "aiml:dashboard-bundle", _aiml_failure_payload(params, code), ttl_seconds=60
            )
        except Exception:
            pass
    finally:
        with _aiml_bundle_inflight_lock:
            _aiml_bundle_inflight.discard(dkey)


@router.get("/dashboard-bundle")
async def get_aiml_dashboard_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    workspace_ids: str = Query(default=None),
) -> dict[str, Any]:
    """Get all AI/ML dashboard data in a single request (submit-and-poll: 202 on cache miss)."""
    params = {
        "start_date": start_date or get_default_start_date(),
        "end_date": end_date or get_default_end_date(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    # AIML cost grain is daily_sku_breakdown. Missing models/clusters/agents
    # gold must not mark this tab (or Genie / Unity Gateway) unsupported.
    blocked = source_scope_unavailable(
        {"daily_sku_breakdown"},
        "The selected shared source does not publish AI/ML cost detail.",
    )
    if blocked:
        return _aiml_failure_payload(params, "SOURCE_SCOPE_UNSUPPORTED")

    _dkey = bundle_cache_key("aiml:dashboard-bundle", params["start_date"], params["end_date"], id_list)

    if (_dcached := delta_cache_get(_dkey)) is not None:
        if isinstance(_dcached, dict) and "_error" in _dcached:
            raise HTTPException(status_code=500, detail=_dcached.get("_error", "Bundle compute failed"))
        return _dcached

    if not _aiml_available and (_time.time() - _aiml_last_failure) < _AIML_RETRY_INTERVAL:
        logger.info("aiml bundle: queries recently timed out; returning 503 (retry in %.0fs)", _AIML_RETRY_INTERVAL - (_time.time() - _aiml_last_failure))
        raise HTTPException(status_code=503, detail="AI/ML data temporarily unavailable — warehouse queries timed out recently")

    ws_clause = wf.build_ws_filter_clause(id_list=id_list)

    with _aiml_bundle_inflight_lock:
        if _dkey not in _aiml_bundle_inflight:
            _aiml_bundle_inflight.add(_dkey)
            threading.Thread(
                target=_compute_aiml_bundle,
                args=(params, id_list, ws_clause, _dkey),
                daemon=True,
                name="aiml-bundle-bg",
            ).start()
            logger.info("aiml dashboard-bundle: started background compute for %s", _dkey)
        else:
            logger.debug("aiml dashboard-bundle: already inflight for %s", _dkey)

    return JSONResponse(
        status_code=202,
        content={"status": "pending", "cache_key": _dkey},
        headers={"Retry-After": "2"},
    )
