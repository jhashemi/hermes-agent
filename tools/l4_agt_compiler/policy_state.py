"""Runtime policy enable/disable state manager.

State is persisted in ``~/.hermes/policies/.state.json`` (configurable).
The file is the runtime source of truth; the L4 header ``enabled``/
``enforcement`` fields are the ratification-time defaults used as fallback
when no runtime override exists.

Schema (per task spec)::

    {
      "policies": {
        "<system>.<axis>": {
          "enabled": true|false,
          "enforcement": "enforce|monitor|disabled",
          "last_changed_at": "ISO-8601",
          "changed_by": "<actor-id>",
          "reason": "<optional, required for disable>",
          "auto_revert_at": "ISO-8601 (24h after disable)"
        }
      }
    }
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

# ── constants ─────────────────────────────────────────────────────────────────

VALID_ENFORCEMENT_VALUES = ("enforce", "monitor", "disabled")

_AUTO_REVERT_HOURS = 24

DEFAULT_STATE_PATH = Path.home() / ".hermes" / "policies" / ".state.json"


# ── public API ────────────────────────────────────────────────────────────────


def load_state(state_path: Path = DEFAULT_STATE_PATH) -> dict:
    """Load the state file; return ``{"policies": {}}`` if missing/corrupt."""
    state_path = Path(state_path)
    if not state_path.is_file():
        return {"policies": {}}
    try:
        text = state_path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"policies": {}}
        if "policies" not in data or not isinstance(data["policies"], dict):
            data["policies"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"policies": {}}


def save_state(state: dict, state_path: Path = DEFAULT_STATE_PATH) -> None:
    """Persist state atomically (write-temp + rename) to avoid partial writes."""
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory so rename is atomic on Linux
    fd, tmp_name = tempfile.mkstemp(
        dir=str(state_path.parent), prefix=".state_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp_name, str(state_path))  # atomic on POSIX
    except Exception:
        # Clean up temp file if something went wrong
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def set_policy_state(
    policy_id: str,
    *,
    enabled: bool,
    enforcement: str,
    changed_by: str,
    reason: Optional[str] = None,
    state_path: Path = DEFAULT_STATE_PATH,
) -> dict:
    """Update runtime state for *policy_id* and persist.

    Validates ``enforcement`` against allowed values.
    Sets ``auto_revert_at`` to now+24h when ``enforcement == "disabled"``.
    Clears ``auto_revert_at`` otherwise.

    Returns the updated policy entry dict.
    """
    if enforcement not in VALID_ENFORCEMENT_VALUES:
        raise ValueError(
            f"enforcement must be one of {VALID_ENFORCEMENT_VALUES}, got {enforcement!r}"
        )

    state = load_state(state_path)
    now_iso = _utcnow_iso()

    entry: dict = {
        "enabled": enabled,
        "enforcement": enforcement,
        "last_changed_at": now_iso,
        "changed_by": changed_by,
    }
    if reason is not None:
        entry["reason"] = reason
    else:
        # Preserve existing reason if any
        existing = state["policies"].get(policy_id, {})
        if "reason" in existing:
            entry["reason"] = existing["reason"]

    if enforcement == "disabled":
        auto_dt = _utcnow() + _dt.timedelta(hours=_AUTO_REVERT_HOURS)
        entry["auto_revert_at"] = auto_dt.isoformat()
    else:
        # Clear auto-revert when re-enabling
        entry.pop("auto_revert_at", None)

    state["policies"][policy_id] = entry
    save_state(state, state_path)
    return entry


def get_policy_state(
    policy_id: str,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    header_defaults: Optional[dict] = None,
) -> dict:
    """Return the effective state for *policy_id*.

    Priority: runtime state file > header defaults > hardcoded safe defaults.

    ``header_defaults`` may be the dict returned by
    ``header_parser.parse_l4_header()``.
    """
    state = load_state(state_path)
    if policy_id in state["policies"]:
        return dict(state["policies"][policy_id])

    # Fall back to header defaults if provided
    if header_defaults:
        return {
            "enabled": header_defaults.get("enabled", False),
            "enforcement": header_defaults.get("enforcement", "disabled"),
            "last_changed_at": None,
            "changed_by": None,
        }

    # Hardcoded safe defaults
    return {
        "enabled": False,
        "enforcement": "disabled",
        "last_changed_at": None,
        "changed_by": None,
    }


def check_auto_revert(state: dict) -> list[str]:
    """Return a list of policy_ids whose ``auto_revert_at`` has passed.

    The caller (e.g. PolicyEngine on load) is responsible for actually
    performing the revert.  This function is read-only.
    """
    now = _utcnow()
    expired: list[str] = []
    for policy_id, entry in state.get("policies", {}).items():
        raw_ts = entry.get("auto_revert_at")
        if not raw_ts:
            continue
        try:
            revert_dt = _dt.datetime.fromisoformat(raw_ts)
            # Make timezone-aware if necessary for comparison
            if revert_dt.tzinfo is None:
                revert_dt = revert_dt.replace(tzinfo=_dt.timezone.utc)
            if now >= revert_dt:
                expired.append(policy_id)
        except (ValueError, TypeError):
            continue
    return expired


# ── helpers ───────────────────────────────────────────────────────────────────


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()
