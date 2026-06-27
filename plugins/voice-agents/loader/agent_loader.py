"""
Executive Agent Loader

Loads research-grade agent personas with their complete embodied interview data
(L0-L4 layers), voice configuration, and system prompts.
"""

import json
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class AgentProfile:
    """Loaded agent profile with interview data"""
    name: str
    title: str
    bio: str
    personality: Dict[str, Any]
    system_prompt: str
    interview_data: Dict[str, Any]
    voice_config: Dict[str, Any]
    
    @property
    def interview_questions_count(self) -> int:
        """Total questions in L4 assembled interview"""
        if self.interview_data.get('responses'):
            return len(self.interview_data['responses'])
        return 0
    
    @property
    def quality_score(self) -> float:
        """Research-grade quality score (0-1)"""
        return self.interview_data.get('quality_score', 0.0)

class AgentLoader:
    """Loads agents from organized file structure"""
    
    def __init__(self, platform_root: Path = None):
        self.platform_root = platform_root or Path("/home/ubuntu/executive_agents_platform")
        self.agents_dir = self.platform_root / "agents"
        self.registry_path = self.agents_dir / "agents_registry.yaml"
        self._registry = None
        self._loaded_agents: Dict[str, AgentProfile] = {}
    
    @property
    def registry(self) -> Dict:
        """Load agents registry"""
        if self._registry is None:
            with open(self.registry_path) as f:
                self._registry = yaml.safe_load(f)
        return self._registry
    
    def load_agent(self, agent_id: str) -> AgentProfile:
        """Load complete agent profile with interview data"""
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
        
        agent = AgentProfile(
            name=profile_data['name'],
            title=profile_data['title'],
            bio=profile_data['bio'],
            personality=profile_data.get('personality', {}),
            system_prompt=profile_data['system_prompt'],
            interview_data=interview_data,
            voice_config=voice_config
        )
        
        self._loaded_agents[agent_id] = agent
        return agent
    
    def list_agents(self, status: str = None) -> list:
        """List available agents, optionally filtered by status"""
        agents = []
        for agent_id, entry in self.registry['agents'].items():
            if status and entry.get('status') != status:
                continue
            agents.append({
                'id': agent_id,
                'name': entry.get('name', agent_id),
                'status': entry.get('status'),
                'interview_complete': entry.get('interview_complete'),
                'questions': entry.get('questions', 0),
                'voice_ready': entry.get('voice_ready', False)
            })
        return agents
    
    def get_production_agents(self) -> list:
        """Get all production-ready agents"""
        return self.list_agents(status='production')
    
    def validate_agent(self, agent_id: str) -> Dict[str, Any]:
        """Validate agent data integrity"""
        try:
            agent = self.load_agent(agent_id)
            return {
                'valid': True,
                'agent_id': agent_id,
                'name': agent.name,
                'questions': agent.interview_questions_count,
                'quality': agent.quality_score,
                'interview_data_size_kb': len(json.dumps(agent.interview_data)) / 1024,
                'system_prompt_length': len(agent.system_prompt)
            }
        except Exception as e:
            return {
                'valid': False,
                'agent_id': agent_id,
                'error': str(e)
            }

if __name__ == '__main__':
    loader = AgentLoader()
    
    # List all agents
    print("Available Agents:")
    for agent in loader.list_agents():
        print(f"  {agent['id']}: {agent['questions']} questions, voice={agent['voice_ready']}")
    
    # Load Demis
    print("\nLoading Demis Hassabis...")
    demis = loader.load_agent('demis_hassabis')
    print(f"  Name: {demis.name}")
    print(f"  Questions: {demis.interview_questions_count}")
    print(f"  Quality: {demis.quality_score}")
    
    # Validate all
    print("\nValidation:")
    for agent_id in ['demis_hassabis', 'steve_jobs']:
        result = loader.validate_agent(agent_id)
        print(f"  {agent_id}: {'✓' if result['valid'] else '✗'}")
