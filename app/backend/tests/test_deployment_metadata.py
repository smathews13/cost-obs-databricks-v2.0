"""Deployment provenance exposed to the top-rail badge."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from backend import db
from backend.routers import health

_CLEAR_ENV = (
    "DATABRICKS_APP_NAME",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_APP_DEPLOYED_AT",
    "DATABRICKS_APP_DEPLOYER",
    "DATABRICKS_APP_COMMIT",
    "GIT_COMMIT",
    "COST_OBS_DEPLOYED_AT",
    "COST_OBS_DEPLOYER",
    "COST_OBS_COMMIT_SHA",
)


@pytest.fixture(autouse=True)
def reset_deployment_metadata_state(monkeypatch):
    for name in _CLEAR_ENV:
        monkeypatch.delenv(name, raising=False)
    with health._deployment_metadata_lock:
        health._deployment_metadata_cache.clear()
        health._deployment_metadata_inflight.clear()
    yield
    with health._deployment_metadata_lock:
        health._deployment_metadata_cache.clear()
        health._deployment_metadata_inflight.clear()


def test_extracts_authoritative_active_deployment_metadata():
    app = SimpleNamespace(
        active_deployment=SimpleNamespace(
            create_time="2026-08-30T21:29:46Z",
            creator="deployer@example.com",
            git_source=SimpleNamespace(
                resolved_commit="ed86035f1234567890",
                commit="older-value",
            ),
        ),
        git_source=None,
        default_git_source=None,
    )

    assert health._deployment_metadata_from_app(app) == {
        "deployed_at": "2026-08-30T21:29:46Z",
        "deployer": "deployer@example.com",
        "commit_sha": "ed86035f1234567890",
        "available": True,
        "source": "databricks_apps_api",
    }


def test_missing_deployment_metadata_is_explicitly_unavailable():
    assert health._deployment_metadata_from_app(SimpleNamespace()) == {
        "deployed_at": None,
        "deployer": None,
        "commit_sha": None,
        "available": False,
        "source": "databricks_apps_api",
    }
    assert health._deployment_metadata_from_env() == {
        "deployed_at": None,
        "deployer": None,
        "commit_sha": None,
        "available": False,
        "source": "release_environment",
    }
    assert health._deployment_metadata_from_databricks_app_env() == {
        "deployed_at": None,
        "deployer": None,
        "commit_sha": None,
        "available": False,
        "source": "databricks_app_environment",
    }


def test_deployment_metadata_cache_is_bounded_and_short_lived():
    assert health._deployment_metadata_cache.maxsize == 8
    assert health._deployment_metadata_cache.ttl == health.cache_ttls.DEPLOYMENT_METADATA


def test_release_environment_is_a_field_by_field_fallback(monkeypatch):
    monkeypatch.setenv("COST_OBS_DEPLOYED_AT", "2026-08-30T21:29:46Z")
    monkeypatch.setenv("COST_OBS_DEPLOYER", "")
    monkeypatch.setenv("COST_OBS_COMMIT_SHA", "ed86035f")

    assert health._deployment_metadata_from_env() == {
        "deployed_at": "2026-08-30T21:29:46Z",
        "deployer": None,
        "commit_sha": "ed86035f",
        "available": True,
        "source": "release_environment",
    }


def test_databricks_app_env_is_an_extra_merge_input(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_DEPLOYED_AT", "2026-09-01T00:00:00Z")
    monkeypatch.setenv("DATABRICKS_APP_DEPLOYER", "app-deployer@example.com")
    monkeypatch.setenv("DATABRICKS_APP_COMMIT", "app-commit")

    assert health._deployment_metadata_from_databricks_app_env() == {
        "deployed_at": "2026-09-01T00:00:00Z",
        "deployer": "app-deployer@example.com",
        "commit_sha": "app-commit",
        "available": True,
        "source": "databricks_app_environment",
    }


def test_endpoint_reads_current_app_from_databricks_runtime(monkeypatch):
    app = SimpleNamespace(
        active_deployment=SimpleNamespace(
            create_time="2026-08-30T21:29:46Z",
            creator="deployer@example.com",
            git_source=SimpleNamespace(resolved_commit="ed86035f1234567890"),
        ),
    )
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs")
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(apps=SimpleNamespace(get=lambda name: app)),
    )

    result = asyncio.run(health.deployment_metadata())

    assert result["source"] == "databricks_apps_api"
    assert result["verified"] is True
    assert result["deployed_at"] == "2026-08-30T21:29:46Z"
    assert result["deployer"] == "deployer@example.com"
    assert result["commit_sha"] == "ed86035f1234567890"


def test_gcp_app_detail_fallback_uses_observed_cli_shape_without_active_deployment(
    monkeypatch,
):
    """GCP app get can omit active_deployment while retaining app-level provenance."""
    observed_gcp_app = SimpleNamespace(
        name="cost-obs-gcp",
        update_time="2026-08-31T16:42:03Z",
        updater="gcp-deployer@example.com",
        creator="gcp-creator@example.com",
        active_deployment=None,
        git_source=None,
        default_git_source=None,
        git_repository=SimpleNamespace(
            url="https://github.com/example/cost-obs",
            commit="gcp-source-commit",
        ),
    )
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs-gcp")
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                host="https://1234567890123456.7.gcp.databricks.com"
            ),
            apps=SimpleNamespace(get=lambda _name: observed_gcp_app),
        ),
    )

    result = asyncio.run(health.deployment_metadata())

    assert result == {
        "deployed_at": "2026-08-31T16:42:03Z",
        "deployer": "gcp-deployer@example.com",
        "commit_sha": "gcp-source-commit",
        "available": True,
        "source": "databricks_apps_api",
        "verified": True,
    }


@pytest.mark.parametrize(
    "workspace_host",
    (
        "https://dbc-example.cloud.databricks.com",
        "https://adb-1234567890123456.7.azuredatabricks.net",
        "https://1234567890123456.7.gcp.databricks.com",
    ),
)
def test_endpoint_uses_active_deployment_api_on_all_clouds(
    monkeypatch,
    workspace_host,
):
    calls: list[tuple[str, ...]] = []
    active_summary = SimpleNamespace(
        deployment_id="deployment-active",
        create_time=None,
        creator=None,
        git_source=None,
    )
    apps_api = SimpleNamespace(
        get=lambda name: (
            calls.append(("get", name))
            or SimpleNamespace(active_deployment=active_summary)
        ),
        get_deployment=lambda app_name, deployment_id: (
            calls.append(("get_deployment", app_name, deployment_id))
            or SimpleNamespace(
                create_time="2026-08-30T21:29:46Z",
                creator="deployer@example.com",
                git_source=SimpleNamespace(resolved_commit="active-commit"),
            )
        ),
    )
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs")
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(
            config=SimpleNamespace(host=workspace_host),
            apps=apps_api,
        ),
    )

    result = asyncio.run(health.deployment_metadata())

    assert result["source"] == "databricks_apps_api"
    assert result["verified"] is True
    assert result["commit_sha"] == "active-commit"
    assert calls == [
        ("get", "cost-obs"),
        ("get_deployment", "cost-obs", "deployment-active"),
    ]


def test_gcp_legacy_runtime_resolves_app_name_from_service_principal(monkeypatch):
    client_id = "runtime-app-client"
    calls: list[str] = []
    app = SimpleNamespace(
        name="cost-obs-gcp",
        service_principal_client_id=client_id,
        service_principal_name=None,
    )
    apps_api = SimpleNamespace(
        list=lambda: iter([app]),
        get=lambda name: (
            calls.append(name)
            or SimpleNamespace(
                active_deployment=SimpleNamespace(
                    create_time="2026-08-30T21:29:46Z",
                    creator="deployer@example.com",
                    git_source=SimpleNamespace(resolved_commit="gcp-commit"),
                )
            )
        ),
    )
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", client_id)
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                host="https://1234567890123456.7.gcp.databricks.com"
            ),
            apps=apps_api,
        ),
    )

    result = asyncio.run(health.deployment_metadata())

    assert result["commit_sha"] == "gcp-commit"
    assert result["verified"] is True
    assert calls == ["cost-obs-gcp"]


def test_endpoint_labels_process_start_as_approximate_restart_in_local_runtime():
    result = asyncio.run(health.deployment_metadata())

    assert result["deployed_at"] == health._PROCESS_STARTED_AT
    assert result["deployer"] is None
    assert result["commit_sha"] is None
    assert result["available"] is True
    assert result["source"] == "process_start_approximate_restart"
    assert result["verified"] is False


def test_committed_release_metadata_is_last_safe_fallback(monkeypatch, tmp_path):
    metadata_file = tmp_path / "release-metadata.json"
    metadata_file.write_text(json.dumps({
        "deployed_at": "2026-08-29T20:00:00Z",
        "deployer": "release automation",
        "commit_sha": "committed-fallback",
    }))
    monkeypatch.setattr(
        health,
        "_COMMITTED_DEPLOYMENT_METADATA_PATH",
        metadata_file,
    )

    result = asyncio.run(health.deployment_metadata())

    assert result == {
        "deployed_at": "2026-08-29T20:00:00Z",
        "deployer": "release automation",
        "commit_sha": "committed-fallback",
        "available": True,
        "source": "committed_release_metadata",
        "verified": False,
    }


def test_fallbacks_fill_missing_active_deployment_fields(monkeypatch):
    monkeypatch.setenv("COST_OBS_COMMIT_SHA", "release-commit")
    merged = health._merge_deployment_metadata(
        {
            "deployed_at": "2026-08-30T21:29:46Z",
            "deployer": "deployer@example.com",
            "commit_sha": None,
            "source": "databricks_apps_api",
        },
        health._deployment_metadata_from_env(),
    )

    assert merged["deployed_at"] == "2026-08-30T21:29:46Z"
    assert merged["deployer"] == "deployer@example.com"
    assert merged["commit_sha"] == "release-commit"
    assert merged["source"] == "databricks_apps_api+release_environment"
    assert merged["verified"] is False


def test_databricks_app_env_fills_fields_the_apps_api_left_blank(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_COMMIT", "app-env-commit")
    merged = health._merge_deployment_metadata(
        {
            "deployed_at": "2026-08-30T21:29:46Z",
            "deployer": "deployer@example.com",
            "commit_sha": None,
            "source": "databricks_apps_api",
        },
        health._deployment_metadata_from_env(),
        health._deployment_metadata_from_databricks_app_env(),
    )

    assert merged["commit_sha"] == "app-env-commit"
    assert merged["source"] == "databricks_apps_api+databricks_app_environment"
    assert merged["verified"] is False


def test_verified_only_when_source_is_apps_api():
    apps_only = health._merge_deployment_metadata(
        {
            "deployed_at": "2026-08-30T21:29:46Z",
            "deployer": "deployer@example.com",
            "commit_sha": "abc",
            "source": "databricks_apps_api",
        }
    )
    assert apps_only["verified"] is True

    env_only = health._merge_deployment_metadata(
        health._deployment_metadata_from_app(None),
        {
            "deployed_at": "2026-08-30T21:29:46Z",
            "deployer": None,
            "commit_sha": None,
            "source": "databricks_app_environment",
        },
    )
    assert env_only["source"] == "databricks_app_environment"
    assert env_only["verified"] is False


def test_concurrent_endpoint_calls_share_one_sdk_fetch_and_reuse_cache(monkeypatch):
    calls = 0

    def get_app(_name):
        nonlocal calls
        calls += 1
        time.sleep(0.03)
        return SimpleNamespace(
            active_deployment=SimpleNamespace(
                create_time="2026-08-30T21:29:46Z",
                creator="deployer@example.com",
                git_source=SimpleNamespace(resolved_commit="ed86035f"),
            )
        )

    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs")
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(apps=SimpleNamespace(get=get_app)),
    )

    async def load_concurrently():
        return await asyncio.gather(*(health.deployment_metadata() for _ in range(8)))

    results = asyncio.run(load_concurrently())
    cached = asyncio.run(health.deployment_metadata())

    assert calls == 1
    assert all(result["commit_sha"] == "ed86035f" for result in results)
    assert all(result["verified"] is True for result in results)
    assert cached["source"] == "databricks_apps_api"


def test_sdk_error_uses_release_environment_fallback(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs")
    monkeypatch.setenv("COST_OBS_DEPLOYED_AT", "2026-08-30T21:29:46Z")
    monkeypatch.setenv("COST_OBS_COMMIT_SHA", "fallback")
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(
            apps=SimpleNamespace(get=lambda _name: (_ for _ in ()).throw(RuntimeError("down")))
        ),
    )

    result = asyncio.run(health.deployment_metadata())

    assert result["source"] == "release_environment"
    assert result["verified"] is False
    assert result["commit_sha"] == "fallback"


def test_sdk_error_uses_databricks_app_env_fallback(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs")
    monkeypatch.setenv("DATABRICKS_APP_DEPLOYED_AT", "2026-09-01T00:00:00Z")
    monkeypatch.setenv("DATABRICKS_APP_COMMIT", "app-fallback")
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(
            apps=SimpleNamespace(get=lambda _name: (_ for _ in ()).throw(RuntimeError("down")))
        ),
    )

    result = asyncio.run(health.deployment_metadata())

    assert result["source"] == "databricks_app_environment"
    assert result["verified"] is False
    assert result["deployed_at"] == "2026-09-01T00:00:00Z"
    assert result["commit_sha"] == "app-fallback"


def test_timed_out_fetch_stays_single_flight_without_spawning_more_workers(monkeypatch):
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def get_app(_name):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1)
        return SimpleNamespace(
            active_deployment=SimpleNamespace(
                create_time="2026-08-30T21:29:46Z",
                creator="deployer@example.com",
                git_source=SimpleNamespace(resolved_commit="late"),
            )
        )

    monkeypatch.setenv("DATABRICKS_APP_NAME", "cost-obs")
    monkeypatch.setattr(health, "_DEPLOYMENT_METADATA_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        db,
        "get_workspace_client",
        lambda: SimpleNamespace(apps=SimpleNamespace(get=get_app)),
    )

    try:
        async def time_out_concurrently():
            return await asyncio.gather(*(health.deployment_metadata() for _ in range(6)))

        results = asyncio.run(time_out_concurrently())
        assert started.is_set()
        assert calls == 1
        assert all(result["deployed_at"] == health._PROCESS_STARTED_AT for result in results)
        assert all(result["verified"] is False for result in results)
        with health._deployment_metadata_lock:
            assert len(health._deployment_metadata_inflight) == 1
    finally:
        release.set()

    for _ in range(20):
        with health._deployment_metadata_lock:
            if not health._deployment_metadata_inflight:
                break
        time.sleep(0.01)
    assert calls == 1


def test_lakebase_instance_name_and_console_url(monkeypatch):
    monkeypatch.setenv(
        "ENDPOINT_NAME",
        "projects/cost-obs-db-v21/branches/production/endpoints/primary",
    )
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    assert health.lakebase_instance_name() == "cost-obs-db-v21"
    assert health.lakebase_page_url() == (
        "https://dbc.example.com/compute/database-instances/cost-obs-db-v21"
    )
