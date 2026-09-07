"""Tests for WAVE-A6: dispatcher must honor ``waiting_for_event=cron:<id>``
and ``waiting_for_condition=<iso-ts>`` on ``kind='dependency'`` blocks.

Symptom before the fix (t_1e9365ce, 2026-08-22 02:00–02:14 UTC):
a time-gated verification ticket block-looped 9× in 14 minutes. Every block
used ``kind=dependency`` with ``waiting_for_event=cron:9c906066fee9`` and a
precise ``waiting_for_condition``. The dispatcher re-promoted within 15–60
seconds of each block, spawning a fresh worker each time, even though the
physical event could not occur before 2026-08-23T01:55Z (~24h later).

Root cause: :func:`hermes_cli.kanban_db._dependency_waiting_for_satisfied`
only checked the ``waiting_for=<task_id>`` field of the ``dependency_wait``
event's payload. When only ``waiting_for_event`` / ``waiting_for_condition``
were present, the predicate returned ``True`` vacuously (no task-id to
gate on), the ``all([]) == True`` on empty parents fired, and the task
was re-promoted to ``ready`` on the next dispatcher tick.

Fix: the predicate now also honors ``waiting_for_event=cron:<id>`` (holds
until the cron's ``last_run_at`` ≥ event ``created_at``) and any
ISO-8601 timestamp inside ``waiting_for_condition`` (holds until the
timestamp is in the past). Compound blocks combine via AND — a single
unsatisfied gate keeps the task blocked.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


@pytest.fixture
def fake_cron(monkeypatch):
    """Stub the lazy ``from cron import jobs`` import used by
    :func:`_cron_fired_since` with an in-memory dict-backed replacement.

    Test authors call ``fake_cron.set(job_id, last_run_at=<iso>)`` to arm
    the fake. ``last_run_at=None`` (default) represents an armed cron
    that has not yet fired.
    """
    import sys
    import types

    class _FakeCronJobs:
        def __init__(self):
            self._jobs: dict = {}

        def set(self, job_id, *, last_run_at=None, exists=True):
            if exists:
                self._jobs[job_id] = {"id": job_id, "last_run_at": last_run_at}
            else:
                self._jobs.pop(job_id, None)

        def get_job(self, job_id):
            return self._jobs.get(job_id)

    fake = _FakeCronJobs()
    fake_pkg = types.ModuleType("cron")
    fake_jobs_mod = types.ModuleType("cron.jobs")
    fake_jobs_mod.get_job = fake.get_job  # type: ignore[attr-defined]
    fake_pkg.jobs = fake_jobs_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cron", fake_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_jobs_mod)
    return fake


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status(conn, task_id: str) -> str:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None, f"task {task_id} not found"
    return row["status"]


def _dep_block(conn, *, waiting_for_event=None, waiting_for_condition=None,
               waiting_for=None, title="waiter") -> str:
    """Create a task and dependency-block it on the given envelope."""
    t = kb.create_task(conn, title=title)
    kb.claim_task(conn, t)  # ready -> running (block_task requires that)
    assert kb.block_task(
        conn, t,
        reason="waiting on external event",
        kind="dependency",
        waiting_for=waiting_for,
        waiting_for_event=waiting_for_event,
        waiting_for_condition=waiting_for_condition,
    )
    return t


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Core acceptance: ``waiting_for_event=cron:<id>`` gates re-promotion
# ---------------------------------------------------------------------------


class TestWaitingForEventCron:
    """Acceptance #1–#2 from t_907e94e6."""

    def test_cron_not_yet_fired_holds_block(self, conn, fake_cron):
        """Cron exists, ``last_run_at`` is None → dependency NOT satisfied."""
        fake_cron.set("9c906066fee9", last_run_at=None)

        child = _dep_block(conn, waiting_for_event="cron:9c906066fee9",
                           title="24h flip verification")
        assert _status(conn, child) == "todo"

        # Simulate dispatcher tick — the pre-fix bug promoted here.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert _status(conn, child) == "todo", (
            "waiting_for_event=cron:<id> with last_run_at=None must hold; "
            "this was the block-loop that spawned 9× workers in 14 minutes"
        )

    def test_cron_fired_before_block_holds(self, conn, fake_cron):
        """A cron that fired BEFORE this block was created must not
        satisfy the gate — the worker is waiting on the NEXT fire, not
        any historical one. Otherwise every second block on a
        just-fired cron would auto-promote instantly.
        """
        # Cron fired 10 minutes ago
        old_fire = datetime.now(timezone.utc) - timedelta(minutes=10)
        fake_cron.set("cron123", last_run_at=_iso(old_fire))

        # Block emitted NOW — its dependency_wait.created_at is later
        # than the cron's last_run_at.
        child = _dep_block(conn, waiting_for_event="cron:cron123", title="watcher")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert _status(conn, child) == "todo"

    def test_cron_fired_after_block_promotes(self, conn, fake_cron):
        """Acceptance #2: once the cron fires, the task promotes normally."""
        fake_cron.set("cron123", last_run_at=None)
        child = _dep_block(conn, waiting_for_event="cron:cron123", title="watcher")
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "todo"

        # Cron fires
        fire_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        fake_cron.set("cron123", last_run_at=_iso(fire_time))

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert _status(conn, child) == "ready"

    def test_no_repromotion_over_five_minute_window(self, conn, fake_cron):
        """DoD: file a block with ``waiting_for_event`` on a future cron;
        verify no re-promotion for repeated dispatcher ticks. In
        production this was 5 minutes; we simulate the tick loop
        directly so the test stays fast.
        """
        fake_cron.set("future-cron", last_run_at=None)
        child = _dep_block(conn, waiting_for_event="cron:future-cron",
                           title="held")

        # Simulate ~20 dispatcher ticks (the loop that burned inference
        # in production spawned once every ~30-60s → 20 ticks ≈ 10min).
        for _ in range(20):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "no promotion while cron has not fired"
        assert _status(conn, child) == "todo"

    def test_unknown_cron_id_falls_through(self, conn, fake_cron):
        """If the referenced cron id doesn't exist (stale envelope,
        deleted job), fall through so the operator can unblock manually
        via ``kanban_unblock``. Otherwise a mis-typed cron id would
        strand the task forever with no observable signal.
        """
        # Do not arm any cron with id "ghost".
        child = _dep_block(conn, waiting_for_event="cron:ghost", title="stale")

        promoted = kb.recompute_ready(conn)
        assert promoted == 1, (
            "unresolvable cron id should fall through to task_links gate; "
            "task has no parents so it promotes vacuously"
        )
        assert _status(conn, child) == "ready"


# ---------------------------------------------------------------------------
# Core acceptance: ``waiting_for_condition`` gates on ISO timestamps
# ---------------------------------------------------------------------------


class TestWaitingForCondition:
    """DoD: also honor ``waiting_for_condition`` — if truthy, unblock;
    if falsy (future timestamp), hold."""

    def test_future_iso_timestamp_holds_block(self, conn):
        """A condition string referencing a future timestamp must hold."""
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        child = _dep_block(conn,
                           waiting_for_condition=f"physical event by {_iso(future)}",
                           title="24h verifier")

        assert _status(conn, child) == "todo"
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "todo"

    def test_past_iso_timestamp_promotes(self, conn):
        """A condition timestamp already in the past → satisfied."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        child = _dep_block(conn,
                           waiting_for_condition=f"after {_iso(past)}",
                           title="already-due verifier")

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert _status(conn, child) == "ready"

    def test_non_timestamp_condition_falls_through(self, conn):
        """A non-parseable free-form condition (no ISO ts inside) falls
        through so the operator can unblock — we can't evaluate arbitrary
        predicates and stranding the task is worse than a soft-fail.
        """
        child = _dep_block(conn,
                           waiting_for_condition="human review required",
                           title="soft")
        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert _status(conn, child) == "ready"

    def test_z_suffix_timestamp_parses(self, conn):
        """The condition ``waiting_for_condition="physical event >= 2026-08-23T01:55Z"``
        from the real block-loop must be parseable (``Z`` suffix)."""
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        # Format with Z suffix instead of +00:00
        z_ts = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        child = _dep_block(conn,
                           waiting_for_condition=f"physical event >= {z_ts}",
                           title="prod-shape")
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "todo"


# ---------------------------------------------------------------------------
# Compound gates: AND semantics
# ---------------------------------------------------------------------------


class TestCompoundGates:
    def test_both_gates_must_clear(self, conn, fake_cron):
        """When both ``waiting_for_event`` and ``waiting_for_condition``
        are present, ALL must clear — the block-loop ticket carried both
        simultaneously.
        """
        fake_cron.set("cron-x", last_run_at=None)
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        child = _dep_block(
            conn,
            waiting_for_event="cron:cron-x",
            waiting_for_condition=f"after {_iso(future)}",
            title="compound",
        )

        # Fire the cron but keep the timestamp in the future → still held.
        fake_cron.set("cron-x",
                      last_run_at=_iso(datetime.now(timezone.utc) + timedelta(seconds=1)))
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "todo"

    def test_all_gates_cleared_promotes(self, conn, fake_cron):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        fake_cron.set("cron-y",
                      last_run_at=_iso(datetime.now(timezone.utc) + timedelta(seconds=1)))
        child = _dep_block(
            conn,
            waiting_for_event="cron:cron-y",
            waiting_for_condition=f"after {_iso(past)}",
            title="compound-clear",
        )

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert _status(conn, child) == "ready"

    def test_event_plus_task_dependency_and(self, conn, fake_cron):
        """``waiting_for=<task_id>`` + ``waiting_for_event=cron:...``
        must both clear (AND). Task done but cron unfired → still held.
        """
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()

        fake_cron.set("cron-z", last_run_at=None)
        child = _dep_block(
            conn,
            waiting_for=parent,
            waiting_for_event="cron:cron-z",
            title="both",
        )

        assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "todo"


# ---------------------------------------------------------------------------
# Acceptance #3: existing task-completion dependency blocks unchanged
# ---------------------------------------------------------------------------


class TestNoRegressionOnTaskCompletion:
    """Existing ``waiting_for=<task_id>`` blocks must keep today's
    immediate-resume-on-parent-done behavior (VFE-DISPATCH-01)."""

    def test_task_id_only_still_works(self, conn):
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)
        child = _dep_block(conn, waiting_for=parent, title="child")

        assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "todo"

        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        assert kb.recompute_ready(conn) == 1
        assert _status(conn, child) == "ready"

    def test_legacy_no_envelope_falls_through(self, conn):
        """A ``dependency`` block with an empty envelope (no
        ``waiting_for*``) is legacy shape — must still fall through to
        the task_links gate."""
        t = kb.create_task(conn, title="legacy")
        conn.execute(
            "UPDATE tasks SET status='todo', block_kind='dependency' WHERE id=?",
            (t,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'dependency_wait', ?, strftime('%s','now'))",
            (t, json.dumps({"reason": "legacy"})),
        )
        conn.commit()

        assert kb.recompute_ready(conn) == 1
        assert _status(conn, t) == "ready"


# ---------------------------------------------------------------------------
# ISO parser unit coverage — critical for correct gate evaluation
# ---------------------------------------------------------------------------


class TestIsoParser:
    def test_z_suffix(self):
        # Z means UTC; verify epoch round-trips against the value we
        # feed back through fromisoformat, not a hand-computed epoch.
        ts = kb._parse_iso_ts("2026-08-23T01:55:00Z")
        assert ts is not None
        expected = datetime(2026, 8, 23, 1, 55, 0, tzinfo=timezone.utc).timestamp()
        assert abs(ts - expected) < 1.0

    def test_offset_suffix(self):
        assert kb._parse_iso_ts("2026-08-23T01:55:00+00:00") is not None

    def test_space_separator(self):
        assert kb._parse_iso_ts("2026-08-23 01:55:00") is not None

    def test_minute_precision(self):
        assert kb._parse_iso_ts("2026-08-23T01:55Z") is not None

    def test_embedded_timestamp(self):
        """Free-form predicates should still yield the timestamp."""
        assert kb._parse_iso_ts(
            "cron 9c906066 fires 2026-08-23T01:55Z (24h flip)"
        ) is not None

    def test_no_timestamp_returns_none(self):
        assert kb._parse_iso_ts("human approval required") is None
        assert kb._parse_iso_ts("") is None
        assert kb._parse_iso_ts(None) is None  # type: ignore[arg-type]

    def test_garbage_returns_none(self):
        assert kb._parse_iso_ts("2026-13-45T99:99Z") is None


# ---------------------------------------------------------------------------
# _cron_fired_since unit coverage
# ---------------------------------------------------------------------------


class TestCronFiredSince:
    def test_unknown_cron_returns_none(self, fake_cron):
        """No cron registered → None (unresolvable, operator escalation)."""
        assert kb._cron_fired_since("no-such-id", int(time.time())) is None

    def test_never_fired_returns_false(self, fake_cron):
        fake_cron.set("j1", last_run_at=None)
        assert kb._cron_fired_since("j1", int(time.time())) is False

    def test_fired_before_since_returns_false(self, fake_cron):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        fake_cron.set("j2", last_run_at=_iso(old))
        # since_epoch is NOW → cron fired 10min ago, so it fired < since
        assert kb._cron_fired_since("j2", int(time.time())) is False

    def test_fired_after_since_returns_true(self, fake_cron):
        recent = datetime.now(timezone.utc) + timedelta(seconds=1)
        fake_cron.set("j3", last_run_at=_iso(recent))
        # since_epoch is 5s ago → cron fires later → satisfied
        assert kb._cron_fired_since("j3", int(time.time()) - 5) is True

    def test_empty_cron_id_returns_none(self):
        assert kb._cron_fired_since("", int(time.time())) is None

    def test_corrupt_last_run_at_returns_none(self, fake_cron):
        fake_cron.set("j4", last_run_at="not-a-timestamp")
        assert kb._cron_fired_since("j4", int(time.time())) is None
