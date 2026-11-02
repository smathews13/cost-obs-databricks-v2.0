"""Unit tests for the pure builders in backend.lakebase_sync (Phase B).

These cover the logic that has no I/O — type mapping, column-spec resolution
(sentinels / NOT NULL / read expressions), DDL + DML generation, and the
incremental-window math — which is exactly the part that must be correct-by-
construction since the sync itself can only be exercised against live Lakebase.

Run with: pytest app/backend/tests/test_lakebase_sync.py -v
"""
import datetime as dt

from backend.lakebase_sync import (
    GENERIC_KEY_SENTINEL,
    WORKSPACE_ID_SENTINEL,
    _is_not_owner_error,
    _rows_to_tuples,
    build_add_column_sql,
    build_column_spec,
    build_create_table_sql,
    build_delete_range_sql,
    build_insert_sql,
    build_select_sql,
    compute_reprocess_start,
    iter_date_chunks,
    map_delta_type_to_pg,
)

# ── map_delta_type_to_pg ──────────────────────────────────────────────────────

def test_type_map_scalars():
    assert map_delta_type_to_pg("date") == "date"
    assert map_delta_type_to_pg("string") == "text"
    assert map_delta_type_to_pg("bigint") == "bigint"
    assert map_delta_type_to_pg("long") == "bigint"
    assert map_delta_type_to_pg("int") == "integer"
    assert map_delta_type_to_pg("double") == "double precision"
    assert map_delta_type_to_pg("float") == "double precision"
    assert map_delta_type_to_pg("boolean") == "boolean"


def test_type_map_decimal_preserves_precision():
    assert map_delta_type_to_pg("decimal(38,18)") == "numeric(38,18)"
    assert map_delta_type_to_pg("DECIMAL(10, 2)") == "numeric(10, 2)"
    assert map_delta_type_to_pg("decimal") == "numeric"


def test_type_map_timestamps():
    assert map_delta_type_to_pg("timestamp") == "timestamptz"
    assert map_delta_type_to_pg("timestamp_ntz") == "timestamp"


def test_type_map_complex_and_unknown_fall_back_to_text():
    assert map_delta_type_to_pg("struct<a:int,b:string>") == "text"
    assert map_delta_type_to_pg("array<string>") == "text"
    assert map_delta_type_to_pg("map<string,int>") == "text"
    assert map_delta_type_to_pg("") == "text"
    assert map_delta_type_to_pg("some_future_type") == "text"


def test_type_map_case_insensitive():
    assert map_delta_type_to_pg("STRING") == "text"
    assert map_delta_type_to_pg("BigInt") == "bigint"


# ── build_column_spec ─────────────────────────────────────────────────────────

def _spec_by_name(specs, name):
    return next(s for s in specs if s["name"] == name)


def test_spec_workspace_id_pk_gets_account_sentinel():
    cols = [("usage_date", "date"), ("workspace_id", "string"), ("total_spend", "double")]
    specs = build_column_spec(cols, pk=["usage_date", "workspace_id"])
    ws = _spec_by_name(specs, "workspace_id")
    assert ws["pg_type"] == "text"
    assert ws["is_pk"] is True
    assert ws["not_null"] is True
    assert ws["sentinel"] == WORKSPACE_ID_SENTINEL
    assert ws["read_expr"] == "COALESCE(CAST(`workspace_id` AS STRING), '__account__') AS `workspace_id`"


def test_spec_date_pk_no_sentinel_plain_read():
    cols = [("usage_date", "date"), ("workspace_id", "string")]
    specs = build_column_spec(cols, pk=["usage_date", "workspace_id"])
    ud = _spec_by_name(specs, "usage_date")
    assert ud["pg_type"] == "date"
    assert ud["not_null"] is True
    assert ud["sentinel"] is None
    assert ud["read_expr"] == "`usage_date`"


def test_spec_non_pk_is_nullable_and_plain():
    cols = [("usage_date", "date"), ("workspace_id", "string"), ("total_spend", "double")]
    specs = build_column_spec(cols, pk=["usage_date", "workspace_id"])
    ts = _spec_by_name(specs, "total_spend")
    assert ts["pg_type"] == "double precision"
    assert ts["is_pk"] is False
    assert ts["not_null"] is False
    assert ts["sentinel"] is None
    assert ts["read_expr"] == "`total_spend`"


def test_spec_generic_string_pk_sentinel():
    cols = [("usage_date", "date"), ("tag_key", "string"), ("tag_value", "string")]
    specs = build_column_spec(cols, pk=["usage_date", "tag_key", "tag_value"])
    tk = _spec_by_name(specs, "tag_key")
    assert tk["sentinel"] == GENERIC_KEY_SENTINEL
    assert tk["read_expr"] == "COALESCE(CAST(`tag_key` AS STRING), '__na__') AS `tag_key`"


def test_spec_complex_non_pk_is_cast_to_string():
    cols = [("usage_date", "date"), ("payload", "struct<a:int>")]
    specs = build_column_spec(cols, pk=["usage_date"])
    p = _spec_by_name(specs, "payload")
    assert p["pg_type"] == "text"
    assert p["is_pk"] is False
    assert p["read_expr"] == "CAST(`payload` AS STRING) AS `payload`"


def test_spec_statement_id_pk_string_sentinel():
    # dbsql_cost_per_query: pk is statement_id (string), source date is query_date (non-pk).
    cols = [("statement_id", "string"), ("query_date", "date"), ("cost", "double")]
    specs = build_column_spec(cols, pk=["statement_id"])
    sid = _spec_by_name(specs, "statement_id")
    assert sid["pg_type"] == "text" and sid["not_null"] is True
    assert sid["sentinel"] == GENERIC_KEY_SENTINEL
    qd = _spec_by_name(specs, "query_date")
    assert qd["pg_type"] == "date" and qd["not_null"] is False


# ── DDL / DML generation ──────────────────────────────────────────────────────

def test_create_table_sql_has_pk_and_notnull_and_quotes():
    cols = [("usage_date", "date"), ("workspace_id", "string"), ("total_spend", "double")]
    specs = build_column_spec(cols, pk=["usage_date", "workspace_id"])
    sql = build_create_table_sql("daily_usage_summary", specs, ["usage_date", "workspace_id"])
    assert 'CREATE TABLE IF NOT EXISTS "daily_usage_summary"' in sql
    assert '"usage_date" date NOT NULL' in sql
    assert '"workspace_id" text NOT NULL' in sql
    assert '"total_spend" double precision' in sql
    assert 'PRIMARY KEY ("usage_date", "workspace_id")' in sql
    # non-PK column must NOT be NOT NULL
    assert '"total_spend" double precision NOT NULL' not in sql


def test_add_column_sql_never_not_null():
    spec = {"name": "new_col", "pg_type": "double precision", "not_null": True}
    sql = build_add_column_sql("daily_usage_summary", spec)
    assert sql == 'ALTER TABLE "daily_usage_summary" ADD COLUMN IF NOT EXISTS "new_col" double precision'
    assert "NOT NULL" not in sql


def test_select_sql_full_has_no_where():
    cols = [("usage_date", "date"), ("workspace_id", "string")]
    specs = build_column_spec(cols, pk=["usage_date", "workspace_id"])
    sql = build_select_sql("cat", "sch", "daily_usage_summary", specs,
                           source_date_col="usage_date")
    assert "WHERE" not in sql
    assert "FROM `cat`.`sch`.`daily_usage_summary`" in sql
    assert "COALESCE(CAST(`workspace_id` AS STRING), '__account__')" in sql


def test_select_sql_windowed_between():
    cols = [("usage_date", "date"), ("workspace_id", "string")]
    specs = build_column_spec(cols, pk=["usage_date", "workspace_id"])
    sql = build_select_sql("cat", "sch", "daily_usage_summary", specs,
                           source_date_col="usage_date",
                           date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 7, 7))
    assert "WHERE `usage_date` BETWEEN DATE'2026-07-01' AND DATE'2026-07-07'" in sql


def test_insert_sql_placeholders_match_columns():
    sql = build_insert_sql("daily_usage_summary", ["usage_date", "workspace_id", "total_spend"])
    assert sql == (
        'INSERT INTO "daily_usage_summary" ("usage_date", "workspace_id", "total_spend") '
        'VALUES (%s, %s, %s)'
    )


def test_insert_sql_on_conflict_when_pk_given():
    sql = build_insert_sql(
        "daily_usage_summary", ["usage_date", "workspace_id", "total_spend"],
        pk=["usage_date", "workspace_id"],
    )
    assert sql.endswith('ON CONFLICT ("usage_date", "workspace_id") DO NOTHING')


def test_delete_range_sql():
    sql = build_delete_range_sql("daily_usage_summary", "usage_date")
    assert sql == 'DELETE FROM "daily_usage_summary" WHERE "usage_date" BETWEEN %s AND %s'


def test_iter_date_chunks():
    # 16 days in 7-day chunks -> [1-7], [8-14], [15-16]
    chunks = iter_date_chunks(dt.date(2026, 7, 1), dt.date(2026, 7, 16), 7)
    assert chunks == [
        (dt.date(2026, 7, 1), dt.date(2026, 7, 7)),
        (dt.date(2026, 7, 8), dt.date(2026, 7, 14)),
        (dt.date(2026, 7, 15), dt.date(2026, 7, 16)),
    ]
    # single day
    assert iter_date_chunks(dt.date(2026, 7, 1), dt.date(2026, 7, 1), 7) == [
        (dt.date(2026, 7, 1), dt.date(2026, 7, 1))
    ]
    # inverted range -> empty
    assert iter_date_chunks(dt.date(2026, 7, 2), dt.date(2026, 7, 1), 7) == []


# ── window math + row projection ──────────────────────────────────────────────

def test_compute_reprocess_start():
    today = dt.date(2026, 7, 16)
    assert compute_reprocess_start(14, 0, today) == dt.date(2026, 7, 2)
    assert compute_reprocess_start(5, 1, today) == dt.date(2026, 7, 10)
    assert compute_reprocess_start(0, 0, today) == today


def test_rows_to_tuples_projects_in_order_with_missing_as_none():
    rows = [
        {"usage_date": dt.date(2026, 7, 1), "workspace_id": "123", "total_spend": 9.5},
        {"usage_date": dt.date(2026, 7, 2), "workspace_id": "456"},  # missing total_spend
    ]
    tuples = _rows_to_tuples(rows, ["usage_date", "workspace_id", "total_spend"])
    assert tuples == [
        (dt.date(2026, 7, 1), "123", 9.5),
        (dt.date(2026, 7, 2), "456", None),
    ]


def test_not_owner_error_is_detected():
    assert _is_not_owner_error(Exception('must be owner of table agg_sync_state_v2'))
    assert not _is_not_owner_error(Exception("permission denied for schema cost_obs_gold"))
