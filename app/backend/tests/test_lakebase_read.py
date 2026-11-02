"""Unit tests for the pure logic in backend.lakebase_read (Phase B reader cutover).

Covers the dialect translation (Databricks ws-filter → Postgres), the mode flag, and the
shape of the hand-written PG templates (sentinel NULLIF, psycopg params, no backticks).
The registry + I/O need the SDK/live Lakebase and are exercised in the shadow deploy.
"""
import os

from backend.lakebase_read import (
    KPI_TREND_PG,
    PG_APPS_SUMMARY,
    PG_AVG_DAILY_WORKSPACES,
    PG_BILLING_BY_WORKSPACE,
    PG_BILLING_SUMMARY,
    PG_PLATFORM_KPIS,
    PG_SQL_TOOL_ATTRIBUTION,
    PG_TAG_STATS,
    lakebase_read_mode,
    pg_ws_filter,
)


def test_mode_defaults_on(monkeypatch=None):
    os.environ.pop("COST_OBS_LAKEBASE_READ", None)
    assert lakebase_read_mode() == "on"


def test_mode_reads_env():
    for val, exp in [("shadow", "shadow"), ("on", "on"), ("OFF", "off"), ("bogus", "on"), ("", "on")]:
        os.environ["COST_OBS_LAKEBASE_READ"] = val
        assert lakebase_read_mode() == exp
    os.environ.pop("COST_OBS_LAKEBASE_READ", None)


def test_pg_ws_filter_drops_cast():
    assert pg_ws_filter("AND CAST(workspace_id AS STRING) IN ('1','2')") == "AND workspace_id IN ('1','2')"


def test_pg_ws_filter_handles_alias_and_case():
    assert pg_ws_filter("AND cast(u.workspace_id as string) IN ('1')") == "AND workspace_id IN ('1')"


def test_pg_ws_filter_empty():
    assert pg_ws_filter("") == ""


def test_pg_ws_filter_strips_source_label():
    raw = "AND CAST(workspace_id AS STRING) IN ('1') AND source_label IN ('primary','share')"
    out = pg_ws_filter(raw)
    assert "source_label" not in out
    assert "workspace_id IN ('1')" in out


def test_summary_template_is_postgres():
    sql = PG_BILLING_SUMMARY.format(ws_filter="AND workspace_id IN ('1')")
    assert "`" not in sql                                   # no Databricks backticks
    assert "%(start_date)s" in sql and "%(end_date)s" in sql  # psycopg named params
    assert "FROM daily_usage_summary" in sql                # unqualified (search_path)
    # account-level sentinel mapped back to NULL so COUNT(DISTINCT) matches Delta
    assert "COUNT(DISTINCT NULLIF(workspace_id, '__account__'))" in sql


def test_by_workspace_maps_sentinel_back_to_null():
    sql = PG_BILLING_BY_WORKSPACE.format(ws_filter="")
    # the served workspace_id must be NULL for account-level rows, not the sentinel
    assert "NULLIF(workspace_id, '__account__') AS workspace_id" in sql
    assert "GROUP BY NULLIF(workspace_id, '__account__')" in sql


def test_dual_read_skips_lakebase_when_source_labels_set(monkeypatch):
    from backend.db import reset_source_labels, set_source_labels
    from backend.lakebase_read import dual_read

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)

    def _boom(*_a, **_k):
        raise AssertionError("Lakebase must not be queried when source_labels are set")

    monkeypatch.setattr("backend.db.lakebase_query", _boom)
    tok = set_source_labels(["extra-share"])
    try:
        delta = [{"date": "2026-01-01", "value": 1}]
        assert dual_read("kpi_trend_total_spend", KPI_TREND_PG["total_spend"],
                         {"start_date": "2026-01-01", "end_date": "2026-01-31"},
                         "", delta, ["date"]) is delta
    finally:
        reset_source_labels(tok)


def test_warehouse_view_fallback_off_when_lakebase_on(monkeypatch):
    from backend.lakebase_read import warehouse_view_fallback

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.source_labels_active", lambda: False)
    assert warehouse_view_fallback() is False


def test_warehouse_view_fallback_on_when_mode_off(monkeypatch):
    from backend.lakebase_read import warehouse_view_fallback

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "off")
    assert warehouse_view_fallback() is True


def test_dual_read_on_does_not_touch_warehouse(monkeypatch):
    from backend.lakebase_read import dual_read

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.source_labels_active", lambda: False)
    lb_rows = [{"product": "SQL", "total_spend": 3}]
    monkeypatch.setattr("backend.db.lakebase_query", lambda *_a, **_k: lb_rows)

    def _boom():
        raise AssertionError("warehouse gold must not run when Postgres serving is on")

    out = dual_read(
        "sku_breakdown",
        "SELECT 1",
        {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        "",
        fetch_delta=_boom,
    )
    assert out == lb_rows


def test_dual_read_on_pg_error_uses_warehouse_gold(monkeypatch):
    from backend.lakebase_read import dual_read

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.source_labels_active", lambda: False)

    def _boom_pg(*_a, **_k):
        raise RuntimeError("pg down")

    monkeypatch.setattr("backend.db.lakebase_query", _boom_pg)
    gold = [{"product": "SQL", "total_spend": 4}]
    out = dual_read(
        "sku_breakdown",
        "SELECT 1",
        {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        "",
        fetch_delta=lambda: gold,
    )
    assert out == gold


def test_dual_read_on_both_fail_returns_empty(monkeypatch):
    from backend.lakebase_read import dual_read

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.source_labels_active", lambda: False)
    monkeypatch.setattr("backend.db.lakebase_query", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("pg down")))
    out = dual_read(
        "sku_breakdown",
        "SELECT 1",
        {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        "",
        fetch_delta=lambda: (_ for _ in ()).throw(RuntimeError("gold missing")),
    )
    assert out == []


def test_dual_read_on_empty_postgres_uses_warehouse_gold(monkeypatch):
    from backend.lakebase_read import dual_read

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.source_labels_active", lambda: False)
    monkeypatch.setattr("backend.db.lakebase_query", lambda *_a, **_k: [])
    gold = [{"product": "SQL", "total_spend": 8}]
    out = dual_read(
        "sku_breakdown",
        "SELECT 1",
        {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        "",
        fetch_delta=lambda: gold,
    )
    assert out == gold


def test_dual_read_on_empty_delta_queries_lakebase(monkeypatch):
    from backend.lakebase_read import dual_read

    monkeypatch.setenv("COST_OBS_LAKEBASE_READ", "on")
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.source_labels_active", lambda: False)
    lb_rows = [{"date": "2026-01-01", "value": 9}]
    monkeypatch.setattr("backend.db.lakebase_query", lambda *_a, **_k: lb_rows)
    out = dual_read("kpi_trend_total_spend", KPI_TREND_PG["total_spend"],
                    {"start_date": "2026-01-01", "end_date": "2026-01-31"},
                    "", [], ["date"])
    assert out == lb_rows


def test_genie_templates_exclude_pk_sentinel():
    from backend.lakebase_read import PG_GENIE_BY_WORKSPACE, PG_GENIE_TIMESERIES

    for sql in (PG_GENIE_BY_WORKSPACE, PG_GENIE_TIMESERIES):
        formatted = sql.format(ws_filter="")
        assert "genie_surface IS NOT NULL" in formatted
        assert "NOT IN ('__na__', '')" in formatted


def test_aiml_gold_sql_covers_dashboard_queries():
    from backend.materialized_views_extra import AIML_GOLD_SQL

    for key in (
        "aiml_summary", "aiml_providers", "aiml_endpoints",
        "aiml_categories", "aiml_timeseries", "aiml_sku_catalog",
    ):
        sql = AIML_GOLD_SQL[key].format(catalog="c", schema="s", ws_filter="")
        assert "system." not in sql
        assert "`c`.`s`.`daily_" in sql
        assert ":start_date" in sql and ":end_date" in sql


def test_new_gold_pg_templates_are_postgres():
    from backend.lakebase_read import (
        PG_AIML_CATEGORIES,
        PG_AIML_ENDPOINTS,
        PG_AIML_PROVIDERS,
        PG_AIML_TIMESERIES,
        PG_CLOUD_BY_CHARGE_TYPE,
        PG_CLOUD_SUMMARY,
        PG_CLOUD_TIMESERIES,
        PG_COST_BY_TAG,
        PG_DBSQL_BY_USER,
        PG_DBSQL_BY_WAREHOUSE,
        PG_DBSQL_TIMESERIES,
        PG_DBSQL_TOP,
        PG_TAG_COVERAGE_TIMESERIES,
    )
    for sql in (
        PG_AIML_PROVIDERS, PG_AIML_CATEGORIES, PG_AIML_TIMESERIES, PG_AIML_ENDPOINTS,
        PG_DBSQL_BY_USER, PG_DBSQL_BY_WAREHOUSE, PG_DBSQL_TIMESERIES, PG_DBSQL_TOP,
        PG_TAG_COVERAGE_TIMESERIES, PG_COST_BY_TAG, PG_CLOUD_SUMMARY,
        PG_CLOUD_BY_CHARGE_TYPE, PG_CLOUD_TIMESERIES,
    ):
        formatted = sql.format(ws_filter="") if "{ws_filter}" in sql else sql
        assert "`" not in formatted
        assert "system." not in formatted
        assert "%(start_date)s" in formatted and "%(end_date)s" in formatted


def test_sql_tool_and_kpis_templates_are_postgres():
    for sql in (
        PG_SQL_TOOL_ATTRIBUTION.format(ws_filter=""),
        PG_PLATFORM_KPIS.format(ws_filter=""),
        PG_TAG_STATS.format(ws_filter=""),
        PG_AVG_DAILY_WORKSPACES.format(ws_filter=""),
        PG_APPS_SUMMARY.format(ws_filter=""),
        KPI_TREND_PG["total_spend"].format(ws_filter=""),
    ):
        assert "`" not in sql
        assert "%(start_date)s" in sql and "%(end_date)s" in sql
        assert "system." not in sql


def test_remaining_gold_tables_registered():
    from backend.materialized_views_extra import EXTRA_CREATE, EXTRA_MERGE, EXTRA_TABLE_REFRESH

    for name in (
        "daily_user_spend",
        "daily_untagged_objects",
        "daily_aiml_models",
        "daily_aiml_ml_clusters",
        "daily_aiml_agents",
        "daily_object_spend",
        "daily_infra_cluster_spend",
        "daily_dbsql_warehouse_type",
        "warehouse_optimizer_findings",
        "daily_warehouse_idle",
        "daily_kpi_facts",
        "daily_query_users",
        "daily_apps_identities",
        "use_case_query_objects",
        "daily_azure_actuals",
        "daily_gcp_actuals",
    ):
        assert name in EXTRA_CREATE
        assert name in EXTRA_TABLE_REFRESH
        assert "{catalog}" in EXTRA_CREATE[name]
    assert "daily_user_spend" in EXTRA_MERGE
    assert "daily_warehouse_idle" in EXTRA_MERGE
    assert "warehouse_optimizer_findings" not in EXTRA_MERGE  # always full rebuild
    assert "use_case_query_objects" not in EXTRA_MERGE


def test_remaining_pg_templates_are_postgres():
    from backend.lakebase_read import (
        PG_AI_GATEWAY_ROWS,
        PG_AIML_MODELS,
        PG_INFRA_CLUSTERS,
        PG_OBJECT_SPEND,
        PG_UNTAGGED,
        PG_USERS_SUMMARY,
        PG_USERS_TOP,
        PG_WH_META,
    )
    for sql in (PG_USERS_SUMMARY, PG_USERS_TOP, PG_AIML_MODELS, PG_INFRA_CLUSTERS, PG_WH_META, PG_OBJECT_SPEND, PG_AI_GATEWAY_ROWS):
        formatted = sql.format(ws_filter="")
        assert "`" not in formatted
        assert "system." not in formatted
        assert "%(start_date)s" in formatted
    untagged = PG_UNTAGGED.format(ws_filter="", id_col="cluster_id", name_col="cluster_name")
    assert "FROM daily_untagged_objects" in untagged
    assert "`" not in untagged


def test_viewer_gold_pg_templates_are_postgres():
    from backend.lakebase_read import (
        PG_APPS_IDENTITIES,
        PG_AVAILABLE_QUERIES,
        PG_AZURE_SUMMARY,
        PG_GCP_SUMMARY,
        PG_KPI_BILLING,
        PG_STICKINESS,
        PG_WAREHOUSE_IDLE,
        PG_WORKSPACE_LIST,
        PLATFORM_KPI_TREND_PG,
    )
    for sql in (PG_WORKSPACE_LIST, PG_WAREHOUSE_IDLE, PG_KPI_BILLING, PG_STICKINESS, PG_APPS_IDENTITIES):
        formatted = sql.format(ws_filter="")
        assert "`" not in formatted
        assert "system." not in formatted
        assert "%(start_date)s" in formatted
    assert "daily_azure_actuals" in PG_AZURE_SUMMARY
    assert "daily_gcp_actuals" in PG_GCP_SUMMARY
    assert "use_case_query_objects" in PG_AVAILABLE_QUERIES
    assert "system." not in PLATFORM_KPI_TREND_PG["total_queries"].format(ws_filter="")


def test_kpi_trend_pg_covers_sparkline_kpis():
    from backend.lakebase_read import KPI_TREND_PG, PG_APPS_WORKSPACES, PG_AVAILABLE_TAGS
    for key in (
        "total_spend", "tagged_spend", "infra_cost", "aiml_spend",
        "apps_spend", "apps_avg_cost_per_app",
    ):
        sql = KPI_TREND_PG[key].format(ws_filter="")
        assert "`" not in sql
        assert "system." not in sql
        assert "%(start_date)s" in sql
    tags = PG_AVAILABLE_TAGS
    assert "daily_tag_summary" in tags
    assert "system." not in tags
    ws = PG_APPS_WORKSPACES.format(ws_filter="AND workspace_id IN ('1')")
    assert "daily_apps_summary" in ws
    assert "system." not in ws
    # Filter must sit in the inner scan so the join's workspace_id is not ambiguous.
    inner = ws.split("LEFT JOIN")[0]
    assert "AND workspace_id IN ('1')" in inner


def test_cluster_vm_cost_uses_instance_pricing_when_gold_has_no_dollars():
    from backend.routers.billing import _cluster_vm_cost
    row = {
        "driver_instance_type": "m5.xlarge",
        "worker_instance_type": "m5.xlarge",
        "total_dbu_hours": 10,
    }
    assert _cluster_vm_cost(row, "AWS") > 0
    assert _cluster_vm_cost({"estimated_aws_cost": 3, "total_dbu_hours": 10}, "AWS") == 3.0


def test_kpi_trend_response_empty_and_daily():
    from backend.routers.billing import _kpi_trend_response
    def identity(d):
        return d
    empty = _kpi_trend_response("total_spend", "daily", [], identity)
    assert empty["data_points"] == []
    out = _kpi_trend_response(
        "total_spend", "daily",
        [{"date": "2026-01-01", "value": 10}, {"date": "2026-01-02", "value": 20}],
        identity,
    )
    assert out["summary"]["trend"] == "increasing"
    assert out["data_points"][1]["value"] == 20.0


def test_families_from_cluster_rows_rolls_driver_and_worker():
    from backend.routers.billing import _families_from_cluster_rows

    rows = [
        {
            "cluster_id": "c1",
            "driver_instance_type": "m5d.xlarge",
            "worker_instance_type": "m5d.xlarge",
            "total_dbu_hours": 10,
            "days_active": 2,
        },
        {
            "cluster_id": "c2",
            "driver_instance_type": "r5d.2xlarge",
            "worker_instance_type": None,
            "total_dbu_hours": 4,
            "days_active": 1,
        },
    ]
    families = _families_from_cluster_rows(rows, "AWS")
    by_name = {f["instance_family"]: f for f in families}
    assert "m5d" in by_name
    assert by_name["m5d"]["total_dbu_hours"] == 20  # driver + worker
    assert by_name["r5d"]["total_dbu_hours"] == 4


def test_format_aws_timeseries_from_infra_gold():
    from backend.routers.billing import _format_aws_timeseries

    out = _format_aws_timeseries(
        [{"usage_date": "2026-01-01", "cloud": "AWS", "total_dbu_hours": 10}],
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
    )
    assert out["timeseries"][0]["AWS Cost"] == 5.0
    assert "Infrastructure" in out["instance_families"]
