"""Tests for VFE-COMPLETE-01: mandatory completion protocol as kanban_complete
primitive — structured metadata schema, server-side verification pass, and
the grace-period heuristic.

Covers:
- CompletionVerificationError raised on bad artifacts, bad commits, inactive
  services, and unverified cross-host propagation (strict mode ON).
- Grace-period warning comment emitted (not refused) when summary claims work
  but no structured metadata is present (strict mode OFF).
- Good structured metadata passes in both modes.
- Feature flag OFF: no verification run, no warning when no claim words.
- Feature flag ON: verification passes when all checks succeed.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> str:
    """Create a git repo, make a commit, and return the commit hash."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init commit"],
        check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _make_task(conn, assignee="worker-a"):
    """Create and claim a task so it's in 'running' state for completion."""
    tid = kb.create_task(
        conn,
        title="test task",
        body="test body",
        assignee=assignee,
        created_by="test",
    )
    kb.claim_task(conn, tid, claimer="test-claimer")
    return tid


def _strict_on():
    """Context manager / patcher that enables strict verification."""
    return mock.patch.object(
        kb, "_load_complete_strict_verification", return_value=True,
    )


# ---------------------------------------------------------------------------
# Helper unit tests (pure functions, no DB)
# ---------------------------------------------------------------------------

def test_has_structured_verification_detects_artifacts():
    assert kb._has_structured_verification({"artifacts": ["/tmp/x"]}) is True


def test_has_structured_verification_detects_commit_hashes():
    assert kb._has_structured_verification(
        {"commit_hashes": [{"hash": "abc"}]}
    ) is True


def test_has_structured_verification_detects_deployed_services():
    assert kb._has_structured_verification(
        {"deployed_services": [{"unit": "foo"}]}
    ) is True


def test_has_structured_verification_detects_cross_host():
    assert kb._has_structured_verification(
        {"cross_host_propagation": [{"artifact": "/x"}]}
    ) is True


def test_has_structured_verification_detects_evidence():
    assert kb._has_structured_verification(
        {"verification_evidence": {"git_log": "abc"}}
    ) is True


def test_has_structured_verification_none():
    assert kb._has_structured_verification(None) is False


def test_has_structured_verification_empty():
    assert kb._has_structured_verification({}) is False


def test_has_structured_verification_empty_lists():
    assert kb._has_structured_verification(
        {"artifacts": [], "commit_hashes": []}
    ) is False


def test_has_structured_verification_irrelevant_keys():
    assert kb._has_structured_verification(
        {"changed_files": ["a.py"], "tests_run": 5}
    ) is False


def test_summary_claims_work_created():
    assert kb._summary_claims_work("I created a new file", None) is True


def test_summary_claims_work_committed():
    assert kb._summary_claims_work(None, "committed the change") is True


def test_summary_claims_work_deployed():
    assert kb._summary_claims_work("deployed the service", "") is True


def test_summary_claims_work_shipped():
    assert kb._summary_claims_work("shipped the feature", None) is True


def test_summary_claims_work_pushed():
    assert kb._summary_claims_work("pushed to origin", None) is True


def test_summary_claims_work_no_claim():
    assert kb._summary_claims_work("read the docs", "finished analysis") is False


def test_summary_claims_work_empty():
    assert kb._summary_claims_work(None, None) is False


# ---------------------------------------------------------------------------
# Artifact verification (Part 3 check 1)
# ---------------------------------------------------------------------------

def test_verify_artifacts_pass_existing(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("data")
    failures: list[str] = []
    kb._verify_completion_artifacts(
        {"artifacts": [str(f)]}, failures=failures,
    )
    assert failures == []


def test_verify_artifacts_fails_missing():
    failures: list[str] = []
    kb._verify_completion_artifacts(
        {"artifacts": ["/nonexistent/path/abc.py"]}, failures=failures,
    )
    assert len(failures) == 1
    assert "does not exist" in failures[0]


def test_verify_artifacts_skips_non_list():
    failures: list[str] = []
    kb._verify_completion_artifacts(
        {"artifacts": "not-a-list"}, failures=failures,
    )
    assert failures == []


def test_verify_artifacts_skips_empty_entries():
    failures: list[str] = []
    kb._verify_completion_artifacts(
        {"artifacts": ["", "  "]}, failures=failures,
    )
    assert failures == []


def test_verify_artifacts_multiple_mixed(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("ok")
    failures: list[str] = []
    kb._verify_completion_artifacts(
        {"artifacts": [str(f), "/nonexistent/bad.py"]}, failures=failures,
    )
    assert len(failures) == 1
    assert "/nonexistent/bad.py" in failures[0]


# ---------------------------------------------------------------------------
# Commit verification (Part 3 check 2)
# ---------------------------------------------------------------------------

def test_verify_commits_pass_existing(tmp_path):
    repo = tmp_path / "repo"
    commit_hash = _init_git_repo(repo)
    failures: list[str] = []
    kb._verify_completion_commits(
        {"commit_hashes": [
            {"repo": str(repo), "branch": "main", "hash": commit_hash,
             "verified_on_origin": False}
        ]},
        failures=failures,
    )
    assert failures == []


def test_verify_commits_fails_bad_hash(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    failures: list[str] = []
    kb._verify_completion_commits(
        {"commit_hashes": [
            {"repo": str(repo), "branch": "main", "hash": "deadbeef",
             "verified_on_origin": False}
        ]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "deadbeef" in failures[0]


def test_verify_commits_fails_missing_hash():
    failures: list[str] = []
    kb._verify_completion_commits(
        {"commit_hashes": [
            {"repo": "", "branch": "main", "hash": "",
             "verified_on_origin": False}
        ]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "missing 'hash'" in failures[0]


def test_verify_commits_fails_non_dict_entry():
    failures: list[str] = []
    kb._verify_completion_commits(
        {"commit_hashes": ["not-a-dict"]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "not a dict" in failures[0]


def test_verify_commits_skips_non_list():
    failures: list[str] = []
    kb._verify_completion_commits(
        {"commit_hashes": "not-a-list"}, failures=failures,
    )
    assert failures == []


def test_verify_commits_no_repo_uses_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    commit_hash = _init_git_repo(repo)
    monkeypatch.chdir(str(repo))
    failures: list[str] = []
    kb._verify_completion_commits(
        {"commit_hashes": [
            {"repo": "", "branch": "main", "hash": commit_hash,
             "verified_on_origin": False}
        ]},
        failures=failures,
    )
    assert failures == []


# ---------------------------------------------------------------------------
# Service verification (Part 3 check 3)
# ---------------------------------------------------------------------------

def test_verify_services_fails_inactive():
    failures: list[str] = []
    kb._verify_completion_services(
        {"deployed_services": [
            {"host": "", "unit": "definitely-not-active-service-xyz",
             "pid": 1, "started_at": "2026-01-01T00:00:00Z"}
        ]},
        failures=failures,
    )
    assert len(failures) >= 1
    assert "not active" in failures[0]


def test_verify_services_fails_missing_unit():
    failures: list[str] = []
    kb._verify_completion_services(
        {"deployed_services": [{"host": "", "unit": ""}]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "missing 'unit'" in failures[0]


def test_verify_services_fails_non_dict_entry():
    failures: list[str] = []
    kb._verify_completion_services(
        {"deployed_services": [42]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "not a dict" in failures[0]


def test_verify_services_skips_non_list():
    failures: list[str] = []
    kb._verify_completion_services(
        {"deployed_services": "not-a-list"}, failures=failures,
    )
    assert failures == []


# ---------------------------------------------------------------------------
# Cross-host propagation verification (Part 3 check 4)
# ---------------------------------------------------------------------------

def test_verify_cross_host_fails_missing_artifact():
    failures: list[str] = []
    kb._verify_completion_cross_host(
        {"cross_host_propagation": [
            {"artifact": "", "source_host": "h1", "target_hosts": ["h2"],
             "mechanism": "rsync", "verified": True}
        ]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "missing 'artifact'" in failures[0]


def test_verify_cross_host_fails_explicit_unverified(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("ok")
    failures: list[str] = []
    kb._verify_completion_cross_host(
        {"cross_host_propagation": [
            {"artifact": str(f), "source_host": "h1",
             "target_hosts": ["h2"], "mechanism": "rsync",
             "verified": False}
        ]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "unverified" in failures[0]


def test_verify_cross_host_fails_non_dict_entry():
    failures: list[str] = []
    kb._verify_completion_cross_host(
        {"cross_host_propagation": ["bad"]},
        failures=failures,
    )
    assert len(failures) == 1
    assert "not a dict" in failures[0]


def test_verify_cross_host_skips_non_list():
    failures: list[str] = []
    kb._verify_completion_cross_host(
        {"cross_host_propagation": "nope"}, failures=failures,
    )
    assert failures == []


# ---------------------------------------------------------------------------
# _run_completion_verification integration
# ---------------------------------------------------------------------------

def test_run_completion_verification_passes_on_none():
    assert kb._run_completion_verification(None, "t_x", None) == []


def test_run_completion_verification_passes_on_empty_dict():
    assert kb._run_completion_verification(None, "t_x", {}) == []


def test_run_completion_verification_passes_on_irrelevant_metadata():
    assert kb._run_completion_verification(
        None, "t_x", {"changed_files": ["a.py"]}
    ) == []


def test_run_completion_verification_collects_multiple_failures():
    failures = kb._run_completion_verification(
        None, "t_x",
        {
            "artifacts": ["/nonexistent/a.py", "/nonexistent/b.py"],
            "deployed_services": [{"unit": "not-a-real-service-xyz"}],
        },
    )
    assert len(failures) >= 3  # 2 artifacts + 1 service


# ---------------------------------------------------------------------------
# complete_task integration: strict mode ON
# ---------------------------------------------------------------------------

def test_complete_task_strict_refuses_bad_artifact(kanban_home):
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        with _strict_on():
            with pytest.raises(kb.CompletionVerificationError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="created new module",
                    metadata={"artifacts": ["/nonexistent/xyz.py"]},
                )
            assert "does not exist" in str(exc_info.value)
        # Task stays running.
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_complete_task_strict_refuses_bad_commit(kanban_home):
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        with _strict_on():
            with pytest.raises(kb.CompletionVerificationError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="committed the fix",
                    metadata={
                        "commit_hashes": [
                            {"repo": "", "branch": "main",
                             "hash": "deadbeef",
                             "verified_on_origin": False}
                        ]
                    },
                )
            assert "deadbeef" in str(exc_info.value)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_complete_task_strict_refuses_inactive_service(kanban_home):
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        with _strict_on():
            with pytest.raises(kb.CompletionVerificationError):
                kb.complete_task(
                    conn, tid,
                    summary="deployed the service",
                    metadata={
                        "deployed_services": [
                            {"host": "", "unit": "no-such-service-abc",
                             "pid": 1, "started_at": "2026-01-01T00:00:00Z"}
                        ]
                    },
                )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_complete_task_strict_refuses_unverified_cross_host(kanban_home):
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        with _strict_on():
            with pytest.raises(kb.CompletionVerificationError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="shipped to hermes2",
                    metadata={
                        "cross_host_propagation": [
                            {"artifact": "/tmp/x", "source_host": "h1",
                             "target_hosts": ["h2"], "mechanism": "rsync",
                             "verified": False}
                        ]
                    },
                )
            assert "unverified" in str(exc_info.value)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_complete_task_strict_passes_good_metadata(kanban_home, tmp_path):
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        artifact_path = tmp_path / "deliverable.txt"
        artifact_path.write_text("result")
        with _strict_on():
            ok = kb.complete_task(
                conn, tid,
                summary="created the deliverable",
                metadata={
                    "artifacts": [str(artifact_path)],
                    "verification_evidence": {
                        "stat_output": str(artifact_path),
                    },
                },
            )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
    finally:
        conn.close()


def test_complete_task_strict_passes_good_commit(kanban_home, tmp_path):
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        repo = tmp_path / "repo"
        commit_hash = _init_git_repo(repo)
        with _strict_on():
            ok = kb.complete_task(
                conn, tid,
                summary="committed the fix",
                metadata={
                    "commit_hashes": [
                        {"repo": str(repo), "branch": "main",
                         "hash": commit_hash,
                         "verified_on_origin": False}
                    ],
                },
            )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
    finally:
        conn.close()


def test_complete_task_strict_no_metadata_no_claim_words_passes(kanban_home):
    """Strict mode with no structured metadata and no claim words in
    summary should pass (nothing to verify)."""
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        with _strict_on():
            ok = kb.complete_task(
                conn, tid,
                summary="read the documentation and wrote a summary",
            )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# complete_task integration: strict mode OFF (grace period)
# ---------------------------------------------------------------------------

def test_complete_task_grace_warns_on_claim_without_metadata(kanban_home):
    """Flag OFF + summary claims work + no structured metadata → WARNING
    comment emitted, completion proceeds."""
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        ok = kb.complete_task(
            conn, tid,
            summary="committed the new feature and deployed it",
            metadata={"changed_files": ["a.py"]},
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        # Check the warning comment was added.
        comments = kb.list_comments(conn, tid)
        warning_comments = [
            c for c in comments
            if c.author == "complete-protocol"
            and "WARNING" in (c.body or "")
        ]
        assert len(warning_comments) == 1
        assert "grace-period" in warning_comments[0].body
    finally:
        conn.close()


def test_complete_task_grace_no_warning_when_structured_metadata(kanban_home, tmp_path):
    """Flag OFF + summary claims work + structured metadata present →
    NO warning comment."""
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        f = tmp_path / "out.txt"
        f.write_text("x")
        ok = kb.complete_task(
            conn, tid,
            summary="created the file",
            metadata={"artifacts": [str(f)]},
        )
        assert ok is True
        comments = kb.list_comments(conn, tid)
        warning_comments = [
            c for c in comments
            if c.author == "complete-protocol"
        ]
        assert len(warning_comments) == 0
    finally:
        conn.close()


def test_complete_task_grace_no_warning_when_no_claim_words(kanban_home):
    """Flag OFF + summary has no claim words → NO warning comment."""
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        ok = kb.complete_task(
            conn, tid,
            summary="analyzed the data and wrote a report",
        )
        assert ok is True
        comments = kb.list_comments(conn, tid)
        warning_comments = [
            c for c in comments
            if c.author == "complete-protocol"
        ]
        assert len(warning_comments) == 0
    finally:
        conn.close()


def test_complete_task_grace_no_warning_when_metadata_has_evidence(kanban_home):
    """Flag OFF + summary claims work + metadata has verification_evidence
    → NO warning (structured verification is present)."""
    conn = kb.connect()
    try:
        tid = _make_task(conn)
        ok = kb.complete_task(
            conn, tid,
            summary="committed the change",
            metadata={
                "verification_evidence": {"git_log": "abc123"},
            },
        )
        assert ok is True
        comments = kb.list_comments(conn, tid)
        warning_comments = [
            c for c in comments
            if c.author == "complete-protocol"
        ]
        assert len(warning_comments) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature flag loading
# ---------------------------------------------------------------------------

def test_load_complete_strict_verification_defaults_false(kanban_home):
    """In a fresh HERMES_HOME with no config override, the flag defaults
    to False (grace period)."""
    assert kb._load_complete_strict_verification() is False


# ---------------------------------------------------------------------------
# CompletionVerificationError
# ---------------------------------------------------------------------------

def test_completion_verification_error_message():
    err = kb.CompletionVerificationError(
        ["fail1", "fail2"], "t_abc",
    )
    assert err.failures == ["fail1", "fail2"]
    assert err.completing_task_id == "t_abc"
    assert "2 check(s)" in str(err)
    assert "fail1" in str(err)
    assert "fail2" in str(err)


def test_completion_verification_error_is_value_error():
    err = kb.CompletionVerificationError(["f"], "t_x")
    assert isinstance(err, ValueError)


# ---------------------------------------------------------------------------
# Preamble injection (Part 1)
# ---------------------------------------------------------------------------

def test_preamble_in_kanban_guidance():
    """The MANDATORY COMPLETION PROTOCOL section is present in
    KANBAN_GUIDANCE."""
    from agent.prompt_builder import KANBAN_GUIDANCE
    assert "MANDATORY COMPLETION PROTOCOL" in KANBAN_GUIDANCE
    assert "ARTIFACTS" in KANBAN_GUIDANCE
    assert "COMMITS" in KANBAN_GUIDANCE
    assert "DEPLOYED SERVICES" in KANBAN_GUIDANCE
    assert "CROSS-HOST PROPAGATION" in KANBAN_GUIDANCE
    assert "VERIFICATION EVIDENCE" in KANBAN_GUIDANCE
    assert "complete_strict_verification" in KANBAN_GUIDANCE


# ---------------------------------------------------------------------------
# Config flag in DEFAULT_CONFIG
# ---------------------------------------------------------------------------

def test_config_flag_present_in_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    kanban_cfg = DEFAULT_CONFIG.get("kanban", {})
    assert "complete_strict_verification" in kanban_cfg
    assert kanban_cfg["complete_strict_verification"] is False