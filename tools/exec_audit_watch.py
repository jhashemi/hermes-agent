#!/usr/bin/env python3
"""Executive accountability audit watchdog.

Sweeps local kanban boards for executive-persona accountability violations:
  - cards assigned to an exec persona that are stalled (blocked/todo) past
    STALE_DAYS without a completed run
  - cards whose assignee missed an explicit deadline parsed from the title

Silent (exit 0, no output) when clean — designed for a 15-min cron whose
stdout is delivered to the operator's Telegram. Any output = alert.

Stdlib only (sqlite3); no side effects. h2 board sweep is a follow-up.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

BOARDS_ROOT = Path(os.environ.get("KANBAN_BOARDS_ROOT", "/home/ubuntu/.hermes/kanban/boards"))
STALE_DAYS = 5
EXEC_HINTS = (
    "elon", "musk", "demis", "hassabis", "dean", "jeff", "steve", "jobs",
    "fei_fei", "hamilton", "knuth", "ive", "friston", "trigani", "byers",
    "zeus", "atlas", "helios", "orion", "bahram",
)
DEADLINE_RE = re.compile(r"deadline[^0-9]{0,40}(\d{4}-\d{2}-\d{2})", re.I)


def audit_board(db: Path, alerts: list[str]) -> None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        if "status" not in cols:
            return
        now = time.time()
        q = (
            "SELECT id, title, assignee, status, created_at FROM tasks "
            "WHERE status IN ('blocked','todo','triage')"
        )
        for tid, title, assignee, status, created in con.execute(q):
            age_days = (now - (created or now)) / 86400 if isinstance(created, (int, float)) else 0
            who = (assignee or "").lower()
            is_exec = any(h in who for h in EXEC_HINTS)
            if not is_exec:
                continue
            if status == "blocked" and age_days >= STALE_DAYS:
                alerts.append(
                    f"[exec-audit] {db.parent.name}/{tid} BLOCKED {age_days:.0f}d "
                    f"assignee={assignee}: {title[:80]}"
                )
            m = DEADLINE_RE.search(title or "")
            if m:
                try:
                    dl = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
                    if now > dl + 86400:
                        alerts.append(
                            f"[exec-audit] {db.parent.name}/{tid} DEADLINE MISSED "
                            f"{m.group(1)} assignee={assignee}: {title[:80]}"
                        )
                except ValueError:
                    pass
        con.close()
    except sqlite3.Error:
        pass


def main() -> int:
    alerts: list[str] = []
    if BOARDS_ROOT.is_dir():
        for db in sorted(BOARDS_ROOT.rglob("kanban.db")):
            audit_board(db, alerts)
    for line in alerts:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
