"""Cost Observability & Control (COC) - FastAPI Application"""

import asyncio
import concurrent.futures
import concurrent.futures.thread as _fut_thread
import logging
import os
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend.routers import (
    ai_gateway,
    aiml,
    apps,
    aws_actual,
    azure_actual,
    billing,
    conversational,
    dbsql,
    debug,
    gcp_actual,
    genie_cost,
    health,
    permissions,
    settings,
    setup,
    tagging,
    user,
    users_groups,
    warehouse_health,
)

# Budget router is internal-only (excluded from external mirror). Import
# defensively so builds without budget.py still start up.
try:
    from backend.routers import budget  # type: ignore
    _has_budget = True
except ImportError:
    budget = None  # type: ignore
    _has_budget = False

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


class UserAuthMiddleware:
    """Propagate x-forwarded-access-token into the db layer's ContextVar.

    When Databricks Apps user authorization (Public Preview) is enabled, the
    platform injects the end-user's OAuth token via this header on every request.
    We store it in a ContextVar so get_connection() can use it instead of the SP
    token, giving the user their own UC identity for all SQL queries.

    If the header is absent the ContextVar stays at its default (""), and
    get_connection() falls back to the service-principal path as before.

    Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) because
    BaseHTTPMiddleware runs call_next in a separate task context, which breaks
    ContextVar propagation to downstream request handlers.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from backend.db import _auth_mode, _user_token, reset_source_labels, set_source_labels
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            raw_token = headers.get(b"x-forwarded-access-token", b"").decode()
            # If auth mode is locked to SP, never use the user token — every query
            # in every request uses the service principal identity consistently.
            token = "" if _auth_mode == "sp" else raw_token
            ctx_token = _user_token.set(token)
            # Propagate the MV source-label selection (?source_labels=a,b) so MV
            # reads can be narrowed to the chosen sources. Absent/blank = all.
            sl_token = None
            try:
                from urllib.parse import parse_qs
                qs = parse_qs(scope.get("query_string", b"").decode())
                # Repeated ?source_labels=a&source_labels=b — parse_qs returns the full
                # list, so labels may safely contain commas.
                labels = [x for x in qs.get("source_labels", []) if x and x.strip()]
                sl_token = set_source_labels(labels)
            except Exception:
                sl_token = set_source_labels([])
            try:
                await self.app(scope, receive, send)
            finally:
                _user_token.reset(ctx_token)
                if sl_token is not None:
                    reset_source_labels(sl_token)
        else:
            await self.app(scope, receive, send)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for request/response logging with correlation IDs."""

    _SILENT_PATHS = {"/api/ping", "/api/health"}

    async def dispatch(self, request: Request, call_next):
        # Skip logging for high-frequency keepalive / health endpoints
        if request.url.path in self._SILENT_PATHS:
            return await call_next(request)

        # Generate request ID for correlation
        request_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        # Log incoming request
        logger.info(
            f"[{request_id}] → {request.method} {request.url.path} "
            f"(client: {request.client.host if request.client else 'unknown'})"
        )

        # Process request
        response = await call_next(request)

        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000

        # Log response
        logger.info(
            f"[{request_id}] ← {response.status_code} in {duration_ms:.0f}ms"
        )

        # Add request ID to response headers for debugging
        response.headers["X-Request-ID"] = request_id

        return response


def setup_and_check_warehouse():
    """Set up dedicated warehouse and log configuration.

    This function:
    1. Creates a dedicated Large serverless warehouse if needed (when DATABRICKS_HTTP_PATH is 'auto' or not set)
    2. Uses an existing warehouse if DATABRICKS_HTTP_PATH is configured
    3. Logs the warehouse configuration for verification
    """
    try:
        from backend.db import get_workspace_client, setup_warehouse_connection

        # Set up the warehouse connection (creates dedicated warehouse if needed)
        http_path = setup_warehouse_connection()

        # Extract warehouse ID from HTTP path
        warehouse_id = http_path.split("/")[-1] if http_path else None

        if warehouse_id:
            try:
                w = get_workspace_client()
                warehouse = w.warehouses.get(warehouse_id)

                # Log warehouse configuration
                logger.info("=" * 60)
                logger.info("SQL Warehouse Configuration")
                logger.info("=" * 60)
                logger.info(f"  Name: {warehouse.name}")
                logger.info(f"  ID: {warehouse.id}")
                logger.info(f"  Size: {warehouse.cluster_size}")
                logger.info(f"  Type: {'Serverless' if warehouse.enable_serverless_compute else 'Pro'}")
                logger.info(f"  Min Clusters: {warehouse.min_num_clusters}")
                logger.info(f"  Max Clusters: {warehouse.max_num_clusters}")
                logger.info(f"  State: {warehouse.state}")
                logger.info(f"  Auto-Stop: {warehouse.auto_stop_mins} minutes")
                logger.info("=" * 60)

                # Check if warehouse is undersized for 14+ parallel queries
                recommended_size = "Large"
                size_order = ["2X-Small", "X-Small", "Small", "Medium", "Large", "X-Large", "2X-Large", "3X-Large", "4X-Large"]
                current_idx = size_order.index(warehouse.cluster_size) if warehouse.cluster_size in size_order else -1
                recommended_idx = size_order.index(recommended_size) if recommended_size in size_order else 4

                if current_idx < recommended_idx:
                    logger.warning(
                        f"⚠️  Warehouse '{warehouse.name}' is sized {warehouse.cluster_size}. "
                        f"Recommended: {recommended_size} or larger for optimal performance with 14+ parallel queries."
                    )
                else:
                    logger.info(f"✓ Warehouse size {warehouse.cluster_size} meets recommended size ({recommended_size})")

            except Exception as e:
                logger.warning(f"Could not fetch warehouse details: {e}")
        else:
            logger.warning("No warehouse ID found in DATABRICKS_HTTP_PATH")

    except Exception as e:
        logger.error(f"Warehouse setup failed: {e}")
        raise  # This is critical - we can't proceed without a warehouse


def setup_system_table_grants():
    """Grant the active identity access to all required system tables.

    Runs at startup as a non-fatal step. Assumes the first user to deploy
    the app is a workspace admin. If user auth (sql scope) is active the
    grants run as the workspace admin user; otherwise they run as the SP.

    Grants cover every system table the app queries so no manual GRANT
    statements are ever needed after deployment.
    """
    from backend.db import execute_query, get_workspace_client

    SYSTEM_TABLES = [
        ("CATALOG", "system"),
        ("SCHEMA",  "system.billing"),
        ("TABLE",   "system.billing.usage"),
        ("TABLE",   "system.billing.list_prices"),
        ("TABLE",   "system.billing.account_prices"),
        ("SCHEMA",  "system.query"),
        ("TABLE",   "system.query.history"),
        ("SCHEMA",  "system.compute"),
        ("TABLE",   "system.compute.clusters"),
        ("TABLE",   "system.compute.warehouses"),
        ("TABLE",   "system.compute.warehouse_events"),
        ("SCHEMA",  "system.lakeflow"),
        ("TABLE",   "system.lakeflow.jobs"),
        ("TABLE",   "system.lakeflow.pipelines"),
        ("TABLE",   "system.lakeflow.job_run_timeline"),
        ("SCHEMA",  "system.serving"),
        ("TABLE",   "system.serving.served_entities"),
        ("SCHEMA",  "system.access"),
        ("TABLE",   "system.access.audit"),
        ("TABLE",   "system.access.workspaces_latest"),
    ]

    try:
        w = get_workspace_client()
        principal = (w.current_user.me().user_name or "").strip()
        if not principal:
            logger.warning("Could not determine principal for system table grants — skipping")
            return

        logger.info(f"Granting system table access to: {principal}")
        succeeded = failed = 0

        for obj_type, obj_name in SYSTEM_TABLES:
            privilege = "USE CATALOG" if obj_type == "CATALOG" else (
                "USE SCHEMA" if obj_type == "SCHEMA" else "SELECT"
            )
            sql = f"GRANT {privilege} ON {obj_type} {obj_name} TO `{principal}`"
            try:
                execute_query(sql, no_cache=True)
                succeeded += 1
            except Exception as e:
                err = str(e).lower()
                # Already granted or object doesn't exist yet — both are non-fatal
                if "already" in err or "not found" in err or "does not exist" in err:
                    succeeded += 1
                else:
                    logger.warning(f"Grant failed (non-fatal): {sql} — {e}")
                    failed += 1

        logger.info(f"System table grants complete: {succeeded} ok, {failed} failed")

        # Also grant the current identity permission to create the app schema.
        # Needed when sql scope is not configured and the SP runs DDL.
        # Fails silently if the SP isn't a catalog owner/metastore admin.
        from backend.db import (
            StorageConfigurationError,
            get_catalog_schema,
            validate_app_storage_target,
        )
        catalog, schema = get_catalog_schema()
        try:
            validate_app_storage_target(catalog, schema)
        except StorageConfigurationError:
            logger.warning("Skipping catalog grants — storage config not valid yet (wizard pending or invalid config)")
            return
        for catalog_grant in [
            f"GRANT USE CATALOG ON CATALOG {catalog} TO `{principal}`",
            f"GRANT CREATE SCHEMA ON CATALOG {catalog} TO `{principal}`",
        ]:
            try:
                execute_query(catalog_grant, no_cache=True)
                logger.info(f"Catalog grant succeeded: {catalog_grant}")
            except Exception as e:
                err = str(e).lower()
                if "already" in err or "not found" in err or "does not exist" in err:
                    pass
                else:
                    logger.debug(f"Catalog grant failed (non-fatal — SP may not own catalog): {e}")

        # If running under user OAuth (workspace admin), also pre-grant the SP identity
        # the UC permissions it needs on the app schema so scheduled nightly refresh works
        # without manual grants. Non-fatal — skipped if DATABRICKS_CLIENT_ID is not set.
        sp_client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
        if sp_client_id and sp_client_id != principal:
            http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
            warehouse_id = http_path.split("/")[-1] if http_path and "/" in http_path else ""
            sp_schema_grants = [
                f"GRANT USE CATALOG ON CATALOG {catalog} TO `{sp_client_id}`",
                f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{sp_client_id}`",
                f"GRANT CREATE TABLE ON SCHEMA {catalog}.{schema} TO `{sp_client_id}`",
                f"GRANT SELECT ON SCHEMA {catalog}.{schema} TO `{sp_client_id}`",
            ]
            # Warehouse CAN_USE must be granted via REST API — SQL syntax is invalid.
            # setup.py _grant_warehouse_can_use() handles this on each setup/status call.
            sp_ok = 0
            for grant_sql in sp_schema_grants:
                try:
                    execute_query(grant_sql, no_cache=True)
                    sp_ok += 1
                except Exception as e:
                    err = str(e).lower()
                    if "already" in err:
                        sp_ok += 1
                    else:
                        logger.debug(f"SP pre-grant failed (non-fatal): {grant_sql} — {e}")
            logger.info(f"SP schema pre-grants: {sp_ok}/{len(sp_schema_grants)} applied for {sp_client_id}")

            # Warehouse CAN_USE — must use REST API (SQL syntax not supported)
            if warehouse_id:
                try:
                    from backend.routers.setup import _grant_warehouse_can_use
                    _grant_warehouse_can_use(w, sp_client_id)
                    logger.info(f"Warehouse CAN_USE granted for SP {sp_client_id} on warehouse {warehouse_id}")
                except Exception as wh_err:
                    logger.warning(
                        f"Warehouse CAN_USE grant failed for {sp_client_id} on {warehouse_id} — "
                        f"a workspace admin may need to grant this manually: {wh_err}"
                    )

    except Exception as e:
        logger.warning(f"System table grant setup failed (non-fatal): {e}")


def setup_system_access_schema():
    """Enable system.access schema so workspace names resolve in billing views.

    Calls the Unity Catalog SystemSchemas API to enable the 'access' schema on
    the current metastore. This is idempotent — safe to call if already enabled.
    Requires the SP to be a metastore admin or account admin; fails silently
    otherwise (workspace data will still show, just without names).
    """
    try:
        from backend.db import get_workspace_client
        w = get_workspace_client()

        # Get the current metastore ID
        metastore = w.metastores.current()
        metastore_id = metastore.metastore_id
        if not metastore_id:
            logger.warning("Could not determine metastore ID — skipping system.access setup")
            return

        # Enable the access schema (idempotent)
        w.system_schemas.enable(metastore_id=metastore_id, schema_name="access")
        logger.info("system.access schema enabled (or already enabled)")

    except Exception as e:
        err = str(e).lower()
        if "already enabled" in err or "already exists" in err:
            logger.info("system.access schema already enabled")
        else:
            logger.warning(
                f"Could not enable system.access schema (non-fatal — workspace names will not resolve): "
                f"{type(e).__name__}: {e}"
            )


def setup_materialized_views():
    """Refresh materialized views post-deploy — only if setup is durably complete.

    Startup must never create tables for the first time. Initial table creation is
    exclusively the wizard's job (POST /api/setup/create-tables). This function
    only kicks off a background refresh when it can confirm that setup has already
    been completed durably (i.e. the core MV tables exist in the configured location).

    If config is invalid or setup is incomplete, startup logs the issue and skips
    — the wizard or a configuration fix is required before any writes happen.
    """
    try:
        from backend.db import (
            StorageConfigurationError,
            execute_query,
            get_catalog_schema,
            validate_app_storage_target,
        )
        from backend.materialized_views import (
            check_materialized_views_exist,
            create_materialized_views,
        )

        catalog, schema = get_catalog_schema()

        # No location configured (env/.settings/DBFS all empty — the GCP case where
        # DBFS is disabled and a redeploy wiped .settings) — try to auto-discover it
        # from where the app's MV tables already live, so the app self-heals instead
        # of showing "setup incomplete" with the tables sitting right there.
        if not catalog or not schema:
            try:
                from backend.routers.setup import autodiscover_storage_location
                discovered = autodiscover_storage_location()
                if discovered:
                    catalog, schema = discovered
            except Exception as _disc_err:
                logger.warning("Storage auto-discovery skipped (non-fatal): %s", _disc_err)

        # Validate config before touching anything
        try:
            validate_app_storage_target(catalog, schema)
        except StorageConfigurationError as cfg_err:
            logger.critical(
                "Startup MV setup SKIPPED — invalid storage configuration: %s. "
                "Fix COST_OBS_CATALOG / COST_OBS_SCHEMA in the app environment and redeploy.",
                cfg_err,
            )
            return

        logger.info(f"Checking durable setup state in {catalog}.{schema}...")

        # Derive setup completion from durable state — whether core MV tables exist.
        # Do NOT use setup_done.json (ephemeral, wiped on every git redeploy).
        from backend.routers.setup import _CORE_REQUIRED_TABLES as _CORE_TABLES
        try:
            tables = check_materialized_views_exist(catalog, schema)
            setup_complete = all(tables.get(t, False) for t in _CORE_TABLES)
        except Exception as chk_err:
            logger.warning(f"Could not check MV table existence (non-fatal): {chk_err}")
            setup_complete = False

        if not setup_complete:
            logger.info(
                "Startup MV setup SKIPPED — core tables absent in %s.%s. "
                "Complete the setup wizard to create them.",
                catalog, schema,
            )
            return

        # Rebuild the additional-MV-source union views if any are configured. Their
        # definitions persist in UC across redeploys, but mv_sources is restored from
        # DBFS (the local .settings copy is wiped on redeploy) and MV reads remap to the
        # `<name>__unified` views — so rebuild is cheap insurance that they always exist.
        try:
            from backend.db import get_mv_sources
            if get_mv_sources():
                import threading as _uth

                from backend.materialized_views import rebuild_unified_views
                _uth.Thread(
                    target=lambda: rebuild_unified_views(catalog, schema), daemon=True
                ).start()
                logger.info("Rebuilding additional-MV-source union views in background")
        except Exception as _uerr:
            logger.warning("Could not rebuild unified MV views on startup (non-fatal): %s", _uerr)

        # Tables durably exist — background refresh is safe, but skip if recently refreshed.
        # Refreshing on every restart hammers the warehouse during cold start. The nightly
        # scheduler handles regular refreshes; startup only refreshes if data is stale (>26h).
        # Read freshness from the MV table itself (persists across deploys) rather than from
        # a local log file that gets wiped on every redeploy, which caused spurious concurrent
        # rebuilds conflicting with the nightly scheduled run (DELTA_METADATA_CHANGED errors).
        import threading
        from datetime import datetime, timezone
        _hours_since = float("inf")
        try:
            _fresh_result = execute_query(
                f"SELECT CAST(MAX(usage_date) AS DATE) as latest FROM `{catalog}`.`{schema}`.`daily_usage_summary`",
                no_cache=True,
            )
            if _fresh_result and _fresh_result[0].get("latest"):
                _latest_dt = datetime.fromisoformat(str(_fresh_result[0]["latest"])).replace(tzinfo=timezone.utc)
                _hours_since = (datetime.now(timezone.utc) - _latest_dt).total_seconds() / 3600
        except Exception:
            pass  # table missing or warehouse unavailable → treat as stale

        if _hours_since < 26:
            logger.info(f"Startup MV refresh SKIPPED — data is {_hours_since:.1f}h old (< 26h, fresh enough)")
            return

        # Concurrency is now handled INSIDE create_materialized_views via its
        # single write guard (in-process + cross-worker flock), so every refresh
        # path — startup, nightly, on-demand, wizard — serializes on one lock.
        # (Previously only this startup path locked, so the others raced and
        # produced duplicate rows.) No separate flock here — that would
        # double-lock against the internal guard and self-deadlock.
        def _bg_refresh():
            import time as _time
            _bg_start = _time.monotonic()
            from backend.routers.settings import append_refresh_history, mark_refresh_started
            mark_refresh_started("startup")
            try:
                logger.info("Refreshing materialized views in background (post-deploy, data is stale)...")
                r = create_materialized_views(catalog, schema)
                ok = sum(1 for v in r.values() if v == "created")
                logger.info(f"Background MV refresh complete: {ok}/{len(r)} tables rebuilt")
                # Record the startup/auto refresh in rebuild history — otherwise only
                # manual "Rebuild now" and nightly runs (which go through _run_mv_refresh)
                # ever appear, and the app's own refreshes look untracked.
                try:
                    failed = [k for k, v in r.items() if isinstance(v, str) and v.startswith("error")]
                    append_refresh_history(
                        "partial_error" if failed else "success", "startup",
                        duration_seconds=_time.monotonic() - _bg_start,
                        error="; ".join(failed) if failed else None,
                    )
                except Exception as _hist_exc:
                    logger.debug("Could not record startup refresh history: %s", _hist_exc)
                try:
                    from backend.db import clear_query_cache, delta_cache_invalidate
                    clear_query_cache()
                    delta_cache_invalidate()
                except Exception as inv_exc:
                    logger.warning(f"Cache invalidation after startup refresh failed: {inv_exc}")
            except Exception as ex:
                logger.warning(f"Background MV refresh failed (non-fatal): {ex}")
                append_refresh_history(
                    "error", "startup",
                    duration_seconds=_time.monotonic() - _bg_start,
                    error=str(ex),
                )
        threading.Thread(target=_bg_refresh, daemon=True).start()
        logger.info(f"Materialized views exist — background refresh started (data was {_hours_since:.1f}h stale)")

    except Exception as e:
        logger.warning(f"Materialized views startup check failed (non-fatal): {e}")


# Picks which worker fires the nightly run. Must differ from the rebuild write
# guard's file: flock treats two opens of one file independently, so sharing it
# makes the scheduled rebuild wait on its own lock forever.
_SCHEDULE_LOCK_PATH = "/tmp/cost-obs-mv-schedule.lock"


def _run_mv_refresh(user_token: str | None = None, lookback_days: int = 180, force_full: bool = False) -> dict:
    """Run CREATE OR REPLACE TABLE for all MV tables. Returns results dict."""
    import json
    import os
    import time
    from datetime import datetime, timezone

    from backend.db import _user_token as _db_user_token
    from backend.db import get_catalog_schema
    from backend.materialized_views import refresh_materialized_views

    # Always run DDL as the service principal regardless of whether a user token
    # is present.  The forwarded OAuth token (sql scope) grants SELECT access but
    # does NOT guarantee CAN_USE on the warehouse or CREATE TABLE on the schema —
    # both of which are required for a rebuild.  The SP owns both by design.
    ctx_tok = _db_user_token.set("")
    logger.info("MV refresh running as service principal (forced for DDL)")

    catalog, schema = get_catalog_schema()
    if not catalog or not schema:
        logger.info(
            "MV refresh SKIPPED — catalog/schema not configured yet. "
            "Complete the setup wizard to enable scheduled rebuilds."
        )
        _db_user_token.reset(ctx_tok)
        return {}

    log_dir = os.path.join(os.path.dirname(__file__), "..", ".settings")
    log_path = os.path.join(log_dir, "mv_refresh_log.json")
    log_tmp = log_path + ".tmp"

    refresh_start = time.monotonic()
    start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Capture trigger BEFORE force_full may be promoted by window-change detection.
    # A scheduled run that promotes to full due to a window change is still "scheduled".
    trigger = "manual" if force_full else "scheduled"
    from backend.routers.settings import mark_refresh_started
    mark_refresh_started(trigger, lookback_days)
    results: dict = {}
    log_data: dict = {"last_refresh_utc": start_utc, "duration_seconds": 0, "mv_timings": {}, "status": "error", "error": "unknown"}
    try:
        # If lookback_days differs from the last run, force a full rebuild so the
        # incremental MERGE path doesn't silently ignore the extended window.
        # force_full=True when the caller explicitly requests it (e.g. UI Rebuild button).
        # Otherwise detect a window change from the log so the scheduled nightly refresh
        # auto-promotes to full rebuild when lookback_days was changed in Settings.
        if not force_full:
            prev_lookback = None
            try:
                with open(log_path) as _lf:
                    prev_lookback = json.load(_lf).get("lookback_days")
            except Exception:
                pass
            # If prev_lookback is unknown (no log) or differs, force full so the
            # incremental MERGE doesn't silently leave the table at the old window.
            if prev_lookback is None or prev_lookback != lookback_days:
                force_full = True
                reason = "no prior log" if prev_lookback is None else f"window changed {prev_lookback}→{lookback_days}"
                logger.info(f"Forcing full rebuild: {reason}")
        else:
            logger.info(f"Full rebuild forced by caller (lookback={lookback_days}d)")
        results = refresh_materialized_views(catalog, schema, lookback_days=lookback_days, force_full_rebuild=force_full)
        mv_timings = results.pop("__mv_timings__", {})
        duration = round(time.monotonic() - refresh_start, 1)
        failed = {k: v for k, v in results.items() if isinstance(v, str) and v.startswith("error:")}
        # "schema" or "error" in failed means a top-level failure (not per-table).
        total_failure = "schema" in failed or "error" in failed or (failed and len(failed) == len(results))
        log_data = {
            "last_refresh_utc": start_utc,
            "duration_seconds": duration,
            "mv_timings": mv_timings,
            "status": "error" if total_failure else ("partial_error" if failed else "success"),
            "lookback_days": lookback_days,
        }
        if failed:
            log_data["error"] = "; ".join(f"{k}: {v}" for k, v in failed.items())
            logger.error(f"MV refresh: {len(failed)} table(s) failed — {log_data['error']}")
        else:
            logger.info(f"MV refresh complete in {duration}s")
        # Invalidate caches so next request hits fresh MV data immediately
        try:
            from backend.routers.billing import _mv_cache
            _mv_cache["available"] = None
            from backend.db import clear_query_cache, delta_cache_invalidate
            clear_query_cache()
            delta_cache_invalidate()
        except Exception as cache_exc:
            logger.warning(f"Cache invalidation after MV refresh failed: {cache_exc}")
    except Exception as exc:
        duration = round(time.monotonic() - refresh_start, 1)
        log_data = {
            "last_refresh_utc": start_utc,
            "duration_seconds": duration,
            "mv_timings": {},
            "status": "error",
            "error": str(exc)[:500],
            "lookback_days": lookback_days,
        }
        raise
    finally:
        _db_user_token.reset(ctx_tok)
        try:
            os.makedirs(log_dir, exist_ok=True)
            # Append this run to refresh_history (keep last 20)
            history_entry = {
                "timestamp": start_utc,
                "status": log_data.get("status", "error"),
                "duration_seconds": log_data.get("duration_seconds", 0),
                "lookback_days": lookback_days,
                "trigger": trigger,
            }
            if log_data.get("error"):
                history_entry["error"] = log_data["error"][:200]
            prev_history: list = []
            try:
                with open(log_path) as _lf:
                    prev_history = json.load(_lf).get("refresh_history", [])
            except Exception:
                pass
            log_data["refresh_history"] = (prev_history + [history_entry])[-20:]
            with open(log_tmp, "w") as f:
                json.dump(log_data, f)
            os.replace(log_tmp, log_path)
            # Persist to Delta so the log survives redeployments
            try:
                from backend.routers.settings import save_refresh_log_to_delta
                save_refresh_log_to_delta(log_data)
            except Exception as delta_exc:
                logger.warning(f"Could not save refresh log to Delta: {delta_exc}")
        except Exception as log_exc:
            logger.warning(f"Failed to write MV refresh log: {log_exc}")

    return results


def startup_tasks():
    """Run all startup tasks: setup warehouse, setup MVs, warm cache, setup alerts."""
    # Step 0: Set up dedicated warehouse (creates Large serverless warehouse if needed)
    setup_and_check_warehouse()

    # Step 0a: No synthetic warehouse warm-up ping. Startup steps below that
    # genuinely need the warehouse (system.access setup, MV creation) issue real
    # queries that trigger auto-start on their own — we never fire a warm-up query.

    # Step 0b: Enable system.access schema for workspace name resolution
    setup_system_access_schema()

    # Step 0c: Grant the active identity access to all required system tables
    # Only run on first setup — grants are persistent, no need to re-run every restart.
    from backend.db import read_dbfs_setup_complete
    from backend.routers.setup import SETUP_DONE_FILE
    _setup_complete = os.path.exists(SETUP_DONE_FILE) or read_dbfs_setup_complete()
    if not _setup_complete:
        setup_system_table_grants()
    else:
        logger.info("Setup already complete — skipping system table grants")

    # Step 0d: Restore refresh log from Delta (file is ephemeral, wiped on redeploy).
    # Must happen before setup_materialized_views() which reads the log for stale-check.
    try:
        from backend.routers.settings import restore_refresh_log_from_delta
        restore_refresh_log_from_delta()
    except Exception as e:
        logger.warning(f"Refresh log restore failed (non-fatal): {e}")

    # Step 1: Create materialized views if needed
    setup_materialized_views()

    # Step 1b: Restore workspace filter from Delta (file is ephemeral, wiped on redeploy)
    try:
        from backend.routers.setup import restore_workspace_filter_from_delta
        restore_workspace_filter_from_delta()
    except Exception as e:
        logger.warning(f"Workspace filter restore failed (non-fatal): {e}")

    # prewarm_cache_sync() disabled — 7 parallel billing queries at startup compete with
    # first-user DBSQL bundle requests. Re-enable once the 202+poll pattern is proven stable.
    logger.info("Startup cache prewarm skipped (disabled for stability)")

    # Step 6: Pre-warm permissions check (warms SDK auth + caches result for wizard)
    try:
        from backend.routers.permissions import _check_permissions_sync
        logger.info("Pre-warming permissions check...")
        _check_permissions_sync()
        logger.info("Permissions pre-warm complete")
    except Exception as e:
        logger.warning(f"Permissions pre-warm failed (non-fatal): {e}")

    # Step 7: Pre-warm ALL tabs (slower queries, runs after alerts)
    # prewarm_all_tabs() disabled — 9 tagging + 5 AI/ML queries in parallel saturate the
    # warehouse exactly when the first user's DBSQL bundle request arrives. Re-enable once stable.
    logger.info("All-tabs prewarm skipped (disabled for stability)")

    # Step 8: Pre-warm tables status cache so Settings panel loads instantly on first open.
    # Runs last — by this point the warehouse is warm and billing queries are cached.
    if _setup_complete:
        try:
            from backend.routers.settings import _prewarm_tables_cache
            logger.info("Pre-warming tables status cache...")
            _prewarm_tables_cache()
            logger.info("Tables status cache pre-warm complete")
        except Exception as e:
            logger.warning(f"Tables status cache pre-warm failed (non-fatal): {e}")


# Max concurrent request-path SQL threads. High enough to handle bursty traffic
# across all endpoints; low enough to avoid overwhelming the warehouse connection pool.
_REQUEST_EXECUTOR_MAX_WORKERS = 20


class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor that creates daemon worker threads not tracked by atexit.

    Standard ThreadPoolExecutor blocks SIGTERM exit in two independent ways:
    (1) loop.shutdown_default_executor() calls executor.shutdown(wait=True), joining
        in-flight threads that may be mid-way through a 30-45 s SQL query.
    (2) concurrent.futures._python_exit() (registered via atexit) joins every thread
        in _threads_queues at interpreter shutdown — a second, separate barrier.

    This subclass fixes both:
    - _adjust_thread_count() spawns daemon threads and skips _threads_queues
      registration, so _python_exit() never sees them and they are killed
      automatically when the main thread exits.
    - shutdown() logs active thread count then uses wait=False so
      shutdown_default_executor() returns immediately instead of joining.
    """

    def _adjust_thread_count(self):
        def weakref_cb(_, q=self._work_queue):
            q.put(None)
        if len(self._threads) < self._max_workers:
            t = threading.Thread(
                target=_fut_thread._worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                name=f"{self._thread_name_prefix}_{len(self._threads)}",
                daemon=True,
            )
            t.start()
            self._threads.add(t)
            # Intentionally NOT added to _fut_thread._threads_queues:
            # _python_exit() only joins threads in that mapping, so omitting
            # here prevents the atexit barrier from blocking process exit.

    def shutdown(self, wait=True, *, cancel_futures=False):
        active = sum(1 for t in self._threads if t.is_alive())
        if active:
            logger.warning(
                "SIGTERM: abandoning %d in-flight daemon SQL thread(s) — "
                "they will be killed when the process exits",
                active,
            )
        super().shutdown(wait=False, cancel_futures=True)


async def _run_in_daemon_thread(fn):
    """Await fn() running in a daemon thread.

    Unlike loop.run_in_executor(), uses a daemon thread so long-running
    background work (MV refresh, cache prewarm) is automatically killed on
    SIGTERM, allowing the process to exit within the 15-second grace window.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _target():
        try:
            loop.call_soon_threadsafe(fut.set_result, fn())
        except BaseException as exc:
            if not fut.done():
                loop.call_soon_threadsafe(fut.set_exception, exc)

    threading.Thread(target=_target, daemon=True).start()
    return await fut


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Replace the default executor so asyncio.to_thread() / run_in_executor(None, ...)
    # don't stall process exit at SIGTERM. asyncio.run() calls
    # loop.shutdown_default_executor() → executor.shutdown(wait=True) during cleanup;
    # the non-blocking override returns immediately, letting daemon threads die naturally.
    asyncio.get_running_loop().set_default_executor(
        _DaemonThreadPoolExecutor(
            max_workers=_REQUEST_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="to-thread",
        )
    )

    # Warehouse setup runs synchronously before accepting requests so the
    # setup wizard immediately shows the correct warehouse on first load.
    # Everything else (MV creation, job scheduling, cache prewarm) runs in
    # the background and does not block the app from starting.
    try:
        from backend.db import setup_warehouse_connection
        setup_warehouse_connection()
    except Exception as e:
        logger.warning(f"Warehouse setup during lifespan failed (non-fatal): {e}")

    # Restore refresh log from Delta synchronously BEFORE starting the scheduler.
    # startup_tasks also does this, but it runs in an executor thread and can lose
    # the race against the scheduler's first-iteration catch-up check — causing a
    # spurious "missed rebuild" catch-up on every pod restart/redeploy.
    try:
        from backend.routers.settings import (
            clear_refresh_in_progress,
            restore_refresh_log_from_delta,
        )
        restore_refresh_log_from_delta()
        clear_refresh_in_progress()
        logger.info("Refresh log restored from Delta (pre-scheduler)")
    except Exception as _rle:
        logger.warning("Pre-scheduler refresh log restore failed (non-fatal): %s", _rle)

    # Remaining startup tasks run in a daemon thread so SIGTERM can kill them
    # within the 15-second grace window without waiting for slow SQL connections.
    threading.Thread(target=startup_tasks, daemon=True, name="startup-tasks").start()

    # MV refresh scheduler — frequency/hour configurable via Settings > General.
    # Defaults: nightly at 05:00 UTC. Uses a file lock so only one worker fires.
    async def _daily_mv_refresh_loop():
        import fcntl
        import json as _sched_json
        from datetime import datetime, timezone

        def _last_rebuild_dt():
            _log = os.path.join(os.path.dirname(__file__), "..", ".settings", "mv_refresh_log.json")
            try:
                with open(_log) as f:
                    d = _sched_json.load(f)
                    ts = d.get("last_refresh_utc")
                    if ts:
                        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                pass
            return None

        first_iteration = True
        while True:
            try:
                from backend.routers.settings import load_schedule_settings
                sched = await asyncio.to_thread(load_schedule_settings)
                # Clear first_iteration unconditionally so re-enabling later doesn't re-trigger catch-up
                is_first = first_iteration
                if first_iteration:
                    first_iteration = False

                if not sched.get("enabled", True):
                    await asyncio.sleep(3600)  # check again in 1h
                    continue

                hour_utc = sched.get("hour_utc", 5)
                frequency = sched.get("frequency", "nightly")
                now = datetime.now(timezone.utc)

                # On startup, check if we missed the scheduled run while the pod was suspended.
                # If today's scheduled time has passed and the last rebuild was before it, run now.
                if is_first:
                    scheduled_today = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
                    if now > scheduled_today:
                        should_run_today = (
                            frequency == "nightly"
                            or (frequency == "weekly" and now.weekday() == 0)
                            or (frequency == "monthly" and now.day == 1)
                        )
                        if should_run_today:
                            last_dt = _last_rebuild_dt()
                            if last_dt is None or last_dt < scheduled_today:
                                logger.info(f"Missed scheduled rebuild at {hour_utc:02d}:00 UTC — running catch-up now")
                                lock_path = _SCHEDULE_LOCK_PATH
                                try:
                                    with open(lock_path, "w") as lf:
                                        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                        lookback_days = sched.get("lookback_days", 180)
                                        await _run_in_daemon_thread(lambda: _run_mv_refresh(lookback_days=lookback_days))
                                        logger.info("Catch-up rebuild complete")
                                        fcntl.flock(lf, fcntl.LOCK_UN)
                                except BlockingIOError:
                                    logger.info("Catch-up rebuild: another worker already running — skipping")
                                except Exception as _e:
                                    logger.error(f"Catch-up rebuild failed: {_e}")
                                # Re-read now after potential rebuild
                                now = datetime.now(timezone.utc)

                next_run = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(days=1)
                wait = (next_run - now).total_seconds()
                logger.info(f"Next MV rebuild in {wait/3600:.1f}h ({frequency} at {hour_utc:02d}:00 UTC)")
                await asyncio.sleep(max(wait, 0))

                # Re-read settings in case they changed while sleeping
                sched = await asyncio.to_thread(load_schedule_settings)
                if not sched.get("enabled", True):
                    continue
                frequency = sched.get("frequency", "nightly")
                now = datetime.now(timezone.utc)

                # Check if this run day matches the frequency
                should_run = (
                    frequency == "nightly"
                    or (frequency == "weekly" and now.weekday() == 0)   # Monday
                    or (frequency == "monthly" and now.day == 1)
                )
                if not should_run:
                    logger.info(f"Skipping rebuild (frequency={frequency}, weekday={now.weekday()}, day={now.day})")
                    continue

                lock_path = _SCHEDULE_LOCK_PATH
                try:
                    with open(lock_path, "w") as lf:
                        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        lookback_days = sched.get("lookback_days", 180)
                        logger.info(f"Running scheduled rebuild ({frequency}, lookback={lookback_days}d)...")
                        await _run_in_daemon_thread(lambda: _run_mv_refresh(lookback_days=lookback_days))
                        logger.info("Scheduled rebuild complete")
                        fcntl.flock(lf, fcntl.LOCK_UN)
                        # Evaluate cost-alert thresholds against the freshly-refreshed data
                        # and deliver breaches to Slack. Gated inside run_alert_check on a
                        # webhook being configured (configuring one is the opt-in), so this
                        # never posts unless the admin set it up.
                        try:
                            from backend.alerting import run_alert_check
                            res = await _run_in_daemon_thread(lambda: run_alert_check(send=True))
                            if res.get("sent"):
                                logger.info("Nightly alert check: %d breach(es) posted to Slack", len(res.get("breaches", [])))
                        except Exception as _alert_exc:
                            logger.warning(f"Nightly alert check failed (non-fatal): {_alert_exc}")
                except BlockingIOError:
                    logger.info("Rebuild already running in another worker — skipping")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"MV rebuild scheduler error: {e}")
                await asyncio.sleep(3600)  # retry in 1h on unexpected error

    # NOTE: there is NO warehouse keepalive or warming task of any kind. A prior
    # design pinged the warehouse every 5 min to keep it from auto-stopping;
    # with --workers 2 that fired twice every 5 min, pinning the (often shared)
    # serverless warehouse online ~24x7, inflating cost and polluting the usage
    # data this app reports on. The warehouse now warms solely as a side effect of
    # real user queries; with no users it auto-stops as intended. A cold start adds
    # ~15-30s to the first query after idle; the frontend shows a "Starting" splash
    # via the REST-only /api/health/sql-warehouse poll (which issues no SQL).
    scheduler_task = asyncio.create_task(_daily_mv_refresh_loop())
    yield
    scheduler_task.cancel()
    try:
        from backend.routers.setup import shutdown_readiness_executor
        shutdown_readiness_executor()
    except Exception as _e:
        logger.warning("Readiness executor shutdown failed (non-fatal): %s", _e)


app = FastAPI(
    title="Cost Observability & Control (COC)",
    description="Cost observability and analytics control dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

# Request logging middleware
app.add_middleware(RequestLoggingMiddleware)

# User authorization middleware — runs inside logging so requests show correct identity.
# Reads x-forwarded-access-token injected by Databricks Apps when user authorization
# is enabled (Public Preview). No-op when the header is absent.
app.add_middleware(UserAuthMiddleware)

# CORS configuration - externalized for production
# Set CORS_ORIGINS env var for production (comma-separated list of origins)
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-Forwarded-Email"],
)

# Include routers
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(ai_gateway.router, prefix="/api/ai-gateway", tags=["ai-gateway"])
if _has_budget and budget is not None:
    app.include_router(budget.router, prefix="/api/budget", tags=["budget"])
app.include_router(user.router, prefix="/api/user", tags=["user"])
app.include_router(billing.router, prefix="/api/billing", tags=["billing"])
app.include_router(setup.router, prefix="/api/setup", tags=["setup"])
app.include_router(aiml.router, prefix="/api/aiml", tags=["aiml"])
app.include_router(apps.router, prefix="/api/apps", tags=["apps"])
app.include_router(tagging.router, prefix="/api/tagging", tags=["tagging"])
app.include_router(aws_actual.router, prefix="/api/aws-actual", tags=["aws-actual"])
app.include_router(azure_actual.router, prefix="/api/azure-actual", tags=["azure-actual"])
app.include_router(gcp_actual.router, prefix="/api/gcp-actual", tags=["gcp-actual"])
app.include_router(dbsql.router, prefix="/api/dbsql", tags=["dbsql"])
app.include_router(permissions.router, prefix="/api/permissions", tags=["permissions"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(users_groups.router, prefix="/api/users-groups", tags=["users-groups"])
app.include_router(warehouse_health.router, prefix="/api/sql/warehouse-health", tags=["warehouse-health"])
app.include_router(genie_cost.router, prefix="/api/genie-cost", tags=["genie-cost"])
app.include_router(conversational.router, prefix="/api/conversational", tags=["conversational"])
app.include_router(debug.router, prefix="/api/debug", tags=["debug"])

# Serve static files in production.
# index.html gets Cache-Control: no-cache so browsers always fetch the latest
# after a deploy (prevents "Failed to fetch dynamically imported module" errors
# when Vite content-hashed chunk filenames change between deploys).
# JS/CSS assets under /assets/ are served as-is — their filenames are content-
# hashed so they can be cached indefinitely by the browser.
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):

    class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response

    app.add_middleware(NoCacheHTMLMiddleware)
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
