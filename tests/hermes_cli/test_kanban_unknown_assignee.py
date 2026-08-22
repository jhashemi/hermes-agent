"""FIX-8: assignee validation in ``create_task``.

Behavior under test (see ``hermes_cli/kanban_db.py::create_task``):

1. Soft-warn (default, ``kanban.enforce_known_assignee=False``):
   ``create_task`` with an assignee that neither maps to an on-disk profile
   nor sits in ``KNOWN_ASSIGNEE_ALIASES`` STILL emits a typed
   ``assignee_unknown`` task event (so dashboards see the routing gap), but
   the task lands with its normally-computed status. This preserves existing
   test fixtures that use synthetic assignees like ``alice``/``bob``.

2. Hard-enforce (``kanban.enforce_known_assignee=True``):
   The same call additionally routes the task into ``blocked`` regardless of
   the caller's requested ``initial_status``. Dashboards / operators see the
   task on the blocked lane with the ``assignee_unknown`` event as the
   ``block-with-reason`` payload.

3. Known-good assignees (``default`` alias or a real profile) never emit the
   event and never trigger the block path.
"""
from __future__ import annotations

import json
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME + clean kanban DB, mirroring the pattern in
    ``test_kanban_default_assignee.py``.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_unknown_assignee_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id ASC",
        (task_id, kind),
    ).fetchall()
    out = []
    for r in rows:
        pl = r["payload"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
        out.append(json.loads(pl) if pl else {})
    return out


def test_unknown_assignee_emits_event_and_soft_warns(
    isolated_kanban_home, monkeypatch, caplog
):
    """FIX-8 (1/3) — default flag OFF: task lands ``ready``, but the
    ``assignee_unknown`` typed event is emitted so the dashboard can surface
    the routing gap without silently dropping the write.
    """
    kb, _ = isolated_kanban_home
    # Force ``profile_exists`` to reject the synthetic assignee so we exercise
    # the unknown-assignee branch even though the test-tmp HERMES_HOME has no
    # profile registry populated.
    from hermes_cli import profiles as _profiles
    monkeypatch.setattr(_profiles, "profile_exists", lambda name: False)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            task_id = kb.create_task(
                conn, title="soft-warn task", assignee="nonexistent-profile",
            )

    # (a) event emitted
    with kb.connect_closing() as conn:
        events = _events(conn, task_id, "assignee_unknown")
    assert len(events) == 1, f"expected exactly 1 assignee_unknown event, got {events}"
    payload = events[0]
    assert payload["proposed"] == "nonexistent-profile"
    assert isinstance(payload["known_profiles"], list)
    assert "reason" in payload
    assert payload["enforce_flag"] is False

    # (b) task status is normal ready (flag off = no forced block)
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["status"] == "ready"
    assert row["assignee"] == "nonexistent-profile"

    # (c) warning logged
    assert any(
        "unknown assignee" in rec.getMessage() for rec in caplog.records
    ), "expected 'unknown assignee' warning in log records"


def test_unknown_assignee_hard_enforce_blocks_task(
    isolated_kanban_home, monkeypatch
):
    """FIX-8 (2/3) — with ``kanban.enforce_known_assignee=True``, the same
    unknown assignee lands the task in ``blocked`` regardless of the
    caller's requested ``initial_status``. The ``assignee_unknown`` event
    is emitted alongside for the block-with-reason payload.
    """
    kb, _ = isolated_kanban_home
    from hermes_cli import profiles as _profiles
    monkeypatch.setattr(_profiles, "profile_exists", lambda name: False)
    # Flip the flag by monkeypatching the helper — cheaper and more explicit
    # than round-tripping through config.yaml on disk.
    monkeypatch.setattr(kb, "_kanban_enforce_known_assignee", lambda: True)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(
            conn, title="hard-enforce task", assignee="nonexistent-profile",
        )

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        events = _events(conn, task_id, "assignee_unknown")
    assert row["status"] == "blocked", (
        f"hard-enforce flag ON should route unknown-assignee tasks to blocked, "
        f"got status={row['status']!r}"
    )
    assert len(events) == 1
    assert events[0]["enforce_flag"] is True


def test_known_assignee_does_not_emit_event(isolated_kanban_home, monkeypatch):
    """FIX-8 (3/3) — a known assignee (``default`` alias) never emits
    ``assignee_unknown`` and lands in the normally-computed status.
    """
    kb, _ = isolated_kanban_home

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(
            conn, title="good task", assignee="default",
        )

    with kb.connect_closing() as conn:
        events = _events(conn, task_id, "assignee_unknown")
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert events == [], "known assignee should not emit assignee_unknown"
    assert row["status"] == "ready"
    assert row["assignee"] == "default"


def test_unassigned_task_does_not_emit_event(isolated_kanban_home):
    """Unassigned tasks (assignee=None) must not trip the unknown-assignee
    check — they're handled by the separate ``kanban.default_assignee``
    dispatcher path (see test_kanban_default_assignee.py).
    """
    kb, _ = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="unassigned", assignee=None)
    with kb.connect_closing() as conn:
        events = _events(conn, task_id, "assignee_unknown")
    assert events == []
