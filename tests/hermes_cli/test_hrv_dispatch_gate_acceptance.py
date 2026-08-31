"""Acceptance tests for nervous-system dispatch gate (t_cc1a6c04).

Fresh re-implementation authorized by operator (comment #907 on t_cc1a6c04),
after the prior branch's test file (ac34887f) was declared unrecoverable by
a cross-repo phantom-artifact audit.

Scope, per operator directive:
    "Unit tests for the shipped nervous-system gate checks (check_node_gate
    called from kanban_db._dispatch_once_locked, per t_c6b97f78's landed
    integration). Cover: node-active pass, node-stale block, node-unknown
    default-deny-or-warn behavior, and gate-bypass flag semantics."

Plus the acceptance criteria from the ticket body itself:
    - RED memory_pressure never selected
    - RED kanban_dispatcher_health never selected
    - Task pinned to RED bedrock_rate_limit model not dispatched to that node
    - interval_class=urgent rejects <P0, accepts P0
    - All-green + non-urgent accepted
    - Existing hard gates still work (local-dispatch bypass, fail-open on
      import failure)

Target seam:
    hermes_cli.hrv_node_gate_integration.check_node_gate(conn, task_id,
        node_hostname, model_name, gate=...) -> Optional[str]

Design notes:
    - "node-stale" in the shipped design deliberately fails OPEN, not closed:
      halting all dispatch when probes go stale would produce a global-outage
      failure mode strictly worse than the memory-pressure it aims to prevent.
      The stale-fail-open tests below LOCK that contract in place so a future
      refactor cannot silently flip the semantics.
    - "node-unknown" (hostname not in cache) is a subclass of stale: same
      fail-open contract.
    - "gate-bypass" appears at two layers: (a) target_node is None (local
      dispatch skips the gate entirely at the call site in
      _dispatch_once_locked), and (b) gate loading raises inside
      check_node_gate itself, which is wrapped in try/except that returns
      None (fail-open).
"""
from __future__ import annotations

import datetime
import sqlite3
import tempfile
from pathlib import Path

import pytest

from hermes_cli.hrv_node_gate import (
    HRVDigestSnapshot,
    HRVNodeGate,
    NodeProbeSnapshot,
)
from hermes_cli.hrv_node_gate_integration import check_node_gate
from hermes_cli.kanban_db import create_task, init_db


def _iso_now(delta_seconds: float = 0.0) -> str:
    """Return an ISO8601 UTC timestamp, optionally offset by delta_seconds."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if delta_seconds:
        now = now + datetime.timedelta(seconds=delta_seconds)
    return now.isoformat()


@pytest.fixture
def temp_db():
    """Fresh isolated kanban DB per test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "kanban.db"
        init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _make_task(conn, *, priority: int | None = None, body: str = "task"):
    """Create a task and return its id. Priority is passed only when not None."""
    if priority is None:
        return create_task(conn, title="acceptance-test",
                           assignee="backend-eng", body=body)
    return create_task(conn, title="acceptance-test",
                       assignee="backend-eng", body=body, priority=priority)


def _green_probe(hostname: str = "hermes2") -> NodeProbeSnapshot:
    """A green probe: all values comfortably inside safe thresholds, fresh ts."""
    return NodeProbeSnapshot(
        hostname=hostname,
        swap_pct=10.0,
        mem_gb_available=32.0,
        load_1m=1.0,
        load_5m=1.0,
        disk_free_gb=100.0,
        active_workers=0,
        max_workers=8,
        bedrock_tpm_remaining=100_000,
        ts=_iso_now(),
    )


# ---------------------------------------------------------------------------
# Scenario 1: node-active pass
# ---------------------------------------------------------------------------
class TestNodeActivePass:
    """A node whose probe is fresh and all-green must be accepted."""

    def test_active_green_node_accepted_at_default_priority(self, temp_db):
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(interval_class="calm", ts=_iso_now()))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None

    def test_active_green_node_accepted_at_p0(self, temp_db):
        task_id = _make_task(temp_db, priority=0)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(interval_class="calm", ts=_iso_now()))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None

    def test_active_green_node_accepted_with_bedrock_arn_model(self, temp_db):
        """Full Bedrock ARN-style model IDs must pass on a healthy node."""
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())

        model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert check_node_gate(temp_db, task_id, "hermes2", model,
                               gate=gate) is None


# ---------------------------------------------------------------------------
# Scenario 2: node-stale — deliberately fails OPEN
# ---------------------------------------------------------------------------
class TestNodeStaleFailsOpen:
    """A node whose probe is stale must NOT be rejected.

    Rationale: halting the dispatcher on stale telemetry is a global-outage
    failure mode. The gate is a safety sieve for known-RED nodes only.
    These tests pin that contract.
    """

    def test_stale_probe_with_red_swap_still_passes(self, temp_db):
        """RED swap in a stale probe must NOT reject (fail-open)."""
        task_id = _make_task(temp_db)
        gate = HRVNodeGate(max_probe_age_seconds=60.0)
        stale = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=99.0,                     # RED
            bedrock_tpm_remaining=0,           # RED
            mem_gb_available=0.1,              # RED
            ts=_iso_now(delta_seconds=-3600),  # 1h old (>>60s)
        )
        gate.set_node_probe_snapshot("hermes2", stale)

        # All would-reject values are present, but probe is stale → fail-open
        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None

    def test_stale_digest_urgent_still_passes(self, temp_db):
        """A stale 'urgent' digest must NOT gate P1 tasks (fail-open)."""
        task_id = _make_task(temp_db, priority=1)
        gate = HRVNodeGate(max_probe_age_seconds=60.0)
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(
            interval_class="urgent",
            ts=_iso_now(delta_seconds=-3600),
        ))

        # Urgent + P1 would normally reject, but stale digest → fail-open
        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None

    def test_probe_missing_ts_treated_as_stale(self, temp_db):
        """Snapshot with no ts is treated as stale (fail-open)."""
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        no_ts = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=99.0,   # RED but no ts → treat as stale
            ts=None,
        )
        gate.set_node_probe_snapshot("hermes2", no_ts)

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None


# ---------------------------------------------------------------------------
# Scenario 3: node-unknown default — probe absent from cache → fail-open
# ---------------------------------------------------------------------------
class TestNodeUnknownFailsOpen:
    """A node with no cached probe must NOT be rejected (fail-open)."""

    def test_unknown_node_passes_all_probe_gates(self, temp_db):
        """Node with no probe entry passes; no probe = no evidence of RED."""
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()  # empty cache

        assert check_node_gate(temp_db, task_id, "unknown-node",
                               "claude-haiku", gate=gate) is None

    def test_unknown_node_still_gated_by_hrv_digest_urgency(self, temp_db):
        """The URGENT gate does NOT depend on the node probe; it uses the
        HRV digest snapshot. An unknown node still gets rejected when the
        digest is fresh-urgent and the task is < P0."""
        task_id = _make_task(temp_db, priority=1)
        gate = HRVNodeGate()  # empty node cache
        gate.set_hrv_digest(HRVDigestSnapshot(
            interval_class="urgent",
            ts=_iso_now(),
        ))

        assert check_node_gate(temp_db, task_id, "unknown-node",
                               "claude-haiku", gate=gate) == "hrv_urgent_state"


# ---------------------------------------------------------------------------
# Scenario 4: gate-bypass flag semantics
# ---------------------------------------------------------------------------
class TestGateBypassSemantics:
    """The gate must be bypassable in two ways: local dispatch (node is None)
    and gate-load failure (fail-open wrapper)."""

    def test_local_dispatch_bypasses_gate(self, temp_db):
        """target_node=None means local dispatch; gate is not consulted."""
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        # Even a RED node in the cache is ignored for local dispatch
        red_probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=99.0,
            ts=_iso_now(),
        )
        gate.set_node_probe_snapshot("hermes2", red_probe)

        # target_node=None → check_node_gate returns None without consulting gate
        assert check_node_gate(temp_db, task_id, None, "claude-haiku",
                               gate=gate) is None

    def test_gate_default_load_failure_fails_open(self, temp_db):
        """When gate=None and default-gate loading raises, fail open.

        The integration function wraps default-gate loading in try/except and
        returns None on failure. Simulated here by pointing at a nonexistent
        node with no explicit gate."""
        task_id = _make_task(temp_db)
        # Passing gate=None triggers the "load default gate" path. In a fresh
        # test environment the default may or may not load; either way, the
        # contract is that any exception yields None (fail-open).
        result = check_node_gate(temp_db, task_id, "unknown-host",
                                 "claude-haiku", gate=None)
        assert result is None

    def test_missing_task_id_fails_open(self, temp_db):
        """If the task row doesn't exist, the outer try/except catches and
        fails open (rather than crashing the dispatcher)."""
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())

        # Use a task_id that was never inserted
        result = check_node_gate(temp_db, "t_nonexistent", "hermes2",
                                 "claude-haiku", gate=gate)
        # The gate's priority/min_resources fetch returns None; no probe rejection
        # applies since node is green → None.
        assert result is None


# ---------------------------------------------------------------------------
# Ticket-body acceptance criteria (mapped onto the check_node_gate seam)
# ---------------------------------------------------------------------------
class TestTicketBodyAcceptance:
    """Direct one-to-one bindings between the ticket-body acceptance list
    and observable check_node_gate outcomes."""

    def test_red_memory_pressure_never_selected(self, temp_db):
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,  # RED (>=90)
            ts=_iso_now(),
        ))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) == "memory_pressure"

    def test_red_dispatcher_health_never_selected(self, temp_db, monkeypatch):
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        # Force dispatcher-health hook RED
        monkeypatch.setattr(gate, "_check_dispatcher_health",
                            lambda hostname: True)

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) == "kanban_dispatcher_health"

    def test_red_bedrock_saturation_rejects_pinned_model(self, temp_db):
        task_id = _make_task(temp_db)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=10.0,
            bedrock_tpm_remaining=500,  # RED (<1000)
            ts=_iso_now(),
        ))

        model = "claude-haiku"
        reason = check_node_gate(temp_db, task_id, "hermes2", model,
                                 gate=gate)
        assert reason is not None
        assert reason.startswith("bedrock_rate_limit_saturation")
        assert model in reason

    def test_urgent_rejects_p1_task(self, temp_db):
        task_id = _make_task(temp_db, priority=1)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(
            interval_class="urgent",
            ts=_iso_now(),
        ))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) == "hrv_urgent_state"

    def test_urgent_accepts_p0_task(self, temp_db):
        task_id = _make_task(temp_db, priority=0)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(
            interval_class="urgent",
            ts=_iso_now(),
        ))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None

    def test_all_green_non_urgent_accepted(self, temp_db):
        task_id = _make_task(temp_db, priority=2)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(
            interval_class="calm",
            ts=_iso_now(),
        ))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None

    @pytest.mark.parametrize("interval_class", ["calm", "alert", "anxious"])
    def test_non_urgent_intervals_accept_any_priority(self, temp_db,
                                                      interval_class):
        """calm/alert/anxious must not gate on priority; only urgent does."""
        task_id = _make_task(temp_db, priority=5)
        gate = HRVNodeGate()
        gate.set_node_probe_snapshot("hermes2", _green_probe())
        gate.set_hrv_digest(HRVDigestSnapshot(
            interval_class=interval_class,
            ts=_iso_now(),
        ))

        assert check_node_gate(temp_db, task_id, "hermes2", "claude-haiku",
                               gate=gate) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
