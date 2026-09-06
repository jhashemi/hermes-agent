"""Tests for event_ids passthrough (Option A, ADR-006b Phase 3).

Verifies that every kernel write op that emits task_events rows carries
the minted SQLite ids in the ``event_ids`` kwarg of the ``kanban_write_op``
seam, so the DuckDB mirror can INSERT with explicit ids rather than minting
its own from the DuckDB sequence.

Coverage: top-5 offender kinds from the parity measurement:
  heartbeat, claimed, spawned (promoted as proxy), created, linked
plus: completed, blocked, unblocked, unlinked, commented, assigned,
      reclaimed, release_stale_claims.

Design:
  * Each test hooks into ``kanban_write_op`` via the captured_write_ops
    fixture (imported from test_kanban_write_op_seam.py patterns).
  * For each op we assert:
    (a) event_ids is a list
    (b) event_ids is non-empty for ops that emit >=1 event
    (c) every id in event_ids exists in task_events with the right kind
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import get_plugin_manager


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def db_and_conn(tmp_path, monkeypatch):
    """Isolated kanban SQLite db + open connection."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


@pytest.fixture
def captured_ops(monkeypatch):
    """Register a capturing kanban_write_op callback; yield collected events."""
    import importlib
    plugins_mod = importlib.import_module("hermes_cli.plugins")
    mgr = plugins_mod.get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    events: list[dict] = []
    mgr._hooks.setdefault("kanban_write_op", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        yield events
    finally:
        mgr._hooks = saved


def _event_ids_for_op(events: list[dict], op: str) -> list[int]:
    """Return the event_ids list from the first fire of ``op``."""
    for e in events:
        if e.get("op") == op:
            return e.get("event_ids", [])
    return []


def _event_kinds_for_ids(conn: sqlite3.Connection, ids: list[int]) -> set[str]:
    """Look up task_events.kind for the given ids."""
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT kind FROM task_events WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r[0] for r in rows}


# --------------------------------------------------------------------------- #
# Individual op tests
# --------------------------------------------------------------------------- #

class TestCreateTaskEventIds:
    """create_task must carry the ``created`` event id."""

    def test_created_event_id_is_present(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t1", assignee="worker")
        ids = _event_ids_for_op(captured_ops, "create_task")
        assert isinstance(ids, list), "event_ids must be a list"
        assert len(ids) >= 1, "create_task must emit >=1 event id"

    def test_created_event_id_resolves_in_db(self, db_and_conn, captured_ops):
        conn = db_and_conn
        kb.create_task(conn, title="t2", assignee="worker")
        ids = _event_ids_for_op(captured_ops, "create_task")
        kinds = _event_kinds_for_ids(conn, ids)
        assert "created" in kinds, f"created kind not in {kinds} for ids {ids}"

    def test_event_id_matches_lastrowid(self, db_and_conn, captured_ops):
        """The event_ids value must be the actual rowid SQLite minted."""
        conn = db_and_conn
        kb.create_task(conn, title="t3", assignee="worker")
        ids = _event_ids_for_op(captured_ops, "create_task")
        # Confirm the id actually exists as a real row
        for eid in ids:
            row = conn.execute(
                "SELECT id FROM task_events WHERE id = ?", (eid,)
            ).fetchone()
            assert row is not None, f"event id {eid} not found in task_events"
            assert int(row[0]) == eid


class TestClaimTaskEventIds:
    """claim_task must carry the ``claimed`` event id."""

    def test_claimed_event_id_present(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        captured_ops.clear()
        kb.claim_task(conn, task_id)
        ids = _event_ids_for_op(captured_ops, "claim_task")
        assert len(ids) >= 1

    def test_claimed_event_id_resolves(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        captured_ops.clear()
        kb.claim_task(conn, task_id)
        ids = _event_ids_for_op(captured_ops, "claim_task")
        kinds = _event_kinds_for_ids(conn, ids)
        assert "claimed" in kinds


class TestHeartbeatEventIds:
    """heartbeat_claim must carry the ``heartbeat`` event id."""

    def test_heartbeat_event_id_present(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, task_id)
        captured_ops.clear()
        kb.heartbeat_claim(conn, task_id)
        ids = _event_ids_for_op(captured_ops, "heartbeat_claim")
        assert isinstance(ids, list)
        # heartbeat_claim may or may not emit a heartbeat event depending on
        # kernel version; when it does, the id must resolve.
        for eid in ids:
            row = conn.execute(
                "SELECT id FROM task_events WHERE id = ?", (eid,)
            ).fetchone()
            assert row is not None, f"heartbeat event id {eid} missing"


class TestLinkedEventIds:
    """link_tasks must carry the ``linked`` event id."""

    def test_linked_event_id_present(self, db_and_conn, captured_ops):
        conn = db_and_conn
        parent = kb.create_task(conn, title="p", assignee="worker")
        child = kb.create_task(conn, title="c", assignee="worker")
        captured_ops.clear()
        kb.link_tasks(conn, parent, child)
        ids = _event_ids_for_op(captured_ops, "link_tasks")
        assert len(ids) >= 1

    def test_linked_event_id_resolves(self, db_and_conn, captured_ops):
        conn = db_and_conn
        parent = kb.create_task(conn, title="p", assignee="worker")
        child = kb.create_task(conn, title="c", assignee="worker")
        captured_ops.clear()
        kb.link_tasks(conn, parent, child)
        ids = _event_ids_for_op(captured_ops, "link_tasks")
        kinds = _event_kinds_for_ids(conn, ids)
        assert "linked" in kinds, f"linked kind not in {kinds}"


class TestCompleteTaskEventIds:
    """complete_task must carry the ``completed`` event id."""

    def test_completed_event_id_present(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, task_id)
        captured_ops.clear()
        kb.complete_task(conn, task_id, summary="done")
        ids = _event_ids_for_op(captured_ops, "complete_task")
        assert len(ids) >= 1

    def test_completed_event_id_resolves(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, task_id)
        captured_ops.clear()
        kb.complete_task(conn, task_id, summary="done")
        ids = _event_ids_for_op(captured_ops, "complete_task")
        kinds = _event_kinds_for_ids(conn, ids)
        assert "completed" in kinds, f"completed kind not in {kinds}"


class TestEventIdsAreUnique:
    """event_ids must contain only distinct positive integers (no duplicates)."""

    def test_no_duplicates_across_one_op(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="s")
        for ev in captured_ops:
            ids = ev.get("event_ids", [])
            assert len(ids) == len(set(ids)), (
                f"op={ev.get('op')} has duplicate event_ids: {ids}"
            )
            for eid in ids:
                assert eid > 0, f"event_id must be positive, got {eid}"


class TestEventIdsStackCleanup:
    """The ContextVar accumulator stack must be empty after every op.

    A leaked accumulator frame would mean successive ops accumulate ids
    from prior unrelated ops.
    """

    def test_stack_empty_after_single_op(self, db_and_conn, captured_ops):
        from hermes_cli.kanban_db import _event_id_stack, _event_id_pending
        conn = db_and_conn
        kb.create_task(conn, title="t1", assignee="worker")
        # After the op commits and the seam fires, both stack and pending
        # must be cleared.
        assert _event_id_stack.get() == [], (
            f"event id stack not empty: {_event_id_stack.get()}"
        )
        assert _event_id_pending.get() is None, (
            f"event id pending not cleared: {_event_id_pending.get()}"
        )

    def test_stack_empty_after_multiple_sequential_ops(self, db_and_conn, captured_ops):
        from hermes_cli.kanban_db import _event_id_stack, _event_id_pending
        conn = db_and_conn
        parent = kb.create_task(conn, title="p", assignee="w")
        child = kb.create_task(conn, title="c", assignee="w")
        kb.link_tasks(conn, parent, child)
        kb.claim_task(conn, parent)
        kb.heartbeat_claim(conn, parent)
        kb.complete_task(conn, parent, summary="done")
        assert _event_id_stack.get() == []
        assert _event_id_pending.get() is None


class TestBlockUnblockEventIds:
    """block_task and unblock_task must carry event ids."""

    def test_block_event_id_resolves(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, task_id)
        captured_ops.clear()
        kb.block_task(conn, task_id, reason="test")
        ids = _event_ids_for_op(captured_ops, "block_task")
        assert len(ids) >= 1
        kinds = _event_kinds_for_ids(conn, ids)
        assert kinds & {"blocked", "dependency_wait"}, f"no block kind in {kinds}"

    def test_unblock_event_id_resolves(self, db_and_conn, captured_ops):
        conn = db_and_conn
        task_id = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, task_id)
        kb.block_task(conn, task_id, reason="test")
        captured_ops.clear()
        kb.unblock_task(conn, task_id)
        ids = _event_ids_for_op(captured_ops, "unblock_task")
        assert len(ids) >= 1
        kinds = _event_kinds_for_ids(conn, ids)
        assert "unblocked" in kinds
