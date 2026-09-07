"""
Executive Board Plugin

Telegram-based command interface for multi-agent boardroom sessions with LiveKit
voice orchestration, OKR decision persistence, and governance enforcement.

Hard Constraints:
  - Prompt caching sacred (no per-command mutations)
  - Message role alternation strict (user ↔ assistant only)
  - No schema migrations (reads okr_accountability.db, kanban_board.db)
  - Voice agents off on hermes1, route to hermes2 only
  - Resemble TTS only
"""

__version__ = "1.0.0-skeleton"
__author__ = "NousResearch"

from .commands import (
    board_start,
    board_join,
    board_poll,
    board_archive,
    board_config,
    dispatch_command,
    init_plugin,
)

__all__ = [
    "board_start",
    "board_join",
    "board_poll",
    "board_archive",
    "board_config",
    "dispatch_command",
    "init_plugin",
]
