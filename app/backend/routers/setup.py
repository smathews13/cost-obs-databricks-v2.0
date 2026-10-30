"""Setup API endpoints for initializing materialized views."""

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from backend.materialized_views import (
    _MV_TABLES,
    _build_errors,
    check_materialized_views_exist,
    create_materialized_views,
    drop_materialized_views,
    get_catalog_schema,
    refresh_materialized_views,
)
from backend.db import get_workspace_client, _user_token as _db_user_token

router = APIRouter()
logger = logging.getLogger(__name__)

SETTINGS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".settings")
# Written when the setup wizard completes. This file lives in .settings/ which is
# wiped on every Databricks Apps git redeploy — ensuring the wizard always runs on
# a fresh deployment, even if tables already exist from a previous run.
SETUP_DONE_FILE = os.path.join(SETTINGS_DIR, "setup_done.json")

# Simple in-process state for the background create-tables task
_create_task_state: dict = {"status": "idle", "error": None, "started_at": None, "elapsed_seconds": None, "table_progress": {}, "table_errors": {}}  # idle | running | interrupted | done | error

_TASK_STATE_FILE = os.path.join(SETTINGS_DIR, "build_progress.json")


def _persist_task_state() -> None:
    """Best-effort write of current task state to disk and DBFS for pod-restart recovery."""
    payload = {k: v for k, v in _create_task_state.items() if k != "started_at"}
    payload["saved_at"] = __import__("datetime").datetime.utcnow().isoformat()
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        # Atomic write via temp file + rename so concurrent readers never see partial JSON.
        _tmp = _TASK_STATE_FILE + ".tmp"
        with open(_tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(_tmp, _TASK_STATE_FILE)
    except Exception as exc:
        logger.debug("Could not persist task state to file: %s", exc)
    try:
        from backend.db import write_build_state as write_dbfs_build_state
        write_dbfs_build_state(payload)
    except Exception as exc:
        logger.warning("Could not persist task state to DBFS: %s", exc)


def _restore_task_state() -> None:
    """On startup restore last-known task state. 'running' → 'interrupted'; 'done'/'idle' → 'idle'.

    Priority: local .settings/build_progress.json (fast) → DBFS fallback (survives pod restart).
    """
    saved: dict | None = None
    try:
        with open(_TASK_STATE_FILE) as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        try:
            from backend.db import read_build_state as read_dbfs_build_state
            saved = read_dbfs_build_state()
            if saved:
                logger.info("Restored build task state from DBFS (pod restart recovery)")
        except Exception as exc:
            logger.debug("Could not read DBFS build state: %s", exc)
    except Exception as exc:
        logger.debug("Could not restore task state from file: %s", exc)

    if not saved:
        return

    status = saved.get("status", "idle")
    if status == "running":
        _create_task_state.update({
            "status": "interrupted",
            "error": None,
            "started_at": None,
            "elapsed_seconds": None,
            "table_progress": saved.get("table_progress", {}),
            "table_errors": saved.get("table_errors") or {},
            "table_results": saved.get("table_results") or {},
        })
        logger.info("Restored interrupted task state from previous pod session")
    elif status == "error":
        _create_task_state.update({
            "status": "error",
            "error": saved.get("error"),
            "started_at": None,
            "elapsed_seconds": None,
            "table_progress": saved.get("table_progress", {}),
            "table_errors": saved.get("table_errors") or {},
            "table_results": saved.get("table_results") or {},
        })


_restore_task_state()


def _load_shared_task_state() -> dict | None:
    """Read the build progress the *builder worker* last wrote to the pod filesystem.

    Returns None when the file is absent or unreadable. Kept separate from
    _restore_task_state (which runs once at startup and downgrades 'running' →
    'interrupted'); this reads the live file on every /status poll.
    """
    try:
        with open(_TASK_STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return None


# Ordering of task lifecycle states, most-advanced last. Terminal states
# (error, done) outrank the in-flight ones so a finished build is never masked
# by a worker that still thinks it is idle/running.
_TASK_STATE_RANK = {"idle": 0, "interrupted": 1, "running": 2, "error": 3, "done": 4}


def _reconcile_task_state_from_disk() -> None:
    """Fold the shared on-disk build progress into this worker's in-memory copy.

    Table creation runs in exactly ONE uvicorn worker, but /status is polled
    round-robin across ALL workers (`--workers 2`). A worker that did not run the
    build otherwise reports a stale 'idle' with empty table_progress — which makes
    the setup wizard's progress list flicker between the real list and nothing, and
    never observe the terminal 'done' (so it hangs until the client safety timeout).

    build_progress.json is written by the builder on every table completion and on
    every status transition, so it is the authority whenever it is further along
    than this worker's memory. We only ever move *forward* (higher rank, or same
    rank with more tables done), so the builder worker — which is always at least
    as current — is never regressed by a lagging disk read.
    """
    disk = _load_shared_task_state()
    if not disk:
        return

    def _done_count(state: dict) -> int:
        # A failed table is as settled as a built one: counting only "done" kept a
        # non-builder worker from ever reporting a table error mid-build.
        return sum(1 for v in (state.get("table_progress") or {}).values() if v in ("done", "error"))

    mem_rank = _TASK_STATE_RANK.get(_create_task_state.get("status", "idle"), 0)
    disk_rank = _TASK_STATE_RANK.get(disk.get("status", "idle"), 0)
    # A fresh build started after a previous one finished: disk shows 'running'
    # while this (non-builder) worker still holds a stale terminal state. The latest
    # persisted state is only ever 'running' when a build is actually in flight
    # (the builder persists 'done'/'error' when it ends), so adopt it — otherwise
    # this worker would report the old build's 'done' for the new run.
    fresh_rerun = disk.get("status") == "running" and _create_task_state.get("status") in ("done", "error")
    if fresh_rerun or disk_rank > mem_rank or (
        disk_rank == mem_rank and _done_count(disk) > _done_count(_create_task_state)
    ):
        _create_task_state["status"] = disk.get("status", _create_task_state["status"])
        _create_task_state["error"] = disk.get("error")
        _create_task_state["table_progress"] = disk.get("table_progress") or {}
        _create_task_state["table_errors"] = disk.get("table_errors") or {}
        _create_task_state["table_results"] = disk.get("table_results") or {}
        _create_task_state["user_grants"] = disk.get("user_grants")


# App config/state tables Create Tables bootstraps after the gold tables.
_SETUP_CONFIG_TABLES = [
    "app_response_cache", "app_user_permissions",
    "app_mv_refresh_state", "app_schedule_settings", "app_workspace_filter",
]


# Core billing tables — must exist for the dashboard to be functional.
# Used by get_setup_status to gate the "ready" state.
_CORE_REQUIRED_TABLES = frozenset({
    "daily_usage_summary",
    "daily_product_breakdown",
    "daily_workspace_breakdown",
})

def autodiscover_storage_location() -> tuple[str, str] | None:
    """Find where the app's own MV tables already live and adopt that location.

    Runs when no catalog/schema is configured — the case on a workspace where DBFS
    is disabled (so the app's durable override can't be stored there) and a redeploy
    wiped `.settings/`. Without this the app forgets where its tables are and shows
    "setup incomplete" even though the tables exist.

    Searches system.information_schema for the CORE MV tables and adopts the single
    location that holds all of them. Conservative: if ambiguous it does nothing.
    On success it persists the location and the setup-done flag. Returns
    (catalog, schema) on success, else None.
    """
    global _setup_confirmed_ready
    from backend.db import execute_query, save_catalog_schema

    core = sorted(_CORE_REQUIRED_TABLES)
    in_list = ", ".join("'" + t + "'" for t in core)
    try:
        rows = execute_query(
            f"""SELECT table_catalog AS c, table_schema AS s, COUNT(*) AS n
                FROM system.information_schema.tables
                WHERE table_name IN ({in_list})
                GROUP BY table_catalog, table_schema
                ORDER BY n DESC""",
            no_cache=True,
        )
    except Exception as e:
        logger.warning("Storage auto-discovery query failed (non-fatal): %s", e)
        return None

    candidates = [(r["c"], r["s"]) for r in (rows or []) if int(r.get("n") or 0) >= len(core)]
    if not candidates:
        logger.info("Storage auto-discovery: no schema holds the core MV tables yet.")
        return None
    if len(candidates) > 1:
        logger.warning(
            "Storage auto-discovery ambiguous — %d schemas hold the core tables (%s); "
            "leaving unconfigured so the setup wizard can pick.",
            len(candidates), candidates,
        )
        return None

    cat, sch = candidates[0]
    try:
        save_catalog_schema(cat, sch)
    except Exception as e:
        logger.warning("Storage auto-discovery: could not persist %s.%s: %s", cat, sch, e)
        return None

    try:
        import time as _time
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETUP_DONE_FILE, "w") as f:
            json.dump({"completed_at": _time.time(), "autodiscovered": True}, f)
        _setup_confirmed_ready = True
    except Exception as e:
        logger.debug("Storage auto-discovery: could not write setup-done flag: %s", e)

    logger.info("Storage auto-discovered and adopted: %s.%s", cat, sch)
    return cat, sch


# Auto-fail bootstrap after this many seconds to prevent infinite spinner
_BOOTSTRAP_TIMEOUT_SECONDS = 25 * 60  # 25 minutes

# Once confirmed True in this process, skip all DBFS/UC checks on subsequent requests.
_setup_confirmed_ready: bool = False


SYSTEM_TABLE_GRANTS = [
    ("USE CATALOG", "CATALOG", "system"),
    ("USE SCHEMA",  "SCHEMA",  "system.billing"),
    ("SELECT",      "TABLE",   "system.billing.usage"),
    ("SELECT",      "TABLE",   "system.billing.list_prices"),
    ("SELECT",      "TABLE",   "system.billing.account_prices"),
    ("USE SCHEMA",  "SCHEMA",  "system.query"),
    ("SELECT",      "TABLE",   "system.query.history"),
    ("USE SCHEMA",  "SCHEMA",  "system.compute"),
    ("SELECT",      "TABLE",   "system.compute.clusters"),
    ("SELECT",      "TABLE",   "system.compute.warehouses"),
    ("SELECT",      "TABLE",   "system.compute.warehouse_events"),
    ("USE SCHEMA",  "SCHEMA",  "system.lakeflow"),
    ("SELECT",      "TABLE",   "system.lakeflow.jobs"),
    ("SELECT",      "TABLE",   "system.lakeflow.pipelines"),
    ("SELECT",      "TABLE",   "system.lakeflow.job_run_timeline"),
    ("USE SCHEMA",  "SCHEMA",  "system.serving"),
    ("SELECT",      "TABLE",   "system.serving.served_entities"),
    ("USE SCHEMA",  "SCHEMA",  "system.access"),
    ("SELECT",      "TABLE",   "system.access.audit"),
    ("SELECT",      "TABLE",   "system.access.workspaces_latest"),
]


def _grant_sp_schema_access(catalog: str, schema: str) -> dict:
    """Grant the app SP required permissions via SQL GRANT statements.

    Uses the SQL warehouse (JDBC) instead of the UC REST API to avoid the
    'unity-catalog' OAuth scope requirement — forwarded user tokens and SP
    m2m tokens do not carry that scope in Databricks Apps.

    Runs as the calling user (set via _user_token ContextVar by the caller).
    Returns {"ok": bool, "sp_client_id": str, "applied": int, "failed": int, "errors": list}
    """
    from backend.db import _user_token

    sp_client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
    if not sp_client_id:
        logger.warning("DATABRICKS_CLIENT_ID not set — skipping SP grants")
        return {"ok": False, "sp_client_id": "", "applied": 0, "failed": 0,
                "errors": ["DATABRICKS_CLIENT_ID not set — app has no service principal to grant"]}

    p = sp_client_id  # principal name in GRANT statements

    # App schema grants only — system table grants are handled by _grant_system_via_user_sql.
    # Running system GRANT as the SP always fails (SP is not a metastore admin), so
    # including them here just inflates the "applied" count with fake successes.
    grant_stmts: list[tuple[str, str]] = [
        (f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{p}`",                           f"CATALOG/{catalog}"),
        (f"GRANT CREATE SCHEMA ON CATALOG `{catalog}` TO `{p}`",                         f"CREATE_SCHEMA/{catalog}"),
        (f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{p}`",                  f"SCHEMA/{catalog}.{schema}"),
        (f"GRANT CREATE TABLE ON SCHEMA `{catalog}`.`{schema}` TO `{p}`",                f"CREATE_TABLE/{catalog}.{schema}"),
        (f"GRANT MODIFY ON SCHEMA `{catalog}`.`{schema}` TO `{p}`",                      f"MODIFY/{catalog}.{schema}"),
        (f"GRANT SELECT ON SCHEMA `{catalog}`.`{schema}` TO `{p}`",                      f"SELECT/{catalog}.{schema}"),
    ]

    ok = failed = 0
    errors: list[str] = []

    from backend.db import execute_query as _exec, _user_token as _exec_tok
    for sql_stmt, label in grant_stmts:
        ctx = _exec_tok.set("")  # force SP auth for sql scope
        try:
            _exec(sql_stmt, no_cache=True)
            ok += 1
            logger.info(f"SQL GRANT ok: {label} → {p}")
        except Exception as e:
            err_lower = str(e).lower()
            # "already" = already granted (idempotent, non-fatal).
            # "not found"/"does not exist" = catalog/schema not created yet (non-fatal, wizard handles it).
            if any(kw in err_lower for kw in ("already", "not found", "does not exist")):
                ok += 1
                logger.debug(f"GRANT skipped (pre-existing or not yet created): {label}: {e}")
            else:
                msg = _clean_sdk_error(str(e))
                logger.warning(f"SQL GRANT failed: {label}: {msg}")
                errors.append(f"{label}: {msg}")
                failed += 1
        finally:
            _exec_tok.reset(ctx)

    logger.info(f"SP SQL grants: {ok} ok, {failed} failed for {p}")

    # Warehouse CAN_USE via REST API — uses /api/2.0/permissions/ (not UC API,
    # no unity-catalog scope required).
    try:
        user_tok = _user_token.get()
        host = os.getenv("DATABRICKS_HOST", "")
        if user_tok and host:
            from databricks.sdk import WorkspaceClient
            w = WorkspaceClient(host=host, token=user_tok, auth_type="pat")
        else:
            w = get_workspace_client()
        _grant_warehouse_can_use(w, sp_client_id)
    except Exception as e:
        logger.warning(f"Warehouse CAN_USE grant failed (non-fatal): {e}")

    return {
        "ok": failed == 0,
        "sp_client_id": sp_client_id,
        "applied": ok,
        "failed": failed,
        "errors": errors,
    }


def _grant_warehouse_can_use(w, sp_client_id: str) -> None:
    """Grant CAN_USE on the configured SQL warehouse to the app SP via REST API.

    Called from _grant_sp_schema_access on every /api/setup/status load when
    a user OAuth token is present. Idempotent — re-running after a redeploy
    that creates a new SP re-grants without any manual intervention.
    """
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    if not http_path:
        logger.warning("DATABRICKS_HTTP_PATH not set — skipping warehouse CAN_USE grant")
        return

    # Extract warehouse ID from path like /sql/1.0/warehouses/{id}
    parts = http_path.strip("/").split("/")
    warehouse_id = parts[-1] if parts and parts[-1] != "warehouses" else ""
    if not warehouse_id:
        logger.warning(f"Cannot parse warehouse ID from http_path: {http_path}")
        return

    try:
        w.api_client.do(
            "PATCH",
            f"/api/2.0/permissions/warehouses/{warehouse_id}",
            body={
                "access_control_list": [{
                    "service_principal_name": sp_client_id,
                    "permission_level": "CAN_USE",
                }]
            },
        )
        logger.info(f"Granted CAN_USE on warehouse {warehouse_id} to SP {sp_client_id}")
    except Exception as e:
        logger.warning(f"Failed to grant warehouse CAN_USE on {warehouse_id}: {e}")


@router.get("/status")
async def get_setup_status() -> dict[str, Any]:
    """Check setup status for the wizard gate.

    setup_done.json lives in .settings/ which is wiped on every Databricks Apps
    git redeploy. If the file is absent the wizard always shows, regardless of
    whether tables already exist from a previous deployment.

    The only exception: if a table-creation task is actively running (the wizard
    is on the create-tables step and polling), return 'initializing' so it keeps
    polling instead of resetting back to the wizard start.
    """
    global _setup_confirmed_ready
    import asyncio as _asyncio

    # Cross-worker reconciliation: adopt the shared build progress written by
    # whichever worker is running the build. Without this, polls that land on a
    # non-builder worker make the wizard's progress list flicker and never see
    # the terminal 'done'. Cheap (one small local file read); safe (forward-only).
    _reconcile_task_state_from_disk()

    if _setup_confirmed_ready and _create_task_state["status"] != "running":
        catalog, schema = get_catalog_schema()
        return {
            "catalog": catalog, "schema": schema,
            "tables": {}, "all_tables_exist": True, "missing_tables": [],
            "status": "ready", "task": _create_task_state.copy(), "next_poll_ms": 30000,
        }

    # While table creation is running (wizard polling mid-flow), keep returning
    # initializing regardless of setup_done state so the wizard doesn't reset.
    if _create_task_state["status"] == "running":
        import time as _time
        catalog, schema = get_catalog_schema()
        started = _create_task_state.get("started_at") or _time.monotonic()
        elapsed = int(_time.monotonic() - started)
        _create_task_state["elapsed_seconds"] = elapsed
        if elapsed > _BOOTSTRAP_TIMEOUT_SECONDS:
            _create_task_state["status"] = "error"
            _create_task_state["error"] = (
                f"Table creation timed out after {elapsed // 60} minutes. "
                "The warehouse may be cold or the billing dataset is very large. "
                "Use the Setup wizard to retry, or check app logs for details."
            )
            logger.error(f"Bootstrap timed out after {elapsed}s — marking as error")
            _persist_task_state()
        else:
            return {
                "catalog": catalog,
                "schema": schema,
                "tables": {},
                "all_tables_exist": False,
                "missing_tables": [],
                "status": "initializing",
                "task": _create_task_state.copy(),
                "next_poll_ms": 5000,
            }

    # With multiple uvicorn workers each worker has its own _create_task_state.
    # If another worker is running the task, the shared file will show "running"
    # even though this worker's in-process state shows "idle".
    if _create_task_state["status"] == "idle":
        try:
            with open(_TASK_STATE_FILE) as _fh:
                _file_task = json.load(_fh)
            if _file_task.get("status") == "running":
                _cat, _sch = get_catalog_schema()
                return {
                    "catalog": _cat, "schema": _sch,
                    "tables": {}, "all_tables_exist": False, "missing_tables": [],
                    "status": "initializing",
                    "task": _file_task,
                    "next_poll_ms": 5000,
                }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    catalog, schema = get_catalog_schema()

    # No catalog configured — wizard must run first.
    if not catalog or not schema:
        return {
            "catalog": catalog,
            "schema": schema,
            "tables": {},
            "all_tables_exist": False,
            "missing_tables": [],
            "status": "setup_required",
            "task": _create_task_state.copy(),
            "next_poll_ms": 30000,
        }

    # Short-circuit: if the local flag is absent (e.g. container restart wiped
    # .settings/) but DBFS records a previous completion, check UC metadata to
    # verify core tables still exist. check_materialized_views_exist uses the UC
    # REST API (no SQL warehouse needed) so this is fast even on a cold start.
    if not os.path.exists(SETUP_DONE_FILE):
        from backend.db import read_setup_complete as read_dbfs_setup_complete
        loop = _asyncio.get_running_loop()
        dbfs_complete = await loop.run_in_executor(None, read_dbfs_setup_complete)
        if dbfs_complete:
            # Verify tables still exist — they may have been dropped between deploys.
            tables = await loop.run_in_executor(None, check_materialized_views_exist, catalog, schema)
            core_exist = all(tables.get(t, False) for t in _CORE_REQUIRED_TABLES)
            if not core_exist:
                # DBFS flag is stale — tables are gone. Wizard must run again.
                missing = [name for name, exists in tables.items() if not exists]
                logger.info("DBFS flag set but core tables missing (%s) — returning setup_required", missing)
                return {
                    "catalog": catalog, "schema": schema,
                    "tables": tables, "all_tables_exist": False, "missing_tables": missing,
                    "status": "setup_required",
                    "task": _create_task_state.copy(), "next_poll_ms": 30000,
                }
            # Tables exist — restore setup_done.json for future fast-paths.
            try:
                import datetime as _dt
                os.makedirs(SETTINGS_DIR, exist_ok=True)
                with open(SETUP_DONE_FILE, "w") as _f:
                    json.dump({"completed_at": _dt.datetime.utcnow().isoformat(),
                               "restored_from_dbfs": True}, _f)
                logger.info("setup_done.json restored from DBFS flag (container restart)")
            except Exception as _e:
                logger.warning(f"Could not restore setup_done.json: {_e}")
            _setup_confirmed_ready = True
            return {
                "catalog": catalog, "schema": schema,
                "tables": tables, "all_tables_exist": True, "missing_tables": [],
                "status": "ready",
                "task": _create_task_state.copy(),
                "next_poll_ms": 30000,
            }

    loop = _asyncio.get_running_loop()
    tables = await loop.run_in_executor(None, check_materialized_views_exist, catalog, schema)

    core_exist = all(tables.get(t, False) for t in _CORE_REQUIRED_TABLES)
    all_exist = all(tables.values())
    missing = [name for name, exists in tables.items() if not exists]

    if not os.path.exists(SETUP_DONE_FILE):
        # No setup_done.json — wizard must run. Tables may already exist from a
        # previous app or a dropped-and-rebuilt scenario; the wizard handles both
        # (it detects existing tables and skips re-creation). Do NOT auto-heal here:
        # auto-heal cannot distinguish a brand-new app deployment from a git
        # redeploy of the same app, so it would silently skip the wizard for new
        # apps that share a catalog/schema with an older deployment.
        # Container-restart recovery (same app, same SP, wiped .settings/) is
        # handled by the DBFS check above — that path is SP-scoped so it is safe.
        return {
            "catalog": catalog,
            "schema": schema,
            "tables": tables,
            "all_tables_exist": core_exist,
            "missing_tables": missing,
            "status": "setup_required",
            "task": _create_task_state.copy(),
            "next_poll_ms": 30000,
        }

    if not core_exist:
        # setup_done.json exists but core tables aren't built yet — background build
        # in progress or tables were dropped after a completed setup. Return "ready"
        # so the dashboard shows (falls back to direct system-table queries). Don't
        # force the wizard again: the user already completed setup.
        _setup_confirmed_ready = True
        return {
            "catalog": catalog,
            "schema": schema,
            "tables": tables,
            "all_tables_exist": False,
            "missing_tables": missing,
            "status": "ready",
            "tables_building": True,
            "task": _create_task_state.copy(),
            "next_poll_ms": 5000,
        }

    # Tables exist — but if a user OAuth token is present, re-run SP grants in the
    # background. Each git deploy creates a new SP with no grants; auto-bootstrap only
    # fires when tables are missing. Re-granting here is idempotent and non-fatal.
    if all_exist:
        user_token = _db_user_token.get()
        if user_token:
            import threading as _threading
            _token_snap = user_token
            _catalog_snap = catalog
            _schema_snap = schema
            _sp_id_snap = os.getenv("DATABRICKS_CLIENT_ID", "")
            def _bg_grant():
                tok = _db_user_token.set(_token_snap)
                try:
                    _grant_sp_schema_access(_catalog_snap, _schema_snap)
                finally:
                    _db_user_token.reset(tok)
                if _sp_id_snap:
                    _grant_system_via_user_sql(_token_snap, _sp_id_snap)
            _threading.Thread(target=_bg_grant, daemon=True).start()

    # Core tables exist (checked above) and setup_done.json is present (either
    # pre-existing or just auto-healed). Non-core tables being absent just means
    # some SQL/job views are still building — the dashboard falls back gracefully.
    # Never return "setup_required" here: it would re-show the wizard incorrectly.
    _setup_confirmed_ready = True
    return {
        "catalog": catalog,
        "schema": schema,
        "tables": tables,
        "all_tables_exist": all_exist,
        "missing_tables": missing,
        "status": "ready",
        "task": _create_task_state.copy(),
        "next_poll_ms": 30000,
    }


@router.post("/complete")
async def mark_setup_complete(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Write setup_done.json and kick off the initial MV build in the background.

    Called by the frontend when the user clicks 'Complete' in the setup wizard.
    Without this file, every fresh git redeploy will show the wizard.
    The background rebuild creates all materialized views so the dashboard has
    data immediately after setup — no manual Rebuild click required.
    """
    global _setup_confirmed_ready
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETUP_DONE_FILE, "w") as f:
            import time as _time
            json.dump({"completed_at": _time.time()}, f)
        logger.info("Setup wizard marked complete — setup_done.json written")
    except Exception as e:
        logger.error(f"Failed to write setup_done.json: {e}")
        return {"ok": False, "error": str(e)}

    _setup_confirmed_ready = True

    # Persist to DBFS so the flag survives container restarts (Databricks Apps
    # recreates the container from scratch on every stop/start, wiping .settings/).
    from backend.db import write_setup_complete as write_dbfs_setup_complete
    await asyncio.to_thread(write_dbfs_setup_complete)

    # Invalidate the settings/tables cache so the post-setup check shows fresh data.
    from backend.routers import settings as _settings_router
    _settings_router._tables_cache = None

    # Kick off the initial MV build in the background — the catalog/schema were
    # just created so no data exists yet.  Failures are non-fatal: the dashboard
    # falls back to direct system-table queries until the build completes.
    try:
        catalog, schema = get_catalog_schema()
        if catalog and schema:
            background_tasks.add_task(_refresh_tables_task, catalog, schema)
            logger.info(f"Initial MV build queued for {catalog}.{schema}")
    except Exception as e:
        logger.warning(f"Could not queue initial MV build (non-fatal): {e}")

    return {"ok": True}


@router.post("/mark-complete")
async def mark_setup_complete_manual() -> dict[str, Any]:
    """Mark setup complete WITHOUT triggering a rebuild.

    For when the app-managed tables already exist but the app lost its
    completion flag (e.g. a redeploy wiped .settings/ and DBFS is disabled, so
    the durable flag couldn't be restored). Lets the user dismiss the
    'setup incomplete' banner directly instead of re-running the wizard.
    """
    global _setup_confirmed_ready
    try:
        import time as _time
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETUP_DONE_FILE, "w") as f:
            json.dump({"completed_at": _time.time(), "manual": True}, f)
    except Exception as e:
        logger.error(f"Failed to write setup_done.json (manual mark-complete): {e}")
        return {"ok": False, "error": str(e)}

    _setup_confirmed_ready = True
    try:
        from backend.db import write_dbfs_setup_complete
        write_dbfs_setup_complete()
    except Exception as e:
        logger.debug("mark-complete: DBFS persist skipped (non-fatal): %s", e)
    try:
        from backend.routers import settings as _settings_router
        _settings_router._tables_cache = None
    except Exception:
        pass
    return {"ok": True}


@router.post("/rerun")
async def rerun_setup() -> dict[str, Any]:
    """Clear setup_done.json and reset task state so the wizard shows again.

    Called when an admin clicks Re-run Setup Wizard in Settings. Safe to call
    at any time — existing tables are left in place and are not dropped.
    """
    try:
        if os.path.exists(SETUP_DONE_FILE):
            os.remove(SETUP_DONE_FILE)
            logger.info("setup_done.json removed — wizard will reappear on next load")
    except Exception as e:
        logger.warning(f"Could not remove setup_done.json: {e}")

    _create_task_state["status"] = "idle"
    _create_task_state["error"] = None
    _create_task_state["started_at"] = None
    _create_task_state["elapsed_seconds"] = None
    _create_task_state["table_progress"] = {}

    return {"ok": True}


@router.post("/reset-bootstrap")
async def reset_bootstrap_state() -> dict[str, Any]:
    """Reset the in-process bootstrap state so auto-init can retry.

    Call this if the app is stuck on the 'Setting up your workspace' spinner.
    Resets the internal task state to 'idle' so the next /status poll will
    attempt auto-bootstrap again (or fall through to setup_required if no
    user token is available).
    """
    prev = _create_task_state.copy()
    _create_task_state["status"] = "idle"
    _create_task_state["error"] = None
    logger.info(f"Bootstrap state manually reset (was: {prev})")
    return {"ok": True, "previous": prev, "current": _create_task_state.copy()}


@router.get("/created-tables")
async def get_created_tables() -> dict[str, Any]:
    """Which tables Create Tables manages exist in the app schema (UC REST, no warehouse).

    The wizard shows this when no build ran in this session, so its build results
    (task.table_results) aren't available.
    """
    catalog, schema = get_catalog_schema()
    names = list(_MV_TABLES) + _SETUP_CONFIG_TABLES
    try:
        exists = await asyncio.to_thread(check_materialized_views_exist, catalog, schema, names, True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not list the app tables: {_clean_sdk_error(str(e))}")
    return {
        "catalog": catalog,
        "schema": schema,
        "tables": [{"name": n, "exists": bool(exists.get(n))} for n in names],
    }


@router.get("/bootstrap-state")
async def get_bootstrap_state() -> dict[str, Any]:
    """Return current in-process bootstrap task state for debugging."""
    return _create_task_state.copy()


def _build_system_grants_sql(sp_id: str) -> str:
    """Return the full set of system table GRANT statements as a copyable SQL block."""
    lines = []
    for privilege, obj_type, obj_name in SYSTEM_TABLE_GRANTS:
        parts = obj_name.split(".")
        q = ".".join(f"`{p}`" for p in parts)
        if obj_type == "CATALOG":
            lines.append(f"GRANT USE CATALOG ON CATALOG {q} TO `{sp_id}`;")
        elif obj_type == "SCHEMA":
            lines.append(f"GRANT USE SCHEMA ON SCHEMA {q} TO `{sp_id}`;")
        elif obj_type == "TABLE":
            lines.append(f"GRANT SELECT ON TABLE {q} TO `{sp_id}`;")
    return "\n".join(lines)


def _grant_system_via_user_sql(user_token: str, sp_id: str) -> dict:
    """Try SQL GRANT on system tables using the calling user's forwarded token.

    Uses databricks.sql.connect() directly with the forwarded token — the
    documented Apps pattern for user-auth + sql scope.  Opportunistic: works
    when the app has user auth + sql scope configured AND the user is a
    metastore admin.  Expected to fail in other configurations.

    Returns {"ok", "applied", "failed", "errors", "needs_admin", "obo_scope_missing"}.
    needs_admin=True  → user lacks privilege; surface copyable SQL for an admin.
    obo_scope_missing → token absent or sql scope not configured on the app.
    """
    if not user_token:
        return {"ok": False, "applied": 0, "failed": 0,
                "errors": ["No forwarded user token available"],
                "needs_admin": False, "obo_scope_missing": True}

    host = os.getenv("DATABRICKS_HOST", "")
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    if not http_path:
        wh_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
        if wh_id:
            http_path = f"/sql/1.0/warehouses/{wh_id}"
    if not host or not http_path:
        return {"ok": False, "applied": 0, "failed": 0,
                "errors": ["Warehouse not configured — complete warehouse setup first"],
                "needs_admin": False, "obo_scope_missing": False}

    grant_stmts: list[tuple[str, str]] = []
    for privilege, obj_type, obj_name in SYSTEM_TABLE_GRANTS:
        parts = obj_name.split(".")
        q = ".".join(f"`{p}`" for p in parts)
        if obj_type == "CATALOG":
            grant_stmts.append((f"GRANT USE CATALOG ON CATALOG {q} TO `{sp_id}`", f"CATALOG/{obj_name}"))
        elif obj_type == "SCHEMA":
            grant_stmts.append((f"GRANT USE SCHEMA ON SCHEMA {q} TO `{sp_id}`", f"SCHEMA/{obj_name}"))
        elif obj_type == "TABLE":
            grant_stmts.append((f"GRANT SELECT ON TABLE {q} TO `{sp_id}`", f"TABLE/{obj_name}"))

    ok = failed = 0
    errors: list[str] = []
    needs_admin = False
    obo_scope_missing = False

    sql_host = host.removeprefix("https://").removeprefix("http://")
    try:
        from databricks import sql as dbsql
        conn = dbsql.connect(
            server_hostname=sql_host,
            http_path=http_path,
            access_token=user_token,
        )
    except Exception as e:
        err_lower = str(e).lower()
        if any(kw in err_lower for kw in ("scope", "oauth", "token", "auth")):
            obo_scope_missing = True
        logger.warning(f"User SQL GRANT connect failed: {_clean_sdk_error(str(e))}")
        return {"ok": False, "applied": 0, "failed": len(grant_stmts),
                "errors": [_clean_sdk_error(str(e))],
                "needs_admin": False, "obo_scope_missing": obo_scope_missing}

    try:
        with conn.cursor() as cursor:
            for sql_stmt, label in grant_stmts:
                try:
                    cursor.execute(sql_stmt)
                    ok += 1
                    logger.info(f"User SQL GRANT ok: {label} → {sp_id}")
                except Exception as e:
                    err_lower = str(e).lower()
                    if "already" in err_lower:
                        ok += 1
                        logger.debug(f"User SQL GRANT already exists: {label}")
                    elif any(kw in err_lower for kw in (
                        "insufficient", "permission", "denied",
                        "unauthorized", "forbidden",
                    )):
                        needs_admin = True
                        failed += 1
                        errors.append(f"{label}: {_clean_sdk_error(str(e))}")
                    elif any(kw in err_lower for kw in ("scope", "required scopes", "oauth")):
                        obo_scope_missing = True
                        failed += 1
                        errors.append(f"{label}: {_clean_sdk_error(str(e))}")
                    elif "system.access.audit" in label and any(
                        kw in err_lower for kw in ("not found", "does not exist", "table_or_view_not_found", "no such")
                    ):
                        # Audit schema not yet enabled in Account Console — GRANT can't
                        # succeed until an account admin enables System Tables > access.
                        failed += 1
                        errors.append(
                            "system.access.audit: Audit log schema not enabled — "
                            "an account admin must enable System Tables > access schema "
                            "in the Databricks Account Console before this grant can be applied."
                        )
                        logger.warning("system.access.audit GRANT skipped — access schema not enabled at account level")
                    else:
                        failed += 1
                        errors.append(f"{label}: {_clean_sdk_error(str(e))}")
    finally:
        conn.close()

    logger.info(f"User SQL system grants: {ok} ok, {failed} failed "
                f"(needs_admin={needs_admin}, obo_scope_missing={obo_scope_missing})")
    return {"ok": failed == 0, "applied": ok, "failed": failed, "errors": errors,
            "needs_admin": needs_admin, "obo_scope_missing": obo_scope_missing}


@router.post("/grant-sp-system-access")
async def grant_sp_system_access(request: Request) -> dict[str, Any]:
    """Re-run all SP grants using the current user's OAuth token.

    Call this after a git deploy when the new SP is missing system table or
    app schema grants. Requires the calling user to be a metastore admin or
    account admin so the GRANT statements succeed on system tables.
    Returns a summary of how many grants were applied.
    """
    from backend.materialized_views import get_catalog_schema

    user_token = request.headers.get("x-forwarded-access-token", "")
    sp_id = os.getenv("DATABRICKS_CLIENT_ID", "")
    catalog, schema = get_catalog_schema()

    # App catalog grants run as SP (SP owns the catalog, has sql scope).
    # Set _db_user_token so the warehouse CAN_USE sub-grant inside
    # _grant_sp_schema_access uses the user's token (has CAN_MANAGE on warehouse).
    pre_tok = _db_user_token.set(user_token)
    try:
        app_result = _grant_sp_schema_access(catalog, schema)
    finally:
        _db_user_token.reset(pre_tok)

    # System table grants — try as the calling user via SQL warehouse (requires
    # user auth + sql scope + metastore admin).  Opportunistic: fall back to
    # copyable SQL when token lacks scope or user lacks privilege.
    sys_result = _grant_system_via_user_sql(user_token, sp_id)

    total_applied = app_result["applied"] + sys_result["applied"]
    total_failed = app_result["failed"] + sys_result["failed"]
    all_errors = app_result.get("errors", []) + sys_result.get("errors", [])

    # Build copyable SQL for any failed system grants so the UI can surface it
    # with a "Run as metastore admin" label.
    # Always show copyable SQL when grants couldn't be applied (failed OR no token).
    # system.access.audit requires account-admin; other tables need metastore admin.
    grants_sql = _build_system_grants_sql(sp_id) if (sys_result["failed"] > 0 or sys_result.get("obo_scope_missing")) else None

    return {
        "ok": total_failed == 0,
        "status": "ok" if total_failed == 0 else "partial",
        "catalog": catalog,
        "schema": schema,
        "sp_client_id": sp_id,
        "applied": total_applied,
        "failed": total_failed,
        "errors": all_errors,
        "needs_admin": sys_result.get("needs_admin", False),
        "obo_scope_missing": sys_result.get("obo_scope_missing", False),
        "grants_sql": grants_sql,
    }


def _clean_sdk_error(msg: str) -> str:
    """Strip Databricks SDK config debug blob from exception messages.

    SDK exceptions append '. Config: host=..., client_id=...' to every error.
    That leaks credentials and is not useful to display in the UI.
    """
    for marker in (". Config:", " Config: ", "\nConfig:"):
        idx = msg.find(marker)
        if idx > 0:
            return msg[:idx]
    return msg


def _execute_as_sp(sql: str) -> list[dict]:
    """Execute a query as the SP — explicitly clears the user token from context."""
    from backend.db import execute_query, _user_token
    tok = _user_token.set("")
    try:
        return execute_query(sql, no_cache=True) or []
    finally:
        _user_token.reset(tok)


def _preflight_catalog_check(catalog: str) -> dict:
    """Verify the target catalog exists using the UC SDK (no warehouse needed).

    Returns {"ok": bool, "status": str, "message": str}.
    Statuses: invalid_config | catalog_missing | catalog_check_failed | ready
    """
    if not catalog:
        return {
            "ok": False,
            "status": "invalid_config",
            "message": "No catalog configured — return to the Storage step.",
        }
    try:
        w = get_workspace_client()
        w.catalogs.get(catalog)
        return {"ok": True, "status": "ready", "message": f"Catalog `{catalog}` is accessible."}
    except Exception as e:
        msg = _clean_sdk_error(str(e))
        if any(kw in msg.lower() for kw in ("not found", "does not exist", "404", "catalog_not_found")):
            return {
                "ok": False,
                "status": "catalog_missing",
                "message": (
                    f"Catalog `{catalog}` does not exist. "
                    "Create it in Unity Catalog before continuing."
                ),
            }
        return {
            "ok": False,
            "status": "catalog_check_failed",
            "message": f"Could not verify catalog `{catalog}`: {msg}",
        }


def _verify_built_objects_as_sp(catalog: str, schema: str, skipped: frozenset[str] = frozenset()) -> dict:
    """Probe every MV table with a one-row read as the SP to confirm UC grants propagated.

    `skipped`: tables the build skipped (optional source not connected, e.g. cloud
    invoice actuals). They don't exist, so probing them would fail every setup that
    hasn't connected those sources.

    Returns {"ok": bool, "failed": list[str], "errors": dict[str, str]}
    """
    failed: list[str] = []
    errors: dict[str, str] = {}
    for table in _MV_TABLES:
        if table in skipped:
            continue
        try:
            _execute_as_sp(f"SELECT 1 FROM `{catalog}`.`{schema}`.`{table}` LIMIT 1")
        except Exception as e:
            failed.append(table)
            errors[table] = _clean_sdk_error(str(e))
    return {"ok": len(failed) == 0, "failed": failed, "errors": errors}


@router.post("/create-tables")
async def create_tables(
    request: Request,
    background_tasks: BackgroundTasks,
    catalog: str = Query(default=None, description="Target catalog"),
    schema: str = Query(default=None, description="Target schema"),
    run_in_background: bool = Query(default=True, description="Run in background"),
) -> dict[str, Any]:
    """Create all materialized view tables.

    This will create pre-aggregated tables from system tables for fast queries.
    Tables are created with 180 days (6 months) of historical data.

    WARNING: This operation can take several minutes on large accounts.
    Set run_in_background=true (default) to run asynchronously.
    """
    cat, sch = get_catalog_schema()
    target_catalog = catalog or cat
    target_schema = schema or sch

    if _create_task_state.get("status") == "running":
        raise HTTPException(status_code=409, detail="Table creation already in progress")

    if run_in_background:
        import time as _time
        # Before the state turns 'running': if this stalled or raised after it, no
        # build would start and every retry would be refused as already in progress.
        grant_to = await _admin_installer_email(request)
        _create_task_state["status"] = "running"
        _create_task_state["error"] = None
        _create_task_state["started_at"] = _time.monotonic()
        _create_task_state["elapsed_seconds"] = 0
        _create_task_state["table_progress"] = {t: "pending" for t in _MV_TABLES + _SETUP_CONFIG_TABLES}
        _create_task_state["table_errors"] = {}
        _create_task_state["table_results"] = {}
        _create_task_state["user_grants"] = None
        _persist_task_state()
        # Read the raw header token directly — _auth_mode may have been locked to "sp"
        # (e.g. after a scope error on a previous request), which forces _db_user_token
        # to "" even when x-forwarded-access-token IS present in the request.
        # Setup operations must always run as the user, not the SP.
        _token_snap = (
            request.headers.get("x-forwarded-access-token", "")
            or _db_user_token.get()
        )
        background_tasks.add_task(
            _create_tables_task, target_catalog, target_schema, _token_snap,
            grant_to or "", grant_to is not None,
        )
        return {
            "status": "started",
            "message": "Table creation started in background. Check /api/setup/status for progress.",
            "catalog": target_catalog,
            "schema": target_schema,
        }
    else:
        # Run synchronously (blocking)
        results = create_materialized_views(target_catalog, target_schema)
        return {
            "status": "completed",
            "catalog": target_catalog,
            "schema": target_schema,
            "results": results,
        }


def _create_tables_task(
    catalog: str, schema: str, user_token: str = "", user_email: str = "", grant_installer: bool = False,
):
    """Background task: grants → build → verify.

    The catalog-existence check is done by the frontend endpoint (user token)
    before this task starts. This task does NOT re-check via the SP because the
    SP has no USE CATALOG until the pre-creation grants run.

    Pre-grants: Apply USE CATALOG + CREATE SCHEMA to the SP first. The
      permissions step may have silently no-op'd these when the catalog didn't
      exist at grant time; re-applying now that it exists is the authoritative fix.
    3s sleep: brief propagation wait between grants and warehouse ops.
    Phase 2 — Build: create_materialized_views runs as SP.
    Post-grants: Apply schema-level privileges now that the schema exists, and
      grant the installing user USE + MANAGE on the catalog and schema the SP owns.
    Phase 3 — Verify: one-row probe on every MV table as SP with retry backoff.
      UC grant propagation can take 10-60s; [0,10,20,30]s delays = 60s budget.
    Only marks "done" after Phase 3 passes.
    """
    import time as _time

    logger.info(f"Starting background table creation for {catalog}.{schema}")

    if not catalog or not schema:
        _create_task_state["status"] = "error"
        _create_task_state["error"] = "Catalog and schema must be configured before creating tables."
        _persist_task_state()
        return

    try:
        # Pre-creation grants — must run before create_materialized_views.
        # The SP needs USE CATALOG + CREATE SCHEMA before it can create the schema.
        if user_token:
            sp_id = os.getenv("DATABRICKS_CLIENT_ID", "")
            pre_tok = _db_user_token.set(user_token)
            try:
                pre_grant = _grant_sp_schema_access(catalog, schema)
                logger.info(
                    f"Pre-creation grants: ok={pre_grant['ok']} "
                    f"applied={pre_grant['applied']} failed={pre_grant['failed']}"
                )
                # Only hard-fail if zero grants landed — partial system-table
                # failures are non-fatal for table creation in the app catalog.
                if pre_grant.get("applied", 0) == 0 and pre_grant.get("failed", 0) > 0:
                    _create_task_state["status"] = "error"
                    _create_task_state["error"] = (
                        f"Could not grant SP catalog access to `{catalog}`: "
                        + "; ".join(pre_grant.get("errors", []))
                    )
                    _persist_task_state()
                    return
            finally:
                _db_user_token.reset(pre_tok)

            if sp_id:
                sys_grant = _grant_system_via_user_sql(user_token, sp_id)
                logger.info(
                    f"System table grants (UC API): ok={sys_grant['ok']} "
                    f"applied={sys_grant['applied']} failed={sys_grant['failed']}"
                )
                if sys_grant.get("failed", 0) > 0:
                    logger.warning(
                        "Some system table grants failed — SP may lack access to "
                        "system.query.history / system.compute.clusters etc. "
                        "Errors: %s", sys_grant.get("errors", [])
                    )

            # Brief propagation pause before the SP hits the warehouse.
            _time.sleep(3)
        else:
            logger.warning(
                "No user token for pre-creation grants — SP may lack USE CATALOG. "
                "Proceeding; if table creation fails with a permission error, "
                "re-run the wizard with an authenticated browser session."
            )

        # Phase 2: Build — no user token in context; all queries run as SP.
        def _on_table_event(table_name: str, event: str) -> None:
            _create_task_state["table_progress"][table_name] = event
            if event in ("done", "error"):
                _persist_task_state()

        def _on_table_error(table_name: str, message: str) -> None:
            _create_task_state["table_errors"][table_name] = _clean_sdk_error(message)

        # Stop at the first failed table: the wizard shows the failure and offers
        # a retry, instead of spinning while every other table and the Lakebase
        # copy finish first.
        results = create_materialized_views(
            catalog, schema, on_table_event=_on_table_event,
            on_table_error=_on_table_error, stop_on_error=True,
        )
        logger.info(f"Table creation completed: {results}")
        _create_task_state["table_results"] = _table_results(results)

        all_errors = _build_errors(results)
        if all_errors:
            failed_name, first_error = next(iter(all_errors.items()))
            detail = _create_task_state["table_errors"].get(failed_name) or first_error.replace("error: ", "", 1)
            _create_task_state["status"] = "error"
            _create_task_state["error"] = (
                detail if failed_name in ("catalog", "schema") else f"Creating `{failed_name}` failed: {detail}"
            )
            _persist_task_state()
            return

        # Explicitly bootstrap all config/state Delta tables now that the SP has
        # schema-level permissions. Lazy creation would fail silently until post-setup
        # grants propagate; doing it here makes it part of the authoritative setup path.
        from backend.db import _ensure_response_cache_table
        from backend.routers.settings import (
            _ensure_permissions_table,
            _ensure_refresh_log_table,
            _ensure_schedule_table,
            _ensure_workspace_filter_table,
        )
        _config_table_fns = [
            ("app_response_cache",    _ensure_response_cache_table),
            ("app_user_permissions",  _ensure_permissions_table),
            ("app_mv_refresh_state",  _ensure_refresh_log_table),
            ("app_schedule_settings", _ensure_schedule_table),
            ("app_workspace_filter",  _ensure_workspace_filter_table),
        ]
        for _tname, _fn in _config_table_fns:
            try:
                _create_task_state["table_progress"][_tname] = "running"
                _fn()
                _create_task_state["table_progress"][_tname] = "done"
                _create_task_state["table_results"][_tname] = "ensured"
                logger.info("%s table ensured during setup", _tname)
            except Exception as _te:
                _create_task_state["table_progress"][_tname] = "error"
                _create_task_state["table_results"][_tname] = f"error: {_clean_sdk_error(str(_te))}"
                logger.warning("Could not bootstrap %s during setup: %s", _tname, _te)

        # Post-creation grants — schema now exists so schema-level privileges
        # (USE SCHEMA, CREATE TABLE, SELECT) actually land in UC.
        if user_token:
            post_tok = _db_user_token.set(user_token)
            try:
                _grant_sp_schema_access(catalog, schema)
            finally:
                _db_user_token.reset(post_tok)

        # The SP owns what it just built; without these the installer can't browse
        # or manage the app catalog. Reported to the wizard rather than failing setup.
        # Admins only (see _admin_installer_email): the grants include MANAGE.
        if grant_installer:
            try:
                _create_task_state["user_grants"] = _grant_user_catalog_visibility(
                    catalog, schema, user_token, user_email
                )
            except Exception as _ge:
                logger.warning("Installer catalog grants failed: %s", _ge)
                _create_task_state["user_grants"] = {
                    "ok": False, "grantee": user_email, "applied": [], "failed": [],
                    "error": _clean_sdk_error(str(_ge)), "manual_sql": "",
                }

        # Phase 3: Verify with retry backoff for grant propagation.
        retry_delays = [0, 10, 20, 30]
        last_verify: dict = {}
        for delay in retry_delays:
            if delay > 0:
                _time.sleep(delay)
            last_verify = _verify_built_objects_as_sp(
                catalog, schema,
                frozenset(t for t, r in _create_task_state["table_results"].items() if r.startswith("skipped")),
            )
            logger.info(
                f"SP verification (delay +{delay}s): ok={last_verify['ok']} "
                f"failed={last_verify.get('failed', [])}"
            )
            if last_verify["ok"]:
                break

        if last_verify.get("ok"):
            _create_task_state["status"] = "done"
            _create_task_state["error"] = None
        else:
            failed = last_verify.get("failed", [])
            errors_map = last_verify.get("errors", {})
            first_fail = failed[0] if failed else "unknown"
            first_err = _clean_sdk_error(errors_map.get(first_fail, "SP cannot read table"))
            _create_task_state["status"] = "error"
            _create_task_state["error"] = (
                f"SP verification failed on `{first_fail}`: {first_err}. "
                "Retry table creation in 1-2 minutes if grants were just applied."
            )
            logger.error(f"Phase 3 verification failed: {_create_task_state['error']}")
        _persist_task_state()

    except Exception as e:
        _create_task_state["status"] = "error"
        _create_task_state["error"] = _clean_sdk_error(str(e))
        _persist_task_state()
        logger.error(f"Table creation failed: {e}")


@router.post("/refresh-tables")
async def refresh_tables(
    background_tasks: BackgroundTasks,
    catalog: str = Query(default=None, description="Target catalog"),
    schema: str = Query(default=None, description="Target schema"),
    run_in_background: bool = Query(default=True, description="Run in background"),
) -> dict[str, Any]:
    """Refresh all materialized view tables with latest data.

    This rebuilds all tables from scratch with current data.
    Should be run daily to keep data fresh.
    """
    cat, sch = get_catalog_schema()
    target_catalog = catalog or cat
    target_schema = schema or sch

    if run_in_background:
        background_tasks.add_task(
            _refresh_tables_task, target_catalog, target_schema
        )
        return {
            "status": "started",
            "message": "Table refresh started in background. Check /api/setup/status for progress.",
            "catalog": target_catalog,
            "schema": target_schema,
        }
    else:
        results = refresh_materialized_views(target_catalog, target_schema)
        return {
            "status": "completed",
            "catalog": target_catalog,
            "schema": target_schema,
            "results": results,
        }


def _refresh_tables_task(catalog: str, schema: str):
    """Background task to refresh tables."""
    logger.info(f"Starting background table refresh for {catalog}.{schema}")
    _create_task_state["table_progress"] = {t: "pending" for t in _MV_TABLES}
    def _on_table_event(table_name: str, event: str) -> None:
        _create_task_state["table_progress"][table_name] = event
    try:
        results = refresh_materialized_views(catalog, schema, on_table_event=_on_table_event)
        logger.info(f"Table refresh completed: {results}")
        # Invalidate the billing module's MV availability cache so the next KPI
        # request re-detects the now-existing tables instead of serving zeros for
        # up to 5 minutes (the cache TTL).
        try:
            from backend.routers.billing import _mv_cache
            _mv_cache["available"] = None
            _mv_cache["checked_at"] = 0
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Table refresh failed: {e}")
    finally:
        # Always invalidate the Delta response cache — even on partial/failed refresh,
        # stale payloads should not continue serving when the underlying data has changed.
        try:
            from backend.db import delta_cache_invalidate
            delta_cache_invalidate()
            logger.info("Delta response cache cleared after MV refresh")
        except Exception:
            pass


# ============================================================================
# AWS CUR Setup Endpoints
# ============================================================================

@router.get("/aws-cur/status")
async def get_aws_cur_status() -> dict[str, Any]:
    """Check the status of AWS CUR integration.

    Returns information about:
    - Available external locations that might contain CUR data
    - Existing CUR tables (bronze/silver/gold)
    - Whether the system is ready for CUR setup
    """
    from backend.aws_cur_setup import check_cur_prerequisites, get_catalog_schema

    catalog, schema = get_catalog_schema()
    prerequisites = check_cur_prerequisites(catalog, schema)

    return {
        "catalog": catalog,
        "schema": schema,
        "external_locations": prerequisites["external_locations"],
        "existing_tables": prerequisites["existing_tables"],
        "tables_exist": len(prerequisites["existing_tables"]) == 3,
        "ready_for_setup": prerequisites["ready"],
        "status": "configured" if len(prerequisites["existing_tables"]) == 3 else "not_configured",
    }


@router.post("/aws-cur/create-tables")
async def create_aws_cur_tables(
    background_tasks: BackgroundTasks,
    s3_path: str = Query(default=None, description="S3 path to CUR data (e.g., s3://bucket/cur-reports/)"),
    catalog: str = Query(default=None, description="Target catalog"),
    schema: str = Query(default=None, description="Target schema"),
    load_data: bool = Query(default=False, description="Load data from S3 after creating tables"),
    run_in_background: bool = Query(default=True, description="Run in background"),
) -> dict[str, Any]:
    """Create AWS CUR medallion tables (bronze/silver/gold).

    This creates the table structure for processing AWS Cost and Usage Reports.

    Prerequisites:
    1. CUR 2.0 must be enabled in AWS Billing Console
    2. CUR data must be exported to S3 (Parquet format)
    3. Unity Catalog External Location must exist pointing to the CUR bucket
    4. Storage Credential must have read access to the S3 bucket

    Args:
        s3_path: S3 path where CUR data is stored (required if load_data=True)
        catalog: Target catalog for tables
        schema: Target schema for tables
        load_data: If True, also loads data from S3 into bronze table
        run_in_background: Run table creation in background
    """
    from backend.aws_cur_setup import create_cur_tables, get_catalog_schema

    cat, sch = get_catalog_schema()
    target_catalog = catalog or cat
    target_schema = schema or sch

    if load_data and not s3_path:
        return {
            "status": "error",
            "message": "s3_path is required when load_data=True",
        }

    if run_in_background:
        background_tasks.add_task(
            _create_cur_tables_task, target_catalog, target_schema, s3_path, load_data
        )
        return {
            "status": "started",
            "message": "AWS CUR table creation started in background. Check /api/setup/aws-cur/status for progress.",
            "catalog": target_catalog,
            "schema": target_schema,
        }
    else:
        results = create_cur_tables(target_catalog, target_schema, s3_path, load_data)
        return {
            "status": "completed",
            "catalog": target_catalog,
            "schema": target_schema,
            "results": results,
        }


def _create_cur_tables_task(catalog: str, schema: str, s3_path: str | None, load_data: bool):
    """Background task to create CUR tables."""
    from backend.aws_cur_setup import create_cur_tables

    logger.info(f"Starting background CUR table creation for {catalog}.{schema}")
    try:
        results = create_cur_tables(catalog, schema, s3_path, load_data)
        logger.info(f"CUR table creation completed: {results}")
    except Exception as e:
        logger.error(f"CUR table creation failed: {e}")


@router.post("/aws-cur/refresh")
async def refresh_aws_cur_tables(
    background_tasks: BackgroundTasks,
    s3_path: str = Query(default=None, description="S3 path to CUR data"),
    catalog: str = Query(default=None, description="Target catalog"),
    schema: str = Query(default=None, description="Target schema"),
    run_in_background: bool = Query(default=True, description="Run in background"),
) -> dict[str, Any]:
    """Refresh AWS CUR tables with latest data.

    This incrementally loads new CUR data from S3 and refreshes
    the silver and gold tables.
    """
    from backend.aws_cur_setup import refresh_cur_tables, get_catalog_schema

    cat, sch = get_catalog_schema()
    target_catalog = catalog or cat
    target_schema = schema or sch

    if run_in_background:
        background_tasks.add_task(
            _refresh_cur_tables_task, target_catalog, target_schema, s3_path
        )
        return {
            "status": "started",
            "message": "AWS CUR table refresh started in background.",
            "catalog": target_catalog,
            "schema": target_schema,
        }
    else:
        results = refresh_cur_tables(target_catalog, target_schema, s3_path)
        return {
            "status": "completed",
            "catalog": target_catalog,
            "schema": target_schema,
            "results": results,
        }


def _refresh_cur_tables_task(catalog: str, schema: str, s3_path: str | None):
    """Background task to refresh CUR tables."""
    from backend.aws_cur_setup import refresh_cur_tables

    logger.info(f"Starting background CUR table refresh for {catalog}.{schema}")
    try:
        results = refresh_cur_tables(catalog, schema, s3_path)
        logger.info(f"CUR table refresh completed: {results}")
    except Exception as e:
        logger.error(f"CUR table refresh failed: {e}")


# ============================================================================
# Bootstrap Admin (called on first-run wizard completion)
# ============================================================================


@router.post("/bootstrap-admin")
async def bootstrap_admin(request: Request) -> dict[str, Any]:
    """Save the deploying user as admin on first-run setup completion."""
    # X-Forwarded-Email is injected by Databricks Apps on Azure but may be absent on AWS.
    # Fall back to resolving identity from the forwarded OAuth token via the SDK.
    user_email = request.headers.get("X-Forwarded-Email", "")
    if not user_email:
        try:
            from backend.db import get_user_workspace_client
            import asyncio as _asyncio
            loop = _asyncio.get_running_loop()
            me = await loop.run_in_executor(
                None, lambda: get_user_workspace_client().current_user.me()
            )
            user_email = me.user_name or ""
        except Exception as e:
            logger.warning(f"Could not resolve user identity for bootstrap-admin: {e}")
    if not user_email:
        return {"status": "skipped", "reason": "no user email available"}

    try:
        from backend.routers.settings import _load_user_permissions, _save_user_permissions_to_table

        perms = _load_user_permissions()
        if user_email in perms.get("admins", []):
            return {"status": "ok", "email": user_email, "role": "admin", "note": "already admin"}

        admins = perms.get("admins", []) + [user_email]
        consumers = perms.get("consumers", [])

        _save_user_permissions_to_table(admins, consumers)
        logger.info(f"Bootstrapped admin in Delta table: {user_email}")
        return {"status": "ok", "email": user_email, "role": "admin"}

    except Exception as e:
        logger.error(f"Bootstrap admin failed: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================================
# Catalog creation (storage step — must succeed before permissions step)
# ============================================================================


def _table_results(results: dict) -> dict[str, str]:
    """Per-table outcome for the wizard: created / refreshed / skipped... / error...

    The build result also carries "catalog" and "schema" step statuses and
    "__"-prefixed metadata, which aren't tables.
    """
    return {
        k: v for k, v in results.items()
        if isinstance(v, str) and not k.startswith("__") and k not in ("catalog", "schema")
    }


async def _admin_installer_email(request: Request) -> str | None:
    """The caller's email when they may receive catalog grants, else None.

    The grants include MANAGE on the app catalog, so only app admins get them. On a
    fresh deploy with no admins configured everyone is admin (user._get_user_role),
    which keeps first-time setup working.
    """
    from backend.routers.user import _get_user_role
    email = request.headers.get("X-Forwarded-Email", "")
    try:
        role = await _get_user_role(email)
    except Exception as e:
        logger.warning("Skipping catalog grants: could not resolve the caller's role: %s", e)
        return None
    if role != "admin":
        logger.info("Skipping catalog grants for non-admin caller %s", email or "(unknown)")
        return None
    return email


def _resolve_installer_email(user_token: str, user_email: str = "") -> str:
    """The installing user's UC principal: the forwarded email, else the token's identity."""
    if user_email:
        return user_email
    host = os.getenv("DATABRICKS_HOST", "")
    if not user_token or not host:
        return ""
    try:
        from databricks.sdk import WorkspaceClient as _WC
        return _WC(host=host, token=user_token, auth_type="pat").current_user.me().user_name or ""
    except Exception as e:
        logger.warning(f"Could not resolve the installing user: {_clean_sdk_error(str(e))}")
        return ""


def _grant_user_catalog_visibility(
    catalog: str, schema: str | None, user_token: str, user_email: str = ""
) -> dict:
    """Grant the installing user USE + MANAGE on the app catalog (and schema).

    The SP creates the catalog/schema/tables, so it owns them and nobody else can
    see them until it grants access. Grants:
      - USE CATALOG, USE SCHEMA, MANAGE ON CATALOG (browse every app schema, manage grants)
      - USE SCHEMA, SELECT, MANAGE ON SCHEMA (read the tables, manage their grants)

    Uses SQL GRANT via the warehouse as the SP — NOT the UC REST API — because SP
    M2M tokens in Databricks Apps lack the unity-catalog OAuth scope required for
    direct UC API calls. Each grant runs on its own so one refusal doesn't skip the
    rest. Returns {"ok", "grantee", "applied", "failed", "manual_sql"}; manual_sql is
    what a catalog owner can run when a grant didn't land.
    """
    grantee = _resolve_installer_email(user_token, user_email)
    if not grantee or "`" in grantee:  # quoted with backticks below
        return {"ok": False, "grantee": "", "applied": [], "failed": [],
                "error": "Could not identify the signed-in user to grant catalog access to.",
                "manual_sql": ""}
    stmts = [
        f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{grantee}`",
        f"GRANT USE SCHEMA ON CATALOG `{catalog}` TO `{grantee}`",
        f"GRANT MANAGE ON CATALOG `{catalog}` TO `{grantee}`",
    ]
    if schema:
        stmts += [
            f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{grantee}`",
            f"GRANT SELECT ON SCHEMA `{catalog}`.`{schema}` TO `{grantee}`",
            f"GRANT MANAGE ON SCHEMA `{catalog}`.`{schema}` TO `{grantee}`",
        ]
    from backend.db import execute_query as _exec, _user_token as _exec_tok
    applied: list[str] = []
    failed: list[dict] = []
    ctx = _exec_tok.set("")  # clear user token → SP M2M auth (has sql scope + owns objects)
    try:
        for stmt in stmts:
            try:
                _exec(stmt, no_cache=True)
                applied.append(stmt)
            except Exception as e:
                msg = _clean_sdk_error(str(e))
                if "already" in msg.lower():
                    applied.append(stmt)
                else:
                    failed.append({"sql": stmt, "error": msg})
    finally:
        _exec_tok.reset(ctx)
    if failed:
        logger.warning(
            f"Catalog access for {grantee} on `{catalog}`: {len(applied)} granted, "
            f"{len(failed)} failed — first error: {failed[0]['error']}"
        )
    else:
        logger.info(f"Granted {grantee} USE + MANAGE on `{catalog}`" + (f".`{schema}`" if schema else ""))
    return {
        "ok": not failed,
        "grantee": grantee,
        "applied": applied,
        "failed": failed,
        "manual_sql": ";\n".join(f["sql"] for f in failed) + (";" if failed else ""),
    }


@router.post("/ensure-catalog")
async def ensure_catalog(request: Request) -> dict[str, Any]:
    """Create the target catalog if it doesn't already exist, then grant SP access.

    Flow:
    1. Try w.catalogs.create() as SP (SP becomes owner — can self-grant everything).
    2. If catalog already existed (owned by user), grant SP USE CATALOG + CREATE SCHEMA
       via SQL GRANT using the user's forwarded token (avoids unity-catalog scope issue).
    3. Verify SP can now access the catalog. If still blocked, return clear SQL to run.
    """
    import asyncio as _asyncio

    catalog, _ = get_catalog_schema()
    user_token = request.headers.get("x-forwarded-access-token", "")
    grant_to = await _admin_installer_email(request)
    sp_id = os.getenv("DATABRICKS_CLIENT_ID", "")

    if not catalog:
        raise HTTPException(status_code=400, detail="No catalog configured.")

    def _grant_sp_via_user_token() -> bool:
        """Grant SP USE CATALOG + CREATE SCHEMA using the calling user's SQL credentials.

        Uses SQL GRANT via the warehouse (NOT the UC REST API) — forwarded user
        tokens and SP M2M tokens in Databricks Apps lack the unity-catalog OAuth
        scope required for direct UC API calls, but they have the sql scope needed
        for warehouse-based GRANT statements.

        Best-effort — returns True on success, False on any failure.
        """
        if not user_token or not sp_id:
            return False
        try:
            from backend.db import execute_query as _exec, _user_token as _exec_tok
            # Run the GRANT as the calling user (catalog owner). The user token is
            # set directly in the ContextVar so get_connection() uses it for auth.
            ctx = _exec_tok.set(user_token)
            try:
                _exec(f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{sp_id}`", no_cache=True)
                _exec(f"GRANT CREATE SCHEMA ON CATALOG `{catalog}` TO `{sp_id}`", no_cache=True)
            finally:
                _exec_tok.reset(ctx)
            logger.info(f"Granted SP USE CATALOG + CREATE SCHEMA on `{catalog}` via user SQL GRANT")
            return True
        except Exception as e:
            logger.warning(f"User token SQL grant on `{catalog}` failed: {_clean_sdk_error(str(e))}")
            return False

    def _try_grant_create_catalog_via_user_token() -> bool:
        """Attempt GRANT CREATE CATALOG ON METASTORE using the calling user's token.

        Succeeds silently if the user is a metastore admin, allowing the subsequent
        w.catalogs.create() to succeed without any manual intervention.
        Best-effort — returns True on success, False if the user lacks metastore admin.
        """
        if not user_token or not sp_id:
            return False
        try:
            from backend.db import execute_query as _exec, _user_token as _exec_tok
            ctx = _exec_tok.set(user_token)
            try:
                _exec(f"GRANT CREATE CATALOG ON METASTORE TO `{sp_id}`", no_cache=True)
            finally:
                _exec_tok.reset(ctx)
            logger.info(f"Granted CREATE CATALOG ON METASTORE to SP `{sp_id}` via user token")
            return True
        except Exception as e:
            logger.debug(f"CREATE CATALOG grant via user token failed (not metastore admin?): {_clean_sdk_error(str(e))}")
            return False

    def _create():
        w = get_workspace_client()  # SP M2M — has unity-catalog scope

        # Step 1: Check whether the catalog already exists before attempting creation.
        # The SP may lack USE CATALOG on a pre-existing catalog, causing create() to
        # return a permission error rather than "already exists" — so we probe first.
        already_existed = False
        try:
            w.catalogs.get(catalog)
            logger.info(f"Catalog `{catalog}` already exists (SP can see it)")
            already_existed = True
        except Exception as e:
            msg_lower = _clean_sdk_error(str(e)).lower()
            if any(kw in msg_lower for kw in ("not found", "does not exist", "no such")):
                pass  # Catalog truly doesn't exist — proceed to create
            else:
                # SP can't see the catalog (likely exists but SP lacks USE CATALOG).
                # Try granting USE CATALOG via user token, then re-probe.
                logger.debug(f"catalogs.get(`{catalog}`) failed — catalog may exist but SP lacks access: {msg_lower}")
                _grant_sp_via_user_token()
                try:
                    w.catalogs.get(catalog)
                    logger.info(f"Catalog `{catalog}` exists — accessible after user-token grant")
                    already_existed = True
                except Exception:
                    pass  # Still not visible — will try create below

        if not already_existed:
            # Step 2: Catalog doesn't exist. Try to grant CREATE CATALOG via user token
            # first (silent success if user is metastore admin), then create as SP.
            _try_grant_create_catalog_via_user_token()
            try:
                w.catalogs.create(name=catalog)
                logger.info(f"Catalog `{catalog}` created via SDK as SP")
            except Exception as e:
                msg = _clean_sdk_error(str(e))
                lower = msg.lower()
                if any(kw in lower for kw in ("already exists", "already_exists", "already exist")):
                    logger.info(f"Catalog `{catalog}` created concurrently — treating as existing")
                    already_existed = True
                elif any(kw in lower for kw in ("permission", "privilege", "forbidden",
                                                 "unauthorized", "insufficient", "does not have")):
                    return {
                        "ok": False, "catalog": catalog,
                        "message": (
                            f"Service principal lacks CREATE CATALOG. "
                            f"Run in Databricks SQL then retry: "
                            f"GRANT CREATE CATALOG ON METASTORE TO `{sp_id}`"
                        ),
                    }
                else:
                    logger.warning(f"ensure-catalog create failed for `{catalog}`: {msg}")
                    return {"ok": False, "catalog": catalog, "message": f"Could not create catalog: {msg}"}

        # Step 3: If catalog pre-existed (or was just created by someone else), grant
        # SP access via the user's token so SP can verify and create schemas.
        if already_existed:
            _grant_sp_via_user_token()

        # Step 4: Verify SP can now access the catalog.
        try:
            w.catalogs.get(catalog)
            logger.info(f"Catalog `{catalog}` verified accessible to SP")
            if grant_to is not None:
                _grant_user_catalog_visibility(catalog, None, user_token, grant_to)
            return {"ok": True, "catalog": catalog, "message": f"Catalog `{catalog}` is ready."}
        except Exception as e:
            msg = _clean_sdk_error(str(e))
            lower = msg.lower()
            if any(kw in lower for kw in ("permission", "privilege", "does not have",
                                          "use catalog", "access", "forbidden", "unauthorized")):
                # Still blocked — user token grant likely failed (scope restriction).
                # Show the manual SQL so the user can unblock themselves.
                return {
                    "ok": False, "catalog": catalog,
                    "message": (
                        f"Catalog `{catalog}` exists but the service principal still needs access. "
                        f"Run in Databricks SQL then retry: "
                        f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{sp_id}`; "
                        f"GRANT CREATE SCHEMA ON CATALOG `{catalog}` TO `{sp_id}`"
                    ),
                }
            logger.warning(f"ensure-catalog verify failed for `{catalog}`: {msg}")
            return {"ok": False, "catalog": catalog, "message": f"Catalog `{catalog}` not found: {msg}"}

    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create)


@router.post("/ensure-schema")
async def ensure_schema(request: Request) -> dict[str, Any]:
    """Create the target schema if it doesn't already exist.

    Runs as the SP via UC SDK. The SP owns the catalog after ensure-catalog
    creates it, so CREATE SCHEMA succeeds without any extra privilege grant.
    """
    import asyncio as _asyncio

    catalog, schema = get_catalog_schema()
    user_token = request.headers.get("x-forwarded-access-token", "")
    grant_to = await _admin_installer_email(request)

    if not catalog or not schema:
        raise HTTPException(status_code=400, detail="No catalog/schema configured.")

    def _create():
        w = get_workspace_client()  # SP M2M — has unity-catalog scope
        try:
            w.schemas.create(name=schema, catalog_name=catalog)
            logger.info(f"Schema `{catalog}`.`{schema}` created via SDK as SP")
        except Exception as e:
            msg = _clean_sdk_error(str(e))
            lower = msg.lower()
            if any(kw in lower for kw in ("already exists", "already_exists", "already exist")):
                logger.info(f"Schema `{catalog}`.`{schema}` already exists — ok")
            else:
                logger.warning(f"ensure-schema SDK create failed: {msg}")
                return {"ok": False, "schema": f"{catalog}.{schema}", "message": f"Could not create schema: {msg}"}

        # Verify via SDK get(). Permission errors mean schema exists, SP just lacks
        # USE SCHEMA yet — same as catalog, grants run later.
        try:
            w.schemas.get(f"{catalog}.{schema}")
            logger.info(f"Schema `{catalog}`.`{schema}` verified")
            if grant_to is not None:
                _grant_user_catalog_visibility(catalog, schema, user_token, grant_to)
            return {"ok": True, "schema": f"{catalog}.{schema}",
                    "message": f"Schema `{catalog}.{schema}` is ready."}
        except Exception as e:
            msg = _clean_sdk_error(str(e))
            lower = msg.lower()
            if any(kw in lower for kw in ("permission", "privilege", "forbidden", "unauthorized",
                                          "does not have", "access", "insufficient")):
                logger.info(f"Schema `{catalog}`.`{schema}` exists (SP lacks USE SCHEMA, grants pending)")
                if grant_to is not None:
                    _grant_user_catalog_visibility(catalog, schema, user_token, grant_to)
                return {"ok": True, "schema": f"{catalog}.{schema}",
                        "message": f"Schema `{catalog}.{schema}` is ready."}
            logger.warning(f"ensure-schema verify failed: {msg}")
            return {"ok": False, "schema": f"{catalog}.{schema}", "message": f"Schema not found: {msg}"}

    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create)


# ============================================================================
# Catalog Permission Grant (setup wizard helper)
# ============================================================================


@router.post("/grant-catalog-access")
async def grant_catalog_access(request: Request) -> dict[str, Any]:
    """Attempt to grant the current user CREATE SCHEMA on the target catalog.

    Called from the setup wizard when table creation fails with a permission
    error. Uses the user's OAuth token — only succeeds if they are a metastore
    admin or catalog owner. Returns ok=True/False with a message so the frontend
    can either show success and let them retry, or confirm they need an admin.
    """
    import asyncio as _asyncio

    catalog, _ = get_catalog_schema()
    user_email = request.headers.get("X-Forwarded-Email", "")

    try:
        from backend.db import get_user_workspace_client
        from databricks.sdk.service.catalog import SecurableType

        loop = _asyncio.get_running_loop()

        def _do_grants():
            w = get_user_workspace_client()
            if not user_email:
                me = w.current_user.me()
                principal = me.user_name or ""
            else:
                principal = user_email

            errors = []
            for privilege, securable_type, full_name in [
                ("USE CATALOG",   SecurableType.CATALOG, catalog),
                ("CREATE SCHEMA", SecurableType.CATALOG, catalog),
            ]:
                try:
                    # Use raw REST API — w.grants.update() raises "unable to parse
                    # response" on success because the SDK fails to deserialize the
                    # minimal PATCH response body (same bug as SP grants).
                    w.api_client.do(
                        "PATCH",
                        f"/api/2.1/unity-catalog/permissions/{securable_type.value}/{full_name}",
                        body={"changes": [{"principal": principal, "add": [privilege]}]},
                    )
                except Exception as e:
                    err = str(e).lower()
                    if "already" not in err:
                        errors.append(f"{privilege}: {e}")

            return principal, errors

        principal, errors = await loop.run_in_executor(None, _do_grants)

        if errors:
            return {
                "ok": False,
                "message": f"Could not apply all grants — you may not have metastore admin rights. "
                           f"Errors: {'; '.join(errors)}",
                "sql": (
                    f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{principal}`;\n"
                    f"GRANT CREATE SCHEMA ON CATALOG `{catalog}` TO `{principal}`;"
                ),
            }

        return {
            "ok": True,
            "message": f"Granted USE CATALOG and CREATE SCHEMA on `{catalog}` to `{principal}`. "
                       f"Click Create Tables to continue.",
        }

    except Exception as e:
        catalog, _ = get_catalog_schema()
        user = user_email or "your-user@example.com"
        return {
            "ok": False,
            "message": f"Grant attempt failed: {e}",
            "sql": (
                f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{user}`;\n"
                f"GRANT CREATE SCHEMA ON CATALOG `{catalog}` TO `{user}`;"
            ),
        }


# ============================================================================
# Token Generation (for local development)
# ============================================================================


@router.post("/generate-token")
async def generate_token() -> dict[str, Any]:
    """Generate a Databricks PAT using the app's OAuth credentials.

    Useful for local development: once the app is running with OAuth,
    generate a token to use as DATABRICKS_TOKEN in a local .env file.
    """
    try:
        w = get_workspace_client()
        host = w.config.host or os.getenv("DATABRICKS_HOST", "")
        response = w.tokens.create(
            comment="cost-obs local development",
            lifetime_seconds=7776000,  # 90 days
        )
        token_value = response.token_value
        expiry = response.token_info.expiry_time if response.token_info else None
        return {
            "status": "created",
            "token": token_value,
            "host": host,
            "expiry_time": expiry,
        }
    except Exception as e:
        logger.error(f"Failed to generate token: {e}")
        return {"status": "error", "message": str(e)}



# ============================================================================
# Readiness Checks
# ============================================================================

import re as _re
import threading
import time
from concurrent.futures import Future as _Future
from dataclasses import dataclass
from enum import Enum


class CheckStatus(str, Enum):
    HEALTHY = "healthy"
    NOT_CONFIGURED = "not_configured"
    TIMEOUT_STARTING = "timeout_starting"
    PERMISSION_DENIED = "permission_denied"
    INTERNAL_ERROR = "internal_error"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class WarehouseCheckResult:
    status: CheckStatus
    ok: bool
    message: str
    warehouse_id: str | None = None
    source: str | None = None


@dataclass
class TimedWarehouseCheckCache:
    result: WarehouseCheckResult
    expires_at: float

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


_CORE_TABLES = {"system.billing.usage", "system.billing.list_prices"}

_wh_check_lock = threading.Lock()
_wh_check_inflight: _Future | None = None
_wh_check_cache: TimedWarehouseCheckCache | None = None

# Short TTLs for transient states prevent repeated slow callers from pile-up.
_WH_TTL_BY_STATUS: dict[CheckStatus, int] = {
    CheckStatus.HEALTHY: 300,
    CheckStatus.PERMISSION_DENIED: 300,
    CheckStatus.NOT_CONFIGURED: 3600,
    CheckStatus.TIMEOUT_STARTING: 15,
    CheckStatus.INTERNAL_ERROR: 5,
    CheckStatus.UNAVAILABLE: 15,
}
_WH_CHECK_TIMEOUT = 30  # seconds the caller blocks waiting for a warehouse response

# Table checks run as SP — result doesn't vary by user, 5-min cache is safe.
_table_readiness_cache: dict[str, Any] | None = None
_table_readiness_cache_ts: float = 0.0
_TABLE_CACHE_TTL = 300


def _resolve_warehouse_config() -> tuple[str, str]:
    """Return (source, warehouse_id). Source is 'app_resource', 'http_path', or 'none'."""
    wh_env_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    if wh_env_id:
        return "app_resource", wh_env_id
    if http_path and http_path.lower() != "auto":
        parts = http_path.strip("/").split("/")
        wh_id = parts[-1] if parts and parts[-1] != "warehouses" else ""
        return "http_path", wh_id
    return "none", ""


def _run_blocking_warehouse_check() -> WarehouseCheckResult:
    """Execute a test query as the SP. Runs inside a daemon thread.

    Classifies exceptions into typed CheckStatus so callers can apply the
    right cache TTL and surface a useful error message.
    Guards against misconfigured callers: returns NOT_CONFIGURED immediately
    if no warehouse is resolvable, instead of letting execute_query fail
    with an unhelpful connection error.
    """
    from backend.db import execute_query, _user_token
    source, warehouse_id = _resolve_warehouse_config()
    if source == "none":
        return WarehouseCheckResult(
            status=CheckStatus.NOT_CONFIGURED,
            ok=False,
            message="No warehouse configured",
            source="none",
        )
    tok = _user_token.set("")
    try:
        execute_query("SELECT current_user()", no_cache=True)
        return WarehouseCheckResult(
            status=CheckStatus.HEALTHY,
            ok=True,
            message="",
            warehouse_id=warehouse_id,
            source=source,
        )
    except Exception as exc:
        msg = str(exc)
        logger.error("Warehouse readiness check failed: %s", msg, exc_info=True)
        lower = msg.lower()
        if any(kw in lower for kw in ("permission", "denied", "unauthorized", "forbidden", "privilege")):
            status = CheckStatus.PERMISSION_DENIED
        elif any(kw in lower for kw in ("timeout", "timed out", "starting", "unavailable", "connection")):
            status = CheckStatus.TIMEOUT_STARTING
        else:
            status = CheckStatus.INTERNAL_ERROR
        return WarehouseCheckResult(status=status, ok=False, message=msg, warehouse_id=warehouse_id, source=source)
    finally:
        _user_token.reset(tok)


def _cache_ttl_for_warehouse(result: WarehouseCheckResult) -> int:
    return _WH_TTL_BY_STATUS.get(result.status, 15)


def _get_cached_warehouse_check() -> WarehouseCheckResult | None:
    with _wh_check_lock:
        if _wh_check_cache is not None and _wh_check_cache.is_valid():
            return _wh_check_cache.result
        return None


def _set_cached_warehouse_check(result: WarehouseCheckResult) -> None:
    global _wh_check_cache
    ttl = _cache_ttl_for_warehouse(result)
    with _wh_check_lock:
        _wh_check_cache = TimedWarehouseCheckCache(
            result=result,
            expires_at=time.monotonic() + ttl,
        )


def _get_or_start_warehouse_check_future() -> _Future:
    """Single-flight: reuse in-flight future if one is already running."""
    global _wh_check_inflight
    with _wh_check_lock:
        if _wh_check_inflight is not None and not _wh_check_inflight.done():
            return _wh_check_inflight
        future: _Future = _Future()

        def _run(f=future):
            try:
                f.set_result(_run_blocking_warehouse_check())
            except Exception as exc:
                f.set_exception(exc)

        threading.Thread(target=_run, daemon=True, name="wh-check").start()
        _wh_check_inflight = future
        return future


def check_warehouse_readiness() -> WarehouseCheckResult:
    """Return a WarehouseCheckResult via cache → single-flight → timeout.

    NOT_CONFIGURED flows through the same cache path as other statuses so that
    the 3600s TTL is respected instead of re-reading env vars on every request.
    """
    from concurrent.futures import TimeoutError as _FutureTimeout
    cached = _get_cached_warehouse_check()
    if cached is not None:
        return cached
    future = _get_or_start_warehouse_check_future()
    try:
        result = future.result(timeout=_WH_CHECK_TIMEOUT)
    except _FutureTimeout:
        logger.warning("Warehouse readiness timed out after %ds (may be starting up)", _WH_CHECK_TIMEOUT)
        result = WarehouseCheckResult(
            status=CheckStatus.TIMEOUT_STARTING,
            ok=False,
            message="Warehouse check timed out — warehouse may be starting up. Try re-checking once running.",
        )
    except Exception as exc:
        logger.error("Warehouse check future raised: %s", exc, exc_info=True)
        result = WarehouseCheckResult(
            status=CheckStatus.INTERNAL_ERROR,
            ok=False,
            message=str(exc),
        )
    _set_cached_warehouse_check(result)
    return result


def _uc_identifier(part: str) -> str:
    """Quote a single UC identifier part only when it requires backtick quoting."""
    if _re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", part):
        return part
    escaped = part.replace("`", "``")
    return f"`{escaped}`"


def _build_fix_sql(table: str, sp_client_id: str) -> str:
    """Return GRANT statements to fix SP access. Uses per-part identifier quoting."""
    if not sp_client_id:
        return ""
    parts = table.split(".")
    if len(parts) == 3:
        q0, q1, q2 = _uc_identifier(parts[0]), _uc_identifier(parts[1]), _uc_identifier(parts[2])
        if parts[0] == "system":
            # System tables are read-only — CREATE TABLE and MODIFY grants fail.
            if table == "system.access.audit":
                # Requires account-level enablement before metastore admin can grant.
                return (
                    f"-- Step 1: Account admin enables the access schema in Account Console\n"
                    f"-- (Account Console → System Tables → enable 'access' schema)\n"
                    f"--\n"
                    f"-- Step 2: Metastore admin runs:\n"
                    f"GRANT USE CATALOG ON CATALOG {q0} TO `{sp_client_id}`;\n"
                    f"GRANT USE SCHEMA ON SCHEMA {q0}.{q1} TO `{sp_client_id}`;\n"
                    f"GRANT SELECT ON TABLE {q0}.{q1}.{q2} TO `{sp_client_id}`;"
                )
            return (
                f"GRANT USE CATALOG ON CATALOG {q0} TO `{sp_client_id}`;\n"
                f"GRANT USE SCHEMA ON SCHEMA {q0}.{q1} TO `{sp_client_id}`;\n"
                f"GRANT SELECT ON TABLE {q0}.{q1}.{q2} TO `{sp_client_id}`;"
            )
        return (
            f"GRANT USE CATALOG ON CATALOG {q0} TO `{sp_client_id}`;\n"
            f"GRANT USE SCHEMA ON SCHEMA {q0}.{q1} TO `{sp_client_id}`;\n"
            f"GRANT CREATE TABLE ON SCHEMA {q0}.{q1} TO `{sp_client_id}`;\n"
            f"GRANT MODIFY ON SCHEMA {q0}.{q1} TO `{sp_client_id}`;\n"
            f"GRANT SELECT ON TABLE {q0}.{q1}.{q2} TO `{sp_client_id}`;"
        )
    q_table = ".".join(_uc_identifier(p) for p in parts)
    return f"GRANT SELECT ON TABLE {q_table} TO `{sp_client_id}`;"


def _check_table_as_sp(table: str) -> tuple[bool, str]:
    """Run check_table_access with the user token cleared (forces SP auth)."""
    from backend.routers.permissions import check_table_access
    from backend.db import _user_token
    tok = _user_token.set("")
    try:
        return check_table_access(table)
    finally:
        _user_token.reset(tok)


def _safe_table_check_result(table_name: str, future: _Future) -> tuple[bool, str, CheckStatus]:
    """Resolve a table check future and return (ok, error_msg, status_str).

    Logs full tracebacks for internal errors so they don't disappear silently.
    """
    try:
        ok, msg = future.result()
        if ok:
            return True, "", CheckStatus.HEALTHY
        lower = msg.lower()
        if any(kw in lower for kw in ("permission", "denied", "privilege", "unauthorized", "forbidden")):
            return False, msg, CheckStatus.PERMISSION_DENIED
        # Unity Catalog returns "table/view not found" or "does not exist" instead of a
        # permission error when the SP lacks SELECT on a system table — it hides the table
        # from the SP's metadata view entirely. Treat this as a missing grant, not an
        # internal error, so the Fix button is shown and the cache TTL is appropriate.
        if table_name.startswith("system.") and any(
            kw in lower for kw in ("does not exist", "not found", "table or view not found", "no such")
        ):
            if table_name == "system.access.audit":
                clear_msg = (
                    "Audit log schema not enabled — an account admin must enable "
                    "System Tables > access schema in the Databricks Account Console, "
                    "then a metastore admin can run the GRANT below"
                )
            else:
                clear_msg = (
                    f"Missing SELECT grant — Unity Catalog hides `{table_name}` from the SP "
                    f"until a metastore admin runs the GRANT below"
                )
            return False, clear_msg, CheckStatus.PERMISSION_DENIED
        return False, msg, CheckStatus.INTERNAL_ERROR
    except Exception as exc:
        logger.error("Table check future for %s raised: %s", table_name, exc, exc_info=True)
        return False, str(exc), CheckStatus.INTERNAL_ERROR


def _build_warehouse_item(
    wh_result: WarehouseCheckResult, warehouse_id: str, sp_client_id: str
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": "SQL Warehouse",
        "description": "Service principal can execute queries on the configured warehouse",
        "category": "core",
        "source": wh_result.source or "none",
        "granted": wh_result.ok,
    }
    if not wh_result.ok:
        item["error"] = wh_result.message
        if sp_client_id and warehouse_id:
            item["fix_sql"] = f"GRANT CAN_USE ON WAREHOUSE `{warehouse_id}` TO `{sp_client_id}`;"
    return item


def _calc_overall(
    core_tables_pass: bool,
    warehouse_pass: bool,
    enhanced_all_pass: bool,
    core_checks: list[dict],
) -> str:
    if core_tables_pass and warehouse_pass and enhanced_all_pass:
        return "ready"
    if core_tables_pass and warehouse_pass:
        return "core_ready"
    if core_tables_pass or any(c["granted"] for c in core_checks):
        return "needs_action"
    return "not_ready"


def _check_readiness_sync(bypass_cache: bool = False) -> dict[str, Any]:
    """Run all readiness checks as the SP and return a categorized result."""
    from concurrent.futures import as_completed
    from backend.routers.permissions import REQUIRED_PERMISSIONS

    global _table_readiness_cache, _table_readiness_cache_ts

    if bypass_cache:
        reset_readiness_caches()

    sp_client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
    _, warehouse_id = _resolve_warehouse_config()

    # Snapshot the table cache reference once — prevents TOCTOU if a concurrent
    # bypass_cache=True request calls reset_readiness_caches() between the
    # is-None check and the subsequent ["core"] / ["enhanced"] reads.
    cached_tables = _table_readiness_cache
    table_cache_valid = (
        cached_tables is not None
        and (time.monotonic() - _table_readiness_cache_ts) < _TABLE_CACHE_TTL
    )
    wh_cached = _get_cached_warehouse_check()

    # Both caches fresh — merge and return immediately without any blocking calls.
    if table_cache_valid and wh_cached is not None:
        wh_item = _build_warehouse_item(wh_cached, warehouse_id, sp_client_id)
        overall = _calc_overall(
            all(c["granted"] for c in cached_tables["core"]),  # type: ignore[index]
            wh_item["granted"],
            all(c["granted"] for c in cached_tables["enhanced"]),  # type: ignore[index]
            cached_tables["core"],  # type: ignore[index]
        )
        return {
            "overall": overall,
            "warehouse": wh_item,
            "core": cached_tables["core"],  # type: ignore[index]
            "enhanced": cached_tables["enhanced"],  # type: ignore[index]
            "sp_client_id": sp_client_id,
        }

    # Kick off the warehouse future before table checks so both run concurrently.
    source, _ = _resolve_warehouse_config()
    if source != "none" and wh_cached is None:
        _get_or_start_warehouse_check_future()

    if not table_cache_valid:
        with ThreadPoolExecutor(max_workers=len(REQUIRED_PERMISSIONS)) as pool:
            table_futures: dict = {
                pool.submit(_check_table_as_sp, perm["table"]): perm
                for perm in REQUIRED_PERMISSIONS
            }
            access_results: dict[str, tuple[bool, str, str]] = {}
            for future in as_completed(table_futures):
                perm = table_futures[future]
                ok, msg, status = _safe_table_check_result(perm["table"], future)
                access_results[perm["table"]] = (ok, msg, status)

        core_checks: list[dict] = []
        enhanced_checks: list[dict] = []
        for perm in REQUIRED_PERMISSIONS:
            ok, msg, _ = access_results[perm["table"]]
            is_core = perm["table"] in _CORE_TABLES
            check: dict[str, Any] = {
                "table": perm["table"],
                "name": perm["name"],
                "description": perm["description"],
                "required": perm["required"],
                "granted": ok,
                "category": "core" if is_core else "enhanced",
            }
            if not ok:
                check["fix_sql"] = _build_fix_sql(perm["table"], sp_client_id)
                if msg:
                    check["error"] = msg
            if is_core:
                core_checks.append(check)
            else:
                enhanced_checks.append(check)

        _table_readiness_cache = {
            "core": core_checks,
            "enhanced": enhanced_checks,
            "sp_client_id": sp_client_id,
        }
        _table_readiness_cache_ts = time.monotonic()
    else:
        # cached_tables snapshot taken above — safe from concurrent reset
        core_checks = cached_tables["core"]  # type: ignore[index]
        enhanced_checks = cached_tables["enhanced"]  # type: ignore[index]

    # Collect warehouse result — reuses the in-flight future started above.
    wh_result = check_warehouse_readiness()
    wh_item = _build_warehouse_item(wh_result, warehouse_id, sp_client_id)
    overall = _calc_overall(
        all(c["granted"] for c in core_checks),
        wh_item["granted"],
        all(c["granted"] for c in enhanced_checks),
        core_checks,
    )
    return {
        "overall": overall,
        "warehouse": wh_item,
        "core": core_checks,
        "enhanced": enhanced_checks,
        "sp_client_id": sp_client_id,
    }


def shutdown_readiness_executor() -> None:
    """Clear the in-flight warehouse check on shutdown.

    Previously shut down a ThreadPoolExecutor; now a no-op for the executor
    (warehouse check uses a daemon thread) but still clears the future reference.
    """
    global _wh_check_inflight
    with _wh_check_lock:
        _wh_check_inflight = None
    logger.info("Readiness executor shut down (daemon thread)")


def reset_readiness_caches() -> None:
    """Clear all readiness caches. Used in tests and forced re-check."""
    global _table_readiness_cache, _table_readiness_cache_ts, _wh_check_cache, _wh_check_inflight
    with _wh_check_lock:
        _table_readiness_cache = None
        _table_readiness_cache_ts = 0.0
        _wh_check_cache = None
        _wh_check_inflight = None


@router.get("/readiness")
async def get_readiness(refresh: bool = False) -> dict[str, Any]:
    """Structured readiness report for the setup wizard and Settings → Permissions.

    All checks run as the service principal regardless of auth_mode or request
    headers.  Cached with status-appropriate TTLs.  Pass ?refresh=true to force
    a live re-check.
    """
    import asyncio as _asyncio
    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _check_readiness_sync(bypass_cache=refresh),
    )


@router.get("/list-workspaces")
async def list_workspaces() -> dict:
    """Return all workspaces in the account for the setup wizard workspace picker.

    Queries the app's own daily_workspace_breakdown table rather than
    system.access.workspaces_latest, which requires a separate schema grant the SP
    may not hold. That MV is often still building when the wizard reaches this step
    (Create Tables can be skipped and keeps running in the background), so an empty
    or missing MV falls back to the top-nav filter's list without the pool applied.
    """
    from backend.db import execute_query, get_catalog_schema
    try:
        catalog, schema = get_catalog_schema()
        rows = await asyncio.to_thread(execute_query, f"""
            SELECT
                CAST(workspace_id AS STRING) AS workspace_id,
                MAX(workspace_name) AS workspace_name
            FROM `{catalog}`.`{schema}`.daily_workspace_breakdown
            WHERE workspace_id IS NOT NULL
            GROUP BY workspace_id
            ORDER BY workspace_name NULLS LAST
        """, no_cache=True)
        workspaces = []
        for r in (rows or []):
            wid = r.get("workspace_id")
            if not wid:
                continue
            raw_name = (r.get("workspace_name") or "").strip()
            # A workspace with no real display name in billing history is one that
            # no longer exists in the account (deleted) — its name column was never
            # populated or was dropped. Flag it "historical" so the wizard can label
            # it instead of showing a bare workspace id that looks like a live one.
            historical = (not raw_name) or (raw_name == wid)
            workspaces.append({
                "id": wid,
                "name": raw_name or wid,
                "historical": historical,
            })
        if workspaces:
            return {"workspaces": workspaces}
        logger.info("list-workspaces: daily_workspace_breakdown empty — using the top-nav workspace list")
    except Exception as e:
        logger.warning("list-workspaces: daily_workspace_breakdown unavailable (%s) — using the top-nav workspace list", e)

    from datetime import date, timedelta
    from backend.routers.billing import workspace_list
    # Unscoped: the picker must offer workspaces outside the current pool.
    data = await workspace_list(
        (date.today() - timedelta(days=365)).isoformat(),
        date.today().isoformat(),
        apply_pool=False,
    )
    workspaces = [
        {"id": w["id"], "name": w.get("name") or w["id"], "historical": bool(w.get("historical"))}
        for w in data.get("workspaces", [])
    ]
    out: dict = {"workspaces": workspaces}
    if data.get("error"):
        out["error"] = data["error"]
    return out


@router.get("/list-catalogs")
async def list_catalogs() -> dict:
    """Catalogs the CURRENT USER can see, for the storage-step Browse picker.

    Uses the user's OAuth token (get_user_workspace_client) so the list reflects
    what they actually have access to — picking from it avoids typos, and typing a
    name the list does not contain is the signal that a brand-new catalog/schema is
    intended. Never throws: a failure returns an empty list plus an error string so
    the field stays usable (the user can always type a name in by hand).
    """
    from backend.db import get_user_workspace_client

    def _list() -> list[str]:
        w = get_user_workspace_client()
        return [c.name for c in w.catalogs.list() if getattr(c, "name", None)]

    try:
        names = await asyncio.to_thread(_list)
        # Hide catalogs the app can never use as its storage location.
        hidden = {"system", "__databricks_internal", "samples"}
        return {"catalogs": sorted(n for n in names if n and n.lower() not in hidden)}
    except Exception as e:
        logger.warning("list-catalogs failed: %s", e)
        return {"catalogs": [], "error": _clean_sdk_error(str(e))}


@router.get("/list-schemas")
async def list_schemas(
    catalog: str = Query(..., description="Catalog to list schemas from"),
) -> dict:
    """Schemas in `catalog` the current user can see, for the Browse picker."""
    from backend.db import get_user_workspace_client

    cat = (catalog or "").strip()
    if not cat:
        return {"catalog": "", "schemas": []}

    def _list() -> list[str]:
        w = get_user_workspace_client()
        return [s.name for s in w.schemas.list(catalog_name=cat) if getattr(s, "name", None)]

    try:
        names = await asyncio.to_thread(_list)
        return {"catalog": cat, "schemas": sorted(n for n in names if n and n.lower() != "information_schema")}
    except Exception as e:
        logger.warning("list-schemas failed for %s: %s", cat, e)
        return {"catalog": cat, "schemas": [], "error": _clean_sdk_error(str(e))}


@router.get("/preflight-catalog")
async def preflight_catalog_endpoint(request: Request) -> dict:
    """Check the configured catalog exists before table creation.

    Uses SP credentials + SHOW CATALOGS SQL so we avoid the unity-catalog OAuth
    scope requirement (forwarded user tokens from Databricks Apps don't carry it).
    Distinguishes "catalog missing" from "SP has no USE privilege" by error text.
    """
    import asyncio as _asyncio

    catalog, schema = get_catalog_schema()

    def _check():
        if not catalog:
            return {"ok": False, "status": "invalid_config",
                    "message": "No catalog configured — return to the Storage step."}
        try:
            w = get_workspace_client()
            w.catalogs.get(catalog)
            return {"ok": True, "status": "ready", "message": f"Catalog `{catalog}` is accessible."}
        except Exception as e:
            msg = _clean_sdk_error(str(e))
            msg_lower = msg.lower()
            # "not found" / "does not exist" / CATALOG_NOT_FOUND → catalog genuinely absent
            if any(kw in msg_lower for kw in ("not found", "does not exist", "404", "catalog_not_found")):
                return {
                    "ok": False,
                    "status": "catalog_missing",
                    "message": f"Catalog `{catalog}` does not exist. Create it in Unity Catalog before continuing.",
                }
            # SP has no USE CATALOG yet — grants haven't landed or weren't applied.
            # Treat this as OK for preflight; the pre-creation grant in _create_tables_task
            # will apply USE CATALOG before create_materialized_views runs.
            if any(kw in msg_lower for kw in ("permission", "privilege", "unauthorized", "forbidden", "403", "insufficient")):
                return {"ok": True, "status": "ready",
                        "message": f"Catalog `{catalog}` exists (SP permissions will be applied at build time)."}
            return {
                "ok": False,
                "status": "catalog_check_failed",
                "message": f"Could not verify catalog `{catalog}`: {msg}",
            }

    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(None, _check)


def _workspace_filter_table() -> str:
    from backend.routers.settings import _config_table
    return _config_table("app_workspace_filter")


def _parse_saved_workspace_ids(row: dict) -> list[str]:
    """Read either app_workspace_filter schema.

    Create Tables (settings._ensure_workspace_filter_table) creates
    (workspace_ids_json, updated_at); deployments that saved before that step ran
    have the older (workspace_ids) comma-separated column.
    """
    from backend.workspace_filter import _is_safe_id
    raw_json = row.get("workspace_ids_json")
    if raw_json:
        try:
            ids = json.loads(raw_json)
        except (TypeError, ValueError):
            ids = []
        if not isinstance(ids, list):
            ids = []
    else:
        ids = str(row.get("workspace_ids") or "").split(",")
    return [s for s in (str(i).strip() for i in ids) if _is_safe_id(s)]


def _read_saved_workspace_ids() -> list[str]:
    """The workspace pool saved in Delta. [] means all workspaces.

    Returns [] when the table doesn't exist yet; raises on any other error.
    """
    from backend.db import execute_query
    from backend.routers.settings import _table_missing
    try:
        rows = execute_query(f"SELECT * FROM {_workspace_filter_table()} LIMIT 1", no_cache=True)
    except Exception as e:
        if _table_missing(e):
            return []
        raise
    return _parse_saved_workspace_ids(rows[0]) if rows else []


def _write_saved_workspace_ids(ids: list[str]) -> None:
    """Replace the saved pool. An empty list clears it (all workspaces).

    Each case is a single statement, so a failed write leaves the previous pool intact
    and concurrent saves can't leave more than one row.
    """
    from backend.db import execute_query, execute_write
    from backend.routers.settings import _ensure_workspace_filter_table
    _ensure_workspace_filter_table()
    table = _workspace_filter_table()
    if not ids:
        execute_write(f"DELETE FROM {table}")
        return
    cols = {str(r.get("col_name")) for r in execute_query(f"DESCRIBE TABLE {table}", no_cache=True)}
    if "workspace_ids_json" in cols:
        execute_write(
            f"INSERT OVERWRITE {table} (workspace_ids_json, updated_at) VALUES (:v, current_timestamp())",
            {"v": json.dumps(ids)},
        )
    else:
        execute_write(f"INSERT OVERWRITE {table} (workspace_ids) VALUES (:v)", {"v": ",".join(ids)})


@router.get("/workspace-filter")
async def get_workspace_filter() -> dict:
    """Return the saved workspace pool. [] means all workspaces.

    503 when the pool can't be read: the wizard preselects from this, and treating
    an unreadable pool as "all workspaces" would let a save widen it silently.
    """
    settings_path = os.path.join(SETTINGS_DIR, "workspace_filter.json")
    import re as _re
    try:
        with open(settings_path) as f:
            data = json.load(f)
        ids = [str(i) for i in data.get("workspace_ids", []) if _re.match(r'^[a-zA-Z0-9_\-\.]+$', str(i))]
        return {"workspace_ids": ids}
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # The file is missing until startup restores it from Delta (it's wiped on deploy).
    try:
        return {"workspace_ids": await asyncio.to_thread(_read_saved_workspace_ids)}
    except Exception as e:
        logger.warning("workspace-filter: could not read saved pool: %s", e)
        raise HTTPException(status_code=503, detail=_pool_read_error_detail(e))


def _pool_read_error_detail(e: Exception) -> str:
    """What the wizard shows when the saved pool can't be read.

    Access problems don't clear on retry (e.g. a redeploy's new SP has no grant on
    the table yet), so name the table and the grant instead of "try again".
    """
    msg = _clean_sdk_error(str(e))
    lower = msg.lower()
    if any(k in lower for k in ("permission", "insufficient", "does not have", "not authorized", "schema_not_found")):
        sp = os.getenv("DATABRICKS_CLIENT_ID", "") or "<app service principal>"
        table = _workspace_filter_table()
        return (
            f"The app can't read its saved workspace scope in {table}: {msg} "
            f"A catalog owner can fix this with: GRANT SELECT, MODIFY ON TABLE {table} TO `{sp}`; then Retry."
        )
    return f"Could not read the saved workspace scope: {msg} Try again in a moment."


def restore_workspace_filter_from_delta() -> None:
    """Restore workspace filter from Delta to the settings file on startup.

    The .settings/ directory is ephemeral in Databricks Apps — wiped on every
    git deploy. This function reads the last-saved filter from the Delta table
    and writes it back to the file so workspace_filter.py can read it normally.
    Safe to call when the table doesn't exist yet (first deploy).
    """
    settings_path = os.path.join(SETTINGS_DIR, "workspace_filter.json")
    if os.path.exists(settings_path):
        return
    try:
        ids = _read_saved_workspace_ids()
        if ids:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(settings_path, "w") as f:
                json.dump({"workspace_ids": ids}, f)
            logger.info("Workspace filter restored from Delta: %d workspace(s)", len(ids))
        else:
            logger.info("No workspace filter saved in Delta — showing all workspaces")
    except Exception as e:
        logger.warning("Workspace filter restore from Delta failed (non-fatal): %s", e)


@router.post("/save-workspace-filter")
async def save_workspace_filter(request: Request) -> dict:
    """Persist selected workspace IDs to .settings/workspace_filter.json and Delta. Admin only.

    An empty list means all workspaces and clears the saved pool. Admins can change
    the pool at any time; every save replaces the previous one.
    """
    import time as _time
    import re as _re
    t0 = _time.monotonic()

    from backend.routers.user import _get_user_role
    user_email = request.headers.get("X-Forwarded-Email", os.getenv("USER", ""))
    logger.info("save-workspace-filter: request from %s", user_email or "(unknown)")

    role = await _get_user_role(user_email)
    logger.info("save-workspace-filter: user role=%s (%.1fms)", role, (_time.monotonic() - t0) * 1000)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required to modify the workspace filter pool")

    body = await request.json()
    raw_ids: list = body.get("workspace_ids", [])
    valid_ids = [str(i) for i in raw_ids if _re.match(r'^[a-zA-Z0-9_\-\.]+$', str(i))]
    logger.info("save-workspace-filter: validated %d/%d ids (%.1fms)", len(valid_ids), len(raw_ids), (_time.monotonic() - t0) * 1000)

    # Write to file (fast path for running workers)
    settings_path = os.path.join(SETTINGS_DIR, "workspace_filter.json")
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump({"workspace_ids": valid_ids}, f)
        elapsed_ms = (_time.monotonic() - t0) * 1000
        logger.info("save-workspace-filter: wrote %s in %.1fms — ids=%s", settings_path, elapsed_ms, valid_ids)
    except Exception as e:
        elapsed_ms = (_time.monotonic() - t0) * 1000
        logger.error("save-workspace-filter: write failed after %.1fms — path=%s error=%s", elapsed_ms, settings_path, e)
        raise HTTPException(status_code=500, detail=f"Failed to persist workspace filter: {e}")

    # Write to Delta (survives redeploys — restored to file on next startup)
    try:
        await asyncio.to_thread(_write_saved_workspace_ids, valid_ids)
        logger.info("save-workspace-filter: persisted to Delta in %.1fms", (_time.monotonic() - t0) * 1000)
    except Exception as e:
        logger.warning("save-workspace-filter: Delta write failed (non-fatal — file is primary): %s", e)

    # Cache keys carry the pool (workspace_filter.pool_cache_tag), so other workers
    # stop serving the old scope on their own; this drops this worker's entries and
    # the shared response cache right away.
    try:
        from backend.db import clear_query_cache, delta_cache_invalidate
        clear_query_cache()
        await asyncio.to_thread(delta_cache_invalidate)
    except Exception as e:
        logger.warning("save-workspace-filter: cache clear failed (non-fatal): %s", e)

    return {"saved": valid_ids}


def _record_drop_in_refresh_log() -> None:
    """Append a 'dropped' entry to mv_refresh_log.json and the Delta refresh log table."""
    import json as _json
    from datetime import datetime as _dt
    _log_path = os.path.join(os.path.dirname(__file__), "..", "..", ".settings", "mv_refresh_log.json")
    drop_entry = {
        "timestamp": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "dropped",
        "duration_seconds": 0,
        "lookback_days": 0,
        "trigger": "manual",
    }
    try:
        with open(_log_path) as f:
            log = _json.load(f)
    except (FileNotFoundError, OSError, _json.JSONDecodeError):
        log = {}
    history = log.get("refresh_history", [])
    history.append(drop_entry)
    log["refresh_history"] = history[-10:]
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)
    with open(_log_path, "w") as f:
        _json.dump(log, f)
    try:
        from backend.routers.settings import save_refresh_log_to_delta
        save_refresh_log_to_delta(log)
    except Exception:
        pass
    # Invalidate the tables status cache so the next poll reflects empty tables
    try:
        import backend.routers.settings as _settings_mod
        _settings_mod._tables_cache = None
        _settings_mod._tables_cache_ts = 0.0
    except Exception:
        pass
    # Invalidate debug cache so next diagnostics run re-checks
    try:
        import backend.routers.debug as _debug_mod
        _debug_mod._debug_cache = None
        _debug_mod._debug_cache_ts = 0.0
    except Exception:
        pass


@router.delete("/drop-materialized-views")
async def drop_mvs() -> dict:
    """Drop all app-managed materialized view tables. Irreversible — use with caution."""
    try:
        from backend.db import get_catalog_schema
        catalog, schema = get_catalog_schema()
        results = drop_materialized_views(catalog, schema)
        all_dropped = all(v == "dropped" for v in results.values())
        _record_drop_in_refresh_log()
        return {"ok": all_dropped, "results": results, "catalog": catalog, "schema": schema}
    except Exception as e:
        logger.exception("drop_mvs: unhandled exception")
        return {"ok": False, "results": {}, "error": str(e)}


@router.get("/mv-overrides")
async def get_mv_overrides() -> dict:
    """Return current MV table name overrides and the default table names."""
    from backend.db import get_mv_table_overrides, get_catalog_schema
    catalog, schema = get_catalog_schema()
    overrides = get_mv_table_overrides()
    tables = []
    for name in _MV_TABLES:
        default_path = f"{catalog}.{schema}.{name}"
        tables.append({
            "logical_name": name,
            "default_path": default_path,
            "override_path": overrides.get(name, ""),
            "is_overridden": name in overrides,
        })
    return {"tables": tables, "overrides": overrides}


@router.post("/mv-overrides")
async def save_mv_overrides(request: Request) -> dict:
    """Persist MV table name overrides. Send {} or omit a key to clear an override."""
    from backend.db import save_mv_table_overrides, get_mv_table_overrides
    body = await request.json()
    overrides_raw: dict = body.get("overrides", {})
    # Only keep non-empty string values for known logical names
    valid = {
        k: v.strip()
        for k, v in overrides_raw.items()
        if k in _MV_TABLES and isinstance(v, str) and v.strip()
    }
    save_mv_table_overrides(valid)
    return {"ok": True, "saved": valid, "cleared": [k for k in _MV_TABLES if k not in valid]}
