# `.skill` File Install Pipeline — Implementation Plan

**Goal:** Receive `.skill` files via gateway (Telegram/Discord/etc.) or NATS subject, run security scan, install to skills directory atomically. Stop rejecting them as unsupported documents.

**Architecture:** New module `tools/skill_file_install.py` handles the format-detect → bundle-build → quarantine → scan → install flow. Gateway document handlers detect `.skill` BEFORE generic-rejection branch, route to the new module. NATS subject `hermes.skill.install.request` carries base64-encoded payload + sender metadata; same module handles it.

**Tech Stack:** Python 3.11, existing `tools/skills_hub.py` (`SkillBundle`, `quarantine_bundle`, `install_from_quarantine`), `tools/skills_guard.py` (`scan_skill`, `should_allow_install`, `ScanResult`), pytest.

**Owning ADR:** ADR-009 (to be written) — Skill-file install canonical pipeline; security-first; auth-gated.

---

## File Structure

```
tools/
  skill_file_install.py     # NEW: format-sniff + bundle-build + scan + install
                            # Public API: install_skill_file(payload_bytes, filename, sender_id, **opts) -> InstallReport

gateway/platforms/
  telegram.py               # MODIFY: detect .skill before line 3783 rejection branch
  discord.py                # MODIFY: detect .skill before line 4238 rejection branch

gateway/builtin_hooks/
  skill_install_nats_hook.py  # NEW: subscribe to hermes.skill.install.request, dispatch

tests/tools/
  test_skill_file_install.py  # 18 RED→GREEN tests covering all formats + security verdicts

docs/
  plans/2026-06-06-skill-file-install.md   # this file
```

---

## Format Sniff Decision

A `.skill` file is one of three formats — auto-detected from bytes:

| Format | Sniff | Contents |
|--------|-------|----------|
| **Single-file** | text starts with `---\n` (YAML frontmatter) and contains `\nname:` within first 1KB | One `SKILL.md` (the canonical case the user just sent) |
| **Tar.gz bundle** | bytes start with `\x1f\x8b` (gzip magic) | Tar archive containing `SKILL.md` + optional `references/`, `scripts/`, `assets/` |
| **Zip bundle** | bytes start with `PK\x03\x04` (zip magic) | Zip archive, same layout |

Anything else → `InvalidSkillFileError("not a recognized .skill format")`.

---

## Security Layers (in order)

1. **Authorization gate** — sender must be in `skill_install.allowed_senders` (config) OR be the gateway `home_chat`/admin user. Default: empty allowlist = locked (no installs accepted from anyone). User must explicitly opt in.
2. **Size limit** — payload max 5 MB (skills should be small).
3. **Format sniff** — must be one of the 3 known formats.
4. **Path validation** — `_validate_bundle_rel_path()` already in skills_hub.py rejects `..`, absolute paths, drive letters, hidden dirs. Reused.
5. **Static scan** — `scan_skill()` runs THREAT_PATTERNS regex + invisible unicode + structural checks.
6. **Verdict gate** — `should_allow_install(result, force=False)` blocks `block` verdicts. `warn` verdicts proceed but get reported back.
7. **Audit log** — every install (success or block) appends to `~/.hermes/skills/.audit.log` via `append_audit_log()`.

---

## Task 1: ADR-009 + Plan doc

- [ ] **Step 1: Write ADR-009** — `docs/ADR/ADR-009-SKILL-FILE-INSTALL.md`. Status: Proposed. Includes invariants 1-7 above + threshold contract for verdicts.
- [ ] **Step 2: Update ADR README** — add row for ADR-009.
- [ ] **Step 3: Verify guard clean** — `python3 scripts/adr_index_guard.py`.

(Plan doc is this file.)

---

## Task 2: TDD core — `skill_file_install.py`

**Files:**
- Create: `tools/skill_file_install.py`
- Test: `tests/tools/test_skill_file_install.py`

- [ ] **Step 1: RED tests for format detection** — 5 tests covering single-file, tar.gz, zip, junk-bytes, empty.

```python
# tests/tools/test_skill_file_install.py
import pytest
from tools.skill_file_install import detect_format, SkillFileFormat, InvalidSkillFileError

class TestDetectFormat:
    def test_single_file_skill_md(self):
        content = b"---\nname: foo\ndescription: bar\n---\n# Foo\n"
        assert detect_format(content) == SkillFileFormat.SINGLE_MARKDOWN

    def test_tar_gz_bundle(self):
        # gzip magic bytes
        content = b"\x1f\x8b\x08\x00" + b"\x00" * 100
        assert detect_format(content) == SkillFileFormat.TAR_GZ

    def test_zip_bundle(self):
        content = b"PK\x03\x04" + b"\x00" * 100
        assert detect_format(content) == SkillFileFormat.ZIP

    def test_junk_bytes_rejected(self):
        with pytest.raises(InvalidSkillFileError):
            detect_format(b"this is not a skill file")

    def test_empty_rejected(self):
        with pytest.raises(InvalidSkillFileError):
            detect_format(b"")
```

- [ ] **Step 2: GREEN minimal `detect_format`**

- [ ] **Step 3: RED tests for `build_bundle`** — converts bytes to `SkillBundle`. 5 tests: single-file inferred-name, single-file frontmatter-name, tar.gz extraction, zip extraction, traversal-path rejection.

- [ ] **Step 4: GREEN `build_bundle`** — uses `tarfile`, `zipfile`, `_validate_bundle_rel_path` for safe extraction.

- [ ] **Step 5: RED tests for `install_skill_file`** — full pipeline orchestration with mock auth + mock scanner.

- [ ] **Step 6: GREEN `install_skill_file`** — orchestrates: auth → format → bundle → quarantine → scan → verdict → install. Returns typed `InstallReport`.

- [ ] **Step 7: Commit**

```bash
git add tools/skill_file_install.py tests/tools/test_skill_file_install.py
git commit -m "feat(skills): skill-file install pipeline with format sniff + security gate"
```

---

## Task 3: Authorization gate (`SkillInstallAuthorizer`)

**Files:**
- Create: `tools/skill_file_install.py` (extend, same file)
- Test: `tests/tools/test_skill_file_install.py` (extend)

- [ ] **Step 1: RED tests** — 4 tests: empty allowlist denies all; allowlist with sender_id allows; admin override allows; missing sender_id rejected.

- [ ] **Step 2: GREEN `SkillInstallAuthorizer`** — reads `skill_install.allowed_senders` from config; honors `HERMES_SKILL_INSTALL_ALLOW_ALL=1` env override (off by default).

- [ ] **Step 3: Commit**

---

## Task 4: Wire Telegram document handler

**Files:**
- Modify: `gateway/platforms/telegram.py`
- Test: `tests/gateway/test_telegram_skill_file.py`

- [ ] **Step 1: RED test** — async test using a mocked `Update.message.document` with `.skill` filename. Asserts `install_skill_file` is called with sender_id; user receives install report (NOT "Unsupported document type").

- [ ] **Step 2: GREEN — modify `telegram.py:3783`** — before the `if ext not in SUPPORTED_DOCUMENT_TYPES` check, add:

```python
if ext == ".skill" or original_filename.endswith(".skill"):
    file_obj = await doc.get_file()
    payload = bytes(await file_obj.download_as_bytearray())
    sender_id = str(msg.from_user.id) if msg.from_user else ""
    from tools.skill_file_install import install_skill_file
    report = install_skill_file(
        payload=payload,
        filename=original_filename or "untitled.skill",
        sender_id=sender_id,
        platform="telegram",
    )
    event.text = report.user_message  # human-readable status
    await self.handle_message(event)
    return
```

- [ ] **Step 3: Commit**

---

## Task 5: Wire Discord document handler (parallel to Telegram)

Same pattern at `discord.py:4238`. Adapt to Discord's attachment object shape.

---

## Task 6: NATS subject `hermes.skill.install.request`

**Files:**
- Create: `gateway/builtin_hooks/skill_install_nats_hook.py`

- [ ] **Step 1: RED test** — publish a `{"payload_b64": "...", "filename": "x.skill", "sender_id": "..."}` JSON to mock NATS connection; assert `install_skill_file` called.

- [ ] **Step 2: GREEN** — JetStream pull-consumer on `hermes.skill.install.request`, durable=`skill_installer`, decodes JSON+base64, calls module, publishes result to `hermes.skill.install.result` for observability.

- [ ] **Step 3: Commit**

---

## Task 7: Add `.skill` to `SUPPORTED_DOCUMENT_TYPES` (canonical recognition)

**Files:**
- Modify: `gateway/platforms/base.py:815`

```python
".skill": "application/vnd.hermes.skill",
```

This means: even if the dedicated handler ever fails to fire, the document is at least cached (not rejected outright), and downstream tooling can find it.

---

## Self-Review (run after all tasks)

- [ ] All RED tests verifiably failed first
- [ ] All GREEN tests pass
- [ ] No bypass of auth gate (default empty allowlist denies all)
- [ ] Scanner verdict `block` rejects install (no override path)
- [ ] Path traversal in tar.gz rejected (existing `_validate_bundle_rel_path` covers this — verify with regression test)
- [ ] Audit log written for both success and block
- [ ] CHANGELOG entry
- [ ] ADR-009 ratified, index guard clean
- [ ] Hermes-fork remote topology verified before push (origin must be `jhashemi/hermes-agent`)
