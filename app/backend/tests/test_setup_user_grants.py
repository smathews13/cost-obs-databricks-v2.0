"""Create Tables grants the installing user USE + MANAGE on the app catalog and reports it."""

from backend.routers import setup


def _exec(monkeypatch, refuse=()):
    ran = []

    def _query(sql, *args, **kwargs):
        ran.append(sql)
        if any(r in sql for r in refuse):
            raise RuntimeError("PERMISSION_DENIED: User does not have MANAGE on Catalog 'cat'")
        return []
    monkeypatch.setattr("backend.db.execute_query", _query)
    return ran


def test_grants_use_and_manage_on_catalog_and_schema(monkeypatch):
    ran = _exec(monkeypatch)
    out = setup._grant_user_catalog_visibility("cat", "sch", "", "admin@example.com")
    assert out["ok"] and out["grantee"] == "admin@example.com" and out["manual_sql"] == ""
    assert ran == [
        "GRANT USE CATALOG ON CATALOG `cat` TO `admin@example.com`",
        "GRANT USE SCHEMA ON CATALOG `cat` TO `admin@example.com`",
        "GRANT MANAGE ON CATALOG `cat` TO `admin@example.com`",
        "GRANT USE SCHEMA ON SCHEMA `cat`.`sch` TO `admin@example.com`",
        "GRANT SELECT ON SCHEMA `cat`.`sch` TO `admin@example.com`",
        "GRANT MANAGE ON SCHEMA `cat`.`sch` TO `admin@example.com`",
    ]


def test_one_refused_grant_does_not_skip_the_rest(monkeypatch):
    ran = _exec(monkeypatch, refuse=("MANAGE ON CATALOG",))
    out = setup._grant_user_catalog_visibility("cat", "sch", "", "admin@example.com")
    assert not out["ok"]
    assert len(ran) == 6 and len(out["applied"]) == 5
    assert out["failed"][0]["sql"] == "GRANT MANAGE ON CATALOG `cat` TO `admin@example.com`"
    assert out["manual_sql"] == "GRANT MANAGE ON CATALOG `cat` TO `admin@example.com`;"


def test_no_identity_is_reported_not_silent(monkeypatch):
    ran = _exec(monkeypatch)
    out = setup._grant_user_catalog_visibility("cat", "sch", "", "")
    assert not out["ok"] and out["error"] and ran == []


def test_created_tables_lists_gold_and_config_tables(monkeypatch):
    import asyncio
    monkeypatch.setattr(setup, "get_catalog_schema", lambda: ("cat", "sch"))
    monkeypatch.setattr(
        setup, "check_materialized_views_exist",
        lambda catalog, schema, names, raise_on_failure=False: {n: n != "daily_sku_breakdown" for n in names},
    )
    out = asyncio.run(setup.get_created_tables())
    assert (out["catalog"], out["schema"]) == ("cat", "sch")
    by_name = {t["name"]: t["exists"] for t in out["tables"]}
    assert by_name["daily_sku_breakdown"] is False
    assert by_name["daily_usage_summary"] is True
    assert by_name["app_workspace_filter"] is True


class _Req:
    def __init__(self, email):
        self.headers = {"X-Forwarded-Email": email}


def test_only_admins_receive_catalog_grants(monkeypatch):
    import asyncio

    async def _role(email):
        return "admin" if email == "admin@example.com" else "consumer"
    monkeypatch.setattr("backend.routers.user._get_user_role", _role)
    assert asyncio.run(setup._admin_installer_email(_Req("admin@example.com"))) == "admin@example.com"
    assert asyncio.run(setup._admin_installer_email(_Req("viewer@example.com"))) is None
    assert asyncio.run(setup._admin_installer_email(_Req(""))) is None


def test_table_results_skip_build_steps_and_metadata():
    out = setup._table_results({
        "catalog": "ok", "schema": "exists", "__mv_timings__": {"x": 1}, "__note": "n",
        "daily_usage_summary": "refreshed", "daily_sku_breakdown": "created",
    })
    assert out == {"daily_usage_summary": "refreshed", "daily_sku_breakdown": "created"}


def test_role_lookup_failure_means_no_grants(monkeypatch):
    import asyncio

    async def _boom(email):
        raise RuntimeError("warehouse starting")
    monkeypatch.setattr("backend.routers.user._get_user_role", _boom)
    assert asyncio.run(setup._admin_installer_email(_Req("admin@example.com"))) is None


def test_created_tables_reports_a_failed_listing(monkeypatch):
    import asyncio
    import pytest
    from fastapi import HTTPException

    def _fail(catalog, schema, names, raise_on_failure):
        assert raise_on_failure
        raise RuntimeError("could not list tables in cat.sch: PERMISSION_DENIED")
    monkeypatch.setattr(setup, "get_catalog_schema", lambda: ("cat", "sch"))
    monkeypatch.setattr(setup, "check_materialized_views_exist", _fail)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(setup.get_created_tables())
    assert exc.value.status_code == 503 and "PERMISSION_DENIED" in exc.value.detail


def test_verification_skips_tables_the_build_skipped(monkeypatch):
    probed = []

    def _probe(sql):
        probed.append(sql)
        if "daily_cloud_actuals" in sql:
            raise RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] daily_cloud_actuals")
    monkeypatch.setattr(setup, "_execute_as_sp", _probe)
    out = setup._verify_built_objects_as_sp("cat", "sch", frozenset({"daily_cloud_actuals"}))
    assert out["ok"] and not any("daily_cloud_actuals" in q for q in probed)
    assert not setup._verify_built_objects_as_sp("cat", "sch")["ok"]
