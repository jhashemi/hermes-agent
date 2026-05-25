# OKR: Wire Hermes-Agent to Executive Agents Framework

**Objective:** Eliminate duplicate code in hermes-agent by properly depending on production-ready executive-agents-framework for LLDAP, NATS/JetStream, CQRS, and agent orchestration

**Problem Identified:**
- ❌ executive_agents_lldap_cqrs.py created in hermes-agent (duplicate)
- ❌ okr_atomic_creation.py created in hermes-agent (belongs in framework)
- ❌ kanban_worker_executive_agent_actor.py created in hermes-agent (exists in framework)
- ❌ NATS event publishing commented out in hermes-agent
- ❌ donald_knuth + werner_vogels not wired to framework agents
- ❌ Hermes-agent reinventing instead of importing

**Discovery:**
- ✅ /home/ubuntu/executive_agents_framework is production-ready
- ✅ LldapAdapter (ldap3-backed, tested)
- ✅ LDAPAgentLocator (eligibility filtering)
- ✅ NATSEventBus (JetStream, offline fallback)
- ✅ NATSConnectionPool (pooling, reconnection, health checks)
- ✅ ExecutiveAgentActor (full CQRS lifecycle)
- ✅ KanbanWorkerExecutiveAgentActor (workers as agents)
- ✅ Container (DI, composition, profiles)
- ✅ 80+ unit/integration/e2e tests

**Solution:**
1. Delete duplicates from hermes-agent/src/
2. Make hermes-agent depend on executive-agents-framework
3. Wire donald_knuth + werner_vogels via framework Container
4. Import LldapAdapter, NATSEventBus, agents from framework
5. Remove reinvented code paths

---

## Key Results

### KR1: Dependency Management (30 hours)
- Add executive-agents-framework to hermes-agent/requirements.txt
- Test import paths
- Verify all framework modules accessible
- Set up editable install for development
- Target: Zero import errors, all tests pass

### KR2: Delete Duplicates (20 hours)
- Remove executive_agents_lldap_cqrs.py
- Remove okr_atomic_creation.py
- Remove kanban_worker_executive_agent_actor.py
- Delete okr_atomic_creation test file
- Target: 0 duplicates, clean working directory

### KR3: Wire Agent Registry (40 hours)
- Load LldapAdapter from framework
- Load LDAPAgentLocator from framework
- Wire Container to load agents at startup
- Instantiate donald_knuth + werner_vogels via LLDAPAgentLocator
- Register agents in executor
- Target: Both agents fully loaded and operational

### KR4: Wire Event Bus (30 hours)
- Replace NATSConnectionPool placeholder with framework version
- Configure JetStream streams (exec.agents.*, exec.okr.*, exec.consensus.*)
- Activate event publishing (deliberation, consensus, sign-off)
- Activate event subscriptions (workflow triggers)
- Test event flow end-to-end
- Target: All events publish/subscribe correctly

### KR5: Testing & Verification (20 hours)
- Run hermes-agent test suite with framework dependency
- Verify agent loading from LLDAP
- Verify NATS event publishing
- Test deliberation workflow
- Test consensus workflow
- Target: 100% tests pass, zero regressions

---

## Detailed Tasks (18 tasks)

### Phase 1: Setup (May 26-27) - 6 tasks
1. Add executive-agents-framework to requirements
2. Test import paths
3. Create wrapper module for framework exports
4. Verify all modules accessible
5. Set up editable install
6. Document framework wiring in README

### Phase 2: Delete Duplicates (May 28) - 4 tasks
7. Remove executive_agents_lldap_cqrs.py
8. Remove okr_atomic_creation.py 
9. Remove kanban_worker_executive_agent_actor.py
10. Remove associated test files

### Phase 3: Agent Registry Wiring (May 29-30) - 5 tasks
11. Load LldapAdapter from framework
12. Load LDAPAgentLocator from framework
13. Configure agent eligibility filters
14. Instantiate donald_knuth from LLDAP
15. Instantiate werner_vogels from LLDAP

### Phase 4: Event Bus Wiring (May 31-June 1) - 2 tasks
16. Wire NATSEventBus from framework
17. Configure JetStream subject hierarchy

### Phase 5: Testing (June 2) - 1 task
18. Verify end-to-end workflow and testing

---

## Success Criteria

| Criterion | Target |
|-----------|--------|
| Duplicate code | 0 files in hermes-agent/src/ |
| Framework imports | 100% successful |
| Agent loading | Both agents loaded from LLDAP |
| Event publishing | All events published successfully |
| Tests passing | 100% pass rate |
| No regressions | All existing features work |

---

## Dependencies

**Depends On:**
- Framework Modular Architecture OKR (if framework needs packaging)
- May use Framework OKR artifacts (packaging, PyPI setup)

**Blocks:**
- Framework OKR can proceed independently

---

## Accountable & Timeline

Accountable: jeff_dean (infrastructure)
Partner: demis_hassabis (architecture)
Timeline: May 26 - June 2, 2026 (8 days)
Effort: 140 hours

---

**Status: PROPOSED - Ready for validation + consensus**

This eliminates technical debt and ensures hermes-agent uses production-ready infrastructure.
