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

## Follow-ups discovered during Task 5 smoke test (2026-06-07)

The room-agent → ADR-008 voice bridge contract drifted between the two
implementations. Smoke test validated everything except this round-trip;
the bridge HTTP layer answers but with a different schema than
`tools/livekit_room_agent.py` expects:

| Endpoint | room_agent sends | voice_bridge_service expects |
|---|---|---|
| `POST /transcribe` | raw PCM body, `Content-Type: audio/wav` | JSON `{audio_url: str}` |
| `POST /synthesize` request | JSON `{text, voice, room}` | JSON `{text, voice_uuid, agent_id, project_uuid?}` |
| `POST /synthesize` response | raw mp3 body | JSON `{audio_path, format, voice_uuid}` |

Resolution options (pick one):

1. **Adapter shim in room_agent**: upload PCM to a short-lived URL (S3 presign
   or local /tmp HTTP), call `/transcribe` with that URL; map our
   `voice` → ADR-008 `voice_uuid` via a small lookup table; fetch
   `audio_path` from a sidecar file server and stream as audio frames.
2. **Add raw-bytes overload to voice_bridge_service**: accept
   `Content-Type: audio/wav` directly in `/transcribe` and return raw mp3
   from `/synthesize` when `Accept: audio/mpeg`. Lower-friction for
   downstream callers; small Python addition in
   `voice_twins/src/voice_bridge_service.py`.
3. **Build the LiveKit-native bridge** (recommended long-term): replace the
   HTTP round-trip with direct Deepgram + Resemble plugin participants in
   the LiveKit room (LiveKit agents pattern). Removes the bridge from the
   audio path entirely.

Smoke test validated 1–7 of 8 checks against the live LiveKit Cloud
project; see `scripts/livekit/smoke_test.py`.

### Resolution (2026-06-07): Option 2 + streaming TTS

Adopted **Option 2** with a refinement: instead of overloading
`/synthesize` for raw bytes, we point the room agent at the platform's
existing `/stream_synthesize` endpoint (Resemble WebSocket adapter at
`wss://websocket.cluster.resemble.ai/stream`), so audio reaches the
LiveKit room as Resemble emits it rather than after the full clip is
rendered.

Commits:

- `hermes-agent` `731e99a39` — `feat(livekit): Option 2 — stream-synthesize via Resemble WS over /stream_synthesize`
  - `transcribe_via_bridge`: sends `Content-Type: audio/x-pcm` with
    `X-Sample-Rate` / `X-Channels` hints (matches LiveKit AudioStream
    output: 16-bit mono PCM @16kHz).
  - `stream_synthesize_via_bridge`: new async generator hitting
    `/stream_synthesize`, yields PCM chunks as they arrive.
  - `RoomAgent._on_gateway_out` now publishes per-Resemble-chunk PCM
    frames (TTFB ~200-400ms vs ~Ns for buffered `/synthesize`).
  - Audio frame topic renamed `audio/mp3` → `audio/pcm22050`.
  - STT response handler accepts both `{transcript}` (hexagonal) and
    `{text}` (legacy monolith).
  - 7/7 livekit_room_agent tests + 27/27 LiveKit-related tests green.
- `executive_agents_platform` `85bba26` — `feat(voice-bridge): /transcribe raw-bytes overload + Resemble WS protocol fix`
  - `TranscribeHandler` accepts non-JSON content types as raw bytes; PCM
    is wrapped in a minimal RIFF/WAVE header before being handed to
    `STTPort.transcribe(audio_path)`.
  - `ResembleStreamingAdapter` now sends required `project_uuid` and
    configurable `model` (defaults `chatterbox-turbo`) in the WS
    payload, per `docs.resemble.ai/docs/streaming/websocket`.

Verified live: `/transcribe` (raw PCM) and `/stream_synthesize` HTTP
plumbing both round-trip end-to-end (HTTP 200, `audio/pcm` chunked
response). Resemble WS server itself rejects every model
(`ultra`/`chatterbox`/`chatterbox-turbo`) for the current account
voices with `DBCacheError: <model> is not available for this voice` —
that is an account-tier issue (streaming requires Business plan per
docs) and is **out of scope** for the contract resolution.

#### Outstanding before audio fully round-trips

- Resemble account: upgrade to a tier that exposes
  `chatterbox-turbo` / streaming on the existing voice clones, OR
  re-clone the voices on a tier that does. Voice metadata claims
  `streaming: true` but synthesis backend rejects the request.
- Voice config: `agents/<id>/voice_config.yaml` files store stale
  `voice_uuid` prefixes; canonical UUIDs in the live account are
  `0858e915` (Steve Jobs) and `95184f6f` (Demis Hassabis). Refresh
  these once the account-tier issue is resolved.
- Option 3 (LiveKit-native plugins) remains the recommended long-term
  direction; defer until the HTTP path is validated end-to-end.

---

### Resolution v3 — Option 4: HTTP-streaming pivot (2026-06-07)

**Recon overturned the v2 RCA.** Before re-cloning voices on a different
tier (Option 2), exhaustive probing revealed two distinct root causes:

1. **The `project_uuid: 9dbceb60` hardcoded in 6+ files was bogus.**
   It is *not* a project on `jj@syntaxdata.com`. Resemble REST returns
   `400 User does not have access to this project`. The actual projects
   on this account are:
   - `60c8690f` — "executive agents framework" (the right one)
   - `31dd950f` — "Default"
   - `a6269985` "Tax", `40dc1fb8`/`4258692e` "DataConnectix"

2. **WebSocket streaming is account-tier gated, not voice-tier gated.**
   Every voice on the account — custom clones AND Resemble's own stock
   library voices (Luma, Nova, Atlas, Titan…) — returns
   `DBCacheError: <model> is not available for this voice` for every
   model name (`chatterbox-turbo`, `chatterbox`, `ultra`, `turbo`,
   `rapid_speech`, `sk2turbo`). 26 model-name permutations across both
   voice variants on both projects: 0/26 produced audio. Re-cloning
   would not have changed this.

**The HTTP-chunked streaming endpoint works on this tier.** Probed
`https://f.cluster.resemble.ai/stream` with the correct
`project_uuid=60c8690f`:

| Voice           | TTFB  | Total | Bytes  | Format                  |
| --------------- | ----- | ----- | ------ | ----------------------- |
| Steve Jobs      | 76 ms | 2.4 s | 269 KB | PCM 16-bit mono 22050Hz |
| Demis Hassabis  | 88 ms | 2.0 s | 244 KB | PCM 16-bit mono 22050Hz |

48kHz and 44.1kHz also work (WAV, parseable RIFF header). Server
rejects `output_format=pcm` (HTTP 500) — wav-only on this tier; we
strip the 44-byte RIFF header client-side when raw PCM is needed.

#### Implementation

`ResembleStreamingAdapter` rewritten to use HTTP chunked transfer:
  - POST to `f.cluster.resemble.ai/stream` with `Authorization: Token`
  - Streams `Content-Type: audio/wav` chunked response as it arrives
  - `raw_pcm=True` (default) strips RIFF/WAVE header → pure PCM frames
    ready for LiveKit `audio_pcm22050` topic
  - `raw_pcm=False` passes the WAV header through unchanged for
    consumers that want a parseable file
  - Quality knobs preserved per call: `sample_rate` (22050/44100/48000),
    `precision` (PCM_16/PCM_24/PCM_32), `output_format`
  - 4xx → `_NonTransientError` (no retry); 5xx → transient (retry with
    exponential backoff, capped at `max_retries`)
  - Public class name unchanged — `composition.create_streaming_tts_adapter`
    needs no edits
  - WS implementation preserved as `ResembleWSStreamingAdapter` (yields
    nothing, logs a deprecation warning); swap one line in `composition`
    when the account is upgraded

**Fallback chain (preserved + extended for Option 4):**
  1. Streaming HTTP `/stream` (low TTFB, primary)
  2. REST sync `/synthesize` (full file, MP3, fallback 1) — already
     wired in `StreamSynthesizeHandler` via `ResembleTTSAdapter` →
     `ResembleTTSClient`
  3. Async clip API `/projects/{p}/clips` (highest quality, fallback 2)
     — same client, second tier of `ResembleTTSClient.synthesize()`

Both fallbacks were preserved per user direction: keep non-async
fallback AND higher-quality option.

#### Files touched

- `executive_agents_platform/src/infrastructure/adapters/resemble_streaming.py`
  — full rewrite to HTTP-chunked client
- `executive_agents_platform/src/voice_bridge_service.py` — 6× `9dbceb60` → `60c8690f`
- `executive_agents_platform/agents/{steve_jobs,demis_hassabis}/voice_config.yaml`
  — `project_uuid` corrected
- `executive_agents_platform/poll_voice_build.py` — `9dbceb60` → `60c8690f`
- `executive_agents_platform/tests/test_resemble_http_streaming.py` — 12 new tests
  (HTTP chunked response, RIFF stripping, quality knobs, error classification)
- `executive_agents_platform/tests/test_edge_cases.py` — 2 health-check tests
  ported from WS to HTTP path; 18 retry/QoS tests stay protocol-agnostic
- `executive_agents_platform/voice_agents_plugin.py` — relative-import fallback
  so plugin loads both as `~/.hermes/plugins/voice-agents/...` package and as
  flat sys.path module (fixes 5 pre-existing test-collection errors)
- `executive_agents_platform/tests/test_enrich_persona.py` — REPO path → repo-relative

#### Verification

- 12/12 new HTTP-streaming tests green
- 79/79 pre-existing edge-case tests green (only the 2 WS health-check
  cases needed the HTTP rewrite)
- 10/10 retry/transient tests green (protocol-agnostic, untouched)
- Live smoke test: `f.cluster.resemble.ai/stream` round-trips raw PCM
  (94 KB, 27 chunks, no RIFF leak) and 48kHz WAV (252 KB, valid header)
  for Steve Jobs voice end-to-end through `ResembleStreamingAdapter`

#### Carry-overs

- Account-tier upgrade for WS streaming is now an explicit, separate
  ADR-track item — not blocking voice operation on this account
- Hermes-agent `livekit_room_agent.py` is unchanged; the bridge contract
  (`/stream_synthesize` returns chunked PCM bytes) is preserved verbatim
- Option 3 (LiveKit-native plugins) remains the long-term direction

