# Policy-Driven Dynamic Event Registry Architecture

**Problem:** CWSA layers need event subscriptions, but these can't be hardcoded. They must adapt based on:
- Execution hierarchy position (OKR → Goal → Task → Subtask)
- Accountable agent (Demis, Werner, Jeff, Margaret)
- LLDAP OU and inherited policies
- Role-based subscriptions (kanban_worker, embodied_agent, executive_agent)

**Solution:** Policy-driven registry that computes subscriptions dynamically at dispatch time

---

## The Architecture

### Layer 1: Policy Evaluation Engine

```python
class DynamicEventRegistryPolicy:
    """
    Compute event subscriptions based on execution context.
    
    Input: Task, Accountable Agent, LLDAP OU, Role
    Output: Event subscriptions needed for this execution context
    """
    
    def compute_subscriptions(self, context: ExecutionContext) -> EventSubscriptionSet:
        """
        Dynamic subscription computation:
        
        1. Query LLDAP for policies on accountable agent's OU
        2. Query hierarchy to determine nesting level
        3. Query role to determine capability requirements
        4. Merge all applicable policies
        5. Return computed event subscriptions
        """
        
        # Get policies from LLDAP (inherited from parent OUs)
        lldap_policies = self.lldap_client.get_inherited_policies(
            user_dn=context.accountable_agent.dn,
            ou=context.execution_ou
        )
        
        # Get hierarchy-based policies
        hierarchy_policies = self.get_hierarchy_policies(
            level=context.hierarchy_level,  # OKR, Goal, Task, Subtask
            path=context.hierarchy_path
        )
        
        # Get role-based policies
        role_policies = self.get_role_policies(
            role=context.task.role,
            agent_type=context.accountable_agent.agent_type
        )
        
        # Merge all policies (union of subscriptions)
        merged = self.merge_policies(
            lldap_policies,
            hierarchy_policies,
            role_policies
        )
        
        # Resolve to actual event subscriptions
        subscriptions = self.resolve_to_subscriptions(merged)
        
        return subscriptions
```

### Layer 2: Hierarchy-Based Event Routing

```python
class HierarchyEventRouter:
    """Route events based on task nesting level and accountable agent."""
    
    HIERARCHY_EVENT_MATRIX = {
        # OKR-level tasks: Strategic decisions, council involvement
        "OKR": {
            "on_start": ["council_task_started", "strategy_session_opened"],
            "on_success": ["okr_achieved", "council_consensus_recorded"],
            "on_failure": ["okr_failed", "escalate_to_board"],
            "feedback": ["okr_outcome_recorded", "strategic_learnings"],
        },
        
        # Goal-level tasks: Execution planning, resource allocation
        "Goal": {
            "on_start": ["goal_dispatch", "resource_allocation_plan"],
            "on_success": ["goal_completed", "metrics_updated"],
            "on_failure": ["goal_failed", "fallback_triggered"],
            "feedback": ["execution_metrics", "resource_optimization"],
        },
        
        # Task-level: Actual work execution
        "Task": {
            "on_start": ["task_claimed", "worker_validation"],
            "on_success": ["task_complete", "worker_cleanup"],
            "on_failure": ["task_failed", "silent_crash_check"],
            "feedback": ["execution_feedback", "worker_health"],
        },
        
        # Subtask: Recursive decomposition
        "Subtask": {
            "on_start": ["subtask_started", "contingency_armed"],
            "on_success": ["subtask_complete", "parent_updated"],
            "on_failure": ["subtask_failed", "alternative_triggered"],
            "feedback": ["decomposition_feedback"],
        },
    }
    
    def get_events_for_level(self, level: str, event_type: str) -> List[str]:
        """Get events that should fire at this hierarchy level."""
        return self.HIERARCHY_EVENT_MATRIX.get(level, {}).get(event_type, [])
```

### Layer 3: Accountable Agent Steering

```python
class AccountableAgentEventRouter:
    """Route events based on accountable agent's strategic role."""
    
    AGENT_STEERING_MATRIX = {
        "demis_hassabis": {  # Strategy & Planning
            "steering_events": [
                "mcts_tree_search_started",
                "game_theory_solution_computed",
                "long_term_planning_phase",
                "recursive_improvement_cycle",
            ],
            "decision_events": [
                "strategy_decision_required",
                "council_consensus_needed",
            ],
            "learning_events": [
                "strategic_pattern_learned",
                "prediction_accuracy_recorded",
            ],
        },
        
        "werner_vogels": {  # Distributed Systems & Resilience
            "steering_events": [
                "failure_mode_analysis",
                "cascade_prevention_check",
                "fault_tolerance_decision",
                "distributed_coordination",
            ],
            "decision_events": [
                "system_resilience_decision",
                "failure_path_selection",
            ],
            "learning_events": [
                "resilience_pattern_learned",
                "fault_recovery_metrics",
            ],
        },
        
        "jeff_dean": {  # Execution & Optimization
            "steering_events": [
                "parallelization_analysis",
                "resource_optimization_phase",
                "performance_prediction",
                "workload_distribution",
            ],
            "decision_events": [
                "execution_strategy_decision",
                "resource_allocation_decision",
            ],
            "learning_events": [
                "execution_pattern_learned",
                "performance_metrics_recorded",
            ],
        },
        
        "margaret_hamilton": {  # Reliability & Safety
            "steering_events": [
                "reliability_verification",
                "safety_protocol_check",
                "graceful_degradation_plan",
                "error_recovery_sequence",
            ],
            "decision_events": [
                "safety_gate_decision",
                "error_recovery_decision",
            ],
            "learning_events": [
                "reliability_pattern_learned",
                "error_handling_feedback",
            ],
        },
    }
    
    def get_events_for_agent(self, agent_id: str, event_category: str) -> List[str]:
        """Get events specific to this accountable agent's role."""
        return self.AGENT_STEERING_MATRIX.get(agent_id, {}).get(event_category, [])
```

### Layer 4: LLDAP Policy Integration

```python
class LLDAPPolicyEvaluator:
    """
    Query LLDAP for inherited policies that determine event subscriptions.
    
    Policy inheritance:
    OU=Root
    ├── OU=Engineering (CPU budget, memory limits, escalation rules)
    │   ├── OU=Infrastructure (relaxed limits, system access)
    │   └── OU=Services (standard limits, restricted access)
    ├── OU=Research (high compute budget, experimental features)
    └── OU=Operations (strict reliability, audit logging)
    """
    
    def get_inherited_policies(self, user_dn: str, ou: str) -> PolicySet:
        """
        Query LLDAP inheritance chain:
        1. Get policies on user's OU
        2. Walk up to parent OUs, accumulate policies
        3. Return merged policy set
        """
        policies = PolicySet()
        
        # Walk from leaf OU up to root
        current_ou = ou
        while current_ou:
            # Query synthetic policy holder
            policy_user = f"policy-holder-{current_ou}"
            attrs = self.lldap_client.get_attributes(
                dn=f"cn={policy_user},{current_ou}",
                attributes=[
                    "eventSubscriptions",
                    "resourceBudget",
                    "escalationRules",
                    "toolAccessList",
                    "mcpSubscriptions",
                ]
            )
            
            # Merge inherited attributes (child overrides parent)
            policies.merge(self.parse_lldap_attributes(attrs))
            
            # Move to parent OU
            current_ou = self.get_parent_ou(current_ou)
        
        return policies
    
    def get_resource_budget(self, user_dn: str) -> ResourceBudget:
        """Get CPU, memory, tool budget from LLDAP policies."""
        policies = self.get_inherited_policies(user_dn, user_dn)
        
        return ResourceBudget(
            cpu_cores=policies.get_int("cpuCores", default=4),
            memory_gb=policies.get_int("memoryGb", default=8),
            tool_calls_per_hour=policies.get_int("toolCallsPerHour", default=1000),
            mcts_iterations=policies.get_int("mctsIterations", default=100),
        )
```

### Layer 5: Dynamic Registry Wiring at Dispatch

```python
class DynamicEventRegistry:
    """Wire event subscriptions dynamically at dispatch time."""
    
    def compute_registry_for_task(self, task: Task, context: ExecutionContext) -> EventRegistry:
        """
        Called in dispatch_once() before spawning worker.
        Computes which events should fire for this task based on full context.
        """
        registry = EventRegistry()
        
        # Get accountable agent
        accountable_agent = self.resolve_accountable_agent(task)
        
        # Get execution OU from accountable agent
        execution_ou = self.lldap_client.get_ou(accountable_agent.dn)
        
        # Compute hierarchy level
        hierarchy_level = self.compute_hierarchy_level(task)
        
        # Build execution context
        context = ExecutionContext(
            task=task,
            accountable_agent=accountable_agent,
            execution_ou=execution_ou,
            hierarchy_level=hierarchy_level,
            hierarchy_path=self.build_hierarchy_path(task),
        )
        
        # Compute subscriptions from policies
        policy_engine = DynamicEventRegistryPolicy()
        subscriptions = policy_engine.compute_subscriptions(context)
        
        # Add hierarchy-based events
        hierarchy_router = HierarchyEventRouter()
        for event_type in ["on_start", "on_success", "on_failure", "feedback"]:
            events = hierarchy_router.get_events_for_level(
                hierarchy_level,
                event_type
            )
            subscriptions.add_events(event_type, events)
        
        # Add accountable agent-specific steering events
        agent_router = AccountableAgentEventRouter()
        for category in ["steering_events", "decision_events", "learning_events"]:
            events = agent_router.get_events_for_agent(
                accountable_agent.id,
                category
            )
            subscriptions.add_events(category, events)
        
        # Add LLDAP-derived subscriptions (MCPs, tools)
        lldap_eval = LLDAPPolicyEvaluator()
        lldap_subs = lldap_eval.get_event_subscriptions(
            accountable_agent.dn,
            execution_ou
        )
        subscriptions.merge(lldap_subs)
        
        # Register all subscriptions
        for event_name, handlers in subscriptions.items():
            registry.subscribe(event_name, handlers)
        
        return registry


# Usage in dispatch_once()
def dispatch_once_v3(conn):
    result = DispatchResult()
    
    for task in get_pending_tasks(conn):
        # Dynamically compute event registry based on context
        context = ExecutionContext(task=task)
        event_registry = DynamicEventRegistry().compute_registry_for_task(
            task,
            context
        )
        
        # Inject into task context
        task.event_registry = event_registry
        
        # Spawn worker with dynamic event wiring
        worker = spawn_worker(task, event_registry)
        result.spawned.append(worker)
    
    return result
```

---

## Event Flow Example: Task Execution with Dynamic Wiring

```
Task: "Implement Feature X" 
├─ Hierarchy Level: Goal (under OKR "Q2 2026 Growth")
├─ Accountable Agent: jeff_dean (Execution & Optimization)
├─ Execution OU: OU=Engineering,OU=Services
└─ Role: kanban_worker

Dynamic Wiring Computes:
│
├─ Hierarchy Events (Goal level):
│  ├─ on_start: goal_dispatch, resource_allocation_plan
│  ├─ on_success: goal_completed, metrics_updated
│  ├─ on_failure: goal_failed, fallback_triggered
│  └─ feedback: execution_metrics, resource_optimization
│
├─ Accountable Agent Events (jeff_dean → Execution):
│  ├─ steering_events: parallelization_analysis, resource_optimization_phase
│  ├─ decision_events: execution_strategy_decision, resource_allocation_decision
│  └─ learning_events: execution_pattern_learned, performance_metrics_recorded
│
├─ LLDAP Inherited Policies (OU=Services):
│  ├─ CPU Budget: 4 cores (from OU=Engineering)
│  ├─ Memory Budget: 8GB (from OU=Engineering)
│  ├─ Tool Access: [terminal, file, web] (restricted from parent)
│  ├─ MCP Subscriptions: [serena] (allowed by OU=Services)
│  └─ Escalation Rule: "Defer if CPU > 80%, escalate if > 90%"
│
└─ Role Events (kanban_worker):
   ├─ on_start: task_claimed, worker_validation
   ├─ on_success: task_complete, worker_cleanup
   ├─ on_failure: task_failed, silent_crash_check
   └─ feedback: execution_feedback, worker_health

Final Registry (Merged):
│
├─ task_claimed → [validate_resources, check_mcps_available]
├─ worker_validation → [embodied_state_check, feasibility_assess]
├─ execution_strategy_decision → [mcts_evaluation, resource_plan]
├─ parallelization_analysis → [identify_parallel_work, optimize_schedule]
├─ goal_dispatch → [publish_to_council, update_okr_progress]
├─ execution_metrics → [record_performance, learn_pattern]
├─ task_complete → [cleanup, escalate_learnings, update_config]
└─ worker_cleanup → [verify_state, mark_done, cascade_events_up_hierarchy]

Events Fire In Order:
1. on_start fires: task_claimed, worker_validation, goal_dispatch
2. Embodied state reflection runs (from worker_validation)
3. MCTS evaluation runs (from execution_strategy_decision)
4. Resource plan optimized (from resource_allocation_decision)
5. Worker executes task
6. on_success fires: task_complete, metrics_updated, cascades to parent Goal
7. Feedback recorded: execution_metrics, performance_metrics_recorded
8. Config auto-updates from learnings
```

---

## Key Advantages

### 1. **No Hardcoding**
- Policies are data (LLDAP attributes)
- Subscriptions computed at runtime
- New hierarchy levels, agents, OUs work automatically

### 2. **Inheritance & Override**
- Child OUs inherit parent policies
- Child policies override parent
- Clean precedence rules

### 3. **Auditability**
- Every subscription traced to policy source
- Can query: "Why did this event fire?"
- LLDAP audit log shows policy access

### 4. **Scalability**
- Add new accountable agents → Just add row to AGENT_STEERING_MATRIX
- Add new hierarchy levels → Just add row to HIERARCHY_EVENT_MATRIX
- Add new OUs → Just configure LLDAP policies

### 5. **Integration with CWSA Layers**
- Layer 1 (Resource Validation): Subscriptions tell when to validate
- Layer 2 (Executive Steering): Agent-specific events trigger MCTS
- Layer 3 (Embodied State): State reflection events fire automatically
- Layer 4 (Recursive Planning): Hierarchy events cascade planning
- Layer 5 (Feedback): Feedback events recorded per subscription
- Layer 6 (Self-Improve): Learning events trigger config updates
- Layer 7 (Council): Escalation events route to council

---

## Implementation Checklist

### Phase 1: Foundation (Week 1)
- [ ] LLDAP policy attributes schema defined
- [ ] HierarchyEventRouter implemented
- [ ] AccountableAgentEventRouter implemented
- [ ] Tests: Event routing for each agent type

### Phase 2: LLDAP Integration (Week 2)
- [ ] LLDAPPolicyEvaluator implemented
- [ ] Policy inheritance chain working
- [ ] Tests: Policy resolution from different OUs
- [ ] Tests: Override precedence rules

### Phase 3: Dynamic Registry (Week 2-3)
- [ ] DynamicEventRegistry.compute_registry_for_task() implemented
- [ ] Event merging and conflict resolution
- [ ] Tests: E2E event computation for sample tasks
- [ ] Instrumentation: Log which policy fired each event

### Phase 4: CWSA Integration (Week 3-4)
- [ ] Wire into dispatch_once() (Layer 1 entry point)
- [ ] Each CWSA layer uses subscriptions
- [ ] Tests: CWSA layers respond to events
- [ ] Feature flag: DYNAMIC_EVENT_REGISTRY=true

### Phase 5: Production (Week 4-5)
- [ ] Chaos testing: Randomly change OUs, agents, roles
- [ ] Audit trail: Verify all events traced to policies
- [ ] Performance: Event computation <100ms per task
- [ ] Monitoring: Alert if unknown event fires

---

## Example LLDAP Policy Configuration

```ldif
# OU=Engineering policies (inherited by all engineering OUs)
dn: cn=policy-holder-Engineering,ou=Engineering,dc=company,dc=com
cn: policy-holder-Engineering
cpuCores: 4
memoryGb: 8
toolCallsPerHour: 1000
mctsIterations: 100
eventSubscriptions: steering_events,decision_events,learning_events
escalationRule: "if cpu > 90% then escalate to jeff_dean"

# OU=Services overrides (inherits from OU=Engineering, overrides specific)
dn: cn=policy-holder-Services,ou=Services,ou=Engineering,dc=company,dc=com
cn: policy-holder-Services
cpuCores: 2                    # Override: less CPU than parent
memoryGb: 4                    # Override: less memory
toolAccessList: terminal,file  # Override: restricted tools (no web)
mcpSubscriptions: serena       # Add: only serena MCP
eventSubscriptions: steering_events,feedback_events  # Override: exclude decision_events

# Result for user in OU=Services:
# - CPU: 2 cores (from Services, overrides Engineering's 4)
# - Memory: 4GB (from Services, overrides Engineering's 8)
# - Tools: terminal, file (from Services override)
# - MCPs: serena (inherited from Services)
# - Events: steering_events, feedback_events (from Services override)
```

---

## Prevents Future RCAs

**Old Problem:** Event subscriptions hardcoded → Can't adapt to context → Silent failures

**New Solution:** Policies-driven subscriptions → Adapts automatically → Failures caught early

**Example:** If an OU gets reclassified from "Research" to "Production":
- Old: Code doesn't know, tasks still use research events
- New: LLDAP policy changes → Event subscriptions change automatically → Production events fire

---

## Integration with Executive Council

Council members validate policy changes:

```python
class CouncilPolicyGate:
    """Council consensus on policy changes."""
    
    async def propose_policy_change(self, policy_change: PolicyChange):
        """
        Before applying policy change:
        1. Demis: Strategic impact assessment
        2. Werner: System resilience impact
        3. Jeff: Performance impact
        4. Margaret: Reliability impact
        5. Consensus vote: Deploy or reject
        """
        
        votes = await self.get_agent_votes(policy_change)
        
        if votes.consensus_reached():
            self.apply_policy_change(policy_change)
            self.audit_log(f"Policy change approved by council: {policy_change}")
        else:
            self.reject_policy_change(policy_change, reason=votes.dissent)
```

---

## Status

🚀 **READY FOR IMPLEMENTATION**

- ✅ Architecture designed
- ✅ LLDAP integration planned
- ✅ Event routing matrices defined
- ✅ Integration with CWSA layers mapped
- ✅ Prevents hardcoding, enables dynamic adaptation

**Next:** Implement in Phase 1-2 of CWSA rollout.
