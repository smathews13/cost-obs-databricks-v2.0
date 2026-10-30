"""Genie surfaces come from usage_metadata.genie.surface, and the gold table rebuilds once."""

from backend import lakebase_sync, materialized_views
from backend.materialized_views_extra import (
    CREATE_DAILY_SKU_BREAKDOWN,
    EXTRA_TABLE_REFRESH,
    MERGE_DAILY_SKU_BREAKDOWN,
)
from backend.routers import genie_cost

SURFACE = "get_json_object(to_json(u.usage_metadata), '$.genie.surface')"


def test_live_queries_classify_by_usage_metadata_surface():
    for sql in (genie_cost._GENIE_BY_WORKSPACE_SQL, genie_cost._GENIE_TIMESERIES_SQL):
        assert f"{SURFACE} AS genie_surface_raw" in sql
        for raw, bucket in (("GENIE_CODE", "genie_code"), ("GENIE_ONE", "genie_one"), ("GENIE_AGENTS", "genie_agents")):
            assert f"genie_surface_raw = '{raw}'" in sql
            assert f"THEN '{bucket}'" in sql
    assert f"{SURFACE} AS genie_surface" in genie_cost._GENIE_DIAGNOSTICS_SQL


def test_gold_table_uses_the_same_signal_for_build_and_merge():
    for sql in (CREATE_DAILY_SKU_BREAKDOWN, MERGE_DAILY_SKU_BREAKDOWN):
        assert f"{SURFACE} = 'GENIE_CODE'" in sql
        assert f"{SURFACE} = 'GENIE_ONE'" in sql
        assert f"{SURFACE} = 'GENIE_AGENTS'" in sql
    assert EXTRA_TABLE_REFRESH["daily_sku_breakdown"]["definition_version"] == 2


def _props(monkeypatch, value):
    calls = []

    def _query(sql, *args, **kwargs):
        calls.append(sql)
        if isinstance(value, Exception):
            raise value
        return [{"key": materialized_views._DEFINITION_VERSION_PROP, "value": value}]
    monkeypatch.setattr(materialized_views, "execute_query", _query)
    return calls


def test_table_without_the_version_property_is_outdated(monkeypatch):
    _props(monkeypatch, "Table c.s.t does not have property: cost_obs.definition_version")
    assert materialized_views._definition_outdated("c", "s", "t", {"definition_version": 2})


def test_table_at_the_current_version_is_not_outdated(monkeypatch):
    _props(monkeypatch, "2")
    assert not materialized_views._definition_outdated("c", "s", "t", {"definition_version": 2})


def test_unversioned_tables_never_query_properties(monkeypatch):
    calls = _props(monkeypatch, "1")
    assert not materialized_views._definition_outdated("c", "s", "t", {})
    assert calls == []


def test_unreadable_properties_leave_the_normal_path_alone(monkeypatch):
    _props(monkeypatch, RuntimeError("TABLE_OR_VIEW_NOT_FOUND"))
    assert not materialized_views._definition_outdated("c", "s", "t", {"definition_version": 2})


def test_stamp_records_the_version(monkeypatch):
    calls = _props(monkeypatch, "")
    materialized_views._stamp_definition_version("c", "s", "t", {"definition_version": 2})
    assert calls == ["ALTER TABLE `c`.`s`.`t` SET TBLPROPERTIES ('cost_obs.definition_version' = '2')"]


def test_rebuilt_tables_are_rewritten_not_emptied(monkeypatch):
    seen = {}
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.get_pg_conn", lambda: (_ for _ in ()).throw(RuntimeError("no pg")))
    monkeypatch.setattr(
        lakebase_sync, "sync_one_aggregate",
        lambda catalog, schema, table, cfg, *, full=False, rewrite=False: seen.setdefault(table, (full, rewrite)),
    )
    lakebase_sync.sync_all_aggregates("c", "s", full_tables={"daily_sku_breakdown"})
    assert seen["daily_sku_breakdown"] == (False, True)
    assert seen["daily_usage_summary"] == (False, False)


def _run_sync(monkeypatch, *, stored_mode=None, rewrite=False):
    import datetime as dt
    from contextlib import contextmanager

    ran: list[str] = []

    class _Cur:
        def __init__(self):
            self._one = None

        def execute(self, sql, params=None):
            ran.append(sql if params is None or "mode = %s" not in sql else f"{sql} <- {params[0]}")
            if "backfilled_at, mode" in sql:
                self._one = ("2026-10-01", stored_mode)
            elif "LIMIT 1" in sql:
                self._one = (1,)
            else:
                self._one = None

        def executemany(self, sql, rows):
            ran.append(f"INSERT x{len(rows)}")

        def fetchone(self):
            return self._one

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    @contextmanager
    def _pg():
        yield _Conn()

    def _query(sql, *a, **k):
        if "MIN(" in sql:
            return [{"mn": dt.date(2026, 10, 1), "mx": dt.date(2026, 10, 20)}]
        return [{"usage_date": dt.date(2026, 10, 1), "total_spend": 1.0}]

    monkeypatch.setattr("backend.db.get_pg_conn", _pg)
    monkeypatch.setattr("backend.db.execute_query", _query)
    monkeypatch.setattr(lakebase_sync, "_describe_delta_columns", lambda *a: [("usage_date", "date"), ("total_spend", "double")])
    monkeypatch.setattr(lakebase_sync, "_ensure_pg_table", lambda *a: None)
    monkeypatch.setattr(lakebase_sync, "_ensure_sync_state_table", lambda *a: None)
    monkeypatch.setattr(lakebase_sync, "_finish", lambda *a: None)

    cfg = {"pk": ["usage_date"], "source_date_col": "usage_date", "reprocess_days": 14}
    out = lakebase_sync.sync_one_aggregate("c", "s", "daily_sku_breakdown", cfg, rewrite=rewrite)
    return out, ran


def test_rewrite_replaces_every_chunk_without_emptying(monkeypatch):
    out, ran = _run_sync(monkeypatch, rewrite=True)
    assert out["mode"] == "rewrite" and out["window_start"] == "2026-10-01"
    assert not any("TRUNCATE" in q or "backfilled_at = NULL" in q for q in ran)
    # Marked pending before the first chunk, so a failed run is retried.
    pending = next(i for i, q in enumerate(ran) if q.endswith("<- rewrite_pending"))
    first_chunk = next(i for i, q in enumerate(ran) if "BETWEEN" in q)
    assert pending < first_chunk
    assert sum(q.startswith('DELETE FROM "daily_sku_breakdown" WHERE "usage_date" BETWEEN') for q in ran) == 3
    assert ran[-1].startswith('DELETE FROM "daily_sku_breakdown" WHERE "usage_date" < %s OR')
    assert ran[-1].endswith('IS NULL')


def test_unfinished_rewrite_is_resumed_on_the_next_sync(monkeypatch):
    out, _ = _run_sync(monkeypatch, stored_mode="rewrite_pending")
    assert out["mode"] == "rewrite" and out["window_start"] == "2026-10-01"


def test_normal_sync_stays_incremental(monkeypatch):
    out, _ = _run_sync(monkeypatch, stored_mode="incremental")
    assert out["mode"] == "incremental"


class _ClearCur:
    def __init__(self, refuse):
        self.ran, self._refuse = [], refuse

    def execute(self, sql, params=None):
        self.ran.append(sql)
        for frag, err in self._refuse.items():
            if sql.startswith(frag):
                raise RuntimeError(err)


def test_clear_falls_back_to_delete_without_truncate_privilege():
    cur = _ClearCur({"TRUNCATE": "permission denied for table daily_sku_breakdown"})
    lakebase_sync._clear_table(cur, "daily_sku_breakdown")
    assert cur.ran == [
        "SAVEPOINT clear_table",
        'TRUNCATE TABLE "daily_sku_breakdown"',
        "ROLLBACK TO SAVEPOINT clear_table",
        'DELETE FROM "daily_sku_breakdown"',
        "RELEASE SAVEPOINT clear_table",
    ]


def test_clear_failure_propagates_so_the_loading_flag_rolls_back():
    import pytest
    cur = _ClearCur({"TRUNCATE": "permission denied for table t", "DELETE": "permission denied for table t"})
    with pytest.raises(RuntimeError):
        lakebase_sync._clear_table(cur, "t")
