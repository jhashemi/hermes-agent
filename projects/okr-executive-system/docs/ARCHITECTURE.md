# OKR-DRIVEN EXECUTIVE SYSTEM - STRATEGIC PLAN

**Phase 0: Strategic Planning & Architecture** (THIS SESSION)  
**Provider**: Bedrock claude-opus-4-7 (1M context)  
**Duration**: 4-6 hours (planning + design)  
**Output**: Complete architecture design + implementation sequence

---

## I. SYSTEM REQUIREMENTS ANALYSIS

### User Requirements (Canonicalized)

1. **Input**: OKR specifications (goals, key results, KPIs)
2. **Processing**:
   - Research solutions via expert executive agents
   - Plan implementation hierarchically with mental models
   - Assign to accountable executive agent
   - Execute with full cognitive embodiment
   - Generate and submit PR for code review
3. **Output**: 
   - Implemented solution (as PR)
   - KPIs/metrics calculated and tracked
   - Full audit trail (events)
4. **Quality Requirements**:
   - SOLID principles throughout
   - Hexagonal architecture (domain ↔ adapters)
   - Event-driven integration
   - Org hierarchical support
   - Production-ready on day 1

### Non-Functional Requirements

- **Scalability**: Handle 1-100+ OKRs
- **Reliability**: No data loss, async event processing
- **Maintainability**: SOLID principles enable changes
- **Testability**: TDD-friendly architecture
- **Observability**: Full event audit trail
- **Extensibility**: New engines/adapters without modification

---

## II. CRITICAL PATH ANALYSIS

### Dependencies (What Must Exist First)

```
LEVEL 1 (Abstractions):
├─ Event Definitions (no dependencies)
├─ Domain Model Interfaces (no dependencies)
└─ Repository Pattern (no dependencies)

LEVEL 2 (Foundation):
├─ OKR Domain Model (depends on LEVEL 1)
├─ Event Bus/NATS Adapter (depends on LEVEL 1)
├─ Task/Goal Domain Model (depends on LEVEL 1)
└─ Agent/Team Domain Model (depends on LEVEL 1)

LEVEL 3 (Engines - Can build in PARALLEL):
├─ OKREngine (depends on LEVEL 2 OKR model)
├─ ResearchEngine (depends on LEVEL 2 Agent model)
├─ PlanningEngine (depends on LEVEL 2 Goal model)
├─ ExecutionEngine (depends on LEVEL 2 Task + Agent models)
├─ ReviewEngine (depends on LEVEL 2 Code model)
└─ MetricsEngine (depends on LEVEL 2 Metric model)

LEVEL 4 (Orchestration - Depends on all LEVEL 3):
├─ OKRExecutionOrchestrator
├─ State Machine Manager
└─ Event Router

LEVEL 5 (Adapters - Depend on LEVEL 4):
├─ GitHub Adapter
├─ Kanban Adapter
├─ Metrics DB Adapter
└─ Notification Adapter

LEVEL 6 (Integration - Final):
├─ Presentation Layer
├─ API Endpoints
└─ Testing Suite
```

**Critical Path**: LEVEL 1 → LEVEL 2 → (LEVEL 3 parallel) → LEVEL 4 → LEVEL 5 → LEVEL 6

---

## III. MINIMAL VIABLE ARCHITECTURE

### What We MUST Have (MVP)

```
Core Components:
1. Event System (NATS, Event definitions)
2. Domain Models (OKR, Goal, Task, Agent, Metric)
3. OKREngine (parse + decompose)
4. ExecutionEngine (assign + execute)
5. ReviewEngine (generate PR)
6. MetricsEngine (calculate KPIs)
7. Orchestrator (state machine)

Adapters:
1. NATS Adapter (event broker)
2. GitHub Adapter (PR submission)
3. Memory Repository (in-process for MVP)

Interfaces:
1. Engine interface (all engines implement)
2. Adapter interface (all adapters implement)
3. Repository pattern (data persistence)
4. Event producer/consumer
```

### What We Can ADD LATER

- Advanced mental models library
- Multi-LLM support
- Distributed execution
- ML-based research
- Advanced metrics
- UI dashboard
- Audit reporting

---

## IV. INTERFACE-FIRST DESIGN

### Core Interface (All Engines Implement)

```python
class ExecutionEngine(ABC):
    """Interface that all engines implement"""
    
    @abstractmethod
    async def execute(self, input_model: DomainModel) -> ExecutionResult:
        """Execute the engine's logic"""
        
    @abstractmethod
    def get_event_subscriptions(self) -> List[str]:
        """What events does this engine consume?"""
        
    @abstractmethod
    def get_published_events(self) -> List[str]:
        """What events does this engine produce?"""
    
    # Common lifecycle
    async def on_event(self, event: Event) -> None:
        """Handle incoming event"""
        
    async def shutdown(self) -> None:
        """Graceful shutdown"""
```

### Event Interface

```python
@dataclass
class Event:
    """Base event class - all events inherit"""
    id: str  # UUID
    timestamp: datetime
    source: str  # Which engine produced this?
    event_type: str  # okr.parsed, execution.completed, etc.
    payload: Dict  # Event data
    correlation_id: str  # Link related events
    
class EventBroker(ABC):
    """Abstract event broker - implement for NATS, etc."""
    
    @abstractmethod
    async def publish(self, event: Event) -> None:
        
    @abstractmethod
    async def subscribe(self, 
                       topic: str, 
                       handler: Callable[[Event], None]) -> None:
```

### Repository Interface (Data Persistence)

```python
class Repository(ABC, Generic[T]):
    """Data persistence pattern"""
    
    @abstractmethod
    async def save(self, model: T) -> None:
    
    @abstractmethod
    async def load(self, id: str) -> Optional[T]:
    
    @abstractmethod
    async def list_all(self) -> List[T]:
    
    @abstractmethod
    async def delete(self, id: str) -> None:
```

---

## V. EVENT FLOW (Complete)

### Phase 1: Parse & Validate OKR

```
INPUT: OKR Specification
  ↓
OKREngine.execute()
  ├─ Validate structure
  ├─ Parse goals + KRs
  └─ Decompose into Goal objects
  ↓
PUBLISH: okr.parsed
  ├─ Subscribers: Orchestrator, MetricsEngine
  └─ Payload: {okr_id, goals, metrics}
  ↓
STATE UPDATE: OKR → PARSED
```

### Phase 2: Research Solutions

```
SUBSCRIBE: okr.parsed
  ↓
ResearchEngine.execute()
  ├─ Query expert agents
  ├─ Evaluate options
  └─ Rank by criteria
  ↓
PUBLISH: research.completed
  ├─ Subscribers: Orchestrator, PlanningEngine
  └─ Payload: {okr_id, options, recommendation}
  ↓
STATE UPDATE: OKR → RESEARCHED
```

### Phase 3: Create Plan

```
SUBSCRIBE: research.completed
  ↓
PlanningEngine.execute()
  ├─ Select mental model
  ├─ Create hierarchy
  ├─ Define dependencies
  └─ Assign effort/priority
  ↓
PUBLISH: plan.created
  ├─ Subscribers: Orchestrator, ExecutionEngine
  └─ Payload: {plan_id, tasks, dependencies}
  ↓
STATE UPDATE: OKR → PLANNED
```

### Phase 4: Assign & Execute

```
SUBSCRIBE: plan.created
  ↓
ExecutionEngine.execute()
  ├─ Assign tasks to agents
  ├─ Allocate across cluster (VCG)
  └─ Start execution
  ↓
PUBLISH: task.assigned
  ├─ Subscribers: Kanban, MetricsEngine
  └─ Payload: {task_id, agent_id, machine_id}
  ↓
PUBLISH: execution.started
  ↓
Agent executes (cognitive harness)
  ↓
PUBLISH: execution.completed
  ├─ Subscribers: ReviewEngine, MetricsEngine
  └─ Payload: {task_id, success, output, metrics}
  ↓
STATE UPDATE: OKR → EXECUTING → EXECUTING_REVIEW
```

### Phase 5: Generate & Submit PR

```
SUBSCRIBE: execution.completed
  ↓
ReviewEngine.execute()
  ├─ Collect all outputs
  ├─ Generate PR content
  ├─ Add test results
  └─ Submit to GitHub
  ↓
PUBLISH: pr.created
  ├─ Subscribers: MetricsEngine, Notification
  └─ Payload: {pr_number, commit_sha, url}
  ↓
PUBLISH: pr.submitted
  ├─ Subscribers: MetricsEngine
  └─ Payload: {pr_number, review_url}
  ↓
STATE UPDATE: OKR → REVIEWING
```

### Phase 6: Calculate Metrics

```
SUBSCRIBE: execution.completed, pr.created
  ↓
MetricsEngine.execute()
  ├─ Collect KPI data
  ├─ Calculate success metrics
  ├─ Compare to targets
  └─ Update OKR progress
  ↓
PUBLISH: metrics.calculated
  ├─ Subscribers: Notification, Dashboard
  └─ Payload: {okr_id, kpis, progress, status}
  ↓
STATE UPDATE: OKR → COMPLETE
  ↓
OUTPUT: OKR with Results + PR + KPIs
```

---

## VI. IMPLEMENTATION SEQUENCE (CORRECT ORDER)

### Phase 0: Abstractions (2 hours)

**What to build**:
1. Event class + EventBroker interface
2. ExecutionEngine interface (all engines implement)
3. Repository interface
4. Domain model base classes

**Why first**: Everything depends on these.

**Validation**: Unit tests that prove interfaces work.

### Phase 1: Domain Models (2 hours)

**What to build**:
1. OKRModel (goal, key results, metrics)
2. GoalModel (decomposed goals)
3. TaskModel (executable tasks)
4. AgentModel (agent capabilities)
5. MetricModel (KPI definitions)
6. EventModel (6 event types)

**Why after Phase 0**: Interfaces define the contracts.

**Validation**: Tests prove models serialize/deserialize correctly.

### Phase 2: Event Infrastructure (1 hour)

**What to build**:
1. NATS adapter implementing EventBroker
2. Event publisher/consumer
3. Event routing logic

**Why after Phase 1**: Need models to pass through events.

**Validation**: Tests prove events route correctly.

### Phase 3: Engines - Core 4 (2 hours - PARALLEL OK)

**What to build**:
1. OKREngine (parse + decompose)
2. ExecutionEngine (assign + allocate)
3. ReviewEngine (generate PR)
4. MetricsEngine (calculate KPIs)

**Why these 4 first**: Complete the critical path (Parse → Execute → Review → Measure)

**Why parallel OK**: Each depends only on Phase 1 models + Phase 2 events.

**Validation**: Integration tests for each engine with mock inputs.

### Phase 4: Orchestrator (1 hour)

**What to build**:
1. OKRExecutionOrchestrator
2. State machine for OKR lifecycle
3. Event routing to engines

**Why after Phase 3**: Needs all engines to exist.

**Validation**: End-to-end test of full pipeline.

### Phase 5: Adapters (1 hour)

**What to build**:
1. GitHub adapter (PR submission)
2. Kanban adapter (task tracking)
3. Memory repository

**Why after Phase 4**: Orchestrator validates what adapters need.

**Validation**: Integration tests with real systems (or mocks).

### Phase 6: Polish & Testing (2 hours)

**What to build**:
1. Error handling across all components
2. Logging/observability
3. Comprehensive test suite
4. Documentation

**Validation**: Full QA pass, stress tests.

---

## VII. QUALITY GATES (What Proves Each Phase Works)

### Phase 0 Validation

```python
def test_interfaces_are_abstract():
    """Prove interfaces can't be instantiated"""
    
def test_event_has_required_fields():
    """Prove Event model is complete"""
    
def test_engine_interface_minimal():
    """Prove ExecutionEngine has all needed methods"""
```

### Phase 1 Validation

```python
def test_okr_model_creates():
    """Prove OKR model instantiates"""
    
def test_models_serialize():
    """Prove models → JSON → models"""
    
def test_event_models_match_phases():
    """Prove event types match pipeline phases"""
```

### Phase 2 Validation

```python
async def test_event_published():
    """Prove event publishes to NATS"""
    
async def test_event_consumed():
    """Prove subscriber receives event"""
    
async def test_event_correlation():
    """Prove correlation_id links events"""
```

### Phase 3 Validation

```python
async def test_okr_engine_parses():
    """Prove OKREngine.execute() works"""
    
async def test_execution_engine_allocates():
    """Prove ExecutionEngine allocates to agents"""
    
async def test_review_engine_generates_pr():
    """Prove ReviewEngine creates PR-ready output"""
    
async def test_metrics_engine_calculates():
    """Prove MetricsEngine computes KPIs"""
```

### Phase 4 Validation

```python
async def test_orchestrator_routes():
    """Prove orchestrator routes events correctly"""
    
async def test_state_machine_transitions():
    """Prove OKR transitions through states"""
    
async def test_end_to_end_pipeline():
    """Full OKR → PR → KPIs execution"""
```

### Phase 5 Validation

```python
async def test_github_adapter_submits():
    """Prove GitHub adapter creates PR"""
    
async def test_kanban_adapter_creates_task():
    """Prove Kanban adapter tracks tasks"""
    
async def test_repository_crud():
    """Prove repository saves/loads data"""
```

---

## VIII. CRITICAL DESIGN DECISIONS

### Decision 1: Event-First, Not Task-First

**Why**: Enables async processing, audit trail, decoupling.

**Impact**: Each engine is independent, can be developed in parallel.

### Decision 2: Interface-First, Not Implementation-First

**Why**: Enforces contracts, enables testing, prevents coupling.

**Impact**: All engines follow same pattern, easy to add new ones.

### Decision 3: State Machine for OKR Lifecycle

**Why**: Clear transitions, prevents invalid states, audit trail via events.

**Impact**: System is always in known state, easy to reason about.

### Decision 4: Repository Pattern for Data

**Why**: Decouples domain from persistence, easy to swap implementations.

**Impact**: Can use memory in MVP, swap to DuckDB/postgres later.

### Decision 5: Orchestrator Centralizes Control

**Why**: Single point of flow control, easier to debug, easier to monitor.

**Impact**: One place to understand full system behavior.

---

## IX. RISKS & MITIGATIONS

| Risk | Mitigation |
|------|-----------|
| Event ordering (events arrive out of order) | Correlation IDs, idempotent handlers |
| Engine failures | Retry logic, error events, dead letter queue |
| State machine bugs | Comprehensive state tests, visual diagram |
| GitHub API failures | Graceful degradation, queue for retry |
| Cluster allocation failures | VCG dispatcher already proven |
| Metric calculation errors | Validation tests, audit trail |

---

## X. SUCCESS CRITERIA (Phase 0 → Phase 6)

### End of Phase 0
- ✅ All interfaces defined and reviewed
- ✅ All domain models specified
- ✅ Event flow documented and diagrammed
- ✅ Implementation sequence finalized
- ✅ TDD test cases prepared

### End of Phase 6
- ✅ Full OKR → PR → KPIs pipeline works end-to-end
- ✅ All tests passing (>90% coverage)
- ✅ SOLID principles verified
- ✅ Event audit trail complete
- ✅ Production deployment ready
- ✅ Documentation complete

---

## NEXT STEPS

**This document is the blueprint.** Before building:

1. ✅ Review architecture with team
2. ✅ Validate event flow with user
3. ✅ Agree on implementation sequence
4. ✅ Prepare TDD test suite
5. ✅ Set up repository structure
6. ✅ Begin Phase 0 with concrete implementation

**Ready to build Phase 0 (Abstractions)?**

