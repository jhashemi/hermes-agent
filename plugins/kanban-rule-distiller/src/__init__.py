"""Kanban Rule Distiller Service.

VFE-NERVE-05: Observes operator-forced ticket closures and proposes
Event-Condition-Action (ECA) rules for autonomous handling. Rules are
distilled via LLM and presented as kanban tickets for human ratification
(RED gate — never auto-apply).

Service topology:
  1. Watches: kanban_completed events from SQLite
  2. Filters: operator_override=true OR "operator authority"/"triage false-positive" in summary
  3. Distills: rule proposals via structured LLM call
  4. Proposes: new kanban ticket (assignee=operator, priority=60)
  5. Ratifies: on ticket completion → append to ~/.hermes/kanban/eca_rules.yaml
  6. Metrics: exported on :9097/metrics

Environment:
  HERMES_RULE_DISTILLER_POLL_INTERVAL  (default: 5)
  HERMES_RULE_DISTILLER_MODEL          (default: haiku-4-5)
  HERMES_RULE_DISTILLER_METRICS_PORT   (default: 9097)
  HERMES_KANBAN_BOARD                  (default: current)
"""

__version__ = "0.1.0"
__all__ = ["RuleDistiller", "ECARule", "RuleProposal"]
