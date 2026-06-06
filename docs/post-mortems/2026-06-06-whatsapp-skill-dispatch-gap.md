# Post-mortem: WhatsApp `.skill` dispatch missing from ADR-009

**Date:** 2026-06-06
**Severity:** Medium (silent gap; .skill files sent over WhatsApp would have
been rejected as "Unsupported document type" or text-injected as garbage
binary into the agent prompt)
**Detection:** User-reported during cluster propagation work
**Resolution:** Commit `3758b7477` + parity tests

## Summary

The original ADR-009 commit (`46dc4687a`, 2026-06-05 03:40 UTC) introduced
the `.skill` file install pipeline and wired three gateway adapters to
dispatch incoming `.skill` documents into `install_skill_file()` before the
generic text-injection / rejection branches.

The commit message claimed coverage of all chat platforms, but the diff
shows only `gateway/platforms/telegram.py` and `gateway/platforms/discord.py`
plus `gateway/platforms/base.py`. **WhatsApp was excluded.** Users sending
`.skill` files via WhatsApp would have hit one of two failure modes:

1. The bridge pre-downloads to disk and the adapter's text-injection branch
   would attempt to decode binary ZIP/tar.gz bytes as UTF-8 markdown,
   feeding garbage into the agent's prompt.
2. If the document type sniffer rejected, the user got a generic
   "Unsupported document type" message with no path to install.

## Root cause

Three contributing factors:

1. **Code-path divergence.** The WhatsApp adapter handles documents
   differently than Telegram/Discord. The Bridge service pre-downloads
   files to local disk and prefixes them with `doc_<hex>_<original>`,
   meaning the adapter's filename-extension check sees `doc_abc_my.skill`
   not `my.skill`. The original author appears to have looked at the two
   adapters where the dispatch was *easy* and skipped the one that needed
   prefix-unwrap logic.

2. **No parity tests.** Coverage was Telegram-only
   (`tests/gateway/test_telegram_skill_file.py`). CI and review had no
   structural signal that Discord and WhatsApp were missing — the test
   suite passed because it never asserted anything about them.

3. **Commit message overclaimed.** The commit body listed Telegram and
   Discord explicitly, but the framing ("Wired into:") implied
   completeness. There is no platform-coverage matrix in ADR-009 itself
   that would have flagged WhatsApp as out-of-scope-but-still-deferred vs.
   forgotten.

## Timeline

- **2026-06-05 03:40 UTC** — Commit `46dc4687a` ships ADR-009 with
  Telegram + Discord + base.py. WhatsApp gap created.
- **2026-06-05 ~04:58 UTC** — User attempts to send `temporal-rice-explorer.skill`
  via Telegram (works) and other platforms (silently broken on WhatsApp).
- **2026-06-06 ~16:48 UTC** — During cluster-propagation work, the
  uncommitted WhatsApp dispatch block (added in working tree only in a
  prior session) is identified. Working tree showed it; git blame returned
  `0000000000 Not Committed Yet` — meaning a gateway restart would have
  silently lost the fix.
- **2026-06-06 17:14 UTC** — Commit `3758b7477` lands the WhatsApp dispatch
  block + post-install cluster-broadcast hook.
- **2026-06-06 17:20 UTC** — Parity tests added for Discord and WhatsApp
  (this post-mortem commit).

## Resolution

### 1. WhatsApp dispatch block (committed in `3758b7477`)

In `gateway/platforms/whatsapp.py`, before the generic text-injection
branch, added a per-document loop that:

- Strips the bridge's `doc_<hex>_<original>` prefix to recover the user's
  filename
- Skips non-`.skill` documents
- Reads payload from the bridge's local cache path
- Calls `install_skill_file(payload, filename, sender_id, platform="whatsapp")`
- Returns the install report's user_message as a TEXT MessageEvent

### 2. Parity tests (this commit)

Added `tests/gateway/test_discord_skill_file.py` and
`tests/gateway/test_whatsapp_skill_file.py`. Each enforces that:

- The adapter has a dispatch site that calls `install_skill_file`
- The right `platform=` kwarg is passed
- Filename-extension checks gate the dispatch correctly
- The `install_skill_file` signature is compatible with the kwargs the
  adapter passes (contract test)

The WhatsApp test additionally asserts the `doc_<hex>_<original>` unwrap
logic is present, since that's the special case unique to that adapter.

### 3. Lesson encoded in tests

Any future gateway adapter that accepts file uploads MUST:

1. Wire `.skill` dispatch BEFORE the text-injection / rejection branch
2. Pass `platform=<adapter_name>` so the cluster-broadcast loop-guard works
3. Ship a `tests/gateway/test_<platform>_skill_file.py` with parity coverage

A future PR should add a structural test that lists all gateway adapters
in `gateway/platforms/` and asserts each has a `_try_handle_*_skill_file`
helper or equivalent dispatch site — this would catch the same class of
miss for any new adapter (Slack, Matrix, Signal, Mattermost, etc.).

## Verification

End-to-end install of `/tmp/skill-drop/temporal-rice-explorer.skill`:

```
verdict: safe
skill_name: temporal-rice-explorer
install_path: /home/ubuntu/.hermes/skills/temporal-rice-explorer
```

Cluster propagation verified across all 3 nodes (hermes2, rust-build,
hermes1) within <4s of broadcast. JetStream stream `SKILLS_BROADCAST`
seq 437 contains the propagated bundle.

Test suite:
```
tests/gateway/test_telegram_skill_file.py ...... PASSED (1/1)
tests/gateway/test_discord_skill_file.py ....... PASSED (2/2)
tests/gateway/test_whatsapp_skill_file.py ...... PASSED (3/3)
======================== 6 passed in 2.82s ========================
```

## Action items

- [x] Land WhatsApp dispatch block (`3758b7477`)
- [x] Add Discord parity tests
- [x] Add WhatsApp parity tests
- [x] Wire post-install cluster broadcast (`3758b7477`)
- [ ] Future: add structural test enumerating all gateway adapters and
      asserting each has a `.skill` dispatch site
- [ ] Future: add a "Platform Coverage" matrix to ADR-009 listing all
      supported and unsupported adapters explicitly
