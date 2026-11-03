"""Older billing endpoints no screen calls must still stay inside the saved workspace scope."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import lakebase_read, workspace_filter
from backend.routers import billing

LEGACY = [
    "/by-workspace", "/timeseries", "/etl-breakdown", "/infra-costs", "/infra-costs-timeseries",
    "/aws-costs", "/aws-costs-timeseries", "/dashboard-bundle", "/spend-by-user-group",
    "/spend-anomalies", "/platform-kpis",
]


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _execute(sql, params=None, **_k):
        calls.append(("live", sql))
        return []

    def _gold(name, pg_tmpl, params, ws_clause="", live_sql=None, extra_format=None):
        calls.append(("gold", ws_clause))
        if live_sql is not None:
            calls.append(("live", live_sql))
        return []

    def _mv(template, params, ws_filter=""):
        calls.append(("gold", ws_filter))
        return []

    def _no_sdk():
        raise RuntimeError("no workspace client in tests")

    monkeypatch.setattr(workspace_filter, "get_configured_workspace_ids", lambda: ["111"])
    monkeypatch.setattr(billing, "execute_query", _execute)
    monkeypatch.setattr(billing, "_exec_mv", _mv)
    monkeypatch.setattr(billing, "_check_mv_available", lambda: True)
    monkeypatch.setattr(billing, "source_scope_unavailable", lambda *a, **k: None)
    monkeypatch.setattr(billing, "get_workspace_client", _no_sdk)
    monkeypatch.setattr(billing, "execute_queries_parallel", lambda qs, timeout=None: {k: fn() for k, fn in qs})
    monkeypatch.setattr(lakebase_read, "gold_read", _gold)
    monkeypatch.setattr(lakebase_read, "warehouse_view_fallback", lambda: True)
    monkeypatch.setattr("backend.routers.settings.anomaly_spike_threshold", lambda: 20.0)
    return calls


@pytest.mark.parametrize("path", LEGACY)
def test_legacy_endpoint_only_reads_the_saved_scope(sent, path):
    app = FastAPI()
    app.include_router(billing.router, prefix="/api/billing")
    res = TestClient(app).get(f"/api/billing{path}")

    assert res.status_code == 200
    assert sent, "endpoint ran no queries"
    for kind, text in sent:
        if kind == "gold":
            assert "'111'" in text, f"{path} read gold data without the scope"
        elif "system.billing.usage" in text:
            assert "'111'" in text, f"{path} ran live billing SQL without the scope"


def test_scoped_platform_kpis_skip_live_sql_the_scope_cannot_reach(sent):
    app = FastAPI()
    app.include_router(billing.router, prefix="/api/billing")
    TestClient(app).get("/api/billing/platform-kpis")
    assert not [t for k, t in sent if k == "live" and "system.lakeflow" in t]
