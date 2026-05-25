# KanbanWorkerActor as ExecutiveAgentActor

**User Insight:** KanbanWorkerActor should extend ExecutiveAgentActor  
**Benefit:** Full agent capabilities for workers  
**Result:** Agents with reasoning, memory, and event-driven completion  

---

## Why This Matters

Workers aren't just task runners - they're agents with:
- ✅ Cognitive architecture (deliberation, reasoning)
- ✅ Memory (prior patterns, context)
- ✅ Decision audit trail (why they chose this approach)
- ✅ Full autonomy (no manual intervention needed)

---

## Architecture

### Inheritance Hierarchy
```
ExecutiveAgentActor (base agent)
    ↓
KanbanWorkerActor (task-specific agent)
    ↓
Specific worker tasks (inherit both capabilities)
```

### Agent Lifecycle

```
Worker Instance Created
    ↓
Phase 1: DELIBERATION
    ├─ Load task specification
    ├─ Access memory/prior patterns
    ├─ Reason about approach
    ├─ Audit reasoning trail
    └─ Generate execution plan
    ↓
Phase 2: EXECUTION
    ├─ Run work_fn()
    ├─ Handle success/failure
    └─ Capture results & metadata
    ↓
Phase 3: EMISSION
    ├─ Emit TaskCompleted/TaskBlocked event
    ├─ Include agent_id + reasoning
    └─ NATS pub/sub delivery
    ↓
Exit with code
```

---

## Implementation

### Base: ExecutiveAgentActor
```python
class ExecutiveAgentActor(ABC):
    def __init__(self, agent_id: str, task_id: str = None)
    
    @abstractmethod
    def deliberate(self) -> Dict:
        """Agent reasoning phase"""
    
    @abstractmethod
    def execute(self) -> Tuple[bool, str, Dict]:
        """Agent execution phase"""
    
    def emit_event(self, event_type: str, event_data: Dict) -> int
```

### Extension: KanbanWorkerActor
```python
class KanbanWorkerActor(ExecutiveAgentActor):
    def deliberate(self):
        """Reason about task approach"""
    
    def execute(self):
        """Execute work_fn with agent context"""
    
    def emit_completed(self, summary, metadata) -> int
    def emit_blocked(self, reason) -> int
    
    def run(self) -> int
        """Full lifecycle: deliberate → execute → emit"""
```

---

## Events Now Include Agent Context

### TaskCompletedEvent
```json
{
  "task_id": "t_doc_api_12345",
  "agent_id": "margaret_hamilton",
  "summary": "API Documentation complete",
  "metadata": {
    "files_created": 5,
    "coverage": "95%"
  },
  "reasoning": {
    "task_id": "t_doc_api_12345",
    "approach": "Execute work_fn and emit event",
    "risks": ["work_fn may throw"],
    "plan": [...]
  },
  "completed_at": "2026-05-25T08:16:23Z"
}
```

### TaskBlockedEvent
```json
{
  "task_id": "t_doc_api_12345",
  "agent_id": "margaret_hamilton",
  "reason": "Provider quota exhausted",
  "blocked_at": "2026-05-25T08:16:23Z"
}
```

---

## Worker Implementation Pattern

### Simple: Just Do Work
```python
def work():
    return (True, "Done", {"files": 5})

agent = KanbanWorkerActor("t_12345", agent_id="margaret_hamilton")
agent.set_work(work)
exit_code = agent.run()  # Full lifecycle
sys.exit(exit_code)
```

### Advanced: Full Agent Reasoning
```python
def work():
    # Access agent memory
    prior_patterns = agent.memory.get("api_doc_patterns")
    
    # Reason about approach
    approach = agent.deliberate()
    
    # Execute with context
    result = generate_api_docs(
        standards=approach["standards"],
        patterns=prior_patterns
    )
    
    return (True, "Complete", {"files": 5})
```

---

## Subject Tree (NATS)

Workers are agents, so their events follow the agent hierarchy:

```
exec.agents.kanban.{task_id}.completed
    ↓ (Contains)
    ├─ agent_id
    ├─ reasoning (deliberation)
    ├─ summary
    ├─ metadata
    └─ timestamp

exec.agents.kanban.{task_id}.blocked
    ↓ (Contains)
    ├─ agent_id
    ├─ reason
    └─ timestamp
```

---

## Event Handler (Same as Before)

```python
class KanbanEventHandler:
    def handle_task_completed(self, subject: str, event_json: str):
        event = TaskCompletedEvent(**json.loads(event_json))
        
        # Now we know:
        # - Which agent completed it
        # - What they were reasoning
        # - Full context of decision
        
        hermes_kanban_complete(event.task_id, event.summary)
    
    def handle_task_blocked(self, subject: str, event_json: str):
        event = TaskBlockedEvent(**json.loads(event_json))
        
        # Now we know:
        # - Which agent is blocked
        # - Why (with full context)
        
        hermes_kanban_block(event.task_id, event.reason)
```

---

## Why This Is Better

| Aspect | Standalone Worker | ExecutiveAgentActor Worker |
|--------|-------------------|--------------------------|
| Memory | None | Access to prior patterns |
| Reasoning | None | Full deliberation trail |
| Autonomy | Limited (just executes) | Full (decides approach) |
| Auditability | What happened? | Why did it happen? |
| Learning | No | Learns from experience |
| Recovery | Manual retry | Can retry with new reasoning |
| Scalability | Single task | Handles complexity |

---

## Deployment

### Phase 1: Now (May 25)
- ✅ Implementation complete
- ✅ Tested and working
- All new workers use this pattern

### Phase 2: May 26
- Migrate existing dispatch tasks
- All workers become agents
- Zero protocol violations

### Phase 3: May 27-30
- Full executive agent orchestration
- Agents deliberate together (consensus)
- Cognitive architecture at scale

### Phase 4: June 1 Production
- All workers are full agents
- Event-driven completion
- Full reasoning audit trail
- Ready for production deployment

---

## Files

- `/home/ubuntu/hermes-agent/src/kanban_worker_executive_agent_actor.py`
- Implementation: 10.5 KB
- Fully typed, tested, documented

---

## Status: ✅ IMPLEMENTED

**Workers are now full executive agents with:**
- ✅ Cognitive capabilities (deliberation)
- ✅ Memory access
- ✅ Event-driven completion
- ✅ Full reasoning audit trail
- ✅ Zero protocol violations

**Next:** Migrate all dispatch tasks to use this pattern.
