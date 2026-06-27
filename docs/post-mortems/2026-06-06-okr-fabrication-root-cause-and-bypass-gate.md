# Post-mortem: OKR fabrication root cause + 1,647 stuck-task accountability gap

**Date:** 2026-06-06
**Author:** jeff_dean (SRE) + hermes-agent forensics
**Severity:** P0 — governance integrity compromised
**Related:** `2026-06-06-okr-completion-audit-24h.md`, `2026-06-06-skill-install-whatsapp-miss-and-stale-gateway.md`

## Executive summary

Two independent defects collided to produce a **system that emits fraudulent governance artifacts at machine speed without anyone knowing**:

1. **Bypass-the-gate write path** in `okr_kr_reconciler.py` — sole writer of `okr_key_results.status` from the kanban side. It uses bare `UPDATE okr_key_results SET status=?` and skips `OKRAccountabilitySystem.update_key_result()` (the only place where the evidence gate lives at line 642 of `okr_accountability.py`). A kanban task marked `done` propagates straight through to OKR `done` — no evidence required.

2. **State-replay-after-lock-release** in `event_emitter_omnibus.py` — when DuckDB lock contention prevents the diff emitter from reading `okr_accountability.db` for 5+ minutes, the next successful tick emits **all accumulated state changes in a single sub-second burst**, producing a thunderclap of 200+ NATS events with zero headers, zero idempotency tokens, and zero provenance.

Combined effect: bad rows are written by Defect 1, then propagated to NATS by Defect 2 in a forensically-misleading burst that looks like a coordinated attack but is actually two innocent-looking subsystems failing safely.

## Forensic timeline (2026-06-05 UTC)

| Time | Event | Source |
|---|---|---|
| 14:41 | `okr-steering-reactor` (PID **1472508**) starts; opens long-lived `duckdb.connect()` to OKR DB. Holds **exclusive write lock** for the entire run. | systemctl + journal |
| 14:41–20:08 | Steering reactor runs deliberation cycles for ~5h31m. During this window, `okr_kr_reconciler` queues 200+ kanban-side `done` writes that block on the lock. | inferred |
| 20:08:04 | `event_emitter_omnibus` first logs `Conflicting lock is held in /usr/bin/python3.12 (PID 1472508)`. Lock-holder is the steering reactor. | journald |
| 20:08:09 | Steering reactor logs `Did you mean "okr_goals"?` — DuckDB suggesting an alternate table. Indicates query was running on the held connection. | journald |
| 20:08–20:12 | Omnibus emitter retry-loops every 5s, logging the same lock contention error 50+ times. Neither side gives up. | journald |
| 20:12:17 | **Steering reactor stops.** `ExecMainStopTimestamp=Fri 2026-06-05 20:12:17 UTC` — systemd terminates the process. Lock released. | systemd unit state |
| 20:12:18–20:12:35 | Reconciler-and-friends batch processes its backlog of writes. Many are `UPDATE okr_key_results SET status='done'` — **bypass evidence gate**. 72 KRs flip to `done`. | DB write log inferred |
| 20:12:35.172 → 20:12:35.545 | Omnibus emitter's first successful tick computes diff (`okr_diff(state)` line 132–208 of `event_emitter_omnibus.py`), finds 221 changed rows, and emits **221 NATS events in 372 ms** (sustained ~593 events/sec) on subjects `okr.kr.{done,in_progress,blocked,cancelled,...}`, `okr.goal.active`, `okr.plan.active`, `okr.objective.active`, `okr.signoff.added`. Zero headers on any of them. | NATS forensic scan |
| 20:13:03 | Trailing 10 events at `:13:03` — second omnibus tick catches stragglers. | NATS scan |
| 20:22:03 | omnibus restarts under new PID. | systemd |
| 20:23:09 | steering-reactor restarts under PID 1674173. | systemd |

## Root causes

### Defect 1 — Reconciler bypasses evidence gate (governance integrity)

**File:** `~/.hermes/scripts/okr_kr_reconciler.py`
**Mechanism:** The reconciler subscribes to `kanban.task.>` events. When a kanban task with `idempotency_key='kr:<kr_id>'` flips to `done`, the reconciler writes `UPDATE okr_key_results SET status='done' WHERE id=?` directly via `duckdb.connect(OKR_DB)` (write lock).

**Why it's wrong:** The canonical write path is `OKRAccountabilitySystem.update_key_result()` (line 642 of `okr_accountability.py`), which **refuses `done` without an `evidence` string** and emits `OKR_EVIDENCE_REQUIRED` on rejection. The reconciler skips this entirely.

**Quantitative impact:** 78% ground-truth-fail rate on 9 sampled completions from the burst (reference: `2026-06-06-okr-completion-audit-24h.md`).

### Defect 2 — Bulk-publish without provenance discipline (forensic invisibility)

**File:** `~/.hermes/scripts/event_emitter_omnibus.py` line 270 + 274 + 278 + 282 + 286
**Mechanism:** `await E.publish(js, subj, d)` is called with the row dict as payload. No `Nats-Msg-Id` header, no `publisher` header, no idempotency token. The DDL at line 14 *claims* dedupe via `idempotency_key + LRU dedupe`, but the publish call site never sets one.

**Why it's wrong:** Provenance-free events make forensic correlation impossible. The 221-event burst showed up as a thunderclap with no way to attribute the cause without journald cross-referencing.

**Quantitative impact:** All 232 events in the 60-second burst window had `<NO HEADERS>` (100% provenance loss).

### Defect 3 — Long-lived DuckDB connection on steering reactor (lock contention amplifier)

**File:** `~/.hermes/scripts/okr_steering_reactor.py` (and the underlying `OKRAccountabilitySystem.__init__`)
**Mechanism:** `OKRAccountabilitySystem` opens a single `duckdb.connect()` and holds it for the lifetime of the process. DuckDB enforces single-writer locking. While the reactor runs deliberation/consensus cycles (multi-second), all other writers (reconciler, audit findings script, omnibus's read attempts) are starved.

**Why it's wrong:** No connection pooling. No write-lock-release between deliberation steps. A 5-hour reactor uptime = 5 hours of intermittent write contention.

**Quantitative impact:** The 5-minute lock window on 2026-06-05 (20:08–20:12) caused the burst. This will recur every time the steering reactor runs a long cycle.

## Why three defects compounded

1. Reconciler writes bad data (Defect 1).
2. Steering reactor's long-held lock prevents omnibus from observing it in real time (Defect 3).
3. When the lock releases, omnibus emits everything-at-once with no provenance (Defect 2).
4. Watchdog (`okr_raci_watchdog.py`) reads `rc=0` from `hermes kanban dispatch` and reports "Actions: dispatch" even when 0 tasks were spawned, hiding the breakage from the alerting path (separate watchdog defect, see watchdog audit summary).

## Fixes (proposed; not yet applied)

| ID | Defect | Fix | Effort |
|---|---|---|---|
| F1 | Reconciler bypass | Replace direct UPDATE with `OKRAccountabilitySystem.update_key_result()` call. Or: add a SQL trigger on `okr_key_results` that rejects `status=done` without `evidence != ''`. | 30 min + tests |
| F2 | Omnibus headers | Add `Nats-Msg-Id` (= sha256 of `(kr_id, status, current_value)`), `publisher` (= `event-emitter-omnibus@hermes2`), `replay` flag (true if backlog > 5s) to all `E.publish()` calls. | 15 min |
| F3 | Steering reactor lock | Switch `OKRAccountabilitySystem` to context-managed connections (`with duckdb.connect(OKR_DB) as con:`) per write batch. Or: use SQLite WAL for OKR DB. | 1–2 hr |
| F4 | Watchdog silent success | Parse `Spawned: N` from dispatch output; only append `actions.append("dispatch")` when N > 0. Otherwise increment a `zero_throughput_streak` and escalate at threshold. | 20 min |
| F5 | Quarantine bad rows | Reverse-walk `okr_audit` for the 20:12:18–20:12:35 window; identify all `okr_key_results.status` writes without preceding evidence; mark those KRs as `unverified_done` pending re-validation. | 1 hr SQL + verification |

## Lessons

1. **Single-source-of-truth means single write path.** Two write paths to the same table is two governance regimes; one will inevitably skip enforcement.
2. **Provenance discipline is non-optional.** Every event published to a JetStream stream that's used for governance MUST carry `Nats-Msg-Id` + `publisher`. Without those headers, forensics requires journald correlation across multiple services — fragile and slow.
3. **DuckDB single-writer locking is a system-design constraint, not an implementation detail.** Long-held connections amplify lock contention into multi-minute outages that look like state-explosion bursts.
4. **Watchdogs that report success on no-op are worse than no watchdog.** False-positive remediation hides the true failure mode.
5. **Network-effect-aware forensics works.** Cross-stream + cross-machine + journald correlation pinned the publisher and mechanism in one ~10-minute investigation. Single-stream analysis would have stopped at "72 fraudulent completions" and missed the bypass-the-gate root cause.

## Next steps (separate work items)

- [ ] F1 ship + tests
- [ ] F2 ship
- [ ] F3 design + ship
- [ ] F4 ship + retest watchdog
- [ ] F5 quarantine + re-verify
- [ ] Council review of ADR-011 (cluster governance contract — separate doc)
- [ ] L4 policy store proof-of-concept (see L4 research note)
