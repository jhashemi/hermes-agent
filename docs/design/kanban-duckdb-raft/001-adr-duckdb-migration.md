# ADR-001: DuckDB Migration for Hermes Kanban Boards

**Status**: Proposed
**Date**: 2026-05-21
**Authors**: Jeff Dean (infrastructure), Werner Vogels (distributed systems)

## Context

Hermes kanban boards use per-board SQLite databases at `~/.hermes/kanban/boards/<slug>/kanban.db` (plus `~/.hermes/kanban/kanban.db` for the default board). This architecture caused a 24-hour outage from 5 independent schema-migration failures across hermes1 (100.107.83.25) and hermes2 (100.79.15.66).

DuckDB 1.5.2 is already installed on hermes1. The columnar storage model and single-file architecture make it a natural fit for consolidating per-board SQLite DBs into one unified database.

### Problems with Current Architecture

1. **Schema drift**: Each board's SQLite file runs independent migrations. When `connect()` → `init_db()` → `_migrate_add_optional_columns()` fires on 5 different `.db` files across 2 machines, any divergence in migration ordering or partial failure causes schema mismatch.
2. **N+1 query patterns**: `list_boards()` scans the filesystem for `boards/*/kanban.db` files, then each board's stats require a separate `connect(board=slug)` call. Analytics across all boards requires opening N connections.
3. **No cross-board queries**: "Show all tasks across every board assigned to agent X" requires iterating every board DB — O(boards) connections.
4. **Filesystem-based board discovery**: `list_boards()` depends on `boards_root().is_dir()` and directory scanning — fragile on NFS/FUSE (already noted in WAL fallback code).

## Decision

Replace per-board SQLite databases with a **unified DuckDB database** at `~/.hermes/kanban/kanban.duckdb`.

### Schema Design: Partitioned Table Model

Rather than creating separate tables per board (`tasks_executive_agents`, `tasks_okr_2026_q2`), we use a **`board` column partition** model:

```sql
-- All tasks from all boards in ONE table with a board column
CREATE TABLE tasks (
    board        TEXT NOT NULL DEFAULT 'default',
    id           TEXT NOT NULL,  -- globally unique (includes board prefix)
    title        TEXT NOT NULL,
    body         TEXT,
    -- ... all 25+ existing columns ...
    PRIMARY KEY (board, id)
);

CREATE TABLE task_links (
    board      TEXT NOT NULL DEFAULT 'default',
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (board, parent_id, child_id)
);

CREATE TABLE task_comments (
    board      TEXT NOT NULL DEFAULT 'default',
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    -- ...
);

CREATE TABLE task_events (
    board      TEXT NOT NULL DEFAULT 'default',
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    -- ...
);

CREATE TABLE task_runs (
    board      TEXT NOT NULL DEFAULT 'default',
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    -- ...
);

CREATE TABLE kanban_notify_subs (
    board      TEXT NOT NULL DEFAULT 'default',
    task_id    TEXT NOT NULL,
    -- ...
    PRIMARY KEY (board, task_id, platform, chat_id, thread_id)
);

-- Board metadata table (replaces per-board board.json files)
CREATE TABLE boards (
    slug       TEXT PRIMARY KEY,
    display_name TEXT,
    archived   INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    metadata   TEXT  -- JSON blob for extensibility
);
```

**Rationale for partition-by-column over per-board tables**:
- DuckDB's columnar storage means `WHERE board = 'executive_agents'` effectively partitions at scan time — no separate table needed.
- Single table = single migration path. `SCHEMA_SQL` changes apply once, not N times.
- Cross-board analytics become trivial: `SELECT board, COUNT(*) FROM tasks WHERE status='running' GROUP BY board`.
- DuckDB doesn't have native partitioning (like PostgreSQL's `PARTITION BY`), but its columnar scan with filter pushdown achieves equivalent performance for our dataset sizes (thousands, not millions of rows per board).

### DuckDB Columnar Benefits

1. **Analytics queries**: `SELECT status, COUNT(*) FROM tasks GROUP BY status` — DuckDB scans only the `status` column, not the entire row.
2. **Efficient aggregations**: Board stats, assignee workloads, failure rates — all columnar-scannable.
3. **Compression**: Columnar storage compresses repetitive values well (e.g., `status` has only ~8 distinct values across thousands of rows).
4. **Parquet export**: `COPY (SELECT * FROM tasks) TO 'kanban_backup.parquet'` for zero-cost snapshots.

### SQLite Fallback

For embedded/constrained environments where DuckDB binary isn't available:

```python
def connect(db_path=None, *, board=None, engine=None):
    """Open kanban DB with engine selection.
    
    engine='duckdb' (default when available) | 'sqlite' (fallback)
    """
    _engine = engine or _detect_engine()
    if _engine == 'duckdb':
        return _connect_duckdb(db_path, board=board)
    return _connect_sqlite(db_path, board=board)

def _detect_engine():
    """Return 'duckdb' if importable and DUCKDB_PATH exists, else 'sqlite'."""
    try:
        import duckdb
        return 'duckdb'
    except ImportError:
        return 'sqlite'
```

The SQLite fallback path preserves the current per-board `.db` file architecture. We do NOT try to maintain a single-file SQLite mode — the whole point is moving away from that.

### Connection API Changes

Current:
```python
conn = connect(board='executive_agents')  # opens boards/executive_agents/kanban.db
```

New:
```python
conn = connect(board='executive_agents')  # opens kanban.duckdb, all queries filtered by board
```

The returned connection object will be wrapped in a `KanbanConnection` adapter that:
- On DuckDB: passes through to `duckdb.DuckDBPyConnection`
- On SQLite: wraps `sqlite3.Connection` as before
- Both expose the same `.execute()`, `.executescript()`, `.fetchone()`, `.fetchall()` interface

### Write Transaction Adaptation

DuckDB doesn't have `BEGIN IMMEDIATE`. The `write_txn` context manager adapts:

```python
@contextlib.contextmanager
def write_txn(conn):
    if _is_duckdb(conn):
        conn.execute("BEGIN TRANSACTION")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
```

**Key difference**: DuckDB uses MVCC, not WAL. Concurrent writes are serialized by DuckDB's internal lock (single-writer per DB file). No `IMMEDIATE` needed — the `BEGIN TRANSACTION` acquires the write lock on first write statement.

### `PRAGMA` Adaptation

SQLite PRAGMAs that need DuckDB equivalents or removal:

| SQLite PRAGMA | DuckDB Equivalent | Notes |
|---|---|---|
| `PRAGMA journal_mode=WAL` | N/A | DuckDB always uses MVCC |
| `PRAGMA synchronous=NORMAL` | N/A | DuckDB handles durability internally |
| `PRAGMA foreign_keys=ON` | `PRAGMA foreign_keys=ON` | Supported in DuckDB ≥1.0 |
| `PRAGMA table_info(tasks)` | `DESCRIBE tasks` | Different result shape |

## Consequences

### Positive
- **Single migration path**: Schema changes apply once to one database.
- **Cross-board analytics**: Natural columnar queries across all boards.
- **Smaller backup surface**: One `.duckdb` file instead of N `.db` files.
- **Better compression**: Columnar storage for monitoring/timeseries patterns.

### Negative
- **Single-writer bottleneck**: DuckDB allows one writer at a time (same as SQLite WAL, but no concurrent readers during writes in some configs). For our workload (sub-second writes, seconds between dispatches), this is acceptable.
- **Binary size**: DuckDB adds ~30MB to the dependency. Already installed on hermes1.
- **No NFS/FUSE mode**: DuckDB's single-file lock assumes local filesystem. Tailscale-mounted paths won't work — RAFT replication handles cross-machine sync instead.
- **Migration complexity**: One-time SQLite → DuckDB import with careful row mapping.

### Risks and Mitigations

| Risk | Mitigation |
|---|---|
| DuckDB write lock contention | Monitor `write_txn` hold time; batch small writes |
| Migration data loss | Archive SQLite files, not delete; validate row counts post-import |
| API incompatibility | `KanbanConnection` adapter normalizes `sqlite3.Row` vs `duckdb.row` |
| Test suite breakage | Dual-engine test runners; SQLite path must still pass |

## Implementation Order

1. `KanbanConnection` adapter class (unified interface)
2. `connect()` with engine detection
3. DuckDB schema (with `board` column)
4. Migration script: SQLite → DuckDB
5. Update all `_kb.connect(board=slug)` call sites to pass `board` through to queries
6. Update `_migrate_add_optional_columns()` for DuckDB (single-pass, no PRAGMA)
7. Archive SQLite files post-migration