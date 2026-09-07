# Storage Durability + DI Audit — 2026-06-06

**Auditor:** hermes_agent (Telegram session, J Hash directive)
**Scope:** EAF + hermes-agent harness + AGT integration surface
**User policy:** **DuckDB > SQLite > Postgres** for persistence, always. SQLite is a legacy or replication-only backend; Postgres is forbidden. In-memory adapters are TEST-ONLY.

---

## TL;DR

| Layer | Status | Severity |
|---|---|---|
| EAF kanban canonical writer (`DuckDBKanbanRepository` → `data/kanban.duckdb`) | ✅ DuckDB | OK |
| EAF OKR canonical writer (`OKRAccountabilitySystem` → `data/okr_accountability.db`) | ✅ DuckDB (file header confirms `4455434b`) | OK |
| EAF DI container (`composition/container.py`) wires `_get_default_storage()` = `DuckDBOKRStorage` singleton | ✅ DuckDB | OK |
| EAF 15 DuckDB adapters in `infrastructure/adapters/` | ✅ | OK |
| **Hermes-agent harness kanban (`hermes_cli/kanban_db.py`) — SQLite-backed, used by live dispatcher** | ⚠️ SQLite | **P1** |
| **`SQLiteKanbanAdapter` (one-way DuckDB→SQLite replicator) — must DOCUMENT, not delete** | ⚠️ replicator-only | **P3** |
| **`okr_atomic_creation.InMemoryStorage` — DEFAULT for `AtomicOKRCreationTransaction` constructor** | ⚠️ in-memory | **P1** |
| **`cluster/factory.py` — defaults to `InMemoryHeartbeatAdapter` when `heartbeat=None`** | ⚠️ in-memory | **P0** |
| **6 SQLite production adapters in EAF** (`okr_stimulus_hook`, `bridge_strict_watchdog`, `content_addressed_store`, `crdt_ref_store`, `idempotency_ledger`, `sqlite_kanban_adapter`) | ⚠️ SQLite | **P2** |
| **3 orphan SQLite DBs in `~/.hermes/` (no findable writer)** | ⚠️ SQLite | **P2** |
| **`unified_integration.InMemoryEventBus` — settable via `use_in_memory_bus=True` flag** | ✅ flag-gated, default False | OK (audit trail) |
| `gateway/api_server.py:ResponseStore` — SQLite ephemeral response cache | ⚠️ SQLite + `:memory:` fallback | **P3** |

7 distinct durability or distributedness violations. None are fatal — the EAF canonical SSOT is correctly DuckDB. But the hermes-agent dispatcher and several handler-side adapters are SQLite, which is the bridge between the harness and the EAF and would lose its single-writer/replication story under cluster scale-out.

---

## Findings

### F1 (P0) — `cluster/factory.py` defaults to in-memory heartbeat in production

```python
# /home/ubuntu/executive_agents_framework/src/executive_agents/cluster/factory.py:24
def build_phase_a_service(
    heartbeat: Optional[HeartbeatPort] = None,
    ...
):
    if heartbeat is None:
        heartbeat = InMemoryHeartbeatAdapter()      # ← prod fall-through
```

**Problem:** in a 4-node NATS cluster, heartbeat must be durable + cross-machine. In-memory means each machine has its own heartbeat ring; cross-machine liveness queries return stale or missing data. Per the user's "actor-stream" architecture, heartbeat should be a NATS subject (`heartbeat.<node>.tick` durable consumer with TTL).

**Fix:** create `NATSHeartbeatAdapter` reading/writing `heartbeat.>` subjects with JetStream, default factory to it. Keep `InMemoryHeartbeatAdapter` for unit tests only.

---

### F2 (P1) — `okr_atomic_creation.AtomicOKRCreationTransaction` default storage is InMemory

```python
# /home/ubuntu/executive_agents_framework/src/executive_agents/orchestration/okr_atomic_creation.py:303,493,515
class InMemoryStorage:
    ...

class AtomicOKRCreationTransaction:
    def __init__(
        self,
        ...
        storage_backend: Optional[InMemoryStorage] = None,
        ...
    ):
        ...
        self.storage: InMemoryStorage = storage_backend or InMemoryStorage()
```

**Problem:** type annotation says `InMemoryStorage` but production wiring passes `DuckDBOKRStorage`. The fall-through default and the type hint are TEST-shaped. The DI container *does* override (line 939: `_get_default_storage()` → `DuckDBOKRStorage` singleton), so the live executive agents are DuckDB-correct. But any caller that constructs `AtomicOKRCreationTransaction` directly (e.g. ad-hoc scripts, tests promoted to production, kanban workers) gets in-memory.

**Fix:** widen the type hint to `Optional[OKRStoragePort]` (Protocol), and have the constructor itself fall through to `_get_default_storage()` — not `InMemoryStorage()`. This makes the safe path the default path.

---

### F3 (P1) — Hermes-agent harness kanban is SQLite-backed

```python
# /home/ubuntu/hermes-agent/hermes_cli/kanban_db.py
"""SQLite-backed Kanban board for multi-profile, multi-project collaboration..."""
import sqlite3
```

`/home/ubuntu/.hermes/kanban.db` (9.6 MB) is SQLite; **EAF's `data/kanban.duckdb` is DuckDB**. Two boards. Two backends. The EAF `SQLiteKanbanAdapter` exists precisely as a one-way DuckDB → SQLite replicator so the legacy harness dispatcher sees agent assignments. This is DOCUMENTED in `docs/ADR/ADR-001-DUCKDB-SSOT-KANBAN-ARCHITECTURE.md`, but it's a **bridge, not an endpoint** — long-term the harness must speak DuckDB directly.

**Decision branch:**
- **Short-term (Phase 0.5):** confirm `SQLiteKanbanAdapter` replication is healthy + monotonic. Surface lag metric via vein emitter.
- **Medium-term (Phase 1):** add `DuckDBKanbanReader` to hermes-agent harness so dispatcher can read DuckDB directly. Keep SQLite write path for legacy CLI compat.
- **Long-term (Phase 2):** sunset SQLite kanban entirely. ADR-012 candidate.

---

### F4 (P2) — Six SQLite-using files in EAF production code

| File | Role | Recommendation |
|---|---|---|
| `infrastructure/adapters/okr_stimulus_hook.py:287` | reads `ACTION_TRACKER_DB` (SQLite) | Migrate to DuckDB or document why action tracker is intentionally SQLite |
| `infrastructure/adapters/bridge_strict_watchdog.py:117,151,164` | bridge enforcement state | Migrate to DuckDB |
| `infrastructure/storage/content_addressed_store.py:80` | CAS blob store | Migrate to DuckDB or rename folder; conflicts with `cas_duckdb_index.py` sibling |
| `infrastructure/storage/crdt_ref_store.py:80` | CRDT ref store | Migrate to DuckDB |
| `deliberation/idempotency_ledger.py:93` | GH comment idempotency ledger | Migrate to DuckDB |
| `infrastructure/adapters/sqlite_kanban_adapter.py` | one-way replicator | KEEP — its job is to be SQLite; document explicitly |
| `infrastructure/systems/okr_accountability.py:38,513` | imports sqlite3 for legacy kanban-path branch only | Remove SQLite branch; require DuckDB kanban |

---

### F5 (P2) — Three orphan SQLite DBs in `~/.hermes/`

```
~/.hermes/distributed_task_ledger.db        SQLite, 20 KB, 3 rows in distributed_tasks
~/.hermes/kanban/executive-agents/kanban.db SQLite, 119 KB
~/.hermes/cron/state/okr_raci_watchdog.state.db SQLite, 45 KB
```

`grep` for the writer of `distributed_task_ledger.db` returned no hits across hermes-agent, EAF, ~/.hermes/scripts. **The writer is not in any tree we audit.** Either it's been removed (orphan data file) or it lives somewhere we haven't surfaced. The other two have likely writers in `~/.hermes/scripts/` cron jobs but the search timed out before completing.

**Action:** investigate ownership; if dead, archive + delete; if live, migrate to DuckDB per F4 pattern.

---

### F6 (P3) — Gateway response store SQLite + `:memory:` fallback

```python
# /home/ubuntu/hermes-agent/gateway/platforms/api_server.py:312
class ResponseStore:
    def __init__(self, max_size, db_path=None):
        if db_path:
            self._conn = sqlite3.connect(db_path, ...)
        else:
            self._conn = sqlite3.connect(":memory:", ...)   # ← runtime gateway state
```

This is ephemeral REST API response cache (not a primary SSOT). `:memory:` fallback is acceptable for stateless gateway replicas, but explicit DuckDB for the persistent path would unify the persistence story. Low priority — gateway response cache is rebuildable.

---

## DI Container Health (positive)

`composition/container.py` is an **exemplar** of correct DI:
- Line 6: comment "ALL profiles use REAL adapters — zero test doubles. Tests use `:memory:` DuckDB."
- Line 909: `kanban_repo = DuckDBKanbanRepository()` (default kanban SSOT path)
- Line 939: `okr_storage = _get_default_storage()` → DuckDBOKRStorage singleton
- 91 adapter modules wired through dataclass containers (`StorageContainer`, `CognitiveContainer`, `Phase13Container`, `NervousSystemContainer`, `ServicesContainer`, `SystemsContainer`, `AgentContainer`)
- `composition/container_v2.py` is the in-progress refactor consolidating wiring

The container correctly enforces hexagonal: adapters instantiated **only** at composition root, never by domain code. The user's "container = resolver" stance is honored.

The defects are NOT in the container — they are in the **factory functions** and **constructor defaults** that bypass the container (F1, F2).

---

## Recommended Remediation (kanban tasks dispatched alongside this audit)

| Task | Title | KR linkage | Priority |
|---|---|---|---|
| AUDIT-1 | Replace `InMemoryHeartbeatAdapter` default with `NATSHeartbeatAdapter` | New: distributed-heartbeat | P0 |
| AUDIT-2 | Widen `AtomicOKRCreationTransaction.storage_backend` typing + fall-through | Hardens P0-3 (connection discipline) | P1 |
| AUDIT-3 | Migrate 5 SQLite-using EAF adapters to DuckDB | Hardens P0-4 (audit ledger) | P2 |
| AUDIT-4 | Investigate + classify 3 orphan SQLite DBs | Cleanup | P2 |
| AUDIT-5 | Document `SQLiteKanbanAdapter` as replication-only with sunset plan (ADR-012 candidate) | Architecture | P3 |
| AUDIT-6 | Add `DuckDBKanbanReader` to hermes-agent harness so dispatcher reads DuckDB directly | Phase 1 prep | P1 |

These slot under the **parent rollout OKR `fd95a387`**, NOT the Phase 0 OKR — Phase 0 is closed for scope; the audit findings inform Phase 0.5 and beyond.

---

## What this means for the live Phase 0 dispatch

Phase 0 KRs P0-1 through P0-8 are NOT blocked by these findings. The DuckDB CHECK constraint (P0-1), audit ledger (P0-4), and Prom+VM stack (P0-6) all land on DuckDB-correct surfaces. The InMemory and SQLite findings are **adjacent technical debt** that should be remediated in parallel, not fix-the-dispatch-first.

The one piece of Phase 0 that needs an addendum is **P0-3 (connection discipline)** — the same context-managed connection pattern should apply to all DuckDB-backed adapters, not just `okr_accountability.py`. AUDIT-2 is the explicit codification of that.
