"""The nightly rebuild must not wait on its own lock, and a stuck one must show up."""

import fcntl
import threading

import pytest

from backend import app as app_module
from backend import materialized_views as mv
from backend.routers import settings


@pytest.fixture
def lock_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(mv, "_MV_LOCK_PATH", str(tmp_path / "rebuild.lock"))
    monkeypatch.setattr(app_module, "_SCHEDULE_LOCK_PATH", str(tmp_path / "schedule.lock"))
    return tmp_path


def test_schedule_lock_is_not_the_rebuild_lock():
    assert app_module._SCHEDULE_LOCK_PATH != mv._MV_LOCK_PATH


def test_rebuild_runs_while_the_scheduler_holds_its_lock(lock_paths):
    with open(app_module._SCHEDULE_LOCK_PATH, "w") as schedule:
        fcntl.flock(schedule, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entered = threading.Event()

        def rebuild():
            with mv._mv_write_guard():
                entered.set()

        worker = threading.Thread(target=rebuild, daemon=True)
        worker.start()
        worker.join(5)
        assert entered.is_set(), "the scheduled rebuild waited on the scheduler's own lock"


def test_a_held_rebuild_lock_fails_with_a_clear_error(lock_paths, monkeypatch):
    monkeypatch.setattr(mv, "_MV_LOCK_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(mv, "_MV_LOCK_POLL_SECONDS", 0.05)
    with open(mv._MV_LOCK_PATH, "w") as stuck:
        fcntl.flock(stuck, fcntl.LOCK_EX)
        with pytest.raises(mv.MvRebuildBusyError, match="still holds the rebuild lock"):
            with mv._mv_write_guard():
                pass

    with mv._mv_write_guard():
        pass


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    path = tmp_path / "mv_refresh_log.json"
    monkeypatch.setattr(settings, "_refresh_log_path", lambda: str(path))
    saved: list[dict] = []
    monkeypatch.setattr(settings, "save_refresh_log_to_delta", lambda data: saved.append(data))
    return saved


def test_post_deploy_build_counts_as_the_last_rebuild(log_file):
    settings.append_refresh_history("success", "startup", duration_seconds=12)

    log = settings.read_refresh_log_file()
    assert log["status"] == "success"
    assert log["last_refresh_utc"] == log["refresh_history"][-1]["timestamp"]


def test_config_events_do_not_count_as_a_rebuild(log_file):
    settings.append_refresh_history("config", "config", note="source added")
    assert "last_refresh_utc" not in settings.read_refresh_log_file()


def test_status_shows_a_rebuild_running_in_any_worker(lock_paths, log_file):
    assert settings.current_refresh_status() is None

    entered, release = threading.Event(), threading.Event()

    def rebuild():
        mv.set_rebuild_trigger("scheduled")
        with mv._mv_write_guard():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=rebuild, daemon=True)
    worker.start()
    assert entered.wait(5)
    try:
        running = settings.current_refresh_status()["in_progress"]
        assert running["trigger"] == "scheduled"
        assert running["started_utc"]
    finally:
        release.set()
        worker.join(5)

    status = settings.current_refresh_status()
    assert status is None or status["in_progress"] is None


def test_a_writer_waiting_its_turn_keeps_the_holders_details(lock_paths, monkeypatch):
    monkeypatch.setattr(mv, "_MV_LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(mv, "_MV_LOCK_POLL_SECONDS", 0.05)
    entered, release = threading.Event(), threading.Event()

    def holder():
        mv.set_rebuild_trigger("startup")
        with mv._mv_write_guard():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    assert entered.wait(5)
    try:
        def waiter():
            mv.set_rebuild_trigger("manual")
            with pytest.raises(mv.MvRebuildBusyError):
                with mv._mv_write_guard():
                    pass

        waiting = threading.Thread(target=waiter)
        waiting.start()
        waiting.join(5)
        assert mv.current_rebuild()["trigger"] == "startup"
    finally:
        release.set()
        worker.join(5)


def test_concurrent_history_writes_lose_nothing(log_file):
    writers = [
        threading.Thread(target=settings.append_refresh_history, args=("config", "config"), kwargs={"note": str(i)})
        for i in range(12)
    ]
    for w in writers:
        w.start()
    for w in writers:
        w.join(5)

    notes = {e["note"] for e in settings.read_refresh_log_file()["refresh_history"]}
    assert notes == {str(i) for i in range(12)}


@pytest.fixture
def stale_deploy(lock_paths, log_file, monkeypatch):
    builds: list[str] = []
    monkeypatch.setattr("backend.db.get_catalog_schema", lambda: ("cat", "sch"))
    monkeypatch.setattr("backend.db.validate_app_storage_target", lambda c, s: None)
    monkeypatch.setattr("backend.db.execute_query", lambda *a, **k: [{"latest": "2026-01-01"}])
    monkeypatch.setattr("backend.db.get_mv_sources", lambda: [])
    monkeypatch.setattr(mv, "check_materialized_views_exist", lambda c, s: {"daily_usage_summary": True})
    monkeypatch.setattr("backend.routers.setup._CORE_REQUIRED_TABLES", ["daily_usage_summary"])
    monkeypatch.setattr(mv, "create_materialized_views", lambda c, s: builds.append(c) or {"daily_usage_summary": "created"})

    class _Inline:
        def __init__(self, target, **_k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr("threading.Thread", _Inline)
    return builds


def test_post_deploy_refresh_runs_and_is_recorded(stale_deploy):
    app_module.setup_materialized_views()

    assert stale_deploy == ["cat"]
    assert settings.read_refresh_log_file()["status"] == "success"


def test_post_deploy_refresh_skips_while_another_rebuild_holds_the_schedule_lock(stale_deploy):
    with open(app_module._SCHEDULE_LOCK_PATH, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app_module.setup_materialized_views()

    assert stale_deploy == []
