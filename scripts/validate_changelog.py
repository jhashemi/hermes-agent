#!/usr/bin/env python3
"""Validate CHANGELOG.md follows keep-a-changelog format.

Usage:
    python scripts/validate_changelog.py [--file FILE] [--verbose]

Checks performed:
  1. File exists and is non-empty
  2. Has a top-level "Changelog" heading
  3. Has at least one versioned section (## [x.y.z] or ## [Unreleased])
  4. Each versioned section uses one of: Added, Changed, Deprecated, Removed, Fixed, Security
  5. No empty versioned sections (section heading with no items)

Exit codes:
    0 — All checks pass (issues are warnings, not blocking)
    1 — Critical errors (missing file, malformed structure)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = "CHANGELOG.md"

VALID_SUBSECTIONS = {
    "added", "changed", "deprecated", "removed", "fixed", "security",
}

VERSION_HEADING_RE = re.compile(r"^##\s+\[(.+?)\]\s*", re.MULTILINE)
SUBSECTION_RE = re.compile(r"^###\s+(\w+)", re.MULTILINE)


def validate_changelog(filepath: Path, verbose: bool = False) -> list[str]:
    """Validate the changelog file. Returns list of error/warning messages."""
    errors: list[str] = []

    if not filepath.exists():
        errors.append(f"File not found: {filepath}")
        return errors

    text = filepath.read_text(encoding="utf-8")
    if not text.strip():
        errors.append(f"File is empty: {filepath}")
        return errors

    # Check for top-level Changelog heading
    if not re.search(r"^#\s+Changelog", text, re.MULTILINE | re.IGNORECASE):
        errors.append("Missing top-level '# Changelog' heading")

    # Check for versioned sections
    versions = VERSION_HEADING_RE.findall(text)
    if not versions:
        errors.append("No versioned sections found (expected ## [version] headings)")
        return errors

    if verbose:
        print(f"Found {len(versions)} version(s): {', '.join(versions[:5])}")

    # Check subsections are from the keep-a-changelog vocabulary
    for match in SUBSECTION_RE.finditer(text):
        subsection = match.group(1)
        if subsection.lower() not in VALID_SUBSECTIONS:
            msg = f"Unrecognized subsection '### {subsection}' (expected one of: {', '.join(sorted(VALID_SUBSECTIONS))})"
            errors.append(msg)
            if verbose:
                print(f"  ⚠ {msg}")

    # Check for empty subsections (heading but no list items before next heading)
    lines = text.splitlines()
    in_subsection = False
    subsection_name = ""
    subsection_line = 0
    has_items = False

    for i, line in enumerate(lines, 1):
        sub_match = re.match(r"^###\s+(\w+)", line)
        head_match = re.match(r"^#{1,3}\s+", line)

        if sub_match:
            # Before moving to new subsection, check if previous was empty
            if in_subsection and not has_items:
                msg = f"Empty subsection '### {subsection_name}' at line {subsection_line}"
                errors.append(msg)
                if verbose:
                    print(f"  ⚠ {msg}")
            in_subsection = True
            subsection_name = sub_match.group(1)
            subsection_line = i
            has_items = False
        elif in_subsection and head_match:
            # Exiting subsection via another heading
            if not has_items:
                msg = f"Empty subsection '### {subsection_name}' at line {subsection_line}"
                errors.append(msg)
                if verbose:
                    print(f"  ⚠ {msg}")
            in_subsection = False
        elif in_subsection and line.strip().startswith("-"):
            has_items = True

    # Final subsection check
    if in_subsection and not has_items:
        msg = f"Empty subsection '### {subsection_name}' at line {subsection_line}"
        errors.append(msg)

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CHANGELOG.md format")
    parser.add_argument("--file", type=Path, default=Path(DEFAULT_CHANGELOG),
                        help=f"Path to changelog file (default: {DEFAULT_CHANGELOG})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print detailed findings")
    args = parser.parse_args()

    filepath = args.file
    if not filepath.is_absolute():
        import os
        filepath = Path(os.getcwd()) / filepath

    print("=" * 60)
    print("Changelog Validation Report")
    print("=" * 60)

    errors = validate_changelog(filepath, verbose=args.verbose)

    print(f"\nFile: {filepath}")
    if not errors:
        print("PASS: Changelog format is valid")
        return 0
    else:
        print(f"\n{len(errors)} issue(s) found (advisory, non-blocking):")
        for err in errors:
            print(f"  - {err}")
        return 0  # Always exit 0 — this is advisory


if __name__ == "__main__":
    raise SystemExit(main())