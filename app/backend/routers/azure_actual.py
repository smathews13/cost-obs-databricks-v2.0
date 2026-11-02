"""
Azure Actual Costs Router — queries azure_cost_gold table.

Mirrors aws_actual.py but for Azure Cost Management Export data.
The gold table is created by server/azure_cost_setup.py and populated
either from real Azure exports or from scripts/generate_synthetic_azure_costs.py.
"""

import asyncio
import logging
import os
import time
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query

from backend.db import execute_query

logger = logging.getLogger(__name__)
router = APIRouter()

# Cache Azure table availability — information_schema.tables is slow
_azure_status_cache: dict[str, Any] = {"available": None, "checked_at": 0}
_AZURE_STATUS_TTL = 300  # 5 minutes


def get_catalog_schema() -> tuple[str, str]:
    """Defaults match the cloud-infra-costs private preview DAB repo
    (catalog=billing, schema=azure). Override via env vars if needed."""
    catalog = os.environ.get("AZURE_COST_CATALOG", os.environ.get("COST_OBS_CATALOG", "billing"))
    schema = os.environ.get("AZURE_COST_SCHEMA", "azure")
    return catalog, schema


CHECK_AZURE_TABLES = """
SELECT 1
FROM information_schema.tables
WHERE table_schema = '{schema}'
  AND table_name = '{table}'
  AND table_catalog = '{catalog}'
LIMIT 1
"""

AZURE_ACTUAL_SUMMARY = """
SELECT
  SUM(cost_in_billing_currency)               AS total_cost,
  SUM(cost_in_usd)                            AS total_cost_usd,
  COUNT(DISTINCT usage_metadata.cluster_id)   AS cluster_count,
  COUNT(DISTINCT usage_metadata.warehouse_id) AS warehouse_count,
  COUNT(DISTINCT DATE(usage_date))            AS days_in_range
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
"""

AZURE_COSTS_BY_CLUSTER = """
SELECT
  usage_metadata.cluster_id    AS cluster_id,
  SUM(CASE WHEN charge_type = 'Compute'    THEN cost_in_billing_currency ELSE 0 END) AS compute_cost,
  SUM(CASE WHEN charge_type = 'Storage'    THEN cost_in_billing_currency ELSE 0 END) AS storage_cost,
  SUM(CASE WHEN charge_type = 'Networking' THEN cost_in_billing_currency ELSE 0 END) AS network_cost,
  SUM(cost_in_billing_currency)                                                       AS total_cost,
  COUNT(DISTINCT DATE(usage_date))                                                    AS days_active
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
  AND usage_metadata.cluster_id IS NOT NULL
GROUP BY usage_metadata.cluster_id
ORDER BY total_cost DESC
LIMIT 100
"""

AZURE_COSTS_BY_WAREHOUSE = """
SELECT
  usage_metadata.warehouse_id  AS warehouse_id,
  SUM(cost_in_billing_currency) AS total_cost,
  COUNT(DISTINCT DATE(usage_date)) AS days_active
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
  AND usage_metadata.warehouse_id IS NOT NULL
GROUP BY usage_metadata.warehouse_id
ORDER BY total_cost DESC
LIMIT 50
"""

AZURE_COSTS_BY_CHARGE_TYPE = """
SELECT
  charge_type,
  SUM(cost_in_billing_currency) AS total_cost,
  SUM(cost_in_usd)              AS total_cost_usd
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
GROUP BY charge_type
ORDER BY total_cost DESC
"""

AZURE_COSTS_BY_PRICING_MODEL = """
SELECT
  pricing_model,
  SUM(cost_in_billing_currency) AS total_cost
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
GROUP BY pricing_model
ORDER BY total_cost DESC
"""

AZURE_COSTS_TIMESERIES = """
SELECT
  DATE(usage_date)              AS date,
  charge_type,
  SUM(cost_in_billing_currency) AS daily_cost
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE usage_date >= :start_date
  AND usage_date < :end_date
GROUP BY DATE(usage_date), charge_type
ORDER BY date
"""


@router.get("/status")
async def get_azure_status() -> dict[str, Any]:
    """Check if Azure cost tables are available (cached 5 min)."""
    catalog, schema = get_catalog_schema()

    if _azure_status_cache["available"] is not None and (time.time() - _azure_status_cache["checked_at"]) < _AZURE_STATUS_TTL:
        available = _azure_status_cache["available"]
    else:
        try:
            from backend.lakebase_read import warehouse_view_fallback
            if not warehouse_view_fallback():
                from backend.db import lakebase_query
                rows = await asyncio.to_thread(
                    lakebase_query, "SELECT 1 FROM daily_azure_actuals LIMIT 1", {}
                )
                available = bool(rows)
            else:
                query = CHECK_AZURE_TABLES.format(catalog=catalog, schema=schema, table="actuals_gold")
                results = await asyncio.to_thread(execute_query, query)
                available = len(results) > 0
        except Exception as e:
            logger.warning(f"Azure cost tables not available: {e}")
            available = False
        _azure_status_cache["available"] = available
        _azure_status_cache["checked_at"] = time.time()

    return {
        "azure_available": available,
        "catalog": catalog,
        "schema": schema,
        "table": "actuals_gold" if available else None,
    }


@router.get("/summary")
async def get_azure_actual_summary(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    catalog, schema = get_catalog_schema()
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_azure_status()
    if not status["azure_available"]:
        return {"available": False, "message": "Azure cost data not configured.", "start_date": start_date, "end_date": end_date}

    qparams = {"start_date": start_date, "end_date": end_date}
    query = AZURE_ACTUAL_SUMMARY.format(catalog=catalog, schema=schema)

    def _azure_summary():
        from backend.lakebase_read import PG_AZURE_SUMMARY, gold_read, warehouse_view_fallback
        return gold_read(
            "azure_summary", PG_AZURE_SUMMARY, qparams, "",
            query if warehouse_view_fallback() else None,
        )

    results = await asyncio.to_thread(_azure_summary)
    if not results:
        return {"available": True, "total_cost": 0, "total_cost_usd": 0, "cluster_count": 0, "warehouse_count": 0, "days_in_range": 0, "start_date": start_date, "end_date": end_date}

    row = results[0]
    return {
        "available": True,
        "total_cost": float(row.get("total_cost") or 0),
        "total_cost_usd": float(row.get("total_cost_usd") or 0),
        "cluster_count": row.get("cluster_count") or 0,
        "warehouse_count": row.get("warehouse_count") or 0,
        "days_in_range": row.get("days_in_range") or 0,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/by-cluster")
async def get_azure_costs_by_cluster(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    catalog, schema = get_catalog_schema()
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_azure_status()
    if not status["azure_available"]:
        return {"available": False, "clusters": [], "start_date": start_date, "end_date": end_date}

    qparams = {"start_date": start_date, "end_date": end_date}
    query = AZURE_COSTS_BY_CLUSTER.format(catalog=catalog, schema=schema)

    def _azure_clusters():
        from backend.lakebase_read import PG_AZURE_BY_CLUSTER, gold_read, warehouse_view_fallback
        return gold_read(
            "azure_by_cluster", PG_AZURE_BY_CLUSTER, qparams, "",
            query if warehouse_view_fallback() else None,
        )

    results = await asyncio.to_thread(_azure_clusters)

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
    for c in clusters:
        c["percentage"] = (c["total_cost"] / total_cost * 100) if total_cost > 0 else 0

    return {"available": True, "clusters": clusters, "total_cost": total_cost, "start_date": start_date, "end_date": end_date}


@router.get("/by-charge-type")
async def get_azure_costs_by_charge_type(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    catalog, schema = get_catalog_schema()
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_azure_status()
    if not status["azure_available"]:
        return {"available": False, "charge_types": [], "start_date": start_date, "end_date": end_date}

    qparams = {"start_date": start_date, "end_date": end_date}
    query = AZURE_COSTS_BY_CHARGE_TYPE.format(catalog=catalog, schema=schema)

    def _azure_charge():
        from backend.lakebase_read import (
            PG_AZURE_BY_CHARGE_TYPE,
            gold_read,
            warehouse_view_fallback,
        )
        return gold_read(
            "azure_by_charge_type", PG_AZURE_BY_CHARGE_TYPE, qparams, "",
            query if warehouse_view_fallback() else None,
        )

    results = await asyncio.to_thread(_azure_charge)
    total = sum(float(r.get("total_cost") or 0) for r in results)
    charge_types = [
        {
            "charge_type": r.get("charge_type") or "Other",
            "total_cost": float(r.get("total_cost") or 0),
            "total_cost_usd": float(r.get("total_cost_usd") or 0),
            "percentage": float(r.get("total_cost") or 0) / total * 100 if total > 0 else 0,
        }
        for r in results
    ]
    return {"available": True, "charge_types": charge_types, "total_cost": total, "start_date": start_date, "end_date": end_date}


@router.get("/timeseries")
async def get_azure_costs_timeseries(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    catalog, schema = get_catalog_schema()
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_azure_status()
    if not status["azure_available"]:
        return {"available": False, "timeseries": [], "charge_types": [], "start_date": start_date, "end_date": end_date}

    qparams = {"start_date": start_date, "end_date": end_date}
    query = AZURE_COSTS_TIMESERIES.format(catalog=catalog, schema=schema)

    def _azure_ts():
        from backend.lakebase_read import PG_AZURE_TIMESERIES, gold_read, warehouse_view_fallback
        return gold_read(
            "azure_timeseries", PG_AZURE_TIMESERIES, qparams, "",
            query if warehouse_view_fallback() else None,
        )

    results = await asyncio.to_thread(_azure_ts)

    data_by_date: dict[str, dict] = {}
    charge_types_set: set[str] = set()
    for row in results:
        date_str = str(row.get("date"))
        ct = row.get("charge_type") or "Other"
        cost = float(row.get("daily_cost") or 0)
        charge_types_set.add(ct)
        if date_str not in data_by_date:
            data_by_date[date_str] = {"date": date_str}
        data_by_date[date_str][ct] = cost

    charge_types = sorted(list(charge_types_set))
    timeseries = []
    for date_str in sorted(data_by_date.keys()):
        row = data_by_date[date_str]
        for ct in charge_types:
            row.setdefault(ct, 0)
        timeseries.append(row)

    return {"available": True, "timeseries": timeseries, "charge_types": charge_types, "start_date": start_date, "end_date": end_date}


@router.get("/dashboard-bundle")
async def get_azure_actual_dashboard_bundle(
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
) -> dict[str, Any]:
    """Get all Azure actual cost data in a single parallel request."""
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    status = await get_azure_status()
    if not status["azure_available"]:
        return {"available": False, "message": "Azure cost data not configured", "start_date": start_date, "end_date": end_date}

    summary, by_cluster, by_charge_type, timeseries = await asyncio.gather(
        get_azure_actual_summary(start_date, end_date),
        get_azure_costs_by_cluster(start_date, end_date),
        get_azure_costs_by_charge_type(start_date, end_date),
        get_azure_costs_timeseries(start_date, end_date),
    )

    return {
        "available": True,
        "summary": summary,
        "by_cluster": by_cluster,
        "by_charge_type": by_charge_type,
        "timeseries": timeseries,
        "start_date": start_date,
        "end_date": end_date,
    }
