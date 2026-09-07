"""Cluster-wide dispatch backpressure signal.

Computes cluster stress from per-node HRV probes (memory_pressure,
kanban_dispatcher_health) and publishes a single ``dispatch.backpressure``
signal on NATS so downstream card-creator services can hold non-urgent
work while the cluster is under duress.

Contract (t_95d86e0c, ADR-006b Phase 2):

  * ``stress_fraction = red_nodes / total_known_nodes`` where a node is RED
    if its cached probe snapshot has ``swap_pct >= 90`` (memory_pressure RED)
    OR ``kanban_dispatcher_health`` flag RED (systemd unit ``activating`` /
    crashed in last 10min).
  * Threshold: 0.5 (configurable via ``kanban.backpressure_threshold`` — a
    single kanban_db key or environment override).
  * Hysteresis: enter ``active`` at ``>= threshold``; clear at
    ``< threshold - 0.1``. This prevents flapping around the edge.
  * NATS subject: ``dispatch.backpressure``
    - Payload on entry:  ``{"state": "active", "red_nodes": [...], "stress_fraction": 0.667, "ts": "..."}``
    - Payload on clear:  ``{"state": "clear", "ts": "..."}``
  * KV bucket ``dispatch_backpressure_state`` mirrors the latest state as a
    single blob under key ``current`` so late subscribers (services that
    restart after publish) can pick up the current state without waiting for
    a transition.

Design notes:
  * The compute step is a pure function so tests can drive transitions
    deterministically without NATS.
  * The broadcaster wraps an injectable NATS publish coroutine and an
    injectable KV putter, so unit tests use plain lambdas.
  * ``BackpressureBroadcaster.tick()`` is idempotent — it publishes only on
    a state transition. Same-state repeat calls are cheap no-ops.
  * Fail-open: if the total-known-nodes count is zero (nothing published a
    probe yet), we do NOT go active. That would halt an entire cold cluster.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

SUBJECT_BACKPRESSURE = "dispatch.backpressure"
KV_BUCKET_BACKPRESSURE = "dispatch_backpressure_state"
KV_KEY_CURRENT = "current"

DEFAULT_THRESHOLD = 0.5
DEFAULT_HYSTERESIS = 0.1  # clear at threshold - hysteresis

STATE_ACTIVE = "active"
STATE_CLEAR = "clear"


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class NodeStress:
    """A single node's stress status as seen by the dispatcher.

    Mirrors the fields the HRV gate already caches, but we only surface
    the two conditions the spec calls out: memory_pressure and
    kanban_dispatcher_health.
    """

    hostname: str
    memory_pressure_red: bool = False
    dispatcher_health_red: bool = False
    ts: Optional[str] = None  # ISO8601 of the underlying probe timestamp

    @property
    def is_red(self) -> bool:
        """True if either RED condition holds."""
        return bool(self.memory_pressure_red or self.dispatcher_health_red)


@dataclass
class BackpressureSnapshot:
    """Result of a compute step.

    ``state`` is the *new* desired state, ``changed`` records whether it
    differs from the last-known state that was passed in.
    """

    state: str
    red_nodes: List[str] = field(default_factory=list)
    stress_fraction: float = 0.0
    total_nodes: int = 0
    changed: bool = False
    ts: str = ""

    def to_payload(self) -> Dict[str, Any]:
        """JSON-serialisable NATS payload matching the spec."""
        if self.state == STATE_ACTIVE:
            return {
                "state": STATE_ACTIVE,
                "red_nodes": list(self.red_nodes),
                "stress_fraction": round(self.stress_fraction, 4),
                "total_nodes": self.total_nodes,
                "ts": self.ts,
            }
        return {"state": STATE_CLEAR, "ts": self.ts}


# ── Pure compute step ─────────────────────────────────────────────────────────


def _iso_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def compute_backpressure(
    nodes: Iterable[NodeStress],
    *,
    current_state: str = STATE_CLEAR,
    threshold: float = DEFAULT_THRESHOLD,
    hysteresis: float = DEFAULT_HYSTERESIS,
    now_iso: Optional[str] = None,
) -> BackpressureSnapshot:
    """Pure compute — decide next backpressure state from probes.

    Args:
      nodes: current per-node stress snapshots.
      current_state: the last-published state (``"active"`` / ``"clear"``);
        needed for hysteresis.
      threshold: fraction at or above which we go active.
      hysteresis: the buffer band below threshold we must fall through
        before clearing. Effective clear point = ``threshold - hysteresis``.
      now_iso: pinned timestamp for tests. Defaults to real wall-clock.

    Returns:
      A :class:`BackpressureSnapshot` capturing the next state, the RED
      node list, and whether it differs from ``current_state``.
    """
    nodes_list = list(nodes)
    total = len(nodes_list)
    red = [n for n in nodes_list if n.is_red]
    red_hostnames = sorted(n.hostname for n in red)

    if total == 0:
        # Fail-open: no probes yet → never go active
        stress = 0.0
    else:
        stress = len(red) / total

    ts = now_iso or _iso_now()

    if current_state == STATE_ACTIVE:
        # Only clear when we drop through the lower band
        if stress < (threshold - hysteresis):
            return BackpressureSnapshot(
                state=STATE_CLEAR,
                red_nodes=red_hostnames,
                stress_fraction=stress,
                total_nodes=total,
                changed=True,
                ts=ts,
            )
        return BackpressureSnapshot(
            state=STATE_ACTIVE,
            red_nodes=red_hostnames,
            stress_fraction=stress,
            total_nodes=total,
            changed=False,
            ts=ts,
        )

    # current_state == "clear" (or unknown → treat as clear)
    if total > 0 and stress >= threshold:
        return BackpressureSnapshot(
            state=STATE_ACTIVE,
            red_nodes=red_hostnames,
            stress_fraction=stress,
            total_nodes=total,
            changed=True,
            ts=ts,
        )
    return BackpressureSnapshot(
        state=STATE_CLEAR,
        red_nodes=red_hostnames,
        stress_fraction=stress,
        total_nodes=total,
        changed=False,
        ts=ts,
    )


# ── Broadcaster (async, injectable transport) ────────────────────────────────


PublishFn = Callable[[str, bytes], Awaitable[None]]
"""Async publish(subject, payload_bytes) — usually ``js.publish``."""

KVPutFn = Callable[[str, str, bytes], Awaitable[None]]
"""Async put(bucket, key, value_bytes) — mirrors latest state for late subs."""


class BackpressureBroadcaster:
    """Stateful wrapper that publishes on transitions only.

    Compose it once at dispatcher startup and call :meth:`tick` every loop
    iteration with the latest per-node stress snapshots. It publishes to
    NATS only when the state actually changes, and always mirrors the
    latest state to KV (idempotent — cheap PUT).
    """

    def __init__(
        self,
        *,
        publish: Optional[PublishFn] = None,
        kv_put: Optional[KVPutFn] = None,
        threshold: float = DEFAULT_THRESHOLD,
        hysteresis: float = DEFAULT_HYSTERESIS,
        subject: str = SUBJECT_BACKPRESSURE,
        kv_bucket: str = KV_BUCKET_BACKPRESSURE,
        kv_key: str = KV_KEY_CURRENT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._publish = publish
        self._kv_put = kv_put
        self._threshold = threshold
        self._hysteresis = hysteresis
        self._subject = subject
        self._kv_bucket = kv_bucket
        self._kv_key = kv_key
        self._clock = clock

        self._state: str = STATE_CLEAR
        self._last_snapshot: Optional[BackpressureSnapshot] = None
        self._last_publish_ok_ts: Optional[float] = None
        self._transitions: int = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_snapshot(self) -> Optional[BackpressureSnapshot]:
        return self._last_snapshot

    @property
    def transitions(self) -> int:
        """How many state changes we've broadcast in this lifetime.

        Test helper — production shouldn't rely on it.
        """
        return self._transitions

    async def tick(self, nodes: Iterable[NodeStress]) -> BackpressureSnapshot:
        """Evaluate probes, publish on transition, always mirror to KV.

        Returns the snapshot for the caller to log / measure.
        """
        snap = compute_backpressure(
            nodes,
            current_state=self._state,
            threshold=self._threshold,
            hysteresis=self._hysteresis,
        )
        self._last_snapshot = snap

        payload_bytes = json.dumps(snap.to_payload(), sort_keys=True).encode()

        if snap.changed:
            self._state = snap.state
            self._transitions += 1
            logger.info(
                "dispatch.backpressure transition -> state=%s stress=%.2f red=%s total=%d",
                snap.state,
                snap.stress_fraction,
                snap.red_nodes,
                snap.total_nodes,
            )
            if self._publish is not None:
                try:
                    await self._publish(self._subject, payload_bytes)
                    self._last_publish_ok_ts = self._clock()
                except Exception as exc:  # pragma: no cover - transport failures
                    logger.warning(
                        "dispatch.backpressure NATS publish failed: %s", exc
                    )

        # Always mirror to KV — cheap idempotent overwrite, gives late
        # subscribers the current state without waiting for a transition.
        if self._kv_put is not None:
            try:
                await self._kv_put(
                    self._kv_bucket, self._kv_key, payload_bytes
                )
            except Exception as exc:  # pragma: no cover - transport failures
                logger.warning(
                    "dispatch.backpressure KV put failed: %s", exc
                )

        return snap


# ── Convenience: bridge from HRVNodeGate probe cache ─────────────────────────


def nodes_from_probe_cache(
    probe_cache: Dict[str, Any],
    dispatcher_health_red: Optional[Dict[str, bool]] = None,
) -> List[NodeStress]:
    """Convert the HRV gate's per-hostname probe cache into NodeStress.

    Args:
      probe_cache: ``{hostname: NodeProbeSnapshot-like}`` — the gate's
        internal cache. We duck-type: any object with ``swap_pct`` and
        ``ts`` attributes works.
      dispatcher_health_red: optional ``{hostname: bool}`` from a systemd
        watcher. When absent, dispatcher_health is treated as green (only
        memory_pressure feeds the RED signal).

    This keeps the bridge symmetric with the spec's two RED conditions
    (memory_pressure, kanban_dispatcher_health) and avoids re-inventing
    probe caching in the broadcaster.
    """
    dhr = dispatcher_health_red or {}
    out: List[NodeStress] = []
    for hostname, probe in probe_cache.items():
        swap = getattr(probe, "swap_pct", None)
        mem_red = swap is not None and swap >= 90.0
        out.append(
            NodeStress(
                hostname=hostname,
                memory_pressure_red=bool(mem_red),
                dispatcher_health_red=bool(dhr.get(hostname, False)),
                ts=getattr(probe, "ts", None),
            )
        )
    return out


__all__ = [
    "SUBJECT_BACKPRESSURE",
    "KV_BUCKET_BACKPRESSURE",
    "KV_KEY_CURRENT",
    "DEFAULT_THRESHOLD",
    "DEFAULT_HYSTERESIS",
    "STATE_ACTIVE",
    "STATE_CLEAR",
    "NodeStress",
    "BackpressureSnapshot",
    "compute_backpressure",
    "BackpressureBroadcaster",
    "nodes_from_probe_cache",
]
