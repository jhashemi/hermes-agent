# Post-Mortem: OKR-2026-Q2 Kanban Dispatch Loop (May 20-21, 2026)

## Incident Summary
The okr-2026-q2 kanban board auto-dispatch reported identical numbers (10 backlog, 5 in progress, 3 review, 5 done) for 24+ hours with zero actual progress. Workers spawned but never completed tasks.

## Timeline
- **May 20, ~20:00 UTC**: pass.wafer.ai 429 rate limit hit (2000 req/5h window exhausted)
- **May 20-21**: Workers spawn → 429 at wafer.ai → fallback to ollama-cloud → also 429 (weekly limit) → fallback to local ollama → no model → worker exits rc=0 → "protocol violation"
- **May 21, ~20:23 UTC**: Schema fix deployed (executescript crash loop resolved)
- **May 21, 20:23+ UTC**: Dispatcher now ticks successfully, spawns 12-28 workers/tick
- **May 21, 20:26+ UTC**: `spawned=12, crashed=14, auto_blocked=2` → cycle repeats
- **Every tick**: Tasks get reclaimed, respawned, crash, blocked → unblocked → repeat

## Root Cause Chain

### Primary: All model providers exhausted simultaneously
| Provider | Status | Limit | Error |
|----------|--------|-------|-------|
| pass.wafer.ai | EXHAUSTED | 2000 req/5h | Concurrency limit |
| ollama-cloud | EXHAUSTED | Weekly limit | Usage limit reached |
| kimi-coding | EXHAUSTED | Billing cycle | Usage limit reached |
| hermes1 local ollama | NO MODEL | N/A | 0 models pulled |
| hermes2 local ollama | NO MODEL | N/A | 0 models pulled |

### Secondary: Dispatcher overspawns relative to quota
- 12-28 workers per tick × ~36 API calls per worker = **400-1000 calls per tick**
- 5h rate limit: 2000 requests → **2 ticks exhaust the entire window**
- Circuit breaker trips at consecutive_failures=2, moves to "blocked"
- Tasks unblock after cooldown → cycle repeats infinitely

### Tertiary: Worker exit code misreporting
- Workers exit with rc=0 after exhausting retries (should be rc=1)
- Dispatcher interprets rc=0 as "worker answered conversationally without completing"
- Logged as "protocol violation" — misleading diagnosis
- Real issue: worker couldn't reach any model provider

## Fixes Applied
1. **Pulled qwen3:4b on hermes1 ollama** — local fallback now functional
2. **Schema fix** (documented separately) — kanban tick no longer crashes

## Fixes Needed
1. **Dispatcher concurrency cap**: max N concurrent workers based on rate limit budget
2. **Model quota awareness**: dispatcher should track 429s and throttle spawning
3. **Worker exit code**: rc=1 on provider exhaustion (not rc=0)
4. **Local model fallback**: pull at least one model on both machines' ollama
5. **OpenRouter as fallback tier**: hermes2 has OpenRouter key (not exhausted)

## Prevention (OKR KRs)
- KR1: Gateway health cron (5min) ✅ already deployed
- KR2: Schema migration test ✅ already passes
- KR3: SCHEMA_SQL linter ✅ already passes  
- KR4: DuckDB migration (eliminates per-board schema drift)
- KR5: RAFT consensus (multi-machine state sync)

## Action Items
| # | Action | Priority | Owner |
|---|--------|----------|-------|
| 1 | Pull glm-5.1 or qwen3:14b on both ollamas | P0 | infra |
| 2 | Add `kanban.max_concurrent_workers` config | P0 | dispatcher |
| 3 | Fix worker exit code on provider exhaustion | P1 | run_agent.py |
| 4 | Add rate-limit budget tracking to dispatcher | P1 | kanban_db.py |
| 5 | Add OpenRouter as fallback tier on hermes1 profiles | P1 | profiles |
| 6 | Unstick blocked okr-2026-q2 tasks (reset consecutive_failures) | P0 | operations |