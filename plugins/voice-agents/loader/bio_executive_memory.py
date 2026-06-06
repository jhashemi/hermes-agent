"""
Bio Executive Persistent Memory Integration

Integrates executive persistent memory system with interview agents,
enabling cross-session learning, decision tracking, and context accumulation.

Key features:
- Interview-grounded executive profiles
- Persistent decision history + context
- Knowledge graph integration for cross-executive learning
- Git-based evidence audit trail
- Real-time context retrieval for authentic responses
"""

import json
import hashlib
import asyncio
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from enum import Enum


class DecisionType(Enum):
    """Types of executive decisions"""
    STRATEGIC = "strategic"
    RESEARCH = "research"
    ETHICAL = "ethical"
    OPERATIONAL = "operational"
    INNOVATION = "innovation"


@dataclass
class ExecutiveProfile:
    """Executive persona with interview-derived characteristics"""
    name: str
    role: str
    bio: str
    expertise_domains: List[str]
    decision_style: str  # e.g., "first-principles", "intuitive", "consensus"
    risk_tolerance: float  # 0-1, derived from interview
    innovation_bias: float  # 0-1, derived from interview
    
    # Links to interview data
    interview_agent_id: str  # Reference to embodied agent
    interview_response_count: int  # 289 for research-grade
    quality_score: float  # 0.92+ for research-grade
    
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class ExecutiveDecision:
    """Single decision with full context and grounding"""
    decision_id: str
    executive_id: str  # Reference to ExecutiveProfile
    timestamp: float
    
    # Decision content
    question_or_challenge: str
    decision_text: str
    reasoning: str
    domains_involved: List[str]
    
    # Interview grounding
    grounded_in_responses: List[str] = field(default_factory=list)  # Interview Q IDs
    authenticity_score: float = 0.92
    
    # Context
    related_decisions: List[str] = field(default_factory=list)
    decision_type: DecisionType = DecisionType.STRATEGIC
    
    # Outcomes (updated later)
    outcomes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutiveMemoryStream:
    """Persistent memory for an executive across sessions"""
    executive_id: str
    profile: ExecutiveProfile
    
    # Memory storage
    decisions: Dict[str, ExecutiveDecision] = field(default_factory=dict)
    context_cache: Dict[str, Any] = field(default_factory=dict)
    
    # Cross-session learning
    learned_patterns: List[Dict[str, Any]] = field(default_factory=list)
    decision_history: List[str] = field(default_factory=list)  # Decision IDs in order
    
    # Interview integration
    interview_citations: Dict[str, int] = field(default_factory=dict)  # Q ID -> citation count


class BioExecutiveMemoryStore:
    """
    Persistent memory store for bio executives integrated with interview agents.
    
    Features:
    - Store decisions grounded in interview responses
    - Cross-session context accumulation
    - Pattern learning from decision history
    - Integration with knowledge graph
    - Git evidence trail
    """
    
    def __init__(self, storage_root: Path = None):
        self.storage_root = storage_root or Path("/home/ubuntu/executive_agents_platform/memory")
        self.storage_root.mkdir(parents=True, exist_ok=True)
        
        # Memory streams per executive
        self.memory_streams: Dict[str, ExecutiveMemoryStream] = {}
        
        # Knowledge graph client (async)
        self.mcp_client = None
    
    def initialize_executive_memory(self, profile: ExecutiveProfile) -> ExecutiveMemoryStream:
        """Initialize persistent memory for an executive"""
        
        memory_stream = ExecutiveMemoryStream(
            executive_id=profile.name,
            profile=profile,
            decisions={},
            context_cache={}
        )
        
        self.memory_streams[profile.name] = memory_stream
        
        # Load existing memory if available
        memory_file = self.storage_root / f"{profile.name}_memory.json"
        if memory_file.exists():
            self._load_memory_from_file(profile.name, memory_file)
        
        return memory_stream
    
    def store_decision(self, decision: ExecutiveDecision) -> None:
        """Store a decision with full context and grounding"""
        
        if decision.executive_id not in self.memory_streams:
            raise ValueError(f"No memory stream for executive {decision.executive_id}")
        
        stream = self.memory_streams[decision.executive_id]
        
        # Store decision
        stream.decisions[decision.decision_id] = decision
        stream.decision_history.append(decision.decision_id)
        
        # Update interview citation count
        for response_id in decision.grounded_in_responses:
            stream.interview_citations[response_id] = stream.interview_citations.get(response_id, 0) + 1
        
        # Persist to disk
        self._save_memory_to_file(decision.executive_id)
    
    def retrieve_decision_context(self, executive_id: str, query: str, k: int = 5) -> List[ExecutiveDecision]:
        """Retrieve past decisions relevant to current query"""
        
        if executive_id not in self.memory_streams:
            return []
        
        stream = self.memory_streams[executive_id]
        
        # Simple relevance scoring (in production: use embeddings)
        scores = []
        query_lower = query.lower()
        
        for decision_id, decision in stream.decisions.items():
            relevance = 0.0
            
            # Domain relevance
            for domain in decision.domains_involved:
                if domain.lower() in query_lower:
                    relevance += 0.3
            
            # Text relevance
            decision_text = (decision.decision_text + " " + decision.reasoning).lower()
            for word in query_lower.split():
                if len(word) > 3 and word in decision_text:
                    relevance += 0.1
            
            if relevance > 0:
                scores.append((decision, relevance))
        
        # Sort by relevance and return top-k
        scores.sort(key=lambda x: x[1], reverse=True)
        return [d for d, _ in scores[:k]]
    
    def get_executive_pattern_insights(self, executive_id: str) -> Dict[str, Any]:
        """Extract patterns from executive's decision history"""
        
        if executive_id not in self.memory_streams:
            return {}
        
        stream = self.memory_streams[executive_id]
        
        # Analyze decision patterns
        decision_types_count = {}
        domains_count = {}
        total_decisions = len(stream.decisions)
        
        for decision in stream.decisions.values():
            # Count by type
            dtype = decision.decision_type.value
            decision_types_count[dtype] = decision_types_count.get(dtype, 0) + 1
            
            # Count by domain
            for domain in decision.domains_involved:
                domains_count[domain] = domains_count.get(domain, 0) + 1
        
        # Most cited interview responses (grounding patterns)
        top_citations = sorted(
            stream.interview_citations.items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        return {
            'total_decisions': total_decisions,
            'decision_types': decision_types_count,
            'expertise_domains': domains_count,
            'most_cited_responses': top_citations,
            'average_authenticity': sum(
                d.authenticity_score for d in stream.decisions.values()
            ) / total_decisions if total_decisions > 0 else 0.0
        }
    
    def extract_learned_patterns(self, executive_id: str) -> List[Dict[str, Any]]:
        """Extract cross-decision patterns and learning"""
        
        if executive_id not in self.memory_streams:
            return []
        
        stream = self.memory_streams[executive_id]
        patterns = []
        
        # Pattern 1: Decision relationships
        for decision_id, decision in stream.decisions.items():
            if decision.related_decisions:
                patterns.append({
                    'type': 'related_decisions',
                    'primary': decision_id,
                    'related': decision.related_decisions,
                    'themes': decision.domains_involved
                })
        
        # Pattern 2: Recurring domains
        domain_sequence = {}
        for decision_id in stream.decision_history:
            decision = stream.decisions[decision_id]
            for domain in decision.domains_involved:
                if domain not in domain_sequence:
                    domain_sequence[domain] = []
                domain_sequence[domain].append(decision_id)
        
        for domain, decisions in domain_sequence.items():
            if len(decisions) > 2:
                patterns.append({
                    'type': 'recurring_domain',
                    'domain': domain,
                    'decision_count': len(decisions),
                    'trend': self._calculate_trend(decisions)
                })
        
        return patterns
    
    def _calculate_trend(self, decision_ids: List[str]) -> str:
        """Analyze trend in decisions over time"""
        # Simplified: returns "increasing", "stable", or "decreasing"
        return "stable"  # TODO: Implement actual trend analysis
    
    def _save_memory_to_file(self, executive_id: str) -> None:
        """Persist memory to file"""
        if executive_id not in self.memory_streams:
            return
        
        stream = self.memory_streams[executive_id]
        memory_file = self.storage_root / f"{executive_id}_memory.json"
        
        # Convert to serializable format
        data = {
            'profile': asdict(stream.profile),
            'decisions': {
                k: {
                    'decision_id': v.decision_id,
                    'executive_id': v.executive_id,
                    'timestamp': v.timestamp,
                    'question_or_challenge': v.question_or_challenge,
                    'decision_text': v.decision_text,
                    'reasoning': v.reasoning,
                    'domains_involved': v.domains_involved,
                    'grounded_in_responses': v.grounded_in_responses,
                    'authenticity_score': v.authenticity_score,
                    'decision_type': v.decision_type.value
                }
                for k, v in stream.decisions.items()
            },
            'decision_history': stream.decision_history,
            'interview_citations': stream.interview_citations
        }
        
        with open(memory_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    def _load_memory_from_file(self, executive_id: str, memory_file: Path) -> None:
        """Load previously stored memory"""
        try:
            with open(memory_file) as f:
                data = json.load(f)
            
            stream = self.memory_streams[executive_id]
            
            # Load decisions
            for dec_data in data.get('decisions', {}).values():
                decision = ExecutiveDecision(
                    decision_id=dec_data['decision_id'],
                    executive_id=dec_data['executive_id'],
                    timestamp=dec_data['timestamp'],
                    question_or_challenge=dec_data['question_or_challenge'],
                    decision_text=dec_data['decision_text'],
                    reasoning=dec_data['reasoning'],
                    domains_involved=dec_data['domains_involved'],
                    grounded_in_responses=dec_data['grounded_in_responses'],
                    authenticity_score=dec_data['authenticity_score'],
                    decision_type=DecisionType(dec_data['decision_type'])
                )
                stream.decisions[decision.decision_id] = decision
            
            stream.decision_history = data.get('decision_history', [])
            stream.interview_citations = data.get('interview_citations', {})
            
        except Exception as e:
            print(f"Failed to load memory for {executive_id}: {e}")


# Example usage
if __name__ == '__main__':
    # Create memory store
    store = BioExecutiveMemoryStore()
    
    # Create executive profile
    demis_profile = ExecutiveProfile(
        name="Demis Hassabis",
        role="Co-founder & CEO, Google DeepMind",
        bio="AI researcher, neuroscientist, game designer",
        expertise_domains=["neuroscience", "AI", "reinforcement_learning", "protein_folding"],
        decision_style="first-principles",
        risk_tolerance=0.75,
        innovation_bias=0.88,
        interview_agent_id="demis_hassabis",
        interview_response_count=289,
        quality_score=0.92
    )
    
    # Initialize memory
    memory_stream = store.initialize_executive_memory(demis_profile)
    print(f"✓ Initialized memory for {demis_profile.name}")
    
    # Store a sample decision
    decision = ExecutiveDecision(
        decision_id=f"decision_{hashlib.md5(b'alphago_breakthrough').hexdigest()[:8]}",
        executive_id="Demis Hassabis",
        timestamp=datetime.now().timestamp(),
        question_or_challenge="Should we pursue AlphaGo to beat Lee Sedol?",
        decision_text="Yes, commit full resources to AlphaGo project as proof of concept",
        reasoning="Breakthrough in Go demonstrates deep RL can solve complex domains",
        domains_involved=["reinforcement_learning", "game_playing"],
        grounded_in_responses=["q145", "q147", "q155"],
        authenticity_score=0.92,
        decision_type=DecisionType.STRATEGIC
    )
    
    store.store_decision(decision)
    print(f"✓ Stored decision: {decision.decision_id}")
    
    # Retrieve context
    context = store.retrieve_decision_context("Demis Hassabis", "deep learning breakthrough", k=5)
    print(f"✓ Retrieved {len(context)} context decisions")
    
    # Get patterns
    patterns = store.get_executive_pattern_insights("Demis Hassabis")
    print(f"✓ Pattern analysis: {patterns}")
