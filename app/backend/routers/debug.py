"""Debugger endpoint — parallel health checks for common deployment issues."""

import asyncio
import logging
import os
import time as _time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

router = APIRouter()
logger = logging.getLogger(__name__)

_CHECK_TIMEOUT = 25  # seconds per individual check

# Response-level cache — all 11+ checks run in parallel but still take ~5-15s
# on a cold warehouse. Cache the full result for 5 min so the System Readiness
# panel doesn't re-trigger a warehouse round-trip on every page load.
_debug_cache: dict | None = None
_debug_cache_ts: float = 0.0
_DEBUG_CACHE_TTL = 5 * 60  # 5 minutes


# ── Individual check functions ────────────────────────────────────────────────

def _check_billing_usage() -> dict:
    try:
        from backend.db import execute_query
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.billing.usage LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {
                "status": "warn",
                "detail": "system.billing.usage is accessible but has no rows",
                "fix": "Billing data may take 24–48h after workspace creation. Verify your account has active workloads.",
            }
        return {"status": "pass", "detail": f"{cnt:,} rows accessible"}
    except Exception as e:
        err = str(e)
        if "PERMISSION_DENIED" in err or "permission" in err.lower() or "AnalysisException" in err:
            fix = (
                "Grant access to the app identity:\n"
                "  GRANT USE CATALOG ON CATALOG system TO `<identity>`;\n"
                "  GRANT USE SCHEMA ON SCHEMA system.billing TO `<identity>`;\n"
                "  GRANT SELECT ON TABLE system.billing.usage TO `<identity>`"
            )
            root_cause = "Missing SELECT permission on system.billing.usage"
        else:
            fix = "Check that the SQL warehouse is running and the app identity has SELECT on system.billing.usage"
            root_cause = "SQL warehouse or query execution error"
        return {"status": "fail", "detail": err[:250], "root_cause": root_cause, "fix": fix}


def _check_list_prices() -> dict:
    try:
        from backend.db import execute_query
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.billing.list_prices LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        status = "pass" if cnt > 0 else "warn"
        detail = f"{cnt:,} rows accessible" if cnt > 0 else "Table accessible but empty — cost calculations may show $0"
        return {"status": status, "detail": detail, "fix": "Grant SELECT on system.billing.list_prices to the app identity" if cnt == 0 else ""}
    except Exception as e:
        return {
            "status": "fail",
            "detail": str(e)[:250],
            "root_cause": "Missing SELECT permission on system.billing.list_prices",
            "fix": "Grant SELECT on system.billing.list_prices to the app identity",
        }


def _check_query_history() -> dict:
    try:
        from backend.db import execute_query
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.query.history LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        status = "pass" if cnt > 0 else "warn"
        return {"status": status, "detail": f"{cnt:,} rows accessible"}
    except Exception as e:
        return {
            "status": "fail",
            "detail": str(e)[:250],
            "root_cause": "Missing SELECT permission on system.query.history",
            "fix": "Grant USE SCHEMA on system.query and SELECT on system.query.history to the app identity. SQL tab data depends on this.",
        }


def _check_workspaces_table() -> dict:
    try:
        from backend.db import execute_query
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.access.workspaces_latest LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        status = "pass" if cnt > 0 else "warn"
        detail = f"{cnt:,} rows accessible — workspace names resolve correctly" if cnt > 0 else "Table accessible but empty"
        return {"status": status, "detail": detail}
    except Exception as e:
        return {
            "status": "warn",
            "detail": f"system.access.workspaces_latest not accessible: {str(e)[:150]}",
            "root_cause": "system.access schema not enabled in this workspace",
            "fix": (
                "Enable the system.access schema via Databricks CLI:\n"
                "  databricks unity-catalog system-schemas enable --schema access\n"
                "Workspace names will display as IDs until enabled. Non-critical."
            ),
        }


def _check_mv_existence() -> dict:
    from backend.db import get_catalog_schema, get_workspace_client
    from backend.materialized_views import _MV_TABLES

    catalog, schema = get_catalog_schema()
    try:
        wc = get_workspace_client()
        existing = {t.name for t in wc.tables.list(catalog_name=catalog, schema_name=schema)}
    except Exception as e:
        return {
            "status": "fail",
            "detail": f"Could not list tables in {catalog}.{schema}: {e}",
            "root_cause": "Missing USE CATALOG or USE SCHEMA permissions",
            "fix": "Verify the app identity has USE CATALOG and USE SCHEMA on the app catalog/schema",
        }

    missing = [t for t in _MV_TABLES if t not in existing]
    if not missing:
        return {"status": "pass", "detail": f"All {len(_MV_TABLES)} materialized view tables present in {catalog}.{schema}"}
    return {
        "status": "fail",
        "detail": f"Missing: {', '.join(missing)}",
        "root_cause": "Materialized views have not been built yet",
        "fix": "Click 'Rebuild Materialized Views' below. This is the most common cause of zeros across the entire dashboard.",
        "missing_tables": missing,
    }


def _check_mv_populated() -> dict:
    try:
        from backend.db import execute_query, get_catalog_schema
        catalog, schema = get_catalog_schema()
        result = execute_query(
            f"SELECT COUNT(*) AS cnt, MAX(usage_date) AS latest "
            f"FROM `{catalog}`.`{schema}`.`daily_usage_summary` LIMIT 1",
            None, no_cache=True
        )
        row = (result or [{}])[0]
        cnt = int(row.get("cnt", 0))
        latest = row.get("latest")
        if cnt == 0:
            return {
                "status": "fail",
                "detail": "daily_usage_summary exists but has no rows",
                "root_cause": "MV tables exist but ETL did not populate them",
                "fix": "The MV tables were created but the ETL did not complete. Click 'Rebuild Materialized Views' below.",
            }
        return {"status": "pass", "detail": f"{cnt:,} rows, latest usage_date: {latest}"}
    except Exception as e:
        return {
            "status": "warn",
            "detail": f"Could not query daily_usage_summary: {str(e)[:200]}",
            "root_cause": "Could not read daily_usage_summary MV",
            "fix": "Rebuild materialized views if they were recently dropped or are missing",
        }


def _check_recent_billing_data() -> dict:
    try:
        from backend.db import execute_query
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.billing.usage "
            "WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {
                "status": "warn",
                "detail": "No billing rows in the last 30 days — dashboard will show $0 for default date range",
                "fix": (
                    "Billing data has a 24–48h lag. If the workspace is new, wait 48h.\n"
                    "Also verify active workloads exist. Try extending the date range back further."
                ),
            }
        return {"status": "pass", "detail": f"{cnt:,} billing rows in the last 30 days"}
    except Exception as e:
        return {"status": "warn", "detail": f"Could not check recent data: {str(e)[:200]}"}


def _check_warehouse_configured() -> dict:
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    if not http_path or http_path == "auto":
        warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
        if warehouse_id:
            # Resource binding is present — path just hasn't resolved yet
            return {
                "status": "warn",
                "detail": f"DATABRICKS_WAREHOUSE_ID is set ({warehouse_id}) but DATABRICKS_HTTP_PATH hasn't resolved — first query will trigger resolution",
                "root_cause": "Warehouse ID found but HTTP path not yet resolved",
                "fix": "Restart the app to force warehouse resolution, or set DATABRICKS_HTTP_PATH explicitly in app.yaml.",
            }
        return {
            "status": "fail",
            "detail": "No SQL warehouse configured — all queries will fail with 'Missing DATABRICKS_HTTP_PATH'",
            "root_cause": "No SQL warehouse resource attached to the app",
            "fix": (
                "Add a SQL warehouse resource in the Databricks Apps UI:\n"
                "  1. App settings → Resources → + Add resource → SQL warehouse\n"
                "  2. Select your warehouse, set permission to 'Can use'\n"
                "  3. Leave resource key as 'sql-warehouse' (the app.yaml expects this key)\n"
                "  4. Save and redeploy the app\n\n"
                "Alternatively, set DATABRICKS_HTTP_PATH explicitly in app.yaml:\n"
                "  - name: DATABRICKS_HTTP_PATH\n"
                "    value: /sql/1.0/warehouses/<your-warehouse-id>"
            ),
        }
    wh_id = http_path.split("/")[-1] if "/" in http_path else http_path
    try:
        from backend.db import get_workspace_client
        wc = get_workspace_client()
        wh = wc.warehouses.get(wh_id)
        # SDK returns a State enum — str() gives "State.RUNNING"; check with "in"
        state_str = str(wh.state or "").upper()
        if "RUNNING" in state_str:
            return {"status": "pass", "detail": f"Warehouse '{wh.name}' ({wh.cluster_size}) is running"}
        if "STARTING" in state_str or "RESIZING" in state_str:
            return {"status": "warn", "detail": f"Warehouse '{wh.name}' ({wh.cluster_size}) is starting up — queries will queue briefly"}
        return {
            "status": "warn",
            "detail": f"Warehouse '{wh.name}' ({wh.cluster_size}) is stopped — first query will auto-start it (may take ~30s)",
        }
    except Exception as e:
        return {"status": "warn", "detail": f"Warehouse {wh_id} configured but could not verify state: {e}"}


def _check_sp_identity() -> dict:
    client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
    if not client_id:
        return {
            "status": "warn",
            "detail": "DATABRICKS_CLIENT_ID not set — app runs as the deploying user identity, not a dedicated service principal",
            "root_cause": "No service principal configured",
            "fix": "Set DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET in App config for a dedicated SP. Required for scheduled MV refreshes.",
        }
    client_secret = os.getenv("DATABRICKS_CLIENT_SECRET", "")
    if not client_secret:
        return {
            "status": "warn",
            "detail": f"SP client_id is set ({client_id}) but DATABRICKS_CLIENT_SECRET is missing",
            "root_cause": "Service principal secret not configured",
            "fix": "Set DATABRICKS_CLIENT_SECRET in the App configuration",
        }
    return {"status": "pass", "detail": f"Service principal configured: {client_id}"}


def _check_warehouse_list_access() -> dict:
    """Check that at least one warehouse is visible to list_warehouses — the root cause of 'No warehouses found'."""
    from backend.db import get_user_workspace_client, get_workspace_client

    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    configured_id = http_path.split("/")[-1] if http_path and "/" in http_path else None

    user_count = sp_count = 0
    user_error = sp_error = None

    # Test user OAuth token listing
    try:
        user_client = get_user_workspace_client()
        user_whs = list(user_client.warehouses.list())
        user_count = len(user_whs)
    except Exception as e:
        user_error = str(e)[:200]

    # Test SP M2M token listing
    try:
        sp_client = get_workspace_client()
        sp_whs = list(sp_client.warehouses.list())
        sp_count = len(sp_whs)
    except Exception as e:
        sp_error = str(e)[:200]

    def _is_scope_err(msg: str) -> bool:
        m = msg.lower()
        return "required scopes" in m or "does not have required scopes" in m or "insufficient_scope" in m

    # Both paths have warehouses — fully healthy
    if user_count > 0 and sp_count > 0:
        return {"status": "pass", "detail": f"Warehouses visible: {user_count} via user OAuth, {sp_count} via SP"}

    # User token errored (likely OAuth scope issue) but SP has warehouses — SP fallback active, will work
    if user_error and sp_count > 0:
        root_cause = (
            "User OAuth token raised an exception calling warehouses.list() — the forwarded "
            "token may only have 'sql' scope and not 'all-apis' scope required for the warehouse "
            "management REST API. The SP fallback is active so the setup wizard will still show "
            f"warehouses, but this indicates a token scope limitation. Error: {user_error}"
        )
        return {
            "status": "warn",
            "detail": f"User token errored on warehouses.list() but SP found {sp_count} warehouse(s) — SP fallback active",
            "root_cause": root_cause,
            "fix": (
                "This is non-blocking — warehouse listing falls back to the SP automatically. "
                "If queries should run as the user (User OAuth mode), ensure the app requests "
                "'all-apis' OAuth scope or grant the user's token broader access. "
                "Alternatively, switch to SP auth mode in Settings → Permissions."
            ),
        }

    # User token returned empty but SP has warehouses — SP fallback active, will work
    if user_count == 0 and not user_error and sp_count > 0:
        return {
            "status": "warn",
            "detail": f"User OAuth token sees 0 warehouses but SP found {sp_count} — SP fallback active",
            "root_cause": (
                "The user's OAuth token has no CAN_USE grants on any warehouse (common when "
                "permissions were only granted to the app's service principal, not the end-user). "
                "The setup wizard will show the SP's warehouses via the fallback path."
            ),
            "fix": (
                "This is non-blocking — warehouse listing falls back to the SP automatically. "
                "If you want the user token to list warehouses directly (User OAuth mode), "
                "grant CAN_USE on the warehouse to the end-user or their group."
            ),
        }

    # User has warehouses but SP errored or is empty — healthy for user OAuth mode but risky for SP mode
    if user_count > 0 and sp_count == 0:
        detail = f"User OAuth token found {user_count} warehouse(s) but SP sees 0"
        if sp_error:
            detail += f" (SP error: {sp_error})"
        return {
            "status": "warn",
            "detail": detail,
            "root_cause": "The app service principal has no CAN_USE grant on any warehouse. Queries running as the SP (e.g. background jobs, scheduled refresh) will fail.",
            "fix": (
                "Grant CAN_USE on the warehouse to the app SP: "
                "SQL Warehouses → [warehouse name] → Permissions tab → add SP with 'Can use'. "
                "Warehouse permissions cannot be granted via SQL — use the UI or REST API."
            ),
        }

    # Both failed — the 'No warehouses found' bug in full
    detail_parts = ["warehouses.list() returned 0 for both user OAuth and SP tokens"]
    if user_error:
        detail_parts.append(f"user token error: {user_error}")
    if sp_error:
        detail_parts.append(f"SP token error: {sp_error}")

    if user_error and _is_scope_err(user_error):
        root_cause = (
            "The forwarded user OAuth token lacks the 'all-apis' scope required to call the "
            "warehouse management REST API, AND the SP also has no warehouse access. "
            "Fix the SP CAN_USE grant first; the OAuth scope issue is secondary."
        )
    else:
        root_cause = "No warehouse visible to app identity or user token"

    fix_lines = [
        "Neither the user OAuth token nor the service principal has CAN_USE on any warehouse.",
        "",
        "Fix — grant CAN_USE on the app warehouse to the service principal:",
        "  1. In Databricks: SQL Warehouses → select the warehouse → Permissions",
        "  2. Add the SP (client ID or display name) with 'Can use' permission",
        "  3. Alternatively, grant via REST API:",
        "     PUT /api/2.0/permissions/warehouses/<warehouse-id>",
        "     Body: {\"access_control_list\": [{\"service_principal_name\": \"<SP-client-id>\", \"permission_level\": \"CAN_USE\"}]}",
        "",
        "If using User OAuth mode, the end-user also needs CAN_USE on the warehouse.",
    ]
    if configured_id:
        fix_lines.insert(0, f"Configured warehouse ID: {configured_id}")

    return {
        "status": "fail",
        "detail": "; ".join(detail_parts),
        "root_cause": root_cause,
        "fix": "\n".join(fix_lines),
    }


def _check_workspace_pool() -> dict:
    import json
    settings_dir = os.path.join(os.path.dirname(__file__), "..", "..", ".settings")
    settings_path = os.path.join(settings_dir, "workspace_filter.json")
    try:
        with open(settings_path) as f:
            data = json.load(f)
        ws_ids = data.get("workspace_ids", [])
        if ws_ids:
            return {"status": "pass", "detail": f"Workspace pool: {len(ws_ids)} workspace(s) configured"}
        return {"status": "pass", "detail": "No workspace pool configured — all workspaces visible in the filter dropdown"}
    except FileNotFoundError:
        return {"status": "pass", "detail": "No workspace pool configured — all workspaces visible in the filter dropdown"}
    except Exception as e:
        return {"status": "warn", "detail": f"Could not read workspace_filter.json: {e}"}


def _check_mv_consistency() -> dict:
    """Cross-check spend totals across core MV tables to catch partial build failures.

    This is the root cause of the 'some tabs show $0, others show real data' pattern:
    some tables built successfully, others timed out or errored during the initial build.
    """
    from backend.db import execute_query, get_catalog_schema
    catalog, schema = get_catalog_schema()

    _TABLES = {
        "daily_usage_summary":    f"SELECT COUNT(*) AS cnt, COALESCE(SUM(total_spend),0) AS spend FROM `{catalog}`.`{schema}`.`daily_usage_summary` WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30)",
        "daily_product_breakdown":f"SELECT COUNT(*) AS cnt, COALESCE(SUM(total_spend),0) AS spend FROM `{catalog}`.`{schema}`.`daily_product_breakdown` WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30)",
        "daily_workspace_breakdown":f"SELECT COUNT(*) AS cnt, COALESCE(SUM(total_spend),0) AS spend FROM `{catalog}`.`{schema}`.`daily_workspace_breakdown` WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30)",
    }

    stats: dict = {}
    for tbl, sql in _TABLES.items():
        try:
            row = (execute_query(sql, None, no_cache=True) or [{}])[0]
            stats[tbl] = {"cnt": int(row.get("cnt", 0)), "spend": float(row.get("spend") or 0)}
        except Exception as exc:
            stats[tbl] = {"cnt": -1, "spend": -1, "error": str(exc)[:120]}

    valid = {t: s for t, s in stats.items() if s["cnt"] >= 0}
    max_spend = max((s["spend"] for s in valid.values()), default=0)

    # Tables that exist but are completely empty
    empty = [t for t, s in valid.items() if s["cnt"] == 0]
    # Tables with rows but $0 spend (list_prices was missing when MVs were built)
    zero_spend = [t for t, s in valid.items() if s["cnt"] > 0 and s["spend"] == 0]
    # Errors (table doesn't exist or query failed)
    errored = [t for t, s in stats.items() if s.get("cnt", -1) == -1]

    if not empty and not zero_spend and not errored:
        summary = "; ".join(f"{t.split('_',1)[1]}: {s['cnt']:,}r/${s['spend']:,.0f}" for t, s in valid.items())
        return {
            "status": "pass",
            "detail": f"All core MV tables consistent — {summary}",
            "table_summary": {t: {"rows": s.get("cnt", -1), "spend": round(s.get("spend", 0), 2)} for t, s in stats.items()},
        }

    issues = []
    if empty and max_spend > 0:
        issues.append(f"Partial build failure — empty tables while others have data: {', '.join(empty)}")
    elif empty:
        issues.append(f"Empty tables (no data in last 30 days): {', '.join(empty)}")
    if zero_spend:
        issues.append(f"Rows present but $0 spend (list_prices missing at build time): {', '.join(zero_spend)}")
    if errored:
        issues.append(f"Tables missing or inaccessible: {', '.join(errored)}")

    fix_parts = [
        "This causes the 'some tabs show $0, others show real data' pattern.",
        "Root cause: the initial MV build partially failed — some tables timed out or",
        "errored mid-build while others completed.",
        "",
        "Fix: click 'Rebuild Materialized Views' in this Debugger or the Configuration tab.",
        "The rebuild recreates all tables from scratch and is safe to re-run.",
    ]
    if zero_spend:
        fix_parts += [
            "",
            "If $0 spend persists after rebuild: system.billing.list_prices may have been",
            "empty when the MVs first built. Wait for list_prices to populate then rebuild again.",
        ]

    status = "fail" if (empty and max_spend > 0) or errored else "warn"
    if errored:
        root_cause = "One or more MV tables are missing or inaccessible"
    elif empty and max_spend > 0:
        root_cause = "MV build partially failed — some tables built, others errored"
    elif zero_spend:
        root_cause = "list_prices table was empty when MVs were built"
    else:
        root_cause = "MV tables have incomplete or inconsistent data"
    return {
        "status": status,
        "detail": "; ".join(issues),
        "root_cause": root_cause,
        "fix": "\n".join(fix_parts),
        "table_summary": {t: {"rows": s.get("cnt", -1), "spend": round(s.get("spend", 0), 2)} for t, s in stats.items()},
    }


# ── Per-tab visualization health checks ──────────────────────────────────────

def _tab_dbu() -> dict:
    from backend.db import execute_query, get_catalog_schema
    catalog, schema = get_catalog_schema()
    try:
        result = execute_query(
            f"SELECT COUNT(*) AS cnt, COALESCE(SUM(total_spend), 0) AS spend "
            f"FROM `{catalog}`.`{schema}`.`daily_usage_summary` "
            f"WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) LIMIT 1",
            None, no_cache=True
        )
        row = (result or [{}])[0]
        cnt, spend = int(row.get("cnt", 0)), float(row.get("spend") or 0)
        if cnt == 0:
            return {"status": "fail", "detail": "No rows in last 30 days — all charts will show $0", "root_cause": "daily_usage_summary MV is empty or not built", "fix": "Rebuild materialized views. Confirm system.billing.usage has recent data."}
        if spend == 0:
            return {"status": "warn", "detail": f"{cnt:,} rows but $0 spend — list_prices may be missing", "root_cause": "Rows present but no cost data (list_prices empty at build time)", "fix": "Verify system.billing.list_prices has data (see Permissions section)"}
        return {"status": "pass", "detail": f"${spend:,.0f} spend across {cnt:,} day-workspace rows in last 30 days"}
    except Exception as e:
        err = str(e)
        is_missing = "TABLE_OR_VIEW_NOT_FOUND" in err or "table or view" in err.lower()
        return {"status": "warn", "detail": f"Query failed: {err[:200]}", "root_cause": "MV table not found — tables were dropped or never built" if is_missing else "MV table not accessible or missing", "fix": "Rebuild materialized views from the Configuration tab.", "failure_class": "missing_mv" if is_missing else None}


def _tab_kpis() -> dict:
    from backend.db import execute_query, get_catalog_schema
    catalog, schema = get_catalog_schema()
    try:
        result = execute_query(
            f"SELECT COUNT(*) AS cnt FROM `{catalog}`.`{schema}`.`daily_query_stats` "
            f"WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {"status": "warn", "detail": "No query stats rows in last 30 days — KPI metrics relying on query history will show 0", "root_cause": "daily_query_stats MV is empty or has no recent data", "fix": "Verify system.query.history access and rebuild MVs"}
        return {"status": "pass", "detail": f"{cnt:,} query-stat rows in last 30 days"}
    except Exception as e:
        err = str(e)
        is_missing = "TABLE_OR_VIEW_NOT_FOUND" in err or "table or view" in err.lower()
        return {"status": "warn", "detail": f"Query failed: {err[:200]}", "root_cause": "MV table not found — tables were dropped or never built" if is_missing else "daily_query_stats MV not accessible or missing", "fix": "Rebuild materialized views from the Configuration tab.", "failure_class": "missing_mv" if is_missing else None}


def _tab_sql() -> dict:
    from backend.db import execute_query, get_catalog_schema
    catalog, schema = get_catalog_schema()
    try:
        r1 = execute_query(
            f"SELECT COUNT(*) AS cnt FROM `{catalog}`.`{schema}`.`sql_tool_attribution` "
            f"WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) LIMIT 1",
            None, no_cache=True
        )
        r2 = execute_query(
            f"SELECT COUNT(*) AS cnt FROM `{catalog}`.`{schema}`.`dbsql_cost_per_query` "
            f"WHERE query_date >= DATE_SUB(CURRENT_DATE(), 30) LIMIT 1",
            None, no_cache=True
        )
        cnt1 = int((r1 or [{}])[0].get("cnt", 0))
        cnt2 = int((r2 or [{}])[0].get("cnt", 0))
        if cnt1 == 0 and cnt2 == 0:
            return {"status": "warn", "detail": "No SQL attribution or per-query cost data in last 30 days — SQL tab charts will be empty", "root_cause": "No DBSQL warehouse usage or MV tables are empty", "fix": "Verify system.query.history access. SQL tab requires active DBSQL warehouse usage."}
        parts = []
        if cnt1 > 0:
            parts.append(f"{cnt1:,} tool-attribution rows")
        if cnt2 > 0:
            parts.append(f"{cnt2:,} per-query cost rows")
        return {"status": "pass", "detail": ", ".join(parts) + " in last 30 days"}
    except Exception as e:
        err = str(e)
        is_missing = "TABLE_OR_VIEW_NOT_FOUND" in err or "table or view" in err.lower()
        return {"status": "warn", "detail": f"Query failed: {err[:200]}", "root_cause": "MV table not found — tables were dropped or never built" if is_missing else "SQL MV tables not accessible or missing", "fix": "Rebuild materialized views from the Configuration tab.", "failure_class": "missing_mv" if is_missing else None}


def _tab_aiml() -> dict:
    from backend.db import execute_query
    try:
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.billing.usage "
            "WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) "
            "AND usage_quantity > 0 "
            "AND (billing_origin_product = 'MODEL_SERVING' "
            "     OR billing_origin_product = 'VECTOR_SEARCH' "
            "     OR sku_name LIKE '%ANTHROPIC%' "
            "     OR sku_name LIKE '%OPENAI%' "
            "     OR sku_name LIKE '%GEMINI%' "
            "     OR sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%' "
            "     OR sku_name LIKE '%INFERENCE%' "
            "     OR sku_name LIKE '%FINE_TUNING%') LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {"status": "warn", "detail": "No AI/ML usage rows in last 30 days — AIML tab will show $0", "root_cause": "No Model Serving, Foundation Model APIs, or AI Search usage detected in last 30 days", "fix": "AIML tab only shows data if your account uses Model Serving, Foundation Model APIs, or AI Search. This may be expected if those features aren't in use."}
        return {"status": "pass", "detail": f"{cnt:,} AI/ML billing rows in last 30 days"}
    except Exception as e:
        return {"status": "warn", "detail": f"Query failed: {str(e)[:200]}", "root_cause": "Missing permission on system.billing.usage", "fix": "Verify the app identity has USE SCHEMA on system.billing and SELECT on system.billing.usage. If the catalog is inaccessible, grant USE CATALOG on the system catalog. See the Permissions section for detailed grant SQL."}


def _tab_apps() -> dict:
    from backend.db import execute_query
    try:
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.billing.usage "
            "WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) "
            "AND billing_origin_product = 'APPS' LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {"status": "warn", "detail": "No Databricks Apps usage rows in last 30 days — Apps tab will show $0", "root_cause": "No Databricks Apps deployed or no Apps usage in last 30 days", "fix": "Apps tab only shows data if your account runs Databricks Apps. Expected to be empty if Apps aren't deployed."}
        return {"status": "pass", "detail": f"{cnt:,} Apps billing rows in last 30 days"}
    except Exception as e:
        return {"status": "warn", "detail": f"Query failed: {str(e)[:200]}", "root_cause": "Missing permission on system.billing.usage", "fix": "Verify the app identity has USE SCHEMA on system.billing and SELECT on system.billing.usage. If the catalog is inaccessible, grant USE CATALOG on the system catalog. See the Permissions section for detailed grant SQL."}


def _tab_tagging() -> dict:
    from backend.db import execute_query
    try:
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.compute.clusters "
            "LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {"status": "warn", "detail": "No cluster activity in last 30 days — Tagging tab untagged resource lists will be empty", "root_cause": "No cluster activity or system.compute.clusters has no recent data", "fix": "Tagging tab requires system.compute.clusters access and active clusters. Verify the app identity has SELECT on system.compute.clusters."}
        return {"status": "pass", "detail": f"{cnt:,} cluster events in last 30 days — untagged resource analysis available"}
    except Exception as e:
        return {"status": "warn", "detail": f"Query failed (system.compute.clusters may not be accessible): {str(e)[:200]}", "root_cause": "Missing SELECT permission on system.compute.clusters", "fix": "Grant SELECT on system.compute.clusters to the app identity"}


def _tab_infra() -> dict:
    from backend.db import execute_query
    try:
        # Check for any cloud cost data (AWS/Azure/GCP actual cost tables)
        result = execute_query(
            "SELECT COUNT(*) AS cnt FROM system.billing.usage "
            "WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 30) "
            "AND sku_name NOT LIKE '%All Purpose%' AND sku_name NOT LIKE '%Jobs Compute%' "
            "AND sku_name NOT LIKE '%SQL%' AND sku_name NOT LIKE '%Delta Live%' LIMIT 1",
            None, no_cache=True
        )
        cnt = int((result or [{}])[0].get("cnt", 0))
        if cnt == 0:
            return {"status": "warn", "detail": "No infrastructure/cloud cost data found in last 30 days", "root_cause": "Cloud cost exports not configured or no infrastructure usage", "fix": "Infra tab shows cloud provider actual costs (AWS CUR, Azure, GCP billing exports). Configure cloud cost exports in the Configuration tab."}
        return {"status": "pass", "detail": f"{cnt:,} infrastructure billing rows in last 30 days"}
    except Exception as e:
        return {"status": "warn", "detail": f"Query failed: {str(e)[:200]}", "root_cause": "Missing permission on system.billing.usage", "fix": "Verify the app identity has USE SCHEMA on system.billing and SELECT on system.billing.usage. If the catalog is inaccessible, grant USE CATALOG on the system catalog. See the Permissions section for detailed grant SQL."}


# Ordered check registry
_CHECKS = [
    {"id": "sp_identity",          "category": "configuration",      "label": "Service principal identity",                      "fn": _check_sp_identity},
    {"id": "warehouse",            "category": "configuration",      "label": "SQL warehouse configured",                        "fn": _check_warehouse_configured},
    {"id": "warehouse_list_access","category": "configuration",      "label": "Warehouse list access (Switch Warehouse UI)",      "fn": _check_warehouse_list_access},
    {"id": "workspace_pool",       "category": "configuration",      "label": "Workspace filter pool",                           "fn": _check_workspace_pool},
    {"id": "billing_usage",        "category": "permissions",        "label": "system.billing.usage access",                     "fn": _check_billing_usage},
    {"id": "list_prices",          "category": "permissions",        "label": "system.billing.list_prices access",               "fn": _check_list_prices},
    {"id": "query_history",        "category": "permissions",        "label": "system.query.history access",                     "fn": _check_query_history},
    {"id": "workspaces_table",     "category": "permissions",        "label": "system.access.workspaces_latest access",          "fn": _check_workspaces_table},
    {"id": "mv_existence",         "category": "materialized_views", "label": "Materialized view table presence",                "fn": _check_mv_existence},
    {"id": "mv_populated",         "category": "materialized_views", "label": "Materialized view data availability",             "fn": _check_mv_populated},
    {"id": "mv_consistency",       "category": "materialized_views", "label": "MV table spend consistency (partial-zeros check)", "fn": _check_mv_consistency},
    {"id": "recent_billing_data",  "category": "data",               "label": "Recent billing data (last 30 days)",              "fn": _check_recent_billing_data},
    {"id": "tab_dbu",            "category": "tab_health",         "label": "DBU & Billing tab",                       "fn": _tab_dbu},
    {"id": "tab_kpis",           "category": "tab_health",         "label": "Platform KPIs tab",                       "fn": _tab_kpis},
    {"id": "tab_sql",            "category": "tab_health",         "label": "SQL Warehousing tab",                     "fn": _tab_sql},
    {"id": "tab_aiml",           "category": "tab_health",         "label": "AI/ML tab",                               "fn": _tab_aiml},
    {"id": "tab_apps",           "category": "tab_health",         "label": "Apps tab",                                "fn": _tab_apps},
    {"id": "tab_tagging",        "category": "tab_health",         "label": "Tagging tab",                             "fn": _tab_tagging},
    {"id": "tab_infra",          "category": "tab_health",         "label": "Infrastructure (Cloud Costs) tab",        "fn": _tab_infra},
]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/run")
async def run_diagnostics(no_cache: bool = False) -> dict:
    """Run all diagnostic checks in parallel and return structured results.

    Results are cached for 5 minutes. Pass ?no_cache=true to force a fresh run
    (useful after applying a fix when you want to verify it immediately).
    """
    global _debug_cache, _debug_cache_ts
    if not no_cache and _debug_cache is not None and (_time.time() - _debug_cache_ts) < _DEBUG_CACHE_TTL:
        return _debug_cache

    import asyncio

    loop = asyncio.get_running_loop()

    async def _run_one(check: dict) -> dict:
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, check["fn"]),
                timeout=_CHECK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result = {"status": "warn", "detail": f"Check timed out after {_CHECK_TIMEOUT}s"}
        except Exception as exc:
            result = {"status": "fail", "detail": f"Unexpected error: {exc}"}

        return {
            "id": check["id"],
            "category": check["category"],
            "label": check["label"],
            "status": result.get("status", "fail"),
            "detail": result.get("detail", ""),
            "fix": result.get("fix", ""),
            **{k: v for k, v in result.items() if k not in ("status", "detail", "fix")},
        }

    results = list(await asyncio.gather(*[_run_one(c) for c in _CHECKS]))

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    warned = sum(1 for r in results if r["status"] == "warn")

    out = {
        "checks": results,
        "summary": {"passed": passed, "failed": failed, "warned": warned, "total": len(results)},
    }
    _debug_cache = out
    _debug_cache_ts = _time.time()
    return out


@router.post("/rebuild-mvs")
async def rebuild_mvs(background_tasks: BackgroundTasks, request: Request) -> dict:
    """Trigger a full MV rebuild in the background. Admin only."""
    from backend.routers.user import _get_user_role
    user_email = request.headers.get("X-Forwarded-Email", os.getenv("USER", ""))
    if await _get_user_role(user_email) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required to rebuild materialized views")

    from backend.db import _user_token as _db_user_token
    from backend.db import get_catalog_schema
    from backend.materialized_views import refresh_materialized_views

    catalog, schema = get_catalog_schema()

    def _do_rebuild():
        import contextlib
        # Always run as SP — same reasoning as _run_mv_refresh: the forwarded
        # OAuth token (sql scope) may not have CAN_USE on the warehouse or
        # CREATE TABLE on the schema, but the SP does.
        ctx_tok = _db_user_token.set("")
        try:
            logger.info("Debugger-triggered MV rebuild starting for %s.%s", catalog, schema)
            results = refresh_materialized_views(catalog, schema)
            failed = [k for k, v in results.items() if isinstance(v, str) and v.startswith("error:")]
            if failed:
                logger.error("Debugger MV rebuild: %d tables failed: %s", len(failed), failed)
            else:
                logger.info("Debugger MV rebuild complete")
            # Invalidate caches
            with contextlib.suppress(Exception):
                from backend.routers.billing import _mv_cache
                _mv_cache["available"] = None
            with contextlib.suppress(Exception):
                from backend.db import clear_query_cache, delta_cache_invalidate
                clear_query_cache()
                delta_cache_invalidate()
        except Exception as exc:
            logger.error("Debugger MV rebuild failed: %s", exc)
        finally:
            _db_user_token.reset(ctx_tok)

    # Invalidate debug cache so the next /run reflects the fresh MV state
    global _debug_cache, _debug_cache_ts
    _debug_cache = None
    _debug_cache_ts = 0.0

    background_tasks.add_task(_do_rebuild)
    return {"status": "started", "message": "MV rebuild started in background — re-run diagnostics in a few minutes to verify"}


@router.post("/parity-check")
async def parity_check(request: Request, start_date: str = "", end_date: str = "") -> dict:
    """Run the Phase B dual-read parity check (Delta vs Lakebase) on demand.

    Admin only. Explicit operator trigger, so it runs regardless of the ambient
    COST_OBS_PARITY_CHECK flag. Defaults to the last 30 days. Shadow-only — this
    changes nothing about what the app serves; it records a verdict in parity_log
    for the §13 burn-in that gates reader cutover.
    """
    from datetime import date, timedelta

    from backend.routers.user import _get_user_role
    user_email = request.headers.get("X-Forwarded-Email", os.getenv("USER", ""))
    if await _get_user_role(user_email) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required to run parity checks")

    end_date = end_date or date.today().isoformat()
    start_date = start_date or (date.today() - timedelta(days=30)).isoformat()

    from backend.lakebase_parity import check_daily_usage_totals
    report = await asyncio.to_thread(check_daily_usage_totals, start_date, end_date, force=True)
    return {"start_date": start_date, "end_date": end_date, "report": report}


@router.get("/parity-status")
async def parity_status() -> dict:
    """Return recent parity_log verdicts (most recent first). Empty when Lakebase
    is unavailable or no checks have run."""
    from backend.lakebase_parity import get_parity_status
    rows = await asyncio.to_thread(get_parity_status)
    return {"checks": rows}


@router.get("/lakebase-info")
async def lakebase_info() -> dict:
    """Diagnose the Lakebase connection: role, database, search_path, schemas +
    ownership, and whether the app SP can create in its target schema."""
    def _probe() -> dict:
        from backend.db import get_pg_conn
        out: dict = {}
        with get_pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), current_setting('search_path')")
            r = cur.fetchone()
            out["current_user"], out["current_database"], out["search_path"] = r[0], r[1], r[2]
            cur.execute(
                "SELECT nspname, pg_get_userbyid(nspowner) AS owner, "
                "has_schema_privilege(current_user, nspname, 'CREATE') AS can_create, "
                "has_schema_privilege(current_user, nspname, 'USAGE') AS can_use "
                "FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' "
                "ORDER BY nspname"
            )
            out["schemas"] = [
                {"schema": s, "owner": o, "can_create": c, "can_use": u}
                for s, o, c, u in cur.fetchall()
            ]
            cur.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS db_create")
            out["can_create_in_database"] = cur.fetchone()[0]
        return out
    try:
        return await asyncio.to_thread(_probe)
    except Exception as e:
        return {"error": str(e)}


@router.post("/sync-lakebase")
async def sync_lakebase(request: Request, background_tasks: BackgroundTasks, full: bool = False) -> dict:
    """Run the Phase B app-managed sync of the daily_* aggregates into Lakebase.

    Admin only. Runs in the BACKGROUND and returns immediately — a full backfill of
    millions of rows exceeds the Databricks Apps 300s HTTP limit, so we never block the
    request on it. Poll GET /api/settings/tables (lakebase_sync_status) for progress.
    The Delta aggregates are read as-is (no rebuild) and range-replaced into Lakebase.
    """
    from backend.routers.user import _get_user_role
    user_email = request.headers.get("X-Forwarded-Email", os.getenv("USER", ""))
    if await _get_user_role(user_email) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required to sync Lakebase")

    def _run_sync() -> None:
        from backend.lakebase_sync import sync_all_aggregates
        try:
            res = sync_all_aggregates(None, None, full=full)
            logger.info("debug sync-lakebase done: ok=%s/%s (full=%s)", res.get("ok"), res.get("total"), full)
        except Exception as exc:
            logger.error("debug sync-lakebase failed: %s", exc)

    background_tasks.add_task(_run_sync)
    return {"status": "started", "full": full,
            "message": "Lakebase sync started in background — poll GET /api/settings/tables (lakebase_sync_status)"}
