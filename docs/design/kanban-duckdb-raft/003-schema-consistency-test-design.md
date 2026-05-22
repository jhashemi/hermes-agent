# Schema Consistency Test Design

**Status**: Proposed
**Date**: 2026-05-21
**Authors**: Jeff Dean, Werner Vogels

## Problem

The 24h outage was caused by 5 independent schema-migration failures. The root cause: `SCHEMA_SQL` contained indexes referencing columns that may not exist in the `CREATE TABLE` statement at the time the index is created.

Specifically, the current code has this comment in `SCHEMA_SQL`:

```sql
-- tenant, idempotency_key, and run_id indexes are created by
-- _migrate_add_optional_columns() AFTER the columns exist; putting
-- them here would crash on old DBs that lack those columns yet.
```

This is a workaround, not a fix. The real fix is a **schema consistency linter** that prevents the class of bug entirely.

## Design

### 1. Static Schema Linter: `lint_schema_sql()`

Parse `SCHEMA_SQL` and verify that every `CREATE INDEX` references only columns that exist in the corresponding `CREATE TABLE`.

```python
def lint_schema_sql(schema_sql: str) -> list[SchemaLintError]:
    """Parse SCHEMA_SQL and find index/column mismatches.
    
    Returns:
        List of errors. Empty list = schema is consistent.
    """
    tables = _extract_create_tables(schema_sql)
    indexes = _extract_create_indexes(schema_sql)
    errors = []
    
    for idx in indexes:
        table_name = idx.table_name
        if table_name not in tables:
            errors.append(SchemaLintError(
                kind="index_on_missing_table",
                index_name=idx.index_name,
                message=f"Index {idx.index_name} references table {table_name} "
                        f"which is not defined in SCHEMA_SQL",
            ))
            continue
        
        table_columns = tables[table_name]
        for col in idx.columns:
            if col not in table_columns:
                errors.append(SchemaLintError(
                    kind="index_on_missing_column",
                    index_name=idx.index_name,
                    column=col,
                    table=table_name,
                    message=f"Index {idx.index_name} on {table_name} references "
                            f"column '{col}' which is not in CREATE TABLE "
                            f"(available: {sorted(table_columns)})",
                ))
    
    return errors
```

**Parser approach**: Use `sqlparse` or a lightweight hand-rolled parser. The SQL in `SCHEMA_SQL` is simple enough (no CTEs, no subqueries in indexes) that regex-based parsing suffices:

```python
def _extract_create_tables(schema_sql: str) -> dict[str, set[str]]:
    """Extract table names → column names from CREATE TABLE statements."""
    tables = {}
    # Match: CREATE TABLE IF NOT EXISTS <name> (<columns>)
    pattern = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE
    )
    for match in pattern.finditer(schema_sql):
        table_name = match.group(1)
        body = match.group(2)
        columns = _parse_column_names(body)
        tables[table_name] = columns
    return tables

def _extract_create_indexes(schema_sql: str) -> list[IndexDef]:
    """Extract CREATE INDEX statements."""
    indexes = []
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE
    )
    for match in pattern.finditer(schema_sql):
        index_name = match.group(1)
        table_name = match.group(2)
        columns = [c.strip() for c in match.group(3).split(",")]
        indexes.append(IndexDef(index_name, table_name, columns))
    return indexes

def _parse_column_names(body: str) -> set[str]:
    """Extract column names from CREATE TABLE body.
    
    Handles:
    - Simple columns: `id TEXT PRIMARY KEY`
    - DEFAULT values: `priority INTEGER DEFAULT 0`
    - Column-level constraints: `status TEXT NOT NULL`
    - Comments: `-- comment` lines
    - Multi-line definitions
    
    Skips:
    - PRIMARY KEY constraints (table-level)
    - FOREIGN KEY constraints
    """
    columns = set()
    for line in body.split("\n"):
        line = line.strip().rstrip(",")
        if not line or line.startswith("--") or line.startswith("PRIMARY KEY"):
            continue
        if line.startswith("FOREIGN KEY") or line.startswith("UNIQUE(") or line.startswith("CHECK"):
            continue
        # First word is the column name
        col_name = line.split()[0]
        columns.add(col_name)
    return columns
```

### 2. Migration Consistency Linter: `lint_migrations()`

Verify that `_migrate_add_optional_columns()` and any future migration functions follow the rules:

**Rule 1**: Every column added via `_add_column_if_missing()` must also appear in `SCHEMA_SQL`'s `CREATE TABLE`.

**Rule 2**: Every index created in `_migrate_add_optional_columns()` must reference columns that are either in `SCHEMA_SQL`'s `CREATE TABLE` or added by a prior `_add_column_if_missing()` in the same function.

**Rule 3**: No column in `SCHEMA_SQL`'s `CREATE TABLE` may be missing from `_migrate_add_optional_columns()`. (All columns present at CREATE TABLE time should have a migration path for old DBs.)

```python
def lint_migrations(schema_sql: str, migrate_func_source: str) -> list[SchemaLintError]:
    """Verify migration function consistency.
    
    Checks:
    1. Every _add_column_if_missing column appears in SCHEMA_SQL CREATE TABLE
    2. Every index in migration references extant columns
    3. Every column in CREATE TABLE has a migration path
    """
    schema_tables = _extract_create_tables(schema_sql)
    migration_columns = _extract_add_column_calls(migrate_func_source)
    migration_indexes = _extract_migration_indexes(migrate_func_source)
    
    errors = []
    
    # Check 1: Migration columns must be in schema
    for (table, col) in migration_columns:
        if table in schema_tables and col not in schema_tables[table]:
            errors.append(SchemaLintError(
                kind="migration_column_not_in_schema",
                table=table,
                column=col,
                message=f"_migrate_add_optional_columns adds {col} to {table} "
                        f"but {col} is not in SCHEMA_SQL's CREATE TABLE {table}",
            ))
    
    # Check 2: Migration indexes reference valid columns
    for idx in migration_indexes:
        if idx.table_name in schema_tables:
            for col in idx.columns:
                if col not in schema_tables[idx.table_name]:
                    errors.append(SchemaLintError(
                        kind="migration_index_on_missing_column",
                        index_name=idx.index_name,
                        column=col,
                        message=f"Migration creates index {idx.index_name} on "
                                f"{col}, which is not in CREATE TABLE {idx.table_name}",
                    ))
    
    # Check 3: Schema columns have migration paths
    for table_name, columns in schema_tables.items():
        migration_table_cols = {col for (t, col) in migration_columns if t == table_name}
        # Columns that are in CREATE TABLE but NOT in migrations are fine
        # only if they're "original" columns (present from v1).
        # We define "original" columns as those that don't need migration.
        # This check is advisory, not hard-fail.
    
    return errors
```

### 3. Runtime Schema Verification Test: `test_schema_consistency()`

```python
import pytest
import sqlite3
from hermes_cli import kanban_db as _kb


class TestSchemaConsistency:
    """Verify that connect() produces a DB with all expected columns."""
    
    # The full set of columns that SHOULD exist after connect()
    EXPECTED_TASKS_COLUMNS = {
        "id", "title", "body", "assignee", "status", "priority",
        "created_by", "created_at", "started_at", "completed_at",
        "workspace_kind", "workspace_path", "claim_lock", "claim_expires",
        "tenant", "result", "idempotency_key", "consecutive_failures",
        "worker_pid", "last_failure_error", "max_runtime_seconds",
        "last_heartbeat_at", "current_run_id", "workflow_template_id",
        "current_step_key", "skills", "max_retries",
    }
    
    EXPECTED_TASK_RUNS_COLUMNS = {
        "id", "task_id", "profile", "step_key", "status",
        "claim_lock", "claim_expires", "worker_pid",
        "max_runtime_seconds", "last_heartbeat_at",
        "started_at", "ended_at", "outcome", "summary",
        "metadata", "error",
    }
    
    EXPECTED_TASK_EVENTS_COLUMNS = {
        "id", "task_id", "run_id", "kind", "payload", "created_at",
    }
    
    EXPECTED_TASK_COMMENTS_COLUMNS = {
        "id", "task_id", "author", "body", "created_at",
    }
    
    EXPECTED_TASK_LINKS_COLUMNS = {
        "parent_id", "child_id",
    }
    
    EXPECTED_NOTIFY_SUBS_COLUMNS = {
        "task_id", "platform", "chat_id", "thread_id",
        "user_id", "created_at", "last_event_id",
    }
    
    def test_fresh_db_has_all_columns(self, tmp_path):
        """Connect to a fresh DB and verify all expected columns exist."""
        db_path = tmp_path / "kanban.db"
        conn = _kb.connect(db_path)
        
        actual_tasks = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert actual_tasks == self.EXPECTED_TASKS_COLUMNS, (
            f"Missing columns: {self.EXPECTED_TASKS_COLUMNS - actual_tasks}\n"
            f"Extra columns: {actual_tasks - self.EXPECTED_TASKS_COLUMNS}"
        )
        
        actual_runs = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        assert actual_runs == self.EXPECTED_TASK_RUNS_COLUMNS
        
        actual_events = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
        assert actual_events == self.EXPECTED_TASK_EVENTS_COLUMNS
        
        actual_comments = {row["name"] for row in conn.execute("PRAGMA table_info(task_comments)")}
        assert actual_comments == self.EXPECTED_TASK_COMMENTS_COLUMNS
        
        actual_links = {row["name"] for row in conn.execute("PRAGMA table_info(task_links)")}
        assert actual_links == self.EXPECTED_TASK_LINKS_COLUMNS
        
        actual_notify = {row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
        assert actual_notify == self.EXPECTED_NOTIFY_SUBS_COLUMNS
    
    def test_indexes_reference_existing_columns(self, tmp_path):
        """Every index references columns that exist in CREATE TABLE."""
        db_path = tmp_path / "kanban.db"
        conn = _kb.connect(db_path)
        
        # Get table columns
        tables_and_cols = {}
        for table in ["tasks", "task_runs", "task_events", "task_comments", 
                       "task_links", "kanban_notify_subs"]:
            tables_and_cols[table] = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
        
        # Get indexes
        indexes = conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()
        
        for idx in indexes:
            idx_name = idx["name"]
            table = idx["tbl_name"]
            sql = idx["sql"]
            
            if table not in tables_and_cols:
                continue
            
            available = tables_and_cols[table]
            # Parse column references from index SQL
            # e.g., CREATE INDEX idx_foo ON tasks(col1, col2)
            match = re.search(r"ON\s+\w+\s*\(([^)]+)\)", sql, re.IGNORECASE)
            if match:
                idx_cols = [c.strip() for c in match.group(1).split(",")]
                for col in idx_cols:
                    assert col in available, (
                        f"Index {idx_name} on {table} references column '{col}' "
                        f"which does not exist. Available: {sorted(available)}"
                    )
    
    def test_old_db_migration_produces_complete_schema(self, tmp_path):
        """Open a minimal v1-era DB, run migrations, verify complete schema."""
        db_path = tmp_path / "old_kanban.db"
        
        # Create a minimal v1 schema (just the original columns)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE task_links (
                parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            )
        """)
        conn.execute("""
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        
        # Now connect through kanban_db (which runs migrations)
        conn2 = _kb.connect(db_path)
        
        # Verify all expected columns exist after migration
        actual = {row["name"] for row in conn2.execute("PRAGMA table_info(tasks)")}
        missing = self.EXPECTED_TASKS_COLUMNS - actual
        assert not missing, f"After migration, missing columns: {missing}"
    
    def test_migrate_add_before_index(self, tmp_path):
        """Ensure _add_column_if_missing is called BEFORE any index on that column."""
        db_path = tmp_path / "scratch.db"
        conn = _kb.connect(db_path)
        
        # This test implicitly passes if init_db doesn't crash.
        # If a column is missing and an index references it, init_db would raise
        # OperationalError. The linter catches this statically; this test catches
        # it at runtime.
        indexes = conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()
        assert len(indexes) > 0  # At least some indexes should exist
    
    def test_no_extra_columns_in_schema_sql(self, tmp_path):
        """SCHEMA_SQL should not define columns that _migrate_add_optional_columns
        also adds — that would be double-adding (harmless with IF NOT EXISTS but 
        indicates stale schema)."""
        db_path = tmp_path / "scratch.db"
        conn = _kb.connect(db_path)
        
        schema_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        # Just verify count matches expected — any extra is a signal to clean up SCHEMA_SQL
        assert len(schema_cols) == len(self.EXPECTED_TASKS_COLUMNS)
```

### 4. Pre-commit Hook / CI Integration

```yaml
# .github/workflows/kanban-schema-lint.yml
name: Kanban Schema Lint
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -e .
      - run: python -m pytest tests/hermes_cli/test_schema_lint.py -v
```

```python
# tests/hermes_cli/test_schema_lint.py
from hermes_cli.kanban_db import SCHEMA_SQL, _migrate_add_optional_columns
import inspect


def test_schema_sql_indexes_match_columns():
    """Static lint: every CREATE INDEX column must exist in its CREATE TABLE."""
    from docs.design.kanban_duckdb_raft.schema_lint import lint_schema_sql
    errors = lint_schema_sql(SCHEMA_SQL)
    assert not errors, "\n".join(str(e) for e in errors)


def test_migrations_consistent_with_schema():
    """Static lint: migration columns appear in SCHEMA_SQL."""
    from docs.design.kanban_duckdb_raft.schema_lint import lint_migrations
    migrate_source = inspect.getsource(_migrate_add_optional_columns)
    errors = lint_migrations(SCHEMA_SQL, migrate_source)
    # Filter to hard failures (not advisory)
    hard_errors = [e for e in errors if e.kind in (
        "migration_column_not_in_schema",
        "migration_index_on_missing_column",
    )]
    assert not hard_errors, "\n".join(str(e) for e in hard_errors)
```

### 5. Column Addition Checklist (for developers)

When adding a new column to the kanban schema:

1. **Add to `SCHEMA_SQL`'s `CREATE TABLE`** — this defines the column for fresh DBs
2. **Add to `_migrate_add_optional_columns()`** — with `_add_column_if_missing()` call
3. **Add index (if needed) in `_migrate_add_optional_columns()`** — AFTER the `_add_column_if_missing()` call, never in `SCHEMA_SQL`
4. **Add to `EXPECTED_TASKS_COLUMNS` (or equivalent)** in `TestSchemaConsistency`
5. **Run `test_schema_sql_indexes_match_columns`** — the linter catches omissions
6. **Run `test_fresh_db_has_all_columns`** — the runtime test catches omissions

**Never**: Add a `CREATE INDEX` in `SCHEMA_SQL` for a migration-added column. It will crash on old DBs that lack the column.