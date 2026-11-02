"""Unit tests for the pure comparison logic in backend.lakebase_parity (Phase B).

Covers the dual-read parity comparator — value tolerance (abs + relative),
row-set diffing, keyed vs keyless (headline totals) comparison, and the verdict
gate. This is the logic that decides whether Lakebase reads may be cut over, so
it must be correct-by-construction; the I/O around it can only run against live
Lakebase.

Run with: pytest app/backend/tests/test_lakebase_parity.py -v
"""
from decimal import Decimal

from backend.lakebase_parity import (
    _as_float,
    _row_key,
    compare_result_sets,
    compare_values,
)

# ── compare_values ────────────────────────────────────────────────────────────

def test_compare_values_exact_numeric():
    assert compare_values(100.0, 100.0) == (True, 0.0)


def test_compare_values_within_abs_tol():
    ok, drift = compare_values(100.0, 100.005, tol_abs=0.01, tol_rel=0.0)
    assert ok is True and abs(drift - 0.005) < 1e-9


def test_compare_values_beyond_tol_fails():
    ok, drift = compare_values(100.0, 101.0, tol_abs=0.01, tol_rel=0.005)
    assert ok is False and abs(drift - 1.0) < 1e-9


def test_compare_values_relative_tol_for_large_numbers():
    # 0.5% of 1_000_000 = 5000, so a 4000 drift is tolerated.
    ok, _ = compare_values(1_000_000.0, 1_004_000.0, tol_abs=0.01, tol_rel=0.005)
    assert ok is True


def test_compare_values_decimal_and_int_coerced():
    assert compare_values(Decimal("9.50"), 9.5)[0] is True
    assert compare_values(5, 5.0)[0] is True


def test_compare_values_null_semantics():
    assert compare_values(None, None) == (True, 0.0)
    assert compare_values(None, 0.0)[0] is False
    assert compare_values(1.0, None)[0] is False


def test_compare_values_strings_exact():
    assert compare_values("SQL", "SQL") == (True, None)
    assert compare_values("SQL", "JOBS")[0] is False


def test_bool_not_treated_as_numeric():
    assert _as_float(True) is None
    # True vs 1 must be an exact (non-numeric) mismatch, not 1.0 == 1.0
    assert compare_values(True, 1)[0] is False


# ── _row_key ──────────────────────────────────────────────────────────────────

def test_row_key_keyless_is_totals_bucket():
    assert _row_key({"total": 5}, []) == ("__totals__",)


def test_row_key_keyed_stringifies_and_handles_null():
    assert _row_key({"usage_date": "2026-07-01", "workspace_id": 123}, ["usage_date", "workspace_id"]) == ("2026-07-01", "123")
    assert _row_key({"usage_date": "2026-07-01", "workspace_id": None}, ["usage_date", "workspace_id"]) == ("2026-07-01", "")


# ── compare_result_sets ───────────────────────────────────────────────────────

def _keyed(rows):
    return rows


def test_result_sets_perfect_match_keyed_passes():
    d = [{"usage_date": "2026-07-01", "workspace_id": "1", "spend": 10.0},
         {"usage_date": "2026-07-01", "workspace_id": "2", "spend": 20.0}]
    r = compare_result_sets(d, list(d), key_cols=["usage_date", "workspace_id"])
    assert r["verdict"] == "PASS"
    assert r["matched"] == 2 and r["hard_mismatches"] == 0
    assert r["missing_in_lakebase"] == 0 and r["extra_in_lakebase"] == 0


def test_result_sets_keyless_totals_match_passes():
    d = [{"total_spend": 1000.0, "total_dbus": 50.0}]
    lb = [{"total_spend": 1000.004, "total_dbus": 50.0}]  # within abs tol
    r = compare_result_sets(d, lb, key_cols=[])
    assert r["verdict"] == "PASS"
    assert r["matched"] == 1


def test_result_sets_value_drift_beyond_tol_fails():
    d = [{"usage_date": "2026-07-01", "workspace_id": "1", "spend": 10.0}]
    lb = [{"usage_date": "2026-07-01", "workspace_id": "1", "spend": 25.0}]
    r = compare_result_sets(d, lb, key_cols=["usage_date", "workspace_id"])
    assert r["verdict"] == "FAIL"
    assert r["hard_mismatches"] == 1
    assert r["detail"]["value_mismatches"][0]["column"] == "spend"


def test_result_sets_within_tolerance_counted_and_passes():
    d = [{"k": "a", "spend": 1_000_000.0}]
    lb = [{"k": "a", "spend": 1_004_000.0}]  # 0.4% < 0.5%
    r = compare_result_sets(d, lb, key_cols=["k"])
    assert r["verdict"] == "PASS"
    assert r["within_tolerance"] == 1 and r["hard_mismatches"] == 0


def test_result_sets_missing_row_fails():
    d = [{"k": "a", "spend": 1.0}, {"k": "b", "spend": 2.0}]
    lb = [{"k": "a", "spend": 1.0}]
    r = compare_result_sets(d, lb, key_cols=["k"])
    assert r["verdict"] == "FAIL"
    assert r["missing_in_lakebase"] == 1
    assert r["detail"]["missing_keys"] == [["b"]]


def test_result_sets_extra_row_fails():
    d = [{"k": "a", "spend": 1.0}]
    lb = [{"k": "a", "spend": 1.0}, {"k": "z", "spend": 9.0}]
    r = compare_result_sets(d, lb, key_cols=["k"])
    assert r["verdict"] == "FAIL"
    assert r["extra_in_lakebase"] == 1


def test_result_sets_max_missing_leniency_for_long_tail():
    d = [{"k": "a", "spend": 1.0}, {"k": "b", "spend": 2.0}, {"k": "c", "spend": 3.0}]
    lb = [{"k": "a", "spend": 1.0}, {"k": "b", "spend": 2.0}]  # 1 missing
    strict = compare_result_sets(d, lb, key_cols=["k"], max_missing=0)
    lenient = compare_result_sets(d, lb, key_cols=["k"], max_missing=1)
    assert strict["verdict"] == "FAIL"
    assert lenient["verdict"] == "PASS"


def test_value_cols_default_excludes_keys():
    # A key column that "differs" as a string must not be compared as a value.
    d = [{"usage_date": "2026-07-01", "workspace_id": "1", "spend": 10.0}]
    lb = [{"usage_date": "2026-07-01", "workspace_id": "1", "spend": 10.0}]
    r = compare_result_sets(d, lb, key_cols=["usage_date", "workspace_id"])
    assert r["verdict"] == "PASS" and r["hard_mismatches"] == 0
