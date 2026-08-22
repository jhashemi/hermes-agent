"""FIX-4 (t_477e2f4d): auto-unlink REVIEW tickets from blocked subject parents.

A REVIEW / VERIFY ticket that gates itself on the blocked subject it is
supposed to review is a deadlock: the dispatcher records
``claim_rejected: parents_not_done`` forever and the reviewer never runs.
``kanban_db.create_task`` detects the pattern heuristically (title keyword
+ parent id in title/body) and severs the parent link so the review lands
as a sibling. These tests pin the heuristic + the auto-unlink contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DoD 1: REVIEW ticket linked to a blocked subject -> parent auto-unlinked
# ---------------------------------------------------------------------------


def test_review_of_blocked_parent_auto_unlinks(kanban_home):
    """A ``REVIEW <parent-id>`` ticket parented on that blocked parent has
    the parent link severed, becomes ``ready``, and records both a
    ``review_of_blocked_parent`` event and an audit comment."""
    with kb.connect() as conn:
        subject = kb.create_task(conn, title="MERGE-S8 shard merge", assignee="worker")
        kb.claim_task(conn, subject)
        assert kb.block_task(conn, subject, reason="stuck on rebase") is True

        review = kb.create_task(
            conn,
            title=f"REVIEW {subject}",
            body=f"Peer-review the MERGE-S8 shard in {subject} for correctness.",
            parents=[subject],
            assignee="reviewer",
        )

        # 1. Parent link severed → review has NO parents in the link table.
        parents_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (review,)
        ).fetchall()
        assert [r["parent_id"] for r in parents_rows] == [], (
            "review ticket should have zero parents after auto-unlink"
        )

        # 2. Review is `ready`, not `todo` (parent is no longer gating it).
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (review,)
        ).fetchone()
        assert row["status"] == "ready"

        # 3. `review_of_blocked_parent` event recorded with the parent id.
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (review,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "review_of_blocked_parent" in kinds
        payload = json.loads(
            next(e["payload"] for e in events if e["kind"] == "review_of_blocked_parent")
        )
        assert payload["auto_unlinked_parents"] == [subject]
        assert "reason" in payload and payload["reason"]

        # 4. Audit comment attached by `kernel` naming the subject id.
        comments = kb.list_comments(conn, review)
        kernel_comments = [c for c in comments if c.author == "kernel"]
        assert len(kernel_comments) == 1
        assert subject in kernel_comments[0].body
        assert "review" in kernel_comments[0].body.lower()

        # 5. The `created` event reflects the post-unlink parent list (empty).
        created_payload = json.loads(
            next(e["payload"] for e in events if e["kind"] == "created")
        )
        assert created_payload["parents"] == []


def test_verify_keyword_also_triggers_auto_unlink(kanban_home):
    """The heuristic covers VERIFY as well as REVIEW."""
    with kb.connect() as conn:
        subject = kb.create_task(conn, title="deploy shard", assignee="worker")
        kb.claim_task(conn, subject)
        kb.block_task(conn, subject, reason="stuck")

        review = kb.create_task(
            conn,
            title=f"VERIFY {subject} rollout",
            body=f"Confirm the {subject} deploy landed cleanly.",
            parents=[subject],
            assignee="reviewer",
        )

        parents_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (review,)
        ).fetchall()
        assert parents_rows == []


def test_review_of_blocked_parent_preserves_other_parents(kanban_home):
    """Only the blocked-subject parent is severed; other legitimate
    non-blocked parents are kept."""
    with kb.connect() as conn:
        subject = kb.create_task(conn, title="MERGE-S9 shard", assignee="worker")
        kb.claim_task(conn, subject)
        kb.block_task(conn, subject, reason="stuck")

        other = kb.create_task(conn, title="prep step", assignee="worker")

        review = kb.create_task(
            conn,
            title=f"REVIEW {subject}",
            body=f"Peer-review {subject}.",
            parents=[subject, other],
            assignee="reviewer",
        )

        parents_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (review,),
        ).fetchall()
        remaining = [r["parent_id"] for r in parents_rows]
        assert remaining == [other], (
            f"only the blocked subject should be severed; got {remaining!r}"
        )
        # `other` is not done → review stays `todo` (normal parent-gate).
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (review,)
        ).fetchone()
        assert row["status"] == "todo"


# ---------------------------------------------------------------------------
# DoD 2: normal parent linkage still works
# ---------------------------------------------------------------------------


def test_normal_parent_linkage_survives(kanban_home):
    """A non-review child of a blocked parent MUST keep the parent link.

    Auto-unlink is scoped to REVIEW/VERIFY-shaped titles; a plain follow-up
    task should still be gated on its parent (the whole point of parent
    links is dependency ordering).
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="build shard", assignee="worker")
        kb.claim_task(conn, parent)
        kb.block_task(conn, parent, reason="stuck")

        child = kb.create_task(
            conn,
            title="deploy shard",
            body=f"Ship the artefact built in {parent}.",
            parents=[parent],
            assignee="worker",
        )

        parents_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (child,)
        ).fetchall()
        assert [r["parent_id"] for r in parents_rows] == [parent]

        # Child is `todo` because parent isn't `done`.
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child,)
        ).fetchone()
        assert row["status"] == "todo"

        # No `review_of_blocked_parent` event or kernel comment.
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (child,)
        ).fetchall()
        assert "review_of_blocked_parent" not in [e["kind"] for e in events]

        comments = kb.list_comments(conn, child)
        assert not [c for c in comments if c.author == "kernel"]


def test_review_of_non_blocked_parent_survives(kanban_home):
    """A review ticket whose parent is NOT blocked (e.g. still running or
    already done) keeps the parent link — the heuristic only fires for a
    ``blocked`` subject, which is the deadlock condition."""
    with kb.connect() as conn:
        subject = kb.create_task(conn, title="feature A", assignee="worker")
        # Leave it in `ready`.

        review = kb.create_task(
            conn,
            title=f"REVIEW {subject}",
            body=f"Peer-review {subject}.",
            parents=[subject],
            assignee="reviewer",
        )

        parents_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (review,)
        ).fetchall()
        assert [r["parent_id"] for r in parents_rows] == [subject]


def test_review_title_without_parent_id_reference_not_unlinked(kanban_home):
    """A ``REVIEW`` title that does NOT name its parent id anywhere is not
    treated as a review of that parent — it might be a legitimate review
    task with its own dependency chain. False positives here would be
    worse than the deadlock we're fixing.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="unrelated setup", assignee="worker")
        kb.claim_task(conn, parent)
        kb.block_task(conn, parent, reason="stuck")

        child = kb.create_task(
            conn,
            title="REVIEW quarterly threat model",
            body="Sweep top-10 CVE list for the quarter.",
            parents=[parent],
            assignee="reviewer",
        )

        parents_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (child,)
        ).fetchall()
        assert [r["parent_id"] for r in parents_rows] == [parent]


def test_unknown_parent_still_rejected(kanban_home):
    """Unknown parent ids must still raise (dangling-link protection is
    orthogonal to the FIX-4 auto-unlink)."""
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="unknown parent"):
            kb.create_task(
                conn,
                title="REVIEW t_deadbeef",
                body="Peer-review t_deadbeef.",
                parents=["t_deadbeef"],
                assignee="reviewer",
            )


# ---------------------------------------------------------------------------
# Heuristic unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, body, parent_id, expected",
    [
        ("REVIEW t_abc123", "peer-review t_abc123", "t_abc123", True),
        ("VERIFY t_abc123 rollout", "check t_abc123", "t_abc123", True),
        ("Audit of t_abc123", "notes: t_abc123", "t_abc123", True),
        ("QA sweep", "cover t_abc123", "t_abc123", True),  # QA is a review keyword
        ("misc work", "cover t_abc123", "t_abc123", False),  # no review keyword
        ("REVIEW other work", "no ref here", "t_abc123", False),  # no id ref
        ("deploy t_abc123", "just do it", "t_abc123", False),  # no review keyword
        ("peer-review t_abc123", "body", "t_abc123", True),
        ("", "", "t_abc123", False),
        ("REVIEW t_abc123", "body", "", False),  # empty parent id
    ],
)
def test_looks_like_review_of(title, body, parent_id, expected):
    assert kb._looks_like_review_of(title, body, parent_id) is expected
