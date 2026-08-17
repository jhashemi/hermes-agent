"""HRV velocity factor cache for the kanban dispatcher.

Subscribes to ``hrv.pulse.tick`` on NATS and caches the latest
per-agent velocity_factor from the pulse payload's ``agents`` dict.
The kanban dispatcher reads this cache to adjust task dispatch
ordering — agents with low velocity_factor (STRESS/CRISIS autonomic
state) get deprioritized relative to HOMEOSTATIC agents.

Thread-safety: a single ``threading.Lock`` guards the dict. The
NATS subscription runs in a background asyncio task in the gateway's
event loop; the dispatcher reads from a worker thread via
``asyncio.to_thread``. The lock is held for a trivial dict copy,
so contention is negligible.

Failure modes:
  - NATS unavailable → cache stays empty → all agents get
    velocity_factor=1.0 (the safe default — no agent is penalized
    without positive evidence of stress).
  - Malformed pulse payload → skipped at parse time, cache
    unchanged.
  - No agents in payload → cache unchanged (no agents to update).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
# Max age in seconds for a cached velocity factor. If the latest tick
# is older than this, we treat the agent as HOMEOSTATIC (1.0) — stale
# HRV data is worse than no HRV data.
MAX_CACHE_AGE_SEC = 300  # 5 minutes (5 pulse ticks at 60s)


class HRVVelocityCache:
    """Thread-safe cache of per-agent velocity_factor from hrv.pulse.tick.

    Usage:
        cache = HRVVelocityCache()
        await cache.start()  # starts NATS subscription
        vf = cache.velocity_factor("margaret_hamilton")  # 0.5, 0.75, or 1.0
        cache.stop()
    """

    def __init__(self, nats_url: str | None = None) -> None:
        self._nats_url = nats_url or NATS_URL
        self._lock = threading.Lock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._last_tick_ts: float = 0.0
        self._nc: Any = None
        self._task: asyncio.Task | None = None
        self._running = False

    def velocity_factor(self, agent_id: str) -> float:
        """Return the cached velocity_factor for an agent.

        Returns 1.0 (HOMEOSTATIC) when:
          - agent not in cache
          - cache is stale (last tick > MAX_CACHE_AGE_SEC ago)
          - payload missing velocity_factor
        This is the safe default — never penalize without evidence.
        """
        with self._lock:
            if time.time() - self._last_tick_ts > MAX_CACHE_AGE_SEC:
                return 1.0
            agent_data = self._agents.get(agent_id)
            if agent_data is None:
                return 1.0
            vf = agent_data.get("velocity_factor")
            if vf is None or not isinstance(vf, (int, float)):
                return 1.0
            return float(vf)

    def autonomic_state(self, agent_id: str) -> str:
        """Return the cached autonomic_state for an agent.

        Returns "HOMEOSTATIC" when agent not in cache or cache is stale.
        """
        with self._lock:
            if time.time() - self._last_tick_ts > MAX_CACHE_AGE_SEC:
                return "HOMEOSTATIC"
            agent_data = self._agents.get(agent_id)
            if agent_data is None:
                return "HOMEOSTATIC"
            return agent_data.get("autonomic_state", "HOMEOSTATIC")

    def all_agents(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of the full agents cache."""
        with self._lock:
            if time.time() - self._last_tick_ts > MAX_CACHE_AGE_SEC:
                return {}
            return dict(self._agents)

    def is_stale(self) -> bool:
        """True if the cache has no data or data is older than MAX_CACHE_AGE_SEC."""
        with self._lock:
            return time.time() - self._last_tick_ts > MAX_CACHE_AGE_SEC

    def _on_tick(self, msg: Any) -> None:
        """Sync callback for hrv.pulse.tick subscription.

        Parses the pulse payload and updates the cache. Never raises —
        a malformed tick must not crash the subscription.
        """
        try:
            payload = json.loads(msg.data.decode())
            agents = payload.get("agents")
            if not isinstance(agents, dict) or not agents:
                return
            with self._lock:
                self._agents = agents
                self._last_tick_ts = time.time()
            logger.debug(
                "hrv velocity cache: updated from tick seq=%s agents=%d",
                payload.get("seq"), len(agents),
            )
        except Exception as e:  # noqa: BLE001 — sensor pattern
            logger.warning("hrv velocity cache: dropped malformed tick: %s", e)

    async def _subscribe_loop(self) -> None:
        """Connect to NATS and subscribe to hrv.pulse.tick."""
        try:
            import nats
        except ImportError:
            logger.warning("hrv velocity cache: nats-py not installed; disabled")
            return

        while self._running:
            try:
                self._nc = await nats.connect(
                    self._nats_url,
                    name="hrv-velocity-cache",
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=5,
                )
                await self._nc.subscribe("hrv.pulse.tick", cb=self._on_tick)
                logger.info("hrv velocity cache: subscribed to hrv.pulse.tick on %s",
                            self._nats_url)
                # Keep the connection alive until stopped
                while self._running:
                    await asyncio.sleep(1)
                await self._nc.drain()
                await self._nc.close()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("hrv velocity cache: connection failed (%s); retrying", e)
                await asyncio.sleep(5)

    async def start(self) -> None:
        """Start the NATS subscription in the background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._subscribe_loop())

    def stop(self) -> None:
        """Signal the subscription to stop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None


# ── Module-level singleton ────────────────────────────────────────
# The gateway creates one instance and shares it across dispatch ticks.
# Other processes (CLI one-shots) use the module-level getter.
_singleton: HRVVelocityCache | None = None
_singleton_lock = threading.Lock()


def get_velocity_cache() -> HRVVelocityCache:
    """Return the process-level HRVVelocityCache singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = HRVVelocityCache()
        return _singleton