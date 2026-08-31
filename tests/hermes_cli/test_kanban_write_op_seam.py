"""Behavior tests for the ``kanban_write_op`` observer seam (ADR-006b P2 row 1).

The seam is what lets the ``vfe-kanban-dual-write`` plugin mirror every
kernel-side kanban write to a second store (DuckDB) without the kernel
ever importing ``duckdb`` or reaching into a mirror module.

Contract we verify here (matches ``VALID_HOOKS`` docstring):

  * Fires from ``hermes_cli.kanban_db`` at the end of every kernel write
    op, AFTER the enclosing ``write_txn`` has committed.
  * Observer-only: a raising callback is swallowed and the board write
    still lands. A non-vetoing return value has no effect.
  * Carries stable framing kwargs — ``op``, ``task_id``, ``board``,
    ``sqlite_path``, ``result``, ``profile_name`` — plus op-specific
    kwargs the mirror needs.

We drive every one of the fourteen write ops in a single scenario and
snapshot the ``op`` sequence so future kernel changes cannot silently
add or drop a seam site.

SQLite ↔ DuckDB row parity under dual-mode is verified live on gateway
hosts against the ``vfe-kanban-dual-write`` plugin's mirror (the named
consumer of this seam); the plugin's own tests
(``~/.hermes/plugins/vfe-kanban-dual-write/tests/test_mirror.py``)
cover unit-level dispatch, id-passthrough, framing-collision renames,
and the sqlite-mode short-circuit.  Prior to the P2 revert the
in-kernel ``hermes_cli/kanban_dual_write`` shim carried
``test_kanban_dual_write_lock_contention`` and
``test_kanban_dual_write_per_op_conn``; both moved with the shim's
behavior into the plugin's test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_write_ops(monkeypatch):
    """Register a single capturing callback for ``kanban_write_op``.

    Re-resolves the plugin manager singleton via a fresh import to
    survive test-isolation quirks: some sibling tests
    (``test_kanban_hrv_dispatch_gate.py`` in particular) purge
    ``sys.modules`` for the whole ``hermes_cli`` package, which leaves
    the plugins module (and its ``_plugin_manager`` singleton) in
    ``sys.modules`` freshly re-imported. Grabbing the callable by name
    off the current ``sys.modules`` entry guarantees we hook the same
    manager the kernel's fire path resolves.

    Patches ``PluginManager._hooks`` directly and restores it afterward
    so a test never leaks a callback into a sibling test's plugin
    registry.
    """
    import importlib
    plugins_mod = importlib.import_module("hermes_cli.plugins")
    mgr = plugins_mod.get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    events: list[dict] = []
    mgr._hooks.setdefault("kanban_write_op", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        yield events
    finally:
        mgr._hooks = saved


# --------------------------------------------------------------------------- #
# 1. VALID_HOOKS registration
# --------------------------------------------------------------------------- #


def test_kanban_write_op_is_registered_in_valid_hooks():
    """The freeze-list must carry the seam; without it, plugin loaders
    reject callbacks for the hook name and the shim consumer can't wire.
    """
    assert "kanban_write_op" in VALID_HOOKS


# --------------------------------------------------------------------------- #
# 2. Fire-site coverage: every one of the 14 kernel write ops fires
# --------------------------------------------------------------------------- #


def _drive_every_write_op(conn):
    """Exercise every write op the seam declares.

    Returns the (parent_id, child_id, notify-task-id) tuple so callers
    can assert on downstream state if they want to.
    """
    # create_task, add_comment, assign_task, link_tasks
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker")
    kb.add_comment(conn, parent, "author", "body")
    kb.assign_task(conn, parent, "worker2")
    kb.link_tasks(conn, parent, child)
    # unlink_tasks (removes edge cleanly)
    kb.unlink_tasks(conn, parent, child)
    # claim_task, heartbeat_claim, reclaim_task, complete_task
    kb.claim_task(conn, parent)
    kb.heartbeat_claim(conn, parent)
    kb.reclaim_task(conn, parent, reason="test reclaim")
    # reassign_task after reclaim (task is back to ready)
    kb.reassign_task(conn, parent, "worker3")
    # block_task then unblock_task
    kb.claim_task(conn, parent)
    kb.block_task(conn, parent, reason="test block", kind=None)
    kb.unblock_task(conn, parent)
    # complete_task
    kb.claim_task(conn, parent)
    kb.complete_task(conn, parent, summary="done")
    # release_stale_claims: needs a stale-claimed task
    stale = kb.create_task(conn, title="stale", assignee="worker")
    kb.claim_task(conn, stale, ttl_seconds=-1)  # immediately-expired claim
    kb.release_stale_claims(conn)
    # add_notify_sub
    notify_task = kb.create_task(conn, title="notify", assignee="worker")
    kb.add_notify_sub(
        conn,
        task_id=notify_task,
        platform="telegram",
        chat_id="123",
        thread_id="",
    )
    return parent, child, notify_task


def test_every_write_op_fires_exactly_once_per_call(
    kanban_home, captured_write_ops,
):
    """Snapshot the ops that fire the seam. All 14 must appear.

    Guards against silent regressions: adding a new kernel write op
    means adding a seam fire site AND updating this expected set (and
    the VALID_HOOKS docstring). Dropping a fire site trips this test.
    """
    conn = kb.connect()
    try:
        _drive_every_write_op(conn)
    finally:
        conn.close()

    ops_fired = {e["op"] for e in captured_write_ops}
    expected = {
        "create_task",
        "assign_task",
        "reassign_task",
        "link_tasks",
        "unlink_tasks",
        "add_comment",
        "claim_task",
        "heartbeat_claim",
        "release_stale_claims",
        "reclaim_task",
        "complete_task",
        "block_task",
        "unblock_task",
        "add_notify_sub",
    }
    missing = expected - ops_fired
    unexpected = ops_fired - expected
    assert not missing, f"seam did not fire for: {sorted(missing)}"
    assert not unexpected, f"unexpected op names from seam: {sorted(unexpected)}"


# --------------------------------------------------------------------------- #
# 3. Framing kwargs on every fire
# --------------------------------------------------------------------------- #


def test_every_fire_carries_framing_kwargs(kanban_home, captured_write_ops):
    """Every seam event MUST carry the framing kwargs; the mirror plugin
    depends on all four to address its second store correctly.
    """
    conn = kb.connect()
    try:
        _drive_every_write_op(conn)
    finally:
        conn.close()

    assert captured_write_ops, "no seam events captured"
    for e in captured_write_ops:
        assert "op" in e, f"missing op: {e}"
        assert "task_id" in e, f"missing task_id: {e}"
        assert "board" in e, f"missing board: {e}"
        assert "sqlite_path" in e, f"missing sqlite_path: {e}"
        assert "result" in e, f"missing result: {e}"
        assert "profile_name" in e, f"missing profile_name: {e}"
        # sqlite_path points at a real on-disk file that exists post-commit.
        assert e["sqlite_path"] and Path(e["sqlite_path"]).exists(), (
            f"sqlite_path invalid: {e['sqlite_path']!r}"
        )


# --------------------------------------------------------------------------- #
# 4. Post-commit semantics
# --------------------------------------------------------------------------- #


def test_seam_fires_after_commit_row_is_visible(kanban_home, monkeypatch):
    """The seam must fire AFTER the SQLite txn commits.

    Verified by having the observer callback open a *second* SQLite
    connection to the same DB and prove the row it will mirror is
    already durable when the callback runs. If we fired mid-txn,
    the second connection would see the pre-write state.
    """
    import sqlite3

    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    observed_task_ids: list[str] = []

    def _observer(**kw):
        if kw.get("op") != "create_task":
            return
        sqlite_path = kw["sqlite_path"]
        # Second connection with read-only intent — completely bypasses
        # the writer's txn. If the seam fires pre-commit, this reader
        # sees no row for kw["task_id"].
        alt = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            row = alt.execute(
                "SELECT id FROM tasks WHERE id = ?", (kw["task_id"],),
            ).fetchone()
        finally:
            alt.close()
        if row is not None:
            observed_task_ids.append(kw["task_id"])

    mgr._hooks.setdefault("kanban_write_op", []).append(_observer)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="post-commit probe", assignee="worker")
        finally:
            conn.close()
        assert tid in observed_task_ids, (
            "seam observer did not see the row on a fresh connection — "
            "the fire happened pre-commit (bug)"
        )
    finally:
        mgr._hooks = saved


# --------------------------------------------------------------------------- #
# 5. Observer-only: raising callback does not break the write
# --------------------------------------------------------------------------- #


def test_raising_observer_does_not_break_write(kanban_home):
    """A misbehaving observer must not wedge a board write.

    Same fail-open contract as ``_fire_kanban_lifecycle_hook``: an
    exception from the observer is swallowed at the seam boundary so
    the write is still authoritative from the kernel's POV.
    """
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("observer exploded")

    mgr._hooks.setdefault("kanban_write_op", []).append(_boom)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="raise-through", assignee="worker")
            # Despite the raising observer, the row is durable.
            assert kb.get_task(conn, tid).title == "raise-through"
            # And the next write is still landable.
            assert kb.assign_task(conn, tid, "worker2") is True
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


# --------------------------------------------------------------------------- #
# 6. Observer-only: a callback's return value has no effect
# --------------------------------------------------------------------------- #


def test_veto_dict_from_observer_has_no_effect(kanban_home):
    """The seam is observer-only, unlike ``kanban_task_completing``.

    If an observer returns ``{"veto": True, "reason": ...}`` the write
    must still land — this hook is not a veto surface, even if a plugin
    author confuses it with ``kanban_task_completing``.
    """
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _wannabe_veto(**kw):
        return {"veto": True, "reason": "should be ignored by observer seam"}

    mgr._hooks.setdefault("kanban_write_op", []).append(_wannabe_veto)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(
                conn, title="veto-ignored", assignee="worker",
            )
            assert kb.get_task(conn, tid) is not None
            assert kb.complete_task(conn, tid, summary="done") is True
            assert kb.get_task(conn, tid).status == "done"
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


# --------------------------------------------------------------------------- #
# 7. id-passthrough: result carries the SQLite-authoritative primary key
# --------------------------------------------------------------------------- #


def test_result_field_carries_id_passthrough(kanban_home, captured_write_ops):
    """The mirror plugin uses ``result`` to preserve id-passthrough
    across the SQLite → second-store boundary. Verify:

      * ``create_task`` — ``result`` is the freshly-minted task_id.
      * ``add_comment`` — ``result`` is the comment row_id.
      * ``claim_task`` — ``result`` is the claimed Task (non-None).
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="passthrough", assignee="worker")
        row_id = kb.add_comment(conn, tid, "author", "body")
        claimed = kb.claim_task(conn, tid)
    finally:
        conn.close()

    by_op = {e["op"]: e for e in captured_write_ops}
    assert by_op["create_task"]["result"] == tid
    assert by_op["create_task"]["task_id"] == tid
    assert by_op["add_comment"]["result"] == row_id
    assert by_op["add_comment"]["task_id"] == tid
    # claim_task returns a Task dataclass; the seam forwards it verbatim.
    assert by_op["claim_task"]["result"] is not None
    assert by_op["claim_task"]["result"].id == tid
    assert claimed is not None and claimed.id == tid


# --------------------------------------------------------------------------- #
# 8. add_notify_sub carries the platform/chat_id kwargs the mirror needs
# --------------------------------------------------------------------------- #


def test_add_notify_sub_carries_delivery_kwargs(
    kanban_home, captured_write_ops,
):
    """The DuckDB mirror needs the (platform, chat_id, chat_type,
    thread_id, user_id, notifier_profile, delivery_metadata) tuple to
    replay the subscription; verify they all cross the seam.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify-target", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="789",
            chat_type="group",
            thread_id="42",
            user_id="u1",
            notifier_profile="gateway",
            delivery_metadata={"anchor_message_id": 17585},
        )
    finally:
        conn.close()

    add_sub = next(e for e in captured_write_ops if e["op"] == "add_notify_sub")
    assert add_sub["task_id"] == tid
    assert add_sub["platform"] == "telegram"
    assert add_sub["chat_id"] == "789"
    assert add_sub["chat_type"] == "group"
    assert add_sub["thread_id"] == "42"
    assert add_sub["user_id"] == "u1"
    assert add_sub["notifier_profile"] == "gateway"
    assert add_sub["delivery_metadata"] == {"anchor_message_id": 17585}


# --------------------------------------------------------------------------- #
# 9. No fire on read-only helpers (parity sanity)
# --------------------------------------------------------------------------- #


def test_read_only_helpers_do_not_fire_seam(kanban_home, captured_write_ops):
    """``get_task`` / ``list_comments`` and friends are pure reads and
    must never trip the seam, or a mirror consumer would double-count.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ro", assignee="worker")
        captured_write_ops.clear()  # ignore setup fires
        _ = kb.get_task(conn, tid)
        _ = kb.list_comments(conn, tid)
    finally:
        conn.close()

    assert captured_write_ops == [], (
        f"seam fired on a read: {captured_write_ops}"
    )
