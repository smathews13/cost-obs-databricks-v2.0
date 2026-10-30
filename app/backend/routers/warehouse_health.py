"""Warehouse rightsizing and utilization recommendations.

Uses system.compute.warehouse_events, system.compute.warehouses, and
system.query.history to detect underutilized warehouses via three heuristics:
  1. IDLE_RUNNING  — warehouse running 2+ hours with no queries in 24h
  2. OVER_SCALED   — scaled to 2+ clusters but peak concurrency is low
  3. OVERSIZED     — large size with low queue wait and fast queries
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter

from backend.db import (
    execute_query,
    execute_queries_parallel,
    local_source_is_selected,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_health_cache: dict | None = None
_health_cache_ts: float = 0.0
_HEALTH_CACHE_TTL = 30 * 60  # 30 minutes — recommendations don't change rapidly

# ── Tunable thresholds ────────────────────────────────────────────────────────
_IDLE_LOOKBACK_HOURS = 2
_IDLE_NO_QUERY_HOURS = 24
_OVER_SCALED_CONCURRENCY_PER_CLUSTER = 10
_OVERSIZED_MAX_QUEUE_MS = 15000     # 15 s avg queue — warehouse is not under pressure
_OVERSIZED_MAX_MEDIAN_DURATION_S = 180  # 3 min execution — queries don't need Large
_OVERSIZED_MIN_QUERIES = 5
_LARGE_SIZES = ("Large", "X-Large", "2X-Large", "3X-Large", "4X-Large")


def _shared_scope_unavailable() -> dict[str, Any]:
    return {
        "available": False,
        "reason": "shared_scope_unsupported",
        "reason_detail": "Optimize recommendations use local warehouse metadata.",
        "error_code": "SOURCE_SCOPE_UNSUPPORTED",
        "error": "Optimize recommendations use local warehouse metadata.",
        "recommendations": [],
        "warehouses_analyzed": 0,
    }


def _warehouse_scope_blocked() -> bool:
    # Optimize reads this workspace's live system.compute / query.history.
    # Shared gold must never open that path, even if the share lists idle tables.
    return not local_source_is_selected()

_SQL_IDLE = f"""
WITH current_warehouses AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id, warehouse_type
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
recent_queries AS (
  SELECT DISTINCT compute.warehouse_id AS warehouse_id
  FROM system.query.history
  WHERE start_time >= NOW() - INTERVAL {_IDLE_NO_QUERY_HOURS} HOUR
    AND compute.warehouse_id IS NOT NULL
),
running_warehouses AS (
  SELECT
    warehouse_id,
    MAX(event_time) AS last_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= NOW() - INTERVAL {_IDLE_LOOKBACK_HOURS} HOUR
    AND cluster_count > 0
  GROUP BY warehouse_id
)
SELECT
  rw.warehouse_id,
  w.warehouse_name,
  w.warehouse_size,
  w.workspace_id,
  w.warehouse_type,
  rw.last_event_time
FROM running_warehouses rw
JOIN current_warehouses w USING (warehouse_id)
WHERE rw.warehouse_id NOT IN (SELECT warehouse_id FROM recent_queries)
  AND COALESCE(w.warehouse_type, 'CLASSIC') != 'SERVERLESS'
"""

_SQL_OVER_SCALED = f"""
WITH current_warehouses AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
cluster_scale_events AS (
  SELECT
    warehouse_id,
    MAX(cluster_count) AS max_clusters_observed
  FROM system.compute.warehouse_events
  WHERE event_time >= NOW() - INTERVAL 30 DAY
  GROUP BY warehouse_id
  HAVING MAX(cluster_count) >= 2
),
concurrent_per_minute AS (
  SELECT
    compute.warehouse_id AS warehouse_id,
    DATE_TRUNC('minute', start_time) AS minute_bucket,
    COUNT(*) AS concurrent_queries
  FROM system.query.history
  WHERE start_time >= NOW() - INTERVAL 30 DAY
    AND compute.warehouse_id IN (SELECT warehouse_id FROM cluster_scale_events)
  GROUP BY 1, 2
),
max_concurrency AS (
  SELECT warehouse_id, MAX(concurrent_queries) AS max_concurrent
  FROM concurrent_per_minute
  GROUP BY warehouse_id
)
SELECT
  cse.warehouse_id,
  w.warehouse_name,
  w.warehouse_size,
  w.workspace_id,
  cse.max_clusters_observed,
  COALESCE(mc.max_concurrent, 0) AS max_concurrent
FROM cluster_scale_events cse
JOIN current_warehouses w USING (warehouse_id)
LEFT JOIN max_concurrency mc USING (warehouse_id)
WHERE COALESCE(mc.max_concurrent, 0) < (cse.max_clusters_observed * {_OVER_SCALED_CONCURRENCY_PER_CLUSTER})
"""

_SQL_OVERSIZED = f"""
WITH current_warehouses AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id, warehouse_type
  FROM system.compute.warehouses
  WHERE warehouse_size IN {_LARGE_SIZES}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
large_warehouses AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id
  FROM current_warehouses
  WHERE COALESCE(warehouse_type, 'CLASSIC') != 'SERVERLESS'
),
qstats AS (
  SELECT
    compute.warehouse_id AS warehouse_id,
    COUNT(*) AS query_count,
    AVG(COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0)) AS avg_queue_ms,
    PERCENTILE_APPROX(
      GREATEST(0.0,
        (UNIX_TIMESTAMP(end_time) - UNIX_TIMESTAMP(start_time))
        - (COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0
      ),
      0.5
    ) AS median_duration_seconds
  FROM system.query.history
  WHERE start_time >= NOW() - INTERVAL 30 DAY
    AND compute.warehouse_id IN (SELECT warehouse_id FROM large_warehouses)
    AND end_time IS NOT NULL
  GROUP BY compute.warehouse_id
  HAVING COUNT(*) >= {_OVERSIZED_MIN_QUERIES}
)
SELECT
  lw.warehouse_id,
  lw.warehouse_name,
  lw.warehouse_size,
  lw.workspace_id,
  q.query_count,
  q.avg_queue_ms,
  q.median_duration_seconds
FROM large_warehouses lw
JOIN qstats q USING (warehouse_id)
WHERE q.avg_queue_ms < {_OVERSIZED_MAX_QUEUE_MS}
  AND q.median_duration_seconds < {_OVERSIZED_MAX_MEDIAN_DURATION_S}
"""


def _build_recommendation(
    row: dict, rtype: str, extra: dict | None = None
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "warehouse_id": row.get("warehouse_id"),
        "warehouse_name": row.get("warehouse_name"),
        "warehouse_size": row.get("warehouse_size"),
        "workspace_id": str(row.get("workspace_id") or ""),
        "recommendation_type": rtype,
        **(extra or {}),
    }
    if rtype == "IDLE_RUNNING":
        rec["last_event_time"] = str(row.get("last_event_time")) if row.get("last_event_time") else None
        rec["recommendation_text"] = (
            f"Warehouse has been running for {_IDLE_LOOKBACK_HOURS}+ hours with no queries "
            f"in the last {_IDLE_NO_QUERY_HOURS}h. Consider reducing auto_stop_minutes."
        )
    elif rtype == "OVER_SCALED":
        mc = int(row.get("max_clusters_observed") or 0)
        concur = int(row.get("max_concurrent") or 0)
        rec["max_clusters_observed"] = mc
        rec["max_concurrent"] = concur
        rec["recommendation_text"] = (
            f"Warehouse scaled to {mc} clusters but peak concurrency was only {concur} queries. "
            f"Consider reducing max_num_clusters."
        )
    elif rtype == "OVERSIZED":
        q = float(row.get("avg_queue_ms") or 0)
        d = float(row.get("median_duration_seconds") or 0)
        size = row.get("warehouse_size", "")
        rec["avg_queue_ms"] = q
        rec["median_duration_seconds"] = d
        rec["query_count"] = int(row.get("query_count") or 0)
        rec["recommendation_text"] = (
            f"{size} warehouse with avg queue time {q/1000:.1f}s and median query duration {d:.1f}s. "
            f"Consider downsizing one tier."
        )
    return rec


def _run_health_queries() -> dict[str, Any]:
    """Serve Lakebase gold findings; live warehouse SQL only when fallback is allowed."""
    from backend.lakebase_read import PG_WAREHOUSE_OPTIMIZER, gold_read, warehouse_view_fallback

    gold_rows = gold_read("warehouse_optimizer", PG_WAREHOUSE_OPTIMIZER, {})
    if gold_rows:
        recommendations: list[dict] = []
        seen_wids: set[str] = set()
        for row in gold_rows:
            rtype = row.get("recommendation_type") or ""
            extra = {}
            if rtype == "IDLE_RUNNING":
                extra = {"last_event_time": row.get("extra_1")}
            elif rtype == "OVER_SCALED":
                extra = {
                    "max_clusters_observed": row.get("extra_n1"),
                    "max_concurrent": row.get("extra_n2"),
                }
            elif rtype == "OVERSIZED":
                extra = {
                    "avg_queue_ms": row.get("extra_n1"),
                    "median_duration_seconds": row.get("extra_n2"),
                    "query_count": row.get("extra_n3"),
                }
            mapped = {**row, **extra}
            recommendations.append(_build_recommendation(mapped, rtype, extra))
            seen_wids.add(row.get("warehouse_id") or "")
        order = {"IDLE_RUNNING": 0, "OVER_SCALED": 1, "OVERSIZED": 2}
        recommendations.sort(key=lambda r: order.get(r["recommendation_type"], 9))
        return {
            "available": True,
            "recommendations": recommendations,
            "warehouses_analyzed": len(seen_wids),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    if not warehouse_view_fallback():
        return {
            "available": True,
            "recommendations": [],
            "warehouses_analyzed": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    results = execute_queries_parallel([
        ("idle", lambda: execute_query(_SQL_IDLE)),
        ("over_scaled", lambda: execute_query(_SQL_OVER_SCALED)),
        ("oversized", lambda: execute_query(_SQL_OVERSIZED)),
    ], timeout=90.0)

    recommendations = []
    seen_wids = set()

    for row in (results.get("idle") or []):
        recommendations.append(_build_recommendation(row, "IDLE_RUNNING"))
        seen_wids.add(row.get("warehouse_id") or "")

    for row in (results.get("over_scaled") or []):
        recommendations.append(_build_recommendation(row, "OVER_SCALED"))
        seen_wids.add(row.get("warehouse_id") or "")

    for row in (results.get("oversized") or []):
        recommendations.append(_build_recommendation(row, "OVERSIZED"))
        seen_wids.add(row.get("warehouse_id") or "")

    order = {"IDLE_RUNNING": 0, "OVER_SCALED": 1, "OVERSIZED": 2}
    recommendations.sort(key=lambda r: order.get(r["recommendation_type"], 9))

    return {
        "available": True,
        "recommendations": recommendations,
        "warehouses_analyzed": len(seen_wids),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("")
async def get_warehouse_health() -> dict[str, Any]:
    """Return rightsizing recommendations for all warehouses."""
    global _health_cache, _health_cache_ts

    if _warehouse_scope_blocked():
        return _shared_scope_unavailable()

    if _health_cache is not None and (time.time() - _health_cache_ts) < _HEALTH_CACHE_TTL:
        return _health_cache

    try:
        payload = await asyncio.to_thread(_run_health_queries)
    except Exception as e:
        logger.warning(f"Warehouse health queries failed: {e}")
        return {"available": False, "error": str(e), "recommendations": [], "warehouses_analyzed": 0,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    _health_cache = payload
    _health_cache_ts = time.time()
    return payload


def _build_idle_time_sql(ws_clause: str, ws_clause_wh: str) -> str:
    return f"""
WITH all_events AS (
  -- Look back 7 days before the window start to catch warehouses already running at boundary
  SELECT
    warehouse_id,
    event_time,
    cluster_count
  FROM system.compute.warehouse_events
  WHERE event_time < CAST(:end_ts AS TIMESTAMP)
    AND event_time >= CAST(:start_ts AS TIMESTAMP) - INTERVAL 7 DAYS
),
events_with_next AS (
  SELECT
    warehouse_id,
    event_time,
    cluster_count,
    LEAD(event_time) OVER (PARTITION BY warehouse_id ORDER BY event_time) AS next_event_time
  FROM all_events
),
running_windows AS (
  SELECT
    warehouse_id,
    GREATEST(event_time, CAST(:start_ts AS TIMESTAMP)) AS window_start,
    LEAST(
      COALESCE(next_event_time, CAST(:end_ts AS TIMESTAMP)),
      CAST(:end_ts AS TIMESTAMP)
    ) AS window_end
  FROM events_with_next
  WHERE cluster_count > 0
    AND COALESCE(next_event_time, CAST(:end_ts AS TIMESTAMP)) > CAST(:start_ts AS TIMESTAMP)
    AND event_time < CAST(:end_ts AS TIMESTAMP)
),
warehouse_uptime AS (
  SELECT
    warehouse_id,
    SUM((UNIX_TIMESTAMP(window_end) - UNIX_TIMESTAMP(window_start)) / 60.0) AS total_running_minutes
  FROM running_windows
  WHERE window_start < window_end
  GROUP BY warehouse_id
),
billing_agg AS (
  -- Single-pass scan of system.billing.usage producing both the billing-derived
  -- uptime fallback (classic-only, LEAST-capped at window duration) and total
  -- warehouse spend for the proration below. Consolidated to avoid scanning
  -- billing.usage twice — the extra scan was pressuring the warehouse enough
  -- to starve concurrent bundles' queries (tagging, aiml) into timeout.
  SELECT
    u.usage_metadata.warehouse_id AS warehouse_id,
    LEAST(
      SUM(
        CASE
          WHEN u.usage_end_time > CAST(:start_ts AS TIMESTAMP)
               AND u.usage_start_time < CAST(:end_ts AS TIMESTAMP)
          THEN (UNIX_TIMESTAMP(LEAST(u.usage_end_time, CAST(:end_ts AS TIMESTAMP)))
                - UNIX_TIMESTAMP(GREATEST(u.usage_start_time, CAST(:start_ts AS TIMESTAMP)))
               ) / 60.0
          ELSE 0
        END
      ),
      (UNIX_TIMESTAMP(CAST(:end_ts AS TIMESTAMP)) - UNIX_TIMESTAMP(CAST(:start_ts AS TIMESTAMP))) / 60.0
    ) AS billing_running_minutes,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_quantity > 0
    {ws_clause}
  GROUP BY u.usage_metadata.warehouse_id
),
combined_uptime AS (
  SELECT
    COALESCE(e.warehouse_id, b.warehouse_id) AS warehouse_id,
    COALESCE(e.total_running_minutes, b.billing_running_minutes) AS total_running_minutes,
    CASE WHEN e.warehouse_id IS NOT NULL THEN 'events' ELSE 'billing' END AS uptime_source
  FROM warehouse_uptime e
  FULL OUTER JOIN billing_agg b ON e.warehouse_id = b.warehouse_id
),
wh_info AS (
  -- max_clusters + auto_stop_minutes drive the two-path idle model. Column
  -- names match system.compute.warehouses schema.
  SELECT
    warehouse_id, warehouse_name, warehouse_size, workspace_id, warehouse_type,
    COALESCE(auto_stop_minutes, 10) AS auto_stop_mins,
    COALESCE(max_clusters, 1) AS max_num_clusters
  FROM system.compute.warehouses
  WHERE 1=1 {ws_clause_wh}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
-- Union-of-query-windows for busy time. Sum-of-durations overcounts under
-- concurrency; interval-merge collapses overlapping queries to a single window
-- so busy time never exceeds wall-clock even when N queries ran in parallel.
qw_raw AS (
  SELECT
    qh.compute.warehouse_id AS warehouse_id,
    GREATEST(qh.start_time, CAST(:start_ts AS TIMESTAMP)) AS w_start,
    LEAST(COALESCE(qh.end_time, CAST(:end_ts AS TIMESTAMP)), CAST(:end_ts AS TIMESTAMP)) AS w_end
  FROM system.query.history qh
  WHERE qh.start_time < CAST(:end_ts AS TIMESTAMP)
    AND COALESCE(qh.end_time, CAST(:end_ts AS TIMESTAMP)) > CAST(:start_ts AS TIMESTAMP)
    AND qh.compute.warehouse_id IS NOT NULL
),
qw_flagged AS (
  SELECT warehouse_id, w_start, w_end,
    MAX(w_end) OVER (
      PARTITION BY warehouse_id ORDER BY w_start
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS prev_max_end
  FROM qw_raw
),
qw_grouped AS (
  SELECT warehouse_id, w_start, w_end,
    SUM(CASE WHEN prev_max_end IS NULL OR w_start > prev_max_end THEN 1 ELSE 0 END)
      OVER (PARTITION BY warehouse_id ORDER BY w_start
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS grp
  FROM qw_flagged
),
qw_merged AS (
  SELECT warehouse_id, grp,
    MIN(w_start) AS grp_start,
    MAX(w_end) AS grp_end
  FROM qw_grouped
  GROUP BY warehouse_id, grp
),
busy_union AS (
  SELECT warehouse_id,
    SUM(GREATEST((UNIX_TIMESTAMP(grp_end) - UNIX_TIMESTAMP(grp_start)) / 60.0, 0)) AS busy_union_minutes
  FROM qw_merged
  GROUP BY warehouse_id
),
-- Warm-hold approximation: gaps between merged busy windows, each capped at
-- auto_stop_mins. Any gap longer than auto_stop_mins would have triggered a
-- shutdown, so only the first auto_stop_mins of the gap is billable warm-hold.
qw_with_lag AS (
  SELECT
    qm.warehouse_id, qm.grp_start, qm.grp_end,
    LAG(qm.grp_end) OVER (PARTITION BY qm.warehouse_id ORDER BY qm.grp_start) AS prev_end,
    wh.auto_stop_mins
  FROM qw_merged qm
  JOIN wh_info wh ON qm.warehouse_id = wh.warehouse_id
),
warm_hold AS (
  SELECT warehouse_id,
    SUM(GREATEST(
      LEAST(
        (UNIX_TIMESTAMP(grp_start) - UNIX_TIMESTAMP(prev_end)) / 60.0,
        auto_stop_mins * 1.0
      ),
      0
    )) AS warm_hold_minutes
  FROM qw_with_lag
  WHERE prev_end IS NOT NULL
  GROUP BY warehouse_id
),
lookback_minutes AS (
  SELECT (UNIX_TIMESTAMP(CAST(:end_ts AS TIMESTAMP))
          - UNIX_TIMESTAMP(CAST(:start_ts AS TIMESTAMP))) / 60.0 AS lookback_min
)
SELECT
  u.warehouse_id,
  COALESCE(i.warehouse_name, u.warehouse_id) AS warehouse_name,
  COALESCE(i.warehouse_size, 'Unknown') AS warehouse_size,
  CASE WHEN i.warehouse_type = 'SERVERLESS' THEN 'SERVERLESS' ELSE 'CLASSIC' END AS warehouse_type,
  i.workspace_id,
  u.uptime_source,
  ROUND(u.total_running_minutes) AS total_running_minutes,
  ROUND(COALESCE(bu.busy_union_minutes, 0)) AS busy_union_minutes,
  ROUND(GREATEST(u.total_running_minutes - COALESCE(bu.busy_union_minutes, 0), 0)) AS idle_minutes,
  CASE
    WHEN u.total_running_minutes > 0
    THEN ROUND(100.0 * GREATEST(u.total_running_minutes - COALESCE(bu.busy_union_minutes, 0), 0)
         / u.total_running_minutes, 1)
    ELSE 0.0
  END AS idle_pct,
  i.auto_stop_mins,
  i.max_num_clusters,
  ROUND(COALESCE(wh.warm_hold_minutes, 0)) AS warm_hold_minutes,
  CASE
    WHEN (COALESCE(bu.busy_union_minutes, 0) + COALESCE(wh.warm_hold_minutes, 0)) > 0
    THEN ROUND(100.0 * COALESCE(wh.warm_hold_minutes, 0)
         / (COALESCE(bu.busy_union_minutes, 0) + COALESCE(wh.warm_hold_minutes, 0)), 1)
    ELSE 0.0
  END AS keep_alive_score,
  COALESCE(c.total_spend, 0) AS total_spend,
  -- Only allocate spend to idle wall-clock for CLASSIC single-cluster
  -- warehouses. Serverless bills per-query with warm-hold at a reduced rate,
  -- not full-rate wall-clock. Multi-cluster warehouses have concurrent
  -- cluster billing that wall-clock cluster_count > 0 can't reconstruct.
  -- We also suppress attribution when uptime came from the billing fallback
  -- (missing lifecycle events) since the running_minutes denominator is
  -- itself uncertain there.
  CASE
    WHEN COALESCE(i.warehouse_type, 'CLASSIC') != 'SERVERLESS'
      AND i.max_num_clusters <= 1
      AND u.uptime_source = 'events'
      AND u.total_running_minutes > 0
    THEN COALESCE(c.total_spend, 0)
         * GREATEST(u.total_running_minutes - COALESCE(bu.busy_union_minutes, 0), 0)
         / u.total_running_minutes
    ELSE NULL
  END AS estimated_idle_spend,
  -- Low-confidence: serverless running > 95% of the lookback window is almost
  -- certainly a keep-alive artifact (something pings just under auto_stop_mins),
  -- not literal continuous compute. Flag it so the UI can badge the row.
  (i.warehouse_type = 'SERVERLESS'
    AND u.total_running_minutes >= 0.95 * (SELECT lookback_min FROM lookback_minutes)) AS low_confidence
FROM combined_uptime u
LEFT JOIN busy_union bu ON u.warehouse_id = bu.warehouse_id
LEFT JOIN warm_hold wh ON u.warehouse_id = wh.warehouse_id
JOIN wh_info i ON u.warehouse_id = i.warehouse_id
LEFT JOIN billing_agg c ON u.warehouse_id = c.warehouse_id
WHERE u.total_running_minutes >= 10
ORDER BY
  COALESCE(estimated_idle_spend, -1) DESC,
  warm_hold_minutes DESC,
  total_spend DESC
LIMIT 25
"""


def _build_serverless_check_sql(ws_clause_wh: str) -> str:
    return f"""
WITH latest_warehouses AS (
  SELECT warehouse_id, warehouse_type
  FROM system.compute.warehouses
  WHERE change_time >= CURRENT_TIMESTAMP() - INTERVAL 90 DAYS
  {ws_clause_wh}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
)
SELECT COUNT(*) AS serverless_count
FROM latest_warehouses
WHERE warehouse_type = 'SERVERLESS'
"""

_idle_time_cache: dict | None = None
_idle_time_cache_ts: float = 0.0
_IDLE_TIME_CACHE_TTL = 30 * 60


@router.get("/idle-time")
async def get_warehouse_idle_time(
    start_date: str | None = None,
    end_date: str | None = None,
    workspace_ids: str | None = None,
) -> dict[str, Any]:
    """Idle time per warehouse: uptime from warehouse_events (with 7-day lookback) minus active query time.

    Falls back to hourly billing buckets for classic warehouses that emit no lifecycle events
    in the window — each row carries `uptime_source` = 'events' or 'billing' so the UI can flag
    the approximation.
    """
    global _idle_time_cache, _idle_time_cache_ts
    from backend import workspace_filter as wf

    # backend.db has no date-default helpers; importing them made every request 500.
    sd = start_date or (date.today() - timedelta(days=30)).isoformat()
    ed = end_date or date.today().isoformat()
    if _warehouse_scope_blocked():
        return {
            **_shared_scope_unavailable(),
            "warehouses": [],
            "serverless_detected": False,
            "start_date": sd,
            "end_date": ed,
        }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    cache_key = f"{sd}:{ed}:{','.join(id_list) if id_list else 'pool:' + wf.pool_cache_tag()}"

    if (_idle_time_cache is not None
            and (time.time() - _idle_time_cache_ts) < _IDLE_TIME_CACHE_TTL
            and _idle_time_cache.get("_cache_key") == cache_key):
        return {k: v for k, v in _idle_time_cache.items() if k != "_cache_key"}

    ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
    ws_clause_wh = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)

    _ed_dt = date.fromisoformat(ed)
    params = {
        "start_ts": f"{sd} 00:00:00",
        "end_ts": (_ed_dt + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
        "start_date": sd,
        "end_date": ed,
    }

    from backend.lakebase_read import (
        PG_WAREHOUSE_IDLE, PG_WAREHOUSE_IDLE_SERVERLESS, gold_read, warehouse_view_fallback,
    )
    sql = _build_idle_time_sql(ws_clause, ws_clause_wh)
    try:
        rows = await asyncio.to_thread(
            gold_read, "warehouse_idle", PG_WAREHOUSE_IDLE, params, ws_clause,
            sql if warehouse_view_fallback() else None,
        )
    except Exception as e:
        logger.warning("warehouse idle-time query failed: %s", e)
        return {"available": False, "error": str(e), "warehouses": [], "serverless_detected": False,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    warehouses = []
    for row in rows:
        _est_idle = row.get("estimated_idle_spend")
        warehouses.append({
            "warehouse_id": row.get("warehouse_id"),
            # Gold rows carry NULL for '__na__' names (and ids, for the unattributed bucket);
            # match the live SQL's COALESCE to the id.
            "warehouse_name": row.get("warehouse_name") or row.get("warehouse_id") or "Unattributed",
            "warehouse_size": row.get("warehouse_size"),
            "warehouse_type": row.get("warehouse_type") or "CLASSIC",
            "workspace_id": str(row.get("workspace_id") or ""),
            "uptime_source": row.get("uptime_source") or "events",
            "total_running_minutes": int(row.get("total_running_minutes") or 0),
            # Renamed from total_query_minutes — busy time is now union-of-query-windows
            # so it's bounded by wall-clock even under high concurrency.
            "busy_union_minutes": int(row.get("busy_union_minutes") or 0),
            "idle_minutes": int(row.get("idle_minutes") or 0),
            "idle_pct": (
                float(row.get("idle_pct") or 0)
                if row.get("idle_pct") is not None
                else (
                    round(100.0 * int(row.get("idle_minutes") or 0)
                          / int(row.get("total_running_minutes") or 0), 1)
                    if int(row.get("total_running_minutes") or 0) > 0 else 0.0
                )
            ),
            "warm_hold_minutes": int(row.get("warm_hold_minutes") or 0),
            "keep_alive_score": (
                float(row.get("keep_alive_score") or 0)
                if row.get("keep_alive_score") is not None
                else (
                    round(100.0 * int(row.get("warm_hold_minutes") or 0)
                          / (int(row.get("busy_union_minutes") or 0) + int(row.get("warm_hold_minutes") or 0)), 1)
                    if (int(row.get("busy_union_minutes") or 0) + int(row.get("warm_hold_minutes") or 0)) > 0 else 0.0
                )
            ),
            "auto_stop_mins": int(row.get("auto_stop_mins") or 10),
            "max_num_clusters": int(row.get("max_num_clusters") or 1),
            "total_spend": float(row.get("total_spend") or 0),
            # None when the SQL suppressed attribution (serverless / multi-cluster /
            # billing-fallback uptime). Frontend renders "—" for null.
            "estimated_idle_spend": float(_est_idle) if _est_idle is not None else None,
            "low_confidence": bool(row.get("low_confidence") or False),
        })
    lookback_min = max((
        ( date.fromisoformat(ed) - date.fromisoformat(sd) ).days + 1
    ) * 1440, 1)
    for wh in warehouses:
        if (wh.get("warehouse_type") == "SERVERLESS"
                and wh.get("total_running_minutes", 0) >= 0.95 * lookback_min):
            wh["low_confidence"] = True

    # If no classic warehouses found, check whether serverless warehouses exist (can't generate lifecycle events)
    serverless_detected = False
    if not warehouses:
        try:
            check_sql = _build_serverless_check_sql(ws_clause_wh)
            check_rows = await asyncio.to_thread(
                gold_read, "warehouse_idle_serverless", PG_WAREHOUSE_IDLE_SERVERLESS, {}, "",
                check_sql if warehouse_view_fallback() else None,
            )
            serverless_detected = bool(check_rows and int(check_rows[0].get("serverless_count") or 0) > 0)
        except Exception:
            pass

    payload = {
        "available": True,
        "warehouses": warehouses,
        "serverless_detected": serverless_detected,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _idle_time_cache = {**payload, "_cache_key": cache_key}
    _idle_time_cache_ts = time.time()
    return payload


@router.get("/{warehouse_id}")
async def get_warehouse_health_detail(warehouse_id: str) -> dict[str, Any]:
    """Return health detail for a specific warehouse (served from cache)."""
    result = await get_warehouse_health()
    recs = [r for r in result.get("recommendations", []) if r.get("warehouse_id") == warehouse_id]
    return {
        "available": result["available"],
        "warehouse_id": warehouse_id,
        "recommendations": recs,
        "generated_at": result.get("generated_at"),
    }
