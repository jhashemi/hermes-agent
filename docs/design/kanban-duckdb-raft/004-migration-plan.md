# Migration Plan: SQLite → DuckDB (Zero-Downtime, Backward-Compatible)

**Status**: Proposed
**Date**: 2026-05-21
**Authors**: Jeff Dean, Werner Vogels

## Overview

Migrate from per-board SQLite `.db` files to a unified DuckDB `kanban.duckdb`, zero downtime, with rollback capability.

## Current State

```
~/.hermes/kanban/
├── kanban.db                          # default board DB
├── boards/
│   ├── executive_agents/
│   │   ├── kanban.db                  # executive_agents board DB
│   │   └── board.json
│   ├── okr_2026_q2/
│   │   ├── kanban.db                  # okr_2026_q2 board DB
│   │   └── board.json
│   └── ...                            # N boards, N kanban.db files
├── current                            # symlink to active board slug
└── (no unified database)
```

## Target State

```
~/.hermes/kanban/
├── kanban.duckdb                      # UNIFIED database (all boards)
├── kanban.duckdb.wal                  # DuckDB WAL (auto-managed)
├── raft/                              # RAFT consensus state
│   ├── log.bin
│   └── state.json
├── archive/                           # Migrated SQLite files (read-only backup)
│   ├── 2026-05-21_kanban.db.default.gz
│   ├── 2026-05-21_kanban.db.executive_agents.gz
│   └── 2026-05-21_kanban.db.okr_2026_q2.gz
├── boards/                            # Still exists for board.json metadata
│   ├── executive_agents/
│   │   └── board.json                 # Preserved (metadata only)
│   └── okr_2026_q2/
│       └── board.json
└── current                            # symlink (preserved)
```

## Migration Phases

### Phase 0: Pre-Migration Checks

Run on BOTH machines before starting.

```python
def pre_migration_check():
    """Verify system is healthy before migration."""
    checks = []
    
    # 1. DuckDB importable
    try:
        import duckdb
        checks.append(("duckdb_version", duckdb.__version__, "OK"))
    except ImportError:
        checks.append(("duckdb_import", None, "FAIL: duckdb not installed"))
    
    # 2. All board DBs readable
    for board in list_boards():
        slug = board["slug"]
        try:
            conn = connect(board=slug)
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            checks.append((f"board_{slug}_tasks", task_count, "OK"))
            conn.close()
        except Exception as e:
            checks.append((f"board_{slug}_read", None, f"FAIL: {e}"))
    
    # 3. Disk space (DuckDB file needs ~60% of total SQLite sizes due to compression)
    total_sqlite_size = sum(
        kanban_db_path(board=board["slug"]).stat().st_size
        for board in list_boards()
    )
    available = shutil.disk_usage(kanban_home()).free
    needed = total_sqlite_size * 2  # DuckDB + safety margin
    checks.append(("disk_space_needed_mb", needed // 1024 // 1024, 
                    "OK" if available > needed else "FAIL"))
    
    # 4. No in-flight tasks (ideally empty ready queue)
    for board in list_boards():
        conn = connect(board=board["slug"])
        running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        checks.append((f"board_{board['slug']}_running", running, 
                        "OK" if running == 0 else "WARN"))
        conn.close()
    
    return checks
```

### Phase 1: Dual-Write Shadow Mode

**Goal**: Write to both SQLite and DuckDB simultaneously. Read from SQLite (canonical). Compare results.

```python
class DualWriteConnection:
    """Wraps SQLite (primary) + DuckDB (shadow) for dual-write mode."""
    
    def __init__(self, sqlite_conn, duckdb_conn, board: str):
        self._sqlite = sqlite_conn
        self._duckdb = duckdb_conn
        self._board = board
        self._discrepancies = []
    
    @contextlib.contextmanager
    def write_txn(self):
        with _write_txn_sqlite(self._sqlite):
            try:
                with _write_txn_duckdb(self._duckdb):
                    yield self
            except Exception as e:
                # DuckDB write failed — log but don't block
                self._discrepancies.append(("duckdb_write_fail", str(e)))
                yield self  # Still allow SQLite write
    
    def execute(self, sql, params=None):
        # Write to SQLite (primary)
        result = self._sqlite.execute(sql, params)
        # Shadow-write to DuckDB (fire and forget errors)
        try:
            self._duckdb.execute(sql, params)
        except Exception as e:
            self._discrepancies.append(("duckdb_shadow_fail", sql, str(e)))
        return result
```

**Duration**: Run in shadow mode for **48 hours** minimum. Monitor discrepancy logs.

**Activation**: Set `HERMES_KANBAN_ENGINE=shadow` in gateway environment.

### Phase 2: Data Import (One-Time)

Import all SQLite board DBs into DuckDB. This is a **batch operation** that runs once while the gateway is in maintenance mode.

```python
def migrate_sqlite_to_duckdb(dry_run: bool = True):
    """Import all SQLite board DBs into DuckDB.
    
    Steps:
    1. Create DuckDB with full schema (with board column)
    2. For each board, ATTACH its SQLite DB and INSERT INTO DuckDB
    3. Validate row counts match
    4. Archive SQLite files
    """
    import duckdb
    
    duckdb_path = kanban_home() / "kanban.duckdb"
    ddb = duckdb.connect(str(duckdb_path))
    
    # Create schema
    ddb.execute(DUCKDB_SCHEMA_SQL)
    
    boards = list_boards()
    results = []
    
    for board_meta in boards:
        slug = board_meta["slug"]
        sqlite_path = kanban_db_path(board=slug)
        
        if not sqlite_path.exists():
            results.append({"board": slug, "status": "skip", "reason": "no SQLite file"})
            continue
        
        # Count rows in SQLite
        sqlite_conn = sqlite3.connect(str(sqlite_path))
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_tasks = sqlite_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        sqlite_runs = sqlite_conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_runs'"
        ).fetchone()[0]
        
        if dry_run:
            results.append({
                "board": slug, "status": "dry_run",
                "sqlite_tasks": sqlite_tasks,
                "sqlite_path": str(sqlite_path),
            })
            sqlite_conn.close()
            continue
        
        # Attach SQLite DB to DuckDB session
        ddb.execute(f"ATTACH '{sqlite_path}' AS sqlite_src (TYPE SQLITE)")
        
        # Import tasks with board column
        ddb.execute(f"""
            INSERT INTO tasks (
                board, id, title, body, assignee, status, priority,
                created_by, created_at, started_at, completed_at,
                workspace_kind, workspace_path, claim_lock, claim_expires,
                tenant, result, idempotency_key, consecutive_failures,
                worker_pid, last_failure_error, max_runtime_seconds,
                last_heartbeat_at, current_run_id,
                workflow_template_id, current_step_key, skills, max_retries
            )
            SELECT '{slug}', id, title, body, assignee, status, priority,
                   created_by, created_at, started_at, completed_at,
                   workspace_kind, workspace_path, claim_lock, claim_expires,
                   tenant, result, idempotency_key, consecutive_failures,
                   worker_pid, last_failure_error, max_runtime_seconds,
                   last_heartbeat_at, current_run_id,
                   workflow_template_id, current_step_key, skills, max_retries
            FROM sqlite_src.tasks
        """)
        
        # Import task_links
        ddb.execute(f"""
            INSERT INTO task_links (board, parent_id, child_id)
            SELECT '{slug}', parent_id, child_id FROM sqlite_src.task_links
        """)
        
        # Import task_comments
        try:
            ddb.execute(f"""
                INSERT INTO task_comments (board, id, task_id, author, body, created_at)
                SELECT '{slug}', id, task_id, author, body, created_at 
                FROM sqlite_src.task_comments
            """)
        except Exception:
            pass  # Table may not exist in very old SQLite DBs
        
        # Import task_events
        try:
            ddb.execute(f"""
                INSERT INTO task_events (board, id, task_id, run_id, kind, payload, created_at)
                SELECT '{slug}', id, task_id, run_id, kind, payload, created_at
                FROM sqlite_src.task_events
            """)
        except Exception as e:
            results.append({"board": slug, "warning": f"task_events import: {e}"})
        
        # Import task_runs
        try:
            ddb.execute(f"""
                INSERT INTO task_runs (
                    board, id, task_id, profile, step_key, status,
                    claim_lock, claim_expires, worker_pid,
                    max_runtime_seconds, last_heartbeat_at,
                    started_at, ended_at, outcome, summary, metadata, error
                )
                SELECT '{slug}', id, task_id, profile, step_key, status,
                       claim_lock, claim_expires, worker_pid,
                       max_runtime_seconds, last_heartbeat_at,
                       started_at, ended_at, outcome, summary, metadata, error
                FROM sqlite_src.task_runs
            """)
        except Exception as e:
            results.append({"board": slug, "warning": f"task_runs import: {e}"})
        
        # Import kanban_notify_subs
        try:
            ddb.execute(f"""
                INSERT INTO kanban_notify_subs (
                    board, task_id, platform, chat_id, thread_id,
                    user_id, created_at, last_event_id
                )
                SELECT '{slug}', task_id, platform, chat_id, thread_id,
                       user_id, created_at, last_event_id
                FROM sqlite_src.kanban_notify_subs
            """)
        except Exception:
            pass
        
        # Detach SQLite
        ddb.execute("DETACH sqlite_src")
        sqlite_conn.close()
        
        # Validate row counts
        duckdb_tasks = ddb.execute(
            f"SELECT COUNT(*) FROM tasks WHERE board = '{slug}'"
        ).fetchone()[0]
        
        if duckdb_tasks != sqlite_tasks:
            results.append({
                "board": slug, "status": "MISMATCH",
                "sqlite_count": sqlite_tasks, "duckdb_count": duckdb_tasks,
            })
            # DON'T archive — investigation needed
            continue
        
        # Archive SQLite file
        archive_dir = kanban_home() / "archive"
        archive_dir.mkdir(exist_ok=True)
        archive_name = f"2026-05-21_kanban.db.{slug}.gz"
        import gzip
        with open(sqlite_path, 'rb') as f_in:
            with gzip.open(archive_dir / archive_name, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        # Rename (not delete) original
        sqlite_path.rename(sqlite_path.with_suffix(".db.migrated"))
        
        results.append({
            "board": slug, "status": "OK",
            "tasks_imported": duckdb_tasks,
            "archived_to": str(archive_dir / archive_name),
        })
    
    ddb.close()
    return results
```

### Phase 3: Cutover — DuckDB Primary, SQLite Read-Only

**Goal**: DuckDB becomes the primary read/write source. SQLite files are in `.migrated` state (not deleted).

```python
def connect(db_path=None, *, board=None, engine=None):
    _engine = engine or _detect_engine()
    
    if _engine == 'duckdb':
        conn = _connect_duckdb(board=board)
        return KanbanConnection(conn, engine='duckdb', board=board)
    
    # SQLite fallback: only works if .db files still exist (pre-migration)
    conn = _connect_sqlite(db_path, board=board)
    return KanbanConnection(conn, engine='sqlite', board=board)


def _connect_duckdb(*, board=None):
    """Open DuckDB, returning connection with board context."""
    import duckdb
    ddb_path = kanban_home() / "kanban.duckdb"
    conn = duckdb.connect(str(ddb_path))
    
    # Set board context for queries that need it
    if board:
        conn.execute(f"SET default_board = '{_normalize_board_slug(board)}'")
    
    return conn
```

### Phase 4: Remove SQLite Fallback (After Validation)

After **7 days** of DuckDB-primary operation with zero discrepencies:

1. Delete `.db.migrated` files (archives exist as gz backups)
2. Remove SQLite-specific code paths from `connect()`
3. Remove `DualWriteConnection` class
4. Remove `HERMES_KANBAN_ENGINE` env var handling

## Rollback Plan

| Stage | Rollback Action |
|---|---|
| Phase 1 (shadow) | Remove `HERMES_KANBAN_ENGINE=shadow`, restart gateway |
| Phase 2 (import) | Delete `kanban.duckdb`, restore `.db.migrated` → `.db` |
| Phase 3 (cutover) | Set `HERMES_KANBAN_ENGINE=sqlite`, restore `.db.migrated` files |
| Phase 4 (cleanup) | Archives in `archive/` directory; restore from gzip |

**Rollback time target**: <5 minutes for any phase.

## Zero-Downtime Strategy

The migration does NOT require stopping the gateway:

1. **Phase 1 (shadow)**: Gateway continues normally. Shadow writes are async — failures are logged, not surfaced.
2. **Phase 2 (import)**: Run during low-activity period. DuckDB is created fresh; SQLite DBs continue serving reads. Import is additive.
3. **Phase 3 (cutover)**: Single-restart event. Gateway reads `HERMES_KANBAN_ENGINE=duckdb` on startup. Duration: ~2 seconds (Python import + DuckDB open).

**Critical**: The dispatcher and notifier watchers must be quiesced during the cutover restart. The gateway's existing `asyncio.create_task()` pattern means tasks are cancelled cleanly on shutdown.

## Cross-Machine Migration Sequence

Since hermes1 and hermes2 must end up with identical DuckDB databases, the migration order matters:

1. **Migrate hermes1 first** (it has DuckDB 1.5.2 installed)
2. **Start RAFT on hermes1** as leader
3. **Migrate hermes2** — import its SQLite files into a fresh DuckDB
4. **Join hermes2 to RAFT cluster** as follower
5. **RAFT log replay** synchronizes any writes that happened between step 1 and step 4

Alternatively (and simpler for initial migration):

1. **Stop dispatchers on both machines** (`HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`)
2. **Migrate hermes1** (SQLite → DuckDB)
3. **Migrate hermes2** (SQLite → DuckDB)
4. **Validate both DuckDBs are identical** (row counts per board)
5. **Start RAFT on both** (first to become leader wins)
6. **Re-enable dispatchers**

**Window**: 5-15 minutes of no-dispatcher operation. Existing in-flight workers continue running; they write their results back when they finish, via RAFT.