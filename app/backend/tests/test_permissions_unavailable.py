import asyncio

import pytest

from backend.routers import settings, user


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(user, "_perm_cache", None)
    monkeypatch.setattr(user, "_perm_cache_at", 0.0)
    monkeypatch.setattr(user, "USER_PERMISSIONS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(settings, "USER_PERMISSIONS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr("backend.db.lakebase_available", lambda: False)
    monkeypatch.setattr("backend.db.get_catalog_schema", lambda: ("cat", "sch"))
    monkeypatch.setattr(settings, "_ensure_permissions_table", lambda: None)
    monkeypatch.setattr(user, "app_creator_email", lambda: "creator@example.com")


def _delta_raises(monkeypatch, message):
    def _boom(*_a, **_k):
        raise RuntimeError(message)

    monkeypatch.setattr("backend.db.execute_query", _boom)


def _role(email):
    return asyncio.run(user._get_user_role(email))


def test_unreadable_permissions_leave_only_the_app_creator_as_admin(monkeypatch):
    _delta_raises(monkeypatch, "PERMISSION_DENIED: no SELECT on app_user_permissions")
    assert _role("viewer@example.com") == "consumer"
    assert _role("Creator@example.com") == "admin"


def test_a_permissions_table_that_does_not_exist_yet_means_fresh_install(monkeypatch):
    _delta_raises(monkeypatch, "[TABLE_OR_VIEW_NOT_FOUND] app_user_permissions")
    assert _role("anyone@example.com") == "admin"


def test_settings_admin_check_follows_the_same_rule(monkeypatch):
    _delta_raises(monkeypatch, "warehouse unreachable")
    perms = settings._load_user_permissions()
    assert perms.get("unavailable") is True
    assert settings._is_admin_email("viewer@example.com", perms) is False
    assert settings._is_admin_email("creator@example.com", perms) is True
    assert settings._is_admin_email("anyone@example.com", {"admins": [], "consumers": []}) is True


def test_first_admin_route_never_overwrites_admins_it_could_not_read(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.routers import setup

    _delta_raises(monkeypatch, "warehouse unreachable")
    saved = []
    monkeypatch.setattr(settings, "_save_user_permissions_to_table", lambda *a, **k: saved.append(a))
    app = FastAPI()
    app.include_router(setup.router, prefix="/api/setup")
    res = TestClient(app).post(
        "/api/setup/bootstrap-admin", headers={"X-Forwarded-Email": "creator@example.com"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "error"
    assert saved == []
