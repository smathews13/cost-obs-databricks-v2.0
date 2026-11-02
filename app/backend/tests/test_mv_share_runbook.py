"""Regression coverage for the standalone gold/share publisher runbook."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend import materialized_views
from backend.routers import settings

ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = ROOT / "app" / "backend" / "assets" / "cost_obs_mv_share_publisher.py"
REPOSITORY_RUNBOOK = ROOT / "notebooks" / "cost_obs_mv_share_publisher.py"


class _Request:
    headers: dict[str, str] = {}

    def __init__(self, body: dict | None = None):
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


def test_runbook_is_generated_from_the_exact_runtime_gold_contract():
    rendered = RUNBOOK.read_text()
    canonical_sql = materialized_views.gold_share_create_sql()
    contract_hash = hashlib.sha256(
        json.dumps(
            canonical_sql,
            sort_keys=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert list(materialized_views.CORE_GOLD_CREATE) == [
        "daily_usage_summary",
        "daily_product_breakdown",
        "daily_workspace_breakdown",
        "sql_tool_attribution",
        "daily_query_stats",
        "dbsql_cost_per_query",
        "daily_tag_summary",
        "daily_apps_summary",
    ]
    for table_name in canonical_sql:
        assert f"`{{catalog}}`.`{{schema}}`.`{table_name}`" in rendered
    assert f"Contract: `{contract_hash}`" in rendered
    assert rendered.count("CREATE OR REPLACE TABLE") == len(canonical_sql)
    assert "CREATE SHARE IF NOT EXISTS" in rendered
    assert "SHOW ALL IN SHARE" in rendered
    assert "ADD TABLE" in rendered
    assert '# environment_version = "5"' in rendered
    assert '_dropdown("share_mode", "table", ["table", "schema"], "Share mode")' in rendered
    assert '_dropdown("allow_schema_migration", "no", ["no", "yes"],' in rendered
    assert "will not replace them automatically" in rendered
    assert "ALTER SHARE `{SHARE}` ADD SCHEMA `{CATALOG}`.`{SCHEMA}`" in rendered
    assert "ALTER SHARE `{SHARE}` REMOVE TABLE `{cat}`.`{sch}`.`{tbl}`" in rendered
    assert "SCHEMA ALREADY SHARED" in rendered
    assert '_widget("lookback_days", "180", "Billing/query lookback days")' in rendered
    for source_table in (
        "system.access.workspaces_latest",
        "system.billing.list_prices",
        "system.billing.usage",
        "system.query.history",
    ):
        assert source_table in rendered
    assert "SKIPPED_PERMISSION" in rendered
    assert "SKIPPED_OPTIONAL" in rendered
    assert "Continuing with partial capability" in rendered
    assert "Source-table permission preflight failed" not in rendered
    assert "GRANT SELECT ON TABLE" in rendered
    assert "GRANT SELECT ON SCHEMA `<recipient_catalog>`.`<shared_schema>`" in rendered
    assert "Lakebase is not part of this share" in rendered
    assert "Lakebase was not published" in rendered
    assert "imports compatible rows into Lakebase" in rendered
    assert "Unfiltered dashboard reads stay on" in rendered
    assert "source-label filter reads warehouse unified views" in rendered
    assert "Required gold tables were not built" in rendered
    assert "Schema share would publish invoice gold tables" in rendered
    assert "not copied into Lakebase" in rendered
    assert "Lakebase / Postgres copies" in rendered
    assert "Connected cloud invoice actuals are intentionally out of scope" in rendered
    assert "daily_cloud_actuals" not in canonical_sql
    assert "daily_azure_actuals" not in canonical_sql
    assert "daily_gcp_actuals" not in canonical_sql
    assert "{ai_gateway_table}" in rendered
    assert "cmeg" not in rendered.lower()
    # /notebooks is internal-only and absent from the public mirror.
    if REPOSITORY_RUNBOOK.exists():
        assert REPOSITORY_RUNBOOK.read_text() == rendered
    compile(rendered, str(RUNBOOK), "exec")


def test_share_sql_keeps_ai_gateway_optional_and_excludes_invoice_actuals():
    sql = materialized_views.gold_share_create_sql()
    assert "{ai_gateway_table}" in sql["daily_ai_gateway"]
    assert "daily_cloud_actuals" not in sql
    assert "daily_azure_actuals" not in sql
    assert "daily_gcp_actuals" not in sql
    assert "daily_ai_gateway" in materialized_views.OPTIONAL_SHARE_TABLES
    assert "use_case_dashboard_objects" in materialized_views.OPTIONAL_SHARE_TABLES


def test_settings_download_serves_the_generated_notebook_to_admins():
    request = SimpleNamespace(headers={"X-Forwarded-Email": "admin@example.com"})
    with patch.object(settings, "_require_admin", return_value="admin@example.com"):
        response = asyncio.run(settings.download_materialized_view_runbook(request))

    assert Path(response.path) == RUNBOOK
    assert response.media_type == "text/x-python"
    assert "cost_obs_mv_share_publisher.py" in response.headers["content-disposition"]


def test_settings_download_honors_the_runbook_kill_switch():
    request = SimpleNamespace(headers={"X-Forwarded-Email": "admin@example.com"})
    with (
        patch.object(settings, "_require_admin", return_value="admin@example.com"),
        patch.object(settings, "get_app_settings", return_value={"enable_mv_share_runbook": False}),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings.download_materialized_view_runbook(request))
    assert exc.value.status_code == 404


def test_settings_download_rejects_non_admins():
    request = SimpleNamespace(headers={"X-Forwarded-Email": "user@example.com"})
    with patch.object(
        settings,
        "_require_admin",
        side_effect=HTTPException(status_code=403, detail="Admin role required"),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings.download_materialized_view_runbook(request))
    assert exc.value.status_code == 403


def test_artifact_settings_default_on_and_round_trip_through_unified_settings():
    assert settings._APP_SETTINGS_DEFAULTS["enable_architecture_view"] is True
    assert settings._APP_SETTINGS_DEFAULTS["enable_mv_share_runbook"] is True

    with (
        patch.object(
            settings, "get_app_settings", return_value=dict(settings._APP_SETTINGS_DEFAULTS)
        ),
        patch.object(settings, "_load_alert_thresholds", return_value={}),
        patch.object(settings, "_load_pricing_settings", return_value={}),
        patch.object(settings, "load_schedule_settings", return_value={}),
        patch.object(settings, "_webhook_masked", return_value={}),
        patch.object(settings, "_capabilities", return_value={}),
    ):
        snapshot = settings._settings_snapshot(_Request())

    assert snapshot["experimental"]["enable_architecture_view"] is True
    assert snapshot["experimental"]["enable_mv_share_runbook"] is True


def test_artifact_settings_can_be_disabled_by_an_admin():
    request = _Request(
        {
            "experimental": {
                "enable_architecture_view": False,
                "enable_mv_share_runbook": False,
            },
        }
    )
    with (
        patch.object(settings, "_require_admin"),
        patch.object(settings, "save_app_settings") as save,
        patch.object(settings, "_settings_snapshot", return_value={"saved": True}),
    ):
        result = asyncio.run(settings.put_unified_settings(request))

    save.assert_called_once_with(
        {
            "enable_architecture_view": False,
            "enable_mv_share_runbook": False,
        }
    )
    assert result == {"saved": True}
