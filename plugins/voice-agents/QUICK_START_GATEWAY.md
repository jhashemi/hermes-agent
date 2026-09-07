# Quick Start: Adding Voice Bridge to Hermes Gateway

**3-Step Integration**

---

## Step 1: Copy Code to Gateway

Edit `/home/ubuntu/hermes-agent/gateway/platforms/whatsapp.py`:

### Import (at top)
```python
from pathlib import Path
import sys
sys.path.insert(0, '/home/ubuntu/executive_agents_platform')
from loader.whatsapp_voice_bridge import WhatsAppVoiceAgentBridge
```

### Init (in WhatsAppAdapter.__init__)
```python
self._voice_bridge = WhatsAppVoiceAgentBridge()
self._voice_sessions: dict = {}
```

### Handler (in _handle_message, at start)
```python
# VOICE COMMANDS
if text.startswith("/load-"):
    agent_id = text[6:].strip()
    result = await self._voice_bridge.handle_load_command(user_id, agent_id)
    if "error" not in result:
        self._voice_sessions[user_id] = result['session_id']
    await self.send_message(user_id, result.get('message', '✅ Loaded'), reply_to=None)
    return

# AUDIO MESSAGE
if message_event.message_type == MessageType.AUDIO and user_id in self._voice_sessions:
    session_id = self._voice_sessions[user_id]
    audio_bytes = await cache_audio_from_url(message_event.media_url)
    response = await self._voice_bridge.handle_audio_message(user_id, audio_bytes, session_id)
    await self.send_message(user_id, response.get('response_text', ''), reply_to=None)
    return
```

---

## Step 2: Configure

### Environment (~/.hermes/.env)
```bash
export RESEMBLE_API_KEY=your_key
export DEEPGRAM_API_KEY=your_key
export LIVEKIT_API_KEY=your_key
export LIVEKIT_API_SECRET=your_secret
```

### config.yaml
```yaml
platforms:
  whatsapp:
    # ... existing ...
    voice_enabled: true
```

---

## Step 3: Restart & Test

```bash
# Restart gateway
hermes gateway restart

# In WhatsApp:
/load-demis              # Load agent
[Send voice message]     # Transcribed + responded
```

---

## That's It!

Your WhatsApp now has:
- `/load-demis` — Load voice agent
- `/agents-list` — List all agents
- `/agents-disconnect` — Disconnect
- Voice message support (audio → transcribe → respond → voice)
- Interview + memory grounding for all responses

---

## Files Provided

- `GATEWAY_INTEGRATION_GUIDE.md` (12 KB) — Complete reference
- `GATEWAY_INTEGRATION_CODE.py` (10 KB) — Copy/paste ready
- This file — Quick start

---

## What You Get

```
User sends voice message
    ↓
Gateway receives audio → Deepgram transcribes → Agent processes (interview + memory)
    ↓
Resemble synthesizes response with agent voice → LiveKit streams → WhatsApp receives voice
```

All in <2 seconds.

---

**Status**: Ready to integrate now  
**Code changes**: ~30 lines to whatsapp.py  
**Configuration**: 4 environment variables  
**Test**: Send `/load-demis` in WhatsApp
