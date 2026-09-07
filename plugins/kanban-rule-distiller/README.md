# Kanban Rule Distiller — VFE-NERVE-05

Watches operator-forced ticket closures and proposes Event-Condition-Action (ECA) rules for autonomous recovery.

## Architecture

```
kanban.db (completed events) ──→ Distiller ──→ LLM ──→ Proposal Ticket ──→ Ratification ──→ eca_rules.yaml
                                                              (RED gate)
```

## Components

### Distiller Service (`daemon.py`)

- **Polls** `kanban_completed` events every 5 seconds
- **Filters** for `operator_override=true` in metadata OR keywords in summary
  - `"operator authority"`
  - `"triage false-positive"`
  - `"operator override"`
  - `"forced closure"`
- **Ingests** context: block history, upstream completions, timing
- **Distills** rules via haiku-tier LLM call (structured JSON output)
- **Proposes** as kanban ticket for human ratification

### ECA Rules File (`~/.hermes/kanban/eca_rules.yaml`)

Versioned, git-tracked file consumed by NERVE-04 recheck job:

```yaml
version: 1
rules:
  - id: cascade_review_completion
    event: kanban_block_loop_detected
    condition: |
      latest_block.reason contains "review-required"
      AND there_exists(recent_completed, lambda t: t.title contains "REVIEW" and t.completed_within(300))
    action: auto_unblock
    audit_note: "Auto-healed by cascade_review_completion rule"
    provenance:
      source_incident: t_c43af288
      distilled_at: 2026-08-16T22:32:00Z
      distiller_model: haiku-4-5
      ratified_by: jeff_dean
      ratified_at: 2026-08-16T22:35:00Z
```

## Ratification Workflow (RED Gate)

1. **Distiller** creates proposal ticket with `assignee=operator` (default: `jeff_dean`)
2. **Operator** reviews the rule against 3+ similar incidents
3. **If PASS**: Operator completes the ticket with status=ready
4. **Completion** triggers rule append to `eca_rules.yaml`
5. **NERVE-04** recheck job loads rules and executes matching predicates

**Critical**: No rule auto-applies without operator sign-off. Ever.

## Configuration

### Environment Variables

- `HERMES_RULE_DISTILLER_POLL_INTERVAL` (default: 5) — seconds between polls
- `HERMES_RULE_DISTILLER_MODEL` (default: haiku-4-5) — LLM model for distillation
- `HERMES_RULE_DISTILLER_LOG_LEVEL` (default: INFO) — logging level
- `HERMES_KANBAN_BOARD` (default: current) — kanban board to watch

### Systemd Unit

```bash
# Install
cp systemd/kanban-rule-distiller.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable kanban-rule-distiller.service

# Start
systemctl --user start kanban-rule-distiller.service

# Logs
journalctl --user -u kanban-rule-distiller.service -f

# Stop (and prevent rule proposals)
systemctl --user stop kanban-rule-distiller.service
```

## Metrics

Service exports on `:9097/metrics`:
- `kanban_rules_proposed_total` — counter
- `kanban_rules_ratified_total{outcome="pass"|"fail"}` — counter
- `kanban_rule_hits_total{rule_id}` — counter
- `kanban_rule_distiller_poll_duration_seconds` — histogram

## Rollback

To stop proposals without affecting ratified rules:
```bash
systemctl --user stop kanban-rule-distiller.service
```

Ratified rules in `eca_rules.yaml` continue firing until removed/disabled.

## Testing

### Local Development

```bash
# Create test data
cd /home/ubuntu/hermes-agent
python -m pytest tests/kanban/test_rule_distiller.py -v

# Run daemon in foreground
HERMES_RULE_DISTILLER_POLL_INTERVAL=2 python plugins/kanban-rule-distiller/src/daemon.py
```

### Regression: VFE-NERVE-05

Replay `t_c43af288` (motivating incident) and verify the distiller generates a sensible cascade rule.

## Dependencies

- `hermes_cli.kanban_db` — kanban database functions
- `pyyaml` — ECA rules YAML serialization
- LLM provider (anthropic/openai/local) for rule distillation

## Governance

**RED zone** — this ticket adds autonomous rule generation. Every proposal ratifies through human approval. Never auto-apply.

**Pair review required** before merge (suggested: @hamilton).

See: AGENTS.md § "RED-ZONE pair review"

## Reference

- Motivating: operator RCA 22:31 UTC 2026-08-16 — "learning is manual; every friction I solve should be extracted automatically"
- Parent: VFE-NERVE-01..04
- Task: t_e534fa8a
