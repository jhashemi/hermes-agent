# ADR-011: HRV-as-Veins OTEL Substrate, Differentiable RSI, and Per-System Policy Catalog

**Status:** Accepted (council 3-of-3 APPROVE after r2 + C.1–C.8 surgical nits; see Appendix C)
**Date:** 2026-06-06
**Authors:** Jeff Dean persona (lead), J Hash (principal)
**Supersedes:** none
**Refines:** ADR-009 (skill-install pipeline), ADR-010 (voice bridge)
**Related:** post-mortems/2026-06-06-okr-fabrication-root-cause-and-stuck-tasks.md
**Revision history:** r1 (initial) → r2 (Vogels APPROVE_W_REVISIONS + Hassabis APPROVE_W_REVISIONS + Hamilton REQUEST_RESPIN; see `reviews/ADR-011-r1-council-summary.md`)

---

## 0. TL;DR

Replace the rigid "1 cron monitor per ADR" plan with a **biologically-inspired observability substrate**:

- **Heart (HRV pacemaker)**: emits `hrv.pulse.tick` and `hrv.status.digest` on NATS at adaptive intervals (already alive — 1,420 msgs in HRV stream).
- **Veins (per-system OTEL exporters)**: every autonomous system exposes a `/metrics` Prometheus endpoint *and* publishes structured health envelopes onto `health.<system>.<signal>` NATS subjects. Veins carry **deoxygenated state** (errors, saturation, latency, lock-wait) back to the heart.
- **Brain (adaptive scheduler + RSI optimizer)**: reads HRV digest + per-system veins, produces a **differentiable loss vector** that SGD-optimizes the RSI cycle's hyperparameters (interval choices, policy mutation rates, council quorum thresholds).
- **Executive policy layer (L4 + AgentOS)**: every system declares a **4-axis policy bundle** (governance/runtime/integration/usage) authored in L4 (NL→formal), validated by `jl4-service`, registered in AgentOS PolicyEngine, enforced at AgentMesh MessageBroker.
- **Cron monitors are demoted to safety nets** — silent-on-green watchdogs that fire only when veins go offline (i.e., the substrate itself fails). They are NOT the primary observability path.

This unifies four threads that were previously orthogonal: HRV pacing, OKR governance, L4 policy ratification, and AGT integration.

---

## 1. Context

### 1.1 What we found in the cluster snapshot (P1)

19 autonomous systemd user services on hermes2 (full inventory in §A.1):

```
agent-actors                 hermes-gateway                 okr-kr-reconciler
cluster-broadcast-listener   hermes-skills-broadcast        okr-steering-reactor
consensus-reactor            kr-watchdog                    org-chart-router
event-emitter-omnibus        memory-recorder                serena-lsp
github-webhook-bridge        novnc                          vcg-crdt-bridge
                             okr-dispatch-reactor           voice-bridge
```

Plus core infra:
- **NATS JetStream**: 4-node cluster `hermes-nats-prod` (8 active streams)
- **DuckDB**: `okr_accountability.db` (90 MiB), `kanban.db` (5.6 MB), `event_emitter.duckdb`
- **LLDAP**: currently inactive on hermes2 (governance impact — see §6)
- **Syncthing**: 3-device mesh (hermes2 ↔ hermes1 ↔ rust-build)
- **HRV pacemaker**: emits `hrv.pulse.tick` every ~60s; runs via cron (systemd unit dead)
- **Gateway skill-install pipeline**: 3-platform (Telegram/Discord/WhatsApp) + NATS broadcast hook

### 1.2 What's missing

- **Zero OTEL/Prometheus instrumentation.** No listeners on 4317/4318/9090. Only transitive lockfile entries.
- **Zero per-system health subjects.** The 8 NATS streams carry *work* (events, dispatches, OKR state) but not *vital signs*.
- **HRV signal is undifferentiated.** Current `rsi_adaptive_scheduler.py` collapses load + lock_wait + reactor_count into a 4-class enum (anxious/urgent/engaged/regular). Coarse — and not gradient-friendly.
- **Policy is ad-hoc.** ADRs are markdown narratives. No machine-readable contract that tells AgentMesh "block publish if violation."
- **OKR fabrication root cause persists.** `okr_kr_reconciler.py` bare UPDATE bypasses `OKRAccountabilitySystem.update_key_result()` evidence gate. No technical control prevents recurrence.

### 1.3 Why now

Three forcing functions:
1. **Cron monitor proliferation risk** — if we add 1 monitor per ADR (we're at ADR-010, headed to ~30), we'd accumulate 30 brittle cron jobs that each become their own failure surface. Won't scale.
2. **OKR fabrication needs a structural fix** — post-mortem identified 3 bypass paths. Markdown ADRs cannot enforce; need executable policy at the message layer.
3. **L4 + AGT research is fresh** — clean integration window before more debt accrues.

---

## 2. Decision

### 2.1 Architecture (6 concentric layers)

```
              ┌─────────────────────────────────────────────────────┐
              │  L5 — RATIFICATION  (NL → L4 AST → AgentOS Policy)  │
              │  Council quorum (Vogels/Hassabis/Hamilton)          │
              ├─────────────────────────────────────────────────────┤
              │  L4 — POLICY CATALOG  (per-system 4-axis bundles)   │
              │  governance / runtime / integration / usage         │
              ├─────────────────────────────────────────────────────┤
              │  L3 — ENFORCEMENT  (AgentMesh MessageBroker)        │
              │  Every NATS publish/subscribe consults PolicyEngine │
              ├─────────────────────────────────────────────────────┤
              │  L2 — OBSERVABILITY  (HRV veins + OTEL exporters)   │
              │  Prometheus /metrics + NATS health.<sys>.<signal>   │
              ├─────────────────────────────────────────────────────┤
              │  L1 — SUBSTRATE  (NATS, DuckDB, LLDAP, Syncthing)   │
              ├─────────────────────────────────────────────────────┤
              │  L0 — DATA-LAYER ENFORCEMENT  (NEW in r2)            │
              │  DuckDB triggers + AgentOS-co-signed write paths    │
              │  Closes the bypass below the message bus.            │
              └─────────────────────────────────────────────────────┘
```

Read top-down for human authoring (NL rule → law). Read bottom-up for runtime (substrate emits → veins carry → scheduler decides → enforcer blocks).

**Why L0 was added in r2:** Hamilton review caught that the post-mortem's Bypass Path 1 (`okr_kr_reconciler.py` issuing bare `UPDATE` directly via `duckdb.connect`) NEVER touches NATS — so a message-bus PolicyEngine cannot intercept it. The fix must live at the data layer. See §2.6.

### 2.2 The HRV-OTEL substrate

**Veins protocol** — every autonomous system MUST expose two channels:

1. **Prometheus `/metrics`** on a service-local port (registry in §A.2). Standard counters/gauges/histograms. Scraped by node_exporter pattern locally; aggregated by a single Prom instance later.
2. **Health envelope NATS publish** on `health.<system>.<signal>`, every 30s (or on state transition):
   ```json
   {
     "system": "consensus-reactor",
     "node": "hermes2",
     "ts": "2026-06-06T13:00:00Z",
     "vital_signs": {
       "loop_lag_ms": 12,           // p50 inner-loop latency
       "queue_depth": 3,             // pending work
       "error_rate_60s": 0.0,        // errors/sec last minute
       "saturation": 0.18,           // 0..1, resource pressure
       "deps_ok": ["nats", "duckdb"], // upstream deps reachable
       "deps_fail": []
     },
     "state": "healthy",             // healthy|degraded|critical|offline
     "since": "2026-06-06T12:55:00Z" // when current state began
   }
   ```

**HRV pacemaker** consumes the veins, computes a digest, publishes `hrv.status.digest`:
```json
{
  "ts": "2026-06-06T13:00:00Z",
  "feature_vector": {                // x_t for SGD
    "load1": 8.79,
    "lock_wait": 0,
    "veins_healthy_frac": 0.95,      // 18/19 reporting healthy
    "p99_loop_lag_ms": 240,
    "error_rate_aggregate": 0.0,
    "queue_depth_total": 14,
    "msg_rate_60s": 593
  },
  "interval_class": "regular",       // legacy, kept for compat
  "next_tick_recommendation_s": 60
}
```

### 2.3 Differentiable RSI

The RSI cycle is parameterized by **θ** (a small vector, ~20 floats):
- `θ_interval_base`, `θ_interval_load_coef`, `θ_interval_lock_coef` (scheduler timing)
- `θ_council_quorum_required`, `θ_council_round_max` (deliberation)
- `θ_policy_mutation_rate`, `θ_policy_mutation_temp` (exploration)
- `θ_evidence_gate_strictness`, ... (governance)

At each cycle, observe `(x_t, action_a_t, outcome_y_t)`:
- `x_t` = HRV feature vector
- `a_t` = scheduler/governance choices (interval, quorum, etc.)
- `y_t` = downstream metrics 1 hour later: completion_rate, error_rate, lock_wait_count, fabrication_count

Define **loss** `L(θ) = w_1·(1 - completion_rate) + w_2·error_rate + w_3·fabrication_count + w_4·avg_lock_wait + λ·||θ||²`

All terms are smooth/differentiable in θ via the linkage `a_t = π_θ(x_t)` where `π_θ` is the policy network (start with linear; upgrade to MLP after baseline). Gradient flow:
- `∂L/∂θ` via finite differences (cheap: ~20 evals/cycle) initially.
- Upgrade to autograd via JAX once the substrate is stable.

**SGD update**: θ ← θ - η·∇L, η=0.01, momentum=0.9. State persisted to `~/.hermes/rsi-adaptive/theta.json`. Bounded clipping per parameter (no quorum < 2, no interval < 60s, etc.).

The loss vector is also published on `rsi.loss.tick` for council review — humans can inspect what the system is optimizing for.

### 2.4 Policy catalog (4 axes per system)

Every autonomous system + core infra component declares a bundle at:
```
~/.hermes/policies/<system>/
  governance.l4    # WHO can change behavior + signoff requirements
  runtime.l4       # invariants the running system MUST maintain
  integration.l4   # what subjects/streams it can pub/sub + with what auth
  usage.l4         # quotas, rate limits, cost ceilings, who may invoke
```

L4 source files compiled by `jl4-service` (Docker `ghcr.io/smucclaw/jl4-service`) → validated CoreL4 → registered in AgentOS PolicyEngine via `register_policy(system, axis, l4_artifact)`.

**Example axes for `consensus-reactor`:**

```l4
-- governance.l4
RULE consensus-reactor-config-change
GIVEN actor : Agent, change : ConfigChange
IF actor.role = "consensus-reactor-config-change" AND
   change.signoff_count >= 2 AND
   change.includes_council_approval = TRUE
THEN MAY apply change
DESPITE consensus-reactor-emergency-override

-- runtime.l4
RULE consensus-quorum-invariant
GIVEN round : DeliberationRound
MUST round.unique_participants >= 3
DESPITE NOTHING

-- integration.l4
RULE consensus-publish-allow
GIVEN msg : Publish
IF msg.subject MATCHES "consensus.*" AND
   msg.has_header "X-Hermes-Signoff"
THEN MAY publish

-- usage.l4
RULE consensus-rate-limit
GIVEN window : 60s
MUST count(consensus.cycle.start) <= 6
```

### 2.5 Cron monitors — demoted role

Cron monitors become **substrate-failure detectors only**:
- `hrv-pacemaker-pulse-watchdog.cron`: alerts if no `hrv.pulse.tick` in >5 min.
- `vein-silence-watchdog.cron`: alerts if any system's `health.<sys>.*` subject has been silent >5×its declared cadence.
- `policy-engine-availability.cron`: alerts if AgentOS PolicyEngine is unreachable.

These are **silent-on-green** (zero output when substrate is alive) and **never proliferate per ADR**. Capped at ~5 total.

---

## 3. Consequences

### 3.1 Positive
- **Single source of health truth** (NATS `health.>` subjects + HRV digest); humans + agents read same signal.
- **Differentiable optimization target** — RSI tunes itself with bounded SGD vs. hand-tuned heuristics.
- **L4-encoded contracts** — OKR fabrication structurally impossible (PolicyEngine rejects bare-UPDATE publishes that lack evidence header).
- **Cron monitor sprawl avoided** — capped at 5 substrate watchdogs vs. 30+ per-ADR.
- **Policy authoring is NL-first** — operator describes rule in English, GPT-4o extracts to L4, council ratifies.

### 3.2 Negative
- **L4 toolchain is GHC 9.10.2 + Cabal** — extra build dependency. Mitigation: use prebuilt `jl4-service` Docker image (no local Haskell needed).
- **PolicyEngine is a hot path** — every NATS publish consults it. Mitigation: in-process LRU cache keyed on `(system, axis, action_hash)`; fall back to "allow with audit-only" if PolicyEngine unreachable >2s.
- **SGD on a small data regime** is noisy. Mitigation: warm-start θ from current scheduler heuristics; only apply gradient updates after 50+ cycles of observations.
- **24 systems × 4 axes = 96 L4 files to author.** Mitigation: catalog scaffolder generates skeletons; per-system author fills in 1-page.

### 3.3 Risks (and mitigations)
- **R1: PolicyEngine deadlocks the cluster.** → Circuit breaker: if `policy_eval_p99 > 200ms` for 60s, fall back to allowlist mode + alert.
- **R2: HRV digest becomes the bottleneck for RSI cadence.** → Decouple: scheduler reads cached digest with 30s staleness tolerance.
- **R3: SGD drives θ to pathological values.** → Bounded clipping per parameter + canary: a separate non-SGD baseline policy runs in shadow; if the optimized θ underperforms baseline by >20% for 3 cycles, revert.
- **R4: Council fatigue from per-policy ratification.** → Tier policies: T1 (substrate, requires 3-of-3), T2 (per-system runtime, requires 2-of-3), T3 (usage limits, single-author).

---

## 4. Implementation plan (sliced for spike→incremental rollout)

### Phase 0 — Substrate skeleton (this week)
- [ ] Write `tools/hrv_vein_emitter.py` — drop-in library systems import to publish `health.<sys>.<signal>` envelopes.
- [ ] Write `tools/policy_catalog/scaffolder.py` — generates 4 stub `.l4` files per system.
- [ ] Stand up single Prometheus instance on hermes2:9090, `node_exporter` on each cluster machine.
- [ ] Deploy `jl4-service` Docker on hermes2; verify `/validate` REST.

### Phase 1 — Spike: 1 system end-to-end (next week)
- [ ] Pick `consensus-reactor` (smallest blast radius, already governance-adjacent).
- [ ] Author 4 L4 files + register with `jl4-service`.
- [ ] Wire vein emitter into reactor loop.
- [ ] Wire AgentMesh-style PolicyEnforcer shim into one publish path.
- [ ] Validate end-to-end: NL rule → L4 → registered → enforced → vein observed → HRV digest reflects.

### Phase 2 — Differentiable RSI (week 3)
- [ ] Add `tools/rsi_optimizer.py` — finite-diff gradient + bounded SGD.
- [ ] Replace 4-class enum in `rsi_adaptive_scheduler.py` with `π_θ(x_t)` policy.
- [ ] Shadow-mode run for 5 cycles before applying θ updates.

### Phase 3 — Catalog rollout (weeks 4-6)
- [ ] Author L4 bundles for remaining 18 systems (1/day, with council review for T1/T2).
- [ ] Migrate cron monitors to substrate-failure detectors only.
- [ ] Decommission per-ADR cron monitor pattern.

### Phase 4 — Validation (week 7)
- [ ] RED tests: synthetic OKR fabrication attempt → AgentMesh PolicyEnforcer rejects.
- [ ] RED tests: vein silence → HRV digest reflects → scheduler defers cycle.
- [ ] Council 3-of-3 sign-off on stable θ + policy catalog v1.

---

## 5. Alternatives considered

### 5.1 Pure cron monitor proliferation (rejected)
1 monitor per ADR. Brittle, doesn't scale, doesn't catch unknown-unknown failures.

### 5.2 OpenTelemetry without HRV (rejected)
OTEL alone is reactive (humans read dashboards). HRV makes the loop close — observation → policy adaptation → outcome — without operator in critical path.

### 5.3 Markdown ADRs as policy (status quo, rejected)
Cannot enforce. OKR fabrication post-mortem demonstrates this directly.

### 5.4 Adopt AGT monorepo wholesale (deferred)
AGT (microsoft/agent-governance-toolkit) is excellent but uses different message bus + different identity backend. We absorb its **patterns** (PolicyEngine API shape, Audit Ledger structure) without forking the codebase. Re-evaluate at Phase 4.

---

## 6. Open questions

- **Q1: Where does AgentOS run?** Co-located with hermes-gateway? Separate process? Decision needed before Phase 1.
- **Q2: LLDAP currently inactive on hermes2** — is identity/role-resolution needed for governance.l4 evaluation? (Likely yes; need to restart or migrate.)
- **Q3: Differentiable loss weights `w_1..w_4`** — initial values are guesses. Should they themselves be optimized (meta-learning)? Defer to Phase 2.
- **Q4: How do per-project telemetry namespaces compose?** When `executive_agents_framework` and `hermes-agent` and `psi_artifact` all emit veins, do we use prefix `health.<project>.<system>.<signal>` (4-tuple)? Lean toward yes.

---

## 7. Council review (pending)

- **Vogels** — substrate scalability + Prometheus single-point review.
- **Hassabis** — differentiable RSI design + reward shaping.
- **Hamilton** — governance contract enforcement + audit trail completeness.

---

## Appendix A — System & Infra Inventory

### A.1 Autonomous systems (hermes2, 19 services)

| # | Service | Purpose | Stream(s) | Port |
|---|---|---|---|---|
| 1 | `agent-actors` | Actor framework runtime | `EXEC_AGENT` | TBD |
| 2 | `cluster-broadcast-listener` | Cross-node message reception | `cluster.>` | — |
| 3 | `consensus-reactor` | Council deliberation orchestrator | `consensus.>` | TBD |
| 4 | `event-emitter-omnibus` | DuckDB → NATS state mirror | `exec_okr.>` | — |
| 5 | `github-webhook-bridge` | Inbound webhook → NATS | `webhook.github.>` | TBD |
| 6 | `hermes-gateway` | Multi-platform message gateway | many | 8745, 8746 |
| 7 | `hermes-skills-broadcast` | NATS skill propagation receiver | `SKILLS_BROADCAST` | — |
| 8 | `kr-watchdog` | OKR/KR escalation cron-style | `exec_okr.escalation.>` | — |
| 9 | `memory-recorder` | Conversation memory persistence | — | TBD |
| 10 | `novnc` | VNC web bridge (dev tool) | — | 6080 |
| 11 | `okr-dispatch-reactor` | OKR work dispatch | `exec_dispatch.>` | — |
| 12 | `okr-kr-reconciler` | KR state reconciliation **(bypass risk)** | `exec_okr.>` | — |
| 13 | `okr-steering-reactor` | OKR steering committee | `exec_okr.steering.>` | — |
| 14 | `org-chart-router` | Role-based routing | `org.>` | — |
| 15 | `serena-lsp` | LSP code-graph singleton | — | TBD |
| 16 | `vcg-crdt-bridge` | CRDT sync bridge | `VCG_CRDT_SYNC` | — |
| 17 | `voice-bridge` | ADR-010 voice bridge | `VOICE_BRIDGE` | TBD |
| 18 | `x11vnc` | VNC server (dev tool) | — | 5900 |
| 19 | `xvfb` | Virtual framebuffer (dev tool) | — | — |

### A.2 Core infrastructure components

| Component | Type | Endpoint | Owner |
|---|---|---|---|
| NATS JetStream cluster | Message bus | `nats://100.127.115.56:4222` (4 nodes) | infra |
| DuckDB: `okr_accountability.db` | OKR SSOT | file (90 MiB) | EAF |
| DuckDB: `kanban.db` | Task SSOT | file (5.6 MiB) | hermes-agent |
| DuckDB: `event_emitter.duckdb` | Mirror state | file | omnibus |
| LLDAP | Identity / RBAC | `localhost:17170` (currently inactive) | infra |
| Syncthing | File sync | TCP 22000, REST 8384 | infra |
| HRV stream | Pacemaker | `hrv.>` (1,420 msgs) | RSI |
| Skills hub | Skill registry | `~/.hermes/skills/` | hermes-agent |

Each component gets its own 4-axis policy bundle.

### A.3 Prometheus port allocation (proposed, sequential 9101+)

```
9101  agent-actors                  9111  okr-dispatch-reactor
9102  consensus-reactor             9112  okr-kr-reconciler
9103  event-emitter-omnibus         9113  okr-steering-reactor
9104  github-webhook-bridge         9114  org-chart-router
9105  hermes-gateway                9115  serena-lsp
9106  hermes-skills-broadcast       9116  vcg-crdt-bridge
9107  kr-watchdog                   9117  voice-bridge
9108  memory-recorder               9118  cluster-broadcast-listener
9109  hrv-pacemaker (special)       9120  policy-engine
9110  rsi-optimizer (special)       9121  agent-mesh
                                    9122  agent-os
```

Reserved: 9100 (node_exporter), 9090 (Prometheus self).

---

# Appendix B — r2 Revisions (council-driven)

This appendix contains binding revisions to the ADR body above. Where r2 conflicts with the body, **r2 wins**.

## B.1 (Hamilton respin) — L0 Data-Layer Enforcement

**Problem closed:** Bypass Path 1 from post-mortem 2026-06-06 — `okr_kr_reconciler.py` writes `UPDATE okr_key_results SET status='done'` directly via `duckdb.connect(OKR_DB)`. Never emits NATS. Message-bus PolicyEngine cannot intercept SQL.

**Solution:** Two-pronged enforcement at the data layer (Phase 0 — BLOCKING).

### B.1.1 DuckDB BEFORE-UPDATE trigger

Install on `okr_accountability.db` at startup (in `OKRAccountabilitySystem.__init__`):

```sql
CREATE OR REPLACE TRIGGER tr_okr_kr_evidence_required
BEFORE UPDATE ON okr_key_results
FOR EACH ROW
WHEN (NEW.status = 'done' AND
      (NEW.evidence_uri IS NULL OR NEW.evidence_uri = ''))
EXECUTE FUNCTION raise_evidence_required();
```

The function `raise_evidence_required()` raises a DuckDB error including the connection's session-id, aborting the transaction. Any reconciler attempting bare UPDATE → fails with a structural error, not a silent log line.

### B.1.2 Lint gate at code layer

`tools/policy_catalog/lint_okr_writes.py` (new) — AST-walks `~/.hermes/scripts/` and `~/executive_agents_framework/` for direct `duckdb.connect(*okr_accountability*).execute("UPDATE")` calls. Pre-commit + CI hook. Allows ONLY:

```python
from executive_agents_framework.okr_accountability import OKRAccountabilitySystem
sys = OKRAccountabilitySystem(...)
sys.update_key_result(kr_id, status="done", evidence_uri="...")  # canonical write
```

### B.1.3 Post-mortem fix F3 (connection lifetime) referenced in plan

Add explicit Phase 0 task: refactor `OKRAccountabilitySystem` to context-managed connection per write batch, NOT process-lifetime hold. Resolves Bypass Path 3 at the source (Hamilton noted SGD-based mitigation is too slow).

## B.2 (Hamilton respin) — Audit Ledger Schema (Merkle-chained)

**New table** `okr_audit_ledger` (separate DuckDB file `~/.hermes/audit/ledger.db` written by AgentOS, NOT by emitting reactors):

```sql
CREATE TABLE audit_ledger (
    event_id          TEXT PRIMARY KEY,
    ts                TIMESTAMP NOT NULL,
    actor_id          TEXT NOT NULL,           -- LLDAP-resolved
    action            TEXT NOT NULL,           -- e.g. 'okr.kr.update'
    subject           TEXT NOT NULL,           -- target object id
    evidence_hash     TEXT,                    -- sha256 of evidence_uri content
    policy_eval_result TEXT NOT NULL,          -- 'allow' | 'deny' | 'fallback_audit_only'
    policy_engine_version TEXT NOT NULL,
    chain_prev_hash   TEXT NOT NULL,           -- prev row sha256
    chain_this_hash   TEXT NOT NULL,           -- sha256(prev || this row)
    fallback_unverified BOOL DEFAULT FALSE     -- TRUE iff written during PE outage
);
```

- Hash chain enables tamper detection.
- `fallback_unverified=TRUE` rows are auto-revisited by AgentOS when PolicyEngine recovers; either upgraded to `allow`/`deny` or escalated.

## B.3 (Hamilton respin) — Hardened Fallback

PolicyEngine fallback (R1 in r1) is itself policy-controlled in r2:

- Max fallback duration: **120s** before automatic restart of PolicyEngine + alert.
- Out-of-band alert via separate channel (NOT NATS — the system whose message bus is being bypassed).
  - Phase 0: email + Telegram DM to incident-commander.
  - Phase 2+: dedicated `policyengine.outage.>` JetStream on a different cluster (or local SQLite spool until recovery).
- All fallback rows in audit ledger flagged `fallback_unverified=TRUE`.
- Fallback duration cumulative per UTC day capped at 30 minutes; exceeding cap halts new policy-gated publishes (denial-of-service risk acknowledged but accepted; explicit incident-commander unblock required).

## B.4 (Hamilton respin) — LLDAP is Phase 0 BLOCKING

LLDAP currently inactive on hermes2. Every `actor.role` reference in any `governance.l4` rule is unevaluable until restored.

**Phase 0 task added:** restart `lldap.service` (or migrate to hermes1) and verify role queries return for all profiles in `/home/ubuntu/.hermes/config.yaml:agents.profiles`. Until done, ALL governance-axis policies are flagged `DRAFT/UNENFORCED` in the catalog.

## B.5 (Hassabis critical) — Loss Function r2

Replace §2.3 loss formulation:

**Original (r1, broken):**
`L(θ) = w_1·(1 - completion_rate) + w_2·error_rate + w_3·fabrication_count + w_4·avg_lock_wait + λ·||θ||²`

**Revised (r2):**
```
L(θ) = w_1·(1 - egcr_z(θ))         # evidence-gated completion rate, z-scored
     + w_2·err_z(θ)                 # error_rate_60s, z-scored
     + w_3·fab_count_z(θ)           # fabrication_count, z-scored
     + w_4·lock_wait_z(θ)           # lock_wait_ms, z-scored
     + λ·||θ - θ_0||²              # trust region around warm-start
where:
  egcr = evidence_gated_completion_rate measured by ISOLATED auditor service
  egcr is computed from audit_ledger rows where policy_eval_result='allow'
  AND evidence_hash != '' AND chain integrity verified
  Auditor reads audit_ledger over READ-ONLY DuckDB; subscribes to NO θ-controlled subject
```

- All four signals z-scored independently over warm-up window (≥50 cycles); raw scales no longer dominate.
- `egcr` instead of raw `completion_rate` → no Goodhart gradient toward relaxing evidence gate.
- Trust region (λ-term) anchors θ near warm-start; allows bounded SGD steps.
- `w_1..w_4` documented prior: `w_3=4.0` (fabrication is most consequential), `w_4=2.0`, `w_2=1.5`, `w_1=1.0`.

## B.6 (Hassabis high) — Surrogate-Model Gradient

Replace §2.3 finite-diff gradient with surrogate-model approach:

```python
# Pseudo-code
log = collect_recent_cycles(N=200)  # (x_t, a_t, y_t) tuples
f_hat = fit_linear_surrogate(log)   # JAX, ~200 free params
grad_L_theta = jax.grad(loss_via_surrogate)(theta)
theta_new = theta - eta * jnp.clip(grad_L_theta, -delta_max, delta_max)
```

Avoids 40-hour serial FD problem. Linear surrogate first; escalate to MLP only if R² < 0.6 on held-out cycles.

## B.7 (Hassabis high) — Exploration via Annealed Noise

For first 100 cycles after a θ change:
```
a_t = pi_theta(x_t) + xi_t        where xi_t ~ N(0, sigma_t²·I)
sigma_t = sigma_0 · exp(-cycle/tau)    sigma_0=0.05, tau=50
```
Disables on cycle 100, freezes for any θ touching evidence gate or quorum.

## B.8 (Hassabis high) — SPRT Revert Trigger

Replace 20%×3-cycle threshold with SPRT:
```
# H0: theta_live ≥ theta_baseline (no need to revert)
# H1: theta_live < theta_baseline by margin Δ=0.10
# alpha=0.05, beta=0.05; minimum 10 cycles of evidence
# If P(H1|data) > 0.95 → revert
```
Shadow baseline evaluated on disjoint replay windows, NOT live-correlated.

## B.9 (Vogels) — Substrate Hardening

- **Prometheus replica**: hermes2:9090 + hermes1:9090 (identical scrape config) + `remote_write` to VictoriaMetrics single-node on hermes2:8428. Both Prom write to VM; readers query VM. SPOF eliminated.
- **PolicyEngine target latency**: p99 ≤ 5ms under nominal load.
- **Cache invalidation**: push via `policy.invalidate.<system>.<axis>` subject on every `register_policy()` call. TTL ceiling 60s.
- **AgentOS deployment**: separate failure domain. Phase 0 decision: runs on hermes1 (separate machine from hermes2 NATS clients).
- **Vein anti-storm**: state-transition envelopes 5s per-system debounce; `health.>` consumer `MaxAckPending=50, MaxDeliver=3`; HRV digest 1/30s newest-wins.
- **θ checkpoints**: `~/.hermes/rsi-adaptive/theta-<epoch>.json` every 10 cycles; retain last 5; CLI `rsi rollback --to <epoch>`.
- **Policy versioning**: AgentOS `rollback_policy(system, axis, version)` API; SemVer mandatory on every `.l4` file (already in template).

## B.10 r2 Phase ordering

Phase 0 (BLOCKING for any later phase):
1. ✅ DuckDB BEFORE-UPDATE trigger on okr_key_results
2. ✅ Lint gate for direct DB writes
3. ✅ OKRAccountabilitySystem refactor: per-batch context-managed connection
4. ✅ Audit ledger schema + AgentOS-only writer
5. ✅ LLDAP restart on hermes2 (or migrate to hermes1)
6. ✅ Prometheus replica + VictoriaMetrics remote_write
7. ✅ AgentOS standup on hermes1 (separate failure domain)
8. ✅ Hardened fallback: 120s cap, OOB alert, ledger flag

Phase 1 (single-system spike, was unchanged):
- consensus-reactor as proving ground.

Phase 2 (differentiable RSI, NEW changes):
- Surrogate-model gradient (NOT FD)
- z-scored evidence-gated loss
- Annealed exploration noise
- SPRT revert trigger
- Trust-region update

Phase 3 (catalog rollout):
- Reordered: omnibus + reconciler integration.l4 moved up to Phase 0.5 (between 0 and 1).

Phase 4 (validation):
- RED test: synthetic bare-UPDATE attempt against okr_kr → trigger fires, transaction aborts, audit row written.
- RED test: synthetic provenance-free publish → AgentMesh enforcer rejects.
- RED test: synthetic 5-hour DB lock-hold → cannot occur (context-managed connection design).
- RED test: PolicyEngine outage → fallback alerts + ledger rows flagged.
- Council 3-of-3 sign-off.


---

# Appendix C — r2 Surgical Revisions (Council R2 nits)

After r2 dispatch, council returned 3-of-3 APPROVE_WITH_REVISIONS (Hamilton lifted REQUEST_RESPIN). The following surgical edits address remaining nits.

## C.1 (Hamilton) — DuckDB trigger DDL correction (B.1.1 supersession)

The B.1.1 DDL used PostgreSQL `EXECUTE FUNCTION` syntax which DuckDB does not support. Replaced with a CHECK constraint (always enforced, no stored-function dependency):

```sql
ALTER TABLE okr_key_results
ADD CONSTRAINT chk_done_requires_evidence
CHECK (status != 'done' OR (evidence_uri IS NOT NULL AND evidence_uri != ''));
```

Lint gate (B.1.2) must resolve path *constants* (e.g., `OKR_DB`) via AST, not just string-literal regex match.

## C.2 (Hamilton) — okr-steering-reactor connection hold L4 rule

Add to `~/.hermes/policies/okr-steering-reactor/runtime.l4` (and analogously for any process with write access to `okr_accountability.db`):

```l4
§ okr-steering-conn-hold-limit
GIVEN proc : Process
MUST proc's okr_db_conn_hold_duration_s IS AT MOST 30
DESPITE okr-steering-emergency-override
```

Required before Phase 1.

## C.3 (Hamilton) — §3.3 LLDAP-outage risk addition

**R5: LLDAP outage during active governance evaluation.**
Mitigation: governance.l4 evaluations that require role resolution **deny-safe** when LLDAP unreachable. Distinct from PolicyEngine outage (B.3 fallback). LLDAP outage produces immediate denial + OOB alert; cumulative LLDAP-down time per UTC day capped at 5 min before halt of governance-axis-gated operations.

## C.4 (Vogels) — "nominal load" definition for PolicyEngine 5ms p99

B.9 §② is amended:

> *PolicyEngine target latency: p99 ≤ 5ms under **nominal load**, defined as ≤ 200 concurrent `policy_eval` calls/sec, ≥ 80% LRU cache-hit rate, warm cache. The §3.3-R1 circuit breaker threshold of 200ms (40× nominal) covers the cold-start, cache-flood, and overload paths; not in conflict.*

## C.5 (Hassabis) — B.5 chain-integrity failure handling

Auditor service behavior on hash-chain mismatch detection:
1. Affected rows EXCLUDED from `egcr` numerator.
2. `audit.integrity.alert.<event_id>` published on dedicated subject (read by incident-commander OOB).
3. Auditor halts SGD application of θ updates until integrity is reconfirmed by re-hashing from last known-good genesis row.

## C.6 (Hassabis) — B.10 Phase 2 explicit auditor deploy task

Phase 2 prerequisite gating task added (BEFORE loss computation begins):
- ✅ Stand up isolated auditor systemd unit (read-only ledger.db mount, no NATS subscription except chain-alert publish, separate `agentos-auditor.service`).

## C.7 (Hassabis) — B.7 noise-freeze dimensions explicit

The annealed-noise exploration zeroes the gradient on the dimensions corresponding to:
- `θ_evidence_gate_strictness`
- `θ_council_quorum_required`

These dimensions remain at warm-start values for the entire optimization run (≥500 cycles minimum); only re-enabled by explicit council action documented in a follow-up ADR.

## C.8 (Hassabis) — B.8 SPRT formulation pinned

Pin to **classical SPRT** (frequentist log-likelihood ratio):
```
λ_t = log P(data | H₁) - log P(data | H₀)
revert if λ_t ≥ log((1-β)/α) ≈ log(0.95/0.05) = 2.94
continue if λ_t ≤ log(β/(1-α)) ≈ log(0.05/0.95) = -2.94
otherwise: keep observing
```
Minimum 10 cycles before any decision. Drops the Bayesian-posterior phrasing.

---

## Council Status — POST-R2-NITS

| Reviewer | r1 verdict | r2 verdict | Final (after C.1–C.8 nits) |
|---|---|---|---|
| Vogels | APPROVE_W_REVISIONS | APPROVE_W_REVISIONS | **APPROVE** (C.4 closes) |
| Hassabis | APPROVE_W_REVISIONS | APPROVE_W_REVISIONS | **APPROVE** (C.5–C.8 close) |
| Hamilton | REQUEST_RESPIN | APPROVE_W_REVISIONS | **APPROVE** (C.1–C.3 close) |

**ADR-011 r2 + C.1–C.8 nits = COUNCIL-RATIFIED 3-of-3.**

Status changed to **Accepted**. Proceeding to P6b: atomic OKR creation via canonical lifecycle.

