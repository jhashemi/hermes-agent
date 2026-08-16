#!/usr/bin/env python3
"""kanban-parity-cron.py — 5-minute parity check between SQLite + DuckDB.

ADR-012 §4 (dual-write shadow observation). Runs once per invocation
(designed to be scheduled by cron every 5 minutes during the G1b window).
For each board that has BOTH a kanban.db and a kanban.duckdb, verifies
row counts + a sample-hash of task rows. On drift, emits a NATS event on
subject ``hrv.kanban.parity.divergence`` with the details.

Exit codes:
  0 = all boards in parity
  1 = one or more boards diverged (still emits NATS, still logs)
  2 = infra error (nats down, db unreadable, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "projects" / "hermes-kanban" / "src"))

from hermes_kanban import duckdb_kanban_adapter as dka  # noqa: E402


KANBAN_HOME = Path.home() / ".hermes" / "kanban"
NATS_SUBJECT = "hrv.kanban.parity.divergence"
DEFAULT_NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")


def _find_dual_boards() -> list[tuple[str, Path, Path]]:
    """Return (board_name, sqlite_path, duckdb_path) for every board with both."""
    out: list[tuple[str, Path, Path]] = []
    boards_dir = KANBAN_HOME / "boards"
    if not boards_dir.exists():
        return out
    for board_dir in sorted(boards_dir.iterdir()):
        if not board_dir.is_dir():
            continue
        sq = board_dir / "kanban.db"
        dk = board_dir / "kanban.duckdb"
        if sq.exists() and dk.exists():
            out.append((board_dir.name, sq, dk))
    return out


def _sqlite_stats(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        counts: dict[str, int] = {}
        for tab in ("tasks", "task_comments", "task_events", "task_runs",
                    "task_links", "kanban_notify_subs"):
            try:
                (n,) = conn.execute(f"SELECT COUNT(*) FROM {tab}").fetchone()
                counts[tab] = n
            except sqlite3.OperationalError:
                counts[tab] = -1
        # Deterministic sample hash: id + status + last_heartbeat_at + completed_at over tasks
        rows = conn.execute(
            "SELECT id, status, COALESCE(last_heartbeat_at, 0), "
            "COALESCE(completed_at, 0) FROM tasks ORDER BY id"
        ).fetchall()
        h = hashlib.sha256()
        for r in rows:
            h.update(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}\n".encode())
        return {"counts": counts, "task_hash": h.hexdigest()}
    finally:
        conn.close()


def _duckdb_stats(path: Path) -> dict:
    with dka.connect(path) as conn:
        counts: dict[str, int] = {}
        for tab in ("tasks", "task_comments", "task_events", "task_runs",
                    "task_links", "kanban_notify_subs"):
            try:
                (n,) = conn.execute(
                    f"SELECT COUNT(*) FROM {tab}"
                ).fetchone()
                counts[tab] = n
            except Exception:
                counts[tab] = -1
        try:
            rows = conn.execute(
                "SELECT id, status, COALESCE(last_heartbeat_at, 0), "
                "COALESCE(completed_at, 0) FROM tasks ORDER BY id"
            ).fetchall()
        except Exception:
            rows = []
        h = hashlib.sha256()
        for r in rows:
            h.update(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}\n".encode())
        return {"counts": counts, "task_hash": h.hexdigest()}


def _diff(board: str, sq_stats: dict, dk_stats: dict) -> dict:
    d: dict[str, Any] = {"board": board, "diverged": False, "reasons": []}
    for tab, n in sq_stats["counts"].items():
        m = dk_stats["counts"].get(tab, -1)
        if n != m:
            d["diverged"] = True
            d["reasons"].append(
                f"row_count[{tab}]: sqlite={n} duckdb={m}"
            )
    if sq_stats["task_hash"] != dk_stats["task_hash"]:
        d["diverged"] = True
        d["reasons"].append(
            f"task_hash: sqlite={sq_stats['task_hash'][:16]}… "
            f"duckdb={dk_stats['task_hash'][:16]}…"
        )
    d["sqlite"] = sq_stats
    d["duckdb"] = dk_stats
    return d


async def _emit_nats(url: str, payload: dict) -> bool:
    try:
        import nats  # type: ignore
    except ImportError:
        return False
    try:
        nc = await asyncio.wait_for(nats.connect(url), timeout=3.0)
        try:
            await nc.publish(NATS_SUBJECT, json.dumps(payload).encode())
            await nc.flush(timeout=2.0)
        finally:
            await nc.close()
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    ap.add_argument("--no-nats", action="store_true",
                    help="skip NATS emission, just print + exit code")
    ap.add_argument("--json", action="store_true",
                    help="emit full JSON report to stdout")
    args = ap.parse_args()

    boards = _find_dual_boards()
    if not boards:
        report = {
            "ts": int(time.time()),
            "boards_checked": 0,
            "note": "no dual (sqlite+duckdb) boards found",
        }
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("[parity-cron] no dual boards found")
        return 0

    per_board: list[dict] = []
    diverged: list[dict] = []
    for name, sq_path, dk_path in boards:
        try:
            sq = _sqlite_stats(sq_path)
            dk = _duckdb_stats(dk_path)
            d = _diff(name, sq, dk)
            per_board.append(d)
            if d["diverged"]:
                diverged.append(d)
        except Exception as exc:
            per_board.append({
                "board": name,
                "diverged": True,
                "reasons": [f"error: {type(exc).__name__}: {exc}"],
            })
            diverged.append(per_board[-1])

    report = {
        "ts": int(time.time()),
        "boards_checked": len(boards),
        "diverged_count": len(diverged),
        "boards": per_board,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"[parity-cron] checked={len(boards)} "
            f"diverged={len(diverged)}"
        )
        for d in diverged:
            print(f"  ! {d['board']}: {'; '.join(d['reasons'])}")

    if diverged and not args.no_nats:
        payload = {
            "ts": report["ts"],
            "diverged_boards": [
                {"board": d["board"], "reasons": d["reasons"]}
                for d in diverged
            ],
        }
        ok = asyncio.run(_emit_nats(args.nats_url, payload))
        if not ok:
            print(
                f"[parity-cron] WARNING: failed to emit NATS on "
                f"{NATS_SUBJECT}",
                file=sys.stderr,
            )

    return 1 if diverged else 0


if __name__ == "__main__":
    sys.exit(main())
