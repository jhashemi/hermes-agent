# VFE-COMPLETE-01 — verification_venv completion gate (FIX-3)

**Task:** `t_906fe15d` on board `adr-006b-phase-2`
**Depends on:** `t_6bf7daef-completion-evidence-gate` (the `kanban_task_completing` pre-hook seam)
**Scope:** Kanban worker completion protocol — enforce that every completion declares WHICH Python interpreter was used to verify its claims.

## Why

The completion-theater RCA (2026-08-22) documented four+ cases where workers called `kanban_complete()` with claims that independently failed verification. A common thread: workers built ad-hoc scratch venvs that reported different `pytest --collect-only` counts than the canonical framework venv (concrete instance: 9877 tests in scratch venv vs 7409 tests in canonical framework venv). Under the previous evidence-gate seam, workers could pass structured metadata checks (artifacts exist, commits pushed, systemd units active) while still verifying their claims in a completely different interpreter than the one running the framework — the tests they claimed passed weren't necessarily the tests the framework cares about.

FIX-3 closes that loophole by requiring every completion to name its `verification_venv` in the metadata and gating that value against an operator-maintained allowlist.

## Contract

Every call to `kanban_complete()` MUST pass a `metadata` dict containing:

```python
metadata = {
    ...  # existing evidence-gate fields (artifacts, commit_hashes, ...)
    "verification_venv": "/absolute/path/to/python",
}
```

The value must be:

1. Present (not `None`, not missing).
2. A `str`.
3. An **absolute path** (not a relative path, not empty, not whitespace-only).
4. On the `kanban.canonical_venvs` allowlist in `~/.hermes/config.yaml` (when the allowlist is non-empty and enforcement is on).

## Three verdicts

The pure grader — `hermes_cli.kanban_completion_venv.grade_verification_venv` — returns one of three verdicts:

| Verdict | Trigger | Effect on completion |
|---|---|---|
| `ok` | Field present, absolute, on the allowlist (OR allowlist is empty) | Completion proceeds. |
| `veto_missing` | Field missing, non-string, empty, or relative path | **HARD VETO**: `CompletionEvidenceRejected` raised, task stays `running`, `completion_blocked_evidence` audit event emitted. Worker retries with corrected metadata. |
| `downgrade_non_allowlist` | Field is an absolute path but NOT on the allowlist | Completion **proceeds** (soft downgrade). The observer hook flags the task as `awaiting-verification` via a comment; operators filter these for manual review. |

The three-tier design is intentional: **shape errors** (missing / malformed field) are hard blocks because they represent a worker that did not follow the protocol at all. **Provenance errors** (worker used an unrecognized venv) are soft because a worker may have a legitimate reason to verify in a non-canonical env (e.g., a fresh Docker sandbox for a hostile-network task) and the operator should adjudicate.

## Configuration

Two independent flags in `~/.hermes/config.yaml`:

```yaml
kanban:
  # Master switch. Default: false (grace period — plugin abstains).
  # Flip to true only after workers have been retrained on the protocol
  # AND the canonical_venvs allowlist below is populated.
  enforce_completion_venv: false

  # Allowlist of canonical Python interpreters. Empty list is equivalent
  # to "shape check only" — any absolute path counts as ok. Populate
  # with the exact interpreter paths the framework expects.
  canonical_venvs:
    - /home/ubuntu/executive_agents_framework/.venv/bin/python
    - /home/ubuntu/hermes-agent/venv/bin/python
    - /home/ubuntu/executive_agents_platform/.venv/bin/python
```

**Rollout order (mandatory):**

1. Deploy this hermes-agent commit (adds the pure helper and the seam wiring).
2. Update the `vfe-complete-protocol` plugin to register `completing_hook` on the `kanban_task_completing` hook.
3. Populate `kanban.canonical_venvs` with real paths.
4. Retrain workers (VFE-COMPLETE-01 skill update — see below).
5. Flip `enforce_completion_venv: true`.

Skipping step 4 will produce a spike in `completion_blocked_evidence` events until every worker's completion prompt is updated. Skipping step 3 will leave the plugin in "shape check only" mode — better than nothing but not full enforcement.

## Wiring diagram

```
  worker calls kanban_complete(metadata={"verification_venv": "..."})
                    │
                    ▼
  hermes_cli.kanban_db.complete_task()
                    │
                    ▼
  _collect_completing_veto() → invoke_hook("kanban_task_completing", ...)
                    │
                    ▼
  vfe-complete-protocol plugin's registered callback:
     hermes_cli.kanban_completion_venv.completing_hook(...)
                    │
                    ▼
  load_enforce_completion_venv_flag() ─── off → return None (abstain)
                    │ on
                    ▼
  load_canonical_venvs() → allowlist
                    │
                    ▼
  grade_verification_venv(metadata, allowlist)
       │              │                │
       ▼              ▼                ▼
      ok         downgrade         veto_missing
       │              │                │
       ▼              ▼                ▼
     None           None       {"veto": True, "reason": ...}
       │              │                │
       ▼              ▼                ▼
  complete       complete         raise CompletionEvidenceRejected
                                  → task stays running
                                  → audit event emitted
```

## Testing

The hermes-agent test suite covers this contract in
`tests/hermes_cli/test_kanban_completion_venv_gate.py` (28 tests):

- **13 unit tests** for `grade_verification_venv` (every branch: missing / non-dict / non-string / empty / relative / empty-allowlist / hit / miss / whitespace / stripping / enum values).
- **6 unit tests** for the `completing_hook` wrapper (enforce off / enforce on × verdict matrix, plus the "extra kwargs tolerated" contract).
- **7 integration tests** that register the callback on the real `kanban_task_completing` seam via `_patch_invoke_hook` and drive `kb.complete_task` end-to-end (missing → veto, non-allowlist → complete, allowlist hit → complete, retry after veto → complete, enforce-off → complete, empty allowlist → complete, audit event shape).
- **2 smoke tests** for the config loaders (return types).

Run just this suite:

```
pytest tests/hermes_cli/test_kanban_completion_venv_gate.py -v
```

The complementary evidence-gate tests in `tests/hermes_cli/test_kanban_completion_evidence_gate.py` (9 tests) MUST also pass — they cover the underlying seam that this gate plugs into.

## Where the code lives

- `hermes_cli/kanban_completion_venv.py` — pure helper module. `grade_verification_venv`, `VenvVerdict`, `VenvCheckResult`, config loaders, and `completing_hook` (the callback the plugin registers).
- `hermes_cli/kanban_db.py` — pre-existing `kanban_task_completing` seam (restored by this commit; see the seam-restore commit message for the merge-drop RCA).
- `~/.hermes/plugins/vfe-complete-protocol/__init__.py` — plugin registration (one-liner to register `completing_hook` — deployment step, not part of hermes-agent).
- `tests/hermes_cli/test_kanban_completion_venv_gate.py` — test coverage described above.

## Related tickets

- `t_b7ae4edf` — original VFE-COMPLETE-01 plugin (observer hook, evidence-shape checks).
- `t_6bf7daef-completion-evidence-gate` — pre-completion veto SEAM (the seam this gate plugs into).
- `t_906fe15d` — THIS ticket (FIX-3: verification_venv policy on top of the seam).
