"""FIX-3: verification-environment contract for ``kanban_complete``.

Pure helpers for checking whether a worker's ``verification_venv`` claim
is trustworthy. Used by the ``vfe-complete-protocol`` plugin's
``kanban_task_completing`` callback to gate completions on the
verification-environment allowlist.

Why this lives in ``hermes_cli`` (not the plugin):

* The check is a pure function of ``(verification_venv, canonical_venvs)``.
  It performs no DB writes, no subprocess calls, no side effects. Placing
  it in-repo lets the hermes-agent kanban test suite (DoD item 5 of
  t_906fe15d) exercise the policy end-to-end via the seam without
  installing an out-of-tree plugin.
* The seam contract (``kanban_task_completing``) stays plugin-neutral —
  the plugin is still the one that decides to register the hook and
  produce the veto dict. The core just provides the primitive.

Policy semantics
----------------

The completion metadata field ``verification_venv`` MUST be an absolute
path to a Python interpreter. The completion is graded against the
``kanban.canonical_venvs`` allowlist from config:

* **missing / non-string / not absolute** →
  :class:`VenvVerdict.veto_missing` — the pre-hook should return
  ``{"veto": True, "reason": ...}`` to reject the completion.
* **absolute path, but NOT on the allowlist** →
  :class:`VenvVerdict.downgrade_non_allowlist` — the pre-hook should
  ABSTAIN (return ``None``) so completion proceeds, and the observer
  hook should emit an ``awaiting-verification`` comment so operators
  can filter these for manual review.
* **absolute path AND on the allowlist** →
  :class:`VenvVerdict.ok` — no action needed, completion proceeds.

Enforcement is controlled by the config flag
``kanban.enforce_completion_venv`` (default ``off``). When ``off``, the
plugin should abstain regardless of verdict (grace-period behavior).
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

_log = logging.getLogger(__name__)


class VenvVerdict(str, enum.Enum):
    """The verdict of :func:`grade_verification_venv`."""

    ok = "ok"
    """The venv path is absolute and on the allowlist."""

    veto_missing = "veto_missing"
    """The ``verification_venv`` field is missing, empty, non-string, or
    not an absolute path. Completion must be vetoed — the worker did not
    claim any verification environment, so the completion has no
    provenance."""

    downgrade_non_allowlist = "downgrade_non_allowlist"
    """The ``verification_venv`` field is an absolute path but NOT on the
    ``kanban.canonical_venvs`` allowlist. Completion proceeds, but the
    task is flagged ``awaiting-verification`` for operator review — the
    worker ran verification in an ad-hoc env that may or may not match
    the canonical framework env."""


@dataclass
class VenvCheckResult:
    """Structured verdict from :func:`grade_verification_venv`.

    ``verdict`` is the enum member. ``reason`` is a human-readable
    sentence suitable for a veto message or comment body. ``value`` is
    the raw ``verification_venv`` from metadata (may be ``None``).
    ``allowlist`` is the resolved canonical-venv list (for the audit
    trail). ``extras`` carries any additional diagnostic fields (e.g.
    the reason a non-string value was rejected).
    """

    verdict: VenvVerdict
    reason: str
    value: Any = None
    allowlist: List[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    @property
    def is_veto(self) -> bool:
        return self.verdict is VenvVerdict.veto_missing

    @property
    def is_downgrade(self) -> bool:
        return self.verdict is VenvVerdict.downgrade_non_allowlist

    @property
    def is_ok(self) -> bool:
        return self.verdict is VenvVerdict.ok


# The RCA-documented failure instance: workers built ephemeral scratch
# venvs that reported different pytest --collect-only counts than the
# canonical framework venv. When the allowlist is empty (config not set),
# we cannot gate — grade every completion as ``ok`` so the plugin can
# choose to abstain via its enforce flag rather than false-positive.
# The plugin's own flag ``kanban.enforce_completion_venv`` is what
# actually turns enforcement on; this helper is content-only.


def grade_verification_venv(
    metadata: Any,
    canonical_venvs: List[str],
) -> VenvCheckResult:
    """Grade ``metadata["verification_venv"]`` against the allowlist.

    Pure function — no filesystem probes, no subprocess. The venv path
    is not required to exist on disk (that would make tests brittle);
    what's checked is the *claim shape* and *allowlist membership*.

    Parameters
    ----------
    metadata
        The ``metadata`` dict from ``kanban_complete``. When not a dict
        the field is considered absent (verdict: ``veto_missing``).
    canonical_venvs
        The resolved ``kanban.canonical_venvs`` allowlist. When empty
        the completion is graded as ``ok`` regardless — enforcement is
        the plugin's responsibility via its own flag.

    Returns
    -------
    VenvCheckResult
    """
    # Normalize the allowlist: strip whitespace, drop empties.
    allowlist = [
        v.strip() for v in (canonical_venvs or [])
        if isinstance(v, str) and v.strip()
    ]

    if not isinstance(metadata, dict):
        return VenvCheckResult(
            verdict=VenvVerdict.veto_missing,
            reason=(
                "verification_venv field missing: metadata is not a dict "
                f"(got {type(metadata).__name__})"
            ),
            value=None,
            allowlist=allowlist,
        )

    raw = metadata.get("verification_venv")
    if raw is None:
        return VenvCheckResult(
            verdict=VenvVerdict.veto_missing,
            reason=(
                "verification_venv field missing from completion metadata. "
                "Every completion MUST declare which Python interpreter "
                "was used to verify claims (pytest, imports, service "
                "checks). See FIX-3 (t_906fe15d) and the "
                "VFE-COMPLETE-01 protocol reference."
            ),
            value=None,
            allowlist=allowlist,
        )
    if not isinstance(raw, str):
        return VenvCheckResult(
            verdict=VenvVerdict.veto_missing,
            reason=(
                "verification_venv must be a string absolute path "
                f"(got {type(raw).__name__}: {raw!r})"
            ),
            value=raw,
            allowlist=allowlist,
        )
    stripped = raw.strip()
    if not stripped:
        return VenvCheckResult(
            verdict=VenvVerdict.veto_missing,
            reason="verification_venv is empty after stripping whitespace",
            value=raw,
            allowlist=allowlist,
        )
    if not os.path.isabs(stripped):
        return VenvCheckResult(
            verdict=VenvVerdict.veto_missing,
            reason=(
                "verification_venv must be an ABSOLUTE path to a Python "
                f"interpreter (got relative path: {stripped!r})"
            ),
            value=raw,
            allowlist=allowlist,
        )

    # Empty allowlist → we can't distinguish canonical from ad-hoc, so
    # we grade as ok. The plugin's ``enforce_completion_venv`` flag is
    # what turns real gating on; if enforcement is on with an empty
    # allowlist that's a config error, not a per-completion failure.
    if not allowlist:
        return VenvCheckResult(
            verdict=VenvVerdict.ok,
            reason=(
                "verification_venv is absolute; canonical_venvs allowlist "
                "is empty so no membership check performed"
            ),
            value=stripped,
            allowlist=allowlist,
            extras={"empty_allowlist": True},
        )

    if stripped in allowlist:
        return VenvCheckResult(
            verdict=VenvVerdict.ok,
            reason=(
                f"verification_venv {stripped!r} matches canonical "
                "allowlist"
            ),
            value=stripped,
            allowlist=allowlist,
        )

    return VenvCheckResult(
        verdict=VenvVerdict.downgrade_non_allowlist,
        reason=(
            f"verification_venv {stripped!r} is absolute but NOT on the "
            f"canonical allowlist {allowlist}. Completion proceeds but "
            "is flagged awaiting-verification for operator review — the "
            "worker's env may not match the canonical framework env "
            "(see FIX-3 t_906fe15d RCA: workers reported 9877 tests in "
            "scratch venv vs 7409 tests in canonical framework venv)."
        ),
        value=stripped,
        allowlist=allowlist,
    )


# ---------------------------------------------------------------------------
# Config loaders
# ---------------------------------------------------------------------------


def load_canonical_venvs() -> List[str]:
    """Load ``kanban.canonical_venvs`` from Hermes config.

    Returns an empty list on any load failure (no config file, YAML
    parse error, missing key, wrong type). Callers should treat an
    empty allowlist as "enforcement effectively off for the membership
    check" — see :func:`grade_verification_venv`.
    """
    try:
        from hermes_cli.config import load_config  # local import — avoid cycles
        config = load_config()
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("kanban.canonical_venvs load failed: %s", exc)
        return []
    if not isinstance(config, dict):
        return []
    kanban_cfg = config.get("kanban")
    if not isinstance(kanban_cfg, dict):
        return []
    raw = kanban_cfg.get("canonical_venvs")
    if not isinstance(raw, list):
        return []
    return [v.strip() for v in raw if isinstance(v, str) and v.strip()]


def load_enforce_completion_venv_flag() -> bool:
    """Load ``kanban.enforce_completion_venv`` from Hermes config.

    Returns ``False`` (off / grace period) by default. When ``True``,
    the plugin's pre-hook produces a veto for ``veto_missing`` verdicts
    and abstains-with-warning for ``downgrade_non_allowlist``. When
    ``False``, the plugin abstains for all verdicts — no behavior change.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("kanban.enforce_completion_venv load failed: %s", exc)
        return False
    if not isinstance(config, dict):
        return False
    kanban_cfg = config.get("kanban")
    if not isinstance(kanban_cfg, dict):
        return False
    return bool(kanban_cfg.get("enforce_completion_venv", False))


# ---------------------------------------------------------------------------
# Pre-hook callback — the one line the plugin registers
# ---------------------------------------------------------------------------


def completing_hook(
    task_id: str,
    *,
    metadata: Optional[dict] = None,
    canonical_venvs: Optional[List[str]] = None,
    enforce: Optional[bool] = None,
    **_: Any,
) -> Optional[dict]:
    """``kanban_task_completing`` callback: gate on verification_venv.

    Parameters
    ----------
    task_id
        The task being completed (kept for logging / event context).
    metadata
        The completion ``metadata`` dict.
    canonical_venvs
        Injectable allowlist — defaults to :func:`load_canonical_venvs`.
        Overridable in tests.
    enforce
        Injectable enforcement flag — defaults to
        :func:`load_enforce_completion_venv_flag`. Overridable in tests.

    Returns
    -------
    dict | None
        ``{"veto": True, "reason": ..., "source": ...}`` when the
        completion must be blocked. ``None`` (abstain) when the
        completion should proceed — either because enforcement is off,
        the venv is ok, or the verdict is a soft downgrade.
    """
    if enforce is None:
        enforce = load_enforce_completion_venv_flag()
    if not enforce:
        # Grace period — plugin abstains and the observer hook can still
        # emit informational comments.
        return None

    if canonical_venvs is None:
        canonical_venvs = load_canonical_venvs()

    result = grade_verification_venv(metadata, canonical_venvs)
    if result.is_veto:
        return {
            "veto": True,
            "reason": result.reason,
            "source": "vfe-complete-protocol:verification_venv",
        }
    # ``downgrade_non_allowlist`` and ``ok`` both let completion proceed.
    # The observer hook handles awaiting-verification bookkeeping.
    return None
