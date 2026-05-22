# OKR SYSTEM DUPLICATION AUDIT - 2026-05-22

## CRITICAL FINDING: OKR System Duplicates 2914 LOC of Existing EAF Code

### Duplications (DELETE, import EAF instead):

| OKR File | LOC | EAF Equivalent | LOC | Match |
|----------|-----|---------------|-----|-------|
| dependency_scheduler.py | 196 | VCGTaskScheduler | 87 | EXACT |
| advanced_scheduler.py MCTS | 150 | FractalMCTS + VCGMCTSIntegration | 590 | HIGH |
| advanced_scheduler.py VCG | 100 | VCGDispatcher + VCGAuctionMechanism | 1131 | HIGH |
| advanced_scheduler.py Hybrid | 130 | VCGMCTSIntegration | 277 | EXACT |
| planning_engine.py RICE | 80 | RICEScorer | 41 | EXACT |
| planning_engine.py goals | 196 | GoalHierarchy | 628 | HIGH |
| execution_engine.py | 257 | VCGTaskScheduler + VCGDispatcher | 788 | HIGH |
| review_engine.py | 265 | PairCodingTeamExecutiveAgent | (pair_events) | HIGH |
| metrics_engine.py | 234 | OKRAccountabilitySystem + KpiTracker | 909 | HIGH |
| domain/models.py | 249 | shared_types.py | 197 | HIGH |

### Genuinely Novel (KEEP):
- MakespanMinimizer (~100 LOC) — critical-path, no EAF equivalent
- GitHubIssueQueueManager (~50 LOC) — GitHub integration, no EAF equivalent
- Orchestrator glue (~50 LOC) — thin composition

### Also Available in Nebula-deep-v5:
- VCGAuctionMechanism (430 LOC) — reserve price, combinatorial auction
- StandardMCTS (265 LOC)
- SelfReinforcingMCTS (435 LOC)
- SelfModifyingMCTS (575 LOC)
- HybridRecursiveMCTS (721 LOC)

### Refactor Plan:
1. Add EAF as dependency to pyproject.toml
2. Delete duplicated files
3. Refactor engines as thin composition over EAF
4. Keep MakespanMinimizer as novel contribution
5. Wire DuckDB via EAF's DurableStorageManager
6. Run EAF's 1656 tests to verify
