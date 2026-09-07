"""Tests for FIX-3: verification-environment contract (t_906fe15d).

Two layers:

* **Unit tests** for ``grade_verification_venv`` (pure function that
  grades a metadata dict against a ``canonical_venvs`` allowlist).
* **Integration tests** via the ``kanban_task_completing`` pre-hook
  seam, mirroring the fixture pattern from
  ``test_kanban_completion_evidence_gate.py``. Registers the plugin's
  ``completing_hook`` as a ``kanban_task_completing`` callback and
  drives ``kb.complete_task`` end-to-end.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_completion_venv import (
    VenvCheckResult,
    VenvVerdict,
    completing_hook,
    grade_verification_venv,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_kanban_completion_evidence_gate.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated kanban DB rooted at ``tmp_path/.hermes``."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _patch_invoke_hook(monkeypatch, callbacks):
    """Patch ``hermes_cli.plugins.invoke_hook`` to run *callbacks* for
    the ``kanban_task_completing`` hook and pass through empty results
    for every other hook name.

    Signature mirrors ``PluginManager.invoke_hook`` — exceptions in a
    callback are swallowed and treated as abstention.
    """
    def _fake(hook_name, **kwargs):
        if hook_name != "kanban_task_completing":
            return []
        out = []
        for cb in callbacks:
            try:
                ret = cb(**kwargs)
            except Exception:
                continue
            if ret is not None:
                out.append(ret)
        return out
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake)


CANONICAL = [
    "/home/ubuntu/executive_agents_framework/.venv/bin/python",
    "/home/ubuntu/hermes-agent/venv/bin/python",
    "/home/ubuntu/executive_agents_platform/.venv/bin/python",
]


# ---------------------------------------------------------------------------
# Unit tests — grade_verification_venv (pure function, all branches)
# ---------------------------------------------------------------------------


class TestGradeVenv:
    """Every branch of the pure grading function."""

    def test_metadata_none_is_veto_missing(self):
        r = grade_verification_venv(None, CANONICAL)
        assert r.verdict is VenvVerdict.veto_missing
        assert "not a dict" in r.reason
        assert r.value is None
        assert r.allowlist == CANONICAL
        assert r.is_veto is True

    def test_metadata_non_dict_is_veto_missing(self):
        r = grade_verification_venv("not-a-dict", CANONICAL)
        assert r.verdict is VenvVerdict.veto_missing

    def test_field_absent_is_veto_missing(self):
        r = grade_verification_venv({"other": "field"}, CANONICAL)
        assert r.verdict is VenvVerdict.veto_missing
        assert "verification_venv field missing" in r.reason
        # Reason must point workers at the docs so they can fix it.
        assert "FIX-3" in r.reason or "VFE-COMPLETE-01" in r.reason

    def test_field_none_is_veto_missing(self):
        r = grade_verification_venv({"verification_venv": None}, CANONICAL)
        assert r.verdict is VenvVerdict.veto_missing

    def test_field_non_string_is_veto_missing(self):
        r = grade_verification_venv({"verification_venv": 42}, CANONICAL)
        assert r.verdict is VenvVerdict.veto_missing
        assert "must be a string" in r.reason
        assert r.value == 42

    def test_field_empty_string_is_veto_missing(self):
        r = grade_verification_venv({"verification_venv": "   "}, CANONICAL)
        assert r.verdict is VenvVerdict.veto_missing
        assert "empty" in r.reason.lower()

    def test_field_relative_path_is_veto_missing(self):
        r = grade_verification_venv(
            {"verification_venv": "venv/bin/python"}, CANONICAL
        )
        assert r.verdict is VenvVerdict.veto_missing
        assert "ABSOLUTE" in r.reason

    def test_empty_allowlist_is_ok(self):
        """Empty allowlist → no membership check → ok.

        Enforcement is the plugin's responsibility; the pure grader must
        not false-positive when the config isn't set up.
        """
        r = grade_verification_venv(
            {"verification_venv": "/some/absolute/path/python"}, []
        )
        assert r.verdict is VenvVerdict.ok
        assert r.extras.get("empty_allowlist") is True

    def test_allowlist_hit_is_ok(self):
        r = grade_verification_venv(
            {"verification_venv": CANONICAL[1]}, CANONICAL
        )
        assert r.verdict is VenvVerdict.ok
        assert r.value == CANONICAL[1]
        assert r.is_ok is True

    def test_allowlist_miss_is_downgrade(self):
        r = grade_verification_venv(
            {"verification_venv": "/tmp/scratch-venv/bin/python"}, CANONICAL
        )
        assert r.verdict is VenvVerdict.downgrade_non_allowlist
        assert r.is_downgrade is True
        assert r.is_veto is False
        assert "NOT on the canonical allowlist" in r.reason
        assert "awaiting-verification" in r.reason
        assert r.value == "/tmp/scratch-venv/bin/python"

    def test_allowlist_normalizes_whitespace(self):
        """Config with stray whitespace / empties must not break the check."""
        messy = ["  " + CANONICAL[0] + "  ", "", "   ", CANONICAL[1]]
        r = grade_verification_venv(
            {"verification_venv": CANONICAL[0]}, messy
        )
        assert r.verdict is VenvVerdict.ok

    def test_value_carries_stripped_string_on_success(self):
        r = grade_verification_venv(
            {"verification_venv": "  " + CANONICAL[0] + "  "}, CANONICAL
        )
        assert r.verdict is VenvVerdict.ok
        assert r.value == CANONICAL[0]

    def test_verdict_enum_values(self):
        assert VenvVerdict.ok.value == "ok"
        assert VenvVerdict.veto_missing.value == "veto_missing"
        assert VenvVerdict.downgrade_non_allowlist.value == "downgrade_non_allowlist"


# ---------------------------------------------------------------------------
# Unit tests — completing_hook wrapper (enforce flag combined with grader)
# ---------------------------------------------------------------------------


class TestCompletingHook:
    """The pre-hook callback the plugin registers on ``kanban_task_completing``."""

    def test_enforce_off_abstains_regardless(self):
        r = completing_hook(
            "t_test", metadata=None, canonical_venvs=CANONICAL, enforce=False
        )
        assert r is None

    def test_enforce_off_abstains_even_when_would_veto(self):
        r = completing_hook(
            "t_test", metadata={}, canonical_venvs=CANONICAL, enforce=False,
        )
        assert r is None

    def test_enforce_on_missing_produces_veto(self):
        r = completing_hook(
            "t_test", metadata={}, canonical_venvs=CANONICAL, enforce=True,
        )
        assert isinstance(r, dict)
        assert r.get("veto") is True
        assert "verification_venv" in r.get("reason", "")
        assert r.get("source") == "vfe-complete-protocol:verification_venv"

    def test_enforce_on_ok_abstains(self):
        r = completing_hook(
            "t_test",
            metadata={"verification_venv": CANONICAL[0]},
            canonical_venvs=CANONICAL,
            enforce=True,
        )
        assert r is None

    def test_enforce_on_downgrade_abstains(self):
        """Downgrade lets completion proceed; observer hook handles the flag."""
        r = completing_hook(
            "t_test",
            metadata={"verification_venv": "/tmp/scratch/bin/python"},
            canonical_venvs=CANONICAL,
            enforce=True,
        )
        assert r is None

    def test_extra_kwargs_are_tolerated(self):
        """The seam passes many kwargs (task_id, board, assignee, summary,
        result, metadata, profile_name). The hook must accept and ignore extras."""
        r = completing_hook(
            "t_test",
            board="default",
            assignee="alice",
            profile_name="worker",
            summary="done",
            result=None,
            metadata={"verification_venv": CANONICAL[0]},
            canonical_venvs=CANONICAL,
            enforce=True,
        )
        assert r is None


# ---------------------------------------------------------------------------
# Integration tests — verify the seam actually blocks/allows based on
# metadata["verification_venv"] via the plugin callback.
# ---------------------------------------------------------------------------


def _venv_policy(canonical=CANONICAL, enforce=True):
    """Build a ``kanban_task_completing`` callback that closes over the
    allowlist + enforce flag, matching what the plugin does in prod."""
    def callback(**kwargs):
        return completing_hook(
            kwargs.get("task_id"),
            metadata=kwargs.get("metadata"),
            canonical_venvs=canonical,
            enforce=enforce,
        )
    return callback


def test_missing_verification_venv_is_vetoed(kanban_home, monkeypatch):
    """FIX-3 DoD (3): mock completion missing ``verification_venv`` → gate vetoes."""
    _patch_invoke_hook(monkeypatch, [_venv_policy()])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-missing-venv", assignee="alice")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.CompletionEvidenceRejected) as exc:
            kb.complete_task(
                conn, tid, summary="done", metadata={"artifacts": []},
            )
        assert exc.value.completing_task_id == tid
        assert "verification_venv" in exc.value.reason
        # Task stays running (not moved to done)
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_non_allowlist_verification_venv_completion_succeeds(
    kanban_home, monkeypatch
):
    """FIX-3 DoD (4): non-allowlist venv → completion proceeds (soft downgrade).

    The seam contract is binary (veto | abstain). A downgrade verdict
    abstains — the completion is durable. The ``awaiting-verification``
    bookkeeping (observer-hook comment) is a separate concern tested
    downstream. Here we assert the seam contract only.
    """
    _patch_invoke_hook(monkeypatch, [_venv_policy()])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-scratch-venv", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            summary="done in scratch venv",
            metadata={"verification_venv": "/tmp/scratch-venv/bin/python"},
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_allowlist_hit_completion_succeeds(kanban_home, monkeypatch):
    _patch_invoke_hook(monkeypatch, [_venv_policy()])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-canonical-venv", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            summary="done canonically",
            metadata={"verification_venv": CANONICAL[1]},
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_retry_after_veto_with_valid_venv_succeeds(kanban_home, monkeypatch):
    """A vetoed completion can be retried with corrected metadata.

    Regression guard mirroring the HallucinatedCardsError retry
    contract: the veto path must not leave partial state.
    """
    _patch_invoke_hook(monkeypatch, [_venv_policy()])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-retry", assignee="alice")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.CompletionEvidenceRejected):
            kb.complete_task(conn, tid, summary="attempt-1", metadata={})
        assert kb.get_task(conn, tid).status == "running"
        # Retry with valid metadata
        ok = kb.complete_task(
            conn, tid,
            summary="attempt-2 with venv",
            metadata={"verification_venv": CANONICAL[0]},
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_enforce_off_lets_missing_venv_complete(kanban_home, monkeypatch):
    """Feature flag off = zero behavior change. Guards against
    accidental enforce-on defaults after a config refactor."""
    _patch_invoke_hook(monkeypatch, [_venv_policy(enforce=False)])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-grace-period", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, summary="grace", metadata={})
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_empty_allowlist_lets_any_absolute_venv_complete(
    kanban_home, monkeypatch
):
    """Empty ``canonical_venvs`` config = shape check only.

    The pure grader returns ok, so completion proceeds. Guards the
    config-misconfiguration path from becoming an accidental hard
    block for every completion.
    """
    _patch_invoke_hook(monkeypatch, [_venv_policy(canonical=[])])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-empty-allowlist", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            summary="empty allowlist",
            metadata={"verification_venv": "/anywhere/python"},
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_veto_audit_event_captures_reason_and_source(
    kanban_home, monkeypatch
):
    """The audit-event payload written on veto must carry the plugin's
    veto reason and source label so the RCA trail is complete."""
    import json
    import sqlite3
    _patch_invoke_hook(monkeypatch, [_venv_policy()])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fix3-audit", assignee="alice")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.CompletionEvidenceRejected):
            kb.complete_task(conn, tid, summary="bad", metadata={})
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'completion_blocked_evidence' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert row is not None
        # sqlite3.Row supports index and key access
        payload_raw = row[0] if isinstance(row, (tuple, sqlite3.Row)) else row["payload"]
        payload = json.loads(payload_raw)
        assert "verification_venv" in (payload.get("reason") or "")
        assert (
            payload.get("veto_source") == "vfe-complete-protocol:verification_venv"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config loaders — surface / smoke tests. Real values are env-dependent so
# we only assert the return type and no-exception contract.
# ---------------------------------------------------------------------------


class TestConfigLoaders:
    def test_load_canonical_venvs_returns_list(self):
        from hermes_cli.kanban_completion_venv import load_canonical_venvs
        result = load_canonical_venvs()
        assert isinstance(result, list)
        # Every entry, if any, is a non-empty string.
        for entry in result:
            assert isinstance(entry, str)
            assert entry.strip() == entry
            assert entry

    def test_load_enforce_flag_returns_bool(self):
        from hermes_cli.kanban_completion_venv import (
            load_enforce_completion_venv_flag,
        )
        assert isinstance(load_enforce_completion_venv_flag(), bool)
