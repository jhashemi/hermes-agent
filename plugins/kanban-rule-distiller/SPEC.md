# VFE-NERVE-05 Implementation Specification

## Status
**Implementation Phase 1**: Core infrastructure scaffolded. Awaiting NERVE-01..04 completion for typed envelope schema and kanban context payload structure.

## Overview

VFE-NERVE-05 builds an **autonomous rule distillation pipeline** that observes when operators manually override kanban block-loops and proposes Event-Condition-Action (ECA) rules to prevent similar incidents autonomously in the future.

The pipeline is:
1. **Observation** — kanban completed events with operator_override markers
2. **Ingestion** — block history, timing, upstream task state
3. **Distillation** — LLM generates candidate rules (haiku-tier, structured JSON)
4. **Proposal** — kanban ticket for human ratification (RED gate)
5. **Ratification** — operator reviews and approves
6. **Persistence** — rule appended to `~/.hermes/kanban/eca_rules.yaml` (git-tracked)
7. **Execution** — NERVE-04 recheck job loads and fires matching rules

## Architecture

```
kanban.db (task_events)
    ↓
[RuleDistiller.poll_for_operator_overrides()]
    ├─ Filter: operator_override=true OR text-match
    ├─ Ingest: _get_task_block_history()
    ├─ Ingest: _get_upstream_context()
    │   ├─ Parents (if any)
    │   ├─ Recent completed tasks
    │   └─ Block recurrence metadata
    │
    ├─ Distill: _distill_rule_via_llm()
    │   ├─ Prompt: structured incident context
    │   ├─ Model: haiku-tier (cheap)
    │   └─ Output: JSON { rule_id, event, condition, action, audit_note }
    │
    └─ Propose: _propose_rule_as_ticket()
        ├─ Create kanban ticket
        ├─ assignee=operator (default: jeff_dean)
        ├─ priority=60
        ├─ body=rule preview + incident context
        └─ On completion → append to eca_rules.yaml

Ratification Workflow:
    Rule Proposal Ticket
        ↓
    [Operator reviews rule against 3+ similar incidents]
        ↓
    [Operator completes ticket with status=ready if PASS]
        ↓
    [Completion event triggers RuleDistiller._finalize_ratification()]
        ├─ Verify ticket completion status
        ├─ Append rule to eca_rules.yaml
        ├─ Update provenance: ratified_by, ratified_at
        ├─ Emit kanban_rules_ratified_total{outcome=pass}
        └─ Create audit event

Consumption:
    ~/.hermes/kanban/eca_rules.yaml
        ↓
    [NERVE-04 recheck job polls rules]
        ├─ For each task with unmet deps or repeated blocks:
        │   ├─ Check rule.condition against current state
        │   ├─ If match: execute rule.action
        │   └─ Emit kanban_rule_hits_total{rule_id}
        │
        └─ Metrics exported on :9097/metrics
```

## Data Schema

### Task Completion Event (operator_override)

```json
{
  "task_id": "t_c43af288",
  "status": "done",
  "completed_at": 1786919830,
  "run": {
    "id": 42,
    "outcome": "completed",
    "summary": "Operator authority: force-closed due to triage false-positive. Parent t_c43af287 completed at 22:30, unblocking this should have been automatic.",
    "metadata": {
      "operator_override": true,
      "reason": "triage_false_positive",
      "block_recurrence_count": 3,
      "last_block_kind": "dependency"
    }
  }
}
```

### Block History Event (context for distillation)

```json
{
  "task_id": "t_c43af288",
  "events": [
    {
      "kind": "blocked",
      "created_at": 1786919700,
      "payload": {
        "kind": "dependency",
        "reason": "waiting_for: [t_c43af287] (parents)",
        "waiting_for": ["t_c43af287"],
        "block_recurrences": 0
      }
    },
    {
      "kind": "block_loop_detected",
      "created_at": 1786919730,
      "payload": {
        "reason": "dependency | still waiting for t_c43af287 (4x)",
        "recurrences": 4
      }
    }
  ]
}
```

### ECA Rule (persisted in eca_rules.yaml)

```yaml
version: 1
rules:
  - id: cascade_parent_completion_v1
    event: kanban_block_loop_detected
    condition: |
      latest_block.kind == "dependency"
      AND latest_block.reason contains "t_c43af287"
      AND exists(completed_tasks, lambda t: t.id in ["t_c43af287"] and t.completed_within(300))
    action: auto_unblock
    audit_note: "Auto-healed by cascade_parent_completion_v1 rule"
    provenance:
      source_incident: t_c43af288
      distilled_at: "2026-08-16T22:32:00Z"
      distiller_model: haiku-4-5
      ratified_by: jeff_dean
      ratified_at: "2026-08-16T22:35:00Z"
      rule_version: 1
```

## LLM Distillation Prompt

```
You are an expert in block-loop recovery patterns in multi-agent task-execution systems.

An operator manually override-closed this kanban ticket due to a recurring block:

**Incident:**
  Task: {task_id}
  Title: {task_title}
  Block Pattern: {block_history_summary}

**Context:**
  - This task was blocked on: {block_reason}
  - Block recurred: {block_recurrence_count}x for the same cause
  - Parents: {parents_list}
  - Upstream completed in last 300s: {recent_completions_list}
  - Timing: first block → recurrence loop → operator override in {duration}s

**Operator's Summary:**
  "{summary_first_line}"

Propose a SINGLE brief ECA (Event-Condition-Action) rule that would have
automatically recovered from this pattern WITHOUT human intervention.

The rule must:
  - Trigger on a real event (e.g., kanban_block_loop_detected, kanban_completed, task_deadline_exceeded)
  - Define a predicate about current task state (e.g., latest_block.reason, parents[], timing)
  - Specify an action (auto_unblock, escalate, create_child_task, notify_operator)
  - Be conservative — false positives are worse than false negatives

Return ONLY valid JSON (no markdown, no explanation):
{
  "rule_id": "cascade_parent_completion_v1",
  "event": "kanban_block_loop_detected",
  "condition": "latest_block.kind == 'dependency' AND parent t_c43af287 completed recently",
  "action": "auto_unblock",
  "explanation": "Brief rationale why this rule would have helped"
}
```

## Implementation Phases

### Phase 1: Core Infrastructure (Current, t_e534fa8a)
- [x] RuleDistiller class with poll loop
- [x] Operator-override detection (metadata + text-match)
- [x] Block history ingestion
- [x] ECA rule dataclasses and YAML persistence
- [x] Test scaffolding
- [x] Systemd unit template
- [ ] LLM distillation integration (waiting on NERVE-01 context payload)
- [ ] Kanban ticket creation (waiting on NERVE-01..02)
- [ ] Ratification workflow (waiting on NERVE-03..04)

### Phase 1b: Dependency Waitlist
This task is blocked on VFE-NERVE-01..04:
- **NERVE-01** — typed envelope schema (block/complete event payloads with structured fields)
- **NERVE-02** — NATS event streaming (event bus wiring)
- **NERVE-03** — Notification pipeline (deliver rule proposals to operator)
- **NERVE-04** — Recheck & rule execution (consume eca_rules.yaml and fire matching rules)

Once NERVE-01 lands, NERVE-05 can integrate:
1. Structured block_history.waiting_for, block_history.unblocks fields
2. Kanban ticket creation via kanban_db.create_task()
3. Completion event watcher for rule ratification

### Phase 2: Full Integration (Post-NERVE-01..04)
- [ ] Wire LLM distillation endpoint
- [ ] Create rule proposal kanban tickets
- [ ] Implement ratification workflow
- [ ] Add metrics export (:9097)
- [ ] E2E test: replay t_c43af288 and verify rule proposal
- [ ] Operator training / runbook

### Phase 3: Tuning & Governance
- [ ] Collect metrics on rule hit rates
- [ ] Tune LLM prompt based on false positives
- [ ] Establish rule naming/versioning conventions
- [ ] Document operator runbook for rule reviews
- [ ] Add rule rollback / disable mechanism

## Dependencies

### Required (Present)
- `sqlite3` — kanban.db queries
- `pyyaml` — ECA rules serialization
- `asyncio` — event loop

### Optional (For Full Integration)
- `prometheus_client` — metrics export
- `anthropic` or other LLM provider — rule distillation
- `hermes_cli.kanban_db` — ticket creation (NERVE-02)
- NATS (via hermes plugins) — event streaming (NERVE-02)

## Configuration

### Environment Variables

```bash
HERMES_RULE_DISTILLER_POLL_INTERVAL=5         # seconds
HERMES_RULE_DISTILLER_MODEL=haiku-4-5          # LLM model name
HERMES_RULE_DISTILLER_LOG_LEVEL=INFO           # DEBUG|INFO|WARNING|ERROR
HERMES_RULE_DISTILLER_METRICS_PORT=9097        # prometheus scrape port
HERMES_KANBAN_BOARD=default                    # kanban board to watch
```

### File Paths

```
~/.hermes/kanban.db                            # read kanban events
~/.hermes/kanban/eca_rules.yaml                # persisted rules (git-tracked)
~/.config/systemd/user/kanban-rule-distiller.service
~/.hermes/bin/kanban-rule-distiller            # wrapper script
```

## Testing Strategy

### Unit Tests (Phase 1)
- [x] Operator-override detection (text + metadata)
- [x] ECA rule serialization
- [x] YAML load/save round-trip
- [ ] LLM prompt construction

### Integration Tests (Phase 2, after NERVE-01..04)
- [ ] Full distillation pipeline (LLM → proposal ticket)
- [ ] Ratification workflow (ticket completion → rule append)
- [ ] NERVE-04 rule execution (condition matching)
- [ ] Metrics tracking

### Regression Test (Before Merge)
- [ ] Replay t_c43af288 (motivating incident) and verify sensible cascade rule proposal
- [ ] Test on 3+ similar historical incidents
- [ ] Verify no false-positive proposals on clean task completions

## Safety / Governance

### RED Gate Violations (Checked Pre-Merge)

❌ **Never auto-apply rules without operator sign-off**
- Rule distiller creates *proposal* tickets (kanban_create)
- Operator explicitly ratifies via ticket completion
- No implicit acceptance or auto-deployment

❌ **No rule proposals without incident context**
- Every rule traces back to a source_incident (task_id)
- Provenance fully captured: distilled_at, distiller_model, ratified_by, ratified_at
- Audit trail in task_events for every rule ratification

❌ **No metrics that disclose operator workload without consent**
- Metrics track rule counts, hit rates, timing — NOT operator names or audit trails
- Operator privacy preserved (ratified_by is stored but not exposed in metrics)

### Pair Review Checklist

- [ ] LLM prompt is conservative (prefers false negatives over false positives)
- [ ] Condition DSL is readable by humans (not bytecode/AST)
- [ ] Rule rollback mechanism tested (systemctl stop + remove from yaml)
- [ ] Operator runbook drafted
- [ ] Metrics reviewed for privacy / confidentiality
- [ ] 3+ regression tests pass

## Reference

- **Motivating incident**: t_c43af288 (operator RCA 22:31 UTC 2026-08-16)
  > "Learning is manual. Every friction I solve should be extracted automatically."
- **Parent chain**: VFE-NERVE-01..04
- **Related**: VFE-WIRE-* (spine), VFE-FILTER-* (coherence filter), VFE-DIST-* (distribution)
