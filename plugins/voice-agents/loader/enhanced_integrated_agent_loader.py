"""
Enhanced Integrated Agent Loader - ALL Memory Systems

Combines ALL 3 memory systems:
1. Park et al. Authenticity Retrieval (289-question interview)
2. Bio Executive Persistent Memory (decision tracking + context)
3. Voice Synthesis Integration (Resemble + Deepgram + LiveKit)

Plus future expansion:
4. Semantic memory (embeddings-based retrieval)
5. Episodic memory (session-based context)
6. Procedural memory (learned workflows)

Creates a COMPLETE executive agent platform with cross-session learning + voice.
"""

import json
import yaml
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime

# Core memory systems
from authenticity_retrieval import AuthenticityMemoryStream
from bio_executive_memory import (
    BioExecutiveMemoryStore, ExecutiveProfile, ExecutiveDecision, DecisionType
)
from voice_integration import VoiceIntegrationBridge, ResembleVoiceClone

logger = logging.getLogger(__name__)


# ============================================================================
# SEMANTIC MEMORY SYSTEM (Embeddings-based retrieval)
# ============================================================================

@dataclass
class SemanticMemoryEntry:
    """Entry in semantic memory (concept-based)"""
    concept: str
    embedding: List[float]
    references: List[str]  # Linked to interview responses or decisions
    importance: float  # 0.0-1.0
    timestamp: float
    domains: List[str]


class SemanticMemoryStore:
    """Semantic memory - concept/embedding-based retrieval"""
    
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.entries: Dict[str, SemanticMemoryEntry] = {}
        self.logger = logging.getLogger(f"semantic-memory.{agent_id}")
    
    def store_concept(self, concept: str, embedding: List[float], 
                     references: List[str], domains: List[str],
                     importance: float = 0.5):
        """Store a semantic concept with embedding"""
        entry = SemanticMemoryEntry(
            concept=concept,
            embedding=embedding,
            references=references,
            importance=importance,
            timestamp=datetime.now().timestamp(),
            domains=domains
        )
        self.entries[concept] = entry
        self.logger.info(f"Stored concept: {concept} (importance: {importance})")
    
    def retrieve_similar_concepts(self, query_embedding: List[float], 
                                 k: int = 5) -> List[SemanticMemoryEntry]:
        """Retrieve semantically similar concepts"""
        # Simple cosine similarity for now
        from sklearn.metrics.pairwise import cosine_similarity
        import numpy as np
        
        if not self.entries:
            return []
        
        query = np.array(query_embedding).reshape(1, -1)
        candidates = []
        
        for concept, entry in self.entries.items():
            embedding = np.array(entry.embedding).reshape(1, -1)
            similarity = cosine_similarity(query, embedding)[0][0]
            candidates.append((similarity, entry))
        
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in candidates[:k]]
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get semantic memory statistics"""
        return {
            'total_concepts': len(self.entries),
            'domains_covered': set(d for e in self.entries.values() for d in e.domains),
            'average_importance': sum(e.importance for e in self.entries.values()) / len(self.entries) if self.entries else 0
        }


# ============================================================================
# EPISODIC MEMORY SYSTEM (Session/time-based context)
# ============================================================================

@dataclass
class EpisodeDic:
    """Episode in episodic memory (session event)"""
    episode_id: str
    timestamp: float
    session_id: str
    query: str
    response: str
    context: Dict[str, Any]
    emotional_valence: float  # -1.0 (negative) to +1.0 (positive)
    salience: float  # 0.0-1.0 (how memorable)
    decisions_involved: List[str]


class EpisodicMemoryStore:
    """Episodic memory - session/context-based retrieval"""
    
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.episodes: Dict[str, EpisodicEntry] = {}
        self.logger = logging.getLogger(f"episodic-memory.{agent_id}")
    
    def store_episode(self, session_id: str, query: str, response: str,
                     context: Dict[str, Any], emotional_valence: float = 0.0,
                     salience: float = 0.5, decisions: List[str] = None):
        """Store an episode from a session"""
        episode_id = f"episode_{hashlib.md5(f'{session_id}_{query}'.encode()).hexdigest()[:8]}"
        
        entry = EpisodicEntry(
            episode_id=episode_id,
            timestamp=datetime.now().timestamp(),
            session_id=session_id,
            query=query,
            response=response,
            context=context,
            emotional_valence=emotional_valence,
            salience=salience,
            decisions_involved=decisions or []
        )
        
        self.episodes[episode_id] = entry
        self.logger.info(f"Stored episode: {episode_id} (salience: {salience})")
        return episode_id
    
    def retrieve_session_context(self, session_id: str, k: int = 5) -> List[EpisodicEntry]:
        """Retrieve all episodes from a session"""
        session_episodes = [e for e in self.episodes.values() if e.session_id == session_id]
        # Sort by salience (most memorable first)
        session_episodes.sort(key=lambda e: e.salience, reverse=True)
        return session_episodes[:k]
    
    def retrieve_similar_episodes(self, query: str, k: int = 5) -> List[EpisodicEntry]:
        """Retrieve episodically similar past interactions"""
        if not self.episodes:
            return []
        
        # Simple text similarity (could use embeddings)
        query_words = set(query.lower().split())
        candidates = []
        
        for episode in self.episodes.values():
            episode_words = set(episode.query.lower().split())
            similarity = len(query_words & episode_words) / (len(query_words | episode_words) + 1e-6)
            candidates.append((similarity, episode))
        
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [episode for _, episode in candidates[:k]]
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get episodic memory statistics"""
        return {
            'total_episodes': len(self.episodes),
            'sessions_recorded': len(set(e.session_id for e in self.episodes.values())),
            'average_salience': sum(e.salience for e in self.episodes.values()) / len(self.episodes) if self.episodes else 0
        }


# ============================================================================
# PROCEDURAL MEMORY SYSTEM (Learned workflows)
# ============================================================================

@dataclass
class ProcedureStep:
    """Step in a learned procedure"""
    step_id: str
    description: str
    conditions: Dict[str, Any]  # Conditions under which this step applies
    action: str
    next_steps: List[str]
    success_rate: float  # How often this step leads to desired outcome


class ProceduralMemoryStore:
    """Procedural memory - learned workflows and patterns"""
    
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.procedures: Dict[str, List[ProcedureStep]] = {}
        self.logger = logging.getLogger(f"procedural-memory.{agent_id}")
    
    def learn_workflow(self, workflow_name: str, steps: List[ProcedureStep]):
        """Learn a new workflow/procedure"""
        self.procedures[workflow_name] = steps
        self.logger.info(f"Learned workflow: {workflow_name} ({len(steps)} steps)")
    
    def retrieve_relevant_procedure(self, context: Dict[str, Any]) -> Optional[List[ProcedureStep]]:
        """Retrieve a procedure matching current context"""
        # Simple matching - could be more sophisticated
        for workflow_name, steps in self.procedures.items():
            if any(self._context_matches(context, step.conditions) for step in steps):
                return steps
        return None
    
    def _context_matches(self, context: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
        """Check if context matches conditions"""
        for key, expected_value in conditions.items():
            if context.get(key) != expected_value:
                return False
        return True
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get procedural memory statistics"""
        return {
            'total_procedures': len(self.procedures),
            'total_steps': sum(len(steps) for steps in self.procedures.values()),
            'average_success_rate': sum(
                step.success_rate 
                for steps in self.procedures.values() 
                for step in steps
            ) / sum(len(steps) for steps in self.procedures.values()) if self.procedures else 0
        }


# ============================================================================
# INTEGRATED EXECUTIVE AGENT (ALL MEMORY SYSTEMS)
# ============================================================================

@dataclass
class FullyIntegratedExecutiveAgent:
    """Complete executive agent with ALL 3+3 memory systems"""
    name: str
    title: str
    bio: str
    personality: Dict[str, Any]
    system_prompt: str
    interview_data: Dict[str, Any]
    voice_config: Dict[str, Any]
    
    # CORE MEMORY SYSTEMS (3)
    interview_memory_stream: AuthenticityMemoryStream  # Park et al. retrieval
    bio_memory_store: BioExecutiveMemoryStore  # Executive persistent memory
    voice_bridge: VoiceIntegrationBridge  # Voice synthesis
    
    # EXPANSION MEMORY SYSTEMS (3)
    semantic_memory: SemanticMemoryStore  # Concept-based
    episodic_memory: EpisodicMemoryStore  # Session-based
    procedural_memory: ProceduralMemoryStore  # Workflow-based
    
    # Profile
    executive_profile: ExecutiveProfile
    
    @property
    def interview_questions_count(self) -> int:
        if self.interview_data.get('responses'):
            return len(self.interview_data['responses'])
        return 0
    
    @property
    def quality_score(self) -> float:
        return self.interview_data.get('quality_score', 0.0)
    
    async def process_query_with_all_memory(self, query: str, session_id: str, 
                                           context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Process query using ALL memory systems"""
        context = context or {}
        
        response_components = {}
        
        # 1. INTERVIEW MEMORY (authenticity)
        interview_result = self.interview_memory_stream.retrieve_for_query(query, k=3)
        response_components['interview'] = interview_result
        
        # 2. EXECUTIVE PERSISTENT MEMORY (decisions)
        decision_context = self.bio_memory_store.retrieve_decision_context(self.name, query, k=3)
        response_components['decisions'] = decision_context
        
        # 3. VOICE SYNTHESIS (prepare audio)
        voice_ready = bool(self.voice_config.get('voice_uuid'))
        response_components['voice_ready'] = voice_ready
        
        # 4. SEMANTIC MEMORY (concepts)
        # This would use embeddings - placeholder
        response_components['semantic'] = []
        
        # 5. EPISODIC MEMORY (session context)
        episode_context = self.episodic_memory.retrieve_session_context(session_id, k=3)
        response_components['episodes'] = [e.__dict__ for e in episode_context]
        
        # 6. PROCEDURAL MEMORY (workflows)
        procedure = self.procedural_memory.retrieve_relevant_procedure(context)
        response_components['procedure'] = procedure is not None
        
        # Store episode
        episode_id = self.episodic_memory.store_episode(
            session_id=session_id,
            query=query,
            response="[Processing...]",
            context=context,
            salience=0.7
        )
        
        response_components['episode_id'] = episode_id
        
        return response_components
    
    def get_complete_memory_report(self) -> Dict[str, Any]:
        """Get comprehensive report of all memory systems"""
        return {
            'agent': self.name,
            'core_memory_systems': {
                'interview': self.interview_memory_stream.get_statistics(),
                'executive_persistent': self.bio_memory_store.get_executive_pattern_insights(self.name),
                'voice': {
                    'voice_uuid': self.voice_config.get('voice_uuid'),
                    'model': self.voice_config.get('model'),
                    'ready': bool(self.voice_config.get('voice_uuid'))
                }
            },
            'expansion_memory_systems': {
                'semantic': self.semantic_memory.get_statistics(),
                'episodic': self.episodic_memory.get_statistics(),
                'procedural': self.procedural_memory.get_statistics()
            },
            'interview_quality': self.quality_score,
            'questions_available': self.interview_questions_count,
            'timestamp': datetime.now().isoformat()
        }


# ============================================================================
# ENHANCED AGENT LOADER (with all 6 memory systems)
# ============================================================================

class EnhancedIntegratedAgentLoader:
    """
    Loads agents with ALL 6 memory systems:
    
    Core (3):
    - Complete interview data (L0-L4)
    - Park et al. authenticity retrieval
    - Bio executive persistent memory
    - Voice synthesis integration
    
    Expansion (3):
    - Semantic memory (embeddings-based concepts)
    - Episodic memory (session/context-based)
    - Procedural memory (learned workflows)
    """
    
    def __init__(self, platform_path: str = "/home/ubuntu/executive_agents_platform"):
        self.platform_path = Path(platform_path)
        self.agents_path = self.platform_path / "agents"
        self.logger = logging.getLogger("enhanced-agent-loader")
    
    def load_agent_with_all_memory(self, agent_id: str) -> FullyIntegratedExecutiveAgent:
        """Load agent with all 6 memory systems initialized"""
        
        agent_path = self.agents_path / agent_id
        profile_path = agent_path / "agent_profile.yaml"
        voice_config_path = agent_path / "voice_config.yaml"
        interview_data_path = agent_path / "interview_data"
        
        # Load agent profile
        with open(profile_path) as f:
            profile = yaml.safe_load(f)
        
        # Load voice config
        with open(voice_config_path) as f:
            voice_config = yaml.safe_load(f)
        
        # Load interview data
        interview_data = self._load_interview_data(interview_data_path)
        
        # Initialize CORE memory systems (3)
        interview_memory = AuthenticityMemoryStream(agent_id, interview_data)
        bio_memory = BioExecutiveMemoryStore()
        voice_bridge = VoiceIntegrationBridge()
        
        # Initialize EXPANSION memory systems (3)
        semantic_memory = SemanticMemoryStore(agent_id)
        episodic_memory = EpisodicMemoryStore(agent_id)
        procedural_memory = ProceduralMemoryStore(agent_id)
        
        # Create executive profile
        executive_profile = ExecutiveProfile(
            name=profile['name'],
            title=profile['title'],
            organization=profile.get('organization', ''),
            biography=profile['bio']
        )
        
        # Create fully integrated agent
        agent = FullyIntegratedExecutiveAgent(
            name=profile['name'],
            title=profile['title'],
            bio=profile['bio'],
            personality=profile.get('personality', {}),
            system_prompt=profile.get('system_prompt', ''),
            interview_data=interview_data,
            voice_config=voice_config,
            interview_memory_stream=interview_memory,
            bio_memory_store=bio_memory,
            voice_bridge=voice_bridge,
            semantic_memory=semantic_memory,
            episodic_memory=episodic_memory,
            procedural_memory=procedural_memory,
            executive_profile=executive_profile
        )
        
        self.logger.info(f"Loaded agent: {profile['name']} with all 6 memory systems")
        
        return agent
    
    def _load_interview_data(self, interview_path: Path) -> Dict[str, Any]:
        """Load all interview data files (L0-L4)"""
        interview_data = {}
        
        if interview_path.exists():
            for json_file in interview_path.glob("*.json"):
                with open(json_file) as f:
                    data = json.load(f)
                    interview_data[json_file.stem] = data
        
        return interview_data


# Export for use in voice bridge
__all__ = [
    'FullyIntegratedExecutiveAgent',
    'EnhancedIntegratedAgentLoader',
    'SemanticMemoryStore',
    'EpisodicMemoryStore',
    'ProceduralMemoryStore',
]
