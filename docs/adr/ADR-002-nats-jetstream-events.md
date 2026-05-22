# ADR-O-002: Use NATS JetStream for Event Broker

**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent  

## Decision

Use NATS JetStream (via existing VCGGateway integration) as the event broker for the OKR-driven executive system.

## Context

The OKR system is fully event-driven:
1. Parse OKR → publish `okr.parsed`
2. Research solution → publish `research.completed`
3. Create plan → publish `plan.created`
4. Allocate tasks → publish `task.assigned`
5. Execute → publish `execution.completed`
6. Generate PR → publish `pr.created`
7. Calculate metrics → publish `metrics.calculated`

Requirements:
- Pub/sub event mechanism
- Persistence (audit trail of all events)
- Durable consumers (can replay events)
- Multi-machine support (hermes1 + hermes2)
- Already integrated in production

VCGGateway already has NATS integration:
- Location: `gateway/vcg_gateway.py` (318 lines)
- Has: `_nats`, `_js` (JetStream context)
- Status: Ready to use, just needs to be enabled

## Alternatives

### 1. Custom Event System
**Rationale for rejection**:
- Reinventing wheel
- No persistence
- No failure recovery
- Single point of failure

### 2. Kafka
**Rationale for rejection**:
- Not deployed
- Requires external service
- Overkill for this scale
- More operational complexity

### 3. Redis pub/sub
**Rationale for rejection**:
- No persistence
- Can lose events on crash
- Not suitable for audit trail

### 4. RabbitMQ
**Rationale for rejection**:
- Not deployed
- Heavy operational overhead
- Not needed for current scale

### 5. NATS JetStream ✅ **CHOSEN**

**Rationale for selection**:
- Already integrated in `vcg_gateway.py`
- JetStream provides persistence
- Durable consumers for replay
- Multi-machine capable (via cluster)
- Lightweight (< 100MB binary)
- Proven production system

## Rationale

NATS JetStream is perfect because:

1. **Already integrated**: `VCGGateway._js` is ready to use
   ```python
   # In gateway/vcg_gateway.py line 59:
   self._js = None  # JetStream context
   ```

2. **Persistence**: JetStream stores events persistently
   ```python
   # Events can be replayed after restart
   async def subscribe(self, topic: str, handler: Callable):
       psub = await self.js.subscribe(topic, deliver_policy=DeliverPolicy.All)
       # Can replay all events from beginning
   ```

3. **Audit trail**: Every event is logged
   - Can answer: "What happened when?"
   - Can recompute state: "Replay all events from Jan 1st"
   - Can debug: "Why did this fail?"

4. **Durable consumers**: Replay capability
   ```python
   # Subscribe from beginning (for debugging)
   psub = await self.js.subscribe(topic, deliver_policy=DeliverPolicy.All)
   
   # Or from latest (normal operation)
   psub = await self.js.subscribe(topic, deliver_policy=DeliverPolicy.Latest)
   ```

5. **Multi-machine**: NATS cluster works across hermes1 + hermes2

## Consequences

### Positive
- All state changes become events
- Full audit trail available
- Can debug by replaying events
- Components decouple via events
- Easy to add new subscribers (no coupling)
- Events are first-class citizens

### Negative
- Requires NATS infrastructure
- Events must be immutable (can't modify published events)
- Subscribers must be idempotent (may receive duplicates)
- Network latency between machines

### Mitigation
- NATS already running on hermes2
- Make all event handlers idempotent
- Use event version numbers for compatibility

## Related Decisions

- **ADR-O-001**: DuckDB stores snapshots of event state
- **ADR-O-003**: Hexagonal architecture enables event decoupling
- **ADR-K-001**: Kanban also moving to DuckDB (complementary)

## Implementation Notes

### Current VCGGateway Integration

```python
class VCGGateway:
    def __init__(self, dispatcher, nats_servers=None):
        self._nats = None
        self._js = None  # JetStream context
        self._nats_servers = nats_servers or ["nats://localhost:4222"]
    
    async def connect(self):
        """Connect to NATS and start heartbeat"""
        # Currently commented out (lines 102-103):
        # await self._connect_nats()
        # Need to uncomment to enable
```

### To Enable

1. Uncomment NATS connection in VCGGateway:
   ```python
   @asynccontextmanager
   async def connect(self):
       await self._connect_nats()  # Uncomment this
       self._start_heartbeat_thread()
       yield self
       # ...
   ```

2. Wire into OKRExecutionOrchestrator:
   ```python
   class OKRExecutionOrchestrator(InstanceOrchestrator):
       def __init__(self, gateway: VCGGateway):
           self.gateway = gateway
           self.js = gateway._js  # Use existing JetStream
       
       async def execute_okr(self, okr_spec: Dict):
           # Publish events
           await self.js.publish("okr.parsed", event.to_json())
   ```

### Event Topics

Define standard NATS topics:
```
okr.parsed                  # OKREngine publishes
research.completed          # ResearchEngine publishes
plan.created               # PlanningEngine publishes
task.assigned              # ExecutionEngine publishes
execution.started          # ExecutionEngine publishes
execution.completed        # ExecutionEngine publishes
pr.created                 # ReviewEngine publishes
pr.submitted               # ReviewEngine publishes
metrics.calculated         # MetricsEngine publishes
```

### Idempotency Pattern

All subscribers must be idempotent:
```python
async def on_task_assigned(self, event: Event):
    # Check if already processed
    existing = await db.find_by_correlation_id(event.correlation_id)
    if existing:
        return  # Already processed, skip
    
    # Process event
    await self.kanban_db.create_task(...)
    
    # Record that we processed this correlation_id
    await db.record_processed(event.correlation_id)
```

## Testing

Need to test:
1. Events publish to JetStream
2. Subscribers receive events
3. Correlation IDs link events
4. Replay works (can replay old events)
5. Idempotency (replaying same event twice = once)

## References

- [NATS JetStream Docs](https://docs.nats.io/nats-concepts/jetstream)
- [Existing VCGGateway](../../gateway/vcg_gateway.py)
- Related: ADR-O-001 (Data Store), ADR-O-003 (Architecture)
