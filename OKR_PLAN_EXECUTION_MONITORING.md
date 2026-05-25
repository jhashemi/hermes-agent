# OKR: Automated Plan Execution Monitoring with Dynamic Cron Lifecycle

**Objective:** Implement automated cron monitoring system that dynamically creates monitors for active dispatched plans and automatically tears down monitors upon plan completion and sign-off  
**Accountable:** jeff_dean  
**Timeline:** May 26 - June 5, 2026 (11 days)  
**Status:** in_progress  

---

## Strategic Context

Current state: Plans execute autonomously but lack real-time monitoring. Manual intervention needed to track progress and clean up monitoring infrastructure.

Target state: Fully automated lifecycle management where cron monitors are dynamically created when plans are dispatched, actively monitor progress, and self-destruct upon plan completion and sign-off.

---

## Key Results

### KR1: Dynamic Cron Provisioning (35 hours)
**Definition:** Automatically create cron monitors when plans transition to "dispatched" status  
**Acceptance Criteria:**
- [ ] Plan status change detector (watches for "proposed" → "dispatched")
- [ ] Cron template engine (generates monitor script for each plan)
- [ ] NATS event-driven provisioning (publish plan:dispatched → subscribe create_cron)
- [ ] Cron naming convention: `plan-monitor-{plan_id}` (globally unique)
- [ ] Metadata stored: plan_id, goal_id, okr_id, dispatch_time, accountable_agent
- [ ] Tests: 10+ plan dispatch scenarios

**Success Metric:** 100% of dispatched plans have active cron monitors within 30 seconds of dispatch  
**Owner:** jeff_dean  

### KR2: Real-Time Monitoring & Observability (40 hours)
**Definition:** Cron monitors actively track plan execution, task progress, and agent health  
**Acceptance Criteria:**
- [ ] Metrics collected: Task completion %, blocked tasks, agent health, runtime
- [ ] Alerts on anomalies: >50% tasks blocked, agent crash, deadline at risk
- [ ] Event emission: plan:progress_checkpoint (hourly), plan:risk_detected (on alert)
- [ ] Dashboard: Real-time plan execution view (% done, ETA, risk level)
- [ ] SLA tracking: Plan vs. Goal horizon deadlines
- [ ] Tests: 15+ monitoring scenarios (normal, degraded, failure)

**Success Metric:** <5 second detection of anomalies, 100% alert delivery  
**Owner:** jeff_dean  

### KR3: Intelligent Teardown & Cleanup (30 hours)
**Definition:** Automatically tear down cron monitors upon plan completion + sign-off  
**Acceptance Criteria:**
- [ ] Completion detector: Monitors plan status → "completed" or "accepted"
- [ ] Sign-off validator: Confirms accountable agent has signed off (Reviewed + Approved)
- [ ] Pre-deletion checklist: Verify all tasks done, audit trail complete, metrics archived
- [ ] Cron deletion: `hermes cron remove plan-monitor-{plan_id}`
- [ ] Cleanup: Archive metrics, delete temp files, log completion
- [ ] Idempotency: Safe to call teardown multiple times (no errors)
- [ ] Tests: 10+ completion + sign-off scenarios

**Success Metric:** 100% cron monitors deleted within 5 minutes of sign-off, zero orphaned monitors  
**Owner:** jeff_dean  

### KR4: Disaster Recovery & Resilience (25 hours)
**Definition:** Handle edge cases, failures, and recovery gracefully  
**Acceptance Criteria:**
- [ ] Orphaned monitor detection: Find monitors for completed/deleted plans
- [ ] Recovery mechanism: Auto-cleanup orphaned monitors (daily sweep)
- [ ] Monitor crash recovery: Restart failed cron, alert if unrecoverable
- [ ] Plan deletion handling: Cleanup cron if plan is deleted
- [ ] Database corruption: Reconcile plan ↔ cron state, fix mismatches
- [ ] Tests: 15+ failure scenarios (agent crash, cron failure, plan deletion)

**Success Metric:** Zero orphaned monitors, 99.9% recovery success rate  
**Owner:** jeff_dean  

---

## Implementation Plan

### Phase 1: Provisioning Engine (May 26-28)
**Deliverables:**
1. Plan status detector (watches OKR bridge)
2. Cron template generator
3. NATS event publisher (plan:dispatched)
4. Cron creation handler
5. Testing (10+ scenarios)

**Metrics:**
- Provisioning latency: <30s per plan
- Success rate: 100%
- Template accuracy: 100%

### Phase 2: Monitoring System (May 29-31)
**Deliverables:**
1. Metrics collector (plan progress, task status)
2. Anomaly detector (risk scoring)
3. Alert engine (threshold-based)
4. Event emitter (progress checkpoints)
5. Dashboard queries
6. Testing (15+ scenarios)

**Metrics:**
- Anomaly detection latency: <5 seconds
- Alert accuracy: >95%
- Dashboard response: <500ms

### Phase 3: Teardown & Cleanup (June 1-3)
**Deliverables:**
1. Completion detector
2. Sign-off validator
3. Pre-deletion checklist
4. Cron deletion handler
5. Metrics archival
6. Testing (10+ scenarios)

**Metrics:**
- Teardown latency: <5 minutes after sign-off
- Success rate: 100%
- Zero orphaned monitors

### Phase 4: Resilience & Recovery (June 4-5)
**Deliverables:**
1. Orphaned monitor detector
2. Daily cleanup sweep
3. Crash recovery mechanism
4. Plan deletion handler
5. State reconciliation
6. Testing (15+ scenarios)

**Metrics:**
- Orphaned monitor count: 0
- Recovery success rate: 99.9%
- Mean time to recovery: <10 minutes

---

## Technical Architecture

### Event Flow

```
Plan dispatched
    ↓ (event: plan:dispatched)
Cron provisioning
    ↓ (creates plan-monitor-{plan_id})
Cron executes every 5 minutes
    ├─ Check plan status
    ├─ Collect metrics
    ├─ Detect anomalies
    ├─ Emit alerts
    └─ Update dashboard
    
Plan completes + signed off
    ↓ (event: plan:accepted)
Completion detector
    ├─ Verify sign-off
    ├─ Archive metrics
    └─ Delete cron
```

### Cron Monitor Template

```bash
#!/bin/bash
# Cron monitor for plan {PLAN_ID}
# Created: {TIMESTAMP}
# Plan: {PLAN_NAME}
# Goal: {GOAL_ID}
# OKR: {OKR_ID}

PLAN_ID="{PLAN_ID}"
INTERVAL=300  # 5 minutes

while true; do
  # Get plan status
  STATUS=$(hermes okr plan show $PLAN_ID --status)
  
  # Collect metrics
  TASKS_DONE=$(hermes kanban list --plan $PLAN_ID --status done | wc -l)
  TASKS_BLOCKED=$(hermes kanban list --plan $PLAN_ID --status blocked | wc -l)
  
  # Detect anomalies
  if [ $TASKS_BLOCKED -gt 10 ]; then
    hermes send alert "Plan $PLAN_ID: $TASKS_BLOCKED tasks blocked"
    hermes emit event "plan:risk_detected" "{plan_id: $PLAN_ID, risk: blocked_tasks, count: $TASKS_BLOCKED}"
  fi
  
  # Check for completion
  if [ "$STATUS" = "accepted" ]; then
    hermes emit event "plan:accepted" "{plan_id: $PLAN_ID, timestamp: $(date -Iseconds)}"
    hermes cron remove plan-monitor-$PLAN_ID
    exit 0
  fi
  
  # Sleep until next check
  sleep $INTERVAL
done
```

### State Machine

```
Plan States:
  proposed → validated → pending_signoff → approved → dispatched
                                                          ↓
  (Cron provisioned here)              (Cron deletes here)
                                                          ↓
                          → in_progress → completed → in_review
                          ↓
                    (Cron monitoring)              (Cron active)
                                                          ↓
                                    → accepted → archived
                                    (Cron deleted)
```

---

## Metrics & KPIs

### Primary Metrics
- **Provisioning Latency:** <30 seconds per plan
- **Monitoring Coverage:** 100% of dispatched plans
- **Anomaly Detection:** <5 seconds
- **Alert Accuracy:** >95%
- **Teardown Success:** 100% within 5 minutes
- **Orphaned Monitors:** 0
- **Recovery Rate:** 99.9%

### Secondary Metrics
- **Cron Uptime:** 99.99%
- **Dashboard Response:** <500ms
- **Metrics Accuracy:** >99%
- **Storage Efficiency:** Metrics archived, temp files cleaned

---

## Success Criteria

**By June 5, 2026:**

- ✅ All active plans have cron monitors
- ✅ Monitors track progress in real-time
- ✅ Anomalies detected within 5 seconds
- ✅ Cron auto-deletes upon sign-off
- ✅ Zero orphaned monitors
- ✅ 99.9% recovery from failures
- ✅ Full integration with OKR lifecycle

---

## Risk Mitigation

| Risk | Probability | Mitigation |
|------|-------------|-----------|
| Monitor provisioning delay | Low | NATS event-driven, <30s SLA |
| Monitor failure | Low | Health checks, crash recovery |
| Incomplete sign-off detection | Medium | Validator checks all conditions |
| Orphaned monitors | Low | Daily sweep + recovery |
| Metrics storage overflow | Low | Archive old metrics, compress |

---

## Timeline

```
May 26-28 (Phase 1: Provisioning)
├─ Status detector
├─ Template engine
├─ NATS event wiring
└─ Cron creation

May 29-31 (Phase 2: Monitoring)
├─ Metrics collector
├─ Anomaly detector
├─ Alert engine
└─ Dashboard

June 1-3 (Phase 3: Teardown)
├─ Completion detector
├─ Sign-off validator
├─ Checklist verification
└─ Cleanup

June 4-5 (Phase 4: Resilience)
├─ Orphaned detector
├─ Recovery mechanism
├─ Daily sweep
└─ Full testing
```

---

## Deliverables

### Code
- `plan_cron_provisioner.py` - Provisions monitors
- `plan_cron_monitor.sh` - Monitor template (bash)
- `plan_completion_detector.py` - Detects completion
- `plan_cron_cleanup.py` - Tears down monitors
- `plan_orphan_recovery.py` - Recovery mechanism
- Tests: 50+ test scenarios

### Documentation
- `PLAN_CRON_LIFECYCLE.md` (architecture)
- `PLAN_MONITORING_GUIDE.md` (operations)
- `PLAN_CRON_RECOVERY.md` (disaster recovery)
- Runbooks (manual intervention guide)

### Metrics
- Provisioning SLA monitoring
- Anomaly detection metrics
- Teardown tracking
- Recovery success rates

---

**OKR Created:** May 25, 2026, 10:00 UTC  
**Accountable:** jeff_dean  
**Status:** in_progress  
**Target Completion:** June 5, 2026  
**Confidence:** 85% (requires tight NATS event integration)

---

**Next Steps:**
1. jeff_dean reviews this OKR
2. Breakdown into 16 kanban tasks (phased by May 26)
3. Begin Phase 1 provisioning engine
4. Daily standups May 26 - June 5
5. Integration with existing plan lifecycle system
