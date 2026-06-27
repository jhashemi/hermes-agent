# Executive Voice Agents Platform

Hermes Gateway plugin for executive voice agents with 8 memory systems.

## Quick Install

```bash
# Local
python3 install_plugin.py

# Remote
python3 install_plugin.py --remote ubuntu@ip-172-31-30-216
```

## WhatsApp Commands

```
/load-demis           Connect to Demis Hassabis
/load-steve-jobs      Connect to Steve Jobs
/voice-agents         List available agents
/voice-info demis     Get agent details
/voice-disconnect     End session
```

## Architecture

```
WhatsApp Message
    ↓
Hermes Gateway (_handle_message)
    ↓
invoke_hook("pre_gateway_dispatch")       ← Native plugin system
    ↓
voice_agents_plugin.pre_gateway_dispatch_hook()
    ├─ /load-{agent}?  → Create session, rewrite with agent context
    ├─ /voice-agents?  → Return agent list (rewrite)
    ├─ /voice-disconnect? → End session (rewrite)
    ├─ /voice-info {id}? → Return agent info (rewrite)
    ├─ Active session? → Rewrite with agent context
    └─ Normal message? → Return None (pass through)
    ↓
Gateway continues to agent (unchanged)
```

## 8 Memory Systems

| # | System | Purpose |
|---|--------|---------|
| 1 | Authenticity Retrieval | 289-question interview (Park et al.) |
| 2 | Bio Executive Persistent | Decision tracking + context |
| 3 | Voice Synthesis | Resemble + Deepgram + LiveKit |
| 4 | Semantic Memory | Embedding-based concepts |
| 5 | Episodic Memory | Session context + emotional valence |
| 6 | Procedural Memory | Learned workflows |
| 7 | Temporal Memory | Exponential decay, time-aware |
| 8 | Hierarchical Temporal | Multi-scale pattern detection |

## Available Agents

| Agent | Questions | Voice |
|-------|-----------|-------|
| Demis Hassabis | 289 (0.92 quality) | Resemble UUID: 36eb02fe |
| Steve Jobs | 289 (0.90 quality) | Configured |
| Donald Knuth | Stub | — |
| Jeff Dean | Stub | — |
| Jony Ive | Stub | — |
| Jordan Tigani | Stub | — |

## Files

```
executive_agents_platform/
├── pyproject.toml              # Packaging config
├── plugin.yaml                 # Hermes plugin manifest
├── voice_agents_plugin.py      # Native pre_gateway_dispatch hook
├── install_plugin.py           # Install/uninstall script
├── README.md                   # This file
├── loader/
│   ├── __init__.py
│   ├── interview_loader.py
│   ├── authenticity_retrieval.py
│   ├── bio_executive_memory.py
│   ├── voice_integration.py
│   ├── complete_memory_systems.py
│   ├── enhanced_integrated_agent_loader.py
│   ├── integrated_agent_loader.py
│   ├── agent_loader.py
│   └── whatsapp_voice_bridge.py
├── agents/
│   ├── agents_registry.yaml
│   ├── demis_hassabis/
│   │   ├── agent_profile.yaml
│   │   ├── voice_config.yaml
│   │   └── interview_data/     (19 JSON files)
│   └── steve_jobs/
│       ├── agent_profile.yaml
│       └── interview_data/
│           └── L4_assembled_interview_complete.json
└── tests/
```

## Integration Method

This plugin uses the **native Hermes plugin system** (`invoke_hook("pre_gateway_dispatch")`),
NOT a custom `GatewayHookManager`. This means:

- ✅ No modifications to `gateway/run.py`
- ✅ No custom hook manager class
- ✅ Works across gateway upgrades
- ✅ Plugin loads via `plugin.yaml` manifest
- ✅ Hook registered via `plugin_interface.register_hook()`

### Why This Matters

The previous approach (custom `GatewayHookManager` + `get_hook_manager()` call in `run.py`)
bypassed the native plugin system. Commands would silently fail if:
- The gateway was upgraded (patch to run.py would be lost)
- The hook import chain broke (already happened once with `ModuleNotFoundError`)
- The custom `__init__.py` export was wrong (already happened with `ImportError`)

The native plugin system is the correct, maintainable approach.

## Uninstall

```bash
python3 install_plugin.py --uninstall
python3 install_plugin.py --remote ubuntu@ip-172-31-30-216 --uninstall
```