from backend import workspace_filter as wf


def _pool(monkeypatch, ids):
    monkeypatch.setattr(wf, "get_configured_workspace_ids", lambda: list(ids))


def test_requested_workspaces_are_narrowed_to_the_saved_pool(monkeypatch):
    _pool(monkeypatch, ["111", "222"])
    clause = wf.build_ws_filter_clause(id_list=["222", "999"])
    assert clause == "AND CAST(u.workspace_id AS STRING) IN ('222')"


def test_requesting_only_out_of_pool_workspaces_returns_nothing(monkeypatch):
    _pool(monkeypatch, ["111"])
    assert wf.build_ws_filter_clause(id_list=["999"]) == "AND 1 = 0"
    assert wf.build_ws_filter_clause(single_id="999") == "AND 1 = 0"


def test_unsafe_or_empty_requests_fall_back_to_the_pool_not_everything(monkeypatch):
    _pool(monkeypatch, ["111"])
    expected = "AND CAST(u.workspace_id AS STRING) IN ('111')"
    assert wf.build_ws_filter_clause(id_list=["'; DROP TABLE x; --"]) == expected
    assert wf.build_ws_filter_clause(id_list=[]) == expected


def test_without_a_pool_requests_pick_any_workspace(monkeypatch):
    _pool(monkeypatch, [])
    assert (
        wf.build_ws_filter_clause(id_list=["999"])
        == "AND CAST(u.workspace_id AS STRING) IN ('999')"
    )
    assert wf.build_ws_filter_clause(id_list=[]) == ""


def test_ai_gateway_requests_cannot_widen_the_saved_pool(monkeypatch):
    from backend.routers import ai_gateway

    _pool(monkeypatch, ["111"])
    sql, params = ai_gateway._apply_workspace_filter("WHERE x {workspace_filter}", ["111", "999"])
    assert params == {"ws_0": "111"}
    sql, params = ai_gateway._apply_workspace_filter("WHERE x {workspace_filter}", ["999"])
    assert "AND 1 = 0" in sql and params == {}


def test_kpi_total_workspaces_counts_only_the_scope(monkeypatch):
    _pool(monkeypatch, ["111", "222"])
    assert wf.scoped_workspace_ids(None) == ["111", "222"]
    assert wf.scoped_workspace_ids(["222", "999"]) == ["222"]
    _pool(monkeypatch, [])
    assert wf.scoped_workspace_ids(None) is None


def test_filter_is_applied_to_every_branch_of_a_union_query():
    sql = "SELECT 1 FROM a u WHERE x AND u.usage_quantity > 0 UNION ALL SELECT 2 FROM b u WHERE y AND u.usage_quantity > 0"
    out = wf.inject_ws_filter(sql, "AND CAST(u.workspace_id AS STRING) IN ('111')")
    assert out.count("IN ('111')") == 2
