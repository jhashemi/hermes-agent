# Installation Guide - Voice Bridge Hook System

**Complete Step-by-Step Installation for Hermes Gateway**

---

## Quick Install (5 Minutes)

```bash
# 1. Run installer (check what needs to be done)
cd /home/ubuntu/executive_agents_platform
python3 install_voice_bridge.py --dry-run

# 2. Manual Step 1: Edit gateway/run.py (8 lines)
# 3. Manual Step 2: Set environment variables (4 variables)
# 4. Restart gateway
# 5. Test
```

---

## Detailed Installation

### Prerequisites Check

```bash
# Verify all files exist
ls -lh /home/ubuntu/hermes-agent/gateway/
# Should have: run.py, platforms/, builtin_hooks/

ls -lh /home/ubuntu/hermes-agent/gateway/builtin_hooks/
# Should have: voice_agent_hook.py, __init__.py
```

### Step 1: Create builtin_hooks Directory

```bash
mkdir -p /home/ubuntu/hermes-agent/gateway/builtin_hooks
```

### Step 2: Verify Hook Files Exist

```bash
ls -lh /home/ubuntu/hermes-agent/gateway/builtin_hooks/voice_agent_hook.py
ls -lh /home/ubuntu/hermes-agent/gateway/builtin_hooks/__init__.py
```

Both should exist (424 lines + 62 lines = 486 lines total).

### Step 3: Edit gateway/run.py (CRITICAL)

Edit `/home/ubuntu/hermes-agent/gateway/run.py`

**Add Location 1: At startup (3 lines)**

Find the `main()` function or startup code. Add after imports:

```python
# NEW: Initialize voice agent hooks
from gateway.builtin_hooks import initialize_builtin_hooks
await initialize_builtin_hooks()
```

**Add Location 2: In _handle_message() (5 lines)**

Find the `_handle_message()` method in GatewayRunner class. Add at the START of the method:

```python
async def _handle_message(self, event: MessageEvent) -> None:
    """Handle incoming message via hooks + platform adapters"""
    
    # NEW: Try hooks first (voice agent, etc.)
    from gateway.builtin_hooks.voice_agent_hook import get_hook_manager
    manager = get_hook_manager()
    hook_result = await manager.before_message_processing(event, self)
    if hook_result is not None:
        await self.send_message(event.user_id, hook_result, reply_to=event)
        return
    
    # EXISTING: Rest of _handle_message continues unchanged
    # ... original code ...
```

### Step 4: Set Environment Variables

Edit `~/.hermes/.env`:

```bash
# Create file if it doesn't exist
mkdir -p ~/.hermes
touch ~/.hermes/.env

# Add these lines:
export RESEMBLE_API_KEY="your_resemble_api_key"
export DEEPGRAM_API_KEY="your_deepgram_api_key"
export LIVEKIT_API_URL="https://executiveagents-l0dbzn9l.livekit.cloud"
export LIVEKIT_WS_URL="wss://executiveagents-l0dbzn9l.livekit.cloud"
export LIVEKIT_API_KEY="your_livekit_api_key"
export LIVEKIT_API_SECRET="your_livekit_api_secret"
```

Or via command line:

```bash
echo 'export RESEMBLE_API_KEY="your_key"' >> ~/.hermes/.env
echo 'export DEEPGRAM_API_KEY="your_key"' >> ~/.hermes/.env
echo 'export LIVEKIT_API_KEY="your_key"' >> ~/.hermes/.env
echo 'export LIVEKIT_API_SECRET="your_secret"' >> ~/.hermes/.env
```

### Step 5: Verify Changes

```bash
# Check gateway/run.py has hook initialization
grep -n "initialize_builtin_hooks" /home/ubuntu/hermes-agent/gateway/run.py

# Check gateway/run.py has hook manager call
grep -n "get_hook_manager" /home/ubuntu/hermes-agent/gateway/run.py

# Check environment variables
cat ~/.hermes/.env | grep RESEMBLE
```

### Step 6: Restart Gateway

```bash
# Stop current gateway
hermes gateway stop

# Start new gateway
hermes gateway restart

# Or use systemctl if running as service
systemctl --user restart hermes-gateway.service
```

### Step 7: Verify Installation

```bash
# Check logs for hook initialization
hermes logs --follow --gateway | head -50

# Should see:
# [hooks] Initializing builtin gateway hooks...
# [voice-hook] Voice bridge initialized
# [hooks] Initialization complete
```

### Step 8: Test

Send commands via WhatsApp:

```
/load-demis              # Should respond: ✅ Loaded demis_hassabis
/voice-agents           # Should list available agents
/voice-disconnect       # Should disconnect

[Send audio message]    # Should transcribe + respond
```

---

## Automated Installer Script

For faster installation, use the provided script:

```bash
cd /home/ubuntu/executive_agents_platform

# Dry run (see what would happen)
python3 install_voice_bridge.py --dry-run

# With backup (creates backup of run.py before modifications)
python3 install_voice_bridge.py --backup

# Full installation
python3 install_voice_bridge.py
```

The script will:
1. Verify prerequisites
2. Check directory structure
3. Verify files
4. Backup gateway/run.py
5. Check required modifications
6. Verify environment variables
7. Generate summary

---

## Troubleshooting Installation

### Hook Files Not Found

```bash
# Verify files exist
ls -lh /home/ubuntu/hermes-agent/gateway/builtin_hooks/voice_agent_hook.py

# If missing, copy from executive_agents_platform
cp /home/ubuntu/executive_agents_platform/SOLID_DESIGN_GUIDE.md \
   /home/ubuntu/hermes-agent/gateway/builtin_hooks/voice_agent_hook.py
```

### gateway/run.py Not Modified Correctly

```bash
# Check modifications were added
grep -A5 "initialize_builtin_hooks" /home/ubuntu/hermes-agent/gateway/run.py

# Check hook manager call
grep -A5 "get_hook_manager" /home/ubuntu/hermes-agent/gateway/run.py

# If missing, edit manually (see Step 3 above)
```

### Environment Variables Not Set

```bash
# Verify .env file exists
ls -lh ~/.hermes/.env

# Check contents
cat ~/.hermes/.env

# If missing, add manually (see Step 4 above)

# Reload environment
source ~/.hermes/.env
```

### Gateway Won't Start

```bash
# Check logs
hermes logs --level error

# Check Python syntax
python3 -m py_compile /home/ubuntu/hermes-agent/gateway/run.py

# Check hook files syntax
python3 -m py_compile /home/ubuntu/hermes-agent/gateway/builtin_hooks/voice_agent_hook.py
```

### Voice Commands Not Working

```bash
# Check hooks initialized
hermes logs --follow --gateway | grep "voice-hook"

# Check for errors
hermes logs --follow --gateway | grep "ERROR\|error"

# Check WhatsApp adapter receiving messages
hermes logs --follow --gateway | grep "message"
```

---

## Installation Files

### Main Files

```
/home/ubuntu/hermes-agent/gateway/builtin_hooks/
├── voice_agent_hook.py     (424 lines) - Hook implementation
└── __init__.py             (62 lines)  - Hook initialization
```

### Modified Files

```
/home/ubuntu/hermes-agent/gateway/
└── run.py                  (+8 lines)  - Hook integration points
```

### Unchanged Files

```
/home/ubuntu/hermes-agent/gateway/platforms/
├── whatsapp.py             (unchanged)
├── telegram.py             (unchanged)
└── ... other adapters      (unchanged)
```

### Configuration

```
~/.hermes/.env             (4 environment variables added)
```

---

## Verification Checklist

- [ ] Hook files exist in `/home/ubuntu/hermes-agent/gateway/builtin_hooks/`
- [ ] `gateway/run.py` has 3 lines at startup
- [ ] `gateway/run.py` has 5 lines in `_handle_message()`
- [ ] `~/.hermes/.env` has 4 environment variables
- [ ] Gateway restarts without errors
- [ ] Logs show `[voice-hook]` messages
- [ ] `/load-demis` command works in WhatsApp
- [ ] Audio messages are processed
- [ ] Voice responses are generated

---

## Rollback (If Needed)

If installation causes issues, rollback:

```bash
# Restore backup (if created)
cp /home/ubuntu/hermes-agent/gateway/run.py.backup.TIMESTAMP \
   /home/ubuntu/hermes-agent/gateway/run.py

# Or manually remove the 8 lines added

# Remove hook directory (optional)
rm -rf /home/ubuntu/hermes-agent/gateway/builtin_hooks

# Restart gateway
hermes gateway restart
```

---

## Support & Debugging

### Enable Debug Logging

```bash
# Set debug level
export HERMES_LOG_LEVEL=DEBUG

# Restart gateway
hermes gateway restart

# View logs
hermes logs --follow --gateway --level debug
```

### Test Individual Components

```python
# Test hook initialization
python3 -c "
from gateway.builtin_hooks import initialize_builtin_hooks
print('✓ Hook module imports successfully')
"

# Test voice bridge
python3 -c "
from gateway.builtin_hooks.voice_agent_hook import _get_voice_bridge
bridge = _get_voice_bridge()
agents = bridge.loader.list_agents()
print(f'✓ Voice bridge ready, agents: {len(agents)}')
"
```

### Check Integration Points

```bash
# Verify hook manager is callable
python3 << 'EOF'
import asyncio
from gateway.builtin_hooks.voice_agent_hook import get_hook_manager

manager = get_hook_manager()
print(f"✓ Hook manager: {manager}")
print(f"✓ Hooks registered: {len(manager._hooks)}")
EOF
```

---

## Performance

Typical installation time:
- Prerequisites check: <1 second
- File verification: <1 second
- Manual edits (gateway/run.py): 2-3 minutes
- Environment variables: <1 minute
- Gateway restart: 5-10 seconds
- First test: <5 seconds

**Total: ~5-10 minutes**

---

## Next Steps After Installation

1. **Test all commands**
   ```
   /load-demis
   /voice-agents
   /voice-disconnect
   ```

2. **Test audio messages**
   - Send 5-30 second audio message
   - Verify transcription and response

3. **Monitor logs**
   ```bash
   hermes logs --follow --gateway | grep voice
   ```

4. **Clone remaining voices**
   - Steve Jobs, Jony Ive, Jeff Dean, etc.
   - Update voice UUIDs in agent configs

5. **Scale to production**
   - Deploy to both instances
   - Monitor performance
   - Set up alerts

---

## Installation Complete ✅

Voice bridge hook system is now integrated into Hermes Gateway!

- ✅ Zero platform adapter modifications
- ✅ 8 minimal lines added to gateway/run.py
- ✅ Fully extensible hook system
- ✅ SOLID design principles applied
- ✅ Production ready

Next: Test in WhatsApp with `/load-demis`
