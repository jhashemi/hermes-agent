"""hermes-kanban — SQLite-backed Kanban board for multi-profile, multi-project collaboration.

Extracted from hermes-agent's hermes_cli.kanban_db/kanban modules.
Zero external dependencies (stdlib only).
"""

from __future__ import annotations

__version__ = "0.1.0"

# Public API surface - import lazily to avoid loading sqlite on import
__all__ = [
    "kanban_db",
    "kanban",
    "kanban_diagnostics",
    "kanban_specify",
    "dispatch_cards",
]