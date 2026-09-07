"""RED tests for the .skill file install pipeline (ADR-009).

Covers format detection, bundle building, authorization, and the full
install_skill_file() orchestration with mocked scanner.
"""
from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools.skill_file_install import (
    InstallReport,
    InvalidSkillFileError,
    SkillFileFormat,
    SkillInstallAuthorizer,
    build_bundle,
    detect_format,
    install_skill_file,
)


# ---------------------------- format detection ----------------------------

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
            detect_format(b"this is just plain text without frontmatter")

    def test_empty_rejected(self):
        with pytest.raises(InvalidSkillFileError):
            detect_format(b"")

    def test_yaml_without_name_rejected(self):
        # Has frontmatter but no `name:` key — not a skill
        content = b"---\ndescription: only description\n---\n# Foo\n"
        with pytest.raises(InvalidSkillFileError):
            detect_format(content)


# ---------------------------- bundle build ----------------------------

class TestBuildBundle:
    def test_single_markdown_extracts_name_from_frontmatter(self):
        content = b"---\nname: my-skill\ndescription: a test skill\n---\n# My Skill\n"
        bundle = build_bundle(content, SkillFileFormat.SINGLE_MARKDOWN, fallback_name="ignored")
        assert bundle.name == "my-skill"
        assert "SKILL.md" in bundle.files
        assert "my-skill" in bundle.files["SKILL.md"]

    def test_single_markdown_falls_back_to_filename_when_no_name(self):
        # Frontmatter exists but `name:` was blank — use fallback
        content = b"---\ndescription: anonymous skill\nname: \n---\n# Anon\n"
        bundle = build_bundle(content, SkillFileFormat.SINGLE_MARKDOWN, fallback_name="my-fallback")
        assert bundle.name == "my-fallback"

    def test_tar_gz_extracts_files(self):
        # Build a tarball in memory: SKILL.md + references/api.md
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            skill_md = b"---\nname: bundled-skill\ndescription: test\n---\n# Bundled\n"
            info = tarfile.TarInfo("SKILL.md")
            info.size = len(skill_md)
            tf.addfile(info, io.BytesIO(skill_md))
            api = b"# API\n"
            info2 = tarfile.TarInfo("references/api.md")
            info2.size = len(api)
            tf.addfile(info2, io.BytesIO(api))
        buf.seek(0)
        bundle = build_bundle(buf.getvalue(), SkillFileFormat.TAR_GZ, fallback_name="x")
        assert bundle.name == "bundled-skill"
        assert "SKILL.md" in bundle.files
        assert "references/api.md" in bundle.files

    def test_tar_gz_traversal_rejected(self):
        # ../etc/passwd inside the archive → must be rejected
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("../etc/passwd")
            info.size = 5
            tf.addfile(info, io.BytesIO(b"hello"))
        buf.seek(0)
        with pytest.raises(ValueError):  # _validate_bundle_rel_path
            build_bundle(buf.getvalue(), SkillFileFormat.TAR_GZ, fallback_name="x")

    def test_zip_extracts_files(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", "---\nname: zipped\ndescription: test\n---\n# Zipped\n")
            zf.writestr("scripts/helper.py", "print('hello')\n")
        buf.seek(0)
        bundle = build_bundle(buf.getvalue(), SkillFileFormat.ZIP, fallback_name="x")
        assert bundle.name == "zipped"
        assert "SKILL.md" in bundle.files
        assert "scripts/helper.py" in bundle.files

    def test_zip_traversal_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", "owned")
        buf.seek(0)
        with pytest.raises(ValueError):
            build_bundle(buf.getvalue(), SkillFileFormat.ZIP, fallback_name="x")

    def test_bundle_missing_skill_md_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.md", "no SKILL.md here")
        buf.seek(0)
        with pytest.raises(InvalidSkillFileError):
            build_bundle(buf.getvalue(), SkillFileFormat.ZIP, fallback_name="x")


# ---------------------------- authorization ----------------------------

class TestSkillInstallAuthorizer:
    def test_default_empty_allowlist_denies_everyone(self, monkeypatch):
        monkeypatch.delenv("HERMES_SKILL_INSTALL_ALLOW_ALL", raising=False)
        auth = SkillInstallAuthorizer(allowlist=set())
        assert not auth.is_authorized("anyone")
        assert not auth.is_authorized("445462521")

    def test_explicit_allowlist_allows_listed_senders(self, monkeypatch):
        monkeypatch.delenv("HERMES_SKILL_INSTALL_ALLOW_ALL", raising=False)
        auth = SkillInstallAuthorizer(allowlist={"445462521", "user@example.com"})
        assert auth.is_authorized("445462521")
        assert auth.is_authorized("user@example.com")
        assert not auth.is_authorized("999999")

    def test_env_override_allows_all(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        auth = SkillInstallAuthorizer(allowlist=set())
        assert auth.is_authorized("anyone")

    def test_empty_sender_id_denied_even_with_allow_all(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        auth = SkillInstallAuthorizer(allowlist=set())
        assert not auth.is_authorized("")
        assert not auth.is_authorized(None)


# ---------------------------- full pipeline (orchestration) ----------------------------

class TestInstallSkillFile:
    """End-to-end orchestration with patched filesystem boundaries.

    Patches `quarantine_bundle`, `scan_skill`, `should_allow_install`,
    `install_from_quarantine` so we test the orchestration logic without
    touching ~/.hermes/skills/.
    """

    def _patch_pipeline(self, monkeypatch, *, scan_verdict="safe", findings_count=0):
        """Install minimal stand-ins for the four security/install primitives.

        scan_verdict ∈ {'safe', 'caution', 'dangerous'} (matches production).
        """
        from tools import skill_file_install as mod

        captured = {}

        class _FakeQuarantinePath:
            def __init__(self, name):
                self.name = name
            def __truediv__(self, other):
                return self
            def __str__(self):
                return f"/tmp/quarantine/{self.name}"

        def fake_quarantine(bundle):
            captured["quarantine_bundle"] = bundle
            return _FakeQuarantinePath(bundle.name)

        class _FakeScanResult:
            def __init__(self):
                self.verdict = scan_verdict
                self.findings = []
                self.summary = f"verdict={scan_verdict}, findings={findings_count}"
                self.skill_name = "x"

        def fake_scan(path, source="community"):
            captured["scan_path"] = path
            captured["scan_source"] = source
            return _FakeScanResult()

        def fake_should_allow(result, force=False):
            captured["force"] = force
            if result.verdict == "dangerous":
                return False, "blocked"
            return True, "ok"

        def fake_install(quarantine_path, skill_name, category, bundle, scan_result):
            captured["installed"] = True
            captured["install_name"] = skill_name
            return Path(f"/fake/skills/{skill_name}")

        monkeypatch.setattr(mod, "quarantine_bundle", fake_quarantine)
        monkeypatch.setattr(mod, "scan_skill", fake_scan)
        monkeypatch.setattr(mod, "should_allow_install", fake_should_allow)
        monkeypatch.setattr(mod, "install_from_quarantine", fake_install)
        return captured

    def test_unauthorized_sender_returns_unauthorized_verdict(self, monkeypatch):
        monkeypatch.delenv("HERMES_SKILL_INSTALL_ALLOW_ALL", raising=False)
        captured = self._patch_pipeline(monkeypatch)
        report = install_skill_file(
            payload=b"---\nname: x\ndescription: y\n---\n# X\n",
            filename="x.skill",
            sender_id="unknown_sender",
            allowlist=set(),
        )
        assert report.verdict == "unauthorized"
        assert "installed" not in captured  # never reached install

    def test_invalid_format_rejected(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        captured = self._patch_pipeline(monkeypatch)
        report = install_skill_file(
            payload=b"junk bytes",
            filename="bad.skill",
            sender_id="trusted_user",
        )
        assert report.verdict == "invalid_format"
        assert "installed" not in captured

    def test_size_limit_enforced(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        captured = self._patch_pipeline(monkeypatch)
        # 6 MB payload — over 5 MB limit
        big = b"---\nname: too-big\ndescription: x\n---\n" + (b"a" * (6 * 1024 * 1024))
        report = install_skill_file(
            payload=big,
            filename="huge.skill",
            sender_id="trusted_user",
        )
        assert report.verdict == "too_large"
        assert "installed" not in captured

    def test_pass_verdict_installs(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        captured = self._patch_pipeline(monkeypatch, scan_verdict="safe")
        report = install_skill_file(
            payload=b"---\nname: clean-skill\ndescription: clean\n---\n# Clean\n",
            filename="clean.skill",
            sender_id="trusted_user",
        )
        assert report.verdict == "safe"
        assert report.skill_name == "clean-skill"
        assert captured.get("installed") is True
        assert captured["install_name"] == "clean-skill"
        assert "Installed skill" in report.user_message or "✅" in report.user_message

    def test_warn_verdict_installs_with_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        captured = self._patch_pipeline(monkeypatch, scan_verdict="caution")
        report = install_skill_file(
            payload=b"---\nname: warn-skill\ndescription: warn\n---\n# Warn\n",
            filename="warn.skill",
            sender_id="trusted_user",
        )
        assert report.verdict == "caution"
        assert captured.get("installed") is True

    def test_block_verdict_refuses_install(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        captured = self._patch_pipeline(monkeypatch, scan_verdict="dangerous")
        report = install_skill_file(
            payload=b"---\nname: bad-skill\ndescription: bad\n---\n# Bad\n",
            filename="bad.skill",
            sender_id="trusted_user",
        )
        assert report.verdict == "dangerous"
        assert "installed" not in captured  # blocked before install
        assert "❌" in report.user_message or "BLOCKED" in report.user_message

    def test_force_flag_only_for_programmatic_callers(self, monkeypatch):
        """Gateway calls never pass force=True; explicit programmatic calls can."""
        monkeypatch.setenv("HERMES_SKILL_INSTALL_ALLOW_ALL", "1")
        captured = self._patch_pipeline(monkeypatch, scan_verdict="safe")
        # Default force=False
        install_skill_file(
            payload=b"---\nname: x\ndescription: x\n---\n# X\n",
            filename="x.skill",
            sender_id="t",
        )
        assert captured["force"] is False
