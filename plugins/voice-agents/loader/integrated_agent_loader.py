"""
Integrated Agent Loader with Bio Executive Persistent Memory

Combines:
1. Park et al. authenticity retrieval (289-question interview)
2. Bio executive persistent memory (decision tracking + context)
3. Interview-grounded persona profiles

Creates a complete executive agent platform with cross-session learning.
"""

import json
import yaml
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

from authenticity_retrieval import AuthenticityMemoryStream
from bio_executive_memory import (
    BioExecutiveMemoryStore, ExecutiveProfile, ExecutiveDecision, DecisionType
)


@dataclass
class IntegratedExecutiveAgent:
    """Complete executive agent with interview authenticity + persistent memory"""
    name: str
    title: str
    bio: str
    personality: Dict[str, Any]
    system_prompt: str
    interview_data: Dict[str, Any]
    voice_config: Dict[str, Any]
    
    # Memory systems
    interview_memory_stream: AuthenticityMemoryStream  # Park et al. retrieval
    bio_memory_store: BioExecutiveMemoryStore  # Executive persistent memory
    executive_profile: ExecutiveProfile  # Profile with memory integration
    
    @property
    def interview_questions_count(self) -> int:
        if self.interview_data.get('responses'):
            return len(self.interview_data['responses'])
        return 0
    
    @property
    def quality_score(self) -> float:
        return self.interview_data.get('quality_score', 0.0)
    
    def get_authentic_response(self, query: str) -> str:
        """Get response grounded in interview memory stream"""
        return self.interview_memory_stream.generate_authentic_response(query)
    
    def retrieve_interview_memories(self, query: str, k: int = 5) -> list:
        """Retrieve top-k interview responses for a query"""
        return self.interview_memory_stream.retrieve_for_query(query, k)
    
    def retrieve_decision_context(self, query: str, k: int = 5) -> list:
        """Retrieve past decisions relevant to current challenge"""
        return self.bio_memory_store.retrieve_decision_context(self.name, query, k)
    
    def store_decision(self, question: str, decision: str, reasoning: str,
                      domains: list, grounded_responses: list) -> str:
        """Store a new decision in persistent memory"""
        decision_id = f"decision_{hashlib.md5(question.encode()).hexdigest()[:8]}"
        
        exec_decision = ExecutiveDecision(
            decision_id=decision_id,
            executive_id=self.name,
            timestamp=datetime.now().timestamp(),
            question_or_challenge=question,
            decision_text=decision,
            reasoning=reasoning,
            domains_involved=domains,
            grounded_in_responses=grounded_responses,
            authenticity_score=self.quality_score,
            decision_type=DecisionType.STRATEGIC
        )
        
        self.bio_memory_store.store_decision(exec_decision)
        return decision_id
    
    def get_memory_insights(self) -> Dict[str, Any]:
        """Get comprehensive memory insights"""
        return {
            'interview_memory': self.interview_memory_stream.get_statistics(),
            'executive_memory': self.bio_memory_store.get_executive_pattern_insights(self.name),
            'learned_patterns': self.bio_memory_store.extract_learned_patterns(self.name)
        }


class IntegratedAgentLoader:
    """
    Loads agents with:
    - Complete interview data (L0-L4)
    - Park et al. authenticity retrieval
    - Bio executive persistent memory
    """
    
    def __init__(self, platform_root: Path = None):
        self.platform_root = platform_root or Path("/home/ubuntu/executive_agents_platform")
        self.agents_dir = self.platform_root / "agents"
        self.registry_path = self.agents_dir / "agents_registry.yaml"
        self._registry = None
        self._loaded_agents: Dict[str, IntegratedExecutiveAgent] = {}
        
        # Initialize memory stores
        self.interview_memory_store = {}  # Per-agent memory streams
        self.bio_memory_store = BioExecutiveMemoryStore(self.platform_root / "memory")
    
    @property
    def registry(self) -> Dict:
        if self._registry is None:
            with open(self.registry_path) as f:
                self._registry = yaml.safe_load(f)
        return self._registry
    
    def load_agent(self, agent_id: str) -> IntegratedExecutiveAgent:
        """Load agent with all memory systems integrated"""
        if agent_id in self._loaded_agents:
            return self._loaded_agents[agent_id]
        
        agent_entry = self.registry['agents'].get(agent_id)
        if not agent_entry:
            raise ValueError(f"Agent '{agent_id}' not found in registry")
        
        agent_dir = self.agents_dir / agent_entry['path']
        
        # Load profile YAML
        profile_path = agent_dir / agent_entry['profile_file']
        with open(profile_path) as f:
            profile_data = yaml.safe_load(f)
        
        # Load voice config YAML
        voice_config_path = agent_dir / agent_entry['voice_config_file']
        with open(voice_config_path) as f:
            voice_config = yaml.safe_load(f)
        
        # Load interview data (L4 assembled)
        interview_path = agent_dir / agent_entry['interview_data_path']
        with open(interview_path) as f:
            interview_data = json.load(f)
        
        # Initialize Park et al. authenticity memory stream
        interview_memory_stream = AuthenticityMemoryStream(profile_data['name'], interview_data)
        
        # Initialize bio executive profile with persistent memory
        executive_profile = ExecutiveProfile(
            name=profile_data['name'],
            role=profile_data['title'],
            bio=profile_data['bio'],
            expertise_domains=self._extract_domains(interview_data),
            decision_style=self._infer_decision_style(profile_data),
            risk_tolerance=profile_data.get('personality', {}).get('big5', {}).get('openness', 0.5),
            innovation_bias=profile_data.get('personality', {}).get('big5', {}).get('openness', 0.5),
            interview_agent_id=agent_id,
            interview_response_count=agent_entry.get('questions', 289),
            quality_score=agent_entry.get('quality', 0.92)
        )
        
        # Initialize persistent memory for this executive
        self.bio_memory_store.initialize_executive_memory(executive_profile)
        
        agent = IntegratedExecutiveAgent(
            name=profile_data['name'],
            title=profile_data['title'],
            bio=profile_data['bio'],
            personality=profile_data.get('personality', {}),
            system_prompt=profile_data['system_prompt'],
            interview_data=interview_data,
            voice_config=voice_config,
            interview_memory_stream=interview_memory_stream,
            bio_memory_store=self.bio_memory_store,
            executive_profile=executive_profile
        )
        
        self._loaded_agents[agent_id] = agent
        return agent
    
    def _extract_domains(self, interview_data: Dict) -> list:
        """Extract expertise domains from interview data"""
        domains = set()
        for response in interview_data.get('responses', []):
            domain = response.get('domain', 'general')
            domains.add(domain)
        return list(domains)
    
    def _infer_decision_style(self, profile_data: Dict) -> str:
        """Infer decision-making style from profile"""
        # Simple heuristic based on system prompt
        prompt = profile_data.get('system_prompt', '').lower()
        
        if 'first-principles' in prompt or 'first principles' in prompt:
            return 'first-principles'
        elif 'intuition' in prompt or 'intuitive' in prompt:
            return 'intuitive'
        elif 'consensus' in prompt or 'collaborative' in prompt:
            return 'consensus'
        else:
            return 'analytical'
    
    def list_agents(self) -> list:
        """List all available agents"""
        agents = []
        for agent_id, entry in self.registry['agents'].items():
            agents.append({
                'id': agent_id,
                'status': entry.get('status'),
                'questions': entry.get('questions', 0),
                'quality': entry.get('quality', 0.0)
            })
        return agents
    
    def get_agent_memory_report(self, agent_id: str) -> Dict[str, Any]:
        """Get comprehensive memory report for an agent"""
        agent = self.load_agent(agent_id)
        return agent.get_memory_insights()


if __name__ == '__main__':
    loader = IntegratedAgentLoader()
    
    # Load Demis with all memory systems
    print("Loading Demis Hassabis with integrated memory systems...")
    demis = loader.load_agent('demis_hassabis')
    
    # Test interview memory retrieval
    print("\n=== Interview Memory Test ===")
    interview_memories = demis.retrieve_interview_memories("How do you approach AGI safety?", k=3)
    print(f"Retrieved {len(interview_memories)} interview responses")
    
    # Test decision storage
    print("\n=== Decision Storage Test ===")
    decision_id = demis.store_decision(
        question="Should DeepMind pursue protein folding research?",
        decision="Yes, this aligns with our mission to solve scientific problems with AI",
        reasoning="Protein folding is fundamental to biology and medicine",
        domains=["protein_folding", "scientific_discovery"],
        grounded_responses=["q85", "q92", "q105"]
    )
    print(f"Stored decision: {decision_id}")
    
    # Test decision context retrieval
    print("\n=== Decision Context Retrieval ===")
    context_decisions = demis.retrieve_decision_context("scientific breakthrough", k=3)
    print(f"Retrieved {len(context_decisions)} context decisions")
    
    # Test memory insights
    print("\n=== Comprehensive Memory Insights ===")
    insights = demis.get_memory_insights()
    print(f"Interview memory: {insights['interview_memory']['total_memories']} memories")
    print(f"Executive memory: {insights['executive_memory'].get('total_decisions', 0)} decisions")
    print(f"Learned patterns: {len(insights['learned_patterns'])} patterns")
