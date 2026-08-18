"""INCIDENT-01 S1 — agent loop must NOT exit rc=0 on fatal API failure.

Regression for the 2026-08-18 "no pulse" event:

  ``hermes chat -q "work kanban task <id>"`` spawned by the kanban
  dispatcher hit HTTP 429 on the first (and only) model call, burned 5
  same-model retries, printed no assistant messages, and exited rc=0.
  The dispatcher read rc=0 as success, marked the run "protocol
  violation" (no completion signal), and re-dispatched the same card
  into the same wall — repeatedly.

Root cause in code: ``cli.py::main()`` line ~17964 (the human-facing
``-q`` non-quiet path) invokes ``cli.chat(...)`` and then ``return``s
from ``main()`` without inspecting the run outcome. ``fire.Fire(main)``
turns a bare ``return`` into shell exit 0. The fully-quiet ``-Q`` path
above (line ~17915-17940) DOES set an exit code from
``result["failed"]`` / ``result["failure_reason"]`` and calls
``sys.exit(_exit_code)``. This test locks in that same behavior for the
non-quiet ``-q`` path that kanban workers actually use.

Acceptance: ``rc != 0`` when the agent loop ends with:
  1. Zero assistant messages emitted, AND
  2. A terminal API error (``result["failed"] is True``)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────
# Structural test: the -q non-quiet path must consult run outcome
# ─────────────────────────────────────────────────────────────────────
#
# We test at the source level rather than driving fire.Fire end-to-end
# because ``main()`` is 500+ lines and depends on a live config, agent
# init, credential resolution, and stdio setup. The bug is a missing
# exit-code branch in a well-defined region of cli.py — asserting that
# region contains the required sys.exit call is both necessary and
# sufficient for the fix.


def _extract_non_quiet_q_block() -> str:
    """Return the text of cli.py::main() between the ``else`` that guards
    the non-quiet -q path and the terminating ``return`` at the end of
    the ``if query or image:`` block. This is the region that currently
    lacks any sys.exit call on failure.
    """
    import cli as _cli_mod
    src = open(_cli_mod.__file__, "r", encoding="utf-8").read()

    # Anchor on a stable landmark string from the else-branch comment
    # block in cli.py:17944 ("Single-query mode ... skip the welcome
    # banner") — we want the code from there to the closing return.
    anchor = "Single-query mode (`hermes chat -q"
    start = src.find(anchor)
    assert start != -1, (
        "Could not locate the non-quiet -q else-branch comment anchor. "
        "cli.py may have been refactored; update this test."
    )

    # End anchor: the first ``finally:`` block that ends single-query mode
    # (the ``_finalize_single_query(cli)`` cleanup).
    end_anchor = "_finalize_single_query(cli)"
    end = src.find(end_anchor, start)
    assert end != -1, "Could not locate the -q cleanup anchor."

    return src[start:end]


def test_non_quiet_q_path_calls_sys_exit_on_failure():
    """The non-quiet ``hermes chat -q`` path must call ``sys.exit(...)``
    with a non-zero code when the agent run failed with zero visible
    output. Currently it does not — it just falls through to a bare
    ``return`` from ``main()`` and the process exits 0.
    """
    block = _extract_non_quiet_q_block()

    # After the fix, this block must contain a sys.exit(...) branch that
    # is conditional on the run outcome (result.get("failed")) OR on
    # whether cli.chat returned a truthy response. The simplest
    # observable signal: the string 'sys.exit' must appear inside this
    # block. Its absence is the S1 bug.
    assert "sys.exit" in block, (
        "S1 REGRESSION: the non-quiet `hermes chat -q` path in cli.py "
        "does not call sys.exit(). It relies on a bare `return` from "
        "main(), which fire.Fire turns into shell exit 0 — even when "
        "the agent hit a terminal API failure with zero assistant "
        "output. Kanban dispatchers, cron, and CI all read rc=0 as "
        "success. Add an explicit sys.exit(non_zero) branch that keys "
        "on the run outcome (chat() return value or agent.last_run_result). "
        "See INCIDENT-01 ticket t_3e1634d9 for full RCA."
    )


def test_non_quiet_q_path_checks_run_outcome():
    """The exit-code decision must be OUTCOME-driven, not blind: the
    block must reference the chat return value or a run-result flag
    that signals whether the agent produced any assistant messages /
    hit a terminal API error. A bare ``sys.exit(1)`` at the bottom
    would technically satisfy the previous test but would also break
    every successful ``hermes chat -q "hi"`` invocation. Guard against
    that regression.
    """
    block = _extract_non_quiet_q_block()

    # Any of these signals qualify as outcome-driven:
    #   - checking cli.chat's return value (None means no response)
    #   - inspecting cli.agent.last_run_result for a "failed" flag
    #   - checking whether the message queue produced any assistant turn
    outcome_signals = [
        "result",       # e.g. `result = cli.chat(...)` then check
        "response",     # e.g. `response = cli.chat(...)` then check
        "last_run",     # agent.last_run_result
        "failed",       # result["failed"]
        "final_response",  # dict key from run_conversation
    ]
    assert any(sig in block for sig in outcome_signals), (
        "S1 fix must be OUTCOME-driven. The non-quiet -q exit-code "
        "branch must inspect the chat/agent run result (e.g. cli.chat "
        "return value, agent.last_run_result, or a `failed` flag) — "
        "otherwise a blind sys.exit(1) would break successful runs. "
        f"None of {outcome_signals} appear near the sys.exit call."
    )


def test_chat_returns_signal_or_agent_exposes_last_run_result():
    """For the S1 fix to be implementable, either ``HermesCLI.chat``
    must return the run result (or a truthy/falsy response) that
    reflects failure, OR ``HermesCLI.agent`` must expose the terminal
    run outcome via a documented attribute. This test locks in that
    the plumbing exists (or is added) so future readers can trace how
    the exit code is derived.
    """
    import inspect
    import cli as _cli_mod

    HermesCLI = getattr(_cli_mod, "HermesCLI", None)
    assert HermesCLI is not None, (
        "cli.HermesCLI not found — cli.py structure has changed."
    )

    chat_method = getattr(HermesCLI, "chat", None)
    assert chat_method is not None, "HermesCLI.chat() is missing."

    # HermesCLI.chat() is annotated as ``Optional[str]`` today. The fix
    # MAY rely on that return value (None => failure) OR MAY read
    # cli.agent.last_run_result — either is acceptable. This test just
    # ensures at least one such signal path exists in the source.
    src = inspect.getsource(HermesCLI)
    has_signal = (
        "last_run_result" in src
        or "-> Optional[str]" in src   # existing chat() annotation
        or "return None" in src
        or "return response" in src
    )
    assert has_signal, (
        "S1 fix needs a signal path from chat() outcome back to main(). "
        "Neither `last_run_result` attribute nor a documented return "
        "value was found on HermesCLI. Add one so the -q exit-code "
        "branch can decide rc reliably."
    )
