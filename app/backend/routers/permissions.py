"""Permissions check endpoints for system table access verification."""

import asyncio
import contextvars
import logging
import os
import threading
import time
from typing import Any

from fastapi import APIRouter, Request

from backend.db import get_workspace_client

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_check_client():
    """WorkspaceClient for permission checks.

    Always uses the user OAuth token if one is in context — ignores auth_mode
    so the permissions display reflects the actual requesting user, not the
    globally locked identity. Falls back to the SP singleton when no token present.
    """
    from databricks.sdk import WorkspaceClient

    from backend.db import _user_token
    user_token = _user_token.get()
    if user_token:
        host = os.getenv("DATABRICKS_HOST", "")
        if host:
            return WorkspaceClient(host=host, token=user_token, auth_type="pat")
    return get_workspace_client()

# Simple in-process cache so repeated wizard loads are instant (5-min TTL)
_permissions_cache: dict[str, Any] | None = None
_permissions_cache_ts: float = 0.0
_PERMISSIONS_CACHE_TTL = 300  # 5 minutes

# Required system tables and their descriptions
REQUIRED_PERMISSIONS = [
    {
        "table": "system.billing.usage",
        "name": "Billing Usage",
        "description": "Core billing and DBU consumption data",
        "required": True,
    },
    {
        "table": "system.billing.list_prices",
        "name": "List Prices",
        "description": "SKU pricing for cost calculations",
        "required": True,
    },
    {
        "table": "system.query.history",
        "name": "Query History",
        "description": "DBSQL query analytics and cost attribution",
        "required": False,
    },
    {
        "table": "system.compute.clusters",
        "name": "Clusters",
        "description": "Cluster metadata for interactive workloads",
        "required": False,
    },
    {
        "table": "system.lakeflow.pipelines",
        "name": "SDP Pipelines",
        "description": "SDP pipeline names and metadata",
        "required": False,
    },
    {
        "table": "system.serving.served_entities",
        "name": "Model Serving",
        "description": "Model serving endpoint information",
        "required": False,
    },
    {
        "table": "system.access.audit",
        "name": "Audit Logs",
        "description": "Workspace audit events (optional)",
        "required": False,
    },
    {
        "table": "system.access.workspaces_latest",
        "name": "Workspace Names",
        "description": "Human-readable workspace names in billing views",
        "required": False,
    },
]


def check_table_access(table: str) -> tuple[bool, str]:
    """Check if the app can query a system table.

    Returns (granted, error_message). error_message is empty string on success.

    Uses SDK tables.get() first — instant REST call, no warehouse needed.
    Falls back to a one-row table probe via SQL warehouse only when SDK returns an ambiguous
    result (not a clear grant or denial). This avoids blocking on warehouse
    cold-start which can take several minutes.
    """
    # SDK check first — fast, no warehouse required, works during cold-start
    try:
        w = _get_check_client()
        w.tables.get(table)
        return True, ""
    except Exception as e:
        err = str(e)
        err_lower = err.lower()
        # Clear permission denial — no need to try SQL
        if any(kw in err_lower for kw in ("permission", "denied", "unauthorized", "not authorized", "403")):
            logger.warning(f"Access check failed for {table}: {type(e).__name__}: {e}")
            return False, err
        # Table not found in UC — definitely no access
        if any(kw in err_lower for kw in ("does not exist", "not found", "table_or_view_not_found")):
            logger.warning(f"Table not found for {table}: {type(e).__name__}: {e}")
            return False, err
        # SDK call itself failed for an unexpected reason — try SQL as fallback
        logger.debug(f"SDK check failed for {table} ({e}), trying SQL fallback")

    import os

    from backend.db import execute_query
    http_path = os.getenv("DATABRICKS_HTTP_PATH", "")
    if http_path and http_path.lower() != "auto":
        try:
            execute_query(f"SELECT 1 FROM {table} LIMIT 1", no_cache=True)
            return True, ""
        except Exception as e:
            logger.warning(f"SQL access check failed for {table}: {type(e).__name__}: {e}")
            return False, str(e)

    return False, "Could not verify table access"


def _get_current_user() -> tuple[str, str]:
    """Return (email, display_name) for the current identity."""
    try:
        w = _get_check_client()
        current_user = w.current_user.me()
        email = current_user.user_name or "unknown"
        name = current_user.display_name or email
        return email, name
    except Exception as e:
        logger.warning(f"Could not get current user: {e}")
        return "unknown", "Unknown User"


def _get_sp_info() -> dict[str, str]:
    """Return the app service principal's client_id and display_name.

    Always uses the SP singleton (not the user token) — we're describing the
    app's own identity, not the requesting user's identity.
    Cached indefinitely per process since the SP doesn't change at runtime.
    """
    sp_client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
    try:
        w = get_workspace_client()
        me = w.current_user.me()
        display_name = me.display_name or me.user_name or sp_client_id
        app_id = me.user_name or sp_client_id
        return {"client_id": sp_client_id or app_id, "display_name": display_name}
    except Exception as e:
        logger.debug(f"Could not fetch SP identity: {e}")
        return {"client_id": sp_client_id, "display_name": sp_client_id}


def _check_permissions_sync(bypass_cache: bool = False) -> dict[str, Any]:
    """Run all permission checks and user lookup in parallel.

    Results are cached for _PERMISSIONS_CACHE_TTL seconds to avoid hitting
    the UC REST API on every wizard page load. Pass bypass_cache=True to force
    a fresh check (e.g. after the user grants new permissions).
    """
    global _permissions_cache, _permissions_cache_ts

    if not bypass_cache and _permissions_cache is not None:
        age = time.monotonic() - _permissions_cache_ts
        if age < _PERMISSIONS_CACHE_TTL:
            logger.debug(f"Returning cached permissions result (age: {age:.0f}s)")
            return _permissions_cache

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Fire table checks + user lookup + SP info all in parallel
    with ThreadPoolExecutor(max_workers=len(REQUIRED_PERMISSIONS) + 2) as pool:
        future_to_table = {
            pool.submit(check_table_access, perm["table"]): perm["table"]
            for perm in REQUIRED_PERMISSIONS
        }
        user_future = pool.submit(_get_current_user)
        sp_future = pool.submit(_get_sp_info)

        access_results: dict[str, tuple[bool, str]] = {}
        for future in as_completed(future_to_table):
            table = future_to_table[future]
            access_results[table] = future.result()

        user_email, user_name = user_future.result()
        sp_info = sp_future.result()

    # Assemble results
    results = []
    granted_count = 0
    required_granted = 0
    required_count = 0

    for perm in REQUIRED_PERMISSIONS:
        has_access, error_msg = access_results[perm["table"]]

        if has_access:
            granted_count += 1
            if perm["required"]:
                required_granted += 1

        if perm["required"]:
            required_count += 1

        row = {
            "table": perm["table"],
            "name": perm["name"],
            "description": perm["description"],
            "required": perm["required"],
            "granted": has_access,
        }
        if error_msg:
            row["error"] = error_msg
        results.append(row)

    # Determine overall status
    all_required_granted = required_granted == required_count

    result = {
        "permissions": results,
        "summary": {
            "total": len(results),
            "granted": granted_count,
            "required_count": required_count,
            "required_granted": required_granted,
            "all_required_granted": all_required_granted,
            "ready_to_use": all_required_granted,
        },
        "user": {
            "email": user_email,
            "name": user_name,
        },
        "sp": sp_info,
        "help_url": "https://docs.databricks.com/en/admin/system-tables/index.html",
    }

    _permissions_cache = result
    _permissions_cache_ts = time.monotonic()
    return result


@router.get("/check")
async def check_permissions(request: Request, refresh: bool = False) -> dict[str, Any]:
    """
    Check user's access to required system tables.

    When Databricks Apps user authorization is active (x-forwarded-access-token
    present), checks run as the end user and results are not cached (each user
    may have different grants). Otherwise cached for 5 minutes per process.
    Pass ?refresh=true to force a live re-check (e.g. after granting permissions).
    """
    from backend.db import _user_token

    # Read the token directly from the request rather than relying on middleware
    # ContextVar propagation, which is unreliable through BaseHTTPMiddleware.
    user_token = request.headers.get("x-forwarded-access-token", "")
    using_user_auth = bool(user_token)

    # Set ContextVar here so it's captured by copy_context() before the daemon thread starts.
    ctx_tok = _user_token.set(user_token)
    try:
        bypass = refresh or using_user_auth
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        async_fut: asyncio.Future = loop.create_future()

        def _run():
            try:
                r = ctx.run(_check_permissions_sync, bypass_cache=bypass)
                loop.call_soon_threadsafe(async_fut.set_result, r)
            except Exception as exc:
                if not async_fut.done():
                    loop.call_soon_threadsafe(async_fut.set_exception, exc)

        threading.Thread(target=_run, daemon=True, name="permissions-check").start()
        result = await async_fut
    finally:
        _user_token.reset(ctx_tok)

    from backend.db import _auth_mode
    # Report the locked mode if known, otherwise fall back to header presence
    if _auth_mode in ("user", "sp"):
        result["auth_mode"] = _auth_mode
    else:
        result["auth_mode"] = "user" if using_user_auth else "service_principal"
    return result
