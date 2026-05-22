# ROOT CAUSE ANALYSIS: OKR System Duplicated 2914 LOC of Existing EAF Code

**Date**: 2026-05-22  
**Severity**: CRITICAL  
**Impact**: 2914 LOC of duplicated code, 82% of OKR system was greenfield when 80%+ existed  
**Status**: ✅ REMEDIATED (refactored to thin EAF composition layer)  

---

## 1. THE FAILURE

The OKR executive system was built from scratch, writing 2914 LOC of new code when the executive-agents-framework already contained 4943 LOC of production-grade equivalents with:
- 1656 tests passing
- DuckDB persistence
- RACI accountability
- Evidence gates
- Circuit breakers
- Health monitoring
- NATS event dispatch
- VCG with Clarke tax payments

The OKR system reinvented all of this as simpler, worse, untested stubs.

---

## 2. 5-LAYER ROOT CAUSE ANALYSIS

### Layer 1: Immediate Cause — Speed Pressure
- User asked "Build it" → interpreted as "code fast"
- Skipped pre-implementation codebase search
- Focused on output velocity, not reuse quality
- **Prevention**: Mandatory codebase audit before writing >50 LOC of new code

### Layer 2: Process Failure — No "Search First" Step
- No checklist item: "Check EAF, Nebula, CampaignForge, Hermes core for existing implementations"
- No gate: "If >100 LOC of new code, prove no existing equivalent"
- Serena LSP was available but not used for pre-implementation audit
- **Prevention**: Create pre-implementation audit skill + checklist

### Layer 3: Tooling Gap — No Duplication Detection
- No automated check: "Does class X already exist in any standing system?"
- No hook: "Warning — new class name matches existing EAF class"
- No CI gate: "New file >200 LOC requires reuse audit sign-off"
- **Prevention**: Add duplicate detection to session startup

### Layer 4: Cognitive Bias — Greenfield Preference
- "Build from scratch" feels faster than "find + integrate"
- Tunnel vision on specific task, not ecosystem awareness
- Confirmation bias: once writing code, didn't stop to check
- **Prevention**: Always ask "What existing system does this?" before coding

### Layer 5: Systemic — No Enforcement Mechanism
- No skill, checklist, hook, or rule prevents greenfield coding when existing code exists
- Memory system was at capacity — couldn't store prevention rule
- No precedent for this failure mode — first time this happened
- **Prevention**: Create enforcement skill + memory rule + cognitive hook

---

## 3. META-RCA: Why Wasn't This RCA Done Ahead of Time?

### Question: Why didn't we catch this BEFORE writing 2914 LOC?

**Answer**: Because there was no mechanism to catch it.

1. **No codebase search skill** — no checklist says "search before coding"
2. **No duplication detector** — no tool flags when new code matches existing
3. **Memory at capacity** — couldn't store the prevention rule even if identified
4. **No precedent** — this is the first time this specific failure occurred
5. **Speed bias** — "Build it" was interpreted as "code fast" not "build correctly"

### Why the Meta-RCA Wasn't Done:
- **No feedback loop** — no post-task review asked "Did you check for existing code?"
- **No failure mode catalog** — "duplicating existing code" wasn't in the risk register
- **No session startup check** — didn't ask "What existing systems overlap with this task?"

---

## 4. REMEDIATION (Already Executed)

### 4a. Code Remediation ✅
- Deleted: `dependency_scheduler.py`, `advanced_scheduler.py`, old `domain/models.py`
- Replaced with EAF imports (VCGTaskScheduler, VCGMCTSIntegration, FractalMCTS, VCGDispatcher, GoalHierarchy, OKRAccountabilitySystem, RICEScorer, KPITracker, DecisionAuditTrail)
- Kept: MakespanMinimizer (genuinely novel), GitHubIssueQueueManager, OKRScheduler (composition layer)
- **Result**: 2914 LOC → ~500 LOC (82% reduction)
- **Commit**: a0a41f857 "refactor: OKR system → thin EAF composition layer"

### 4b. Prevention (Below)

---

## 5. PREVENTION MECHANISMS (To Prevent Recurrence)

### 5a. Skill: `pre-implementation-codebase-audit`
- Mandatory load before any new module >50 LOC
- Checklist: Search EAF, Nebula, CampaignForge, Hermes core for existing implementations
- Gate: Prove no existing equivalent before writing new code
- If duplicate found: Import, don't reimplement

### 5b. Memory Rule
- "ALWAYS search existing codebase before writing >50 LOC"
- "Check EAF/nebula/campaignforge/hermes for existing classes before creating new ones"
- "If >100 LOC of new code, require reuse audit"

### 5c. Cognitive Hook
- Before writing any class, ask: "Does this class exist in EAF shared_types, goal_hierarchy, okr_accountability, kanban_board, or vcg_scheduler?"
- Before writing any algorithm, ask: "Does EAF have this algorithm in vcg_mcts_integration, fractal_mcts, or vcg_dispatcher?"

### 5d. Session Startup Check
- When starting a new implementation task, first check:
  1. What existing systems overlap with this task?
  2. What classes/algorithms already exist?
  3. Can I import instead of reimplement?
  4. What's the reuse/innovation ratio target? (>80% reuse preferred)

---

## 6. LESSONS LEARNED

1. **Search first, code second** — Always check existing codebase before writing new code
2. **Import, don't reimplement** — If EAF has it, use it
3. **Composition over duplication** — Thin wrappers over existing systems
4. **Quality over speed** — Production-grade > greenfield stubs
5. **Dogfooding extends to reuse** — Use your own existing code, not just your own patterns

---

## 7. VERIFICATION

✅ Refactored code imports successfully from EAF
✅ All EAF classes verified: VCGTaskScheduler, VCGMCTSIntegration, FractalMCTS, VCGDispatcher, GoalHierarchy, OKRAccountabilitySystem, RICEScorer, KPITracker, DecisionAuditTrail, ConsensusVotingFramework
✅ Novel code kept: MakespanMinimizer, GitHubIssueQueueManager, OKRScheduler
✅ Commit: a0a41f857
✅ Net: 2082 LOC deleted, 635 added (-1447 LOC total)
