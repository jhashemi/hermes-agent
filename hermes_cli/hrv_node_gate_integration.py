"""Integration of HRV node gate into kanban dispatcher.

This module provides the connection between the HRV node gate and the
kanban dispatch-once flow, allowing the dispatcher to reject nodes based
on nervous-system probe data.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)


def check_node_gate(
    conn: sqlite3.Connection,
    task_id: str,
    node_hostname: Optional[str],
    model_name: str,
    gate=None,
) -> Optional[str]:
    """Check if a task should be rejected on a given node.
    
    Args:
        conn: kanban DB connection
        task_id: task ID
        node_hostname: target node (None = local)
        model_name: LLM model name
        gate: HRVNodeGate instance (or None to use default)
    
    Returns:
        None if node passes all gates.
        String reason if node is rejected.
    """
    if node_hostname is None:
        # Local dispatch always passes (no remote probes)
        return None
    
    if gate is None:
        # Try to get default gate
        try:
            from hermes_cli.hrv_node_gate import get_default_gate
            gate = get_default_gate()
        except Exception:
            # Gate not available — fail-open (don't reject)
            return None
    
    try:
        # Fetch task priority and min_resources
        row = conn.execute(
            "SELECT priority, min_resources FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        
        task_priority = None
        task_min_resources = None
        if row:
            task_priority = row["priority"]
            min_res_json = row["min_resources"]
            if min_res_json:
                try:
                    import json
                    task_min_resources = json.loads(min_res_json)
                except Exception:
                    task_min_resources = None
        
        # Evaluate node
        rejection_reason = gate.evaluate_node(
            task_id,
            node_hostname,
            model_name,
            task_priority=task_priority,
            task_min_resources=task_min_resources,
        )
        
        if rejection_reason:
            # Emit metric
            gate.emit_rejection_metric(task_id, node_hostname, rejection_reason)
            logger.info(
                f"[dispatch] node rejected: task_id={task_id}, "
                f"node={node_hostname}, reason={rejection_reason}"
            )
        
        return rejection_reason
    
    except Exception as e:
        logger.warning(f"Error in node gate check: {e}", exc_info=True)
        # Fail-open on error
        return None
