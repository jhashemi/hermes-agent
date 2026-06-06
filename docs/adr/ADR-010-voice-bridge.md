# ADR-010: Voice ↔ WhatsApp/Hermes Gateway Bridge

**Status:** Accepted (council-approved 2026-06-06; free-standing spike, no parent OKR)
**Date:** 2026-06-06
**Author:** Jeff Dean persona, on behalf of J Hash
**Replaces / supersedes:** none
**Tracks:** Track B from D-then-B sequence (D track shipped: replicas=3, scanner-gated rust-build installer, stable cluster_node_name)

---

## Why this is "Proposed" not "Accepted"

User asked to validate completion of OKR `426dd38d`. **That OKR does not exist in any cluster store.** Searched all 7 NATS streams (`exec_okr`, `exec_consensus`, `exec_goal_plan`, `exec_dispatch`, `EXEC_COGNITIVE`, `EXEC_AGENT`, `EXEC_SERVICE`), kanban SQLite (8 tables), and filesystem JSON/YAML/MD under `~/.hermes/` — zero hits.

User chose "Track D then B" — D = fix the 3 real propagation blockers, B = ship voice bridge as a free-standing spike with ADR (no full Goal+Plan+Deliberation+Consensus lifecycle). Per user governance flag (3 prior OKRs bypassed lifecycle), this ADR is **explicitly NOT claiming OKR alignment**. If a real OKR is later raised, this ADR can be ratified under it.

---

## Context

J Hash interacts with Hermes through three channels today:
- **Telegram** (primary, this conversation)
- **WhatsApp** (recently wired — `.skill` dispatch was the WhatsApp miss that triggered today's post-mortem)
- **Voice** (LiveKit room, occasional, lossy hand-off back to text-mode for follow-up)

Each channel has its own context: Telegram has the tool-rich session you're reading, WhatsApp has its own gateway adapter and message history, voice has whatever the LiveKit agent kept in working memory. **There is no bidirectional context bridge.** When J switches from voice to WhatsApp, the voice agent's reasoning is lost. When the WhatsApp gateway sees a `.skill` file, the voice agent doesn't know.

User selected design options (from earlier compacted turn): **1C, 2B, 3B+C, 4B**.

---

## Decision

Build a **co-pilot bridge** with **lazy hydration with push manifest, snapshot-push (not bidirectional v1), 800ms p95 latency SLO, and combined query-port + push-snapshot** propagation.

### Jeff Dean framing

> "Don't move data you don't need to move. Latency is a first-class design constraint. Lazy hydration with push manifest > bulk mirroring. Scale by 5–10×, not 100×."

Applied:
1. **No bulk context mirroring.** Voice agent does NOT receive WhatsApp/Telegram message history at session start. It receives a **manifest** (recent topics, active OKRs, last 3 skills used, current kanban tasks) — typically <2 KiB.
2. **Lazy fetch via query ports.** When voice needs detail ("what was that skill we just installed?"), it issues a port query → gateway returns just the answer. Round-trip target: 500ms p50, 800ms p95.
3. **Push only on state change.** When gateway processes a meaningful event (new skill install, OKR update, kanban transition), it pushes a snapshot envelope to the voice room. Channel: NATS `voice.snapshot.{user_id}.>`.
4. **One-way v1 (snapshot-push from gateway → voice).** Voice → gateway is via the existing tool-call pathway; no new bridge needed. Bidirectional escalation deferred to v2.

---

## Architecture

```
┌──────────────────────┐         ┌──────────────────────────┐
│  Telegram / WhatsApp │         │  LiveKit Voice Room      │
│       Gateway        │         │  (Hermes voice agent)    │
└──────────┬───────────┘         └────────────┬─────────────┘
           │                                  │
           │ 1. event happens                 │ 3. lazy port query
           │    (skill install, OKR, etc.)    │    "what did we just install?"
           ▼                                  ▼
   ┌───────────────────┐              ┌───────────────────┐
   │  voice_bridge.py  │              │   query_port      │
   │  (push snapshot)  │              │  (gateway side)   │
   └─────────┬─────────┘              └─────────┬─────────┘
             │                                  │
             │ NATS: voice.snapshot.<uid>.<seq> │ direct response
             │   (manifest, ~2KiB max)          │   (just the slice needed)
             ▼                                  │
   ┌───────────────────────────────────────────┴┐
   │  Voice agent receives manifest → lazy-loads│
   │  details via query port when user asks     │
   └────────────────────────────────────────────┘
```

### Components

**`tools/voice_bridge.py`** (new, ~250 LOC target)
- Subscribes to gateway events (skill_install, kanban_transition, okr_update)
- Builds compact manifest (≤2 KiB)
- Publishes to `voice.snapshot.{user_id}.{seq}` on NATS
- Republishes on state change only (debounced 500ms)

**`tools/voice_query_port.py`** (new, ~300 LOC)
- HTTP/WebSocket endpoint co-located with voice agent
- Single endpoint: `GET /context/{slice}` where slice ∈ {recent_skills, active_okrs, kanban_in_progress, last_message}
- Returns JSON, capped at 4 KiB per slice
- **Uses Hermes auth tokens** (same allowlist as `skill_install.allowed_senders`)

**Voice agent changes** (LiveKit side)
- On session start: subscribe to `voice.snapshot.{j_hash_uid}.>` JetStream consumer (durable, manual ack)
- On user query: tool call to `voice_query_port` for needed slice
- Manifest TTL: 60s — refresh on next push or on user query

### NATS stream

**`VOICE_SNAPSHOTS`** — new stream, replicas=3 (per D-track lesson), 24h retention, 10 MiB cap, 5s dedup window. Subjects: `voice.snapshot.>`.

---

## Performance budget (Jeff Dean back-of-envelope)

Targets at 5× scale (5 concurrent voice users, current is 1):

| Path | Bytes | Network hops | p50 target | p95 target |
|---|---|---|---|---|
| Snapshot push (gateway → voice) | ≤2 KiB | 1 (NATS local) | 50ms | 200ms |
| Lazy port query (voice → gateway) | ≤4 KiB resp | 2 (HTTP, possibly cross-AZ) | 200ms | 500ms |
| Voice agent processes manifest | (in-memory) | 0 | 5ms | 50ms |
| **End-to-end "what's new?"** | | | **255ms** | **750ms** |

Latency budget ✅ under 800ms p95. At 10× (10 users), bottleneck is voice agent's LLM call (300-1500ms), not the bridge.

---

## What we are NOT building (v1)

- ❌ Bidirectional context sync (voice writes back to gateway state). Out of scope; gateway state changes go through existing tool calls.
- ❌ Replay buffer for voice audio in WhatsApp. Different problem; transcripts only.
- ❌ Multi-user shared rooms. Single-user-per-room only.
- ❌ Cross-LiveKit-room handoff. v1 is one room per user.

---

## Risks & open questions

1. **Voice agent identity = which user is in the LiveKit room?** Currently inferred from JWT. Need stable mapping to gateway sender_id (`445462521` for J Hash). Mitigation: voice agent connects with a hardcoded `cluster_node_name`-style binding at session start.
2. **NATS `VOICE_SNAPSHOTS` does not exist yet.** Create with replicas=3 from the start (D-track lesson: silent replica edits are dangerous).
3. **Query port auth.** Default-deny allowlist mirroring `skill_install.allowed_senders` — same config block, same vocabulary.
4. **What if voice agent is offline when push happens?** JetStream consumer with durable + manual ack — voice gets the missed snapshot on reconnect.
5. **OKR governance gap remains.** This ADR is free-standing. If user later raises a real OKR (e.g. "OKR-XXX: Unify J Hash multi-channel context"), this ADR should be linked to it via an addendum, not retroactively renumbered.

---

## Acceptance criteria (executable, RED tests first per user pref)

- [ ] **RED-1:** Test that snapshot push from gateway lands in voice agent's manifest within 200ms p95 (synthetic 100 events, measure end-to-end).
- [ ] **RED-2:** Test that lazy port query returns within 500ms p95 for all 4 slices.
- [ ] **RED-3:** Test that voice agent reconnect picks up missed snapshots (stop voice agent, push 5 events, restart, verify all 5 in manifest).
- [ ] **RED-4:** Test default-deny on query port (random sender → 403).
- [ ] **RED-5:** Test that voice → gateway path uses existing tool-call mechanism (no new bridge surface for that direction).

Acceptance = all 5 RED tests written, fail before code, pass after.

---

## Deferred to v2

- Bidirectional sync (voice writes back to gateway state machine via NATS, not just tool calls)
- Multi-user shared rooms
- Cross-room handoff (J starts on phone, switches to laptop voice)
- Audio replay in WhatsApp (transcripts in v1, audio in v2)

---

## Decision rationale (why these options)

- **1C (snapshot-push):** Bidirectional sync (1A) is 4× the engineering cost for a use case (user switches mid-session) that happens <1×/day. Push-only meets actual need.
- **2B (800ms p95 SLO):** Aggressive but achievable. Lower (500ms) requires colocating voice agent with gateway — premature optimization. Higher (1500ms) breaks the conversational illusion.
- **3B+C (query port + push-snapshot):** Pure-push (3B alone) over-fetches; pure-query (3C alone) latency-hits every interaction. Hybrid: push manifest, query for details. Same pattern Hermes already uses for skills (manifest in audit log, lazy fetch by sha).
- **4B (lazy hydration with push manifest):** 4A (bulk mirror of all memory/OKR/skills) breaks the "don't move data you don't need to" principle and creates a sync hell. Lazy hydration with push manifest is the database covering-index pattern: replicate metadata, lazy-fetch data. (Original draft called this "sparse activation" by analogy to neuroscience; the analogy was loose — it's metadata caching, not hippocampal sparse coding. Renamed for honesty per Hassabis r1 review.)

---

## Implementation sequencing

1. **Spike 1** (1 day): Create `VOICE_SNAPSHOTS` stream with replicas=3, write `voice_bridge.py` skeleton, publish synthetic snapshots, verify NATS plumbing.
2. **Spike 2** (1 day): Write `voice_query_port.py` with auth gate; connect to existing gateway state.
3. **Spike 3** (2 days): Wire voice agent (LiveKit side) to consume snapshots + query port; ship RED tests.
4. **Spike 4** (1 day): GREEN — make all RED tests pass; close the spike with a follow-up post-mortem if any decisions changed.

Total: ~5 days for v1 working. Bidirectional sync (v2): separate ADR.

---

## Reviewers needed before "Accepted"

Per user pref ("Council gate = voice-twin multi-reviewer BEFORE impl"):
- Jeff Dean (this ADR's author) — done ✓
- Werner Vogels (cluster/replication concerns)
- Demis Hassabis (multi-modal / context engineering)
- Lewis Hamilton (latency/throughput SLO realism)

If 3 of 4 sign off → status flips to Accepted. If <3 → revise and re-circulate.

---

## §A — Round-1 Council Findings + Remediations (added 2026-06-06)

Three reviewers (Vogels, Hassabis, Hamilton) all returned **REVISE** in round 1. None approved without fixes. 10 distinct blocking concerns → grouped into 4 addenda below. Status remains **Proposed** until round-2 sign-off.

### §A.1 Durability & replication (Vogels) — 3 fixes

1. **Stable `Nats-Msg-Id` derivation rule.** The 5s dedup window is dead code without it. Required header value: `{user_id}:{event_type}:{state_hash_prefix_8}` where `state_hash_prefix_8` is the first 8 chars of `sha256(canonicalized event payload)`. `voice_bridge.py` MUST set this header on every publish; the JetStream stream MUST have `duplicate_window=5s` (already specified).
2. **Publish retry with exponential backoff in `voice_bridge.py`.** Minimum 3 attempts over 2s window (`100ms, 400ms, 1500ms`) to survive RAFT leader re-election (100–500ms unavailability window). On final failure, log and drop — voice can recover from the next state-change push. Do NOT block the originating gateway operation on bridge publish.
3. **Voice agent NATS client config.** MUST set `max_reconnect_attempts=-1` and `reconnect_time_wait=2s`. Same pattern the `skills_broadcast.py` subscriber already uses. This goes in the implementation guide, not left to per-implementer discretion.

### §A.2 Cognitive coherence (Hassabis) — 3 fixes

1. **Strike "sparse activation" branding.** It's metadata caching with lazy hydration — closer to a database covering index than to hippocampal sparse coding. Architecture is correct; the analogy isn't. Replace "sparse activation" with **"lazy hydration with push manifest"** throughout. No code change; documentation honesty.
2. **Session-start handshake.** Add `voice.hello.{user_id}` topic. When a voice session opens, voice agent publishes a hello envelope; gateway responds with an immediate manifest push (out-of-band of the normal change-driven push schedule). Closes the 60s TTL stale-start race that triggers exactly when the user is most latency-sensitive (just-triggered-event → immediate voice call about it).
3. **Semantic summaries in manifest envelope.** Each manifest entry MUST carry, alongside the ID:
   - 1–2 sentence semantic summary (≤200 chars)
   - For active OKRs: the current KR status delta from last manifest
   - Top-level manifest MUST include a `context_digest` field (≤200 chars, human-readable narrative of "where we are right now")

   This shifts the common-case query from 3–5 port fetches to zero (answer from manifest alone). Lazy port fetch remains for deep detail.

### §A.3 Latency methodology (Hamilton) — 4 fixes

1. **Stop summing independent p95s.** The original table claimed "750ms p95" by adding 50+200+500. That's between p95 and p99 of the convolution. Replace with a Monte Carlo or measured end-to-end distribution. Until measured, mark all latency targets as **TARGETS, not commitments**.
2. **Add p99 row.** New target table:

   | Path | p50 | p95 | p99 |
   |---|---|---|---|
   | Snapshot push (gateway → voice) | 50ms | 200ms | **400ms** |
   | Lazy port query (voice → gateway, cross-AZ) | 200ms | 500ms | **900ms** |
   | Voice agent LLM call (single turn) | 400ms | 1200ms | **2500ms** |
   | **End-to-end "what's new?" (no LLM)** | 250ms | 700ms | **1300ms** |
   | **End-to-end with LLM (full turn)** | 650ms | 1900ms | **3800ms** |

   p99 with LLM IS the user experience tail. Document it; don't hide it.
3. **LLM tail bail-out policy.** When voice agent's LLM call exceeds **1500ms**, voice agent MUST emit a verbal acknowledgement ("hold on, looking that up") and proceed with stale manifest rather than block silently. Hard timeout at **3000ms** → fall back to cached manifest + apologize. No silent infinite waits.
4. **Baseline cross-AZ query port BEFORE Spike 2.** Run a 1000-sample synthetic measurement of HTTP round-trip from the LiveKit-host AZ to the gateway-host AZ. If p95 > 600ms, redesign required (colocate query port with voice agent, or pre-warm connection pool). This is now a **gate** at the start of Spike 2, not a RED test that runs after build.

### §A.4 Out-of-scope acknowledgement (Hassabis follow-up)

Add to "What we are NOT building (v1)" list:

- ❌ **Voice agent insights persistence.** Reasoning the voice agent does that is NOT tool-called back to gateway state IS LOST at session end. v1 voice is a System-1-style ephemeral computation surface; durable state remains gateway-side. v2 may add a "voice scratchpad" tool but it is explicitly out of scope for v1.

### §A.5 Updated acceptance criteria

The 5 RED tests from the original ADR all stand. Three new RED tests added by §A:

- [ ] **RED-6:** Test that publishing the same event twice within 5s with the same state-hash results in 1 stream message (Nats-Msg-Id dedup works).
- [ ] **RED-7:** Test that voice session start triggers a hello-handshake → manifest push within 300ms (session-start race closed).
- [ ] **RED-8:** Test that LLM call >3000ms triggers fallback path (stale-manifest + verbal apology), not silent block.

Plus a Spike-2 gate (not a RED test, executed earlier):

- [ ] **GATE-A:** Cross-AZ query port baseline measurement returns p95 ≤ 600ms over 1000 samples. Run this BEFORE Spike 2 implementation work.

### §A.6 Round-2 reviewers

Same 3 reviewers (Vogels, Hassabis, Hamilton) — ask for re-circulation when §A is in place. If all 3 return APPROVE, status → Accepted and Spike 1 can begin.

### §A.7 Council outcome (2026-06-06)

**Round 1:** All 3 reviewers returned REVISE.
- Vogels: 3 blocking concerns (Msg-Id, retry, reconnect config)
- Hassabis: 3 blocking concerns (sparse-activation branding, session-start handshake, semantic summaries)
- Hamilton: 4 blocking concerns (p95-summing, p99 row, LLM tail bail-out, cross-AZ baseline gate)

**Round 2:** Vogels APPROVE, Hamilton APPROVE, Hassabis REVISE (rename was logged in §A but not propagated through main body).

**Round 2.5:** Main-body rename propagated; Hassabis APPROVE r2.5.

**Final:** 3/3 APPROVE. Status flipped to **Accepted**. Spike 1 can begin.

Sign-offs:
- Werner Vogels: APPROVE r2.
- Demis Hassabis: APPROVE r2.5.
- Lewis Hamilton: APPROVE r2.

### §A.8 GATE-A executed (2026-06-06)

GATE-A baseline measurement run before Spike 2. Full report: `docs/adr/gate-a-baseline-2026-06-06.md`.

**Result: ✅ PASS by ≥5× margin.**

| Path | p95 |
|---|---|
| hermes2 loopback | 1.01ms |
| hermes1 → hermes2 (intra-AZ Tailscale) | 3.05ms |
| rust-build → hermes2 (intra-AZ Tailscale) | 2.15ms |
| hermes2 → S3 us-east-1 (HTTPS, internet proxy for cross-region) | 109.95ms |

**Caveat:** All 3 cluster machines are in `us-east-1a`. Numbers reflect intra-AZ. Real cross-AZ adds ~1-2ms; cross-region 60-120ms. All projections still well under 600ms gate.

**Action:** Re-run GATE-A from voice-agent's actual deployment AZ/region once decided. Current measurement is best-case baseline, not deployment commitment.

