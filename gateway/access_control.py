"""Access control and user authorization for WhatsApp gateway.

Restricts agent persona loading and instance switching to approved users.
Default whitelist: Taylor Swanson, James Daily, Aunik Zaman, Setareh Hashemi.

Provides commands:
  /access-list        - Show who has access
  /access-grant       - Grant access to a new user
  /access-revoke      - Revoke access from a user
  /access-status      - Check if current user has access

Thread-safety: All methods are protected by threading.Lock() to prevent race
conditions during concurrent access to the whitelist and JSON file operations.
"""

import os
import json
import threading
from typing import Set, Optional, Dict, Any
from pathlib import Path
from gateway.platforms.base import MessageEvent


# ============================================================================
# Access Control Configuration
# ============================================================================

# Default whitelist (user phone numbers or IDs)
DEFAULT_WHITELIST: Set[str] = {
    "taylor_swanson",      # Taylor Swanson (you)
    "james_daily",         # James Daily
    "aunik_zaman",         # Aunik Zaman
    "setareh_hashemi",     # Setareh Hashemi
}

# File to persist access list (survives restarts)
ACCESS_CONTROL_FILE = Path(os.path.expanduser("~/.hermes/access_control.json"))
ACCESS_CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Access Control Manager
# ============================================================================

class AccessControlManager:
    """Manages who can access restricted commands.
    
    Thread-safe: Uses threading.Lock() to protect all access to whitelist
    and JSON file operations. All public methods are atomic and safe for
    concurrent access from multiple threads.
    """

    def __init__(self):
        self.whitelist: Set[str] = set(DEFAULT_WHITELIST)
        self._lock = threading.Lock()  # Protects whitelist and file I/O
        self._load_from_file()

    def _load_from_file(self):
        """Load whitelist from persistent storage.
        
        NOTE: Called from __init__ before any threads access this instance,
        so no lock needed here. Subsequent loads should acquire lock.
        """
        if ACCESS_CONTROL_FILE.exists():
            try:
                data = json.loads(ACCESS_CONTROL_FILE.read_text())
                if "whitelist" in data and isinstance(data["whitelist"], list):
                    self.whitelist = set(data["whitelist"])
                    return
            except Exception as e:
                import logging
                logging.warning(f"Failed to load access control: {e}")

        # Fall back to defaults
        self.whitelist = set(DEFAULT_WHITELIST)
        self._save_to_file()

    def _save_to_file(self):
        """Persist whitelist to disk.
        
        NOTE: Must be called with self._lock held.
        """
        try:
            data = {
                "whitelist": sorted(list(self.whitelist)),
                "description": "WhatsApp gateway access control",
            }
            ACCESS_CONTROL_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            import logging
            logging.error(f"Failed to save access control: {e}")

    def get_user_id(self, event: MessageEvent) -> str:
        """Extract unique user identifier from message event.

        Priority:
          1. event.user_id (WhatsApp contact ID)
          2. event.chat_id (falls back to chat ID)
          3. Fallback: unknown_user
        
        Thread-safe: No shared state accessed.
        """
        if hasattr(event, "user_id") and event.user_id:
            return event.user_id
        if hasattr(event, "chat_id") and event.chat_id:
            return event.chat_id
        return "unknown_user"

    def has_access(self, event: MessageEvent) -> bool:
        """Check if user in event has access to restricted commands.
        
        Thread-safe: Acquires lock before reading whitelist.
        """
        user_id = self.get_user_id(event)
        with self._lock:
            return user_id in self.whitelist

    def grant_access(self, user_id: str) -> bool:
        """Grant access to a user. Returns True if newly added.
        
        Thread-safe: Acquires lock before modifying whitelist and saving.
        """
        with self._lock:
            if user_id in self.whitelist:
                return False  # Already had access
            self.whitelist.add(user_id)
            self._save_to_file()
            return True

    def revoke_access(self, user_id: str) -> bool:
        """Revoke access from a user. Returns True if was removed.
        
        Thread-safe: Acquires lock before modifying whitelist and saving.
        """
        with self._lock:
            if user_id not in self.whitelist:
                return False  # Didn't have access
            self.whitelist.discard(user_id)
            self._save_to_file()
            return True

    def check_access(self, user_id: str) -> bool:
        """Check if a specific user ID has access.
        
        Thread-safe: Acquires lock before reading whitelist.
        This is a direct check without needing a MessageEvent.
        """
        with self._lock:
            return user_id in self.whitelist

    def list_users(self) -> str:
        """Format list of whitelisted users.
        
        Thread-safe: Acquires lock before reading whitelist.
        """
        with self._lock:
            if not self.whitelist:
                return "Access list is empty."

            lines = ["🔐 **Whitelisted Users:**\n"]
            for user_id in sorted(self.whitelist):
                lines.append(f"  • {user_id}")

            lines.append(f"\nTotal: {len(self.whitelist)} users")
            return "\n".join(lines)

    def reset_to_defaults(self) -> None:
        """Reset whitelist to default set.
        
        Thread-safe: Acquires lock before modifying whitelist and saving.
        """
        with self._lock:
            self.whitelist = set(DEFAULT_WHITELIST)
            self._save_to_file()


# Global instance (initialized once)
_access_manager: Optional[AccessControlManager] = None
_access_manager_lock = threading.Lock()


def get_access_manager() -> AccessControlManager:
    """Get or create the global access control manager.
    
    Thread-safe: Uses a separate lock to protect singleton initialization.
    """
    global _access_manager
    if _access_manager is None:
        with _access_manager_lock:
            if _access_manager is None:
                _access_manager = AccessControlManager()
    return _access_manager


# ============================================================================
# Restricted Command Decorator
# ============================================================================

class AccessDeniedError(Exception):
    """Raised when user lacks access to a command."""
    pass


def require_access(command_name: str):
    """Decorator to restrict command access.

    Usage:
        @require_access("load-demis")
        async def handle_load_agent_command(...):\
            ...
    """
    def decorator(func):
        async def wrapper(gateway_runner, event: MessageEvent, *args, **kwargs):
            manager = get_access_manager()
            if not manager.has_access(event):
                user_id = manager.get_user_id(event)
                return (
                    f"🚫 Access Denied\n\n"
                    f"User: {user_id}\n"
                    f"Command: /{command_name}\n\n"
                    f"You don't have permission to use this command. "
                    f"Contact an administrator.\n\n"
                    f"Current access: /access-status"
                )
            return await func(gateway_runner, event, *args, **kwargs)
        return wrapper
    return decorator


# ============================================================================
# Access Control Command Handlers
# ============================================================================

async def handle_access_list_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /access-list command."""
    manager = get_access_manager()
    return manager.list_users()


async def handle_access_grant_command(
    gateway_runner,
    event: MessageEvent,
    user_id: str,
) -> str:
    """Handle /access-grant <user_id> command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        user_id: User ID to grant access to

    Returns:
        Confirmation message
    """
    manager = get_access_manager()

    # Only allow Taylor Swanson (you) to grant access
    requester_id = manager.get_user_id(event)
    if requester_id not in DEFAULT_WHITELIST:
        return (
            f"🚫 Only administrators can grant access.\n"
            f"Your ID: {requester_id}"
        )

    if not user_id or user_id.strip() == "":
        return "❌ Usage: /access-grant <user_id>"

    user_id = user_id.strip().lower()

    # Prevent granting to unknown users without confirmation
    if " " in user_id:
        return "❌ User ID cannot contain spaces. Use underscores: john_doe"

    newly_added = manager.grant_access(user_id)

    if newly_added:
        return (
            f"✅ Granted access to **{user_id}**\n\n"
            f"They can now use agent and instance commands.\n\n"
            f"Current access: /access-list"
        )
    else:
        return f"ℹ️ User **{user_id}** already has access."


async def handle_access_revoke_command(
    gateway_runner,
    event: MessageEvent,
    user_id: str,
) -> str:
    """Handle /access-revoke <user_id> command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        user_id: User ID to revoke access from

    Returns:
        Confirmation message
    """
    manager = get_access_manager()

    # Only allow default whitelist members to revoke
    requester_id = manager.get_user_id(event)
    if requester_id not in DEFAULT_WHITELIST:
        return (
            f"🚫 Only administrators can revoke access.\n"
            f"Your ID: {requester_id}"
        )

    if not user_id or user_id.strip() == "":
        return "❌ Usage: /access-revoke <user_id>"

    user_id = user_id.strip().lower()

    # Prevent revoking access from default whitelist
    if user_id in DEFAULT_WHITELIST:
        return (
            f"🔒 Cannot revoke access from default administrator: **{user_id}**\n\n"
            f"To modify core administrators, edit the source code."
        )

    was_removed = manager.revoke_access(user_id)

    if was_removed:
        return (
            f"✅ Revoked access from **{user_id}**\n\n"
            f"They can no longer use agent and instance commands.\n\n"
            f"Current access: /access-list"
        )
    else:
        return f"ℹ️ User **{user_id}** did not have access to revoke."


async def handle_access_status_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /access-status command."""
    manager = get_access_manager()
    user_id = manager.get_user_id(event)
    has_access = manager.has_access(event)

    status_icon = "✅" if has_access else "🚫"
    permission = "Granted" if has_access else "Denied"

    return (
        f"{status_icon} **Access Status**\n\n"
        f"User ID: {user_id}\n"
        f"Permission: {permission}\n\n"
        f"Available commands:\n"
        f"  /access-list      - Show all whitelisted users\n"
        f"  /access-status    - Check your permission\n"
        f"  /access-grant     - Grant access (admin only)\n"
        f"  /access-revoke    - Revoke access (admin only)"
    )


# Access control command registry
ACCESS_COMMAND_HANDLERS = {
    "access-list": handle_access_list_command,
    "access-status": handle_access_status_command,
    "access-grant": handle_access_grant_command,  # Requires user_id param
    "access-revoke": handle_access_revoke_command,  # Requires user_id param
}


def is_access_command(command_name: Optional[str]) -> bool:
    """Check if a command is an access control command."""
    if not command_name:
        return False
    canonical = command_name.lower().lstrip("/")
    return canonical in ACCESS_COMMAND_HANDLERS


def get_access_command_handler(command_name: str):
    """Get handler for an access control command."""
    canonical = command_name.lower().lstrip("/")
    return ACCESS_COMMAND_HANDLERS.get(canonical)


# ============================================================================
# Restricted Command Wrappers
# ============================================================================

async def check_access_and_execute(
    gateway_runner,
    event: MessageEvent,
    command_name: str,
    handler_func,
    *args,
    **kwargs,
) -> str:
    """Check access before executing a restricted command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        command_name: Name of the command being executed
        handler_func: The handler function to call if access granted
        *args, **kwargs: Arguments to pass to handler

    Returns:
        Response from handler or access denied message
    """
    manager = get_access_manager()

    if not manager.has_access(event):
        user_id = manager.get_user_id(event)
        return (
            f"🚫 Access Denied\n\n"
            f"User: {user_id}\n"
            f"Command: /{command_name}\n\n"
            f"You don't have permission to use this command. "
            f"Contact an administrator.\n\n"
            f"Status: /access-status"
        )

    # User has access, execute the handler
    return await handler_func(gateway_runner, event, *args, **kwargs)
