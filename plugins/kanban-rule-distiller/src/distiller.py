"""ECA rule distiller — watches operator-forced closures and proposes rules."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple
from datetime import datetime, timedelta

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ECARule:
    """Event-Condition-Action rule for autonomous block recovery."""
    
    id: str  # e.g., "cascade_review_completion"
    event: str  # e.g., "kanban_block_loop_detected"
    condition: str  # DSL/pseudocode predicate
    action: str  # Action name (e.g., "auto_unblock")
    audit_note: str
    provenance: dict[str, Any]  # source_incident, distilled_at, distiller_model, ratified_by, ratified_at
    
    def to_dict(self) -> dict:
        """Serialize to YAML-compatible dict."""
        return asdict(self)


@dataclass
class RuleProposal:
    """Candidate rule proposal before ratification."""
    
    source_incident: str  # task ID that triggered distillation
    rule: ECARule
    proposal_ticket: Optional[str] = None  # kanban ticket id when created
    ratified: bool = False
    ratified_at: Optional[datetime] = None
    ratified_by: Optional[str] = None


class RuleDistiller:
    """Service that watches operator-forced closures and distills ECA rules."""
    
    def __init__(
        self,
        kanban_db_path: Optional[Path] = None,
        eca_rules_path: Optional[Path] = None,
        model: str = "haiku-4-5",
        poll_interval: float = 5.0,
        provider_base_url: Optional[str] = None,
        provider_api_key: Optional[str] = None,
        provider_name: Optional[str] = None,
    ):
        """Initialize the rule distiller.
        
        Args:
            kanban_db_path: Path to kanban.db (default: ~/.hermes/kanban.db)
            eca_rules_path: Path to eca_rules.yaml (default: ~/.hermes/kanban/eca_rules.yaml)
            model: Model to use for rule distillation (default: haiku-4-5)
            poll_interval: Seconds between polls
            provider_base_url: LLM provider base URL
            provider_api_key: LLM provider API key
            provider_name: Provider name (e.g., "anthropic")
        """
        self.kanban_db_path = kanban_db_path or Path.home() / ".hermes" / "kanban.db"
        self.eca_rules_path = eca_rules_path or Path.home() / ".hermes" / "kanban" / "eca_rules.yaml"
        self.model = model
        self.poll_interval = poll_interval
        
        # LLM provider config
        self.provider_base_url = provider_base_url
        self.provider_api_key = provider_api_key
        self.provider_name = provider_name or "anthropic"
        
        # State tracking
        self._seen_completions: set[Tuple[str, int]] = set()  # (task_id, run_id)
        self._proposed_rules: dict[str, RuleProposal] = {}
        self._last_cursor: Optional[int] = None
        
        # Ensure directories exist
        self.kanban_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.eca_rules_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _get_kanban_connection(self) -> sqlite3.Connection:
        """Get a connection to the kanban database."""
        conn = sqlite3.connect(str(self.kanban_db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def _load_eca_rules(self) -> dict:
        """Load existing ECA rules from YAML."""
        if not self.eca_rules_path.exists():
            return {"version": 1, "rules": []}
        
        try:
            with open(self.eca_rules_path) as f:
                data = yaml.safe_load(f) or {}
            return data
        except Exception as e:
            logger.warning(f"Failed to load ECA rules from {self.eca_rules_path}: {e}")
            return {"version": 1, "rules": []}
    
    def _save_eca_rules(self, rules_data: dict) -> None:
        """Save ECA rules to YAML."""
        try:
            self.eca_rules_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.eca_rules_path, "w") as f:
                yaml.dump(rules_data, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Saved ECA rules to {self.eca_rules_path}")
        except Exception as e:
            logger.error(f"Failed to save ECA rules: {e}")
            raise
    
    def _is_operator_override_completion(
        self, task: dict, run: dict
    ) -> bool:
        """Check if a completion was operator-forced."""
        # Check metadata for explicit override flag
        if run.get("metadata"):
            try:
                meta = json.loads(run["metadata"]) if isinstance(run["metadata"], str) else run["metadata"]
                if meta.get("operator_override"):
                    return True
            except Exception:
                pass
        
        # Check summary for operator-related keywords
        summary = run.get("summary", "")
        if summary:
            keywords = ["operator authority", "triage false-positive", "operator override", "forced closure"]
            if any(kw.lower() in summary.lower() for kw in keywords):
                return True
        
        return False
    
    async def _fetch_recent_completions(self) -> Sequence[dict]:
        """Fetch recent completed tasks that may need rule distillation."""
        if not self.kanban_db_path.exists():
            logger.warning(f"Kanban DB not found at {self.kanban_db_path}")
            return []
        
        try:
            conn = self._get_kanban_connection()
            cursor = conn.cursor()
            
            # Fetch tasks completed in the last 6 hours with operator-override indicators
            now = int(time.time())
            six_hours_ago = now - (6 * 3600)
            
            rows = cursor.execute("""
                SELECT t.id, t.title, t.body, t.parents,
                       r.id as run_id, r.summary, r.metadata, r.started_at, r.ended_at
                FROM tasks t
                LEFT JOIN task_runs r ON t.id = r.task_id
                WHERE t.status = 'done'
                  AND t.completed_at > ?
                ORDER BY t.completed_at DESC
                LIMIT 100
            """, (six_hours_ago,)).fetchall()
            
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Error fetching completions: {e}")
            return []
    
    async def _get_task_block_history(self, task_id: str) -> list[dict]:
        """Fetch the block history for a task."""
        try:
            conn = self._get_kanban_connection()
            cursor = conn.cursor()
            
            rows = cursor.execute("""
                SELECT kind, payload, created_at
                FROM task_events
                WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected', 'unblocked')
                ORDER BY created_at DESC
                LIMIT 20
            """, (task_id,)).fetchall()
            
            conn.close()
            
            result = []
            for row in rows:
                payload = {}
                try:
                    if row["payload"]:
                        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                except Exception:
                    pass
                
                result.append({
                    "kind": row["kind"],
                    "payload": payload,
                    "created_at": row["created_at"],
                })
            
            return result
        except Exception as e:
            logger.error(f"Error fetching block history for {task_id}: {e}")
            return []
    
    async def _get_upstream_context(self, task_id: str) -> dict:
        """Fetch context about upstream dependencies and recent events."""
        try:
            conn = self._get_kanban_connection()
            cursor = conn.cursor()
            
            # Get parents
            row = cursor.execute("SELECT parents FROM tasks WHERE id = ?", (task_id,)).fetchone()
            parents = []
            if row and row["parents"]:
                try:
                    parents = json.loads(row["parents"]) if isinstance(row["parents"], str) else row["parents"]
                except Exception:
                    pass
            
            # Get recent completed siblings/related tasks
            recent = cursor.execute("""
                SELECT id, title, status, completed_at
                FROM tasks
                WHERE status = 'done' AND completed_at > ?
                ORDER BY completed_at DESC
                LIMIT 5
            """, (int(time.time()) - 3600,)).fetchall()
            
            conn.close()
            
            return {
                "parents": parents,
                "recent_completions": [dict(r) for r in recent],
            }
        except Exception as e:
            logger.error(f"Error fetching upstream context: {e}")
            return {}
    
    async def _distill_rule_via_llm(
        self,
        incident_context: dict,
    ) -> Optional[ECARule]:
        """Call LLM to distill a candidate rule from incident context.
        
        Args:
            incident_context: Dict with task_id, summary, block_history, upstream_context
            
        Returns:
            Proposed ECARule or None if distillation failed.
        """
        # This is a placeholder; full LLM integration will be added
        # when NERVE-01 typed-envelope schema lands.
        
        logger.info(f"[STUB] Would distill rule for incident context: {incident_context.get('task_id')}")
        
        # TODO: Call LLM with structured prompt:
        # """
        # You are an expert in block-loop recovery patterns in multi-agent systems.
        # 
        # An operator force-closed this ticket due to a recurring block:
        # - Task: {task_id}
        # - Summary: {summary}
        # - Block history: {block_history}
        # - Upstream context: {upstream_context}
        # 
        # Propose a brief ECA rule that would have auto-recovered from this pattern.
        # 
        # Return JSON:
        # {
        #   "rule_id": "cascade_review_completion",
        #   "event": "kanban_block_loop_detected",
        #   "condition": "latest_block.reason contains 'review-required' AND ...",
        #   "action": "auto_unblock",
        #   "audit_note": "..."
        # }
        # """
        
        return None
    
    async def _propose_rule_as_ticket(
        self,
        proposal: RuleProposal,
    ) -> Optional[str]:
        """Create a kanban ticket to present the rule for operator ratification.
        
        Args:
            proposal: The rule proposal
            
        Returns:
            Ticket ID or None if creation failed
        """
        # This requires access to the kanban_db functions from hermes_cli.kanban_db
        # For now, this is a stub that would be called from the daemon.
        
        logger.info(f"[STUB] Would create ratification ticket for rule: {proposal.rule.id}")
        return None
    
    async def poll_for_operator_overrides(self) -> int:
        """Poll for recent operator-override completions and distill rules.
        
        Returns:
            Number of new rule proposals created
        """
        logger.info("Polling for operator-override completions...")
        
        completions = await self._fetch_recent_completions()
        new_proposals = 0
        
        for row in completions:
            task_id = row["id"]
            run_id = row["run_id"]
            
            # Skip if we've already seen this completion
            if (task_id, run_id) in self._seen_completions:
                continue
            
            self._seen_completions.add((task_id, run_id))
            
            # Check if this is an operator-override completion
            if not self._is_operator_override_completion(row, row):
                continue
            
            logger.info(f"Found operator-override completion: {task_id}")
            
            # Fetch context
            block_history = await self._get_task_block_history(task_id)
            upstream_context = await self._get_upstream_context(task_id)
            
            incident_context = {
                "task_id": task_id,
                "summary": row.get("summary"),
                "block_history": block_history,
                "upstream_context": upstream_context,
            }
            
            # Distill rule
            rule = await self._distill_rule_via_llm(incident_context)
            if rule is None:
                logger.warning(f"Failed to distill rule for {task_id}")
                continue
            
            # Create proposal
            proposal = RuleProposal(
                source_incident=task_id,
                rule=rule,
            )
            
            # Create kanban ticket
            ticket_id = await self._propose_rule_as_ticket(proposal)
            if ticket_id:
                proposal.proposal_ticket = ticket_id
                self._proposed_rules[ticket_id] = proposal
                new_proposals += 1
                logger.info(f"Created rule proposal ticket: {ticket_id} for incident {task_id}")
        
        return new_proposals
    
    async def run(self) -> None:
        """Main event loop for the distiller service."""
        logger.info(f"Rule distiller starting (poll_interval={self.poll_interval}s)")
        
        try:
            while True:
                try:
                    new_proposals = await self.poll_for_operator_overrides()
                    if new_proposals > 0:
                        logger.info(f"Created {new_proposals} new rule proposal(s)")
                except Exception as e:
                    logger.error(f"Error in poll cycle: {e}", exc_info=True)
                
                await asyncio.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Rule distiller shutting down")
        except Exception as e:
            logger.error(f"Unexpected error in rule distiller: {e}", exc_info=True)
            raise
