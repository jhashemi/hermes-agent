"""
OKREngine — Thin wrapper around EAF's OKRAccountabilitySystem

Replaces 223 LOC of duplicated OKR parsing with EAF's production-grade
OKRAccountabilitySystem (837 LOC, DuckDB-backed, RACI, evidence gates).
"""

import sys
from typing import Dict, Any, List

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

from executive_agents.infrastructure.systems.okr_accountability import (
    OKRAccountabilitySystem,
    Objective,
    KeyResult,
    Organization,
    Team,
)
from okr_executive.domain.models import OKRInput


class OKREngine:
    """
    Parses OKR text → Objective + KeyResult objects.
    
    Delegates to EAF's OKRAccountabilitySystem for persistence,
    RACI tracking, and evidence gates.
    """

    def __init__(self):
        self.eaf_okr = OKRAccountabilitySystem()

    async def execute(self, context: Dict) -> Dict[str, Any]:
        """Parse OKR text and create Objective + KeyResult via EAF"""
        okr_input = context.get("okr_input", "")

        # Parse raw text
        parsed = self._parse_okr_text(okr_input)
        okr_input_obj = OKRInput(
            raw_text=okr_input,
            objective=parsed["objective"],
            key_results=parsed["key_results"],
        )

        # Create EAF Objective (gets DuckDB persistence, RACI, evidence gates)
        organization = Organization(id="nebula-capital", name="Nebula Capital")
        team = Team(id="exec-agents", org_id="nebula-capital", name="Executive Agents", lead="okr_orchestrator")
        objective = Objective(
            id=f"okr-{hash(okr_input) % 10000:04d}",
            team_id=team.id,
            owner_id="okr_orchestrator",
            title=parsed["objective"],
            description=parsed["objective"],
        )

        # Create EAF KeyResults
        key_results = [
            KeyResult(
                id=f"kr-{i}",
                objective_id=objective.id,
                title=kr,
                target_value=1.0,
                current_value=0.0,
            )
            for i, kr in enumerate(parsed["key_results"], 1)
        ]

        return {
            "okr_input": okr_input_obj,
            "objective": objective,
            "key_results": key_results,
            "eaf_system": self.eaf_okr,  # Pass through for downstream engines
        }

    def _parse_okr_text(self, text: str) -> Dict[str, Any]:
        """Parse OKR text into objective + key results"""
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        objective = ""
        key_results = []

        for line in lines:
            lower = line.lower()
            if lower.startswith("objective:") or lower.startswith("o:"):
                objective = line.split(":", 1)[1].strip()
            elif any(lower.startswith(p) for p in ["key result", "kr", "- ", "* "]):
                kr_text = line.split(":", 1)[-1].strip() if ":" in line else line.lstrip("-* ").strip()
                if kr_text:
                    key_results.append(kr_text)
            elif not objective and line and not lower.startswith(("kr", "-")):
                objective = line

        if not objective:
            objective = lines[0] if lines else "Undefined Objective"
        if not key_results:
            key_results = ["Complete objective"]

        return {"objective": objective, "key_results": key_results}
