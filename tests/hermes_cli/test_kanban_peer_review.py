"""FIX-5 peer-review routing primitive (t_b56c4ca7).

Kills the "waiting on jeff" limbo by making peer review a first-class
routing directive on the task row instead of a comment convention.

The primitive is three parts:

1. ``tasks.peer_review_assignee`` — nullable column naming the reviewer.
2. ``kanban_complete(pending_peer_review=True)`` transitions
   ``running|ready → review`` instead of ``running → done``, stashes the
   reviewer on the row (from arg or from the row itself), and preserves
   the original ``assignee`` so a fail verdict can bounce cleanly back.
3. The dispatcher's review-column pass consults ``peer_review_assignee``
   in preference to ``assignee``, so the NEXT tick spawns the reviewer
   named on the row. The reviewer's completion carries
   ``review_verdict='pass'|'fail'`` — pass → done; fail → back to
   ``ready`` for the original assignee.

Anti-regression: none of this fires on plain completions
(``pending_peer_review=False``, ``review_verdict=None``), so pre-FIX-5
kanban flows keep working exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME for a fresh kanban DB per test.

    Same pattern as test_kanban_block_kinds — HERMES_HOME + Path.home
    monkeypatched so no test bleeds into a live board. The kanban DB env
    (HERMES_KANBAN_DB / HERMES_KANBAN_TASK) is also cleared so the
    dispatcher's board resolution can't reach past the fixture.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _mark_ready(conn, tid: str) -> None:
    """Kick a freshly-created task from ``running`` → ``ready``.

    ``create_task`` defaults to ``initial_status='running'`` when the
    caller doesn't pass one, but the dispatcher only spawns from
    ``ready``. Tests use this helper to drive the dispatcher pass without
    faking a claim.
    """
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (tid,)
        )


def _claim_running(conn, tid: str, claimer: str) -> None:
    """Move a task from ``ready`` → ``running`` under a named claimer.

    This mirrors what the dispatcher does on a live tick without
    depending on the actual dispatch loop; each test controls exactly
    which profile is "running" before it exercises ``complete_task``.
    """
    _mark_ready(conn, tid)
    claimed = kb.claim_task(conn, tid, claimer=claimer)
    assert claimed is not None, f"claim_task returned None for {tid}"


# ---------------------------------------------------------------------------
# Schema + Task dataclass wiring
# ---------------------------------------------------------------------------


def test_schema_has_peer_review_assignee_column(kanban_home: Path) -> None:
    """DoD #1 — ``tasks.peer_review_assignee`` exists as a nullable TEXT.

    Belt-and-suspenders: verifies both the fresh-SCHEMA_SQL and the
    ``_migrate_add_optional_columns`` paths land the same column shape.
    """
    with kb.connect_closing() as conn:
        cols = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(tasks)")
        }
        assert "peer_review_assignee" in cols, (
            "tasks table missing peer_review_assignee — schema migration "
            "did not run"
        )
        col = cols["peer_review_assignee"]
        assert col["type"].upper() == "TEXT"
        # Nullable — legacy rows must not be forced to populate it.
        assert col["notnull"] == 0


def test_create_task_persists_peer_review_assignee(kanban_home: Path) -> None:
    """``create_task(peer_review_assignee=...)`` persists the reviewer.

    Anti-regression: workers coordinating peer review across sibling
    profiles should be able to declare the reviewer at task creation
    time (before any worker has claimed the ticket), so the routing
    directive lives on the row rather than in a comment.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="peer-review-declared-up-front",
            assignee="worker-alice",
            peer_review_assignee="reviewer-bob",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.peer_review_assignee == "reviewer-bob"
        # created event carries the reviewer too so board history stays
        # queryable without touching the row.
        events = kb.list_events(conn, tid)
        created = [e for e in events if e.kind == "created"]
        assert created, "no created event emitted"
        assert created[0].payload.get("peer_review_assignee") == "reviewer-bob"


# ---------------------------------------------------------------------------
# pending_peer_review handoff
# ---------------------------------------------------------------------------


def test_pending_peer_review_transitions_running_to_review(
    kanban_home: Path,
) -> None:
    """DoD #2 setup: ``pending_peer_review=True`` moves task to ``review``.

    * status is ``review`` (not ``done``).
    * ``peer_review_assignee`` is populated from the completion arg.
    * ``assignee`` is UNCHANGED — a fail verdict must bounce back to the
      original worker, so the column can't be rewritten.
    * ``completed_at`` is NOT set (the task isn't done yet).
    * A ``pending_peer_review`` event carries the reviewer + summary.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="worker-hands-off-to-review",
            assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")

        ok = kb.complete_task(
            conn, tid,
            summary="fix landed on branch feat/x — please review",
            pending_peer_review=True,
            peer_review_assignee="reviewer-bob",
        )
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "review", (
            f"expected status=review, got {task.status!r}"
        )
        assert task.peer_review_assignee == "reviewer-bob"
        assert task.assignee == "worker-alice", (
            "original assignee must be preserved so fail-verdict can bounce back"
        )
        assert task.completed_at is None, (
            "task is not done yet — completed_at must stay NULL"
        )
        # Claim state is cleared so the dispatcher's review pass can
        # actually pick it up next tick (it filters on claim_lock IS NULL).
        assert task.claim_lock is None

        # Event ledger: the handoff produces pending_peer_review, NOT
        # completed. If we emit "completed" here downstream automation
        # (notifiers, gateway) will mark the task done in their own store.
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "pending_peer_review" in kinds, (
            f"expected pending_peer_review event, saw {kinds}"
        )
        assert "completed" not in kinds, (
            "must NOT emit 'completed' — task is still in flight"
        )
        pending_ev = [e for e in events if e.kind == "pending_peer_review"][-1]
        assert pending_ev.payload["reviewer"] == "reviewer-bob"
        assert "please review" in (pending_ev.payload.get("summary") or "")


def test_pending_peer_review_uses_row_reviewer_when_arg_omitted(
    kanban_home: Path,
) -> None:
    """Row-level ``peer_review_assignee`` is honoured when the completion
    call omits it — declaration-then-handoff is a valid pattern (e.g.
    the CLI or a parent task sets the reviewer at creation time).
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="row-declared-reviewer",
            assignee="worker-alice",
            peer_review_assignee="reviewer-bob",
        )
        _claim_running(conn, tid, "worker-alice")

        ok = kb.complete_task(
            conn, tid, summary="done, please review",
            pending_peer_review=True,
        )  # <-- no peer_review_assignee arg
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "review"
        assert task.peer_review_assignee == "reviewer-bob"


def test_pending_peer_review_without_reviewer_falls_through(
    kanban_home: Path,
) -> None:
    """Stray ``pending_peer_review=True`` on a task that never scoped a
    reviewer must NOT strand the ticket in review forever. Instead the
    completion degrades to the normal ``done`` path so the ledger is
    consistent.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="no-reviewer-declared",
            assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")

        ok = kb.complete_task(
            conn, tid, summary="all done",
            pending_peer_review=True,
        )  # no reviewer anywhere → treat as plain done
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert task.peer_review_assignee is None


# ---------------------------------------------------------------------------
# Dispatcher spawns the reviewer (DoD #2)
# ---------------------------------------------------------------------------


def _stub_spawner() -> Tuple[Callable, List[Tuple[str, str]]]:
    """Return a ``spawn_fn`` that records (task_id, assignee) instead of
    forking a worker, plus the list it writes to.

    Kept in-file rather than a shared fixture because the test suite
    already uses this pattern; a local stub keeps the assertions
    inline-readable.
    """
    calls: List[Tuple[str, str]] = []

    def _spawn(task, workspace, **_kwargs):  # pragma: no cover - trivial
        calls.append((task.id, task.assignee or ""))
        return 424242  # arbitrary non-zero pid so _set_worker_pid runs

    return _spawn, calls


def test_dispatcher_spawns_reviewer_from_peer_review_assignee(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD #2 — dispatcher recognizes ``status='review'`` and spawns the
    reviewer named in ``peer_review_assignee``, NOT the original
    ``assignee``.

    Uses a stub ``spawn_fn`` and a stub ``profile_exists`` so the test
    doesn't depend on real Hermes profiles being installed. What we're
    testing here is the dispatcher's routing decision, not the profile
    resolver.
    """
    import hermes_cli.profiles as prof_mod
    monkeypatch.setattr(prof_mod, "profile_exists", lambda name: True)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="review-column-routes-to-reviewer",
            assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")
        assert kb.complete_task(
            conn, tid, summary="ready for review",
            pending_peer_review=True, peer_review_assignee="reviewer-bob",
        )
        # Sanity: row is in review with reviewer set.
        pre = kb.get_task(conn, tid)
        assert pre is not None and pre.status == "review"

        spawn_fn, calls = _stub_spawner()
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn)

        # The dispatch tick spawned exactly this task, addressed to the
        # reviewer profile — this is the whole point of the primitive.
        assert calls == [(tid, "reviewer-bob")], (
            f"expected spawn for reviewer-bob, got {calls}; dispatch={result}"
        )
        # The row itself now sees the reviewer as the claimer (via
        # claim_review_task), status='running', but ``assignee`` is
        # untouched — original worker preserved for a possible bounce.
        post = kb.get_task(conn, tid)
        assert post is not None
        assert post.status == "running"
        assert post.assignee == "worker-alice"
        assert post.peer_review_assignee == "reviewer-bob"


# ---------------------------------------------------------------------------
# Reviewer verdict — pass (DoD #3, DoD #4 happy path)
# ---------------------------------------------------------------------------


def test_review_verdict_pass_transitions_to_done(kanban_home: Path) -> None:
    """DoD #3 (pass) — reviewer's ``review_verdict='pass'`` transitions
    the task from ``running`` (a review-run) to ``done`` and clears the
    reviewer routing on the row.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="reviewer-signs-off", assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")
        kb.complete_task(
            conn, tid, summary="ready",
            pending_peer_review=True, peer_review_assignee="reviewer-bob",
        )
        # Move to review-run: reviewer claims via the review-column path.
        claimed = kb.claim_review_task(conn, tid, claimer="reviewer-bob")
        assert claimed is not None

        ok = kb.complete_task(
            conn, tid,
            summary="LGTM — verified fix landed on master",
            review_verdict="pass",
        )
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert task.peer_review_assignee is None, (
            "verdict=pass must clear peer_review_assignee — a second review "
            "pass has to be explicitly re-scoped"
        )
        assert task.assignee == "worker-alice", (
            "original assignee stays on the row for audit"
        )
        assert task.completed_at is not None
        events = kb.list_events(conn, tid)
        done_evs = [e for e in events if e.kind == "completed"]
        assert done_evs, "expected a completed event on verdict=pass"
        assert done_evs[-1].payload.get("review_verdict") == "pass"


# ---------------------------------------------------------------------------
# Reviewer verdict — fail (bounces back)
# ---------------------------------------------------------------------------


def test_review_verdict_fail_bounces_to_ready_for_original(
    kanban_home: Path,
) -> None:
    """DoD #3 (fail) — reviewer's ``review_verdict='fail'`` returns the
    task to ``ready`` with the ORIGINAL assignee, clears the reviewer,
    emits a ``review_rejected`` event, and does NOT mark the task done.

    This is the crucial anti-regression case: rejection must NOT
    accidentally complete the task (which would defeat the whole
    primitive), NOR permanently strand it (which would recreate the
    limbo we set out to kill).
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="reviewer-rejects", assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")
        kb.complete_task(
            conn, tid, summary="ready",
            pending_peer_review=True, peer_review_assignee="reviewer-bob",
        )
        assert kb.claim_review_task(conn, tid, claimer="reviewer-bob") is not None

        ok = kb.complete_task(
            conn, tid,
            summary="regression in test_x — needs work on branch feat/y",
            metadata={"failing_test": "test_x", "rerun_after": "commit ABC"},
            review_verdict="fail",
        )
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready", (
            f"expected ready (bounce), got {task.status!r}"
        )
        assert task.assignee == "worker-alice", (
            "fail must respawn the original worker, not the reviewer"
        )
        assert task.peer_review_assignee is None
        assert task.completed_at is None
        assert task.claim_lock is None

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "review_rejected" in kinds
        assert "completed" not in kinds, (
            "verdict=fail must NOT emit a completed event"
        )
        rejected = [e for e in events if e.kind == "review_rejected"][-1]
        assert rejected.payload.get("review_verdict") == "fail"
        assert "regression" in (rejected.payload.get("summary") or "")


def test_review_verdict_invalid_raises(kanban_home: Path) -> None:
    """Typo protection: unknown verdict strings must raise so callers
    don't silently fall through to a plain done. The whole point of the
    verdict is a strict two-way gate.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="v", assignee="worker-alice")
        _claim_running(conn, tid, "worker-alice")
        with pytest.raises(ValueError, match="review_verdict"):
            kb.complete_task(
                conn, tid, summary="…", review_verdict="approve",  # not pass/fail
            )


# ---------------------------------------------------------------------------
# End-to-end: worker → complete-pending → dispatcher → reviewer → done
# (DoD #4 in one pipe)
# ---------------------------------------------------------------------------


def test_e2e_round_trip_worker_to_reviewer_to_done(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD #4 — full happy-path round trip.

    Sequence exercised end-to-end:
      1. Worker creates the task and completes it with
         ``pending_peer_review=True, peer_review_assignee=reviewer``.
      2. Dispatcher tick spawns the REVIEWER (not the worker) — verified
         by asserting on the stub spawn_fn's captured assignee.
      3. Reviewer claims via ``claim_review_task`` and completes with
         ``review_verdict='pass'``.
      4. Terminal state: ``status=done``, ``peer_review_assignee=None``,
         ``assignee`` preserved as the original worker, and the event
         ledger tells the full story (created → pending_peer_review →
         claimed → completed with review_verdict=pass).
    """
    import hermes_cli.profiles as prof_mod
    monkeypatch.setattr(prof_mod, "profile_exists", lambda name: True)

    with kb.connect_closing() as conn:
        # Step 1: worker creates + hands off.
        tid = kb.create_task(
            conn, title="e2e-round-trip", assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")
        assert kb.complete_task(
            conn, tid,
            summary="fix on branch feat/e2e — please review",
            pending_peer_review=True, peer_review_assignee="reviewer-bob",
        )

        # Step 2: dispatcher tick spawns the reviewer.
        spawn_fn, spawn_calls = _stub_spawner()
        kb.dispatch_once(conn, spawn_fn=spawn_fn)
        assert spawn_calls == [(tid, "reviewer-bob")], (
            f"dispatcher did not spawn reviewer — got {spawn_calls}"
        )

        # Step 3: reviewer signs off. In production the reviewer worker
        # would call kanban_complete; here we call the DB fn directly to
        # cover the same path that the tool handler hits.
        assert kb.complete_task(
            conn, tid,
            summary="verified locally, verdict pass",
            review_verdict="pass",
        )

        # Step 4: terminal state assertions.
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert task.peer_review_assignee is None
        assert task.assignee == "worker-alice", (
            "audit trail: original worker stays visible on the row"
        )
        assert task.completed_at is not None

        # Event narrative — the ledger tells the whole story.
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "created" in kinds
        # There should be a pending_peer_review (worker's handoff) and a
        # completed (reviewer's pass), in that order.
        assert kinds.index("pending_peer_review") < kinds.index("completed")
        final = [e for e in events if e.kind == "completed"][-1]
        assert final.payload.get("review_verdict") == "pass"


# ---------------------------------------------------------------------------
# Anti-regression: plain completions (no peer-review args) behave exactly
# as before FIX-5.
# ---------------------------------------------------------------------------


def test_plain_complete_task_unchanged(kanban_home: Path) -> None:
    """Backward compat: ``kanban_complete`` without any FIX-5 args must
    hit the pre-existing done path (no ``peer_review_assignee`` writes,
    no ``pending_peer_review`` event, terminal ``done`` in one call).

    Belt-and-suspenders because the FIX-5 branches sit BEFORE the main
    write txn in ``complete_task``; a regression there would silently
    break every non-peer-review completion on the board.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="plain-old-complete", assignee="worker-alice",
        )
        _claim_running(conn, tid, "worker-alice")
        assert kb.complete_task(
            conn, tid, summary="done — no peer review here",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert task.peer_review_assignee is None
        assert task.completed_at is not None
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "pending_peer_review" not in kinds
        assert "review_rejected" not in kinds
        assert "completed" in kinds
