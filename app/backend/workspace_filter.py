"""Workspace-scoping filter — reads COST_OBS_WORKSPACES env var or .settings/workspace_filter.json.

Set COST_OBS_WORKSPACES to a comma-separated list of workspace IDs to scope
the dashboard to specific workspaces only.  All workspaces are shown when the
variable is not set.

Example:
    COST_OBS_WORKSPACES=3233745148968388,1234567890123456
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_SETTINGS_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".settings", "workspace_filter.json"
)

_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')


def _is_safe_id(s: str) -> bool:
    """Accept numeric IDs and UUID/string workspace IDs; block anything that could inject SQL."""
    return bool(s and _SAFE_ID_RE.match(s))


def clear_cache() -> None:
    """No-op — kept for call-site compatibility. Cache was removed to fix multi-worker staleness."""
    pass


def get_configured_workspace_ids() -> list[str]:
    """Return validated workspace IDs. Checks env var first, then settings file. Empty = no filter.

    Always reads from source (no in-process cache) so every uvicorn worker sees fresh data
    immediately after the admin saves a new pool without a server restart.
    """
    raw = os.environ.get("COST_OBS_WORKSPACES", "").strip()
    if raw:
        parts = [w.strip() for w in raw.split(",") if w.strip()]
        valid = [p for p in parts if _is_safe_id(p)]
        invalid = sorted(set(parts) - set(valid))
        if invalid:
            logger.warning("COST_OBS_WORKSPACES: ignoring unsafe values: %s", invalid)
        return valid
    try:
        with open(_SETTINGS_FILE) as f:
            data = json.load(f)
        return [str(i) for i in data.get("workspace_ids", []) if _is_safe_id(str(i))]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def build_ws_filter_clause(
    col: str = "u.workspace_id",
    single_id: str | None = None,
    id_list: list[str] | None = None,
) -> str:
    """Return a SQL AND clause for workspace filtering, or empty string.

    id_list   — list of IDs from multi-select; takes precedence over env/file config.
    single_id — single ID (legacy compat); takes precedence over env/file config.

    Uses STRING comparison so both numeric and UUID-format workspace IDs are handled correctly.
    """
    if id_list is not None:
        valid = [i for i in id_list if _is_safe_id(i)]
        if not valid:
            return ""
        quoted = ", ".join(f"'{i}'" for i in valid)
        return f"AND CAST({col} AS STRING) IN ({quoted})"
    if single_id and _is_safe_id(single_id):
        return f"AND CAST({col} AS STRING) = '{single_id}'"
    ids = get_configured_workspace_ids()
    if not ids:
        return ""
    quoted = ", ".join(f"'{i}'" for i in ids)
    return f"AND CAST({col} AS STRING) IN ({quoted})"


def pool_cache_tag() -> str:
    """The saved pool as a cache-key fragment.

    Requests without explicit workspace_ids are scoped by the pool, so caches for
    them must change key when an admin changes the pool. Otherwise they keep
    serving the previous scope's data until the TTL runs out.
    """
    return ",".join(sorted(get_configured_workspace_ids()))


def is_workspace_scoped() -> bool:
    """True when workspace IDs restrict data to specific workspaces."""
    return bool(get_configured_workspace_ids())


def inject_ws_filter(sql: str, clause: str) -> str:
    """Inject workspace filter clause after usage_quantity guard in a billing SQL string."""
    if not clause:
        return sql
    for anchor in ("AND u.usage_quantity > 0", "AND usage_quantity > 0"):
        if anchor in sql:
            return sql.replace(anchor, f"{anchor}\n    {clause}", 1)
    return sql
