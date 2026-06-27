"""
Voice Integration System — Complete WhatsApp + LiveKit + Resemble Stack

Integrates:
1. Resemble AI voice cloning (rapid clones)
2. LiveKit Cloud WebRTC streaming
3. Deepgram Nova-3 real-time transcription
4. WhatsApp audio message handling
5. Interview data grounding for voice responses
"""

import os
import json
import asyncio
from typing import Dict, List, Optional, AsyncGenerator
from dataclasses import dataclass
from enum import Enum
import httpx


class VoiceProvider(Enum):
    """Voice synthesis providers"""
    RESEMBLE = "resemble"
    ELEVENLABS = "elevenlabs"
    DEEPGRAM = "deepgram"


@dataclass
class VoiceConfig:
    """Voice configuration for an agent"""
    agent_id: str
    provider: VoiceProvider
    voice_uuid: str  # Resemble UUID or ElevenLabs voice ID
    voice_name: str
    clone_type: str = "rapid"  # For Resemble: "rapid" (fast) or "professional" (slower)
    model: str = "nova-3"  # Resemble model
    quality: str = "high"
    output_format: str = "mp3"
    
    # Latency settings
    latency_target_ms: int = 200  # <200ms for real-time
    streaming_enabled: bool = True
    
    def to_dict(self) -> Dict:
        return {
            'agent_id': self.agent_id,
            'provider': self.provider.value,
            'voice_uuid': self.voice_uuid,
            'voice_name': self.voice_name,
            'clone_type': self.clone_type,
            'model': self.model,
            'quality': self.quality,
            'output_format': self.output_format,
            'latency_target_ms': self.latency_target_ms,
            'streaming_enabled': self.streaming_enabled
        }


@dataclass
class LiveKitConfig:
    """LiveKit Cloud configuration"""
    api_url: str = "https://executiveagents-l0dbzn9l.livekit.cloud"
    ws_url: str = "wss://executiveagents-l0dbzn9l.livekit.cloud"
    api_key: str = ""  # Set from env
    api_secret: str = ""  # Set from env
    room_name_prefix: str = "agent_session"
    max_participants: int = 2
    enable_recording: bool = False
    recording_layout: str = "speaker"


class ResembleVoiceClone:
    """Resemble AI voice cloning client"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv('RESEMBLE_API_KEY')
        self.base_url = "https://api.resemble.ai/v2"
        self.client = httpx.AsyncClient()
        
        if not self.api_key:
            raise ValueError("RESEMBLE_API_KEY environment variable not set")
    
    async def clone_voice_rapid(
        self,
        name: str,
        audio_url_or_path: str,
        description: str = ""
    ) -> Dict:
        """
        Create rapid voice clone from audio sample
        
        Rapid clones: 25-30s audio max, voice_type=rapid only
        Returns: {'uuid': 'voice_id', 'name': name, ...}
        """
        
        headers = {
            'Authorization': f'Token token={self.api_key}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'name': name,
            'description': description,
            'voice_type': 'rapid',  # CRITICAL: Never use 'professional' (fails at "initializing")
        }
        
        # Load audio file and encode as base64
        if audio_url_or_path.startswith('http'):
            # Download from URL
            async with self.client.stream('GET', audio_url_or_path) as response:
                audio_bytes = await response.aread()
        else:
            # Load from file path
            with open(audio_url_or_path, 'rb') as f:
                audio_bytes = f.read()
        
        # Validate audio length
        # Assuming MP3 at ~128kbps: 25s max = 400KB
        if len(audio_bytes) > 400_000:
            raise ValueError(f"Audio too long: {len(audio_bytes)} bytes (max 400KB for 25-30s)")
        
        import base64
        audio_b64 = base64.b64encode(audio_bytes).decode()
        payload['audio_data'] = audio_b64
        
        response = await self.client.post(
            f"{self.base_url}/voices",
            json=payload,
            headers=headers
        )
        
        if response.status_code != 201:
            raise Exception(f"Resemble clone failed: {response.text}")
        
        return response.json()
    
    async def synthesize_speech(
        self,
        text: str,
        voice_uuid: str,
        output_file: str = None
    ) -> bytes:
        """
        Synthesize speech using voice clone
        
        Args:
            text: Text to synthesize (max 5000 chars for rapid voices)
            voice_uuid: UUID of voice to use
            output_file: Optional file path to save audio
        
        Returns:
            Audio bytes (MP3)
        """
        
        if len(text) > 5000:
            raise ValueError(f"Text too long: {len(text)} chars (max 5000)")
        
        headers = {
            'Authorization': f'Token token={self.api_key}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'text': text,
            'voice_uuid': voice_uuid,
            'output_format': 'mp3',
            'quality': 'high',
            'voice_type': 'rapid'  # Use rapid for speed
        }
        
        response = await self.client.post(
            f"{self.base_url}/projects/{os.getenv('RESEMBLE_PROJECT_ID')}/synthesize",
            json=payload,
            headers=headers,
            timeout=30.0
        )
        
        if response.status_code != 200:
            raise Exception(f"Synthesis failed: {response.text}")
        
        audio_bytes = response.content
        
        if output_file:
            with open(output_file, 'wb') as f:
                f.write(audio_bytes)
        
        return audio_bytes
    
    async def synthesize_speech_streaming(
        self,
        text: str,
        voice_uuid: str
    ) -> AsyncGenerator[bytes, None]:
        """
        Stream synthesized speech for real-time playback
        
        Yields audio chunks as they arrive from Resemble
        """
        
        headers = {
            'Authorization': f'Token token={self.api_key}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'text': text,
            'voice_uuid': voice_uuid,
            'output_format': 'mp3',
            'quality': 'high',
            'voice_type': 'rapid',
            'stream': True
        }
        
        async with self.client.stream(
            'POST',
            f"{self.base_url}/projects/{os.getenv('RESEMBLE_PROJECT_ID')}/synthesize",
            json=payload,
            headers=headers,
            timeout=30.0
        ) as response:
            if response.status_code != 200:
                raise Exception(f"Stream synthesis failed: {response.text}")
            
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if chunk:
                    yield chunk


class DeepgramTranscription:
    """Deepgram Nova-3 real-time transcription client"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv('DEEPGRAM_API_KEY')
        self.client = httpx.AsyncClient()
        
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY environment variable not set")
    
    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        audio_format: str = "mp3"
    ) -> Dict:
        """
        Transcribe audio using Deepgram Nova-3
        
        Returns: {'text': 'transcribed text', 'confidence': 0.95, ...}
        """
        
        headers = {
            'Authorization': f'Token {self.api_key}',
            'Content-Type': f'audio/{audio_format}'
        }
        
        response = await self.client.post(
            "https://api.deepgram.com/v1/listen?model=nova-3&tier=base",
            content=audio_bytes,
            headers=headers,
            timeout=30.0
        )
        
        if response.status_code != 200:
            raise Exception(f"Transcription failed: {response.text}")
        
        result = response.json()
        
        return {
            'text': result.get('results', {}).get('channels', [{}])[0].get('alternatives', [{}])[0].get('transcript', ''),
            'confidence': result.get('results', {}).get('channels', [{}])[0].get('alternatives', [{}])[0].get('confidence', 0),
            'duration': result.get('metadata', {}).get('duration', 0),
            'raw': result
        }


class LiveKitAgentSession:
    """LiveKit voice agent session"""
    
    def __init__(
        self,
        agent_id: str,
        voice_config: VoiceConfig,
        livekit_config: LiveKitConfig,
        resemble_client: ResembleVoiceClone
    ):
        self.agent_id = agent_id
        self.voice_config = voice_config
        self.livekit_config = livekit_config
        self.resemble_client = resemble_client
        
        # Session state
        self.room_token = None
        self.ws_connection = None
        self.session_id = None
    
    async def create_session(self) -> Dict:
        """Create new LiveKit session"""
        
        import secrets
        import time
        
        self.session_id = f"{self.agent_id}_{int(time.time())}_{secrets.token_hex(4)}"
        
        # In production: use LiveKit SDK to create room
        # For now: return config for client
        
        return {
            'session_id': self.session_id,
            'room_name': self.session_id,
            'agent_id': self.agent_id,
            'voice_config': self.voice_config.to_dict(),
            'livekit_config': {
                'api_url': self.livekit_config.api_url,
                'ws_url': self.livekit_config.ws_url,
                'room_name': self.session_id
            }
        }
    
    async def stream_agent_response(
        self,
        text: str
    ) -> AsyncGenerator[bytes, None]:
        """Stream agent response audio"""
        
        async for chunk in self.resemble_client.synthesize_speech_streaming(
            text=text,
            voice_uuid=self.voice_config.voice_uuid
        ):
            yield chunk


class VoiceIntegrationBridge:
    """
    Bridge between interview agents and voice synthesis
    
    Connects:
    - IntegratedExecutiveAgent (interview + memory)
    - Resemble voice cloning
    - LiveKit streaming
    - Deepgram transcription
    - WhatsApp audio messaging
    """
    
    def __init__(
        self,
        livekit_config: LiveKitConfig = None,
        resemble_api_key: str = None,
        deepgram_api_key: str = None
    ):
        self.livekit_config = livekit_config or LiveKitConfig()
        self.resemble_client = ResembleVoiceClone(resemble_api_key)
        self.deepgram_client = DeepgramTranscription(deepgram_api_key)
        
        # Active sessions
        self.sessions: Dict[str, LiveKitAgentSession] = {}
    
    async def create_agent_voice_session(
        self,
        agent_id: str,
        voice_config: VoiceConfig
    ) -> LiveKitAgentSession:
        """Create voice session for agent"""
        
        session = LiveKitAgentSession(
            agent_id=agent_id,
            voice_config=voice_config,
            livekit_config=self.livekit_config,
            resemble_client=self.resemble_client
        )
        
        session_config = await session.create_session()
        self.sessions[session_config['session_id']] = session
        
        return session
    
    async def process_user_audio(
        self,
        session_id: str,
        audio_bytes: bytes,
        audio_format: str = "mp3"
    ) -> str:
        """
        Process user audio input
        
        1. Transcribe with Deepgram
        2. Return transcribed text
        """
        
        result = await self.deepgram_client.transcribe_audio(
            audio_bytes=audio_bytes,
            audio_format=audio_format
        )
        
        return result['text']
    
    async def generate_agent_response_audio(
        self,
        session_id: str,
        response_text: str
    ) -> AsyncGenerator[bytes, None]:
        """
        Generate agent response audio
        
        1. Get session
        2. Stream via Resemble + LiveKit
        """
        
        if session_id not in self.sessions:
            raise ValueError(f"Session not found: {session_id}")
        
        session = self.sessions[session_id]
        
        async for chunk in session.stream_agent_response(response_text):
            yield chunk


# Environment configuration
def load_voice_config_from_env() -> Dict:
    """Load voice configuration from environment variables"""
    return {
        'resemble': {
            'api_key': os.getenv('RESEMBLE_API_KEY'),
            'project_id': os.getenv('RESEMBLE_PROJECT_ID')
        },
        'deepgram': {
            'api_key': os.getenv('DEEPGRAM_API_KEY')
        },
        'livekit': {
            'api_url': os.getenv('LIVEKIT_API_URL', 'https://executiveagents-l0dbzn9l.livekit.cloud'),
            'ws_url': os.getenv('LIVEKIT_WS_URL', 'wss://executiveagents-l0dbzn9l.livekit.cloud'),
            'api_key': os.getenv('LIVEKIT_API_KEY'),
            'api_secret': os.getenv('LIVEKIT_API_SECRET')
        }
    }


if __name__ == '__main__':
    # Test configuration
    config = load_voice_config_from_env()
    print("Voice configuration loaded:")
    print(json.dumps({k: {kk: 'SET' if vv else 'MISSING' for kk, vv in v.items()} for k, v in config.items()}, indent=2))
