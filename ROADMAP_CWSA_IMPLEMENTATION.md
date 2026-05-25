# Roadmap: From Symptomatic Fixes to Root Architectural Solution

**Current State:** May 25 Symptomatic Fixes (Silent Crash Detection + MCP Prevention)  
**Target State:** Full Comprehensive Worker Steering Architecture (CWSA)  
**Timeline:** 5 weeks to production  
**Vision:** Workers with full cognitive stack, executive steering, continuous learning, recursive self-improvement

---

## The Journey: Why This Progression?

### What We Did (May 24-25)
**Fixed the immediate crisis:**
- ✅ Silent crash detection (6x faster detection)
- ✅ Profile validation (fail-fast at startup)
- ✅ MCP auto-recovery (self-healing)

**Result:** Stopped the cascade, prevented 14-hour manual recovery

**What Was Left Unsolved:**
- ❌ Workers still have minimum viable config
- ❌ No proactive resource checks before execution
- ❌ No cognitive steering (just reactive execution)
- ❌ No learning from failures (static config)
- ❌ No embodied awareness of capabilities
- ❌ No recursive self-improvement

**Analogy:** Fixed the bleeding (crisis management). Now need to fix the disease (architecture).

---

## 5-Week Roadmap to Full CWSA

### Week 1: Layer 1 — Worker Resource Validation

**Goal:** Pre-execution validation; fail-fast if resources missing

**Deliverables:**
- `WorkerResourceValidator` class
- Pre-dispatch checks: toolsets, MCPs, providers, env vars
- Escalation path for missing resources
- Error messages with remediation

**Files:**
- `hermes_cli/worker_validation.py` (200 LOC)
- `.hermes/startup_hooks/worker_resource_check.py` (150 LOC)

**Integration Point:**
```python
# In dispatch_once()
result.validation = validate_worker_resources(profile, task)
if not result.validation.passed:
    # Escalate or error
    escalate_to_council(result.validation.failures)
```

**Testing:**
- TDD: Test missing toolset, missing MCP, missing env var scenarios
- Verify fail-fast behavior
- Verify error messages are actionable

**Deployment:**
- Feature flag: `WORKER_RESOURCE_VALIDATION=true`
- Monitor: % of tasks blocked by validation
- Alert: If validation blocks >5% of tasks

---

### Week 2: Layer 3 — Embodied Worker State Reflection

**Goal:** Workers understand their own capabilities; validate feasibility before execution

**Why Week 2 (not Week 2-3)?**
- Depends on Layer 1 (resource checks)
- Enables Layer 2 (steering decisions need state model)

**Deliverables:**
- `WorkerCapabilityModel` class (introspection)
- `TaskFeasibilityAssessment` (can I execute this?)
- Real-time capability checks
- Continuous health monitoring

**Files:**
- `hermes_cli/embodied_worker_state.py` (300 LOC)
- `hermes_cli/feasibility_assessment.py` (150 LOC)

**Integration:**
```python
# Before task execution
worker_state = worker.reflect_on_capabilities()
feasibility = worker_state.validate_task_feasibility(task)

if not feasibility.feasible:
    # Report gaps + remediation
    escalate_with_gaps(feasibility.missing_capabilities)
```

**Key Metrics:**
- Feasibility prediction accuracy >95%
- False positives <2%
- Latency <100ms

**Testing:**
- Unit tests: Capability introspection
- Integration: Feasibility predictions on known tasks
- E2E: Workers with intentionally limited toolsets

---

### Week 2-3 (Parallel): Layer 2 — Executive Steering via MCTS

**Goal:** Strategic decisions about execution path, resource allocation, escalation

**Deliverables:**
- `ExecutiveSteeringController` (MCTS engine)
- `MCTSNode` (game tree nodes)
- `ExecutionStrategySpace` (possible actions: execute, defer, escalate, remediate)
- `OutcomePredictor` (estimate success of each path)

**Files:**
- `hermes_cli/executive_steering.py` (400 LOC)
- `hermes_cli/mcts_engine.py` (300 LOC)
- `hermes_cli/game_tree.py` (200 LOC)

**Integration:**
```python
# In dispatch_once()
controller = ExecutiveSteeringController()
decision = controller.decide_execution(task, worker)
# decision: Execute now | Validate + retry | Escalate | Defer

match decision.action:
    case ExecutionAction.EXECUTE:
        spawn_worker(task)
    case ExecutionAction.ESCALATE:
        escalate_to_council(task, reason=decision.reasoning)
    case ExecutionAction.DEFER:
        reschedule(task)
```

**Key Insight:**
- MCTS learns from outcomes (feeds into Layer 5/6)
- Decisions get better over time
- Executive council consensus validates paths

**Testing:**
- Simulate resource constraints
- Verify MCTS explores multiple paths
- Validate decisions improve with training

---

### Week 3-4 (Parallel): Layer 5 & 6 — Continuous Learning & Self-Improvement

**Goal:** Configuration improves based on execution feedback

**Why Parallel?**
- Layer 5 (feedback collection) enables Layer 6 (config update)
- Both are data-driven, not dependent on earlier layers

**Deliverables:**
- `ContinuousFeedbackSystem` (outcome recording)
- `SelfImprovingProfileConfig` (config auto-update)
- Pattern learning engine
- Feedback event stream (NATS pub/sub)

**Files:**
- `hermes_cli/execution_feedback.py` (250 LOC)
- `hermes_cli/profile_auto_update.py` (300 LOC)
- `hermes_cli/pattern_learning.py` (200 LOC)

**Integration:**
```python
# Task completes
outcome = ExecutionOutcome(task, success, resource_usage, errors)

# Publish feedback event
feedback_system.record_outcome(outcome)

# Async: Config updates from patterns
profile_config.update_from_feedback(outcome)
```

**Key Loop:**
1. Task executes → Records resource usage
2. If actual > declared, flag gap
3. Run verification tests (5x similar tasks)
4. If success >95%, promote to default
5. Next similar task benefits

**Testing:**
- Simulate missing resources in early attempts
- Verify config updates after 5 successful runs
- Validate promoted changes work on holdout set

---

### Week 4: Layer 4 — Recursive Planning & Reasoning

**Goal:** Hierarchical task decomposition with contingency planning

**Deliverables:**
- `RecursivePlanningEngine` (5-level decomposition)
- `HierarchicalExecutionPlan` (multi-level plans)
- `ContingencyPathGenerator` (rollback paths)
- `SuccessProbabilityEstimator`

**Files:**
- `hermes_cli/recursive_planning.py` (400 LOC)
- `hermes_cli/contingency_planning.py` (250 LOC)

**Integration:**
```python
# Before execution
plan = planning_engine.plan_task_execution(task)

# Multi-level: [Task] → [Subtasks] → [Sub-subtasks] → ...
# Each level: validate + estimate success + generate alternatives

# Store plan as part of task context
task.execution_plan = plan
task.success_estimate = plan.estimate_total_success_probability()
```

**Key Benefit:**
- If subtask fails, don't cascade → switch to contingency
- Confidence estimation prevents risky execution
- Rollback paths documented

---

### Week 5: Layer 7 — Executive Council Escalation + Integration

**Goal:** Complex decisions elevated to executive agents; all layers working together

**Deliverables:**
- `ExecutiveCouncilEscalation` (escalation logic)
- `CouncilDecisionEngine` (consensus voting)
- Integration of all 7 layers
- End-to-end testing

**Files:**
- `hermes_cli/executive_council.py` (300 LOC)

**Integration:**
```python
# High-level dispatch loop
def dispatch_once_v2(conn):
    result = DispatchResult()
    
    # Layer 1: Resource validation
    resource_check = validate_worker_resources(profile)
    if not resource_check.passed:
        # Layer 7: Escalate to council
        council_decision = escalate_to_council(resource_check.failures)
        result.council_escalations.append(council_decision)
        return result
    
    # Layer 3: Embodied state reflection
    feasibility = worker.assess_feasibility(task)
    if not feasibility.feasible:
        # Layer 7: Executive council decides strategy
        decision = executive_council.decide_on_gaps(feasibility.gaps)
        result.strategic_decisions.append(decision)
    
    # Layer 2: Executive steering
    execution_plan = steering_controller.decide_execution(task, worker)
    
    # Layer 4: Recursive planning
    detailed_plan = planning_engine.plan_execution(task)
    
    # Layer 5+6: Continuous learning (async)
    publish_planning_event(detailed_plan)
    
    # Execute based on all layers
    if execution_plan.action == ExecutionAction.EXECUTE:
        spawn_worker(task, plan=detailed_plan)
    
    return result
```

**Testing:**
- E2E: Resource missing → Escalated → Council decides → Config updates
- Verify all 7 layers integrated correctly
- Chaos testing: Simulate various failures

---

## Parallel Workstreams

### Workstream A: Core Layers (Weeks 1-4)
- Implement Layers 1-6 sequentially
- Each layer independently tested
- Feature flags allow staged rollout

### Workstream B: Executive Council
- Week 2-5: Wire up council agent delegation
- Testing: Validate consensus voting
- Integration: Council decisions propagated to config

### Workstream C: Monitoring & Observability
- Continuous: Instrument each layer
- Dashboards: Resource validation success rate, escalation rate, config update frequency
- Alerts: If validation blocks >5%, if escalations >2/hour

### Workstream D: Documentation & Training
- Continuous: Update CWSA docs
- Week 5: Operator training + runbooks
- Post-launch: Capture lessons, update prevention guidelines

---

## Feature Flags & Rollout Strategy

### Phase 1: Shadow Mode (Week 1-2)
```yaml
WORKER_RESOURCE_VALIDATION: "shadow"  # Log but don't block
EMBODIED_STATE_REFLECTION: "shadow"   # Compute but don't use
EXECUTIVE_STEERING: "shadow"          # Suggest but don't enforce
```

### Phase 2: Opt-In (Week 3-4)
```yaml
WORKER_RESOURCE_VALIDATION: "opt_in"     # Profiles can enable
EMBODIED_STATE_REFLECTION: "opt_in"
EXECUTIVE_STEERING: "opt_in"
CONTINUOUS_LEARNING: "always"  # Safe to always-on
```

### Phase 3: Default On (Week 5)
```yaml
WORKER_RESOURCE_VALIDATION: "always"
EMBODIED_STATE_REFLECTION: "always"
EXECUTIVE_STEERING: "always"
RECURSIVE_PLANNING: "always"
EXECUTIVE_COUNCIL_ESCALATION: "always"
```

---

## Success Metrics

### Layer 1 (Resource Validation)
- ✅ 98% of tasks pass validation
- ✅ <0.5s validation latency
- ✅ 100% of missing resources caught before execution

### Layer 2 (Executive Steering)
- ✅ MCTS explores ≥3 paths per decision
- ✅ Steering recommendations adopted ≥95%
- ✅ Decisions improve over time (trending accuracy)

### Layer 3 (Embodied State)
- ✅ Feasibility prediction accuracy >95%
- ✅ False positive rate <2%
- ✅ Latency <100ms

### Layer 4 (Recursive Planning)
- ✅ Plans generated in <1s
- ✅ Contingency paths reduce cascade risk to <1%
- ✅ Success probability estimates accurate >90%

### Layer 5+6 (Learning)
- ✅ Config auto-updates >50% of issues within 1 week
- ✅ Promotion criteria (95% success on 5 tests) validated
- ✅ No config regressions from auto-updates

### Layer 7 (Executive Council)
- ✅ Council escalations <2% of tasks
- ✅ Council decisions accurate >90%
- ✅ Consensus achieved >95% of time

### Overall (CWSA)
- ✅ Silent crash rate: 5% → <0.1%
- ✅ Cascading failures: ~20% → <1%
- ✅ Task success rate: 89% → 99%
- ✅ Mean time to resolution: 14h → 30s

---

## Risk Mitigation

### Risk 1: MCTS Complexity Slows Down Decisions
- **Mitigation:** Time-box MCTS to 100ms; use cached decisions
- **Fallback:** Disable steering, use deterministic policy

### Risk 2: Self-Improving Config Causes Regressions
- **Mitigation:** Always require 5 successful tests before promotion
- **Fallback:** Manual review gate for config changes >0.1MB

### Risk 3: Executive Council Consensus Takes Too Long
- **Mitigation:** Time-box deliberation to 5s; use voting threshold
- **Fallback:** Default safe action (defer task)

### Risk 4: Recursive Planning Creates Infinite Loops
- **Mitigation:** Hard depth limit (5 levels max)
- **Fallback:** Flatten to 2-level plan if timeout

---

## Success Criteria for Production Readiness

- [ ] All 7 layers implemented and unit-tested
- [ ] E2E integration tests pass 100%
- [ ] No config regressions in week-long staging
- [ ] Mean time to escalation <30s (vs. 14h before)
- [ ] Silent crash rate <0.1% (vs. 5% before)
- [ ] Cascading failures <1% (vs. 20% before)
- [ ] Operator confidence: "System now proactively handles issues"
- [ ] Executive council consensus on architecture

---

## Long-Term Vision (Post-CWSA)

### Beyond Week 5: Continuous Evolution
- **Adaptive MCTS:** Learn policy from historical decisions
- **Federated Learning:** Config improvements shared across orgs
- **Predictive Provisioning:** Pre-allocate resources for predicted high-load tasks
- **Causality Learning:** Understand which changes cause which failures
- **Recursive Improvement Loop:** Higher-order learning on the learning system itself

---

## Conclusion

**Current:** Symptomatic fixes (detection + prevention)  
**Target:** Root architectural solution (full cognitive steering stack)

**Progression:**
1. Week 1: Workers validate resources before executing
2. Week 2: Workers understand their own capabilities
3. Week 2-3: Executive agents steer execution decisions
4. Week 3-4: System learns from failures, auto-improves config
5. Week 4: Recursive planning handles contingencies
6. Week 5: Executive council makes strategic decisions
7. Week 5+: All layers working together, cascades prevented, silent crashes <0.1%

**Result:** From reactive crisis management → Proactive, self-aware, continuously-improving executive system.

**Target Date:** June 1, 2026 (production deployment)
