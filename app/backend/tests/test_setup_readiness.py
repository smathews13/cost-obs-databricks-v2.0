"""Regression tests for the readiness check infrastructure in server/routers/setup.py.

Run with: pytest server/tests/test_setup_readiness.py -v
"""
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest.mock import MagicMock, patch

import pytest

import backend.routers.setup as setup_mod
from backend.routers.setup import (
    CheckStatus,
    TimedWarehouseCheckCache,
    WarehouseCheckResult,
    _get_or_start_warehouse_check_future,
    _safe_table_check_result,
    check_warehouse_readiness,
    reset_readiness_caches,
)


@pytest.fixture(autouse=True)
def clean_caches():
    """Reset all readiness caches before and after each test to prevent state leakage."""
    reset_readiness_caches()
    yield
    reset_readiness_caches()


def _mock_token():
    """Return a MagicMock that satisfies ContextVar.set/reset protocol."""
    mock = MagicMock()
    mock.set.return_value = mock
    return mock


# ---------------------------------------------------------------------------
# Bug 2: cold-warehouse hang + single-flight
# ---------------------------------------------------------------------------

def test_warehouse_timeout_returns_quickly():
    """check_warehouse_readiness() must return TIMEOUT_STARTING immediately on timeout,
    not block for _WH_CHECK_TIMEOUT seconds.
    """
    slow_future: Future = Future()
    slow_future.result = MagicMock(side_effect=FutureTimeoutError())  # type: ignore[method-assign]

    with (
        patch.object(setup_mod, "_get_or_start_warehouse_check_future", return_value=slow_future),
        patch.object(setup_mod, "_get_cached_warehouse_check", return_value=None),
        patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-123")),
    ):
        start = time.monotonic()
        result = check_warehouse_readiness()
        elapsed = time.monotonic() - start

    assert result.status == CheckStatus.TIMEOUT_STARTING
    assert not result.ok
    assert elapsed < 2.0  # Must not hang — future.result mock raises immediately


def test_single_flight_reuses_inflight_future():
    """Two consecutive calls to _get_or_start_warehouse_check_future() while a future
    is in-flight must reuse that future — not start a second daemon thread.
    """
    thread_starts: list = []

    class _NoopThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            thread_starts.append(1)

    with patch("backend.routers.setup.threading.Thread", _NoopThread):
        f1 = _get_or_start_warehouse_check_future()
        f2 = _get_or_start_warehouse_check_future()  # Should reuse f1, not start again

    assert len(thread_starts) == 1, "Thread.start() should be called exactly once"
    assert f1 is f2


# ---------------------------------------------------------------------------
# Bug 4: typed failure classification + traceback logging
# ---------------------------------------------------------------------------

def test_blocking_warehouse_check_internal_error():
    """_run_blocking_warehouse_check() must classify an unexpected exception as
    INTERNAL_ERROR (not silently swallow it or mis-classify it).
    """
    with (
        patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-123")),
        patch("backend.db.execute_query", side_effect=RuntimeError("unexpected crash")),
        patch("backend.db._user_token", _mock_token()),
    ):
        result = setup_mod._run_blocking_warehouse_check()

    assert result.status == CheckStatus.INTERNAL_ERROR
    assert not result.ok
    assert "unexpected crash" in result.message


def test_safe_table_check_result_handles_exception():
    """_safe_table_check_result() must catch a future exception and return INTERNAL_ERROR
    rather than propagating the exception to the caller.
    """
    bad_future: Future = Future()
    bad_future.set_exception(RuntimeError("disk exploded"))

    ok, msg, status = _safe_table_check_result("system.billing.usage", bad_future)

    assert not ok
    assert status == CheckStatus.INTERNAL_ERROR
    assert "disk exploded" in msg


# ---------------------------------------------------------------------------
# Fix B: _run_blocking_warehouse_check guard
# ---------------------------------------------------------------------------

def test_blocking_warehouse_check_returns_not_configured_when_no_warehouse():
    """_run_blocking_warehouse_check must return NOT_CONFIGURED immediately when no
    warehouse is resolvable, not fall through to execute_query and produce a
    misleading connection error.
    """
    with patch.object(setup_mod, "_resolve_warehouse_config", return_value=("none", "")):
        result = setup_mod._run_blocking_warehouse_check()

    assert result.status == CheckStatus.NOT_CONFIGURED
    assert not result.ok
    assert result.source == "none"


# ---------------------------------------------------------------------------
# Fix C: NOT_CONFIGURED flows through the cache path
# ---------------------------------------------------------------------------

def test_not_configured_result_is_cached_with_long_ttl():
    """check_warehouse_readiness() must cache a NOT_CONFIGURED result so that the
    env var isn't re-read on every request.  TTL should be close to 3600 s.
    """
    with patch.object(setup_mod, "_resolve_warehouse_config", return_value=("none", "")):
        result1 = check_warehouse_readiness()

    assert result1.status == CheckStatus.NOT_CONFIGURED
    cached = setup_mod._wh_check_cache
    assert cached is not None, "NOT_CONFIGURED result was not written to cache"
    assert cached.is_valid()
    remaining_ttl = cached.expires_at - time.monotonic()
    assert remaining_ttl > 3500, f"Expected ~3600 s TTL, got {remaining_ttl:.0f} s"

    # Second call must hit cache — same object reference, no new thread started
    with patch("backend.routers.setup.threading.Thread",
               side_effect=AssertionError("Thread must not be started on cache hit")):
        result2 = check_warehouse_readiness()

    assert result2 is result1


# ---------------------------------------------------------------------------
# TOCTOU: table cache snapshot survives concurrent reset
# ---------------------------------------------------------------------------

def test_toctou_table_cache_snapshot_survives_concurrent_reset():
    """The cache fast-path must hold a snapshot of _table_readiness_cache so that a
    concurrent reset_readiness_caches() between the is-None check and the data access
    does not raise TypeError: 'NoneType' object is not subscriptable.
    """
    core_data = [
        {"table": "system.billing.usage", "name": "Usage", "granted": True,
         "description": "", "required": True, "category": "core"},
    ]
    wh_result = WarehouseCheckResult(
        status=CheckStatus.HEALTHY, ok=True, message="",
        warehouse_id="wh-1", source="app_resource",
    )
    setup_mod._table_readiness_cache = {"core": core_data, "enhanced": [], "sp_client_id": "sp-1"}
    setup_mod._table_readiness_cache_ts = time.monotonic()
    setup_mod._wh_check_cache = TimedWarehouseCheckCache(
        result=wh_result, expires_at=time.monotonic() + 300,
    )

    def reset_during_wh_check():
        """Simulates another thread calling reset_readiness_caches() at this exact
        moment — after the table cache snapshot is taken but before ["core"] is accessed."""
        setup_mod._table_readiness_cache = None  # nuclear reset mid-flight
        return wh_result

    with (
        patch.object(setup_mod, "_get_cached_warehouse_check", side_effect=reset_during_wh_check),
        patch.object(setup_mod, "_resolve_warehouse_config", return_value=("app_resource", "wh-1")),
    ):
        # Without the snapshot fix this raises TypeError: 'NoneType' is not subscriptable
        result = setup_mod._check_readiness_sync()

    assert result["core"] == core_data, "Snapshot was not used — global was read after reset"


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def test_reset_readiness_caches_clears_all_state():
    """reset_readiness_caches() must zero out every cache field so the next request
    performs a full live check.
    """
    # Populate all fields
    setup_mod._table_readiness_cache = {"core": [], "enhanced": [], "sp_client_id": ""}
    setup_mod._table_readiness_cache_ts = time.monotonic()
    setup_mod._wh_check_cache = TimedWarehouseCheckCache(
        result=WarehouseCheckResult(status=CheckStatus.HEALTHY, ok=True, message=""),
        expires_at=time.monotonic() + 300,
    )
    dummy_future: Future = Future()
    with setup_mod._wh_check_lock:
        setup_mod._wh_check_inflight = dummy_future

    reset_readiness_caches()

    assert setup_mod._table_readiness_cache is None
    assert setup_mod._table_readiness_cache_ts == 0.0
    assert setup_mod._wh_check_cache is None
    assert setup_mod._wh_check_inflight is None
