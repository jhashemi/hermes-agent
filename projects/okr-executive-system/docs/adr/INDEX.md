# Architecture Decision Records (ADR) Index

**Purpose**: Track all significant architectural decisions with context, alternatives, and consequences.

**Format**: [ADR-NNN-title.md](./template.md)

**Status Legend**:
- 🟢 **Accepted**: Decision approved, implementation underway/complete
- 🟡 **Proposed**: Decision proposed, awaiting approval
- 🟠 **Deprecated**: Decision superseded by newer ADR
- 🔴 **Rejected**: Decision rejected, not implemented

---

## Kanban System ADRs

### 🟡 ADR-K-001: DuckDB Migration for Kanban Boards

**File**: `design/kanban-duckdb-raft/001-adr-duckdb-migration.md`  
**Date**: 2026-05-21  
**Status**: Proposed  
**Authors**: Jeff Dean, Werner Vogels

**Summary**: Replace per-board SQLite databases with unified DuckDB at `~/.hermes/kanban/kanban.duckdb`

**Key Decisions**:
- Single DuckDB file with board partitioning (not per-board tables)
- Columnar storage for analytics queries
- KanbanConnection adapter for API compatibility
- SQLite fallback for constrained environments

**Related OKR Decisions**: ADR-O-001 (DuckDB for OKR metrics)

---

## OKR System ADRs

### 🟢 ADR-O-001: Use DuckDB for Primary OKR Data Store

**File**: `adr/ADR-001-duckdb-primary-datastore.md`  
**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent

**Summary**: Use DuckDB as primary data store for OKR system (OKRs, goals, tasks, metrics)

**Key Decisions**:
- DuckDB for all OKR persistence (not SQLite)
- Columnar storage optimized for KPI analytics
- Schema design with tables: okrs, goals, metrics, tasks
- Repository pattern for adapter flexibility

**Rationale**: Already deployed, perfect for analytics queries, ACID compliant

**Consequences**:
- Fast metric aggregations
- SQL interface enables BI tools
- Can scale to PostgreSQL without SQL changes

**Related Decisions**: 
- ✅ ADR-K-001 (Kanban also using DuckDB)
- ↔️ ADR-O-002 (NATS events - complementary)

---

### 🟢 ADR-O-002: Use NATS JetStream for Event Broker

**File**: `adr/ADR-002-nats-jetstream-events.md`  
**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent

**Summary**: Use NATS JetStream (via VCGGateway) for event publishing/subscribing

**Key Decisions**:
- Events published to NATS topics
- JetStream provides persistence (audit trail)
- Durable consumers for replay capability
- VCGGateway integration for existing infrastructure

**Rationale**: Already integrated, no additional infrastructure, proven in production

**Consequences**:
- All state changes become events
- Full audit trail available
- Components decouple via events
- Multi-machine deployment ready

**Related Decisions**:
- ↔️ ADR-O-001 (DuckDB stores event snapshots)
- ↔️ ADR-O-003 (Hexagonal architecture enables this)

---

### 🟢 ADR-O-003: Use Hexagonal (Ports & Adapters) Architecture

**File**: `adr/ADR-003-hexagonal-architecture.md`  
**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent

**Summary**: Use hexagonal architecture to separate domain logic from infrastructure

**Key Decisions**:
- All external systems are adapters (GitHub, Kanban, NATS, DuckDB)
- All engines implement ExecutionEngine interface (port)
- Domain logic never depends on infrastructure
- Easy to test with mock adapters

**Rationale**: SOLID principles, testability, maintainability, extensibility

**Consequences**:
- More interfaces than typical code
- Stronger behavior guarantees
- Easier to refactor safely
- Easier to add new adapters

**Related Decisions**:
- ↔️ ADR-O-001, ADR-O-002 (implemented as adapters)
- → ADR-O-004 (SOLID principles enable this)

---

### 🟢 ADR-O-004: Apply SOLID Principles to All Components

**File**: `adr/ADR-004-solid-principles.md`  
**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent

**Summary**: Enforce SOLID principles in all components

**Key Decisions**:
- **S**ingle Responsibility: Each engine has one job
- **O**pen/Closed: Engines extensible, closed for modification
- **L**iskov Substitution: All engines implement common interface
- **I**nterface Segregation: Engines expose minimal interface
- **D**ependency Inversion: All depend on abstractions

**Rationale**: Proven to improve maintainability, testability, extensibility

**Consequences**:
- More test setup required
- Stronger guarantees about code behavior
- Harder to introduce bugs
- Easier to understand intent

**Related Decisions**:
- ← ADR-O-003 (Hexagonal architecture)
- ← ADR-O-005 (VCG allocation follows principles)

---

### 🟢 ADR-O-005: Use VCG (Vickrey-Clarke-Groves) for Task Allocation

**File**: `adr/ADR-005-vcg-task-allocation.md`  
**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent

**Summary**: Use VCG algorithm for optimal task allocation to agents

**Key Decisions**:
- VCG mechanism for game-theoretically optimal allocation
- No incentive for agents to game the system
- Handles multi-skill requirements
- Includes adaptive weighting for learning

**Rationale**: Mathematically proven optimal, truthful bidding is dominant strategy, already implemented and tested

**Consequences**:
- Best resource utilization
- Tasks go to best agents
- More complex than simple algorithms
- Requires skill matching logic

**Related Decisions**:
- ✅ Already implemented in `gateway/vcg_dispatcher.py`
- ↔️ ADR-O-002 (VCG allocations published as events)

---

## Decision Relationships

```
ADR-O-001 (DuckDB Data)
    ↔ ADR-K-001 (Kanban->DuckDB)
    ↔ ADR-O-002 (NATS Events)

ADR-O-002 (NATS Events)
    ↔ ADR-O-003 (Hexagonal - enables decoupling)
    ↔ ADR-O-005 (VCG publishes events)

ADR-O-003 (Hexagonal)
    ← ADR-O-004 (SOLID enables this)
    ← ADR-O-001, ADR-O-002, ADR-O-005 (implemented via adapters)

ADR-O-004 (SOLID)
    → ADR-O-003, ADR-O-005 (follow SOLID)

ADR-O-005 (VCG Allocation)
    ✅ Already in: gateway/vcg_dispatcher.py
    ↔ ADR-O-002 (publishes events)
    ↔ ADR-O-004 (follows SOLID)
```

---

## Implementation Status

| ADR | Component | Status | Location |
|-----|-----------|--------|----------|
| ADR-O-001 | DuckDBRepository | ✅ Designed | `adr/ADR-001-*.md` |
| ADR-O-002 | NATSJetStreamBroker | ✅ Designed | `adr/ADR-002-*.md` |
| ADR-O-003 | Hexagonal Architecture | ✅ Designed | `adr/ADR-003-*.md` |
| ADR-O-004 | SOLID Principles | ✅ Designed | `adr/ADR-004-*.md` |
| ADR-O-005 | VCG Allocation | ✅ Existing | `gateway/vcg_dispatcher.py` |

---

## How to Add New ADRs

1. Create file: `adr/ADR-NNN-title.md`
2. Use template from `adr/template.md`
3. Add entry to this index
4. Link related ADRs (both directions)
5. Update decision relationships diagram

---

## References

- [ADR Format](https://adr.github.io/) - Standard format for architecture decisions
- [Existing ADRs](./adr/) - All ADR files
- [Kanban ADR](./design/kanban-duckdb-raft/001-adr-duckdb-migration.md) - Kanban->DuckDB decision

---

## Decision Authority

**OKR System Decisions** (ADR-O-*):
- Led by: Hermes Agent
- Approved by: (pending)
- Implementation: Phase 1+ of OKR system

**Kanban System Decisions** (ADR-K-*):
- Led by: Jeff Dean, Werner Vogels
- Approved by: (pending)
- Implementation: DuckDB migration project

