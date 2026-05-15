"""
kanban_thread_pool.py — Tiered thread-pool slot manager for the Kanban dispatcher.

Design
──────
Workers in the Kanban system are OS processes (not threads), but the
*scheduling decisions* (claim → spawn → track PID) happen on the daemon
thread.  Under load, many concurrent spawn decisions hit the same SQLite
connection and the same OS fork() path.  Without a pool, it's trivially
possible to exceed ``max_concurrent_board`` during a single tick if
dispatch_once is called re-entrantly or from multiple coroutines.

This module provides:

  WorkerSlot
      A lightweight Disposable token.  Acquiring one means "the pool has
      reserved a slot for you; go ahead and spawn."  Releasing (via
      context-manager __exit__ or explicit .release()) returns the slot
      to the pool.  The Disposable pattern guarantees the slot is
      returned even if the spawn raises.

  ThreadPool (misleadingly named — these are *process-spawn budget slots*,
      not threads, but the pattern is identical to a thread pool semaphore)
      Tracks three priority tiers: HIGH / NORMAL / LOW.
      HIGH slots are reserved for retried tasks (consecutive_failures > 0).
      NORMAL slots are the default lane.
      LOW slots are for tasks that have been waiting the longest.

  PoolStats
      Lightweight dataclass returned by ThreadPool.stats() for health
      telemetry / the circuit-breaker status endpoint.

Usage (from dispatch_once)
──────────────────────────

    pool = ThreadPool.for_board("default", board_cap=6, per_profile_cap=2)

    for row in ready_rows:
        slot = pool.acquire(assignee=row["assignee"],
                            priority=_row_priority(row),
                            timeout=0)        # non-blocking
        if slot is None:
            result.deferred_concurrency.append((row["id"], "pool exhausted"))
            continue

        with slot:                            # <- Disposable: auto-released on exit
            pid = spawn_fn(task, workspace)
            _set_worker_pid(conn, task_id, pid)

    pool.sync_from_db(conn)  # optional: rehydrate from DB after tick

Disposable protocol
────────────────────
WorkerSlot implements both the context-manager protocol AND an explicit
.release() method.  Callers should use `with slot:` whenever possible.
Explicit .release() is provided for cases where the lifetime crosses
function boundaries (e.g. the slot is acquired in dispatch_once but
released in the on_worker_exit callback).

    slot = pool.acquire(assignee="backend-eng", priority=Priority.NORMAL)
    if slot is None:
        ...  # pool full
    try:
        do_work(slot)
    finally:
        slot.release()   # idempotent -- safe to call twice
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


# -- Priority tiers ------------------------------------------------------------

class Priority(IntEnum):
    HIGH   = 3   # retried tasks (consecutive_failures > 0)
    NORMAL = 2   # default
    LOW    = 1   # long-waiting tasks (oldest created_at)


# -- Slot token (Disposable) ---------------------------------------------------

class WorkerSlot:
    """A reservation token from a :class:`ThreadPool`.

    Implements the Disposable pattern:
    - Use as a context manager (``with slot: ...``) for automatic return.
    - Or call ``.release()`` explicitly (idempotent).

    Attributes
    ----------
    assignee : str
        The profile name this slot was reserved for.
    priority : Priority
        The priority lane this slot came from.
    acquired_at : float
        Monotonic timestamp when the slot was acquired.
    task_id : str | None
        Optionally set by the caller for diagnostic tracking.
    """

    __slots__ = ("_pool", "assignee", "priority", "acquired_at", "task_id", "_released")

    def __init__(
        self,
        pool: "ThreadPool",
        assignee: str,
        priority: Priority,
    ) -> None:
        self._pool = pool
        self.assignee = assignee
        self.priority = priority
        self.acquired_at = time.monotonic()
        self.task_id: Optional[str] = None
        self._released = False

    # -- Disposable ------------------------------------------------------------

    def release(self) -> None:
        """Return this slot to the pool.  Idempotent."""
        if not self._released:
            self._released = True
            self._pool._release(self)
            log.debug(
                "thread_pool slot released assignee=%s priority=%s held=%.2fs task=%s",
                self.assignee,
                self.priority.name,
                time.monotonic() - self.acquired_at,
                self.task_id,
            )

    def __enter__(self) -> "WorkerSlot":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    def __del__(self) -> None:
        # Safety net: if the caller forgets to release, GC reclaims the slot.
        if not self._released:
            log.warning(
                "thread_pool WorkerSlot GC-released (forgot context manager?) "
                "assignee=%s task=%s",
                self.assignee,
                self.task_id,
            )
            self.release()

    def __repr__(self) -> str:
        state = "released" if self._released else "held"
        return (
            f"<WorkerSlot {state} assignee={self.assignee!r} "
            f"priority={self.priority.name} task={self.task_id!r}>"
        )


# -- Pool stats ----------------------------------------------------------------

@dataclass
class PoolStats:
    board:               str
    board_cap:           int
    per_profile_cap:     int
    total_active:        int
    total_available:     int
    high_active:         int
    normal_active:       int
    low_active:          int
    per_profile:         Dict[str, int] = field(default_factory=dict)
    oldest_slot_age_s:   Optional[float] = None


# -- Thread pool ---------------------------------------------------------------

class ThreadPool:
    """Tiered spawn-slot pool for the Kanban dispatcher.

    Enforces:
    - Board-wide cap (``board_cap``, default 6).
    - Per-profile cap (``per_profile_cap``, default 2).
    - Priority tiering: HIGH > NORMAL > LOW.

    Slots are :class:`WorkerSlot` Disposables: acquire one, use it,
    context-manager releases it back automatically.

    Parameters
    ----------
    board : str
        Board name for logging / stats.
    board_cap : int
        Maximum total active slots.
    per_profile_cap : int
        Maximum slots per assignee profile.
    high_reserved : int
        Slots reserved exclusively for HIGH-priority tasks.
        HIGH tasks can always use these even when NORMAL/LOW would be
        denied.  Must be < board_cap.
    """

    def __init__(
        self,
        board: str = "default",
        board_cap: int = 6,
        per_profile_cap: int = 2,
        high_reserved: int = 2,
    ) -> None:
        if high_reserved >= board_cap:
            raise ValueError("high_reserved must be < board_cap")
        self._board = board
        self._board_cap = board_cap
        self._per_profile_cap = per_profile_cap
        self._high_reserved = high_reserved

        self._lock = threading.Lock()
        # Active slots indexed by id(slot) for O(1) release.
        self._active: Dict[int, WorkerSlot] = {}
        # Per-profile active count.
        self._profile_counts: Dict[str, int] = {}

    # -- Factory ---------------------------------------------------------------

    @classmethod
    def for_board(
        cls,
        board: str = "default",
        board_cap: int = 6,
        per_profile_cap: int = 2,
        high_reserved: int = 2,
    ) -> "ThreadPool":
        """Convenience constructor; mirrors the config-key names."""
        return cls(
            board=board,
            board_cap=board_cap,
            per_profile_cap=per_profile_cap,
            high_reserved=high_reserved,
        )

    # -- Acquire ---------------------------------------------------------------

    def acquire(
        self,
        assignee: str,
        priority: Priority = Priority.NORMAL,
        timeout: float = 0.0,
        task_id: Optional[str] = None,
    ) -> Optional[WorkerSlot]:
        """Try to acquire a slot.

        Parameters
        ----------
        assignee : str
            Profile name for the worker (used for per-profile cap).
        priority : Priority
            Determines which reservation tier to draw from.
        timeout : float
            Seconds to block waiting for a slot.  ``0`` is non-blocking
            (returns None immediately if pool is full).  Use sparingly --
            the dispatcher loop is designed to defer and retry next tick.
        task_id : str | None
            Optional task ID for diagnostic tracking.

        Returns
        -------
        WorkerSlot or None
            None when no slot is available within ``timeout``.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                slot = self._try_acquire(assignee, priority, task_id)
                if slot is not None:
                    return slot
                if time.monotonic() >= deadline:
                    return None
            # Wait a tiny bit before retrying (only matters when timeout > 0).
            time.sleep(0.05)

    def _try_acquire(
        self,
        assignee: str,
        priority: Priority,
        task_id: Optional[str],
    ) -> Optional[WorkerSlot]:
        """Internal: attempt acquisition under self._lock."""
        total = len(self._active)
        profile_count = self._profile_counts.get(assignee, 0)

        # Per-profile cap always applies.
        if profile_count >= self._per_profile_cap:
            log.debug(
                "thread_pool acquire denied: profile=%s at cap %d/%d",
                assignee, profile_count, self._per_profile_cap,
            )
            return None

        # Board-wide cap: HIGH priority can use reserved slots;
        # NORMAL/LOW cannot use the last ``_high_reserved`` slots.
        normal_available = self._board_cap - self._high_reserved
        if priority == Priority.HIGH:
            if total >= self._board_cap:
                log.debug(
                    "thread_pool acquire denied: board full %d/%d (HIGH)",
                    total, self._board_cap,
                )
                return None
        else:
            if total >= normal_available:
                log.debug(
                    "thread_pool acquire denied: board at normal cap %d/%d priority=%s",
                    total, normal_available, priority.name,
                )
                return None

        slot = WorkerSlot(self, assignee, priority)
        slot.task_id = task_id
        self._active[id(slot)] = slot
        self._profile_counts[assignee] = profile_count + 1
        log.debug(
            "thread_pool slot acquired assignee=%s priority=%s total=%d/%d task=%s",
            assignee, priority.name, len(self._active), self._board_cap, task_id,
        )
        return slot

    # -- Release (called by WorkerSlot.release()) ------------------------------

    def _release(self, slot: WorkerSlot) -> None:
        with self._lock:
            self._active.pop(id(slot), None)
            count = self._profile_counts.get(slot.assignee, 0)
            if count > 0:
                self._profile_counts[slot.assignee] = count - 1
            else:
                self._profile_counts.pop(slot.assignee, None)

    # -- Sync from DB ----------------------------------------------------------

    def sync_from_db(self, conn: sqlite3.Connection) -> None:
        """Reconcile in-memory counts with DB running-task reality.

        Call at the start of each dispatch tick to handle slots that were
        leaked (e.g. daemon restart while workers were running).  Any
        profile whose DB count exceeds the in-memory count gets topped up
        by synthetic orphan slots; excess in-memory slots are removed.

        This is a best-effort reconciliation -- it does not create/destroy
        actual OS processes.
        """
        rows = conn.execute(
            "SELECT assignee, COUNT(*) as cnt FROM tasks "
            "WHERE status = 'running' GROUP BY assignee"
        ).fetchall()
        db_counts: Dict[str, int] = {r["assignee"]: r["cnt"] for r in rows if r["assignee"]}
        db_total = sum(db_counts.values())

        with self._lock:
            # Update per-profile counts to match DB reality.
            all_profiles = set(self._profile_counts) | set(db_counts)
            for prof in all_profiles:
                db_c = db_counts.get(prof, 0)
                mem_c = self._profile_counts.get(prof, 0)
                if db_c != mem_c:
                    log.debug(
                        "thread_pool sync profile=%s mem=%d db=%d -> using db",
                        prof, mem_c, db_c,
                    )
                    if db_c == 0:
                        self._profile_counts.pop(prof, None)
                    else:
                        self._profile_counts[prof] = db_c

            # Adjust the active-slot registry size to match DB total.
            mem_total = len(self._active)
            if db_total < mem_total:
                # More in-memory slots than DB running tasks -> remove surplus
                # (ordered by priority: release LOW first).
                surplus = mem_total - db_total
                to_remove = sorted(
                    self._active.values(),
                    key=lambda s: s.priority,
                )[:surplus]
                for s in to_remove:
                    s._released = True  # prevent __del__ re-entry
                    self._active.pop(id(s), None)
                    log.debug(
                        "thread_pool sync removed orphan slot assignee=%s task=%s",
                        s.assignee, s.task_id,
                    )
            elif db_total > mem_total:
                # Fewer in-memory slots than DB running tasks -> add synthetic
                # slots so the cap is respected.
                deficit = db_total - mem_total
                for prof, _db_c in db_counts.items():
                    while deficit > 0 and len(self._active) < self._board_cap:
                        slot = WorkerSlot(self, prof, Priority.NORMAL)
                        slot.task_id = "<orphan>"
                        self._active[id(slot)] = slot
                        deficit -= 1
                    if deficit == 0:
                        break

    # -- Stats -----------------------------------------------------------------

    def stats(self) -> PoolStats:
        """Return a snapshot of current pool state for telemetry."""
        with self._lock:
            total = len(self._active)
            hi = sum(1 for s in self._active.values() if s.priority == Priority.HIGH)
            nm = sum(1 for s in self._active.values() if s.priority == Priority.NORMAL)
            lo = sum(1 for s in self._active.values() if s.priority == Priority.LOW)
            ages = [time.monotonic() - s.acquired_at for s in self._active.values()]
            return PoolStats(
                board=self._board,
                board_cap=self._board_cap,
                per_profile_cap=self._per_profile_cap,
                total_active=total,
                total_available=max(0, self._board_cap - total),
                high_active=hi,
                normal_active=nm,
                low_active=lo,
                per_profile=dict(self._profile_counts),
                oldest_slot_age_s=max(ages) if ages else None,
            )

    def drain(self, timeout: float = 30.0) -> bool:
        """Block until all slots are released or ``timeout`` expires.

        Useful for graceful shutdown.  Returns True if drained cleanly.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active:
                    return True
            time.sleep(0.1)
        log.warning(
            "thread_pool drain timed out after %.0fs; %d slots still held",
            timeout, len(self._active),
        )
        return False

    def __repr__(self) -> str:
        st = self.stats()
        return (
            f"<ThreadPool board={self._board!r} "
            f"active={st.total_active}/{st.board_cap} "
            f"profiles={st.per_profile}>"
        )


# -- Priority helper -----------------------------------------------------------

def slot_priority_for_task(
    consecutive_failures: int,
    created_at: Optional[float],
) -> Priority:
    """Derive the Priority tier for a ready task row.

    - HIGH  : has prior failures (retry).
    - NORMAL: freshly created (within last 5 minutes).
    - LOW   : has been waiting > 5 minutes with no failures.
    """
    if consecutive_failures and consecutive_failures > 0:
        return Priority.HIGH
    if created_at is not None:
        age = time.time() - created_at
        if age > 300:
            return Priority.LOW
    return Priority.NORMAL
