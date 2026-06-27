"""
Voice-Enabled Agent Loader

Integrates voice synthesis with IntegratedExecutiveAgent
Creates complete voice + interview + memory pipeline
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

from integrated_agent_loader import IntegratedAgentLoader, IntegratedExecutiveAgent
from voice_integration import (
    VoiceConfig, VoiceProvider, VoiceIntegrationBridge,
    LiveKitConfig, ResembleVoiceClone
)


@dataclass
class VoiceEnabledAgent:
    """Agent with voice synthesis integration"""
    agent: IntegratedExecutiveAgent
    voice_config: VoiceConfig
    voice_bridge: VoiceIntegrationBridge
    
    async def create_voice_session(self):
        """Create new voice session for this agent"""
        return await self.voice_bridge.create_agent_voice_session(
            self.agent.name,
            self.voice_config
        )
    
    async def process_whatsapp_audio(self, audio_bytes: bytes, session_id: str = None) -> str:
        """Process WhatsApp audio input"""
        if not session_id:
            raise ValueError("session_id required")
        
        # Transcribe user audio
        user_text = await self.voice_bridge.process_user_audio(
            session_id=session_id,
            audio_bytes=audio_bytes,
            audio_format="mp3"
        )
        
        return user_text
    
    async def generate_voice_response(self, text: str, session_id: str):
        """Generate voice response, stream back to WhatsApp"""
        
        # Stream audio chunks
        async for audio_chunk in self.voice_bridge.generate_agent_response_audio(
            session_id=session_id,
            response_text=text
        ):
            yield audio_chunk


class VoiceEnabledAgentLoader:
    """
    Loads agents with complete voice integration:
    - Interview data (L0-L4)
    - Park et al. authenticity retrieval
    - Bio executive persistent memory
    - Voice synthesis (Resemble)
    - LiveKit streaming
    - Deepgram transcription
    """
    
    def __init__(
        self,
        platform_root: Path = None,
        livekit_config: LiveKitConfig = None
    ):
        self.interview_loader = IntegratedAgentLoader(platform_root)
        self.platform_root = platform_root or Path("/home/ubuntu/executive_agents_platform")
        
        # Initialize voice infrastructure
        self.livekit_config = livekit_config or LiveKitConfig()
        self.voice_bridge = VoiceIntegrationBridge(
            livekit_config=self.livekit_config,
            resemble_api_key=os.getenv('RESEMBLE_API_KEY'),
            deepgram_api_key=os.getenv('DEEPGRAM_API_KEY')
        )
        
        self._loaded_agents: Dict[str, VoiceEnabledAgent] = {}
    
    def load_voice_config_from_yaml(self, agent_id: str) -> VoiceConfig:
        """Load voice configuration for agent from YAML"""
        
        config_path = (
            self.platform_root / "agents" / agent_id / "voice_config.yaml"
        )
        
        import yaml
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
        
        voice_section = config_data.get('voice', {})
        
        return VoiceConfig(
            agent_id=agent_id,
            provider=VoiceProvider(voice_section.get('provider', 'resemble')),
            voice_uuid=voice_section.get('voice_uuid', ''),
            voice_name=voice_section.get('voice_name', ''),
            clone_type=voice_section.get('clone_type', 'rapid'),
            model=voice_section.get('model', 'nova-3'),
            quality=voice_section.get('quality', 'high'),
            output_format=voice_section.get('output_format', 'mp3'),
            latency_target_ms=voice_section.get('latency_target_ms', 200),
            streaming_enabled=voice_section.get('streaming_enabled', True)
        )
    
    def load_voice_enabled_agent(self, agent_id: str) -> VoiceEnabledAgent:
        """Load agent with complete voice integration"""
        
        if agent_id in self._loaded_agents:
            return self._loaded_agents[agent_id]
        
        # Load interview + memory agent
        interview_agent = self.interview_loader.load_agent(agent_id)
        
        # Load voice configuration
        voice_config = self.load_voice_config_from_yaml(agent_id)
        
        # Create voice-enabled agent
        agent = VoiceEnabledAgent(
            agent=interview_agent,
            voice_config=voice_config,
            voice_bridge=self.voice_bridge
        )
        
        self._loaded_agents[agent_id] = agent
        return agent


# WhatsApp Integration Point
class WhatsAppVoiceAgentBridge:
    """
    Bridge between WhatsApp messaging and voice agents
    
    Handles:
    1. /load-{agent} commands → create voice session
    2. Audio messages → transcribe + get response
    3. Response → synthesize + send audio back to WhatsApp
    """
    
    def __init__(self, platform_root: Path = None):
        self.loader = VoiceEnabledAgentLoader(platform_root)
        self.active_sessions: Dict[str, str] = {}  # user_id -> agent_id
    
    async def handle_load_command(
        self,
        user_id: str,
        agent_id: str
    ) -> Dict[str, Any]:
        """Handle /load-{agent} command"""
        
        # Load agent
        agent = self.loader.load_voice_enabled_agent(agent_id)
        
        # Create voice session
        session = await agent.create_voice_session()
        
        # Store active session
        self.active_sessions[user_id] = agent_id
        
        return {
            'status': 'session_created',
            'agent': agent.agent.name,
            'session_id': session['session_id'],
            'message': f"✅ Connected to {agent.agent.name}\n\n📱 Send audio messages or text"
        }
    
    async def handle_audio_message(
        self,
        user_id: str,
        audio_bytes: bytes,
        session_id: str
    ) -> Dict[str, Any]:
        """Handle audio message from WhatsApp"""
        
        # Get active agent
        agent_id = self.active_sessions.get(user_id)
        if not agent_id:
            return {'error': 'No agent loaded. Use /load-{agent}'}
        
        agent = self.loader.load_voice_enabled_agent(agent_id)
        
        # Transcribe user audio
        try:
            user_text = await agent.process_whatsapp_audio(
                audio_bytes=audio_bytes,
                session_id=session_id
            )
        except Exception as e:
            return {'error': f'Transcription failed: {str(e)}'}
        
        # Get interview + memory response
        response_text = agent.agent.get_authentic_response(user_text)
        
        # Store as decision
        agent.agent.store_decision(
            question=user_text,
            decision=response_text,
            reasoning="Response generated from audio input",
            domains=[],
            grounded_responses=[]
        )
        
        return {
            'status': 'response_ready',
            'user_input': user_text,
            'response_text': response_text,
            'session_id': session_id
        }
    
    async def generate_response_audio(
        self,
        response_text: str,
        session_id: str,
        agent_id: str
    ):
        """Generate voice response audio to stream back"""
        
        agent = self.loader.load_voice_enabled_agent(agent_id)
        
        async for chunk in agent.generate_voice_response(
            response_text,
            session_id
        ):
            yield chunk


if __name__ == '__main__':
    print("Voice Integration Ready")
    print("=" * 50)
    print(f"Platform: /home/ubuntu/executive_agents_platform/")
    print(f"Voice Providers: Resemble AI + LiveKit + Deepgram")
    print(f"Ready to stream agent responses via WhatsApp audio")
