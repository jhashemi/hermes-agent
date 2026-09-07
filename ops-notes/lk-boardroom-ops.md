# LK Boardroom ops — consolidated (2026-09-01, from dnalambda panel run)

## CRITICAL: LiveKit Cloud room lifecycle (agent-only rooms)
- LK Cloud closes a room when the last NON-AGENT participant leaves. Agent-kind participants do NOT keep rooms alive.
- Implicitly-created rooms get empty_timeout=300s → agent-only boardrooms die at ~5min: every agent session ends simultaneously with disconnect_reason=10 (ROOM_CLOSED).
- FIX (shipped): harness creates room explicitly: CreateRoomRequest(name, empty_timeout=3600, departure_timeout=3600, max_participants=20). Verified dnalambda7-060649 ran full 1193.7s with zero session_ended.

## Harness invocation (tests/harness_text_boardroom.py in executive_agents_platform)
- Interpreter: /home/ubuntu/hermes-agent/venv/bin/python (has livekit; platform/.venv does NOT)
- PYTHONPATH=/home/ubuntu/voice_twins/src:/home/ubuntu/executive_agents_platform/src:/home/ubuntu/executive_agents_framework/src:/home/ubuntu/shared_voice_memory
- Agent-only mode: --agent-only (injector joins kind=agent, NOT hidden — hidden participants cause DROP=participant_not_found on lk.chat delivery)
- Preflight: room.remote_participants shows generic agent-AJ_* identities; rely on persona-tagged voice.session_started NATS events (override) + grace window (~20s) for dispatch-stagger tail
- Engine can drop mid-run ("engine is closed") → harness now reconnects once and retries send
- Commit chain: 616ee3c (scenario+agent-only), 725ab2d (grace window), 94b3d1d (reconnect), 3d4d222 (CreateRoom timeouts)

## Workers
- zeus (jeff_dean): unit from ~/systemd-user-config/hermes2/, needed stale placement drop-in (dead :8194 gate) REMOVED
- friston (karl_friston): NEW unit port 8098 (cloned from zeus, sed persona/port), persisted to systemd-user-config
- Rule: 1 job per worker process; SDK kills job process after room close ("process did not exit in time" noise is post-close cleanup)

## Speaker attribution
- transcripts in data/livekit_sessions/<room>_text_harness.json (participant=agent-<persona>)
- Cross-check: sqlite memory_store.db session_summaries.summary_metadata.disconnect_reason (numeric LK DisconnectReason; 10=ROOM_CLOSED)
- /tmp/rca_debug.log has per-persona text_input_cb / router decision lines (boardroom graph: LlmScoredTarget + recency/anti-domination)
