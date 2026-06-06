# ADR-013: LiveKit Room Rendezvous on top of ADR-008 Voice Bridge

**Status:** Proposed
**Date:** 2026-06-06
**Supersedes:** None
**Depends on:** ADR-008 (Voice<->Gateway Bridge), ADR-009 (Skill File Install)

## Context

ADR-008 wired Deepgram STT + Resemble TTS through an HTTP bridge service
(`localhost:8193`) and JetStream subjects `voice_bridge.{context,gateway_out,
voice_out,mode}.<room>` so a Hermes gateway turn can speak through a phone
voice call (Twilio) or a WhatsApp voice note. The bridge is text-on-the-wire:
audio is transcribed at the edge (Twilio media stream → Deepgram → text →
JetStream) and synthesized at the edge (JetStream → Resemble → mp3 → Twilio).

The user wants a multi-modal session: one logical chat where text from
Telegram and voice from a real-time room (LiveKit) co-exist, share gateway
memory, share tool-call state, and route to the same Hermes agent.

## Decision

Sit a sidecar process inside a LiveKit room as a participant. The sidecar
plays the same role at the LiveKit edge that the Twilio media handler plays
at the phone edge: audio in → transcript on `voice_bridge.voice_out.<room>`,
turns from `voice_bridge.gateway_out.<room>` → audio out.

`<room>` is derived deterministically from `chat_id` (sha256(chat_id)[:16])
so Telegram and LiveKit can both bind to the same room from independent
entry points without coordination. A new `/voice join` slash command in
Telegram spawns the sidecar; `/voice leave` kills it.

## Consequences

+ One Hermes agent, one memory, three transports.
+ Reuses Deepgram + Resemble credentials and bridge HTTP API — no new
  STT/TTS pipeline.
+ LiveKit Cloud free tier (5000 participant-minutes/month) is plenty for
  prototype.
- Sidecar is a new process per active room. ~30MB RSS each. Acceptable
  for ≤ 10 concurrent rooms.
- LiveKit Cloud egress depends on user's network. p95 latency target
  600ms voice-in → text-out (gateway), 800ms text-in → voice-out (sidecar).
- Self-hosting deferred. If LiveKit Cloud goes down or pricing changes,
  the sidecar process is portable to self-hosted livekit-server.

## Alternatives considered

| Option | Why not |
|---|---|
| Twilio Voice + WebRTC | Already used by ADR-008 for phone audio. Twilio's WebRTC SDK is browser-only; sidecar would need a headless browser. LiveKit's server-side participant SDK is purpose-built for this. |
| Daily.co | Comparable to LiveKit. LiveKit's Python SDK is more mature and the participant model fits the "Hermes joins as a peer" mental model better. |
| Raw aiortc + custom signaling | ~3000 LOC of WebRTC plumbing. LiveKit Cloud handles SFU, TURN, signaling — we just publish/subscribe tracks. |
| OpenAI Realtime API in the room | Lowest latency (single round-trip) but bypasses Hermes tool-calling and memory. Defer to a later ADR. |

## Subjects (extends ADR-008)

No new subjects. Reuses:
- `voice_bridge.context.<room>` (set at session start by /voice join)
- `voice_bridge.gateway_out.<room>` (gateway → sidecar → speaker)
- `voice_bridge.voice_out.<room>` (mic → sidecar → STT → gateway)
- `voice_bridge.mode.<room>` (FSM audit)

## Bidirectional flow

```
Telegram message  →  gateway  →  agent  →  reply
                         │                    │
                         ↓                    ↓
                voice_bridge.context     voice_bridge.gateway_out.<room>
                                                  ↓
                                           livekit_room_agent (sidecar)
                                                  ↓
                                          Resemble TTS → audio frames → LiveKit room
                                                                              ↓
                                                                          user hears
user speaks → LiveKit room → audio frames → livekit_room_agent → Deepgram STT
                                                                       ↓
                                                          voice_bridge.voice_out.<room>
                                                                       ↓
                                                            existing ADR-008 router
                                                                       ↓
                                                          gateway dispatches as if user texted
```

## Open questions

- Do we want voice-only mode (suppress text echo to Telegram while in room)?
  Defer; user can toggle via existing `/voice` settings.
- How to handle tool-call output that's too long to speak? Truncate at 500
  chars + "see Telegram for the full output". Defer to a UX iteration.
