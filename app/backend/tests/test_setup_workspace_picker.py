import asyncio
import json

import pytest
from fastapi import HTTPException

from backend.routers import setup


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    """Route execute_query/execute_write through a recorder; `row` is the stored filter row."""
    state = {"cols": ["workspace_ids_json", "updated_at"], "row": None, "writes": [], "mv": [], "read_error": None,
             "cache_clears": 0}

    def _query(sql, *args, **kwargs):
        if state["read_error"] and sql.startswith("SELECT *"):
            raise state["read_error"]
        if "daily_workspace_breakdown" in sql:
            return state["mv"]
        if sql.startswith("DESCRIBE"):
            return [{"col_name": c} for c in state["cols"]]
        if "app_workspace_filter" in sql:
            return [state["row"]] if state["row"] is not None else []
        return []

    def _write(sql, params=None):
        state["writes"].append((sql, params))
        return 1

    monkeypatch.setattr(setup, "SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr("backend.db.get_catalog_schema", lambda: ("cat", "sch"))
    monkeypatch.setattr("backend.db.execute_query", _query)
    monkeypatch.setattr("backend.db.execute_write", _write)
    monkeypatch.setattr("backend.routers.settings._ensure_workspace_filter_table", lambda: None)
    monkeypatch.setattr("backend.routers.settings._config_table", lambda name: f"`cat`.`sch`.{name}")

    def _clear(*_args):
        state["cache_clears"] += 1
    monkeypatch.setattr("backend.db.delta_cache_invalidate", _clear)
    return state


class _Req:
    headers = {"X-Forwarded-Email": "admin@example.com"}

    def __init__(self, ids):
        self._ids = ids

    async def json(self):
        return {"workspace_ids": self._ids}


@pytest.fixture
def admin(monkeypatch):
    async def _role(_email):
        return "admin"
    monkeypatch.setattr("backend.routers.user._get_user_role", _role)


def test_list_workspaces_falls_back_to_top_nav_list_when_mv_empty(fake_db, monkeypatch):
    calls = []

    async def _top_nav(start_date=None, end_date=None, *, apply_pool):
        calls.append(apply_pool)
        return {"workspaces": [
            {"id": "111", "name": "prod", "historical": False},
            {"id": "222", "name": None, "historical": True},
        ]}
    monkeypatch.setattr("backend.routers.billing.workspace_list", _top_nav)
    out = asyncio.run(setup.list_workspaces())
    assert calls == [False]  # the picker lists workspaces outside the current pool
    assert out["workspaces"] == [
        {"id": "111", "name": "prod", "historical": False},
        {"id": "222", "name": "222", "historical": True},
    ]


def test_list_workspaces_prefers_mv_when_built(fake_db, monkeypatch):
    fake_db["mv"] = [{"workspace_id": "111", "workspace_name": "prod"}]

    async def _top_nav(*args, **kwargs):
        raise AssertionError("fallback should not run")
    monkeypatch.setattr("backend.routers.billing.workspace_list", _top_nav)
    assert asyncio.run(setup.list_workspaces())["workspaces"] == [{"id": "111", "name": "prod", "historical": False}]


@pytest.mark.parametrize("row, ids", [
    ({"workspace_ids_json": '["111", "222"]'}, ["111", "222"]),
    ({"workspace_ids_json": "[]"}, []),
    ({"workspace_ids": "111,222"}, ["111", "222"]),
    ({"workspace_ids": ""}, []),
    ({"workspace_ids": "  "}, []),
    ({"workspace_ids_json": "not json"}, []),
])
def test_parse_saved_ids_reads_both_schemas(row, ids):
    assert setup._parse_saved_workspace_ids(row) == ids


def test_get_returns_saved_pool_in_either_schema(fake_db):
    fake_db["row"] = {"workspace_ids": ""}
    assert asyncio.run(setup.get_workspace_filter()) == {"workspace_ids": []}
    fake_db["row"] = {"workspace_ids_json": '["111"]', "updated_at": None}
    assert asyncio.run(setup.get_workspace_filter()) == {"workspace_ids": ["111"]}


def test_save_writes_json_column_created_by_create_tables(fake_db, admin):
    assert asyncio.run(setup.save_workspace_filter(_Req(["111", "222"]))) == {"saved": ["111", "222"]}
    [(sql, params)] = fake_db["writes"]  # one atomic statement, no DELETE-then-INSERT
    assert sql.startswith("INSERT OVERWRITE") and "workspace_ids_json" in sql
    assert json.loads(params["v"]) == ["111", "222"]


def test_save_writes_legacy_csv_column(fake_db, admin):
    fake_db["cols"] = ["workspace_ids"]
    asyncio.run(setup.save_workspace_filter(_Req(["111", "222"])))
    [(sql, params)] = fake_db["writes"]
    assert sql.startswith("INSERT OVERWRITE") and "(workspace_ids)" in sql and params == {"v": "111,222"}


def test_saving_all_workspaces_clears_pool(fake_db, admin, tmp_path):
    fake_db["row"] = {"workspace_ids": ""}
    assert asyncio.run(setup.save_workspace_filter(_Req([]))) == {"saved": []}
    assert json.loads((tmp_path / "workspace_filter.json").read_text()) == {"workspace_ids": []}
    assert [w[0].split()[0] for w in fake_db["writes"]] == ["DELETE"]


def test_saved_pool_can_be_changed_and_cleared(fake_db, admin, tmp_path):
    fake_db["row"] = {"workspace_ids_json": '["111"]'}
    assert asyncio.run(setup.save_workspace_filter(_Req(["222"]))) == {"saved": ["222"]}
    assert json.loads((tmp_path / "workspace_filter.json").read_text()) == {"workspace_ids": ["222"]}
    assert asyncio.run(setup.save_workspace_filter(_Req([]))) == {"saved": []}
    assert json.loads((tmp_path / "workspace_filter.json").read_text()) == {"workspace_ids": []}
    assert [w[0].split()[0] for w in fake_db["writes"]] == ["INSERT", "DELETE"]


def test_save_clears_response_caches(fake_db, admin):
    asyncio.run(setup.save_workspace_filter(_Req(["111"])))
    assert fake_db["cache_clears"] == 1


def test_save_requires_admin(fake_db, monkeypatch):
    async def _role(_email):
        return "consumer"
    monkeypatch.setattr("backend.routers.user._get_user_role", _role)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(setup.save_workspace_filter(_Req(["111"])))
    assert exc.value.status_code == 403
    assert fake_db["writes"] == []


def test_bundle_cache_key_changes_with_pool(monkeypatch):
    from backend import db
    monkeypatch.setattr("backend.workspace_filter.get_configured_workspace_ids", lambda: ["111"])
    scoped = db.bundle_cache_key("genie:dashboard-bundle", "2026-01-01", "2026-01-31", None)
    monkeypatch.setattr("backend.workspace_filter.get_configured_workspace_ids", lambda: [])
    unscoped = db.bundle_cache_key("genie:dashboard-bundle", "2026-01-01", "2026-01-31", None)
    assert scoped != unscoped
    # An explicit selection doesn't depend on the pool.
    assert db.bundle_cache_key("x", "a", "b", ["111"]) == db.bundle_cache_key("x", "a", "b", ["111"])


def test_unreadable_pool_is_an_error_not_all_workspaces(fake_db):
    fake_db["read_error"] = RuntimeError("warehouse unavailable")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(setup.get_workspace_filter())
    assert exc.value.status_code == 503


def test_settings_file_wins_over_delta(fake_db, tmp_path):
    (tmp_path / "workspace_filter.json").write_text('{"workspace_ids": []}')
    fake_db["row"] = {"workspace_ids_json": '["111"]'}
    fake_db["read_error"] = RuntimeError("should not be read")
    assert asyncio.run(setup.get_workspace_filter()) == {"workspace_ids": []}


def test_access_errors_name_the_table_and_grant(fake_db, monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-123")
    fake_db["read_error"] = RuntimeError("[INSUFFICIENT_PERMISSIONS] User does not have SELECT on Table")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(setup.get_workspace_filter())
    assert exc.value.status_code == 503
    assert "GRANT SELECT, MODIFY ON TABLE `cat`.`sch`.app_workspace_filter TO `sp-123`" in exc.value.detail
