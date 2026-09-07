"""
Executive Voice Agents Platform - Loader Package

8 memory systems for authentic executive agent responses:
1. Authenticity Retrieval (Park et al. 289 Q&A)
2. Bio Executive Persistent (decisions + context)
3. Voice Synthesis Integration (Resemble + Deepgram + LiveKit)
4. Semantic Memory (embeddings)
5. Episodic Memory (sessions + context)
6. Procedural Memory (workflows)
7. Temporal Memory (exponential decay)
8. Hierarchical Temporal Memory (multi-scale patterns)
"""

from .interview_loader import InterviewLoader
from .authenticity_retrieval import AuthenticityMemoryStream
from .bio_executive_memory import BioExecutiveMemoryStore
from .complete_memory_systems import (
    TemporalMemoryStore,
    HierarchicalTemporalMemory,
)

__all__ = [
    "InterviewLoader",
    "AuthenticityMemoryStream",
    "BioExecutiveMemoryStore",
    "TemporalMemoryStore",
    "HierarchicalTemporalMemory",
]

__version__ = "1.0.0"