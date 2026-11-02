"""
AWS Actual Costs Router - Integrates with AWS CUR 2.0 data

This router queries actual AWS billing data from the cloud-infra-costs
medallion architecture (bronze/silver/gold tables) when available.

Source: https://github.com/databricks-solutions/cloud-infra-costs
"""

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query

from backend import cache_ttls
from backend.db import bundle_cache_key, delta_cache_get, delta_cache_put, execute_query

logger = logging.getLogger(__name__)
router = APIRouter()

# Cache CUR availability — information_schema.tables is slow, no need to re-check on every request
_cur_status_cache: dict[str, Any] = {"available": None, "checked_at": 0}
_CUR_STATUS_TTL = 300  # 5 minutes

# Check if CUR tables exist
CHECK_CUR_TABLES = """
SELECT 1
FROM information_schema.tables
WHERE table_schema = '{schema}'
  AND table_name = '{table}'
  AND table_catalog = '{catalog}'
LIMIT 1
"""

# Summary of actual AWS costs
AWS_ACTUAL_SUMMARY = """
SELECT
  SUM(unblended_cost) as total_unblended,
  SUM(net_unblended_cost) as total_net_unblended,
  SUM(amortized_cost) as total_amortized,
  SUM(net_amortized_cost) as total_net_amortized,
  COUNT(DISTINCT usage_metadata.cluster_id) as cluster_count,
  COUNT(DISTINCT usage_metadata.warehouse_id) as warehouse_count,
  COUNT(DISTINCT DATE(usage_date)) as days_in_range
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
"""

# Costs by cluster
AWS_COSTS_BY_CLUSTER = """
SELECT
  usage_metadata.cluster_id as cluster_id,
  SUM(CASE WHEN charge_type = 'Compute' THEN net_unblended_cost ELSE 0 END) as compute_cost,
  SUM(CASE WHEN charge_type = 'Storage' THEN net_unblended_cost ELSE 0 END) as storage_cost,
  SUM(CASE WHEN charge_type = 'Networking' THEN net_unblended_cost ELSE 0 END) as network_cost,
  SUM(net_unblended_cost) as total_cost,
  COUNT(DISTINCT DATE(usage_date)) as days_active
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
  AND usage_metadata.cluster_id IS NOT NULL
GROUP BY usage_metadata.cluster_id
ORDER BY total_cost DESC
LIMIT 100
"""

# Costs by warehouse
AWS_COSTS_BY_WAREHOUSE = """
SELECT
  usage_metadata.warehouse_id as warehouse_id,
  SUM(net_unblended_cost) as total_cost,
  COUNT(DISTINCT DATE(usage_date)) as days_active
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
  AND usage_metadata.warehouse_id IS NOT NULL
GROUP BY usage_metadata.warehouse_id
ORDER BY total_cost DESC
LIMIT 50
"""

# Costs by charge type
AWS_COSTS_BY_CHARGE_TYPE = """
SELECT
  charge_type,
  SUM(unblended_cost) as unblended_cost,
  SUM(net_unblended_cost) as net_unblended_cost,
  SUM(amortized_cost) as amortized_cost,
  SUM(net_amortized_cost) as net_amortized_cost
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
GROUP BY charge_type
ORDER BY net_unblended_cost DESC
"""

# Daily timeseries
AWS_COSTS_TIMESERIES = """
SELECT
  DATE(usage_date) as date,
  charge_type,
  SUM(net_unblended_cost) as daily_cost
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
GROUP BY DATE(usage_date), charge_type
ORDER BY date
"""

# Cost type comparison (for showing different cost metrics)
AWS_COST_TYPES_DAILY = """
SELECT
  DATE(usage_date) as date,
  SUM(unblended_cost) as unblended,
  SUM(net_unblended_cost) as net_unblended,
  SUM(amortized_cost) as amortized,
  SUM(net_amortized_cost) as net_amortized
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
GROUP BY DATE(usage_date)
ORDER BY date
"""


def get_catalog_schema() -> tuple[str, str]:
    """Get catalog and schema from environment or defaults.

    Defaults match the cloud-infra-costs private preview DAB repo
    (catalog=billing, schema=aws). Override via env vars if your deployment
    uses a different catalog or schema name.
    """
    import os
    catalog = os.environ.get("AWS_COST_CATALOG", os.environ.get("COST_OBS_CATALOG", "billing"))
    schema = os.environ.get("AWS_COST_SCHEMA", "aws")
    return catalog, schema


@router.get("/status")
async def get_cur_status() -> dict[str, Any]:
    """Check if AWS CUR tables are available (cached 5 min)."""
    catalog, schema = get_catalog_schema()

    if _cur_status_cache["available"] is not None and (time.time() - _cur_status_cache["checked_at"]) < _CUR_STATUS_TTL:
        available = _cur_status_cache["available"]
    else:
        try:
            from backend.lakebase_read import warehouse_view_fallback
            if not warehouse_view_fallback():
                from backend.db import lakebase_query
                rows = await asyncio.to_thread(
                    lakebase_query, "SELECT 1 FROM daily_cloud_actuals LIMIT 1", {}
                )
                available = bool(rows)
            else:
                query = CHECK_CUR_TABLES.format(catalog=catalog, schema=schema, table="actuals_gold")
                results = await asyncio.to_thread(execute_query, query)
                available = len(results) > 0
        except Exception as e:
            logger.warning(f"CUR tables not available: {e}")
            available = False
        _cur_status_cache["available"] = available
        _cur_status_cache["checked_at"] = time.time()

    return {
        "cur_available": available,
        "catalog": catalog,
        "schema": schema,
        "table": "actuals_gold" if available else None,
    }


@router.get("/summary")
async def get_aws_actual_summary(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get summary of actual AWS costs."""
    catalog, schema = get_catalog_schema()

    # Default date range
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    # Check if CUR is available
    status = await get_cur_status()
    if not status["cur_available"]:
        return {
            "available": False,
            "message": "AWS CUR data not configured. Using estimated costs.",
            "start_date": start_date,
            "end_date": end_date,
        }

    query = AWS_ACTUAL_SUMMARY.format(catalog=catalog, schema=schema)
    qparams = {"start_date": start_date, "end_date": end_date}

    def _cloud_summary():
        from backend.lakebase_read import PG_CLOUD_SUMMARY, dual_read, warehouse_view_fallback
        live = (lambda: execute_query(query, qparams)) if warehouse_view_fallback() else None
        return dual_read("cloud_summary", PG_CLOUD_SUMMARY, qparams, "", fetch_delta=live)

    results = await asyncio.to_thread(_cloud_summary)

    if not results:
        return {
            "available": True,
            "total_unblended": 0,
            "total_net_unblended": 0,
            "total_amortized": 0,
            "total_net_amortized": 0,
            "cluster_count": 0,
            "warehouse_count": 0,
            "days_in_range": 0,
            "start_date": start_date,
            "end_date": end_date,
        }

    row = results[0]
    return {
        "available": True,
        "total_unblended": float(row.get("total_unblended") or 0),
        "total_net_unblended": float(row.get("total_net_unblended") or 0),
        "total_amortized": float(row.get("total_amortized") or 0),
        "total_net_amortized": float(row.get("total_net_amortized") or 0),
        "cluster_count": row.get("cluster_count") or 0,
        "warehouse_count": row.get("warehouse_count") or 0,
        "days_in_range": row.get("days_in_range") or 0,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/by-cluster")
async def get_aws_costs_by_cluster(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get actual AWS costs by cluster."""
    catalog, schema = get_catalog_schema()

    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_cur_status()
    if not status["cur_available"]:
        return {
            "available": False,
            "clusters": [],
            "start_date": start_date,
            "end_date": end_date,
        }

    query = AWS_COSTS_BY_CLUSTER.format(catalog=catalog, schema=schema)
    qparams = {"start_date": start_date, "end_date": end_date}

    def _cloud_clusters():
        from backend.lakebase_read import PG_CLOUD_BY_CLUSTER, dual_read, warehouse_view_fallback
        live = (lambda: execute_query(query, qparams)) if warehouse_view_fallback() else None
        return dual_read("cloud_by_cluster", PG_CLOUD_BY_CLUSTER, qparams, "", fetch_delta=live)

    results = await asyncio.to_thread(_cloud_clusters)

    clusters = []
    total_cost = 0
    for row in results:
        cost = float(row.get("total_cost") or 0)
        total_cost += cost
        clusters.append({
            "cluster_id": row.get("cluster_id"),
            "compute_cost": float(row.get("compute_cost") or 0),
            "storage_cost": float(row.get("storage_cost") or 0),
            "network_cost": float(row.get("network_cost") or 0),
            "total_cost": cost,
            "days_active": row.get("days_active") or 0,
        })

    # Calculate percentages
    for cluster in clusters:
        cluster["percentage"] = (cluster["total_cost"] / total_cost * 100) if total_cost > 0 else 0

    return {
        "available": True,
        "clusters": clusters,
        "total_cost": total_cost,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/by-charge-type")
async def get_aws_costs_by_charge_type(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get actual AWS costs by charge type (Compute, Storage, Networking)."""
    catalog, schema = get_catalog_schema()

    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_cur_status()
    if not status["cur_available"]:
        return {
            "available": False,
            "charge_types": [],
            "start_date": start_date,
            "end_date": end_date,
        }

    query = AWS_COSTS_BY_CHARGE_TYPE.format(catalog=catalog, schema=schema)
    qparams = {"start_date": start_date, "end_date": end_date}

    def _cloud_charge():
        from backend.lakebase_read import (
            PG_CLOUD_BY_CHARGE_TYPE,
            dual_read,
            warehouse_view_fallback,
        )
        live = (lambda: execute_query(query, qparams)) if warehouse_view_fallback() else None
        return dual_read("cloud_by_charge_type", PG_CLOUD_BY_CHARGE_TYPE, qparams, "", fetch_delta=live)

    results = await asyncio.to_thread(_cloud_charge)

    charge_types = []
    total = 0
    for row in results:
        cost = float(row.get("net_unblended_cost") or 0)
        total += cost
        charge_types.append({
            "charge_type": row.get("charge_type") or "Other",
            "unblended_cost": float(row.get("unblended_cost") or 0),
            "net_unblended_cost": cost,
            "amortized_cost": float(row.get("amortized_cost") or 0),
            "net_amortized_cost": float(row.get("net_amortized_cost") or 0),
        })

    for ct in charge_types:
        ct["percentage"] = (ct["net_unblended_cost"] / total * 100) if total > 0 else 0

    return {
        "available": True,
        "charge_types": charge_types,
        "total_cost": total,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/timeseries")
async def get_aws_costs_timeseries(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get daily AWS costs timeseries by charge type."""
    catalog, schema = get_catalog_schema()

    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_cur_status()
    if not status["cur_available"]:
        return {
            "available": False,
            "timeseries": [],
            "charge_types": [],
            "start_date": start_date,
            "end_date": end_date,
        }

    query = AWS_COSTS_TIMESERIES.format(catalog=catalog, schema=schema)
    qparams = {"start_date": start_date, "end_date": end_date}

    def _cloud_ts():
        from backend.lakebase_read import PG_CLOUD_TIMESERIES, dual_read, warehouse_view_fallback
        live = (lambda: execute_query(query, qparams)) if warehouse_view_fallback() else None
        return dual_read("cloud_timeseries", PG_CLOUD_TIMESERIES, qparams, "", fetch_delta=live)

    results = await asyncio.to_thread(_cloud_ts)

    # Pivot data by date
    data_by_date: dict[str, dict[str, float]] = {}
    charge_types_set: set[str] = set()

    for row in results:
        date_str = str(row.get("date"))
        charge_type = row.get("charge_type") or "Other"
        cost = float(row.get("daily_cost") or 0)

        charge_types_set.add(charge_type)

        if date_str not in data_by_date:
            data_by_date[date_str] = {"date": date_str}
        data_by_date[date_str][charge_type] = cost

    # Convert to list and fill missing values
    charge_types = sorted(list(charge_types_set))
    timeseries = []
    for date_str in sorted(data_by_date.keys()):
        row = data_by_date[date_str]
        for ct in charge_types:
            if ct not in row:
                row[ct] = 0
        timeseries.append(row)

    return {
        "available": True,
        "timeseries": timeseries,
        "charge_types": charge_types,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/dashboard-bundle")
async def get_aws_actual_dashboard_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get all AWS actual cost data in a single request."""
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    # Check status first — don't cache the unavailable response (status has its own TTL)
    status = await get_cur_status()

    if not status["cur_available"]:
        return {
            "available": False,
            "message": "AWS CUR data not configured",
            "start_date": start_date,
            "end_date": end_date,
        }

    _dkey = bundle_cache_key("aws_actual/dashboard-bundle", start_date, end_date, None)
    try:
        if (_dcached := delta_cache_get(_dkey)) is not None:
            return _dcached
    except Exception as _ce:
        logger.debug("Delta cache read failed for aws_actual/dashboard-bundle: %s", _ce)

    # Fetch all data in parallel for 4x faster response
    summary, by_cluster, by_charge_type, timeseries = await asyncio.gather(
        get_aws_actual_summary(start_date, end_date),
        get_aws_costs_by_cluster(start_date, end_date),
        get_aws_costs_by_charge_type(start_date, end_date),
        get_aws_costs_timeseries(start_date, end_date),
    )

    _resp = {
        "available": True,
        "summary": summary,
        "by_cluster": by_cluster,
        "by_charge_type": by_charge_type,
        "timeseries": timeseries,
        "start_date": start_date,
        "end_date": end_date,
    }
    try:
        delta_cache_put(_dkey, "aws_actual/dashboard-bundle", _resp, ttl_seconds=cache_ttls.BUNDLE)
    except Exception as _ce:
        logger.debug("Delta cache write failed for aws_actual/dashboard-bundle: %s", _ce)
    return _resp
