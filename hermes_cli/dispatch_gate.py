"""INCIDENT-01 (2026-08-18) — dispatch-gate model liveness probe.

Extends failure class #9 (bedrock 429 probe) to a general per-node,
per-model liveness signal read at kanban-dispatch time. The 2026-08-18
"no pulse" event burned 6 worker spawns into Everett's quota-dead
``ollama-cloud/glm-5.2`` endpoint before a human noticed — because the
dispatcher had no signal that the model was dead beyond "the last worker
crashed", which its retry policy is designed to absorb.

Contract:

    check_model_liveness(node, provider, model, kv_reader, now)
        -> LivenessVerdict(allow: bool, reason: str)

    record_model_liveness(
        node, provider, model, alive, reason,
        kv_writer, now, ttl_seconds=300,
    )
        -> None

The gate is a PURE PREDICATE. The KV reader/writer callables are
injected — callers wire in NATS KV, a SQLite table, or an in-memory
dict as appropriate. This module has zero runtime dependencies beyond
the standard library, so it is safe to import from kanban_db.py at
dispatch time without dragging in NATS / prometheus / etc.

Fail-open guarantees (mandatory — see ticket t_3e1634d9):
  1. Missing KV entry ⇒ allow. New (node, model) pairs are not blocked.
  2. Stale KV entry (age > ttl_seconds) ⇒ allow. A grudge older than
     5 minutes is discarded so recovered accounts get another chance.
  3. KV reader raising ⇒ allow. A broken probe MUST NOT block work.
  4. Malformed KV value ⇒ allow, log the shape mismatch to caller
     via the verdict.reason for diagnostics.

Blocking behavior:
  - Fresh (age ≤ ttl_seconds) entry with alive=False ⇒ deny.
  - Fresh entry with alive=True ⇒ allow (positive signal).

Key format is a single string so any flat KV works. The layout uses
NATS-friendly dot separators:

    hrv.node_state.<node>.<provider>.<model>

Model / provider slugs are lowercased and stripped for the key
computation only; the value dict keeps the original casing for the
diagnostic message.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_TTL_SECONDS = 300


# ─────────────────────────────────────────────────────────────────────
# Public dataclass — carries the verdict + human-readable reason.
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LivenessVerdict:
    """Result of a liveness check.

    Attributes:
        allow: True when the dispatcher may spawn onto (node, provider, model).
        reason: Human-readable diagnostic. Always populated so callers
            can log the decision without inspecting flags.
    """

    allow: bool
    reason: str


def _liveness_key(node: str, provider: str, model: str) -> str:
    """Compute the KV key. Kept public-shape so writers agree with readers.

    Lowercases and strips for canonical form. Non-string inputs coerced
    to str so a numeric or None inbound doesn't crash the whole probe.
    """
    node_slug = str(node or "").strip().lower() or "unknown"
    provider_slug = str(provider or "").strip().lower() or "unknown"
    model_slug = str(model or "").strip().lower() or "unknown"
    return f"hrv.node_state.{node_slug}.{provider_slug}.{model_slug}"


# ─────────────────────────────────────────────────────────────────────
# check_model_liveness — pure predicate consumed by the dispatch gate
# ─────────────────────────────────────────────────────────────────────


def check_model_liveness(
    node: str,
    provider: str,
    model: str,
    kv_reader: Callable[[str], Optional[dict]],
    *,
    now: Optional[float] = None,
) -> LivenessVerdict:
    """Return the dispatch verdict for a (node, provider, model) triple.

    Args:
        node: Cluster node name the dispatcher would spawn the worker on.
        provider: Resolved provider slug (e.g. "ollama-cloud", "bedrock").
        model: Resolved model slug.
        kv_reader: Callable(key) → dict | None. Return None (or raise)
            when the key is absent — the gate fails OPEN either way.
        now: Injectable clock for tests. Defaults to time.time().

    Returns:
        LivenessVerdict — check ``.allow`` before dispatching, log
        ``.reason`` regardless.
    """
    now = now if now is not None else time.time()
    key = _liveness_key(node, provider, model)

    # ── Fail-open: reader errors never block real work ────────────────
    try:
        entry = kv_reader(key)
    except Exception as exc:
        logger.debug(
            "dispatch_gate: kv_reader(%s) raised %r — failing open", key, exc,
        )
        return LivenessVerdict(
            allow=True,
            reason=f"unknown (kv reader raised {type(exc).__name__}); fail-open",
        )

    if not isinstance(entry, dict):
        # None (absent) or malformed (garbage in KV). Fail open with an
        # honest diagnostic so operators can spot bad writers.
        return LivenessVerdict(
            allow=True,
            reason="no state for this (node, provider, model); fail-open",
        )

    # ── Age check: stale entries are discarded ────────────────────────
    recorded_at = entry.get("recorded_at")
    ttl = entry.get("ttl_seconds") or DEFAULT_TTL_SECONDS
    try:
        age = float(now) - float(recorded_at)
    except (TypeError, ValueError):
        return LivenessVerdict(
            allow=True,
            reason=f"malformed recorded_at ({recorded_at!r}); fail-open",
        )
    try:
        ttl_f = float(ttl)
    except (TypeError, ValueError):
        ttl_f = float(DEFAULT_TTL_SECONDS)

    if age > ttl_f:
        return LivenessVerdict(
            allow=True,
            reason=(
                f"stale state ({age:.0f}s old, ttl={ttl_f:.0f}s); "
                "discarded and failing open"
            ),
        )

    # ── Fresh entry — the alive flag decides ──────────────────────────
    alive = bool(entry.get("alive"))
    diag = str(entry.get("reason") or "").strip() or "no diagnostic recorded"

    if alive:
        return LivenessVerdict(
            allow=True,
            reason=f"fresh positive signal ({age:.0f}s old): {diag}",
        )

    return LivenessVerdict(
        allow=False,
        reason=(
            f"model marked dead {age:.0f}s ago (ttl={ttl_f:.0f}s): {diag}. "
            "Dispatch rejected — route to a different node/model or hold."
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# record_model_liveness — writer used by failed runs / cron probes
# ─────────────────────────────────────────────────────────────────────


def record_model_liveness(
    node: str,
    provider: str,
    model: str,
    alive: bool,
    reason: str,
    kv_writer: Callable[[str, dict], Any],
    *,
    now: Optional[float] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Publish a liveness record for the gate to consult later.

    Called by:
      - The retry loop when a terminal API error identifies a model as
        quota-dead (see agent/error_classifier.py — quota-exhaustion
        classification from INCIDENT-01 S2).
      - A periodic cron probe that runs a 1-token smoke test against
        each (node, provider, model) triple every 5 minutes.

    The writer callable is injected so this module remains dependency-
    free. Wire it to a NATS KV bucket, a SQLite row, or an in-memory
    dict as your deployment requires.

    Fail-safe: writer errors are logged and swallowed. A broken writer
    must not crash the runtime path that discovers a dead model — the
    dispatch gate will simply not have this signal on the next spawn.
    """
    now = now if now is not None else time.time()
    key = _liveness_key(node, provider, model)
    value = {
        "node": node,
        "provider": provider,
        "model": model,
        "alive": bool(alive),
        "reason": str(reason or ""),
        "recorded_at": float(now),
        "ttl_seconds": int(ttl_seconds),
    }
    try:
        kv_writer(key, value)
    except Exception as exc:  # pragma: no cover — logged, not raised
        logger.warning(
            "dispatch_gate: kv_writer(%s) failed: %r — signal lost this cycle",
            key, exc,
        )


# ─────────────────────────────────────────────────────────────────────
# Convenience: in-memory KV pair for tests and single-node deployments
# ─────────────────────────────────────────────────────────────────────


def make_inmemory_kv() -> tuple[Callable[[str], Optional[dict]], Callable[[str, dict], None]]:
    """Return (reader, writer) callables backed by a private dict.

    Handy for tests, single-node dev, and cron probes that don't need
    cluster-wide visibility. Production deployments should wire the
    reader/writer to a shared NATS JetStream KV bucket instead.
    """
    store: dict[str, dict] = {}

    def _read(key: str) -> Optional[dict]:
        return store.get(key)

    def _write(key: str, value: dict) -> None:
        store[key] = value

    return _read, _write
