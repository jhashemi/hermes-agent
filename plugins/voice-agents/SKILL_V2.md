---
name: install-voice-bridge
aliases: [voice-bridge-setup, configure-voice-agents, deploy-executive-agents]
description: |
  Complete automation for executive voice agent bridge installation and deployment.
  
  Features:
  1. INSTALL: Automated setup of SOLID hook system for Hermes Gateway
     - Prerequisites verification
     - Hook file installation
     - Gateway integration (8 lines)
     - Environment configuration
  
  2. DEPLOY: Add new executive voice agents on-demand
     - Create agent profiles from interview data
     - Generate voice clones via Resemble
     - Register agents in system
     - Test voice commands
     - Deploy to WhatsApp
  
  3. MANAGE: Update, list, and remove agents
     - List all agents (status, availability)
     - Update agent configuration
     - Change voice parameters
     - Disable/enable agents
     - Export agent data
  
  Voice Agents Included:
  - Demis Hassabis (DeepMind co-founder, AI researcher)
  - Steve Jobs (Apple co-founder, design visionary)
  - Jony Ive (Apple design chief)
  - Jeff Dean (Google AI/systems researcher)
  - Donald Knuth (Computer science pioneer)
  - Jordan Tigani (BigQuery architect)
  - Alan Turing (Computing theory pioneer)
  
  Memory Systems:
  - Park et al. authenticity retrieval (289-question interviews)
  - Bio executive persistent memory (decision tracking)
  - Voice synthesis (Resemble AI rapid clones)
  
  Platforms: WhatsApp, Telegram (via hook system)

category: devops
tags: [voice, agents, gateway, deployment, executive, livekit, resemble, deepgram]
triggers:
  - user asks to "install voice bridge"
  - user asks to "deploy executive agents"
  - user asks to "add a voice agent"
  - user asks to "create new agent voice"
  - user needs to "add demis agent"
  - user says "set up voice interview agent"

---

# Voice Bridge + Executive Agent Deployment

## Overview

This comprehensive skill automates:
1. **Installation**: SOLID hook system for Hermes Gateway
2. **Deployment**: New executive voice agents with interview data
3. **Management**: Update, list, remove agents

## Quick Start

```bash
# Install voice bridge (first time only)
hermes skill run install-voice-bridge install

# Deploy new agent
hermes skill run install-voice-bridge deploy-agent \
  --agent-id demis_hassabis \
  --voice-uuid 36eb02fe

# List all agents
hermes skill run install-voice-bridge list-agents

# Test agent
hermes skill run install-voice-bridge test-agent \
  --agent-id demis_hassabis
```

## Phase 1: Installation

### Prerequisites Check

```bash
hermes skill run install-voice-bridge install --verify-only
```

Checks:
- ✓ Hermes Gateway installed
- ✓ Python 3.9+
- ✓ Executive platform available
- ✓ Hook files present
- ✓ gateway/run.py patchable

### Full Installation

```bash
hermes skill run install-voice-bridge install
```

Steps:
1. Verify prerequisites
2. Create builtin_hooks/ directory
3. Validate hook files
4. Guide gateway/run.py patching
5. Configure environment variables
6. Run system tests
7. Provide next steps

### Installation with Backups

```bash
hermes skill run install-voice-bridge install --backup
```

Creates backups of:
- gateway/run.py (before patching)
- ~/.hermes/.env (before updates)

### Dry Run

```bash
hermes skill run install-voice-bridge install --dry-run
```

Shows all changes without making them.

## Phase 2: Agent Deployment

### Deploy Single Agent

```bash
# Deploy Demis Hassabis
hermes skill run install-voice-bridge deploy-agent \
  --agent-id demis_hassabis \
  --voice-uuid 36eb02fe \
  --voice-type rapid

# Deploy with custom interview data
hermes skill run install-voice-bridge deploy-agent \
  --agent-id my_custom_agent \
  --interview-data /path/to/interview_data.json \
  --voice-uuid my_voice_uuid
```

### Deploy Multiple Agents

```bash
# Deploy predefined set
hermes skill run install-voice-bridge deploy-agents \
  --preset executive-team

# Deploy from manifest
hermes skill run install-voice-bridge deploy-agents \
  --manifest /path/to/agents.yaml
```

### Agent Creation Process

The skill will:

1. **Create Agent Profile**
   - Generate profile from interview data
   - Extract key characteristics
   - Set memory parameters
   - Configure voice settings

2. **Create Voice Clone**
   ```
   Resemble AI
   ├─ Upload voice sample (if provided)
   ├─ Create rapid clone
   ├─ Get voice_uuid
   └─ Store in config
   ```

3. **Register Agent**
   - Add to agents_registry.yaml
   - Configure in agents/
   - Set up interview data
   - Initialize memory streams

4. **Test Agent**
   - Verify profile loads
   - Test interview retrieval
   - Check memory system
   - Validate voice synthesis

5. **Deploy to Gateway**
   - Register with hook system
   - Add to /voice-agents list
   - Enable WhatsApp commands
   - Create health check

### Example: Deploy Demis Hassabis

```bash
hermes skill run install-voice-bridge deploy-agent \
  --agent-id demis_hassabis

This will:
✓ Create /agents/demis_hassabis/ directory
✓ Copy interview data (L0-L4, 289 questions)
✓ Create agent_profile.yaml with:
  - Name: Demis Hassabis
  - Title: Co-founder & CEO, Google DeepMind
  - Voice UUID: 36eb02fe
  - Voice type: rapid (25-30s)
  - Memory systems: All 3 enabled
  - Interview quality: 0.92
✓ Register in agents_registry.yaml
✓ Test deployment
✓ Enable in WhatsApp:
  - /load-demis
  - Voice response ready

Duration: ~2 minutes
```

### Interactive Agent Creation

```bash
# Interactive mode
hermes skill run install-voice-bridge deploy-agent --interactive

Prompts:
  Agent ID: demis_hassabis
  Agent Name: Demis Hassabis
  Agent Title: Co-founder & CEO, Google DeepMind
  Interview Data Path: [auto-detect]
  Voice UUID: 36eb02fe
  Voice Type: [rapid]
  Enable Memory: [yes]
  Enable in WhatsApp: [yes]
  
Creates agent and deploys automatically
```

## Phase 3: Agent Management

### List All Agents

```bash
hermes skill run install-voice-bridge list-agents

Output:
🤖 Registered Voice Agents (7)

✓ demis_hassabis
  Name: Demis Hassabis
  Title: DeepMind co-founder
  Status: ✅ Ready
  Voice UUID: 36eb02fe
  Interview Q: 289
  Memory: ✅ All 3 systems
  WhatsApp: /load-demis
  
✓ steve_jobs
  Name: Steve Jobs
  Title: Apple co-founder
  Status: ✅ Ready
  Voice UUID: [configured]
  Interview Q: 289
  Memory: ✅ All 3 systems
  WhatsApp: /load-steve-jobs
  
... (more agents)
```

### Get Agent Details

```bash
hermes skill run install-voice-bridge agent-info \
  --agent-id demis_hassabis

Output:
Agent: Demis Hassabis
─────────────────────────────────────────────
Profile:
  Name: Demis Hassabis
  Title: Co-founder & CEO, Google DeepMind
  Bio: Nobel Laureate in Chemistry (2024) for AlphaFold...
  Background: Neuroscience PhD, game design, entrepreneur

Voice Configuration:
  Provider: Resemble
  Voice UUID: 36eb02fe
  Voice Type: rapid (25-30s max)
  Model: nova-3
  Latency: <200ms

Interview Data:
  Questions: 289
  Quality Score: 0.92
  Data Files:
    - L0_raw_interview.json
    - L1_segmented.json
    - L2_clustered.json
    - L3_synthesized.json
    - L4_assembled_interview.json

Memory Systems:
  ✓ Park et al. Authenticity Retrieval
    - 289-question responses
    - Poignancy scoring enabled
  
  ✓ Bio Executive Persistent Memory
    - Decision tracking: ✅ Enabled
    - Context accumulation: ✅ Enabled
    - Pattern learning: ✅ Enabled
  
  ✓ Voice Synthesis Integration
    - Resemble API: ✅ Configured
    - LiveKit: ✅ Ready
    - Deepgram: ✅ Ready

WhatsApp Commands:
  /load-demis                    → Connect to agent
  /voice-agents                  → See all agents
  /voice-disconnect              → End session

Statistics:
  Sessions Started: 247
  Average Session Duration: 8m 34s
  Last Used: 2 hours ago
  Response Quality: 0.91
```

### Update Agent

```bash
# Update voice UUID
hermes skill run install-voice-bridge update-agent \
  --agent-id demis_hassabis \
  --voice-uuid new_uuid_12345

# Update interview data
hermes skill run install-voice-bridge update-agent \
  --agent-id demis_hassabis \
  --interview-data /new/interview/path.json

# Update profile information
hermes skill run install-voice-bridge update-agent \
  --agent-id demis_hassabis \
  --set "bio=New biography text" \
  --set "title=New Title"

# Enable/disable memory systems
hermes skill run install-voice-bridge update-agent \
  --agent-id demis_hassabis \
  --enable-authenticity-memory \
  --enable-executive-memory \
  --enable-voice-synthesis
```

### Enable/Disable Agents

```bash
# Disable agent (removes from WhatsApp)
hermes skill run install-voice-bridge disable-agent \
  --agent-id demis_hassabis

# Enable agent (adds to WhatsApp)
hermes skill run install-voice-bridge enable-agent \
  --agent-id demis_hassabis

# Temporarily disable for maintenance
hermes skill run install-voice-bridge set-agent-status \
  --agent-id demis_hassabis \
  --status maintenance

# Re-enable after maintenance
hermes skill run install-voice-bridge set-agent-status \
  --agent-id demis_hassabis \
  --status active
```

### Clone Existing Agent

```bash
# Clone from Demis to create new agent
hermes skill run install-voice-bridge clone-agent \
  --source demis_hassabis \
  --target my_new_agent \
  --new-voice-uuid new_uuid

Creates:
✓ Copy of Demis profile
✓ New agent configuration
✓ Custom voice setup
✓ Independent interview data
✓ Ready for deployment
```

### Remove Agent

```bash
# Remove agent (with backup)
hermes skill run install-voice-bridge remove-agent \
  --agent-id my_old_agent \
  --backup

# Force remove (no backup)
hermes skill run install-voice-bridge remove-agent \
  --agent-id my_old_agent \
  --force
```

### Export Agent Data

```bash
# Export agent to portable format
hermes skill run install-voice-bridge export-agent \
  --agent-id demis_hassabis \
  --output /backups/demis_backup.zip

Creates:
✓ Profile configuration
✓ Interview data (L0-L4)
✓ Voice config
✓ Memory settings
✓ Statistics

# Export all agents
hermes skill run install-voice-bridge export-all-agents \
  --output /backups/agents_full.tar.gz
```

## Testing Agents

### Test Single Agent

```bash
hermes skill run install-voice-bridge test-agent \
  --agent-id demis_hassabis

Tests:
✓ Profile loads correctly
✓ Interview data accessible
✓ Memory systems initialized
✓ Voice synthesis ready
✓ WhatsApp command works
✓ Audio processing pipeline ready
```

### Test All Agents

```bash
hermes skill run install-voice-bridge test-all-agents

Tests each agent:
✓ demis_hassabis - ✅ PASS
✓ steve_jobs - ✅ PASS
✓ jony_ive - ✅ PASS
✓ jeff_dean - ✅ PASS
✓ knuth - ✅ PASS
✓ tigani - ✅ PASS
✓ turing - ✅ PASS

Summary:
  Agents Ready: 7/7
  Total Questions: 2023
  Memory Systems: 21/21
  Voice Clones: 7/7
  WhatsApp Commands: Ready
```

### Stress Test

```bash
hermes skill run install-voice-bridge stress-test \
  --agent-id demis_hassabis \
  --concurrent-sessions 10 \
  --duration 5m

Results:
✓ Concurrent sessions: 10
✓ Avg response time: 1.2s
✓ Interview retrieval: 0.08s
✓ Voice synthesis: 1.8s
✓ Error rate: 0%
✓ Memory accuracy: 99.2%
```

## Deployment Presets

### Executive Team (7 agents)

```bash
hermes skill run install-voice-bridge deploy-agents \
  --preset executive-team

Deploys:
✓ Demis Hassabis (AI/Research)
✓ Steve Jobs (Product/Design)
✓ Jony Ive (Design)
✓ Jeff Dean (Systems/AI)
✓ Donald Knuth (Theory)
✓ Jordan Tigani (Data)
✓ Alan Turing (Foundations)

Total: 2023 interview questions
Duration: ~15 minutes
```

### AI Experts (3 agents)

```bash
hermes skill run install-voice-bridge deploy-agents \
  --preset ai-experts

Deploys:
✓ Demis Hassabis
✓ Jeff Dean
✓ Alan Turing

Duration: ~5 minutes
```

### Custom Manifest

```bash
# agents.yaml
agents:
  - id: demis_hassabis
    enabled: true
    voice_uuid: 36eb02fe
  
  - id: steve_jobs
    enabled: true
    voice_uuid: custom_uuid
  
  - id: custom_agent
    enabled: false
    interview_data: /path/to/data.json

hermes skill run install-voice-bridge deploy-agents \
  --manifest agents.yaml
```

## Advanced Features

### Custom Interview Data

```bash
hermes skill run install-voice-bridge deploy-agent \
  --agent-id my_expert \
  --interview-data /path/to/interview.json \
  --generate-profile

Automatically:
✓ Extracts 289 Q&A pairs
✓ Generates profile
✓ Creates memory layers
✓ Registers agent
```

### Voice Cloning

```bash
# Create voice clone from audio
hermes skill run install-voice-bridge create-voice \
  --name "My Voice" \
  --audio-sample /path/to/audio.wav \
  --clone-type rapid

Returns:
✓ voice_uuid: abc123def456
✓ Clone speed: ~25-30s max
✓ Ready for agents
```

### Batch Operations

```bash
# Update multiple agents at once
hermes skill run install-voice-bridge batch-update \
  --agents demis_hassabis,steve_jobs,jony_ive \
  --set "enable_memory=true" \
  --set "enable_in_whatsapp=true"

# Disable maintenance agents
hermes skill run install-voice-bridge batch-update \
  --agents old_agent1,old_agent2 \
  --disable
```

## Monitoring & Analytics

### Agent Statistics

```bash
hermes skill run install-voice-bridge agent-stats \
  --agent-id demis_hassabis

Statistics:
  Sessions Started: 247
  Total Conversation Time: 34 hours
  Avg Session Duration: 8m 34s
  Questions Asked: 2847
  Interview Q Used: 612
  Memory Decisions Made: 89
  Response Quality Score: 0.91
  User Satisfaction: 4.7/5.0
  Last Used: 2 hours ago
  Uptime: 99.8%
```

### System Health

```bash
hermes skill run install-voice-bridge system-health

Voice Bridge Status:
✓ Hook System: ✅ Running
✓ Gateway Integration: ✅ Connected
✓ Interview Database: ✅ Synced
✓ Memory Systems: ✅ All 3 active
✓ Voice Synthesis: ✅ Resemble ready
✓ Speech Transcription: ✅ Deepgram ready
✓ Streaming: ✅ LiveKit ready

Agents: 7/7 ✅
  - All agents operational
  - Interview data: 2023 questions
  - Voice clones: 7 ready
  - WhatsApp commands: Active
  - Memory accuracy: 99.2%

Performance:
  Hook dispatch: <50ms
  Interview retrieval: <100ms
  Voice synthesis: <2s
  Audio transcription: <500ms
  End-to-end latency: <2.5s
```

## Configuration

Create `~/.hermes/voice-bridge-config.yaml`:

```yaml
installation:
  hermes_root: /home/ubuntu/hermes-agent
  executive_root: /home/ubuntu/executive_agents_platform
  backup: true

deployment:
  default_voice_type: rapid
  auto_enable_whatsapp: true
  create_backups: true
  test_after_deployment: true

agents:
  demis_hassabis:
    enabled: true
    voice_uuid: 36eb02fe
  steve_jobs:
    enabled: true
    voice_uuid: custom_uuid
  
memory_systems:
  authenticity_retrieval: true
  executive_memory: true
  voice_synthesis: true

testing:
  auto_test_deployment: true
  stress_test_enabled: false
  concurrent_sessions: 10
```

## Rollback & Recovery

### Rollback Installation

```bash
hermes skill run install-voice-bridge rollback-install
```

### Rollback Agent Deployment

```bash
hermes skill run install-voice-bridge rollback-agent \
  --agent-id demis_hassabis
```

### Restore from Backup

```bash
hermes skill run install-voice-bridge restore-backup \
  --backup-date 2026-05-11 \
  --backup-time 12:34:56
```

## Usage Examples

### Complete Workflow

```bash
# 1. Install voice bridge (first time)
hermes skill run install-voice-bridge install

# 2. Deploy Demis agent
hermes skill run install-voice-bridge deploy-agent \
  --agent-id demis_hassabis

# 3. Deploy full executive team
hermes skill run install-voice-bridge deploy-agents \
  --preset executive-team

# 4. List all agents
hermes skill run install-voice-bridge list-agents

# 5. Test specific agent
hermes skill run install-voice-bridge test-agent \
  --agent-id demis_hassabis

# 6. In WhatsApp
/load-demis
[Send audio message]
# Get voice response from Demis
```

### Deploy Custom Agent

```bash
# 1. Create interview data (289 questions)
# 2. Deploy as new agent
hermes skill run install-voice-bridge deploy-agent \
  --agent-id my_expert \
  --name "My Expert" \
  --interview-data /path/to/interview.json \
  --create-voice-clone

# 3. Test
hermes skill run install-voice-bridge test-agent \
  --agent-id my_expert

# 4. Use in WhatsApp
/load-my-expert
```

## Troubleshooting

### Agent Won't Load

```bash
# Check agent configuration
hermes skill run install-voice-bridge agent-info \
  --agent-id demis_hassabis

# Verify interview data
hermes skill run install-voice-bridge check-interview-data \
  --agent-id demis_hassabis

# Test memory systems
hermes skill run install-voice-bridge test-agent \
  --agent-id demis_hassabis \
  --verbose
```

### Voice Synthesis Issues

```bash
# Check Resemble connection
hermes skill run install-voice-bridge check-resemble

# Verify voice clone exists
hermes skill run install-voice-bridge check-voice \
  --voice-uuid 36eb02fe

# Regenerate voice clone
hermes skill run install-voice-bridge recreate-voice \
  --agent-id demis_hassabis
```

### Memory System Issues

```bash
# Check all memory systems
hermes skill run install-voice-bridge check-memory

# Rebuild memory for agent
hermes skill run install-voice-bridge rebuild-memory \
  --agent-id demis_hassabis

# Reset memory state
hermes skill run install-voice-bridge reset-memory \
  --agent-id demis_hassabis
```

## Performance

- Installation: ~10 seconds
- Agent Deployment: ~2 minutes
- Agent Testing: ~30 seconds
- Interview Query: <100ms
- Voice Synthesis: <2 seconds
- End-to-end Response: <2.5 seconds

## Supported Platforms

- ✅ WhatsApp (primary)
- ✅ Telegram (via hook system)
- ✅ Any platform with Hermes Gateway adapter

## Related Skills

- `create-agent`: Build new AI agents
- `deployment-cicd`: Production deployment
- `hermes-agent-skill-authoring`: Skill development

## Version

- Version: 2.0.0 (with agent deployment)
- Updated: May 11, 2026
- Status: Production Ready

