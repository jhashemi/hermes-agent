"""DB-layer ghost-assignee guard in ``kanban_db.create_task`` (t_15f3ec29).

The kanban_create *tool* already refuses unknown assignees (see
``tests/tools/test_kanban_tools.py::test_create_refuses_unknown_assignee``),
but direct DB inserts bypass the tool layer:

* dashboard HTTP ``POST /tasks``
* ``hermes kanban create`` CLI subcommand
* ``kanban_repository_facade`` (used by hermes-kanban plugin surfaces)
* ``clarify_tool`` (auto-spawned clarify tasks)
* ``kanban_swarm`` / decompose helpers that call ``create_task`` directly

Three ghost-assignee stalls in the same week (``orchestrator``,
``cc-deployment-expert``, ``cc-knowledge-architect``) came in through
those insert paths — the tool-layer check never fired. This module
verifies that ``kanban_db.create_task`` itself now refuses ghosts, that
the ``HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE`` escape hatch works, and
that the dashboard endpoint surfaces a proper HTTP 400.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def strict_env(monkeypatch, tmp_path):
    """Isolate HERMES_HOME and turn OFF the top-level conftest's
    ``HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE=1`` autouse escape hatch."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_KANBAN_VIRTUAL_ASSIGNEES", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_create_task_refuses_ghost_profile(strict_env):
    """``create_task`` at the DB layer rejects unknown assignees.

    ``ValueError`` (not a structured dict) is the right signal here:
    the tool layer returns a JSON error, the dashboard endpoint maps
    ValueError→400, and the CLI wrapper catches ValueError and prints
    a clean rc=2 error line. One raise, three surface treatments.
    """
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="unknown_assignee"):
            kb.create_task(
                conn,
                title="dispatched to ghost",
                assignee="cc-deployment-expert",  # not a profile, not virtual
            )
        # Row must not have been written.
        rows = conn.execute(
            "SELECT id FROM tasks WHERE title = ?", ("dispatched to ghost",)
        ).fetchall()
        assert rows == []
    finally:
        conn.close()


def test_create_task_accepts_real_profile(strict_env):
    """A profile directory on disk under HERMES_HOME/profiles satisfies
    the guard."""
    from hermes_cli import kanban_db as kb

    profiles_root = strict_env / "profiles"
    profiles_root.mkdir(parents=True, exist_ok=True)
    (profiles_root / "researcher-a").mkdir()

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="dispatched to a real profile",
            assignee="researcher-a",
        )
        assert task_id
    finally:
        conn.close()


def test_create_task_accepts_registered_virtual_assignee(monkeypatch, strict_env):
    """A name in the virtual-assignee registry passes even without a
    profile directory on disk (this is how ``livekit-boardroom`` and
    other route-through primitives work)."""
    from hermes_cli import kanban_db as kb

    registry = strict_env / "kanban" / "virtual_assignees.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("livekit-boardroom:\n  handler: boardroom-driver\n")
    monkeypatch.setenv("HERMES_KANBAN_VIRTUAL_ASSIGNEES", str(registry))

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="route to real handler",
            assignee="livekit-boardroom",
        )
        assert task_id
    finally:
        conn.close()


def test_create_task_accepts_default_profile(strict_env):
    """``default`` is always a known profile (see profile_exists)."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="default profile task",
            assignee="default",
        )
        assert task_id
    finally:
        conn.close()


def test_escape_hatch_bypasses_guard(monkeypatch, strict_env):
    """``HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE=1`` skips the guard.

    Only used by migrations, replaying corrupt boards, and the test
    suite itself. Documented in the DB-layer code and the migration
    runbook.
    """
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE", "1")
    conn = kb.connect()
    try:
        # Would fail without the escape hatch.
        task_id = kb.create_task(
            conn,
            title="migration replay",
            assignee="legacy-ghost-profile",
        )
        assert task_id
        # And the assignee actually landed.
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "legacy-ghost-profile"
    finally:
        conn.close()


def test_escape_hatch_empty_string_does_not_bypass(monkeypatch, strict_env):
    """An empty-string value for the escape hatch is treated as unset —
    same convention as every other tri-state env var in Hermes."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE", "   ")
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="unknown_assignee"):
            kb.create_task(
                conn,
                title="still ghost",
                assignee="another-ghost",
            )
    finally:
        conn.close()


def test_empty_assignee_still_allowed_at_db_layer(strict_env):
    """None / empty ``assignee`` is not the guard's job — the tool layer
    already refuses it with a dedicated ``assignee is required`` error.
    Enforcing here too would break legitimate paths that create
    unassigned rows on purpose (triage cards, dashboard drafts)."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="unassigned by design",
            assignee=None,
        )
        assert task_id
    finally:
        conn.close()


def test_guard_message_includes_assignee_and_hint(strict_env):
    """The ``ValueError`` message must name the offending assignee and
    the escape hatch so operators can debug from the log line alone —
    no need to grep source or open a Python REPL."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        with pytest.raises(ValueError) as exc_info:
            kb.create_task(
                conn,
                title="broken",
                assignee="cc-knowledge-architect",
            )
        msg = str(exc_info.value)
        assert "cc-knowledge-architect" in msg
        assert "virtual_assignees" in msg or "profile" in msg
        assert "HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE" in msg
    finally:
        conn.close()


def test_dashboard_endpoint_returns_400_on_ghost_assignee(monkeypatch, strict_env):
    """The dashboard's ``POST /tasks`` maps the DB-layer ``ValueError``
    to HTTP 400 with the guard's message intact.

    Historically this endpoint would either 500 (uncaught) or silently
    create a ghost row that stalled forever. Since the guard is at the
    DB layer, the endpoint's existing ``except ValueError`` block
    surfaces the failure the right way with no code change on the
    dashboard side.
    """
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router as kanban_router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(kanban_router)
    client = TestClient(app)

    resp = client.post(
        "/tasks",
        json={
            "title": "ghost via dashboard",
            "assignee": "orchestrator",  # canonical stall assignee
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.text
    assert "unknown_assignee" in body
    assert "orchestrator" in body
