"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import os
from typing import Any


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        return os.getenv(key_env, "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain


# =============================================================================
# INCIDENT-01 (2026-08-18) — fallback-chain diversity lint
# =============================================================================
#
# Everett's ``~/.hermes/config.yaml`` had 3 consecutive ollama-cloud entries
# in ``fallback_providers``. When ollama-cloud's account quota was exhausted,
# every entry in the chain failed simultaneously — one account outage killed
# the whole cascade. The lint catches this shape at config time (via
# ``hermes config lint``) or at agent startup (called by the runtime as a
# best-effort warning). See ticket t_3e1634d9 for the full RCA.
#
# The identity used for "same account" is (provider, resolved_api_key_ref).
# We deliberately DO NOT include base_url in the identity — a chain of
# entries all hitting the same ollama-cloud account but with different
# regional base_urls still fails together on an account-level 429.
#
# Fail-safe posture: the lint is pure and side-effect-free; callers decide
# whether warnings block startup, degrade to log lines, or exit non-zero.


def _account_identity(entry: dict) -> tuple[str, str]:
    """Provider + credential reference. Two entries with the same tuple
    share an account and will fail together on account-level quota.

    Uses the ``key_env`` (or ``api_key_env`` alias) name as the credential
    reference — the actual key value is neither read nor logged. An inline
    ``api_key`` is treated as its own bucket keyed off the first 8 chars so
    two inline entries with the same literal key still collide.
    """
    provider = str(entry.get("provider") or "").strip().lower()
    key_env = str(
        entry.get("key_env") or entry.get("api_key_env") or ""
    ).strip().lower()
    if key_env:
        cred_ref = f"env:{key_env}"
    else:
        inline = str(entry.get("api_key") or "").strip()
        if inline:
            # Hash-like short prefix so two entries sharing an inline key
            # collide, but we never move the raw key around.
            cred_ref = f"inline:{inline[:8]}"
        else:
            # No credential declared on the entry → provider-default resolution.
            # All such entries for a given provider share one account bucket.
            cred_ref = "provider-default"
    return (provider, cred_ref)


def lint_fallback_chain(config: dict | None) -> list[str]:
    """Return warnings about the fallback chain's cross-account diversity.

    Empty list ⇒ clean. Each string is a human-readable finding suitable
    for stderr / log output. The lint is pure and side-effect-free.

    Rules:
      1. Two or more CONSECUTIVE entries sharing a provider-account → warn.
         The consecutive shape is the exact incident: one account outage
         drops the whole run of adjacent entries with no diversity gap.
      2. Every fallback entry sharing the PRIMARY model's account → warn.
         The chain has no cross-account diversity at all; an account-level
         outage takes down primary + entire fallback.
    """
    warnings: list[str] = []
    config = config or {}

    chain = get_fallback_chain(config)
    if not chain:
        return warnings   # No fallback configured — nothing to lint.

    # ── Rule 1: consecutive same-account entries ─────────────────────
    run_start = 0
    while run_start < len(chain):
        run_end = run_start + 1
        base_identity = _account_identity(chain[run_start])
        while (
            run_end < len(chain)
            and _account_identity(chain[run_end]) == base_identity
        ):
            run_end += 1
        run_len = run_end - run_start
        if run_len >= 2:
            provider, cred_ref = base_identity
            warnings.append(
                f"fallback_providers: {run_len} consecutive entries share "
                f"provider account (provider={provider!r}, credential={cred_ref!r}, "
                f"positions {run_start}..{run_end - 1}). One account-level "
                f"outage (429 quota / auth revocation) will drop all of them "
                f"simultaneously. Interleave a different provider account "
                f"between them for real diversity."
            )
        run_start = run_end

    # ── Rule 2: no cross-account diversity vs primary ────────────────
    primary_provider = str(
        (config.get("model") or {}).get("provider")
        or config.get("provider")
        or ""
    ).strip().lower()
    if primary_provider:
        primary_identity = (
            primary_provider,
            # Best effort — mirror _account_identity's fallback path.
            "provider-default",
        )
        # If any explicit credential reference exists at top-level for the
        # primary, use it — otherwise leave "provider-default".
        for k in ("api_key_env", "key_env"):
            if config.get(k):
                primary_identity = (
                    primary_provider,
                    f"env:{str(config[k]).strip().lower()}",
                )
                break

        all_share_primary = all(
            _account_identity(entry)[0] == primary_identity[0]
            for entry in chain
        )
        if all_share_primary and len(chain) >= 1:
            warnings.append(
                f"fallback_providers: every entry ({len(chain)} total) "
                f"uses the same provider as the primary model "
                f"(provider={primary_provider!r}). No cross-account diversity — "
                f"an account-level outage on {primary_provider!r} takes down "
                f"both primary and the entire fallback chain. Add at least "
                f"one entry from a different provider account."
            )

    return warnings
