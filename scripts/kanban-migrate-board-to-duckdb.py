#!/usr/bin/env python3
"""One-shot migrator: copy a SQLite kanban board to DuckDB (ADR-012 P1 / G1a-FIX).

Copies rows verbatim from every ADR-012 §Appendix A table on the source
SQLite database to a sibling DuckDB file. This is the "seed the mirror"
step run once per board BEFORE turning on ``HERMES_KANBAN_WRITE_BACKEND=dual``.

Usage:
    kanban-migrate-board-to-duckdb.py --board <slug>                    (single board)
    kanban-migrate-board-to-duckdb.py --sqlite <path> [--duckdb <path>] (explicit)
    kanban-migrate-board-to-duckdb.py --all-boards                      (every board)
    add --dry-run for a read-only rehearsal (no DuckDB writes)

The DuckDB output file is ``kanban.duckdb`` next to the source SQLite
file unless ``--duckdb`` is explicit. Existing DuckDB files are refused
unless ``--overwrite`` is passed (a paranoia guard: we do not want a
partial migration silently clobbering a live shadow-window store).

Idempotence: pass ``--overwrite`` to replay a migration. A fresh migrate
is byte-deterministic per board given a quiescent source (i.e., the
dispatcher is paused).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

_TABLES = (
    "tasks",
    "task_links",
    "task_comments",
    "task_events",
    "task_runs",
    "kanban_notify_subs",
)


def _hermes_home() -> Path:
    v = os.environ.get("HERMES_HOME")
    if v:
        return Path(v).expanduser()
    return Path.home() / ".hermes"


def discover_boards() -> list[tuple[str, Path]]:
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


def _resolve_sqlite(args: argparse.Namespace) -> list[tuple[str, Path]]:
    if args.all_boards:
        return discover_boards()
    if args.board:
        for slug, path in discover_boards():
            if slug == args.board:
                return [(slug, path)]
        raise SystemExit(f"board '{args.board}' not found under {_hermes_home()}")
    if args.sqlite:
        p = Path(args.sqlite).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"sqlite path does not exist: {p}")
        return [(p.parent.name or "default", p)]
    raise SystemExit("supply --board, --sqlite, or --all-boards")


def _duckdb_path_for(sqlite_path: Path) -> Path:
    return sqlite_path.with_suffix(".duckdb")


def _sqlite_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _try_import_adapter():
    """Prefer the ADR-012 adapter for schema init; fall back gracefully."""
    try:
        from hermes_kanban import duckdb_kanban_adapter as dka  # type: ignore
        return dka
    except Exception:
        return None


def _init_duckdb(duck_path: Path, dka) -> "duckdb.DuckDBPyConnection":  # type: ignore[name-defined]
    import duckdb
    if dka is not None and hasattr(dka, "connect"):
        return dka.connect(duck_path)
    # Fallback: bare duckdb open. We do NOT re-implement the schema here;
    # if the adapter is not importable, the migrator refuses to invent
    # DDL that might drift from the authoritative adapter.
    raise RuntimeError(
        "hermes_kanban.duckdb_kanban_adapter is not importable in this "
        "environment; install the extracted hermes-kanban package before "
        "running the migrator (schema authority must come from the adapter)."
    )


def _duckdb_columns(duck, table: str) -> list[str] | None:
    """Return column names of a DuckDB table, or None if missing."""
    try:
        rows = duck.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    return [r[0] for r in rows]


def _copy_table(
    src: sqlite3.Connection,
    duck,
    table: str,
    dry_run: bool,
) -> dict:
    if not _sqlite_table_exists(src, table):
        return {"table": table, "skipped": "source table absent", "rows": 0}
    src_cols = _sqlite_columns(src, table)
    n_rows = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if dry_run or n_rows == 0:
        return {"table": table, "rows": int(n_rows), "dry_run": dry_run}
    # Filter to the intersection of SQLite and DuckDB column sets so a
    # schema drift (SQLite has a newer column the DuckDB adapter hasn't
    # picked up yet) does NOT crash the migration. Dropped columns are
    # reported so the operator can audit.
    duck_cols = _duckdb_columns(duck, table)
    if duck_cols is None:
        return {"table": table, "skipped": "duckdb table absent", "rows": 0}
    cols = [c for c in src_cols if c in duck_cols]
    dropped = [c for c in src_cols if c not in duck_cols]
    if not cols:
        return {"table": table, "skipped": "no overlapping columns", "rows": 0}
    col_list = ",".join(cols)
    placeholders = ",".join("?" for _ in cols)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    written = 0
    cursor = src.execute(f"SELECT {col_list} FROM {table} ORDER BY rowid ASC")
    BATCH = 500
    while True:
        rows = cursor.fetchmany(BATCH)
        if not rows:
            break
        try:
            duck.executemany(insert_sql, rows)
        except Exception as exc:
            raise RuntimeError(
                f"insert into duckdb {table} failed after {written} rows: {exc}"
            ) from exc
        written += len(rows)
    result: dict = {"table": table, "rows": written}
    if dropped:
        result["dropped_columns"] = dropped
    return result


def migrate_board(
    board: str,
    sqlite_path: Path,
    *,
    duck_path: Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    duck_path = duck_path or _duckdb_path_for(sqlite_path)
    started = time.time()
    result: dict = {
        "board": board,
        "sqlite_path": str(sqlite_path),
        "duckdb_path": str(duck_path),
        "dry_run": dry_run,
        "started_at": started,
        "tables": [],
    }
    if duck_path.exists() and not overwrite and not dry_run:
        result["skipped"] = f"duckdb target exists (pass --overwrite): {duck_path}"
        return result

    dka = _try_import_adapter()
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    duck = None
    try:
        if not dry_run:
            if duck_path.exists() and overwrite:
                duck_path.unlink()
            duck = _init_duckdb(duck_path, dka)
            duck.execute("BEGIN")
        for t in _TABLES:
            result["tables"].append(_copy_table(src, duck, t, dry_run))
        if not dry_run and duck is not None:
            duck.execute("COMMIT")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if duck is not None and not dry_run:
            try:
                duck.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        src.close()
        if duck is not None:
            try:
                duck.close()
            except Exception:
                pass
    result["duration_seconds"] = round(time.time() - started, 3)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Copy a SQLite kanban board to a sibling DuckDB store"
    )
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--board", help="board slug under $HERMES_HOME/kanban/boards")
    src.add_argument("--sqlite", help="explicit SQLite path")
    src.add_argument("--all-boards", action="store_true",
                     help="migrate every discovered board")
    ap.add_argument("--duckdb", help="explicit DuckDB target (default: sibling .duckdb)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing DuckDB target")
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only rehearsal; count rows without writing")
    ap.add_argument("--json", action="store_true", help="emit JSON, no prose")
    args = ap.parse_args(argv)

    boards = _resolve_sqlite(args)
    if not boards:
        print("no boards to migrate", file=sys.stderr)
        return 1

    if args.duckdb and len(boards) > 1:
        raise SystemExit("--duckdb is only valid for a single-board migration")

    reports = []
    exit_code = 0
    for slug, path in boards:
        try:
            duck_path = Path(args.duckdb).expanduser().resolve() if args.duckdb else None
            rep = migrate_board(
                slug, path,
                duck_path=duck_path,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            rep = {
                "board": slug, "sqlite_path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            }
            exit_code = 1
        reports.append(rep)
        if args.json:
            print(json.dumps(rep, sort_keys=True))
        else:
            if rep.get("skipped"):
                print(f"[SKIP]  {slug}: {rep['skipped']}")
            elif rep.get("error"):
                print(f"[ERROR] {slug}: {rep['error']}")
            else:
                total = sum(t.get("rows", 0) for t in rep.get("tables", []))
                dry = " (dry-run)" if rep["dry_run"] else ""
                print(f"[OK]    {slug}: {total} rows in "
                      f"{rep.get('duration_seconds', 0)}s{dry}"
                      f" → {rep['duckdb_path']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
