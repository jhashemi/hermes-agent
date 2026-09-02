"""Governance-gate alias tests for ``recompute_ready`` (wave 20260902b).

Incident class (production 2026-09-01, board executive-agents, task
``t_8a45c6e7``): an orchestrator deliberately blocked a card carrying an
operator WONTFIX gate by writing the row directly::

    UPDATE tasks SET status='blocked', block_kind='needs_input' ...
    _append_event(conn, tid, "task.blocked", {"reason": "FENCE ..."})

— i.e. the block landed under the ``task.blocked`` event alias with a
prose payload, and the row's ``block_kind`` column carried the governance
marker. Every auto-recovery guard in ``recompute_ready`` only inspects
canonical ``blocked`` events, so the next unrelated ``recompute_ready``
sweep (a completion elsewhere on the board) silently promoted the
operator-gated card back to ``ready`` — exposing a P0 operator-gated
security card to auto-dispatch (incident: bare ``promoted`` event 4139).

The block-recheck watchdog (FIX-7) already treats ``task.blocked`` as a
first-class deliberate-block alias (see ``kanban_block_recheck.py``
``_TRIGGER_KINDS``, incident wave 20260901g). These tests pin the same
contract on ``recompute_ready``'s guards:

* a ``blocked`` row whose most recent block-ish event is a
  ``task.blocked`` alias must NOT be auto-promoted (sticky);
* a row whose ``block_kind`` is a governance kind must NOT be
  auto-promoted while that gate is outstanding — status-agnostically,
  mirroring the t_93231838 lesson;
* ``unblock_task`` remains the single legitimate exit;
* breaker-driven auto-recovery (no deliberate block, no alias) is
  unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block paths can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _orchestrator_alias_block(conn, tid, reason):
    """Reproduce the production write: raw row flip + ``task.blocked`` alias.

    This is exactly how wave 20260901e/i fenced t_8a45c6e7 — no canonical
    ``blocked`` event is emitted, only the alias and the row columns.
    """
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=?",
            (tid,),
        )
        kb._append_event(
            conn, tid, "task.blocked",
            {"reason": reason, "wave": "test_20260902b"},
        )


# ---------------------------------------------------------------------------
# The incident: alias block on a blocked row must not auto-promote
# ---------------------------------------------------------------------------


def test_alias_block_keeps_governance_row_blocked(kanban_home: Path) -> None:
    """A task.blocked alias gate must survive an unrelated recompute_ready."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="operator-gated security card")
        _orchestrator_alias_block(conn, tid, "operator WONTFIX gate")
        assert kb.get_task(conn, tid).status == "blocked"

        # An unrelated completion somewhere else sweeps the board.
        kb.recompute_ready(conn)

        assert kb.get_task(conn, tid).status == "blocked", (
            "recompute_ready auto-promoted a deliberately alias-blocked "
            "governance card — incident 4139 regression"
        )


def test_alias_block_governance_gate_is_status_agnostic(
    kanban_home: Path,
) -> None:
    """Even if external SQL flips the row to todo, the gate must hold."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="operator-gated card")
        _orchestrator_alias_block(conn, tid, "operator gate")
        # External fix-up flips the row behind the guard's back.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='todo' WHERE id=?", (tid,)
            )

        kb.recompute_ready(conn)

        assert kb.get_task(conn, tid).status != "ready", (
            "governance gate (block_kind=needs_input + task.blocked alias) "
            "must hold even when row status was externally flipped"
        )


def test_unblock_task_remains_the_exit(kanban_home: Path) -> None:
    """A deliberate operator unblock releases the alias-gated card."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="operator-gated card")
        _orchestrator_alias_block(conn, tid, "operator gate")

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"

        # And recompute_ready must not fight the explicit unblock.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Regression guards: existing auto-recovery semantics must not regress
# ---------------------------------------------------------------------------


def test_breaker_blocked_row_without_gate_still_recovers(
    kanban_home: Path,
) -> None:
    """Circuit-breaker rows (no deliberate block, no alias) auto-recover."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="breaker victim")
        # Breaker-style block: raw status flip, no block-ish events at all.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked' WHERE id=?", (tid,)
            )

        kb.recompute_ready(conn)

        assert kb.get_task(conn, tid).status == "ready"


def test_canonical_block_event_still_sticky(kanban_home: Path) -> None:
    """Canonical block_task blocks stay sticky (pre-existing #28712)."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="canonical block")
        kb.block_task(conn, tid, reason="review-required", kind="needs_input")
        assert kb.get_task(conn, tid).status == "blocked"

        kb.recompute_ready(conn)

        assert kb.get_task(conn, tid).status == "blocked"