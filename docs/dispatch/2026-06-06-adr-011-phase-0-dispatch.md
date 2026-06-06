# ADR-011 Phase 0 Dispatch Log

**Dispatched:** 2026-06-06 ~15:08 UTC
**Dispatched by:** hermes_agent (Telegram session, J Hash)
**Target board:** `~/.hermes/kanban/boards/adr-006b-phase-2/kanban.db` (where kanban_create resolved — to be confirmed/migrated if dispatcher convention differs)
**Parent OKR:** `adr_011_phase_0_l0_substrate` (id `64a7371a`) — child of `adr_011_full_rollout_q3` (id `fd95a387`)
**Council ratification:** ADR-011 r2 + C.1–C.8 nits — 3-of-3 APPROVE (Vogels, Hassabis, Hamilton)
**Lifecycle compliance:** Goal+Plan+Deliberation+Consensus complete BEFORE dispatch (Vogels r1, Hassabis r1, Hamilton r1+r2 + post-nits)

## Wave 1 — independent (6 tasks, parallel-launchable)

| Kanban id | KR | Title | Assignee | Status @ T+30s |
|---|---|---|---|---|
| `t_9dfc38d6` | P0-3-CONNCONTEXT | Refactor OKRAccountabilitySystem to context-managed connections | jeff_dean | running |
| `t_0113ea70` | P0-2-LINTGATE | AST lint gate on direct OKR DB writes | jeff_dean | running |
| `t_6d774685` | P0-4-AUDITLEDGER | Audit ledger DuckDB with Merkle chain | jeff_dean | running |
| `t_2dfe2605` | P0-5-LLDAPRESTORE | LLDAP active and reachable | werner_vogels | running |
| `t_edcfcfc3` | P0-6-PROMVMSTACK | Prometheus replica + VictoriaMetrics remote_write | jeff_dean | running |
| `t_85db528d` | P0-8-AGTSMOKETEST | AGT smoke test (agent-os, agent-sre, agent-mesh) | jeff_dean | running |

## Wave 2 — dependent (2 tasks, gated)

| Kanban id | KR | Title | Assignee | Status | Depends on |
|---|---|---|---|---|---|
| `t_e3d39ee9` | P0-1-DBTRIGGER | DuckDB CHECK constraint on okr_key_results | margaret_hamilton | todo | `t_9dfc38d6` |
| `t_9df96686` | P0-7-VEINEMITTERSPIKE | Vein emitter integrated into consensus-reactor | demis_hassabis | todo | `t_edcfcfc3` |

## Why these dependency edges
- **P0-1 → after P0-3**: ALTER TABLE in DuckDB requires exclusive lock. Long-lived connections (today's 18h33m steering-reactor lock) prevent it. P0-3 fixes the connection pattern; P0-1's migration runs cleanly afterward.
- **P0-7 → after P0-6**: Vein emitter publishes Prom metrics on `:9102`. Without Prom replica + VictoriaMetrics scraping, the metrics go nowhere — RED test asserts VM has the data.

## RACI alignment with KR ownership

| KR | Accountable in OKR | Kanban assignee | Match |
|---|---|---|---|
| P0-1 | margaret_hamilton | margaret_hamilton | ✅ |
| P0-2 | margaret_hamilton (responsible: jeff_dean) | jeff_dean | ✅ (responsible) |
| P0-3 | jeff_dean | jeff_dean | ✅ |
| P0-4 | margaret_hamilton (responsible: jeff_dean) | jeff_dean | ✅ (responsible) |
| P0-5 | werner_vogels | werner_vogels | ✅ |
| P0-6 | werner_vogels (responsible: jeff_dean) | jeff_dean | ✅ (responsible) |
| P0-7 | demis_hassabis | demis_hassabis | ✅ |
| P0-8 | werner_vogels (responsible: jeff_dean) | jeff_dean | ✅ (responsible) |

## Skills pinned per task domain

| Task | Skills |
|---|---|
| P0-1 | test-driven-development, production-distributed-systems |
| P0-2 | test-driven-development, serena-lsp |
| P0-3 | hexagonal-ssot-architecture, test-driven-development, pre-implementation-codebase-audit |
| P0-4 | hexagonal-ssot-architecture, test-driven-development, systemd-python-services |
| P0-5 | lldap-enterprise-directory, lldap-graphql-write-pitfalls |
| P0-6 | production-distributed-systems, systemd-python-services, test-driven-development |
| P0-7 | test-driven-development, serena-lsp, production-distributed-systems |
| P0-8 | test-driven-development, pre-implementation-codebase-audit |

## Hand-off note for workers
Each task body contains:
- KR id (for `update_key_result` after completion)
- Acceptance evidence shape (RED test path, what it must assert)
- Pre-implementation audit checklist (serena-lsp first, find_referencing_symbols before patching)
- Pitfalls section (sourced from r1/r2 council reviews)
- Hand-off instructions (`kanban_complete` summary + `metadata.changed_files`)

Workers MUST NOT mark a KR done by direct DB write. Canonical path only:
`OKRAccountabilitySystem.update_key_result(kr_id=<id>, current_value=1.0, evidence_uri='file:///...')` — and once P0-1's CHECK constraint is in place, the schema enforces this structurally.

## Out of scope for these 8 tasks
- Phase 0.5 AGT integration (KRs R-2/R-3/R-4 in parent OKR `fd95a387`) — these get dispatched after Phase 0 completion gate.
- Triage of 12,365 NULL-assignee tasks in OKR-Q2 board — independent backfill work, requires human triage.
- HRV-aware substrate watchdogs (P7m) — depends on P0-7 + Phase 0.5.

## Verification commands

```bash
# Live status
sqlite3 ~/.hermes/kanban/boards/adr-006b-phase-2/kanban.db \
  "SELECT id, status, assignee, substr(title,1,60) FROM tasks WHERE idempotency_key LIKE 'adr-011-p0-%'"

# OKR progress
cd /home/ubuntu/executive_agents_framework
.venv/bin/python -c "
from executive_agents.infrastructure.systems.okr_accountability import OKRAccountabilitySystem
s = OKRAccountabilitySystem(db_path='data/okr_accountability.db')
con = s._con
rows = con.execute('SELECT id, title, status, current_value FROM okr_key_results WHERE objective_id = ?', ['64a7371a']).fetchall()
for r in rows: print(r)
"
```
