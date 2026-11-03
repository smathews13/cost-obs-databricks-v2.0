import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import settings, setup

ADMIN_ONLY = [
    ("POST", "/api/setup/save-workspace-filter"),
    ("GET", "/api/setup/list-workspaces"),
    ("POST", "/api/setup/complete"),
    ("POST", "/api/setup/mark-complete"),
    ("POST", "/api/setup/rerun"),
    ("POST", "/api/setup/reset-bootstrap"),
    ("POST", "/api/setup/grant-sp-system-access"),
    ("POST", "/api/setup/create-tables"),
    ("POST", "/api/setup/refresh-tables"),
    ("POST", "/api/setup/aws-cur/create-tables"),
    ("POST", "/api/setup/aws-cur/refresh"),
    ("POST", "/api/setup/ensure-catalog"),
    ("POST", "/api/setup/ensure-schema"),
    ("POST", "/api/setup/grant-catalog-access"),
    ("POST", "/api/setup/generate-token"),
    ("DELETE", "/api/setup/drop-materialized-views"),
    ("POST", "/api/setup/mv-overrides"),
    ("POST", "/api/setup/bootstrap-admin"),
    ("POST", "/api/settings/mv-sources"),
    ("DELETE", "/api/settings/mv-sources"),
    ("POST", "/api/settings/catalog"),
    ("POST", "/api/settings/refresh-mvs"),
    ("POST", "/api/settings/auth-mode"),
    ("POST", "/api/settings/azure-connections"),
    ("DELETE", "/api/settings/azure-connections/abc"),
    ("POST", "/api/settings/webhook/send-alert"),
    ("PUT", "/api/settings/pricing-mode"),
]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(setup.router, prefix="/api/setup")
    app.include_router(settings.router, prefix="/api/settings")
    return TestClient(app, raise_server_exceptions=False)


def _as(monkeypatch, role):
    async def _role(_email):
        return role

    monkeypatch.setattr("backend.routers.user._get_user_role", _role)


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY)
def test_consumers_cannot_change_app_setup(client, monkeypatch, method, path):
    _as(monkeypatch, "consumer")
    called = []
    monkeypatch.setattr(
        "backend.routers.setup._write_saved_workspace_ids", lambda ids: called.append(ids)
    )

    res = client.request(
        method,
        path,
        json={"workspace_ids": ["111"]},
        headers={"X-Forwarded-Email": "viewer@example.com"},
    )

    assert res.status_code == 403
    assert called == []


def test_admins_pass_the_admin_check(client, monkeypatch):
    _as(monkeypatch, "admin")
    res = client.post(
        "/api/setup/reset-bootstrap", headers={"X-Forwarded-Email": "admin@example.com"}
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
