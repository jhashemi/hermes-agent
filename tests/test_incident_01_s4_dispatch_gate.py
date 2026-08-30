"""INCIDENT-01 S4 — dispatch-gate model liveness probe.

Reproduces the 2026-08-18 incident's deepest layer: even after S1
(worker exits non-zero) and S2 (fallback cascade fires) land, the
dispatcher would STILL keep re-dispatching a card to a node whose
resolved model is quota-dead. The only signal is one failed run per
attempt — no cross-run learning.

The dispatch gate must:
  1. Before spawning a worker, resolve the model that would be used
     for that (task, node) pair.
  2. Check a cached KV entry ``hrv.node_state.<node>.<provider>.<model>``
     with a 5-minute TTL, populated by an out-of-band probe (or by
     failed runs' post-mortems).
  3. When the entry says the model is quota-dead within its TTL:
     reject the (task, node) pair and let the dispatcher route
     elsewhere or hold the card.
  4. Fall OPEN (allow dispatch) when the KV is unreachable, empty,
     or the entry is stale — never block on a broken probe.

Public surface expected:

    from hermes_cli.dispatch_gate import (
        check_model_liveness,    # pure predicate
        record_model_liveness,   # writer used by failed runs / cron
    )

    verdict = check_model_liveness(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        kv_reader=some_kv,          # callable(key) -> dict | None
        now=1787044252.0,           # injectable clock
    )
    verdict.allow          # bool — dispatch may proceed
    verdict.reason         # str — human-readable diagnostic

    record_model_liveness(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        alive=False,
        reason="429 session usage limit",
        kv_writer=some_kv,          # callable(key, value_dict)
        now=1787044252.0,
        ttl_seconds=300,
    )
"""

from __future__ import annotations

import pytest


def _import_gate():
    from hermes_cli.dispatch_gate import check_model_liveness, record_model_liveness
    return check_model_liveness, record_model_liveness


# ─────────────────────────────────────────────────────────────────────
# Fail-open behavior — the gate must never block on missing signal
# ─────────────────────────────────────────────────────────────────────


def test_missing_kv_entry_allows_dispatch():
    """No entry in the KV ⇒ we have no evidence the model is dead.
    Must allow dispatch (fail open). Blocking on missing signal would
    freeze new node-model pairs forever.
    """
    check, _ = _import_gate()
    verdict = check(
        node="everett",
        provider="bedrock",
        model="us.anthropic.claude-sonnet-4-6",
        kv_reader=lambda key: None,
        now=1787044252.0,
    )
    assert verdict.allow is True
    assert "no state" in verdict.reason.lower() or "unknown" in verdict.reason.lower()


def test_kv_reader_raising_allows_dispatch():
    """Probe/KV broken → fail open. A broken health-signal MUST NOT
    block real work — the ticket calls out this fail-open guarantee.
    """
    check, _ = _import_gate()

    def _bad_reader(key):
        raise RuntimeError("NATS KV unreachable")

    verdict = check(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        kv_reader=_bad_reader,
        now=1787044252.0,
    )
    assert verdict.allow is True, "Gate must fail OPEN on probe errors"


def test_stale_kv_entry_allows_dispatch():
    """An entry older than ``ttl_seconds`` is treated as no signal.
    A model that was 429'd 30 minutes ago has probably recovered —
    don't hold the grudge forever.
    """
    check, _ = _import_gate()
    now = 2_000_000_000.0
    # Entry recorded 10 minutes ago, TTL 5 minutes ⇒ stale
    stale_entry = {
        "alive": False,
        "reason": "429 usage limit",
        "recorded_at": now - 600,
        "ttl_seconds": 300,
    }
    verdict = check(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        kv_reader=lambda k: stale_entry,
        now=now,
    )
    assert verdict.allow is True, "Stale entries must not block dispatch"


# ─────────────────────────────────────────────────────────────────────
# Blocking behavior — the gate must reject dead models within TTL
# ─────────────────────────────────────────────────────────────────────


def test_fresh_dead_entry_blocks_dispatch():
    """Entry recorded 60s ago says the model is 429-dead. Must block
    dispatch to this (node, provider, model) so the card gets routed
    elsewhere or held instead of re-spawning into the same wall.
    """
    check, _ = _import_gate()
    now = 2_000_000_000.0
    fresh_dead = {
        "alive": False,
        "reason": "429 session usage limit (ollama-cloud)",
        "recorded_at": now - 60,
        "ttl_seconds": 300,
    }
    verdict = check(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        kv_reader=lambda k: fresh_dead,
        now=now,
    )
    assert verdict.allow is False, (
        "S4 REGRESSION: fresh dead-model KV entry did not block "
        "dispatch. INCIDENT-01 root cause: dispatcher kept spawning "
        "workers to Everett's quota-dead ollama-cloud endpoint. "
        "See ticket t_3e1634d9."
    )
    assert "429" in verdict.reason or "usage" in verdict.reason.lower(), (
        f"Verdict should carry the diagnostic. Got: {verdict.reason!r}"
    )


def test_fresh_alive_entry_allows_dispatch():
    """Positive-signal path: last probe said the model is healthy.
    Must allow.
    """
    check, _ = _import_gate()
    now = 2_000_000_000.0
    fresh_alive = {
        "alive": True,
        "reason": "1-token smoke test ok, latency=1200ms",
        "recorded_at": now - 30,
        "ttl_seconds": 300,
    }
    verdict = check(
        node="everett",
        provider="bedrock",
        model="us.anthropic.claude-sonnet-4-6",
        kv_reader=lambda k: fresh_alive,
        now=now,
    )
    assert verdict.allow is True


# ─────────────────────────────────────────────────────────────────────
# Writer contract — record_model_liveness populates the KV correctly
# ─────────────────────────────────────────────────────────────────────


def test_record_model_liveness_writes_expected_shape():
    """Failed workers (or a cron probe) must be able to publish a
    death record that the gate can later read."""
    _, record = _import_gate()

    captured: dict[str, dict] = {}

    def _writer(key: str, value: dict) -> None:
        captured[key] = value

    now = 2_000_000_000.0
    record(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        alive=False,
        reason="HTTP 429: you have reached your session usage limit",
        kv_writer=_writer,
        now=now,
        ttl_seconds=300,
    )

    assert len(captured) == 1, f"Expected exactly one KV write. Got: {captured!r}"
    key, value = next(iter(captured.items()))
    # Key must be predictable so the reader can look it up
    assert "everett" in key
    assert "ollama-cloud" in key
    assert "glm-5.2" in key
    # Value must carry the fields the reader depends on
    assert value["alive"] is False
    assert "usage limit" in value["reason"].lower()
    assert value["recorded_at"] == pytest.approx(now)
    assert value["ttl_seconds"] == 300


def test_record_then_check_roundtrip():
    """A written record must round-trip: same node/provider/model on
    the reader side finds the writer's entry.
    """
    check, record = _import_gate()

    kv: dict[str, dict] = {}
    record(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        alive=False,
        reason="quota exhausted",
        kv_writer=lambda k, v: kv.__setitem__(k, v),
        now=100.0,
        ttl_seconds=300,
    )

    verdict = check(
        node="everett",
        provider="ollama-cloud",
        model="glm-5.2",
        kv_reader=lambda k: kv.get(k),
        now=150.0,   # 50s after write, well inside TTL
    )
    assert verdict.allow is False, (
        "Roundtrip failed: writer and reader agree on key format? "
        f"KV contents: {kv!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Integration hook — the dispatch spawn path must consult the gate
# ─────────────────────────────────────────────────────────────────────


def test_spawn_task_calls_dispatch_gate():
    """Structural check: the kanban dispatch spawn path must reference
    ``check_model_liveness`` (directly or via a wrapper). Without this
    the pure gate function exists but is never invoked, and the whole
    incident recurs.

    This is intentionally a source-level assertion — the dispatch spawn
    machinery is 500+ lines of environment-dependent code that we don't
    want to drive end-to-end in a unit test. A source-level reference
    is necessary AND sufficient to confirm the wire-up.
    """
    import inspect
    from hermes_cli import kanban_db as _kdb

    src = inspect.getsource(_kdb)
    # Either the pure predicate or a wrapper must be referenced.
    assert (
        "check_model_liveness" in src
        or "dispatch_gate" in src
    ), (
        "S4 REGRESSION: kanban_db.py does not reference the dispatch "
        "gate. Without wire-up the gate is dead code and the incident "
        "recurs. Add a check_model_liveness() call to the spawn path "
        "(near spawn_on_remote / SSH probe region, line ~9575)."
    )
