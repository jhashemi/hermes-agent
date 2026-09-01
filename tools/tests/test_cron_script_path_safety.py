"""Regression tests for the cron-script path-safety bug class (2026-09-01).

Failure class: files under ~/.hermes/scripts that are SYMLINKS resolving
outside that directory get rejected by the cron runner's path-safety check
('script path resolves outside the scripts directory') — exec-audit-watchdog
failed 56x, audit-unstick 29x, cron-meta 30x with this exact signature.

These tests fail until every cron-referenced script entry is a REAL file
inside the scripts directory.
"""
import json
import os
from pathlib import Path

SCRIPTS_DIR = Path(os.environ.get("HERMES_SCRIPTS_DIR", "/home/ubuntu/.hermes/scripts"))
JOBS_JSON = Path(os.environ.get("HERMES_CRON_JOBS", "/home/ubuntu/.hermes/cron/jobs.json"))


def _cron_referenced_scripts() -> set[str]:
    """Script basenames the cron runner will actually execute."""
    try:
        jobs = json.loads(JOBS_JSON.read_text())
    except (OSError, ValueError):
        return set()
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs", [])
    names = set()
    for j in jobs:
        script = (j or {}).get("script") or ""
        if script:
            names.add(Path(script).name)
    return names


def _script_entries():
    return sorted(p for p in SCRIPTS_DIR.iterdir() if p.suffix in (".py", ".sh", ".bash"))


def test_scripts_dir_has_entries():
    entries = _script_entries()
    assert entries, f"no scripts found under {SCRIPTS_DIR}"


def test_cron_referenced_scripts_are_real_files_inside_scripts_dir():
    """THE bug: cron-referenced scripts that are symlinks resolving outside
    the scripts dir get blocked by the path-safety check. (Symlinks for
    non-cron library modules are the sanctioned convention and stay.)"""
    referenced = _cron_referenced_scripts()
    assert referenced, f"could not parse cron jobs from {JOBS_JSON}"
    bad = []
    for name in sorted(referenced):
        p = SCRIPTS_DIR / name
        if not p.exists():
            bad.append(f"{name} (missing)")
        elif p.is_symlink():
            target = Path(os.path.realpath(p))
            if SCRIPTS_DIR not in target.parents:
                bad.append(f"{name} -> {target}")
    assert not bad, "cron-referenced scripts blocked by path-safety:\n" + "\n".join(bad)


def test_cron_referenced_py_scripts_are_python_not_bash():
    """Companion bug class: .py extension holding bash content (exec_audit_watch
    failed 56x with 'SyntaxError' on an 'exec ... \"$@\"' line)."""
    bad = []
    for name in sorted(_cron_referenced_scripts()):
        p = SCRIPTS_DIR / name
        if p.suffix != ".py" or not p.is_file():
            continue
        head = p.read_text(errors="replace")[:200]
        if head.startswith("#!/bin/bash") or head.startswith("#!/bin/sh"):
            bad.append(name)
    assert not bad, ".py files containing bash (Python runner will SyntaxError):\n" + "\n".join(bad)


def test_every_script_resolves_to_existing_target():
    """A stub whose runpy target was deleted would fail every run — catch it."""
    import re
    bad = []
    for p in _script_entries():
        if p.suffix != ".py":
            continue
        for m in re.finditer(r'runpy\.run_path\("([^"]+)"', p.read_text(errors="replace")):
            if not Path(m.group(1)).exists():
                bad.append(f"{p.name} -> {m.group(1)}")
    assert not bad, "stubs pointing at missing targets:\n" + "\n".join(bad)
