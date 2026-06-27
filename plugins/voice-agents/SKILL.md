---
name: install-voice-bridge
aliases: [voice-bridge-setup, configure-voice-agents]
description: |
  Automated installation and configuration of the executive voice agent bridge 
  for Hermes Gateway. Sets up voice synthesis, speech transcription, and interview-based 
  agent responses with SOLID design principles (zero platform adapter modifications).
  
  This skill:
  - Verifies prerequisites (Hermes Gateway, Python environment)
  - Copies hook files to gateway/builtin_hooks/
  - Modifies gateway/run.py with hook integration (8 lines)
  - Configures environment variables for voice services
  - Tests installation with voice commands
  - Provides rollback capability
  
  Supports: WhatsApp, Telegram (all platforms via hook system)
  Voice Agents: Demis Hassabis, Steve Jobs, Jony Ive, Jeff Dean, Donald Knuth, etc.
  Memory Systems: 
    - Park et al. authenticity retrieval (289-question interviews)
    - Bio executive persistent memory (decision tracking)
    - Voice synthesis (Resemble AI rapid clones)

category: devops
tags: [voice, agents, gateway, livekit, resemble, deepgram, installation, automation]
triggers:
  - user asks to "install voice bridge"
  - user asks to "set up voice agents" 
  - user asks to "configure executive agents"
  - user needs to "deploy voice bridge"
  - user says "automate voice bridge setup"

---

# Voice Bridge Skill - Hermes Gateway Installation

## Overview

This skill automates the complete installation and configuration of the executive voice agent bridge system for Hermes Gateway. It requires minimal user input and handles all complex setup tasks.

## What This Skill Does

1. **Prerequisites Verification**
   - Checks Hermes Gateway is installed
   - Verifies Python 3.9+ available
   - Confirms all required directories exist

2. **Hook System Installation**
   - Creates `/gateway/builtin_hooks/` directory
   - Copies voice agent hook implementation
   - Creates hook initialization module

3. **Gateway Integration**
   - Modifies `gateway/run.py` with hook initialization (3 lines at startup)
   - Adds hook manager dispatch (5 lines in _handle_message)
   - Automatic backup of original files

4. **Environment Configuration**
   - Creates/updates `~/.hermes/.env`
   - Configures API keys for:
     - Resemble (voice synthesis)
     - Deepgram (speech transcription)
     - LiveKit (real-time streaming)

5. **Testing & Validation**
   - Tests hook initialization
   - Verifies environment variables
   - Tests voice commands in gateway
   - Provides live logs monitoring

6. **Rollback Support**
   - Stores backups of modified files
   - One-command rollback if needed
   - Clean uninstallation

## Prerequisites

- Hermes Agent installed: `/home/ubuntu/hermes-agent/`
- Executive agents platform: `/home/ubuntu/executive_agents_platform/`
- Python 3.9+
- Bash/Shell access
- API keys for:
  - Resemble AI (voice synthesis)
  - Deepgram (speech transcription)
  - LiveKit (real-time streaming)

## Quick Start

```bash
hermes skill run install-voice-bridge
```

The skill will guide you through:
1. Verification
2. Installation
3. Configuration
4. Testing

## Advanced Usage

### Dry-Run (See What Would Happen)

```bash
hermes skill run install-voice-bridge --dry-run
```

### Install with Backup

```bash
hermes skill run install-voice-bridge --backup
```

### Install Specific Components

```bash
# Only verify prerequisites
hermes skill run install-voice-bridge verify

# Only create directories
hermes skill run install-voice-bridge setup-dirs

# Only modify gateway/run.py
hermes skill run install-voice-bridge patch-gateway

# Only configure environment
hermes skill run install-voice-bridge configure-env

# Only run tests
hermes skill run install-voice-bridge test

# Rollback previous installation
hermes skill run install-voice-bridge rollback
```

## Configuration Options

Create `.hermes/voice-bridge-config.yaml`:

```yaml
# Installation settings
installation:
  hermes_root: /home/ubuntu/hermes-agent
  executive_root: /home/ubuntu/executive_agents_platform
  backup: true
  dry_run: false

# API Configuration
apis:
  resemble:
    api_key: ${RESEMBLE_API_KEY}
    provider: resemble
    voice_type: rapid
  deepgram:
    api_key: ${DEEPGRAM_API_KEY}
    model: nova-3
  livekit:
    api_url: https://executiveagents-l0dbzn9l.livekit.cloud
    api_key: ${LIVEKIT_API_KEY}
    api_secret: ${LIVEKIT_API_SECRET}

# Voice Agents
agents:
  demis_hassabis:
    enabled: true
    voice_uuid: 36eb02fe
  steve_jobs:
    enabled: true
    voice_uuid: ""  # Configure after installation
  jony_ive:
    enabled: false

# Testing
testing:
  test_commands: true
  test_audio: true
  test_memory: true
```

## Architecture

### Hook System

The skill installs a SOLID-compliant hook system that intercepts messages before platform adapters process them:

```
Message Received
    ↓
gateway/run.py (8 lines added)
    ├─ Hook Manager
    │   ├─ VoiceAgentMessageInterceptor
    │   │   ├─ /load-{agent}? → INTERCEPT
    │   │   ├─ /voice-agents? → INTERCEPT
    │   │   ├─ audio message? → INTERCEPT
    │   │   └─ other → PASS THROUGH
    │   └─ (Future hooks)
    │
    └─ If no match: Original Platform Handler (unchanged)
```

### Modified Files

- `gateway/run.py`: +8 lines (2 locations, 3 + 5 lines)
- `~/.hermes/.env`: +4 environment variables

### New Files

- `gateway/builtin_hooks/voice_agent_hook.py` (424 lines)
- `gateway/builtin_hooks/__init__.py` (62 lines)

### Unchanged Files

- `gateway/platforms/whatsapp.py` (100% untouched)
- `gateway/platforms/telegram.py` (100% untouched)
- All other platform adapters (untouched)

## Voice Commands Available After Installation

```
/load-demis                 # Load Demis Hassabis voice agent
/load-steve-jobs           # Load Steve Jobs
/load-jony                 # Load Jony Ive
/load-jeff                 # Load Jeff Dean
/load-knuth                # Load Donald Knuth
/load-tigani               # Load Jordan Tigani
/load-turing               # Load Alan Turing

/voice-agents              # List available voice agents
/voice-disconnect          # Disconnect from current agent

[Send audio message]       # Transcribed + processed + voice responded
```

## Output Example

```
╔════════════════════════════════════════════════════════════════════════╗
║           HERMES VOICE BRIDGE INSTALLATION & CONFIGURATION            ║
╚════════════════════════════════════════════════════════════════════════╝

Step 1: Prerequisites Verification
✓ Hermes Gateway installed at /home/ubuntu/hermes-agent
✓ Executive agents platform at /home/ubuntu/executive_agents_platform
✓ Python 3.11 available
✓ All directories exist
✓ Gateway run.py found (line count: 5927)

Step 2: Creating Directory Structure
✓ Created /home/ubuntu/hermes-agent/gateway/builtin_hooks/
✓ Verified hook files exist:
  - voice_agent_hook.py (424 lines)
  - __init__.py (62 lines)

Step 3: Patching gateway/run.py
✓ Backed up to: gateway/run.py.backup.20260511_091234
✓ Added 3 lines at startup (initialize_builtin_hooks)
✓ Added 5 lines in _handle_message (hook manager dispatch)
✓ Total modifications: 8 lines (non-invasive)

Step 4: Configuring Environment Variables
✓ ~/.hermes/.env created
✓ RESEMBLE_API_KEY configured
✓ DEEPGRAM_API_KEY configured
✓ LIVEKIT_API_KEY configured
✓ LIVEKIT_API_SECRET configured

Step 5: Restarting Gateway
⏳ Stopping gateway...
✓ Gateway stopped
⏳ Starting gateway...
✓ Gateway started (PID: 12345)

Step 6: Verifying Installation
✓ Hook manager initialized
✓ Voice bridge module loaded
✓ Environment variables loaded
✓ All voice agents registered (7 agents ready)

Step 7: Testing Voice Commands
✓ /load-demis → ✓ Connected to Demis Hassabis
✓ /voice-agents → ✓ 7 agents listed
✓ /voice-disconnect → ✓ Disconnected

╔════════════════════════════════════════════════════════════════════════╗
║                    ✅ INSTALLATION COMPLETE                           ║
╚════════════════════════════════════════════════════════════════════════╝

Next Steps:
1. Test in WhatsApp: /load-demis
2. Send audio message to hear voice response
3. Monitor logs: hermes logs --follow --gateway | grep voice-hook
4. Configure additional voice agents as needed
5. Deploy to production instances

Rollback Command (if needed):
  hermes skill run install-voice-bridge rollback
```

## Troubleshooting

### Installation Failed

```bash
# Check prerequisites
hermes skill run install-voice-bridge verify

# Check logs
hermes logs --follow | grep -i error

# Check Python syntax
python3 -m py_compile /home/ubuntu/hermes-agent/gateway/builtin_hooks/voice_agent_hook.py
```

### Voice Commands Not Working

```bash
# Check hook initialization
hermes logs --follow | grep voice-hook

# Check environment variables
cat ~/.hermes/.env | grep RESEMBLE

# Check gateway restart
hermes gateway restart
hermes logs --follow --gateway | head -50
```

### Audio Messages Not Processed

```bash
# Verify session loaded
/load-demis in WhatsApp

# Check Deepgram/Resemble keys
cat ~/.hermes/.env | grep -E "DEEPGRAM|RESEMBLE"

# Check logs for API errors
hermes logs --follow --gateway | grep -i "deepgram\|resemble"
```

### Rollback

```bash
# Revert all modifications
hermes skill run install-voice-bridge rollback

# Verify rollback
hermes logs --follow | grep rollback
```

## Files Reference

### Installed by Skill

```
gateway/builtin_hooks/
├── voice_agent_hook.py       (424 lines - Hook implementation)
└── __init__.py               (62 lines - Hook initialization)

gateway/run.py                (+8 lines - Hook integration)
~/.hermes/.env                (+4 variables - API configuration)
```

### Backups Created

```
gateway/run.py.backup.TIMESTAMP     (Original gateway/run.py)
.hermes/.env.backup.TIMESTAMP       (Original environment file)
```

## Advanced Features

### Custom Voice Agents

After installation, add new voice agents by creating agent profiles:

```bash
# In executive_agents_platform/agents/
mkdir -p agents/your_agent/interview_data/
# Add L0-L4 interview data JSON files
# Create agent_profile.yaml and voice_config.yaml

# Register in agents/agents_registry.yaml
```

### Memory System Integration

The installed system includes three memory layers:

1. **Interview Authenticity** (Park et al.)
   - 289-question interview responses
   - Poignancy scoring for relevant responses

2. **Executive Persistent Memory**
   - Decision tracking across sessions
   - Context accumulation
   - Pattern learning

3. **Voice Synthesis**
   - Resemble AI rapid clones
   - Real-time audio streaming
   - <2 second latency

### Monitoring & Logs

```bash
# Real-time logs
hermes logs --follow --gateway | grep voice

# Search logs
hermes logs --search "voice-hook" --limit 100

# Export logs
hermes logs --export --format json --file voice-bridge.json
```

## Performance

- Installation time: ~5-10 minutes
- Hook dispatch overhead: <50ms
- Voice synthesis latency: <2 seconds
- Audio transcription latency: <500ms
- Memory retrieval: <100ms

## Uninstallation

```bash
# Complete rollback
hermes skill run install-voice-bridge rollback

# Remove hook directory (optional)
rm -rf /home/ubuntu/hermes-agent/gateway/builtin_hooks

# Remove backup files (optional)
rm -f /home/ubuntu/hermes-agent/gateway/run.py.backup.*
```

## Support

- Documentation: `/home/ubuntu/executive_agents_platform/INSTALLATION_GUIDE.md`
- Quick reference: `/home/ubuntu/executive_agents_platform/QUICK_INSTALL.txt`
- Design guide: `/home/ubuntu/executive_agents_platform/SOLID_DESIGN_GUIDE.md`
- Troubleshooting: See section above

## SOLID Design Principles

This skill implements the hook system following SOLID:

- **S**ingle Responsibility: Each hook handles one message type
- **O**pen/Closed: Extensible via hooks, closed for modification
- **L**iskov Substitution: Hooks implement abstract interface
- **I**nterface Segregation: Minimal hook interface
- **D**ependency Inversion: Depends on abstractions

Result: Zero platform adapter modifications, fully extensible architecture.

## Version

- Version: 1.0.0
- Updated: May 11, 2026
- Status: Production Ready
- License: MIT

## Related Skills

- `hermes-agent-skill-authoring`: Create new Hermes skills
- `whatsapp-livekit-agent-gateway`: WhatsApp voice agent integration
- `create-agent`: Create new AI agents
- `deployment-cicd`: Deploy to production

