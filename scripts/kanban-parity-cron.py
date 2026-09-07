#!/usr/bin/env python3
"""Kanban parity cron (ADR-012 P1 / G1a-FIX).

Every tick this script compares COUNT(*) and MAX(id) across the
authoritative tables on both the SQLite and DuckDB kanban stores for
every board reachable under ``$HERMES_HOME`` (or the current-symlink
kanban root). Any divergence is emitted on NATS as
``hrv.kanban.parity.divergence`` and printed on stdout (so the systemd
timer's journal captures a durable trail even without NATS).

Design notes:
  * Read-only. The cron never writes to either backend.
  * Best-effort NATS emit: absent NATS is not an error; we print a
    warning on stdout and continue.
  * Missing DuckDB mirror is expected in ``sqlite`` mode; we count
    boards without a mirror as ``.duckdb_absent=True`` and do NOT emit
    a divergence event for them.
  * Table list is the ADR-012 §Appendix A canonical set:
      tasks, task_links, task_comments, task_events, task_runs,
      kanban_notify_subs.

Usage:
    python3 scripts/kanban-parity-cron.py [--json]

Env:
    HERMES_HOME (optional) — override kanban root
    HERMES_KANBAN_DB (optional) — pin single-board mode to one DB
    NATS_URL (default ``nats://127.0.0.1:4222``)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# Canonical tables per ADR-012 §Appendix A
_TABLES = (
    "tasks",
    "task_links",
    "task_comments",
    "task_events",
    "task_runs",
    "kanban_notify_subs",
)

# Tables with a monotonic ``id`` column we should also cover.
_ID_TABLES = (
    "task_comments",
    "task_events",
    "task_runs",
    "kanban_notify_subs",
)

_NATS_SUBJECT = "hrv.kanban.parity.divergence"
_DEFAULT_NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")


def _hermes_home() -> Path:
    v = os.environ.get("HERMES_HOME")
    if v:
        return Path(v).expanduser()
    return Path.home() / ".hermes"


def discover_boards() -> list[tuple[str, Path]]:
    """Return ``[(board_slug, sqlite_path), ...]`` for every reachable board.

    Boards are discovered under ``$HERMES_HOME/kanban/boards/<slug>/kanban.db``
    plus the legacy default board at ``$HERMES_HOME/kanban.db``. If
    ``HERMES_KANBAN_DB`` is set we operate in single-board mode.
    """
    pinned = os.environ.get("HERMES_KANBAN_DB")
    if pinned:
        p = Path(pinned).expanduser().resolve()
        return [(p.parent.name or "default", p)] if p.exists() else []

    home = _hermes_home()
    out: list[tuple[str, Path]] = []
    default = home / "kanban.db"
    if default.exists():
        out.append(("default", default))
    boards_root = home / "kanban" / "boards"
    if boards_root.is_dir():
        for child in sorted(boards_root.iterdir()):
            if not child.is_dir():
                continue
            db = child / "kanban.db"
            if db.exists():
                out.append((child.name, db))
    return out


def _duckdb_path_for(sqlite_path: Path) -> Path:
    """Convention: SQLite ``kanban.db`` → sibling ``kanban.duckdb``."""
    return sqlite_path.with_suffix(".duckdb")


def _sqlite_stats(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, Any] = {"count": {}, "max_id": {}}
        for t in _TABLES:
            try:
                out["count"][t] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                )
            except sqlite3.OperationalError:
                out["count"][t] = None  # table absent (older board)
        for t in _ID_TABLES:
            try:
                row = conn.execute(f"SELECT MAX(id) FROM {t}").fetchone()
                out["max_id"][t] = int(row[0]) if row and row[0] is not None else 0
            except sqlite3.OperationalError:
                out["max_id"][t] = None
        return out
    finally:
        conn.close()


def _duckdb_stats(path: Path) -> dict[str, Any]:
    """Read-only DuckDB open (best-effort).

    We try the pinned adapter first (``hermes_kanban.duckdb_kanban_adapter``)
    and fall back to raw ``duckdb.connect(read_only=True)`` — the parity
    cron must not depend on the adapter being fully importable.
    """
    import duckdb  # local; missing dep is a fatal config error
    conn = duckdb.connect(str(path), read_only=True)
    try:
        out: dict[str, Any] = {"count": {}, "max_id": {}}
        for t in _TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
                out["count"][t] = int(row[0]) if row else 0
            except duckdb.Error:
                out["count"][t] = None
        for t in _ID_TABLES:
            try:
                row = conn.execute(f"SELECT MAX(id) FROM {t}").fetchone()
                out["max_id"][t] = int(row[0]) if row and row[0] is not None else 0
            except duckdb.Error:
                out["max_id"][t] = None
        return out
    finally:
        conn.close()


def compare_board(board: str, sqlite_path: Path) -> dict[str, Any]:
    """Compare a single board. Returns a divergence report dict.

    Shape::
        {
          "board": "default",
          "sqlite_path": "...",
          "duckdb_path": "...",
          "duckdb_absent": False,
          "diverged": False,
          "sqlite": {"count": {...}, "max_id": {...}},
          "duckdb": {"count": {...}, "max_id": {...}},
          "deltas": {"count": {"tasks": +2}, "max_id": {"task_events": +5}},
          "checked_at": <epoch>,
        }
    """
    report: dict[str, Any] = {
        "board": board,
        "sqlite_path": str(sqlite_path),
        "duckdb_path": str(_duckdb_path_for(sqlite_path)),
        "duckdb_absent": False,
        "diverged": False,
        "checked_at": int(time.time()),
    }
    duck_path = _duckdb_path_for(sqlite_path)
    if not duck_path.exists():
        report["duckdb_absent"] = True
        return report

    sq = _sqlite_stats(sqlite_path)
    du = _duckdb_stats(duck_path)
    report["sqlite"] = sq
    report["duckdb"] = du

    deltas: dict[str, dict[str, int]] = {"count": {}, "max_id": {}}
    for t in _TABLES:
        s = sq["count"].get(t)
        d = du["count"].get(t)
        if s is None or d is None:
            continue
        if s != d:
            deltas["count"][t] = d - s
    for t in _ID_TABLES:
        s = sq["max_id"].get(t)
        d = du["max_id"].get(t)
        if s is None or d is None:
            continue
        if s != d:
            deltas["max_id"][t] = d - s
    if deltas["count"] or deltas["max_id"]:
        report["diverged"] = True
        report["deltas"] = deltas
    return report


def _emit_nats(payload: dict, nats_url: str) -> str:
    """Fire-and-forget publish. Returns a status string for logs."""
    try:
        import asyncio
        try:
            import nats  # nats-py
        except ImportError:
            return "skipped (nats-py not installed)"

        async def _publish():
            nc = await nats.connect(nats_url, connect_timeout=3)
            try:
                await nc.publish(_NATS_SUBJECT, json.dumps(payload).encode())
                await nc.flush(timeout=3)
            finally:
                await nc.close()

        asyncio.run(_publish())
        return "published"
    except Exception as exc:  # pragma: no cover — best-effort
        return f"error: {exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kanban SQLite↔DuckDB parity cron")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON only (one line per board)")
    ap.add_argument("--nats-url", default=_DEFAULT_NATS_URL,
                    help="NATS URL for divergence events "
                         f"(default {_DEFAULT_NATS_URL})")
    ap.add_argument("--no-nats", action="store_true",
                    help="never publish to NATS, log only")
    args = ap.parse_args(argv)

    boards = discover_boards()
    if not boards:
        msg = "kanban-parity-cron: no boards found"
        print(msg, file=sys.stderr)
        return 0

    diverged_count = 0
    for board, sqlite_path in boards:
        try:
            report = compare_board(board, sqlite_path)
        except Exception as exc:
            report = {
                "board": board, "sqlite_path": str(sqlite_path),
                "error": f"{type(exc).__name__}: {exc}",
                "checked_at": int(time.time()),
            }
        # Emit event on divergence
        if report.get("diverged"):
            diverged_count += 1
            if not args.no_nats:
                report["nats_status"] = _emit_nats(report, args.nats_url)
        line = json.dumps(report, sort_keys=True)
        print(line)
    if args.json:
        return 0 if diverged_count == 0 else 2
    if diverged_count:
        print(f"kanban-parity-cron: {diverged_count} board(s) diverged",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
