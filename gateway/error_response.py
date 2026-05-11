"""Standardized error response handling for gateway endpoints.

This module provides a consistent ErrorResponse dataclass for all error
responses across the gateway, including auth failures, validation errors,
and other error conditions.

Error codes follow HTTP status conventions:
  - 400: Bad Request (validation errors)
  - 401: Unauthorized (authentication failures)
  - 403: Forbidden (access denied)
  - 404: Not Found (resource not found)
  - 500: Internal Server Error (unexpected errors)
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any
from enum import Enum


class ErrorCode(str, Enum):
    """Standardized error codes for gateway responses."""
    
    # Authentication errors (401)
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    
    # Authorization errors (403)
    ACCESS_DENIED = "ACCESS_DENIED"
    INSUFFICIENT_PERMISSIONS = "INSUFFICIENT_PERMISSIONS"
    
    # Validation errors (400)
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_USER_ID = "INVALID_USER_ID"
    INVALID_COMMAND = "INVALID_COMMAND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    
    # Resource errors (404)
    NOT_FOUND = "NOT_FOUND"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    INSTANCE_NOT_FOUND = "INSTANCE_NOT_FOUND"
    COMMAND_NOT_FOUND = "COMMAND_NOT_FOUND"
    
    # Server errors (500)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    OPERATION_FAILED = "OPERATION_FAILED"


class ErrorSeverity(str, Enum):
    """Error severity levels for classification."""
    
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ErrorResponse:
    """Standardized error response format.
    
    Attributes:
        code: Error code (enum value as string)
        message: User-facing error message
        context: Optional context dict with additional details
        severity: Error severity level
        user_id: Optional user ID for audit/logging
        command: Optional command name that triggered the error
    """
    
    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    severity: str = ErrorSeverity.MEDIUM.value
    user_id: Optional[str] = None
    command: Optional[str] = None
    
    def __post_init__(self):
        """Validate error response fields."""
        if not self.code:
            raise ValueError("code is required")
        if not self.message:
            raise ValueError("message is required")
        if not isinstance(self.context, dict):
            raise ValueError("context must be a dictionary")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)
    
    def to_string(self, include_context: bool = False) -> str:
        """Convert to user-friendly error string.
        
        Args:
            include_context: Whether to include context details in output
            
        Returns:
            Formatted error message
        """
        lines = [f"❌ Error: {self.message}"]
        
        if self.code:
            lines.append(f"Code: {self.code}")
        
        if include_context and self.context:
            for key, value in self.context.items():
                lines.append(f"{key.title()}: {value}")
        
        return "\n".join(lines)
    
    def to_emoji_response(self) -> str:
        """Convert to emoji-formatted response for chat platforms.
        
        Returns:
            Formatted error message with emojis
        """
        emoji = "🚫" if self.code == ErrorCode.ACCESS_DENIED else "❌"
        lines = [f"{emoji} {self.message}"]
        
        if self.context.get("user_id"):
            lines.append(f"Your ID: {self.context['user_id']}")
        
        if self.context.get("details"):
            lines.append(f"\n{self.context['details']}")
        
        return "\n\n".join([line for line in lines if line])


def create_access_denied_error(
    user_id: str,
    command: Optional[str] = None,
    reason: Optional[str] = None,
) -> ErrorResponse:
    """Create standardized access denied error.
    
    Args:
        user_id: User ID that was denied
        command: Optional command name
        reason: Optional reason for denial
        
    Returns:
        ErrorResponse instance
    """
    message = "Access Denied. You don't have permission to perform this action."
    
    context = {
        "user_id": user_id,
    }
    
    if command:
        context["command"] = command
    
    if reason:
        context["reason"] = reason
    
    return ErrorResponse(
        code=ErrorCode.ACCESS_DENIED,
        message=message,
        context=context,
        severity=ErrorSeverity.MEDIUM.value,
        user_id=user_id,
        command=command,
    )


def create_validation_error(
    field: str,
    reason: str,
    user_id: Optional[str] = None,
) -> ErrorResponse:
    """Create standardized validation error.
    
    Args:
        field: Field name that failed validation
        reason: Reason for validation failure
        user_id: Optional user ID
        
    Returns:
        ErrorResponse instance
    """
    return ErrorResponse(
        code=ErrorCode.INVALID_INPUT,
        message=f"Validation failed for '{field}': {reason}",
        context={
            "field": field,
            "reason": reason,
        },
        severity=ErrorSeverity.LOW.value,
        user_id=user_id,
    )


def create_not_found_error(
    resource_type: str,
    resource_id: str,
    user_id: Optional[str] = None,
) -> ErrorResponse:
    """Create standardized not found error.
    
    Args:
        resource_type: Type of resource not found (e.g., "agent", "instance")
        resource_id: ID/name of resource
        user_id: Optional user ID
        
    Returns:
        ErrorResponse instance
    """
    code_map = {
        "agent": ErrorCode.AGENT_NOT_FOUND,
        "instance": ErrorCode.INSTANCE_NOT_FOUND,
    }
    
    code = code_map.get(resource_type.lower(), ErrorCode.NOT_FOUND)
    
    return ErrorResponse(
        code=code,
        message=f"{resource_type.capitalize()} '{resource_id}' not found.",
        context={
            "resource_type": resource_type,
            "resource_id": resource_id,
        },
        severity=ErrorSeverity.LOW.value,
        user_id=user_id,
    )


def create_internal_error(
    operation: str,
    reason: Optional[str] = None,
    user_id: Optional[str] = None,
) -> ErrorResponse:
    """Create standardized internal error.
    
    Args:
        operation: Operation that failed
        reason: Optional reason for failure
        user_id: Optional user ID
        
    Returns:
        ErrorResponse instance
    """
    message = f"Could not complete '{operation}'. Please try again later."
    
    context = {
        "operation": operation,
    }
    
    if reason:
        context["reason"] = reason
    
    return ErrorResponse(
        code=ErrorCode.INTERNAL_ERROR,
        message=message,
        context=context,
        severity=ErrorSeverity.HIGH.value,
        user_id=user_id,
    )
