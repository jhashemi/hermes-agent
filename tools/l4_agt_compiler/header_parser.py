"""L4 file header parser.

Every .l4 file MUST start with a block of ``-- key: value`` comment lines
(per SCHEMA.md). This module extracts those fields into a plain dict.

Recognised header fields
------------------------
  system, axis, tier, author, ratified, council_signoffs, supersedes,
  version, enabled, enforcement

Type coercions applied
----------------------
  enabled        → bool   (``true`` → True, anything else → False)
  enforcement    → str    validated against VALID_ENFORCEMENT_VALUES
  council_signoffs → list  (comma-separated, square-brackets stripped)

Safe defaults (when a field is absent)
---------------------------------------
  enabled      = False         # safe default: new/un-migrated files start disabled
  enforcement  = "disabled"    # safe default
  council_signoffs = []
  all other string fields = ""
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Union

# ── validation constants ──────────────────────────────────────────────────────

VALID_ENFORCEMENT_VALUES = ("enforce", "monitor", "disabled")

_STRING_FIELDS = ("system", "axis", "tier", "author", "ratified", "supersedes", "version")

# Regex for a header comment line: ``-- key: value`` (leading whitespace OK)
_HEADER_RE = re.compile(r"^\s*--\s+(\w[\w_-]*):\s*(.*?)\s*$")


# ── public API ────────────────────────────────────────────────────────────────


def parse_l4_header(l4_path: Union[Path, str]) -> dict:
    """Read *only* the header block of an L4 file and return a parsed dict.

    The header block ends at the first non-blank, non-comment line.

    Returns a dict with keys:
        system, axis, tier, author, ratified, council_signoffs,
        supersedes, version, enabled, enforcement

    Missing fields receive safe defaults (see module docstring).
    Malformed values are silently normalised rather than raising.
    """
    l4_path = Path(l4_path)

    raw: dict[str, str] = {}
    try:
        with l4_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Stop at the first line that is not a comment and not blank
                stripped = line.rstrip("\n")
                if stripped.strip() == "":
                    continue  # blank lines within the header block are OK
                m = _HEADER_RE.match(stripped)
                if m is None:
                    break  # first non-comment line → header is over
                key, value = m.group(1).lower(), m.group(2)
                raw[key] = value
    except OSError:
        # File unreadable — return all defaults
        pass

    return _coerce(raw)


# ── internal helpers ──────────────────────────────────────────────────────────


def _coerce(raw: dict[str, str]) -> dict:
    result: dict = {}

    # Simple string fields
    for field in _STRING_FIELDS:
        result[field] = raw.get(field, "").strip()

    # council_signoffs: "[a, b, c]" or "a, b, c" → list
    raw_signoffs = raw.get("council_signoffs", "")
    result["council_signoffs"] = _parse_list(raw_signoffs)

    # enabled: "true" (case-insensitive) → True; anything else → False
    raw_enabled = raw.get("enabled", "false").strip().lower()
    result["enabled"] = raw_enabled == "true"

    # enforcement: must be one of the valid values; fall back to "disabled"
    raw_enforcement = raw.get("enforcement", "disabled").strip().lower()
    if raw_enforcement not in VALID_ENFORCEMENT_VALUES:
        raw_enforcement = "disabled"
    result["enforcement"] = raw_enforcement

    return result


def _parse_list(raw: str) -> list[str]:
    """Parse a comma-separated string (optionally wrapped in ``[...]``) → list."""
    raw = raw.strip()
    if raw.startswith("["):
        raw = raw[1:]
    if raw.endswith("]"):
        raw = raw[:-1]
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]
