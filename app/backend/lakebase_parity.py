"""Phase B — dual-read parity harness (gates the reader cutover).

The design (docs/LAKEBASE_MIGRATION_DESIGN.md §13) makes parity an explicit
program milestone: before any dashboard reader is flipped from the Delta
aggregate (warehouse) to its Lakebase copy, we run BOTH queries for a burn-in
window, log every mismatch, and gate cutover on a fixed tolerance (0 hard
mismatches on headline totals; small drift tolerated on long-tail rows).

This module is that harness. It is **shadow-only and flag-gated** — nothing here
changes what any endpoint serves. A check runs the authoritative Delta query and
the candidate Lakebase query, compares the result sets, and records a verdict in
a `parity_log` table. Readers keep reading Delta until the burn-in passes and a
human flips the cutover — which is a later, deliberate change, not this one.

Enable with `COST_OBS_PARITY_CHECK=true`; off by default.

NOTE on dialect: Databricks SQL → Postgres rewrite (§14: QUALIFY, INTERVAL,
backticks→double-quotes, etc.) is per-query manual work. This harness takes an
explicit (delta_sql, lakebase_sql) pair rather than auto-translating, because an
incomplete auto-translator would silently produce false parity results.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Default numeric tolerances for "is this drift acceptable?".
_DEFAULT_TOL_ABS = 0.01   # $0.01 / 0.01 DBU
_DEFAULT_TOL_REL = 0.005  # 0.5%

# Cap detail lists so a wildly-diverging check can't write a huge log row.
_DETAIL_CAP = 50

_PARITY_LOG_TABLE = "parity_log"
_PARITY_LOG_DDL = f"""
CREATE TABLE IF NOT EXISTS {_PARITY_LOG_TABLE} (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checked_at         timestamptz NOT NULL DEFAULT now(),
    check_name         text NOT NULL,
    verdict            text NOT NULL,
    delta_rows         bigint,
    lakebase_rows      bigint,
    matched            int,
    within_tolerance   int,
    hard_mismatches    int,
    missing_in_lakebase int,
    extra_in_lakebase  int,
    detail             jsonb
)
"""


def parity_enabled() -> bool:
    """Master flag — parity checks only run when explicitly enabled."""
    return os.getenv("COST_OBS_PARITY_CHECK", "").strip().lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# Pure comparison logic (no I/O — unit-tested)
# ─────────────────────────────────────────────────────────────────────────────

def _row_key(row: dict[str, Any], key_cols: list[str]) -> tuple:
    """Key tuple for a row. With no key columns the whole set is one bucket
    ('__totals__'), so headline single-row aggregates compare column-by-column."""
    if not key_cols:
        return ("__totals__",)
    return tuple("" if row.get(c) is None else str(row.get(c)) for c in key_cols)


def _as_float(v: Any) -> float | None:
    """Best-effort numeric coercion; None for non-numeric (incl. bool, which we
    deliberately do not treat as numeric)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass
    try:
        return float(str(v))
    except (ValueError, TypeError):
        return None


def compare_values(
    a: Any, b: Any, *, tol_abs: float = _DEFAULT_TOL_ABS, tol_rel: float = _DEFAULT_TOL_REL
) -> tuple[bool, float | None]:
    """Compare two values. Returns (ok, abs_drift).

    Numeric: ok if |a-b| <= max(tol_abs, tol_rel*max(|a|,|b|)). Non-numeric:
    ok on exact equality (drift None). NULL==NULL is ok; NULL vs value is not.
    """
    if a is None and b is None:
        return True, 0.0
    if a is None or b is None:
        return False, None
    # Booleans are a distinct type — never coerce True/False to 1/0. Only
    # bool-vs-bool can match; bool-vs-anything-else is a real type discrepancy.
    if isinstance(a, bool) or isinstance(b, bool):
        return (isinstance(a, bool) and isinstance(b, bool) and a == b), None
    fa, fb = _as_float(a), _as_float(b)
    if fa is not None and fb is not None:
        drift = abs(fa - fb)
        allowed = max(tol_abs, tol_rel * max(abs(fa), abs(fb)))
        return drift <= allowed, drift
    return (a == b), None


def compare_result_sets(
    delta_rows: list[dict[str, Any]],
    lakebase_rows: list[dict[str, Any]],
    *,
    key_cols: list[str],
    value_cols: list[str] | None = None,
    tol_abs: float = _DEFAULT_TOL_ABS,
    tol_rel: float = _DEFAULT_TOL_REL,
    max_missing: int = 0,
) -> dict[str, Any]:
    """Compare two result sets keyed by key_cols. Delta is authoritative.

    value_cols defaults to every column present in the Delta rows except the
    key columns. Returns a report dict with counts, capped detail lists, and a
    verdict (PASS unless row-set differences exceed max_missing or any value
    drifts beyond tolerance).
    """
    d_index = {_row_key(r, key_cols): r for r in delta_rows}
    l_index = {_row_key(r, key_cols): r for r in lakebase_rows}

    if value_cols is None:
        cols: list[str] = []
        for r in delta_rows:
            for k in r:
                if k not in key_cols and k not in cols:
                    cols.append(k)
        value_cols = cols

    missing_in_lakebase = [k for k in d_index if k not in l_index]
    extra_in_lakebase = [k for k in l_index if k not in d_index]

    matched = 0
    within_tolerance = 0
    value_mismatches: list[dict[str, Any]] = []
    for k, d_row in d_index.items():
        l_row = l_index.get(k)
        if l_row is None:
            continue
        row_ok = True
        row_tolerated = False
        for col in value_cols:
            ok, drift = compare_values(d_row.get(col), l_row.get(col), tol_abs=tol_abs, tol_rel=tol_rel)
            if not ok:
                row_ok = False
                if len(value_mismatches) < _DETAIL_CAP:
                    value_mismatches.append({
                        "key": list(k), "column": col,
                        "delta": _jsonable(d_row.get(col)),
                        "lakebase": _jsonable(l_row.get(col)),
                        "abs_drift": drift,
                    })
            elif drift is not None and drift > 0:
                row_tolerated = True
        if row_ok:
            matched += 1
            if row_tolerated:
                within_tolerance += 1

    hard_mismatches = len({tuple(m["key"]) for m in value_mismatches})
    row_set_diff = len(missing_in_lakebase) + len(extra_in_lakebase)
    verdict = "PASS" if (row_set_diff <= max_missing and hard_mismatches == 0) else "FAIL"

    return {
        "verdict": verdict,
        "delta_rows": len(delta_rows),
        "lakebase_rows": len(lakebase_rows),
        "matched": matched,
        "within_tolerance": within_tolerance,
        "hard_mismatches": hard_mismatches,
        "missing_in_lakebase": len(missing_in_lakebase),
        "extra_in_lakebase": len(extra_in_lakebase),
        "detail": {
            "value_mismatches": value_mismatches,
            "missing_keys": [list(k) for k in missing_in_lakebase[:_DETAIL_CAP]],
            "extra_keys": [list(k) for k in extra_in_lakebase[:_DETAIL_CAP]],
        },
    }


def _jsonable(v: Any) -> Any:
    """Coerce a value to something json.dumps can handle (for log detail)."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# ─────────────────────────────────────────────────────────────────────────────
# I/O — run a check, log the verdict, read history
# ─────────────────────────────────────────────────────────────────────────────

def run_parity_check(
    name: str,
    delta_sql: str,
    lakebase_sql: str,
    *,
    key_cols: list[str],
    delta_params: dict[str, Any] | None = None,
    lakebase_params: tuple | dict | None = None,
    max_missing: int = 0,
    log: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Run the Delta + Lakebase queries, compare, and (best-effort) log the
    verdict. Returns the report, or a skipped/error dict. Never raises — parity
    is a diagnostic, it must not take down a request or a build.

    The ambient COST_OBS_PARITY_CHECK flag gates automatic/background runs;
    `force=True` (an explicit operator trigger) runs regardless.
    """
    if not (force or parity_enabled()):
        return {"skipped": True, "reason": "parity_disabled"}
    from backend.db import execute_query, lakebase_available, lakebase_query

    if not lakebase_available():
        return {"skipped": True, "reason": "lakebase_unavailable"}

    try:
        delta_rows = execute_query(delta_sql, delta_params, no_cache=True)
        lakebase_rows = lakebase_query(lakebase_sql, lakebase_params)
    except Exception as e:
        logger.warning("parity[%s]: query failed (non-fatal): %s", name, e)
        return {"skipped": True, "error": str(e)}

    report = compare_result_sets(
        delta_rows, lakebase_rows, key_cols=key_cols, max_missing=max_missing
    )
    report["name"] = name
    report["checked_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()

    level = logging.INFO if report["verdict"] == "PASS" else logging.WARNING
    logger.log(
        level,
        "parity[%s]: %s — delta=%d lakebase=%d matched=%d tol=%d hard=%d miss=%d extra=%d",
        name, report["verdict"], report["delta_rows"], report["lakebase_rows"],
        report["matched"], report["within_tolerance"], report["hard_mismatches"],
        report["missing_in_lakebase"], report["extra_in_lakebase"],
    )
    if log:
        _write_parity_log(name, report)
    return report


def _write_parity_log(name: str, report: dict[str, Any]) -> None:
    from backend.db import get_pg_conn
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_PARITY_LOG_DDL)
                cur.execute(
                    f"""
                    INSERT INTO {_PARITY_LOG_TABLE}
                        (check_name, verdict, delta_rows, lakebase_rows, matched,
                         within_tolerance, hard_mismatches, missing_in_lakebase,
                         extra_in_lakebase, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        name, report["verdict"], report["delta_rows"],
                        report["lakebase_rows"], report["matched"],
                        report["within_tolerance"], report["hard_mismatches"],
                        report["missing_in_lakebase"], report["extra_in_lakebase"],
                        json.dumps(report.get("detail", {})),
                    ),
                )
    except Exception as e:
        logger.debug("parity[%s]: could not write parity_log (non-fatal): %s", name, e)


def get_parity_status(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent parity_log rows (most recent first), or [] if unavailable."""
    from backend.db import lakebase_available, lakebase_query
    if not lakebase_available():
        return []
    try:
        return lakebase_query(
            f"SELECT checked_at, check_name, verdict, delta_rows, lakebase_rows, "
            f"matched, within_tolerance, hard_mismatches, missing_in_lakebase, "
            f"extra_in_lakebase FROM {_PARITY_LOG_TABLE} "
            f"ORDER BY checked_at DESC LIMIT {int(limit)}"
        )
    except Exception as e:
        logger.debug("get_parity_status failed (non-fatal): %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Concrete headline check — daily_usage_summary totals (§13 "headline totals")
# ─────────────────────────────────────────────────────────────────────────────

def check_daily_usage_totals(
    start_date: str, end_date: str, catalog: str | None = None,
    schema: str | None = None, force: bool = False,
) -> dict[str, Any]:
    """Headline parity: account-wide spend/DBU totals over a window, Delta vs
    Lakebase. Keyless (single-row) → strict column-by-column comparison."""
    from backend.db import get_catalog_schema
    if not catalog or not schema:
        c, s = get_catalog_schema()
        catalog = catalog or c
        schema = schema or s

    metrics = "SUM(total_spend) AS total_spend, SUM(total_dbus) AS total_dbus, " \
              "SUM(effective_list_spend) AS effective_list_spend, " \
              "SUM(user_attributed_spend) AS user_attributed_spend"
    delta_sql = (
        f"SELECT {metrics} FROM `{catalog}`.`{schema}`.`daily_usage_summary` "
        f"WHERE usage_date BETWEEN :start_date AND :end_date"
    )
    lakebase_sql = (
        f"SELECT {metrics} FROM daily_usage_summary "
        f"WHERE usage_date BETWEEN %s AND %s"
    )
    return run_parity_check(
        "daily_usage_totals", delta_sql, lakebase_sql,
        key_cols=[],
        delta_params={"start_date": start_date, "end_date": end_date},
        lakebase_params=(start_date, end_date),
        max_missing=0,
        force=force,
    )
