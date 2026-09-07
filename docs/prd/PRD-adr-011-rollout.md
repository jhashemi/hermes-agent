# PRD: ADR-011 Full Rollout — HRV-OTEL Substrate, AGT Integration, Differentiable RSI

**Document:** PRD for parent OKR `adr_011_full_rollout_q3` (id `fd95a387`)
**Child OKR:** `adr_011_phase_0_l0_substrate` (id `64a7371a`)
**Owner (Accountable):** hermes_agent (the system itself)
**Implementer:** jeff_dean (responsible) + margaret_hamilton + werner_vogels + demis_hassabis (responsible)
**Horizon:** Q3 2026 (~12 weeks from 2026-06-13 Phase 0 completion)
**Status:** Authored 2026-06-06 post-council ratification
**Related ADR:** [ADR-011](../adr/ADR-011-hrv-otel-substrate-and-policy-catalog.md)
**Lifecycle phase:** Goal+Plan+Deliberation+Consensus complete → Implementation phased.

---

## 1. Problem Statement

ADR-011 defines a 6-layer architecture (L0 data → L5 ratification) that:
1. Closes the OKR fabrication bypass paths structurally (not just procedurally).
2. Replaces the rigid "1 cron monitor per ADR" plan with a biologically-inspired observability substrate.
3. Wires Hermes EAF into Microsoft's Agent Governance Toolkit (AGT) — already cloned at `/home/ubuntu/agt/` with real Python, Rust, and Go SDK code.
4. Establishes a 96-file policy catalog (24 systems × 4 axes: governance/runtime/integration/usage) as the executable governance contract.
5. Makes the RSI cycle hyperparameters differentiable so SGD optimizes them — bounded, with isolated auditor preventing Goodhart loops.

This rollout PRD describes how the full architecture is operationalized over a Q3 horizon. Phase 0 (its own PRD) is the precondition. Phases 0.5 → 4 are this PRD.

## 2. Users and Stakeholders

| Role | Responsibility | Engagement |
|---|---|---|
| hermes_agent (Accountable) | The system being ratified | Continuous |
| jeff_dean (Responsible) | Implementation lead | Daily |
| margaret_hamilton (Responsible) | Governance enforcement contract | Per-KR sign-off |
| werner_vogels (Responsible) | Substrate scalability | Per-phase review |
| demis_hassabis (Responsible) | Differentiable RSI design | Phase 2 lead |
| donald_knuth (Consulted) | Formal correctness (jl4, SPRT) | KR-R-5, KR-R-6 |

## 3. Goals (9 KRs across 4 phases)

| KR | Phase | Title | Dependency |
|---|---|---|---|
| R-1-PHASE0DONE | Gate | Phase 0 child OKR all KRs GREEN | child OKR |
| R-2-AGTAGENTOS | 0.5 | AGT agent-os PolicyEngine wired | R-1 |
| R-3-AGTAGENTMESH | 0.5 | AGT agent-mesh policy_provider as enforcer | R-2 |
| R-4-AGTAGENTSRE | 0.5 | AGT agent-sre SLO + OTEL pipeline | R-2 |
| R-5-JL4SERVICE | 1 | jl4-service Docker; NL→L4→register flow | R-2 |
| R-6-DIFFERENTIABLERSI | 2 | rsi_optimizer.py with z-scored loss + SPRT | R-1 (audit ledger) |
| R-7-CATALOG96 | 3 | 96 L4 files authored + ratified + registered | R-2, R-3, R-5 |
| R-8-FOURREDTESTS | 4 | 4 bypass-path RED tests GREEN | R-2, R-3, R-7 |
| R-9-COUNCILFINAL | 4 | Final council 3-of-3 sign-off | R-8 |

## 4. Phase Plan

### Phase 0 (1 week, separate PRD)
8 KRs. Closes bypass paths; substrate hardening; AGT smoke test. **BLOCKING** for everything below.

### Phase 0.5 — AGT Integration (3 weeks)
Wire the three AGT Python packages already cloned at `/home/ubuntu/agt/agent-governance-python/`:

- **agent-os** (50+ existing tests around PolicyEngine, audit, adapters): host on hermes1 in a separate failure domain. Wrap with Hermes adapter exposing `register_policy(system, axis, l4_artifact)`, `evaluate(action, context)`, `rollback_policy(system, axis, version)`. Target p99 ≤ 5ms under nominal load (≤200 evals/sec, 80% cache hit).
- **agent-mesh** (`agentmesh.gateway.policy_provider` + relay/store): intercept every Hermes NATS publish/subscribe via wrapper class around the existing NATS client. On policy deny → raise + audit-ledger row. Circuit breaker per ADR-011 §B.3 (120s max fallback, 30min/UTC day cumulative cap).
- **agent-sre** (SLO definitions + OTEL exporter): define SLOs from each `runtime.l4` invariant; SLO breach → `policy.violation.<system>.<rule>` NATS event consumed by HRV pacemaker for differentiable loss term.

**Pin SHA in `/home/ubuntu/.hermes/agt-pinned.txt` before any wiring.**

### Phase 1 — Single-system spike (2 weeks)
Use **consensus-reactor** as proving ground. Already has vein emitter from Phase 0 KR P0-7. Add:
- Author + ratify all 4 L4 files (consensus-reactor, already exists from this turn's hand-authoring).
- Register them in agent-os PolicyEngine.
- Wire AgentMesh enforcer for `consensus.>` subjects.
- Validate end-to-end: policy violation → AgentMesh blocks → audit row → HRV digest reflects.
- Stand up jl4-service Docker on hermes1.

### Phase 2 — Differentiable RSI (3 weeks)
- Stand up isolated auditor systemd unit (read-only ledger.db mount, no NATS subscription except chain-alert publish).
- Implement `tools/rsi_optimizer.py` per ADR-011 §B.5–B.8 + C.5–C.8:
  - z-scored evidence-gated loss
  - JAX surrogate-model gradient
  - Annealed Gaussian noise (σ_0=0.05, τ=50, 100-cycle window; frozen on evidence_gate + quorum dimensions)
  - Classical SPRT revert (α=β=0.05, Δ=0.10, ≥10 cycles)
  - θ checkpoint every 10 cycles, last 5 retained
- Replace 4-class enum in `rsi_adaptive_scheduler.py` with `π_θ(x_t)`.
- Shadow-mode for 5 cycles before applying θ updates.

### Phase 3 — Catalog rollout (3 weeks)
- Author remaining 92 L4 files (96 total - 4 consensus-reactor already done).
- T1 substrate (7 systems × 4 = 28): council 3-of-3.
- T2 reactors (16 × 4 = 64): council 2-of-3.
- T3 dev tools (3 × 4 = 12): single-author.
- Each compiled by jl4-service, registered in agent-os, enforced by agent-mesh.

### Phase 4 — Validation & sign-off (1 week)
- 4 bypass-path RED tests GREEN (KR R-8):
  1. Synthetic bare-UPDATE → CHECK aborts + audit row
  2. Synthetic provenance-free publish → AgentMesh rejects + audit row
  3. Synthetic 5h DB lock attempt → impossible by P0-3 design (verified)
  4. PolicyEngine outage → 120s fallback + OOB alert + flagged audit + 30min/day halt
- Final council 3-of-3 sign-off recorded as `audit_ledger` rows with `policy_eval_result='ratified-v1'`.

## 5. AGT Integration Architecture

```
                       Hermes EAF
                          │
                          ▼
              ┌──────────────────────┐
              │  Hermes adapter shim │  src/executive_agents/infrastructure/policy/
              │  (port + adapter)    │  agt_agent_os_adapter.py
              └──────────┬───────────┘
                         │
                         ▼
       ┌─────────────────────────────────────┐
       │   AGT (Microsoft, MIT-licensed)     │
       │   /home/ubuntu/agt/                 │
       │                                     │
       │   ├── agent-governance-python/      │
       │   │   ├── agent-os/   (PolicyEngine)│  on hermes1
       │   │   ├── agent-sre/  (SLO + OTEL)  │  on hermes1
       │   │   └── agent-mesh/ (NATS enforce)│  per-client lib
       │   ├── agent-governance-rust/        │  (deferred to Q4)
       │   └── agent-governance-golang/      │  (deferred to Q4)
       └─────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │   l4-ide jl4-service │  Docker on hermes1
              │   /validate REST     │
              └──────────────────────┘
```

Key isolation properties:
- AGT runs on hermes1; Hermes EAF reactors on hermes2 (separate failure domain — Vogels' r1 requirement).
- AGT version pinned by SHA in `/home/ubuntu/.hermes/agt-pinned.txt`; updates require explicit ADR.
- Hermes does NOT modify AGT source — adapter pattern only.

## 6. Acceptance Criteria

Per-KR (R-1 through R-9):
1. Acceptance evidence URI populated.
2. RED test in `scripts/red_tests/` passes.
3. Audit ledger row chain-verified.
4. KR transitions only via canonical `update_key_result()` path (Phase 0 enforced).

Per-phase gate:
- Phase 0 → 0.5: child OKR completion = 1.0
- Phase 0.5 → 1: AGT smoke + 3 wiring KRs GREEN
- Phase 1 → 2: consensus-reactor end-to-end proof artifact
- Phase 2 → 3: 5 stable shadow cycles + first applied θ update
- Phase 3 → 4: 96 files registered + enforced
- Phase 4: 3-of-3 council sign-off

## 7. Risks & Mitigations

- **R1: AGT API changes mid-rollout.** Mitigation: SHA pin; never bump without ADR.
- **R2: jl4-service Docker unstable on hermes1.** Mitigation: fallback to L4 textual validation by GPT-4o + manual review; defer enforcement until stable; tracked in audit ledger.
- **R3: Differentiable RSI collapses θ to pathological values.** Mitigation: clip per parameter; SPRT revert; freeze evidence-gate + quorum dimensions entirely.
- **R4: Catalog authoring stalls (96 files is a lot).** Mitigation: scaffolder generates stubs; council reviews in batches; T1 most urgent, T3 deferrable.
- **R5: AgentMesh enforcer overhead exceeds 5ms p99.** Mitigation: LRU cache invalidation push; separate failure domain reduces tail; circuit breaker per §B.3.
- **R6: LLDAP outage during operations.** Mitigation: §C.3 — deny-safe; cumulative 5-min/day cap before halt.

## 8. Open Questions

- Does AgentOS PolicyEngine on hermes1 need its own NATS cluster, or can it share `hermes-nats-prod`? (Lean: same cluster, dedicated subjects; tracked in §B.3 of ADR.)
- Do we adopt AGT Rust SDK in Q4 for hot-path enforcement? (Deferred decision; Q3 is Python-only.)

## 9. Sign-off log

- 2026-06-06: ADR-011 r2 + C.1–C.8 ratified 3-of-3
- 2026-06-06: OKRs created via canonical lifecycle (atomic transaction completed inside surgical lock-release window)
- 2026-06-06: This PRD authored
- 2026-06-13 target: Phase 0 complete (child OKR 1.0)
- TBD: per-phase completion gates
- Q3 close: final 3-of-3 ratification recorded in audit ledger
