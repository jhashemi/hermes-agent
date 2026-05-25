# Complete Integration: CWSA + Dynamic Event Registry + LLDAP

**Purpose:** Show how all pieces fit together: Worker steering + dynamic events + policy-driven execution

---

## The Full Stack: From Task Dispatch to Outcome Learning

### Step 1: Task Arrives in Kanban

```
Task: "Research ML Optimization Techniques"
├─ OKR: "Q2 2026 Growth"
├─ Goal: "Implement Advanced Scheduling"
├─ Accountable Agent: demis_hassabis (Strategy)
├─ LLDAP DN: cn=demis_hassabis,ou=Research,dc=company,dc=com
└─ Role: executive_agent
```

### Step 2: Dispatcher Runs Dynamic Event Registry (LAYER 1 + EVENT SYSTEM)

```python
# In dispatch_once()
task = get_pending_task()

# CWSA Layer 1: Validate Resources
resource_check = validate_worker_resources(
    profile=task.profile,
    required_toolsets=["hermes-cli", "kanban"]
)
if not resource_check.passed:
    # Event fires: resource_validation_failed
    publish_event("resource_validation_failed", task)
    # Layer 7: Escalate to council
    escalate_to_council(resource_check.failures)
    continue

# Dynamic Event Registry: Compute subscriptions from context
dynamic_registry = DynamicEventRegistry()
event_subscriptions = dynamic_registry.compute_registry_for_task(task)
# Returns: {
#   "on_start": ["council_task_started", "strategy_session_opened", "mcts_initialized"],
#   "on_steering": ["game_theory_solution_computed", "long_term_planning_phase"],
#   "on_success": ["strategy_recorded", "council_consensus_recorded"],
#   "on_failure": ["escalate_to_board", "reverify_assumptions"],
#   "feedback": ["strategic_learnings", "prediction_accuracy_recorded"],
# }

task.event_subscriptions = event_subscriptions
task.event_registry = build_registry_from_subscriptions(event_subscriptions)

# CWSA Layer 3: Embodied State Reflection
worker_state = worker.reflect_on_capabilities()
# Event fires: embodied_state_reflected
publish_event("embodied_state_reflected", worker_state)

# CWSA Layer 4: Recursive Planning
plan = planning_engine.plan_task_execution(task)
task.execution_plan = plan
# Events fire: plan_created, decomposition_complete
publish_event("plan_created", plan)

# CWSA Layer 2: Executive Steering
steering_decision = executive_steering_controller.decide_execution(task, worker)
# Event fires: execution_strategy_decision
publish_event("execution_strategy_decision", steering_decision)

# Spawn worker with all context injected
spawn_worker(task, event_registry, execution_plan, steering_decision)
```

### Step 3: Worker Startup (Resource Validation + State Check)

```python
# Worker process starts
def worker_main(task, event_registry, execution_plan, steering_decision):
    
    # Subscribe to events
    for event_name, handlers in event_registry.items():
        subscribe(event_name, handlers)
    
    # CWSA Layer 1: Pre-execution Resource Validation
    startup_check = WorkerResourceValidator().validate_startup(task.profile, task)
    if not startup_check.passed:
        # Event fires: worker_resource_insufficient
        publish_event("worker_resource_insufficient", startup_check.failures)
        # Layer 7: Signal escalation
        signal_council_escalation(startup_check.failures)
        sys.exit(1)  # Don't execute with insufficient resources
    
    # Event fires: worker_startup_success
    publish_event("worker_startup_success", task)
    
    # CWSA Layer 3: Embodied State Validation
    feasibility = worker_state.validate_task_feasibility(task)
    if not feasibility.feasible:
        # Event fires: task_feasibility_failed
        publish_event("task_feasibility_failed", feasibility.missing_capabilities)
        # Layer 7: Escalate with gaps
        escalate_with_gaps(feasibility.missing_capabilities)
        # But continue — let council decide
    
    # CWSA Layer 4: Execute with contingency paths
    try:
        # Primary path
        result = execute_with_fallbacks(task, execution_plan, steering_decision)
        # Event fires: task_executed_successfully
        publish_event("task_executed_successfully", result)
        
        # CWSA Layer 5+6: Feedback + Learning
        outcome = ExecutionOutcome(task, success=True, result=result)
        
        # Event fires: execution_feedback_recorded
        publish_event("execution_feedback_recorded", outcome)
        
        # Config auto-updates from outcome
        profile_config.update_from_feedback(outcome)
        # Event fires: config_updated_from_feedback
        publish_event("config_updated_from_feedback", profile_config)
        
        # Call kanban_complete (framework hook)
        kanban_complete(task_id=task.id, status="done", result=outcome)
        
    except Exception as e:
        # Failure handling
        outcome = ExecutionOutcome(task, success=False, error=e)
        
        # Event fires: execution_failed
        publish_event("execution_failed", outcome)
        
        # CWSA Layer 4: Try contingency paths
        for contingency in execution_plan.contingency_paths:
            try:
                result = execute_contingency(contingency)
                outcome.success = True
                outcome.used_contingency = contingency
                # Event fires: contingency_succeeded
                publish_event("contingency_succeeded", outcome)
                break
            except:
                pass
        
        if not outcome.success:
            # Event fires: all_paths_exhausted
            publish_event("all_paths_exhausted", outcome)
            
            # CWSA Layer 7: Escalate to council
            council_decision = escalate_to_council(outcome)
            
            # Event fires: council_escalation_resolved
            publish_event("council_escalation_resolved", council_decision)
            
            if council_decision.action == "retry":
                kanban_block(task_id=task.id, reason=council_decision.reason)
            else:
                kanban_complete(task_id=task.id, status=council_decision.status)
```

### Step 4: Events Cascade Up Hierarchy

```python
# Task completes successfully
# Event: task_complete
# Subscriptions (from dynamic registry): ["feedback_recorded", "parent_updated", "metrics_updated"]

# Each subscriber fires:
feedback_system.record_outcome(task, outcome)
parent_goal.update_progress(task.result)
metrics_aggregator.record_task_metrics(task)

# Parent Goal receives update
# Event: goal_progress_updated
# Subscriptions: ["okr_progress_calculated", "council_informed"]

# Parent OKR receives update
# Event: okr_progress_updated
# Subscriptions: ["strategic_update", "board_notification"]

# Events cascade from Task → Goal → OKR → Board
```

### Step 5: Feedback System + Learning (CWSA Layers 5+6)

```python
# ContinuousFeedbackSystem records
outcome = ExecutionOutcome(
    task=task,
    success=True,
    actual_resources_used={
        "cpu_seconds": 15.3,
        "memory_peak_gb": 2.1,
        "network_calls": 23,
        "llm_tokens": 4200,
    },
    execution_time_seconds=45.2,
    confidence_score=0.98,
)

# Compare with declared
declared_resources = task.declared_resources
# {
#   "cpu_seconds": 20,
#   "memory_peak_gb": 4,
#   "network_calls": 30,
#   "llm_tokens": 5000,
# }

gaps = actual_resources_used - declared_resources
# Negative gaps mean we over-allocated (good for safety, bad for efficiency)
# Positive gaps mean we under-allocated (bad, would cause failure)

if any(gap > 0 for gap in gaps.values()):
    # Found resource gap — update config
    profile_config.add_resource_requirement(
        task_type=task.type,
        resource=gaps,
        evidence=outcome
    )
    
    # Verify on 5 similar tasks
    test_results = profile_config.test_on_similar_tasks(
        task_type=task.type,
        num_tests=5
    )
    
    if test_results.success_rate > 0.95:
        # Promote to default config
        profile_config.promote_to_default(
            resource_update=gaps,
            evidence=test_results,
        )
        
        # Event fires: config_promoted_from_learning
        publish_event("config_promoted_from_learning", profile_config)

# Pattern analysis
patterns = feedback_system.analyze_execution_patterns()
# Example: "Tasks of type 'ml_training' always need >= 8GB memory"

if patterns[0].confidence > 0.9:
    # Add proactively to future tasks
    profile_config.add_requirement_proactively(
        task_type=patterns[0].task_type,
        resource=patterns[0].required_resource,
        reason=patterns[0].rationale,
    )
```

### Step 6: LLDAP Policy Evolution

```python
# Over time, feedback system sees patterns in OU=Research
# Tasks in Research need higher memory budget than Services

# Council proposes policy change:
policy_change = PolicyChange(
    ou="OU=Research,dc=company,dc=com",
    attribute="memoryGb",
    old_value=8,
    new_value=16,
    rationale="Research tasks require more memory for experimentation",
    evidence=pattern_analysis,
)

# Council votes:
# - Demis: +1 (Strategic value of research)
# - Werner: 0 (Neutral on resource allocation)
# - Jeff: +1 (More memory = better parallelization)
# - Margaret: +1 (Higher budget = fewer failures)

# Consensus reached → Policy applied
council.apply_policy_change(policy_change)

# LLDAP updated:
# dn: cn=policy-holder-Research,ou=Research,dc=company,dc=com
# memoryGb: 16  # Was 8

# Future tasks in OU=Research automatically get 16GB memory policy
# Dynamic event registry picks it up at dispatch time
```

---

## Complete Data Flow Diagram

```
┌─ Task Arrives ─────────────────────────────────────┐
│                                                    │
├─ dispatch_once() called                           │
│  ├─ CWSA Layer 1: Resource validation              │
│  │  └─ Event: resource_validation_complete         │
│  │                                                  │
│  ├─ Dynamic Event Registry: Compute subscriptions  │
│  │  ├─ Query LLDAP for accountable agent's policies│
│  │  ├─ Query hierarchy level → event routing       │
│  │  ├─ Query agent role → steering events          │
│  │  └─ Event: subscriptions_computed               │
│  │                                                  │
│  ├─ CWSA Layer 3: Embodied state reflection        │
│  │  └─ Event: embodied_state_reflected             │
│  │                                                  │
│  ├─ CWSA Layer 4: Recursive planning               │
│  │  └─ Event: plan_created (with contingencies)    │
│  │                                                  │
│  ├─ CWSA Layer 2: Executive steering (MCTS)        │
│  │  └─ Event: execution_strategy_decision          │
│  │                                                  │
│  └─ spawn_worker(task, registry, plan, decision)   │
│                                                    │
├─ Worker Startup (subprocess)                       │
│  ├─ CWSA Layer 1: Pre-exec resource check          │
│  │  └─ Event: worker_startup_complete              │
│  │                                                  │
│  ├─ CWSA Layer 3: Task feasibility validation      │
│  │  └─ Event: feasibility_assessed                 │
│  │                                                  │
│  ├─ Execute task with event_registry subscriptions │
│  │  └─ Events fire: on_task_started, etc.          │
│  │                                                  │
│  ├─ Task completes successfully                    │
│  │  ├─ CWSA Layer 5: Record execution feedback     │
│  │  │  └─ Event: execution_feedback_recorded       │
│  │  │                                               │
│  │  ├─ CWSA Layer 6: Self-improve config           │
│  │  │  └─ Event: config_updated_from_feedback      │
│  │  │                                               │
│  │  ├─ Call kanban_complete()                      │
│  │  │  └─ Event: task_complete                     │
│  │  │                                               │
│  │  └─ Events cascade to parent Goal               │
│  │     └─ Event: goal_progress_updated             │
│  │        └─ Events cascade to parent OKR          │
│  │           └─ Event: okr_progress_updated        │
│  │                                                  │
│  └─ Continuous Learning System (async)            │
│     ├─ Analyze execution patterns                  │
│     ├─ Detect resource gaps                        │
│     ├─ Test fixes on similar tasks                 │
│     ├─ Promote successful changes to default       │
│     └─ Propose policy updates to council           │
│                                                    │
└─ Council Updates LLDAP Policies                    │
   └─ Future tasks use improved policies             │
```

---

## How This Prevents the May 24 Cascade

### Scenario: Provider Exhausted

**Old Way (May 24):**
1. ✅ Worker starts
2. ❌ Try to call kanban tool → Tool not found (missing toolset)
3. ✅ Worker exits cleanly
4. ❌ Task marked "running" with dead PID
5. ❌ Silent: No one knows it failed
6. ⏳ ~14 hours later: TTL expiry triggers detection

**New Way (With CWSA + Dynamic Registry):**
1. 📋 dispatch_once() runs
2. ✅ Dynamic registry: Compute subscriptions from context
   - Hierarchy: Task level
   - Agent: demis_hassabis (Strategy)
   - LLDAP OU: OU=Research
3. ✅ CWSA Layer 1: Validate resources
   - Check: toolsets = [hermes-cli, kanban] ✅
   - Check: llm provider reachable ❌ (provider exhausted!)
   - Event fires: resource_validation_failed
4. 🚨 CWSA Layer 7: Escalate immediately
   - Send to council: "Provider exhausted"
   - Council suggests: Use fallback provider
5. 🔨 CWSA Layer 2: MCTS evaluates options
   - Path 1: Wait for provider recovery (risky)
   - Path 2: Use fallback provider (safe)
   - Path 3: Defer task until recovery
   - MCTS chooses: Path 2 (fallback)
6. ✅ Worker spawns with fallback provider
   - Events fire: provider_fallback_activated
7. ✅ Task executes with fallback
8. ✅ CWSA Layer 5+6: Record outcome + learn
   - "Provider primary was exhausted"
   - "Fallback worked successfully"
   - Config updates: fallback provider priority increases
9. 📊 LLDAP policy updated by council
   - "On provider failure, auto-activate fallback"

**Result:**
- Old: 14-hour manual recovery
- New: Automatic recovery in 30 seconds
- Learning: Next time, fallback is primary

---

## The 7 CWSA Layers + Dynamic Event Registry

| Layer | Purpose | Events | Policy-Driven |
|-------|---------|--------|---------------|
| **1. Resource Validation** | Pre-exec checks | resource_validation_* | LLDAP: toolset requirements |
| **2. Executive Steering** | MCTS decisions | execution_strategy_decision | Agent-specific steering |
| **3. Embodied State** | Self-model | embodied_state_reflected | Role-based capabilities |
| **4. Recursive Planning** | Contingencies | plan_created | Hierarchy-based decomposition |
| **5. Feedback** | Outcome record | execution_feedback_recorded | Role-based feedback types |
| **6. Self-Improve** | Config update | config_updated_from_feedback | Policy-promoted changes |
| **7. Council** | Escalation | council_escalation_resolved | LLDAP policy governance |

---

## Status

✅ **Architecture Complete**
- CWSA: 7 layers designed + roadmap
- Dynamic Event Registry: Policy-driven subscriptions
- LLDAP Integration: Hierarchy + inheritance + overrides
- Full integration: Task dispatch → execution → learning → policy evolution

🚀 **Ready for Implementation**
- Week 1: Foundation (validation + event routing)
- Week 2-3: Steering + LLDAP integration
- Week 4-5: Learning + council integration

**Next Step:** Begin Week 1 implementation of resource validation + event registry foundation.
