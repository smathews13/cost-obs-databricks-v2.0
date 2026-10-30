"""Phase B — app-managed direct load of the daily_* aggregates into Lakebase.

The warehouse builds/refreshes the `daily_*` Delta aggregates as before (see
`materialized_views.py`). This module then reads those rows over the warehouse
and writes them into Lakebase Postgres tables via the app's own OAuth pool
(`get_pg_conn`) — no UC catalog, no metastore admin, no managed synced tables.

Mechanism (per docs/LAKEBASE_MIGRATION_DESIGN.md §4):
  - Range-replace the recent window: within ONE transaction, DELETE the window
    from Postgres and re-INSERT the fresh rows read from Delta. Idempotent — a
    re-run can't duplicate because we delete before inserting.
  - Full backfill replaces the whole table.
  - The Postgres schema is derived at runtime from the Delta table's DESCRIBE
    output so it can never drift from what the build actually produces; primary
    keys and the reprocess window come from `_TABLE_REFRESH_CONFIG`.

Everything here is best-effort and guarded by `lakebase_available()`: on a
warehouse-only deploy (no Lakebase) the sync is skipped entirely and the Delta
aggregates + their current readers are unaffected. This module does NOT change
the Delta builds and does NOT repoint any reader — reader cutover is gated on a
separate dual-read parity burn-in (§13).
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel for a NULL workspace_id (account-level usage) so it can sit in a
# Postgres primary key (§4a). Other string PK columns that are unexpectedly NULL
# use a generic sentinel so the PK stays valid rather than the row being dropped.
WORKSPACE_ID_SENTINEL = "__account__"
GENERIC_KEY_SENTINEL = "__na__"

# agg_sync_state.mode while a rewrite is in flight; see sync_one_aggregate.
_REWRITE_PENDING = "rewrite_pending"

# Batch size for executemany inserts.
_INSERT_BATCH = 5000

# Sync reads/writes the Delta aggregate one date-window at a time so a large
# table never lands in memory all at once (a busy workspace can have >1M rows in
# a daily aggregate). 7 days keeps each chunk small for the daily_* tables.
_CHUNK_DAYS = 7

# Tables NOT app-managed-synced to Lakebase. `dbsql_cost_per_query` is per-QUERY
# granular (tens of millions of rows) — raw detail, not a gold dashboard
# aggregate (design §5: "sync gold, not raw"). It stays warehouse-served for
# drill-downs; syncing it would be large + costly and isn't on the hot path.
_SYNC_EXCLUDE: frozenset[str] = frozenset({"dbsql_cost_per_query"})

# Watermark table: one row per aggregate, exposing sync + freshness separately.
# App-owned watermark table. `agg_sync_state` was created by an earlier identity
# the Apps SP cannot ALTER/INSERT into ("must be owner of table").
_SYNC_STATE_TABLE = "agg_sync_state_v2"
_SYNC_STATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {_SYNC_STATE_TABLE} (
    table_name      text PRIMARY KEY,
    last_synced_at  timestamptz NOT NULL,
    mode            text NOT NULL,
    window_start    date,
    rows_synced     bigint,
    max_source_date date,
    duration_ms     bigint,
    backfilled_at   timestamptz
)
"""


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no I/O — unit-tested in tests/unit/test_lakebase_sync.py)
# ─────────────────────────────────────────────────────────────────────────────

def map_delta_type_to_pg(delta_type: str) -> str:
    """Map a Databricks/Delta column type to a Postgres column type.

    Unknown/complex types (struct/array/map/etc.) fall back to `text`; the read
    expression CASTs them to string so the sync can't break on an unmapped type.
    """
    t = (delta_type or "").strip().lower()
    # Preserve decimal precision/scale, e.g. decimal(38,18) -> numeric(38,18).
    if t.startswith("decimal") or t.startswith("numeric"):
        params = t[t.find("(") : t.find(")") + 1] if "(" in t else ""
        return f"numeric{params}" if params else "numeric"
    base = t.split("(", 1)[0].strip()
    mapping = {
        "date": "date",
        "string": "text",
        "varchar": "text",
        "char": "text",
        "binary": "bytea",
        "boolean": "boolean",
        "bool": "boolean",
        "tinyint": "smallint",
        "byte": "smallint",
        "smallint": "smallint",
        "short": "smallint",
        "int": "integer",
        "integer": "integer",
        "bigint": "bigint",
        "long": "bigint",
        "float": "double precision",
        "real": "double precision",
        "double": "double precision",
        "timestamp": "timestamptz",
        "timestamp_ntz": "timestamp",
    }
    return mapping.get(base, "text")


def _is_string_pg_type(pg_type: str) -> bool:
    return pg_type in ("text",)


def build_column_spec(
    columns: list[tuple[str, str]], pk: list[str]
) -> list[dict[str, Any]]:
    """Resolve each Delta column into a Postgres column spec.

    Returns a list of dicts (in DESCRIBE order) with:
      name, pg_type, is_pk, not_null, sentinel (str|None), read_expr.

    Rules:
      - PK string columns become `text NOT NULL` with a COALESCE sentinel, so a
        NULL key (e.g. account-level workspace_id) still forms a valid PK.
      - PK non-string columns (date/timestamp/numeric) keep their mapped type,
        NOT NULL, no sentinel (the aggregate grain guarantees they're present).
      - Non-PK columns keep their mapped type and are nullable.
    """
    pk_set = set(pk)
    specs: list[dict[str, Any]] = []
    for name, delta_type in columns:
        pg_type = map_delta_type_to_pg(delta_type)
        is_pk = name in pk_set
        # A complex/unknown Delta type (struct/array/map/…) fell back to `text`;
        # such a column must be CAST to string on read. Genuine string types
        # (string/varchar/char) map to text too but need no cast.
        base_delta = (delta_type or "").strip().lower().split("(", 1)[0]
        needs_cast_to_string = pg_type == "text" and base_delta not in (
            "string", "varchar", "char",
        )
        sentinel: str | None = None
        if is_pk and _is_string_pg_type(pg_type):
            sentinel = (
                WORKSPACE_ID_SENTINEL if name == "workspace_id" else GENERIC_KEY_SENTINEL
            )
        # Read expression (Databricks SQL side).
        col_ref = f"`{name}`"
        if sentinel is not None:
            read_expr = f"COALESCE(CAST({col_ref} AS STRING), '{sentinel}') AS `{name}`"
        elif needs_cast_to_string:
            read_expr = f"CAST({col_ref} AS STRING) AS `{name}`"
        else:
            read_expr = col_ref
        specs.append(
            {
                "name": name,
                "pg_type": pg_type,
                "is_pk": is_pk,
                "not_null": is_pk,
                "sentinel": sentinel,
                "read_expr": read_expr,
            }
        )
    return specs


def build_create_table_sql(table: str, specs: list[dict[str, Any]], pk: list[str]) -> str:
    """Build a CREATE TABLE IF NOT EXISTS for the Postgres aggregate table."""
    lines = []
    for s in specs:
        nn = " NOT NULL" if s["not_null"] else ""
        lines.append(f'    "{s["name"]}" {s["pg_type"]}{nn}')
    if pk:
        pk_cols = ", ".join(f'"{c}"' for c in pk)
        lines.append(f"    PRIMARY KEY ({pk_cols})")
    body = ",\n".join(lines)
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n{body}\n)'


def build_add_column_sql(table: str, spec: dict[str, Any]) -> str:
    """ALTER to add a single column (additive schema evolution). Never NOT NULL
    on an add — an existing table may already have rows."""
    return f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{spec["name"]}" {spec["pg_type"]}'


def build_select_sql(
    catalog: str,
    schema: str,
    table: str,
    specs: list[dict[str, Any]],
    *,
    source_date_col: str | None,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
) -> str:
    """Build the Databricks SQL that reads rows from the Delta aggregate.

    When `source_date_col` + a date range is given, the read is windowed
    (BETWEEN date_from AND date_to) so sync can chunk by date and keep memory
    bounded; otherwise it reads the whole table.
    """
    select_list = ",\n  ".join(s["read_expr"] for s in specs)
    sql = f"SELECT\n  {select_list}\nFROM `{catalog}`.`{schema}`.`{table}`"
    if source_date_col and date_from is not None and date_to is not None:
        sql += (
            f"\nWHERE `{source_date_col}` BETWEEN DATE'{date_from.isoformat()}' "
            f"AND DATE'{date_to.isoformat()}'"
        )
    return sql


def build_insert_sql(table: str, colnames: list[str], pk: list[str] | None = None) -> str:
    """INSERT with ON CONFLICT DO NOTHING when a PK is given, so the sync is
    resilient to duplicate keys in the source Delta aggregate (e.g. a build whose
    GROUP-BY grain is finer than its declared PK, or legacy rows from a prior
    concurrency bug). Without this a single dup row aborts the whole table load."""
    cols = ", ".join(f'"{c}"' for c in colnames)
    placeholders = ", ".join(["%s"] * len(colnames))
    sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders})'
    if pk:
        conflict = ", ".join(f'"{c}"' for c in pk)
        sql += f' ON CONFLICT ({conflict}) DO NOTHING'
    return sql


def build_delete_range_sql(table: str, source_date_col: str) -> str:
    return f'DELETE FROM "{table}" WHERE "{source_date_col}" BETWEEN %s AND %s'


def compute_reprocess_start(
    reprocess_days: int, overlap_days: int, today: _dt.date | None = None
) -> _dt.date:
    """First date of the incremental window: today - (reprocess_days + overlap)."""
    today = today or _dt.date.today()
    return today - _dt.timedelta(days=int(reprocess_days) + int(overlap_days))


def iter_date_chunks(
    start: _dt.date, end: _dt.date, chunk_days: int
) -> list[tuple[_dt.date, _dt.date]]:
    """Split [start, end] (inclusive) into ≤ chunk_days windows.

    Sync reads/writes one chunk at a time so a huge aggregate never lands in
    memory all at once. Returns [] when start > end.
    """
    if start > end or chunk_days < 1:
        return []
    chunks: list[tuple[_dt.date, _dt.date]] = []
    cs = start
    while cs <= end:
        ce = min(cs + _dt.timedelta(days=chunk_days - 1), end)
        chunks.append((cs, ce))
        cs = ce + _dt.timedelta(days=1)
    return chunks


def _rows_to_tuples(rows: list[dict[str, Any]], colnames: list[str]) -> list[tuple]:
    """Project dict rows into positional tuples matching colnames order."""
    return [tuple(r.get(c) for c in colnames) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# I/O — Delta read, Postgres write, watermark
# ─────────────────────────────────────────────────────────────────────────────

def _describe_delta_columns(catalog: str, schema: str, table: str) -> list[tuple[str, str]]:
    """Return [(col_name, delta_type)] for a Delta table via DESCRIBE TABLE.

    Filters out the partition/detail section markers that DESCRIBE can append.
    """
    from backend.db import execute_query

    rows = execute_query(
        f"DESCRIBE TABLE `{catalog}`.`{schema}`.`{table}`", no_cache=True
    )
    cols: list[tuple[str, str]] = []
    for r in rows:
        name = (r.get("col_name") or "").strip()
        dtype = (r.get("data_type") or "").strip()
        # DESCRIBE appends a blank row then a "# Partitioning"/"# Detailed ..."
        # section — stop at the first non-column marker.
        if not name or name.startswith("#"):
            break
        cols.append((name, dtype))
    return cols


def _ensure_pg_table(conn, table: str, specs: list[dict[str, Any]], pk: list[str]) -> None:
    """Create the Postgres table if absent; add any missing columns (additive).

    PK changes are NOT applied to an existing table — a breaking key change is a
    versioned cutover (§9), not an in-place mutation.
    """
    def _create() -> None:
        with conn.cursor() as cur:
            cur.execute(build_create_table_sql(table, specs, pk))
            # Additive evolution: add columns that appeared in the build since create.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND table_schema = current_schema()",
                (table,),
            )
            existing = {row[0] for row in cur.fetchall()}
            for s in specs:
                if s["name"] not in existing:
                    logger.info("lakebase_sync: adding column %s.%s", table, s["name"])
                    cur.execute(build_add_column_sql(table, s))

    try:
        _create()
    except Exception as e:
        if not _is_not_owner_error(e):
            raise
        logger.warning(
            "lakebase_sync: %s is owned by another identity — dropping so this role can recreate it",
            table,
        )
        _reclaim_table(conn, table)
        _create()


def _is_not_owner_error(exc: BaseException) -> bool:
    return "must be owner" in str(exc).lower()


def _clear_table(cur, table: str) -> None:
    """Empty a table inside the caller's transaction.

    TRUNCATE needs the TRUNCATE privilege, which the setup job's grants don't give
    the app role on tables the job created (SELECT/INSERT/UPDATE/DELETE only), so
    fall back to DELETE when it's refused.
    """
    cur.execute("SAVEPOINT clear_table")
    try:
        cur.execute(f'TRUNCATE TABLE "{table}"')
    except Exception as e:
        if _is_not_owner_error(e) or "permission denied" not in str(e).lower():
            raise
        cur.execute("ROLLBACK TO SAVEPOINT clear_table")
        cur.execute(f'DELETE FROM "{table}"')
    cur.execute("RELEASE SAVEPOINT clear_table")


def _reclaim_table(conn, table: str) -> None:
    """Drop a leftover table owned by another Lakebase identity so this role can recreate it."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def _ensure_sync_state_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_SYNC_STATE_DDL)
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = 'backfilled_at' "
            "AND table_schema = current_schema()",
            (_SYNC_STATE_TABLE,),
        )
        if cur.fetchone() is not None:
            return
        try:
            cur.execute(
                f"ALTER TABLE {_SYNC_STATE_TABLE} ADD COLUMN IF NOT EXISTS backfilled_at timestamptz"
            )
        except Exception as e:
            if not _is_not_owner_error(e):
                raise
            conn.rollback()
            # Table belongs to the other Lakebase identity (app vs setup job).
            # Gold copy can continue; incremental watermarks may be incomplete.
            logger.warning(
                "lakebase_sync: cannot ALTER %s (%s) — continuing without adding backfilled_at",
                _SYNC_STATE_TABLE, e,
            )


def _write_sync_state(
    conn,
    table: str,
    *,
    mode: str,
    window_start: _dt.date | None,
    rows_synced: int,
    max_source_date: _dt.date | None,
    duration_ms: int,
    mark_backfilled: bool = False,
) -> None:
    # backfilled_at is set only when a FULL sync completes, and is PRESERVED across
    # later incremental syncs (COALESCE) — it is the durable "a full backfill has
    # completed" signal that decides full-vs-incremental, so a partial backfill that
    # left the table non-empty can't get stuck doing incremental-only forever.
    try:
        with conn.cursor() as cur:
            cur.execute(
            f"""
            INSERT INTO {_SYNC_STATE_TABLE}
                (table_name, last_synced_at, mode, window_start, rows_synced,
                 max_source_date, duration_ms, backfilled_at)
            VALUES (%s, now(), %s, %s, %s, %s, %s, %s)
            ON CONFLICT (table_name) DO UPDATE SET
                last_synced_at  = EXCLUDED.last_synced_at,
                mode            = EXCLUDED.mode,
                window_start    = EXCLUDED.window_start,
                rows_synced     = EXCLUDED.rows_synced,
                max_source_date = EXCLUDED.max_source_date,
                duration_ms     = EXCLUDED.duration_ms,
                backfilled_at   = COALESCE(EXCLUDED.backfilled_at, {_SYNC_STATE_TABLE}.backfilled_at)
            """,
            (table, mode, window_start, rows_synced, max_source_date, duration_ms,
             _dt.datetime.now(_dt.timezone.utc) if mark_backfilled else None),
        )
    except Exception as e:
        if not _is_not_owner_error(e) and "permission denied" not in str(e).lower():
            raise
        conn.rollback()
        logger.warning("lakebase_sync: skipping watermark write for %s (%s)", table, e)


def sync_one_aggregate(
    catalog: str,
    schema: str,
    table: str,
    cfg: dict[str, Any],
    *,
    full: bool = False,
    rewrite: bool = False,
) -> dict[str, Any]:
    """Sync one Delta aggregate into its Lakebase table via chunked range-replace.

    ``rewrite`` replaces the whole date range chunk by chunk, like incremental but
    from the first date, then drops rows outside it. For a Delta table rebuilt with
    changed history: unlike ``full`` it never empties the copy, so readers keep
    getting rows (old or new per chunk) instead of a blank tab during the load.

    Reads/writes ONE date window (``_CHUNK_DAYS``) at a time so a large aggregate
    never lands in memory all at once. Full/empty → TRUNCATE then load every
    chunk; incremental → per-chunk DELETE+INSERT over the reprocess window (both
    idempotent). Raises on failure so `sync_all_aggregates` can record per-table
    status; it isolates each table so one failure doesn't abort the rest.
    """
    from backend.db import execute_query, get_pg_conn

    t0 = time.monotonic()
    pk: list[str] = list(cfg.get("pk") or [])
    source_date_col: str | None = cfg.get("source_date_col")
    reprocess_days = int(cfg.get("reprocess_days", 14))
    overlap_days = int(cfg.get("overlap_days", 0))

    columns = _describe_delta_columns(catalog, schema, table)
    if not columns:
        raise RuntimeError(f"DESCRIBE returned no columns for {table}")
    specs = build_column_spec(columns, pk)
    colnames = [s["name"] for s in specs]
    insert_sql = build_insert_sql(table, colnames, pk)
    have_date = bool(source_date_col) and source_date_col in {c for c, _ in columns}

    # Ensure the PG table + watermark exist, and learn whether a FULL backfill has
    # completed (backfilled_at). Incremental is only safe once a full backfill has
    # actually finished — otherwise a partial load (some chunks committed before a
    # failure) would leave the table non-empty and every later sync would go
    # incremental, permanently missing the older history that never loaded.
    with get_pg_conn() as conn:
        _ensure_pg_table(conn, table, specs, pk)
        _ensure_sync_state_table(conn)
        with conn.cursor() as cur:
            cur.execute(f'SELECT 1 FROM "{table}" LIMIT 1')
            table_empty = cur.fetchone() is None
            cur.execute(f"SELECT backfilled_at, mode FROM {_SYNC_STATE_TABLE} WHERE table_name = %s", (table,))
            row = cur.fetchone()
            backfill_done = bool(row and row[0])
            # A rewrite that didn't finish left old rows outside the window it reached;
            # keep rewriting until one completes (see _REWRITE_PENDING).
            if not full and row and len(row) > 1 and row[1] == _REWRITE_PENDING:
                rewrite = True

    do_incremental = (not full) and (not table_empty) and backfill_done and have_date

    def _insert_rows(cur, tuples: list[tuple]) -> None:
        for i in range(0, len(tuples), _INSERT_BATCH):
            cur.executemany(insert_sql, tuples[i : i + _INSERT_BATCH])

    # Fallback for a table with no usable date column (none today): single-shot.
    if not have_date:
        rows = execute_query(build_select_sql(catalog, schema, table, specs, source_date_col=None), no_cache=True)
        tuples = _rows_to_tuples(rows, colnames)
        with get_pg_conn() as conn, conn.cursor() as cur:
            _clear_table(cur, table)
            _insert_rows(cur, tuples)
        _finish(table, "full", None, len(tuples), None, t0)
        return {"table": table, "mode": "full", "rows": len(tuples), "window_start": None,
                "max_source_date": None, "duration_ms": int((time.monotonic() - t0) * 1000)}

    # Data date range (cheap MIN/MAX) drives both the chunk bounds and freshness.
    mm = execute_query(
        f"SELECT MIN(`{source_date_col}`) AS mn, MAX(`{source_date_col}`) AS mx "
        f"FROM `{catalog}`.`{schema}`.`{table}`", no_cache=True,
    )
    mn = mm[0].get("mn") if mm else None
    mx = mm[0].get("mx") if mm else None
    if mn is None or mx is None:  # empty Delta table
        with get_pg_conn() as conn:
            _write_sync_state(conn, table, mode="full", window_start=None,
                              rows_synced=0, max_source_date=None,
                              duration_ms=int((time.monotonic() - t0) * 1000),
                              mark_backfilled=True)
        return {"table": table, "mode": "full", "rows": 0, "window_start": None,
                "max_source_date": None, "duration_ms": int((time.monotonic() - t0) * 1000)}

    mode = ("rewrite" if rewrite else "incremental") if do_incremental else "full"
    if do_incremental and rewrite:
        range_start = mn
        # Recorded before any chunk is replaced. _finish overwrites it on success;
        # if this run fails, the next sync sees it and rewrites again instead of
        # dropping back to the 14-day window.
        with get_pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {_SYNC_STATE_TABLE} SET mode = %s WHERE table_name = %s", (_REWRITE_PENDING, table))
    elif do_incremental:
        range_start = max(compute_reprocess_start(reprocess_days, overlap_days), mn)
    else:
        range_start = mn
        with get_pg_conn() as conn:  # clear before full load
            # Readers skip tables without backfilled_at, so they serve warehouse
            # gold instead of a half-loaded copy until this load finishes. The flag
            # and the clear commit together: a clear that fails must not leave the
            # table flagged as loading, which readers would treat as empty forever.
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {_SYNC_STATE_TABLE} SET backfilled_at = NULL WHERE table_name = %s",
                        (table,),
                    )
                    _clear_table(cur, table)
                conn.commit()
            except Exception as e:
                if not _is_not_owner_error(e):
                    raise
                logger.warning(
                    "lakebase_sync: %s is owned by another identity — dropping so this role can recreate it",
                    table,
                )
                _reclaim_table(conn, table)
                _ensure_pg_table(conn, table, specs, pk)
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {_SYNC_STATE_TABLE} SET backfilled_at = NULL WHERE table_name = %s",
                        (table,),
                    )
                conn.commit()

    total = 0
    for cs, ce in iter_date_chunks(range_start, mx, _CHUNK_DAYS):
        sel = build_select_sql(catalog, schema, table, specs,
                               source_date_col=source_date_col, date_from=cs, date_to=ce)
        tuples = _rows_to_tuples(execute_query(sel, no_cache=True), colnames)
        with get_pg_conn() as conn, conn.cursor() as cur:
            if do_incremental:
                cur.execute(build_delete_range_sql(table, source_date_col), (cs, ce))
            _insert_rows(cur, tuples)
        total += len(tuples)

    if mode == "rewrite":
        with get_pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f'DELETE FROM "{table}" WHERE "{source_date_col}" < %s OR "{source_date_col}" > %s '
                f'OR "{source_date_col}" IS NULL',
                (range_start, mx),
            )

    _finish(table, mode, range_start, total, mx, t0)
    return {"table": table, "mode": mode, "rows": total,
            "window_start": range_start.isoformat(),
            "max_source_date": mx.isoformat() if hasattr(mx, "isoformat") else str(mx),
            "duration_ms": int((time.monotonic() - t0) * 1000)}


def _finish(table, mode, window_start, rows_synced, max_source_date, t0) -> None:
    """Write the watermark row for a completed table sync (own short txn)."""
    from backend.db import get_pg_conn
    duration_ms = int((time.monotonic() - t0) * 1000)
    with get_pg_conn() as conn:
        _write_sync_state(conn, table, mode=mode, window_start=window_start,
                          rows_synced=rows_synced, max_source_date=max_source_date,
                          duration_ms=duration_ms, mark_backfilled=(mode == "full"))
    logger.info("lakebase_sync: %s %s — %d rows (window_start=%s, %dms)",
                table, mode, rows_synced, window_start, duration_ms)


def sync_all_aggregates(
    catalog: str | None = None,
    schema: str | None = None,
    *,
    full: bool = False,
    full_tables: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Sync every daily_* aggregate into Lakebase. Best-effort, per-table isolated.

    `full_tables` have their whole history replaced even when `full` is False —
    used for tables the Delta build just recreated, whose history may have changed.
    They go through ``rewrite`` so the copy stays readable while it loads.

    No-op (returns skipped) when Lakebase is unavailable, so warehouse-only
    deploys are unaffected. Never raises — a failing table is recorded and the
    rest continue.
    """
    from backend.db import get_catalog_schema, lakebase_available
    from backend.materialized_views import _TABLE_REFRESH_CONFIG

    if not lakebase_available():
        logger.info("lakebase_sync: Lakebase unavailable — skipping aggregate sync")
        return {"skipped": True, "reason": "lakebase_unavailable"}

    if not catalog or not schema:
        c, s = get_catalog_schema()
        catalog = catalog or c
        schema = schema or s

    # Ensure the Lakebase Postgres schema (what the pool's search_path targets)
    # exists — the app SP holds CAN_CONNECT_AND_CREATE, so the app-managed sync
    # creates its own target schema rather than hard-depending on the setup-job
    # bootstrap having run first. Uses the pool's search_path schema (PGSCHEMA
    # env / COST_OBS_SCHEMA) so unqualified CREATE TABLE lands there.
    import os as _os
    from backend.db import get_pg_conn
    # App-owned schema so we can CREATE/INSERT even when leftover daily_* tables
    # in COST_OBS_SCHEMA belong to another Postgres role.
    pg_schema = _os.environ.get("COST_OBS_PG_GOLD_SCHEMA", "cost_obs_gold")
    try:
        with get_pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{pg_schema}"')
            cur.execute(f'SET search_path TO "{pg_schema}", public')
    except Exception as e:
        logger.warning("lakebase_sync: could not ensure schema %s (non-fatal): %s", pg_schema, e)

    tables_to_sync = [t for t in _TABLE_REFRESH_CONFIG if t not in _SYNC_EXCLUDE]
    results: dict[str, Any] = {}
    ok = 0
    for table in tables_to_sync:
        try:
            results[table] = sync_one_aggregate(
                catalog, schema, table, _TABLE_REFRESH_CONFIG[table], full=full,
                rewrite=not full and table in full_tables,
            )
            ok += 1
        except Exception as e:  # isolate per-table failures
            logger.warning("lakebase_sync: %s failed (non-fatal): %s", table, e)
            results[table] = {"table": table, "error": str(e)}
    logger.info(
        "lakebase_sync: synced %d/%d aggregates (full=%s, excluded=%s)",
        ok, len(tables_to_sync), full, sorted(_SYNC_EXCLUDE),
    )
    return {"skipped": False, "full": full, "ok": ok, "total": len(tables_to_sync),
            "excluded": sorted(_SYNC_EXCLUDE), "tables": results}


def get_sync_status() -> list[dict[str, Any]]:
    """Return the per-table sync watermark rows, or [] if unavailable/empty."""
    import os

    from backend.db import lakebase_available, lakebase_query

    if not lakebase_available():
        return []
    try:
        gold = os.environ.get("COST_OBS_PG_GOLD_SCHEMA", "cost_obs_gold")
        return lakebase_query(
            f'SELECT table_name, last_synced_at, mode, window_start, rows_synced, '
            f'max_source_date, duration_ms FROM "{gold}".{_SYNC_STATE_TABLE} ORDER BY table_name'
        )
    except Exception as e:
        logger.debug("get_sync_status failed (non-fatal): %s", e)
        return []
