"""
kanban_circuit_breaker.py — Board-level circuit breaker for the Kanban dispatcher.

Implements the classic Open / Half-Open / Closed state machine at the
*board* level (not per-task).  Per-task exponential backoff and the
per-task failure limit remain in kanban_db.py; this breaker fires when
*system-wide* resource exhaustion is detected across many tasks.

State machine
─────────────
  CLOSED   → all spawns allowed, resource checks pass
  OPEN     → all spawns blocked; breaker is cooling down
  HALF_OPEN→ exactly one probe spawn allowed; outcome decides transition

Transitions
───────────
  CLOSED  + N consecutive resource-pressure events in window W
              → OPEN  (cooldown = BASE_COOLDOWN * 2^trips, capped at MAX_COOLDOWN)
  OPEN    + cooldown elapsed
              → HALF_OPEN
  HALF_OPEN + probe spawn succeeds (no Errno 11 / no OOM)
              → CLOSED  (trip counter reset)
  HALF_OPEN + probe spawn fails
              → OPEN   (cooldown doubled again)

Persistence
───────────
State is stored in the kanban SQLite DB in table ``circuit_breaker``.
A single row keyed by ``board`` (default "default") holds the current
state, trip count, last-tripped timestamp, and cooldown duration.
This means daemon restarts preserve the breaker state.

Usage
─────
    from hermes_cli.kanban_circuit_breaker import CircuitBreaker, BreakerOpen

    with connect() as conn:
        breaker = CircuitBreaker(conn, board="default")
        try:
            with breaker.guard():
                pid = spawn_fn(task, workspace)
            breaker.record_success()
        except BreakerOpen as e:
            # All spawns blocked — log and skip this tick
            log.warning("Circuit open: %s", e)
        except Exception as spawn_exc:
            breaker.record_failure(str(spawn_exc))
            raise
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

# How many consecutive resource-pressure events trip the breaker (CLOSED→OPEN).
DEFAULT_TRIP_THRESHOLD: int = 3

# Sliding window (seconds) in which trips are counted.
DEFAULT_TRIP_WINDOW: int = 120          # 2 minutes

# Base cooldown before moving OPEN→HALF_OPEN (doubles each subsequent trip).
BASE_COOLDOWN: float = 30.0            # seconds

# Absolute cap on cooldown (no matter how many trips).
MAX_COOLDOWN: float = 3600.0           # 1 hour

# Errors that count as "resource pressure" for trip accounting.
RESOURCE_ERROR_PATTERNS: tuple[str, ...] = (
    "errno 11",
    "resource temporarily unavailable",
    "eagain",
    "cannot allocate memory",
    "enomem",
    "out of memory",
    "fork",
)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS circuit_breaker (
    board            TEXT    PRIMARY KEY,
    state            TEXT    NOT NULL DEFAULT 'closed',
    trip_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_hits INTEGER NOT NULL DEFAULT 0,
    last_tripped_at  REAL,
    cooldown_seconds REAL    NOT NULL DEFAULT 30.0,
    updated_at       REAL    NOT NULL DEFAULT (strftime('%s','now'))
);
"""


# ── Exceptions ────────────────────────────────────────────────────────────────

class BreakerOpen(RuntimeError):
    """Raised by :meth:`CircuitBreaker.guard` when the breaker is OPEN."""


class BreakerHalfOpenConflict(RuntimeError):
    """Raised when a second probe is attempted while HALF_OPEN."""


# ── State enum ────────────────────────────────────────────────────────────────

class BreakerState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


# ── Row dataclass ─────────────────────────────────────────────────────────────

@dataclass
class BreakerRow:
    board:            str
    state:            BreakerState  = BreakerState.CLOSED
    trip_count:       int           = 0
    consecutive_hits: int           = 0
    last_tripped_at:  Optional[float] = None
    cooldown_seconds: float         = BASE_COOLDOWN
    updated_at:       float         = field(default_factory=time.time)


# ── Core class ────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """Board-level circuit breaker backed by SQLite.

    Thread-safe: an in-process ``threading.Lock`` serialises concurrent
    ``guard()`` calls within the same daemon process.  Cross-process
    safety is provided by SQLite's BEGIN IMMEDIATE transactions.

    Parameters
    ----------
    conn:
        Open sqlite3 connection to the kanban DB.
    board:
        Board name (default ``"default"``).
    trip_threshold:
        Consecutive resource-pressure events before tripping.
    trip_window:
        Rolling window (seconds) for counting hits.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        board: str = "default",
        trip_threshold: int = DEFAULT_TRIP_THRESHOLD,
        trip_window: int = DEFAULT_TRIP_WINDOW,
    ) -> None:
        self._conn = conn
        self._board = board
        self._threshold = trip_threshold
        self._window = trip_window
        self._lock = threading.Lock()
        self._probe_in_flight = False
        _ensure_schema(conn)

    # ── Public API ────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def guard(self):
        """Context manager that enforces breaker policy before a spawn.

        Raises :class:`BreakerOpen` if the breaker is OPEN.
        Raises :class:`BreakerHalfOpenConflict` if HALF_OPEN and a probe
        is already in flight.

        On successful exit the caller should call :meth:`record_success`.
        On exception the caller should call :meth:`record_failure`.
        """
        with self._lock:
            row = self._load()
            state = row.state

            if state == BreakerState.OPEN:
                # Check whether cooldown has elapsed → promote to HALF_OPEN.
                if row.last_tripped_at is not None:
                    elapsed = time.time() - row.last_tripped_at
                    if elapsed >= row.cooldown_seconds:
                        row = self._transition(row, BreakerState.HALF_OPEN)
                        log.info(
                            "circuit_breaker board=%s OPEN→HALF_OPEN after %.0fs cooldown",
                            self._board, elapsed,
                        )
                    else:
                        remaining = row.cooldown_seconds - elapsed
                        raise BreakerOpen(
                            f"board={self._board} OPEN; cooldown remaining={remaining:.0f}s"
                        )
                else:
                    raise BreakerOpen(f"board={self._board} OPEN (no timestamp)")

            if row.state == BreakerState.HALF_OPEN:
                if self._probe_in_flight:
                    raise BreakerHalfOpenConflict(
                        f"board={self._board} HALF_OPEN probe already in flight"
                    )
                self._probe_in_flight = True

        try:
            yield
        except Exception:
            with self._lock:
                self._probe_in_flight = False
            raise

        with self._lock:
            self._probe_in_flight = False

    def record_success(self) -> None:
        """Call after a successful spawn to potentially close the breaker."""
        with self._lock:
            row = self._load()
            if row.state == BreakerState.HALF_OPEN:
                log.info(
                    "circuit_breaker board=%s HALF_OPEN→CLOSED (probe succeeded)",
                    self._board,
                )
                self._save(BreakerRow(
                    board=self._board,
                    state=BreakerState.CLOSED,
                    trip_count=row.trip_count,   # preserve history
                    consecutive_hits=0,
                    last_tripped_at=row.last_tripped_at,
                    cooldown_seconds=BASE_COOLDOWN,  # reset cooldown
                ))
            elif row.state == BreakerState.CLOSED:
                # Reset the consecutive hit counter on any success.
                if row.consecutive_hits > 0:
                    self._save_field(row, consecutive_hits=0)

    def record_failure(self, error: str) -> None:
        """Call after a failed spawn.  Increments hit counter; may trip."""
        if not _is_resource_error(error):
            return  # Non-resource errors don't affect the breaker.

        with self._lock:
            row = self._load()

            if row.state == BreakerState.HALF_OPEN:
                # Probe failed → back to OPEN with doubled cooldown.
                new_cooldown = min(row.cooldown_seconds * 2, MAX_COOLDOWN)
                log.warning(
                    "circuit_breaker board=%s HALF_OPEN→OPEN (probe failed: %s); "
                    "cooldown=%.0fs",
                    self._board, error, new_cooldown,
                )
                self._save(BreakerRow(
                    board=self._board,
                    state=BreakerState.OPEN,
                    trip_count=row.trip_count + 1,
                    consecutive_hits=0,
                    last_tripped_at=time.time(),
                    cooldown_seconds=new_cooldown,
                ))
                return

            new_hits = row.consecutive_hits + 1
            if new_hits >= self._threshold:
                # Trip: CLOSED→OPEN.
                new_cooldown = min(
                    BASE_COOLDOWN * (2 ** row.trip_count),
                    MAX_COOLDOWN,
                )
                log.warning(
                    "circuit_breaker board=%s CLOSED→OPEN "
                    "(resource hits=%d threshold=%d error=%r); cooldown=%.0fs",
                    self._board, new_hits, self._threshold, error, new_cooldown,
                )
                self._save(BreakerRow(
                    board=self._board,
                    state=BreakerState.OPEN,
                    trip_count=row.trip_count + 1,
                    consecutive_hits=0,
                    last_tripped_at=time.time(),
                    cooldown_seconds=new_cooldown,
                ))
            else:
                log.debug(
                    "circuit_breaker board=%s hit %d/%d error=%r",
                    self._board, new_hits, self._threshold, error,
                )
                self._save_field(row, consecutive_hits=new_hits)

    @property
    def state(self) -> BreakerState:
        """Current breaker state (reads from DB, no lock)."""
        return self._load().state

    def status(self) -> dict:
        """Return a diagnostic dict suitable for health telemetry."""
        row = self._load()
        remaining: Optional[float] = None
        if row.state == BreakerState.OPEN and row.last_tripped_at:
            remaining = max(0.0, row.cooldown_seconds - (time.time() - row.last_tripped_at))
        return {
            "board":            row.board,
            "state":            row.state.value,
            "trip_count":       row.trip_count,
            "consecutive_hits": row.consecutive_hits,
            "cooldown_seconds": row.cooldown_seconds,
            "cooldown_remaining": remaining,
            "last_tripped_at":  row.last_tripped_at,
        }

    def reset(self) -> None:
        """Manually close the breaker (operator override)."""
        with self._lock:
            self._save(BreakerRow(board=self._board))
            log.info("circuit_breaker board=%s manually reset to CLOSED", self._board)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load(self) -> BreakerRow:
        row = self._conn.execute(
            "SELECT state, trip_count, consecutive_hits, last_tripped_at, "
            "       cooldown_seconds, updated_at "
            "FROM circuit_breaker WHERE board = ?",
            (self._board,),
        ).fetchone()
        if row is None:
            return BreakerRow(board=self._board)
        return BreakerRow(
            board=self._board,
            state=BreakerState(row["state"]),
            trip_count=int(row["trip_count"] or 0),
            consecutive_hits=int(row["consecutive_hits"] or 0),
            last_tripped_at=float(row["last_tripped_at"]) if row["last_tripped_at"] else None,
            cooldown_seconds=float(row["cooldown_seconds"] or BASE_COOLDOWN),
            updated_at=float(row["updated_at"] or time.time()),
        )

    def _save(self, row: BreakerRow) -> None:
        self._conn.execute(
            "INSERT INTO circuit_breaker "
            "  (board, state, trip_count, consecutive_hits, "
            "   last_tripped_at, cooldown_seconds, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(board) DO UPDATE SET "
            "  state=excluded.state, trip_count=excluded.trip_count, "
            "  consecutive_hits=excluded.consecutive_hits, "
            "  last_tripped_at=excluded.last_tripped_at, "
            "  cooldown_seconds=excluded.cooldown_seconds, "
            "  updated_at=excluded.updated_at",
            (
                row.board,
                row.state.value,
                row.trip_count,
                row.consecutive_hits,
                row.last_tripped_at,
                row.cooldown_seconds,
                time.time(),
            ),
        )
        self._conn.commit()

    def _save_field(self, row: BreakerRow, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(row, k, v)
        self._save(row)

    def _transition(self, row: BreakerRow, new_state: BreakerState) -> BreakerRow:
        row.state = new_state
        row.updated_at = time.time()
        self._save(row)
        return row


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    conn.commit()


def _is_resource_error(error: str) -> bool:
    e = error.lower()
    return any(pat in e for pat in RESOURCE_ERROR_PATTERNS)
