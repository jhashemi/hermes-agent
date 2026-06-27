#!/usr/bin/env python3
"""
Skill Tool Discovery

Auto-discovers tools registered within SKILL.md files from ~/.hermes/skills/.
Skills declare custom tools via metadata.hermes.tools in their YAML frontmatter,
including handler module/function references that are resolved at discovery time.

SKILL.md Tool Format (in frontmatter):
    ---
    name: my-skill
    description: ...
    metadata:
      hermes:
        tools:
          - name: my_tool
            description: Tool description
            toolset: my-skill
            schema:
              type: object
              properties:
                param1:
                  type: string
                  description: ...
              required: [param1]
            handler_module: my_skill_tool_handler  # file in skill dir
            handler_function: handle_my_tool        # function in that file
            requires_env: [API_KEY]
            is_async: false
            emoji: 📝
    ---

Discovery Flow:
    1. Scan ~/.hermes/skills/ for all SKILL.md files
    2. Parse YAML frontmatter → extract tools array
    3. For each tool, validate schema + resolve handler
    4. Register with tools.registry.ToolRegistry
    5. Return list of registered tool names for logging
"""

import ast
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_yaml_frontmatter(content: str) -> Optional[Dict]:
    """Extract YAML frontmatter from markdown content.
    
    Expects content to start with --- and have closing --- on its own line.
    Returns parsed YAML dict or None if no frontmatter found.
    """
    if not content.startswith("---"):
        return None
    
    lines = content.split("\n")
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    
    if close_idx is None:
        return None
    
    frontmatter_text = "\n".join(lines[1:close_idx])
    
    try:
        import yaml
        return yaml.safe_load(frontmatter_text) or {}
    except Exception as e:
        logger.debug("Failed to parse YAML frontmatter: %s", e)
        return None


def _extract_tool_metadata(skill_path: Path) -> Optional[List[Dict]]:
    """Extract tools array from a skill's SKILL.md frontmatter.
    
    Returns list of tool dicts with keys: name, description, toolset, schema,
    handler_module, handler_function, requires_env, is_async, emoji.
    Returns None if no tools defined or file doesn't exist.
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None
    
    try:
        content = skill_md.read_text(encoding="utf-8")
        frontmatter = _parse_yaml_frontmatter(content)
        if not frontmatter:
            return None
        
        tools = frontmatter.get("metadata", {}).get("hermes", {}).get("tools", [])
        return tools if tools else None
    except Exception as e:
        logger.debug("Error extracting tools from %s: %s", skill_md, e)
        return None


def _validate_tool_schema(schema: Dict) -> bool:
    """Validate that a tool schema is a valid OpenAI function schema.
    
    Checks for required fields: name, description, properties, type.
    """
    if not isinstance(schema, dict):
        return False
    
    # Basic schema validation — must be object type with properties
    if schema.get("type") != "object":
        logger.debug("Schema type must be 'object', got %s", schema.get("type"))
        return False
    
    if "properties" not in schema:
        logger.debug("Schema missing 'properties' field")
        return False
    
    if not isinstance(schema["properties"], dict):
        logger.debug("Schema 'properties' must be a dict")
        return False
    
    return True


def _resolve_handler(
    skill_path: Path,
    handler_module: str,
    handler_function: str,
) -> Optional[Callable]:
    """Dynamically import and resolve a handler function from a skill.
    
    Adds skill directory to sys.path temporarily to allow relative imports,
    then restores it. Returns callable or None if resolution fails.
    
    Args:
        skill_path: Path to the skill directory
        handler_module: Module name (relative to skill) e.g. "my_tool_handler"
        handler_function: Function name within that module e.g. "handle_my_tool"
    """
    skill_dir = str(skill_path.resolve())
    
    # Temporarily add skill dir to path for relative imports
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    
    try:
        # Try to import the handler module
        try:
            # First try as a qualified import (skill module)
            module = importlib.import_module(handler_module)
        except ImportError:
            # Fall back to direct import if qualified name fails
            spec = importlib.util.spec_from_file_location(
                handler_module,
                skill_path / f"{handler_module}.py",
            )
            if not spec or not spec.loader:
                logger.error(
                    "Could not find handler module %s in skill %s",
                    handler_module,
                    skill_path.name,
                )
                return None
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[handler_module] = module
            spec.loader.exec_module(module)
        
        # Get the handler function
        handler = getattr(module, handler_function, None)
        if not callable(handler):
            logger.error(
                "Handler function %s.%s is not callable",
                handler_module,
                handler_function,
            )
            return None
        
        logger.debug(
            "Resolved handler %s.%s from skill %s",
            handler_module,
            handler_function,
            skill_path.name,
        )
        return handler
    except Exception as e:
        logger.error(
            "Error resolving handler %s.%s from skill %s: %s",
            handler_module,
            handler_function,
            skill_path.name,
            e,
        )
        return None
    finally:
        # Clean up sys.path
        if skill_dir in sys.path:
            sys.path.remove(skill_dir)


def discover_skill_tools(skills_dir: Path) -> List[Tuple[str, Dict]]:
    """Discover all tools defined in SKILL.md files.
    
    Scans skills_dir recursively for SKILL.md files, extracts tool metadata,
    and validates each tool's schema and handler. Returns list of
    (skill_name, validated_tool_metadata) tuples ready for registration.
    
    Args:
        skills_dir: Path to ~/.hermes/skills/ directory
    
    Returns:
        List of (skill_name, tool_dict) tuples. Each tool_dict has:
        - name, description, toolset, schema (validated)
        - handler (resolved callable)
        - requires_env, is_async, emoji
        
        Tools with invalid schemas or missing handlers are skipped with
        logged warnings.
    """
    skills_dir = Path(skills_dir).resolve()
    if not skills_dir.exists():
        logger.debug("Skills directory does not exist: %s", skills_dir)
        return []
    
    discovered_tools: List[Tuple[str, Dict]] = []
    
    # Find all SKILL.md files recursively
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        skill_path = skill_md.parent
        skill_name = skill_path.name
        
        # Extract tools array from SKILL.md
        tools_metadata = _extract_tool_metadata(skill_path)
        if not tools_metadata:
            continue
        
        # Process each tool
        for tool_spec in tools_metadata:
            try:
                # Validate required fields
                tool_name = tool_spec.get("name")
                if not tool_name:
                    logger.warning(
                        "Tool in skill %s missing 'name' field",
                        skill_name,
                    )
                    continue
                
                schema = tool_spec.get("schema", {})
                if not _validate_tool_schema(schema):
                    logger.warning(
                        "Tool %s in skill %s has invalid schema",
                        tool_name,
                        skill_name,
                    )
                    continue
                
                # Resolve handler
                handler_module = tool_spec.get("handler_module")
                handler_function = tool_spec.get("handler_function")
                if not handler_module or not handler_function:
                    logger.warning(
                        "Tool %s in skill %s missing handler_module or handler_function",
                        tool_name,
                        skill_name,
                    )
                    continue
                
                handler = _resolve_handler(skill_path, handler_module, handler_function)
                if not handler:
                    logger.warning(
                        "Could not resolve handler for tool %s in skill %s",
                        tool_name,
                        skill_name,
                    )
                    continue
                
                # Build registration-ready metadata
                toolset = tool_spec.get("toolset", f"skill-{skill_name}")
                requires_env = tool_spec.get("requires_env", [])
                is_async = tool_spec.get("is_async", False)
                emoji = tool_spec.get("emoji", "📝")
                description = tool_spec.get("description", "")
                
                # Ensure schema has name field (required by OpenAI)
                schema_with_name = {**schema, "name": tool_name}
                
                tool_entry = {
                    "name": tool_name,
                    "toolset": toolset,
                    "schema": schema_with_name,
                    "handler": handler,
                    "check_fn": None,  # Skills don't have availability checks
                    "requires_env": requires_env,
                    "is_async": is_async,
                    "description": description,
                    "emoji": emoji,
                    "max_result_size_chars": None,
                }
                
                discovered_tools.append((skill_name, tool_entry))
                logger.info(
                    "Discovered tool %s from skill %s",
                    tool_name,
                    skill_name,
                )
            
            except Exception as e:
                logger.error(
                    "Error processing tool in skill %s: %s",
                    skill_name,
                    e,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                continue
    
    return discovered_tools
