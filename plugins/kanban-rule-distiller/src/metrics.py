"""Prometheus metrics for the rule distiller service."""

from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge

# Proposal lifecycle
kanban_rules_proposed_total = Counter(
    "kanban_rules_proposed_total",
    "Number of rule proposals generated",
    ["outcome"],  # distilled | failed_distill | failed_propose
)

kanban_rules_ratified_total = Counter(
    "kanban_rules_ratified_total",
    "Number of rule proposals ratified by operator",
    ["outcome"],  # pass | fail (pass = rule appended to yaml)
)

kanban_rule_hits_total = Counter(
    "kanban_rule_hits_total",
    "Number of times a rule fired and took action",
    ["rule_id"],
)

# Performance
kanban_rule_distiller_poll_duration_seconds = Histogram(
    "kanban_rule_distiller_poll_duration_seconds",
    "Time taken for one poll cycle (seconds)",
)

kanban_rule_distiller_llm_call_duration_seconds = Histogram(
    "kanban_rule_distiller_llm_call_duration_seconds",
    "Time taken for LLM rule distillation call (seconds)",
)

# State
kanban_rule_distiller_proposals_pending = Gauge(
    "kanban_rule_distiller_proposals_pending",
    "Number of rule proposals awaiting operator ratification",
)

kanban_rule_distiller_rules_active = Gauge(
    "kanban_rule_distiller_rules_active",
    "Number of active (ratified) ECA rules",
)
