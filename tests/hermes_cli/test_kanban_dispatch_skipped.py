"""Tests for FIX-9 (t_7fa94b1b): ``dispatch_skipped`` event emission
for silent-skip cases + stall-watchdog trigger integration.

Covers the DoD from t_7fa94b1b:

1. Ready ticket with a nonexistent-profile assignee → run
   ``dispatch_once`` → ``dispatch_skipped`` event with
   ``reason="unknown_profile"`` is emitted.
2. Rate-limiting — three consecutive ``dispatch_once`` calls should
   emit exactly one event.
3. ``dispatch_skipped`` is a trigger kind for the stall-watchdog
   (:mod:`hermes_cli.kanban_stall_watchdog`).
4. Integration: an unclaimable ticket that has aged past ``min_age_s``
   with a ``dispatch_skipped`` event in the recent window auto-escalates
   to ``blocked`` (kind ``needs_input``).

We use the same isolated-HERMES_HOME fixture pattern as the existing
kanban tests so the schema init path is identical to production.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_stall_watchdog as sw


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty default-board kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Silence the completion-theater warning (2026-08-22 RCA): the kanban
    # helpers refuse to trust HERMES_HOME=/tmp/... unless HERMES_KANBAN_HOME
    # is set too, to prevent test writes leaking into the real board root.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _open(kanban_home):
    return kb.connect()


def _events(conn, task_id, kind=None):
    if kind is None:
        rows = conn.execute(
            "SELECT id, kind, payload, created_at FROM task_events "
            "WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, payload, created_at FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id ASC",
            (task_id, kind),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "payload": json.loads(r["payload"]) if r["payload"] else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _reject_all_profiles(monkeypatch):
    """Pretend ``profile_exists`` says no to every assignee.

    This is the shape of the ``unknown_profile`` case — the assignee is
    a legitimate task lane but not a real Hermes profile, so the
    dispatcher must skip and now must emit a ``dispatch_skipped`` event.

    NOTE: FIX-8 also emits an ``assignee_unknown`` event at
    ``create_task`` time. We tolerate that pre-emit — this suite only
    asserts on ``dispatch_skipped`` events. The soft-warn default
    (``kanban.enforce_known_assignee=False``) means the task still
    lands ``ready`` so the dispatcher path we're testing runs.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)


# ---------------------------------------------------------------------------
# DoD 1: unknown_profile emits dispatch_skipped
# ---------------------------------------------------------------------------


def test_unknown_profile_emits_dispatch_skipped(kanban_home, monkeypatch):
    """A ready ticket whose assignee is not a real Hermes profile must
    produce a ``dispatch_skipped`` event with ``reason="unknown_profile"``
    on the first dispatch tick.

    Previously the dispatcher just bucketed the row into
    ``skipped_nonspawnable`` and moved on with no per-task signal,
    so the stall-watchdog (FIX-6) never saw a trigger."""
    _reject_all_profiles(monkeypatch)
    conn = _open(kanban_home)
    tid = kb.create_task(conn, title="ghost-profile", assignee="nonexistent-profile")

    # Spawn function must never be called — the ticket is filtered
    # before claim/spawn. If it fires, our filter path regressed.
    def _boom(task, workspace, board=None):  # pragma: no cover - guard only
        raise AssertionError("spawn_fn must not be called for unknown_profile")

    result = kb.dispatch_once(conn, spawn_fn=_boom)

    assert tid in result.skipped_nonspawnable
    skipped_events = _events(conn, tid, kind="dispatch_skipped")
    assert len(skipped_events) == 1
    payload = skipped_events[0]["payload"]
    assert payload["reason"] == "unknown_profile"
    assert payload["assignee"] == "nonexistent-profile"
    assert "sweep_run" in payload


def test_unassigned_task_emits_dispatch_skipped(kanban_home, monkeypatch):
    """A ready ticket with NO assignee and no ``kanban.default_assignee``
    is silently skipped today. FIX-9 makes that silence visible."""
    # Profile existence is irrelevant here — we never reach the
    # profile-exists check because the assignee is empty. Guard against
    # accidental short-circuit differences by patching anyway.
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    conn = _open(kanban_home)
    tid = kb.create_task(conn, title="unassigned")  # no assignee

    result = kb.dispatch_once(conn)

    assert tid in result.skipped_unassigned
    evts = _events(conn, tid, kind="dispatch_skipped")
    assert len(evts) == 1
    payload = evts[0]["payload"]
    assert payload["reason"] == "unassigned"
    assert "assignee" not in payload or payload["assignee"] is None


# ---------------------------------------------------------------------------
# DoD 2: rate-limiting — 3 dispatch_once calls emit 1 event
# ---------------------------------------------------------------------------


def test_dispatch_skipped_is_rate_limited(kanban_home, monkeypatch):
    """Three consecutive ``dispatch_once`` calls against the same
    unclaimable ticket must produce exactly ONE ``dispatch_skipped``
    event — otherwise a stuck ticket at a 60-second tick would write
    ~1440 rows/day to ``task_events`` (see FIX-9 rate-limit rationale)."""
    _reject_all_profiles(monkeypatch)
    conn = _open(kanban_home)
    tid = kb.create_task(conn, title="rate-limited", assignee="ghost")

    for _ in range(3):
        kb.dispatch_once(conn)

    evts = _events(conn, tid, kind="dispatch_skipped")
    assert len(evts) == 1, (
        f"expected exactly 1 rate-limited event, got {len(evts)}: "
        f"{[e['payload'] for e in evts]}"
    )


def test_dispatch_skipped_re_emits_after_rate_limit_window(kanban_home, monkeypatch):
    """After the rate-limit window (default 15m) passes, another skip
    tick MUST emit a fresh event so the stall-watchdog keeps seeing a
    live trigger. Simulate the age-out by directly aging the existing
    event's ``created_at`` back past the window cutoff."""
    _reject_all_profiles(monkeypatch)
    conn = _open(kanban_home)
    tid = kb.create_task(conn, title="re-emit", assignee="ghost")

    kb.dispatch_once(conn)
    evts = _events(conn, tid, kind="dispatch_skipped")
    assert len(evts) == 1

    # Push the first event back beyond the rate-limit window.
    old_at = int(time.time()) - kb.DISPATCH_SKIPPED_RATE_LIMIT_S - 60
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE id = ?",
        (old_at, evts[0]["id"]),
    )
    conn.commit()

    kb.dispatch_once(conn)

    evts_after = _events(conn, tid, kind="dispatch_skipped")
    assert len(evts_after) == 2, (
        "expected a second event after the rate-limit window aged out"
    )


# ---------------------------------------------------------------------------
# DoD 3: stall-watchdog trigger predicate includes dispatch_skipped
# ---------------------------------------------------------------------------


def test_dispatch_skipped_is_a_stall_watchdog_trigger():
    """The stall-watchdog module MUST list ``dispatch_skipped`` as a
    trigger kind alongside ``claim_rejected`` and ``dependency_wait``.
    This is the wire between FIX-9 and FIX-6 — if it regresses,
    silent-skip tickets never age out into ``blocked``."""
    assert "dispatch_skipped" in sw._TRIGGER_KINDS
    # Existing triggers still present (FIX-6 must not regress).
    assert "claim_rejected" in sw._TRIGGER_KINDS
    assert "dependency_wait" in sw._TRIGGER_KINDS


# ---------------------------------------------------------------------------
# DoD 4: integration — dispatch_skipped ticket auto-escalates after 1h
# ---------------------------------------------------------------------------


def test_dispatch_skipped_ticket_auto_escalates_via_watchdog(
    kanban_home, monkeypatch,
):
    """End-to-end: create a ticket with a nonexistent-profile assignee,
    let the dispatcher emit ``dispatch_skipped``, age the ticket and
    the event past ``min_age_s`` / ``recent_window_s`` boundaries, and
    verify the stall-watchdog escalates the row into ``blocked`` with
    kind ``needs_input``.

    Simulates the operational bug FIX-9 fixes: an assignee typo lands
    a ticket in ``ready`` where it sits silently. With FIX-9 the
    dispatcher leaves a trail; with FIX-6+FIX-9 the watchdog closes
    the loop by surfacing the ticket on the blocked lane."""
    _reject_all_profiles(monkeypatch)
    conn = _open(kanban_home)
    now = 1_800_000_000
    tid = kb.create_task(conn, title="unclaimable", assignee="nonexistent-profile")

    # Tick 1: emit a dispatch_skipped event.
    kb.dispatch_once(conn)
    evts = _events(conn, tid, kind="dispatch_skipped")
    assert len(evts) == 1

    # Age the ticket + event so they satisfy min_age_s (1h) and are
    # still inside recent_window_s (1h). Ticket 2h old; event 10m ago
    # relative to ``now``.
    conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        (now - 7200, tid),
    )
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE id = ?",
        (now - 600, evts[0]["id"]),
    )
    conn.commit()

    result = sw.sweep_once(conn, now=now)

    assert result.escalated_count == 1
    esc = result.escalated[0]
    assert esc.task_id == tid
    assert esc.trigger_kind == "dispatch_skipped"
    assert esc.trigger_reason == "unknown_profile"

    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"


# ---------------------------------------------------------------------------
# Guardrail: dry_run must not write dispatch_skipped events.
# ---------------------------------------------------------------------------


def test_dry_run_dispatch_does_not_emit_dispatch_skipped(kanban_home, monkeypatch):
    """``dispatch_once(dry_run=True)`` is a read-only preview. FIX-9
    must not turn it into a writer — dry-run callers (CLI ``kanban
    dispatch --dry-run``, tests, dashboard previews) expect zero DB
    mutation."""
    _reject_all_profiles(monkeypatch)
    conn = _open(kanban_home)
    tid = kb.create_task(conn, title="dry-run", assignee="ghost")

    kb.dispatch_once(conn, dry_run=True)

    evts = _events(conn, tid, kind="dispatch_skipped")
    assert evts == [], (
        f"dry_run must not write dispatch_skipped events, got {evts}"
    )
