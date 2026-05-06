"""Tests for ADR 002: Resource-aware dispatcher (fork-bomb prevention).

Tests the new can_spawn(), backoff_remaining(), concurrency limits,
and DispatchResult fields added to kanban_db.py.
"""
import os
import time
import sqlite3
import pytest

# Ensure hermes_cli is importable
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from hermes_cli.kanban_db import (
    can_spawn,
    backoff_remaining,
    _capture_health_snapshot,
    _running_workers_by_profile,
    _running_workers_total,
    DispatchResult,
    BACKOFF_DELAYS,
    DEFAULT_MAX_CONCURRENT_PER_PROFILE,
    DEFAULT_MAX_CONCURRENT_BOARD,
    RESOURCE_MAX_PID_RATIO,
    RESOURCE_MIN_MEM_MB,
    RESOURCE_MAX_LOAD_MULTIPLIER,
)


class TestCanSpawn:
    """Tests for the resource health check function."""

    def test_live_system_passes(self):
        """On a healthy system, can_spawn() returns True."""
        ok, reason = can_spawn()
        assert ok is True
        assert reason == "ok"

    def test_pid_pressure_blocks_spawn(self):
        """If PID count exceeds the ratio threshold, can_spawn returns False."""
        health = {"pids_running": 26000, "pid_max": 32768, "mem_available_mb": 8000, "load_1min": 0.5, "cpu_count": 4}
        # 26000/32768 = 79.3% — just under 80% should pass
        ok, _ = can_spawn(health)
        assert ok is True

        # Now push over 80%
        health["pids_running"] = 27000
        ok, reason = can_spawn(health)
        assert ok is False
        assert "pids" in reason

    def test_memory_pressure_blocks_spawn(self):
        """If available RAM is below threshold, can_spawn returns False."""
        health = {"pids_running": 100, "pid_max": 32768, "mem_available_mb": 200, "load_1min": 0.5, "cpu_count": 4}
        ok, reason = can_spawn(health)
        assert ok is False
        assert "mem" in reason

    def test_load_pressure_blocks_spawn(self):
        """If load exceeds cpu_count * multiplier, can_spawn returns False."""
        health = {"pids_running": 100, "pid_max": 32768, "mem_available_mb": 5000, "load_1min": 12.0, "cpu_count": 4}
        ok, reason = can_spawn(health)
        assert ok is False
        assert "load" in reason

    def test_missing_metrics_skip_check(self):
        """If a metric is -1 (unavailable), that check is skipped."""
        health = {"pids_running": -1, "pid_max": -1, "mem_available_mb": -1, "load_1min": -1.0, "cpu_count": -1}
        ok, reason = can_spawn(health)
        assert ok is True

    def test_capture_health_snapshot(self):
        """Health snapshot includes expected keys on Linux."""
        snap = _capture_health_snapshot()
        assert "pids_running" in snap
        assert "pid_max" in snap
        assert "mem_available_mb" in snap
        assert "load_1min" in snap
        assert "cpu_count" in snap
        # On Linux, pids_running should be positive
        if sys.platform == "linux":
            assert snap["pids_running"] > 0
            assert snap["pid_max"] > 0


class TestBackoffRemaining:
    """Tests for exponential backoff calculation."""

    def test_zero_failures_no_backoff(self):
        """No failures → no backoff."""
        assert backoff_remaining(0, time.time()) == 0.0

    def test_none_timestamp_no_backoff(self):
        """No failure timestamp → no backoff."""
        assert backoff_remaining(1, None) == 0.0

    def test_recent_failure_has_backoff(self):
        """A task that just failed should have remaining backoff ≈ full delay."""
        just_now = time.time()
        remaining = backoff_remaining(1, just_now)
        # Schedule for 1 failure: 30s. Just happened, so ~30s remaining.
        assert 29.0 <= remaining <= 30.0

    def test_old_failure_backoff_elapsed(self):
        """A task that failed long ago should have 0 remaining backoff."""
        long_ago = time.time() - 86400  # 1 day ago
        for i in range(1, 9):
            remaining = backoff_remaining(i, long_ago)
            assert remaining == 0.0, f"Backoff should be elapsed for {i} failures 1 day ago"

    def test_backoff_schedule_matches_BACKOFF_DELAYS(self):
        """Verify the backoff schedule matches the defined delays."""
        now = time.time()
        for i, expected_delay in enumerate(BACKOFF_DELAYS, start=1):
            # Failure just happened: remaining should be ≈ full delay
            remaining = backoff_remaining(i, now)
            assert expected_delay - 1.0 <= remaining <= expected_delay, \
                f"Failure {i}: expected ~{expected_delay}s, got {remaining:.1f}s"

    def test_high_failure_clamps_to_last_delay(self):
        """Beyond the schedule, use the last delay value."""
        now = time.time()
        # 20 failures (way beyond schedule length)
        remaining = backoff_remaining(20, now)
        last_delay = BACKOFF_DELAYS[-1]  # 14400s = 4h
        assert last_delay - 1.0 <= remaining <= last_delay


class TestConcurrencyLimits:
    """Tests for per-profile and board-wide concurrency caps."""

    def test_defaults_are_sensible(self):
        """Default limits should prevent fork bombs."""
        assert DEFAULT_MAX_CONCURRENT_PER_PROFILE == 2
        assert DEFAULT_MAX_CONCURRENT_BOARD == 6

    def test_dispatch_result_has_deferred_fields(self):
        """DispatchResult should have the new deferred_* fields."""
        dr = DispatchResult()
        assert hasattr(dr, "deferred_resource")
        assert hasattr(dr, "deferred_concurrency")
        assert hasattr(dr, "deferred_backoff")
        assert hasattr(dr, "health_snapshot")
        assert dr.deferred_resource == []
        assert dr.deferred_concurrency == []
        assert dr.deferred_backoff == []
        assert dr.health_snapshot == {}


class TestHealthSnapshot:
    """Tests for health metric capture on live systems."""

    def test_snapshot_types(self):
        """Health snapshot values should be the right types."""
        snap = _capture_health_snapshot()
        assert isinstance(snap["pids_running"], int)
        assert isinstance(snap["pid_max"], int)
        assert isinstance(snap["mem_available_mb"], int)
        assert isinstance(snap["cpu_count"], int)
        # load_1min can be float
        assert isinstance(snap["load_1min"], (int, float))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])