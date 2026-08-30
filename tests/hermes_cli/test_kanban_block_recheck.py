"""Tests for the block-recheck watchdog (FIX-7 / t_d73124fb).

Covers the DoD for block-recheck:

1. Policy A — a ``gave_up`` ticket older than the cooldown auto-retries
   (blocked -> ready/todo) with a ``blocked_auto_retry_after_cooldown``
   audit event; consecutive_failures is reset (delegates to
   ``unblock_task``).
2. Policy A is idempotent: same trigger doesn't re-fire on a second
   sweep.
3. Policy A honours ``max_cycles`` — after N auto-retries the sweep
   stops touching the ticket, leaving it for a human.
4. Policy A does NOT fire before the cooldown elapses.
5. Policy B — a ``blocked`` ticket with a "memory below threshold"
   reason auto-unblocks when host resources cross above the threshold.
6. Policy B stays blocked when host resources are still below.
7. Policy C — an ISO time-gated ticket unblocks once wall clock >=
   release time, both for absolute ISO and relative T+Xh forms.
8. Policy C does NOT fire before the gate.
9. Policy D — a review-required ticket old enough (>= stale_s) gets an
   escalation event + comment but is NEVER unblocked.
10. Policy D is idempotent: same trigger doesn't spam comments.
11. Non-``blocked`` tickets are NEVER touched (running / ready / todo /
    triage / done / archived).
12. Tickets with no matching policy (Policy E) are counted as considered
    but never mutated.
13. ``dry_run=True`` produces the same action list without any DB
    mutation.
14. Review-required takes precedence over B/C matchers even when the
    reason mentions both.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_block_recheck as brc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty default-board kanban DB.

    Mirrors the fixture used across tests/hermes_cli/test_kanban_db.py
    and tests/hermes_cli/test_kanban_stall_watchdog.py so the schema
    init path is identical to production.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Guard: the dispatcher-set HERMES_KANBAN_DB env would override
    # HERMES_HOME and let tests write to the live board. Nuke it.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _open(kanban_home):
    return kb.connect()


def _make_blocked_ticket(
    conn,
    *,
    task_id: str,
    created_at: int,
    assignee: str = "backend-eng",
    title: str = "test",
    block_kind: str | None = None,
    consecutive_failures: int = 0,
) -> None:
    """Insert a raw ``blocked`` row directly into ``tasks``.

    We can't just call ``create_task`` + ``block_task`` because both
    have side effects (comments, run rows, events) that would pollute
    the audit trail we're trying to test against. Direct INSERT keeps
    each test fixture minimal + explicit.
    """
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, body, assignee, status, created_at, block_kind, "
            "consecutive_failures) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, "test body", assignee, "blocked", created_at,
             block_kind, consecutive_failures),
        )


def _append_trigger(
    conn,
    *,
    task_id: str,
    kind: str,
    created_at: int,
    payload: dict | None = None,
) -> int:
    """Insert a ``gave_up`` or ``blocked`` event with a specific age.

    Returns the new event id so tests can pin idempotency assertions.
    """
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (
                task_id, kind,
                json.dumps(payload) if payload else None,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return int(row["id"])


def _events(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
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


def _status(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row["status"] if row else ""


# ---------------------------------------------------------------------------
# Policy A — gave_up cooldown retry
# ---------------------------------------------------------------------------


def test_policy_a_retries_gave_up_after_cooldown(kanban_home):
    """A ``gave_up`` ticket >= 15min old gets auto-retried: status flips
    off ``blocked`` (to ``ready`` when no parents, ``todo`` otherwise),
    consecutive_failures is reset, and a
    ``blocked_auto_retry_after_cooldown`` audit event fires."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_gave_up"

    _make_blocked_ticket(
        conn, task_id=task_id, created_at=now - 7200,
        consecutive_failures=3,
    )
    trigger_id = _append_trigger(
        conn, task_id=task_id, kind="gave_up",
        created_at=now - 1000,  # > 900s cooldown
        payload={
            "failures": 3,
            "effective_limit": 3,
            "limit_source": "dispatcher",
            "error": "timeout",
            "trigger_outcome": "timed_out",
        },
    )

    result = brc.sweep_once(conn, now=now)

    assert result.considered == 1
    assert result.acted_count == 1
    assert result.unblocked_count == 1
    action = result.actions[0]
    assert action.task_id == task_id
    assert action.policy == "A"
    assert action.action == "unblocked"
    assert action.trigger_event_id == trigger_id

    # Task off ``blocked`` (no parents -> ready).
    assert _status(conn, task_id) == "ready"

    # consecutive_failures reset by unblock_task delegation.
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["consecutive_failures"] == 0

    # Audit event exists with typed payload.
    retry_evts = [
        e for e in _events(conn, task_id)
        if e["kind"] == "blocked_auto_retry_after_cooldown"
    ]
    assert len(retry_evts) == 1
    pl = retry_evts[0]["payload"]
    assert pl["policy"] == "A"
    assert pl["trigger_kind"] == "gave_up"
    assert pl["trigger_event_id"] == trigger_id
    assert pl["cycle"] == 1
    assert pl["max_cycles"] == brc.DEFAULT_GAVE_UP_MAX_CYCLES


def test_policy_a_is_idempotent(kanban_home):
    """Two sweeps against the same trigger fire Policy A exactly once."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_gave_up_idem"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="gave_up",
        created_at=now - 1000,
        payload={"error": "boom"},
    )

    brc.sweep_once(conn, now=now)
    # Re-block the task manually so the 2nd sweep has something to see.
    conn.execute(
        "UPDATE tasks SET status = 'blocked' WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    result2 = brc.sweep_once(conn, now=now + 60)

    # Second sweep counts it but doesn't act — the trigger is the same.
    assert result2.considered == 1
    assert result2.acted_count == 0
    # Still exactly one retry event.
    retry_evts = [
        e for e in _events(conn, task_id)
        if e["kind"] == "blocked_auto_retry_after_cooldown"
    ]
    assert len(retry_evts) == 1


def test_policy_a_respects_max_cycles(kanban_home):
    """After ``max_cycles`` prior auto-retries, Policy A stops firing."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_gave_up_max"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 100_000)
    # Fabricate 3 prior auto-retry events.
    with kb.write_txn(conn):
        for i in range(3):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, "
                "created_at) VALUES (?, NULL, ?, ?, ?)",
                (
                    task_id, "blocked_auto_retry_after_cooldown",
                    json.dumps({"cycle": i + 1, "max_cycles": 3}),
                    now - 90_000 + (i * 3600),
                ),
            )
    _append_trigger(
        conn, task_id=task_id, kind="gave_up",
        created_at=now - 1000, payload={"error": "boom"},
    )

    result = brc.sweep_once(conn, now=now, gave_up_max_cycles=3)

    assert result.acted_count == 0
    assert _status(conn, task_id) == "blocked"


def test_policy_a_waits_for_cooldown(kanban_home):
    """A ``gave_up`` event that hasn't cooled off yet is left alone."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_gave_up_fresh"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 3600)
    _append_trigger(
        conn, task_id=task_id, kind="gave_up",
        created_at=now - 60,  # only 60s < 900s cooldown
        payload={"error": "boom"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.acted_count == 0
    assert _status(conn, task_id) == "blocked"


# ---------------------------------------------------------------------------
# Policy B — precondition recheck
# ---------------------------------------------------------------------------


def test_policy_b_unblocks_when_memory_clears(kanban_home):
    """A memory-preconditioned block auto-unblocks when host mem >=
    the parsed threshold."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_mem_precondition"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 3600)
    trigger_id = _append_trigger(
        conn, task_id=task_id, kind="blocked",
        created_at=now - 300,
        payload={"reason": "swap free below 512 MB", "kind": "transient"},
    )

    result = brc.sweep_once(
        conn, now=now,
        host_resources={"swap_free_mb": 1024.0, "mem_available_mb": 8192.0},
    )

    assert result.acted_count == 1
    action = result.actions[0]
    assert action.policy == "B"
    assert action.action == "unblocked"
    assert action.trigger_event_id == trigger_id
    assert _status(conn, task_id) == "ready"

    evts = [e for e in _events(conn, task_id) if e["kind"] == "precondition_cleared"]
    assert len(evts) == 1
    pl = evts[0]["payload"]
    assert pl["policy"] == "B"
    assert pl["resource"] == "swap_free_mb"
    assert pl["required_mb"] == 512.0
    assert pl["current_mb"] == 1024.0


def test_policy_b_stays_blocked_when_resources_still_low(kanban_home):
    """A memory-preconditioned block stays blocked when host mem is
    still below the threshold."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_mem_still_low"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 3600)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 300,
        payload={"reason": "RAM insufficient (min 2048 MB)"},
    )

    result = brc.sweep_once(
        conn, now=now,
        host_resources={"mem_available_mb": 512.0},
    )

    assert result.acted_count == 0
    assert _status(conn, task_id) == "blocked"


# ---------------------------------------------------------------------------
# Policy C — time-gated release
# ---------------------------------------------------------------------------


def test_policy_c_releases_iso_gate(kanban_home):
    """An absolute ISO time gate releases once wall clock >= release."""
    conn = _open(kanban_home)
    # Pin ``now`` to a fixed epoch that lines up cleanly with the ISO.
    # 1755882000 = 2025-08-22 17:00:00 UTC
    now = 1755882000 + 3600  # 2025-08-22 18:00:00 UTC (== release)
    task_id = "t_time_iso"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 3600,
        payload={"reason": "Do not re-promote before 2025-08-22T18:00:00Z"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.acted_count == 1
    action = result.actions[0]
    assert action.policy == "C"
    assert action.action == "unblocked"
    assert _status(conn, task_id) == "ready"

    evts = [e for e in _events(conn, task_id) if e["kind"] == "time_gate_released"]
    assert len(evts) == 1
    assert evts[0]["payload"]["policy"] == "C"


def test_policy_c_releases_relative_gate(kanban_home):
    """A relative ``T+2h`` gate releases 2h after the trigger event."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_time_rel"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 10_000)
    _append_trigger(
        conn, task_id=task_id, kind="blocked",
        created_at=now - 7300,  # trigger 2h 1min ago
        payload={"reason": "cooldown until T+2h"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.acted_count == 1
    assert result.actions[0].policy == "C"
    assert _status(conn, task_id) == "ready"


def test_policy_c_holds_before_gate(kanban_home):
    """Gate hasn't been reached yet — no action."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_time_pre_gate"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 1000)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 300,
        payload={"reason": "cooldown until T+2h"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.acted_count == 0
    assert _status(conn, task_id) == "blocked"


# ---------------------------------------------------------------------------
# Policy D — review escalation
# ---------------------------------------------------------------------------


def test_policy_d_escalates_stale_review(kanban_home):
    """A review-required block older than ``stale_s`` gets a comment
    and an escalation event but is NEVER unblocked."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_review_stale"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 20_000)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 8000,  # > 7200
        payload={"reason": "review-required: needs sign-off from platform-eng"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.acted_count == 1
    action = result.actions[0]
    assert action.policy == "D"
    assert action.action == "escalated"

    # Never unblocked.
    assert _status(conn, task_id) == "blocked"

    # Comment + escalation event both present.
    comments = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    assert len(comments) == 1
    assert comments[0]["author"] == "block-recheck"
    assert comments[0]["body"].startswith("review_pending_operator_needed:")

    escs = [
        e for e in _events(conn, task_id)
        if e["kind"] == "review_pending_operator_needed"
    ]
    assert len(escs) == 1


def test_policy_d_is_idempotent(kanban_home):
    """Second sweep on the same trigger doesn't double-comment."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_review_idem"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 20_000)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 8000,
        payload={"reason": "review-required"},
    )

    brc.sweep_once(conn, now=now)
    brc.sweep_once(conn, now=now + 3600)

    comments = conn.execute(
        "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert comments["n"] == 1


def test_policy_d_ignores_fresh_review(kanban_home):
    """A recent review-required block is left alone — humans get time
    before the watchdog escalates."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_review_fresh"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 3600)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 60,
        payload={"reason": "review-required"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.acted_count == 0
    assert _status(conn, task_id) == "blocked"


# ---------------------------------------------------------------------------
# Cross-policy invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["running", "ready", "todo", "triage", "done", "archived"]
)
def test_non_blocked_tickets_are_untouched(kanban_home, status):
    """The sweep MUST NOT flip anything that isn't in ``blocked``, even
    with a matching trigger event."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = f"t_untouched_{status}"

    # Same shape as _make_blocked_ticket but with a different status.
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "t", "b", "backend-eng", status, now - 7200),
        )
    _append_trigger(
        conn, task_id=task_id, kind="gave_up", created_at=now - 1000,
        payload={"error": "boom"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.considered == 0
    assert result.acted_count == 0
    assert _status(conn, task_id) == status


def test_policy_e_skips_unmatched_reason(kanban_home):
    """A ``blocked`` event whose reason doesn't match B/C/D counts but
    doesn't fire any action."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_no_policy"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 3600,
        payload={"reason": "user said so, no auto-recovery please"},
    )

    result = brc.sweep_once(conn, now=now)

    assert result.considered == 1
    assert result.acted_count == 0
    assert result.skipped_no_policy == 1
    assert _status(conn, task_id) == "blocked"


def test_dry_run_reports_but_does_not_mutate(kanban_home):
    """``dry_run=True`` returns the same actions but leaves the DB
    untouched."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_dry_run"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="gave_up", created_at=now - 1000,
        payload={"error": "boom"},
    )

    result = brc.sweep_once(conn, now=now, dry_run=True)

    assert result.acted_count == 1
    assert result.actions[0].policy == "A"
    # No mutation.
    assert _status(conn, task_id) == "blocked"
    # No retry event written.
    retry_evts = [
        e for e in _events(conn, task_id)
        if e["kind"] == "blocked_auto_retry_after_cooldown"
    ]
    assert len(retry_evts) == 0


def test_review_takes_precedence_over_time_and_precondition(kanban_home):
    """When a reason mentions BOTH review-required AND (time gate | 
    resource threshold), Policy D wins — never auto-unblock a
    review."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_review_first"

    _make_blocked_ticket(conn, task_id=task_id, created_at=now - 20_000)
    _append_trigger(
        conn, task_id=task_id, kind="blocked", created_at=now - 8000,
        payload={
            "reason": "review-required before merge on T+1h; RAM insufficient",
        },
    )

    result = brc.sweep_once(
        conn, now=now,
        host_resources={"mem_available_mb": 8192.0},
    )

    assert result.acted_count == 1
    assert result.actions[0].policy == "D"
    assert result.actions[0].action == "escalated"
    # Still blocked — review is a hard gate.
    assert _status(conn, task_id) == "blocked"


def test_sweep_all_boards_delegates_per_board(kanban_home, monkeypatch):
    """``sweep_all_boards`` iterates every board on disk, isolates errors
    per board, and captures host_resources exactly once per tick."""
    # Only the default board exists in this fixture. Create a second
    # blocked ticket to observe.
    conn = _open(kanban_home)
    now = 1_800_000_000
    _make_blocked_ticket(conn, task_id="t_multi", created_at=now - 7200)
    _append_trigger(
        conn, task_id="t_multi", kind="gave_up", created_at=now - 1000,
        payload={"error": "boom"},
    )

    # Track host-resource capture calls.
    call_count = {"n": 0}
    real = brc._current_host_resources

    def _spy():
        call_count["n"] += 1
        return real()

    monkeypatch.setattr(brc, "_current_host_resources", _spy)

    results = brc.sweep_all_boards(now=now)
    # At least the default board must have been swept.
    assert len(results) >= 1
    # Resources captured exactly once for the whole tick.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Threshold parser unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("swap free below 512 MB", 512.0),
        ("RAM insufficient (min 2048 MiB)", 2048.0),
        ("disk usage: free below 1 GB", 1024.0),
        ("no numeric threshold here", None),
    ],
)
def test_extract_threshold_mb(reason, expected):
    assert brc._extract_threshold_mb(reason) == expected


@pytest.mark.parametrize(
    "reason,trigger,expected",
    [
        # Absolute ISO with Z.
        ("release at 2026-05-22T18:00:00Z", 0, 1779472800),
        # Relative T+2h — relative to trigger_created_at.
        ("cooldown until T+2h", 1_000_000, 1_007_200),
        ("do not re-promote before T+30m", 1_000_000, 1_001_800),
        ("release at T+1d", 1_000_000, 1_086_400),
        # No match.
        ("no time here", 1_000_000, None),
    ],
)
def test_parse_release_time(reason, trigger, expected):
    assert brc._parse_release_time(reason, trigger_created_at=trigger) == expected


# ---------------------------------------------------------------------------
# main() / CLI subcommand smoke — FIX-7B / t_d9aec252
# ---------------------------------------------------------------------------
#
# These are shallow smoke tests. Deep policy coverage lives above; here we
# only verify the two new entrypoints are wired, parse args, call the
# sweep function, and exit 0 on the empty-board happy path.


def test_module_main_smoke(kanban_home, capsys):
    """`python -m hermes_cli.kanban_block_recheck --dry-run` returns 0."""
    rc = brc.main(["--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "block-recheck sweep" in captured.out
    assert "[DRY RUN]" in captured.out


def test_module_main_honours_cli_overrides(kanban_home, monkeypatch):
    """CLI flags must beat config values."""
    captured_kwargs: dict = {}

    def _fake_sweep(**kwargs):
        captured_kwargs.update(kwargs)
        return {}

    monkeypatch.setattr(brc, "sweep_all_boards", _fake_sweep)
    rc = brc.main([
        "--dry-run",
        "--gave-up-cooldown", "77",
        "--gave-up-max-cycles", "9",
        "--review-stale", "8888",
    ])
    assert rc == 0
    assert captured_kwargs["gave_up_cooldown_s"] == 77
    assert captured_kwargs["gave_up_max_cycles"] == 9
    assert captured_kwargs["review_stale_s"] == 8888
    assert captured_kwargs["dry_run"] is True


def test_cli_subcommand_registered(kanban_home, capsys, monkeypatch):
    """`hermes kanban block-recheck-sweep --all-boards --dry-run --json` works."""
    import argparse
    from hermes_cli import kanban as kcli

    def _fake_sweep(**_kwargs):
        # Return a synthetic non-empty result so JSON formatting is exercised.
        result = brc.RecheckResult()
        result.considered = 3
        action = brc.RecheckAction(
            task_id="t_test01",
            policy="A",
            action="unblocked",
            reason="cooldown elapsed",
            trigger_kind="gave_up",
            trigger_event_id=42,
            trigger_created_at=1_000_000,
            age_s=3600,
        )
        result.actions.append(action)
        return {"default": result}

    monkeypatch.setattr(brc, "sweep_all_boards", _fake_sweep)

    # Build the argparse namespace via the CLI's own parser so we exercise
    # the real subcommand wiring (parser + dispatch table).
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    kcli.build_parser(sub)
    ns = parser.parse_args([
        "kanban", "block-recheck-sweep",
        "--all-boards", "--dry-run", "--json",
    ])
    rc = kcli.kanban_command(ns)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert "default" in payload["boards"]
    b = payload["boards"]["default"]
    assert b["considered"] == 3
    assert len(b["actions"]) == 1
    assert b["actions"][0]["task_id"] == "t_test01"
    assert b["actions"][0]["policy"] == "A"
    assert b["actions"][0]["action"] == "unblocked"


def test_cli_subcommand_human_output(kanban_home, capsys, monkeypatch):
    """Human-text output includes counters + per-board detail."""
    import argparse
    from hermes_cli import kanban as kcli

    def _fake_sweep(**_kwargs):
        r = brc.RecheckResult()
        r.considered = 2
        r.actions.append(brc.RecheckAction(
            task_id="t_esc",
            policy="D",
            action="escalated",
            reason="review-required stale",
            trigger_kind="blocked",
            trigger_event_id=7,
            trigger_created_at=1_000_000,
            age_s=8000,
        ))
        return {"myboard": r}

    monkeypatch.setattr(brc, "sweep_all_boards", _fake_sweep)

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    kcli.build_parser(sub)
    ns = parser.parse_args([
        "kanban", "block-recheck-sweep", "--all-boards",
    ])
    rc = kcli.kanban_command(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "block-recheck-sweep" in out
    assert "boards=1" in out
    assert "applied=1" in out
    assert "escalated=1" in out
    assert "[myboard]" in out
    assert "t_esc" in out
    assert "policy=D" in out


def test_prometheus_counter_increments_on_action(kanban_home):
    """`_BLOCK_RECHECK_ACTIONS` bumps for every recorded action."""
    if brc._BLOCK_RECHECK_ACTIONS is None:
        pytest.skip("prometheus_client not installed")

    # Read the counter's current value for our labels, do a synthetic
    # observe, then confirm it went up by 1.
    labels = brc._BLOCK_RECHECK_ACTIONS.labels(policy="A", action="unblocked")
    before = labels._value.get()  # prometheus_client internal
    action = brc.RecheckAction(
        task_id="t_probe",
        policy="A",
        action="unblocked",
        reason="test",
        trigger_kind="gave_up",
        trigger_event_id=1,
        trigger_created_at=0,
        age_s=0,
    )
    brc._observe_action(action)
    after = labels._value.get()
    assert after == before + 1
