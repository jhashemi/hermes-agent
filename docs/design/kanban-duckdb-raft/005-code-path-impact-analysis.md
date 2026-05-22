# Code Path Impact Analysis: kanban_db.py and Callers

**Status**: Proposed
**Date**: 2026-05-21
**Authors**: Jeff Dean, Werner Vogels

## Summary

The DuckDB migration + RAFT consensus changes touch **3 layers** of the codebase:

1. **Core**: `hermes_cli/kanban_db.py` — connection, transaction, CRUD, migration
2. **Consumers**: `tools/kanban_tools.py`, `gateway/run.py`, `plugins/kanban/`
3. **Tests**: 17+ test files

## Layer 1: Core — `hermes_cli/kanban_db.py`

### Functions Requiring Changes

#### Connection & Path Resolution

| Function | Lines | Current Behavior | Required Change |
|---|---|---|---|
| `kanban_db_path()` | 277-299 | Returns per-board Path (`boards/<slug>/kanban.db`) | Add `engine` param; returns `kanban.duckdb` path for DuckDB engine. Keep SQLite path for fallback. |
| `connect()` | 888-934 | Opens `sqlite3.connect()`, WAL mode, PRAGMAs | Add `engine` param. For DuckDB: `duckdb.connect()`, no WAL/PRAGMA, `KanbanConnection` wrapper. |
| `init_db()` | 937-963 | Forces `connect()` → schema init | No structural change; `connect()` handles engine selection internally. |
| `kanban_home()` | 141-161 | `HERMES_KANBAN_HOME` → `get_default_hermes_root()` | No change needed; anchors both SQLite and DuckDB paths. |
| `boards_root()` | 164-172 | `kanban_home() / "kanban" / "boards"` | No change needed; used for `board.json` metadata discovery, not DB paths. |

#### Schema & Migration

| Function | Lines | Current Behavior | Required Change |
|---|---|---|---|
| `SCHEMA_SQL` | 749-888 | SQLite `CREATE TABLE IF NOT EXISTS` + indexes | Create parallel `DUCKDB_SCHEMA_SQL` with `board` column in all tables. SQLite schema stays for fallback. |
| `_migrate_add_optional_columns()` | 985-1237 | SQLite `ALTER TABLE ADD COLUMN` + index creation | Add DuckDB branch: DuckDB `ALTER TABLE ADD COLUMN` syntax is identical to SQLite's. Skip PRAGMA-based checks (`PRAGMA table_info` → `DESCRIBE table`). |
| `_add_column_if_missing()` | ~970-985 | `ALTER TABLE ... ADD COLUMN` + `PRAGMA table_info` guard | Add DuckDB path using `DESCRIBE` instead of `PRAGMA table_info`. |
| `_INITIALIZED_PATHS` | 886 | Module-level set caching initialized DB paths | Expand to cache DuckDB paths alongside SQLite paths. For DuckDB, only one path (`kanban.duckdb`) regardless of board. |

#### Transaction Management

| Function | Lines | Current Behavior | Required Change |
|---|---|---|---|
| `write_txn()` | 1240-1255 | `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` | Add RAFT guard: `check_writable()` before BEGIN. Add engine dispatch: DuckDB `BEGIN TRANSACTION` (no IMMEDIATE). |

#### CRUD Operations (32 `write_txn` call sites)

All CRUD operations currently take `conn: sqlite3.Connection` and use `with write_txn(conn)`. The changes are uniform:

| Function | Lines | Change |
|---|---|---|
| `create_task()` | ~1532 | `write_txn` → RAFT-guarded + engine-dispatched. SQL unchanged (works in both SQLite and DuckDB). |
| `get_task()` | ~read | Add `WHERE board = ?` filter for DuckDB. |
| `list_tasks()` | ~read | Add `WHERE board = ?` filter for DuckDB. |
| `assign_task()` | ~1884 | write_txn guard. |
| `link_tasks()` | ~2038 | write_txn guard. |
| `unlink_tasks()` | ~2133 | write_txn guard. |
| `parent_ids()` | ~read | Board filter. |
| `child_ids()` | ~read | Board filter. |
| `parent_results()` | ~read | Board filter. |
| `add_comment()` | ~2384 | write_txn guard. |
| `list_comments()` | ~read | Board filter. |
| `list_events()` | ~read | Board filter. |
| `_append_event()` | ~2481 | write_txn guard. |
| `_end_run()` | ~2575 | write_txn guard. |
| `recompute_ready()` | ~2633 | write_txn guard. |
| `claim_task()` | ~2703 | write_txn guard. **Critical path**: claim CAS must work under RAFT. |
| `heartbeat_claim()` | ~2763 | write_txn guard. |
| `release_stale_claims()` | ~2855 | write_txn guard. |
| `reclaim_task()` | ~3114 | write_txn guard. |
| `complete_task()` | ~3218 | write_txn guard. |
| `block_task()` | ~3267 | write_txn guard. |
| `unblock_task()` | ~3302 | write_txn guard. |
| `archive_task()` | ~3408 | write_txn guard. |
| `set_workspace_path()` | ~3477 | write_txn guard. |
| `_record_worker_exit()` | ~3608 | write_txn guard. |
| `heartbeat_worker()` | ~3632 | write_txn guard. |
| `dispatch_once()` | variable | write_txn guard. **High-frequency**: called every 60s. |
| `add_notify_sub()` | ~4309 | write_txn guard. |
| `remove_notify_sub()` | ~4340 | write_txn guard. |
| `unseen_events_for_sub()` | ~read | Board filter. |
| `advance_notify_cursor()` | ~4407 | write_txn guard. |
| `gc_events()` | ~4427 | write_txn guard. |

#### Board Discovery

| Function | Lines | Current Behavior | Required Change |
|---|---|---|---|
| `list_boards()` | 464-505 | Scans `boards_root()` for dirs with `kanban.db` or `board.json` | For DuckDB: query `SELECT * FROM boards` instead of filesystem scan. Fallback to filesystem scan for SQLite. |
| `create_board()` | ~410 | Creates `boards/<slug>/kanban.db` | For DuckDB: `INSERT INTO boards` + DuckDB handles all boards. No filesystem DB creation. |
| `remove_board()` | ~440 | Deletes `boards/<slug>/kanban.db` | For DuckDB: `DELETE FROM tasks WHERE board = ?`, `DELETE FROM boards WHERE slug = ?`. No filesystem deletion. |
| `read_board_metadata()` | ~380 | Reads `boards/<slug>/board.json` | For DuckDB: query `boards` table. Fallback: read `board.json`. |
| `write_board_metadata()` | ~400 | Writes `boards/<slug>/board.json` | For DuckDB: `INSERT/UPDATE boards`. |

### New Functions to Add

```python
# Engine detection and abstraction
def _detect_engine() -> str:                       # "duckdb" or "sqlite"
def _connect_duckdb(*, board=None) -> KanbanConnection
def _connect_sqlite(db_path, *, board=None) -> KanbanConnection

# KanbanConnection adapter class
class KanbanConnection:
    """Unified interface for SQLite and DuckDB connections."""
    def execute(self, sql, params=None): ...
    def executescript(self, sql): ...
    def fetchone(self): ...
    def fetchall(self): ...
    def close(self): ...
    @property
    def engine(self) -> str: ...     # "sqlite" or "duckdb"
    @property
    def board(self) -> str: ...       # board context
    
# RAFT integration
class KanbanRaftNode: ...
class KanbanArbiter: ...
class KanbanReadOnlyError(Exception): ...

# Migration
def migrate_sqlite_to_duckdb(dry_run=True): ...
def _import_sqlite_board_to_duckdb(ddb, sqlite_path, board): ...
def pre_migration_check(): ...
```

## Layer 2: Consumers

### `tools/kanban_tools.py`

**Current**: Calls `_connect()` which does `from hermes_cli import kanban_db as kb; return kb, kb.connect()`.

**Change**: `_connect()` returns `(kb, KanbanConnection)` instead of `(kb, sqlite3.Connection)`. Since `KanbanConnection` exposes the same `.execute()` interface, the 7 tool functions (`kanban_create_task`, `kanban_list_tasks`, etc.) need **no code changes** — only the adapter layer changes.

### `gateway/run.py`

**Current** (`_kanban_advance`, `_kanban_unsub`, `_handle_kanban_command`):
```python
from hermes_cli import kanban_db as _kb
conn = _kb.connect(board=board)
```

**Change**: No change needed. `_kb.connect(board=board)` returns `KanbanConnection` which is duck-type compatible with `sqlite3.Connection`.

**New addition**: `_kanban_raft_arbiter_server()` method added to the gateway class.

### `plugins/kanban/dashboard/plugin_api.py`

**Current**: Imports `kanban_db`, calls `connect()`.

**Change**: Same as tools — `KanbanConnection` adapter makes this transparent.

### `hermes_cli/kanban.py`

**Current**: `run_slash()` dispatches CLI commands, likely calls `connect()` indirectly.

**Change**: No change needed at CLI level.

### `hermes_cli/kanban_specify.py`

**Current**: Kanban specification workflow.

**Change**: No change needed.

## Layer 3: Tests

### Affected Test Files (17 files)

| Test File | Change Type |
|---|---|
| `tests/hermes_cli/test_kanban_db.py` | Major: Add DuckDB engine tests; test `KanbanConnection` adapter; test board column filtering |
| `tests/hermes_cli/test_kanban_cli.py` | Minor: Ensure CLI commands work with DuckDB engine |
| `tests/hermes_cli/test_kanban_boards.py` | Medium: Board creation/deletion tests need DuckDB path |
| `tests/hermes_cli/test_kanban_notify.py` | Minor: notify tests should pass with adapter |
| `tests/hermes_cli/test_kanban_specify.py` | Minor |
| `tests/hermes_cli/test_kanban_specify_db.py` | Medium: DB schema tests need DuckDB variant |
| `tests/hermes_cli/test_kanban_diagnostics.py` | Minor |
| `tests/hermes_cli/test_pin_kanban_board_env.py` | Minor: env var tests |
| `tests/tools/test_kanban_tools.py` | Minor: tool tests should pass through adapter |
| `tests/plugins/test_kanban_dashboard_plugin.py` | Minor |
| `tests/stress/test_subprocess_e2e.py` | Medium: e2e stress test needs both engines |
| `tests/stress/test_property_fuzzing.py` | Medium: fuzz tests need both engines |
| `tests/stress/test_concurrency_mixed.py` | Medium: concurrency tests (RAFT impact) |

### New Test Files to Create

| Test File | Purpose |
|---|---|
| `tests/hermes_cli/test_kanban_duckdb.py` | DuckDB-specific: engine detection, schema, DuckDB SQL compatibility |
| `tests/hermes_cli/test_kanban_raft.py` | RAFT: election, log replication, partition handling, arbiter |
| `tests/hermes_cli/test_schema_lint.py` | Static schema consistency linter |
| `tests/hermes_cli/test_kanban_migration.py` | Migration: SQLite → DuckDB import, validation, rollback |

## Migration Complexity Assessment

### Low Complexity (adapter-transparent)
- All read-only functions (`list_tasks`, `get_task`, `board_stats`, etc.)
- All tool functions in `tools/kanban_tools.py`
- Gateway kanban command handling

### Medium Complexity (SQL dialect differences)
- `_migrate_add_optional_columns()`: `PRAGMA table_info` → `DESCRIBE`
- `write_txn()`: `BEGIN IMMEDIATE` → `BEGIN TRANSACTION`
- Schema creation: `AUTOINCREMENT` semantics differ slightly

### High Complexity (architecture change)
- `connect()`: Complete rewrite with engine detection
- `SCHEMA_SQL` / `DUCKDB_SCHEMA_SQL`: Dual schema maintenance
- `write_txn()`: RAFT integration (propose → commit → apply)
- `list_boards()`: Dual path (SQL query vs filesystem scan)
- Board metadata: `board.json` vs `boards` table

### Highest Complexity (correctness-critical)
- `claim_task()`: CAS under RAFT — must never double-claim
- `dispatch_once()`: High-frequency write under RAFT
- Migration script: Row count validation across engines

## Estimated Effort

| Task | Days | Owner |
|---|---|---|
| `KanbanConnection` adapter | 2 | Jeff |
| DuckDB schema (`DUCKDB_SCHEMA_SQL`) | 1 | Jeff |
| `connect()` engine detection | 1 | Jeff |
| `write_txn()` RAFT guard | 2 | Werner |
| `KanbanRaftNode` implementation | 5 | Werner |
| `KanbanArbiter` implementation | 2 | Werner |
| RAFT RPC server (HTTP/Tailscale) | 3 | Werner |
| Board column filtering (all reads) | 2 | Jeff |
| `_migrate_add_optional_columns` dual path | 1 | Jeff |
| Schema linter | 2 | Jeff |
| Migration script | 3 | Jeff |
| Test updates + new tests | 5 | Both |
| Shadow mode + validation | 3 | Both |
| **Total** | **32** | — |

## Dependency Graph

```
KanbanConnection ──► connect() rewrite ──► board column filtering
      │                                        │
      ▼                                        ▼
DUCKDB_SCHEMA_SQL ──► _migrate dual path ──► Migration script
                                                    │
write_txn RAFT guard ◄── KanbanRaftNode ◄──────────┘
      │                    │
      ▼                    ▼
KanbanArbiter ──► RAFT RPC server ──► Gateway integration
                                          │
                                          ▼
                                    Schema linter
                                          │
                                          ▼
                                    Test updates
```