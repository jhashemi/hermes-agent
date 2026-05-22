"""
OKR-Driven Executive System

Thin orchestration layer over executive-agents-framework.
All scheduling, VCG, MCTS, RICE, goal hierarchy, and accountability
delegated to EAF. Only genuinely novel code (MakespanMinimizer,
GitHubIssueQueueManager) lives here.
"""

__version__ = "0.2.0"  # Refactored: EAF-backed, no duplication
__author__ = "Hermes Agent"

# Make key classes available at package level
from .engines.okr_engine import OKREngine
from .engines.scheduling import OKRScheduler, MakespanMinimizer
from .domain.models import OKRInput, Objective, KeyResult, GoalNode

__all__ = [
    "OKREngine",
    "OKRScheduler",
    "MakespanMinimizer",
    "OKRInput",
    "Objective",
    "KeyResult",
    "GoalNode",
]
