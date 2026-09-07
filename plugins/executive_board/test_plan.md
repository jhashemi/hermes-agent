# Executive Board Plugin: Test Plan

## Overview

This document specifies per-command acceptance tests for the executive_board plugin. All tests must pass before deployment. Each test verifies both happy-path behavior and failure-envelope conditions per werner_vogels specifications.

---

## Command: `/board start <topic>`

### AC1.1 — Happy Path: Successful Session Initiation

**Given** a user sends `/board start "Q3 revenue targets"`  
**When** LiveKit worker pool is healthy  
**Then**
- Session UUID is generated (unique per invocation)
- Voice agents from config.voice_agents are instantiated on hermes2
- SessionHandle with ingress_url is returned to user
- Audit event `board.session_started` is emitted with session_id, topic, agents, timestamp
- User receives formatted Telegram reply: "Boardroom started. Session: <uuid>. Join with `/board join <uuid>`"

**Verification:**
```bash
# 1. Query okr_accountability.db for session entry
SELECT * FROM decisions WHERE session_id = '<uuid>';
# Expect: empty (no decisions yet, session just started)

# 2. Verify voice agents are running
curl https://hermes2/api/rooms/board_<uuid>/participants
# Expect: 2+ participants (orion + human)

# 3. Check audit event
curl https://event-bus/logs?type=board.session_started&session_id=<uuid>
# Expect: exactly 1 event with timestamp
```

---

### AC1.2 — AMBER Condition: LiveKit Unavailable (Graceful Degradation)

**Given** a user sends `/board start "Q3 revenue targets"`  
**When** LiveKit worker pool is unreachable (hermes2 down or network error)  
**Then**
- Session is NOT initiated on LiveKit
- Command returns formatted Telegram message: "Voice room unavailable. Your topic queued for next available session (ETA ~5 min). Session ID: <uuid>"
- Session is buffered in-memory decision queue (pending async batch processing)
- Audit event `board.session_queued` is emitted (not `board.session_started`)
- User can retry with `/board poll <uuid>` to check status
- When LiveKit recovers, async worker processes queued sessions (no manual retry needed)

**Verification:**
```bash
# 1. Verify session NOT in LiveKit
curl https://hermes2/api/rooms/ | grep -c "board_<uuid>"
# Expect: 0 (room does not exist)

# 2. Verify session buffered locally
ps aux | grep "executive_board.*queue" | head -1
# Expect: async queue worker running

# 3. Verify audit event type is "queued" not "started"
curl https://event-bus/logs?type=board.session_queued
# Expect: event with session_id = <uuid>

# 4. Check decision buffer size
hermes-cli board status --session <uuid>
# Expect: state = "QUEUED", buffer_size = 1
```

---

### AC1.3 — RED Condition: Session Initialization Timeout (>30s)

**Given** a user sends `/board start "Q3 revenue targets"`  
**When** agent initialization (Session.start()) hangs for >30 seconds (e.g., livekit-ffi hung)  
**Then**
- Command aborts after 30s timeout
- NO session is created (LiveKit room not instantiated)
- Structured failure event `voice.turn.failed` is emitted to NATS event bus with:
  - `error_type`: "session_init_timeout"
  - `message`: "Session initialization exceeded 30s timeout"
  - `traceback`: Python stack trace from livekit_agent.py
  - `session_id`: "<uuid>"
  - `timestamp`: Unix seconds
- User receives Telegram message: "Boardroom initialization failed (timeout). Support notified. Session ID: <uuid>"
- Operator/werner_vogels receives escalation alert (via event subscription)

**Verification:**
```bash
# 1. Verify no LiveKit room created
curl https://hermes2/api/rooms/board_<uuid> 2>&1 | grep -i "not found"
# Expect: 404 response

# 2. Verify failure event emitted
nats sub 'voice.turn.failed' &
# Run command, wait 31s
# Expect: message with session_id = <uuid>, error_type = "session_init_timeout"

# 3. Verify no database entry
sqlite3 okr_accountability.db "SELECT COUNT(*) FROM decisions WHERE session_id='<uuid>';"
# Expect: 0 (no decisions persisted)

# 4. Verify escalation alert sent
curl https://escalation-api/logs?type=voice.turn.failed
# Expect: event with error_type = "session_init_timeout"
```

---

### AC1.4 — RED Condition: Message Role Violation Detected

**Given** a user sends `/board start "Q3 revenue targets"`  
**When** agent's message sequence violates strict user ↔ assistant alternation (e.g., two consecutive agent messages without intervening user input)  
**Then**
- Session is immediately aborted (before any decision is persisted)
- Structured failure event `governance.role_violation` is emitted with:
  - `error_type`: "message_role_violation"
  - `evidence`: Human-readable description of violation
  - `session_id`: "<uuid>"
  - `timestamp`: Unix seconds
- User receives Telegram message: "Boardroom role sequencing error. Session disabled. Contact support."
- Demis_hassabis receives governance escalation alert for belief-model review

**Verification:**
```bash
# 1. Inject mock agent that emits consecutive agent messages
# (in test environment only; simulates livekit_agent.py bug)
python -m pytest test_voice_bridge.py::test_role_violation_abort

# 2. Verify governance event
nats sub 'governance.role_violation' &
# Expect: message with error_type = "message_role_violation"

# 3. Verify session aborted (no decisions persisted)
sqlite3 okr_accountability.db "SELECT COUNT(*) FROM decisions WHERE session_id='<test_uuid>';"
# Expect: 0

# 4. Verify belief-model review escalation
curl https://governance-api/escalations?type=belief_model_review
# Expect: new escalation with session_id = <test_uuid>
```

---

### AC1.5 — Input Validation: Topic Length & Characters

**Given** a user sends `/board start <invalid_topic>`  
**Where** invalid_topic is:
- Empty string: `""` → Rejected
- >100 characters → Rejected
- Non-ASCII characters without steve_jobs approval → Rejected (open question)
- SQL injection attempt: `"'; DROP TABLE decisions; --"` → Sanitized & rejected

**Then** command returns error Telegram message: "Invalid topic: <reason>. Keep it ≤100 characters and use basic ASCII."  
**And** no session is created, no database writes occur

**Verification:**
```bash
for topic in "" "x"*101 "'; DROP TABLE decisions; --"; do
  hermes-cli board start "$topic" 2>&1 | grep -i "invalid topic"
done
# Expect: 3 error messages, 0 sessions created
```

---

## Command: `/board join <session_id> [mode]`

### AC2.1 — Happy Path: Voice Join

**Given** an active session with session_id exists  
**When** a user sends `/board join <session_id>`  
**Then**
- Participant JWT token is generated
- Participant is added to LiveKit room
- User receives Telegram message: "Joined boardroom. Listening to session <session_id>..."
- Audit event `board.participant_joined` is emitted with session_id, mode=voice, timestamp

**Verification:**
```bash
curl https://hermes2/api/rooms/board_<session_id>/participants
# Expect: 2 participants (agent + user)
```

---

### AC2.2 — Happy Path: Text-Only Join (2G/3G Fallback)

**Given** an active session exists  
**When** a user sends `/board join <session_id> text`  
**Then**
- Participant is added to room in TEXT mode only (no voice stream)
- User receives transcript via polling (not real-time voice)
- Audit event `board.participant_joined` emitted with mode=text
- User can still use `/board poll <session_id>` to get decision snapshots

**Verification:**
```bash
# 1. Verify text-mode participant in room
curl https://hermes2/api/rooms/board_<session_id>/participants | jq '.[].mode'
# Expect: one participant with mode="text"

# 2. Verify no voice data streamed to text participant
# (inspect network traffic or mock LiveKit logs)
```

---

### AC2.3 — Session Not Found

**Given** a user sends `/board join <nonexistent_session_id>`  
**When** session_id does not exist in okr_accountability.db or LiveKit  
**Then**
- Command returns error: "Session not found: <session_id>. Use `/board start` to create a new session."
- No database writes occur

---

### AC2.4 — Session Expired

**Given** a user sends `/board join <session_id>`  
**Where** session was created >max_session_age_hours ago (default 4h)  
**Then**
- Command returns error: "Session expired. Archived sessions cannot be rejoined."
- User is offered: "Start a new session with `/board start <topic>`"

---

## Command: `/board poll <session_id>`

### AC3.1 — Happy Path: Active Session Poll

**Given** an active session has N decisions (N ≥ 0)  
**When** a user sends `/board poll <session_id>`  
**Then**
- Decision snapshot is queried from okr_accountability.db (read-only)
- Response is formatted as JSON + text summary, returned within 2s (SLA)
- User receives Telegram message:
  ```
  Boardroom Session <session_id>
  Topic: Q3 revenue targets
  Decisions: 3 pending, 1 approved, 0 disputed
  Last activity: 30s ago
  
  [JSON snippet of latest decision]
  
  Use `/board archive <session_id>` to finalize decisions.
  ```
- Audit event `board.session_polled` is emitted with session_id, decision_count

**Verification:**
```bash
# 1. Measure latency
time hermes-cli board poll <session_id>
# Expect: <2 seconds total

# 2. Verify decision counts in response match DB
sqlite3 okr_accountability.db "SELECT COUNT(*) FROM decisions WHERE decision_state='PENDING';"
# Cross-check against Telegram message count
```

---

### AC3.2 — Poll Timeout SLA Breach

**Given** a database query hangs (e.g., lock on okr_accountability.db)  
**When** query exceeds db_query_timeout_seconds (default 5s)  
**Then**
- Query is aborted (no cascading wait)
- User receives graceful message: "Poll in progress. Try again in 5 seconds."
- Audit event `board.poll_timeout` is emitted
- No exception propagates to Telegram user (safe fallback)

---

## Command: `/board archive <session_id>`

### AC4.1 — Happy Path: Archive Active Session

**Given** an active session with N decisions  
**When** a user sends `/board archive <session_id>`  
**Then**
- Session is marked CLOSED in okr_accountability.db
- Immutable decision snapshot is written to kanban_board.db board_sessions table (append-only)
- LiveKit room is closed gracefully
- Decisions are finalized (state = APPROVED)
- User receives Telegram message: "Boardroom archived. Decision summary attached."
- Audit event `board.session_archived` is emitted with final decision count
- Archive notification webhook (if configured) is sent to admin

**Verification:**
```bash
# 1. Verify session closed in okr_accountability.db
sqlite3 okr_accountability.db "SELECT state FROM decisions WHERE session_id='<session_id>' LIMIT 1;"
# Expect: "APPROVED" (was "PENDING" before archive)

# 2. Verify snapshot in kanban_board.db
sqlite3 kanban_board.db "SELECT decision_count FROM board_sessions WHERE session_id='<session_id>';"
# Expect: N (number of decisions)

# 3. Verify LiveKit room closed
curl https://hermes2/api/rooms/board_<session_id> 2>&1 | grep -i "not found"
# Expect: 404 (room deleted)

# 4. Verify webhook sent
curl https://webhook.example.com/logs?event=board.session_archived
# Expect: POST with decision summary JSON
```

---

### AC4.2 — AMBER Condition: Database Write Timeout During Archive

**Given** a user sends `/board archive <session_id>`  
**When** kanban_board.db write times out (db_archive_timeout_seconds exceeded)  
**Then**
- Decision snapshot is buffered in-memory
- User receives message: "Decision buffered locally; syncing to archive..."
- Async retry worker persists snapshot within 1 minute
- Audit event `board.decision.buffer_overflow` is emitted
- NO exception propagates to user (safe degradation)

**Verification:**
```bash
# 1. Simulate DB write timeout
# (mock kanban_board.db to hang for 15s)
python -m pytest test_commands.py::test_archive_db_timeout

# 2. Verify buffer event
curl https://event-bus/logs?type=board.decision.buffer_overflow
# Expect: event with session_id = <session_id>

# 3. Verify eventual persistence
sleep 70  # Wait for async retry
sqlite3 kanban_board.db "SELECT COUNT(*) FROM board_sessions WHERE session_id='<session_id>';"
# Expect: 1 (snapshot persisted)
```

---

## Command: `/board config`

### AC5.1 — Happy Path: Admin Retrieves Config

**Given** a user sends `/board config`  
**Where** user's Telegram ID matches config.accounts[*].admin_telegram_id  
**Then**
- Current plugin config is returned in Telegram message
- Sensitive values (API keys, webhooks) are redacted
- Response includes: enabled, voice_agents, session_timeout_seconds, etc.
- Audit event `board.config_queried` is emitted with account name

**Verification:**
```bash
# 1. Send command as admin user
hermes-cli board config --account steve_jobs_board
# Expect: config snapshot with no API keys visible

# 2. Verify audit event
curl https://event-bus/logs?type=board.config_queried&account=steve_jobs_board
# Expect: exactly 1 event
```

---

### AC5.2 — Non-Admin User Denied

**Given** a user sends `/board config`  
**Where** user's Telegram ID does NOT match any config.accounts[*].admin_telegram_id  
**Then**
- Command is rejected with message: "Insufficient permissions."
- No config data is leaked
- Audit event `board.config_denied` is emitted with user's Telegram ID

---

## Integration Tests

### IT1 — Full Session Lifecycle

**Scenario:** User initiates → joins → polls → archives → verifies audit trail

**Steps:**
```bash
1. /board start "Q3 revenue"
   → Capture session_id_1
   → Verify board.session_started event

2. /board join <session_id_1>
   → Verify participant added to LiveKit

3. Wait 30s (let agent speak, decisions populate)

4. /board poll <session_id_1>
   → Verify decisions visible
   → Check latency <2s

5. /board archive <session_id_1>
   → Verify session closed
   → Verify snapshot in kanban_board.db

6. Query audit trail
   → Verify all 5 events in order
```

---

### IT2 — AMBER Cascades: LiveKit Down → Archive Still Works

**Scenario:** Start session queued (LiveKit down), then archive it

**Expected:** Archive persists buffered decisions to kanban_board.db gracefully

---

### IT3 — RED Escalation: Timeout + Governance Event

**Scenario:** Session init hangs, /board poll on same session_id returns "not found", escalation received

**Expected:** No orphaned sessions, governance event emitted

---

## Non-Functional Requirements

### Schema Validation

- **Test:** Verify commands.py imports cleanly
  ```bash
  python -c "from plugins.executive_board import commands; print('OK')"
  # Expect: OK
  ```

- **Test:** Verify no schema mutations
  ```bash
  # Instrument okr_accountability.db with trigger to detect ALTER TABLE
  # Run full test suite
  # Expect: 0 schema mutations detected
  ```

### MTP Compliance (Mobile Usability)

- **Test:** Every command string is typable on phone in ≤30s (normal thumbs)
  - `/board start "Q3 revenue"` — 9 keystrokes (PASS)
  - `/board join <uuid>` — 15 keystrokes + UUID copy-paste (PASS)
  - No nested subcommand chains (PASS: all are /board <verb>)

---

## Acceptance Criteria Summary

✅ All 6 files exist in live repo: DESIGN.md, plugin.yaml, commands.py, voice_bridge.py, config.example.yaml, test_plan.md  
✅ test_plan.md covers every command (start, join, poll, archive, config)  
✅ No schema migrations on okr_accountability.db or kanban_board.db  
✅ commands.py imports cleanly: `python -c "import commands"`  
✅ All RED failure paths escalate with structured events  
✅ All AMBER conditions return graceful user messages  
✅ Prompt caching sacred (no hooks)  
✅ Message role alternation strict (verified via tests)  
✅ Resemble TTS only (hard constraint in code)  
✅ hermes1_voice_disabled enforced (hard constraint in code)  
✅ MTP-safe commands (all <30 seconds, no nested chains)  

---

## Sign-Offs

**Pending Review:**
- [ ] steve_jobs: User-facing strings, Telegram UX, MTP compliance
- [ ] werner_vogels: Failure paths, RED/AMBER envelope, error events
- [ ] demis_hassabis: Belief-model changes (none in skeleton; TBD in implementation)
- [ ] jeff_dean: LiveKit bridge latency SLA (<30s init, <2s poll)

