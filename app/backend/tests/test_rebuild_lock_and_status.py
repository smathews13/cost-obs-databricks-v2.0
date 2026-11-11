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


def test_running_marker_is_kept_locally_until_the_post_deploy_build_finishes(log_file):
    settings.mark_refresh_started("startup")
    assert settings._read_refresh_log_file()["in_progress"]["trigger"] == "startup"

    settings.append_refresh_history("success", "startup", duration_seconds=12)

    log = settings._read_refresh_log_file()
    assert "in_progress" not in log
    assert log["status"] == "success"
    assert log["last_refresh_utc"] == log["refresh_history"][-1]["timestamp"]


def test_config_events_do_not_count_as_a_rebuild(log_file):
    settings.append_refresh_history("config", "config", note="source added")
    assert "last_refresh_utc" not in settings._read_refresh_log_file()


def test_running_marker_never_reaches_delta(monkeypatch):
    stored: dict = {}
    monkeypatch.setattr("backend.db.lakebase_available", lambda: True)
    monkeypatch.setattr("backend.db.lb_set_config", lambda key, value: stored.update({key: value}))
    monkeypatch.setattr("backend.db.execute_write", lambda *a, **k: 0)
    monkeypatch.setattr(settings, "_ensure_refresh_log_table", lambda: None)

    settings.save_refresh_log_to_delta({"status": "success", "in_progress": {"trigger": "manual"}})

    assert stored["refresh_log"] == {"status": "success"}


def test_a_marker_left_by_a_dead_process_is_cleared(log_file):
    settings.mark_refresh_started("manual", 180)
    settings.clear_refresh_in_progress()
    assert "in_progress" not in settings._read_refresh_log_file()
