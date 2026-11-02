import time
from types import SimpleNamespace

import pytest

from backend import db
from backend.routers import ai_gateway


def test_lakebase_failure_trips_cooldown_instead_of_waiting_each_request(monkeypatch):
    calls = []

    class _Pool:
        def getconn(self, timeout=None):
            calls.append(timeout)
            raise TimeoutError("couldn't get a connection")

    monkeypatch.setattr(db, "_get_lakebase_pool", lambda: _Pool())
    monkeypatch.setattr(db, "_lakebase_down_until", 0.0)
    with pytest.raises(RuntimeError):
        with db.get_pg_conn():
            pass
    assert calls == [db._LAKEBASE_GETCONN_TIMEOUT_S]
    assert db.lakebase_available() is False
    with pytest.raises(RuntimeError, match="recent connection failure"):
        with db.get_pg_conn():
            pass
    assert len(calls) == 1
    monkeypatch.setattr(db, "_lakebase_down_until", time.monotonic() - 1)
    assert db.lakebase_available() is True


def test_ws_ids_accepts_ui_csv_and_drops_unsafe(monkeypatch):
    monkeypatch.delenv("COST_OBS_WORKSPACES", raising=False)
    monkeypatch.setattr("backend.workspace_filter.get_configured_workspace_ids", lambda: [])
    assert ai_gateway._ws_ids("222,111", None) == ["111", "222"]
    assert ai_gateway._ws_ids("1'; DROP TABLE x--", ["333"]) == ["333"]
    assert ai_gateway._ws_ids(None, None) is None


def test_workspace_filter_binds_strings_not_ints():
    sql, params = ai_gateway._apply_workspace_filter("WHERE 1=1 {workspace_filter}", ["abc-123"])
    assert "CAST(workspace_id AS STRING) IN (:ws_0)" in sql
    assert params == {"ws_0": "abc-123"}


def test_billing_sums_across_workspaces_and_splits_by_tokens():
    billing = ai_gateway._billing_by_date([
        {"usage_date": "2026-10-01", "estimated_cost_usd": 6},
        {"usage_date": "2026-10-01", "estimated_cost_usd": 4},
    ])
    assert billing == {"2026-10-01": 10.0}
    rows = ai_gateway._annotate_costs([
        {"usage_date": "2026-10-01", "model_name": "a", "total_tokens": 300},
        {"usage_date": "2026-10-01", "model_name": "b", "total_tokens": 100},
    ], billing)
    assert [r["estimated_cost_usd"] for r in rows] == [7.5, 2.5]


def test_inventory_timeout_returns_empty_shape(monkeypatch):
    from backend import unity_gateway as ug
    monkeypatch.setattr(ug, "INVENTORY_TIMEOUT_S", 0.05)
    ug._inventory_cache.clear()
    out = ug.cached_inventory("slow", lambda: time.sleep(0.5) or {"available": True}, {"spaces": []})
    assert out["available"] is False and out["spaces"] == []


def test_genie_conversation_count_pages_through(monkeypatch):
    from backend import unity_gateway as ug
    pages = {None: (["c"] * 100, "t1"), "t1": (["c"] * 42, None)}

    def list_conversations(space_id, page_size, include_all, page_token=None):
        convs, nxt = pages[page_token]
        return SimpleNamespace(conversations=convs, next_page_token=nxt)

    genie = SimpleNamespace(
        list_spaces=lambda page_size: SimpleNamespace(spaces=[]),
        list_conversations=list_conversations,
    )
    monkeypatch.setattr(ug, "get_workspace_client", lambda: SimpleNamespace(genie=genie))
    out = ug.list_genie_space_inventory("space-1")
    assert out["configured_conversation_count"] == 142
    assert out["configured_conversation_count_capped"] is False


def test_idle_time_gold_row_without_name_falls_back_to_id(monkeypatch):
    import asyncio

    from backend import lakebase_read
    from backend.routers import warehouse_health as wh

    row = {"warehouse_id": "abc123", "warehouse_name": None, "warehouse_type": "CLASSIC",
           "total_running_minutes": 120, "idle_minutes": 60, "total_spend": 5,
           "estimated_idle_spend": None}
    monkeypatch.setattr(wh, "_warehouse_scope_blocked", lambda: False)
    monkeypatch.setattr(wh, "_idle_time_cache", None)
    monkeypatch.setattr(lakebase_read, "gold_read", lambda *a, **k: [row])
    monkeypatch.setattr(lakebase_read, "warehouse_view_fallback", lambda: False)
    # No dates: the default-date path used to import missing backend.db helpers and 500.
    out = asyncio.run(wh.get_warehouse_idle_time(None, None, None))
    [w] = out["warehouses"]
    assert w["warehouse_name"] == "abc123"
    assert w["idle_pct"] == 50.0
    assert w["estimated_idle_spend"] is None

    row.update(warehouse_id=None)
    monkeypatch.setattr(wh, "_idle_time_cache", None)
    out = asyncio.run(wh.get_warehouse_idle_time("2026-10-01", "2026-10-07", None))
    assert out["warehouses"][0]["warehouse_name"] == "Unattributed"
