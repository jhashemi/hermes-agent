"""
Tests for kanban_circuit_breaker.py and kanban_thread_pool.py (ADR 003).

Run with: pytest tests/hermes_cli/test_kanban_adr003.py -v
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ===========================================================================
# Circuit breaker tests
# ===========================================================================

class TestCircuitBreaker:
    def setup_method(self):
        from hermes_cli.kanban_circuit_breaker import CircuitBreaker, BreakerState
        self.conn = _make_db()
        self.cb = CircuitBreaker(
            self.conn,
            board="test",
            trip_threshold=3,
            trip_window=120,
        )
        self.BreakerState = BreakerState

    def teardown_method(self):
        self.conn.close()

    def test_initial_state_is_closed(self):
        assert self.cb.state == self.BreakerState.CLOSED

    def test_success_in_closed_state_keeps_closed(self):
        with self.cb.guard():
            pass
        self.cb.record_success()
        assert self.cb.state == self.BreakerState.CLOSED

    def test_non_resource_failures_dont_trip(self):
        for _ in range(10):
            self.cb.record_failure("SyntaxError: invalid syntax")
        assert self.cb.state == self.BreakerState.CLOSED

    def test_resource_failures_trip_after_threshold(self):
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        assert self.cb.state == self.BreakerState.OPEN

    def test_open_raises_breaker_open(self):
        from hermes_cli.kanban_circuit_breaker import BreakerOpen
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        with pytest.raises(BreakerOpen):
            with self.cb.guard():
                pass

    def test_open_transitions_to_half_open_after_cooldown(self):
        from hermes_cli.kanban_circuit_breaker import BreakerOpen
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")

        # Manually backdate last_tripped_at so cooldown appears elapsed.
        row = self.cb._load()
        row.last_tripped_at = time.time() - 999
        self.cb._save(row)

        # guard() should NOT raise — it transitions to HALF_OPEN and allows probe.
        with self.cb.guard():
            pass
        assert self.cb.state == self.BreakerState.HALF_OPEN

    def test_half_open_probe_success_closes_breaker(self):
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        row = self.cb._load()
        row.last_tripped_at = time.time() - 999
        self.cb._save(row)

        with self.cb.guard():
            pass
        self.cb.record_success()
        assert self.cb.state == self.BreakerState.CLOSED

    def test_half_open_probe_failure_reopens_with_doubled_cooldown(self):
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        row = self.cb._load()
        initial_cooldown = row.cooldown_seconds
        row.last_tripped_at = time.time() - 999
        self.cb._save(row)

        with self.cb.guard():
            pass  # transitions to HALF_OPEN
        self.cb.record_failure("fork: Resource temporarily unavailable")
        row2 = self.cb._load()
        assert self.cb.state == self.BreakerState.OPEN
        assert row2.cooldown_seconds == pytest.approx(initial_cooldown * 2, rel=0.1)

    def test_half_open_conflict_raises(self):
        from hermes_cli.kanban_circuit_breaker import BreakerHalfOpenConflict, BreakerState
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        row = self.cb._load()
        row.last_tripped_at = time.time() - 999
        self.cb._save(row)

        guard = self.cb.guard()
        guard.__enter__()
        with pytest.raises(BreakerHalfOpenConflict):
            with self.cb.guard():
                pass
        guard.__exit__(None, None, None)

    def test_manual_reset_closes_breaker(self):
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        assert self.cb.state == self.BreakerState.OPEN
        self.cb.reset()
        assert self.cb.state == self.BreakerState.CLOSED

    def test_status_returns_dict(self):
        status = self.cb.status()
        assert "state" in status
        assert "trip_count" in status
        assert "cooldown_remaining" in status

    def test_consecutive_hits_reset_on_success(self):
        self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        # Not yet tripped; now succeed.
        self.cb.record_success()
        row = self.cb._load()
        assert row.consecutive_hits == 0

    def test_trip_count_accumulates(self):
        from hermes_cli.kanban_circuit_breaker import BreakerOpen

        def trip_and_recover():
            for _ in range(3):
                self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
            row = self.cb._load()
            row.last_tripped_at = time.time() - 999
            self.cb._save(row)
            with self.cb.guard():
                pass
            self.cb.record_success()

        trip_and_recover()
        trip_and_recover()
        row = self.cb._load()
        assert row.trip_count == 2

    def test_cooldown_doubles_per_trip(self):
        from hermes_cli.kanban_circuit_breaker import BASE_COOLDOWN

        # First trip: trip_count was 0, so cooldown = BASE_COOLDOWN * 2^0 = BASE_COOLDOWN
        for _ in range(3):
            self.cb.record_failure("[Errno 11] Resource temporarily unavailable")
        c1 = self.cb._load().cooldown_seconds
        assert c1 == pytest.approx(BASE_COOLDOWN, rel=0.1)  # 1st trip: 2^0 = 1×

        # Recover to HALF_OPEN then fail probe → trip_count becomes 2,
        # cooldown = BASE_COOLDOWN * 2^1 = 2×
        row = self.cb._load()
        row.last_tripped_at = time.time() - 999
        self.cb._save(row)
        with self.cb.guard():
            pass  # HALF_OPEN
        self.cb.record_failure("fork: eagain")  # probe failure: trip_count=2, 2×
        c2 = self.cb._load().cooldown_seconds
        assert c2 == pytest.approx(c1 * 2, rel=0.1)


# ===========================================================================
# ThreadPool / WorkerSlot tests
# ===========================================================================

class TestWorkerSlot:
    def setup_method(self):
        from hermes_cli.kanban_thread_pool import ThreadPool, Priority
        self.pool = ThreadPool(board="test", board_cap=4, per_profile_cap=2, high_reserved=1)
        self.Priority = Priority

    def test_acquire_returns_slot(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        assert slot is not None
        slot.release()

    def test_slot_is_context_manager(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        assert slot is not None
        with slot:
            pass
        # After exit, slot is released.
        assert slot._released

    def test_released_is_idempotent(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        slot.release()
        slot.release()  # should not raise

    def test_slot_returned_to_pool_on_context_exit(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        with slot:
            st = self.pool.stats()
            assert st.total_active == 1
        st2 = self.pool.stats()
        assert st2.total_active == 0

    def test_slot_returned_on_exception(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        try:
            with slot:
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        assert slot._released
        assert self.pool.stats().total_active == 0

    def test_task_id_attribute(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL, task_id="t_abc")
        assert slot.task_id == "t_abc"
        slot.release()

    def test_repr_shows_state(self):
        slot = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        assert "held" in repr(slot)
        slot.release()
        assert "released" in repr(slot)


class TestThreadPool:
    def setup_method(self):
        from hermes_cli.kanban_thread_pool import ThreadPool, Priority
        self.pool = ThreadPool(board="test", board_cap=4, per_profile_cap=2, high_reserved=1)
        self.Priority = Priority

    def test_per_profile_cap_enforced(self):
        s1 = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        s2 = self.pool.acquire("backend-eng", self.Priority.NORMAL)
        s3 = self.pool.acquire("backend-eng", self.Priority.NORMAL)  # should be None
        assert s1 is not None
        assert s2 is not None
        assert s3 is None
        s1.release(); s2.release()

    def test_board_cap_enforced_for_normal(self):
        # board_cap=4, high_reserved=1 → normal cap = 3
        slots = [self.pool.acquire(f"p{i}", self.Priority.NORMAL) for i in range(4)]
        assert all(s is not None for s in slots[:3])
        assert slots[3] is None
        for s in slots[:3]:
            s.release()

    def test_high_priority_can_use_reserved_slots(self):
        # Fill normal slots (3)
        n_slots = [self.pool.acquire(f"p{i}", self.Priority.NORMAL) for i in range(3)]
        # HIGH can still get 1 more (reserved slot)
        h_slot = self.pool.acquire("retry-eng", self.Priority.HIGH)
        assert h_slot is not None
        h_slot.release()
        for s in n_slots:
            s.release()

    def test_stats_reflect_active_slots(self):
        s1 = self.pool.acquire("a", self.Priority.NORMAL)
        s2 = self.pool.acquire("b", self.Priority.HIGH)
        st = self.pool.stats()
        assert st.total_active == 2
        assert st.normal_active == 1
        assert st.high_active == 1
        s1.release(); s2.release()

    def test_drain_returns_true_when_empty(self):
        assert self.pool.drain(timeout=1.0)

    def test_drain_returns_false_on_timeout(self):
        slot = self.pool.acquire("a", self.Priority.NORMAL)
        result = self.pool.drain(timeout=0.2)
        assert result is False
        slot.release()

    def test_sync_from_db_adds_orphan_slots(self):
        conn = _make_db()
        conn.execute(
            "CREATE TABLE tasks (assignee TEXT, status TEXT)"
        )
        conn.execute("INSERT INTO tasks VALUES ('backend-eng','running')")
        conn.execute("INSERT INTO tasks VALUES ('backend-eng','running')")
        conn.commit()
        # Pool starts empty; sync should add 2 orphan slots.
        self.pool.sync_from_db(conn)
        st = self.pool.stats()
        assert st.total_active == 2
        assert st.per_profile.get("backend-eng", 0) == 2
        conn.close()

    def test_sync_from_db_removes_excess_slots(self):
        # Acquire 3 slots, DB says only 1 running.
        s1 = self.pool.acquire("a", self.Priority.LOW)
        s2 = self.pool.acquire("b", self.Priority.NORMAL)
        s3 = self.pool.acquire("c", self.Priority.NORMAL)
        conn = _make_db()
        conn.execute("CREATE TABLE tasks (assignee TEXT, status TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('a','running')")
        conn.commit()
        self.pool.sync_from_db(conn)
        st = self.pool.stats()
        # Should have reduced to 1 (removed LOW-priority slots first).
        assert st.total_active == 1
        conn.close()

    def test_concurrent_acquire_release_thread_safety(self):
        # Pre-check: skip if the host can't start even one new thread.
        import _thread as _t
        try:
            done = threading.Event()
            _t.start_new_thread(lambda: done.set(), ())
            done.wait(timeout=1)
        except RuntimeError:
            pytest.skip("system too resource-constrained to start threads")

        errors = []

        def worker(idx):
            try:
                slot = self.pool.acquire(f"p{idx % 2}", self.Priority.NORMAL, timeout=0.5)
                if slot:
                    time.sleep(0.01)
                    slot.release()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        started = []
        for t in threads:
            try:
                t.start()
                started.append(t)
            except RuntimeError:
                break  # can't start more threads; test what we have
        for t in started:
            t.join()

        assert not errors
        assert self.pool.stats().total_active == 0

    def test_high_reserved_must_be_less_than_board_cap(self):
        from hermes_cli.kanban_thread_pool import ThreadPool
        with pytest.raises(ValueError):
            ThreadPool(board="x", board_cap=4, per_profile_cap=2, high_reserved=4)


class TestSlotPriorityForTask:
    def test_retry_is_high(self):
        from hermes_cli.kanban_thread_pool import slot_priority_for_task, Priority
        assert slot_priority_for_task(2, time.time()) == Priority.HIGH

    def test_fresh_task_is_normal(self):
        from hermes_cli.kanban_thread_pool import slot_priority_for_task, Priority
        assert slot_priority_for_task(0, time.time()) == Priority.NORMAL

    def test_old_task_is_low(self):
        from hermes_cli.kanban_thread_pool import slot_priority_for_task, Priority
        old = time.time() - 600
        assert slot_priority_for_task(0, old) == Priority.LOW


# ===========================================================================
# Integration: dispatch_once wires both systems
# ===========================================================================

class TestDispatchOnceIntegration:
    """Smoke-test that dispatch_once respects the circuit breaker and pool."""

    def _make_kanban_db(self):
        """Create a minimal in-memory kanban DB with the required tables."""
        from hermes_cli.kanban_db import connect
        import hermes_cli.kanban_db as kdb
        # Use the real schema by patching connect to return our in-memory DB.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Bootstrap schema from the real kanban_db
        for stmt in _extract_schema_stmts(kdb):
            try:
                conn.execute(stmt)
            except Exception:
                pass
        conn.commit()
        return conn

    def test_circuit_breaker_open_defers_all_spawns(self):
        """When breaker is OPEN, dispatch_once must not spawn anything."""
        from hermes_cli.kanban_circuit_breaker import BreakerOpen
        from hermes_cli.kanban_db import dispatch_once, kanban_db_path
        import hermes_cli.kanban_db as kdb

        mock_spawn = MagicMock(return_value=12345)

        # Build a CircuitBreaker stub that's always OPEN.
        class AlwaysOpenBreaker:
            @contextlib.contextmanager
            def guard(self):
                raise BreakerOpen("test: always open")
                yield  # unreachable
            def record_success(self): pass
            def record_failure(self, e): pass

        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "kanban.db")
            # Patch kanban_db_path so connect() opens our temp DB.
            orig_fn = kdb.kanban_db_path
            kdb.kanban_db_path = lambda board=None: __import__("pathlib").Path(db_path)
            try:
                conn = kdb.connect()
                try:
                    conn.execute(
                        "INSERT INTO tasks (id, status, assignee, title, priority, created_at) "
                        "VALUES ('t1','ready','backend-eng','test',0,?)",
                        (time.time(),)
                    )
                    conn.commit()
                except Exception as e:
                    conn.close()
                    pytest.skip(f"schema insert failed: {e}")

                result = dispatch_once(
                    conn,
                    spawn_fn=mock_spawn,
                    resource_check=False,
                    backoff_check=False,
                    circuit_breaker=AlwaysOpenBreaker(),
                    thread_pool=False,
                )
                conn.close()
            finally:
                kdb.kanban_db_path = orig_fn

        mock_spawn.assert_not_called()


def _extract_schema_stmts(kdb_module) -> list[str]:
    """Extract CREATE TABLE statements from the kanban_db module source."""
    import inspect
    src = inspect.getsource(kdb_module)
    stmts = []
    in_stmt = False
    buf = []
    for line in src.splitlines():
        if line.strip().upper().startswith("CREATE TABLE"):
            in_stmt = True
        if in_stmt:
            buf.append(line)
            if line.strip().endswith(";"):
                stmts.append("\n".join(buf))
                buf = []
                in_stmt = False
    return stmts
