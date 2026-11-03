from datetime import date

from backend.routers import apps

HOST = "1111111111111111"
SPORTS = "2222222222222222"
END = "2026-11-01"

REGISTRY = {
    "host-app-id": {
        "name": "agent-0",
        "url": f"https://agent-0-{HOST}.aws.databricksapps.com",
        "description": "",
    },
    "host-idle-id": {
        "name": "hello-world",
        "url": f"https://hello-world-{HOST}.aws.databricksapps.com",
        "description": "",
    },
}


def _row(app_id, spend, last):
    return {
        "app_id": app_id,
        "total_dbus": spend,
        "total_spend": spend,
        "workspace_count": 1,
        "days_active": 3,
        "last_usage_date": last,
    }


def _process(raw, workspaces, *, host_in_scope, names=None):
    return apps._process_apps(
        raw,
        False,
        END,
        REGISTRY,
        None,
        app_workspaces=workspaces,
        host_ws_id=HOST,
        host_in_scope=host_in_scope,
        billing_names=names or {},
    )


def test_host_workspace_id_is_read_off_registry_app_urls(monkeypatch):
    monkeypatch.setattr(apps, "_host_ws_id_cache", None)
    assert apps._host_workspace_id(REGISTRY) == HOST


def test_apps_in_another_workspace_are_live_apps_named_from_billing():
    raw = [
        _row("sports-live", 400.0, date(2026, 10, 31)),
        _row("sports-stopped", 14.0, date(2026, 10, 10)),
    ]
    out = _process(
        raw,
        {"sports-live": [SPORTS], "sports-stopped": [SPORTS]},
        host_in_scope=False,
        names={"sports-live": "cost-obs-sports-prod", "sports-stopped": "eventforge"},
    )
    by_name = {a["app_name"]: a for a in out["apps"]}
    assert set(by_name) == {"cost-obs-sports-prod", "eventforge"}
    assert by_name["cost-obs-sports-prod"]["status"] == "active"
    assert by_name["cost-obs-sports-prod"]["deployed_elsewhere"] is True
    assert by_name["cost-obs-sports-prod"]["workspace_names"] == [SPORTS]
    assert by_name["eventforge"]["status"] == "inactive"
    assert out["active_count"] == 1
    assert out["inactive_count"] == 1
    assert out["unregistered_summary"]["count"] == 0


def test_idle_host_apps_are_hidden_when_the_host_workspace_is_out_of_scope():
    out = _process([], {}, host_in_scope=False)
    assert out["apps"] == []


def test_idle_host_apps_show_under_the_host_workspace_when_in_scope():
    out = _process(
        [_row("host-app-id", 5.0, date(2026, 10, 30))], {"host-app-id": [HOST]}, host_in_scope=True
    )
    by_name = {a["app_name"]: a for a in out["apps"]}
    assert set(by_name) == {"agent-0", "hello-world"}
    assert by_name["hello-world"]["total_spend"] == 0
    assert by_name["hello-world"]["workspace_names"] == [HOST]


def test_unregistered_app_billed_only_to_the_host_is_historical_and_counted_once():
    raw = [_row("deleted-id", 14.0, date(2026, 10, 5))]
    out = _process(
        raw, {"deleted-id": [HOST]}, host_in_scope=True, names={"deleted-id": "old-demo"}
    )
    deleted = next(a for a in out["apps"] if a["app_id"] == "deleted-id")
    assert deleted["historical"] is True
    assert deleted["app_name"] == "old-demo"
    assert out["unregistered_summary"]["count"] == 1
    # Two idle registry apps; the deleted app is counted as historical, not inactive too.
    assert out["inactive_count"] == 2


def _reset_billing_names(monkeypatch):
    monkeypatch.setattr(apps, "_billing_names", {})
    monkeypatch.setattr(apps, "_billing_names_time", 0)
    monkeypatch.setattr(apps, "_billing_names_refreshing", False)


def test_lakebase_serving_fetches_billing_names_in_the_background(monkeypatch):
    _reset_billing_names(monkeypatch)
    calls = []

    def fake_query(sql, params):
        calls.append(params)
        return [{"app_id": "sports-live", "app_name": "cost-obs-sports-prod"}]

    started = []

    class FakeThread:
        def __init__(self, target, **_kw):
            self.target = target

        def start(self):
            started.append(self)

    monkeypatch.setattr(apps, "execute_query", fake_query)
    monkeypatch.setattr(apps.threading, "Thread", FakeThread)

    assert apps._billing_app_names(wait=False) == {}
    assert len(started) == 1 and calls == []
    assert apps._billing_app_names(wait=False) == {}
    assert len(started) == 1  # one refresh at a time

    started[0].target()
    assert apps._billing_app_names(wait=False) == {"sports-live": "cost-obs-sports-prod"}
    assert len(calls) == 1


def test_failed_billing_name_lookup_backs_off(monkeypatch):
    _reset_billing_names(monkeypatch)

    def boom(sql, params):
        raise RuntimeError("warehouse down")

    monkeypatch.setattr(apps, "execute_query", boom)
    assert apps._billing_app_names(wait=True) == {}
    retry_in = apps._billing_names_time + apps.BILLING_NAME_TTL - apps.time.time()
    assert 0 < retry_in <= apps.BILLING_NAME_RETRY_SECONDS


def test_cached_bundle_gets_names_for_apps_still_shown_by_id():
    bundle = {
        "apps": {
            "apps": [
                {"app_id": "sports-live", "app_name": "sports-live"},
                {"app_id": "host-app-id", "app_name": "agent-0"},
            ]
        },
        "connected_artifacts": [{"app_id": "sports-live", "app_name": "sports-live"}],
    }
    out = apps._apply_billing_names(
        bundle, {"sports-live": "cost-obs-sports-prod", "host-app-id": "x"}
    )
    assert [a["app_name"] for a in out["apps"]["apps"]] == ["cost-obs-sports-prod", "agent-0"]
    assert out["connected_artifacts"][0]["app_name"] == "cost-obs-sports-prod"
    assert bundle["apps"]["apps"][0]["app_name"] == "sports-live"  # cached copy untouched
