"""Tests for the VFE-COMPLETE-01 pre-completion veto gate.

Covers ``kanban_task_completing`` hook plumbing and its rejection path:

* No callbacks registered -> completion proceeds unchanged (default,
  backward-compatible behaviour when the plugin isn't installed).
* Callback returns ``None`` / abstains -> completion proceeds.
* Callback returns ``{"veto": True, "reason": "..."}`` -> completion
  raises ``CompletionEvidenceRejected``, task stays running, and a
  ``completion_blocked_evidence`` audit event lands.
* Callback raises -> treated as fail-open (per invoke_hook contract);
  completion proceeds.
* Retry after rejection works with corrected metadata (regression
  guard: the veto path must not leave partial state, mirroring the
  HallucinatedCardsError retry contract).
* Multiple vetoing callbacks -> reasons joined; source labels joined.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated kanban DB rooted at ``tmp_path/.hermes``."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Kanban root is resolved independently of HERMES_HOME — must be
    # pinned explicitly or the test leaks fixture cards into the
    # operator's live board. See completion-theater RCA 2026-08-22.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _patch_invoke_hook(monkeypatch, callbacks):
    """Patch ``hermes_cli.plugins.invoke_hook`` to run *callbacks* for
    the ``kanban_task_completing`` hook and pass through empty results
    for every other hook name.

    Each callback in *callbacks* is a plain ``callable(**kwargs)`` that
    returns the veto dict / None / raises.

    The signature mirrors ``PluginManager.invoke_hook``: exceptions in a
    callback are swallowed and the callback is treated as an abstention.
    Non-``None`` return values are appended to the results list.
    """
    def _fake(hook_name, **kwargs):
        if hook_name != "kanban_task_completing":
            return []
        out = []
        for cb in callbacks:
            try:
                ret = cb(**kwargs)
            except Exception:
                # Match real invoke_hook: swallow, treat as abstain.
                continue
            if ret is not None:
                out.append(ret)
        return out
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake)


# ---------------------------------------------------------------------------
# Happy path: no hooks, or abstaining hooks
# ---------------------------------------------------------------------------


def test_complete_no_hooks_registered_succeeds(kanban_home):
    """Backward compat: when no plugin is listening on
    ``kanban_task_completing``, ``complete_task`` behaves exactly as
    before this seam existed.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no-hooks", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, summary="done, no policy loaded")
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_complete_abstaining_hook_returns_none_succeeds(
    kanban_home, monkeypatch
):
    """A callback that returns ``None`` is an abstention -> completion
    proceeds. Also asserts the callback actually received the expected
    kwargs (task_id, summary, metadata) so plugin authors can rely on
    the payload shape.
    """
    captured = {}

    def _cb(**kwargs):
        captured.update(kwargs)
        return None

    _patch_invoke_hook(monkeypatch, [_cb])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="abstain", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            summary="ok",
            metadata={"artifacts": ["/tmp/x"]},
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
        # Callback saw the right payload.
        assert captured["task_id"] == tid
        assert captured["summary"] == "ok"
        assert captured["metadata"] == {"artifacts": ["/tmp/x"]}
        # profile_name is populated by the wrapper.
        assert "profile_name" in captured
    finally:
        conn.close()


def test_complete_non_veto_dict_return_is_abstain(kanban_home, monkeypatch):
    """A dict lacking truthy ``veto`` (e.g. an accidental telemetry
    return) MUST NOT block the completion. Only ``{"veto": True, ...}``
    counts as a rejection. Guards against false-positive rejections
    from observer-shaped return values.
    """
    _patch_invoke_hook(
        monkeypatch,
        [lambda **_: {"veto": False, "note": "looked ok"}],
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="non-veto-dict", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, summary="fine")
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_complete_raising_hook_is_fail_open(kanban_home, monkeypatch):
    """A callback that raises MUST NOT block completions — the veto
    seam is fail-open on plugin errors so a buggy policy plugin cannot
    wedge every task on the board. Mirrors ``invoke_hook``'s existing
    contract for observer hooks.
    """
    def _boom(**_):
        raise RuntimeError("policy plugin exploded")

    _patch_invoke_hook(monkeypatch, [_boom])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="raising-hook", assignee="alice")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, summary="ship it anyway")
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Veto path
# ---------------------------------------------------------------------------


def test_complete_veto_blocks_and_emits_audit_event(kanban_home, monkeypatch):
    """The core assertion of VFE-COMPLETE-01: a veto reply raises
    ``CompletionEvidenceRejected``, the task remains running (state
    UNCHANGED), and a ``completion_blocked_evidence`` audit event is
    durable on the task's event log.
    """
    _patch_invoke_hook(
        monkeypatch,
        [lambda **_: {
            "veto": True,
            "reason": "missing verification_evidence.commit_hashes",
            "source": "vfe-complete-protocol",
        }],
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="veto-me", assignee="alice")
        kb.claim_task(conn, tid)

        with pytest.raises(kb.CompletionEvidenceRejected) as excinfo:
            kb.complete_task(
                conn, tid,
                summary="claiming done without evidence",
                metadata={"tests_run": 42},  # deliberately missing artifacts etc.
            )

        # Exception carries structured reason + source.
        err = excinfo.value
        assert "commit_hashes" in err.reason
        assert err.veto_source == "vfe-complete-protocol"
        assert err.completing_task_id == tid
        # It's a ValueError subclass so existing tool-error handlers catch it.
        assert isinstance(err, ValueError)

        # Task state UNCHANGED — still running, not done.
        assert kb.get_task(conn, tid).status == "running"

        # Audit event landed with the reason + source + metadata_keys.
        events = list(conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id=? ORDER BY id", (tid,),
        ))
        kinds = [r["kind"] for r in events]
        assert kinds.count("completion_blocked_evidence") == 1
        assert "completed" not in kinds

        import json as _json
        blocked = [
            _json.loads(r["payload"])
            for r in events if r["kind"] == "completion_blocked_evidence"
        ][0]
        assert "commit_hashes" in blocked["reason"]
        assert blocked["veto_source"] == "vfe-complete-protocol"
        assert blocked["metadata_keys"] == ["tests_run"]
    finally:
        conn.close()


def test_complete_retry_after_veto_succeeds(kanban_home, monkeypatch):
    """Regression guard: after a rejection, the worker MUST be able to
    retry ``kanban_complete`` with corrected metadata and land the
    completion. Mirrors #22923 semantics for HallucinatedCardsError.

    Uses a stateful callback that vetoes only when
    ``metadata['artifacts']`` is absent — the second call satisfies the
    policy and passes.
    """
    def _policy(**kwargs):
        md = kwargs.get("metadata") or {}
        if not md.get("artifacts"):
            return {
                "veto": True,
                "reason": "artifacts field is required",
                "source": "test-policy",
            }
        return None

    _patch_invoke_hook(monkeypatch, [_policy])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry-after-veto", assignee="alice")
        kb.claim_task(conn, tid)

        # First attempt: missing artifacts -> rejected.
        with pytest.raises(kb.CompletionEvidenceRejected):
            kb.complete_task(conn, tid, summary="oops", metadata={})
        assert kb.get_task(conn, tid).status == "running"

        # Retry with the fix -> lands.
        ok = kb.complete_task(
            conn, tid,
            summary="fixed",
            metadata={"artifacts": ["/tmp/report.pdf"]},
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"

        # Event log shows one rejection followed by one completion.
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (tid,),
            )
        ]
        assert kinds.count("completion_blocked_evidence") == 1
        assert kinds.count("completed") == 1
    finally:
        conn.close()


def test_complete_multiple_vetoes_are_joined(kanban_home, monkeypatch):
    """When two callbacks both return veto dicts, ``complete_task``
    surfaces both reasons in the raised error and both source labels in
    ``veto_source`` (comma-joined). Prevents plugins silently masking
    each other's diagnostics.
    """
    _patch_invoke_hook(
        monkeypatch,
        [
            lambda **_: {"veto": True, "reason": "missing artifacts",
                         "source": "policy-a"},
            lambda **_: {"veto": True, "reason": "missing commit_hashes",
                         "source": "policy-b"},
        ],
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="two-vetoes", assignee="alice")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.CompletionEvidenceRejected) as excinfo:
            kb.complete_task(conn, tid, summary="no evidence")
        err = excinfo.value
        assert "missing artifacts" in err.reason
        assert "missing commit_hashes" in err.reason
        assert "policy-a" in (err.veto_source or "")
        assert "policy-b" in (err.veto_source or "")
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_complete_veto_without_reason_uses_default(kanban_home, monkeypatch):
    """A malformed veto dict (``{"veto": True}`` with no reason) still
    rejects the completion, using a default placeholder reason. Guards
    against a plugin bug producing a silent, unactionable rejection.
    """
    _patch_invoke_hook(monkeypatch, [lambda **_: {"veto": True}])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="veto-no-reason", assignee="alice")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.CompletionEvidenceRejected) as excinfo:
            kb.complete_task(conn, tid, summary="?")
        # Default placeholder reason present.
        assert "no reason" in excinfo.value.reason.lower()
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Interaction with the sibling hallucination gate
# ---------------------------------------------------------------------------


def test_evidence_veto_fires_after_hallucination_gate(kanban_home, monkeypatch):
    """When BOTH gates would reject, the hallucination gate fires
    first — it runs before the veto pre-hook. This test pins the order
    so future refactors don't accidentally swap them.

    Rationale: card-existence is a structural check with no plugin
    dependency; running it first makes the more expensive plugin
    callback unnecessary when there's a simpler reason to reject.
    """
    veto_called = {"count": 0}

    def _cb(**_):
        veto_called["count"] += 1
        return {"veto": True, "reason": "policy blocks it too"}

    _patch_invoke_hook(monkeypatch, [_cb])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="both-gates", assignee="alice")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, tid,
                summary="claims phantom card",
                created_cards=["t_phantomdeadbeef"],
            )
        # Veto hook was NOT reached — hallucination gate short-circuited.
        assert veto_called["count"] == 0
        assert kb.get_task(conn, tid).status == "running"

        # Now clear the phantom claim: the veto hook DOES fire.
        with pytest.raises(kb.CompletionEvidenceRejected):
            kb.complete_task(
                conn, tid,
                summary="no phantoms this time",
                created_cards=[],
            )
        assert veto_called["count"] == 1
    finally:
        conn.close()
