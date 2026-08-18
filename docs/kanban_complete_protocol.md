# Kanban Completion Protocol (VFE-COMPLETE-01)

## Overview

The completion protocol is the primitive that prevents the "committed-only-on-
worker-host" class of failure: a worker marks a task done claiming it
committed/deployed/shipped, but the commit was only on the worker's local
working copy (never pushed), or the artifact exists only on the worker host
(not propagated), or the service was never started.

Before this protocol, every such completion required manual operator
substrate-audit to catch. The protocol makes the `kanban_complete` primitive
catch it itself.

## The MANDATORY COMPLETION PROTOCOL preamble

Every dispatcher-spawned worker session receives the "MANDATORY COMPLETION
PROTOCOL" preamble as part of its system prompt, injected via
`KANBAN_GUIDANCE` in `agent/prompt_builder.py`. This is injected by the
system prompt builder, not left to skill loads — so every worker sees it
regardless of which skills are loaded.

The preamble instructs the worker to verify ALL of the following before
calling `kanban_complete`:

1. **ARTIFACTS** — every file created/modified listed in `artifacts` or
   `metadata.artifacts`, with paths that MUST exist on disk at completion.
2. **COMMITS** — every git commit pushed to origin and verifiable via
   `git log origin/<branch> --grep=<hash>`.
3. **DEPLOYED SERVICES** — for any service claimed running, the unit, PID,
   and start-timestamp recorded, verifiable via `systemctl is-active`.
4. **CROSS-HOST PROPAGATION** — if the deliverable must be on other hosts,
   verify the artifact exists on each target host.
5. **VERIFICATION EVIDENCE** — actual command output (git log, systemctl,
   curl, stat) pasted into `metadata.verification_evidence` as an audit trail.
6. **TEST PROOF** — test runner output pasted into
   `metadata.verification_evidence` when the task specifies coverage.

If ANY step cannot be truthfully answered YES, the worker must call
`kanban_block(kind='capability', reason='<what failed>')` instead of
`kanban_complete`.

## Structured metadata schema

`kanban_complete` accepts the following structured verification fields inside
`metadata`:

```python
metadata = {
    "artifacts": [str],                  # absolute paths — verified to EXIST
    "commit_hashes": [                   # each verified against git log
        {"repo": str, "branch": str, "hash": str,
         "verified_on_origin": bool}
    ],
    "deployed_services": [               # each verified with systemctl is-active
        {"host": str, "unit": str, "pid": int, "started_at": iso8601_str}
    ],
    "cross_host_propagation": [          # for artifacts consumed on other hosts
        {"artifact": str, "source_host": str,
         "target_hosts": [str], "mechanism": str, "verified": bool}
    ],
    "verification_evidence": {           # opaque dict of tool outputs — audit trail
        "systemctl_output": str,
        "curl_output": str,
        "git_log_output": str
    }
}
```

## Server-side verification pass

When the feature flag `kanban.complete_strict_verification` is `true` (Part 3),
`kanban_complete` runs a server-side verification pass on the completing host:

1. **Artifacts**: every path in `metadata.artifacts` must exist on disk.
2. **Commit hashes**: every entry in `metadata.commit_hashes` must return
   non-empty from `git log <branch> --grep=<hash>` (on `origin/<branch>` when
   `verified_on_origin` is true, else the local working copy).
3. **Deployed services**: every entry in `metadata.deployed_services` must
   return `active` from `systemctl is-active <unit>`. Remote-host services are
   verified via SSH.
4. **Cross-host propagation**: every entry in `metadata.cross_host_propagation`
   must have the artifact present on each target host (verified via SSH or
   locally). Entries with `verified: false` are treated as known failures.

If ANY check fails, `kanban_complete` raises `CompletionVerificationError`
(a `ValueError` subclass), the task stays in `running`, and a
`completion_blocked_verification` event is emitted for audit.

## Backward compatibility (grace period)

When the feature flag is `false` (default, grace period):

- `metadata` remains a free-form dict. No verification is run.
- A heuristic check scans the `summary`/`result` prose: if it mentions
  "created" / "committed" / "deployed" / "shipped" / "pushed" AND the
  metadata contains NO structured verification fields (`artifacts`,
  `commit_hashes`, `deployed_services`, `cross_host_propagation`, or
  `verification_evidence`), a WARNING comment is emitted on the task.
- The completion proceeds — the warning is advisory, not blocking.

Warning comment text:

> This completion claims artifacts in its summary but did not provide
> structured verification. See the RCA for the class of failure this pattern
> produces. Once `kanban.complete_strict_verification` is enabled, such
> completions will be REFUSED.

## Feature flag

```yaml
# ~/.hermes/config.yaml
kanban:
  complete_strict_verification: false  # default: OFF (grace period)
```

- **OFF** (default): grace period — warnings only, no refusals.
- **ON**: strict mode — `kanban_complete` refuses on verification failure.

## Rollout

1. **Phase 1 (day 1)**: ship with flag OFF; log warnings only.
2. **Phase 2 (day 2-3)**: collect warning stats; measure false-positive rate.
3. **Phase 2.5 (day 4)**: review warning data; sign-off for enable.
4. **Phase 3 (day 5+)**: flip flag ON; strict refusal active.
5. **Phase 4 (ongoing)**: use SQL over collected metadata to measure
   completion-honesty trends.

## Rollback

Set `kanban.complete_strict_verification: false` in `config.yaml`. The
strict gate stops firing; the grace-period heuristic continues. No schema
changes are needed — the structured metadata fields are optional and
ignored when the flag is off.

## Files changed

- `agent/prompt_builder.py` — MANDATORY COMPLETION PROTOCOL preamble in
  `KANBAN_GUIDANCE`.
- `hermes_cli/kanban_db.py` — `CompletionVerificationError`, verification
  helpers, strict gate in `complete_task`, grace-period heuristic.
- `hermes_cli/config_defaults.py` — `complete_strict_verification` flag.
- `tools/kanban_tools.py` — `CompletionVerificationError` handling in
  `_handle_complete`, schema description for new metadata fields.
- `tests/hermes_cli/test_kanban_complete_verification.py` — test coverage.