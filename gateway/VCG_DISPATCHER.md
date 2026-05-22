"""VCG Dispatcher — Game-Theoretic Task Allocation Runtime

# Overview

The VCG (Vickrey-Clarke-Groves) Dispatcher implements a production-grade task allocation
engine for multi-instance Hermes deployments. It combines:

  1. **Health Monitoring**: Track agent health via heartbeats, auto-degrade stale nodes
  2. **Task Dispatch**: Game-theoretic allocation using RICE scoring + skill matching
  3. **Resource Accounting**: Clarke tax welfare tracking for billing/incentives
  4. **NATS Integration**: Event publishing for monitoring dashboards

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Remote Agent Instances                      │
│  [hermes-local]  [hermes2]  [hermes3]  ... [hermesN]        │
│        │               │           │             │          │
│     health            health      health      health         │
│     capacity          capacity    capacity    capacity       │
└──────────┬─────────────┬───────────┬──────────┬─────────────┘
           │             │           │          │
           └─────────────┼───────────┼──────────┘ (HTTP heartbeats)
                         │
                    ┌────▼──────────────────────┐
                    │   VCGGateway              │
                    │  (vcg_gateway.py)         │
                    │                           │
                    │  ┌───────────────────┐   │
                    │  │ VCGDispatcher     │   │
                    │  │ - register_node   │   │
                    │  │ - dispatch_task   │   │
                    │  │ - health_check    │   │
                    │  │ - resource_report │   │
                    │  └───────────────────┘   │
                    │                           │
                    │  ┌───────────────────┐   │
                    │  │ Heartbeat Thread  │   │
                    │  │ (30s-60s timeout) │   │
                    │  └───────────────────┘   │
                    └────┬─────────────────────┘
                         │
                    ┌────▼──────────────┐
                    │  NATS JetStream   │
                    │  (monitoring)     │
                    └───────────────────┘
```

## Key Components

### VCGNode
Represents a Hermes instance with:
  - Health state (HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN)
  - Health score (0.0-1.0)
  - Capacity tracking (allocated vs available)
  - Heartbeat timestamp for staleness detection
  - Error/failure counters

### VCGAgent
Capability spec for an agent:
  - id: Unique identifier
  - skills: List of capabilities ['vision', 'code-review', ...]
  - capacity: Max concurrent tasks
  - reliability: Baseline health score

### TaskAllocationResult
Outcome of game-theoretic allocation:
  - agent_id: Selected agent
  - welfare: Value to winning agent
  - clarke_tax: Incentive tax (winner's welfare - runner-up's welfare)
  - skill_match: 0.0-1.0 skill match score

### VCGDispatcher (Core)
Central allocation engine:
  - register_node(): Update node health + capacity
  - dispatch_task(): Allocate task using RICE + game theory
  - complete_task(): Free capacity after task completes
  - health_check(): Comprehensive monitoring report
  - resource_report(): Welfare accounting for billing

### VCGGateway (Integration)
Wraps dispatcher for hermes-agent integration:
  - singleton pattern (for gateway/run.py)
  - async/await support (NATS integration)
  - heartbeat thread management
  - event publishing

## Allocation Algorithm

Task allocation uses a multi-factor welfare calculation:

    RICE = (reach × impact × confidence) / effort
    skill_match = (required_skills ∩ agent_skills) / |required_skills|
    utilization = (capacity_max - capacity_available) / capacity_max
    welfare = RICE × skill_match × health_score × (1 - utilization)

The agent with highest welfare wins. Clarke tax ensures incentive compatibility:

    clarke_tax = max_welfare - second_max_welfare

## Health State Machine

```
         ┌─────────────┐
         │   UNKNOWN   │ (initial state after creation)
         └──────┬──────┘
                │ register_node()
                ▼
         ┌──────────────┐
    ┌───►│   HEALTHY    │ (score >= 0.8)
    │    └──────┬───────┘
    │           │ heartbeat missing 30s
    │           ▼
    │    ┌──────────────┐
    │    │  DEGRADED    │ (score 0.5-0.8, or stale >30s)
    │    └──────┬───────┘
    │           │ heartbeat missing 60s, or 3 consecutive failures
    │           ▼
    │    ┌──────────────┐
    │    │ UNHEALTHY    │ (score < 0.5)
    │    └──────────────┘
    │
    └─────── register_node() with good score
         (clears consecutive_failures, transitions back)
```

## Usage Example

### Basic Initialization

```python
from gateway.vcg_dispatcher import VCGDispatcher, VCGAgent

# Define agent specs
agents = [
    VCGAgent(id='hermes-local', skills=['all'], capacity=5),
    VCGAgent(id='hermes2', skills=['vision', 'code-review'], capacity=3),
]

# Create dispatcher
dispatcher = VCGDispatcher(agents=agents)

# Register node health (e.g., from /health endpoint)
dispatcher.register_node('hermes-local', health_score=0.95, capacity_available=4)
dispatcher.register_node('hermes2', health_score=0.75, capacity_available=2)
```

### Task Allocation

```python
# Dispatch a code review task
result = dispatcher.dispatch_task(
    task_id='t_abc123',
    title='Review PR #42 — rate limiter',
    required_skills=['code-review'],
    reach=8,      # RICE: 8 (many users will benefit)
    impact=9,     # RICE: 9 (critical feature)
    confidence=0.9,  # RICE: 0.9 (high confidence)
    effort=2.0,   # RICE: 2 days
)

print(f"Allocated to: {result.agent_id}")
print(f"Welfare: {result.welfare:.1f}")
print(f"Clarke tax: {result.clarke_tax:.1f}")
print(f"Skill match: {result.skill_match:.2f}")
```

### Monitoring

```python
# Health check
health = dispatcher.health_check()
print(f"Healthy nodes: {health['summary']['healthy_nodes']}")
print(f"Capacity utilization: {health['summary']['capacity_utilization']:.2%}")
for agent_id, status in health['nodes'].items():
    print(f"  {agent_id}: {status['state']}, util={status['utilization']:.2%}")

# Resource accounting
report = dispatcher.resource_report()
print(f"Total allocations: {report['totals']['total_allocations']}")
print(f"Total welfare: {report['totals']['total_welfare']:.1f}")
for agent_id, stats in report['agents'].items():
    print(f"  {agent_id}: {stats['allocated_count']} tasks, "
          f"welfare={stats['total_welfare']:.1f}")
```

### Gateway Integration

```python
from gateway.vcg_gateway import VCGGateway, initialize_vcg_gateway
import asyncio

# Load config and initialize
config = {
    "agents": [
        {"id": "hermes-local", "skills": ["all"], "capacity": 5},
        {"id": "hermes2", "skills": ["vision"], "capacity": 3},
    ],
    "nats_servers": ["nats://localhost:4222"],
    "heartbeat_interval_sec": 10.0,
}

gateway = initialize_vcg_gateway(config)

# Use in async context
async def main():
    async with gateway.connect():
        # Allocate task
        result = await gateway.allocate_task(
            task_id='t_123',
            title='Vision analysis',
            required_skills=['vision'],
            effort=1.5,
        )
        if result:
            print(f"Allocated to {result.agent_id}")

        # Report health from remote instance
        gateway.report_node_health('hermes2', health_score=0.85, capacity_available=2)

        # Get status
        health = gateway.get_health_status()
        print(health)

asyncio.run(main())
```

## Integration with instance_orchestrator

The VCG dispatcher should be integrated into the `dispatch_command()` flow:

```python
# In instance_orchestrator.py

from gateway.vcg_gateway import get_vcg_gateway

async def dispatch_command(user_id, platform, message_text, ...):
    # Determine which instance should handle this
    vcg = get_vcg_gateway()
    if vcg:
        allocation = await vcg.allocate_task(
            task_id=f"msg_{user_id}_{time.time()}",
            title=message_text[:100],
            required_skills=extract_skills(message_text),
            reach=5, impact=5, confidence=0.8, effort=0.5,
        )
        if allocation:
            instance = allocation.agent_id
            logger.info(f"VCG allocated to {instance} (welfare={allocation.welfare:.1f})")
    else:
        # Fallback to default instance
        instance = "hermes-local"

    # Dispatch to selected instance
    return await run_remote_agent(instance, message_text)
```

## Integration with remote_agent_api

Health endpoints should feed into the VCG dispatcher:

```python
# In remote_agent_api.py

from gateway.vcg_gateway import get_vcg_gateway

@app.get("/health")
async def health_check():
    # ... existing health check logic ...
    health_data = {...}

    # Report to VCG gateway if available
    vcg = get_vcg_gateway()
    if vcg:
        vcg.report_node_health(
            agent_id=get_instance_id(),
            health_score=calculate_health_score(health_data),
            capacity_available=available_slots(),
            response_time_ms=recent_latency_ms(),
        )

    return health_data
```

## Configuration (config.yaml)

```yaml
vcg:
  enabled: true
  agents:
    - id: hermes-local
      skills: [all]
      capacity: 5
      reliability: 0.95
    - id: hermes2
      skills: [vision, code-review]
      capacity: 3
      reliability: 0.9
  nats_servers: [nats://localhost:4222]
  heartbeat_interval_sec: 10.0
  stale_timeout_degraded_sec: 30
  stale_timeout_unhealthy_sec: 60
```

## Performance Characteristics

- **Dispatch latency**: ~1-2ms (skill matching + welfare calculation)
- **Health check overhead**: ~0.5ms per node (50 nodes = ~25ms)
- **Memory footprint**: ~1KB per agent + 2KB per task in history
- **Heartbeat thread**: Minimal CPU (wakes every 10s, does quick staleness check)

## Testing

Run the comprehensive test suite:

```bash
cd /home/ubuntu/hermes-agent
python -m pytest tests/gateway/test_vcg_dispatcher.py -v
```

27 tests covering:
  - Node state machine and readiness
  - Skill matching (perfect, partial, wildcard 'all')
  - RICE scoring and welfare calculation
  - Capacity constraints and utilization
  - Health score and degradation transitions
  - Stale heartbeat detection (DEGRADED → UNHEALTHY)
  - Clarke tax incentive compatibility
  - Resource accounting and reporting

## Future Enhancements

1. **Dynamic Pricing**: Adjust Clarke tax based on market demand
2. **Multi-Region**: Geographic allocation constraints
3. **RL-Based Learning**: Optimize RICE weights based on outcome feedback
4. **Byzantine Fault Tolerance**: Robust to malicious node reports
5. **Preemption**: High-priority task interruption with backfill
6. **Batching**: Allocate multiple related tasks to same agent for cache locality

## References

- Vickrey, W. (1961). "Counterspeculation, auctions, and competitive sealed tenders"
- Clarke, E. (1971). "Multipart pricing of public goods"
- Groves, T. (1973). "Incentives in teams"
- RICE Prioritization: https://www.intercom.com/blog/rice-simple-prioritization-for-product-managers/
"""
