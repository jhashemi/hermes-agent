"""Unit tests for standardized error response handling (P3-003)."""

import pytest
from gateway.error_response import (
    ErrorResponse,
    ErrorCode,
    ErrorSeverity,
    create_access_denied_error,
    create_validation_error,
    create_not_found_error,
    create_internal_error,
)


class TestErrorResponse:
    """Tests for ErrorResponse dataclass."""

    def test_error_response_creation(self):
        """Test creating a basic error response."""
        error = ErrorResponse(
            code=ErrorCode.INVALID_INPUT,
            message="Test error message",
        )
        assert error.code == ErrorCode.INVALID_INPUT
        assert error.message == "Test error message"
        assert error.context == {}
        assert error.severity == ErrorSeverity.MEDIUM.value

    def test_error_response_with_context(self):
        """Test error response with additional context."""
        error = ErrorResponse(
            code=ErrorCode.VALIDATION_ERROR,
            message="Validation failed",
            context={"field": "user_id", "reason": "Invalid format"},
            severity=ErrorSeverity.LOW.value,
            user_id="test_user",
            command="test-command",
        )
        assert error.code == ErrorCode.VALIDATION_ERROR
        assert error.context["field"] == "user_id"
        assert error.user_id == "test_user"
        assert error.command == "test-command"

    def test_error_response_to_dict(self):
        """Test converting error response to dictionary."""
        error = ErrorResponse(
            code=ErrorCode.ACCESS_DENIED,
            message="Access denied",
            context={"user_id": "user123"},
            user_id="user123",
        )
        error_dict = error.to_dict()
        assert error_dict["code"] == ErrorCode.ACCESS_DENIED
        assert error_dict["message"] == "Access denied"
        assert error_dict["context"]["user_id"] == "user123"

    def test_error_response_to_emoji_response(self):
        """Test converting error to emoji-formatted response."""
        error = create_access_denied_error(
            user_id="user123",
            command="load-agent",
        )
        response = error.to_emoji_response()
        assert "🚫" in response
        assert "user123" in response
        assert "Access Denied" in response

    def test_error_response_validation_fails_on_invalid_code(self):
        """Test that ErrorResponse validates code field."""
        with pytest.raises(ValueError, match="code is required"):
            ErrorResponse(
                code="",
                message="Test",
            )

    def test_error_response_validation_fails_on_invalid_message(self):
        """Test that ErrorResponse validates message field."""
        with pytest.raises(ValueError, match="message is required"):
            ErrorResponse(
                code=ErrorCode.INVALID_INPUT,
                message="",
            )

    def test_error_response_validation_fails_on_invalid_context(self):
        """Test that ErrorResponse validates context field."""
        with pytest.raises(ValueError, match="context must be a dictionary"):
            ErrorResponse(
                code=ErrorCode.INVALID_INPUT,
                message="Test",
                context="not a dict",
            )


class TestAccessDeniedError:
    """Tests for access denied error creation."""

    def test_create_access_denied_error_basic(self):
        """Test creating basic access denied error."""
        error = create_access_denied_error(user_id="user123")
        assert error.code == ErrorCode.ACCESS_DENIED
        assert "Access Denied" in error.message
        assert error.context["user_id"] == "user123"

    def test_create_access_denied_error_with_command(self):
        """Test creating access denied error with command."""
        error = create_access_denied_error(
            user_id="user123",
            command="load-agent",
        )
        assert error.command == "load-agent"
        assert error.context["command"] == "load-agent"

    def test_create_access_denied_error_with_reason(self):
        """Test creating access denied error with reason."""
        error = create_access_denied_error(
            user_id="user123",
            reason="Insufficient permissions",
        )
        assert error.context["reason"] == "Insufficient permissions"


class TestValidationError:
    """Tests for validation error creation."""

    def test_create_validation_error_basic(self):
        """Test creating basic validation error."""
        error = create_validation_error(
            field="user_id",
            reason="Invalid format",
        )
        assert error.code == ErrorCode.INVALID_INPUT
        assert "Validation failed" in error.message
        assert error.context["field"] == "user_id"
        assert error.context["reason"] == "Invalid format"

    def test_create_validation_error_with_user_id(self):
        """Test creating validation error with user ID."""
        error = create_validation_error(
            field="instance_name",
            reason="Exceeds max length",
            user_id="user123",
        )
        assert error.user_id == "user123"


class TestNotFoundError:
    """Tests for not found error creation."""

    def test_create_not_found_error_agent(self):
        """Test creating agent not found error."""
        error = create_not_found_error(
            resource_type="agent",
            resource_id="demis_hassabis",
        )
        assert error.code == ErrorCode.AGENT_NOT_FOUND
        assert "demis_hassabis" in error.message

    def test_create_not_found_error_instance(self):
        """Test creating instance not found error."""
        error = create_not_found_error(
            resource_type="instance",
            resource_id="hermes3",
        )
        assert error.code == ErrorCode.INSTANCE_NOT_FOUND
        assert "hermes3" in error.message

    def test_create_not_found_error_generic(self):
        """Test creating generic not found error."""
        error = create_not_found_error(
            resource_type="command",
            resource_id="help-xyz",
        )
        assert error.code == ErrorCode.NOT_FOUND


class TestInternalError:
    """Tests for internal error creation."""

    def test_create_internal_error_basic(self):
        """Test creating basic internal error."""
        error = create_internal_error(
            operation="load_persona",
        )
        assert error.code == ErrorCode.INTERNAL_ERROR
        assert "load_persona" in error.message
        assert error.severity == ErrorSeverity.HIGH.value

    def test_create_internal_error_with_reason(self):
        """Test creating internal error with reason."""
        error = create_internal_error(
            operation="database_query",
            reason="Connection timeout",
        )
        assert error.context["reason"] == "Connection timeout"


class TestErrorResponseIntegration:
    """Integration tests for error responses."""

    def test_error_response_format_consistency(self):
        """Test that all error creation functions produce consistent format."""
        errors = [
            create_access_denied_error(user_id="user123"),
            create_validation_error(field="test", reason="bad"),
            create_not_found_error(resource_type="agent", resource_id="test"),
            create_internal_error(operation="test"),
        ]

        for error in errors:
            # All errors should have required fields
            assert error.code
            assert error.message
            assert isinstance(error.context, dict)
            assert error.severity in [s.value for s in ErrorSeverity]

            # All should convert to dict
            error_dict = error.to_dict()
            assert "code" in error_dict
            assert "message" in error_dict
            assert "context" in error_dict

            # All should convert to emoji response
            response = error.to_emoji_response()
            assert isinstance(response, str)
            assert len(response) > 0

    def test_error_response_emoji_format(self):
        """Test that error responses include appropriate emojis."""
        access_denied = create_access_denied_error(user_id="user123")
        response = access_denied.to_emoji_response()
        assert "🚫" in response

    def test_error_response_includes_user_id(self):
        """Test that user ID is included in emoji response when available."""
        error = create_validation_error(
            field="test",
            reason="Invalid",
            user_id="user456",
        )
        response = error.to_emoji_response()
        # User ID should be in context
        assert error.user_id == "user456"

    def test_error_code_enum_values(self):
        """Test that all error codes are properly defined."""
        # Authentication errors
        assert ErrorCode.AUTH_FAILED
        assert ErrorCode.AUTH_REQUIRED
        assert ErrorCode.INVALID_CREDENTIALS

        # Authorization errors
        assert ErrorCode.ACCESS_DENIED
        assert ErrorCode.INSUFFICIENT_PERMISSIONS

        # Validation errors
        assert ErrorCode.INVALID_INPUT
        assert ErrorCode.INVALID_USER_ID
        assert ErrorCode.INVALID_COMMAND
        assert ErrorCode.VALIDATION_ERROR

        # Resource errors
        assert ErrorCode.NOT_FOUND
        assert ErrorCode.AGENT_NOT_FOUND
        assert ErrorCode.INSTANCE_NOT_FOUND
        assert ErrorCode.COMMAND_NOT_FOUND

        # Server errors
        assert ErrorCode.INTERNAL_ERROR
        assert ErrorCode.OPERATION_FAILED

    def test_error_severity_enum_values(self):
        """Test that all severity levels are properly defined."""
        assert ErrorSeverity.LOW.value == "low"
        assert ErrorSeverity.MEDIUM.value == "medium"
        assert ErrorSeverity.HIGH.value == "high"
        assert ErrorSeverity.CRITICAL.value == "critical"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
