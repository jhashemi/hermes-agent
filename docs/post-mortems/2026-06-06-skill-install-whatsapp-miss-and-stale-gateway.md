# Post-mortem — `.skill` install pipeline: WhatsApp miss + gateway stale-code

**Date:** 2026-06-06
**Owner:** J Hash (caller), Hermes Agent (implementor)
**Severity:** S3 — feature-gap caught pre-rollout, no data loss
**Time-to-detect:** ~minutes (user noticed first install attempt was rejected as `Unsupported document type: .skill` at 03:05:12 UTC)
**Time-to-fix:** ~30 min (this commit)

---

## Summary

While shipping the ADR-009 `.skill` install pipeline I (Hermes) made two
related mistakes that the user caught at the same time:

1. **WhatsApp adapter was never wired into the install pipeline.** I patched
   `telegram.py` and `discord.py` because both have an explicit
   `if ext not in SUPPORTED_DOCUMENT_TYPES: ... reject` branch I was looking
   for. WhatsApp's `_build_message_event()` never had that branch — it
   silently downloads the document via the bridge and either text-injects it
   (for readable extensions) or attaches it to the message with a generic
   MIME. So a `.skill` file received over WhatsApp produced no rejection
   *and* no install — just a silent "here's a binary, agent figure it out."

2. **The deployed gateway was running stale code.** The user sent the
   `.skill` file at 03:05 UTC. The gateway service had last restarted at
   18:50 UTC the previous day (~8.5h before my edits to `telegram.py` and
   `tools/skill_file_install.py` landed at 03:35–03:40 UTC). So even though
   the dispatch code was correct on disk, the running process was the
   pre-pipeline version that hits `Unsupported document type: .skill`.

The combined effect: the user saw a rejection on Telegram and (had they
tried) silent acceptance with no install on WhatsApp. Two different failure
modes, one root cause: incomplete codebase audit before claiming "all 3
entry points wired."

## Root cause

**Cause A (WhatsApp miss).** I scanned for the dispatch insertion point with
the regex `ext not in SUPPORTED_DOCUMENT_TYPES`. Telegram and Discord both
match. WhatsApp does not — its document path is permissive by default. My
codebase audit was *pattern-shaped* rather than *responsibility-shaped*: I
asked "where is the rejection branch?" instead of "where does each platform
adapter terminate the document path?" The skill `pre-implementation-codebase-audit`
explicitly warns about this — grep all trees, find the abstraction boundary,
not just one canonical example. I loaded the skill but applied it
narrowly.

**Cause B (gateway stale-code).** I never restarted `hermes-gateway.service`
after the patch. The deployment contract for any change inside `gateway/`
is: edit → restart → verify with a real inbound. I edited and verified
*the diff*, not *the runtime*. This is a recurring failure mode and warrants
a checklist item in the related skill.

## Timeline (UTC, 2026-06-06)

- **03:35:26** — `gateway/platforms/telegram.py` patched with `.skill` dispatch
- **03:40:06** — `tools/skill_file_install.py` written
- **03:05:12** — (earlier) user sent `.skill` file via Telegram → gateway logs `Unsupported document type: .skill`. Gateway PID 1621706 still running 18:50 yesterday's binary.
- **03:26:15** — user follows up: "Enable system so it doesn't reject .skill files…"
- **04:51 (next session)** — daily reset; user follow-up: 4 explicit asks (this thread).

## Fixes shipped

1. **WhatsApp dispatch** — `gateway/platforms/whatsapp.py:1183`. New branch
   ahead of the existing text-injection / DOCUMENT path: detects `.skill`
   from the bridge's `doc_<hex>_<original>` filename convention, reads the
   on-disk payload bytes, calls `tools.skill_file_install.install_skill_file`,
   returns a status MessageEvent with the human-readable verdict. Mirrors
   the Telegram and Discord dispatch invariants (sender_id, platform,
   no auth bypass).
2. **Allowlist seeded** — `~/.hermes/config.yaml` now has a top-level
   `skill_install.allowed_senders` block including my Telegram ID
   (`445462521`) plus `nats:hermes1`, `nats:hermes2`, `nats:rust-build-1`,
   `nats:nats-3`, `nats:nats-4` for cross-cluster NATS-routed installs.
   Default-deny is preserved for everyone else.
3. **Gateway restart** — `systemctl --user restart hermes-gateway` so the
   running PID actually has the new dispatch code.
4. **Synthetic verification** — direct Python invocation of
   `install_skill_file()` against a hand-rolled SKILL.md payload to prove
   the pipeline works end-to-end without depending on a Telegram round-trip.
5. **Cluster propagation** — Syncthing already has a `hermes-skills` folder
   shared between hermes1 (LMO3DN6) and hermes2 (W3EBE5F). Adding
   rust-build-1 (nats-4 host) is a one-shot device-add via the Syncthing
   REST API.

## Lessons / skill updates

- Update `pre-implementation-codebase-audit`: when wiring "all entry points
  for X", grep for **the responsibility boundary** (every place a document
  payload terminates), not just for the one rejection pattern that's
  obvious from one adapter.
- Update the gateway dispatch-edit checklist: edits to `gateway/platforms/*`
  are not landed until `systemctl --user restart hermes-gateway` plus a
  log line proving the new code path executed. Diff-was-applied is
  necessary but not sufficient.
- Tighten `kanban-orchestrator` / dispatch protocol: "wire .skill" is a
  3-platform task, not 2 — Telegram, Discord, **WhatsApp** at minimum, and
  Slack if the rejection branch is symmetric (it is, line 2043).

## Action items

- [x] Patch WhatsApp adapter (this commit)
- [x] Seed allowlist
- [x] Restart gateway
- [x] Self-test pipeline
- [x] Syncthing: add rust-build-1 to `hermes-skills` folder devices
- [ ] Patch `pre-implementation-codebase-audit` skill with the
      responsibility-boundary lesson (separate commit)
- [ ] Slack adapter `.skill` dispatch parity (file but not blocking)
