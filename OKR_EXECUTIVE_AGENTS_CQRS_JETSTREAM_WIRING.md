# OKR: Executive Agents CQRS/JetStream Wiring

**Objective:** Wire donald_knuth and werner_vogels as fully autonomous CQRS actors with JetStream event streams

**Accountable:** jeff_dean (infrastructure)  
**Partner:** demis_hassabis (architecture)  
**Timeline:** May 26-31, 2026 (6 days)  
**Effort:** 120 hours  

---

## Problem

Executive agents (donald_knuth, werner_vogels) are defined but NOT connected to the event system:

❌ No NATS JetStream integration
❌ No agent actor instantiation
❌ No event publishing/subscribing
❌ No subject hierarchy configuration
❌ Cannot deliberate or reach consensus without this wiring

**Impact:** OKRs cannot flow through deliberation → consensus → sign-off → dispatch

---

## Solution

Implement full CQRS actor wiring:

1. **Agent Registry** - Instantiate agents as ExecutiveAgentActors
2. **NATS Connection** - Connect to JetStream at startup
3. **Event Publishing** - Emit deliberation/consensus/sign-off events
4. **Event Subscriptions** - Subscribe to agent events, trigger workflows
5. **Subject Hierarchy** - Map agents/okrs to NATS subjects

---

## Key Results

### KR1: Agent Instantiation (30 hours)
- Create agent registry
- Instantiate donald_knuth as ExecutiveAgentActor
- Instantiate werner_vogels as ExecutiveAgentActor
- Load agent identities from LLDAP
- Target: All agents registered and ready

### KR2: JetStream Integration (40 hours)
- Set up NATS connection
- Create JetStream streams per agent
- Configure subject hierarchy (exec.agents.*)
- Set up persistent streams with retention
- Target: All streams created, consumers ready

### KR3: Event Publishing (25 hours)
- Wire deliberation event emission
- Wire consensus event emission
- Wire sign-off event emission
- Wire task completion events
- Target: All events publish successfully

### KR4: Event Subscriptions (15 hours)
- Subscribe to agent deliberation events
- Subscribe to consensus completion events
- Subscribe to sign-off events
- Trigger workflows on events
- Target: All subscriptions active, workflows triggered

### KR5: Testing & Verification (10 hours)
- Test agent instantiation
- Test event publishing/subscribing
- Test end-to-end workflow
- Verify no events lost
- Target: All tests pass, zero event loss

---

## Detailed Tasks (18 tasks)

### Phase 1: Infrastructure (May 26-27) - 6 tasks
1. Design agent registry architecture
2. Design NATS subject hierarchy
3. Create JetStream stream configuration
4. Set up NATS connection pool
5. Create agent factory
6. Load LLDAP agent identities

### Phase 2: Agent Wiring (May 28-29) - 6 tasks
7. Instantiate donald_knuth actor
8. Instantiate werner_vogels actor
9. Wire agent memory/context
10. Wire agent deliberation capability
11. Wire agent consensus capability
12. Verify agent instantiation

### Phase 3: Event System (May 30-31) - 6 tasks
13. Implement deliberation event publishing
14. Implement consensus event publishing
15. Implement sign-off event publishing
16. Wire event subscriptions
17. Test event publishing/subscribing
18. Verify end-to-end workflow

---

## Success Criteria

| Criterion | Target |
|-----------|--------|
| Agent instantiation | ✓ Both agents registered |
| NATS streams | ✓ All streams created |
| Event publishing | ✓ 100% delivery |
| Event subscriptions | ✓ All active |
| Deliberation workflow | ✓ Triggers on OKR creation |
| Consensus workflow | ✓ Completes with verdict |
| Sign-off workflow | ✓ Triggers after consensus |
| End-to-end test | ✓ All phases complete |

---

## Actors Wired

### donald_knuth
- Role: Architecture/Design
- Deliberates on: System design, feasibility, architectural impact
- Publishes: deliberation:complete → exec.agents.donald_knuth.deliberation
- Subscribes to: Consensus verdict → exec.consensus.*.verdict_go
- Signs off on: Plans with architecture impact

### werner_vogels
- Role: Systems/Operations
- Deliberates on: Operational impact, scalability, systems design
- Publishes: deliberation:complete → exec.agents.werner_vogels.deliberation
- Subscribes to: Consensus verdict → exec.consensus.*.verdict_go
- Signs off on: Plans with system operations impact

---

## Timeline

- **May 26-27:** Infrastructure setup (6 tasks)
- **May 28-29:** Agent wiring (6 tasks)
- **May 30-31:** Event system (6 tasks)

**Milestone:** May 31 - Both agents fully wired, ready for OKR deliberation

---

**Status: PROPOSED - Ready for validation**
