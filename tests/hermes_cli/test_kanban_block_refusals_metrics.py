"""Tests for VFE-METRICS-01 — ``kanban_block_refusals_total`` Prometheus counter.

Verifies the full chain:
1. ``block_task`` soft-refusals emit ``block_refused`` events into
   ``task_events`` (durable, DB-backed).
2. ``block_refusal_counts`` aggregates the events by refusal code.
3. The dashboard ``/metrics`` endpoint exposes
   ``kanban_block_refusals_total{code=...,board=...}`` in Prometheus text
   exposition format and the counter reflects the correct number of
   refusals.

Note on ``waiting_for_not_found``: in normal operation the
``_find_missing_parents`` gate (line ~5756) catches non-existent ticket
ids and raises ``MissingWaitingForError`` *before* the soft-refusal
check runs.  The ``waiting_for_not_found`` return path is a TOCTOU
guard (task deleted between the two queries).  We test it by calling
``_emit_block_refusal_event`` directly — the event → aggregation →
endpoint chain is identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# block_refusal_counts — direct DB aggregation
# ---------------------------------------------------------------------------


def test_block_refusal_counts_zero_on_fresh_db(kanban_home: Path) -> None:
    """A fresh DB has no refusals."""
    with kb.connect() as conn:
        counts = kb.block_refusal_counts(conn)
    assert counts == {"waiting_for_not_found": 0, "waiting_for_already_done": 0}


def test_block_refusal_counts_after_three_refusals(kanban_home: Path) -> None:
    """Emit 3 soft-refusals and verify the counter reflects 3 increments.

    Two paths to ``waiting_for_already_done`` via ``block_task`` (the
    reachable soft-refusal in normal operation) + one direct
    ``_emit_block_refusal_event`` for ``waiting_for_not_found`` (the
    TOCTOU guard path that can't be triggered through ``block_task``
    in a single-threaded test).
    """
    with kb.connect() as conn:
        blocking_tid = kb.create_task(conn, title="task that wants to block")
        kb.claim_task(conn, blocking_tid)
        run_id = kb.get_task(conn, blocking_tid).current_run_id

        # Create two done tasks to block against.
        done_tid_1 = kb.create_task(conn, title="done parent 1")
        kb.complete_task(conn, done_tid_1, summary="done", result="finished")
        done_tid_2 = kb.create_task(conn, title="done parent 2")
        kb.complete_task(conn, done_tid_2, summary="done", result="finished")

        # Refusal 1: waiting_for_already_done (done_tid_1 is completed)
        result1 = kb.block_task(
            conn, blocking_tid,
            reason="waiting on done parent 1",
            kind="dependency",
            waiting_for=done_tid_1,
            expected_run_id=run_id,
        )
        assert isinstance(result1, dict)
        assert result1["ok"] is False
        assert result1["code"] == "waiting_for_already_done"

        # Refusal 2: waiting_for_already_done (done_tid_2 is completed)
        result2 = kb.block_task(
            conn, blocking_tid,
            reason="waiting on done parent 2",
            kind="dependency",
            waiting_for=done_tid_2,
            expected_run_id=run_id,
        )
        assert isinstance(result2, dict)
        assert result2["code"] == "waiting_for_already_done"

        # Refusal 3: waiting_for_not_found — emit directly (TOCTOU guard path)
        kb._emit_block_refusal_event(
            conn, blocking_tid, "waiting_for_not_found",
            waiting_for="t_deleted_race",
            reason="task deleted between gate and check",
            kind="dependency",
        )

        counts = kb.block_refusal_counts(conn)

    assert counts["waiting_for_already_done"] == 2
    assert counts["waiting_for_not_found"] == 1
    assert sum(counts.values()) == 3


# ---------------------------------------------------------------------------
# /metrics endpoint — Prometheus exposition
# ---------------------------------------------------------------------------


def _metrics_client(monkeypatch: pytest.MonkeyPatch):
    """Return a Starlette TestClient wired to the dashboard app."""
    from starlette.testclient import TestClient

    from hermes_cli import web_server

    web_server.app.state.auth_required = False
    return TestClient(web_server.app)


def test_metrics_endpoint_exposes_refusal_counter(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Emit 3 soft-refusals, scrape /metrics, verify the counter.

    DoD #3: emit 3 soft-refusals + verify counter reflects 3 increments
    in Prometheus scrape.
    """
    with kb.connect() as conn:
        blocking_tid = kb.create_task(conn, title="blocking task")
        kb.claim_task(conn, blocking_tid)
        run_id = kb.get_task(conn, blocking_tid).current_run_id

        done_tid = kb.create_task(conn, title="done parent")
        kb.complete_task(conn, done_tid, summary="done", result="finished")

        # Refusal 1 + 2: waiting_for_already_done via block_task
        for i in range(2):
            res = kb.block_task(
                conn, blocking_tid,
                reason=f"waiting on done parent (attempt {i+1})",
                kind="dependency",
                waiting_for=done_tid,
                expected_run_id=run_id,
            )
            assert res["code"] == "waiting_for_already_done"

        # Refusal 3: waiting_for_not_found via direct emit (TOCTOU guard)
        kb._emit_block_refusal_event(
            conn, blocking_tid, "waiting_for_not_found",
            waiting_for="t_ghost",
            reason="race",
            kind="dependency",
        )

    client = _metrics_client(monkeypatch)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text

    # Verify Prometheus text format headers
    assert "# HELP kanban_block_refusals_total" in body
    assert "# TYPE kanban_block_refusals_total counter" in body

    # The default board should have the correct counts
    assert 'kanban_block_refusals_total{code="waiting_for_already_done",board="default"} 2' in body
    assert 'kanban_block_refusals_total{code="waiting_for_not_found",board="default"} 1' in body

    # Parse the counter values and confirm they sum to 3
    ad_line = [
        line for line in body.splitlines()
        if 'code="waiting_for_already_done"' in line and "kanban_block_refusals_total" in line
    ]
    nf_line = [
        line for line in body.splitlines()
        if 'code="waiting_for_not_found"' in line and "kanban_block_refusals_total" in line
    ]
    assert ad_line, "waiting_for_already_done metric line missing"
    assert nf_line, "waiting_for_not_found metric line missing"

    ad_val = int(ad_line[0].split()[-1])
    nf_val = int(nf_line[0].split()[-1])
    assert ad_val + nf_val == 3


def test_metrics_endpoint_no_refusals(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh DB with no refusals still returns a valid /metrics response."""
    client = _metrics_client(monkeypatch)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "# TYPE kanban_block_refusals_total counter" in body
    assert 'kanban_block_refusals_total{code="waiting_for_not_found",board="default"} 0' in body
    assert 'kanban_block_refusals_total{code="waiting_for_already_done",board="default"} 0' in body


def test_metrics_endpoint_is_unauthenticated(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """/metrics must be reachable without a session token (Prometheus scraper)."""
    client = _metrics_client(monkeypatch)
    # Deliberately do NOT set the session token header.
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "kanban_block_refusals_total" in response.text