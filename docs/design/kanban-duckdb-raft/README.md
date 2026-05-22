# Kanban DuckDB + RAFT Design Package

**Authors**: Jeff Dean (infrastructure), Werner Vogels (distributed systems)
**Date**: 2026-05-21
**Status**: Proposed

## Background

24h outage from 5 independent schema-migration failures across hermes1 (100.107.83.25) and hermes2 (100.79.15.66). Per-board SQLite DBs create schema drift; no coordination between machines.

## Deliverables

1. **[ADR-001: DuckDB Migration](001-adr-duckdb-migration.md)** — Architecture decision record for replacing per-board SQLite with unified DuckDB. Covers schema design (board column partition vs per-board tables), `KanbanConnection` adapter, SQLite fallback, write transaction adaptation, and PRAGMA migration.

2. **[RAFT Design](002-raft-design.md)** — 2-node + arbiter RAFT cluster for multi-machine kanban consistency. Covers leader election via Tailscale, gateway-arbiter tiebreaker, read-only fallback under partition, RAFT RPC protocol, log persistence, and safety analysis.

3. **[Schema Consistency Test Design](003-schema-consistency-test-design.md)** — Static linter (`lint_schema_sql`) catching index/column mismatches, migration consistency verifier, runtime schema verification pytest suite, and developer checklist for column additions.

4. **[Migration Plan](004-migration-plan.md)** — Zero-downtime 4-phase migration: pre-checks → dual-write shadow mode → data import → cutover. Includes rollback plan, cross-machine sequencing, and archive strategy.

5. **[Code Path Impact Analysis](005-code-path-impact-analysis.md)** — All 32 write_txn call sites, connection/path functions, board discovery, consumer modules, test files. Categorized by complexity (low/medium/high/critical) with estimated effort and dependency graph.

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Storage engine | DuckDB (columnar, single-file) | Analytics, compression, single migration path |
| Board partitioning | `board` column (not per-board tables) | Cross-board queries, single schema, DuckDB column scan |
| RAFT cluster size | 2 data nodes + 1 arbiter (3 voters) | Quorum=2 tolerates 1 failure; arbiter is stateless |
| Arbiter placement | Embedded in gateway process | No separate deployment; always available on both machines |
| RAFT transport | HTTP over Tailscale | Encrypted, debuggable, firewall-friendly |
| Write path | RAFT-gated write_txn | ~10ms latency; all 32 write sites covered uniformly |
| Read path | Local DuckDB, no RAFT | Always available; followers may be stale under partition |
| SQLite fallback | Preserved via engine detection | For DuckDB-unavailable environments |
| Migration strategy | Shadow mode → import → cutover | Zero downtime; 48hr shadow validation |