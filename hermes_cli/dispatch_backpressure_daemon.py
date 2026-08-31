"""Backpressure broadcast daemon — reads the HRV node gate's probe cache
and drives :class:`BackpressureBroadcaster.tick` on a timer.

This is the async runner that connects the two substrates:

  * HRV node gate cache (:class:`hermes_cli.hrv_node_gate.HRVNodeGate` —
    populated by a KV watcher elsewhere) as the *source* of per-node
    stress signals.
  * :class:`hermes_cli.dispatch_backpressure.BackpressureBroadcaster` as
    the *sink*: state machine + NATS publish + KV mirror.

Why a separate daemon (not wired into ``_dispatch_once_locked``):

  * The sync SQLite dispatch tick lock must NOT block on an ``await``.
    Running an async NATS publish under the lock would either deadlock
    (no running loop) or fragilise the lock's contract.
  * The gate probe cache is refreshed asynchronously by a KV subscriber
    already; sampling that cache on an independent timer keeps the
    two concerns cleanly separable.

Usage (systemd unit or long-lived process):

    async def _publish(subject: str, payload: bytes) -> None:
        await js.publish(subject, payload)

    async def _kv_put(bucket: str, key: str, value: bytes) -> None:
        kv = await js.key_value(bucket)
        await kv.put(key, value)

    broadcaster = BackpressureBroadcaster(publish=_publish, kv_put=_kv_put)
    daemon = BackpressureBroadcastDaemon(
        broadcaster=broadcaster,
        probe_source=get_default_gate,
        interval_seconds=5.0,
    )
    await daemon.run_forever()

The daemon has one job: sample → tick → repeat. Failures inside a tick
are logged and swallowed — a broken publish must never take down the
daemon (the same failure would take down every dispatcher subscribing
to the state, defeating the point of a soft-signal channel).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from hermes_cli.dispatch_backpressure import (
    BackpressureBroadcaster,
    BackpressureSnapshot,
    nodes_from_probe_cache,
)

logger = logging.getLogger(__name__)


ProbeSource = Callable[[], Any]
"""Zero-arg callable returning an HRVNodeGate-shaped object with a
``_node_cache`` mapping ``{hostname: NodeProbeSnapshot}``.

Injected so tests can drive the cache deterministically without a real
gate instance. Production wiring passes
:func:`hermes_cli.hrv_node_gate.get_default_gate`.
"""


DispatcherHealthSource = Callable[[], "dict[str, bool]"]
"""Optional zero-arg callable returning ``{hostname: dispatcher_red}``
per-node. When absent, dispatcher_health is treated as green (only
memory pressure feeds the RED signal). Injected so a systemd watcher
can supply live health data without the daemon knowing how it's
sampled.
"""


DEFAULT_INTERVAL_SECONDS = 5.0
"""How often the daemon samples the probe cache. 5s matches the HRV
gate's default probe staleness window (60s / 12 samples), giving the
subscribers a responsive but non-thrashing signal.
"""


class BackpressureBroadcastDaemon:
    """Async runner: sample probe cache → tick broadcaster → sleep.

    Not thread-safe; asyncio single-owner (one instance per process).
    Composed once at startup and driven via :meth:`run_forever`.
    """

    def __init__(
        self,
        *,
        broadcaster: BackpressureBroadcaster,
        probe_source: ProbeSource,
        dispatcher_health_source: Optional[DispatcherHealthSource] = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0.0:
            raise ValueError(
                f"interval_seconds must be > 0 (got {interval_seconds!r})"
            )
        self._broadcaster = broadcaster
        self._probe_source = probe_source
        self._dispatcher_health_source = dispatcher_health_source
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._ticks: int = 0
        self._last_snapshot: Optional[BackpressureSnapshot] = None

    @property
    def ticks(self) -> int:
        return self._ticks

    @property
    def last_snapshot(self) -> Optional[BackpressureSnapshot]:
        return self._last_snapshot

    def stop(self) -> None:
        """Signal the daemon to exit its loop at the next iteration."""
        self._stop.set()

    async def tick_once(self) -> Optional[BackpressureSnapshot]:
        """Sample the probe source once and drive one broadcaster tick.

        Never raises — sample / broadcast failures are logged and
        swallowed so the run loop keeps going.
        """
        try:
            gate = self._probe_source()
            probe_cache = getattr(gate, "_node_cache", None)
            if probe_cache is None or not probe_cache:
                # No probes yet — nothing to broadcast. Treat as
                # green: leaving the broadcaster's state unchanged
                # is the correct behaviour on empty input.
                return None
            dhr = (
                self._dispatcher_health_source()
                if self._dispatcher_health_source is not None
                else None
            )
            nodes = nodes_from_probe_cache(
                probe_cache, dispatcher_health_red=dhr,
            )
            snap = await self._broadcaster.tick(nodes)
            self._last_snapshot = snap
            self._ticks += 1
            return snap
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "dispatch.backpressure daemon tick failed: %s", exc,
                exc_info=True,
            )
            return None

    async def run_forever(self) -> None:
        """Drive :meth:`tick_once` on a fixed cadence until stopped."""
        logger.info(
            "dispatch.backpressure daemon starting (interval=%.1fs)",
            self._interval,
        )
        try:
            while not self._stop.is_set():
                await self.tick_once()
                # ``wait_for`` on the stop event lets us wake early on
                # stop() rather than sleeping the full interval.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._interval,
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            logger.info(
                "dispatch.backpressure daemon exiting (ticks=%d, last=%s)",
                self._ticks,
                (self._last_snapshot.state if self._last_snapshot else "<none>"),
            )


__all__ = [
    "BackpressureBroadcastDaemon",
    "DEFAULT_INTERVAL_SECONDS",
    "ProbeSource",
    "DispatcherHealthSource",
]
