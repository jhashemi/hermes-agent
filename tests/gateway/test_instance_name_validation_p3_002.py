"""
Test suite for P3-002: Instance Name Validation

Tests validation of instance names in /switch-* commands:
- Alphanumeric characters (a-z, A-Z, 0-9) and hyphens only
- Maximum 64 characters
- No leading/trailing hyphens
- Return 400 Bad Request format errors for invalid names
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from gateway.agent_commands import validate_instance_name, handle_switch_instance_command
from gateway.platforms.base import MessageEvent


class TestValidateInstanceName:
    """Test the validate_instance_name() validation function."""

    def test_valid_simple_names(self):
        """Test valid simple alphanumeric instance names."""
        valid_names = [
            "local",
            "hermes2",
            "prod",
            "test",
            "app1",
            "server123",
        ]
        for name in valid_names:
            is_valid, error = validate_instance_name(name)
            assert is_valid is True, f"Should accept valid name: {name}"
            assert error is None, f"Should have no error for: {name}"

    def test_valid_names_with_hyphens(self):
        """Test valid names containing hyphens (but not at edges)."""
        valid_names = [
            "local-01",
            "prod-us-west",
            "hermes-2",
            "test-env",
            "my-instance",
            "app-server-01",
        ]
        for name in valid_names:
            is_valid, error = validate_instance_name(name)
            assert is_valid is True, f"Should accept valid hyphenated name: {name}"
            assert error is None

    def test_valid_max_length(self):
        """Test valid names at maximum length (64 characters)."""
        # 64 character name
        max_name = "a" * 64
        is_valid, error = validate_instance_name(max_name)
        assert is_valid is True
        assert error is None
        
        # Mixed characters at max length
        max_name2 = "prod-" + "a" * 59
        is_valid, error = validate_instance_name(max_name2)
        assert is_valid is True
        assert error is None

    def test_invalid_empty_name(self):
        """Test that empty names are rejected."""
        is_valid, error = validate_instance_name("")
        assert is_valid is False
        assert "empty" in error.lower()

    def test_invalid_whitespace_only(self):
        """Test that whitespace-only names are rejected."""
        is_valid, error = validate_instance_name("   ")
        assert is_valid is False
        # Whitespace after strip will fail character validation, not empty check
        assert "alphanumeric" in error.lower() or "empty" in error.lower()

    def test_invalid_too_long(self):
        """Test that names exceeding 64 characters are rejected."""
        # 65 character name
        long_name = "a" * 65
        is_valid, error = validate_instance_name(long_name)
        assert is_valid is False
        assert "64" in error
        assert "exceed" in error.lower()

    def test_invalid_special_characters(self):
        """Test that special characters are rejected."""
        invalid_names = [
            "instance!",
            "instance@home",
            "instance#1",
            "instance$test",
            "instance%prod",
            "instance&more",
            "instance*all",
            "instance(dev)",
            "instance[test]",
            "instance{prod}",
            "instance.test",
            "instance/test",
            "instance\\test",
            "instance|test",
            "instance:test",
            "instance;test",
            "instance,test",
            "instance<test",
            "instance>test",
            "instance?test",
            "instance=test",
            "instance+test",
            "instance~test",
            "instance^test",
            "instance`test",
            "instance test",  # Space
        ]
        for name in invalid_names:
            is_valid, error = validate_instance_name(name)
            assert is_valid is False, f"Should reject invalid name: {name}"
            assert "alphanumeric" in error.lower() or "hyphen" in error.lower()

    def test_invalid_leading_hyphen(self):
        """Test that names starting with hyphen are rejected."""
        is_valid, error = validate_instance_name("-instance")
        assert is_valid is False
        assert "cannot start" in error.lower()

    def test_invalid_trailing_hyphen(self):
        """Test that names ending with hyphen are rejected."""
        is_valid, error = validate_instance_name("instance-")
        assert is_valid is False
        assert "cannot" in error.lower() and "hyphen" in error.lower()

    def test_invalid_leading_and_trailing_hyphen(self):
        """Test that names with both leading and trailing hyphens are rejected."""
        is_valid, error = validate_instance_name("-instance-")
        assert is_valid is False

    def test_invalid_non_string(self):
        """Test that non-string types are rejected."""
        is_valid, error = validate_instance_name(None)
        assert is_valid is False
        assert "empty" in error.lower()

    def test_case_insensitive_acceptance(self):
        """Test that uppercase letters are accepted."""
        valid_names = [
            "PROD",
            "Local",
            "HerMes2",
            "MixedCase",
        ]
        for name in valid_names:
            is_valid, error = validate_instance_name(name)
            assert is_valid is True, f"Should accept mixed-case name: {name}"

    def test_numbers_at_start(self):
        """Test that names can start with numbers."""
        is_valid, error = validate_instance_name("2prod")
        assert is_valid is True
        assert error is None

    def test_consecutive_hyphens(self):
        """Test that consecutive hyphens in the middle are accepted."""
        is_valid, error = validate_instance_name("instance--test")
        assert is_valid is True
        assert error is None


class TestSwitchInstanceCommandValidation:
    """Test instance name validation in /switch-* commands."""

    @pytest.mark.asyncio
    async def test_valid_instance_name_in_command(self):
        """Test that valid instance names pass through the command handler."""
        # Create mock objects
        gateway_runner = MagicMock()
        event = MagicMock(spec=MessageEvent)
        event.chat_id = "test_chat"
        event.get_command_args = MagicMock(return_value="")
        
        # Mock access manager
        with patch('gateway.agent_commands.get_access_manager') as mock_access:
            mock_mgr = MagicMock()
            mock_mgr.has_access = MagicMock(return_value=True)
            mock_access.return_value = mock_mgr
            
            # Mock instance orchestrator
            with patch('gateway.agent_commands.InstanceOrchestrator') as mock_orch_class:
                mock_orch = MagicMock()
                mock_orch.set_current_instance = MagicMock(return_value=True)
                mock_orch.get_instance = MagicMock(return_value=MagicMock(
                    name="local",
                    is_local=True,
                    description="Local instance"
                ))
                mock_orch_class.return_value = mock_orch
                gateway_runner._instance_orchestrator = mock_orch
                
                # Call with valid instance name
                result = await handle_switch_instance_command(gateway_runner, event, "local")
                
                # Should succeed
                assert "Switched to" in result or "🟢" in result

    @pytest.mark.asyncio
    async def test_invalid_instance_name_too_long(self):
        """Test that instance names exceeding 64 characters are rejected with error."""
        gateway_runner = MagicMock()
        event = MagicMock(spec=MessageEvent)
        event.chat_id = "test_chat"
        event.get_command_args = MagicMock(return_value="")
        
        # Mock access manager
        with patch('gateway.agent_commands.get_access_manager') as mock_access:
            mock_mgr = MagicMock()
            mock_mgr.has_access = MagicMock(return_value=True)
            mock_mgr.get_user_id = MagicMock(return_value="user123")
            mock_access.return_value = mock_mgr
            
            # Call with too-long instance name (65 chars)
            long_name = "a" * 65
            result = await handle_switch_instance_command(gateway_runner, event, long_name)
            
            # Should return an error (either string or emoji format)
            result_str = str(result)
            assert "exceed" in result_str or "64" in result_str or "Invalid" in result_str

    @pytest.mark.asyncio
    async def test_invalid_instance_name_special_chars(self):
        """Test that instance names with special characters are rejected."""
        gateway_runner = MagicMock()
        event = MagicMock(spec=MessageEvent)
        event.chat_id = "test_chat"
        event.get_command_args = MagicMock(return_value="")
        
        # Mock access manager
        with patch('gateway.agent_commands.get_access_manager') as mock_access:
            mock_mgr = MagicMock()
            mock_mgr.has_access = MagicMock(return_value=True)
            mock_mgr.get_user_id = MagicMock(return_value="user123")
            mock_access.return_value = mock_mgr
            
            # Test various invalid names
            invalid_names = ["instance!", "instance@test", "instance#1", "instance test"]
            for invalid_name in invalid_names:
                result = await handle_switch_instance_command(gateway_runner, event, invalid_name)
                result_str = str(result)
                # Should contain error indicator
                assert "❌" in result_str or "Invalid" in result_str or "Validation" in result_str, f"Should reject: {invalid_name}"

    @pytest.mark.asyncio
    async def test_invalid_instance_name_leading_hyphen(self):
        """Test that instance names starting with hyphen are rejected."""
        gateway_runner = MagicMock()
        event = MagicMock(spec=MessageEvent)
        event.chat_id = "test_chat"
        event.get_command_args = MagicMock(return_value="")
        
        # Mock access manager
        with patch('gateway.agent_commands.get_access_manager') as mock_access:
            mock_mgr = MagicMock()
            mock_mgr.has_access = MagicMock(return_value=True)
            mock_mgr.get_user_id = MagicMock(return_value="user123")
            mock_access.return_value = mock_mgr
            
            result = await handle_switch_instance_command(gateway_runner, event, "-instance")
            result_str = str(result)
            assert "❌" in result_str or "Invalid" in result_str or "Validation" in result_str

    @pytest.mark.asyncio
    async def test_invalid_instance_name_trailing_hyphen(self):
        """Test that instance names ending with hyphen are rejected."""
        gateway_runner = MagicMock()
        event = MagicMock(spec=MessageEvent)
        event.chat_id = "test_chat"
        event.get_command_args = MagicMock(return_value="")
        
        # Mock access manager
        with patch('gateway.agent_commands.get_access_manager') as mock_access:
            mock_mgr = MagicMock()
            mock_mgr.has_access = MagicMock(return_value=True)
            mock_mgr.get_user_id = MagicMock(return_value="user123")
            mock_access.return_value = mock_mgr
            
            result = await handle_switch_instance_command(gateway_runner, event, "instance-")
            result_str = str(result)
            assert "❌" in result_str or "Invalid" in result_str or "Validation" in result_str

    @pytest.mark.asyncio
    async def test_valid_hyphenated_instance_name(self):
        """Test that valid hyphenated names are accepted."""
        gateway_runner = MagicMock()
        event = MagicMock(spec=MessageEvent)
        event.chat_id = "test_chat"
        event.get_command_args = MagicMock(return_value="")
        
        # Mock access manager
        with patch('gateway.agent_commands.get_access_manager') as mock_access:
            mock_mgr = MagicMock()
            mock_mgr.has_access = MagicMock(return_value=True)
            mock_access.return_value = mock_mgr
            
            # Mock instance orchestrator
            with patch('gateway.agent_commands.InstanceOrchestrator') as mock_orch_class:
                mock_orch = MagicMock()
                mock_orch.set_current_instance = MagicMock(return_value=True)
                mock_orch.get_instance = MagicMock(return_value=MagicMock(
                    name="prod-us-west",
                    is_local=False,
                    description="Production US West"
                ))
                mock_orch_class.return_value = mock_orch
                gateway_runner._instance_orchestrator = mock_orch
                
                result = await handle_switch_instance_command(gateway_runner, event, "prod-us-west")
                assert "Switched to" in result or "prod-us-west" in result


class TestErrorFormatting:
    """Test that error messages follow 400 Bad Request format."""

    def test_validation_error_includes_details(self):
        """Test that validation errors include helpful details."""
        long_name = "x" * 100
        is_valid, error = validate_instance_name(long_name)
        assert is_valid is False
        # Error should be informative
        assert "64" in error or "exceed" in error.lower()

    def test_special_char_error_mentions_allowed_chars(self):
        """Test that special char errors mention what's allowed."""
        is_valid, error = validate_instance_name("instance@test")
        assert is_valid is False
        assert "alphanumeric" in error.lower() or "character" in error.lower()


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_exactly_64_chars(self):
        """Test exactly 64 characters (boundary)."""
        name = "a" * 64
        is_valid, error = validate_instance_name(name)
        assert is_valid is True
        assert error is None

    def test_single_char_name(self):
        """Test single character names."""
        for char in "abcABC012":
            is_valid, error = validate_instance_name(char)
            assert is_valid is True, f"Should accept single char: {char}"

    def test_single_hyphen_is_invalid(self):
        """Test that a single hyphen by itself is invalid."""
        is_valid, error = validate_instance_name("-")
        assert is_valid is False
        assert "cannot start" in error.lower() or "cannot end" in error.lower()

    def test_only_numbers(self):
        """Test names that are only numbers."""
        is_valid, error = validate_instance_name("123456")
        assert is_valid is True
        assert error is None

    def test_unicode_characters_rejected(self):
        """Test that unicode/international characters are rejected."""
        invalid_names = [
            "instancé",      # é
            "instance_测试",   # Chinese
            "instance_ñ",    # ñ
            "instance_ü",    # ü
        ]
        for name in invalid_names:
            is_valid, error = validate_instance_name(name)
            assert is_valid is False, f"Should reject unicode name: {name}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
