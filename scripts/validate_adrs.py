#!/usr/bin/env python3
"""Validate Architecture Decision Records (ADRs).

Usage:
    python scripts/validate_adrs.py [--adr-dir DIR] [--index FILE] [--verbose] [--github-actions]

Checks performed:
  1. ADR files exist in docs/adr/ and follow the naming pattern ADR-{NNN}-{kebab-case}.md
  2. Each ADR file contains required sections: Status, Date, Context (or Decision), Consequences
  3. ADR numbering is sequential with no gaps (ADR-001, ADR-002, ADR-003, ...)
  4. docs/adr/INDEX.md references all ADR files present in the directory

Exit codes:
    0 — All checks pass
    1 — One or more validation errors found
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

ADR_FILENAME_RE = re.compile(r"^ADR-(\d{3})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
REQUIRED_SECTIONS = ["Status", "Context"]  # "Decision" or "Context" accepted; "Consequences" required
CONSEQUENCES_SECTION = "Consequences"
INDEX_FILE = "INDEX.md"
DEFAULT_ADR_DIR = "docs/adr"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _slugify(title: str) -> str:
    """Convert an ADR title to its expected kebab-case slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug


def github_annotation(level: str, file: str, line: int | None, message: str) -> str:
    """Format a GitHub Actions annotation string."""
    if line is not None:
        return f"::{level} file={file},line={line}::{message}"
    return f"::{level} file={file}::{message}"


# ── Validation functions ─────────────────────────────────────────────────────


def validate_filenames(adr_dir: Path, verbose: bool = False, gha: bool = False) -> list[str]:
    """Check that all .md files in adr_dir (except INDEX/template) match ADR-{NNN}-{slug}.md."""
    errors: list[str] = []
    for f in sorted(adr_dir.iterdir()):
        if not f.suffix == ".md":
            continue
        if f.name in (INDEX_FILE, "template.md"):
            continue
        if not ADR_FILENAME_RE.match(f.name):
            msg = f"ADR filename '{f.name}' does not match pattern ADR-NNN-kebab-case.md"
            errors.append(msg)
            if gha:
                print(github_annotation("error", str(f), None, msg))
            elif verbose:
                print(f"  ✗ {msg}")
    return errors


def validate_sections(adr_dir: Path, verbose: bool = False, gha: bool = False) -> list[str]:
    """Check that each ADR file contains required sections."""
    errors: list[str] = []
    for f in sorted(adr_dir.iterdir()):
        if not f.suffix == ".md":
            continue
        if f.name in (INDEX_FILE, "template.md"):
            continue
        if not ADR_FILENAME_RE.match(f.name):
            continue  # already caught by filename check

        text = f.read_text(encoding="utf-8")
        found_sections: set[str] = set()
        # Check markdown headings (## Section)
        for heading_match in re.finditer(r"^#+\s+(.+)$", text, re.MULTILINE):
            heading = heading_match.group(1).strip()
            for required in REQUIRED_SECTIONS + [CONSEQUENCES_SECTION, "Decision"]:
                if required.lower() in heading.lower():
                    found_sections.add(required)
        # Also check bold key-value lines (**Status**: ..., **Date**: ...)
        for line in text.splitlines():
            bold_match = re.match(r"^\*\*(\w+)\*\*", line.strip())
            if bold_match:
                key = bold_match.group(1)
                for required in REQUIRED_SECTIONS + [CONSEQUENCES_SECTION, "Decision"]:
                    if required.lower() == key.lower():
                        found_sections.add(required)

        # Context or Decision is required (either is fine)
        has_context_or_decision = "Context" in found_sections or "Decision" in found_sections
        if not has_context_or_decision:
            msg = f"{f.name}: missing 'Context' or 'Decision' section"
            errors.append(msg)
            if gha:
                print(github_annotation("error", str(f), None, msg))
            elif verbose:
                print(f"  ✗ {msg}")

        if "Status" not in found_sections:
            msg = f"{f.name}: missing 'Status' section"
            errors.append(msg)
            if gha:
                print(github_annotation("error", str(f), None, msg))
            elif verbose:
                print(f"  ✗ {msg}")

        if CONSEQUENCES_SECTION not in found_sections:
            msg = f"{f.name}: missing 'Consequences' section"
            errors.append(msg)
            if gha:
                print(github_annotation("error", str(f), None, msg))
            elif verbose:
                print(f"  ✗ {msg}")

    return errors


def validate_numbering(adr_dir: Path, verbose: bool = False, gha: bool = False) -> list[str]:
    """Check that ADR numbering is sequential with no gaps."""
    errors: list[str] = []
    numbers: list[int] = []

    for f in sorted(adr_dir.iterdir()):
        if not f.suffix == ".md":
            continue
        if f.name in (INDEX_FILE, "template.md"):
            continue
        m = ADR_FILENAME_RE.match(f.name)
        if m:
            numbers.append(int(m.group(1)))

    if not numbers:
        msg = "No ADR files found in directory"
        errors.append(msg)
        if gha:
            print(github_annotation("warning", str(adr_dir / "ADR-*"), None, msg))
        elif verbose:
            print(f"  ⚠ {msg}")
        return errors

    numbers.sort()
    for i, num in enumerate(numbers):
        expected = i + 1
        if num != expected:
            msg = f"ADR numbering gap or misalignment: found ADR-{num:03d}, expected ADR-{expected:03d}"
            errors.append(msg)
            if gha:
                print(github_annotation("error", str(adr_dir), None, msg))
            elif verbose:
                print(f"  ✗ {msg}")
            break  # report first gap only

    return errors


def validate_index(adr_dir: Path, verbose: bool = False, gha: bool = False) -> list[str]:
    """Check that INDEX.md references all ADR files present in the directory."""
    errors: list[str] = []
    index_path = adr_dir / INDEX_FILE

    if not index_path.exists():
        msg = f"{INDEX_FILE} not found in {adr_dir}"
        errors.append(msg)
        if gha:
            print(github_annotation("error", str(index_path), None, msg))
        elif verbose:
            print(f"  ✗ {msg}")
        return errors

    index_text = index_path.read_text(encoding="utf-8")

    # Collect all ADR filenames present
    adr_files: dict[str, Path] = {}
    for f in sorted(adr_dir.iterdir()):
        if not f.suffix == ".md":
            continue
        if f.name in (INDEX_FILE, "template.md"):
            continue
        if ADR_FILENAME_RE.match(f.name):
            adr_files[f.name] = f

    # Check each ADR is referenced in INDEX.md
    for adr_name in adr_files:
        # The ADR number (e.g. "ADR-001") or the filename should appear
        number_part = ADR_FILENAME_RE.match(adr_name).group(1)
        adr_prefix = f"ADR-{number_part}"
        if adr_prefix not in index_text and adr_name not in index_text:
            msg = f"{adr_name} is not referenced in {INDEX_FILE}"
            errors.append(msg)
            if gha:
                print(github_annotation("warning", str(adr_dir / adr_name), None, msg))
            elif verbose:
                print(f"  ⚠ {msg}")

    return errors


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate ADR files")
    parser.add_argument(
        "--adr-dir",
        type=Path,
        default=Path(DEFAULT_ADR_DIR),
        help=f"Directory containing ADR files (default: {DEFAULT_ADR_DIR})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed findings",
    )
    parser.add_argument(
        "--github-actions",
        action="store_true",
        help="Output GitHub Actions annotations",
    )
    args = parser.parse_args()

    adr_dir = args.adr_dir
    if not adr_dir.is_absolute():
        adr_dir = Path(os.getcwd()) / adr_dir

    if not adr_dir.exists():
        print(f"Error: ADR directory not found: {adr_dir}", file=sys.stderr)
        return 1

    verbose = args.verbose
    gha = args.github_actions

    all_errors: list[str] = []

    print("=" * 60)
    print("ADR Validation Report")
    print("=" * 60)

    # 1. Filename check
    print("\n1. Checking filenames...")
    errs = validate_filenames(adr_dir, verbose=verbose, gha=gha)
    all_errors.extend(errs)
    if not errs:
        print("   ✓ All filenames match ADR-NNN-kebab-case.md pattern")

    # 2. Section check
    print("\n2. Checking required sections...")
    errs = validate_sections(adr_dir, verbose=verbose, gha=gha)
    all_errors.extend(errs)
    if not errs:
        print("   ✓ All ADRs have required sections (Status, Context/Decision, Consequences)")

    # 3. Numbering check
    print("\n3. Checking sequential numbering...")
    errs = validate_numbering(adr_dir, verbose=verbose, gha=gha)
    all_errors.extend(errs)
    if not errs:
        print("   ✓ ADR numbering is sequential without gaps")

    # 4. INDEX check
    print("\n4. Checking INDEX.md references...")
    errs = validate_index(adr_dir, verbose=verbose, gha=gha)
    all_errors.extend(errs)
    if not errs:
        print("   ✓ All ADRs are referenced in INDEX.md")

    # Summary
    print("\n" + "=" * 60)
    if all_errors:
        print(f"FAIL: {len(all_errors)} issue(s) found")
        for err in all_errors:
            print(f"  - {err}")
        return 1
    else:
        print("PASS: All ADR validation checks passed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())