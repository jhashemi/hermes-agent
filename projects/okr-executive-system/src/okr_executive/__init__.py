"""
OKR-Driven Executive System

Enterprise-grade autonomous OKR execution platform with:
- Hexagonal architecture
- SOLID principles
- Event-driven integration
- Full cognitive embodiment
- VCG-based task allocation
"""

__version__ = "0.1.0"
__author__ = "Hermes Agent"

# Make key classes available at package level
from .engines.base import ExecutionEngine
from .domain.models import OKR, Goal, Task, MetricSnapshot

__all__ = [
    "ExecutionEngine",
    "OKR",
    "Goal",
    "Task",
    "MetricSnapshot",
]
