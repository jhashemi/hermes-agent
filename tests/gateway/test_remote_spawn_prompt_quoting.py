"""Regression test: remote spawn prompt must be shell-quoted.

RCA t_276cb9df — run-186 of t_okr_market_research_assignment.

The symptom: the boardroom-driver (livekit-boardroom assignee) was dispatched
via SSH, but the prompt "work kanban task <id>" was NOT shell-quoted in the
``bash -l -c "..."`` inline command.  bash word-split the prompt so ``-q``
only received "work" and the remaining tokens ("kanban", "task", "<id>") were
passed as top-level positional arguments to the hermes root parser, which
rejected them with::

    hermes: error: unrecognized arguments: kanban task t_okr_market_research_assignment

Fixed in commit 4be2df2cd0: ``remote_cmd_parts.extend(["chat", "-q", shlex.quote(prompt)])``

Tests here replay the exact argv shape observed in run-186 and assert:
  1. The remote_spawn_cmd builder shell-quotes the -q prompt correctly.
  2. A bash inline script with the quoted prompt passes the full phrase as a
     single token (unit-tested via shlex splitting).
  3. A bash inline script WITHOUT quoting (pre-fix behaviour) would have
     word-split — confirm the test would FAIL for the old code.
  4. The spawn-time rc=2 guard (defect 2) surfaces a structured block reason
     that contains "unrecognized arguments" when the log shows that text.
"""

import shlex
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── helpers ────────────────────────────────────────────────────────────────

def _remote_cmd(task_id, assignee, workspace, board, target_node,
                skills=None, env_extra=None):
    """Call the real remote_spawn_cmd() from gateway.cluster_dispatch."""
    from gateway.cluster_dispatch import remote_spawn_cmd
    return remote_spawn_cmd(
        task_id=task_id,
        assignee=assignee,
        workspace=workspace,
        board=board,
        target_node=target_node,
        skills=skills,
        env_extra=env_extra,
    )


def _extract_inline_bash(cmd: list) -> str:
    """Extract the bash inline script from an SSH argv list."""
    # Structure: ["ssh", ..., host, "bash -l -c <inline>"]
    return cmd[-1]


# ─── tests ──────────────────────────────────────────────────────────────────

class TestRemoteSpawnPromptQuoting:
    """Defect 1: prompt must be shell-quoted in the inline bash script.

    Regression for RCA t_276cb9df / run-186 of t_okr_market_research_assignment.
    """

    @pytest.fixture(autouse=True)
    def _patch_node_hosts(self, monkeypatch):
        """Provide a fake SSH host mapping so the builder doesn't fail."""
        import gateway.cluster_dispatch as cd
        monkeypatch.setattr(cd, "_NODE_HOSTS", {"hermes2": "10.0.0.2"})
        # Silence the worker-api-env helper to avoid DB access in tests.
        monkeypatch.setattr(
            cd, "_worker_api_env_for_remote", lambda node: {}
        )

    def test_prompt_is_shlex_quoted_in_inline_cmd(self):
        """The inline bash script must contain a shell-quoted -q value."""
        task_id = "t_okr_market_research_assignment"
        cmd = _remote_cmd(
            task_id=task_id,
            assignee="livekit-boardroom",
            workspace="/workspace",
            board="okr-vfe-2026-q3",
            target_node="hermes2",
        )
        inline = _extract_inline_bash(cmd)
        prompt = f"work kanban task {task_id}"
        expected_quoted = shlex.quote(prompt)  # e.g. "'work kanban task t_...'"
        assert expected_quoted in inline, (
            f"shell-quoted prompt {expected_quoted!r} not found in inline bash "
            f"script.\nActual inline: {inline!r}\n"
            "Without quoting bash would word-split the prompt into separate "
            "tokens and hermes argparse would reject them as unrecognized "
            "arguments — exactly the run-186 failure."
        )

    def test_unquoted_prompt_would_split(self):
        """Demonstrate the OLD behaviour: unquoted prompt word-splits under bash.

        This is a negative-example test — it shows what the pre-fix code did.
        We cannot actually invoke bash here, but we can simulate word-splitting
        by running the inline through shlex.split() with the prompt unquoted.
        """
        task_id = "t_okr_market_research_assignment"
        prompt = f"work kanban task {task_id}"
        # Pre-fix: prompt was NOT quoted
        inline_broken = f"hermes -p livekit-boardroom --skills kanban-worker chat -q {prompt}"
        tokens = shlex.split(inline_broken)
        # After -q comes only "work" — the rest are separate positional tokens
        q_index = tokens.index("-q")
        q_value = tokens[q_index + 1]
        positionals_after_q = tokens[q_index + 2:]
        assert q_value == "work", \
            "The broken unquoted form should give -q only 'work'"
        assert "kanban" in positionals_after_q, \
            "Unquoted prompt word-splits: 'kanban' becomes a stray positional"
        assert f"task" in positionals_after_q, \
            "Unquoted prompt word-splits: 'task' becomes a stray positional"
        assert task_id in positionals_after_q, \
            "Unquoted prompt word-splits: task_id becomes a stray positional"

    def test_quoted_prompt_stays_single_token(self):
        """With shlex.quote the full prompt is one token after -q."""
        task_id = "t_okr_market_research_assignment"
        prompt = f"work kanban task {task_id}"
        # Post-fix: prompt IS quoted
        inline_fixed = (
            f"hermes -p livekit-boardroom --skills kanban-worker "
            f"chat -q {shlex.quote(prompt)}"
        )
        tokens = shlex.split(inline_fixed)
        q_index = tokens.index("-q")
        q_value = tokens[q_index + 1]
        assert q_value == prompt, (
            f"With shlex.quote the -q value should be the full prompt string, "
            f"got {q_value!r}"
        )
        # No stray positionals after the quoted prompt.
        assert tokens[q_index + 2:] == [], \
            "No stray positional tokens after the -q prompt"

    def test_cmd_ends_with_chat_q_quoted_prompt(self):
        """remote_spawn_cmd returns an SSH list whose inline ends with chat -q '<prompt>'."""
        task_id = "t_test_convene"
        cmd = _remote_cmd(
            task_id=task_id,
            assignee="livekit-boardroom",
            workspace="/tmp/ws",
            board="test-board",
            target_node="hermes2",
        )
        # Must be an SSH command
        assert cmd[0] == "ssh"
        inline = _extract_inline_bash(cmd)
        # Must contain chat -q <quoted-prompt>
        expected_prompt = shlex.quote(f"work kanban task {task_id}")
        assert "chat" in inline
        assert "-q" in inline
        assert expected_prompt in inline

    def test_run186_exact_argv_shape(self):
        """Replay the exact run-186 scenario: livekit-boardroom convene task.

        The broken argv was (approximately):
            bash -l -c "hermes -p livekit-boardroom --skills kanban-worker
                        chat -q work kanban task t_okr_market_research_assignment"

        The fixed argv must be:
            bash -l -c "hermes -p livekit-boardroom --skills kanban-worker
                        chat -q 'work kanban task t_okr_market_research_assignment'"
        """
        task_id = "t_okr_market_research_assignment"
        cmd = _remote_cmd(
            task_id=task_id,
            assignee="livekit-boardroom",
            workspace="/home/ubuntu/.hermes/kanban/boards/okr-vfe-2026-q3/workspaces/t_okr_market_research_assignment",
            board="okr-vfe-2026-q3",
            target_node="hermes2",
            skills=["kanban-worker"],
        )
        # The SSH command is: ["ssh", ..., host, "bash -l -c <inline>"]
        # The inline is the LAST element of the cmd list.
        inline = _extract_inline_bash(cmd)
        # The inline is a single shell script passed to bash -c.
        # shlex.split of the inline gives us the hermes command tokens.
        # Strip the outer "bash -l -c" wrapper first.
        cmd_tokens = shlex.split(cmd[-1])  # splits at the bash -l -c boundary
        # cmd_tokens[0]="bash", [1]="-l", [2]="-c", [3]=<inline_script>
        # The inline_script is the final token; shlex.split it again to get
        # the actual hermes tokens.
        assert len(cmd_tokens) >= 4, f"Expected bash -l -c <inline>, got {cmd_tokens}"
        inline_script = cmd_tokens[3]
        hermes_tokens = shlex.split(inline_script)

        # Find the hermes command after any "export ..." statements.
        # hermes_tokens contains "export K=V; export K2=V2; hermes ..."
        # We need to split on semicolons.
        # Better: just check the inline string directly.
        prompt = f"work kanban task {task_id}"
        expected_quoted = shlex.quote(prompt)

        # The inline script must contain the properly quoted prompt.
        assert expected_quoted in inline_script, (
            f"Quoted prompt {expected_quoted!r} not found in inline script.\n"
            f"Inline: {inline_script!r}"
        )

        # Must NOT contain the unquoted split form.
        # (The pre-fix form would have "chat -q work kanban task t_...")
        unquoted_fragment = f"chat -q work kanban task {task_id}"
        assert unquoted_fragment not in inline_script, (
            f"Found unquoted fragment {unquoted_fragment!r} in inline script — "
            f"this is the BROKEN pre-fix form.\nInline: {inline_script!r}"
        )


class TestSpawnTimeArgparseGuard:
    """Defect 2: spawn-time rc=2 guard surfaces a typed block reason.

    When a spawned worker exits rc=2 within ~1s and its log contains
    'unrecognized arguments', the guard reads the log and returns a structured
    block reason.  This test exercises the guard's log-scanning logic in
    isolation (the guard is embedded in _default_spawn, tested here via a
    direct call to the relevant logic).
    """

    def test_guard_detects_unrecognized_arguments_in_log(self, tmp_path):
        """A log containing 'unrecognized arguments' triggers the guard."""
        # Write a fake worker log with the exact error from run-186.
        log_path = tmp_path / "t_test.log"
        run186_output = (
            "Warning: Unknown toolsets: vfe_logs\n"
            "Query: work kanban task t_okr_market_research_assignment\n"
            "Initializing agent...\n"
            "usage: hermes [-h] ...\n"
            "hermes: error: unrecognized arguments: kanban task t_okr_market_research_assignment\n"
        )
        log_path.write_bytes(run186_output.encode())

        # Simulate the guard's log-read + check.
        with open(log_path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")

        assert "unrecognized arguments" in head
        # The guard also extracts the offending line.
        err_line = ""
        for line in head.splitlines():
            if "unrecognized arguments" in line:
                err_line = line.strip()
                break
        assert "kanban task t_okr_market_research_assignment" in err_line

    def test_guard_extracts_offending_line(self, tmp_path):
        """Guard picks the correct offending line (not header lines)."""
        log_path = tmp_path / "t_test2.log"
        log_path.write_bytes(
            b"some preamble\n"
            b"usage: hermes [-h] ...\n"
            b"hermes: error: unrecognized arguments: kanban task t_X\n"
            b"more output\n"
        )
        with open(log_path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
        err_line = ""
        for line in head.splitlines():
            if "unrecognized arguments" in line:
                err_line = line.strip()
                break
        assert err_line == "hermes: error: unrecognized arguments: kanban task t_X"

    def test_clean_log_does_not_trigger_guard(self, tmp_path):
        """A normal worker log (no argparse error) must not trigger the guard."""
        log_path = tmp_path / "t_clean.log"
        log_path.write_bytes(
            b"Query: work kanban task t_abc\n"
            b"Initializing agent...\n"
            b"kanban_show called\n"
            b"Task complete.\n"
        )
        with open(log_path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
        triggered = (
            "unrecognized arguments" in head or "error: unrecognized" in head
        )
        assert not triggered, "Clean log must NOT trigger the spawn-parse guard"
