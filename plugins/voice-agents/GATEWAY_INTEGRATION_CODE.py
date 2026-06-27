"""
WhatsApp Gateway Voice Integration - Ready to Copy/Paste

Add these methods + imports to:
/home/ubuntu/hermes-agent/gateway/platforms/whatsapp.py

This code integrates executive agents voice bridge into Hermes Gateway
"""

# ============================================================================
# IMPORTS (add to top of whatsapp.py)
# ============================================================================

# Add to existing imports:
import sys
from pathlib import Path

# Add executive agents imports
sys.path.insert(0, '/home/ubuntu/executive_agents_platform')
from loader.whatsapp_voice_bridge import WhatsAppVoiceAgentBridge

# ============================================================================
# INITIALIZATION (add to WhatsAppAdapter.__init__)
# ============================================================================

def __init_voice_bridge(self):
    """Initialize voice agent bridge"""
    try:
        self._voice_bridge = WhatsAppVoiceAgentBridge()
        self._voice_sessions: dict = {}  # user_id -> session_id
        logger.info("[whatsapp] Voice bridge initialized")
    except Exception as e:
        logger.error(f"[whatsapp] Failed to initialize voice bridge: {e}")
        self._voice_bridge = None

# Call from __init__:
# self.__init_voice_bridge()

# ============================================================================
# MESSAGE ROUTER (replace in _handle_message)
# ============================================================================

async def _handle_message_with_voice(self, message_event):
    """Enhanced message handler with voice support"""
    user_id = message_event.user_id
    text = (message_event.text or "").strip()
    
    # VOICE COMMANDS - intercept before sending to agent
    if self._voice_bridge and text:
        # /load-agent-id commands
        if text.startswith("/load-"):
            agent_id = text[6:].lower().strip()
            await self._voice_load_command(user_id, agent_id)
            return
        
        # /agents-list
        if text == "/agents-list":
            await self._voice_list_agents(user_id)
            return
        
        # /agents-disconnect
        if text == "/agents-disconnect":
            await self._voice_disconnect(user_id)
            return
    
    # AUDIO MESSAGE HANDLING
    if self._voice_bridge and message_event.message_type == MessageType.AUDIO:
        if user_id in self._voice_sessions:
            await self._handle_voice_audio(message_event)
            return
        else:
            await self.send_message(
                user_id,
                "❌ No agent loaded. Use /load-demis to get started.",
                reply_to=None
            )
            return
    
    # DEFAULT: existing message handling continues
    await self._handle_message_existing(message_event)

# ============================================================================
# VOICE COMMAND HANDLERS
# ============================================================================

async def _voice_load_command(self, user_id: str, agent_id: str) -> None:
    """Load voice agent for user"""
    try:
        result = await self._voice_bridge.handle_load_command(user_id, agent_id)
        
        if "error" in result:
            await self.send_message(user_id, f"❌ {result['error']}", reply_to=None)
            return
        
        # Store session
        self._voice_sessions[user_id] = result['session_id']
        
        # Send confirmation
        message = result.get('message', f"✅ Loaded {agent_id}")
        await self.send_message(user_id, message, reply_to=None)
        
        logger.info(f"[whatsapp-voice] User {user_id} loaded agent {agent_id}")
    
    except Exception as e:
        logger.error(f"[whatsapp-voice] Load command failed: {e}")
        await self.send_message(user_id, f"❌ Error: {str(e)}", reply_to=None)

async def _voice_list_agents(self, user_id: str) -> None:
    """List available agents"""
    try:
        agents = self._voice_bridge.loader.list_agents()
        
        if not agents:
            await self.send_message(user_id, "❌ No agents available", reply_to=None)
            return
        
        lines = ["🤖 Available Agents:\n"]
        for agent in agents:
            status = "✅" if agent['status'] == "ready" else "⏳"
            lines.append(f"{status} /load-{agent['id']}")
            lines.append(f"   Questions: {agent['questions']}")
            lines.append(f"   Quality: {agent['quality']}")
            lines.append("")
        
        message = "\n".join(lines)
        await self.send_message(user_id, message, reply_to=None)
    
    except Exception as e:
        logger.error(f"[whatsapp-voice] List failed: {e}")

async def _voice_disconnect(self, user_id: str) -> None:
    """Disconnect from voice agent"""
    if user_id in self._voice_sessions:
        del self._voice_sessions[user_id]
    
    message = "✅ Disconnected. Use /load-{agent} to reconnect."
    await self.send_message(user_id, message, reply_to=None)

# ============================================================================
# AUDIO MESSAGE HANDLER
# ============================================================================

async def _handle_voice_audio(self, message_event):
    """Process WhatsApp audio message through voice agent"""
    user_id = message_event.user_id
    session_id = self._voice_sessions.get(user_id)
    
    if not session_id:
        await self.send_message(
            user_id,
            "❌ Session lost. Use /load-demis to reconnect.",
            reply_to=None
        )
        return
    
    # Get agent ID from session (stored in bridge)
    agent_id = None
    for aid in self._voice_bridge.loader._loaded_agents.keys():
        agent_id = aid
        break
    
    if not agent_id:
        await self.send_message(
            user_id,
            "❌ Agent error. Use /load-demis to reconnect.",
            reply_to=None
        )
        return
    
    # Download audio
    try:
        audio_bytes = await cache_audio_from_url(message_event.media_url)
    except Exception as e:
        logger.error(f"[whatsapp-voice] Audio download failed: {e}")
        await self.send_message(
            user_id,
            f"❌ Failed to download audio: {str(e)}",
            reply_to=None
        )
        return
    
    # Process audio
    try:
        response = await self._voice_bridge.handle_audio_message(
            user_id=user_id,
            audio_bytes=audio_bytes,
            session_id=session_id
        )
        
        if "error" in response:
            await self.send_message(user_id, f"❌ {response['error']}", reply_to=None)
            return
        
        # Send text response
        user_input = response.get('user_input', '[voice]')
        response_text = response.get('response_text', '')
        
        message = f"📝 You: {user_input}\n\n🤖 Agent: {response_text}"
        await self.send_message(user_id, message, reply_to=None)
        
        # Generate voice response
        try:
            audio_data = b""
            async for chunk in self._voice_bridge.generate_response_audio(
                response_text=response_text,
                session_id=session_id,
                agent_id=agent_id
            ):
                audio_data += chunk
            
            if audio_data:
                # Save and send audio
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(audio_data)
                    audio_file = f.name
                
                await self.send_file(user_id, audio_file, file_type="audio")
                logger.info(f"[whatsapp-voice] Sent voice response to {user_id}")
        
        except Exception as e:
            logger.error(f"[whatsapp-voice] Voice generation failed: {e}")
            await self.send_message(
                user_id,
                f"⚠️  Text response above. Voice: {str(e)}",
                reply_to=None
            )
    
    except Exception as e:
        logger.error(f"[whatsapp-voice] Audio processing failed: {e}")
        await self.send_message(
            user_id,
            f"❌ Processing failed: {str(e)}",
            reply_to=None
        )

# ============================================================================
# INTEGRATION CHECKLIST
# ============================================================================
"""
1. Add imports to whatsapp.py:
   from loader.whatsapp_voice_bridge import WhatsAppVoiceAgentBridge

2. In WhatsAppAdapter.__init__(), add:
   self._voice_bridge = WhatsAppVoiceAgentBridge()
   self._voice_sessions = {}

3. In _handle_message(), replace with _handle_message_with_voice()

4. Add all voice command handler methods above

5. Update config.yaml:
   platforms:
     whatsapp:
       voice_enabled: true
       voice_config:
         interview_data_path: /home/ubuntu/executive_agents_platform

6. Set environment variables in ~/.hermes/.env:
   RESEMBLE_API_KEY=...
   DEEPGRAM_API_KEY=...
   LIVEKIT_API_KEY=...
   LIVEKIT_API_SECRET=...

7. Restart gateway:
   hermes gateway restart

8. Test in WhatsApp:
   /load-demis
   [Send voice message]
"""

# ============================================================================
# MINIMAL EXAMPLE (Just Add These)
# ============================================================================
"""
Absolute minimum to get started:

In __init__:
  self._voice_bridge = WhatsAppVoiceAgentBridge()
  self._voice_sessions = {}

In _handle_message(), add at top:
  if text.startswith("/load-"):
      result = await self._voice_bridge.handle_load_command(user_id, text[6:])
      self._voice_sessions[user_id] = result['session_id']
      await self.send_message(user_id, result.get('message', '✅ Loaded'))
      return

That's it! Now users can send /load-demis and voice messages work.
"""
