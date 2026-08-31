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

    Mirrors the fields the HRV gate already caches. The spec (t_5d0beb8f)
    surfaces three RED conditions independently — the broadcaster checks
    each against the >50% node threshold separately so subscribers can
    tell WHY the cluster went into backpressure:

      * memory_pressure_red:      swap_pct >= 90% (or explicit OOM signal)
      * dispatcher_health_red:    kanban dispatcher systemd unit is
                                  activating / crashed in last 10min
      * urgent_state_red:         hrv.status.digest.interval_class ==
                                  'urgent' on this node

    ``is_red`` remains a convenience OR-fold for callers that only care
    whether the node is stressed at all.
    """

    hostname: str
    memory_pressure_red: bool = False
    dispatcher_health_red: bool = False
    urgent_state_red: bool = False
    ts: Optional[str] = None  # ISO8601 of the underlying probe timestamp

    @property
    def is_red(self) -> bool:
        """True if ANY RED condition holds."""
        return bool(
            self.memory_pressure_red
            or self.dispatcher_health_red
            or self.urgent_state_red
        )


@dataclass
class BackpressureSnapshot:
    """Result of a compute step.

    ``state`` is the *new* desired state, ``changed`` records whether it
    differs from the last-known state that was passed in.

    Per-probe fractions are surfaced so subscribers can see exactly WHICH
    trigger fired (and consumer-side circuit breakers can be tuned per
    probe if needed). ``stress_fraction`` is the max across probes — the
    scalar the hysteresis logic actually uses.
    """

    state: str
    red_nodes: List[str] = field(default_factory=list)
    stress_fraction: float = 0.0
    total_nodes: int = 0
    changed: bool = False
    ts: str = ""
    # Per-probe fractions (t_5d0beb8f — payload transparency for subscribers)
    memory_pressure_fraction: float = 0.0
    dispatcher_health_fraction: float = 0.0
    urgent_state_fraction: float = 0.0
    triggering_probes: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """JSON-serialisable NATS payload matching the spec.

        Subscribers use ``state`` for the hold/release decision and
        ``triggering_probes`` for context (which failure class fired).
        """
        if self.state == STATE_ACTIVE:
            return {
                "state": STATE_ACTIVE,
                "red_nodes": list(self.red_nodes),
                "stress_fraction": round(self.stress_fraction, 4),
                "total_nodes": self.total_nodes,
                "triggering_probes": list(self.triggering_probes),
                "probe_fractions": {
                    "memory_pressure": round(self.memory_pressure_fraction, 4),
                    "kanban_dispatcher_health": round(
                        self.dispatcher_health_fraction, 4
                    ),
                    "hrv_urgent_state": round(self.urgent_state_fraction, 4),
                },
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

    Per-probe threshold semantics (t_5d0beb8f):
      Trigger backpressure ACTIVE when the fraction of RED nodes on ANY
      single probe crosses ``threshold``. This is stricter than a
      union-fold, and matches the ticket body's "ANY of: >50% ... OR
      >50% ... OR >50% ..." wording. ``stress_fraction`` reported to
      subscribers is ``max(mem_frac, disp_frac, urgent_frac)``.

    Args:
      nodes: current per-node stress snapshots.
      current_state: the last-published state (``"active"`` / ``"clear"``);
        needed for hysteresis.
      threshold: fraction at or above which we go active (per probe).
      hysteresis: the buffer band below threshold we must fall through
        before clearing. Effective clear point = ``threshold - hysteresis``.
      now_iso: pinned timestamp for tests. Defaults to real wall-clock.

    Returns:
      A :class:`BackpressureSnapshot` capturing the next state, per-probe
      fractions, the list of triggering probes, and whether the state
      differs from ``current_state``.
    """
    nodes_list = list(nodes)
    total = len(nodes_list)
    ts = now_iso or _iso_now()

    if total == 0:
        # Fail-open: no probes yet → never go active (cold-cluster protection)
        return BackpressureSnapshot(
            state=STATE_CLEAR if current_state != STATE_ACTIVE else STATE_ACTIVE,
            red_nodes=[],
            stress_fraction=0.0,
            total_nodes=0,
            changed=False,
            ts=ts,
        )

    mem_count = sum(1 for n in nodes_list if n.memory_pressure_red)
    disp_count = sum(1 for n in nodes_list if n.dispatcher_health_red)
    urgent_count = sum(1 for n in nodes_list if n.urgent_state_red)

    mem_frac = mem_count / total
    disp_frac = disp_count / total
    urgent_frac = urgent_count / total

    # Union of nodes that are RED on at least one probe — useful for the
    # subscriber's "which hostnames are hot" list even if only one probe
    # is above threshold.
    red_hostnames = sorted(n.hostname for n in nodes_list if n.is_red)

    stress = max(mem_frac, disp_frac, urgent_frac)

    # Which probes actually crossed threshold — surfaced in the payload
    # so the subscriber can log/route on the specific failure class.
    triggering: List[str] = []
    if mem_frac >= threshold:
        triggering.append("memory_pressure")
    if disp_frac >= threshold:
        triggering.append("kanban_dispatcher_health")
    if urgent_frac >= threshold:
        triggering.append("hrv_urgent_state")

    common_kwargs: Dict[str, Any] = dict(
        red_nodes=red_hostnames,
        stress_fraction=stress,
        total_nodes=total,
        ts=ts,
        memory_pressure_fraction=mem_frac,
        dispatcher_health_fraction=disp_frac,
        urgent_state_fraction=urgent_frac,
    )

    if current_state == STATE_ACTIVE:
        # Only clear when ALL probes drop through the lower band. This is
        # symmetric with the "any-probe crosses" trigger: we hold active
        # until every trigger has receded, preventing flap when one probe
        # clears but another is still hot.
        clear_point = threshold - hysteresis
        any_still_hot = (
            mem_frac >= clear_point
            or disp_frac >= clear_point
            or urgent_frac >= clear_point
        )
        if not any_still_hot:
            return BackpressureSnapshot(
                state=STATE_CLEAR,
                changed=True,
                triggering_probes=[],
                **common_kwargs,
            )
        return BackpressureSnapshot(
            state=STATE_ACTIVE,
            changed=False,
            triggering_probes=triggering,
            **common_kwargs,
        )

    # current_state == "clear" (or unknown → treat as clear)
    if triggering:
        return BackpressureSnapshot(
            state=STATE_ACTIVE,
            changed=True,
            triggering_probes=triggering,
            **common_kwargs,
        )
    return BackpressureSnapshot(
        state=STATE_CLEAR,
        changed=False,
        triggering_probes=[],
        **common_kwargs,
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
    urgent_state_red: Optional[Dict[str, bool]] = None,
) -> List[NodeStress]:
    """Convert the HRV gate's per-hostname probe cache into NodeStress.

    Args:
      probe_cache: ``{hostname: NodeProbeSnapshot-like}`` — the gate's
        internal cache. We duck-type: any object with ``swap_pct`` and
        ``ts`` attributes works. If the snapshot also carries an
        ``interval_class`` attribute (per-node HRV digest), the "urgent"
        signal is derived from it automatically; callers can still
        override via the ``urgent_state_red`` map.
      dispatcher_health_red: optional ``{hostname: bool}`` from a systemd
        watcher. When absent, dispatcher_health is treated as green (only
        memory_pressure + urgent_state feed the RED signal).
      urgent_state_red: optional ``{hostname: bool}`` for the third
        trigger from t_5d0beb8f (``hrv.status.digest.interval_class ==
        'urgent'`` on that node). When absent AND the probe object does
        not expose ``interval_class``, the trigger is treated as green.
        When the cluster only has a *global* HRV digest (common today),
        callers should broadcast the same boolean to every hostname in
        the cache.

    Keeps the bridge symmetric with the spec's three RED conditions
    (memory_pressure, kanban_dispatcher_health, hrv_urgent_state) and
    avoids re-inventing probe caching in the broadcaster.
    """
    dhr = dispatcher_health_red or {}
    usr = urgent_state_red or {}
    out: List[NodeStress] = []
    for hostname, probe in probe_cache.items():
        swap = getattr(probe, "swap_pct", None)
        mem_red = swap is not None and swap >= 90.0

        # Prefer explicit override, then derive from probe.interval_class
        # if the snapshot carries it. Case-insensitive on the value so
        # producers that vary ('Urgent' vs 'urgent') still match.
        if hostname in usr:
            urgent_red = bool(usr[hostname])
        else:
            ic = getattr(probe, "interval_class", None)
            urgent_red = isinstance(ic, str) and ic.strip().lower() == "urgent"

        out.append(
            NodeStress(
                hostname=hostname,
                memory_pressure_red=bool(mem_red),
                dispatcher_health_red=bool(dhr.get(hostname, False)),
                urgent_state_red=urgent_red,
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
