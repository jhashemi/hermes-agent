"""VFE-DEPLOY-03: dispatcher version advertising + stale-version refusal.

Tests cover:
- ``claimed`` event payload carries ``hermes_agent_version`` when a SHA
  is resolvable.
- Two dispatchers at the same version → both can claim; no refusal.
- Newer version claims first → older version's next claim on a different
  task is refused with a ``claim_refused_stale_version`` audit event
  and the task stays ``ready``.
- Window expiry → older version can claim again once the newer host's
  claim advertisement ages out (guards against permanent hang when the
  newer host disappears).
- Fail-open: when the caller's SHA is unresolvable (``None`` version),
  no refusal is attempted and no advertisement field is written.

The tests use ``HERMES_KANBAN_ADVERTISED_VERSION`` to pin arbitrary SHA
strings and ``HERMES_KANBAN_STALE_VERSION_WINDOW_SECONDS`` to tune the
refusal window. Ancestry is monkey-patched via ``_ANCESTRY_CACHE`` so
tests don't need a real git repo; the production ``git merge-base``
path is exercised by the ``_hermes_agent_version()`` smoke test that
runs against the checkout itself.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB.

    Mirrors the fixture in ``test_kanban_db.py`` — duplicated here so
    this file can move / be run in isolation without cross-file coupling.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _reset_version_state(monkeypatch):
    """Ensure per-test isolation for the module-level SHA / ancestry cache.

    Without this, an env-override set in one test leaks to the next
    because ``_HERMES_AGENT_VERSION`` is a process-wide cache.
    """
    kb._reset_hermes_agent_version_cache()
    # Snapshot + restore the ancestry cache so tests that seed it can't
    # bleed into unrelated tests.
    saved = dict(kb._ANCESTRY_CACHE)
    kb._ANCESTRY_CACHE.clear()
    yield
    kb._ANCESTRY_CACHE.clear()
    kb._ANCESTRY_CACHE.update(saved)
    kb._reset_hermes_agent_version_cache()


def _events(conn, task_id):
    return kb.list_events(conn, task_id)


def _claim_event_payload(conn, task_id):
    for ev in reversed(_events(conn, task_id)):
        if ev.kind == "claimed":
            return ev.payload
    return None


# ---------------------------------------------------------------------------
# Advertising
# ---------------------------------------------------------------------------


def test_claimed_event_carries_hermes_agent_version(kanban_home, monkeypatch):
    """AC1: dispatcher ``claimed`` events gain a ``hermes_agent_version``
    field carrying the caller's advertised SHA.
    """
    monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-v1")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="advertise-me", assignee="a")
        claimed = kb.claim_task(conn, t, claimer="host-x:1")
        assert claimed is not None
        payload = _claim_event_payload(conn, t)
        assert payload is not None
        assert payload.get("hermes_agent_version") == "sha-v1"


def test_advertisement_absent_when_version_unresolvable(kanban_home, monkeypatch):
    """When ``_hermes_agent_version()`` returns ``None`` (git missing,
    non-repo checkout, etc.), the ``claimed`` event is written without
    a ``hermes_agent_version`` field. Pre-fix dispatchers look the same,
    so the receiver's "unknown" bucket collapses naturally.
    """
    monkeypatch.setattr(kb, "_hermes_agent_version", lambda: None)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="no-version", assignee="a")
        claimed = kb.claim_task(conn, t, claimer="host-x:1")
        assert claimed is not None
        payload = _claim_event_payload(conn, t)
        assert payload is not None
        assert "hermes_agent_version" not in payload


# ---------------------------------------------------------------------------
# Same-version peers coexist (AC-4 case 1)
# ---------------------------------------------------------------------------


def test_two_dispatchers_same_version_both_claim(kanban_home, monkeypatch):
    """Two dispatchers running the same SHA can each claim their own
    task without either refusing the other.
    """
    monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-same")
    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        t2 = kb.create_task(conn, title="task-2", assignee="a")

        c1 = kb.claim_task(conn, t1, claimer="host-1:1")
        c2 = kb.claim_task(conn, t2, claimer="host-2:1")

        assert c1 is not None
        assert c2 is not None
        # No refusal events on either task.
        for tid in (t1, t2):
            for ev in _events(conn, tid):
                assert ev.kind != "claim_refused_stale_version", (
                    f"unexpected refusal on {tid}"
                )


# ---------------------------------------------------------------------------
# Newer version wins races (AC-4 case 2, main invariant)
# ---------------------------------------------------------------------------


def test_older_version_claim_refused_after_newer_claims(kanban_home, monkeypatch):
    """Newer version claims first → older version's next claim on a
    different task is refused with a ``claim_refused_stale_version``
    event, and the task stays in ``ready`` for another dispatcher.
    """
    # Seed ancestry: sha-old is a strict ancestor of sha-new.
    kb._ANCESTRY_CACHE[("sha-old", "sha-new")] = True

    with kb.connect() as conn:
        # First dispatcher (newer) claims task-1.
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-new")
        kb._reset_hermes_agent_version_cache()
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        c1 = kb.claim_task(conn, t1, claimer="host-new:1")
        assert c1 is not None

        # Second dispatcher (older) attempts task-2.
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-old")
        kb._reset_hermes_agent_version_cache()
        t2 = kb.create_task(conn, title="task-2", assignee="a")
        c2 = kb.claim_task(conn, t2, claimer="host-old:1")

        assert c2 is None, "older-version claim must be refused"
        # Task stays in ``ready`` — this is the deliberate design: another
        # dispatcher tick from the newer host will pick it up.
        task = kb.get_task(conn, t2)
        assert task.status == "ready"
        assert task.claim_lock is None

        # Refusal event carries structured incoming/highest SHAs.
        refusals = [
            ev for ev in _events(conn, t2)
            if ev.kind == "claim_refused_stale_version"
        ]
        assert len(refusals) == 1
        payload = refusals[0].payload
        assert payload["incoming"] == "sha-old"
        assert payload["highest"] == "sha-new"
        assert payload["window_seconds"] > 0
        assert payload["claimer"] == "host-old:1"


def test_newer_version_not_refused_when_older_claimed_first(kanban_home, monkeypatch):
    """Inverse case: if the older host claimed first (perhaps before an
    upgrade), the newer host's claim on a subsequent task is NOT refused
    (we only refuse strict-older claims, never strict-newer).
    """
    kb._ANCESTRY_CACHE[("sha-old", "sha-new")] = True

    with kb.connect() as conn:
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-old")
        kb._reset_hermes_agent_version_cache()
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        assert kb.claim_task(conn, t1, claimer="host-old:1") is not None

        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-new")
        kb._reset_hermes_agent_version_cache()
        t2 = kb.create_task(conn, title="task-2", assignee="a")
        c2 = kb.claim_task(conn, t2, claimer="host-new:1")
        assert c2 is not None
        # No refusal on t2.
        for ev in _events(conn, t2):
            assert ev.kind != "claim_refused_stale_version"


# ---------------------------------------------------------------------------
# Window expiry (AC-4 case 3, anti-hang guardrail)
# ---------------------------------------------------------------------------


def test_refusal_window_expires_older_version_can_claim_again(kanban_home, monkeypatch):
    """After the refusal window expires, the newer version's advertised
    claim ages out of the query and the older version can claim again.
    Prevents permanent hang when the newer host disappears.
    """
    kb._ANCESTRY_CACHE[("sha-old", "sha-new")] = True
    # Tight window so the test doesn't have to wait — we simulate expiry
    # by rewriting the ``claimed`` event's ``created_at`` backwards.
    monkeypatch.setenv("HERMES_KANBAN_STALE_VERSION_WINDOW_SECONDS", "60")

    with kb.connect() as conn:
        # Newer host claims first.
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-new")
        kb._reset_hermes_agent_version_cache()
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        assert kb.claim_task(conn, t1, claimer="host-new:1") is not None

        # Age the newer host's ``claimed`` event past the window boundary.
        now = int(time.time())
        conn.execute(
            "UPDATE task_events SET created_at = ? "
            "WHERE task_id = ? AND kind = 'claimed'",
            (now - 3600, t1),
        )
        conn.commit()

        # Now the older host's claim on a different task should succeed:
        # no in-window claim advertises a newer SHA anymore.
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-old")
        kb._reset_hermes_agent_version_cache()
        t2 = kb.create_task(conn, title="task-2", assignee="a")
        c2 = kb.claim_task(conn, t2, claimer="host-old:1")
        assert c2 is not None
        # No refusal event was emitted.
        for ev in _events(conn, t2):
            assert ev.kind != "claim_refused_stale_version"


def test_window_zero_disables_refusal_entirely(kanban_home, monkeypatch):
    """An env value of ``0`` disables the refusal path — a hatch for
    recovery / test environments where operators need every claim
    accepted regardless of version.
    """
    kb._ANCESTRY_CACHE[("sha-old", "sha-new")] = True
    monkeypatch.setenv("HERMES_KANBAN_STALE_VERSION_WINDOW_SECONDS", "0")

    with kb.connect() as conn:
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-new")
        kb._reset_hermes_agent_version_cache()
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        assert kb.claim_task(conn, t1, claimer="host-new:1") is not None

        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-old")
        kb._reset_hermes_agent_version_cache()
        t2 = kb.create_task(conn, title="task-2", assignee="a")
        c2 = kb.claim_task(conn, t2, claimer="host-old:1")
        assert c2 is not None, "window=0 must accept older-version claims"


# ---------------------------------------------------------------------------
# Fail-open on unknown ancestry
# ---------------------------------------------------------------------------


def test_unknown_ancestry_does_not_refuse(kanban_home, monkeypatch):
    """When ancestry can't be determined (``None`` from
    ``_is_strict_ancestor``) the claim must proceed — refusing on
    uncertainty would let a missing local commit strand the board.
    """
    # ``_is_strict_ancestor`` returns None → treated as fail-open.
    monkeypatch.setattr(kb, "_is_strict_ancestor", lambda a, b: None)

    with kb.connect() as conn:
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-A")
        kb._reset_hermes_agent_version_cache()
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        assert kb.claim_task(conn, t1, claimer="host-A:1") is not None

        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-B")
        kb._reset_hermes_agent_version_cache()
        t2 = kb.create_task(conn, title="task-2", assignee="a")
        c2 = kb.claim_task(conn, t2, claimer="host-B:1")
        assert c2 is not None


def test_diverged_branches_do_not_refuse(kanban_home, monkeypatch):
    """Two claimants on divergent branches (neither is an ancestor of
    the other) — no single dominant SHA exists, so
    ``_highest_observed_version`` returns None and the claim proceeds.
    Prevents a false-positive refusal during a race window between two
    concurrent feature branches.
    """
    # sha-A and sha-B: neither is an ancestor of the other.
    kb._ANCESTRY_CACHE[("sha-A", "sha-B")] = False
    kb._ANCESTRY_CACHE[("sha-B", "sha-A")] = False

    with kb.connect() as conn:
        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-A")
        kb._reset_hermes_agent_version_cache()
        t1 = kb.create_task(conn, title="task-1", assignee="a")
        assert kb.claim_task(conn, t1, claimer="host-A:1") is not None

        monkeypatch.setenv("HERMES_KANBAN_ADVERTISED_VERSION", "sha-B")
        kb._reset_hermes_agent_version_cache()
        t2 = kb.create_task(conn, title="task-2", assignee="a")
        c2 = kb.claim_task(conn, t2, claimer="host-B:1")
        assert c2 is not None


# ---------------------------------------------------------------------------
# Smoke test: real git resolution against the checkout itself
# ---------------------------------------------------------------------------


def test_hermes_agent_version_resolves_against_real_checkout(monkeypatch):
    """Smoke: the production resolution path returns a 40-char SHA when
    run against the hermes-agent checkout itself (i.e. when no env
    override is set). Guards against the git shell-out silently breaking
    across Python / OS upgrades.

    Skipped when the checkout isn't a git repo (e.g. pip install from
    an sdist) — production correctness on that path is that we return
    None and never refuse, which is covered by
    ``test_advertisement_absent_when_version_unresolvable``.
    """
    monkeypatch.delenv("HERMES_KANBAN_ADVERTISED_VERSION", raising=False)
    kb._reset_hermes_agent_version_cache()
    sha = kb._hermes_agent_version()
    root = kb._hermes_agent_root()
    if not (root / ".git").exists():
        pytest.skip("hermes-agent checkout is not a git repo")
    assert sha is not None
    assert len(sha) == 40, f"expected a 40-char git SHA, got {sha!r}"
    assert all(c in "0123456789abcdef" for c in sha)
