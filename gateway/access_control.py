"""Access control and user authorization for WhatsApp gateway.

Restricts agent persona loading and instance switching to approved users.
Default whitelist: Taylor Swanson, James Daily, Aunik Zaman, Setareh Hashemi.

Provides commands:
  /access-list        - Show who has access
  /access-grant       - Grant access to a new user
  /access-revoke      - Revoke access from a user
  /access-status      - Check if current user has access

Thread-safety: All methods are protected by threading.RLock() to prevent race
conditions during concurrent access to the whitelist and JSON file operations.
RLock (reentrant) is used so a lock-holding method can safely call another
lock-guarded helper without self-deadlocking. File writes use write-to-temp
+ os.replace() for atomicity, so a concurrent reader (or a crash mid-write)
never observes a partial/corrupted JSON file.

Audit Logging: All access grant/revoke operations are logged to ~/.hermes/audit.log
with timestamp, user_id, action, and grantor_id for compliance and debugging.
"""

import os
import json
import tempfile
import threading
import re
from typing import Set, Optional, Dict, Any, Union
from pathlib import Path
from datetime import datetime
from gateway.platforms.base import MessageEvent
from gateway.error_response import (
    ErrorResponse,
    ErrorCode,
    ErrorSeverity,
    EmojiIcon,
    create_access_denied_error,
    create_validation_error,
    format_info,
    format_success,
)


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

# Audit log file
AUDIT_LOG_FILE = Path(os.path.expanduser("~/.hermes/audit.log"))
AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# User ID validation regex: alphanumeric + underscore, max 256 chars
USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
MAX_USER_ID_LENGTH = 256


# ============================================================================
# User ID Validation
# ============================================================================

def validate_user_id(user_id: str) -> tuple[bool, Optional[str]]:
    """Validate user ID format.
    
    Constraints:
    - Max 256 characters
    - Only alphanumeric characters and underscores
    
    Args:
        user_id: The user ID to validate
        
    Returns:
        (is_valid, error_message) tuple
    """
    if not user_id or not isinstance(user_id, str):
        return False, "User ID must be a non-empty string"
    
    if len(user_id) > MAX_USER_ID_LENGTH:
        return False, f"User ID exceeds maximum length of {MAX_USER_ID_LENGTH} characters"
    
    if not USER_ID_PATTERN.match(user_id):
        return False, "User ID can only contain alphanumeric characters and underscores"
    
    return True, None


# ============================================================================
# Access Control Manager
# ============================================================================

class AccessControlManager:
    """Manages who can access restricted commands.
    
    Thread-safe: Uses threading.Lock() to protect all access to whitelist
    and JSON file operations. All public methods are atomic and safe for
    concurrent access from multiple threads.
    
    Includes audit logging for all access grant/revoke operations.
    """

    def __init__(self):
        self.whitelist: Set[str] = set(DEFAULT_WHITELIST)
        # RLock (reentrant) is used so a lock-holding method may safely call
        # another lock-guarded helper (e.g. grant_access -> _save_to_file)
        # without self-deadlocking. Required by P2-002 DoD.
        self._lock = threading.RLock()  # Protects whitelist and file I/O
        self._audit_lock = threading.RLock()  # Protects audit log I/O
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
        """Persist whitelist to disk atomically.

        Uses write-to-temp + os.replace() so a crash or concurrent reader
        never sees a partially-written / truncated JSON file. os.replace()
        is atomic on POSIX and Windows for same-filesystem renames.

        NOTE: Must be called with self._lock held.
        """
        try:
            data = {
                "whitelist": sorted(list(self.whitelist)),
                "description": "WhatsApp gateway access control",
            }
            payload = json.dumps(data, indent=2)
            target = ACCESS_CONTROL_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            # Temp file lives in the same directory so os.replace() stays
            # on the same filesystem (required for atomicity).
            fd, tmp_path = tempfile.mkstemp(
                prefix=".access_control.",
                suffix=".json.tmp",
                dir=str(target.parent),
            )
            try:
                with os.fdopen(fd, "w") as tmp:
                    tmp.write(payload)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, target)  # atomic rename
            except Exception:
                # Clean up the temp file if the rename never happened.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            import logging
            logging.error(f"Failed to save access control: {e}")

    def audit_log(self, user_id: str, action: str, grantor_id: str) -> None:
        """Log an audit trail entry for access control operations.
        
        Args:
            user_id: The user affected by the action
            action: The action performed ("grant" or "revoke")
            grantor_id: The user who performed the action
            
        Thread-safe: Acquires lock before writing to audit log.
        """
        if action not in ("grant", "revoke"):
            action = "unknown"
        
        timestamp = datetime.utcnow().isoformat() + "Z"
        log_entry = {
            "timestamp": timestamp,
            "user_id": user_id,
            "action": action,
            "grantor_id": grantor_id,
        }
        
        try:
            with self._audit_lock:
                with open(AUDIT_LOG_FILE, "a") as f:
                    f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            import logging
            logging.error(f"Failed to write audit log: {e}")

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

    def grant_access(self, user_id: str, grantor_id: str = "system") -> bool:
        """Grant access to a user. Returns True if newly added.
        
        Args:
            user_id: User ID to grant access to
            grantor_id: User who is granting access (for audit trail)
        
        Thread-safe: Acquires lock before modifying whitelist and saving.
        """
        with self._lock:
            if user_id in self.whitelist:
                return False  # Already had access
            self.whitelist.add(user_id)
            self._save_to_file()
        
        # Log the grant operation (outside lock to avoid deadlock)
        self.audit_log(user_id, "grant", grantor_id)
        return True

    def revoke_access(self, user_id: str, grantor_id: str = "system") -> bool:
        """Revoke access from a user. Returns True if was removed.
        
        Args:
            user_id: User ID to revoke access from
            grantor_id: User who is revoking access (for audit trail)
        
        Thread-safe: Acquires lock before modifying whitelist and saving.
        """
        with self._lock:
            if user_id not in self.whitelist:
                return False  # Didn't have access
            self.whitelist.discard(user_id)
            self._save_to_file()
        
        # Log the revoke operation (outside lock to avoid deadlock)
        self.audit_log(user_id, "revoke", grantor_id)
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
                error = create_access_denied_error(
                    user_id=user_id,
                    command=command_name,
                    reason=(
                        "You don't have permission to use this command. "
                        "Contact an administrator. Current access: /access-status"
                    ),
                )
                # Preserve the extra context detail the old hand-rolled string
                # carried (command name + hint) so operators still see it.
                error.context["details"] = (
                    f"Command: /{command_name}\n"
                    f"You don't have permission to use this command. "
                    f"Contact an administrator.\n\n"
                    f"Current access: /access-status"
                )
                return error.to_emoji_response()
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
) -> Union[str, ErrorResponse]:
    """Handle /access-grant <user_id> command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        user_id: User ID to grant access to

    Returns:
        Confirmation message or ErrorResponse
    """
    manager = get_access_manager()

    # Only allow Taylor Swanson (you) to grant access
    requester_id = manager.get_user_id(event)
    if requester_id not in DEFAULT_WHITELIST:
        error = create_access_denied_error(
            user_id=requester_id,
            command="access-grant",
            reason="Only administrators can grant access",
        )
        return error.to_emoji_response()

    if not user_id or user_id.strip() == "":
        error = ErrorResponse(
            code=ErrorCode.INVALID_COMMAND,
            message="Usage: /access-grant <user_id>",
            severity=ErrorSeverity.LOW.value,
            user_id=requester_id,
            command="access-grant",
        )
        return error.to_emoji_response()

    user_id = user_id.strip().lower()

    # Validate user ID format
    is_valid, error_msg = validate_user_id(user_id)
    if not is_valid:
        error = create_validation_error(
            field="user_id",
            reason=error_msg,
            user_id=requester_id,
        )
        return error.to_emoji_response()

    # Prevent granting to unknown users without confirmation
    if " " in user_id:
        error = create_validation_error(
            field="user_id",
            reason="User ID cannot contain spaces. Use underscores: john_doe",
            user_id=requester_id,
        )
        return error.to_emoji_response()

    newly_added = manager.grant_access(user_id, grantor_id=requester_id)

    if newly_added:
        return format_success(
            f"Granted access to **{user_id}**\\n\\n"
            f"They can now use agent and instance commands.\\n\\n"
            f"Current access: /access-list"
        )
    else:
        return format_info(f"User **{user_id}** already has access.")


async def handle_access_revoke_command(
    gateway_runner,
    event: MessageEvent,
    user_id: str,
) -> Union[str, ErrorResponse]:
    """Handle /access-revoke <user_id> command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        user_id: User ID to revoke access from

    Returns:
        Confirmation message or ErrorResponse
    """
    manager = get_access_manager()

    # Only allow default whitelist members to revoke
    requester_id = manager.get_user_id(event)
    if requester_id not in DEFAULT_WHITELIST:
        error = create_access_denied_error(
            user_id=requester_id,
            command="access-revoke",
            reason="Only administrators can revoke access",
        )
        return error.to_emoji_response()

    if not user_id or user_id.strip() == "":
        error = ErrorResponse(
            code=ErrorCode.INVALID_COMMAND,
            message="Usage: /access-revoke <user_id>",
            severity=ErrorSeverity.LOW.value,
            user_id=requester_id,
            command="access-revoke",
        )
        return error.to_emoji_response()

    user_id = user_id.strip().lower()

    # Validate user ID format
    is_valid, error_msg = validate_user_id(user_id)
    if not is_valid:
        error = create_validation_error(
            field="user_id",
            reason=error_msg,
            user_id=requester_id,
        )
        return error.to_emoji_response()

    # Prevent revoking access from default whitelist
    if user_id in DEFAULT_WHITELIST:
        error = ErrorResponse(
            code=ErrorCode.ACCESS_DENIED,
            message=f"Cannot revoke access from default administrator: **{user_id}**. To modify core administrators, edit the source code.",
            context={
                "user_id": user_id,
                "reason": "Default administrator",
            },
            severity=ErrorSeverity.MEDIUM.value,
            user_id=requester_id,
            command="access-revoke",
        )
        return error.to_emoji_response()

    was_removed = manager.revoke_access(user_id, grantor_id=requester_id)

    if was_removed:
        return format_success(
            f"Revoked access from **{user_id}**\\n\\n"
            f"They can no longer use agent and instance commands.\\n\\n"
            f"Current access: /access-list"
        )
    else:
        return format_info(f"User **{user_id}** did not have access to revoke.")


async def handle_access_status_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /access-status command."""
    manager = get_access_manager()
    user_id = manager.get_user_id(event)
    has_access = manager.has_access(event)

    status_icon = EmojiIcon.SUCCESS if has_access else EmojiIcon.ACCESS_DENIED
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
) -> Union[str, ErrorResponse]:
    """Check access before executing a restricted command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        command_name: Name of the command being executed
        handler_func: The handler function to call if access granted
        *args, **kwargs: Arguments to pass to handler

    Returns:
        Response from handler or ErrorResponse
    """
    manager = get_access_manager()

    if not manager.has_access(event):
        user_id = manager.get_user_id(event)
        error = create_access_denied_error(
            user_id=user_id,
            command=command_name,
            reason="You don't have permission to use this command. Contact an administrator.",
        )
        return error.to_emoji_response()

    # User has access, execute the handler
    return await handler_func(gateway_runner, event, *args, **kwargs)
