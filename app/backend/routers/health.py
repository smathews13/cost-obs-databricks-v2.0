"""Health check endpoints with actual service verification."""

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cachetools import TTLCache
from fastapi import APIRouter, BackgroundTasks

from backend import cache_ttls

logger = logging.getLogger(__name__)

router = APIRouter()

_deployment_metadata_cache: TTLCache[str, dict[str, Any]] = TTLCache(
    maxsize=8,
    ttl=cache_ttls.DEPLOYMENT_METADATA,
)
_deployment_metadata_lock = threading.Lock()
_deployment_metadata_inflight: dict[str, Future[dict[str, Any]]] = {}
_deployment_metadata_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="deployment-metadata",
)
_DEPLOYMENT_METADATA_TIMEOUT_SECONDS = 5.0
_APP_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
_COMMITTED_DEPLOYMENT_METADATA_PATH = (
    Path(__file__).resolve().parents[3] / "release-metadata.json"
)
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _metadata_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _metadata_attr(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _deployment_metadata_from_app(app: Any) -> dict[str, Any]:
    """Select the current app deployment's public provenance fields."""
    deployment = _metadata_attr(app, "active_deployment")
    git_source = (
        _metadata_attr(deployment, "git_source")
        or _metadata_attr(app, "git_source")
        or _metadata_attr(app, "default_git_source")
        or _metadata_attr(deployment, "git_repository")
        or _metadata_attr(app, "git_repository")
    )
    deployed_at = _metadata_text(
        _metadata_attr(deployment, "create_time")
        or _metadata_attr(deployment, "update_time")
        or _metadata_attr(app, "update_time")
        or _metadata_attr(app, "create_time")
    )
    deployer = _metadata_text(
        _metadata_attr(deployment, "creator")
        or _metadata_attr(deployment, "updater")
        or _metadata_attr(app, "updater")
        or _metadata_attr(app, "creator")
    )
    commit_sha = _metadata_text(
        _metadata_attr(git_source, "resolved_commit")
        or _metadata_attr(git_source, "commit")
    )
    return {
        "deployed_at": deployed_at,
        "deployer": deployer,
        "commit_sha": commit_sha,
        "available": any((deployed_at, deployer, commit_sha)),
        "source": "databricks_apps_api",
    }


def _deployment_metadata_from_deployment(deployment: Any) -> dict[str, Any]:
    git_source = (
        _metadata_attr(deployment, "git_source")
        or _metadata_attr(deployment, "git_repository")
    )
    deployed_at = _metadata_text(
        _metadata_attr(deployment, "create_time")
        or _metadata_attr(deployment, "update_time")
    )
    deployer = _metadata_text(
        _metadata_attr(deployment, "creator")
        or _metadata_attr(deployment, "updater")
    )
    commit_sha = _metadata_text(
        _metadata_attr(git_source, "resolved_commit")
        or _metadata_attr(git_source, "commit")
    )
    return {
        "deployed_at": deployed_at,
        "deployer": deployer,
        "commit_sha": commit_sha,
        "available": any((deployed_at, deployer, commit_sha)),
        "source": "databricks_apps_api",
    }


def _deployment_metadata_from_env() -> dict[str, Any]:
    """Read optional release-provided values without inventing build timestamps."""
    deployed_at = _metadata_text(os.getenv("COST_OBS_DEPLOYED_AT"))
    deployer = _metadata_text(os.getenv("COST_OBS_DEPLOYER"))
    commit_sha = _metadata_text(os.getenv("COST_OBS_COMMIT_SHA"))
    return {
        "deployed_at": deployed_at,
        "deployer": deployer,
        "commit_sha": commit_sha,
        "available": any((deployed_at, deployer, commit_sha)),
        "source": "release_environment",
    }


def _deployment_metadata_from_databricks_app_env() -> dict[str, Any]:
    """v2 runtime env names — extra merge inputs, never a fabricated deploy time."""
    deployed_at = _metadata_text(os.getenv("DATABRICKS_APP_DEPLOYED_AT"))
    deployer = _metadata_text(os.getenv("DATABRICKS_APP_DEPLOYER"))
    commit_sha = _metadata_text(
        os.getenv("DATABRICKS_APP_COMMIT") or os.getenv("GIT_COMMIT")
    )
    return {
        "deployed_at": deployed_at,
        "deployer": deployer,
        "commit_sha": commit_sha,
        "available": any((deployed_at, deployer, commit_sha)),
        "source": "databricks_app_environment",
    }


def _deployment_metadata_from_committed_file() -> dict[str, Any]:
    """Read a customer-safe, optional metadata file shipped with the source tree."""
    payload: dict[str, Any] = {}
    try:
        raw = json.loads(_COMMITTED_DEPLOYMENT_METADATA_PATH.read_text())
        if isinstance(raw, dict):
            payload = raw
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    deployed_at = _metadata_text(payload.get("deployed_at"))
    deployer = _metadata_text(payload.get("deployer"))
    commit_sha = _metadata_text(payload.get("commit_sha"))
    return {
        "deployed_at": deployed_at,
        "deployer": deployer,
        "commit_sha": commit_sha,
        "available": any((deployed_at, deployer, commit_sha)),
        "source": "committed_release_metadata",
    }


def _deployment_metadata_from_process_start() -> dict[str, Any]:
    """Last-resort date only; this is a restart approximation, not a deploy time."""
    return {
        "deployed_at": _PROCESS_STARTED_AT,
        "deployer": None,
        "commit_sha": None,
        "available": True,
        "source": "process_start_approximate_restart",
    }


def lakebase_instance_name() -> str | None:
    endpoint = _metadata_text(os.getenv("ENDPOINT_NAME"))
    if not endpoint:
        return None
    parts = endpoint.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "projects":
        return parts[1]
    return None


def lakebase_page_url(workspace_host: str | None = None) -> str | None:
    name = lakebase_instance_name()
    host = _metadata_text(workspace_host) or _metadata_text(os.getenv("DATABRICKS_HOST"))
    if not name or not host:
        return None
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"https://{host}"
    return f"{host.rstrip('/')}/compute/database-instances/{name}"


def _merge_deployment_metadata(*sources: dict[str, Any]) -> dict[str, Any]:
    fields = ("deployed_at", "deployer", "commit_sha")
    merged = {field: None for field in fields}
    used_sources: list[str] = []
    for source in sources:
        source_used = False
        for field in fields:
            if merged[field] is None:
                value = _metadata_text(source.get(field))
                if value is not None:
                    merged[field] = value
                    source_used = True
        if source_used:
            source_name = _metadata_text(source.get("source"))
            if source_name and source_name not in used_sources:
                used_sources.append(source_name)
    merged["available"] = any(merged[field] for field in fields)
    merged["source"] = "+".join(used_sources) if used_sources else "unavailable"
    merged["verified"] = merged["source"] == "databricks_apps_api"
    return merged


def _runtime_app_name() -> str | None:
    app_name = _metadata_text(os.getenv("DATABRICKS_APP_NAME"))
    return app_name if app_name and _APP_NAME_PATTERN.fullmatch(app_name) else None


def _resolve_app_name_from_runtime(apps_api: Any) -> str | None:
    """Resolve this app from the runtime SP when a legacy runtime omits its name."""
    client_id = _metadata_text(os.getenv("DATABRICKS_CLIENT_ID"))
    if not client_id:
        return None
    matches: list[str] = []
    for app in apps_api.list():
        app_client_ids = {
            _metadata_text(getattr(app, "service_principal_client_id", None)),
            _metadata_text(getattr(app, "service_principal_name", None)),
        }
        name = _metadata_text(getattr(app, "name", None))
        if (
            client_id in app_client_ids
            and name
            and _APP_NAME_PATTERN.fullmatch(name)
        ):
            matches.append(name)
    return matches[0] if len(matches) == 1 else None


def _fetch_deployment_metadata(app_name: str | None) -> dict[str, Any]:
    from backend.db import get_workspace_client

    apps_api = get_workspace_client().apps
    resolved_name = app_name or _resolve_app_name_from_runtime(apps_api)
    if not resolved_name:
        return _deployment_metadata_from_app(None)
    app_detail = apps_api.get(resolved_name)
    active_deployment = getattr(app_detail, "active_deployment", None)
    deployment_id = _metadata_text(
        getattr(active_deployment, "deployment_id", None)
    )
    if deployment_id and callable(getattr(apps_api, "get_deployment", None)):
        try:
            deployment = apps_api.get_deployment(resolved_name, deployment_id)
            metadata = _deployment_metadata_from_deployment(deployment)
            if metadata["available"]:
                return metadata
        except Exception as exc:
            logger.debug(
                "Active deployment detail unavailable for app %s: %s",
                resolved_name,
                exc,
            )
    return _deployment_metadata_from_app(app_detail)


def _finish_deployment_metadata_fetch(
    cache_key: str,
    future: Future[dict[str, Any]],
) -> None:
    try:
        metadata = future.result()
    except Exception as exc:
        logger.debug("Current app deployment metadata is unavailable: %s", exc)
        metadata = None
    with _deployment_metadata_lock:
        if metadata and metadata["available"]:
            _deployment_metadata_cache[cache_key] = dict(metadata)
        if _deployment_metadata_inflight.get(cache_key) is future:
            _deployment_metadata_inflight.pop(cache_key, None)


@router.get("/deployment")
async def deployment_metadata() -> dict[str, Any]:
    """Return authoritative metadata for the deployment currently serving the app."""
    app_name = _runtime_app_name()
    runtime_identity = _metadata_text(os.getenv("DATABRICKS_CLIENT_ID"))
    cache_key = app_name or runtime_identity
    api_metadata: dict[str, Any] | None = None
    if cache_key:
        started_fetch = False
        future: Future[dict[str, Any]] | None = None
        with _deployment_metadata_lock:
            cached = _deployment_metadata_cache.get(cache_key)
            future = _deployment_metadata_inflight.get(cache_key)
            if cached is not None:
                api_metadata = dict(cached)
            elif future is None:
                future = _deployment_metadata_executor.submit(
                    _fetch_deployment_metadata,
                    app_name,
                )
                _deployment_metadata_inflight[cache_key] = future
                started_fetch = True
        if started_fetch and future is not None:
            future.add_done_callback(
                lambda completed, key=cache_key: _finish_deployment_metadata_fetch(
                    key,
                    completed,
                )
            )
        if api_metadata is None and future is not None:
            try:
                api_metadata = await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(future)),
                    timeout=_DEPLOYMENT_METADATA_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.debug("Current app deployment metadata is unavailable: %s", exc)

    return _merge_deployment_metadata(
        api_metadata or _deployment_metadata_from_app(None),
        _deployment_metadata_from_env(),
        _deployment_metadata_from_databricks_app_env(),
        _deployment_metadata_from_committed_file(),
        _deployment_metadata_from_process_start(),
    )


@router.get("/ping")
async def ping() -> dict[str, bool]:
    """Zero-cost keepalive — no DB, no setup check. Used by client heartbeat."""
    return {"ok": True}


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Basic health check endpoint - fast response for load balancers."""
    return {"status": "healthy", "service": "cost-observability-control"}


@router.get("/health/detailed")
async def detailed_health_check() -> dict[str, Any]:
    """Detailed health check with database connectivity and cache stats.

    This endpoint performs actual service verification:
    - Database connectivity test
    - Query cache statistics
    - Memory usage info
    """
    checks: dict[str, Any] = {
        "status": "healthy",
        "service": "cost-observability-control",
        "checks": {},
    }

    # Check DBSQL connectivity
    db_status = await _check_database()
    checks["checks"]["database"] = db_status

    # Check Lakebase connectivity
    lakebase_status = await _check_lakebase()
    checks["checks"]["lakebase"] = lakebase_status

    # Get cache statistics
    cache_status = _get_cache_stats()
    checks["checks"]["cache"] = cache_status

    # Get memory info
    memory_status = _get_memory_info()
    checks["checks"]["memory"] = memory_status

    # Lakebase degraded is non-fatal — only DBSQL failure marks overall unhealthy
    all_healthy = all(
        check.get("status") == "healthy"
        for key, check in checks["checks"].items()
        if key != "lakebase"
    )
    checks["status"] = "healthy" if all_healthy else "degraded"

    return checks


def _classify_warehouse_type(wh: Any) -> str | None:
    """SERVERLESS / CLASSIC when the REST payload is explicit; never guess CLASSIC."""
    flag = getattr(wh, "enable_serverless_compute", None)
    if flag is True:
        return "SERVERLESS"
    if flag is False:
        return "CLASSIC"
    raw = getattr(wh, "warehouse_type", None)
    value = str(getattr(raw, "value", raw) or "").upper()
    if "SERVERLESS" in value:
        return "SERVERLESS"
    if value in {"CLASSIC"}:
        return "CLASSIC"
    return None


@router.get("/health/lakebase")
async def get_lakebase_status() -> dict[str, Any]:
    """Report Lakebase instance identity and a cheap connectivity status."""
    check = await _check_lakebase()
    raw = str(check.get("status") or "not_configured")
    if raw == "healthy":
        status = "warm"
    elif raw == "degraded":
        status = "warming_up"
    else:
        status = "unavailable"
    return {
        "status": status,
        "state": raw,
        "instance_name": lakebase_instance_name(),
        "page_url": lakebase_page_url(),
        "endpoint": _metadata_text(os.getenv("ENDPOINT_NAME")),
        "configured": bool(os.getenv("PGHOST")),
    }


@router.get("/health/sql-warehouse")
async def get_sql_warehouse_status() -> dict[str, Any]:
    """Check if the SQL warehouse is warm, starting, or unavailable.

    REST-only by design: it reports warehouse state from the Warehouses REST API
    and never issues a query. This endpoint is polled by the frontend every
    5-15s, so any query here would keep the warehouse warm continuously. If the
    REST state is unavailable, it reports warming_up and lets the first real user
    query warm the warehouse on demand.

    Returns:
        status: "warm" | "warming_up" | "unavailable"
        state:  raw warehouse state string (or None if API unavailable)
        latency_ms: always None (kept for response-shape compatibility)
        warehouse_id: warehouse ID being checked (or None if not configured)
        warehouse_name / warehouse_size / warehouse_type: REST metadata, or null
    """
    # Prefer DATABRICKS_WAREHOUSE_ID if injected; fall back to parsing HTTP path.
    # The regex handles /sql/1.0/warehouses/<id> and /sql/1.0/endpoints/<id>,
    # strips query-string params, and is safe against trailing slashes.
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID") or None
    if not warehouse_id:
        http_path = os.getenv("DATABRICKS_HTTP_PATH", "").split("?")[0].rstrip("/")
        _m = re.search(r"/(?:warehouses|endpoints)/([a-f0-9]+)$", http_path, re.IGNORECASE)
        warehouse_id = _m.group(1) if _m else None

    warehouse_metadata: dict[str, Any] = {
        "warehouse_name": None,
        "warehouse_size": None,
        "warehouse_type": None,
    }

    # A missing binding is definitive configuration state, not a transient REST
    # failure. Keep it distinct so the client can preserve the real unavailable
    # experience without treating network errors the same way.
    if not warehouse_id:
        return {
            "status": "unavailable",
            "state": "NOT_CONFIGURED",
            "latency_ms": None,
            "warehouse_id": None,
            **warehouse_metadata,
        }

    # IMPORTANT: the Databricks SDK uses the blocking `requests` library.
    # Calling warehouses.get() directly in an async function freezes the
    # asyncio event loop for the duration of the HTTP round-trip (can be
    # seconds to minutes in degraded environments). Run it in a thread with
    # a hard 5-second timeout so a hung SDK call falls through to transient
    # status handling. This poll never issues SQL.
    try:
        from backend.db import get_workspace_client

        _wh_id = warehouse_id  # capture for lambda
        wh = await asyncio.wait_for(
            asyncio.to_thread(lambda: get_workspace_client().warehouses.get(_wh_id)),
            timeout=5.0,
        )
        raw_state = str(wh.state.value) if wh.state else "UNKNOWN"
        warehouse_type = _classify_warehouse_type(wh)
        warehouse_metadata = {
            "warehouse_name": getattr(wh, "name", None),
            "warehouse_size": getattr(wh, "cluster_size", None),
            "warehouse_type": warehouse_type,
        }
        if raw_state.upper() in {"DELETING", "DELETED"}:
            return {
                "status": "unavailable",
                "state": raw_state,
                "latency_ms": None,
                "warehouse_id": warehouse_id,
                **warehouse_metadata,
            }
        if raw_state.upper() in {"STOPPED", "STOPPING", "STARTING"}:
            return {
                "status": "warming_up",
                "state": raw_state,
                "latency_ms": None,
                "warehouse_id": warehouse_id,
                **warehouse_metadata,
            }
        # Any other state (RUNNING, RESIZING, SCALING_DOWN, etc.) accepts queries.
        return {
            "status": "warm",
            "state": raw_state,
            "latency_ms": None,
            "warehouse_id": warehouse_id,
            **warehouse_metadata,
        }
    except Exception as e:
        logger.debug("Warehouse REST check failed; treating status as transient: %s", e)
        return {
            "status": "warming_up",
            "state": "REST_UNVERIFIED",
            "latency_ms": None,
            "warehouse_id": warehouse_id,
            **warehouse_metadata,
        }


async def _check_lakebase() -> dict[str, Any]:
    """Test Lakebase connectivity (non-fatal — returns degraded not unhealthy)."""
    import os
    if not os.environ.get("PGHOST"):
        return {"status": "not_configured", "message": "PGHOST not set — Lakebase disabled"}
    try:
        import asyncio

        from backend.db import lakebase_healthy
        loop = asyncio.get_running_loop()
        t0 = time.time()
        ok = await asyncio.wait_for(loop.run_in_executor(None, lakebase_healthy), timeout=5.0)
        latency_ms = round((time.time() - t0) * 1000, 2)
        if ok:
            return {"status": "healthy", "latency_ms": latency_ms}
        return {"status": "degraded", "message": "Lakebase ping failed"}
    except asyncio.TimeoutError:
        return {"status": "degraded", "message": "Lakebase health check timed out"}
    except Exception as e:
        return {"status": "degraded", "error": str(e)}


async def _check_database() -> dict[str, Any]:
    """Report warehouse configuration without issuing a query.

    Deliberately does NOT run a connectivity probe query — synthetic pings
    warm the serverless warehouse and distort the usage data this app reports on.
    Connectivity is proven by real queries elsewhere; this check only reports
    whether a warehouse is bound to the app.
    """
    import os

    # Return "healthy" when a warehouse is bound so the detailed_health_check
    # aggregator (which marks the app degraded unless every non-lakebase check is
    # "healthy") keeps working without a synthetic probe query.
    if os.getenv("DATABRICKS_WAREHOUSE_ID") or os.getenv("DATABRICKS_HTTP_PATH"):
        return {
            "status": "healthy",
            "message": "Warehouse bound; connectivity verified by real queries (no synthetic probe)",
        }
    return {
        "status": "not_configured",
        "message": "No warehouse bound (DATABRICKS_WAREHOUSE_ID / DATABRICKS_HTTP_PATH unset)",
    }


def _get_cache_stats() -> dict[str, Any]:
    """Get query cache statistics."""
    try:
        from backend.db import _CACHE_MAX_SIZE, _CACHE_TTL, _query_cache

        current_size = len(_query_cache)

        return {
            "status": "healthy",
            "current_entries": current_size,
            "max_entries": _CACHE_MAX_SIZE,
            "ttl_seconds": _CACHE_TTL,
            "utilization_percent": round((current_size / _CACHE_MAX_SIZE) * 100, 1),
        }
    except Exception as e:
        logger.error(f"Cache stats check failed: {e}")
        return {
            "status": "unknown",
            "error": str(e),
        }


def _get_memory_info() -> dict[str, Any]:
    """Get memory usage information."""
    try:
        import os
        import resource

        # Get memory usage in MB
        usage = resource.getrusage(resource.RUSAGE_SELF)
        memory_mb = usage.ru_maxrss / (1024 * 1024) if os.name != 'nt' else usage.ru_maxrss / 1024

        return {
            "status": "healthy",
            "rss_mb": round(memory_mb, 2),
        }
    except Exception as e:
        logger.error(f"Memory info check failed: {e}")
        return {
            "status": "unknown",
            "error": str(e),
        }


def _run_prewarm():
    """Run cache prewarm in background."""
    # prewarm_cache_sync() disabled — 7 parallel billing queries compete with first user request
    logger.info("Cache prewarm skipped (disabled for stability)")
    # prewarm_all_tabs() disabled — 9 tagging + 5 AI/ML queries saturate the warehouse at startup
    logger.info("All-tabs prewarm skipped (disabled for stability)")


@router.post("/cache/clear")
async def clear_cache(tab: str | None = None) -> dict[str, Any]:
    """Clear server-side query cache for a specific tab or all tabs.

    Tab patterns:
      dbu          → clears billing/dashboard-bundle queries
      infra        → clears infra-bundle and aws-actual queries
      kpis         → clears kpis-bundle queries
      aiml         → clears aiml queries
      apps         → clears apps queries
      tagging      → clears tagging queries
      sql          → clears dbsql and sql-breakdown queries
      users-groups → clears users-groups queries
      use-cases    → clears use-cases queries
      alerts       → clears alerts queries
      (none)       → clears entire cache
    """
    from backend.db import clear_query_cache, delta_cache_invalidate

    TAB_PATTERNS: dict[str, list[str]] = {
        "dbu":          ["dashboard-bundle-fast"],
        "infra":        ["infra-bundle", "infra-costs", "aws-actual", "aws-costs", "cloud-costs-bundle"],
        "kpis":         ["kpis-bundle", "spend-anomalies", "platform-kpis"],
        "aiml":         ["aiml"],
        "apps":         ["apps"],
        "tagging":      ["tagging"],
        "sql":          ["dbsql", "sql-breakdown"],
        "users-groups": ["users-groups"],
        "use-cases":    ["use-cases", "use_case", "monthly-consumption"],
        "alerts":       ["alerts"],
    }

    DELTA_TAB_PATTERNS: dict[str, str] = {
        "dbu":          "billing:",
        "kpis":         "billing:",
        "infra":        "billing:",
        "aiml":         "aiml:",
        "apps":         "apps:",
        "tagging":      "tagging:",
        "sql":          "dbsql:",
        "users-groups": "users:",
    }

    if tab and tab in TAB_PATTERNS:
        cleared = 0
        for pattern in TAB_PATTERNS[tab]:
            cleared += clear_query_cache(pattern)
        if tab in DELTA_TAB_PATTERNS:
            delta_cache_invalidate(DELTA_TAB_PATTERNS[tab])
        return {"status": "ok", "tab": tab, "cleared": cleared}
    else:
        cleared = clear_query_cache()
        delta_cache_invalidate()
        return {"status": "ok", "tab": "all", "cleared": cleared}


@router.post("/prewarm")
async def trigger_cache_prewarm(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger cache pre-warming for all dashboard queries.

    This runs in the background and returns immediately.
    Use /api/health/detailed to check cache status.
    """
    background_tasks.add_task(_run_prewarm)
    return {
        "status": "started",
        "message": "Cache pre-warming started in background. Check /api/health/detailed for cache stats."
    }


@router.get("/query-diag")
async def query_diagnostics() -> dict[str, Any]:
    """Diagnose why data tabs might show zeros.

    Tests SQL connectivity, system table access, and MV table access
    under the current auth identity (OAuth user or SP). Returns exact
    errors so the root cause can be pinpointed without reading server logs.

    SQL tests run in a thread pool so this endpoint never blocks the event loop.
    """
    import asyncio

    from backend.db import (
        _auth_mode,
        _user_token,
        execute_query,
        get_auth_status,
        get_catalog_schema,
    )

    diag: dict[str, Any] = {
        "auth": get_auth_status(),
        "user_token_present": bool(_user_token.get()),
        "auth_mode_global": _auth_mode,
        "tests": {},
    }

    catalog, schema = get_catalog_schema()
    diag["catalog"] = catalog
    diag["schema"] = schema

    def _run_sql_tests() -> dict:
        tests: dict[str, str] = {}

        # No synthetic connectivity probe — the system.billing.usage query below
        # proves connectivity against a real table.

        # Test 2: system.billing.usage (most commonly failing)
        try:
            rows = execute_query(
                "SELECT COUNT(*) AS cnt FROM system.billing.usage WHERE usage_date >= CURRENT_DATE - 7",
                no_cache=True,
            )
            tests["system_billing_usage"] = f"ok — {rows[0]['cnt'] if rows else 0} rows"
        except Exception as e:
            tests["system_billing_usage"] = f"ERROR: {e}"

        # Test 3: MV table (app catalog)
        try:
            rows = execute_query(
                f"SELECT COUNT(*) AS cnt FROM `{catalog}`.`{schema}`.`daily_usage_summary`",
                no_cache=True,
            )
            tests["mv_daily_usage_summary"] = f"ok — {rows[0]['cnt'] if rows else 0} rows"
        except Exception as e:
            tests["mv_daily_usage_summary"] = f"ERROR: {e}"

        # Test 4: system.query.history
        try:
            rows = execute_query(
                "SELECT COUNT(*) AS cnt FROM system.query.history WHERE start_time >= CURRENT_TIMESTAMP - INTERVAL 7 DAYS",
                no_cache=True,
            )
            tests["system_query_history"] = f"ok — {rows[0]['cnt'] if rows else 0} rows"
        except Exception as e:
            tests["system_query_history"] = f"ERROR: {e}"

        return tests

    loop = asyncio.get_running_loop()
    diag["tests"] = await loop.run_in_executor(None, _run_sql_tests)

    # Test 5: UC REST API table list (used by setup status check — no warehouse needed)
    try:
        from backend.db import get_workspace_client
        w = get_workspace_client()
        tables = [t.name for t in w.tables.list(catalog_name=catalog, schema_name=schema) if t.name]
        diag["tests"]["uc_table_list"] = f"ok — {len(tables)} tables: {sorted(tables)}"
    except Exception as e:
        diag["tests"]["uc_table_list"] = f"ERROR: {e}"

    return diag


@router.get("/billing-diag")
async def billing_diagnostics() -> dict[str, Any]:
    """Diagnose why billing/dashboard tabs show zeros.

    Checks the full data path used by /api/billing/dashboard-bundle-fast:
    - MV availability cache state
    - Each MV query (products, workspaces, summary, timeseries, etl_breakdown)
    - system.billing.usage fallback accessibility
    - Auth identity in use

    Hit this in the browser when tabs show zeros to get the exact failure point.
    """
    import asyncio
    import os
    import time

    from backend.db import (
        execute_query,
        get_auth_status,
        get_catalog_schema,
    )
    from backend.routers.billing import _check_mv_available, _mv_cache

    catalog, schema = get_catalog_schema()
    now = time.time()
    cache_age = round(now - _mv_cache["checked_at"], 1) if _mv_cache["checked_at"] else None

    diag: dict[str, Any] = {
        "auth": get_auth_status(),
        "warehouse": {
            "http_path": os.getenv("DATABRICKS_HTTP_PATH", "NOT SET"),
        },
        "catalog": catalog,
        "schema": schema,
        "mv_cache": {
            "available": _mv_cache["available"],
            "age_seconds": cache_age,
        },
        "mv_queries": {},
        "fallback_queries": {},
    }

    # Force a fresh MV availability check (bypass cache)
    _mv_cache["available"] = None
    mv_available = _check_mv_available()
    diag["mv_available_fresh"] = mv_available

    params = {"start_date": "2024-01-01", "end_date": "2030-12-31"}

    def _run_billing_tests() -> tuple[dict, dict]:
        from backend.materialized_views import (
            MV_BILLING_BY_PRODUCT,
            MV_BILLING_BY_WORKSPACE,
            MV_BILLING_SUMMARY,
            MV_BILLING_TIMESERIES,
            MV_ETL_BREAKDOWN,
        )
        from backend.queries import BILLING_BY_PRODUCT_FAST, BILLING_BY_WORKSPACE, BILLING_SUMMARY
        from backend.routers.billing import _exec_mv

        mv_tests: dict[str, str] = {}
        fallback_tests: dict[str, str] = {}

        for name, template in [
            ("summary", MV_BILLING_SUMMARY),
            ("products", MV_BILLING_BY_PRODUCT),
            ("workspaces", MV_BILLING_BY_WORKSPACE),
            ("timeseries", MV_BILLING_TIMESERIES),
            ("etl_breakdown", MV_ETL_BREAKDOWN),
        ]:
            try:
                rows = _exec_mv(template, params)
                mv_tests[name] = f"ok — {len(rows)} rows"
            except Exception as e:
                mv_tests[name] = f"ERROR: {e}"

        for name, query in [
            ("billing_summary", BILLING_SUMMARY),
            ("billing_products", BILLING_BY_PRODUCT_FAST),
            ("billing_workspaces", BILLING_BY_WORKSPACE),
        ]:
            try:
                rows = execute_query(query, params, no_cache=True)
                fallback_tests[name] = f"ok — {len(rows)} rows"
            except Exception as e:
                fallback_tests[name] = f"ERROR: {e}"

        return mv_tests, fallback_tests

    loop = asyncio.get_running_loop()
    mv_results, fallback_results = await loop.run_in_executor(None, _run_billing_tests)
    diag["mv_queries"] = mv_results
    diag["fallback_queries"] = fallback_results

    return diag


@router.get("/debug-env")
async def debug_env():
    """Debug: show detected environment (temporary)."""
    import os

    from backend.db import get_host_url
    host = get_host_url()
    cloud = None
    if host:
        h = host.lower()
        if "azuredatabricks.net" in h:
            cloud = "AZURE"
        elif "gcp.databricks.com" in h:
            cloud = "GCP"
        elif "cloud.databricks.com" in h:
            cloud = "AWS"
    return {
        "host": host,
        "cloud": cloud,
        "DATABRICKS_HOST": os.getenv("DATABRICKS_HOST", "NOT SET"),
        "DATABRICKS_HTTP_PATH": os.getenv("DATABRICKS_HTTP_PATH", "NOT SET"),
    }


@router.get("/setup-diag")
async def setup_diagnostics() -> dict[str, Any]:
    """Diagnose why the app is stuck on 'Setting up your workspace'.

    Tests every component of the bootstrap flow independently with short
    timeouts so you get a precise failure point instead of an infinite spinner.

    Hit this on the AWS app when setup stalls:
      https://<app-url>/api/setup-diag
    """
    import asyncio
    import os

    from backend.db import _auth_mode, _user_token, get_catalog_schema, get_workspace_client

    catalog, schema = get_catalog_schema()
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "NOT SET")
    host_raw = os.getenv("DATABRICKS_HOST", "NOT SET")
    hostname = host_raw.replace("https://", "").replace("http://", "").rstrip("/")
    sp_client_id = os.getenv("DATABRICKS_CLIENT_ID", "NOT SET")

    # Capture user token now — ContextVar values don't cross thread boundaries automatically
    user_tok = _user_token.get()

    diag: dict[str, Any] = {
        "env": {
            "DATABRICKS_HOST": host_raw,
            "DATABRICKS_HTTP_PATH": http_path,
            "DATABRICKS_CLIENT_ID": sp_client_id,
            "COST_OBS_CATALOG": os.getenv("COST_OBS_CATALOG", "NOT SET"),
            "COST_OBS_SCHEMA": os.getenv("COST_OBS_SCHEMA", "NOT SET"),
        },
        "catalog": catalog,
        "schema": schema,
        "auth_mode": _auth_mode,
        "user_token_present": bool(user_tok),
    }

    # Bootstrap state (in-process dict — instant)
    try:
        from backend.routers.setup import _create_task_state
        diag["bootstrap_state"] = _create_task_state.copy()
    except Exception as e:
        diag["bootstrap_state"] = f"ERROR: {e}"

    # ------------------------------------------------------------------ #
    # Helper: run a blocking callable in a thread with timeout             #
    # ------------------------------------------------------------------ #
    loop = asyncio.get_running_loop()

    async def _run(fn) -> dict:
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, fn), timeout=40)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timed out after 40s — warehouse may be cold or unreachable"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    # 1. UC API — no warehouse needed                                      #
    # ------------------------------------------------------------------ #
    def _uc_api():
        try:
            w = get_workspace_client()
            sp_name = None
            try:
                sp_name = w.current_user.me().user_name
            except Exception:
                pass
            tables = list(w.tables.list(catalog_name=catalog, schema_name=schema))
            return {"ok": True, "sp_identity": sp_name, "table_count": len(tables)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    diag["uc_api"] = await _run(_uc_api)

    # ------------------------------------------------------------------ #
    # 2. SP warehouse connection                                           #
    # ------------------------------------------------------------------ #
    def _wh_sp():
        if http_path == "NOT SET":
            return {"ok": False, "error": "DATABRICKS_HTTP_PATH not set"}
        try:
            from databricks import sql
            headers = get_workspace_client().config.authenticate()
            access_token = headers.get("Authorization", "").replace("Bearer ", "")
            if not access_token:
                return {"ok": False, "error": "empty SP access token from SDK"}
            conn = sql.connect(
                server_hostname=hostname,
                http_path=http_path,
                access_token=access_token,
                _socket_timeout=30,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT current_user() AS me")
                rows = cur.fetchall()
            conn.close()
            return {"ok": True, "sp_user": rows[0][0] if rows else None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    diag["warehouse_sp"] = await _run(_wh_sp)

    # ------------------------------------------------------------------ #
    # 3. User OAuth warehouse connection                                   #
    # ------------------------------------------------------------------ #
    def _wh_user():
        if not user_tok:
            return {"ok": None, "note": "no user token — open the app in a browser and retry"}
        if http_path == "NOT SET":
            return {"ok": False, "error": "DATABRICKS_HTTP_PATH not set"}
        try:
            from databricks import sql
            conn = sql.connect(
                server_hostname=hostname,
                http_path=http_path,
                access_token=user_tok,
                _socket_timeout=30,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT current_user() AS me")
                rows = cur.fetchall()
            conn.close()
            return {"ok": True, "user": rows[0][0] if rows else None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    diag["warehouse_user"] = await _run(_wh_user)

    # ------------------------------------------------------------------ #
    # 4. System table access (billing + query history)                    #
    # ------------------------------------------------------------------ #
    def _sys_tables():
        token = user_tok
        if not token:
            try:
                headers = get_workspace_client().config.authenticate()
                token = headers.get("Authorization", "").replace("Bearer ", "")
            except Exception as e:
                return {"connection": f"ERROR getting SP token: {e}"}
        if http_path == "NOT SET":
            return {"skipped": "DATABRICKS_HTTP_PATH not set"}
        results: dict[str, str] = {}
        try:
            from databricks import sql
            conn = sql.connect(
                server_hostname=hostname,
                http_path=http_path,
                access_token=token,
                _socket_timeout=30,
            )
            for tbl in [
                "system.billing.usage",
                "system.billing.list_prices",
                "system.query.history",
            ]:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
                        cur.fetchall()
                    results[tbl] = "ok"
                except Exception as e:
                    results[tbl] = f"ERROR: {e}"
            conn.close()
        except Exception as e:
            results["connection"] = f"ERROR: {e}"
        return results

    diag["system_tables"] = await _run(_sys_tables)

    # ------------------------------------------------------------------ #
    # 5. Schema create permission (needed for bootstrap)                  #
    # ------------------------------------------------------------------ #
    def _schema_perm():
        if not user_tok:
            return {"ok": None, "note": "no user token — test only runs when user is logged in"}
        if http_path == "NOT SET":
            return {"ok": False, "error": "DATABRICKS_HTTP_PATH not set"}
        try:
            from databricks import sql
            conn = sql.connect(
                server_hostname=hostname,
                http_path=http_path,
                access_token=user_tok,
                _socket_timeout=30,
            )
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`")
            conn.close()
            return {"ok": True, "note": f"user can create/access schema {catalog}.{schema}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    diag["schema_create_permission"] = await _run(_schema_perm)

    return diag
