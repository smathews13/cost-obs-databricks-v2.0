"""Regression tests for SP identity and permissions bundle in permissions.py.

Covers:
- _get_sp_info() returns client_id + display_name from the SP singleton
- _get_sp_info() falls back gracefully when workspace client throws
- SP drift: client_id from env var is always included even when API call fails
- _check_permissions_sync() includes sp info in the response payload
- grant bundle targeting: sp_client_id surfaces in the readiness response

Run with: pytest server/tests/test_permissions_bundle.py -v
"""
from unittest.mock import MagicMock, patch

import pytest

import backend.routers.permissions as perms_mod
from backend.routers.permissions import (
    REQUIRED_PERMISSIONS,
    _check_permissions_sync,
    _get_sp_info,
)


@pytest.fixture(autouse=True)
def reset_permissions_cache():
    """Clear the in-process permissions cache between tests."""
    perms_mod._permissions_cache = None
    perms_mod._permissions_cache_ts = 0.0
    yield
    perms_mod._permissions_cache = None
    perms_mod._permissions_cache_ts = 0.0


# ---------------------------------------------------------------------------
# _get_sp_info — identity surfacing
# ---------------------------------------------------------------------------

class TestGetSpInfo:
    def test_returns_client_id_from_env(self, monkeypatch):
        """client_id comes from DATABRICKS_CLIENT_ID env var, always."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-abc-123")

        mock_me = MagicMock()
        mock_me.display_name = "Cost Observer SP"
        mock_me.user_name = "sp-abc-123"

        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        with patch("backend.routers.permissions.get_workspace_client", return_value=mock_client):
            info = _get_sp_info()

        assert info["client_id"] == "sp-abc-123"
        assert info["display_name"] == "Cost Observer SP"

    def test_falls_back_gracefully_on_api_error(self, monkeypatch):
        """When workspace client throws, client_id is still returned from env var."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-fallback-999")

        mock_client = MagicMock()
        mock_client.current_user.me.side_effect = RuntimeError("auth error")

        with patch("backend.routers.permissions.get_workspace_client", return_value=mock_client):
            info = _get_sp_info()

        # Must not raise — must return the env-var value
        assert info["client_id"] == "sp-fallback-999"
        # display_name falls back to client_id when API fails
        assert info["display_name"] == "sp-fallback-999"

    def test_empty_env_var_does_not_raise(self, monkeypatch):
        """When DATABRICKS_CLIENT_ID is absent, should still return a dict."""
        monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)

        mock_client = MagicMock()
        mock_client.current_user.me.side_effect = RuntimeError("no client id")

        with patch("backend.routers.permissions.get_workspace_client", return_value=mock_client):
            info = _get_sp_info()

        assert isinstance(info, dict)
        assert "client_id" in info
        assert "display_name" in info

    def test_uses_display_name_over_user_name(self, monkeypatch):
        """When both display_name and user_name are present, display_name wins."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-xyz")

        mock_me = MagicMock()
        mock_me.display_name = "Human Readable Name"
        mock_me.user_name = "sp-xyz@apps.databricks.com"

        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        with patch("backend.routers.permissions.get_workspace_client", return_value=mock_client):
            info = _get_sp_info()

        assert info["display_name"] == "Human Readable Name"

    def test_falls_back_to_user_name_when_no_display_name(self, monkeypatch):
        """When display_name is None, user_name is used as display_name."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-xyz")

        mock_me = MagicMock()
        mock_me.display_name = None
        mock_me.user_name = "sp-xyz@apps.databricks.com"

        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        with patch("backend.routers.permissions.get_workspace_client", return_value=mock_client):
            info = _get_sp_info()

        assert info["display_name"] == "sp-xyz@apps.databricks.com"


# ---------------------------------------------------------------------------
# _check_permissions_sync — SP info surfaces in response
# ---------------------------------------------------------------------------

class TestCheckPermissionsSyncSpBundle:
    def _mock_all_granted(self):
        """Returns a check_table_access mock that grants every table."""
        return lambda table: (True, "")

    def test_sp_info_included_in_response(self, monkeypatch):
        """Response must contain 'sp' dict with client_id and display_name."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-bundle-test")

        mock_me = MagicMock()
        mock_me.display_name = "Bundle Test SP"
        mock_me.user_name = "sp-bundle-test"
        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        with (
            patch("backend.routers.permissions.get_workspace_client", return_value=mock_client),
            patch("backend.routers.permissions.check_table_access", side_effect=self._mock_all_granted()),
            patch("backend.routers.permissions._get_current_user", return_value=("user@db.com", "User")),
        ):
            result = _check_permissions_sync(bypass_cache=True)

        assert "sp" in result
        assert result["sp"]["client_id"] == "sp-bundle-test"
        assert result["sp"]["display_name"] == "Bundle Test SP"

    def test_response_includes_all_required_tables(self, monkeypatch):
        """Every entry in REQUIRED_PERMISSIONS must appear in the response."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-check")

        mock_me = MagicMock()
        mock_me.display_name = None
        mock_me.user_name = "sp-check"
        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        with (
            patch("backend.routers.permissions.get_workspace_client", return_value=mock_client),
            patch("backend.routers.permissions.check_table_access", return_value=(True, "")),
            patch("backend.routers.permissions._get_current_user", return_value=("user@db.com", "User")),
        ):
            result = _check_permissions_sync(bypass_cache=True)

        returned_tables = {p["table"] for p in result["permissions"]}
        required_tables = {p["table"] for p in REQUIRED_PERMISSIONS}
        assert returned_tables == required_tables

    def test_denied_table_sets_granted_false(self, monkeypatch):
        """A denied table must appear with granted=False and an error field."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-denied")

        mock_me = MagicMock()
        mock_me.display_name = None
        mock_me.user_name = "sp-denied"
        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        def selective_access(table):
            if table == "system.query.history":
                return (False, "PERMISSION_DENIED: Access to system.query.history denied")
            return (True, "")

        with (
            patch("backend.routers.permissions.get_workspace_client", return_value=mock_client),
            patch("backend.routers.permissions.check_table_access", side_effect=selective_access),
            patch("backend.routers.permissions._get_current_user", return_value=("user@db.com", "User")),
        ):
            result = _check_permissions_sync(bypass_cache=True)

        query_hist = next(p for p in result["permissions"] if p["table"] == "system.query.history")
        assert query_hist["granted"] is False
        assert "error" in query_hist

    def test_summary_all_required_granted_false_when_required_table_denied(self, monkeypatch):
        """all_required_granted must be False when a required table is denied."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-required-denied")

        mock_me = MagicMock()
        mock_me.display_name = None
        mock_me.user_name = "sp-required-denied"
        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        # Deny system.billing.usage which is required=True
        def selective_access(table):
            if table == "system.billing.usage":
                return (False, "PERMISSION_DENIED")
            return (True, "")

        with (
            patch("backend.routers.permissions.get_workspace_client", return_value=mock_client),
            patch("backend.routers.permissions.check_table_access", side_effect=selective_access),
            patch("backend.routers.permissions._get_current_user", return_value=("user@db.com", "User")),
        ):
            result = _check_permissions_sync(bypass_cache=True)

        assert result["summary"]["all_required_granted"] is False
        assert result["summary"]["ready_to_use"] is False

    def test_caching_returns_same_object_on_second_call(self, monkeypatch):
        """Second call within TTL must return the cached result (same dict identity)."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-cache")

        mock_me = MagicMock()
        mock_me.display_name = "Cached SP"
        mock_me.user_name = "sp-cache"
        mock_client = MagicMock()
        mock_client.current_user.me.return_value = mock_me

        with (
            patch("backend.routers.permissions.get_workspace_client", return_value=mock_client),
            patch("backend.routers.permissions.check_table_access", return_value=(True, "")),
            patch("backend.routers.permissions._get_current_user", return_value=("user@db.com", "User")),
        ):
            result1 = _check_permissions_sync(bypass_cache=True)
            result2 = _check_permissions_sync()  # Should hit cache

        assert result1 is result2, "Second call should return the cached object"
