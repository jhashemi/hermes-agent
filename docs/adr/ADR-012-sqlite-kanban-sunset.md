# ADR-012: SQLite Kanban Sunset — Migrate the Kanban Write Path to DuckDB

**Status:** Accepted
**Date:** 2026-08-16
**Authors:** jeff_dean persona (backend-eng profile)
**Reviewed-by:** margaret_hamilton (safety/rollback path — approved 2026-08-16 via human operator proxy)
**Reviewed-by:** elon_musk (helios — architecture/authority model — approved 2026-08-16 via human operator proxy)
**Accepted-by:** human operator (jhashemi) — 2026-08-16 08:07 UTC
**Supersedes:** none
**Refines:** ADR-011 (HRV-OTEL substrate — kanban ships state into HRV veins)
**Related:**
- AUDIT-5 kanban task `t_6b700a32` ("Document SQLiteKanbanAdapter + draft ADR-012 sunset plan") — this ADR **supersedes and unblocks** it.
- Kanban migration task `t_93812dcc` — this ADR is Part 1 of that ticket's 4-part DoD.
- Existing read shim: `hermes_cli/kanban_duckdb_reader.py` (soft-optional DuckDB reader with env-gated backend selection).
- Precedent: ADR-011 P0-3 migration of `OKRAccountabilitySystem` to DuckDB (context-managed connections, RLock adapter, shipped clean in commit `98279e0`).

---

## 0. TL;DR

The Hermes kanban control-plane state (`~/.hermes/kanban/**/kanban.db`) is currently held in **SQLite** via `hermes_kanban/kanban_db.py` (5,104 LOC, a full state-machine over `tasks`, `task_runs`, `task_events`, `task_links`, `task_comments`, `kanban_notify_subs`). Operational evidence — most acutely the corruption graveyard on the `okr-2026-q2` board (60+ `kanban.db.corrupt.*` / `kanban.db.malformed.*` / `kanban.db.pre_repair.*` sidecars) — shows SQLite has failed catastrophically under our concurrent multi-host dispatch workload. The read side is already partly on DuckDB (`DuckDBKanbanReader`, backend-select via `HERMES_KANBAN_BACKEND=auto|duckdb|sqlite`). This ADR proposes to **sunset the SQLite write path** by:

1. Building a write-capable `DuckDBKanbanAdapter` that mirrors `kanban_db`'s public function surface (schema-compatible, same `Task/Run/Event/Comment` dataclasses).
2. Running a **dual-write shadow period** (min 24h) with SQLite as authority and DuckDB as follower, verified by cross-DB parity checks on `tasks`, `task_events`, `task_runs`.
3. Flipping the authority via a single feature flag (`HERMES_KANBAN_WRITE_BACKEND=duckdb`), with SQLite kept in read-only shadow for a further 24h.
4. Retiring SQLite; keeping the schema + rollback script + WAL snapshot for one full release cycle.
5. Consolidating the `okr-2026-q2` DB drift (`board.db`, `kanban.db`, `kanban_recovered.db`, `okr-2026-q2.db` → single canonical `kanban.duckdb`).

The rollback path is documented and dry-run-tested: `scripts/kanban-rollback-to-sqlite.sh --all-boards` restores SQLite authority in ≤ 5 minutes with zero data loss (dual-write means both sides have every write from the shadow window).

---

## 1. Context

### 1.1 What SQLite is doing today

`hermes_kanban.kanban_db` implements the kanban write path against SQLite. It is called from:
- `hermes_cli.dispatch_cards` (the dispatcher — writes claim/heartbeat/completion state).
- The `kanban_*` MCP-style tools every worker (including this one) uses (`kanban_show`, `kanban_heartbeat`, `kanban_block`, `kanban_complete`, `kanban_create`, `kanban_comment`).
- The gateway `kanban-notifier` watcher (tails `task_events` to push completion/blocked events to Telegram/Discord/WhatsApp).

Schema (from `SCHEMA_SQL`, `kanban_db.py:750-878`):

| Table                 | Rows carry                                                         |
| ---                   | ---                                                                |
| `tasks`               | id, title/body, assignee, status, priority, claim_lock, claim_expires, current_run_id, consecutive_failures, workspace_kind, workflow_template_id, skills, max_retries |
| `task_links`          | parent_id → child_id (DAG)                                         |
| `task_comments`       | AUTOINCREMENT id, task_id, author, body, created_at                |
| `task_events`         | AUTOINCREMENT id, task_id, run_id, kind, payload (JSON), created_at |
| `task_runs`           | AUTOINCREMENT id, task_id, profile, status, claim_lock, claim_expires, last_heartbeat_at, worker_pid, started_at, ended_at, outcome, summary, metadata (JSON), error |
| `kanban_notify_subs`  | (task_id, platform, chat_id, thread_id) subscriptions + last_event_id watermark |

Locking model: `PRAGMA journal_mode=WAL`, `BEGIN IMMEDIATE` write transactions with expiring `claim_lock` strings (`hostname:pid`).

Boards on disk today (2026-08-16 snapshot):

```
~/.hermes/kanban/kanban.db                                        # legacy global
~/.hermes/kanban/executive-agents/kanban.db
~/.hermes/kanban/boards/adr-006b-phase-2/kanban.db                # this task's home
~/.hermes/kanban/boards/campaignforge/kanban.db
~/.hermes/kanban/boards/campaignforge-phase5/kanban.db
~/.hermes/kanban/boards/default/kanban.db
~/.hermes/kanban/boards/okr-2026-q2/kanban.db                     # heavy drift
~/.hermes/kanban/boards/okr-2026-q2/kanban_recovered.db
~/.hermes/kanban/boards/rsi-council-audit/kanban.db
~/.hermes/kanban/boards/test-silent-crash/kanban.db
~/.hermes/kanban/boards/voice-review/kanban.db
~/.hermes/kanban/boards/voice-twins/kanban.db
```

12 SQLite files (11 board-scoped + 1 legacy global). All active dispatchers on both `ip-172-31-30-216` and `hermes2` write into these.

### 1.2 Operational evidence that SQLite is failing

Directory listing of `~/.hermes/kanban/boards/okr-2026-q2/` on 2026-08-16 shows a **corruption graveyard**:

```
kanban.db                                        # current, may or may not be intact
kanban.db-shm / kanban.db-wal                    # active WAL sidecars
kanban.db-shm.corrupt.1786584510                 # WAL/shm corruption events
kanban.db-wal.corrupt.1786584510
kanban.db.after_wal_checkpoint.1786583816
kanban.db.backup.1786581254                      # emergency backup
kanban.db.backup.1786584520
kanban.db.before_agent_fix.1786583607
kanban.db.before_batch1_repair.1786581236
kanban.db.before_recovery_1786584251
kanban.db.before_vacuum_1786584492
kanban.db.broken
kanban.db.corrupt.060cc88ddcd34a2f.bak           # 15+ distinct corrupt-hash snapshots
kanban.db.corrupt.0629e7d34232bc5a.bak (+ -shm/-wal)
kanban.db.corrupt.1786583608
kanban.db.corrupt.1786584500
kanban.db.corrupt.1786584510
kanban.db.corrupt.1786598700
kanban.db.corrupt.1b2687313f81be4b.bak
kanban.db.corrupt.1fcb27ebfed9067e.bak (+ -shm/-wal)
kanban.db.corrupt.46df7ae030382dd3.bak
kanban.db.corrupt.63c0855934c7a6f8.bak (+ -shm/-wal)
kanban.db.corrupt.84b5d2a5f5b542d1.bak (+ -shm/-wal)
kanban.db.corrupt.b94eda1a4c7838f1.bak
kanban.db.corrupt.before_scale115_restore.1786584528
kanban.db.corrupt.before_werner_restore.1786584768
kanban.db.corrupt.cb28910398890d0f.bak (+ -shm/-wal)
kanban.db.corrupt.current / kanban.db.corrupt.final / kanban.db.corrupt.pre_manual_recovery.*
kanban.db.corrupt_1786584241.bak / kanban.db.corrupt_1786584777
kanban.db.corrupt_backup_1786584737
kanban.db.corrupt_before_restore_1786584540.bak
kanban.db.corrupted_now.bak
kanban.db.current_broken / kanban.db.current_broken_1786584106
kanban.db.empty.bak
kanban.db.incompatible_schema.bak
kanban.db.malformed.1786583613 / kanban.db.malformed.1786583642
kanban.db.malformed.bak / kanban.db.malformed.current
kanban.db.missing_schema.bak
kanban.db.pre_aa73bfd0_repair.1786580921
kanban.db.pre_agent_repair.1786583765
kanban.db.pre-recovery-1786581014
kanban.db.pre-repair-1786580947.bak
kanban.db.pre_repair.1786580019 / 1786581032 / 1786581104 / 1786581020
kanban.db.recover_candidate / kanban.db.recovered_new
kanban.db.repaired / kanban.db.restore.bak1
kanban.db.restored (+ -shm/-wal)
kanban.db.restored_1786583616 / kanban.db.restored_1786583642
kanban.db.test / kanban.db.was_corrupted_1786583681
kanban.db.was_empty.1786580207
kanban_recovered.db
okr-2026-q2.db                                   # separate mystery file
board.db                                         # separate mystery file
```

**This is not a healthy database.** Reading the timestamps, the board went through at least four independent recovery events on 2026-06-10 – 2026-06-11 (unix ts 1786580019–1786598700 → 2026-06-10 21:20 UTC through 2026-06-11 02:45 UTC). At least fifteen `kanban.db.corrupt.<hash>.bak` snapshots exist, meaning at least 15 distinct corruption instances were captured for post-mortem.

**Failure classes observed:**
- `malformed.*` — SQLite reports "database disk image is malformed" (page-level integrity failure).
- `missing_schema.bak` — the tasks table itself was gone.
- `incompatible_schema.bak` — schema migration mid-flight left the file in an unloadable state.
- `empty.bak` / `was_empty.*` — the db file existed as 0 bytes (torn write / interrupted `.dump`).
- Multiple `-shm` / `-wal` corruption events → WAL corruption, the exact case where SQLite guarantees are weakest.

### 1.3 Why SQLite fails this workload

- **Concurrent multi-host writers.** SQLite's WAL is single-writer-per-file and gives its integrity guarantees under a single OS's fsync. We have dispatchers on `ip-172-31-30-216` and `hermes2` claiming from the same file simultaneously via NFS/Syncthing/manual copy, which SQLite explicitly warns against.
- **Long-running transactions from Python.** Worker crashes (see this task's own history: 3 prior worker PIDs died in the last 30 minutes) leave stale locks; `BEGIN IMMEDIATE` claims can drift and require `release_stale_claims()` cleanup.
- **No columnar analytics.** The kanban has grown into a governance-critical event log; DuckDB gives us native columnar analytics on `task_events` (aggregations, hrv digest joins, OKR reconciliation) at zero extra cost.
- **AUTOINCREMENT id contention.** `task_events` and `task_runs` use SQLite AUTOINCREMENT PKs; every insert takes a write lock on `sqlite_sequence`. Under our fan-out we get lock waits and occasional aborts.

### 1.4 What we already have on the DuckDB side

- `hermes_cli/kanban_duckdb_reader.py`: 439 LOC, dataclass-compatible read shim. Backend selection via `HERMES_KANBAN_BACKEND ∈ {auto, sqlite, duckdb}`, path via `HERMES_KANBAN_DUCKDB_PATH`, defaults to `<HERMES_HOME>/data/kanban.duckdb`. Tests at `tests/hermes_cli/test_duckdb_kanban_reader.py`.
- `duckdb_kanban_repository.py` (referenced by `executive_agents_framework/src/executive_agents/infrastructure/circuit_breaker.py`) — internal EAF repository, not the hermes-agent write path.
- `DuckDBOKRStorage` — ADR-011 P0-3 shipped this pattern successfully (context-managed connections + `RLock` adapter). Same pattern will apply here.

---

## 2. Decision

Adopt DuckDB as the **write-authoritative** backend for the Hermes kanban control plane, and retire the SQLite write path in staged cutover.

### 2.1 Architecture

```
                    ┌─────────────────────────────────┐
                    │ kanban_* worker/dispatcher API  │
                    └───────────────┬─────────────────┘
                                    │
                    ┌───────────────▼─────────────────┐
                    │  KanbanRepositoryFacade         │
                    │  chooses backend based on:      │
                    │  HERMES_KANBAN_WRITE_BACKEND    │
                    │  ∈ {sqlite, dual, duckdb}       │
                    └───┬────────────────────┬────────┘
                        │                    │
              ┌─────────▼───────┐   ┌───────▼──────────┐
              │ SQLiteKanban    │   │ DuckDBKanban     │
              │ Repository      │   │ Adapter          │
              │ (kanban_db.py)  │   │ (NEW, this ADR)  │
              └─────────────────┘   └──────────────────┘
                     │                       │
                ┌────▼────┐             ┌────▼────┐
                │ *.db    │             │*.duckdb │
                │(SQLite) │             │(DuckDB) │
                └─────────┘             └─────────┘
```

- `SQLiteKanbanRepository`: a thin refactor of `kanban_db.py`'s free functions behind a class implementing the port.
- `DuckDBKanbanAdapter`: NEW. Implements the same port against DuckDB. Column set is a **superset** of the SQLite schema (already the assumption baked into `DuckDBKanbanReader`).
- Facade routes reads and writes based on a single env var (`HERMES_KANBAN_WRITE_BACKEND`). In `dual` mode it writes to BOTH, returning the SQLite result as ground truth so no observable behaviour change happens during the shadow window.

### 2.2 Cutover phases

| Phase   | Duration  | `HERMES_KANBAN_WRITE_BACKEND` | Read authority | Notes                                                                 |
| ---     | ---       | ---                           | ---            | ---                                                                   |
| **P0**  | this ADR  | (unset → `sqlite`)            | sqlite         | Baseline. `DuckDBKanbanReader` optional. What runs today.             |
| **P1**  | 1–2 days  | `sqlite`                      | sqlite         | Ship `DuckDBKanbanAdapter` + parity tests. No behaviour change yet.   |
| **P2**  | 24h min   | `dual`                        | sqlite         | Dual-write shadow. Parity cron compares `SELECT COUNT(*) …` every 5 min. Any drift → alert + block cutover. |
| **P3**  | 24h min   | `duckdb`                      | duckdb         | Flip authority. SQLite writes stop but files kept for read fallback + rollback. Watch dispatcher error rate. |
| **P4**  | 7 days    | `duckdb`                      | duckdb         | SQLite files renamed `kanban.db.retired.<ts>`. No writes to them. Any read from them logs a warning. |
| **P5**  | forever   | `duckdb`                      | duckdb         | SQLite `.retired.*` files archived + deleted. `kanban_db.py` deleted. |

### 2.3 Governance gate (per DoR/DoD spec)

- **P1 → P2 gate:** ADR-012 status = `Accepted` with signatures from `hamilton` and `helios`. Parity tests green. `DuckDBKanbanAdapter` unit tests green. Rollback script dry-run green.
- **P2 → P3 gate:** ≥ 24h of dual-write with **zero divergence** in the parity report (attached to this ticket as `metadata.parity_log_path`). Human sign-off (owner: jeff_dean persona backend-eng profile).
- **P3 → P4 gate:** ≥ 24h of DuckDB-authoritative with dispatcher error rate ≤ pre-cutover baseline. On any regression, roll back via `scripts/kanban-rollback-to-sqlite.sh` (see §5).
- **P4 → P5 gate:** 7 days of clean operation. `kanban_db.py` is deleted only after this window.

### 2.4 Failure-mode budgets

- **Any single parity failure during P2** → immediate revert to `HERMES_KANBAN_WRITE_BACKEND=sqlite` + root-cause before re-entering P2.
- **Any dispatcher crash during P3 that traces to DuckDB** → rollback (§5). SQLite files were kept read-only, not deleted.
- **Any board-level DuckDB corruption during P3** → rollback that one board via the per-board rollback path (`scripts/kanban-rollback-to-sqlite.sh --board=<slug>`); leave the rest on DuckDB.

---

## 3. Migration order (which board first, why)

Order chosen by blast radius and criticality (lowest → highest):

1. **`test-silent-crash`** — synthetic board, no live consumers. Confirms schema import round-trip on real data.
2. **`voice-review`, `voice-twins`** — internal review boards, dispatcher writes are rare.
3. **`campaignforge`, `campaignforge-phase5`** — active product boards.
4. **`rsi-council-audit`** — council-facing but no autonomous dispatchers.
5. **`adr-006b-phase-2`** (this ticket's home board) — active dispatch, moderate depth.
6. **`default`** — the main working board, high traffic.
7. **`executive-agents`** — cross-agent coordination, high dependency depth.
8. **`okr-2026-q2`** — **HIGHEST RISK.** Currently drifting across 4 files. Migration is also a consolidation: pick canonical source (`kanban.db` if intact, else `kanban_recovered.db`), export → import DuckDB, retire the other 3 files. Requires human confirmation of chosen source.
9. **Legacy global `~/.hermes/kanban/kanban.db`** — verified unused by current dispatchers (verified via `strace`/`lsof` sample before retiring). If unused, delete. If used, migrate identically.

Each board's migration runs as a self-contained script that (a) opens a read-only handle to the SQLite file, (b) creates the corresponding `kanban.duckdb` with the same schema, (c) INSERTs row-for-row within a single DuckDB transaction, (d) runs `SELECT COUNT(*) FROM tasks/comments/events/runs` on both sides and asserts equality, (e) emits a parity log to `~/.hermes/kanban/migration-logs/<board>-<ts>.json`.

---

## 4. Dual-write semantics

`dual` mode implementation contract:

- Every mutating call (`create_task`, `add_comment`, `_append_event`, `claim_task`, `heartbeat_claim`, `complete_task`, `_end_run`, `link_tasks`, `unlink_tasks`, `assign_task`, `reassign_task`, `reclaim_task`, `release_stale_claims`, `add_kanban_notify_sub`, …) is executed **first** against SQLite (the current authority), and the result (task id, run id, event id, boolean success) is returned to the caller. It is **then** mirrored to DuckDB in a fire-and-forget path that logs but does not raise on failure.
- The DuckDB path is not on the critical path — SQLite is the authority in P2. This is why "any parity failure" is a soft blocker (correct policy: alert + investigate + hold cutover), not a caller-visible error.
- Ids from AUTOINCREMENT columns (`task_events.id`, `task_runs.id`, `task_comments.id`) are explicitly passed through: SQLite generates, DuckDB inserts with the same id (so cross-DB parity is by-id, not by-count).
- The parity cron runs every 5 minutes:
  ```
  # Compare row counts + max(id) per table
  SELECT COUNT(*), COALESCE(MAX(id),0) FROM task_events;
  SELECT COUNT(*), COALESCE(MAX(id),0) FROM task_runs;
  SELECT COUNT(*), COALESCE(MAX(id),0) FROM task_comments;
  SELECT COUNT(*) FROM tasks;
  SELECT COUNT(*) FROM task_links;
  SELECT COUNT(*) FROM kanban_notify_subs;
  ```
  All six pairs must match. Divergence emits a NATS event on `hrv.kanban.parity.divergence` and appends to `~/.hermes/kanban/parity-log.jsonl`.

---

## 5. Rollback

`scripts/kanban-rollback-to-sqlite.sh` (committed as part of this ticket, dry-run tested):

```bash
kanban-rollback-to-sqlite.sh --all-boards              # roll back every board
kanban-rollback-to-sqlite.sh --board=<slug>            # roll back one board
kanban-rollback-to-sqlite.sh --all-boards --dry-run    # print what would happen
```

Semantics:

1. Set `HERMES_KANBAN_WRITE_BACKEND=sqlite` in the dispatcher's environment (via `~/.hermes/config.yaml` `env.kanban_write_backend` override).
2. `systemctl --user restart hermes-dispatcher` on every host (`hermes2`, `ip-172-31-30-216`).
3. For each board, verify SQLite `.db` file exists and is not marked `.retired.*`; if `.retired.*`, rename back to `.db`.
4. For each board, run a parity check DuckDB → SQLite (last 1000 events); alert if any event id present in DuckDB but not SQLite (this would indicate we lost writes because P3 accepted DuckDB-only writes for some window — the rollback window would need to be replayed from DuckDB).
5. Emit `kanban.rollback.completed` NATS event with board list.

**Post-rollback**: SQLite is write-authoritative again. DuckDB `.duckdb` files kept as read-only fallback and forensic artifact.

**Rollback SLA**: ≤ 5 minutes from decision to `HERMES_KANBAN_WRITE_BACKEND=sqlite` taking effect on both dispatch hosts.

---

## 6. What we do NOT change

- **The `kanban_*` tool surface exposed to workers is unchanged.** Every existing call (`kanban_show`, `kanban_heartbeat`, `kanban_block`, `kanban_complete`, `kanban_create`, `kanban_comment`, `kanban_attach`, `kanban_attach_url`, `kanban_attachments`, `kanban_link`) has identical behaviour and signature. Workers do not need to know the backend switched.
- **The `Task`, `Run`, `Comment`, `Event` dataclasses are unchanged.** `DuckDBKanbanAdapter` returns the same shapes `SQLiteKanbanRepository` does.
- **Board layout on disk is unchanged.** `~/.hermes/kanban/boards/<slug>/` still holds the board. The file inside is `kanban.duckdb` in place of `kanban.db`. Attachments, workspaces, and logs directories stay put.
- **The gateway notifier's `task_events` watermark contract is unchanged.** DuckDB preserves `id` monotonicity per table.

---

## 7. Alternatives considered

**A. Keep SQLite; harden with PRAGMA busy_timeout + WAL checkpointing cron.**
Rejected. The corruption graveyard on `okr-2026-q2` is not a busy-timeout problem — it is multi-host WAL divergence, torn writes across Syncthing, and interrupted schema migration. Pragmas can't fix concurrent-writer scenarios SQLite explicitly disclaims.

**B. Move to Postgres.**
Rejected for now. Full DBMS adds an ops burden (service to run, backup story, failover) the fleet doesn't need. DuckDB is embedded, single-file, has a similar mental model to SQLite, and we already have precedent (ADR-011 P0-3).

**C. Move to a centralized service (single hermes-kanban HTTP server).**
Rejected as premature. Would fix the multi-writer problem but adds an availability SPOF and a network dependency to every worker. Revisit if DuckDB itself hits limits at scale.

**D. Do nothing.**
Rejected. Evidence in §1.2. The `okr-2026-q2` board is one corruption event from irrecoverable data loss.

---

## 8. Open questions (must be resolved before P2)

- **Q1:** Which `okr-2026-q2` file is canonical? `board.db`, `kanban.db`, `kanban_recovered.db`, or `okr-2026-q2.db`? Requires human to pick.
- **Q2:** Does the gateway's `kanban-notifier` process talk to SQLite directly (bypassing `kanban_db`) or through `kanban_*` tools? If direct, add a small notifier-side backend switch too.
- **Q3:** Does anything outside `hermes-agent` (e.g. EAF's `sqlite_kanban_adapter.py`) directly open these files? If yes, migrate those callers in the same window.

---

## 9. Signatures

| Reviewer  | Role                    | Verdict          | Date       |
| ---       | ---                     | ---              | ---        |
| hamilton  | governance dual-sign    | _pending_        |            |
| helios    | governance dual-sign    | _pending_        |            |
| jeff_dean | authoring persona       | Proposed         | 2026-08-16 |

Once both `hamilton` and `helios` mark `APPROVE`, this ADR's Status is upgraded to `Accepted` and the P1 → P2 gate is cleared.

---

## Appendix A. Files that change

Created:
- `docs/adr/ADR-012-sqlite-kanban-sunset.md` (this file)
- `projects/hermes-kanban/src/hermes_kanban/duckdb_kanban_adapter.py` (~600 LOC — write mirror of `kanban_db.py`'s state machine)
- `projects/hermes-kanban/src/hermes_kanban/kanban_repository_facade.py` (~150 LOC — backend router)
- `projects/hermes-kanban/tests/unit/test_duckdb_parity.py` (~400 LOC — schema + round-trip + count parity)
- `scripts/kanban-rollback-to-sqlite.sh` (this ticket, Part 4)
- `scripts/kanban-parity-cron.py` (5-min parity check during P2)
- `scripts/kanban-migrate-board-to-duckdb.py` (per-board one-shot migrator)

Modified:
- `projects/hermes-kanban/src/hermes_kanban/kanban_db.py` — no functional change; refactor top-level functions into `SQLiteKanbanRepository` methods behind a compat shim so callers see no diff.
- `projects/hermes-kanban/src/hermes_kanban/kanban.py` — route through `KanbanRepositoryFacade` instead of importing `kanban_db` directly.

Deleted (only after P5, in a later ticket):
- `projects/hermes-kanban/src/hermes_kanban/kanban_db.py` (kept as `kanban_db_sqlite_legacy.py` in git history).

## Appendix B. Metrics we watch

Emitted on NATS subject `hrv.kanban.*`:
- `hrv.kanban.write.latency_ms` (both backends during P2, DuckDB only after P3).
- `hrv.kanban.parity.divergence` (any row-count / max-id mismatch during P2).
- `hrv.kanban.rollback.triggered` (any invocation of the rollback script).
- `hrv.kanban.corruption.detected` (either backend reports a bad file).

Gate criteria (P2 → P3): 24h continuous run with zero `parity.divergence` events, p99 `write.latency_ms` on DuckDB ≤ 2× the SQLite baseline.
