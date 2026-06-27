"""
Enhanced Agent Loader with Park et al. Authenticity Retrieval

Combines agent profile loading with research-grade interview authenticity,
enabling responses grounded in comprehensive embodied interview data.
"""

import json
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

from authenticity_retrieval import AuthenticityMemoryStream

@dataclass
class AuthenticAgentProfile:
    """Agent profile with integrated memory stream for authentic responses"""
    name: str
    title: str
    bio: str
    personality: Dict[str, Any]
    system_prompt: str
    interview_data: Dict[str, Any]
    voice_config: Dict[str, Any]
    memory_stream: AuthenticityMemoryStream  # Park et al. retrieval
    
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
        return self.memory_stream.generate_authentic_response(query)
    
    def retrieve_relevant_memories(self, query: str, k: int = 5) -> list:
        """Retrieve top-k relevant interview responses for a query"""
        return self.memory_stream.retrieve_for_query(query, k)


class EnhancedAgentLoader:
    """Loads agents with integrated Park et al. memory authenticity"""
    
    def __init__(self, platform_root: Path = None):
        self.platform_root = platform_root or Path("/home/ubuntu/executive_agents_platform")
        self.agents_dir = self.platform_root / "agents"
        self.registry_path = self.agents_dir / "agents_registry.yaml"
        self._registry = None
        self._loaded_agents: Dict[str, AuthenticAgentProfile] = {}
    
    @property
    def registry(self) -> Dict:
        if self._registry is None:
            with open(self.registry_path) as f:
                self._registry = yaml.safe_load(f)
        return self._registry
    
    def load_agent(self, agent_id: str) -> AuthenticAgentProfile:
        """Load agent with full interview memory stream"""
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
        memory_stream = AuthenticityMemoryStream(profile_data['name'], interview_data)
        
        agent = AuthenticAgentProfile(
            name=profile_data['name'],
            title=profile_data['title'],
            bio=profile_data['bio'],
            personality=profile_data.get('personality', {}),
            system_prompt=profile_data['system_prompt'],
            interview_data=interview_data,
            voice_config=voice_config,
            memory_stream=memory_stream
        )
        
        self._loaded_agents[agent_id] = agent
        return agent
    
    def get_memory_stream_stats(self, agent_id: str) -> Dict[str, Any]:
        """Get Park et al. memory stream statistics for an agent"""
        agent = self.load_agent(agent_id)
        return agent.memory_stream.get_statistics()


if __name__ == '__main__':
    loader = EnhancedAgentLoader()
    
    # Load Demis with memory stream
    print("Loading Demis Hassabis with Park et al. authenticity retrieval...")
    demis = loader.load_agent('demis_hassabis')
    
    # Get stats
    stats = loader.get_memory_stream_stats('demis_hassabis')
    print(f"\n=== Memory Stream Statistics ===")
    for k, v in stats.items():
        print(f"{k}: {v}")
    
    # Test authentic response
    query = "How do you think about the relationship between neuroscience and AI?"
    print(f"\n=== Query: {query} ===")
    
    memories = demis.retrieve_relevant_memories(query, k=3)
    print(f"\nRetrieved {len(memories)} relevant memories:")
    for i, mem in enumerate(memories, 1):
        print(f"{i}. [{mem.domain}] Poignancy={mem.poignancy:.1f}")
        print(f"   {mem.question_text[:60]}...")
    
    print(f"\n=== Authentic Response (grounded in interview) ===")
    response = demis.get_authentic_response(query)
    print(response)
