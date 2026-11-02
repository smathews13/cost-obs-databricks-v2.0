"""Workspace job URL helper used by Settings → setup job link."""

from backend.routers.settings import _resolve_setup_job_id, build_workspace_job_url


def test_job_url_empty_without_host_or_id():
    assert build_workspace_job_url("", "123") == ""
    assert build_workspace_job_url("https://adb.example.net", "") == ""


def test_job_url_adds_https_and_workspace_o():
    assert build_workspace_job_url("adb.example.net", "99", "123456789") == (
        "https://adb.example.net/jobs/99?o=123456789"
    )


def test_job_url_keeps_existing_scheme():
    assert build_workspace_job_url("https://adb.example.net/", "99") == (
        "https://adb.example.net/jobs/99"
    )


def test_resolve_prefers_env_id(monkeypatch):
    monkeypatch.setenv("COST_OBS_SETUP_JOB_ID", "555")

    class _Boom:
        def list(self, **_k):
            raise AssertionError("jobs.list must not run when env id is set")

    class _W:
        jobs = _Boom()

    assert _resolve_setup_job_id(_W()) == "555"


def test_resolve_falls_back_to_job_name(monkeypatch):
    monkeypatch.delenv("COST_OBS_SETUP_JOB_ID", raising=False)
    monkeypatch.setenv("COST_OBS_SETUP_JOB_NAME", "cost-obs-setup")

    class _Job:
        job_id = 777

    class _Jobs:
        def list(self, name=""):
            assert name == "cost-obs-setup"
            return [_Job()]

    class _W:
        jobs = _Jobs()

    assert _resolve_setup_job_id(_W()) == "777"
