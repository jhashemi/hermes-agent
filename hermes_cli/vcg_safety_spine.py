"""HAMILTON CRITICAL-3 safety spine for VCG cluster dispatcher.

This is the last code gap before the F1 merge train (t_64a6a7bd, parent
t_b501de4f). Every prior F1 child skipped CRITICAL-3; grep-verified absent
by jeff_dean run 550. Without this module, VCG arming is a no-go.

Three orthogonal safety gates, each independent of NATS so a broken
broker cannot mask them:

1. **Out-of-band kill switch** at ``/tmp/vcg_dispatch_kill``
   - Poll via :py:meth:`pathlib.Path.exists` (which stat's the inode).
   - If the file exists, the dispatcher must halt new spawns within one
     poll cycle (default 5 s). Removing the file re-enables spawns.
   - Filesystem-only: works even when NATS is dead, when Bedrock is
     rate-limited, when the LLM router is disabled. The operator's
     lock-of-last-resort.

2. **NATS liveness via direct TCP probe**
   - Open a real ``socket.create_connection`` to ``127.0.0.1:4222`` (or
     configured host/port) every 30 s (``NATS_PROBE_INTERVAL_S``). NATS
     accepts TCP immediately on a healthy port; a refused connection or a
     3-second connect timeout (``NATS_PROBE_TIMEOUT_S``) is definitive
     proof the broker is down.
   - On sustained failure we enter *safe mode*: routing decisions still
     compute locally but no NATS publishes fire, and the transition is
     logged to a local file (``/tmp/vcg_dispatch_safe_mode.log`` by
     default) — NEVER to NATS, since NATS is the failed dependency.

3. **Armed-token assertion**
   - At startup the worker asserts ``/etc/vcg_dispatch_armed`` exists on
     disk. If it does not exist the worker refuses to arm and exits
     non-zero.
   - Workers must NEVER self-create this file: doing so would defeat the
     entire out-of-band arming protocol. This module deliberately exposes
     only a read-only :py:func:`assert_armed` and never a writer.

Design invariants
-----------------
- All I/O is real: real filesystem, real socket. The mandated TDD tests
  MUST NOT mock either surface (per DoD in the task body).
- Every function is a pure gate: it either returns / passes / raises,
  never mutates cluster state.
- The safety spine is composable with the existing HRV node gate and
  dispatch backpressure signal — it runs *before* both, because a kill
  switch that requires healthy NATS is not a kill switch.

Author: t_64a6a7bd (F1-A HAMILTON CRITICAL-3), branched off main tip
7bb2fdbe4c06bf14c107589d9306b978cd51b8df.
"""
from __future__ import annotations

import datetime
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_KILL_FILE = Path("/tmp/vcg_dispatch_kill")
DEFAULT_ARMED_FILE = Path("/etc/vcg_dispatch_armed")
DEFAULT_SAFE_MODE_LOG = Path("/tmp/vcg_dispatch_safe_mode.log")

DEFAULT_NATS_HOST = "127.0.0.1"
DEFAULT_NATS_PORT = 4222

# Poll cadences (seconds). Task body pins these — do not weaken them.
KILL_POLL_INTERVAL_S = 5.0
NATS_PROBE_INTERVAL_S = 30.0
NATS_PROBE_TIMEOUT_S = 3.0  # socket connect timeout per attempt


# ── AMBER / RED / GREEN memory state ──────────────────────────────────────────

STATE_GREEN = "green"
STATE_AMBER = "amber"
STATE_RED = "red"

# High-resource task hint: consult ``task["min_resources"]["mem_gb"]`` or a
# ``high_memory`` flag on the task metadata.
#
# NOTE (t_ad7e65f9): the canonical key is ``mem_gb`` — it matches
# :data:`hermes_cli.kanban_db.SUPPORTED_MIN_RESOURCE_KEYS` (shipped by
# t_ce60f550) which is what ``get_task_min_resources()`` returns, and it is
# what the sibling HRV node gate reads (see ``hrv_node_gate.py`` line ~269).
# The legacy ``memory_gb`` spelling is accepted only as a fallback so that
# an in-flight hand-authored task dict from before the schema landed still
# classifies correctly. New callers MUST emit ``mem_gb``.
HIGH_MEMORY_GB_THRESHOLD = 4.0


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class SafeModeState:
    """Snapshot of NATS-liveness safe-mode toggle."""

    active: bool = False
    since: Optional[float] = None  # unix ts when we entered safe mode
    last_probe_ok_ts: Optional[float] = None
    consecutive_failures: int = 0

    def enter(self, now: float) -> None:
        if not self.active:
            self.active = True
            self.since = now

    def clear(self, now: float) -> None:
        self.active = False
        self.since = None
        self.consecutive_failures = 0
        self.last_probe_ok_ts = now


# ── 1. Kill switch ────────────────────────────────────────────────────────────


class KillSwitch:
    """Filesystem-backed out-of-band halt for new dispatcher spawns.

    Every tick calls :py:meth:`engaged`. When ``True`` the dispatcher must
    skip spawn creation for this tick. The check is a single ``stat()``,
    so at 5 s cadence this is <1 ms of overhead per tick.
    """

    def __init__(self, path: Path = DEFAULT_KILL_FILE):
        self.path = Path(path)

    def engaged(self) -> bool:
        """Return True iff the kill file exists.

        Uses :py:meth:`Path.exists` which internally ``stat``s. If the file
        was created after the last poll but before this call it will be
        seen this tick — no caching, no memoization, no stale window.
        """
        try:
            return self.path.exists()
        except OSError as e:
            # Path unreachable (permissions, mount issue) — err on the side
            # of *engaging* the kill switch: an unreachable safety file is
            # more dangerous than a false halt. Operators can inspect the
            # dispatcher log and unblock explicitly.
            logger.warning(
                "[KillSwitch] stat(%s) failed: %s — treating as engaged (fail-safe).",
                self.path,
                e,
            )
            return True

    def poll_engaged(self, deadline_s: float, interval_s: float = KILL_POLL_INTERVAL_S) -> bool:
        """Poll until engaged or ``deadline_s`` elapses; return final state.

        Provided for tests that want to assert 'within one poll cycle'
        semantics without racing wall-clock time. Production dispatchers
        call :py:meth:`engaged` from their own tick loop instead.
        """
        end = time.monotonic() + max(0.0, deadline_s)
        while True:
            if self.engaged():
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(interval_s, remaining))


# ── 2. NATS TCP liveness probe ────────────────────────────────────────────────


def probe_nats_tcp(
    host: str = DEFAULT_NATS_HOST,
    port: int = DEFAULT_NATS_PORT,
    timeout_s: float = NATS_PROBE_TIMEOUT_S,
) -> bool:
    """Return True iff a TCP connect to ``host:port`` succeeds within ``timeout_s``.

    We do NOT speak the NATS protocol — that would require the ``nats-py``
    library, which itself talks to NATS and is the very dependency we're
    trying to validate independently. A plain TCP handshake is enough:
    NATS opens its port even before the ``INFO`` frame lands, so if the
    port refuses or times out the broker is definitively down.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            # We opened the connection successfully — the broker's listen
            # socket is alive. Close cleanly to avoid piling up half-open
            # connections in NATS' accept queue.
            sock.shutdown(socket.SHUT_RDWR)
        return True
    except (OSError, socket.timeout) as e:
        logger.debug("[NATSProbe] %s:%s connect failed: %s", host, port, e)
        return False


class NATSLivenessMonitor:
    """Track NATS-liveness state via periodic direct TCP probes.

    The monitor does not thread on its own — the dispatcher's tick loop
    calls :py:meth:`tick` and inspects :py:attr:`state`. This keeps the
    module deterministic under test and avoids a thread that could
    outlive the dispatcher process.

    Safe-mode semantics
    -------------------
    - Enter *safe mode* on the first failed probe (defensive).
    - Clear safe mode as soon as a probe succeeds.
    - Log every enter/clear transition to a local file — never NATS.
    """

    def __init__(
        self,
        host: str = DEFAULT_NATS_HOST,
        port: int = DEFAULT_NATS_PORT,
        interval_s: float = NATS_PROBE_INTERVAL_S,
        timeout_s: float = NATS_PROBE_TIMEOUT_S,
        safe_mode_log: Path = DEFAULT_SAFE_MODE_LOG,
        probe: Optional[Callable[[str, int, float], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.host = host
        self.port = port
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.safe_mode_log = Path(safe_mode_log)
        self._probe = probe or probe_nats_tcp
        self._clock = clock or time.time
        self.state = SafeModeState()
        self._last_probe_ts: float = 0.0
        self._lock = threading.Lock()

    def _write_transition(self, transition: str, ts: float) -> None:
        """Append a transition line to the safe-mode log (never to NATS)."""
        try:
            self.safe_mode_log.parent.mkdir(parents=True, exist_ok=True)
            iso = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
            with self.safe_mode_log.open("a", encoding="utf-8") as fp:
                fp.write(f"{iso} {transition} host={self.host} port={self.port}\n")
        except OSError as e:
            # We failed to write the safe-mode log itself. This is very bad
            # but recoverable: emit to stderr via the logger. Do NOT try to
            # publish to NATS.
            logger.error("[NATSMonitor] safe-mode log write failed: %s", e)

    def tick(self, *, force: bool = False) -> SafeModeState:
        """Run at most one probe per ``interval_s`` and update state."""
        with self._lock:
            now = self._clock()
            if not force and (now - self._last_probe_ts) < self.interval_s:
                return self.state
            self._last_probe_ts = now
            ok = self._probe(self.host, self.port, self.timeout_s)
            if ok:
                if self.state.active:
                    self._write_transition("safe_mode_clear", now)
                    logger.info(
                        "[NATSMonitor] NATS liveness restored (%s:%s) — clearing safe mode.",
                        self.host,
                        self.port,
                    )
                self.state.clear(now)
            else:
                self.state.consecutive_failures += 1
                if not self.state.active:
                    self._write_transition("safe_mode_enter", now)
                    logger.warning(
                        "[NATSMonitor] NATS %s:%s failed TCP probe — entering safe mode.",
                        self.host,
                        self.port,
                    )
                self.state.enter(now)
            return self.state

    def in_safe_mode(self) -> bool:
        return self.state.active


# ── 3. Armed-token assertion ──────────────────────────────────────────────────


class ArmedTokenMissing(RuntimeError):
    """Raised at startup when the armed-token file is not present."""


def assert_armed(path: Path = DEFAULT_ARMED_FILE) -> None:
    """Refuse to arm unless ``path`` exists on disk.

    This is *read-only* by design. There is no companion ``arm()`` function
    in this module because self-creation of the token defeats the purpose:
    an operator must place the token out-of-band (systemd drop-in,
    ansible, manual ``touch``) so that a fresh worker cannot arm itself
    into a runaway loop.
    """
    p = Path(path)
    if not p.exists():
        raise ArmedTokenMissing(
            f"VCG dispatcher refused to arm: {p} not found. "
            "Operator must place this token out-of-band before dispatch "
            "may spawn workers. Workers MUST NOT self-create it."
        )
    if not p.is_file():
        raise ArmedTokenMissing(
            f"VCG dispatcher refused to arm: {p} exists but is not a regular file."
        )


# ── 4. AMBER-state high-memory routing gate ───────────────────────────────────


def task_is_high_memory(task: dict) -> bool:
    """Return True iff a task is 'high-resource' by memory footprint.

    Priority:

    1. Explicit ``task["high_memory"] is True``.
    2. ``task["min_resources"]["mem_gb"]`` >= threshold (CANONICAL key —
       matches ``hermes_cli.kanban_db.DEFAULT_MIN_RESOURCES`` and what
       ``get_task_min_resources()`` returns).
    3. ``task["min_resources"]["memory_gb"]`` >= threshold (LEGACY spelling,
       accepted only for pre-schema hand-authored dicts; new callers MUST
       emit ``mem_gb``).
    4. Otherwise, treat as ordinary (non-high-memory).

    Fix history: t_ad7e65f9 — original implementation only read
    ``memory_gb``, so DB-sourced tasks (which carry ``mem_gb`` via the
    ``min_resources`` column) never classified high-memory and the AMBER
    gate silently under-blocked.
    """
    if task.get("high_memory") is True:
        return True
    mr = task.get("min_resources") or {}
    # Canonical key first; legacy ``memory_gb`` only if ``mem_gb`` is absent.
    raw = mr.get("mem_gb")
    if raw is None:
        raw = mr.get("memory_gb")
    try:
        mem_gb = float(raw or 0)
    except (TypeError, ValueError):
        mem_gb = 0.0
    return mem_gb >= HIGH_MEMORY_GB_THRESHOLD


def amber_blocks_task(memory_state: str, task: dict) -> bool:
    """Return True iff AMBER memory-state should REJECT this task now.

    Semantics (per task body #4): AMBER is graceful, not a cliff. It
    blocks HIGH-resource tasks only; ordinary tasks continue to route.
    RED blocks everything (upstream HRV node gate handles that).
    """
    state = (memory_state or "").strip().lower()
    if state == STATE_GREEN:
        return False
    if state == STATE_RED:
        return True  # RED blocks all — upstream gate will also reject
    if state == STATE_AMBER:
        return task_is_high_memory(task)
    # Unknown state — fail-open: don't block. The deterministic hard
    # gates further downstream still authoritative.
    return False


# ── 5. Composite entry point ──────────────────────────────────────────────────


@dataclass
class SafetySpineDecision:
    """Composite decision for one dispatcher tick."""

    halt_all_spawns: bool = False
    reason: str = ""
    safe_mode: bool = False
    kill_engaged: bool = False


class SafetySpine:
    """Compose kill-switch + NATS-liveness monitor into one tick decision.

    Usage from the dispatcher driver::

        spine = SafetySpine()
        spine.assert_armed_or_die()          # once, at startup
        ...
        decision = spine.tick()
        if decision.halt_all_spawns:
            logger.warning("[SafetySpine] halted: %s", decision.reason)
            return  # skip spawn phase this tick
        if decision.safe_mode:
            # NATS is dead — skip NATS publishes but continue local routing
            ...
    """

    def __init__(
        self,
        kill_switch: Optional[KillSwitch] = None,
        nats_monitor: Optional[NATSLivenessMonitor] = None,
        armed_file: Path = DEFAULT_ARMED_FILE,
    ):
        self.kill_switch = kill_switch or KillSwitch()
        self.nats_monitor = nats_monitor or NATSLivenessMonitor()
        self.armed_file = Path(armed_file)

    def assert_armed_or_die(self) -> None:
        assert_armed(self.armed_file)

    def tick(self) -> SafetySpineDecision:
        decision = SafetySpineDecision()
        if self.kill_switch.engaged():
            decision.halt_all_spawns = True
            decision.kill_engaged = True
            decision.reason = f"kill file present at {self.kill_switch.path}"
            return decision
        state = self.nats_monitor.tick()
        decision.safe_mode = state.active
        if state.active:
            decision.reason = (
                f"NATS liveness lost — safe mode (failures="
                f"{state.consecutive_failures})"
            )
        return decision


__all__ = [
    "STATE_GREEN",
    "STATE_AMBER",
    "STATE_RED",
    "HIGH_MEMORY_GB_THRESHOLD",
    "DEFAULT_KILL_FILE",
    "DEFAULT_ARMED_FILE",
    "DEFAULT_SAFE_MODE_LOG",
    "DEFAULT_NATS_HOST",
    "DEFAULT_NATS_PORT",
    "KILL_POLL_INTERVAL_S",
    "NATS_PROBE_INTERVAL_S",
    "NATS_PROBE_TIMEOUT_S",
    "SafeModeState",
    "KillSwitch",
    "NATSLivenessMonitor",
    "probe_nats_tcp",
    "ArmedTokenMissing",
    "assert_armed",
    "task_is_high_memory",
    "amber_blocks_task",
    "SafetySpineDecision",
    "SafetySpine",
]
