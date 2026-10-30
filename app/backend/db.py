"""Databricks SQL connection factory."""

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Generator

# Per-request user token set by UserAuthMiddleware when x-forwarded-access-token
# is present (Databricks Apps user authorization preview). Empty string = use SP.
_user_token: ContextVar[str] = ContextVar("_user_token", default="")

# Persisted auth-mode override file (written by POST /api/settings/auth-mode)
_AUTH_MODE_OVERRIDE_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".settings", "auth_mode_override.json"
)


def _load_auth_mode_override() -> str:
    """Read the persisted auth mode preference. Returns 'sp' or 'unknown'."""
    try:
        if os.path.exists(_AUTH_MODE_OVERRIDE_FILE):
            with open(_AUTH_MODE_OVERRIDE_FILE) as f:
                data = json.load(f)
            if data.get("mode") == "sp":
                return "sp"
    except Exception:
        pass
    return "unknown"


# Auth mode is permanently locked to "sp". OAuth/user-token auth is preserved in
# the codebase for future use but is not active. The UserAuthMiddleware already
# gates on this value and will not forward user tokens when mode is "sp".
_auth_mode: str = "sp"  # always "sp" — OAuth disabled until further notice


def _lock_auth_mode(mode: str) -> None:
    """No-op — auth mode is permanently 'sp'. Kept for call-site compatibility."""
    if mode != "sp":
        logger.debug("_lock_auth_mode('%s') ignored — OAuth disabled, always SP.", mode)


def set_auth_mode_override(mode: str) -> None:
    """No-op stub — OAuth is disabled; auth mode is permanently 'sp'.

    Kept for call-site compatibility. The override file and Delta table writes
    are skipped. When OAuth is re-enabled in the future, restore the original
    implementation from git history.
    """
    if mode != "sp":
        logger.warning(
            "set_auth_mode_override('%s') ignored — OAuth is disabled. "
            "Auth mode is permanently locked to 'sp'.", mode
        )
    # Always keep _auth_mode as "sp" — no mutation needed

from cachetools import TTLCache
from databricks import sql
from databricks.sdk import AccountClient, WorkspaceClient
from databricks.sdk.service.sql import (
    CreateWarehouseRequestWarehouseType,
    EndpointInfoWarehouseType,
    SpotInstancePolicy,
    State,
)

logger = logging.getLogger(__name__)


def get_host_url() -> str:
    """Return the Databricks workspace URL with https:// prefix.

    Handles the common case where DATABRICKS_HOST is set to just the hostname
    (e.g. 'fevm-cmegdemos.cloud.databricks.com') without a protocol prefix,
    as well as when the full URL is provided. Falls back to SDK config.
    """
    host = os.getenv("DATABRICKS_HOST", "")
    if not host:
        # Try SDK workspace client (works in Databricks Apps with OAuth)
        try:
            from databricks.sdk import WorkspaceClient
            w = WorkspaceClient()
            host = w.config.host or ""
        except Exception:
            pass
    if not host:
        return ""
    host = host.rstrip("/")
    if not host.startswith("https://") and not host.startswith("http://"):
        host = f"https://{host}"
    return host


_CATALOG_OVERRIDE_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".settings", "catalog_override.json"
)

# DBFS path for catalog/schema — survives git redeploys unlike .settings/.
# Written alongside the local file; read as fallback when local file is absent.
_DBFS_OVERRIDE_PATH = "/databricks/cost-obs-app/catalog_override.json"

# Locations that are forbidden as app storage targets. main.cost_obs is the old
# hardcoded default that shipped in app.yaml — it must never be auto-created.
_FORBIDDEN_STORAGE_LOCATIONS: frozenset[tuple[str, str]] = frozenset({
    ("main", "cost_obs"),
})


class StorageConfigurationError(ValueError):
    """Catalog/schema config is invalid or resolves to a forbidden location."""
    pass


def validate_app_storage_target(catalog: str, schema: str) -> None:
    """Raise StorageConfigurationError if catalog/schema is unfit for app writes.

    Called by every code path that executes DDL or app-table writes so that a
    misconfigured deploy surfaces a clean error rather than silently touching
    forbidden locations.
    """
    if not catalog or not schema:
        raise StorageConfigurationError(
            "App storage location is not configured. "
            "Set COST_OBS_CATALOG and COST_OBS_SCHEMA in the Databricks Apps environment, "
            "then complete the setup wizard."
        )
    if (catalog.lower(), schema.lower()) in _FORBIDDEN_STORAGE_LOCATIONS:
        raise StorageConfigurationError(
            f"'{catalog}.{schema}' is a reserved default location and cannot be used as app storage. "
            "Set COST_OBS_CATALOG and COST_OBS_SCHEMA to a dedicated catalog and schema."
        )


def _read_dbfs_catalog_override() -> tuple[str, str]:
    """Read catalog/schema from DBFS. Best-effort — returns ("", "") on any error.

    On a successful read, also writes the value back to the local .settings/ file
    so subsequent calls within the same process use the fast local path instead of
    hitting the DBFS API on every request.
    """
    try:
        import base64
        w = get_workspace_client()
        resp = w.api_client.do(
            "GET", "/api/2.0/dbfs/read",
            query={"path": _DBFS_OVERRIDE_PATH, "length": 4096},
        )
        raw = resp.get("data", "")
        if not raw:
            return "", ""
        data = json.loads(base64.b64decode(raw).decode("utf-8"))
        cat = data.get("catalog", "").strip()
        sch = data.get("schema", "").strip()
        if cat and sch and (cat.lower(), sch.lower()) not in _FORBIDDEN_STORAGE_LOCATIONS:
            # Restore local file so this process doesn't hit DBFS on every call
            try:
                os.makedirs(os.path.dirname(_CATALOG_OVERRIDE_FILE), exist_ok=True)
                with open(_CATALOG_OVERRIDE_FILE, "w") as f:
                    json.dump({"catalog": cat, "schema": sch}, f)
                logger.info("Restored local catalog override from DBFS: %s.%s", cat, sch)
            except Exception:
                pass
            return cat, sch
    except Exception:
        pass
    return "", ""


def _write_dbfs_catalog_override(catalog: str, schema: str) -> None:
    """Write catalog/schema to DBFS. Best-effort, non-fatal.

    Merges into the shared DBFS settings JSON so existing flags (setup_complete,
    build_state) are preserved.  A plain overwrite here would destroy setup_complete
    if called after the wizard completes (e.g. from POST /api/settings/catalog).
    """
    try:
        import base64
        existing = _read_dbfs_settings()
        existing["catalog"] = catalog
        existing["schema"] = schema
        w = get_workspace_client()
        content = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
        w.api_client.do(
            "POST", "/api/2.0/dbfs/put",
            body={"path": _DBFS_OVERRIDE_PATH, "contents": content, "overwrite": True},
        )
        logger.info("DBFS catalog override saved: %s.%s", catalog, schema)
    except Exception as e:
        logger.warning("Could not write DBFS catalog override (non-fatal): %s", e)


def _read_dbfs_settings() -> dict:
    """Read the raw DBFS settings JSON. Returns {} on any error."""
    try:
        import base64
        w = get_workspace_client()
        resp = w.api_client.do("GET", "/api/2.0/dbfs/read",
                               query={"path": _DBFS_OVERRIDE_PATH, "length": 8192})
        raw = resp.get("data", "")
        if raw:
            return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        pass
    return {}


def write_dbfs_setup_complete() -> None:
    """Set setup_complete=True in the DBFS settings JSON. Best-effort, non-fatal.

    Called when the setup wizard finishes.  The flag survives container restarts
    (Databricks Apps recreates the container on every stop/start, wiping .settings/).
    """
    try:
        import base64
        existing = _read_dbfs_settings()
        existing["setup_complete"] = True
        w = get_workspace_client()
        content = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
        w.api_client.do("POST", "/api/2.0/dbfs/put",
                        body={"path": _DBFS_OVERRIDE_PATH, "contents": content, "overwrite": True})
        logger.info("DBFS setup_complete flag written")
    except Exception as e:
        logger.warning("Could not write DBFS setup_complete flag (non-fatal): %s", e)


def read_dbfs_setup_complete() -> bool:
    """Return True if a previous wizard run set setup_complete in DBFS."""
    return bool(_read_dbfs_settings().get("setup_complete"))


def write_dbfs_build_state(state: dict) -> None:
    """Persist build task state to DBFS so it survives pod restarts. Best-effort, non-fatal.

    Merges into the shared DBFS settings JSON so a single file holds all
    durable app state (setup_complete, catalog override, build_state).
    """
    try:
        import base64
        existing = _read_dbfs_settings()
        existing["build_state"] = state
        w = get_workspace_client()
        content = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
        w.api_client.do("POST", "/api/2.0/dbfs/put",
                        body={"path": _DBFS_OVERRIDE_PATH, "contents": content, "overwrite": True})
        logger.debug("DBFS build_state written (status=%s)", state.get("status"))
    except Exception as e:
        logger.debug("Could not write DBFS build_state (non-fatal): %s", e)


def read_dbfs_build_state() -> dict | None:
    """Return the last-persisted build task state from DBFS, or None if absent."""
    return _read_dbfs_settings().get("build_state") or None


def _accepted_catalog_schema(catalog: str, schema: str) -> tuple[str, str] | None:
    cat, sch = (catalog or "").strip(), (schema or "").strip()
    if not cat or not sch:
        return None
    if (cat.lower(), sch.lower()) in _FORBIDDEN_STORAGE_LOCATIONS:
        return None
    return cat, sch


# get_catalog_schema runs on nearly every request; avoid a Postgres round trip each time.
_lb_catalog_schema_cache: TTLCache = TTLCache(maxsize=1, ttl=30)
_MISSING = object()


def get_catalog_schema() -> tuple[str, str]:
    """Return the catalog and schema for cost observability tables.

    Priority:
      1. COST_OBS_CATALOG/COST_OBS_SCHEMA env vars (Apps UI — always win; needed
         before Lakebase is reachable on a brand-new deploy)
      2. Lakebase app_config (durable, v2 primary)
      3. .settings/catalog_override.json (pod cache, wiped on git redeploy)
      4. DBFS (legacy)

    Returns ("", "") when no location is configured (setup wizard not yet run).
    Never returns a forbidden location — logs CRITICAL and returns ("", "") instead.
    Use validate_app_storage_target() at write-path boundaries to raise explicitly.
    """
    # 1. Env vars are authoritative — set in the Databricks Apps UI
    catalog = os.getenv("COST_OBS_CATALOG", "").strip()
    schema = os.getenv("COST_OBS_SCHEMA", "").strip()
    if catalog and schema:
        accepted = _accepted_catalog_schema(catalog, schema)
        if not accepted:
            logger.critical(
                "COST_OBS_CATALOG=%s and COST_OBS_SCHEMA=%s resolve to a forbidden default location. "
                "Update the app environment variables to a dedicated catalog and schema. "
                "Returning empty to prevent writes to a reserved location.",
                catalog, schema,
            )
            return "", ""
        return accepted

    # 2. Lakebase — durable after first wizard save. Does not need UC catalog
    # to connect (PGHOST / ENDPOINT_NAME), so this is safe on the hot path.
    try:
        cached = _lb_catalog_schema_cache.get("v", _MISSING)
        if cached is _MISSING:
            cached = lb_get_config("catalog_schema") if lakebase_available() else None
            _lb_catalog_schema_cache["v"] = cached
        cfg = cached
        if isinstance(cfg, dict):
            accepted = _accepted_catalog_schema(str(cfg.get("catalog") or ""), str(cfg.get("schema") or ""))
            if accepted:
                return accepted
            if cfg.get("catalog") or cfg.get("schema"):
                logger.warning(
                    "Lakebase catalog_schema contains forbidden location %s.%s — ignoring.",
                    cfg.get("catalog"), cfg.get("schema"),
                )
    except Exception:
        pass

    # 3. Local override file (written by wizard during setup)
    try:
        if os.path.exists(_CATALOG_OVERRIDE_FILE):
            with open(_CATALOG_OVERRIDE_FILE) as f:
                data = json.load(f)
            accepted = _accepted_catalog_schema(str(data.get("catalog") or ""), str(data.get("schema") or ""))
            if accepted:
                return accepted
            if data.get("catalog") or data.get("schema"):
                logger.warning(
                    "catalog_override.json contains forbidden location %s.%s — ignoring.",
                    data.get("catalog"), data.get("schema"),
                )
    except Exception:
        pass

    # 4. DBFS fallback — survives git redeploys that wipe .settings/
    return _read_dbfs_catalog_override()


def save_catalog_schema(catalog: str, schema: str) -> None:
    """Persist a catalog/schema override (written from the Setup Wizard).

    Lakebase is primary. Local file + DBFS stay as fallbacks so a warehouse-only
    rollback and a mid-request worker still see the value.
    """
    catalog = catalog.strip()
    schema = schema.strip()
    validate_app_storage_target(catalog, schema)
    try:
        lb_set_config("catalog_schema", {"catalog": catalog, "schema": schema})
    except Exception as e:
        logger.debug("Could not save catalog/schema to Lakebase (non-fatal): %s", e)
    _lb_catalog_schema_cache.clear()
    os.makedirs(os.path.dirname(_CATALOG_OVERRIDE_FILE), exist_ok=True)
    with open(_CATALOG_OVERRIDE_FILE, "w") as f:
        json.dump({"catalog": catalog, "schema": schema}, f)
    logger.info("Catalog override saved locally: %s.%s", catalog, schema)
    _write_dbfs_catalog_override(catalog, schema)


# ── MV table name overrides ─────────────────────────────────────────────────

_MV_OVERRIDES_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".settings", "mv_table_overrides.json"
)


def get_mv_table_overrides() -> dict[str, str]:
    """Load per-table overrides mapping logical name → fully-qualified table path.

    Returns empty dict when no overrides are configured.
    """
    try:
        with open(_MV_OVERRIDES_FILE) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_mv_table_overrides(overrides: dict[str, str]) -> None:
    """Persist MV table overrides."""
    os.makedirs(os.path.dirname(_MV_OVERRIDES_FILE), exist_ok=True)
    with open(_MV_OVERRIDES_FILE, "w") as f:
        json.dump(overrides, f, indent=2)
    logger.info("MV table overrides saved: %s", list(overrides.keys()))


# ── Additional MV sources (union of Delta-shared / cross-workspace MVs) ───────
#
# Extra locations (typically Delta-shared in from another workspace) whose tables
# share the EXACT structure of this app's own MVs. When one or more sources are
# configured, every MV read is routed through a per-table `<name>__unified` view
# (built by materialized_views.rebuild_unified_views) that UNION ALLs the local
# table with each source's same-named table, tagging every row with a
# `source_label` column. This is additive — local data is always included.

_MV_SOURCES_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".settings", "mv_sources.json"
)

# Suffix of the per-table union view. Public so the router/view-builder agree.
MV_UNIFIED_SUFFIX = "__unified"

# Which MV tables currently have a built `<name>__unified` view. Written by
# rebuild_unified_views; read by apply_mv_overrides so it only ever remaps a table
# to a view that actually exists (never to a missing __unified view).
_MV_UNIFIED_TABLES_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".settings", "mv_unified_views.json"
)

# In-process caches so dashboard rewrites don't open a PG connection per query.
# None = not loaded this process. A loaded empty list is a real "no sources" value.
_mv_sources_mem: list[dict] | None = None
_unified_views_mem: list[str] | None = None


def _reset_mv_source_cache() -> None:
    """Test helper — drop the in-process MV-source / unified-view caches."""
    global _mv_sources_mem, _unified_views_mem
    _mv_sources_mem = None
    _unified_views_mem = None


def get_unified_view_tables() -> list[str]:
    """MV table names that currently have a built `<name>__unified` view.

    Read order: in-process cache, Lakebase (v2 primary), local .settings file,
    then the durable Delta table (warehouse fallback / pre-migration).
    """
    global _unified_views_mem
    if _unified_views_mem is not None:
        return _unified_views_mem
    try:
        cfg = lb_get_config("unified_view_tables")
        if isinstance(cfg, list):
            _unified_views_mem = [t for t in cfg if isinstance(t, str)]
            return _unified_views_mem
    except Exception:
        pass
    try:
        with open(_MV_UNIFIED_TABLES_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            _unified_views_mem = [t for t in data if isinstance(t, str)]
            return _unified_views_mem
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    _unified_views_mem = read_delta_unified_view_tables()
    return _unified_views_mem


def save_unified_view_tables(tables: list[str]) -> None:
    """Record which MV tables have a built unified view.

    Lakebase is primary. Local file + Delta stay as fallbacks.
    """
    global _unified_views_mem
    clean = [t for t in tables if isinstance(t, str)]
    _unified_views_mem = clean
    try:
        lb_set_config("unified_view_tables", clean)
    except Exception as e:
        logger.debug("Could not persist unified-view table list to Lakebase: %s", e)
    try:
        os.makedirs(os.path.dirname(_MV_UNIFIED_TABLES_FILE), exist_ok=True)
        with open(_MV_UNIFIED_TABLES_FILE, "w") as f:
            json.dump(clean, f)
    except OSError as e:
        logger.debug("Could not persist unified-view table list to file: %s", e)
    write_delta_unified_view_tables(clean)


def _unified_views_table() -> str:
    catalog, schema = get_catalog_schema()
    if not catalog or not schema:
        return ""
    return f"`{catalog}`.`{schema}`.`app_unified_views`"


def _ensure_unified_views_table() -> None:
    table = _unified_views_table()
    if not table:
        return
    execute_write(
        f"CREATE TABLE IF NOT EXISTS {table} "
        f"(table_name STRING NOT NULL, updated_at TIMESTAMP) USING DELTA",
        None,
    )


def read_delta_unified_view_tables() -> list[str]:
    """Built-unified-view table names from the durable Delta table. [] on any error."""
    try:
        table = _unified_views_table()
        if not table:
            return []
        _ensure_unified_views_table()
        rows = execute_query(f"SELECT table_name FROM {table}", None, no_cache=True)
        return [r["table_name"] for r in (rows or []) if r.get("table_name")]
    except Exception as e:
        logger.debug("Could not read unified-view list from Delta (non-fatal): %s", e)
        return []


def write_delta_unified_view_tables(tables: list[str]) -> None:
    """Persist the built-unified-view list to the durable Delta table (replace-all)."""
    try:
        table = _unified_views_table()
        if not table:
            return
        _ensure_unified_views_table()
        execute_write(f"DELETE FROM {table}", None)
        for t in tables:
            execute_write(
                f"INSERT INTO {table} (table_name, updated_at) VALUES (:t, current_timestamp())",
                {"t": t},
            )
    except Exception as e:
        logger.warning("Could not persist unified-view list to Delta (non-fatal): %s", e)


def _valid_mv_source(s: object) -> bool:
    return (
        isinstance(s, dict)
        and isinstance(s.get("label"), str) and bool(s["label"].strip())
        and isinstance(s.get("catalog"), str) and bool(s["catalog"].strip())
        and isinstance(s.get("schema"), str) and bool(s["schema"].strip())
    )


def _clean_mv_sources(sources: object) -> list[dict]:
    if not isinstance(sources, list):
        return []
    clean: list[dict] = []
    for s in sources:
        if not _valid_mv_source(s):
            continue
        entry = {"label": s["label"].strip(), "catalog": s["catalog"].strip(), "schema": s["schema"].strip()}
        if isinstance(s.get("tables"), list):
            picked = [str(t).strip() for t in s["tables"] if str(t).strip()]
            if picked:
                entry["tables"] = picked
        if s.get("cloud"):
            entry["cloud"] = str(s["cloud"]).strip().lower()
        if s.get("added_at"):
            entry["added_at"] = str(s["added_at"])
        clean.append(entry)
    return clean


def get_mv_sources() -> list[dict]:
    """Additional MV source locations to union into MV reads.

    Each entry: {"label": str, "catalog": str, "schema": str, "tables"?: list[str]}.
    Read order: in-process cache, Lakebase (v2 primary; empty list is authoritative
    so a cleared picker does not resurrect warehouse rows), local .settings file,
    Delta table, then DBFS (legacy). Never raises — returns [] on any error.
    """
    global _mv_sources_mem
    if _mv_sources_mem is not None:
        return _mv_sources_mem
    try:
        cfg = lb_get_config("mv_sources")
        if isinstance(cfg, list):
            _mv_sources_mem = _clean_mv_sources(cfg)
            return _mv_sources_mem
    except Exception:
        pass
    try:
        with open(_MV_SOURCES_FILE) as f:
            data = json.load(f)
        # A present file is authoritative for this worker — even when it's an empty
        # list (the user removed every source), so we don't resurrect stale entries.
        if isinstance(data, list):
            _mv_sources_mem = _clean_mv_sources(data)
            return _mv_sources_mem
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # File missing (fresh redeploy wiped .settings) — restore from warehouse fallbacks.
    delta = read_delta_mv_sources()
    if delta:
        _mv_sources_mem = _clean_mv_sources(delta)
        return _mv_sources_mem
    dbfs = read_dbfs_mv_sources()
    _mv_sources_mem = _clean_mv_sources(dbfs) if dbfs else []
    return _mv_sources_mem


def save_mv_sources(sources: list[dict]) -> None:
    """Persist additional MV sources. Lakebase is primary; file + Delta + DBFS
    stay as fallbacks. Preserves the optional per-source `tables` selection."""
    global _mv_sources_mem
    clean = _clean_mv_sources(sources)
    _mv_sources_mem = clean
    try:
        lb_set_config("mv_sources", clean)
    except Exception as e:
        logger.debug("Could not persist mv_sources to Lakebase (non-fatal): %s", e)
    try:
        os.makedirs(os.path.dirname(_MV_SOURCES_FILE), exist_ok=True)
        with open(_MV_SOURCES_FILE, "w") as f:
            json.dump(clean, f, indent=2)
    except OSError as e:
        logger.debug("Could not persist mv_sources to file (non-fatal): %s", e)
    write_delta_mv_sources(clean)
    write_dbfs_mv_sources(clean)
    logger.info("MV sources saved: %s", [s["label"] for s in clean])


def _mv_sources_table() -> str:
    """Fully-qualified Delta table for durable MV-source persistence, or '' when the
    storage location isn't configured yet."""
    catalog, schema = get_catalog_schema()
    if not catalog or not schema:
        return ""
    return f"`{catalog}`.`{schema}`.`app_mv_sources`"


def _ensure_mv_sources_table() -> None:
    """Create the durable MV-sources table if it doesn't exist, and add the cloud /
    added_at columns to a pre-existing table (migration for deployments created before
    those columns were introduced)."""
    table = _mv_sources_table()
    if not table:
        return
    execute_write(
        f"CREATE TABLE IF NOT EXISTS {table} "
        f"(label STRING NOT NULL, catalog STRING NOT NULL, schema STRING NOT NULL, "
        f"tables STRING, cloud STRING, added_at STRING, updated_at TIMESTAMP) USING DELTA",
        None,
    )
    # Best-effort column adds for tables created before cloud/added_at existed.
    for col in ("cloud STRING", "added_at STRING"):
        try:
            execute_write(f"ALTER TABLE {table} ADD COLUMNS ({col})", None)
        except Exception:
            pass  # column already present


def read_delta_mv_sources() -> list[dict]:
    """MV sources from the durable Delta table (survives redeploys). [] on any error."""
    try:
        table = _mv_sources_table()
        if not table:
            return []
        _ensure_mv_sources_table()
        rows = execute_query(f"SELECT label, catalog, schema, tables, cloud, added_at FROM {table}", None, no_cache=True)
        out: list[dict] = []
        for r in (rows or []):
            entry = {"label": r.get("label"), "catalog": r.get("catalog"), "schema": r.get("schema")}
            raw = r.get("tables")
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list) and parsed:
                        entry["tables"] = [str(x) for x in parsed]
                except (json.JSONDecodeError, TypeError):
                    pass
            if r.get("cloud"):
                entry["cloud"] = r["cloud"]
            if r.get("added_at"):
                entry["added_at"] = r["added_at"]
            out.append(entry)
        return out
    except Exception as e:
        logger.debug("Could not read mv_sources from Delta (non-fatal): %s", e)
        return []


def write_delta_mv_sources(sources: list[dict]) -> None:
    """Persist MV sources to the durable Delta table (replace-all). Best-effort.
    `added_at` is carried on each source so its original add-time survives the
    replace-all rewrite; only genuinely new sources get a fresh timestamp (set by
    the caller)."""
    try:
        table = _mv_sources_table()
        if not table:
            return
        _ensure_mv_sources_table()
        execute_write(f"DELETE FROM {table}", None)
        for s in sources:
            execute_write(
                f"INSERT INTO {table} (label, catalog, schema, tables, cloud, added_at, updated_at) "
                f"VALUES (:label, :catalog, :schema, :tables, :cloud, :added_at, current_timestamp())",
                {
                    "label": s["label"], "catalog": s["catalog"], "schema": s["schema"],
                    "tables": json.dumps(s["tables"]) if isinstance(s.get("tables"), list) else None,
                    "cloud": s.get("cloud"),
                    "added_at": s.get("added_at"),
                },
            )
        logger.info("MV sources persisted to Delta table (%d source(s))", len(sources))
    except Exception as e:
        logger.warning("Could not persist mv_sources to Delta (non-fatal): %s", e)


def write_dbfs_mv_sources(sources: list[dict]) -> None:
    """Persist MV sources to DBFS so they survive git redeploys. Best-effort."""
    try:
        import base64
        existing = _read_dbfs_settings()
        existing["mv_sources"] = sources
        w = get_workspace_client()
        content = base64.b64encode(json.dumps(existing).encode()).decode("ascii")
        w.api_client.do("POST", "/api/2.0/dbfs/put",
                        body={"path": _DBFS_OVERRIDE_PATH, "contents": content, "overwrite": True})
        logger.debug("DBFS mv_sources written (%d source(s))", len(sources))
    except Exception as e:
        logger.debug("Could not write DBFS mv_sources (non-fatal): %s", e)


def read_dbfs_mv_sources() -> list[dict]:
    """Return MV sources persisted to DBFS, or [] if absent."""
    val = _read_dbfs_settings().get("mv_sources")
    return val if isinstance(val, list) else []


def get_local_source_label() -> str:
    """Label for THIS workspace's own rows in the unified `source_label` column.

    Uses the workspace name from the host (matches the app's other naming), with
    the workspace id and a generic fallback so the column is never blank.
    """
    try:
        w = get_workspace_client()
        host = (w.config.host or "").replace("https://", "").replace("http://", "")
        name = host.split(".")[0] if host else ""
        if name:
            return name
    except Exception:
        pass
    wsid = os.getenv("DATABRICKS_WORKSPACE_ID", "").strip()
    return f"workspace {wsid}" if wsid else "This workspace"


# Request-scoped selection of MV source labels to filter reads by. Set per request
# by UserAuthMiddleware from the `source_labels` query param. Empty = no filter
# (all sources shown) — the default. Only meaningful when additional sources are
# configured, since `source_label` only exists on the `<name>__unified` views.
_source_labels: ContextVar[list[str]] = ContextVar("source_labels", default=[])


def set_source_labels(labels: list[str]) -> object:
    """Set the current request's source-label selection; returns a reset token."""
    return _source_labels.set(list(labels or []))


def reset_source_labels(token: object) -> None:
    try:
        _source_labels.reset(token)  # type: ignore[arg-type]
    except Exception:
        pass


class SourceScopeUnsupportedError(Exception):
    """A selected shared source does not publish a table required by this view."""

    code = "SOURCE_SCOPE_UNSUPPORTED"

    def __init__(
        self,
        message: str = (
            "None of the selected sources publish the managed data required by this view."
        ),
    ):
        super().__init__(message)


def selected_source_labels() -> list[str]:
    """Non-blank source labels selected for the current request."""
    return [str(x) for x in _source_labels.get() if str(x).strip()]


def source_labels_active() -> bool:
    """True when this request narrowed MV reads with ?source_labels=."""
    return bool(selected_source_labels())


def local_source_is_selected() -> bool:
    """Whether request scope includes this workspace's local managed data.

    An empty selection means the normal all-sources scope. With an explicit
    selection, local-only integrations must not leak into a shared-source-only
    response.
    """
    labels = selected_source_labels()
    if not labels:
        return True
    local_label = get_local_source_label().strip()
    return any(label.strip() == local_label for label in labels)


def selected_sources_publish(required_tables: str | list[str] | set[str] | tuple[str, ...]) -> bool:
    """True when the current source selection can serve these gold/unified tables.

    Empty selection means all sources. The local source always publishes.
    A shared source publishes a table when it did not restrict ``tables``, or
    the table is in its declared list, AND a unified view exists for it.
    """
    if isinstance(required_tables, str):
        required = {required_tables} if required_tables.strip() else set()
    else:
        required = {str(t).strip() for t in required_tables if str(t).strip()}
    if not required:
        return True
    labels = selected_source_labels()
    if not labels:
        return True
    local_label = get_local_source_label().strip()
    if any(label.strip() == local_label for label in labels):
        return True
    configured = {str(source.get("label") or ""): source for source in get_mv_sources()}
    unified = set(get_unified_view_tables() or [])
    for label in labels:
        source = configured.get(label)
        if not isinstance(source, dict):
            # Unknown labels do not publish; querying them would look like $0.
            continue
        declared = source.get("tables")
        if not isinstance(declared, list):
            # Unrestricted share: only if the required gold actually exists.
            if required.issubset(unified):
                return True
            continue
        published = {str(table) for table in declared}
        if required.issubset(published) and required.issubset(unified):
            return True
    return False


def source_scope_unavailable(
    required_tables: str | list[str] | set[str] | tuple[str, ...],
    reason_detail: str,
) -> dict[str, Any] | None:
    """Typed unavailable payload when selected sources cannot serve ``required_tables``."""
    if not selected_source_labels() or selected_sources_publish(required_tables):
        return None
    return {
        "available": False,
        "reason": "shared_scope_unsupported",
        "reason_detail": reason_detail,
        "error_code": "SOURCE_SCOPE_UNSUPPORTED",
    }


def _mv_tables_referenced_by_template(mv_query: str) -> set[str]:
    """Managed-table names referenced by an unformatted MV SQL template."""
    names = set(re.findall(r"`\{catalog\}`\.`\{schema\}`\.`([^`]+)`", mv_query))
    try:
        from backend.materialized_views import _MV_TABLES
        return names.intersection(set(_MV_TABLES))
    except Exception:
        return names


def source_label_filter_clause(mv_query: str | None = None) -> str:
    """`` AND source_label IN ('a','b') `` for the current selection, else ''.

    Single-quote-escaped. Appended into the MV templates' existing `{ws_filter}`
    slot by _get_mv_query, so it filters the unified views' rows by their source.
    When a template is supplied, raises SourceScopeUnsupportedError if none of
    the selected sources publish the referenced gold/unified tables.
    """
    labels = selected_source_labels()
    if not labels:
        return ""
    if mv_query is not None:
        referenced = _mv_tables_referenced_by_template(mv_query)
        if referenced and not selected_sources_publish(referenced):
            raise SourceScopeUnsupportedError(
                "None of the selected sources publish the managed data required by this view."
            )
    quoted = ", ".join("'" + x.replace("'", "''") + "'" for x in labels)
    return f" AND source_label IN ({quoted})"


def apply_mv_overrides(sql: str, catalog: str, schema: str) -> str:
    """Rewrite default MV table references in a SQL string, in two layers:

      1. Explicit per-table overrides (get_mv_table_overrides) — user-set paths.
      2. Additional-MV-source unions: when extra sources are configured, each MV
         table is read through its `<name>__unified` view (built by
         rebuild_unified_views), which UNION ALLs local + shared rows and tags
         each with a source_label column. Local data is always included.

    Never routes a table through its own unified view when that view does not
    apply here (the view DDL itself references base tables directly and is built
    without going through this function, so there is no recursion).
    """
    overrides = dict(get_mv_table_overrides())
    # Route reads only through unified views that were actually built (never to a
    # missing __unified view). Empty when no sources are configured.
    for t in get_unified_view_tables():
        # Don't override a table the user already remapped explicitly.
        overrides.setdefault(t, f"`{catalog}`.`{schema}`.`{t}{MV_UNIFIED_SUFFIX}`")
    if not overrides:
        return sql
    for logical_name, full_path in overrides.items():
        sql = sql.replace(f"`{catalog}`.`{schema}`.`{logical_name}`", full_path)
        sql = sql.replace(f"{catalog}.{schema}.{logical_name}", full_path)
    return sql


def get_catalog_schema_info() -> dict:
    """Return catalog/schema info for the settings read-only display."""
    catalog, schema = get_catalog_schema()
    if os.getenv("COST_OBS_CATALOG", "").strip() and os.getenv("COST_OBS_SCHEMA", "").strip():
        source = "env"
    else:
        try:
            cfg = lb_get_config("catalog_schema")
            if isinstance(cfg, dict) and cfg.get("catalog") and cfg.get("schema"):
                source = "lakebase"
            elif os.path.exists(_CATALOG_OVERRIDE_FILE):
                source = "override"
            else:
                source = "env"
        except Exception:
            source = "override" if os.path.exists(_CATALOG_OVERRIDE_FILE) else "env"
    return {"catalog": catalog, "schema": schema, "source": source}


# Dedicated warehouse configuration
DEDICATED_WAREHOUSE_NAME = "Cost Observability App"
DEDICATED_WAREHOUSE_SIZE = "Large"  # Large for 14+ parallel queries
DEDICATED_WAREHOUSE_MIN_CLUSTERS = 1
DEDICATED_WAREHOUSE_MAX_CLUSTERS = 2
DEDICATED_WAREHOUSE_AUTO_STOP_MINS = 10

# Bounded TTL cache for query results (2 hour TTL, max 200 entries)
# Using cachetools.TTLCache to prevent unbounded memory growth
_CACHE_MAX_SIZE = 200  # Max number of cached queries
_CACHE_TTL = 2 * 60 * 60  # 2 hours
_query_cache: TTLCache = TTLCache(maxsize=_CACHE_MAX_SIZE, ttl=_CACHE_TTL)

# SQL connection timeout in seconds
# Set high to accommodate slow system table scans (system.query.history 30-day range)
_CONNECTION_TIMEOUT = 300


def clear_query_cache(pattern: str | None = None) -> int:
    """Clear the query cache.

    Args:
        pattern: Optional string pattern to match cache keys.
                 If provided, only clears matching entries.
                 If None, clears entire cache.

    Returns:
        Number of entries cleared
    """
    global _query_cache
    if pattern is None:
        count = len(_query_cache)
        _query_cache.clear()
        logger.info(f"Cleared entire query cache ({count} entries)")
        return count
    else:
        # Clear entries matching pattern
        keys_to_clear = [k for k in _query_cache.keys() if pattern in k]
        for key in keys_to_clear:
            _query_cache.pop(key, None)
        logger.info(f"Cleared {len(keys_to_clear)} cache entries matching '{pattern}'")
        return len(keys_to_clear)


# ── Delta shared response cache ───────────────────────────────────────────────
# Cross-worker cache for expensive bundle payloads.  The per-process TTLCache
# above is not shared across FastAPI workers, so every worker re-computes the
# same expensive bundle on cache miss.  Storing results in a small Delta table
# lets any worker's computation be reused by all others.
#
# TTL policy (set by callers):
#   account-wide bundles (no ws filter): 1800s (30 min)
#   workspace-scoped bundles:             600s (10 min)

_CREATE_RESPONSE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS `{catalog}`.`{schema}`.`app_response_cache` (
  cache_key    STRING NOT NULL,
  endpoint     STRING,
  payload_json STRING,
  computed_at  TIMESTAMP,
  expires_at   TIMESTAMP
)
CLUSTER BY (cache_key)
"""

_response_cache_table_ready: bool = False


def _ensure_response_cache_table() -> bool:
    """Create app_response_cache if it doesn't exist. Returns False on any error.

    Before setup completes (no catalog configured): returns False silently (debug log).
    After setup (catalog configured but create fails): logs at WARNING — config drift.
    """
    global _response_cache_table_ready
    if _response_cache_table_ready:
        return True
    try:
        cat, sch = get_catalog_schema()
        if not cat or not sch:
            logger.debug("Delta cache table skipped — catalog not configured yet")
            return False
        tok = _user_token.set("")  # always as SP — app-owned table
        try:
            execute_query(
                _CREATE_RESPONSE_CACHE_TABLE.format(catalog=cat, schema=sch),
                no_cache=True,
            )
        finally:
            _user_token.reset(tok)
        _response_cache_table_ready = True
        return True
    except Exception as e:
        cat, sch = get_catalog_schema()
        if cat and sch:
            logger.warning("Could not ensure response cache table (config drift?): %s", e)
        else:
            logger.debug("Could not ensure response cache table (pre-setup): %s", e)
        return False


def bundle_cache_key(endpoint: str, start_date: str, end_date: str, workspace_ids: list[str] | None) -> str:
    """Stable MD5 cache key for a bundle request.

    Includes the active source-label selection so the data-source filter actually
    changes the cached result — without it, every filter selection hashed to the
    same key and the cached combined (all-sources) bundle was served regardless.
    With no explicit workspace_ids the saved pool scopes the query, so it is part
    of the key too.
    """
    from backend.workspace_filter import pool_cache_tag
    ws_part = ",".join(sorted(workspace_ids)) if workspace_ids else f"pool:{pool_cache_tag()}"
    labels = sorted(str(x) for x in _source_labels.get() if str(x).strip())
    src_part = ",".join(labels)
    raw = f"{endpoint}:{start_date}:{end_date}:{ws_part}:{src_part}"
    return hashlib.md5(raw.encode()).hexdigest()


# L1 in-process cache in front of the Delta SQL cache — avoids a warehouse
# round-trip on every bundle request when the payload is already warm.
_delta_l1: TTLCache = TTLCache(maxsize=50, ttl=300)


def _ensure_response_cache_table_lb() -> None:
    """Create the Lakebase response_cache table if missing (lazy, idempotent)."""
    lakebase_execute(
        "CREATE TABLE IF NOT EXISTS response_cache ("
        "cache_key TEXT PRIMARY KEY, endpoint TEXT, payload TEXT, "
        "expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


def delta_cache_get(key: str) -> dict | None:
    """Read a bundle payload from the L2 response cache. Returns None on miss/error.

    On the asyncio event loop: returns None immediately on L1 miss — never blocks.
    On daemon/sync threads: runs the SQL check with a 5-second timeout.
    Async handlers wanting a Delta read should call asyncio.to_thread(delta_cache_get, key).
    """
    if key in _delta_l1:
        return _delta_l1[key]
    # Skip the blocking SQL check when called from the event loop thread.
    # The blocking _cfwait below would freeze the event loop for up to 5 seconds;
    # return None (cache miss) instead and let the handler recompute.
    try:
        import asyncio as _asyncio
        _asyncio.get_running_loop()
        return None  # on event loop — don't block
    except RuntimeError:
        pass  # not on event loop — safe to proceed with blocking SQL
    try:
        cat, sch = get_catalog_schema()
        if not cat or not sch:
            return None
        import threading, contextvars
        from concurrent.futures import Future, wait as _cfwait
        ctx = contextvars.copy_context()

        def _run():
            # L2 primary: Lakebase response_cache (fast KV); warehouse fallback.
            if lakebase_available():
                rows = lakebase_query(
                    "SELECT payload FROM response_cache WHERE cache_key = %s AND expires_at > now() LIMIT 1",
                    (key,),
                )
                return [{"payload_json": rows[0]["payload"]}] if rows else []
            tok = _user_token.set("")
            try:
                return execute_query(
                    f"SELECT payload_json FROM `{cat}`.`{sch}`.`app_response_cache` "
                    f"WHERE cache_key = :key AND expires_at > CURRENT_TIMESTAMP() LIMIT 1",
                    {"key": key},
                    no_cache=True,
                )
            finally:
                _user_token.reset(tok)

        future: Future = Future()

        def _daemon_run(f=future, fn=ctx.run, inner=_run):
            try:
                f.set_result(fn(inner))
            except Exception as e:
                f.set_exception(e)

        threading.Thread(target=_daemon_run, daemon=True, name="sql-delta-cache-get").start()
        done, _ = _cfwait([future], timeout=5.0)
        if not done:
            logger.debug("Delta cache SQL read timed out after 5s — treating as cache miss")
            return None
        rows = future.result()
        if rows and rows[0].get("payload_json"):
            import gzip, base64 as _b64
            result = json.loads(gzip.decompress(_b64.b64decode(rows[0]["payload_json"])).decode())
            _delta_l1[key] = result
            return result
    except Exception as e:
        logger.debug("Delta cache read failed (non-fatal): %s", e)
    return None


def delta_cache_put(key: str, endpoint: str, payload: dict, ttl_seconds: int = 600) -> None:
    """Write a bundle payload to the Delta response cache. Fails silently; one retry on conflict.

    L1 is updated synchronously and immediately. The Delta SQL write runs in a fire-and-forget
    daemon thread so the caller (async handler or daemon thread) is never blocked.
    """
    _delta_l1[key] = payload  # L1 update is always immediate

    def _write(_key=key, _endpoint=endpoint, _payload=payload, _ttl=ttl_seconds):
        import gzip, base64 as _b64
        compressed = _b64.b64encode(gzip.compress(json.dumps(_payload).encode())).decode("ascii")

        # L2 primary: Lakebase response_cache (fast upsert); warehouse fallback.
        if lakebase_available():
            try:
                _ensure_response_cache_table_lb()
                lakebase_execute(
                    "INSERT INTO response_cache (cache_key, endpoint, payload, expires_at) "
                    "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) "
                    "ON CONFLICT (cache_key) DO UPDATE "
                    "  SET endpoint = EXCLUDED.endpoint, payload = EXCLUDED.payload, expires_at = EXCLUDED.expires_at",
                    (_key, _endpoint, compressed, float(_ttl)),
                )
                lakebase_execute("DELETE FROM response_cache WHERE expires_at < now()")
            except Exception as e:
                logger.debug("Lakebase cache write failed (non-fatal): %s", e)
            return

        # Fallback: warehouse Delta table
        if not _ensure_response_cache_table():
            return
        cat, sch = get_catalog_schema()
        if not cat or not sch:
            return
        merge_sql = f"""MERGE INTO `{cat}`.`{sch}`.`app_response_cache` AS tgt
            USING (SELECT
                :key      AS cache_key,
                :endpoint AS endpoint,
                :payload  AS payload_json,
                CURRENT_TIMESTAMP() AS computed_at,
                TIMESTAMPADD(SECOND, {_ttl}, CURRENT_TIMESTAMP()) AS expires_at
            ) AS src
            ON tgt.cache_key = src.cache_key
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED BY TARGET THEN INSERT *"""
        merge_params = {"key": _key, "endpoint": _endpoint, "payload": compressed}
        for attempt in range(2):
            try:
                tok = _user_token.set("")
                try:
                    execute_query(
                        f"DELETE FROM `{cat}`.`{sch}`.`app_response_cache` WHERE expires_at < CURRENT_TIMESTAMP()",
                        no_cache=True,
                    )
                    execute_query(merge_sql, merge_params, no_cache=True)
                finally:
                    _user_token.reset(tok)
                return
            except Exception as e:
                if attempt == 0:
                    logger.debug("Delta cache write conflict, retrying: %s", e)
                else:
                    logger.debug("Delta cache write failed after retry (non-fatal): %s", e)

    threading.Thread(target=_write, daemon=True, name="delta-cache-put").start()


def delta_cache_invalidate(pattern: str | None = None) -> None:
    """Delete Delta cache entries, optionally filtered by endpoint prefix."""
    # Clear L1 in-process cache first (fast, always succeeds).
    # L1 keys are MD5 hashes with no endpoint info, so pattern-based selective
    # clearing is impossible — always clear the full L1 cache. It holds at most
    # 50 entries with a 5-min TTL so a full clear has negligible cost.
    _delta_l1.clear()
    # L2 primary: Lakebase response_cache
    if lakebase_available():
        try:
            if pattern:
                lakebase_execute("DELETE FROM response_cache WHERE endpoint LIKE %s", (f"{pattern}%",))
            else:
                lakebase_execute("DELETE FROM response_cache")
        except Exception as e:
            logger.debug("Lakebase cache invalidation failed (non-fatal): %s", e)
        return
    # Fallback: warehouse Delta table
    try:
        cat, sch = get_catalog_schema()
        if not cat or not sch:
            return
        tok = _user_token.set("")
        try:
            if pattern:
                execute_query(
                    f"DELETE FROM `{cat}`.`{sch}`.`app_response_cache` WHERE endpoint LIKE :pat",
                    {"pat": f"{pattern}%"},
                    no_cache=True,
                )
            else:
                execute_query(
                    f"DELETE FROM `{cat}`.`{sch}`.`app_response_cache`",
                    no_cache=True,
                )
        finally:
            _user_token.reset(tok)
    except Exception as e:
        logger.debug("Delta cache invalidation failed (non-fatal): %s", e)

# Singleton WorkspaceClient instance
_workspace_client: WorkspaceClient | None = None
_workspace_client_lock = threading.Lock()


def get_workspace_client() -> WorkspaceClient:
    """Get or create a singleton WorkspaceClient instance.

    Double-checked locking ensures only one thread runs WorkspaceClient() init
    (prevents concurrent auth fetches on restart). Failures are NOT cached —
    the next caller acquires the lock and retries from scratch, which is correct
    for transient errors (a permanent error cache bricked all SDK usage on any
    single transient init failure).
    """
    global _workspace_client

    if _workspace_client is not None:
        return _workspace_client

    with _workspace_client_lock:
        if _workspace_client is not None:
            return _workspace_client
        token = os.getenv("DATABRICKS_TOKEN")
        host = os.getenv("DATABRICKS_HOST")
        if token and host:
            _workspace_client = WorkspaceClient(host=host, token=token)
        else:
            _workspace_client = WorkspaceClient()
        logger.info("Created WorkspaceClient singleton")

    return _workspace_client


def get_user_workspace_client() -> WorkspaceClient:
    """Get a WorkspaceClient using the current request's OAuth token if available.

    Falls back to the SP singleton when no user token is in context.
    Used for SDK calls (warehouse listing, etc.) that should run as the user
    rather than the SP so they respect the user's permissions.
    """
    user_token = _user_token.get()
    if user_token and _auth_mode != "sp":
        host = os.getenv("DATABRICKS_HOST", "")
        if host:
            # Databricks Apps injects DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET for
            # the SP, so any WorkspaceClient() call also picks them up from env.  When we
            # ALSO pass token=user_token the SDK sees two auth methods ("pat" + "oauth") and
            # raises "more than one authorization method configured".
            #
            # Setting auth_type="pat" is the SDK-supported escape hatch: _validate() returns
            # early when auth_type is set (line 668 of config.py), and init_auth() then uses
            # the PAT credential provider, ignoring the M2M OAuth env vars.
            return WorkspaceClient(host=host, token=user_token, auth_type="pat")
    return get_workspace_client()


_account_client: AccountClient | None = None


def get_account_client() -> AccountClient:
    """Get or create a singleton AccountClient for account-level APIs (budgets, policies).

    Requires DATABRICKS_ACCOUNT_ID to be set in the environment. The accounts host is
    derived from the workspace host cloud unless DATABRICKS_ACCOUNT_HOST is set explicitly.
    Raises ValueError if the account ID is absent — callers catch and return a graceful error.
    """
    global _account_client
    if _account_client is not None:
        return _account_client

    account_id = os.getenv("DATABRICKS_ACCOUNT_ID", "").strip()
    if not account_id:
        # Fall back to auto-detecting the account ID from system.billing.usage so the
        # client works without a manually-set env var (the env var still takes precedence).
        try:
            rows = execute_query(
                "SELECT DISTINCT account_id FROM system.billing.usage "
                "WHERE usage_date >= CURRENT_DATE - 7 AND account_id IS NOT NULL LIMIT 1"
            )
            account_id = str(rows[0].get("account_id", "")).strip() if rows else ""
        except Exception as e:
            logger.debug("Could not auto-detect account_id from billing: %s", e)
    if not account_id:
        raise ValueError(
            "DATABRICKS_ACCOUNT_ID is not set and could not be auto-detected from "
            "system.billing.usage — set it explicitly in the app env / bundle variable"
        )

    accounts_host = os.getenv("DATABRICKS_ACCOUNT_HOST", "").strip()
    if not accounts_host:
        workspace_host = os.getenv("DATABRICKS_HOST", "")
        if "azuredatabricks.net" in workspace_host:
            accounts_host = "https://accounts.azuredatabricks.net"
        elif "gcp.databricks.com" in workspace_host:
            accounts_host = "https://accounts.gcp.databricks.com"
        else:
            accounts_host = "https://accounts.cloud.databricks.com"

    _account_client = AccountClient(host=accounts_host, account_id=account_id)
    logger.info("Created AccountClient singleton (host=%s, account_id=%s)", accounts_host, account_id)
    return _account_client


def ensure_dedicated_warehouse() -> tuple[str, str]:
    """Ensure a dedicated serverless SQL warehouse exists for the app.

    Creates a Large serverless warehouse if one doesn't exist with the expected name.
    Returns the warehouse ID and HTTP path.

    Returns:
        Tuple of (warehouse_id, http_path)
    """
    w = get_workspace_client()

    # Check if dedicated warehouse already exists
    logger.info(f"Checking for dedicated warehouse: {DEDICATED_WAREHOUSE_NAME}")
    existing_warehouses = list(w.warehouses.list())

    for warehouse in existing_warehouses:
        if warehouse.name == DEDICATED_WAREHOUSE_NAME:
            warehouse_id = warehouse.id
            http_path = f"/sql/1.0/warehouses/{warehouse_id}"
            logger.info(f"Found existing dedicated warehouse: {warehouse_id} ({warehouse.cluster_size})")

            # Check if warehouse needs to be started
            if warehouse.state in [State.STOPPED, State.STOPPING]:
                logger.info(f"Starting warehouse {warehouse_id}...")
                w.warehouses.start(warehouse_id)

            # Check if it's undersized and warn
            size_order = ["2X-Small", "X-Small", "Small", "Medium", "Large", "X-Large", "2X-Large", "3X-Large", "4X-Large"]
            current_idx = size_order.index(warehouse.cluster_size) if warehouse.cluster_size in size_order else -1
            target_idx = size_order.index(DEDICATED_WAREHOUSE_SIZE) if DEDICATED_WAREHOUSE_SIZE in size_order else 4

            if current_idx < target_idx:
                logger.warning(
                    f"Dedicated warehouse is sized {warehouse.cluster_size}, "
                    f"but {DEDICATED_WAREHOUSE_SIZE} is recommended. Consider resizing for better performance."
                )

            return warehouse_id, http_path

    # Create new dedicated warehouse
    logger.info(f"Creating dedicated serverless warehouse: {DEDICATED_WAREHOUSE_NAME} ({DEDICATED_WAREHOUSE_SIZE})")

    try:
        warehouse = w.warehouses.create(
            name=DEDICATED_WAREHOUSE_NAME,
            cluster_size=DEDICATED_WAREHOUSE_SIZE,
            warehouse_type=CreateWarehouseRequestWarehouseType.PRO,
            enable_serverless_compute=True,
            min_num_clusters=DEDICATED_WAREHOUSE_MIN_CLUSTERS,
            max_num_clusters=DEDICATED_WAREHOUSE_MAX_CLUSTERS,
            auto_stop_mins=DEDICATED_WAREHOUSE_AUTO_STOP_MINS,
            spot_instance_policy=SpotInstancePolicy.COST_OPTIMIZED,
        )

        warehouse_id = warehouse.id
        http_path = f"/sql/1.0/warehouses/{warehouse_id}"

        logger.info("=" * 60)
        logger.info("Created Dedicated SQL Warehouse")
        logger.info("=" * 60)
        logger.info(f"  Name: {DEDICATED_WAREHOUSE_NAME}")
        logger.info(f"  ID: {warehouse_id}")
        logger.info(f"  Size: {DEDICATED_WAREHOUSE_SIZE}")
        logger.info(f"  Type: Serverless")
        logger.info(f"  Min Clusters: {DEDICATED_WAREHOUSE_MIN_CLUSTERS}")
        logger.info(f"  Max Clusters: {DEDICATED_WAREHOUSE_MAX_CLUSTERS}")
        logger.info(f"  Auto-Stop: {DEDICATED_WAREHOUSE_AUTO_STOP_MINS} minutes")
        logger.info("=" * 60)

        return warehouse_id, http_path

    except Exception as e:
        logger.error(f"Failed to create dedicated warehouse: {e}")
        raise


def setup_warehouse_connection() -> str:
    """Set up the warehouse connection for the app.

    Priority:
    1. DATABRICKS_HTTP_PATH env var (explicit path set in app.yaml)
    2. DATABRICKS_WAREHOUSE_ID env var — injected by Databricks Apps when a
       sql_warehouse resource is declared with valueFrom in app.yaml

    The warehouse is fixed at deploy time and cannot be changed via the settings
    UI. To switch warehouses, update the app resource binding and redeploy.

    Returns:
        The HTTP path being used
    """
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")

    if http_path and http_path.lower() != "auto":
        logger.info(f"Using configured warehouse: {http_path}")
        return http_path

    # Databricks Apps sql_warehouse resource binding
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    if warehouse_id:
        http_path = f"/sql/1.0/warehouses/{warehouse_id}"
        os.environ["DATABRICKS_HTTP_PATH"] = http_path
        logger.info(f"Using warehouse from DATABRICKS_WAREHOUSE_ID resource: {http_path}")
        return http_path

    raise ValueError(
        "No SQL warehouse configured. Set DATABRICKS_HTTP_PATH to an explicit warehouse path "
        "(e.g. /sql/1.0/warehouses/<id>) in app.yaml, or add a sql_warehouse resource binding "
        "so DATABRICKS_WAREHOUSE_ID is injected automatically."
    )


def _get_cache_key(query: str, params: dict[str, Any] | None, *, tag: str | None = None) -> str:
    """Generate a cache key from query and params.

    When running under user authorization, the token hash is included so each
    user's results are cached independently (respects row/column-level security).

    Args:
        tag: Optional prefix for pattern-based cache invalidation (e.g. "use_case").
    """
    key_data = query + json.dumps(params or {}, sort_keys=True)
    token = _user_token.get()
    if token:
        # Use first 16 chars of token hash — enough to distinguish users without
        # exposing the token itself in log output or cache inspection.
        token_prefix = hashlib.md5(token.encode()).hexdigest()[:16]
        key_data = token_prefix + ":" + key_data
    hash_key = hashlib.md5(key_data.encode()).hexdigest()
    return f"{tag}:{hash_key}" if tag else hash_key


def _strip_host_scheme(host: str) -> str:
    """Strip https:// or http:// from a hostname."""
    if host.startswith("https://"):
        return host[8:]
    elif host.startswith("http://"):
        return host[7:]
    return host


def _is_scope_error(exc: Exception) -> bool:
    """Return True if exception indicates the token lacks the 'sql' OAuth scope."""
    msg = str(exc).lower()
    return (
        "required scopes" in msg
        or "does not have required scopes" in msg
        # Databricks SQL HTTP connector returns this when the bearer token
        # lacks the 'sql' OAuth scope — not a network error, a scope rejection.
        or "error during request to server" in msg
    )


def _is_permission_error(exc: Exception) -> bool:
    """Return True if exception indicates the user token lacks table/schema privileges."""
    msg = str(exc).lower()
    return any(s in msg for s in (
        "permission_denied", "insufficient_privileges", "not authorized",
        "user does not have", "does not have privilege",
    ))


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    """Get a Databricks SQL connection as a context manager.

    Auth priority:
    1. Per-request user token from x-forwarded-access-token (user authorization
       preview — set by UserAuthMiddleware when the feature is enabled).
    2. DATABRICKS_TOKEN env var (local dev with explicit PAT/token).
    3. SP OAuth via WorkspaceClient (standard Databricks Apps SP identity).
    """
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")

    if not http_path:
        # Startup warehouse resolution may have failed — try again lazily.
        # This handles cases where DATABRICKS_WAREHOUSE_ID is injected by the
        # Apps resource binding but setup_warehouse_connection() failed at boot.
        try:
            http_path = setup_warehouse_connection()
        except Exception as e:
            raise ValueError(
                "SQL warehouse not configured. "
                "Add a SQL warehouse resource (key: sql-warehouse) in the Databricks Apps UI, "
                "or set DATABRICKS_HTTP_PATH explicitly in app.yaml."
            ) from e

    # 1. User authorization (Databricks Apps preview feature)
    user_token = _user_token.get()
    if user_token:
        host = os.getenv("DATABRICKS_HOST", "")
        if not host:
            w = get_workspace_client()
            host = w.config.host or ""
        conn = sql.connect(
            server_hostname=_strip_host_scheme(host),
            http_path=http_path,
            access_token=user_token,
            _socket_timeout=_CONNECTION_TIMEOUT,
        )
        try:
            yield conn
        finally:
            conn.close()
        return

    dev_token = os.getenv("DATABRICKS_TOKEN")
    dev_host = os.getenv("DATABRICKS_HOST")

    if dev_token and dev_host:
        # 2. Local development with explicit credentials
        conn = sql.connect(
            server_hostname=_strip_host_scheme(dev_host),
            http_path=http_path,
            access_token=dev_token,
            _socket_timeout=_CONNECTION_TIMEOUT,
        )
    else:
        # 3. Databricks App environment — use SP OAuth token from SDK
        w = get_workspace_client()
        config = w.config
        server_hostname = _strip_host_scheme(config.host)

        # config.authenticate() returns {"Authorization": "Bearer <token>"}
        headers = config.authenticate()
        access_token = headers.get("Authorization", "").replace("Bearer ", "")
        if not access_token:
            raise ValueError("Failed to get OAuth token from WorkspaceClient")

        conn = sql.connect(
            server_hostname=server_hostname,
            http_path=http_path,
            access_token=access_token,
            _socket_timeout=_CONNECTION_TIMEOUT,
        )

    try:
        yield conn
    finally:
        conn.close()


def execute_write(query: str, params: dict[str, Any] | None = None) -> int:
    """Execute a write operation (INSERT/UPDATE/DELETE) and return affected rows.

    Does not cache results as these are write operations.
    Delta tables auto-commit every DML statement; explicit commit() is not needed
    and may raise NotSupportedError on some connector versions.
    """
    start_time = time.time()

    def _run(force_sp: bool = False) -> int:
        ctx_tok = _user_token.set("") if force_sp else None
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    return cursor.rowcount if cursor.rowcount is not None else 0
        finally:
            if ctx_tok is not None:
                _user_token.reset(ctx_tok)

    try:
        affected_rows = _run()
        if _user_token.get() and _auth_mode == "unknown":
            _lock_auth_mode("user")
    except Exception as exc:
        if _is_scope_error(exc) and _user_token.get():
            _lock_auth_mode("sp")
            affected_rows = _run(force_sp=True)
        elif _is_permission_error(exc) and _user_token.get():
            logger.warning(f"User token permission denied on write, retrying as SP: {exc}")
            affected_rows = _run(force_sp=True)
        else:
            raise

    elapsed = time.time() - start_time
    _sql_tag = " ".join(query.split())[:60]
    logger.info(f"Write query executed in {elapsed:.2f}s ({affected_rows} rows affected) [{_sql_tag}]")
    return affected_rows


def execute_query(query: str, params: dict[str, Any] | None = None, *, cache_tag: str | None = None, no_cache: bool = False) -> list[dict[str, Any]]:
    """Execute a SQL query and return results as a list of dicts.

    Results are cached for 10 minutes to reduce load on Databricks.

    Args:
        cache_tag: Optional tag for pattern-based cache invalidation (e.g. "use_case").
        no_cache: If True, skip cache read/write entirely (use for security-sensitive queries).
    """
    start_time = time.time()

    # Check cache first (TTLCache handles expiration automatically)
    if not no_cache:
        cache_key = _get_cache_key(query, params, tag=cache_tag)
        if cache_key in _query_cache:
            logger.info(f"Cache hit - returned in {(time.time() - start_time)*1000:.0f}ms")
            return _query_cache[cache_key]

    def _run(force_sp: bool = False) -> list[dict[str, Any]]:
        """Execute the query. force_sp=True forces SP identity for this call."""
        ctx_tok = _user_token.set("") if force_sp else None
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    if cursor.description is not None:
                        columns = [desc[0] for desc in cursor.description]
                        rows = cursor.fetchall()
                        return [dict(zip(columns, row)) for row in rows]
                    return []
        finally:
            if ctx_tok is not None:
                _user_token.reset(ctx_tok)

    # Execute query — detect and lock auth mode on first use.
    try:
        result = _run()
        # Lock to user mode on first successful user-token query
        if _user_token.get() and _auth_mode == "unknown":
            _lock_auth_mode("user")
    except Exception as exc:
        if _is_scope_error(exc) and _user_token.get():
            # Token present but lacks sql scope — lock to SP for all future requests
            _lock_auth_mode("sp")
            result = _run(force_sp=True)
        elif _is_permission_error(exc) and _user_token.get():
            # Token has sql scope but user lacks table privileges — retry as SP.
            # Don't lock permanently: this may be table-specific and the admin
            # can resolve it by setting Force SP in Settings → Permissions.
            logger.warning(f"User token permission denied, retrying as SP: {exc}")
            result = _run(force_sp=True)
        else:
            raise

    # Cache the result (TTLCache handles expiration automatically)
    if not no_cache:
        cache_key = _get_cache_key(query, params, tag=cache_tag)
        _query_cache[cache_key] = result
    elapsed = time.time() - start_time
    _sql_tag = " ".join(query.split())[:60]
    logger.info(f"Query [{_sql_tag}] executed in {elapsed:.2f}s ({len(result)} rows)")
    return result


def get_auth_status() -> dict:
    """Return current auth mode for the settings UI auth indicator.

    Reads the in-process auth state without touching the database.
    """
    token = _user_token.get()
    locked_to_sp = _auth_mode == "sp"
    token_present = bool(token)
    user_token_active = token_present and not locked_to_sp

    if user_token_active:
        identity = "user_oauth"
    else:
        identity = "service_principal"

    # Attempt to decode JWT claims (no verification — informational only)
    has_sql_scope: bool | None = None
    user_email: str | None = None
    token_scopes: list[str] = []
    if token:
        try:
            import base64
            payload_b64 = token.split(".")[1]
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            scp = payload.get("scp", payload.get("scope", ""))
            token_scopes = scp.split() if isinstance(scp, str) else list(scp)
            has_sql_scope = "sql" in token_scopes
            user_email = payload.get("upn") or payload.get("email") or payload.get("preferred_username") or None
        except Exception:
            pass

    # Check whether a manual override is saved on disk
    override_mode: str | None = None
    try:
        if os.path.exists(_AUTH_MODE_OVERRIDE_FILE):
            with open(_AUTH_MODE_OVERRIDE_FILE) as f:
                override_mode = json.load(f).get("mode")
    except Exception:
        pass

    return {
        # Simplified fields used by the header badge
        "user_token_active": user_token_active,
        "identity": identity,
        "locked_to_sp": locked_to_sp,
        "has_sql_scope": has_sql_scope,
        # Richer fields for the Permissions settings panel
        "auth_mode": _auth_mode,          # "unknown" | "user" | "sp"
        "token_present": token_present,   # OAuth header received from Databricks Apps
        "token_scopes": token_scopes,     # scopes decoded from the JWT
        "user_email": user_email,         # email from JWT claims
        "override_mode": override_mode,   # "sp" | "auto" | None (manual override on disk)
    }


# ── Lakebase connection pool ──────────────────────────────────────────────────
# psycopg3 + psycopg_pool with OAuth token rotation.
# Tokens expire ~60 min; the pool's connect factory generates a fresh credential
# on every NEW physical connection, so tokens are always valid for the life of
# the connection.  PgBouncer is not compatible with OAuth roles — connect directly
# to the Lakebase endpoint.

try:
    import psycopg
    import psycopg_pool
    _PSYCOPG_AVAILABLE = True

    class _OAuthLakebaseConnection(psycopg.Connection):
        """psycopg3 connection that mints a fresh Lakebase OAuth token per connect.

        Injecting credentials via a custom connection_class (rather than
        overriding ConnectionPool._connect) lets psycopg_pool run its normal
        connection lifecycle — otherwise the pool's own bookkeeping (e.g.
        `_created_at`) is never set and getconn() raises AttributeError.
        """

        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
            endpoint = os.environ.get("ENDPOINT_NAME", "")
            kwargs["password"] = mint_lakebase_token(endpoint)
            return super().connect(conninfo, **kwargs)
except ImportError:
    _PSYCOPG_AVAILABLE = False
    logger.debug("psycopg/psycopg_pool not installed — Lakebase disabled")

_lakebase_pool: "psycopg_pool.ConnectionPool | None" = None
_lakebase_pool_lock = __import__("threading").Lock()
# A Lakebase that is attached but unusable (no role yet, endpoint suspended)
# must not cost every request psycopg_pool's default 30s wait.
_LAKEBASE_GETCONN_TIMEOUT_S = float(os.getenv("LAKEBASE_CONNECT_TIMEOUT_S", "5"))
_LAKEBASE_COOLDOWN_S = 60
_lakebase_down_until = 0.0


def mint_lakebase_token(endpoint: str) -> str:
    """Mint a Lakebase OAuth password for the current identity.

    Prefers ``WorkspaceClient.postgres.generate_database_credential`` (SDK
    0.28+). Job environments that still ship an older SDK fall back to the
    raw credentials API the setup job already uses.
    """
    if not endpoint:
        raise RuntimeError("ENDPOINT_NAME is required to mint a Lakebase credential")
    w = get_workspace_client()
    postgres = getattr(w, "postgres", None)
    generate = getattr(postgres, "generate_database_credential", None) if postgres is not None else None
    if callable(generate):
        cred = generate(endpoint=endpoint)
        token = getattr(cred, "token", None) or (cred.get("token") if isinstance(cred, dict) else None)
        if token:
            return str(token)
    resp = w.api_client.do(
        "POST", "/api/2.0/postgres/credentials", body={"endpoint": endpoint}
    )
    token = (resp or {}).get("token", "")
    if not token:
        raise RuntimeError("Lakebase credential API returned no token")
    return str(token)


def _resolve_lakebase_runtime_env() -> None:
    """Fill PGHOST/PGUSER when the Apps binding is absent (setup job path)."""
    if os.environ.get("PGHOST"):
        return
    endpoint = os.environ.get("ENDPOINT_NAME", "")
    if not endpoint:
        return
    w = get_workspace_client()
    try:
        ep = w.api_client.do("GET", f"/api/2.0/postgres/{endpoint}")
        host = ((ep or {}).get("status") or {}).get("hosts", {}).get("host", "")
        if host:
            os.environ["PGHOST"] = host
    except Exception as e:
        logger.warning("Could not resolve PGHOST from %s: %s", endpoint, e)
    os.environ.setdefault("PGDATABASE", "databricks_postgres")
    if os.environ.get("PGUSER"):
        return
    try:
        me = w.current_user.me()
        if me.user_name:
            os.environ["PGUSER"] = me.user_name
    except Exception as e:
        logger.warning("Could not resolve PGUSER: %s", e)


_endpoint_resolution_done = False
_endpoint_unresolved = False
_endpoint_retry_at = float("inf")
_endpoint_lock = threading.Lock()


def _find_endpoint_for_host(host: str) -> str:
    """Return the Lakebase endpoint name whose host matches PGHOST, or ""."""
    postgres = get_workspace_client().postgres
    for project in postgres.list_projects():
        if not project.name:
            continue
        for branch in postgres.list_branches(parent=project.name):
            if not branch.name:
                continue
            for ep in postgres.list_endpoints(parent=branch.name):
                hosts = ep.status.hosts if ep.status else None
                if hosts and host in (
                    hosts.host, hosts.read_write_pooled_host,
                    hosts.read_only_host, hosts.read_only_pooled_host,
                ):
                    return ep.name or ""
    return ""


def _ensure_lakebase_endpoint() -> bool:
    """Make sure ENDPOINT_NAME is set when PGHOST is. Returns False if unusable.

    A Lakebase resource added through the Apps UI injects PGHOST but not
    ENDPOINT_NAME. Without it every connect fails to mint a token and each
    request waits out the pool timeout, so resolve it once from PGHOST and
    disable Lakebase outright when no endpoint matches.
    """
    global _endpoint_resolution_done, _endpoint_unresolved, _endpoint_retry_at
    if os.environ.get("ENDPOINT_NAME"):
        return True
    if _endpoint_resolution_done and time.monotonic() >= _endpoint_retry_at:
        _endpoint_resolution_done = False
    if _endpoint_resolution_done:
        return not _endpoint_unresolved
    with _endpoint_lock:
        if not _endpoint_resolution_done:
            host = os.environ.get("PGHOST", "")
            endpoint = ""
            _endpoint_retry_at = float("inf")
            try:
                endpoint = _find_endpoint_for_host(host)
            except Exception as e:
                _endpoint_retry_at = time.monotonic() + 300
                logger.warning("Could not look up the Lakebase endpoint for PGHOST (retrying in 5 min): %s", e)
            if endpoint:
                _endpoint_unresolved = False
                os.environ["ENDPOINT_NAME"] = endpoint
                logger.info("Resolved ENDPOINT_NAME=%s from PGHOST", endpoint)
            else:
                _endpoint_unresolved = True
                logger.warning(
                    "PGHOST is set but ENDPOINT_NAME is not, and no Lakebase endpoint "
                    "visible to the app matches %s. Lakebase is disabled; set ENDPOINT_NAME="
                    "projects/<project>/branches/<branch>/endpoints/<endpoint> in the app config.",
                    host,
                )
            _endpoint_resolution_done = True
    return not _endpoint_unresolved


def _lakebase_conninfo() -> str:
    _resolve_lakebase_runtime_env()
    host = os.environ.get("PGHOST", "")
    if not host or not _ensure_lakebase_endpoint():
        return ""
    port = os.environ.get("PGPORT", "5432")
    database = os.environ.get("PGDATABASE", "")
    user = os.environ.get("PGUSER", "")
    sslmode = os.environ.get("PGSSLMODE", "require")
    schema = os.environ.get("COST_OBS_SCHEMA", "cost_obs")
    gold = os.environ.get("COST_OBS_PG_GOLD_SCHEMA", "cost_obs_gold")
    return (
        f"host={host} port={port} dbname={database} user={user} sslmode={sslmode} "
        f"connect_timeout=10 options='-c search_path={gold},{schema}'"
    )


def _get_lakebase_pool() -> "psycopg_pool.ConnectionPool | None":
    """Return the singleton Lakebase pool, initialising it on first call.

    Returns None when Lakebase is not configured (PGHOST unset) or psycopg
    is not installed.  All callers must handle None gracefully.
    """
    global _lakebase_pool
    if not _PSYCOPG_AVAILABLE:
        return None
    conninfo = _lakebase_conninfo()
    if not conninfo:
        return None
    if _lakebase_pool is not None:
        return _lakebase_pool
    with _lakebase_pool_lock:
        if _lakebase_pool is not None:
            return _lakebase_pool
        # Explicitly SET search_path on every pooled connection. The conninfo
        # `options=-c search_path=...` is not reliably applied (observed: unqualified
        # DDL fails with "no schema has been selected"), so we set it here where it
        # always takes. Falls back to public so unqualified reads still resolve.
        _pg_schema = os.environ.get("COST_OBS_SCHEMA", "cost_obs")
        _pg_gold = os.environ.get("COST_OBS_PG_GOLD_SCHEMA", "cost_obs_gold")

        def _configure_conn(conn) -> None:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_pg_gold}"')
                cur.execute(f'SET search_path TO "{_pg_gold}", "{_pg_schema}", public')
            conn.commit()

        try:
            # Inject OAuth credentials via a custom connection_class (see
            # _OAuthLakebaseConnection). This preserves psycopg_pool's connection
            # lifecycle — overriding pool._connect bypasses it and breaks getconn().
            pool = psycopg_pool.ConnectionPool(
                conninfo=conninfo,
                min_size=1,
                max_size=10,
                open=False,
                connection_class=_OAuthLakebaseConnection,
                configure=_configure_conn,
            )
            pool.open(wait=False)
            _lakebase_pool = pool
            logger.info("Lakebase connection pool initialised (min=1 max=10)")
        except Exception as e:
            logger.warning("Could not initialise Lakebase pool (non-fatal): %s", e)
            return None
    return _lakebase_pool


@contextmanager
def get_pg_conn() -> "Generator[psycopg.Connection, None, None]":
    """Acquire a Lakebase connection from the pool as a context manager.

    Commits on clean exit, rolls back on exception.  Raises RuntimeError when
    Lakebase is not configured so callers can fall back gracefully.
    """
    global _lakebase_down_until
    pool = _get_lakebase_pool()
    if pool is None:
        raise RuntimeError("Lakebase not available — PGHOST not configured or psycopg not installed")
    if time.monotonic() < _lakebase_down_until:
        raise RuntimeError("Lakebase unavailable — recent connection failure, retrying shortly")
    try:
        conn = pool.getconn(timeout=_LAKEBASE_GETCONN_TIMEOUT_S)
    except Exception as e:
        _lakebase_down_until = time.monotonic() + _LAKEBASE_COOLDOWN_S
        logger.warning(
            "Lakebase connection failed; serving fallbacks for %ss: %s", _LAKEBASE_COOLDOWN_S, e,
        )
        raise RuntimeError(f"Lakebase connection failed: {e}") from e
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def lakebase_healthy() -> bool:
    """Return True if the Lakebase pool is up and can execute a trivial query."""
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception as e:
        logger.debug("Lakebase health check failed: %s", e)
        return False


# ── Lakebase-backed app state ─────────────────────────────────────────────────
# These functions mirror the DBFS helpers above but read/write from Lakebase.
# On read, they fall back to DBFS if Lakebase is unavailable (pre-setup pods).
# On write, they write to both so a rollback to V1 doesn't lose state.

def lb_get_config(key: str) -> dict | str | None:
    """Read a config value from app_config. Returns parsed JSON value or None."""
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
                row = cur.fetchone()
        if row:
            import psycopg.types.json as _pj
            val = row[0]
            return val.obj if hasattr(val, "obj") else val
    except Exception as e:
        logger.debug("lb_get_config(%s) failed (non-fatal): %s", key, e)
    return None


def lb_set_config(key: str, value: dict | str | list | None) -> None:
    """Upsert a config value into app_config. Best-effort, non-fatal."""
    try:
        import psycopg.types.json as _pj
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_config (key, value, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (key) DO UPDATE
                      SET value = EXCLUDED.value,
                          updated_at = EXCLUDED.updated_at
                    """,
                    (key, psycopg.types.json.Jsonb(value)),
                )
    except Exception as e:
        logger.debug("lb_set_config(%s) failed (non-fatal): %s", key, e)


def lb_read_setup_complete() -> bool:
    """Return True if setup_complete step is recorded in Lakebase."""
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM setup_state WHERE step = 'setup_complete'")
                return cur.fetchone() is not None
    except Exception as e:
        logger.debug("lb_read_setup_complete failed (non-fatal): %s", e)
    return False


def lb_write_setup_complete() -> None:
    """Record setup_complete in Lakebase. Best-effort, non-fatal."""
    try:
        from datetime import datetime, timezone
        import psycopg.types.json as _pj
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO setup_state (step, completed_at, metadata)
                    VALUES ('setup_complete', %s, %s)
                    ON CONFLICT (step) DO UPDATE
                      SET completed_at = EXCLUDED.completed_at,
                          metadata = EXCLUDED.metadata
                    """,
                    (datetime.now(timezone.utc), psycopg.types.json.Jsonb({})),
                )
        logger.info("Lakebase setup_complete flag written")
    except Exception as e:
        logger.debug("lb_write_setup_complete failed (non-fatal): %s", e)


def lb_read_build_state() -> dict | None:
    """Return the most recent build progress payload from Lakebase, or None."""
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM build_progress ORDER BY recorded_at DESC LIMIT 1"
                )
                row = cur.fetchone()
        if row:
            val = row[0]
            return val.obj if hasattr(val, "obj") else val
    except Exception as e:
        logger.debug("lb_read_build_state failed (non-fatal): %s", e)
    return None


def lb_write_build_state(state: dict) -> None:
    """Append a build progress snapshot to Lakebase. Best-effort, non-fatal."""
    try:
        import psycopg.types.json as _pj
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO build_progress (status, payload) VALUES (%s, %s)",
                    (state.get("status", "unknown"), psycopg.types.json.Jsonb(state)),
                )
    except Exception as e:
        logger.debug("lb_write_build_state failed (non-fatal): %s", e)


# ── General-purpose Lakebase query/execute primitives ─────────────────────────
# The Postgres counterparts of execute_query() for app-state tables that aren't
# a simple KV (alerts, tagging_rules, user_permissions, workspace_cache, …).
# Use %s / %(name)s psycopg placeholders — NOT the warehouse connector style.
# Both RAISE (RuntimeError from get_pg_conn) when Lakebase is unavailable so the
# caller can fall back to the UC/warehouse path during the migration.

def lakebase_available() -> bool:
    """Cheap check: is a Lakebase pool configured and not in a failure cooldown?"""
    return _get_lakebase_pool() is not None and time.monotonic() >= _lakebase_down_until


def lakebase_query(sql: str, params: "tuple | dict | None" = None) -> list[dict[str, Any]]:
    """Run a SELECT against Lakebase and return rows as a list of dicts."""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def lakebase_execute(sql: str, params: "tuple | dict | None" = None) -> int:
    """Run a write (INSERT/UPDATE/DELETE/DDL) against Lakebase; return rowcount.

    Commit/rollback is handled by get_pg_conn().
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def read_setup_complete() -> bool:
    """Return True if setup is complete. Checks Lakebase first, DBFS fallback."""
    if lb_read_setup_complete():
        return True
    return read_dbfs_setup_complete()


def write_setup_complete() -> None:
    """Mark setup complete in both Lakebase and DBFS."""
    lb_write_setup_complete()
    write_dbfs_setup_complete()


def read_build_state() -> dict | None:
    """Return last build state. Checks Lakebase first, DBFS fallback."""
    lb_state = lb_read_build_state()
    if lb_state:
        return lb_state
    return read_dbfs_build_state()


def write_build_state(state: dict) -> None:
    """Persist build state to Lakebase and DBFS."""
    lb_write_build_state(state)
    write_dbfs_build_state(state)


def execute_queries_parallel(
    query_funcs: list[tuple[str, Callable[[], list[dict[str, Any]]]]],
    timeout: float | None = 30.0,
) -> dict[str, list[dict[str, Any]] | None]:
    """Execute multiple queries in parallel using daemon threads.

    Daemon threads are automatically killed when the process exits, so the app
    can shut down cleanly on SIGTERM without waiting for slow SQL connections
    (which have a 300-second socket timeout) to finish.

    Args:
        query_funcs: List of (name, lambda) tuples where lambda executes the query
        timeout: Optional wall-clock timeout in seconds. Queries not finished by
                 deadline return None; their daemon threads are killed on process exit.

    Returns:
        Dictionary mapping query names to results
    """
    import threading
    from concurrent.futures import Future, wait as _cfwait, ALL_COMPLETED

    start_time = time.time()
    results: dict[str, list[dict[str, Any]] | None] = {}

    def _timed(name: str, fn: Callable[[], list[dict[str, Any]]]) -> Callable[[], list[dict[str, Any]]]:
        def wrapped() -> list[dict[str, Any]]:
            t0 = time.time()
            result = fn()
            rows = len(result) if isinstance(result, list) else -1
            logger.info("✓ %s: %.2fs rows=%d", name, time.time() - t0, rows)
            return result
        return wrapped

    _EXPECTED_CODES = (
        "[INSUFFICIENT_PERMISSIONS]",
        "[UNRESOLVED_COLUMN",
        "[TABLE_OR_VIEW_NOT_FOUND]",
        "[SCHEMA_NOT_FOUND]",
    )

    def _collect(future: Future, name: str) -> None:
        try:
            results[name] = future.result()
        except SourceScopeUnsupportedError:
            raise
        except Exception as e:
            if any(code in str(e) for code in _EXPECTED_CODES):
                logger.warning("✗ %s failed (non-fatal): %s", name, e)
            else:
                logger.error("✗ %s failed: %s", name, e)
            results[name] = None

    futures_map: dict[Future, str] = {}
    for name, func in query_funcs:
        future: Future = Future()
        futures_map[future] = name

        def _run(f=future, fn=_timed(name, func)):
            try:
                f.set_result(fn())
            except Exception as e:
                f.set_exception(e)

        threading.Thread(target=_run, daemon=True, name=f"sql-{name}").start()

    if timeout is not None:
        done, not_done = _cfwait(list(futures_map.keys()), timeout=timeout, return_when=ALL_COMPLETED)
        for future in not_done:
            elapsed = time.time() - start_time
            logger.error("✗ %s timed out after %.1fs (daemon thread will be killed on exit)", futures_map[future], elapsed)
            results[futures_map[future]] = None
        for future in done:
            _collect(future, futures_map[future])
    else:
        for future in as_completed(futures_map):
            _collect(future, futures_map[future])

    total_elapsed = time.time() - start_time
    logger.info("Parallel execution: %.2fs total (%d/%d queries completed)", total_elapsed, len(results), len(query_funcs))

    return results
