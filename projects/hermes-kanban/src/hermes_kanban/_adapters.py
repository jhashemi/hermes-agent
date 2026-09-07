"""Adapter stubs for hermes-agent profile/config/constants dependencies.

These are injected at runtime by the host application (hermes-agent).
When running standalone (tests, CLI), sensible defaults are used.

The pattern: each function tries the real hermes_* implementation first,
falls back to a stdlib-only default if the package is not installed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile / config adapters (hermes_cli.profiles / hermes_cli.config)
# ---------------------------------------------------------------------------

def normalize_profile_name(name: str) -> str:
    """Normalize a profile name, stripping whitespace and special chars.

    Falls back to simple string cleaning if hermes_cli is unavailable.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name as _real
        return _real(name)
    except ImportError:
        # Simple fallback: strip, lowercase, replace spaces with hyphens
        return name.strip().lower().replace(" ", "-")


def profile_exists(name: str) -> bool:
    """Check if a profile directory exists under HERMES_HOME.

    Falls back to filesystem check if hermes_cli is unavailable.
    """
    try:
        from hermes_cli.profiles import profile_exists as _real
        return _real(name)
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
        return (home / "profiles" / name).is_dir() or name == "default"


def get_active_profile_name() -> str:
    """Get the currently active profile name.

    Falls back to 'default' if hermes_cli is unavailable.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name as _real
        return _real()
    except ImportError:
        return os.environ.get("HERMES_PROFILE", "default")


def load_config() -> dict[str, Any]:
    """Load the hermes configuration.

    Falls back to empty dict if hermes_cli is unavailable.
    """
    try:
        from hermes_cli.config import load_config as _real
        return _real()
    except ImportError:
        return {}


# ---------------------------------------------------------------------------
# Constants adapter (hermes_constants)
# ---------------------------------------------------------------------------

def get_default_hermes_root() -> Path:
    """Return the default hermes root directory (~/.hermes or HERMES_HOME).

    Falls back to Path(~/.hermes) if hermes_constants is unavailable.
    """
    try:
        from hermes_constants import get_default_hermes_root as _real
        return _real()
    except ImportError:
        override = os.environ.get("HERMES_HOME", "").strip()
        if override:
            return Path(override).expanduser()
        return Path(os.path.expanduser("~/.hermes"))


# ---------------------------------------------------------------------------
# State / WAL adapter (hermes_state)
# ---------------------------------------------------------------------------

def pid_exists(pid: int) -> bool:
    """Check if a process with the given PID exists.

    Falls back to os.kill(pid, 0) if gateway.status is unavailable.
    """
    try:
        from gateway.status import _pid_exists as _real
        return _real(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't have permission to signal it
            return True


def apply_wal_with_fallback(conn: sqlite3.Connection, db_label: str = "kanban.db") -> None:
    """Apply WAL journal mode with fallback to DELETE for incompatible filesystems.

    Falls back to directly setting WAL mode if hermes_state is unavailable.
    """
    try:
        from hermes_state import apply_wal_with_fallback as _real
        _real(conn, db_label=db_label)
    except ImportError:
        # WAL doesn't work on NFS/SMB/FUSE — try WAL, fall back to DELETE
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            logger.debug("WAL mode set for %s", db_label)
        except sqlite3.OperationalError:
            conn.execute("PRAGMA journal_mode=DELETE")
            logger.warning("WAL mode unavailable for %s, using DELETE", db_label)