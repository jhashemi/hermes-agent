"""Validate kanban DB schema consistency across migrations.

Tests that freshly-created DBs have all expected columns, that legacy
minimal schemas are correctly migrated, that SCHEMA_SQL indexes only
reference columns that exist in their CREATE TABLE definitions, and
that the OKR-2026-Q2 subset schema migrates cleanly.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.kanban_db import (
    SCHEMA_SQL,
    _INITIALIZED_PATHS,
    connect,
    init_db,
    kanban_db_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names for *table* via PRAGMA table_info."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _fresh_db(tmp_path: Path) -> Path:
    """Create a brand-new empty DB file and return its path.

    Clears the module-level _INITIALIZED_PATHS cache so ``connect`` will
    treat it as unseen.
    """
    db_path = tmp_path / "kanban.db"
    _INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return db_path


def _create_all_tables_except_tasks(raw: sqlite3.Connection) -> None:
    """Create the auxiliary tables (task_links, task_comments,
    task_events, task_runs, kanban_notify_subs) so that connect()'s
    SCHEMA_SQL CREATE TABLE IF NOT EXISTS passes without error and
    the migration pass can inspect task_runs.
    """
    raw.execute("""
        CREATE TABLE IF NOT EXISTS task_links (
            parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        )
    """)
    raw.execute("""
        CREATE TABLE IF NOT EXISTS task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    raw.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    raw.execute("""
        CREATE TABLE IF NOT EXISTS kanban_notify_subs (
            task_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            user_id TEXT,
            created_at INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, platform, chat_id, thread_id)
        )
    """)


# ---------------------------------------------------------------------------
# 1. Fresh DB has all expected columns
# ---------------------------------------------------------------------------

def test_fresh_db_has_all_columns(tmp_path):
    """A fresh DB created by connect() must contain every column from
    the SCHEMA_SQL CREATE TABLE statement for tasks."""
    db_path = _fresh_db(tmp_path)
    conn = connect(db_path)
    try:
        tasks_cols = _get_columns(conn, "tasks")
    finally:
        conn.close()

    # Extract the columns defined in SCHEMA_SQL for the tasks table.
    expected = _extract_create_table_columns(SCHEMA_SQL, "tasks")
    # The CREATE TABLE definition should be a subset of what we find
    # (migration may add more that aren't in the CREATE TABLE statement).
    assert expected <= tasks_cols, (
        f"Missing columns in fresh tasks table: {expected - tasks_cols}"
    )


# ---------------------------------------------------------------------------
# 2. Minimal schema migration (id, title, status + index-required columns)
# ---------------------------------------------------------------------------

def test_minimal_schema_migration(tmp_path):
    """A DB with a bare-minimum tasks table must gain all missing columns
    after connect().

    We start with only the columns that SCHEMA_SQL's CREATE INDEX
    statements and the migration backfill pass require — everything else
    is expected to be added by _migrate_add_optional_columns().
    """
    db_path = _fresh_db(tmp_path)
    raw = sqlite3.connect(str(db_path))
    # Minimum viable v0.1 schema: columns needed by SCHEMA_SQL indexes
    # (assignee, status) plus columns read by the migration's backfill
    # SELECT (claim_lock, claim_expires, started_at).
    raw.execute(
        "CREATE TABLE tasks ("
        "  id TEXT PRIMARY KEY, "
        "  title TEXT NOT NULL, "
        "  status TEXT NOT NULL, "
        "  assignee TEXT, "
        "  priority INTEGER DEFAULT 0, "
        "  created_at INTEGER NOT NULL, "
        "  claim_lock TEXT, "
        "  claim_expires INTEGER, "
        "  started_at INTEGER"
        ")"
    )
    _create_all_tables_except_tasks(raw)
    raw.commit()
    raw.close()

    conn = connect(db_path)
    try:
        tasks_cols = _get_columns(conn, "tasks")
    finally:
        conn.close()

    # All columns from both the CREATE TABLE statement and the migration
    # should now be present.
    expected = _extract_create_table_columns(SCHEMA_SQL, "tasks")
    assert expected <= tasks_cols, (
        f"Missing columns after minimal migration: {expected - tasks_cols}"
    )


# ---------------------------------------------------------------------------
# 3. Old task_runs schema migration
# ---------------------------------------------------------------------------

def test_old_task_runs_schema_migration(tmp_path):
    """A DB whose task_runs table only has the original v1 columns must
    gain all new columns after connect()."""
    db_path = _fresh_db(tmp_path)
    raw = sqlite3.connect(str(db_path))
    raw.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            assignee TEXT, priority INTEGER DEFAULT 0, created_at INTEGER NOT NULL,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            tenant TEXT, result TEXT, idempotency_key TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_failure_error TEXT, max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER, current_run_id INTEGER,
            workflow_template_id TEXT, current_step_key TEXT,
            skills TEXT, max_retries INTEGER,
            body TEXT, created_by TEXT, started_at INTEGER, completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT
        )
    """)
    raw.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            worker_pid  INTEGER,
            started_at  INTEGER NOT NULL,
            ended_at    INTEGER,
            exit_code   INTEGER,
            log_path    TEXT,
            result_path TEXT
        )
    """)
    _create_all_tables_except_tasks(raw)
    raw.commit()
    raw.close()

    conn = connect(db_path)
    try:
        run_cols = _get_columns(conn, "task_runs")
    finally:
        conn.close()

    # Columns that the migration is expected to add to task_runs.
    expected_new_cols = {
        "status", "profile", "step_key", "claim_lock", "claim_expires",
        "max_runtime_seconds", "last_heartbeat_at", "outcome", "summary",
        "metadata", "error",
    }
    assert expected_new_cols <= run_cols, (
        f"Missing columns in task_runs after migration: {expected_new_cols - run_cols}"
    )


# ---------------------------------------------------------------------------
# 4. SCHEMA_SQL has no orphan indexes
# ---------------------------------------------------------------------------

def test_schema_sql_no_orphan_indexes():
    """Every index defined in SCHEMA_SQL that references tasks or
    task_runs must only reference columns that exist in the
    corresponding CREATE TABLE definition."""
    tasks_columns = _extract_create_table_columns(SCHEMA_SQL, "tasks")
    task_runs_columns = _extract_create_table_columns(SCHEMA_SQL, "task_runs")

    # Find all CREATE INDEX statements in SCHEMA_SQL.
    index_pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\S+)\s+ON\s+(\w+)\s*\(([^)]+)\)",
        re.IGNORECASE,
    )
    orphan = []
    for match in index_pattern.finditer(SCHEMA_SQL):
        idx_name = match.group(1)
        table = match.group(2)
        col_expr = match.group(3)
        # Pick the right column set based on the table.
        if table == "tasks":
            col_set = tasks_columns
        elif table == "task_runs":
            col_set = task_runs_columns
        else:
            # Index on a table we don't check (task_links, etc.) — skip
            continue
        # Extract individual column references from the index expression.
        # Handles composite indexes like "(assignee, status)".
        idx_cols = [c.strip() for c in col_expr.split(",")]
        for ic in idx_cols:
            # Strip ASC/DESC / COLLATE qualifiers if present.
            col_name = ic.split()[0]
            if col_name not in col_set:
                orphan.append((idx_name, table, col_name))

    assert not orphan, (
        "SCHEMA_SQL contains indexes referencing non-existent columns:\n"
        + "\n".join(
            f"  index {name!r} on {table!r} references column {col!r}"
            for name, table, col in orphan
        )
    )


# ---------------------------------------------------------------------------
# 5. OKR-compatible schema migration
# ---------------------------------------------------------------------------

def test_okr_compatible_schema(tmp_path):
    """A DB created with the OKR-2026-Q2 subset of task columns must gain
    all missing columns after connect()."""
    db_path = _fresh_db(tmp_path)
    okr_columns_sql = """
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        assignee TEXT,
        priority INTEGER DEFAULT 0,
        claim_lock TEXT,
        claim_expires INTEGER,
        worker_pid INTEGER,
        evidence TEXT,
        skills TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_failure_error TEXT,
        created_at INTEGER NOT NULL,
        run_id INTEGER,
        current_run_id INTEGER,
        tenant TEXT,
        result TEXT,
        idempotency_key TEXT,
        max_runtime_seconds INTEGER,
        last_heartbeat_at INTEGER,
        workflow_template_id TEXT,
        current_step_key TEXT,
        max_retries INTEGER
    """
    raw = sqlite3.connect(str(db_path))
    raw.execute(f"CREATE TABLE tasks ({okr_columns_sql})")

    # Also create the other tables connect() expects to avoid breaking.
    _create_all_tables_except_tasks(raw)
    raw.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            exit_code INTEGER,
            log_path TEXT,
            result_path TEXT
        )
    """)
    raw.commit()
    raw.close()

    conn = connect(db_path)
    try:
        tasks_cols = _get_columns(conn, "tasks")
    finally:
        conn.close()

    expected = _extract_create_table_columns(SCHEMA_SQL, "tasks")
    assert expected <= tasks_cols, (
        f"Missing columns after OKR schema migration: {expected - tasks_cols}"
    )

    # Also verify the columns that were NOT in the OKR subset got added.
    okr_col_names = {c.strip().split()[0] for c in okr_columns_sql.split(",") if c.strip()}
    # "evidence" and "run_id" are OKR columns not in SCHEMA_SQL — they
    # persist but are harmless.  Check that the real SCHEMA_SQL columns
    # the OKR set lacked are now present.
    missing_from_okr = expected - okr_col_names
    assert missing_from_okr <= tasks_cols, (
        f"Columns missing from OKR set that should have been migrated: "
        f"{missing_from_okr - tasks_cols}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_create_table_columns(schema_sql: str, table: str) -> set[str]:
    """Extract column names from the ``CREATE TABLE IF NOT EXISTS <table>``
    statement inside *schema_sql*.

    Handles SQL ``--`` comments embedded in the column definitions.
    """
    # Find the CREATE TABLE block for the target table.
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\((.+?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(schema_sql)
    if m is None:
        raise ValueError(f"No CREATE TABLE IF NOT EXISTS {table} found in SCHEMA_SQL")
    body = m.group(1)

    # Remove SQL line comments (-- ...) so they don't inject spurious
    # tokens when we split on commas.
    body = re.sub(r"--[^\n]*", "", body)

    columns: set[str] = set()
    # Split on commas that sit at the top nesting level (ignore commas
    # inside DEFAULT expressions etc.).
    depth = 0
    start = 0
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            col_def = body[start:i].strip()
            start = i + 1
            _add_col_def(columns, col_def)
    # Last segment
    col_def = body[start:].strip()
    _add_col_def(columns, col_def)
    return columns


def _add_col_def(columns: set[str], col_def: str) -> None:
    """Parse a single column definition line and add its name to *columns*.

    Skips table-level constraints (PRIMARY KEY, UNIQUE, CHECK, FOREIGN KEY,
    CONSTRAINT) that are not column definitions.  Also skips blank/comment-
    only fragments leftover after ``--`` stripping.
    """
    if not col_def:
        return
    first_word = col_def.split()[0].upper()
    # Table-level constraints are not column definitions.
    if first_word in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
        return
    # The column name is the first token.
    col_name = col_def.split()[0]
    columns.add(col_name)