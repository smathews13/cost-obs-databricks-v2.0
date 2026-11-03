"""App settings endpoints - Cloud infrastructure connections management."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.routers.user import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)
_MV_SHARE_RUNBOOK = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "cost_obs_mv_share_publisher.py"
)

# Captured at module load time — proxy for "when this app process started",
# which in Databricks Apps corresponds to the most recent deployment.
_SERVER_START_TIME = datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"

_APP_STATE_TABLES = (
    "app_cloud_connections",
    "app_mv_refresh_state",
    "app_mv_sources",
    "app_refresh_log",
    "app_schedule_settings",
    "app_settings",
    "app_unified_views",
    "app_user_permissions",
    "app_workspace_filter",
)
_APP_RESPONSE_CACHE_TABLE = "app_response_cache"


def _require_admin(request: Request) -> str:
    """Raise 403 if the requesting user is not an admin. Returns email on success."""
    email = request.headers.get("X-Forwarded-Email", os.getenv("USER", "dev@local"))
    if not _is_admin_email(email, _load_user_permissions()):
        raise HTTPException(status_code=403, detail="Admin role required")
    return email


def _is_admin_email(email: str, perms: dict) -> bool:
    """Mirror user.py::_get_user_role. With no admins configured yet (fresh deploy)
    everyone is admin; while the store can't be read, only the app's creator is."""
    from backend.routers.user import app_creator_email

    needle = str(email).strip().lower()
    admins = [str(item).strip().lower() for item in perms.get("admins", [])]
    if needle in admins:
        return True
    if perms.get("unavailable"):
        creator = app_creator_email()
        return bool(creator) and needle == creator
    return not admins


_ARCHITECTURE_PDF = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "cost-obs-architecture-v2.pdf"
)


def _experimental_enabled(key: str, default: bool = True) -> bool:
    """Read an experimental flag without stalling a download on Lakebase."""
    box: dict[str, bool] = {}

    def _load() -> None:
        try:
            box["value"] = bool(get_app_settings().get(key, default))
        except Exception:
            box["value"] = default

    thread = threading.Thread(target=_load, name=f"settings-flag-{key}", daemon=True)
    thread.start()
    thread.join(0.4)
    return box.get("value", default)


@router.get("/materialized-view-runbook")
async def download_materialized_view_runbook(request: Request) -> FileResponse:
    """Download the generated warehouse gold / Delta Share Run All notebook."""
    _require_admin(request)
    if not _experimental_enabled("enable_mv_share_runbook"):
        raise HTTPException(status_code=404, detail="Materialized view runbook is disabled")
    if not _MV_SHARE_RUNBOOK.is_file():
        raise HTTPException(status_code=404, detail="Materialized view runbook is unavailable")
    return FileResponse(
        _MV_SHARE_RUNBOOK,
        media_type="text/x-python",
        filename="cost_obs_mv_share_publisher.py",
    )


@router.get("/architecture-pdf")
async def download_architecture_pdf() -> FileResponse:
    """Download the committed architecture PDF when the experimental view is on."""
    if not _experimental_enabled("enable_architecture_view"):
        raise HTTPException(status_code=404, detail="Architecture PDF is disabled")
    if not _ARCHITECTURE_PDF.is_file():
        raise HTTPException(status_code=404, detail="Architecture PDF is unavailable")
    return FileResponse(
        _ARCHITECTURE_PDF,
        media_type="application/pdf",
        filename="cost-obs-architecture-v2.pdf",
    )

# In-process cache for /api/settings/tables — expensive parallel SQL + owner lookups
_tables_cache: dict | None = None
_tables_cache_ts: float = 0.0
_TABLES_CACHE_TTL = 15 * 60  # 15 minutes — prewarm fills this at startup; 5 min expired too fast

# Separate long-lived cache for table owner lookups (SDK REST call per table).
# Owners rarely change — 1-hour TTL means re-checks after the tables cache expires
# skip the 7 SDK calls entirely, cutting non-cached load time by ~1-2 s.
_owner_cache: dict[str, str | None] = {}
_owner_cache_ts: dict[str, float] = {}
_OWNER_CACHE_TTL = 60 * 60  # 1 hour


def _get_table_owner_cached(fqn: str) -> str | None:
    """Fetch table owner via Unity Catalog REST API with a 1-hour in-process cache."""
    cached_at = _owner_cache_ts.get(fqn, 0.0)
    if fqn in _owner_cache and (time.time() - cached_at) < _OWNER_CACHE_TTL:
        return _owner_cache[fqn]
    plain = fqn.replace("`", "")
    try:
        from backend.db import get_workspace_client
        info = get_workspace_client().tables.get(plain)
        owner = info.owner or None
        logger.debug(f"[owner] SP client {plain} -> {owner!r}")
    except Exception as e:
        logger.debug(f"[owner] SP client {plain} failed: {e}")
        owner = None
    _owner_cache[fqn] = owner
    _owner_cache_ts[fqn] = time.time()
    return owner


def _prewarm_tables_cache() -> None:
    """Populate the tables status cache proactively at startup.

    Called from startup_tasks() after the warehouse is warm so that the first
    user to open the Config tab sees instant results instead of a 10-30s spinner.
    Runs in a background thread — creates its own event loop to call the async
    endpoint, which is safe for non-main threads.
    """
    global _tables_cache, _tables_cache_ts
    if _tables_cache is not None and (time.time() - _tables_cache_ts) < _TABLES_CACHE_TTL:
        return  # already warm
    import asyncio

    class _FakeRequest:
        class headers:
            @staticmethod
            def get(key, default=""):
                return default

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(get_tables_status(_FakeRequest(), no_cache=True))
    finally:
        loop.close()

# File-based storage (fallback / dev only — production uses Delta tables)
SETTINGS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".settings")
CLOUD_CONNECTIONS_FILE = os.path.join(SETTINGS_DIR, "cloud_connections.json")
WEBHOOK_SETTINGS_FILE = os.path.join(SETTINGS_DIR, "webhook_settings.json")
WAREHOUSE_SETTINGS_FILE = os.path.join(SETTINGS_DIR, "warehouse_settings.json")
PRICING_SETTINGS_FILE = os.path.join(SETTINGS_DIR, "pricing_settings.json")
USER_PERMISSIONS_FILE = os.path.join(SETTINGS_DIR, "user_permissions.json")
SCHEDULE_SETTINGS_FILE = os.path.join(SETTINGS_DIR, "schedule_settings.json")
ALERT_THRESHOLDS_FILE = os.path.join(SETTINGS_DIR, "alert_thresholds.json")
# Legacy file path for backward compatibility
AZURE_CONNECTIONS_FILE = os.path.join(SETTINGS_DIR, "azure_connections.json")


# ── Delta table helpers (config tables that survive deploys) ──────────────────

def _config_table(name: str) -> str:
    from backend.db import (
        StorageConfigurationError,
        get_catalog_schema,
        validate_app_storage_target,
    )
    catalog, schema = get_catalog_schema()
    try:
        validate_app_storage_target(catalog, schema)
    except StorageConfigurationError as e:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail=f"App storage configuration is invalid — {e}",
        )
    return f"`{catalog}`.`{schema}`.`{name}`"


def _table_missing(e: Exception) -> bool:
    s = str(e)
    return "TABLE_OR_VIEW_NOT_FOUND" in s or "42P01" in s


_ensured_tables: set[str] = set()
_ensure_lock = __import__("threading").Lock()


def _ensure_config_table(ddl: str) -> None:
    if ddl in _ensured_tables:
        return
    with _ensure_lock:
        if ddl in _ensured_tables:
            return
        from backend.db import execute_write
        execute_write(ddl, None)
        _ensured_tables.add(ddl)


def _ensure_connections_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_cloud_connections')} "
        f"(id STRING NOT NULL, name STRING, provider STRING, created_at STRING, "
        f"config_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


def _ensure_webhook_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_webhook_settings')} "
        f"(slack_webhook_url STRING, updated_at TIMESTAMP) USING DELTA"
    )


def _ensure_alert_thresholds_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_alert_thresholds')} "
        f"(settings_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


def _ensure_schedule_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_schedule_settings')} "
        f"(settings_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


def _ensure_pricing_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_pricing_settings')} "
        f"(settings_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


# ── Workspace filter pool (survives deploys via Delta) ────────────────────────

def _ensure_workspace_filter_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_workspace_filter')} "
        f"(workspace_ids_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


# ── Refresh log persistence (survives deploys via Delta) ──────────────────────

def _ensure_refresh_log_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_refresh_log')} "
        f"(log_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


def save_refresh_log_to_delta(log_data: dict) -> None:
    """Persist the MV refresh log. Lakebase primary (app_config KV), UC dual-write."""
    import json as _json

    # Primary: Lakebase app_config KV
    try:
        from backend.db import lakebase_available, lb_set_config
        if lakebase_available():
            lb_set_config("refresh_log", log_data)
            logger.info("Refresh log saved to Lakebase (status=%s)", log_data.get("status"))
    except Exception as e:
        logger.warning("Could not save refresh log to Lakebase (non-fatal): %s", e)

    # Dual-write: UC Delta table
    try:
        from backend.db import execute_write
        _ensure_refresh_log_table()
        table = _config_table("app_refresh_log")
        execute_write(f"DELETE FROM {table}", None)
        execute_write(
            f"INSERT INTO {table} (log_json, updated_at) VALUES (:log_json, current_timestamp())",
            {"log_json": _json.dumps(log_data)},
        )
    except Exception as e:
        logger.debug("UC dual-write of refresh log skipped/failed (non-fatal): %s", e)


def append_refresh_history(status: str, trigger: str, *, lookback_days: int | None = None,
                           duration_seconds: float = 0, note: str | None = None,
                           error: str | None = None) -> None:
    """Append one entry to the rebuild-history log (file + Delta), keeping the last 20.

    Used for refresh runs that don't go through app._run_mv_refresh (the startup/auto
    build) and for config/lineage events like adding or removing a shared MV source, so
    the Rebuild history reflects everything that changed the managed tables — not only
    manual "Rebuild now" and nightly runs.
    """
    import json as _json
    from datetime import datetime, timezone
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", ".settings")
    log_path = os.path.join(log_dir, "mv_refresh_log.json")
    try:
        log_data: dict = {}
        try:
            with open(log_path) as f:
                log_data = _json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            log_data = {}
        entry: dict = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status,
            "duration_seconds": round(duration_seconds, 1),
            "lookback_days": lookback_days,
            "trigger": trigger,
        }
        if note:
            entry["note"] = note
        if error:
            entry["error"] = error[:200]
        history = (log_data.get("refresh_history") or []) + [entry]
        log_data["refresh_history"] = history[-20:]
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "w") as f:
            _json.dump(log_data, f)
        save_refresh_log_to_delta(log_data)
    except Exception as e:
        logger.debug("append_refresh_history (non-fatal): %s", e)


def restore_refresh_log_from_delta() -> None:
    """Restore the MV refresh log to the .settings file at startup.

    Reads Lakebase `app_config` first, falls back to the UC Delta table.
    """
    import json as _json
    log_data = None

    # Primary: Lakebase
    try:
        from backend.db import lb_get_config
        cfg = lb_get_config("refresh_log")
        if isinstance(cfg, dict) and cfg:
            log_data = cfg
    except Exception as e:
        logger.debug("Lakebase refresh-log read failed, will try UC: %s", e)

    # Fallback: UC Delta table
    if log_data is None:
        try:
            from backend.db import execute_query
            table = _config_table("app_refresh_log")
            rows = execute_query(f"SELECT log_json FROM {table} LIMIT 1", None, no_cache=True)
            if rows and rows[0].get("log_json"):
                log_data = _json.loads(rows[0]["log_json"])
        except Exception as e:
            if _table_missing(e):
                logger.debug("Refresh log not in UC yet (non-fatal): %s", e)
            else:
                logger.warning(f"Could not restore refresh log from UC (non-fatal): {e}")

    if not log_data:
        return
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", ".settings")
    log_path = os.path.join(log_dir, "mv_refresh_log.json")
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, "w") as f:
        _json.dump(log_data, f)
    logger.info("Restored refresh log (last_refresh=%s)", log_data.get("last_refresh_utc"))


class CloudConnectionCreate(BaseModel):
    name: str
    provider: str  # "azure", "aws", "gcp"
    # Azure fields
    tenant_id: Optional[str] = None
    subscription_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    # AWS fields
    aws_account_id: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    region: Optional[str] = None
    # GCP fields
    project_id: Optional[str] = None
    service_account_key: Optional[str] = None


_CONN_TOP_LEVEL = {"id", "name", "provider", "created_at"}


def _ensure_connections_table_lb() -> None:
    """Create the Lakebase cloud_connections table if missing (lazy, idempotent)."""
    from backend.db import lakebase_execute
    lakebase_execute(
        "CREATE TABLE IF NOT EXISTS cloud_connections ("
        "id TEXT PRIMARY KEY, name TEXT, provider TEXT, created_at TEXT, "
        "config JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


def _load_connections_from_table() -> list[dict]:
    # Primary: Lakebase
    try:
        from backend.db import lakebase_available, lakebase_query
        if lakebase_available():
            _ensure_connections_table_lb()
            rows = lakebase_query(
                "SELECT id, name, provider, created_at, config FROM cloud_connections ORDER BY created_at"
            )
            result = []
            for r in rows:
                conn = {"id": r["id"], "name": r["name"], "provider": r["provider"], "created_at": r["created_at"]}
                cfg = r.get("config")
                if isinstance(cfg, dict):
                    conn.update(cfg)
                result.append(conn)
            return result
    except Exception as e:
        logger.debug("Lakebase connections read failed, will try UC: %s", e)

    # Fallback: UC Delta table
    from backend.db import execute_query
    _ensure_connections_table()
    table = _config_table("app_cloud_connections")
    try:
        rows = execute_query(f"SELECT * FROM {table} ORDER BY created_at", None, no_cache=True)
    except Exception as e:
        if "TABLE_OR_VIEW_NOT_FOUND" in str(e):
            _ensured_tables.clear()
        raise
    result = []
    for r in rows:
        conn: dict = {
            "id": r["id"],
            "name": r["name"],
            "provider": r["provider"],
            "created_at": r["created_at"],
        }
        if r.get("config_json"):
            try:
                conn.update(json.loads(r["config_json"]))
            except Exception:
                pass
        result.append(conn)
    return result


def _save_all_connections_to_table(connections: list[dict]) -> None:
    # Primary: Lakebase
    try:
        from backend.db import lakebase_available, lakebase_execute
        if lakebase_available():
            import psycopg.types.json as _pj
            _ensure_connections_table_lb()
            lakebase_execute("DELETE FROM cloud_connections")
            for conn in connections:
                config = {k: v for k, v in conn.items() if k not in _CONN_TOP_LEVEL}
                lakebase_execute(
                    "INSERT INTO cloud_connections (id, name, provider, created_at, config) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (conn.get("id", ""), conn.get("name", ""), conn.get("provider", ""),
                     conn.get("created_at", ""), _pj.Jsonb(config)),
                )
    except Exception as e:
        logger.warning("Could not save connections to Lakebase (non-fatal): %s", e)

    # Dual-write: UC Delta table
    try:
        from backend.db import execute_write
        _ensure_connections_table()
        table = _config_table("app_cloud_connections")
        execute_write(f"DELETE FROM {table}", None)
        for conn in connections:
            config = {k: v for k, v in conn.items() if k not in _CONN_TOP_LEVEL}
            execute_write(
                f"INSERT INTO {table} (id, name, provider, created_at, config_json, updated_at) "
                f"VALUES (:id, :name, :provider, :created_at, :config_json, current_timestamp())",
                {
                    "id": conn.get("id", ""),
                    "name": conn.get("name", ""),
                    "provider": conn.get("provider", ""),
                    "created_at": conn.get("created_at", ""),
                    "config_json": json.dumps(config),
                },
            )
    except Exception as e:
        logger.debug("UC dual-write of connections skipped/failed (non-fatal): %s", e)


def _upsert_connection_to_table(conn: dict) -> None:
    config = {k: v for k, v in conn.items() if k not in _CONN_TOP_LEVEL}

    # Primary: Lakebase (ON CONFLICT upsert)
    try:
        from backend.db import lakebase_available, lakebase_execute
        if lakebase_available():
            import psycopg.types.json as _pj
            _ensure_connections_table_lb()
            lakebase_execute(
                "INSERT INTO cloud_connections (id, name, provider, created_at, config) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, provider = EXCLUDED.provider, "
                "created_at = EXCLUDED.created_at, config = EXCLUDED.config, updated_at = now()",
                (conn["id"], conn.get("name", ""), conn.get("provider", ""),
                 conn.get("created_at", ""), _pj.Jsonb(config)),
            )
    except Exception as e:
        logger.warning("Could not upsert connection to Lakebase (non-fatal): %s", e)

    # Dual-write: UC Delta table
    try:
        from backend.db import execute_write
        _ensure_connections_table()
        table = _config_table("app_cloud_connections")
        execute_write(f"DELETE FROM {table} WHERE id = :id", {"id": conn["id"]})
        execute_write(
            f"INSERT INTO {table} (id, name, provider, created_at, config_json, updated_at) "
            f"VALUES (:id, :name, :provider, :created_at, :config_json, current_timestamp())",
            {
                "id": conn["id"],
                "name": conn.get("name", ""),
                "provider": conn.get("provider", ""),
                "created_at": conn.get("created_at", ""),
                "config_json": json.dumps(config),
            },
        )
    except Exception as e:
        logger.debug("UC dual-write of connection upsert skipped/failed (non-fatal): %s", e)


def _delete_connection_from_table(connection_id: str) -> None:
    # Primary: Lakebase
    try:
        from backend.db import lakebase_available, lakebase_execute
        if lakebase_available():
            _ensure_connections_table_lb()
            lakebase_execute("DELETE FROM cloud_connections WHERE id = %s", (connection_id,))
    except Exception as e:
        logger.warning("Could not delete connection from Lakebase (non-fatal): %s", e)

    # Dual-write: UC Delta table
    try:
        from backend.db import execute_write
        _ensure_connections_table()
        table = _config_table("app_cloud_connections")
        execute_write(f"DELETE FROM {table} WHERE id = :id", {"id": connection_id})
    except Exception as e:
        logger.debug("UC dual-write of connection delete skipped/failed (non-fatal): %s", e)


def _load_connections() -> list[dict]:
    """Load cloud connections from Delta table, falling back to local file."""
    try:
        conns = _load_connections_from_table()
        if conns:
            return conns
        # Table empty — check file for migration data
    except Exception as e:
        if _table_missing(e):
            logger.debug("Could not load connections from Delta table (not yet created): %s", e)
        else:
            logger.warning(f"Could not load connections from Delta table: {e}")

    # Fallback: local file
    file_conns = _load_connections_from_file()
    if file_conns:
        try:
            _save_all_connections_to_table(file_conns)
            logger.info(f"Migrated {len(file_conns)} cloud connection(s) from file to Delta table")
        except Exception as e:
            logger.warning(f"Could not migrate connections to Delta: {e}")
    return file_conns


def _load_connections_from_file() -> list[dict]:
    """Load cloud connections from local JSON files (legacy / dev fallback)."""
    if os.path.exists(CLOUD_CONNECTIONS_FILE):
        try:
            with open(CLOUD_CONNECTIONS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    if os.path.exists(AZURE_CONNECTIONS_FILE):
        try:
            with open(AZURE_CONNECTIONS_FILE) as f:
                connections = json.load(f)
            for conn in connections:
                if "provider" not in conn:
                    conn["provider"] = "azure"
            _save_connections_to_file(connections)
            return connections
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_connections_to_file(connections: list[dict]) -> None:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(CLOUD_CONNECTIONS_FILE, "w") as f:
        json.dump(connections, f, indent=2)


def _save_connections(connections: list[dict]) -> None:
    """Save cloud connections to Delta table (primary) and file (dev fallback)."""
    try:
        _save_all_connections_to_table(connections)
    except Exception as e:
        logger.warning(f"Could not save connections to Delta table: {e}")
    _save_connections_to_file(connections)


def _mask_connection(conn: dict) -> dict:
    """Mask sensitive fields in a connection for API response."""
    masked = dict(conn)
    for secret_field in ("client_secret", "secret_access_key", "service_account_key"):
        val = masked.get(secret_field)
        if val and len(val) > 4:
            masked[secret_field] = "***" + val[-4:]
        elif val:
            masked[secret_field] = "****"
    return masked


def _get_git_sha() -> str:
    """Return the current git commit SHA (short form). Empty string if unavailable."""
    try:
        import subprocess as _sp
        sha = _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=_sp.DEVNULL,
            cwd=os.path.dirname(__file__),
        ).decode().strip()
        return sha
    except Exception:
        return os.getenv("COMMIT_SHA", "")


def _get_git_info() -> dict:
    """Return git branch, repo remote URL, and commit date.

    In Databricks Apps, the deployed directory has no .git history, so git
    commands fail. Falls back to env vars and then to the server process start
    time (a reliable proxy for the last deployment).
    """
    import subprocess as _sp
    _cwd = os.path.dirname(__file__)
    def _run(cmd: list[str]) -> str:
        try:
            return _sp.check_output(cmd, stderr=_sp.DEVNULL, cwd=_cwd).decode().strip()
        except Exception:
            return ""
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    repo = _run(["git", "remote", "get-url", "origin"])
    commit_date = _run(["git", "log", "-1", "--format=%ci", "HEAD"])
    # Strip seconds+tz for brevity: "2026-05-25 14:30:00 +0000" → "2026-05-25 14:30"
    if commit_date and len(commit_date) >= 16:
        commit_date = commit_date[:16]
    # Databricks Apps doesn't ship .git history, so git log always fails there.
    # Fall back to server start time — the process restarts on every deploy, so
    # this is a reliable proxy for "when was this version last deployed".
    return {
        "branch": branch or os.getenv("GIT_BRANCH", ""),
        "repo": repo or os.getenv("GIT_REPO", ""),
        "commit_date": commit_date or os.getenv("COMMIT_DATE", _SERVER_START_TIME),
    }


_warehouse_cache: dict | None = None  # in-process cache; cleared on server restart


@router.get("/config")
async def get_app_config():
    """Return current app configuration. Warehouse name fetched from SDK; other fields are instant from env vars."""
    import asyncio as _asyncio

    from backend.db import get_catalog_schema

    # Warehouse: resolve ID from env vars, then look up name/state via SDK.
    warehouse_id_resource = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")

    if warehouse_id_resource:
        warehouse_source = "app_resource"
        warehouse_id = warehouse_id_resource
    elif http_path and "/" in http_path:
        warehouse_source = "http_path"
        warehouse_id = http_path.rstrip("/").split("/")[-1]
    elif http_path:
        warehouse_source = "http_path"
        warehouse_id = http_path
    else:
        warehouse_source = "none"
        warehouse_id = ""

    warehouse: dict = (
        {"id": warehouse_id, "name": None, "size": None, "state": "UNKNOWN", "source": warehouse_source}
        if warehouse_id
        else {"id": None, "name": None, "size": None, "state": "NOT_CONFIGURED", "source": "none"}
    )

    if warehouse_id:
        global _warehouse_cache
        if _warehouse_cache and _warehouse_cache.get("_id") == warehouse_id:
            warehouse.update({k: v for k, v in _warehouse_cache.items() if k != "_id"})
        else:
            def _fetch_warehouse():
                try:
                    from backend.db import get_workspace_client
                    w = get_workspace_client()
                    wh = w.warehouses.get(warehouse_id)
                    return {
                        "name": wh.name or None,
                        "size": wh.cluster_size or None,
                        "state": str(wh.state.value) if wh.state else "UNKNOWN",
                    }
                except Exception:
                    return {}
            loop = _asyncio.get_running_loop()
            try:
                detail = await _asyncio.wait_for(loop.run_in_executor(None, _fetch_warehouse), timeout=3)
                if detail:
                    _warehouse_cache = {"_id": warehouse_id, **detail}
                    warehouse.update(detail)
            except Exception:
                pass  # timeout or SDK error — warehouse ID is still shown

    # Identity: SP client ID from env var (no current_user.me() call)
    sp_client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
    identity = {"display_name": sp_client_id, "user_name": sp_client_id} if sp_client_id else None

    # Storage location: env var / override file read. On GCP after a redeploy this can
    # be empty (env unset, .settings wiped, DBFS disabled) until autodiscovery has run —
    # which left the "Catalog & schema" chip showing just ".". Self-heal by discovering
    # the location from where the MV tables already live, same as startup.
    storage_location = None
    try:
        catalog, schema = get_catalog_schema()
        if not (catalog and schema):
            try:
                from backend.routers.setup import autodiscover_storage_location
                discovered = autodiscover_storage_location()
                if discovered:
                    catalog, schema = discovered
            except Exception as _disc_err:
                logger.debug("Config storage auto-discovery skipped (non-fatal): %s", _disc_err)
        storage_location = {
            "catalog": catalog,
            "schema": schema,
            "catalog_source": "env_var" if os.getenv("COST_OBS_CATALOG") else "default",
            "schema_source": "env_var" if os.getenv("COST_OBS_SCHEMA") else "default",
        }
    except Exception as e:
        logger.warning(f"Could not fetch catalog/schema: {e}")

    git = _get_git_info()
    return {
        "warehouse": warehouse,
        "identity": identity,
        "storage_location": storage_location,
        "version": {
            "commit_sha": _get_git_sha(),
            "branch": git["branch"],
            "repo": git["repo"],
            "commit_date": git["commit_date"],
        },
    }


@router.get("/tables")
async def get_tables_status(request: Request, no_cache: bool = False):
    """Return status of each MV table: exists, row count, max date, days behind."""
    global _tables_cache, _tables_cache_ts
    if not no_cache and _tables_cache is not None and (time.time() - _tables_cache_ts) < _TABLES_CACHE_TTL:
        return _tables_cache

    try:
        return await _get_tables_status_inner(request)
    except Exception as e:
        logger.exception("get_tables_status: unhandled exception")
        return {"catalog": None, "schema": None, "tables": [], "error": str(e), "auth_error": None, "refresh_status": None}


async def _get_tables_status_inner(request: Request):
    global _tables_cache, _tables_cache_ts
    from backend.db import _user_token, execute_query, get_catalog_schema

    # Read the raw forwarded token directly — _auth_mode may be locked to "sp"
    # (e.g. warehouse was cold on startup and the scope check failed), which forces
    # _user_token to "" even when x-forwarded-access-token IS present.  Reading the
    # header directly bypasses that lock and ensures table checks always run as the
    # user when the SQL scope is configured.
    _captured_token = (
        request.headers.get("x-forwarded-access-token", "")
        or _user_token.get()
    )

    MV_TABLES = [
        "daily_usage_summary",
        "daily_product_breakdown",
        "daily_workspace_breakdown",
        "sql_tool_attribution",
        "daily_query_stats",
        "dbsql_cost_per_query",
        "app_user_permissions",
    ]
    # Which tables are conceptually "materialized views" (rebuilt on schedule)
    # vs persistent managed tables
    MV_SET = {
        "daily_usage_summary", "daily_product_breakdown", "daily_workspace_breakdown",
        "sql_tool_attribution", "daily_query_stats", "dbsql_cost_per_query",
    }

    try:
        catalog, schema = get_catalog_schema()
    except Exception as e:
        return {"catalog": None, "schema": None, "tables": [], "error": str(e)}


    # Tables that don't have a usage_date column — use an alternate date expression or skip date
    date_expr_overrides = {
        "dbsql_cost_per_query": "CAST(MAX(start_time) AS DATE)",
    }
    no_date_tables = {
        "app_user_permissions",
    }

    min_date_expr_overrides = {
        "dbsql_cost_per_query": "CAST(MIN(start_time) AS DATE)",
    }

    def check_table(table_name: str, fqn: str, table_type: str) -> dict:
        # Force SP auth — SP owns all app tables so it always has SELECT.
        # User token lacks the sql OAuth scope in Databricks Apps, causing
        # "Error during request to server" when used for warehouse queries.
        tok = _user_token.set("")
        try:
            return _check_table_inner(table_name, fqn, table_type)
        finally:
            _user_token.reset(tok)

    # Owner lookups use the module-level cached function — avoids repeating SDK REST
    # calls on every non-cached tables check (owners change rarely; TTL = 1 hour).

    def _check_table_inner(table_name: str, fqn: str, table_type: str) -> dict:
        # Owner is fetched in a separate parallel pool — not here — to avoid
        # the SDK REST call serialising before the SQL query and doubling latency.
        skip_date = table_name in no_date_tables
        _not_found_signals = ("TABLE_OR_VIEW_NOT_FOUND",)

        if skip_date:
            # Single existence probe — avoids a separate DESCRIBE TABLE round-trip.
            try:
                execute_query(f"SELECT 1 FROM {fqn} LIMIT 1")
                return {"name": table_name, "table_type": table_type, "exists": True, "row_count": None, "min_date": None, "max_date": None, "days_behind": None, "owner": None}
            except Exception as e:
                err = str(e)
                if any(s in err for s in _not_found_signals) or "does not exist" in err.lower() or "not found" in err.lower():
                    return {"name": table_name, "table_type": table_type, "exists": False, "row_count": None, "min_date": None, "max_date": None, "days_behind": None, "owner": None}
                return {"name": table_name, "table_type": table_type, "exists": None, "row_count": None, "min_date": None, "max_date": None, "days_behind": None, "owner": None, "error": err[:200]}

        # Single query: SELECT MAX/MIN returns NULL on an empty table and raises
        # TABLE_OR_VIEW_NOT_FOUND if the table doesn't exist — no DESCRIBE needed.
        # Use "TABLE_OR_VIEW_NOT_FOUND" and "table or view" as not-found signals;
        # do NOT use the generic "not found" which also matches COLUMN_NOT_FOUND
        # and would incorrectly mark an existing table with schema drift as absent.
        def _table_not_found(e: str) -> bool:
            return "TABLE_OR_VIEW_NOT_FOUND" in e or "table or view" in e.lower()
        try:
            max_expr = date_expr_overrides.get(table_name, "MAX(usage_date)")
            min_expr = min_date_expr_overrides.get(table_name, "MIN(usage_date)")
            rows = execute_query(f"SELECT {max_expr} as max_date, {min_expr} as min_date, COUNT(*) as row_count FROM {fqn}")
            if not rows:
                return {"name": table_name, "table_type": table_type, "exists": True, "row_count": None, "min_date": None, "max_date": None, "days_behind": None, "owner": None}
            max_date = rows[0].get("max_date")
            min_date = rows[0].get("min_date")
            row_count = rows[0].get("row_count")
            max_date_str = str(max_date) if max_date else None
            min_date_str = str(min_date) if min_date else None
            days_behind = None
            if max_date_str:
                from datetime import date as _date
                try:
                    delta = _date.today() - _date.fromisoformat(max_date_str[:10])
                    days_behind = delta.days
                except Exception:
                    pass
            return {"name": table_name, "table_type": table_type, "exists": True, "row_count": row_count, "min_date": min_date_str, "max_date": max_date_str, "days_behind": days_behind, "owner": None}
        except Exception as e:
            err = str(e)
            if _table_not_found(err):
                return {"name": table_name, "table_type": table_type, "exists": False, "row_count": None, "min_date": None, "max_date": None, "days_behind": None, "owner": None}
            return {"name": table_name, "table_type": table_type, "exists": True, "row_count": None, "min_date": None, "max_date": None, "days_behind": None, "owner": None, "error": err[:200]}

    # Config tables are created lazily on first save — not existing yet is expected
    CONFIG_TABLES: set[str] = set()

    # Build task list: (table_name, fqn, table_type)
    tasks = [
        (t, f"`{catalog}`.`{schema}`.`{t}`", "Materialized View" if t in MV_SET else "Table")
        for t in MV_TABLES
    ]

    results = []
    # 30s — a warming warehouse (e.g. right after adding a shared source kicks off a
    # unified-view rebuild) can take >12s to answer per-table probes; the old 12s
    # produced spurious "?" error rows on healthy tables. Base-table probes are fast
    # once warm, so this only extends the cold/contended case.
    _TABLE_CHECK_TIMEOUT = 30
    import asyncio as _asyncio
    loop = _asyncio.get_running_loop()

    # Use run_in_executor so the event loop is freed while SQL queries run.
    sql_futures_map: dict = {
        loop.run_in_executor(None, check_table, name, fqn, ttype): (name, fqn, ttype)
        for name, fqn, ttype in tasks
    }
    owner_futures_map: dict = {
        loop.run_in_executor(None, _get_table_owner_cached, fqn): name
        for name, fqn, _ in tasks
    }

    sql_done, sql_pending = await _asyncio.wait(
        set(sql_futures_map.keys()), timeout=_TABLE_CHECK_TIMEOUT
    )
    for fut in sql_done:
        try:
            results.append(fut.result())
        except Exception:
            pass

    if sql_pending:
        for fut, (name, _fqn, ttype) in sql_futures_map.items():
            if fut in sql_pending:
                results.append({
                    "name": name, "table_type": ttype, "exists": None,
                    "row_count": None, "min_date": None, "max_date": None, "days_behind": None,
                    "owner": None, "error": "timed out — warehouse may be starting up",
                })
        logger.warning("Table status check timed out — warehouse likely cold")

    owner_done, _ = await _asyncio.wait(set(owner_futures_map.keys()), timeout=1.5)
    owner_map: dict = {}
    for fut in owner_done:
        name = owner_futures_map[fut]
        try:
            owner_map[name] = fut.result()
        except Exception:
            owner_map[name] = None
    for r in results:
        r["owner"] = owner_map.get(r["name"])

    # Preserve original order and tag optional config tables
    order = {name: i for i, (name, _, _) in enumerate(tasks)}
    results.sort(key=lambda r: order.get(r["name"], 99))
    for r in results:
        if r["name"] in CONFIG_TABLES:
            r["optional"] = True

    # Detect auth/permission failures — surface a top-level auth_error so the UI
    # can show an actionable message instead of per-row ⚠ icons.
    _PERM_SIGNALS = ("PERMISSION_DENIED", "INSUFFICIENT_PRIVILEGES", "not authorized",
                     "Not authorized", "Unauthorized", "User does not have", "403")
    perm_errors = [
        r for r in results
        if r.get("error") and any(s in r["error"] for s in _PERM_SIGNALS)
    ]
    auth_error = None
    if perm_errors and len(perm_errors) >= len(tasks) // 2:
        auth_error = (
            "The app service principal lacks permission to read these tables. "
            "Open the Setup wizard and run the Permissions step to grant the SP access to the required system tables and app catalog."
        )

    # Read MV refresh log (atomic write guarantees no partial read)
    refresh_status = None
    _log_path = os.path.join(os.path.dirname(__file__), "..", "..", ".settings", "mv_refresh_log.json")
    try:
        with open(_log_path) as _f:
            _log = json.load(_f)
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        _last = _dt.strptime(_log["last_refresh_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
        _hours = (_dt.now(_tz.utc) - _last).total_seconds() / 3600
        refresh_status = {
            "last_refresh_utc": _log["last_refresh_utc"],
            "duration_seconds": _log.get("duration_seconds"),
            "hours_since_refresh": round(_hours, 1),
            "stale": _hours > 26,
            "status": _log.get("status", "unknown"),
            "lookback_days": _log.get("lookback_days"),
            "refresh_history": _log.get("refresh_history", []),
        }
        if _log.get("error"):
            refresh_status["error"] = _log["error"]
    except (FileNotFoundError, KeyError, ValueError, OSError):
        pass

    # Phase B: Lakebase aggregate-sync watermark. Surfaced SEPARATELY from the
    # Delta build time (refresh_status) above — the two drift independently, so a
    # single freshness number would hide a stalled build behind a fresh sync (§9).
    # Empty list when Lakebase is absent or nothing has synced yet.
    lakebase_sync_status: list = []
    try:
        from backend.lakebase_sync import get_sync_status
        lakebase_sync_status = await asyncio.to_thread(get_sync_status)
    except Exception as e:
        logger.debug("Lakebase sync-status read failed (non-fatal): %s", e)

    result = {
        "catalog": catalog, "schema": schema, "tables": results,
        "auth_error": auth_error, "refresh_status": refresh_status,
        "lakebase_sync_status": lakebase_sync_status,
    }
    _tables_cache = result
    _tables_cache_ts = time.time()
    return result


@router.get("/catalog")
async def get_catalog_settings():
    """Return current catalog/schema and whether it's from an override or env vars."""
    from backend.db import get_catalog_schema_info
    return get_catalog_schema_info()


def build_workspace_job_url(host: str, job_id: str, workspace_id: str = "") -> str:
    """Build a Databricks Jobs UI URL. Empty if host or job_id is missing."""
    h = (host or "").rstrip("/")
    jid = str(job_id or "").strip()
    if not h or not jid:
        return ""
    if not h.startswith("http"):
        h = "https://" + h
    org = f"?o={workspace_id}" if workspace_id else ""
    return f"{h}/jobs/{jid}{org}"


def _resolve_setup_job_id(w) -> str:
    """Prefer the DAB-injected job id; fall back to listing `cost-obs-setup` by name."""
    env_id = (os.getenv("COST_OBS_SETUP_JOB_ID") or "").strip()
    if env_id:
        return env_id
    name = (os.getenv("COST_OBS_SETUP_JOB_NAME") or "cost-obs-setup").strip()
    try:
        for job in w.jobs.list(name=name):
            jid = str(getattr(job, "job_id", "") or "")
            if jid:
                return jid
    except Exception as exc:
        logger.info("app-links: could not list setup job %s: %s", name, exc)
    return ""


@router.get("/app-links")
async def get_app_links() -> dict:
    """URLs for the Settings → Config card: the app's source code in the customer
    workspace, and the app's page in Databricks Apps (its "backend").

    Best-effort and never raises — any field that can't be resolved is left "".
    The app asks the Apps API about ITSELF (SP identity), so this works even when
    the viewer is not a workspace admin. A Git-deployed app has no workspace source
    folder, so its source link falls back to the Databricks Apps page.
    """
    import asyncio
    import re as _re

    host = (os.getenv("DATABRICKS_HOST") or "").rstrip("/")
    if host and not host.startswith("http"):
        host = "https://" + host
    app_name = (os.getenv("DATABRICKS_APP_NAME") or "").strip()
    out: dict[str, Any] = {
        "host": host, "app_name": app_name,
        "app_url": "", "app_page_url": "", "source_code_url": "",
        "setup_job_id": "", "setup_job_url": "",
    }
    if not host:
        return out
    env_job = (os.getenv("COST_OBS_SETUP_JOB_ID") or "").strip()
    if env_job:
        out["setup_job_id"] = env_job
        out["setup_job_url"] = build_workspace_job_url(host, env_job)
    if not app_name:
        return out

    def _resolve() -> dict:
        from backend.db import get_workspace_client
        w = get_workspace_client()
        body = w.api_client.do("GET", f"/api/2.0/apps/{app_name}") or {}
        folder_id = ""
        # Uploaded / bundle deploys carry a /Workspace path we can turn into the
        # folder-browser id. Git deploys report a repo path (no workspace object).
        dep = body.get("active_deployment") or {}
        git_backed = bool(dep.get("git_source"))
        src_path = "" if git_backed else (dep.get("source_code_path") or "")
        if isinstance(src_path, str) and src_path.startswith("/Workspace/"):
            try:
                st = w.api_client.do("GET", "/api/2.0/workspace/get-status", query={"path": src_path}) or {}
                oid = st.get("object_id") if st.get("object_id") is not None else st.get("resource_id")
                if oid is not None:
                    folder_id = str(oid)
            except Exception as exc:
                logger.info("app-links: could not resolve folder id for %s: %s", src_path, exc)
        job_id = _resolve_setup_job_id(w)
        return {"body": body, "folder_id": folder_id, "setup_job_id": job_id}

    try:
        resolved = await asyncio.to_thread(_resolve)
    except Exception as e:
        logger.warning("app-links: could not fetch app %s: %s", app_name, e)
        out["error"] = str(e)
        return out

    body = resolved.get("body") or {}
    app_url = body.get("url") or ""
    out["app_url"] = app_url

    # Workspace id, read off the app's own hostname
    # (<name>-<workspace-id>.<cloud>.databricksapps.com) — nothing hands the
    # container a workspace id directly.
    m = _re.search(r"-(\d{6,})\.", app_url)
    wsid = m.group(1) if m else (os.getenv("DATABRICKS_WORKSPACE_ID") or "").strip()
    org = f"?o={wsid}" if wsid else ""

    # The app's page in Databricks Apps — where compute, logs and deployments live.
    out["app_page_url"] = f"{host}/apps/{app_name}{org}"

    # Source code in the workspace: the folder holding what is serving, when the
    # workspace resolved one; otherwise the app page (a Git deploy has no folder).
    fid = resolved.get("folder_id") or ""
    out["source_code_url"] = f"{host}/browse/folders/{fid}{org}" if fid else out["app_page_url"]
    setup_job_id = str(resolved.get("setup_job_id") or "").strip()
    out["setup_job_id"] = setup_job_id
    out["setup_job_url"] = build_workspace_job_url(host, setup_job_id, wsid)
    return out


# ── Additional MV sources (union of Delta-shared / cross-workspace MVs) ───────

def _invalidate_mv_caches() -> None:
    """Clear caches so a just-changed set of MV sources is reflected immediately."""
    try:
        from backend.db import clear_query_cache, delta_cache_invalidate
        clear_query_cache()
        delta_cache_invalidate()
    except Exception as exc:
        logger.debug("MV cache invalidation (non-fatal): %s", exc)
    try:
        from backend.routers.billing import _mv_cache
        _mv_cache["available"] = None
        _mv_cache["checked_at"] = 0
    except Exception:
        pass


def _detect_source_cloud(catalog: str) -> str | None:
    """Best-effort detect which cloud a Delta-shared catalog originates from
    ('gcp' | 'aws' | 'azure'), via its provider's cloud/region. None if unknown."""
    try:
        from backend.db import get_workspace_client
        w = get_workspace_client()
        provider_name = getattr(w.catalogs.get(catalog), "provider_name", None)
        if not provider_name:
            return None
        prov = w.providers.get(provider_name)
        hint = " ".join(str(getattr(prov, a, "") or "") for a in ("cloud", "region")).lower()
        # recipient_profile can also carry a region/endpoint hint
        rp = getattr(prov, "recipient_profile", None)
        if rp:
            hint += " " + str(getattr(rp, "endpoint", "") or "").lower()
        if "gcp" in hint or "google" in hint:
            return "gcp"
        if "azure" in hint:
            return "azure"
        if "aws" in hint or "amazon" in hint:
            return "aws"
    except Exception as e:
        logger.debug("Source cloud detection failed for %s (non-fatal): %s", catalog, e)
    return None


def _share_last_updated(catalog: str, schema: str, tables: list[str] | None) -> str | None:
    """Best-effort latest lastModified across the source's shared tables (ISO string)."""
    from backend.db import execute_query
    from backend.materialized_views import _MV_TABLES
    probe = (tables or _MV_TABLES)[:1]  # one representative table is enough + cheap
    for t in probe:
        try:
            rows = execute_query(f"DESCRIBE DETAIL `{catalog}`.`{schema}`.`{t}`", None, no_cache=True)
            if rows and rows[0].get("lastModified"):
                return str(rows[0]["lastModified"])
        except Exception:
            continue
    return None


@router.get("/mv-sources")
async def get_mv_sources_endpoint(detail: bool = False) -> dict:
    """Additional MV source locations unioned into every MV read, plus this
    workspace's own label (used for its rows in the source_label column).

    `detail=1` (used by the settings modal, not the top-nav filter) adds each
    source's `share_last_updated` via a DESCRIBE DETAIL probe — kept off the default
    path so the frequently-polled nav filter stays fast."""
    from backend.db import get_local_source_label, get_mv_sources
    sources = get_mv_sources()
    if detail:
        def _enrich():
            for s in sources:
                s["share_last_updated"] = _share_last_updated(s.get("catalog"), s.get("schema"), s.get("tables"))
            return sources
        sources = await asyncio.to_thread(_enrich)
    return {"sources": sources, "local_label": get_local_source_label()}


@router.get("/mv-sources/preview")
async def preview_mv_source(catalog: str, schema: str) -> dict:
    """Probe a candidate source location: for each MV table, whether it exists
    there and matches the local structure (so the Browse UI can show readiness)."""
    from backend.db import get_catalog_schema
    from backend.materialized_views import _MV_TABLES, _table_columns

    cat, sch = get_catalog_schema()
    src_cat, src_sch = (catalog or "").strip(), (schema or "").strip()
    if not src_cat or not src_sch:
        raise HTTPException(status_code=400, detail="catalog and schema are required")

    def _probe() -> list[dict]:
        out = []
        for t in _MV_TABLES:
            local_cols = _table_columns(f"`{cat}`.`{sch}`.`{t}`")
            src_cols = _table_columns(f"`{src_cat}`.`{src_sch}`.`{t}`")
            if src_cols is None:
                status = "absent"
            elif local_cols and src_cols == local_cols:
                status = "match"
            else:
                status = "mismatch"
            out.append({"table": t, "status": status})
        return out

    tables = await asyncio.to_thread(_probe)
    matched = sum(1 for x in tables if x["status"] == "match")
    return {"catalog": src_cat, "schema": src_sch, "tables": tables,
            "matched": matched, "total": len(_MV_TABLES)}


@router.post("/mv-sources", dependencies=[Depends(require_admin)])
async def add_mv_source(request: Request, body: dict) -> dict:
    """Add (or replace by label) an additional MV source and rebuild the unified
    views. The source's tables must match the app's MV structure.

    Not gated on the app admin role — registering a shared-view source only ever
    adds read-only data on top of this workspace's own, never replacing it."""
    from backend.db import get_catalog_schema, get_mv_sources, save_mv_sources
    from backend.materialized_views import _MV_TABLES, rebuild_unified_views

    label = (body.get("label") or "").strip()
    catalog = (body.get("catalog") or "").strip()
    schema = (body.get("schema") or "").strip()
    if not (label and catalog and schema):
        raise HTTPException(status_code=400, detail="label, catalog and schema are required")
    cat, sch = get_catalog_schema()
    if catalog == cat and schema == sch:
        raise HTTPException(status_code=400, detail="Source cannot be the app's own catalog.schema.")

    # Optional per-source view selection (multiselect in the Browse UI). None means
    # "include every matching view" (backward-compatible with pre-picker sources).
    tables = body.get("tables")
    if tables is not None:
        tables = [t for t in tables if t in _MV_TABLES]
        if not tables:
            raise HTTPException(status_code=400, detail="Select at least one view to include.")

    sources = [s for s in get_mv_sources() if s.get("label") != label]
    from datetime import datetime, timezone
    entry = {"label": label, "catalog": catalog, "schema": schema,
             "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if tables is not None:
        entry["tables"] = tables
    cloud = await asyncio.to_thread(_detect_source_cloud, catalog)
    if cloud:
        entry["cloud"] = cloud
    sources.append(entry)
    save_mv_sources(sources)
    summary = await asyncio.to_thread(rebuild_unified_views)
    _invalidate_mv_caches()
    # Record the source addition as a config/lineage step in the rebuild history.
    n_views = len(tables) if tables is not None else len(_MV_TABLES)
    await asyncio.to_thread(
        append_refresh_history, "config", "config",
        note=f"Added shared source '{label}' ({catalog}.{schema}, {n_views} view{'s' if n_views != 1 else ''})",
    )
    return {"ok": True, "sources": sources, "build": summary}


@router.delete("/mv-sources", dependencies=[Depends(require_admin)])
async def remove_mv_source(request: Request, label: str = None) -> dict:
    """Remove an additional MV source by label and rebuild unified views."""
    from backend.db import get_mv_sources, save_mv_sources
    from backend.materialized_views import rebuild_unified_views

    if not label:
        raise HTTPException(status_code=400, detail="label query param is required")
    sources = [s for s in get_mv_sources() if s.get("label") != label]
    save_mv_sources(sources)
    summary = await asyncio.to_thread(rebuild_unified_views)
    _invalidate_mv_caches()
    await asyncio.to_thread(
        append_refresh_history, "config", "config", note=f"Removed shared source '{label}'",
    )
    return {"ok": True, "sources": sources, "build": summary}


@router.post("/catalog", dependencies=[Depends(require_admin)])
async def save_catalog_settings(body: dict):
    """Save catalog/schema override from the Setup Wizard."""
    import asyncio as _asyncio

    from fastapi import HTTPException

    from backend.db import StorageConfigurationError, save_catalog_schema
    catalog = (body.get("catalog") or "").strip()
    schema = (body.get("schema") or "").strip()
    if not catalog or not schema:
        raise HTTPException(status_code=400, detail="catalog and schema are required")
    try:
        loop = _asyncio.get_running_loop()
        await loop.run_in_executor(None, save_catalog_schema, catalog, schema)
    except StorageConfigurationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"catalog": catalog, "schema": schema, "source": "override"}


@router.post("/refresh-mvs", dependencies=[Depends(require_admin)])
async def trigger_mv_refresh(background_tasks: BackgroundTasks, lookback_days: int = 180, force_full: bool = True):
    """Kick off an MV rebuild in the background and return immediately.

    lookback_days: how many days of history to include (default 180 = 6 months).
    force_full: when True (default for UI-triggered rebuilds), bypass incremental MERGE
                and always run full CREATE OR REPLACE for every table.
    """
    global _tables_cache, _tables_cache_ts
    from backend.app import _run_mv_refresh
    # Clear cache immediately so the next Status poll reflects fresh SQL results
    _tables_cache = None
    _tables_cache_ts = 0.0
    background_tasks.add_task(_run_mv_refresh, lookback_days=lookback_days, force_full=force_full)
    return {"status": "queued", "lookback_days": lookback_days, "force_full": force_full}


def _safe_identity_url(host: str, object_id: str) -> str:
    """Return the workspace SCIM identity URL, or an empty string if unsafe."""
    parsed = urlparse((host or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not object_id
    ):
        return ""
    clean_host = f"{parsed.scheme}://{parsed.netloc}"
    return (
        f"{clean_host}/api/2.0/preview/scim/v2/ServicePrincipals/"
        f"{quote(str(object_id), safe='')}"
    )


def _safe_resource_link(value: str) -> str:
    """Keep HTTPS resource links while dropping credentials and unsafe queries."""
    parsed = urlparse((value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return ""
    query = parsed.query
    if query and not (
        query.startswith("o=") and query.removeprefix("o=").isdigit()
    ):
        query = ""
    return parsed._replace(query=query, fragment="").geturl()


@router.get("/auth-status")
async def get_auth_status_endpoint():
    """Return current auth mode for the settings UI indicator."""
    import asyncio as _asyncio
    import os as _os

    from backend.db import get_auth_status, get_workspace_client
    status = get_auth_status()
    # Add SP identity and catalog/schema so the UI renders accurate GRANT SQL without placeholders
    def _fetch_me():
        return get_workspace_client().current_user.me()
    try:
        loop = _asyncio.get_running_loop()
        me = await _asyncio.wait_for(loop.run_in_executor(None, _fetch_me), timeout=4.0)
        # user_name is the SP's applicationId (its actual identity); display_name is the human label
        status["sp_user_name"] = me.user_name or ""
        status["sp_display_name"] = me.display_name or me.user_name or ""
        status["sp_client_id"] = _os.getenv("DATABRICKS_CLIENT_ID", me.user_name or "")
        status["sp_object_id"] = str(getattr(me, "id", "") or "")
    except Exception:
        status["sp_user_name"] = ""
        status["sp_display_name"] = ""
        status["sp_client_id"] = _os.getenv("DATABRICKS_CLIENT_ID", "")
        status["sp_object_id"] = ""
    host = (_os.getenv("DATABRICKS_HOST") or "").rstrip("/")
    status["sp_identity_url"] = _safe_identity_url(
        host, status["sp_object_id"]
    )
    status["effective_oauth_scopes"] = []
    status["oauth_scope_source"] = (
        "not_inspected" if status["sp_client_id"] else "unavailable"
    )
    try:
        from backend.db import get_catalog_schema
        cat, sch = get_catalog_schema()
        status["catalog"] = cat
        status["schema"] = sch
    except Exception:
        status["catalog"] = ""
        status["schema"] = ""
    return status


def _resource_connection_metadata(limit: int = 100) -> list[dict[str, Any]]:
    """Read only non-secret connection fields with a hard result bound."""
    try:
        rows = _load_connections()
    except Exception as exc:
        logger.debug("Resource connection metadata falling back to local state: %s", exc)
        rows = _load_connections_from_file()[:limit]
    return [
        {
            key: connection.get(key)
            for key in ("id", "name", "provider", "created_at")
            if connection.get(key) is not None
        }
        for connection in rows[:limit]
    ]


def _resource_shared_source_metadata(limit: int = 100) -> list[dict[str, Any]]:
    """Read safe shared-source metadata from the Lakebase/config registry."""
    try:
        from backend.db import get_mv_sources

        rows = get_mv_sources()
    except Exception as exc:
        logger.debug("Shared-source resource metadata is unavailable: %s", exc)
        return []

    sources = []
    for row in rows[:limit]:
        tables = row.get("tables")
        if isinstance(tables, str):
            try:
                tables = json.loads(tables)
            except (TypeError, json.JSONDecodeError):
                tables = None
        source = {
            key: row.get(key)
            for key in ("label", "catalog", "schema", "cloud", "added_at")
            if row.get(key) is not None
        }
        if isinstance(tables, list):
            source["tables"] = [str(table) for table in tables[:100]]
        workspace_ids = row.get("workspace_ids")
        if isinstance(workspace_ids, str):
            try:
                workspace_ids = json.loads(workspace_ids)
            except (TypeError, json.JSONDecodeError):
                workspace_ids = None
        if isinstance(workspace_ids, list):
            source["workspace_ids"] = [str(value) for value in workspace_ids[:100]]
        sources.append(source)
    return sources


def _resource_unified_views(limit: int = 100) -> list[str]:
    """Unified-view names from the Lakebase/config registry — no warehouse SHOW VIEWS."""
    try:
        from backend.db import get_unified_view_tables

        names = get_unified_view_tables()
        return [str(name) for name in names[:limit] if name]
    except Exception as exc:
        logger.debug("Unified-view resource metadata is unavailable: %s", exc)
        return []


_RESOURCES_SUBSECTION_TIMEOUT_SECONDS = 3.0
_RESOURCES_ENDPOINT_DEADLINE_SECONDS = 9.0


def _resource_subsection_status(available: bool) -> dict[str, Any]:
    if available:
        return {"available": True}
    return {"available": False, "reason": "temporarily_unavailable"}


def _call_resource_operation(name: str, operation):
    """Run a blocking probe on a daemon thread so timeouts do not pin the executor."""
    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            result_box["value"] = operation()
        except BaseException as exc:  # noqa: BLE001 — probe must not kill the waiter
            error_box["exc"] = exc
        finally:
            done.set()

    threading.Thread(target=_target, name=f"settings-res-{name}", daemon=True).start()
    if not done.wait(_RESOURCES_SUBSECTION_TIMEOUT_SECONDS):
        raise TimeoutError(f"{name} timed out")
    if "exc" in error_box:
        raise error_box["exc"]
    return result_box.get("value")


async def _bounded_resource_call(
    name: str,
    awaitable,
    fallback: Any,
) -> tuple[Any, dict[str, Any]]:
    try:
        value = await asyncio.wait_for(
            awaitable,
            timeout=_RESOURCES_SUBSECTION_TIMEOUT_SECONDS,
        )
        return value, _resource_subsection_status(True)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "Settings Resources subsection %s timed out after %.1fs",
            name,
            _RESOURCES_SUBSECTION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Settings Resources subsection %s failed: %s", name, exc)
    return fallback, _resource_subsection_status(False)


async def _bounded_resource_thread(
    name: str,
    operation,
    fallback: Any,
) -> tuple[Any, dict[str, Any]]:
    return await _bounded_resource_call(
        name,
        asyncio.to_thread(_call_resource_operation, name, operation),
        fallback,
    )


async def _bounded_async_resource(
    name: str,
    operation,
    fallback: Any,
) -> tuple[Any, dict[str, Any]]:
    """Isolate async handlers too, because some contain blocking SDK/file work."""
    return await _bounded_resource_thread(
        name,
        lambda: asyncio.run(operation()),
        fallback,
    )


def _resource_inventory_from_parts(
    sources: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    routed_views: list[str],
    workspace_ids: list[str],
) -> dict[str, Any]:
    from backend import db
    from backend.materialized_views import _MV_TABLES

    process_cache_entries = len(db._query_cache)

    observed_tables = None
    if (
        _tables_cache is not None
        and (time.time() - _tables_cache_ts) < _TABLES_CACHE_TTL
    ):
        observed_tables = {
            "checked_at": datetime.fromtimestamp(
                _tables_cache_ts, timezone.utc
            ).isoformat(),
            "available": sum(
                1 for table in _tables_cache.get("tables", [])
                if table.get("exists") is True
            ),
            "total": len(_tables_cache.get("tables", [])),
        }

    return {
        "inventory": {
            "aggregates": {
                "count": len(_MV_TABLES),
                "names": list(_MV_TABLES),
            },
            "state": {
                "count": len(_APP_STATE_TABLES),
                "names": list(_APP_STATE_TABLES),
            },
            "cache": {
                "count": 2,
                "names": ["in-process query cache", _APP_RESPONSE_CACHE_TABLE],
                "process_entries": process_cache_entries,
                "process_max_entries": db._CACHE_MAX_SIZE,
                "process_ttl_seconds": db._CACHE_TTL,
            },
            "unified_views": {
                "count": len(routed_views),
                "names": list(routed_views),
            },
            "observed_tables": observed_tables,
        },
        "shared_data_sources": sources,
        "cloud_cost_connections": connections,
        "workspace_filter": {
            "mode": "restricted" if workspace_ids else "all_workspaces",
            "count": len(workspace_ids),
        },
    }


def _resource_inventory_snapshot() -> dict[str, Any]:
    """Collect bounded resource metadata without scanning managed data."""
    from backend.workspace_filter import get_configured_workspace_ids

    return _resource_inventory_from_parts(
        _resource_shared_source_metadata(),
        _resource_connection_metadata(),
        _resource_unified_views(),
        get_configured_workspace_ids(),
    )


async def _resource_inventory_snapshot_bounded() -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    """Run independent SQL/config inventory probes concurrently and fail open."""
    from backend.workspace_filter import get_configured_workspace_ids

    (
        (sources, sources_status),
        (connections, connections_status),
        (views, views_status),
        (workspace_ids, workspace_status),
    ) = await asyncio.gather(
        _bounded_resource_thread(
            "shared_data_sources",
            _resource_shared_source_metadata,
            [],
        ),
        _bounded_resource_thread(
            "cloud_cost_connections",
            _resource_connection_metadata,
            [],
        ),
        _bounded_resource_thread(
            "unified_views",
            _resource_unified_views,
            [],
        ),
        _bounded_resource_thread(
            "workspace_filter",
            get_configured_workspace_ids,
            [],
        ),
    )
    return (
        _resource_inventory_from_parts(
            sources,
            connections,
            views,
            workspace_ids,
        ),
        {
            "shared_data_sources": sources_status,
            "cloud_cost_connections": connections_status,
            "unified_views": views_status,
            "workspace_filter": workspace_status,
        },
    )


def _refresh_log_status_for_resources() -> dict | None:
    """Best-effort refresh-log status from Lakebase, then the local file."""
    log_data = None
    try:
        from backend.db import lb_get_config

        cfg = lb_get_config("refresh_log")
        if isinstance(cfg, dict) and cfg:
            log_data = cfg
    except Exception as exc:
        logger.debug("Resource refresh-log Lakebase read failed: %s", exc)
    if log_data is None:
        log_path = os.path.join(
            os.path.dirname(__file__), "..", "..", ".settings", "mv_refresh_log.json"
        )
        try:
            with open(log_path) as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and loaded:
                log_data = loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
    if not isinstance(log_data, dict):
        return None
    last_refresh = log_data.get("last_refresh_utc")
    hours_since = None
    if last_refresh:
        try:
            last = datetime.strptime(last_refresh, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            hours_since = round((datetime.now(timezone.utc) - last).total_seconds() / 3600, 1)
        except (TypeError, ValueError):
            hours_since = None
    result: dict[str, Any] = {
        "last_refresh_utc": last_refresh,
        "last_attempt_utc": log_data.get("last_attempt_utc"),
        "hours_since_refresh": hours_since,
        "stale": hours_since is None or hours_since > 26,
        "status": log_data.get("status"),
        "refresh_history": log_data.get("refresh_history", []),
    }
    return result


def _resource_refresh_snapshot() -> dict[str, Any]:
    return {
        "schedule": load_schedule_settings(),
        "status": _refresh_log_status_for_resources(),
    }


def _deployment_metadata_from_process_start() -> dict[str, Any]:
    from backend.routers.health import _deployment_metadata_from_process_start as health_fallback

    return health_fallback()


def _lakebase_resource_payload(app_link: str = "") -> dict[str, Any]:
    from backend.routers.health import lakebase_instance_name, lakebase_page_url

    host = ""
    parsed = urlparse((app_link or "").strip())
    if parsed.scheme == "https" and parsed.netloc:
        host = f"{parsed.scheme}://{parsed.netloc}"
    name = lakebase_instance_name()
    page_url = _safe_resource_link(lakebase_page_url(host) or "")
    return {
        "name": name or "",
        "endpoint": os.getenv("ENDPOINT_NAME") or "",
        "configured": bool(os.getenv("PGHOST")),
        "database": os.getenv("PGDATABASE") or "",
        "page_url": page_url,
    }


@router.get("/resources")
async def get_resources() -> dict[str, Any]:
    """Return partial safe metadata within a hard Settings deadline."""
    from backend.routers.health import deployment_metadata

    empty_config = {
        "storage_location": {},
        "warehouse": None,
        "version": {},
    }
    empty_links = {
        "app_name": os.getenv("DATABRICKS_APP_NAME", ""),
        "app_url": "",
        "app_page_url": "",
        "source_code_url": "",
    }
    empty_refresh = {
        "schedule": {
            "enabled": False,
            "frequency": "nightly",
            "hour_utc": 5,
            "lookback_days": 180,
        },
        "status": None,
    }
    calls = asyncio.gather(
        _bounded_async_resource("config", get_app_config, empty_config),
        _bounded_async_resource(
            "service_principal",
            get_auth_status_endpoint,
            {},
        ),
        _bounded_async_resource("app_links", get_app_links, empty_links),
        _bounded_async_resource(
            "deployment",
            deployment_metadata,
            _deployment_metadata_from_process_start(),
        ),
        _resource_inventory_snapshot_bounded(),
        _bounded_resource_thread(
            "refresh",
            _resource_refresh_snapshot,
            empty_refresh,
        ),
    )
    try:
        (
            (config, config_status),
            (auth_status, auth_status_status),
            (links, links_status),
            (deployment, deployment_status),
            (inventory, inventory_statuses),
            (refresh, refresh_status),
        ) = await asyncio.wait_for(
            calls,
            timeout=_RESOURCES_ENDPOINT_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(
            "Settings Resources exceeded its %.1fs hard deadline",
            _RESOURCES_ENDPOINT_DEADLINE_SECONDS,
        )
        config, config_status = empty_config, _resource_subsection_status(False)
        auth_status, auth_status_status = {}, _resource_subsection_status(False)
        links, links_status = empty_links, _resource_subsection_status(False)
        deployment = _deployment_metadata_from_process_start()
        deployment_status = _resource_subsection_status(False)
        inventory = _resource_inventory_from_parts([], [], [], [])
        inventory_statuses = {
            name: _resource_subsection_status(False)
            for name in (
                "shared_data_sources",
                "cloud_cost_connections",
                "unified_views",
                "workspace_filter",
            )
        }
        refresh, refresh_status = empty_refresh, _resource_subsection_status(False)

    storage = config.get("storage_location") or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subsections": {
            "config": config_status,
            "service_principal": auth_status_status,
            "app_links": links_status,
            "deployment": deployment_status,
            **inventory_statuses,
            "refresh": refresh_status,
        },
        "app": {
            "name": links.get("app_name") or os.getenv("DATABRICKS_APP_NAME", ""),
            "url": _safe_resource_link(links.get("app_url") or ""),
            "page_url": _safe_resource_link(links.get("app_page_url") or ""),
            "source_code_url": _safe_resource_link(
                links.get("source_code_url") or ""
            ),
            "deployment": deployment,
            "version": {
                key: (config.get("version") or {}).get(key)
                for key in ("commit_sha", "branch", "commit_date")
                if (config.get("version") or {}).get(key)
            },
        },
        "service_principal": {
            "display_name": auth_status.get("sp_display_name") or "",
            "client_id": auth_status.get("sp_client_id") or "",
            "object_id": auth_status.get("sp_object_id") or "",
            "user_name": auth_status.get("sp_user_name") or "",
            "identity_url": auth_status.get("sp_identity_url") or "",
            "execution_identity": "service_principal",
            "execution_explanation": (
                "Dashboard queries and managed-data maintenance run as this "
                "Databricks Apps service principal, not as an interactive user."
            ),
            "effective_oauth_scopes": auth_status.get(
                "effective_oauth_scopes", []
            ),
            "oauth_scope_source": auth_status.get("oauth_scope_source"),
        },
        "warehouse": config.get("warehouse"),
        "lakebase": _lakebase_resource_payload(
            (links.get("app_page_url") or links.get("app_url") or "")
        ),
        "storage": {
            "catalog": storage.get("catalog") or "",
            "schema": storage.get("schema") or "",
            "permissions_table": (
                f"{storage.get('catalog')}.{storage.get('schema')}."
                "app_user_permissions"
                if storage.get("catalog") and storage.get("schema")
                else ""
            ),
        },
        "refresh": refresh,
        **inventory,
    }


@router.get("/billing-access")
async def check_billing_access():
    """Test whether the SP can read system.billing.usage.

    Always runs as the service principal (clears user token) so the result
    reflects SP grants, not the current user's OAuth permissions.
    Used by the frontend to detect missing post-deploy SP grants.

    Returns reason: "warehouse_access" when the SP can't use the SQL endpoint,
    "grants_missing" when UC table privileges are missing.
    Includes warehouse_id so the frontend can show the exact grant command.
    """
    import os as _os

    from backend.db import _user_token, execute_query
    tok = _user_token.set("")
    try:
        await asyncio.to_thread(execute_query, "SELECT 1 FROM system.billing.usage LIMIT 1", no_cache=True)
        return {"ok": True}
    except Exception as e:
        err = str(e)
        http_path = _os.environ.get("DATABRICKS_HTTP_PATH", "")
        warehouse_id = http_path.rstrip("/").split("/")[-1] if "/" in http_path else ""
        sp_client_id = _os.environ.get("DATABRICKS_CLIENT_ID", "")
        # Warehouse CAN_USE failure — distinct from UC table grant failure
        if "not authorized to use this sql endpoint" in err.lower() or (
            "permission_denied" in err.lower() and "sql endpoint" in err.lower()
        ):
            return {
                "ok": False,
                "reason": "warehouse_access",
                "warehouse_id": warehouse_id,
                "sp_client_id": sp_client_id,
            }
        if any(s in err.lower() for s in ("permission_denied", "insufficient_privileges", "not authorized", "user does not have")):
            return {"ok": False, "reason": "grants_missing", "sp_client_id": sp_client_id}
        return {"ok": False, "reason": "error", "error": err[:200]}
    finally:
        _user_token.reset(tok)


class AuthModeRequest(BaseModel):
    mode: str  # "sp" only — OAuth disabled


@router.post("/auth-mode", dependencies=[Depends(require_admin)])
async def set_auth_mode(body: AuthModeRequest):
    """Auth mode endpoint — OAuth is currently disabled.

    Only 'sp' is accepted. 'auto' is rejected until OAuth is re-enabled.
    """
    if body.mode == "auto":
        raise HTTPException(
            status_code=422,
            detail="OAuth / auto-detect mode is disabled. The app runs exclusively as the service principal."
        )
    if body.mode != "sp":
        raise HTTPException(status_code=422, detail="mode must be 'sp'")
    # set_auth_mode_override is a no-op when already SP, but call for log visibility
    from backend.db import set_auth_mode_override
    set_auth_mode_override(body.mode)
    return {"status": "ok", "mode": "sp"}


@router.get("/warehouses")
async def list_warehouses():
    """List all SQL warehouses the user has access to."""
    from backend.db import get_user_workspace_client

    current_http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    current_id = current_http_path.split("/")[-1] if current_http_path else None

    try:
        w = get_user_workspace_client()
        warehouses = list(w.warehouses.list())

        result = []
        for wh in warehouses:
            state = str(wh.state.value) if wh.state else "UNKNOWN"
            result.append({
                "id": wh.id,
                "name": wh.name,
                "size": wh.cluster_size,
                "state": state,
                "is_current": wh.id == current_id,
            })

        # If the user token returned nothing, merge in warehouses visible to the SP.
        # Common during first-time setup: the admin granted CAN_USE only to the SP
        # (not to the user personally), so the user's OAuth token sees an empty list
        # even though the app will work fine once a warehouse is selected.
        if not result:
            from backend.db import get_workspace_client as _get_sp_client
            try:
                sp_warehouses = list(_get_sp_client().warehouses.list())
                for wh in sp_warehouses:
                    state = str(wh.state.value) if wh.state else "UNKNOWN"
                    result.append({
                        "id": wh.id,
                        "name": wh.name,
                        "size": wh.cluster_size,
                        "state": state,
                        "is_current": wh.id == current_id,
                    })
                if result:
                    logger.info(f"User token saw 0 warehouses; SP token found {len(result)} — using SP list for setup")
            except Exception as sp_err:
                logger.warning(f"SP warehouse list fallback also failed: {sp_err}")

        # If the currently configured warehouse isn't in the list (token visibility gap),
        # try fetching it directly — first with the user token, then fall back to the
        # SP M2M client (handles cases where forwarded OAuth token has narrower scope).
        if current_id and not any(r["id"] == current_id for r in result):
            from backend.db import get_workspace_client as _get_sp_client
            wh_info = None
            for label, client in [("user", w), ("sp", _get_sp_client())]:
                try:
                    wh = client.warehouses.get(current_id)
                    state = str(wh.state.value) if wh.state else "UNKNOWN"
                    wh_info = {"id": wh.id, "name": wh.name, "size": wh.cluster_size, "state": state, "is_current": True}
                    break
                except Exception as e2:
                    logger.warning(f"Could not fetch warehouse {current_id} ({label} token): {e2}")
            result.insert(0, wh_info or {"id": current_id, "name": None, "size": None, "state": "UNKNOWN", "is_current": True})

        # Sort: current first, then running, then by name
        result.sort(key=lambda x: (not x["is_current"], x["state"] != "RUNNING", x["name"] or ""))
        return result
    except Exception as e:
        logger.error(f"Failed to list warehouses: {e}")
        # User token raised an exception (e.g. OAuth token lacks all-apis scope).
        # Try the SP M2M client for listing — covers first-time setup where no
        # warehouse is configured yet (current_id is None).
        from backend.db import get_workspace_client as _sp
        try:
            sp_client = _sp()
            sp_whs = list(sp_client.warehouses.list())
            if sp_whs:
                logger.info(f"User token warehouses.list() failed; SP found {len(sp_whs)} warehouse(s)")
                sp_result = []
                for wh in sp_whs:
                    state = str(wh.state.value) if wh.state else "UNKNOWN"
                    sp_result.append({"id": wh.id, "name": wh.name, "size": wh.cluster_size, "state": state, "is_current": wh.id == current_id})
                sp_result.sort(key=lambda x: (not x["is_current"], x["state"] != "RUNNING", x["name"] or ""))
                return sp_result
        except Exception as sp_err:
            logger.warning(f"SP warehouses.list() fallback also failed: {sp_err}")
        # Both failed — last resort: return the currently configured warehouse by ID
        if current_id:
            try:
                wh = _sp().warehouses.get(current_id)
                state = str(wh.state.value) if wh.state else "STOPPED"
                return [{"id": wh.id, "name": wh.name, "size": wh.cluster_size, "state": state, "is_current": True}]
            except Exception as e2:
                logger.warning(f"SP warehouses.get fallback also failed: {e2}")
                return [{"id": current_id, "name": None, "size": None, "state": "UNKNOWN", "is_current": True}]
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cloud-provider")
async def get_cloud_provider():
    """Detect the base cloud provider from the Databricks workspace host URL."""
    from backend.db import get_workspace_client

    host = os.getenv("DATABRICKS_HOST", "")
    # Try getting host from workspace client if env var is empty
    if not host:
        try:
            w = get_workspace_client()
            host = w.config.host or ""
        except Exception:
            pass

    host = host.lower()
    if ".azuredatabricks.net" in host or "adb-" in host:
        provider = "azure"
    elif ".gcp.databricks.com" in host:
        provider = "gcp"
    else:
        # Default to AWS (.cloud.databricks.com and others)
        provider = "aws"

    return {"provider": provider, "host": host}


@router.get("/cloud-connections")
async def list_cloud_connections():
    """List all cloud connections (secrets are masked)."""
    connections = _load_connections()
    return [_mask_connection(c) for c in connections]


# Keep legacy endpoint for backward compatibility
@router.get("/azure-connections")
async def list_azure_connections():
    """List Azure connections (legacy endpoint, returns all connections)."""
    connections = _load_connections()
    return [_mask_connection(c) for c in connections]


@router.post("/cloud-connections")
async def create_cloud_connection(request: Request, conn: CloudConnectionCreate):
    """Create a new cloud connection."""
    _require_admin(request)
    if conn.provider not in ("azure", "aws", "gcp"):
        raise HTTPException(status_code=400, detail="Invalid provider. Must be azure, aws, or gcp.")

    connections = _load_connections()

    new_conn = {
        "id": str(uuid.uuid4())[:8],
        "name": conn.name,
        "provider": conn.provider,
        "created_at": datetime.utcnow().isoformat(),
    }

    if conn.provider == "azure":
        new_conn.update({
            "tenant_id": conn.tenant_id,
            "subscription_id": conn.subscription_id,
            "client_id": conn.client_id,
            "client_secret": conn.client_secret,
        })
    elif conn.provider == "aws":
        new_conn.update({
            "aws_account_id": conn.aws_account_id,
            "access_key_id": conn.access_key_id,
            "secret_access_key": conn.secret_access_key,
            "region": conn.region,
        })
    elif conn.provider == "gcp":
        new_conn.update({
            "project_id": conn.project_id,
            "service_account_key": conn.service_account_key,
        })

    connections.append(new_conn)
    try:
        _upsert_connection_to_table(new_conn)
    except Exception as e:
        logger.warning(f"Could not save connection to Delta table: {e}")
    _save_connections_to_file(connections)

    logger.info(f"Created {conn.provider.upper()} connection: {conn.name}")

    return _mask_connection(new_conn)


# Keep legacy endpoint for backward compatibility
@router.post("/azure-connections", dependencies=[Depends(require_admin)])
async def create_azure_connection(conn: CloudConnectionCreate):
    """Create an Azure connection (legacy endpoint)."""
    conn.provider = "azure"
    return await create_cloud_connection(conn)


@router.delete("/cloud-connections/{connection_id}")
async def delete_cloud_connection(request: Request, connection_id: str):
    """Delete a cloud connection."""
    _require_admin(request)
    connections = _load_connections()
    original_count = len(connections)
    connections = [c for c in connections if c.get("id") != connection_id]

    if len(connections) == original_count:
        raise HTTPException(status_code=404, detail="Connection not found")

    try:
        _delete_connection_from_table(connection_id)
    except Exception as e:
        logger.warning(f"Could not delete connection from Delta table: {e}")
    _save_connections_to_file(connections)
    logger.info(f"Deleted cloud connection: {connection_id}")
    return {"status": "deleted", "id": connection_id}


# Keep legacy endpoint for backward compatibility
@router.delete("/azure-connections/{connection_id}", dependencies=[Depends(require_admin)])
async def delete_azure_connection(connection_id: str):
    """Delete an Azure connection (legacy endpoint)."""
    return await delete_cloud_connection(connection_id)


# ── Webhook Settings ─────────────────────────────────────────────────────

class WebhookSettings(BaseModel):
    slack_webhook_url: str = ""


def _load_webhook_settings() -> dict:
    """Load webhook settings — Lakebase first, then UC Delta, then local file."""
    try:
        from backend.db import lb_get_config
        cfg = lb_get_config("webhook_settings")
        if isinstance(cfg, dict) and cfg.get("slack_webhook_url"):
            return {"slack_webhook_url": cfg.get("slack_webhook_url") or ""}
    except Exception as e:
        logger.debug("Lakebase webhook read failed, will try UC: %s", e)
    try:
        from backend.db import execute_query
        table = _config_table("app_webhook_settings")
        rows = execute_query(f"SELECT * FROM {table} LIMIT 1", None, no_cache=True)
        if rows:
            return {"slack_webhook_url": rows[0].get("slack_webhook_url") or ""}
    except Exception as e:
        if _table_missing(e):
            logger.debug("Could not load webhook settings from Delta table (not yet created): %s", e)
        else:
            logger.warning(f"Could not load webhook settings from Delta table: {e}")

    # Fallback: file
    if os.path.exists(WEBHOOK_SETTINGS_FILE):
        try:
            with open(WEBHOOK_SETTINGS_FILE) as f:
                data = json.load(f)
            if data.get("slack_webhook_url"):
                try:
                    _save_webhook_to_table(data)
                    logger.info("Migrated webhook settings from file to Delta table")
                except Exception as e:
                    logger.warning(f"Could not migrate webhook settings to Delta: {e}")
            return data
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_webhook_to_table(settings: dict) -> None:
    """Persist webhook settings. Lakebase app_config primary; UC Delta dual-write."""
    try:
        from backend.db import lakebase_available, lb_set_config
        if lakebase_available():
            lb_set_config("webhook_settings", {"slack_webhook_url": settings.get("slack_webhook_url") or ""})
    except Exception as e:
        logger.warning("Could not save webhook settings to Lakebase (non-fatal): %s", e)
    try:
        from backend.db import execute_write
        _ensure_webhook_table()
        table = _config_table("app_webhook_settings")
        execute_write(f"DELETE FROM {table}", None)
        execute_write(
            f"INSERT INTO {table} (slack_webhook_url, updated_at) "
            f"VALUES (:url, current_timestamp())",
            {"url": settings.get("slack_webhook_url") or ""},
        )
    except Exception as e:
        logger.debug("UC dual-write of webhook settings skipped/failed (non-fatal): %s", e)


def _save_webhook_settings(settings: dict) -> None:
    """Save webhook settings to Delta table (primary) and file (dev fallback)."""
    try:
        _save_webhook_to_table(settings)
    except Exception as e:
        logger.warning(f"Could not save webhook settings to Delta table: {e}")
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(WEBHOOK_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


@router.get("/webhook")
async def get_webhook_settings() -> dict[str, Any]:
    """Get current webhook settings."""
    settings = _load_webhook_settings()
    # Mask the URL for security
    url = settings.get("slack_webhook_url", "")
    masked = ""
    if url:
        # Only show scheme+host to confirm it's configured without exposing path tokens
        masked = "https://hooks.slack.com/services/****" if "hooks.slack.com" in url else "****"
    return {"slack_webhook_url": masked, "configured": bool(url)}


@router.post("/webhook")
async def save_webhook_settings(request: Request, settings: WebhookSettings) -> dict[str, Any]:
    """Save webhook settings."""
    _require_admin(request)
    _save_webhook_settings({"slack_webhook_url": settings.slack_webhook_url})
    logger.info("Webhook settings updated")
    return {"status": "saved"}


@router.post("/webhook/test")
async def test_webhook(request: Request) -> dict[str, Any]:
    """Send a test message to the configured Slack webhook."""
    _require_admin(request)
    settings = _load_webhook_settings()
    url = settings.get("slack_webhook_url", "")
    if not url:
        return {"success": False, "error": "No webhook URL configured"}

    payload = {
        "text": "Cost Observability & Control - Test notification. Your webhook is working!"
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return {"success": True, "message": "Test message sent"}
            return {"success": False, "error": f"Slack returned status {resp.status_code}"}
    except Exception as e:
        logger.error(f"Webhook test failed: {e}")
        return {"success": False, "error": str(e)}


@router.post("/webhook/send-alert", dependencies=[Depends(require_admin)])
async def send_webhook_alert(alert_data: dict[str, Any]) -> dict[str, Any]:
    """Send an alert notification to the configured Slack webhook."""
    settings = _load_webhook_settings()
    url = settings.get("slack_webhook_url", "")
    if not url:
        return {"success": False, "error": "No webhook URL configured"}

    # Format alert message
    alert_type = alert_data.get("alert_type", "alert")
    usage_date = alert_data.get("usage_date", "unknown")
    daily_spend = alert_data.get("daily_spend", 0)
    change_pct = alert_data.get("change_percent", 0)

    text = (
        f":rotating_light: *Cost Alert: {alert_type.title()}*\n"
        f"Date: {usage_date}\n"
        f"Daily Spend: ${daily_spend:,.2f}\n"
    )
    if change_pct:
        text += f"Change: {change_pct:+.1f}%\n"

    payload = {"text": text}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return {"success": True}
            return {"success": False, "error": f"Slack returned status {resp.status_code}"}
    except Exception as e:
        logger.error(f"Webhook alert failed: {e}")
        return {"success": False, "error": str(e)}


@router.post("/alerts/run")
async def run_alerts(request: Request, send: bool = True) -> dict[str, Any]:
    """Evaluate the configured cost-alert thresholds against the latest spend and,
    when a Slack webhook is set and `send`, post any breaches. Admin-only. This is the
    manual trigger for the same check the nightly scheduler runs."""
    _require_admin(request)
    from backend.alerting import run_alert_check
    return await asyncio.to_thread(run_alert_check, send)


# ── User Permissions ──────────────────────────────────────────────────────────

class UserPermissionsModel(BaseModel):
    admins: list[str] = []
    consumers: list[str] = []


def _permissions_table() -> str:
    """Return the fully-qualified Delta table name for user permissions."""
    from backend.db import get_catalog_schema
    catalog, schema = get_catalog_schema()
    return f"`{catalog}`.`{schema}`.`app_user_permissions`"


def _ensure_permissions_table() -> None:
    """Create the permissions table if it doesn't exist."""
    from backend.db import execute_write
    table = _permissions_table()
    execute_write(
        f"CREATE TABLE IF NOT EXISTS {table} "
        f"(role STRING NOT NULL, email STRING NOT NULL, "
        f"updated_at TIMESTAMP) "
        f"USING DELTA",
        None,
    )


def _permissions_from_role_rows(rows: list[dict], email_key: str) -> dict:
    """Build admins/consumers/owner from stored role rows. Owner is also an admin."""
    owner_emails = [
        str(row.get(email_key) or "").strip()
        for row in rows
        if row.get("role") == "owner" and str(row.get(email_key) or "").strip()
    ]
    admin_emails = [
        str(row.get(email_key) or "").strip()
        for row in rows
        if row.get("role") == "admin" and str(row.get(email_key) or "").strip()
    ]
    consumer_emails = [
        str(row.get(email_key) or "").strip()
        for row in rows
        if row.get("role") == "consumer" and str(row.get(email_key) or "").strip()
    ]
    admins = list(dict.fromkeys([*owner_emails, *admin_emails]))
    consumers = [email for email in dict.fromkeys(consumer_emails) if email not in admins]
    result: dict[str, Any] = {"admins": admins, "consumers": consumers}
    if owner_emails:
        result["owner"] = owner_emails[0]
    return result


def _normalized_permission_rows(
    admins: list[str],
    consumers: list[str],
    *,
    owner: str | None = None,
) -> tuple[list[tuple[str, str]], list[str], list[str], str]:
    normalized_admins = list(dict.fromkeys(
        email.strip().lower() for email in admins if email.strip()
    ))
    if not normalized_admins:
        raise ValueError("At least one administrator is required.")
    normalized_consumers = [
        email
        for email in dict.fromkeys(
            item.strip().lower() for item in consumers if item.strip()
        )
        if email not in normalized_admins
    ]
    normalized_owner = (owner or "").strip().lower()
    if normalized_owner not in normalized_admins:
        normalized_owner = normalized_admins[0]
    rows = [("owner", normalized_owner)]
    rows.extend(
        ("admin", email) for email in normalized_admins
        if email != normalized_owner
    )
    rows.extend(("consumer", email) for email in normalized_consumers)
    return rows, normalized_admins, normalized_consumers, normalized_owner


def _load_user_permissions() -> dict:
    """Load permissions — Lakebase first, then UC Delta table, then local file.

    Sets "unavailable" when the store exists but couldn't be read, so callers don't
    mistake an outage for a fresh install with no admins.
    """
    from backend.routers.user import _permission_store_missing

    read_failed = False
    # Primary: Lakebase user_permissions
    try:
        from backend.db import lakebase_available, lakebase_query
        if lakebase_available():
            read_failed = True
            rows = lakebase_query(
                "SELECT subject_id, role FROM user_permissions WHERE subject_type = 'user'"
            )
            read_failed = False
            perms = _permissions_from_role_rows(rows or [], "subject_id")
            if perms["admins"] or perms["consumers"]:
                return perms
    except Exception as e:
        if _permission_store_missing(e):
            read_failed = False
        logger.debug("Lakebase permissions read failed, will try UC: %s", e)
    try:
        from backend.db import execute_query
        _ensure_permissions_table()
        table = _permissions_table()
        rows = execute_query(f"SELECT role, email FROM {table}", None, no_cache=True)
        perms = _permissions_from_role_rows(rows or [], "email")
        if perms["admins"] or perms["consumers"]:
            logger.info(
                "Loaded permissions from Delta table (%d admins, %d consumers)",
                len(perms["admins"]),
                len(perms["consumers"]),
            )
            return perms
    except Exception as e:
        if _table_missing(e) or _permission_store_missing(e):
            logger.debug("Could not load permissions from Delta table (not yet created): %s", e)
        else:
            logger.warning(f"Could not load permissions from Delta table: {e}")
            read_failed = True

    # Fallback: local file (ephemeral — only useful in dev)
    try:
        if os.path.exists(USER_PERMISSIONS_FILE):
            with open(USER_PERMISSIONS_FILE) as f:
                data = json.load(f)
            result = {
                "admins": data.get("admins", []),
                "consumers": data.get("consumers", []),
            }
            if data.get("owner"):
                result["owner"] = data["owner"]
            return result
    except (json.JSONDecodeError, IOError):
        pass
    if read_failed:
        return {"admins": [], "consumers": [], "unavailable": True}
    return {"admins": [], "consumers": []}


def _save_user_permissions_to_table(
    admins: list[str],
    consumers: list[str],
    *,
    owner: str | None = None,
) -> None:
    """Write permissions (replaces all user rows). Lakebase primary; UC dual-write.

    Security-sensitive: if NEITHER store succeeds we raise, so an admin change
    is never silently lost. Owner is persisted as role='owner' when supplied
    (or as the first admin when omitted).
    """
    from backend.db import clear_query_cache, execute_write, lakebase_available, lakebase_execute
    pairs, normalized_admins, normalized_consumers, _normalized_owner = (
        _normalized_permission_rows(admins, consumers, owner=owner)
    )

    # Primary: Lakebase user_permissions (subject_id = email, subject_type = 'user')
    lb_ok = False
    if lakebase_available():
        try:
            lakebase_execute("DELETE FROM user_permissions WHERE subject_type = 'user'")
            for role, email in pairs:
                lakebase_execute(
                    "INSERT INTO user_permissions (subject_id, subject_type, role) "
                    "VALUES (%s, 'user', %s) "
                    "ON CONFLICT (subject_id) DO UPDATE "
                    "  SET role = EXCLUDED.role, subject_type = 'user', granted_at = now()",
                    (email, role),
                )
            lb_ok = True
        except Exception as e:
            logger.warning("Could not save permissions to Lakebase: %s", e)

    # UC Delta: best-effort dual-write when Lakebase succeeded, otherwise the
    # primary store (propagate errors so a total failure surfaces).
    try:
        _ensure_permissions_table()
        table = _permissions_table()
        execute_write(f"DELETE FROM {table}", None)
        for role, email in pairs:
            execute_write(
                f"INSERT INTO {table} (role, email) VALUES (:role, :email)",
                {"role": role, "email": email},
            )
    except Exception as e:
        if lb_ok:
            logger.debug("UC dual-write of permissions failed (non-fatal, Lakebase is primary): %s", e)
        else:
            raise
    clear_query_cache("perms")
    logger.info(
        "Saved user permissions (%d admins, %d consumers; lakebase=%s)",
        len(normalized_admins),
        len(normalized_consumers),
        lb_ok,
    )


async def _permission_owner(perms: dict[str, Any]) -> dict[str, Any]:
    """Resolve the UI persona owner without changing the authorization policy."""
    admins = [
        str(email).strip().lower()
        for email in perms.get("admins", [])
        if str(email).strip()
    ]
    durable_owner = str(perms.get("owner") or "").strip().lower()
    deployment: dict[str, Any] = {}
    try:
        from backend.routers.health import deployment_metadata

        deployment = await deployment_metadata()
    except Exception as exc:
        logger.debug("Deployment creator unavailable for permission personas: %s", exc)
    deployment_creator = str(deployment.get("deployer") or "").strip().lower()

    if deployment_creator and deployment_creator in admins:
        return {
            "email": deployment_creator,
            "source": deployment.get("source") or "deployment_metadata",
            "verified": deployment.get("source") == "databricks_apps_api",
            "deployment_creator": deployment_creator,
        }
    if durable_owner and durable_owner in admins:
        return {
            "email": durable_owner,
            "source": "permission_store",
            "verified": False,
            "deployment_creator": deployment_creator or None,
        }
    return {
        "email": admins[0] if admins else None,
        "source": "first_configured_admin" if admins else "unavailable",
        "verified": False,
        "deployment_creator": deployment_creator or None,
    }


@router.get("/user-permissions")
async def get_user_permissions(request: Request) -> dict:
    """Return the admin and consumer user lists."""
    perms = _load_user_permissions()
    try:
        from backend.db import get_catalog_schema
        catalog, schema = get_catalog_schema()
        perms["table_location"] = f"{catalog}.{schema}.app_user_permissions"
    except Exception:
        perms["table_location"] = None
    current_user = request.headers.get("X-Forwarded-Email", os.getenv("USER", "dev@local"))
    admins = {str(email).strip().lower() for email in perms.get("admins", [])}
    consumers = {str(email).strip().lower() for email in perms.get("consumers", [])}
    needle = str(current_user).strip().lower()
    if needle in admins:
        current_role = "admin"
    elif needle in consumers:
        current_role = "consumer"
    elif not admins:
        # Fresh install: no admins configured yet — same rule as /api/user/me.
        current_role = "admin"
    else:
        current_role = "consumer"
    from backend.routers.user import ROLE_CAPABILITIES

    perms["current_user"] = current_user
    perms["current_role"] = current_role
    perms["role_capabilities"] = {
        role: dict(capabilities)
        for role, capabilities in ROLE_CAPABILITIES.items()
    }
    perms["owner"] = await _permission_owner(perms)
    return perms


@router.post("/user-permissions")
async def save_user_permissions(request: Request, data: UserPermissionsModel) -> dict:
    """Save permissions to Delta table."""
    _require_admin(request)
    normalized_admins = list(dict.fromkeys(
        email.strip() for email in data.admins if email.strip()
    ))
    normalized_consumers = list(dict.fromkeys(
        email.strip() for email in data.consumers
        if email.strip() and email.strip() not in normalized_admins
    ))
    if not normalized_admins:
        raise HTTPException(
            status_code=400,
            detail="At least one explicit admin must remain once permissions are configured.",
        )
    try:
        current_permissions = _load_user_permissions()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Permission state is temporarily unavailable",
        ) from exc
    if current_permissions.get("unavailable"):
        raise HTTPException(status_code=503, detail="Permission state is temporarily unavailable")
    owner = await _permission_owner(current_permissions)
    owner_email = str(owner.get("email") or "").strip().lower()
    if owner_email and owner_email not in {
        email.lower() for email in normalized_admins
    }:
        raise HTTPException(
            status_code=400,
            detail="The Owner must remain an administrator.",
        )
    try:
        from backend.db import get_catalog_schema
        catalog, schema = get_catalog_schema()
        if not catalog or not schema:
            raise HTTPException(
                status_code=400,
                detail="App storage location not configured — complete the Setup Wizard before managing permissions.",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="App storage location not configured — complete the Setup Wizard before managing permissions.",
        )
    try:
        _save_user_permissions_to_table(
            normalized_admins,
            normalized_consumers,
            owner=owner_email or None,
        )
        logger.info(
            "Permissions saved to Delta table (%s admins, %s consumers)",
            len(normalized_admins),
            len(normalized_consumers),
        )
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Failed to save permissions: {e}")
        raise HTTPException(status_code=500, detail="Failed to save permissions — check server logs")


# ── Refresh Schedule ─────────────────────────────────────────────────────────

_SCHEDULE_DEFAULTS: dict = {"enabled": True, "frequency": "nightly", "hour_utc": 9, "lookback_days": 180}


def _save_schedule_to_table(settings: dict) -> None:
    """Persist schedule settings. Lakebase app_config primary; UC Delta dual-write."""
    try:
        from backend.db import lakebase_available, lb_set_config
        if lakebase_available():
            lb_set_config("schedule_settings", settings)
    except Exception as e:
        logger.warning("Could not save schedule settings to Lakebase (non-fatal): %s", e)
    try:
        from backend.db import execute_write
        _ensure_schedule_table()
        table = _config_table("app_schedule_settings")
        execute_write(f"DELETE FROM {table}", None)
        execute_write(
            f"INSERT INTO {table} (settings_json, updated_at) VALUES (:s, current_timestamp())",
            {"s": json.dumps(settings)},
        )
    except Exception as e:
        logger.debug("UC dual-write of schedule settings skipped/failed (non-fatal): %s", e)


def load_schedule_settings() -> dict:
    """Load schedule settings — Lakebase first, then UC Delta, then file fallback."""
    # Primary: Lakebase
    try:
        from backend.db import lb_get_config
        cfg = lb_get_config("schedule_settings")
        if isinstance(cfg, dict) and cfg:
            return {**_SCHEDULE_DEFAULTS, **cfg}
    except Exception as e:
        logger.debug("Lakebase schedule read failed, will try UC: %s", e)
    try:
        from backend.db import execute_query
        table = _config_table("app_schedule_settings")
        rows = execute_query(f"SELECT settings_json FROM {table} LIMIT 1", None, no_cache=True)
        if rows and rows[0].get("settings_json"):
            return {**_SCHEDULE_DEFAULTS, **json.loads(rows[0]["settings_json"])}
    except Exception as e:
        if _table_missing(e):
            logger.debug("Could not load schedule settings from Delta (not yet created): %s", e)
        else:
            logger.warning("Could not load schedule settings from Delta (storage may not be configured yet): %s", e)

    # Fallback: local file (dev / first run before table exists)
    try:
        if os.path.exists(SCHEDULE_SETTINGS_FILE):
            with open(SCHEDULE_SETTINGS_FILE) as f:
                data = json.load(f)
            # Migrate to Delta opportunistically
            try:
                _save_schedule_to_table(data)
                logger.info("Migrated schedule settings from file to Delta")
            except Exception:
                pass
            return {**_SCHEDULE_DEFAULTS, **data}
    except Exception:
        pass

    # Neither Delta nor file — persist defaults to Delta so they survive the next redeploy
    defaults = dict(_SCHEDULE_DEFAULTS)
    try:
        _save_schedule_to_table(defaults)
        logger.info("Initialized schedule settings in Delta with defaults")
    except Exception:
        pass
    return defaults


@router.get("/schedule")
async def get_schedule_settings() -> dict:
    return load_schedule_settings()


@router.post("/schedule")
async def save_schedule_endpoint(request: Request, data: dict) -> dict:
    _require_admin(request)
    settings = {
        "enabled": bool(data.get("enabled", True)),
        "frequency": data.get("frequency", "nightly"),
        "hour_utc": max(0, min(23, int(data.get("hour_utc", 5)))),
        "lookback_days": int(data.get("lookback_days", 180)),
    }
    if settings["frequency"] not in ("nightly", "weekly", "monthly"):
        settings["frequency"] = "nightly"
    if settings["lookback_days"] not in (180, 365, 730, 1095):
        settings["lookback_days"] = 180
    try:
        _save_schedule_to_table(settings)
    except Exception as e:
        logger.warning("Could not save schedule settings to Delta: %s", e)
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SCHEDULE_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
    logger.info("Schedule settings saved: %s", settings)
    return settings


# ── Alert Thresholds ──────────────────────────────────────────────────────────

_ALERT_THRESHOLD_DEFAULTS: dict = {
    "spike_threshold_percent": 20,
    "daily_budget": 50000,
    "workspace_budget": 10000,
}


def _load_alert_thresholds() -> dict:
    """Load alert thresholds — Lakebase first, then UC Delta, file, then defaults."""
    try:
        from backend.db import lb_get_config
        cfg = lb_get_config("alert_thresholds")
        if isinstance(cfg, dict) and cfg:
            return {**_ALERT_THRESHOLD_DEFAULTS, **cfg}
    except Exception as e:
        logger.debug("Lakebase alert-thresholds read failed, will try UC: %s", e)
    try:
        from backend.db import execute_query
        table = _config_table("app_alert_thresholds")
        rows = execute_query(f"SELECT settings_json FROM {table} LIMIT 1", None, no_cache=True)
        if rows and rows[0].get("settings_json"):
            return {**_ALERT_THRESHOLD_DEFAULTS, **json.loads(rows[0]["settings_json"])}
    except Exception as e:
        if _table_missing(e):
            logger.debug("Could not load alert thresholds from Delta (not yet created): %s", e)
        else:
            logger.warning("Could not load alert thresholds from Delta: %s", e)

    try:
        if os.path.exists(ALERT_THRESHOLDS_FILE):
            with open(ALERT_THRESHOLDS_FILE) as f:
                data = json.load(f)
            try:
                _save_alert_thresholds(data)
                logger.info("Migrated alert thresholds from file to Delta")
            except Exception:
                pass
            return {**_ALERT_THRESHOLD_DEFAULTS, **data}
    except Exception:
        pass

    return dict(_ALERT_THRESHOLD_DEFAULTS)


def _save_alert_thresholds(settings: dict) -> None:
    try:
        from backend.db import lakebase_available, lb_set_config
        if lakebase_available():
            lb_set_config("alert_thresholds", settings)
    except Exception as e:
        logger.warning("Could not save alert thresholds to Lakebase (non-fatal): %s", e)
    try:
        from backend.db import execute_write
        _ensure_alert_thresholds_table()
        table = _config_table("app_alert_thresholds")
        execute_write(f"DELETE FROM {table}", None)
        execute_write(
            f"INSERT INTO {table} (settings_json, updated_at) VALUES (:s, current_timestamp())",
            {"s": json.dumps(settings)},
        )
    except Exception as e:
        logger.warning("Could not save alert thresholds to Delta: %s", e)
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(ALERT_THRESHOLDS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


@router.get("/alert-thresholds")
async def get_alert_thresholds() -> dict:
    return _load_alert_thresholds()


@router.post("/alert-thresholds")
async def save_alert_thresholds_endpoint(request: Request) -> dict:
    _require_admin(request)
    data = await request.json()
    settings = {
        "spike_threshold_percent": max(5.0, min(100.0, float(data.get("spike_threshold_percent", 20)))),
        "daily_budget": max(0.0, float(data.get("daily_budget", 50000))),
        "workspace_budget": max(0.0, float(data.get("workspace_budget", 10000))),
    }
    _save_alert_thresholds(settings)
    logger.info("Alert thresholds saved: %s", settings)
    return {"status": "saved"}


# ── Customer Discounts ────────────────────────────────────────────────────────

_ACCOUNT_PRICES_SQL = """
SELECT
  sku_name,
  cloud,
  currency_code,
  usage_unit,
  pricing.default        AS list_price,
  TRY(pricing.effective_list.default) AS effective_list_price,
  price_start_time       AS start_time,
  price_end_time         AS end_time
FROM system.billing.account_prices
WHERE price_end_time IS NULL
   OR price_end_time > CURRENT_TIMESTAMP
ORDER BY sku_name, cloud
"""

_LIST_PRICES_SQL = """
SELECT
  sku_name,
  cloud,
  currency_code,
  usage_unit,
  pricing.default        AS list_price,
  TRY(pricing.effective_list.default) AS effective_list_price,
  price_start_time       AS start_time,
  price_end_time         AS end_time
FROM system.billing.list_prices
WHERE price_end_time IS NULL
   OR price_end_time > CURRENT_TIMESTAMP
ORDER BY sku_name, cloud
"""


@router.get("/account-prices")
async def get_account_prices() -> dict[str, Any]:
    """Return customer-specific account prices from system.billing.account_prices.

    Falls back to system.billing.list_prices if account_prices is not available
    (the table is currently in private preview).
    """
    from backend.db import execute_query as _exec

    _TRANSIENT_ERRORS = ("table", "not found", "does not exist", "cannot resolve", "http_path", "warehouse")

    # Try account_prices first (negotiated rates, private preview)
    try:
        rows = _exec(_ACCOUNT_PRICES_SQL)
        source = "account_prices"
    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in _TRANSIENT_ERRORS):
            logger.info(f"system.billing.account_prices not available ({e}), falling back to list_prices")
            try:
                rows = _exec(_LIST_PRICES_SQL)
                source = "list_prices"
            except Exception as e2:
                logger.debug(f"system.billing.list_prices also unavailable: {e2}")
                return {"available": False, "prices": [], "source": None,
                        "message": "Billing price tables not accessible"}
        else:
            logger.warning(f"account_prices query failed: {e}")
            return {"available": False, "prices": [], "source": None, "message": str(e)}

    prices = [
        {
            "sku_name": r.get("sku_name") or "",
            "cloud": r.get("cloud") or "",
            "currency_code": r.get("currency_code") or "USD",
            "usage_unit": r.get("usage_unit") or "DBU",
            "list_price": float(r.get("list_price") or 0),
            "effective_list_price": float(r.get("effective_list_price") or r.get("list_price") or 0),
            "start_time": str(r.get("start_time")) if r.get("start_time") else None,
            "end_time": str(r.get("end_time")) if r.get("end_time") else None,
        }
        for r in rows
    ]
    return {"available": True, "prices": prices, "source": source, "count": len(prices)}


# ── Pricing Mode ──────────────────────────────────────────────────────────────

def _save_pricing_to_table(settings: dict) -> None:
    """Persist pricing settings. Lakebase app_config primary; UC Delta dual-write."""
    try:
        from backend.db import lakebase_available, lb_set_config
        if lakebase_available():
            lb_set_config("pricing_settings", settings)
    except Exception as e:
        logger.warning("Could not save pricing settings to Lakebase (non-fatal): %s", e)
    try:
        from backend.db import execute_write
        _ensure_pricing_table()
        table = _config_table("app_pricing_settings")
        execute_write(f"DELETE FROM {table}", None)
        execute_write(
            f"INSERT INTO {table} (settings_json, updated_at) VALUES (:s, current_timestamp())",
            {"s": json.dumps(settings)},
        )
    except Exception as e:
        logger.debug("UC dual-write of pricing settings skipped/failed (non-fatal): %s", e)


def _load_pricing_settings() -> dict:
    """Load pricing settings — Lakebase first, then UC Delta, then file fallback."""
    # Primary: Lakebase
    try:
        from backend.db import lb_get_config
        cfg = lb_get_config("pricing_settings")
        if isinstance(cfg, dict) and cfg:
            return cfg
    except Exception as e:
        logger.debug("Lakebase pricing read failed, will try UC: %s", e)
    try:
        from backend.db import execute_query
        table = _config_table("app_pricing_settings")
        rows = execute_query(f"SELECT settings_json FROM {table} LIMIT 1", None, no_cache=True)
        if rows and rows[0].get("settings_json"):
            return json.loads(rows[0]["settings_json"])
    except Exception as e:
        if _table_missing(e):
            logger.debug("Could not load pricing settings from Delta (not yet created): %s", e)
        else:
            logger.warning("Could not load pricing settings from Delta (storage may not be configured yet): %s", e)

    try:
        with open(PRICING_SETTINGS_FILE) as f:
            data = json.load(f)
        try:
            _save_pricing_to_table(data)
            logger.info("Migrated pricing settings from file to Delta")
        except Exception:
            pass
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Neither Delta nor file — persist defaults to Delta so they survive the next redeploy
    defaults = {"use_account_prices": False}
    try:
        _save_pricing_to_table(defaults)
        logger.info("Initialized pricing settings in Delta with defaults")
    except Exception:
        pass
    return defaults


def _save_pricing_settings(settings: dict) -> None:
    try:
        _save_pricing_to_table(settings)
    except Exception as e:
        logger.warning("Could not save pricing settings to Delta: %s", e)
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(PRICING_SETTINGS_FILE, "w") as f:
        json.dump(settings, f)


@router.get("/pricing-mode")
async def get_pricing_mode() -> dict[str, Any]:
    """Return the current pricing mode setting."""
    settings = _load_pricing_settings()
    return {
        "use_account_prices": settings.get("use_account_prices", False),
    }


@router.put("/pricing-mode", dependencies=[Depends(require_admin)])
async def set_pricing_mode(data: dict) -> dict[str, Any]:
    """Save the pricing mode setting."""
    use_account_prices = bool(data.get("use_account_prices", False))
    _save_pricing_settings({"use_account_prices": use_account_prices})
    return {"use_account_prices": use_account_prices, "status": "ok"}


# Usage-weighted blended account price multiplier query. Weighted by usage in the
# caller's date range / workspaces so the ratio matches the figures it scales.
# SKUs with no negotiated rate are billed at list, so they count at list (ratio 1)
# instead of being dropped — dropping them spread the discount onto undiscounted SKUs.
_ACCOUNT_PRICE_MULTIPLIER_SQL = """
WITH range_usage AS (
  SELECT
    u.sku_name,
    u.cloud,
    SUM(u.usage_quantity) AS total_quantity
  FROM system.billing.usage u
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.usage_quantity > 0
    {workspace_filter}
  GROUP BY u.sku_name, u.cloud
),
current_account_prices AS (
  SELECT sku_name, cloud, currency_code, MIN(pricing.default) AS account_price
  FROM system.billing.account_prices
  WHERE (price_end_time IS NULL OR price_end_time > CURRENT_TIMESTAMP)
    AND pricing.default > 0
  GROUP BY sku_name, cloud, currency_code
),
price_comparison AS (
  SELECT
    ru.sku_name,
    ru.total_quantity,
    lp.pricing.default AS list_price,
    COALESCE(ap.account_price, lp.pricing.default) AS account_price,
    ap.sku_name IS NOT NULL AS has_account_price
  FROM range_usage ru
  JOIN system.billing.list_prices lp
    ON ru.sku_name = lp.sku_name AND ru.cloud = lp.cloud AND lp.price_end_time IS NULL
  LEFT JOIN current_account_prices ap
    ON ru.sku_name = ap.sku_name AND ru.cloud = ap.cloud AND lp.currency_code = ap.currency_code
  WHERE lp.pricing.default > 0
)
SELECT
  SUM(total_quantity * account_price) / NULLIF(SUM(total_quantity * list_price), 0) AS multiplier,
  COUNT(DISTINCT CASE WHEN has_account_price THEN sku_name END) AS sku_count,
  COUNT(DISTINCT sku_name) AS priced_sku_count,
  SUM(total_quantity * list_price)   AS weighted_list_spend,
  SUM(total_quantity * account_price) AS weighted_account_spend
FROM price_comparison
"""

_UNAVAILABLE_MULTIPLIER = {"multiplier": 1.0, "available": False, "sku_count": 0, "discount_percent": 0}


@router.get("/account-price-multiplier")
async def get_account_price_multiplier(
    start_date: str | None = None,
    end_date: str | None = None,
    workspace_ids: str | None = None,
) -> dict[str, Any]:
    """Compute a usage-weighted blended account price multiplier.

    Returns the ratio of account-negotiated prices to list prices, weighted by
    usage in [start_date, end_date] (default: last 30 days) for the selected
    workspaces. The frontend scales the Overview and infrastructure spend
    figures by it when 'use_account_prices' is enabled; other tabs stay at list.

    Returns multiplier=1.0 if account_prices is unavailable or has no rates.
    """
    from datetime import date, timedelta

    from backend import workspace_filter as wf
    from backend.db import execute_query as _exec

    pricing_settings = _load_pricing_settings()
    use_account_prices = pricing_settings.get("use_account_prices", False)

    if not use_account_prices:
        return dict(_UNAVAILABLE_MULTIPLIER)

    params = {
        "start_date": start_date or (date.today() - timedelta(days=30)).isoformat(),
        "end_date": end_date or date.today().isoformat(),
    }
    id_list = [i.strip() for i in workspace_ids.split(",") if i.strip()] if workspace_ids else None
    sql = _ACCOUNT_PRICE_MULTIPLIER_SQL.replace(
        "{workspace_filter}", wf.build_ws_filter_clause(col="u.workspace_id", id_list=id_list)
    )

    try:
        rows = _exec(sql, params)
        if not rows or rows[0].get("multiplier") is None:
            return dict(_UNAVAILABLE_MULTIPLIER)
        row = rows[0]
        sku_count = int(row.get("sku_count") or 0)
        if sku_count == 0:
            return {**_UNAVAILABLE_MULTIPLIER,
                    "message": "No negotiated rates in system.billing.account_prices for SKUs used in this range"}
        multiplier = float(row["multiplier"])
        discount_percent = round((1.0 - multiplier) * 100, 2)
        return {
            "multiplier": multiplier,
            "available": True,
            "sku_count": sku_count,
            "priced_sku_count": int(row.get("priced_sku_count") or 0),
            "discount_percent": discount_percent,
            "weighted_list_spend": float(row.get("weighted_list_spend") or 0),
            "weighted_account_spend": float(row.get("weighted_account_spend") or 0),
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }
    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in ("table", "not found", "does not exist", "cannot resolve")):
            logger.info("system.billing.account_prices not available for multiplier computation")
            return {**_UNAVAILABLE_MULTIPLIER,
                    "message": "system.billing.account_prices not available (private preview)"}
        logger.warning(f"Account price multiplier computation failed: {e}")
        return dict(_UNAVAILABLE_MULTIPLIER)


# ── Unified app settings (Phase 2 aggregator) ─────────────────────────────────
# ONE app-wide prefs table (app_settings) for values that had no home. The
# per-domain tables (app_alert_thresholds, app_webhook_settings, app_pricing_settings,
# app_schedule_settings, app_user_permissions) stay the source of truth for their
# domains — the aggregator composes them and PUT dispatches writes back to them.

_DEFAULT_TAB_VISIBILITY: dict = {
    "dbu": True, "infra": True, "optimizer": True, "kpis": True, "aiml": True,
    "sql": True, "apps": True, "tagging": True, "use-cases": False, "users-groups": True,
}

_APP_SETTINGS_DEFAULTS: dict = {
    "company_name": "",
    "app_display_name": "",
    "default_date_range_days": 30,
    "default_landing_tab": "dbu",
    "auto_refresh_minutes": 0,
    "density": "comfortable",
    "theme": "light",
    "show_workspace_names": True,
    "anomaly_sensitivity": "medium",
    "exp_setup_wizard_link": False,
    "exp_debugger_link": False,
    "enable_architecture_view": True,
    "enable_mv_share_runbook": True,
    "feedback_slack_url": "",
    "enable_use_case_tracking": False,
    "enable_accuracy_checks": False,
    "tab_visibility": _DEFAULT_TAB_VISIBILITY,
}

# Keys the client may write into app_settings (env-bound values are never accepted).
_APP_SETTINGS_ALLOWED = set(_APP_SETTINGS_DEFAULTS.keys())

APP_SETTINGS_FILE = os.path.join(SETTINGS_DIR, "app_settings.json")


def _ensure_app_settings_table() -> None:
    _ensure_config_table(
        f"CREATE TABLE IF NOT EXISTS {_config_table('app_settings')} "
        f"(id STRING NOT NULL, settings_json STRING, updated_at TIMESTAMP) USING DELTA"
    )


def get_app_settings() -> dict:
    """App-wide prefs — Lakebase first, warehouse table then file as fallback."""
    try:
        from backend.db import lb_get_config
        cfg = lb_get_config("app_settings")
        if isinstance(cfg, dict) and cfg:
            return {**_APP_SETTINGS_DEFAULTS, **{k: v for k, v in cfg.items() if k in _APP_SETTINGS_ALLOWED}}
    except Exception as e:
        logger.debug("Lakebase app_settings read failed, will try UC: %s", e)
    try:
        from backend.db import execute_query
        table = _config_table("app_settings")
        rows = execute_query(f"SELECT settings_json FROM {table} WHERE id = 'app' LIMIT 1", None, no_cache=True)
        if rows and rows[0].get("settings_json"):
            return {**_APP_SETTINGS_DEFAULTS, **json.loads(rows[0]["settings_json"])}
    except Exception as e:
        if _table_missing(e):
            logger.debug("app_settings table not yet created: %s", e)
        else:
            logger.warning("Could not load app_settings from Delta: %s", e)
    try:
        if os.path.exists(APP_SETTINGS_FILE):
            with open(APP_SETTINGS_FILE) as f:
                data = json.load(f)
            return {**_APP_SETTINGS_DEFAULTS, **data}
    except (json.JSONDecodeError, IOError):
        pass
    return dict(_APP_SETTINGS_DEFAULTS)


def save_app_settings(partial: dict) -> dict:
    """Merge a partial into app_settings. Lakebase primary; Delta + file fallback."""
    current = get_app_settings()
    clean = {k: v for k, v in (partial or {}).items() if k in _APP_SETTINGS_ALLOWED}
    merged = {**current, **clean}
    try:
        from backend.db import lb_set_config
        lb_set_config("app_settings", merged)
    except Exception as e:
        logger.warning("Could not persist app_settings to Lakebase: %s", e)
    try:
        from backend.db import execute_write
        _ensure_app_settings_table()
        table = _config_table("app_settings")
        execute_write(f"DELETE FROM {table} WHERE id = 'app'", None)
        execute_write(
            f"INSERT INTO {table} (id, settings_json, updated_at) VALUES ('app', :s, current_timestamp())",
            {"s": json.dumps(merged)},
        )
    except Exception as e:
        logger.debug("UC dual-write of app_settings skipped/failed (non-fatal): %s", e)
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(APP_SETTINGS_FILE, "w") as f:
            json.dump(merged, f, indent=2)
    except OSError:
        pass
    try:
        from backend.feedback import invalidate_feedback_settings_cache

        invalidate_feedback_settings_cache()
    except Exception:
        logger.debug("Feedback settings cache invalidation skipped")
    return merged


def workspace_names_enabled() -> bool:
    """Whether resolved workspace display names should be shown (else IDs). Default True."""
    try:
        return bool(get_app_settings().get("show_workspace_names", True))
    except Exception:
        return True


# Sensitivity → spike-threshold multiplier. High = more sensitive => lower threshold.
_ANOMALY_MULTIPLIER = {"low": 1.5, "medium": 1.0, "high": 0.5}


def anomaly_spike_threshold() -> float:
    """Effective day-over-day spike threshold (%) = base threshold × sensitivity multiplier."""
    base = float(_load_alert_thresholds().get("spike_threshold_percent", 20) or 20)
    sens = str(get_app_settings().get("anomaly_sensitivity", "medium")).lower()
    return round(base * _ANOMALY_MULTIPLIER.get(sens, 1.0), 2)


def _is_admin(request: Request) -> bool:
    """Non-raising admin check (mirrors _require_admin's rule)."""
    email = request.headers.get("X-Forwarded-Email", os.getenv("USER", "dev@local"))
    return _is_admin_email(email, _load_user_permissions())


# Cheap capability probes cached in-process (avoid a SQL round-trip on every settings load).
_cap_cache: dict[str, tuple[float, bool]] = {}
_CAP_TTL = 10 * 60


def _cached_probe(key: str, sql: str) -> bool:
    hit = _cap_cache.get(key)
    if hit and (time.time() - hit[0]) < _CAP_TTL:
        return hit[1]
    ok = False
    try:
        from backend.db import execute_query
        execute_query(sql, None, no_cache=True)
        ok = True
    except Exception:
        ok = False
    _cap_cache[key] = (time.time(), ok)
    return ok


def _capabilities(request: Request) -> dict:
    return {
        "smtp_configured": bool(os.getenv("SMTP_HOST")),
        "workspace_names_available": _cached_probe("ws_names", "SELECT 1 FROM system.access.workspaces_latest LIMIT 1"),
        "account_prices_available": _cached_probe("acct_prices", "SELECT 1 FROM system.billing.account_prices LIMIT 1"),
        "is_admin": _is_admin(request),
    }


def _webhook_masked() -> dict:
    settings = _load_webhook_settings()
    url = settings.get("slack_webhook_url", "")
    masked = ("https://hooks.slack.com/services/****" if "hooks.slack.com" in url else "****") if url else None
    return {"configured": bool(url), "masked_url": masked}


def _settings_snapshot(request: Request) -> dict:
    """Compose the full settings object from the per-domain stores + app_settings."""
    app = get_app_settings()
    thresholds = _load_alert_thresholds()
    general_keys = (
        "company_name", "app_display_name", "default_date_range_days", "default_landing_tab",
        "auto_refresh_minutes", "density", "theme", "show_workspace_names",
        "enable_use_case_tracking", "enable_accuracy_checks",
    )
    return {
        "general": {k: app.get(k) for k in general_keys},
        "tab_visibility": app.get("tab_visibility", _DEFAULT_TAB_VISIBILITY),
        "thresholds": {
            "spike_threshold_percent": thresholds.get("spike_threshold_percent", 20),
            "daily_budget": thresholds.get("daily_budget", 50000),
            "workspace_budget": thresholds.get("workspace_budget", 10000),
            "anomaly_sensitivity": app.get("anomaly_sensitivity", "medium"),
        },
        "webhook": _webhook_masked(),
        "pricing": {"use_account_prices": bool(_load_pricing_settings().get("use_account_prices", False))},
        "schedule": load_schedule_settings(),
        "experimental": {
            "exp_setup_wizard_link": bool(app.get("exp_setup_wizard_link", False)),
            "exp_debugger_link": bool(app.get("exp_debugger_link", False)),
            "enable_architecture_view": bool(app.get("enable_architecture_view", True)),
            "enable_mv_share_runbook": bool(app.get("enable_mv_share_runbook", True)),
        },
        "capabilities": _capabilities(request),
    }


@router.get("")
async def get_unified_settings(request: Request) -> dict:
    """Aggregated settings for the settings modal (Phase 2). Read-only; safe for all users."""
    return await asyncio.to_thread(_settings_snapshot, request)


@router.put("")
async def put_unified_settings(request: Request) -> dict:
    """Partial settings update — dispatches each sub-object to its domain store. Admin-only."""
    _require_admin(request)
    body = await request.json()

    def _apply() -> dict:
        # app_settings-backed groups (general prefs, tab visibility, experimental, sensitivity)
        app_partial: dict = {}
        general = body.get("general")
        if isinstance(general, dict):
            for k in (
                "company_name", "app_display_name", "default_date_range_days", "default_landing_tab",
                "auto_refresh_minutes", "density", "theme", "show_workspace_names",
                "enable_use_case_tracking", "enable_accuracy_checks",
            ):
                if k in general:
                    app_partial[k] = general[k]
        exp = body.get("experimental")
        if isinstance(exp, dict):
            for k in (
                "exp_setup_wizard_link",
                "exp_debugger_link",
                "enable_architecture_view",
                "enable_mv_share_runbook",
            ):
                if k in exp:
                    app_partial[k] = bool(exp[k])
        tv = body.get("tab_visibility")
        if isinstance(tv, dict):
            merged_tv = {**_DEFAULT_TAB_VISIBILITY, **{k: bool(v) for k, v in tv.items() if k in _DEFAULT_TAB_VISIBILITY}}
            if not any(merged_tv.values()):
                raise HTTPException(status_code=400, detail="At least one dashboard tab must remain visible.")
            app_partial["tab_visibility"] = merged_tv

        thresholds = body.get("thresholds")
        if isinstance(thresholds, dict):
            if "anomaly_sensitivity" in thresholds:
                s = str(thresholds["anomaly_sensitivity"]).lower()
                app_partial["anomaly_sensitivity"] = s if s in _ANOMALY_MULTIPLIER else "medium"
            if any(k in thresholds for k in ("spike_threshold_percent", "daily_budget", "workspace_budget")):
                cur = _load_alert_thresholds()
                _save_alert_thresholds({
                    "spike_threshold_percent": max(1.0, min(500.0, float(thresholds.get("spike_threshold_percent", cur.get("spike_threshold_percent", 20))))),
                    "daily_budget": max(0.0, float(thresholds.get("daily_budget", cur.get("daily_budget", 50000)))),
                    "workspace_budget": max(0.0, float(thresholds.get("workspace_budget", cur.get("workspace_budget", 10000)))),
                })

        if app_partial:
            save_app_settings(app_partial)

        webhook = body.get("webhook")
        if isinstance(webhook, dict) and webhook.get("slack_webhook_url"):
            _save_webhook_settings({"slack_webhook_url": webhook["slack_webhook_url"]})

        pricing = body.get("pricing")
        if isinstance(pricing, dict) and "use_account_prices" in pricing:
            _save_pricing_settings({"use_account_prices": bool(pricing["use_account_prices"])})

        schedule = body.get("schedule")
        if isinstance(schedule, dict):
            sched = {
                "enabled": bool(schedule.get("enabled", True)),
                "frequency": schedule.get("frequency", "nightly") if schedule.get("frequency") in ("nightly", "weekly", "monthly") else "nightly",
                "hour_utc": max(0, min(23, int(schedule.get("hour_utc", 5)))),
                "lookback_days": int(schedule.get("lookback_days", 180)) if int(schedule.get("lookback_days", 180)) in (180, 365, 730, 1095) else 180,
            }
            _save_schedule_to_table(sched)
            try:
                os.makedirs(SETTINGS_DIR, exist_ok=True)
                with open(SCHEDULE_SETTINGS_FILE, "w") as f:
                    json.dump(sched, f, indent=2)
            except OSError:
                pass

        return _settings_snapshot(request)

    return await asyncio.to_thread(_apply)
