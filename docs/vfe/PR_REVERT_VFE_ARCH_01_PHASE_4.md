# PR: Revert 6 VFE-* core edits — VFE-ARCH-01 Phase 4

**Branch**: `revert/vfe-arch-01-phase4`
**Base**: `origin/main` @ `28863aca96`
**Ticket**: `t_f0a3fdb9` (parent `t_320305b6` VFE-ARCH-01)
**Author**: jeff_dean (executive agent) — infra sign-off
**Reviewer**: operator / margaret_hamilton (Phase 3 gate sign-off already recorded on `t_81169aac`)
**Risk tier**: BLACK (deployment coordination required)

## Summary

Removes the 6 VFE-* commits merged to `origin/main` during Phases 1–2 of VFE-ARCH-01. All 6 have been replaced by out-of-tree plugins (5) plus a worker skill (1). Phase 3 parity gate PASSED on 2026-08-17 (attempt 2) — see `docs/vfe_plugin_parity_report.md` on the parity branch and the parent ticket completion metadata.

## Commits reverted (newest → oldest on main)

| Reverted SHA | Original ticket | Core surface removed |
|---|---|---|
| `28863aca96` | VFE-ROUTE-01 | `hermes_cli/convene_worker.py`, `kanban.py` convene routing, `kanban_db.py` convene fields |
| `ce13fcbc18` | VFE-PULSE-01 | `hermes_cli/hrv_velocity_cache.py`, dispatcher hrv.pulse.tick consumption |
| `83b18cfd96` | VFE-METRICS-01 + VFE-NERVE-FIX-01 | `web_server.py` /metrics/vfe counter, kanban block-refusal metrics |
| `1409f1a37e` | VFE-COG-01 | `agent/cognitive_heartbeat.py`, `plugins/memory/cognitive/__init__.py` predict-then-declare gate |
| `8cf7c2e7f7` | VFE-NERVE-01 (part 2 / NERVE-MERGED) | `kanban_db.py` typed handoff persistence, cascade unblock |
| `c36020b5af` | VFE-NERVE-01 (part 1) | `tools/kanban_tools.py` typed block/complete envelopes |

Total diff vs. origin/main: **22 files changed, 109 insertions(+), 4353 deletions(-)** — see `git diff --stat origin/main..HEAD` from the revert branch.

## Plugin replacements (installed at `~/.hermes/plugins/` on hermes2)

| Feature | Replacement | Type | Status |
|---|---|---|---|
| VFE-ROUTE-01 | `vfe-convene` plugin | plugin | on-disk, discovered, 49/52 tests pass (3 outdated) |
| VFE-COG-01 | `vfe-heartbeat` plugin | plugin | on-disk, discovered, 38/38 tests pass |
| VFE-METRICS-01 | `vfe-metrics` plugin | plugin | on-disk, HTTP :9120/metrics/vfe live, 26/26 tests pass |
| VFE-NERVE-01 | `nerve-cascade` skill | worker skill | on-disk at `~/.hermes/skills/nerve-cascade/`, 125/125 parity checks pass |
| VFE-PULSE-01 | (subsumed into vfe-heartbeat) | plugin | (see heartbeat) |
| kanban_complete protocol | `vfe-complete-protocol` plugin | plugin | on-disk, discovered, 66/66 tests pass |
| logs_unified | `vfe-logs` plugin | plugin | on-disk, discovered, systemd timer active, 26/26 tests pass |

Parity gate verdict: **PASS** — 5/5 physical plugins + 1 skill meet acceptance criteria. Chaos test (concurrent plugin load + daemon thread termination) PASS: gateway `/health` stays HTTP 200 throughout.

## Test evidence on this revert branch

**Blast-radius targeted run** (kanban_db, kanban_cli, kanban_tools, kanban_core_functionality, kanban_lifecycle_hooks, kanban_dispatch_lock): **88/88 pass** in 13.2s.

**Full suite** (`bash scripts/run_tests.sh`, 8-way per-file parallel, ~70 min): 106 tests fail across 36 files. Baseline delta:

- `origin/main` (unmodified) exercised on the same 36 files: **103 tests fail across 33 files** (pre-existing environmental — lldap YAML config missing on this host, daytona / modal / cluster dispatch tests need external services, telegram/discord/whatsapp skill-file probes need runtime gateway state, macos-launcher on linux, etc.).
- Revert branch: **106 tests fail across 36 files**. 
- **Net delta: +3 tests in +3 files.**

The 3 net additions are all flaky under 8-way load — all 3 files rerun single-slice pass 16/16, 15/15, 3/3. `test_zombie_process_cleanup.py::test_timed_out_child_keeps_relay_session_until_its_turn_exits` is timing-sensitive on a 0.3–0.7s threading event; the parallel runner even auto-retried it and got 16/16 green on the retry pass. No revert-caused regression.

## Static verification

- `grep -rn 'cognitive_heartbeat\|convene_worker\|hrv_velocity_cache'` across `.py` files (excluding tests): **zero matches** — no leftover imports of removed modules.
- Full-tree `ast.parse` sweep on all `*.py`: **0 syntax errors**, 3 SyntaxWarnings (pre-existing raw-string patterns unrelated to revert).

## Deployment coordination (BLACK tier)

**Ordering (per ticket procedure step 6):**
1. Verify all 5 plugins present on target gateway host (hermes2, this box): `ls ~/.hermes/plugins/ | grep vfe-` → **already present** (vfe-cluster-sync, vfe-escalation-router, vfe-metrics, vfe-logs, vfe-convene, vfe-complete-protocol, vfe-heartbeat).
2. Verify `nerve-cascade` skill on all worker profiles: `ls ~/.hermes/skills/nerve-cascade/` → **present** (SKILL.md).
3. Merge this revert PR to `origin/main`.
4. Restart gateway to load plugins into the new core-less runtime: `systemctl --user restart hermes-gateway.service` (per Phase 3 non-blocking followup #1 — current pid 1456951 started before plugins existed, `/proc/1456951/maps` has zero vfe-* modules).
5. Smoke: hit `/health` (HTTP 200), `/metrics/vfe` on :9120 (real counter data), confirm at least one kanban task can be created + completed under the new plugin path.

**Rollback**: `git revert` this revert (single command). Restores the 6 core commits atomically.

## Non-blocking follow-ups (from Phase 3 report, tracked separately)

- `margaret_hamilton` profile's `plugins.enabled` lists 3 of 5 vfe-* plugins (missing vfe-convene, vfe-metrics). Housekeeping ticket after Phase 4.
- `vfe-convene` has 3 outdated tests (assume pre-revert `kanban_db.create_task(convene_spec=…)` kwarg). Plugin runtime handles post-revert path correctly; skipif-pre-revert-only cleanup ticket after Phase 4.

## Sign-offs

- **Hamilton** (Phase 3 chaos test + parity gate): recorded on `t_81169aac` metadata `hamilton_signoff: true`, report commit `a25650b` on parity branch.
- **Jeff** (infra revert + test delta analysis): this ticket, run `514`, completion pending.
