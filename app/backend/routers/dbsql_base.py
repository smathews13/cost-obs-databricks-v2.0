"""
Shared DBSQL Query Cost Attribution logic.

Provides a factory function to create parameterized routers for both
the original and PrPr cost-per-query materialized views. The only
difference between the two is the table name.
"""

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
    get_catalog_schema,
    get_host_url,
    source_label_filter_clause,
    source_scope_unavailable,
)

logger = logging.getLogger(__name__)

_dbsql_bundle_inflight: set[str] = set()
_dbsql_bundle_inflight_lock = threading.Lock()


def _resolve_url(url: str | None, host: str) -> str | None:
    """Resolve MV URLs by replacing placeholders and prepending host to relative paths.

    The original MV bakes in 'https://DATABRICKS_HOST/...' as a literal string.
    The PrPr MV uses relative paths like '/sql/history?...'.
    This function normalises both to absolute URLs with the actual host.
    """
    if not url:
        return None
    # Original MV: literal placeholder
    url = url.replace("https://DATABRICKS_HOST", host)
    url = url.replace("https://databricks_host", host)
    # PrPr MV: relative paths
    if url.startswith("/") and host:
        url = f"{host}{url}"
    return url


def _build_queries(table_name: str) -> dict[str, str]:
    """Return SQL templates parameterized by table name."""
    return {
        "check_mv": f"""
            SELECT 1
            FROM `{{catalog}}`.information_schema.tables
            WHERE table_schema = '{{schema}}'
              AND table_name = '{table_name}'
            LIMIT 1
        """,
        "data_range": f"""
            SELECT
              CAST(MIN(start_time) AS DATE) as earliest_date,
              CAST(MAX(start_time) AS DATE) as latest_date,
              1 as total_rows
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
        """,
        "by_source": f"""
            SELECT
              query_source_type,
              COUNT(*) as query_count,
              SUM(query_attributed_dollars_estimation) as total_spend,
              SUM(query_attributed_dbus_estimation) as total_dbus,
              AVG(query_attributed_dollars_estimation) as avg_cost_per_query
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            GROUP BY query_source_type
            ORDER BY total_spend DESC
        """,
        "by_user": f"""
            SELECT
              executed_by,
              query_source_type,
              COUNT(*) as query_count,
              SUM(query_attributed_dollars_estimation) as total_spend,
              SUM(query_attributed_dbus_estimation) as total_dbus
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            GROUP BY executed_by, query_source_type
            ORDER BY total_spend DESC
            LIMIT 100
        """,
        "top_queries": f"""
            SELECT
              statement_id,
              query_source_type,
              query_source_id,
              executed_by,
              warehouse_id,
              workspace_id,
              SUBSTRING(statement_text, 1, 200) as statement_preview,
              duration_seconds,
              query_attributed_dollars_estimation as cost,
              query_attributed_dbus_estimation as dbus,
              query_profile_url,
              url_helper as source_url,
              start_time,
              end_time
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            ORDER BY query_attributed_dollars_estimation DESC
            LIMIT :limit
        """,
        "summary": f"""
            WITH q AS (
              SELECT
                query_attributed_dollars_estimation,
                query_attributed_dbus_estimation,
                duration_seconds,
                executed_by,
                warehouse_id,
                DATE(start_time) as query_date
              FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
              WHERE DATE(start_time) >= :start_date
                AND DATE(start_time) <= :end_date
            ),
            daily_dur AS (
              SELECT query_date, AVG(duration_seconds) as daily_avg_dur
              FROM q GROUP BY query_date
            )
            SELECT
              COUNT(*) as total_queries,
              COUNT(DISTINCT executed_by) as unique_users,
              COUNT(DISTINCT warehouse_id) as unique_warehouses,
              SUM(query_attributed_dollars_estimation) as total_spend,
              SUM(query_attributed_dbus_estimation) as total_dbus,
              AVG(query_attributed_dollars_estimation) as avg_cost_per_query,
              (SELECT AVG(daily_avg_dur) FROM daily_dur) as avg_duration_seconds
            FROM q
        """,
        "by_warehouse": f"""
            SELECT
              warehouse_id,
              COUNT(*) as query_count,
              COUNT(DISTINCT executed_by) as unique_users,
              SUM(query_attributed_dollars_estimation) as total_spend,
              SUM(query_attributed_dbus_estimation) as total_dbus
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            GROUP BY warehouse_id
            ORDER BY total_spend DESC
            LIMIT 50
        """,
        "timeseries": f"""
            SELECT
              DATE(start_time) as date,
              query_source_type,
              COUNT(*) as query_count,
              SUM(query_attributed_dollars_estimation) as daily_spend,
              SUM(query_attributed_dbus_estimation) as daily_dbus
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
            GROUP BY DATE(start_time), query_source_type
            ORDER BY date
        """,
        "queries_by_user": f"""
            SELECT
              statement_id,
              query_source_type,
              query_source_id,
              executed_by,
              warehouse_id,
              workspace_id,
              SUBSTRING(statement_text, 1, 200) as statement_preview,
              duration_seconds,
              query_attributed_dollars_estimation as cost,
              query_attributed_dbus_estimation as dbus,
              query_profile_url,
              url_helper as source_url,
              start_time,
              end_time
            FROM `{{catalog}}`.`{{schema}}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
              AND executed_by = :user
            ORDER BY query_attributed_dollars_estimation DESC
            LIMIT :limit
        """,
    }


def _default_dates(
    start_date: str | None, end_date: str | None
) -> tuple[str, str]:
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    return start_date, end_date


def create_dbsql_router(table_name: str) -> APIRouter:
    """Create a DBSQL cost-attribution router for the given MV table name."""
    router = APIRouter()
    sql = _build_queries(table_name)

    def _exec(query_key: str, params: dict, catalog: str, schema: str, ws_clause: str = "") -> list[dict]:
        """Serve SQL 360 rollups from Lakebase when on; warehouse gold as fallback."""
        template = sql[query_key]
        query = template.format(catalog=catalog, schema=schema)
        if ws_clause:
            query = query.replace(
                "AND DATE(start_time) <= :end_date",
                f"AND DATE(start_time) <= :end_date\n              {ws_clause}",
                1,
            )

        def _delta():
            return execute_query(query, params)

        from backend.lakebase_read import (
            PG_DBSQL_BY_SOURCE,
            PG_DBSQL_BY_USER,
            PG_DBSQL_BY_WAREHOUSE,
            PG_DBSQL_SUMMARY,
            PG_DBSQL_TIMESERIES,
            PG_DBSQL_TOP,
            dual_read,
            warehouse_view_fallback,
        )
        pg = {
            "summary": PG_DBSQL_SUMMARY,
            "by_source": PG_DBSQL_BY_SOURCE,
            "by_user": PG_DBSQL_BY_USER,
            "by_warehouse": PG_DBSQL_BY_WAREHOUSE,
            "timeseries": PG_DBSQL_TIMESERIES,
            "top_queries": PG_DBSQL_TOP,
        }.get(query_key)
        if pg:
            def _gold():
                from backend.db import apply_mv_overrides, get_catalog_schema
                from backend.materialized_views_extra import DBSQL_GOLD_SQL
                gold = DBSQL_GOLD_SQL.get(query_key)
                if gold:
                    cat, sch = get_catalog_schema()
                    sql = gold.format(
                        catalog=cat, schema=sch,
                        ws_filter=ws_clause + source_label_filter_clause(gold),
                    )
                    return execute_query(apply_mv_overrides(sql, cat, sch), params)
                if warehouse_view_fallback():
                    return _delta()
                return []

            return dual_read(
                f"dbsql_{query_key}", pg, params, ws_clause, fetch_delta=_gold,
            )
        if not warehouse_view_fallback():
            return []
        return _delta()

    def _compute_dbsql_bundle(start_date: str, end_date: str, id_list: list | None, workspace_ids_str: str | None, dkey: str) -> None:
        """Background worker: run all DBSQL queries in parallel, build response, write to Delta cache."""
        _start = _time.time()
        try:
            catalog, schema = get_catalog_schema()
            ws_clause = wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)
            params = {"start_date": start_date, "end_date": end_date}

            from backend.lakebase_read import (
                PG_REGION_SCOPE,
                PG_WH_META,
                PG_WH_TYPE_BILLING,
                gold_read,
            )
            queries = [
                ("summary",      lambda: _exec("summary",      params, catalog, schema, ws_clause)),
                ("by_source",    lambda: _exec("by_source",    params, catalog, schema, ws_clause)),
                ("by_user",      lambda: _exec("by_user",      params, catalog, schema, ws_clause)),
                ("by_warehouse", lambda: _exec("by_warehouse", params, catalog, schema, ws_clause)),
                ("timeseries",   lambda: _exec("timeseries",   params, catalog, schema, ws_clause)),
                ("wh_meta",      lambda: gold_read("wh_meta", PG_WH_META, params, ws_clause)),
                ("wh_type_billing", lambda: gold_read("wh_type_billing", PG_WH_TYPE_BILLING, params, ws_clause)),
                ("region_scope", lambda: gold_read("region_scope", PG_REGION_SCOPE, params)),
            ]

            results = execute_queries_parallel(queries, 120.0)

            _empty = {"available": True, "start_date": start_date, "end_date": end_date}

            # summary
            summary_rows = results.get("summary") or []
            if summary_rows:
                r = summary_rows[0]
                summary = {
                    "available": True,
                    "total_queries": r.get("total_queries") or 0,
                    "unique_users": r.get("unique_users") or 0,
                    "unique_warehouses": r.get("unique_warehouses") or 0,
                    "total_spend": float(r.get("total_spend") or 0),
                    "total_dbus": float(r.get("total_dbus") or 0),
                    "avg_cost_per_query": float(r.get("avg_cost_per_query") or 0),
                    "avg_duration_seconds": float(r.get("avg_duration_seconds") or 0),
                    "start_date": start_date, "end_date": end_date,
                }
            else:
                summary = {**_empty, "total_queries": 0, "unique_users": 0, "unique_warehouses": 0,
                           "total_spend": 0, "total_dbus": 0, "avg_cost_per_query": 0, "avg_duration_seconds": 0}

            # by_source
            sources, total_src_spend = [], 0.0
            for r in (results.get("by_source") or []):
                spend = float(r.get("total_spend") or 0)
                total_src_spend += spend
                sources.append({"query_source_type": r.get("query_source_type") or "Unknown",
                                 "query_count": r.get("query_count") or 0, "total_spend": spend,
                                 "total_dbus": float(r.get("total_dbus") or 0),
                                 "avg_cost_per_query": float(r.get("avg_cost_per_query") or 0)})
            for s in sources:
                s["percentage"] = (s["total_spend"] / total_src_spend * 100) if total_src_spend > 0 else 0
            by_source = {**_empty, "sources": sources, "total_spend": total_src_spend}

            # by_user
            users = [{"executed_by": r.get("executed_by") or "Unknown",
                      "query_source_type": r.get("query_source_type") or "Unknown",
                      "query_count": r.get("query_count") or 0,
                      "total_spend": float(r.get("total_spend") or 0),
                      "total_dbus": float(r.get("total_dbus") or 0)}
                     for r in (results.get("by_user") or [])]
            by_user = {**_empty, "users": users}

            # by_warehouse (enriched with metadata)
            wh_meta: dict[str, dict] = {}
            for r in (results.get("wh_meta") or []):
                wid = r.get("warehouse_id")
                if wid:
                    wh_meta[wid] = {"warehouse_name": r.get("warehouse_name"),
                                    "warehouse_type": r.get("warehouse_type") or "CLASSIC",
                                    "warehouse_size": r.get("warehouse_size") or "UNKNOWN",
                                    "workspace_id": str(r.get("workspace_id")) if r.get("workspace_id") else None,
                                    "workspace_name": r.get("workspace_name")}
            warehouses, total_wh_spend = [], 0.0
            for r in (results.get("by_warehouse") or []):
                spend = float(r.get("total_spend") or 0)
                total_wh_spend += spend
                wid = r.get("warehouse_id")
                meta = wh_meta.get(wid, {})
                warehouses.append({"warehouse_id": wid, "warehouse_name": meta.get("warehouse_name"),
                                    "warehouse_type": meta.get("warehouse_type"), "warehouse_size": meta.get("warehouse_size"),
                                    "workspace_id": meta.get("workspace_id"), "workspace_name": meta.get("workspace_name"),
                                    "query_count": r.get("query_count") or 0, "unique_users": r.get("unique_users") or 0,
                                    "total_spend": spend, "total_dbus": float(r.get("total_dbus") or 0)})
            for wh in warehouses:
                wh["percentage"] = (wh["total_spend"] / total_wh_spend * 100) if total_wh_spend > 0 else 0
            by_warehouse = {**_empty, "warehouses": warehouses, "total_spend": total_wh_spend}

            # timeseries
            ts_by_date: dict[str, dict] = {}
            src_types_set: set[str] = set()
            for r in (results.get("timeseries") or []):
                d = str(r.get("date"))
                st = r.get("query_source_type") or "Unknown"
                src_types_set.add(st)
                if d not in ts_by_date:
                    ts_by_date[d] = {"date": d}
                ts_by_date[d][st] = float(r.get("daily_spend") or 0)
            src_types = sorted(src_types_set)
            ts_list = []
            for d in sorted(ts_by_date):
                row_d = ts_by_date[d]
                for st in src_types:
                    row_d.setdefault(st, 0)
                ts_list.append(row_d)
            timeseries = {**_empty, "timeseries": ts_list, "source_types": src_types}

            # warehouse_type_timeseries — derive type lookup from wh_meta (already fetched above)
            wh_type_lookup = {wid: info.get("warehouse_type") or "CLASSIC" for wid, info in wh_meta.items()}
            wh_ts_by_date: dict[str, dict] = {}
            wh_type_set: set[str] = set()
            for r in (results.get("wh_type_billing") or []):
                d = str(r.get("date"))
                wid = r.get("warehouse_id") or ""
                # A warehouse_id present in billing but absent from wh_meta is
                # out-of-region: system.compute.warehouses is region-scoped while
                # system.billing.usage is account-wide. Do NOT default these to
                # CLASSIC — that mislabels out-of-region serverless spend. Mark
                # them "Unknown" so the chart is honest about what it can't classify.
                wt = wh_type_lookup.get(wid, "Unknown")
                spend = float(r.get("daily_spend") or 0)
                wh_type_set.add(wt)
                if d not in wh_ts_by_date:
                    wh_ts_by_date[d] = {"date": d}
                wh_ts_by_date[d][wt] = wh_ts_by_date[d].get(wt, 0) + spend
            wh_types_list = sorted(wh_type_set)
            wh_ts_list = []
            for d in sorted(wh_ts_by_date):
                row_d = wh_ts_by_date[d]
                for wt in wh_types_list:
                    row_d.setdefault(wt, 0)
                wh_ts_list.append(row_d)
            warehouse_type_timeseries = {"available": True, "timeseries": wh_ts_list, "warehouse_types": wh_types_list}

            # region_scope — surface when this deployment's region-scoped system tables
            # (system.compute / system.query) cover fewer workspaces than account-wide
            # billing, so the UI can explain why SQL/Optimize detail looks incomplete.
            def _first_count(key: str, col: str) -> int:
                rows = results.get(key) or []
                try:
                    return int(rows[0].get(col) or 0) if rows else 0
                except (ValueError, TypeError, AttributeError):
                    return 0
            scope_rows = results.get("region_scope") or []
            if scope_rows:
                billing_ws = int(scope_rows[0].get("billing_ws") or 0)
                compute_ws = int(scope_rows[0].get("compute_ws") or 0)
            else:
                billing_ws = _first_count("region_billing_ws", "ws_count")
                compute_ws = _first_count("region_compute_ws", "ws_count")
            # Only claim a gap when both counts are trustworthy (>0) and billing exceeds
            # compute. If either query failed/returned 0, stay silent rather than alarm.
            missing_ws = max(billing_ws - compute_ws, 0) if (billing_ws > 0 and compute_ws > 0) else 0
            # Gate the banner on the region-scoped SQL detail actually being present.
            # On a fresh deploy the region-scoped system tables (system.compute /
            # system.query) can read back partial/empty while the warehouse is still
            # cold, which briefly undercounts in-region workspaces and flashes a false
            # "region-limited" banner that clears on the next refresh. There is no detail
            # to caveat until real query/warehouse rows exist, so hold the banner until
            # then — the whole point of the banner is to explain limited *detail below*.
            detail_ready = (
                bool((by_warehouse or {}).get("warehouses"))
                or int((summary or {}).get("total_queries") or 0) > 0
            )
            region_scope = {
                "billing_workspace_count": billing_ws,
                "in_region_workspace_count": compute_ws,
                "missing_workspace_count": missing_ws,
                "limited": missing_ws > 0 and detail_ready,
            }

            _resp = {
                "available": True,
                "summary": summary,
                "by_source": by_source,
                "by_user": by_user,
                "by_warehouse": by_warehouse,
                "timeseries": timeseries,
                "warehouse_type_timeseries": warehouse_type_timeseries,
                "region_scope": region_scope,
                "start_date": start_date,
                "end_date": end_date,
            }
            delta_cache_put(dkey, f"dbsql:{table_name}:dashboard-bundle", _resp,
                            ttl_seconds=cache_ttls.BUNDLE_FILTERED if id_list else cache_ttls.BUNDLE)
            logger.info("dbsql dashboard-bundle background compute complete: %.1fs table=%s", _time.time() - _start, table_name)
        except Exception as e:
            logger.error("dbsql dashboard-bundle background compute failed: %s", e, exc_info=True)
            error_code = str(getattr(e, "code", "DBSQL_BUNDLE_FAILED"))
            source_unsupported = error_code == "SOURCE_SCOPE_UNSUPPORTED"
            try:
                delta_cache_put(
                    dkey, f"dbsql:{table_name}:dashboard-bundle",
                    {
                        "available": False,
                        "reason": (
                            "shared_scope_unsupported"
                            if source_unsupported
                            else "producer_failed"
                        ),
                        "reason_detail": (
                            "The selected shared source does not publish SQL detail."
                            if source_unsupported
                            else "SQL cost data is temporarily unavailable."
                        ),
                        "error_code": error_code,
                        "message": (
                            "The selected shared source does not publish SQL detail."
                            if source_unsupported
                            else "SQL cost data is temporarily unavailable."
                        ),
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                    ttl_seconds=60,
                )
            except Exception:
                pass
        finally:
            with _dbsql_bundle_inflight_lock:
                _dbsql_bundle_inflight.discard(dkey)

    def _ws_clause(workspace_ids: str | None) -> str:
        id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
        return wf.build_ws_filter_clause(col="workspace_id", id_list=id_list)

    async def check_mv_status() -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        from backend.lakebase_read import warehouse_view_fallback

        available = False
        data_range = {}
        try:
            if not warehouse_view_fallback():
                from backend.db import lakebase_query
                rows = await asyncio.to_thread(
                    lakebase_query, "SELECT 1 FROM daily_dbsql_source LIMIT 1", {}
                )
                available = True
                if rows is not None:
                    try:
                        rng = await asyncio.to_thread(
                            lakebase_query,
                            "SELECT MIN(usage_date) AS earliest_date, MAX(usage_date) AS latest_date, "
                            "COUNT(*) AS total_rows FROM daily_dbsql_source",
                            {},
                        )
                        if rng:
                            row = rng[0]
                            data_range = {
                                "earliest_date": str(row["earliest_date"]) if row.get("earliest_date") else None,
                                "latest_date": str(row["latest_date"]) if row.get("latest_date") else None,
                                "total_rows": int(row.get("total_rows") or 0),
                            }
                    except Exception as e:
                        logger.warning("Could not get Lakebase data range for daily_dbsql_source: %s", e)
            else:
                query = sql["check_mv"].format(catalog=catalog, schema=schema)
                results = await asyncio.to_thread(execute_query, query)
                available = len(results) > 0
        except Exception as e:
            logger.warning(f"DBSQL cost MV ({table_name}) not available: {e}")
            available = False

        if available and not data_range:
            try:
                range_query = sql["data_range"].format(catalog=catalog, schema=schema)
                range_results = await asyncio.to_thread(execute_query, range_query)
                if range_results:
                    row = range_results[0]
                    data_range = {
                        "earliest_date": str(row["earliest_date"]) if row.get("earliest_date") else None,
                        "latest_date": str(row["latest_date"]) if row.get("latest_date") else None,
                        "total_rows": int(row.get("total_rows") or 0),
                    }
            except Exception as e:
                logger.warning(f"Could not get data range for {table_name}: {e}")

        return {
            "mv_available": available,
            "catalog": catalog,
            "schema": schema,
            "table": table_name if available else None,
            "data_range": data_range,
        }

    @router.get("/status")
    async def get_status() -> dict[str, Any]:
        return await check_mv_status()

    @router.get("/summary")
    async def get_summary(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {
                "available": False,
                "message": f"{table_name} MV not configured",
                "start_date": start_date,
                "end_date": end_date,
            }

        results = _exec("summary", {"start_date": start_date, "end_date": end_date}, catalog, schema, _ws_clause(workspace_ids))

        data_range = status.get("data_range", {})

        if not results or not (results[0].get("total_queries") or 0):
            return {
                "available": True,
                "total_queries": 0, "unique_users": 0, "unique_warehouses": 0,
                "total_spend": 0, "total_dbus": 0,
                "avg_cost_per_query": 0, "avg_duration_seconds": 0,
                "start_date": start_date, "end_date": end_date,
                "data_range": data_range,
            }

        row = results[0]
        return {
            "available": True,
            "total_queries": row.get("total_queries") or 0,
            "unique_users": row.get("unique_users") or 0,
            "unique_warehouses": row.get("unique_warehouses") or 0,
            "total_spend": float(row.get("total_spend") or 0),
            "total_dbus": float(row.get("total_dbus") or 0),
            "avg_cost_per_query": float(row.get("avg_cost_per_query") or 0),
            "avg_duration_seconds": float(row.get("avg_duration_seconds") or 0),
            "start_date": start_date,
            "end_date": end_date,
            "data_range": data_range,
        }

    @router.get("/by-source")
    async def get_by_source(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "sources": [], "start_date": start_date, "end_date": end_date}

        results = _exec("by_source", {"start_date": start_date, "end_date": end_date}, catalog, schema, _ws_clause(workspace_ids))

        sources = []
        total_spend = 0
        for row in results:
            spend = float(row.get("total_spend") or 0)
            total_spend += spend
            sources.append({
                "query_source_type": row.get("query_source_type") or "Unknown",
                "query_count": row.get("query_count") or 0,
                "total_spend": spend,
                "total_dbus": float(row.get("total_dbus") or 0),
                "avg_cost_per_query": float(row.get("avg_cost_per_query") or 0),
            })

        for source in sources:
            source["percentage"] = (source["total_spend"] / total_spend * 100) if total_spend > 0 else 0

        return {"available": True, "sources": sources, "total_spend": total_spend, "start_date": start_date, "end_date": end_date}

    @router.get("/by-user")
    async def get_by_user(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "users": [], "start_date": start_date, "end_date": end_date}

        results = _exec("by_user", {"start_date": start_date, "end_date": end_date}, catalog, schema, _ws_clause(workspace_ids))

        users = []
        for row in results:
            users.append({
                "executed_by": row.get("executed_by") or "Unknown",
                "query_source_type": row.get("query_source_type") or "Unknown",
                "query_count": row.get("query_count") or 0,
                "total_spend": float(row.get("total_spend") or 0),
                "total_dbus": float(row.get("total_dbus") or 0),
            })

        return {"available": True, "users": users, "start_date": start_date, "end_date": end_date}

    @router.get("/by-warehouse")
    async def get_by_warehouse(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "warehouses": [], "start_date": start_date, "end_date": end_date}

        results = _exec("by_warehouse", {"start_date": start_date, "end_date": end_date}, catalog, schema, _ws_clause(workspace_ids))

        # Look up warehouse names and types from system.compute.warehouses
        warehouse_meta: dict[str, dict[str, str]] = {}
        try:
            meta_results = await asyncio.to_thread(execute_query, """
                SELECT w.warehouse_id, MAX(w.warehouse_name) as warehouse_name,
                       MAX(w.warehouse_type) as warehouse_type, MAX(w.warehouse_size) as warehouse_size,
                       MAX(w.workspace_id) as workspace_id,
                       MAX(ws.workspace_name) as workspace_name
                FROM system.compute.warehouses w
                LEFT JOIN system.access.workspaces_latest ws ON w.workspace_id = ws.workspace_id
                GROUP BY w.warehouse_id
            """)
            for r in (meta_results or []):
                wid = r.get("warehouse_id")
                if wid:
                    warehouse_meta[wid] = {
                        "warehouse_name": r.get("warehouse_name"),
                        "warehouse_type": r.get("warehouse_type") or "CLASSIC",
                        "warehouse_size": r.get("warehouse_size") or "UNKNOWN",
                        "workspace_id": str(r.get("workspace_id")) if r.get("workspace_id") else None,
                        "workspace_name": r.get("workspace_name"),
                    }
        except Exception as e:
            logger.warning(f"Could not look up warehouse metadata: {e}")

        warehouses = []
        total_spend = 0
        for row in results:
            spend = float(row.get("total_spend") or 0)
            total_spend += spend
            wid = row.get("warehouse_id")
            meta = warehouse_meta.get(wid, {})
            warehouses.append({
                "warehouse_id": wid,
                "warehouse_name": meta.get("warehouse_name"),
                "warehouse_type": meta.get("warehouse_type"),
                "warehouse_size": meta.get("warehouse_size"),
                "workspace_id": meta.get("workspace_id"),
                "workspace_name": meta.get("workspace_name"),
                "query_count": row.get("query_count") or 0,
                "unique_users": row.get("unique_users") or 0,
                "total_spend": spend,
                "total_dbus": float(row.get("total_dbus") or 0),
            })

        for warehouse in warehouses:
            warehouse["percentage"] = (warehouse["total_spend"] / total_spend * 100) if total_spend > 0 else 0

        return {"available": True, "warehouses": warehouses, "total_spend": total_spend, "start_date": start_date, "end_date": end_date}

    @router.get("/top-queries")
    async def get_top_queries(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        limit: int = Query(default=200, le=200),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "queries": [], "start_date": start_date, "end_date": end_date}

        results = _exec("top_queries", {"start_date": start_date, "end_date": end_date, "limit": limit}, catalog, schema, _ws_clause(workspace_ids))

        host = get_host_url()
        queries = []
        for row in results:
            queries.append({
                "statement_id": row.get("statement_id"),
                "query_source_type": row.get("query_source_type") or "Unknown",
                "query_source_id": row.get("query_source_id"),
                "executed_by": row.get("executed_by") or "Unknown",
                "warehouse_id": row.get("warehouse_id"),
                "workspace_id": row.get("workspace_id"),
                "statement_preview": row.get("statement_preview") or "",
                "duration_seconds": float(row.get("duration_seconds") or 0),
                "cost": float(row.get("cost") or 0),
                "dbus": float(row.get("dbus") or 0),
                "query_profile_url": _resolve_url(row.get("query_profile_url"), host),
                "source_url": _resolve_url(row.get("source_url"), host),
                "start_time": str(row.get("start_time")) if row.get("start_time") else None,
                "end_time": str(row.get("end_time")) if row.get("end_time") else None,
            })

        return {"available": True, "queries": queries, "start_date": start_date, "end_date": end_date}

    @router.get("/top-queries-by-source")
    async def get_top_queries_by_source(
        source_type: str = Query(..., description="Query source type to filter by"),
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        limit: int = Query(default=5, le=20),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "queries": [], "start_date": start_date, "end_date": end_date}

        safe_limit = min(int(limit), 20)
        query = f"""
            SELECT
              statement_id,
              query_source_type,
              query_source_id,
              executed_by,
              warehouse_id,
              workspace_id,
              SUBSTRING(statement_text, 1, 200) as statement_preview,
              duration_seconds,
              query_attributed_dollars_estimation as cost,
              query_attributed_dbus_estimation as dbus,
              query_profile_url,
              url_helper as source_url,
              start_time,
              end_time
            FROM `{catalog}`.`{schema}`.`{table_name}`
            WHERE DATE(start_time) >= :start_date
              AND DATE(start_time) <= :end_date
              AND query_source_type = :source_type
            ORDER BY query_attributed_dollars_estimation DESC
            LIMIT {safe_limit}
        """
        params = {"start_date": start_date, "end_date": end_date, "source_type": source_type}
        results = await asyncio.to_thread(execute_query, query, params)

        host = get_host_url()
        queries = []
        for row in results:
            queries.append({
                "statement_id": row.get("statement_id"),
                "query_source_type": row.get("query_source_type") or "Unknown",
                "query_source_id": row.get("query_source_id"),
                "executed_by": row.get("executed_by") or "Unknown",
                "statement_preview": row.get("statement_preview") or "",
                "duration_seconds": float(row.get("duration_seconds") or 0),
                "cost": float(row.get("cost") or 0),
                "dbus": float(row.get("dbus") or 0),
                "query_profile_url": _resolve_url(row.get("query_profile_url"), host),
                "source_url": _resolve_url(row.get("source_url"), host),
            })

        return {"available": True, "queries": queries, "source_type": source_type, "start_date": start_date, "end_date": end_date}

    @router.get("/queries-by-user")
    async def get_queries_by_user(
        user: str = Query(..., description="Raw executed_by identity value"),
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        limit: int = Query(default=100, le=200),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "queries": [], "user": user, "start_date": start_date, "end_date": end_date}

        results = _exec(
            "queries_by_user",
            {"start_date": start_date, "end_date": end_date, "user": user, "limit": limit},
            catalog, schema, _ws_clause(workspace_ids),
        )

        host = get_host_url()
        queries = []
        for row in results:
            queries.append({
                "statement_id": row.get("statement_id"),
                "query_source_type": row.get("query_source_type") or "Unknown",
                "query_source_id": row.get("query_source_id"),
                "executed_by": row.get("executed_by") or "Unknown",
                "warehouse_id": row.get("warehouse_id"),
                "workspace_id": row.get("workspace_id"),
                "statement_preview": row.get("statement_preview") or "",
                "duration_seconds": float(row.get("duration_seconds") or 0),
                "cost": float(row.get("cost") or 0),
                "dbus": float(row.get("dbus") or 0),
                "query_profile_url": _resolve_url(row.get("query_profile_url"), host),
                "source_url": _resolve_url(row.get("source_url"), host),
                "start_time": str(row.get("start_time")) if row.get("start_time") else None,
                "end_time": str(row.get("end_time")) if row.get("end_time") else None,
            })

        total_spend = sum(q["cost"] for q in queries)
        return {
            "available": True,
            "queries": queries,
            "user": user,
            "total_spend": total_spend,
            "query_count": len(queries),
            "start_date": start_date,
            "end_date": end_date,
        }

    @router.get("/timeseries")
    async def get_timeseries(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        catalog, schema = get_catalog_schema()
        start_date, end_date = _default_dates(start_date, end_date)

        status = await check_mv_status()
        if not status["mv_available"]:
            return {"available": False, "timeseries": [], "source_types": [], "start_date": start_date, "end_date": end_date}

        results = _exec("timeseries", {"start_date": start_date, "end_date": end_date}, catalog, schema, _ws_clause(workspace_ids))

        data_by_date: dict[str, dict[str, Any]] = {}
        source_types_set: set[str] = set()

        for row in results:
            date_str = str(row.get("date"))
            source_type = row.get("query_source_type") or "Unknown"
            spend = float(row.get("daily_spend") or 0)

            source_types_set.add(source_type)
            if date_str not in data_by_date:
                data_by_date[date_str] = {"date": date_str}
            data_by_date[date_str][source_type] = spend

        source_types = sorted(list(source_types_set))
        timeseries = []
        for date_str in sorted(data_by_date.keys()):
            row = data_by_date[date_str]
            for st in source_types:
                if st not in row:
                    row[st] = 0
            timeseries.append(row)

        return {"available": True, "timeseries": timeseries, "source_types": source_types, "start_date": start_date, "end_date": end_date}

    @router.get("/warehouse-type-timeseries")
    async def get_warehouse_type_timeseries(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
    ) -> dict[str, Any]:
        start_date, end_date = _default_dates(start_date, end_date)

        try:
            # Get warehouse type metadata
            meta_results = await asyncio.to_thread(execute_query, """
                SELECT warehouse_id, MAX(warehouse_type) as warehouse_type
                FROM system.compute.warehouses
                GROUP BY warehouse_id
            """)
            wh_types = {r["warehouse_id"]: r.get("warehouse_type") or "CLASSIC" for r in (meta_results or [])}
        except Exception:
            wh_types = {}

        try:
            results = await asyncio.to_thread(execute_query, """
                SELECT
                  u.usage_date as date,
                  u.usage_metadata.warehouse_id as warehouse_id,
                  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) as daily_spend
                FROM system.billing.usage u
                LEFT JOIN system.billing.list_prices p
                  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
                WHERE u.billing_origin_product = 'SQL'
                  AND u.usage_date BETWEEN :start_date AND :end_date
                  AND u.usage_quantity > 0
                GROUP BY u.usage_date, u.usage_metadata.warehouse_id
            """, {"start_date": start_date, "end_date": end_date})
        except Exception:
            return {"available": False, "timeseries": [], "warehouse_types": []}

        # Aggregate by date + warehouse_type
        data_by_date: dict[str, dict[str, float]] = {}
        wh_type_set: set[str] = set()
        for row in (results or []):
            date_str = str(row.get("date"))
            wid = row.get("warehouse_id") or ""
            wh_type = wh_types.get(wid, "CLASSIC")
            spend = float(row.get("daily_spend") or 0)
            wh_type_set.add(wh_type)
            if date_str not in data_by_date:
                data_by_date[date_str] = {"date": date_str}
            data_by_date[date_str][wh_type] = data_by_date[date_str].get(wh_type, 0) + spend

        wh_types_list = sorted(list(wh_type_set))
        timeseries = []
        for date_str in sorted(data_by_date.keys()):
            row = data_by_date[date_str]
            for wt in wh_types_list:
                if wt not in row:
                    row[wt] = 0
            timeseries.append(row)

        return {"available": True, "timeseries": timeseries, "warehouse_types": wh_types_list}

    @router.get("/dashboard-bundle")
    async def get_dashboard_bundle(
        start_date: str = Query(default=None),
        end_date: str = Query(default=None),
        workspace_ids: str = Query(default=None),
    ) -> dict[str, Any]:
        """DBSQL bundle — submit-and-poll: cache hit returns 200, cache miss starts background compute and returns 202."""
        start_date, end_date = _default_dates(start_date, end_date)

        blocked = source_scope_unavailable(
            {"daily_dbsql_source"},
            "The selected shared source does not publish SQL detail.",
        )
        if blocked:
            return {
                **blocked,
                "message": blocked["reason_detail"],
                "start_date": start_date,
                "end_date": end_date,
            }

        status = await check_mv_status()
        if not status["mv_available"]:
            return {
                "available": False,
                "message": f"{table_name} MV not configured. See setup instructions.",
                "start_date": start_date,
                "end_date": end_date,
            }

        id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
        _dkey = bundle_cache_key(f"dbsql:{table_name}:dashboard-bundle", start_date, end_date, id_list)
        if (_dcached := delta_cache_get(_dkey)) is not None:
            if isinstance(_dcached, dict) and "_error" in _dcached:
                raise HTTPException(status_code=500, detail=_dcached.get("_error", "Bundle compute failed"))
            return _dcached

        with _dbsql_bundle_inflight_lock:
            if _dkey not in _dbsql_bundle_inflight:
                _dbsql_bundle_inflight.add(_dkey)
                threading.Thread(
                    target=_compute_dbsql_bundle,
                    args=(start_date, end_date, id_list, workspace_ids, _dkey),
                    daemon=True,
                    name=f"dbsql-bundle-bg-{table_name}",
                ).start()
                logger.info("dbsql dashboard-bundle: started background compute for %s", _dkey)
            else:
                logger.debug("dbsql dashboard-bundle: already inflight for %s", _dkey)

        return JSONResponse(
            status_code=202,
            content={"status": "pending", "cache_key": _dkey},
            headers={"Retry-After": "2"},
        )

    # Expose check_mv_status for prpr-specific endpoints
    router.check_mv_status = check_mv_status  # type: ignore[attr-defined]

    return router
