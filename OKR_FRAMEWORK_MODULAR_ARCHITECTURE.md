# OKR: Executive Agents Framework - Modular Architecture & Proper Separation

**Objective:** Relocate custom executive agent implementations from hermes-agent repo into proper modular executive-agents-framework structure with clear separation of concerns

**Accountable:** donald_knuth (architecture)  
**Partner Accountability:** jeff_dean (executive systems)  
**Timeline:** May 26 - June 15, 2026 (21 days)  
**Total Effort:** 185 hours  

---

## Problem Statement

Currently, custom implementations are scattered in `/home/ubuntu/hermes-agent/`:
- ❌ `src/okr_atomic_creation.py` (generic orchestration, not Hermes-specific)
- ❌ `src/kanban_worker_executive_agent_actor.py` (agent architecture, not Hermes-specific)
- ❌ Various domain models mixed with Hermes internals
- ❌ No clear separation between framework and platform

**Impact:**
- Hermes repo bloated with non-Hermes code
- Framework code harder to version/maintain independently
- Nous Research intellectual property mixed with agent platform
- Modular reusability compromised

**Solution:** Proper modular structure:
```
~/executive-agents-framework/
  ├─ src/
  │  ├─ orchestration/
  │  │  ├─ okr_atomic_creation.py (moved from hermes)
  │  │  ├─ goal_hierarchy.py
  │  │  └─ plan_lifecycle.py
  │  ├─ agents/
  │  │  ├─ executive_agent_actor.py
  │  │  ├─ kanban_worker_agent.py (moved from hermes)
  │  │  └─ consensus_engine.py
  │  ├─ storage/
  │  │  ├─ okr_repository.py
  │  │  ├─ goal_repository.py
  │  │  └─ plan_repository.py
  │  └─ __init__.py
  ├─ tests/
  ├─ docs/
  ├─ setup.py
  └─ pyproject.toml

hermes-agent/
  ├─ gateway/
  ├─ tools/
  ├─ plugins/
  └─ (NO executive agent implementation code)
```

---

## Key Results

### KR1: Framework Modularization (50 hours)
- Extract core orchestration from Hermes
- Create proper Python package structure (setup.py, pyproject.toml)
- Establish module boundaries (orchestration, agents, storage, domain)
- Target: 100% of framework code isolated from platform

### KR2: Code Migration (45 hours)
- Move okr_atomic_creation.py → framework/src/orchestration/
- Move kanban_worker_executive_agent_actor.py → framework/src/agents/
- Move domain models (Goal, Plan, OKR, etc.) → framework/src/domain/
- Create import shims in hermes-agent (backward compatibility)
- Target: 0 breaking changes, all tests pass

### KR3: Dependency Management (40 hours)
- Framework depends ONLY on core libraries (no hermes deps)
- Hermes depends on framework (unidirectional)
- Version framework independently (semantic versioning)
- Package for PyPI distribution
- Target: Framework installable as standalone package

### KR4: Integration Layer (35 hours)
- Hermes tools integrate with framework via public API
- Cron jobs reference framework (not internal code)
- CLI wrappers in hermes call framework functions
- Documentation of integration points
- Target: Clean separation with discoverable interfaces

### KR5: Testing & Verification (15 hours)
- Framework tests pass standalone (no hermes dependency)
- Hermes tests pass with framework dependency
- Integration tests verify both work together
- Target: 95%+ coverage, all scenarios green

---

## Detailed Breakdown (30 Tasks)

### Phase 1: Architecture Design (May 26-27) - 5 tasks
1. Design framework module structure and boundaries
2. Document dependencies and integration points
3. Create framework repo structure locally
4. Define public API surface for hermes integration
5. Plan migration sequence (which files move when)

### Phase 2: Framework Modularization (May 28-June 1) - 8 tasks
6. Create framework/src/orchestration/ (okr, goal, plan modules)
7. Create framework/src/agents/ (agent base, worker, consensus)
8. Create framework/src/storage/ (repositories, models)
9. Create framework/src/domain/ (value objects, aggregates)
10. Write framework/__init__.py with public API
11. Add setup.py and pyproject.toml
12. Set up framework-only tests (no hermes deps)
13. Verify framework tests pass standalone

### Phase 3: Code Migration (June 2-6) - 10 tasks
14. Move okr_atomic_creation.py to framework
15. Move kanban_worker_executive_agent_actor.py to framework
16. Move domain models (Goal, Plan, Step, etc.)
17. Move storage backends (DuckDB, memory)
18. Create import shims in hermes (backward compatibility)
19. Update hermes imports to use framework
20. Migrate cron jobs to reference framework
21. Migrate CLI commands to use framework API
22. Run hermes full test suite (verify no breakage)
23. Update documentation (import paths, architecture)

### Phase 4: Dependency Management (June 7-10) - 5 tasks
24. Remove framework deps from hermes/requirements.txt
25. Add framework as local/editable dependency
26. Test PyPI packaging (build, upload, install)
27. Create framework version strategy (semantic versioning)
28. Document framework upgrade path for hermes

### Phase 5: Integration & Testing (June 11-15) - 2 tasks
29. Full integration tests (framework + hermes together)
30. Final verification, documentation, cleanup

---

## Success Criteria

| Criterion | Metric | Target |
|-----------|--------|--------|
| Framework Isolation | Framework tests pass without hermes | 100% |
| Code Relocation | All non-hermes code in framework | 100% |
| Backward Compatibility | Hermes tests pass with framework | 100% |
| API Clarity | Public APIs documented | 100% |
| Test Coverage | Framework + Hermes combined coverage | ≥95% |
| Package Readiness | Framework installable from PyPI | Yes |
| Documentation | Migration guide + architecture | Complete |

---

## Benefits

✅ **Cleaner Separation of Concerns**
- Hermes = platform/gateway
- Framework = orchestration/agents (Nous Research IP)

✅ **Independent Versioning**
- Framework version: 1.0.0
- Hermes version: 0.7.x
- Can iterate independently

✅ **Reusability**
- Framework usable in other platforms
- Multi-agent orchestration available to customers

✅ **Maintainability**
- Framework tests run standalone
- Reduced coupling between projects
- Easier to onboard contributors

✅ **Intellectual Property**
- Clear boundary between Nous Research (framework) and Hermes
- Framework can be open-sourced or commercialized separately

---

## Files Affected

**Will Move from hermes-agent to framework:**
- src/okr_atomic_creation.py
- src/kanban_worker_executive_agent_actor.py
- src/okr_lifecycle_wiring_creation.py
- Domain models (Goal, Plan, OKR, KeyResult)
- Storage backends (DuckDB repository wrappers)
- Cron validators and monitors

**Will Remain in hermes-agent:**
- Gateway platform code
- Tool implementations
- CLI interface
- Hermes-specific plugins
- Agent dispatch logic

**Will Be Created:**
- framework/setup.py
- framework/pyproject.toml
- framework/src/__init__.py
- framework/README.md
- hermes-agent/import_shims.py (backward compat)

---

## Timeline

- **May 26-27:** Architecture design (5 tasks)
- **May 28-June 1:** Framework modularization (8 tasks)
- **June 2-6:** Code migration (10 tasks)
- **June 7-10:** Dependency management (5 tasks)
- **June 11-15:** Integration & testing (2 tasks)

**Milestone:** June 15 - Framework isolated, versioned, and ready for independent deployment

---

## Ownership

- **Donald Knuth** (accountable): Architecture, module design, API definition
- **Jeff Dean** (partner): Executive systems integration, testing strategy
- **Margaret Hamilton** (consulted): Testing framework, verification strategy
- **Donald Knuth** (consulted): Documentation, release planning

---

**Status: PROPOSED - Ready for validation and consensus check**
