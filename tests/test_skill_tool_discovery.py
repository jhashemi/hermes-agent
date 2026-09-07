#!/usr/bin/env python3
"""Tests for VOICE-TOOL-04: Register Skill Tools"""

import json
import tempfile
from pathlib import Path
from typing import Dict

import pytest

from tools.registry import registry, ToolRegistry
from tools.skill_tool_discovery import (
    discover_skill_tools,
    _parse_yaml_frontmatter,
    _extract_tool_metadata,
    _validate_tool_schema,
)


class TestYAMLFrontmatterParsing:
    """Test YAML frontmatter extraction from markdown."""
    
    def test_parse_yaml_frontmatter_valid(self):
        """Test parsing valid YAML frontmatter."""
        content = """---
name: test-skill
version: 1.0.0
---

# Rest of content
"""
        result = _parse_yaml_frontmatter(content)
        assert result is not None
        assert result.get("name") == "test-skill"
        assert result.get("version") == "1.0.0"
    
    def test_parse_yaml_frontmatter_missing(self):
        """Test handling missing frontmatter."""
        content = "# Just markdown without frontmatter"
        result = _parse_yaml_frontmatter(content)
        assert result is None
    
    def test_parse_yaml_frontmatter_with_metadata(self):
        """Test parsing frontmatter with nested metadata."""
        content = """---
name: my-skill
metadata:
  hermes:
    tools:
      - name: my_tool
        description: A tool
---

Content here
"""
        result = _parse_yaml_frontmatter(content)
        assert result is not None
        assert result["name"] == "my-skill"
        assert "tools" in result["metadata"]["hermes"]
        assert len(result["metadata"]["hermes"]["tools"]) == 1


class TestToolSchemaValidation:
    """Test tool schema validation."""
    
    def test_validate_schema_valid(self):
        """Test validation of valid schema."""
        schema = {
            "type": "object",
            "properties": {
                "param1": {"type": "string"},
                "param2": {"type": "number"},
            },
        }
        assert _validate_tool_schema(schema) is True
    
    def test_validate_schema_missing_type(self):
        """Test validation fails without type field."""
        schema = {
            "properties": {"param": {"type": "string"}},
        }
        assert _validate_tool_schema(schema) is False
    
    def test_validate_schema_wrong_type(self):
        """Test validation fails for non-object types."""
        schema = {
            "type": "string",
            "properties": {},
        }
        assert _validate_tool_schema(schema) is False
    
    def test_validate_schema_missing_properties(self):
        """Test validation fails without properties."""
        schema = {
            "type": "object",
        }
        assert _validate_tool_schema(schema) is False
    
    def test_validate_schema_properties_wrong_type(self):
        """Test validation fails when properties is not a dict."""
        schema = {
            "type": "object",
            "properties": [],  # Should be dict
        }
        assert _validate_tool_schema(schema) is False


class TestSkillToolDiscovery:
    """Test skill tool discovery."""
    
    def test_discover_fixture_skill(self):
        """Test discovering tools from the fixture skill."""
        fixture_path = Path(__file__).parent / "fixtures" / "test-skill-with-tool"
        if not fixture_path.exists():
            pytest.skip("Fixture skill not found")
        
        discovered = discover_skill_tools(fixture_path.parent)
        
        # Should discover 2 tools from test-skill-with-tool
        tool_names = [name for _, entry in discovered for name in [entry["name"]]]
        assert "echo_message" in tool_names
        assert "add_numbers" in tool_names
        
        # Verify tool metadata
        for skill_name, tool_entry in discovered:
            if tool_entry["name"] == "echo_message":
                assert skill_name == "test-skill-with-tool"
                assert tool_entry["toolset"] == "test-skill"
                assert tool_entry["is_async"] is False
                assert "message" in tool_entry["schema"]["properties"]
                assert tool_entry["handler"] is not None
    
    def test_discover_nonexistent_directory(self):
        """Test behavior with nonexistent directory."""
        discovered = discover_skill_tools(Path("/nonexistent/path"))
        assert discovered == []
    
    def test_discover_empty_directory(self):
        """Test behavior with empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            discovered = discover_skill_tools(Path(tmpdir))
            assert discovered == []


class TestRegistryIntegration:
    """Test integration with tool registry."""
    
    def test_register_skill_tools(self):
        """Test registering skill tools via registry."""
        fixture_path = Path(__file__).parent / "fixtures" / "test-skill-with-tool"
        if not fixture_path.exists():
            pytest.skip("Fixture skill not found")
        
        # Create isolated registry for testing
        test_registry = ToolRegistry()
        
        # Register skill tools
        registered = test_registry.register_skill_tools(fixture_path.parent)
        
        # Should have registered 2 tools
        assert len(registered) == 2
        assert "echo_message" in registered
        assert "add_numbers" in registered
        
        # Verify tools are in registry
        echo_entry = test_registry.get_entry("echo_message")
        assert echo_entry is not None
        assert echo_entry.toolset == "test-skill"
        
        add_entry = test_registry.get_entry("add_numbers")
        assert add_entry is not None
        assert add_entry.toolset == "test-skill"
    
    def test_duplicate_tool_names_rejected(self):
        """Test that duplicate tool names are rejected."""
        fixture_path = Path(__file__).parent / "fixtures" / "test-skill-with-tool"
        if not fixture_path.exists():
            pytest.skip("Fixture skill not found")
        
        test_registry = ToolRegistry()
        
        # Pre-register a tool with the same name
        test_registry.register(
            name="echo_message",
            toolset="existing",
            schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            },
            handler=lambda args: json.dumps({"ok": True}),
        )
        
        # Try to register skill tools — echo_message should be rejected
        registered = test_registry.register_skill_tools(fixture_path.parent)
        
        # Should only register add_numbers
        assert "echo_message" not in registered
        assert "add_numbers" in registered
        
        # Existing tool should still be in registry
        entry = test_registry.get_entry("echo_message")
        assert entry is not None
        assert entry.toolset == "existing"  # Original toolset


class TestToolDispatch:
    """Test dispatching skill tools."""
    
    def test_echo_tool_dispatch(self):
        """Test dispatching echo_message tool."""
        fixture_path = Path(__file__).parent / "fixtures" / "test-skill-with-tool"
        if not fixture_path.exists():
            pytest.skip("Fixture skill not found")
        
        test_registry = ToolRegistry()
        test_registry.register_skill_tools(fixture_path.parent)
        
        # Dispatch echo_message
        result = test_registry.dispatch("echo_message", {"message": "hello world"})
        result_dict = json.loads(result)
        
        assert result_dict["success"] is True
        assert result_dict["echo"] == "hello world"
    
    def test_add_tool_dispatch(self):
        """Test dispatching add_numbers tool."""
        fixture_path = Path(__file__).parent / "fixtures" / "test-skill-with-tool"
        if not fixture_path.exists():
            pytest.skip("Fixture skill not found")
        
        test_registry = ToolRegistry()
        test_registry.register_skill_tools(fixture_path.parent)
        
        # Dispatch add_numbers
        result = test_registry.dispatch("add_numbers", {"a": 5, "b": 3})
        result_dict = json.loads(result)
        
        assert result_dict["success"] is True
        assert result_dict["result"] == 8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
