"""INCIDENT-01 S3 — ``hermes config lint`` warns on same-account
consecutive fallbacks.

Reproduces the 2026-08-18 incident config: Everett's
``~/.hermes/config.yaml`` had 3 consecutive ``ollama-cloud`` entries in
``fallback_providers``. When ollama-cloud's account 429'd, every entry
in the chain failed simultaneously — one account outage killed the
whole cascade.

The lint must:
  1. Flag N≥2 consecutive fallback entries that share a
     provider-account identity (provider + base_url + api key ref).
  2. Flag a chain with ZERO non-primary-account entries (i.e. every
     entry, including primary, resolves to the same account).
  3. Return a non-empty warning list from a pure function so it can
     be unit-tested without touching the filesystem or argparse.
  4. Wire into ``hermes config lint`` as a new subcommand that exits
     non-zero when warnings are found (so CI / cron / dispatch can
     surface it).

Public surface expected:
    from hermes_cli.fallback_config import lint_fallback_chain
    warnings = lint_fallback_chain(config)   # list[str]

Empty list ⇒ clean. Non-empty ⇒ each string is a human-readable
finding suitable for stderr / log output.
"""

from __future__ import annotations

import pytest

pytest_plugins: list[str] = []


def _import_lint():
    from hermes_cli.fallback_config import lint_fallback_chain
    return lint_fallback_chain


# ─────────────────────────────────────────────────────────────────────
# Baseline: healthy chains produce no warnings
# ─────────────────────────────────────────────────────────────────────


def test_diverse_chain_produces_no_warnings():
    """A chain crossing three different provider accounts is exactly
    what we want — no warnings.
    """
    lint = _import_lint()
    config = {
        "model": {"provider": "bedrock", "default": "us.anthropic.claude-sonnet-4"},
        "provider": "bedrock",
        "fallback_providers": [
            {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
            {"provider": "nous", "model": "hermes-4-70b"},
            {"provider": "anthropic", "model": "claude-3-5-sonnet"},
        ],
    }
    assert lint(config) == []


def test_empty_fallback_chain_is_not_flagged():
    """No fallback configured ⇒ nothing to lint. Users with a single
    trusted provider are allowed to opt out of the whole mechanism.
    """
    lint = _import_lint()
    assert lint({"model": {"provider": "bedrock"}}) == []
    assert lint({"model": {"provider": "bedrock"}, "fallback_providers": []}) == []


def test_single_entry_chain_is_not_flagged():
    """A one-entry chain can't have consecutive-account duplication."""
    lint = _import_lint()
    config = {
        "model": {"provider": "bedrock"},
        "provider": "bedrock",
        "fallback_providers": [
            {"provider": "openrouter", "model": "meta-llama/llama-3.3-70b"},
        ],
    }
    assert lint(config) == []


# ─────────────────────────────────────────────────────────────────────
# Core: 2+ consecutive same-account entries
# ─────────────────────────────────────────────────────────────────────


def test_two_consecutive_same_account_entries_are_flagged():
    """Everett's exact incident config: consecutive ollama-cloud
    entries. One account outage kills both — the fallback is illusory.
    """
    lint = _import_lint()
    config = {
        "model": {"provider": "bedrock"},
        "provider": "bedrock",
        "fallback_providers": [
            {"provider": "ollama-cloud", "model": "glm-5.2"},
            {"provider": "ollama-cloud", "model": "deepseek-v4"},
            {"provider": "anthropic", "model": "claude-3-5-sonnet"},
        ],
    }
    warnings = lint(config)
    assert warnings, (
        "S3 REGRESSION: 2 consecutive ollama-cloud fallback entries "
        "were not flagged. Everett's incident config had exactly this "
        "shape and the whole cascade failed together when the account "
        "429'd. See ticket t_3e1634d9."
    )
    joined = " ".join(warnings).lower()
    assert "ollama-cloud" in joined, (
        f"Warning should name the offending provider. Got: {warnings!r}"
    )
    assert "consecutive" in joined or "adjacent" in joined, (
        f"Warning should identify the failure mode. Got: {warnings!r}"
    )


def test_three_consecutive_same_account_entries_are_flagged():
    """The literal Everett incident config: THREE ollama-cloud entries
    in a row.
    """
    lint = _import_lint()
    config = {
        "model": {"provider": "bedrock"},
        "provider": "bedrock",
        "fallback_providers": [
            {"provider": "ollama-cloud", "model": "glm-5.2"},
            {"provider": "ollama-cloud", "model": "deepseek-v4"},
            {"provider": "ollama-cloud", "model": "kimi-k2"},
        ],
    }
    warnings = lint(config)
    assert warnings, "3 consecutive ollama-cloud entries must be flagged"


def test_non_consecutive_duplicates_are_not_flagged():
    """Same provider twice with a different provider between them is
    NOT a fallback-diversity failure — the middle entry catches quota
    outages on the outer provider.
    """
    lint = _import_lint()
    config = {
        "model": {"provider": "bedrock"},
        "provider": "bedrock",
        "fallback_providers": [
            {"provider": "ollama-cloud", "model": "glm-5.2"},
            {"provider": "openrouter", "model": "meta-llama/llama-3.3"},
            {"provider": "ollama-cloud", "model": "kimi-k2"},
        ],
    }
    # Nothing consecutive; may still warn on other rules, but must NOT
    # emit the "consecutive same-account" finding.
    warnings = lint(config)
    for w in warnings:
        assert "consecutive" not in w.lower(), (
            f"False positive on non-consecutive duplicates: {w!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Core: no non-primary-account entry (chain shares primary's account)
# ─────────────────────────────────────────────────────────────────────


def test_chain_all_sharing_primary_account_is_flagged():
    """If every fallback shares the primary's provider account, an
    account-level outage takes down the whole system — the fallback
    is illusory. This was Everett's failure mode.
    """
    lint = _import_lint()
    config = {
        "model": {"provider": "ollama-cloud", "default": "glm-5.2"},
        "provider": "ollama-cloud",
        "fallback_providers": [
            {"provider": "ollama-cloud", "model": "deepseek-v4"},
            {"provider": "ollama-cloud", "model": "kimi-k2"},
        ],
    }
    warnings = lint(config)
    assert warnings, (
        "S3: chain sharing primary's account must be flagged as no "
        "cross-account diversity."
    )
    joined = " ".join(warnings).lower()
    # Either the "consecutive" rule fires (2 ollama-cloud in a row) OR
    # the "no diversity" rule fires — both are valid findings for this
    # config.
    assert (
        "consecutive" in joined
        or "diversity" in joined
        or "same account" in joined
        or "no fallback" in joined
    ), f"Warning should identify the diversity failure. Got: {warnings!r}"


# ─────────────────────────────────────────────────────────────────────
# CLI wiring: `hermes config lint` subcommand exists and exits non-zero
# ─────────────────────────────────────────────────────────────────────


def test_config_lint_subcommand_registered():
    """The lint must be reachable via `hermes config lint` argparse."""
    from hermes_cli.subcommands.config import build_config_parser
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_config_parser(subparsers, cmd_config=lambda a: None)

    args = parser.parse_args(["config", "lint"])
    # The subcommand attribute name varies but the key must resolve to "lint"
    resolved = (
        getattr(args, "config_command", None)
        or getattr(args, "config_subcommand", None)
    )
    assert resolved == "lint", (
        f"`hermes config lint` subcommand not registered. args={args!r}"
    )


def test_config_command_dispatches_lint(monkeypatch, capsys):
    """`hermes config lint` must invoke lint_fallback_chain and exit
    non-zero when warnings are found."""
    from hermes_cli import config as _cfgmod

    # Stub load_config to return an offending chain
    offending = {
        "model": {"provider": "ollama-cloud"},
        "provider": "ollama-cloud",
        "fallback_providers": [
            {"provider": "ollama-cloud", "model": "a"},
            {"provider": "ollama-cloud", "model": "b"},
        ],
    }
    monkeypatch.setattr(_cfgmod, "load_config", lambda: offending, raising=False)

    class _Args:
        config_command = "lint"

    with pytest.raises(SystemExit) as exc:
        _cfgmod.config_command(_Args())

    assert exc.value.code != 0, (
        "`hermes config lint` must exit non-zero when the chain has "
        "warnings — so CI / cron / dispatch can surface the misconfig."
    )
    out = capsys.readouterr()
    combined = (out.out + out.err).lower()
    assert "ollama-cloud" in combined or "consecutive" in combined, (
        f"Lint output should describe the finding. Got: {out.out + out.err!r}"
    )
