# PRD: ADR-011 Phase 0 — L0 Substrate Hardening + AGT Smoke Test

**Document:** PRD for child OKR `adr_011_phase_0_l0_substrate` (id `64a7371a`)
**Parent OKR:** `adr_011_full_rollout_q3` (id `fd95a387`)
**Owner (Accountable):** margaret_hamilton
**Implementer:** jeff_dean
**Due:** 2026-06-13 (7 days)
**Status:** Authored 2026-06-06 post-council ratification (3-of-3 APPROVE on ADR-011 r2 + post-nits)
**Related ADR:** [ADR-011](../adr/ADR-011-hrv-otel-substrate-and-policy-catalog.md)
**Lifecycle phase:** Goal+Plan+Deliberation+Consensus complete → Implementation now authorized.

---

## 1. Problem Statement

The OKR fabrication post-mortem (2026-06-05/06) identified three structural bypass paths through which `okr_kr_reconciler.py` and `okr_remediate_audit_findings.py --apply` were able to write `status='done'` rows without evidence, fabricating ~72 KR completions. The proposed cure (ADR-011's HRV-OTEL substrate + AGT-based PolicyEngine + L4 catalog) is large. Phase 0 is the **narrow, urgent** subset that closes the bypass paths AT THE DATA LAYER and stands up the supporting substrate, in 7 days, before any further architecture is committed.

Phase 0 is intentionally NOT the full ADR rollout. It is the BLOCKING precondition for any subsequent phase.

The existing OKR DB lock-hold defect was also observed live during this PRD's authoring (PID 1674173 held `okr_accountability.db` for 18h33m; lock conflict resolved by surgical restart). The Phase 0 fix (KR P0-3) prevents recurrence.

## 2. Users and Stakeholders

| Role | Responsibility | Notes |
|---|---|---|
| margaret_hamilton (Accountable) | Final governance sign-off on each KR | Owns the bypass-path closure contract |
| jeff_dean (Responsible) | Implementation lead | Executes all 8 KRs |
| werner_vogels (Consulted) | Substrate scalability review | Reviews Prom+VM stack (KR P0-6), AGT smoke (P0-8) |
| demis_hassabis (Consulted) | Vein-emitter cognitive integration | Reviews KR P0-7 |
| donald_knuth (Consulted) | Formal correctness | Reviews KR P0-2 (lint), KR P0-4 (Merkle hash chain) |
| hermes_agent (Informed) | System self-update | Receives every KR transition event |

## 3. Goals (8 KRs, all RED tests)

| KR | Title | Tier | RED-test gate |
|---|---|---|---|
| P0-1-DBTRIGGER | DuckDB CHECK constraint on okr_key_results | HC4 | `chk_done_requires_evidence` rejects bare-UPDATE |
| P0-2-LINTGATE | AST lint gate on direct OKR DB writes | HC3 | Synthetic offending file → exit non-zero |
| P0-3-CONNCONTEXT | Context-managed connection per write batch | HC4 | 10 concurrent writers, no >1s hold |
| P0-4-AUDITLEDGER | Audit ledger DuckDB at ~/.hermes/audit/ledger.db | HC4 | 1000-row Merkle chain integrity verified |
| P0-5-LLDAPRESTORE | LLDAP active on hermes2 (or migrated) | HC4 | All profiles resolve role in ≤200ms p99 |
| P0-6-PROMVMSTACK | Prom replica + VictoriaMetrics remote_write | HC3 | Kill hermes2:9090, queries still work via VM |
| P0-7-VEINEMITTERSPIKE | Vein emitter in consensus-reactor | HC4 | Health envelopes + Prom /metrics on 9102 |
| P0-8-AGTSMOKETEST | AGT Python imports + tests pass | HC3 | agent-os, agent-sre, agent-mesh import + ≥80% tests pass |

## 4. Non-Goals (deliberately deferred to parent OKR)

- AGT agent-os deeply wired as Hermes PolicyEngine (KR R-2)
- AgentMesh enforcement on every NATS pub/sub (KR R-3)
- jl4-service Docker live (KR R-5)
- Differentiable RSI optimizer (KR R-6)
- 96-file policy catalog completion (KR R-7)
- Final council sign-off (KR R-9)

These are explicit Q3-horizon items in the parent rollout OKR. Phase 0 must NOT scope-creep into them.

## 5. Implementation Order (dependency-respecting)

Day 1: P0-3 (connection discipline) — fixes today's manifestation; clears lock contention for all other work.
Day 1-2: P0-1 (CHECK constraint) — depends on no long-held connections (P0-3); applies via `ALTER TABLE` once.
Day 2: P0-2 (lint gate) — independent.
Day 2-3: P0-4 (audit ledger) — depends on AgentOS-user systemd context decision but not on other KRs.
Day 3: P0-5 (LLDAP) — independent infra task; can parallel with P0-4.
Day 3-4: P0-6 (Prom+VM) — independent.
Day 4-5: P0-7 (vein emitter spike in consensus-reactor) — depends on P0-6 for scrape target.
Day 5-6: P0-8 (AGT smoke test) — independent of all others; uses already-cloned `/home/ubuntu/agt/`.
Day 7: integration RED tests + KR transition events.

## 6. Acceptance Criteria

For EACH KR:
1. RED test exists at `scripts/red_tests/<test_name>.py`.
2. RED test passes in CI.
3. Evidence URI populated as `file:///<absolute path to test log>`.
4. Audit ledger row written with `policy_eval_result='allow'`, `chain_this_hash` chain-verified.
5. KR transitions to `status='done'` ONLY via `OKRAccountabilitySystem.update_key_result()` (canonical write path), which now enforces P0-1 CHECK + P0-2 lint + P0-3 connection discipline.

## 7. Risks & Mitigations

- **R1: P0-3 refactor breaks existing readers.** Mitigation: backward-compat shim — old `self._con` attribute proxies to a per-call read-only connection. Deprecation comment, not removal, until parent OKR.
- **R2: LLDAP migration to hermes1 increases governance latency.** Mitigation: measure p99 in KR P0-5 RED test; if >200ms, restart in-place on hermes2 instead.
- **R3: AGT package install conflicts with EAF venv.** Mitigation: dry-run install in throwaway venv first; pin SHA at install time; no version bumps without explicit ADR.
- **R4: Vein emitter increases consensus-reactor load.** Mitigation: Prom registry + 30s NATS publish is ~0.001 CPU-sec per cycle; measure during KR P0-7 RED test.

## 8. Out-of-band Communication

- Daily standup append to `docs/proofs/2026-Q2-adr-011-phase-0/daily.md`.
- Each KR completion → Telegram DM to J Hash with KR-id + evidence URI.
- Any blocker → kanban_block + audit-ledger row.

## 9. Out-of-scope clarifications

- This PRD does NOT specify Phase 0.5 (AGT integration KRs) — those are R-2/R-3/R-4 in the parent OKR's PRD.
- This PRD does NOT touch the existing 1,225 NULL + 348 placeholder kanban tasks — those are independent triage work.

## 10. Sign-off log (audit trail)

- 2026-06-06: ADR-011 r2 + C.1–C.8 ratified 3-of-3 (Vogels, Hassabis, Hamilton)
- 2026-06-06: OKRs `64a7371a` + `fd95a387` created via canonical lifecycle
- 2026-06-06: This PRD authored
- TBD: Per-KR completion sign-offs (audit ledger)
