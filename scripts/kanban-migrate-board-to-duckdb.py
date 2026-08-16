#!/usr/bin/env python3
"""kanban-migrate-board-to-duckdb.py — per-board SQLite→DuckDB one-shot migrator.

ADR-012 §2.1 / Appendix A. Migrates one board's SQLite kanban.db to a
parallel DuckDB file (kanban.duckdb) in row-for-row parity. Runs entirely
inside a single DuckDB transaction so a mid-migration crash leaves NO
partial state. Emits a per-board parity log to
~/.hermes/kanban/migration-logs/<board>-<ts>.json.

Usage:
  scripts/kanban-migrate-board-to-duckdb.py --board <name> [--dry-run]
  scripts/kanban-migrate-board-to-duckdb.py --path /abs/path/to/kanban.db [--dry-run]

Safety:
  * Read-only against the SQLite source.
  * Refuses to overwrite an existing kanban.duckdb unless --force.
  * --dry-run reads source + emits parity log but writes no DuckDB.
  * The rollback path (scripts/kanban-rollback-to-sqlite.sh) simply removes
    the DuckDB — SQLite remains untouched.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# Ensure the hermes_kanban package is importable when run from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "projects" / "hermes-kanban" / "src"))

from hermes_kanban import duckdb_kanban_adapter as dka  # noqa: E402


KANBAN_HOME = Path.home() / ".hermes" / "kanban"
LOG_DIR = KANBAN_HOME / "migration-logs"

# Tables to migrate (order matters — tasks first for FK-like references)
TABLES = [
    "tasks",
    "task_links",
    "task_comments",
    "task_events",
    "task_runs",
    "kanban_notify_subs",
]


def _resolve_board(name: str | None, path: str | None) -> Path:
    if path:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"error: db path does not exist: {p}")
        return p
    if not name:
        raise SystemExit("error: --board or --path required")
    p = KANBAN_HOME / "boards" / name / "kanban.db"
    if not p.exists():
        raise SystemExit(f"error: board {name!r} has no kanban.db at {p}")
    return p


def _read_all_rows(sq_conn: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple]]:
    cur = sq_conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return cols, rows


def _duck_columns(dk_conn, table: str) -> list[str]:
    rows = dk_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _coerce(value: Any) -> Any:
    """SQLite stores JSON as TEXT; DuckDB may want JSON. Keep as-is —
    the adapter columns are all VARCHAR/BIGINT/BLOB compatible."""
    return value


def _migrate_table(
    sq_conn: sqlite3.Connection,
    dk_conn,
    table: str,
    *,
    dry_run: bool,
) -> dict:
    src_cols, src_rows = _read_all_rows(sq_conn, table)
    dst_cols = _duck_columns(dk_conn, table)
    if not dst_cols:
        return {
            "table": table,
            "status": "skipped_missing_dest",
            "src_rows": len(src_rows),
        }
    # Only migrate the intersection of columns (schema may drift on non-
    # essential fields; primary keys + timestamps + payload must align).
    common = [c for c in src_cols if c in dst_cols]
    if not common:
        return {
            "table": table,
            "status": "skipped_no_common_cols",
            "src_rows": len(src_rows),
            "src_cols": src_cols,
            "dst_cols": dst_cols,
        }
    src_index = {c: i for i, c in enumerate(src_cols)}
    placeholders = ",".join("?" * len(common))
    quoted = ",".join(f'"{c}"' for c in common)
    sql = f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})"
    inserted = 0
    if not dry_run:
        for row in src_rows:
            values = [_coerce(row[src_index[c]]) for c in common]
            dk_conn.execute(sql, values)
            inserted += 1
    return {
        "table": table,
        "status": "ok" if not dry_run else "dry_run",
        "src_rows": len(src_rows),
        "inserted": inserted,
        "columns": common,
        "dropped_columns": [c for c in src_cols if c not in dst_cols],
    }


def _emit_log(entry: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    board = entry.get("board", "unknown")
    ts = entry.get("ts", int(time.time()))
    path = LOG_DIR / f"{board}-{ts}.json"
    path.write_text(json.dumps(entry, indent=2, default=str))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", help="board name under ~/.hermes/kanban/boards/")
    ap.add_argument("--path", help="absolute path to a kanban.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="read + log parity plan without writing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing kanban.duckdb")
    args = ap.parse_args()

    src = _resolve_board(args.board, args.path)
    board = args.board or src.parent.name
    dst = src.with_suffix(".duckdb")

    if dst.exists() and not args.force and not args.dry_run:
        raise SystemExit(
            f"error: {dst} already exists; use --force to overwrite"
        )

    ts = int(time.time())
    print(f"[migrator] source: {src}")
    print(f"[migrator] dest:   {dst}")
    print(f"[migrator] dry-run: {args.dry_run}")

    sq_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    sq_conn.row_factory = None

    if not args.dry_run:
        if dst.exists() and args.force:
            dst.unlink()
        dka.init_db(dst)

    table_reports: list[dict] = []
    started = time.time()

    try:
        if args.dry_run:
            # No DuckDB writes; still need a conn to inspect schema.
            dka.init_db(dst.parent / f".dryrun-{ts}.duckdb")
            with dka.connect(dst.parent / f".dryrun-{ts}.duckdb") as dk:
                for tab in TABLES:
                    table_reports.append(
                        _migrate_table(sq_conn, dk, tab, dry_run=True)
                    )
            (dst.parent / f".dryrun-{ts}.duckdb").unlink(missing_ok=True)
        else:
            with dka.connect(dst) as dk:
                dk.execute("BEGIN TRANSACTION")
                try:
                    for tab in TABLES:
                        table_reports.append(
                            _migrate_table(sq_conn, dk, tab, dry_run=False)
                        )
                    dk.execute("COMMIT")
                except Exception:
                    dk.execute("ROLLBACK")
                    raise
    finally:
        sq_conn.close()

    elapsed = time.time() - started
    total_rows = sum(r.get("src_rows", 0) for r in table_reports)
    total_inserted = sum(r.get("inserted", 0) for r in table_reports)

    entry = {
        "board": board,
        "ts": ts,
        "src": str(src),
        "dst": str(dst),
        "dry_run": args.dry_run,
        "elapsed_seconds": round(elapsed, 3),
        "total_rows": total_rows,
        "total_inserted": total_inserted,
        "tables": table_reports,
    }
    log_path = _emit_log(entry)
    print(f"[migrator] wrote log: {log_path}")
    print(f"[migrator] rows: {total_rows} src / {total_inserted} inserted")
    print(f"[migrator] elapsed: {elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
