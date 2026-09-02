# scripts/dispatch/ — VCG Cluster Dispatcher Module

This directory is the **canonical git home** for the VCG routing brain
and its runtime companions. Files here are deployed to `~/.hermes/scripts/`
via `make deploy-dispatch`.

---

## Files tracked here

| File | Purpose |
|------|---------|
| `llm_cluster_dispatcher.py` | **Primary**: LLM-driven VCG cluster dispatcher (1359 LOC). Routes kanban tasks to hermes1/hermes2 via NATS-backed resource state, AMBER safety gates, and LLM scoring. |
| `hrv_node_state_reader.py` | **Sibling dependency**: NATS KV reader for per-node HRV probe state. Imported by name at runtime by `llm_cluster_dispatcher.py`. |
| `__init__.py` | Package marker (empty). Makes `scripts/dispatch/` importable as a package for tests. |

---

## Deployment

```bash
# Deploy to runtime location after a git pull:
make deploy-dispatch

# Verify no drift between repo and deployed copies:
make verify-dispatch

# Run unit tests:
make test-dispatch
```

The Makefile keeps the deployed copies at `~/.hermes/scripts/` byte-for-byte
identical to the in-repo copies. Any edit to the dispatcher **must** go through
this repo with a PR review — editing `~/.hermes/scripts/llm_cluster_dispatcher.py`
directly is allowed only in emergencies and must be followed immediately by
a `git diff` + commit to sync.

---

## Sibling scripts in `~/.hermes/scripts/` — canonical homes

The following scripts live in `~/.hermes/scripts/` but are **not** adopted into
this repo because they belong to other modules or are EAF-owned:

| Script | Canonical home | Notes |
|--------|---------------|-------|
| `_eaf_eventbus.py` | EAF repo (`executive_agents_framework/src/`) | EAF internal; deployed by EAF, not hermes-agent |
| `_eaf_reward.py` | EAF repo | EAF internal |
| `hrv_consensus_loop.py` | EAF repo (`scripts/`) | HRV subsystem; separate PR path |
| `hrv_pacemaker.py` | EAF repo (`scripts/`) | HRV subsystem |
| `hrv_pacemaker_leader.py` | EAF repo (`scripts/`) | HRV subsystem |
| `hrv_smoke.py` | EAF repo (`scripts/`) | HRV smoke test |
| `cluster_broadcast_listener.py` | EAF repo (`scripts/`) | EAF cluster event bus |
| `cluster_convergence_hermes.py` | EAF repo (`scripts/`) | EAF convergence driver |
| `cpu_watchdog.py` | hermes-agent (`scripts/watchdog/`) | TODO: adopt in a follow-up |
| `drift_scanner.py` | hermes-agent (`scripts/`) | TODO: confirm tracked location |
| `accountability_sweep.py` | EAF repo | OKR accountability subsystem |
| `board_verdict_*.py` | EAF repo | Board verdict pipeline |
| `consensus_reactor.py` | EAF repo | Consensus subsystem |
| `okr_*.py` | EAF repo | OKR subsystem |
| All others | Various / undocumented | See `~/.hermes/scripts/` for full list |

Scripts marked "TODO: adopt" should be tracked in a follow-up hygiene card.
The dispatcher and HRV reader are prioritized because they are the most
safety-critical (dispatcher = single point of task routing; reader = NATS
state gate).

---

## Why this directory exists

`llm_cluster_dispatcher.py` was previously untracked (`~/.hermes/scripts/` only),
which meant:
- Edits had no git history or PR review path
- A disk corruption or `rm` would permanently lose the routing brain
- The `commit_hash` verifier on fix cards had nothing to reference

This was adopted in kanban task `t_b38ff45a` as a pure hygiene move.
No behavior changes were made during adoption.
