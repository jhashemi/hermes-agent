"""Skill-file install pipeline — receive `.skill` files via gateway/NATS,
sniff format, build SkillBundle, run scanner, install if scan permits.

ADR-009: Skill-File Install Pipeline (.skill over Gateway / NATS)

Public API:
    install_skill_file(payload, filename, sender_id, *, platform, force) -> InstallReport

Pipeline (in order):
    1. Authorization gate    (default-deny allowlist)
    2. Size limit             (5 MB)
    3. Format sniff           (single-markdown / tar.gz / zip)
    4. Bundle build           (extract + path-validate)
    5. Quarantine             (~/.hermes/skills/.quarantine)
    6. Scan                   (regex threats + invisible unicode + structural)
    7. Verdict gate           (block → refuse; warn/pass → install)
    8. Install                (atomic move + lockfile + audit log)

Adapters never call the underlying primitives directly. The boundary is
InstallReport, a typed result that adapters can stringify into user-visible
messages.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from tools.skills_hub import (
    SkillBundle,
    _validate_bundle_rel_path,
    _validate_skill_name,
    install_from_quarantine,
    quarantine_bundle,
)
from tools.skills_guard import scan_skill, should_allow_install

logger = logging.getLogger(__name__)


MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
_FRONTMATTER_NAME_RE = re.compile(rb"^name:[ \t]+(\S[^\n\r]*?)[ \t]*$", re.MULTILINE)


# ---------------------------- types ----------------------------


class SkillFileFormat(Enum):
    SINGLE_MARKDOWN = "single_markdown"
    TAR_GZ = "tar_gz"
    ZIP = "zip"


class InvalidSkillFileError(Exception):
    """Payload is not a recognized .skill format or is structurally invalid."""


@dataclass
class InstallReport:
    """Typed result returned to gateway adapters.

    verdict ∈ {safe, caution, dangerous, unauthorized, invalid_format, too_large, error}

    Scanner-derived verdicts ('safe', 'caution', 'dangerous') match the production
    `scan_skill()` taxonomy in tools.skills_guard. The remaining values are
    pipeline-level outcomes that short-circuit before the scanner runs.
    """
    verdict: str
    skill_name: str = ""
    install_path: str = ""
    scan_summary: str = ""
    user_message: str = ""
    error: str = ""
    sender_id: str = ""
    platform: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "skill_name": self.skill_name,
            "install_path": self.install_path,
            "scan_summary": self.scan_summary,
            "user_message": self.user_message,
            "error": self.error,
            "sender_id": self.sender_id,
            "platform": self.platform,
        }


# ---------------------------- format detection ----------------------------


def detect_format(payload: bytes) -> SkillFileFormat:
    """Sniff payload bytes; return SkillFileFormat or raise InvalidSkillFileError."""
    if not payload:
        raise InvalidSkillFileError("empty payload")

    # Gzip magic bytes
    if payload[:2] == b"\x1f\x8b":
        return SkillFileFormat.TAR_GZ

    # Zip magic bytes
    if payload[:4] == b"PK\x03\x04":
        return SkillFileFormat.ZIP

    # Single-file SKILL.md: must start with `---\n` (or `---\r\n`) and have
    # a `name:` key in the first 1KB
    head = payload[:1024]
    if head.startswith((b"---\n", b"---\r\n")):
        if _FRONTMATTER_NAME_RE.search(head):
            return SkillFileFormat.SINGLE_MARKDOWN
        raise InvalidSkillFileError(
            "frontmatter present but no `name:` key found in first 1KB"
        )

    raise InvalidSkillFileError(
        "not a recognized .skill format "
        "(expected SKILL.md frontmatter, gzip, or zip magic bytes)"
    )


# ---------------------------- bundle build ----------------------------


def _extract_name_from_frontmatter(content: bytes) -> Optional[str]:
    """Pull `name:` value from YAML frontmatter (first 4KB)."""
    head = content[:4096]
    m = _FRONTMATTER_NAME_RE.search(head)
    if not m:
        return None
    name = m.group(1).decode("utf-8", errors="replace").strip()
    # Strip surrounding quotes if any
    if name.startswith(('"', "'")) and name.endswith(('"', "'")):
        name = name[1:-1]
    return name or None


def _slugify_fallback(filename: str) -> str:
    """Convert 'My Skill.skill' → 'my-skill' as a name fallback."""
    stem = Path(filename).stem.lower()
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem)
    stem = re.sub(r"-+", "-", stem).strip("-")
    return stem or "untitled-skill"


def _resolve_skill_name(frontmatter_name: Optional[str], fallback_name: str) -> str:
    """Pick name: frontmatter wins, else fallback. Validated via skills_hub."""
    candidate = frontmatter_name or fallback_name
    return _validate_skill_name(candidate)


def build_bundle(
    payload: bytes,
    fmt: SkillFileFormat,
    *,
    fallback_name: str,
    sender_id: str = "",
    platform: str = "unknown",
) -> SkillBundle:
    """Convert raw bytes into a SkillBundle ready for quarantine/scan."""
    files: Dict[str, str | bytes] = {}
    fb_name = _slugify_fallback(fallback_name)

    if fmt == SkillFileFormat.SINGLE_MARKDOWN:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InvalidSkillFileError(f"SKILL.md is not valid UTF-8: {e}")
        files["SKILL.md"] = text
        name = _resolve_skill_name(_extract_name_from_frontmatter(payload), fb_name)

    elif fmt == SkillFileFormat.TAR_GZ:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    # Validate path FIRST, before any I/O
                    safe_path = _validate_bundle_rel_path(member.name)
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    content = extracted.read()
                    # Try utf-8 decode for text files; preserve bytes otherwise
                    if safe_path.endswith((".md", ".txt", ".py", ".yaml", ".yml",
                                           ".json", ".toml", ".cfg", ".ini")):
                        try:
                            files[safe_path] = content.decode("utf-8")
                        except UnicodeDecodeError:
                            files[safe_path] = content
                    else:
                        files[safe_path] = content
        except tarfile.TarError as e:
            raise InvalidSkillFileError(f"corrupt tar.gz: {e}")

        if "SKILL.md" not in files:
            raise InvalidSkillFileError("tar.gz bundle missing SKILL.md at root")
        skill_md_bytes = files["SKILL.md"]
        if isinstance(skill_md_bytes, str):
            skill_md_bytes = skill_md_bytes.encode("utf-8")
        name = _resolve_skill_name(_extract_name_from_frontmatter(skill_md_bytes), fb_name)

    elif fmt == SkillFileFormat.ZIP:
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    safe_path = _validate_bundle_rel_path(info.filename)
                    content = zf.read(info)
                    if safe_path.endswith((".md", ".txt", ".py", ".yaml", ".yml",
                                           ".json", ".toml", ".cfg", ".ini")):
                        try:
                            files[safe_path] = content.decode("utf-8")
                        except UnicodeDecodeError:
                            files[safe_path] = content
                    else:
                        files[safe_path] = content
        except zipfile.BadZipFile as e:
            raise InvalidSkillFileError(f"corrupt zip: {e}")

        if "SKILL.md" not in files:
            raise InvalidSkillFileError("zip bundle missing SKILL.md at root")
        skill_md_bytes = files["SKILL.md"]
        if isinstance(skill_md_bytes, str):
            skill_md_bytes = skill_md_bytes.encode("utf-8")
        name = _resolve_skill_name(_extract_name_from_frontmatter(skill_md_bytes), fb_name)

    else:
        raise InvalidSkillFileError(f"unsupported format: {fmt}")

    return SkillBundle(
        name=name,
        files=files,
        source=f"gateway:{platform}",
        identifier=f"{platform}:{sender_id}",
        trust_level="community",
        metadata={"sender_id": sender_id, "platform": platform},
    )


# ---------------------------- authorization ----------------------------


class SkillInstallAuthorizer:
    """Default-deny gate. Reads `skill_install.allowed_senders` from config
    OR accepts an explicit allowlist set. Honors HERMES_SKILL_INSTALL_ALLOW_ALL=1
    (with explicit logging) for development overrides."""

    def __init__(self, allowlist: Iterable[str] = ()):
        self.allowlist = {s for s in allowlist if s}

    def is_authorized(self, sender_id: Optional[str]) -> bool:
        if not sender_id:
            return False  # never accept empty sender, even with allow-all
        if os.environ.get("HERMES_SKILL_INSTALL_ALLOW_ALL") == "1":
            logger.warning(
                "[skill_install] HERMES_SKILL_INSTALL_ALLOW_ALL=1 — allowing %s",
                sender_id,
            )
            return True
        return sender_id in self.allowlist


def _load_allowlist_from_config() -> set[str]:
    """Read skill_install.allowed_senders from ~/.hermes/config.yaml.

    Returns an empty set if config missing or key absent (default-deny).
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("skill_install", {}) or {}
        senders = section.get("allowed_senders", []) or []
        return {str(s) for s in senders if s}
    except Exception as e:
        logger.warning("[skill_install] could not load allowlist from config: %s", e)
        return set()


# ---------------------------- orchestrator ----------------------------


def _format_user_message(report: InstallReport) -> str:
    """Render a human-readable status string for gateway delivery.

    Production scanner verdicts are: 'safe' | 'caution' | 'dangerous'.
    We surface them through gateway-friendly emoji + summary.
    """
    if report.verdict == "safe":
        return f"✅ Installed skill '{report.skill_name}' (path: {report.install_path})"
    if report.verdict == "caution":
        return (
            f"⚠️ Installed skill '{report.skill_name}' with warnings.\n"
            f"Scan summary: {report.scan_summary}\n"
            f"Path: {report.install_path}"
        )
    if report.verdict == "dangerous":
        return (
            f"❌ Skill '{report.skill_name or 'unknown'}' BLOCKED by security scanner.\n"
            f"Reason: {report.scan_summary or report.error}\n"
            f"See ~/.hermes/skills/.audit.log for details."
        )
    if report.verdict == "unauthorized":
        return (
            "🔒 Skill install rejected: sender not authorized. "
            "Add your sender ID to skill_install.allowed_senders in config."
        )
    if report.verdict == "invalid_format":
        return f"❌ Not a valid .skill file: {report.error}"
    if report.verdict == "too_large":
        return f"❌ Skill file too large (max {MAX_PAYLOAD_BYTES // (1024*1024)} MB)"
    return f"❌ Skill install failed: {report.error or 'unknown error'}"


def install_skill_file(
    payload: bytes,
    filename: str,
    sender_id: str,
    *,
    platform: str = "unknown",
    force: bool = False,
    allowlist: Optional[Iterable[str]] = None,
) -> InstallReport:
    """Run the full .skill install pipeline. Never raises — returns InstallReport.

    Args:
        payload: raw bytes of the .skill file
        filename: original filename (used for fallback name extraction)
        sender_id: platform-native sender ID (e.g. Telegram chat_id, Discord user_id)
        platform: gateway platform name ('telegram', 'discord', 'nats', ...)
        force: bypass scanner verdict (only honored for programmatic callers — gateway
               adapters MUST pass force=False)
        allowlist: optional explicit allowlist (overrides config); used by tests
    """
    report = InstallReport(verdict="error", sender_id=sender_id, platform=platform)

    # 1. Authorization
    effective_allowlist = (
        set(allowlist) if allowlist is not None else _load_allowlist_from_config()
    )
    auth = SkillInstallAuthorizer(allowlist=effective_allowlist)
    if not auth.is_authorized(sender_id):
        report.verdict = "unauthorized"
        report.error = f"sender {sender_id!r} not in allowlist"
        report.user_message = _format_user_message(report)
        logger.warning(
            "[skill_install] DENY unauthorized sender=%s platform=%s filename=%s",
            sender_id, platform, filename,
        )
        return report

    # 2. Size limit
    if len(payload) > MAX_PAYLOAD_BYTES:
        report.verdict = "too_large"
        report.error = f"payload size {len(payload)} > limit {MAX_PAYLOAD_BYTES}"
        report.user_message = _format_user_message(report)
        return report

    # 3. Format sniff
    try:
        fmt = detect_format(payload)
    except InvalidSkillFileError as e:
        report.verdict = "invalid_format"
        report.error = str(e)
        report.user_message = _format_user_message(report)
        return report

    # 4. Bundle build
    try:
        bundle = build_bundle(
            payload, fmt,
            fallback_name=filename,
            sender_id=sender_id,
            platform=platform,
        )
    except (InvalidSkillFileError, ValueError) as e:
        report.verdict = "invalid_format"
        report.error = str(e)
        report.user_message = _format_user_message(report)
        return report

    report.skill_name = bundle.name

    # 5-8. Quarantine → scan → verdict gate → install
    try:
        quarantine_path = quarantine_bundle(bundle)
        scan_result = scan_skill(quarantine_path, source=f"{platform}:{sender_id}")
        report.scan_summary = getattr(scan_result, "summary", "") or ""

        allowed, reason = should_allow_install(scan_result, force=force)
        if not allowed:
            # Propagate the scanner verdict (typically 'dangerous') so callers
            # see the canonical security-scanner taxonomy. The user_message
            # formatter renders this into a human-readable refusal.
            report.verdict = getattr(scan_result, "verdict", "dangerous") or "dangerous"
            report.error = reason or "blocked by scanner"
            report.user_message = _format_user_message(report)
            logger.warning(
                "[skill_install] BLOCK skill=%s sender=%s verdict=%s reason=%s",
                bundle.name, sender_id, report.verdict, reason,
            )
            return report

        install_path = install_from_quarantine(
            quarantine_path,
            skill_name=bundle.name,
            category="",
            bundle=bundle,
            scan_result=scan_result,
        )
        report.install_path = str(install_path)
        report.verdict = getattr(scan_result, "verdict", "pass")
        report.user_message = _format_user_message(report)
        logger.info(
            "[skill_install] INSTALL skill=%s sender=%s verdict=%s path=%s",
            bundle.name, sender_id, report.verdict, install_path,
        )
        return report

    except Exception as e:
        report.verdict = "error"
        report.error = f"install failure: {e}"
        report.user_message = _format_user_message(report)
        logger.exception("[skill_install] unexpected failure for sender=%s", sender_id)
        return report
