# Council Review Round 1 — ADR-011

**Date:** 2026-06-06
**ADR under review:** ADR-011 (HRV-OTEL Substrate, Differentiable RSI, Policy Catalog)
**Outcome:** **2 APPROVE_WITH_REVISIONS + 1 REQUEST_RESPIN → REVISE TO r2**

---

## Verdict matrix

| Reviewer | Verdict | Severity of issues |
|---|---|---|
| Werner Vogels (substrate scalability) | APPROVE_WITH_REVISIONS | High (5 mechanical edits) |
| Demis Hassabis (differentiable RSI) | APPROVE_WITH_REVISIONS | Critical (Goodhart loop) + 7 others |
| Lewis Hamilton (governance enforcement) | **REQUEST_RESPIN** | Critical structural gap |

Cannot proceed to OKR creation (P6b) until r2 written + re-reviewed. Hamilton's verdict is decisive: the architecture as written does NOT close the OKR fabrication bypass paths.

---

## Vogels — 5 required edits (mechanical)

1. **§Phase 0 / Prometheus** → must add `remote_write` to VictoriaMetrics or Thanos before Phase 1; two-node scrape replica recommended; finalize port allocation (no TBDs).
2. **§3.2 PolicyEngine** → add: p99 ≤ 5ms target; LRU cache invalidation via `policy.invalidate.<system>.<axis>` push subject; cache TTL ≤ 60s; **AgentOS must run in separate failure domain from governed services** (resolve Q1 before Phase 1).
3. **§3.3 R3** → θ checkpoint every 10 cycles, retain last 5; `rsi rollback --to <epoch>` command; PolicyEngine must expose `rollback_policy(system, axis, version)`.
4. **§2.2 vein protocol** → anti-storm clause: 5s per-system debounce; `health.>` JetStream consumer `MaxAckPending=50`, `MaxDeliver=3`; HRV digest rate-limited to 1/30s newest-wins.
5. **§6 Q2** → LLDAP active/reachable is **BLOCKING for Phase 1**, not deferred.

## Hassabis — 8 issues (2 critical, 3 high, 3 medium)

**Critical:**
1. **w_1..w_4 dimensionally inhomogeneous** — `lock_wait_ms` will dominate gradient. Z-score normalize each loss term over warm-up window before summing.
2. **`completion_rate` in loss creates Goodhart loop** — optimizer has gradient path to relax `θ_evidence_gate_strictness` to boost completion_rate, recreating fabrication failure mode. **Fix:** replace with `evidence_gated_completion_rate` measured by logically-isolated auditor; isolate auditor from any θ-controlled NATS subject.

**High:**
3. **FD gradient at 1-hour delayed outcome is 40h/step serial** — reframe as surrogate-model gradient (fit linear `f̂(a_t|x_t;θ) → ŷ_t` from logged data, autograd through f̂); skip FD phase entirely.
4. **Zero exploration mechanism** — annealed Gaussian noise on `a_t` for first ~100 cycles; freeze `θ_policy_mutation_rate` and `θ_policy_mutation_temp` until ≥500 cycles.
5. **Shadow baseline causally contaminated; 20%×3-cycle revert underpowered** — replace with SPRT (P(θ_live < θ_baseline) > 0.95, evidence ≥10 cycles); evaluate baseline on disjoint windows or replay.

**Medium:**
6. EMA on y_t (window ≥5 cycles); trust-region bound ‖Δθ‖ ≤ δ_max per step.
7. Effective independent samples gate (not raw cycle count).
8. Document MLP/RL escalation criteria (linear residual R² < 0.6 → MLP).

## Hamilton — 4 mandatory respin items (governance enforcement)

**Bypass Path 1 — DIRECT DUCKDB WRITE NOT CLOSED**
- `okr_kr_reconciler.py` writes via `duckdb.connect(OKR_DB).execute("UPDATE...")`. NEVER touches NATS.
- AgentMesh PolicyEngine sits on the message-bus layer. Cannot intercept SQL.
- **Fix:** DB-layer enforcement — `BEFORE UPDATE` trigger on `okr_key_results` rejecting `status='done' AND evidence=''`, OR architectural constraint making `OKRAccountabilitySystem.update_key_result()` the only write path (verified by lint gate). Phase 0, not Phase 3.

**Bypass Path 2 — PARTIAL CLOSURE, RISKY FALLBACK**
- `integration.l4` for omnibus emitter is deferred to Phase 3 (weeks 4-6) — until then, omnibus continues to emit headerless.
- `allow with audit-only` fallback fires precisely under contention (the post-mortem scenario). Audit log produced during fallback is self-attested, untrusted.
- **Fix:** PolicyEngine fallback is itself policy-controlled — max fallback duration, separate out-of-band alert, audit log written during fallback flagged `unverified` until reconfirmed.

**Bypass Path 3 — DETECTED, NOT PREVENTED**
- ADR-011 catches `lock_wait` in HRV vein → eventually SGD reduces frequency. But that's probabilistic mitigation with 50+ cycle warm-start latency.
- **Fix:** explicit L4 `runtime.l4` rule on `okr-steering-reactor`: `MUST conn.hold_duration_s <= 30`. Plus context-managed connections per write batch (post-mortem fix F3) referenced explicitly in implementation plan.

**Audit ledger integrity — UNADDRESSED**
- Schema undefined. LLDAP inactive means every `actor.role` reference is unevaluable.
- **Fix:** define ledger schema `(event_id, ts, actor_id, action, subject, evidence_hash, policy_eval_result, policy_engine_version, chain_prev_hash)`; hash-chain; written to store NOT controlled by emitting system. LLDAP restart blocking Phase 1.

---

## r2 plan

ADR-011 r2 will:
1. Add new layer L0 (DB-layer enforcement) — DuckDB triggers on `okr_accountability.db`.
2. Reorder Phase 0/1 — substrate hardening BEFORE per-system policy authoring.
3. Move integration.l4 for `event-emitter-omnibus` and `okr-kr-reconciler` to Phase 0 (was Phase 3).
4. Add AgentOS failure-domain isolation (separate machine or at least separate systemd slice).
5. Add audit ledger schema § and Merkle-hash chain.
6. Add LLDAP-restore as Phase 0 blocking task.
7. Replace Goodhart-prone loss with isolated auditor + evidence-gated completion rate.
8. Add ledger-format §.
9. Add SPRT/EMA/exploration to differentiable RSI §.
10. Add anti-storm + AgentOS isolation to Vogels' subsystem §.
11. Re-dispatch Hamilton for re-review BEFORE OKR creation.

Estimated time to author r2: 30 min. Re-review: 10 min.
