"""Regression tests for feature gating and schema classification in setup.py.

Covers:
- _calc_overall() produces correct readiness strings for all cases
- _build_warehouse_item() sets granted=False and fix_sql when warehouse is denied
- _build_fix_sql() generates valid SQL targeting the SP principal
- _check_readiness_sync() routes denied tables to core vs. enhanced correctly
- Warehouse permission denied produces fix_sql (maps to "Grant SQL" in UI)
- All-core-denied produces "not_ready" (worst case, no partial optimism)

Run with: pytest server/tests/test_feature_gating.py -v
"""
from unittest.mock import patch

import pytest

import backend.routers.setup as setup_mod
from backend.routers.setup import (
    CheckStatus,
    WarehouseCheckResult,
    _build_fix_sql,
    _build_warehouse_item,
    _calc_overall,
    reset_readiness_caches,
)


@pytest.fixture(autouse=True)
def clean_caches():
    reset_readiness_caches()
    yield
    reset_readiness_caches()


# ---------------------------------------------------------------------------
# _calc_overall — state machine exhaustion
# ---------------------------------------------------------------------------

class TestCalcOverall:
    def _core_checks(self, *granted_flags: bool) -> list[dict]:
        return [{"granted": g} for g in granted_flags]

    def test_all_pass_returns_ready(self):
        assert _calc_overall(True, True, True, self._core_checks(True)) == "ready"

    def test_core_and_wh_pass_no_enhanced_returns_core_ready(self):
        # enhanced_all_pass=False but core+wh pass → core_ready
        assert _calc_overall(True, True, False, self._core_checks(True)) == "core_ready"

    def test_core_pass_wh_fail_returns_needs_action(self):
        # Core tables ok, warehouse denied
        assert _calc_overall(True, False, False, self._core_checks(True)) == "needs_action"

    def test_core_fail_wh_pass_returns_needs_action(self):
        # Core tables denied but some core checks have granted=True
        assert _calc_overall(False, True, False, self._core_checks(True, False)) == "needs_action"

    def test_all_core_denied_returns_not_ready(self):
        # No core check is granted — worst case
        assert _calc_overall(False, False, False, self._core_checks(False, False)) == "not_ready"

    def test_partial_core_grants_returns_needs_action(self):
        # One core table granted, one denied — tables_pass=False but any granted=True
        checks = self._core_checks(True, False)
        result = _calc_overall(False, False, False, checks)
        assert result == "needs_action"

    def test_empty_core_checks_no_core_pass_returns_not_ready(self):
        # Edge case: no core checks at all, nothing passes
        assert _calc_overall(False, False, False, []) == "not_ready"


# ---------------------------------------------------------------------------
# _build_warehouse_item — fix_sql generation on denial
# ---------------------------------------------------------------------------

class TestBuildWarehouseItem:
    def _denied_result(self) -> WarehouseCheckResult:
        return WarehouseCheckResult(
            status=CheckStatus.PERMISSION_DENIED,
            ok=False,
            message="PERMISSION_DENIED: SP cannot access warehouse wh-123",
        )

    def _healthy_result(self) -> WarehouseCheckResult:
        return WarehouseCheckResult(
            status=CheckStatus.HEALTHY,
            ok=True,
            message="",
        )

    def test_denied_sets_granted_false(self):
        item = _build_warehouse_item(self._denied_result(), "wh-123", "sp-abc")
        assert item["granted"] is False

    def test_denied_includes_fix_sql(self):
        item = _build_warehouse_item(self._denied_result(), "wh-123", "sp-abc")
        assert "fix_sql" in item
        assert "GRANT CAN_USE" in item["fix_sql"]
        assert "wh-123" in item["fix_sql"]
        assert "sp-abc" in item["fix_sql"]

    def test_denied_includes_error_message(self):
        item = _build_warehouse_item(self._denied_result(), "wh-123", "sp-abc")
        assert "error" in item
        assert "PERMISSION_DENIED" in item["error"]

    def test_healthy_has_no_fix_sql(self):
        item = _build_warehouse_item(self._healthy_result(), "wh-123", "sp-abc")
        assert item["granted"] is True
        assert "fix_sql" not in item
        assert "error" not in item

    def test_denied_no_warehouse_id_omits_fix_sql(self):
        # When warehouse_id is empty, we can't generate fix_sql
        item = _build_warehouse_item(self._denied_result(), "", "sp-abc")
        assert item["granted"] is False
        assert "fix_sql" not in item


# ---------------------------------------------------------------------------
# _build_fix_sql — SQL structure for table grants
# ---------------------------------------------------------------------------

class TestBuildFixSql:
    def test_contains_grant_and_select(self):
        sql = _build_fix_sql("system.billing.usage", "sp-test-123")
        assert "GRANT" in sql
        assert "SELECT" in sql.upper()

    def test_contains_sp_client_id(self):
        sql = _build_fix_sql("system.query.history", "my-sp-id")
        assert "my-sp-id" in sql

    def test_contains_table_schema(self):
        sql = _build_fix_sql("system.billing.usage", "sp-abc")
        # Should grant schema-level access before table-level
        assert "system.billing" in sql

    def test_empty_sp_returns_empty_string(self):
        # _build_fix_sql returns "" when sp_client_id is empty — can't target a grant without a principal
        sql = _build_fix_sql("system.billing.usage", "")
        assert sql == ""

    def test_backtick_quoting_for_sp_name(self):
        sql = _build_fix_sql("system.billing.usage", "sp-with-hyphens")
        # SP names with hyphens must be backtick-quoted in Databricks SQL
        assert "`sp-with-hyphens`" in sql


# ---------------------------------------------------------------------------
# _check_readiness_sync — core vs. enhanced routing
# ---------------------------------------------------------------------------

class TestCheckReadinessSyncRouting:
    def test_core_tables_classified_as_core(self, monkeypatch):
        """system.billing.usage and system.billing.list_prices must be in 'core'."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-routing")
        monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-1")

        healthy_wh = WarehouseCheckResult(
            status=CheckStatus.HEALTHY, ok=True, message="",
            warehouse_id="wh-1", source="app_resource",
        )

        with (
            patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-1")),
            patch.object(setup_mod, "check_warehouse_readiness", return_value=healthy_wh),
            patch.object(setup_mod, "_check_table_as_sp", return_value=(True, "")),
        ):
            result = setup_mod._check_readiness_sync(bypass_cache=True)

        core_tables = {c["table"] for c in result["core"]}
        assert "system.billing.usage" in core_tables
        assert "system.billing.list_prices" in core_tables

    def test_enhanced_tables_classified_as_enhanced(self, monkeypatch):
        """system.query.history must be in 'enhanced', not 'core'."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-routing")
        monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-1")

        healthy_wh = WarehouseCheckResult(
            status=CheckStatus.HEALTHY, ok=True, message="",
            warehouse_id="wh-1", source="app_resource",
        )

        with (
            patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-1")),
            patch.object(setup_mod, "check_warehouse_readiness", return_value=healthy_wh),
            patch.object(setup_mod, "_check_table_as_sp", return_value=(True, "")),
        ):
            result = setup_mod._check_readiness_sync(bypass_cache=True)

        enhanced_tables = {c["table"] for c in result["enhanced"]}
        core_tables = {c["table"] for c in result["core"]}
        assert "system.query.history" in enhanced_tables
        assert "system.query.history" not in core_tables

    def test_denied_core_table_gets_fix_sql(self, monkeypatch):
        """A denied core table must have a fix_sql field in the response."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-fix-sql")

        healthy_wh = WarehouseCheckResult(
            status=CheckStatus.HEALTHY, ok=True, message="",
            warehouse_id="wh-1", source="app_resource",
        )

        def denied_check(table):
            if table == "system.billing.usage":
                return (False, "PERMISSION_DENIED")
            return (True, "")

        with (
            patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-1")),
            patch.object(setup_mod, "check_warehouse_readiness", return_value=healthy_wh),
            patch.object(setup_mod, "_check_table_as_sp", side_effect=denied_check),
        ):
            result = setup_mod._check_readiness_sync(bypass_cache=True)

        billing_usage = next(c for c in result["core"] if c["table"] == "system.billing.usage")
        assert billing_usage["granted"] is False
        assert "fix_sql" in billing_usage
        assert "sp-fix-sql" in billing_usage["fix_sql"]

    def test_overall_not_ready_when_all_core_denied(self, monkeypatch):
        """When all core tables and warehouse are denied, overall must be 'not_ready'."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-all-denied")

        denied_wh = WarehouseCheckResult(
            status=CheckStatus.PERMISSION_DENIED, ok=False,
            message="No access to warehouse",
        )

        with (
            patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-1")),
            patch.object(setup_mod, "check_warehouse_readiness", return_value=denied_wh),
            patch.object(setup_mod, "_check_table_as_sp", return_value=(False, "PERMISSION_DENIED")),
        ):
            result = setup_mod._check_readiness_sync(bypass_cache=True)

        assert result["overall"] == "not_ready"

    def test_overall_core_ready_when_core_passes_enhanced_denied(self, monkeypatch):
        """When core tables + warehouse pass but enhanced denied, overall = 'core_ready'."""
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-core-ready")

        healthy_wh = WarehouseCheckResult(
            status=CheckStatus.HEALTHY, ok=True, message="",
            warehouse_id="wh-1", source="app_resource",
        )

        core_tables = {"system.billing.usage", "system.billing.list_prices"}

        def selective_check(table):
            if table in core_tables:
                return (True, "")
            return (False, "PERMISSION_DENIED")

        with (
            patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-1")),
            patch.object(setup_mod, "check_warehouse_readiness", return_value=healthy_wh),
            patch.object(setup_mod, "_check_table_as_sp", side_effect=selective_check),
        ):
            result = setup_mod._check_readiness_sync(bypass_cache=True)

        assert result["overall"] == "core_ready"
