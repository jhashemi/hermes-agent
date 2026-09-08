"""Regression tests: DuckDB kanban mirror parity (t_d70258e5).

Background
----------
On 2026-09-06 ~14:48Z the DuckDB mirrors under
``~/.hermes/kanban/boards/<slug>/kanban.duckdb`` froze on task *status*
transitions while SQLite (dispatcher-authoritative) kept advancing.

RCA chain this file locks in:

1. Fork-merge 4f058fc588 re-introduced the in-kernel
   ``hermes_cli.kanban_dual_write`` install tail at the bottom of
   ``hermes_cli.kanban_db`` (the P4 revert, t_766dd11f, had removed it in
   favor of the ``kanban_write_op`` seam + ``vfe-kanban-dual-write``
   plugin). With the adapter sources later snapshotted out of the tree
   (e78553be2b), the shim's ``_load_facade`` silently failed at import
   time in every process, so kernel writes carried no mirror at all.
2. Status drift accumulated specifically for writers that bypass the
   kernel seam: raw ``UPDATE tasks SET status`` scripts
   (``kanban_writeback.py``, ``blocked_card_sweeper.py``) never emit a
   ``task_events`` row, so nothing downstream could even see the change.

Tests here assert the *seam* contract pieces that the incident broke and
would fail on the pre-fix tree:

* ``test_no_in_kernel_dual_write_shim_tail`` — the P4 end-state: the
  kernel must NOT monkey-patch its own write ops at import time (that
  path is dead weight and a second, invisible mirror once the plugin is
  also enabled). FAILS on pre-fix main where the tail is present.
* ``test_block_task_fires_write_op_seam`` — every dispatch-relevant
  transition op (block/unblock/complete/reassign/reclaim/claim) fires
  the seam so the plugin mirror sees it. FAILS on any tree where one of
  these fire sites was dropped by a merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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
    """Capture ``kanban_write_op`` seam events (mirrors the seam suite)."""
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
# 1. P4 end-state: no in-kernel dual-write shim tail (t_d70258e5 AC2/AC4)
# --------------------------------------------------------------------------- #


def test_no_in_kernel_dual_write_shim_tail():
    """The kernel must not install the in-kernel dual-write shim.

    The P4 revert (t_766dd11f) moved DuckDB mirroring out of the kernel
    into the ``vfe-kanban-dual-write`` plugin consuming the
    ``kanban_write_op`` seam. Fork-merge 4f058fc588 re-imported the
    ``hermes_cli.kanban_dual_write.install()`` tail; once the adapter
    package left the tree (e78553be2b) that tail silently failed at
    import in every process and no kernel write mirrored. The kernel
    file must carry no reference to the shim module at all — if the
    tail ever comes back, this test fails before the next parity break.
    """
    kb_mod = sys.modules.get("hermes_cli.kanban_db")
    if kb_mod is None:  # pragma: no cover - normal import path
        import importlib

        kb_mod = importlib.import_module("hermes_cli.kanban_db")

    # The install wiring, if present, wraps module-level write ops.
    assert not getattr(kb_mod.create_task, "__kanban_dual_write_wrapped__", False), (
        "hermes_cli.kanban_db write ops are wrapped by the in-kernel "
        "kanban_dual_write shim — the P4 revert was undone and kernel "
        "mirroring now bypasses the kanban_write_op seam (t_d70258e5)."
    )

    # And the module must not import the shim at import time.
    assert kb_mod.__file__ is not None
    src = Path(kb_mod.__file__).read_text()
    assert "kanban_dual_write" not in src, (
        "hermes_cli/kanban_db.py references the in-kernel dual-write "
        "shim; the seam+plugin end-state requires the shim tail to be "
        "absent from the kernel module (t_d70258e5)."
    )


# --------------------------------------------------------------------------- #
# 2. Dispatch-path ops fire the seam (t_d70258e5 AC4)
# --------------------------------------------------------------------------- #


def test_dispatch_path_ops_fire_write_op_seam(kanban_home, captured_write_ops):
    """Claim/block/unblock/complete/reclaim/reassign must fire the seam.

    These are the ops the dispatcher exercises on every cycle; if any of
    them stops firing the seam, the DuckDB mirror silently freezes for
    that transition class (the 2026-09-06 drift mechanism).
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="parity-probe", assignee="worker")
        kb.add_comment(conn, tid, "author", "body")  # control: known-good site
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="probe block", kind=None)
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        kb.reclaim_task(conn, tid, reason="probe reclaim")
        kb.reassign_task(conn, tid, "worker2")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="probe done")
    finally:
        conn.close()

    ops = [e["op"] for e in captured_write_ops]
    # NOTE: reassign_task delegates to assign_task internally, which fires
    # its own seam event — so a reassign yields BOTH ops in this order.
    expected = [
        "create_task",
        "add_comment",
        "claim_task",
        "block_task",
        "unblock_task",
        "claim_task",
        "reclaim_task",
        "assign_task",      # inner call from reassign_task
        "reassign_task",    # outer reassign fire
        "claim_task",
        "complete_task",
    ]
    assert ops == expected, (
        f"seam fire sequence drifted from the dispatch-path contract:\n"
        f"  expected: {expected}\n  actual:   {ops}"
    )


def test_archive_and_promote_fire_write_op_seam(kanban_home, captured_write_ops):
    """archive_task / promote_task were missing seam fires (t_d70258e5 AC4).

    Every archive (CLI, dashboard, blocked_card_sweeper junk path) and every
    manual promote (vcg_dispatch_executor) silently skipped the DuckDB mirror
    before this fix. The ops must appear in the seam stream exactly once.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="parity-archive-probe", assignee="worker")
        kb.block_task(conn, tid, reason="park for promote probe", kind=None)
        ok_p, why = kb.promote_task(
            conn, tid, actor="parity-test", reason="probe",
        )
        assert ok_p, f"promote refused: {why}"
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="probe done")
        ok_a = kb.archive_task(conn, tid)
        assert ok_a, "archive refused"
    finally:
        conn.close()

    ops = [e["op"] for e in captured_write_ops]
    assert "promote_task" in ops, "promote_task did not fire the kanban_write_op seam"
    assert "archive_task" in ops, "archive_task did not fire the kanban_write_op seam"
    assert ops.count("promote_task") == 1
    assert ops.count("archive_task") == 1