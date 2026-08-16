"""Tests for VFE-NERVE-01 typed cross-referencing on block/complete.

Covers the additive schema fields wired in for the "Nerves & Reflexes"
sunset arc:

* ``kanban_block``/`block_task` — ``waiting_for``, ``waiting_for_commit``,
  ``waiting_for_event``, ``waiting_for_condition``. ``waiting_for`` is
  validated against ``tasks.id``; a missing id raises
  :class:`MissingWaitingForError` and does NOT mutate task state (an
  auditable ``block_rejected_missing_waiting_for`` event is emitted).
* ``kanban_complete``/`complete_task` — ``unblocks`` (list),
  ``commit_hash``, ``test_run_id``. ``unblocks`` reuses the
  ``HallucinatedCardsError`` failure model: phantom ids block the
  completion; existing-but-not-blocked ids are recorded as a soft
  warning on the ``completed`` event.

The whole point is that a downstream auto-heal / rechecker loop
(VFE-NERVE-02..05) can query these fields directly instead of
regexing prose in ``summary``/``reason``. Backward compat matters as
much as the new capability, so several tests exercise the "no new
fields" call shape and assert nothing regressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``HERMES_HOME`` + a freshly-initialized kanban DB.

    Mirrors ``tests/hermes_cli/test_kanban_block_kinds.py`` so tests
    stay portable if either file moves.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _running_task(conn, title="t", assignee="worker"):
    """Create a task and drive it to ``running`` so ``block_task`` can act."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer=assignee)
    assert claimed is not None
    return tid


def _blocked_task(conn, title="dep", assignee="worker"):
    """Create a task and drive it to ``blocked`` (needs_input)."""
    tid = _running_task(conn, title=title, assignee=assignee)
    kb.block_task(conn, tid, reason="waiting on human", kind="needs_input")
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "blocked"
    return tid


def _payload_of(conn, tid, kind):
    """Return the payload of the most-recent event of ``kind`` for ``tid``."""
    events = [e for e in kb.list_events(conn, tid) if e.kind == kind]
    assert events, f"expected at least one {kind!r} event on {tid}"
    return events[-1].payload or {}


# ---------------------------------------------------------------------------
# block_task: waiting_for validation
# ---------------------------------------------------------------------------


def test_block_with_valid_waiting_for_persists_on_event(kanban_home: Path) -> None:
    """A valid ``waiting_for`` id lands on the emitted ``blocked`` event."""
    with kb.connect_closing() as conn:
        dep = _running_task(conn, title="dep")
        tid = _running_task(conn, title="waiter")
        ok = kb.block_task(
            conn, tid,
            reason="fix pending on dep",
            kind="needs_input",
            waiting_for=dep,
        )
        assert ok is True
        pl = _payload_of(conn, tid, "blocked")
        assert pl.get("waiting_for") == dep
        assert pl.get("kind") == "needs_input"


def test_block_with_phantom_waiting_for_raises_and_no_state_change(
    kanban_home: Path,
) -> None:
    """A phantom ``waiting_for`` id raises and leaves the task in ``running``."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        pre_status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"]
        assert pre_status == "running"
        with pytest.raises(kb.MissingWaitingForError) as exc:
            kb.block_task(
                conn, tid,
                reason="waiting on ghost",
                kind="needs_input",
                waiting_for="t_ghost0000",
            )
        assert "t_ghost0000" in exc.value.phantom
        # Task must NOT have been mutated.
        post_status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"]
        assert post_status == "running"


def test_block_phantom_waiting_for_emits_rejection_event(kanban_home: Path) -> None:
    """The rejection is auditable — an event row must exist."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(kb.MissingWaitingForError):
            kb.block_task(
                conn, tid,
                reason="ghost",
                kind="capability",
                waiting_for="t_nope",
            )
        pl = _payload_of(conn, tid, "block_rejected_missing_waiting_for")
        assert pl.get("waiting_for") == "t_nope"
        assert pl.get("waiting_for_missing") == ["t_nope"]
        assert pl.get("kind") == "capability"


def test_block_without_waiting_for_backward_compat(kanban_home: Path) -> None:
    """Existing callsites (no new kwargs) behave exactly as before."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        ok = kb.block_task(conn, tid, reason="need input", kind="needs_input")
        assert ok is True
        pl = _payload_of(conn, tid, "blocked")
        # None of the new keys should be present on a call that didn't
        # supply them — no None-filled noise leaks into the payload.
        for key in (
            "waiting_for",
            "waiting_for_commit",
            "waiting_for_event",
            "waiting_for_condition",
        ):
            assert key not in pl


# ---------------------------------------------------------------------------
# block_task: other typed fields (opaque strings — no cross-ref validation)
# ---------------------------------------------------------------------------


def test_block_with_all_four_waiting_fields_persists(kanban_home: Path) -> None:
    """All four typed fields survive the round trip to the event payload."""
    with kb.connect_closing() as conn:
        dep = _running_task(conn, title="dep")
        tid = _running_task(conn, title="waiter")
        ok = kb.block_task(
            conn, tid,
            reason="all four fields",
            kind="needs_input",
            waiting_for=dep,
            waiting_for_commit="abc1234",
            waiting_for_event="spine.gate.wave2_cleared",
            waiting_for_condition="p95 < 200ms for 10m",
        )
        assert ok is True
        pl = _payload_of(conn, tid, "blocked")
        assert pl["waiting_for"] == dep
        assert pl["waiting_for_commit"] == "abc1234"
        assert pl["waiting_for_event"] == "spine.gate.wave2_cleared"
        assert pl["waiting_for_condition"] == "p95 < 200ms for 10m"


def test_block_commit_event_condition_without_waiting_for(kanban_home: Path) -> None:
    """The three non-ticket fields work without ``waiting_for`` (no gate)."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        ok = kb.block_task(
            conn, tid,
            reason="pure event wait",
            kind="capability",
            waiting_for_event="ci.pipeline.green",
        )
        assert ok is True
        pl = _payload_of(conn, tid, "blocked")
        assert pl["waiting_for_event"] == "ci.pipeline.green"
        assert "waiting_for" not in pl


def test_block_dependency_kind_persists_handoff_on_dependency_wait(
    kanban_home: Path,
) -> None:
    """A ``dependency``-kind block routes to ``todo`` and emits a
    ``dependency_wait`` event that still carries the typed handoff.
    """
    with kb.connect_closing() as conn:
        dep = _running_task(conn, title="dep")
        tid = _running_task(conn, title="waiter")
        ok = kb.block_task(
            conn, tid,
            reason="parent-gated",
            kind="dependency",
            waiting_for=dep,
        )
        assert ok is True
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["status"] == "todo"
        pl = _payload_of(conn, tid, "dependency_wait")
        assert pl.get("waiting_for") == dep
        assert pl.get("kind") == "dependency"


def test_block_empty_string_waiting_for_treated_as_absent(kanban_home: Path) -> None:
    """An empty/whitespace ``waiting_for`` is coerced to None — no gate, no key."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        ok = kb.block_task(
            conn, tid,
            reason="empty string",
            kind="needs_input",
            waiting_for="   ",
        )
        assert ok is True
        pl = _payload_of(conn, tid, "blocked")
        assert "waiting_for" not in pl


# ---------------------------------------------------------------------------
# complete_task: unblocks validation
# ---------------------------------------------------------------------------


def test_complete_with_valid_unblocks_records_on_event(kanban_home: Path) -> None:
    """``unblocks`` ids that exist AND are ``blocked`` land under the
    ``unblocks`` payload key (not ``unblocks_not_blocked``).
    """
    with kb.connect_closing() as conn:
        blocked_a = _blocked_task(conn, title="a")
        blocked_b = _blocked_task(conn, title="b")
        # The completing worker is a separate task.
        fixer = _running_task(conn, title="fixer", assignee="fixer")
        ok = kb.complete_task(
            conn, fixer,
            summary="fixed the shared bug",
            unblocks=[blocked_a, blocked_b],
        )
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        assert set(pl.get("unblocks", [])) == {blocked_a, blocked_b}
        assert "unblocks_not_blocked" not in pl


def test_complete_with_phantom_unblocks_raises_hallucinated_cards(
    kanban_home: Path,
) -> None:
    """A phantom id in ``unblocks`` raises ``HallucinatedCardsError`` and
    the task is NOT marked done.
    """
    with kb.connect_closing() as conn:
        fixer = _running_task(conn, title="fixer")
        with pytest.raises(kb.HallucinatedCardsError) as exc:
            kb.complete_task(
                conn, fixer,
                summary="claiming to fix a ghost",
                unblocks=["t_ghost0000"],
            )
        assert "t_ghost0000" in exc.value.phantom
        status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (fixer,)
        ).fetchone()["status"]
        assert status == "running"
        # And a completion_blocked_hallucination event was emitted with
        # the new ``unblocks_phantom`` marker.
        pl = _payload_of(conn, fixer, "completion_blocked_hallucination")
        assert pl.get("unblocks_phantom") == ["t_ghost0000"]


def test_complete_with_unblocks_not_blocked_records_soft_warning(
    kanban_home: Path,
) -> None:
    """Ids that exist but aren't in blocked/todo land under
    ``unblocks_not_blocked`` — non-fatal, completion still succeeds.
    """
    with kb.connect_closing() as conn:
        # A task that's ``running`` — exists but not blockable.
        running_dep = _running_task(conn, title="already-running")
        # A truly blocked task.
        real_blocked = _blocked_task(conn, title="real")
        fixer = _running_task(conn, title="fixer", assignee="fixer")
        ok = kb.complete_task(
            conn, fixer,
            summary="one real, one warning",
            unblocks=[real_blocked, running_dep],
        )
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        assert pl.get("unblocks") == [real_blocked]
        assert pl.get("unblocks_not_blocked") == [running_dep]


def test_complete_unblocks_dedupes_and_preserves_order(kanban_home: Path) -> None:
    """Duplicate ids in ``unblocks`` collapse to one, order preserved."""
    with kb.connect_closing() as conn:
        a = _blocked_task(conn, title="a")
        b = _blocked_task(conn, title="b")
        fixer = _running_task(conn, title="fixer", assignee="fixer")
        ok = kb.complete_task(
            conn, fixer,
            summary="dedupe",
            unblocks=[a, b, a, b, a],
        )
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        assert pl.get("unblocks") == [a, b]


def test_complete_with_commit_hash_and_test_run_id(kanban_home: Path) -> None:
    """``commit_hash`` and ``test_run_id`` are plain strings that survive."""
    with kb.connect_closing() as conn:
        fixer = _running_task(conn, title="fixer")
        ok = kb.complete_task(
            conn, fixer,
            summary="fix landed",
            commit_hash="deadbeef",
            test_run_id="gha-12345",
        )
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        assert pl.get("commit_hash") == "deadbeef"
        assert pl.get("test_run_id") == "gha-12345"


def test_complete_without_new_fields_backward_compat(kanban_home: Path) -> None:
    """Existing callsites (no new kwargs) don't leak new keys into the payload."""
    with kb.connect_closing() as conn:
        fixer = _running_task(conn, title="fixer")
        ok = kb.complete_task(conn, fixer, summary="classic completion")
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        for key in (
            "unblocks",
            "unblocks_not_blocked",
            "commit_hash",
            "test_run_id",
        ):
            assert key not in pl


def test_complete_empty_unblocks_list_is_noop(kanban_home: Path) -> None:
    """An explicit empty ``unblocks=[]`` behaves like omitting the kwarg."""
    with kb.connect_closing() as conn:
        fixer = _running_task(conn, title="fixer")
        ok = kb.complete_task(
            conn, fixer,
            summary="empty list",
            unblocks=[],
        )
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        assert "unblocks" not in pl


def test_complete_unblocks_and_created_cards_independent(kanban_home: Path) -> None:
    """``unblocks`` validation is independent from ``created_cards`` —
    a phantom in one doesn't false-positive the other.
    """
    with kb.connect_closing() as conn:
        blocked = _blocked_task(conn, title="real-blocked")
        fixer = _running_task(conn, title="fixer", assignee="fixer")
        # Invent a card id via kanban_create so it's genuinely "created
        # by" the fixer's assignee.
        child = kb.create_task(
            conn, title="child", assignee="fixer",
            created_by="fixer",
        )
        ok = kb.complete_task(
            conn, fixer,
            summary="both fields",
            created_cards=[child],
            unblocks=[blocked],
        )
        assert ok is True
        pl = _payload_of(conn, fixer, "completed")
        assert pl.get("verified_cards") == [child]
        assert pl.get("unblocks") == [blocked]


# ---------------------------------------------------------------------------
# Cross-cutting: waiting_for → unblocks handshake end-to-end
# ---------------------------------------------------------------------------


def test_block_then_complete_end_to_end_handshake(kanban_home: Path) -> None:
    """The full handshake: A blocks on B via ``waiting_for``; B completes
    with ``unblocks=[A]``. Both event payloads carry the structured
    fields so a rechecker loop can pair them without prose parsing.
    """
    with kb.connect_closing() as conn:
        dep = _running_task(conn, title="dep-fix", assignee="fixer")
        waiter = _running_task(conn, title="waiter", assignee="waiter")

        # 1. Waiter blocks on dep with structured waiting_for.
        assert kb.block_task(
            conn, waiter,
            reason="waiting on dep-fix commit",
            kind="needs_input",
            waiting_for=dep,
            waiting_for_commit="abc1234",
        )
        block_pl = _payload_of(conn, waiter, "blocked")
        assert block_pl["waiting_for"] == dep
        assert block_pl["waiting_for_commit"] == "abc1234"

        # 2. Dep completes claiming to unblock waiter.
        assert kb.complete_task(
            conn, dep,
            summary="fix committed",
            unblocks=[waiter],
            commit_hash="abc1234",
        )
        comp_pl = _payload_of(conn, dep, "completed")
        assert comp_pl["unblocks"] == [waiter]
        assert comp_pl["commit_hash"] == "abc1234"

        # 3. Cross-reference works: the commit hash the waiter was
        #    waiting for matches the one dep just landed.
        assert block_pl["waiting_for_commit"] == comp_pl["commit_hash"]


def test_block_kind_validation_still_enforced_with_new_kwargs(
    kanban_home: Path,
) -> None:
    """The pre-existing ``kind`` validation is unaffected by the new kwargs."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError, match="block kind must be one of"):
            kb.block_task(
                conn, tid,
                reason="bad kind",
                kind="not-a-real-kind",
                waiting_for=None,
            )


def test_missing_waiting_for_error_is_value_error_subclass(kanban_home: Path) -> None:
    """Callers with a generic ``except ValueError`` still catch the new
    exception — the failure mode is the same shape as
    ``HallucinatedCardsError``.
    """
    assert issubclass(kb.MissingWaitingForError, ValueError)
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        try:
            kb.block_task(
                conn, tid,
                reason="x",
                kind="capability",
                waiting_for="t_no_such_task",
            )
        except ValueError as e:  # generic catch
            assert isinstance(e, kb.MissingWaitingForError)
            assert e.phantom == ["t_no_such_task"]
        else:
            pytest.fail("expected MissingWaitingForError")
