#!/usr/bin/env python3
"""
Unified Tool Registry for Hermes Agent

Central single-source-of-truth registry that consolidates ALL tools:
  - Built-in tools (terminal, file, web, browser, etc.)
  - MCP tools (Serena, Nexus, n8n, etc.)
  - Skill-registered tools
  - Voice-specific tools

This module replaces fragmented discovery logic across:
  - tools/registry.py (built-in tools)
  - model_tools.py (discovery orchestration)
  - MCP integration points
  - Skill tool discovery

Architecture:
  UnifiedToolRegistry
    ├─ Built-in tool registry (tools/registry.py::ToolRegistry) → _builtin_registry
    ├─ MCP tool registry (MCP server discovery) → _mcp_tools
    ├─ Skill tool registry (skill tool loader) → _skill_tools
    ├─ Voice tool registry (voice-specific tools) → _voice_tools
    └─ Tool metadata aggregation & mutation tracking

Thread-safe with generation counters for cache invalidation.
Single point of access for all consumers (run_agent.py, cli.py, gateway, ACP).
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Callable, Dict, List, Optional, Set, Tuple, Any, Union,
    FrozenSet
)
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# Type Definitions
# =============================================================================

class ToolSource(str, Enum):
    """Enum for tool origin classification."""
    BUILTIN = "builtin"
    MCP = "mcp"
    SKILL = "skill"
    VOICE = "voice"
    PLUGIN = "plugin"


class ToolAvailability(str, Enum):
    """Enum for tool availability status."""
    AVAILABLE = "available"
    CHECK_FAILED = "check_failed"
    UNAVAILABLE = "unavailable"
    REQUIRES_SETUP = "requires_setup"


@dataclass(frozen=True)
class ToolMetadata:
    """Complete metadata for any registered tool.

    Unified schema across all tool sources (built-in, MCP, skill, voice).
    Frozen for hashability; mutation via registry.mutate_tool_metadata().
    """
    name: str
    source: ToolSource
    toolset: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[Callable] = None
    check_fn: Optional[Callable] = None
    requires_env: List[str] = field(default_factory=list)
    is_async: bool = False
    emoji: str = ""
    max_result_size_chars: Optional[Union[int, float]] = None
    voice_aliases: List[str] = field(default_factory=list)
    mcp_server_id: Optional[str] = None  # For MCP tools: server_name
    skill_name: Optional[str] = None     # For skill tools: skill directory name
    tags: List[str] = field(default_factory=list)
    capability_categories: List[str] = field(default_factory=list)
    availability: ToolAvailability = ToolAvailability.AVAILABLE
    availability_reason: str = ""
    deprecated: bool = False
    preferred_in_voice: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict (JSON-compatible, excludes callables)."""
        return {
            "name": self.name,
            "source": self.source.value,
            "toolset": self.toolset,
            "description": self.description,
            "schema": self.schema,
            "requires_env": self.requires_env,
            "is_async": self.is_async,
            "emoji": self.emoji,
            "max_result_size_chars": self.max_result_size_chars,
            "voice_aliases": self.voice_aliases,
            "mcp_server_id": self.mcp_server_id,
            "skill_name": self.skill_name,
            "tags": self.tags,
            "capability_categories": self.capability_categories,
            "availability": self.availability.value,
            "availability_reason": self.availability_reason,
            "deprecated": self.deprecated,
            "preferred_in_voice": self.preferred_in_voice,
        }

    @property
    def key(self) -> Tuple[str, ToolSource]:
        """Unique key combining name + source (MCP can override MCP)."""
        return (self.name, self.source)


@dataclass
class ToolRegistrySnapshot:
    """Coherent snapshot of registry state at a point in time."""
    tools: Dict[str, ToolMetadata]
    toolsets: Dict[str, List[str]]  # toolset_name -> [tool_names]
    sources_present: Set[ToolSource]
    generation: int
    timestamp: float


# =============================================================================
# Check Function Cache
# =============================================================================

_CHECK_FN_CACHE_TTL_SECONDS = 30.0
_check_fn_cache: Dict[int, Tuple[float, ToolAvailability, str]] = {}
_check_fn_cache_lock = threading.Lock()


def _check_fn_cached(
    check_fn: Callable,
    tool_name: str,
) -> Tuple[ToolAvailability, str]:
    """Cache check_fn results with TTL.

    Returns (availability_status, reason_string).
    TTL chosen so env changes (hermes tools enable) propagate in ~30s.
    """
    fn_id = id(check_fn)
    now = time.monotonic()

    with _check_fn_cache_lock:
        cached = _check_fn_cache.get(fn_id)
        if cached:
            ts, avail, reason = cached
            if now - ts < _CHECK_FN_CACHE_TTL_SECONDS:
                logger.debug(
                    "Check cache hit for %s (TTL remaining: %.1fs)",
                    tool_name, _CHECK_FN_CACHE_TTL_SECONDS - (now - ts)
                )
                return avail, reason

    # Call check_fn
    try:
        result = bool(check_fn())
        avail = ToolAvailability.AVAILABLE if result else ToolAvailability.UNAVAILABLE
        reason = ""
        logger.debug("Check passed for %s", tool_name)
    except Exception as e:
        avail = ToolAvailability.CHECK_FAILED
        reason = f"Check error: {type(e).__name__}: {e}"
        logger.debug("Check failed for %s: %s", tool_name, reason)

    with _check_fn_cache_lock:
        _check_fn_cache[fn_id] = (now, avail, reason)

    return avail, reason


def invalidate_check_fn_cache() -> None:
    """Drop all cached check_fn results (called after config changes)."""
    with _check_fn_cache_lock:
        _check_fn_cache.clear()
        logger.info("Check function cache cleared")


# =============================================================================
# UnifiedToolRegistry
# =============================================================================

class UnifiedToolRegistry:
    """
    Central registry consolidating all tool sources.

    Single point of access for:
      - Tool discovery (built-in, MCP, skill, voice)
      - Tool metadata & availability
      - Handler dispatch
      - Voice-specific lookups
      - Schema generation for API calls
      - Mutation tracking (generation counters)

    Thread-safe. Supports dynamic MCP tool list changes without full reload.
    """

    def __init__(self):
        """Initialize unified registry with all sources."""
        self._tools: Dict[str, ToolMetadata] = {}
        self._toolsets: Dict[str, Set[str]] = {}  # toolset -> {tool_names}
        self._voice_index: Dict[str, str] = {}    # voice_alias -> tool_name
        self._sources: Set[ToolSource] = set()

        # Generation counter for cache invalidation
        self._generation: int = 0
        self._lock = threading.RLock()

        # Lazy-loaded references to sub-registries (avoid circular imports)
        self._builtin_registry = None
        self._mcp_registry = None
        self._skill_registry = None

        # Mutation tracking
        self._mutations: List[Dict[str, Any]] = []
        self._max_mutations_to_track = 100

    # =========================================================================
    # Initialization & Loading
    # =========================================================================

    def _get_builtin_registry(self):
        """Lazy-load built-in tool registry (from tools/registry.py)."""
        if self._builtin_registry is None:
            try:
                from tools.registry import registry as builtin_reg, discover_builtin_tools
                # Ensure tools are discovered first
                discover_builtin_tools()
                self._builtin_registry = builtin_reg
                logger.info("Loaded built-in tool registry")
            except ImportError as e:
                logger.warning("Built-in registry unavailable: %s", e)
        return self._builtin_registry

    def load_builtin_tools(self) -> int:
        """Import built-in tool modules and load from registry.

        Returns count of tools loaded.
        """
        with self._lock:
            reg = self._get_builtin_registry()
            if not reg:
                return 0

            # Iterate through all registered tools in built-in registry
            count = 0
            for tool_name in reg.get_all_tool_names():
                entry = reg.get_entry(tool_name)
                if not entry:
                    continue

                metadata = ToolMetadata(
                    name=tool_name,
                    source=ToolSource.BUILTIN,
                    toolset=entry.toolset,
                    description=entry.description or "",
                    schema=entry.schema,
                    handler=entry.handler,
                    check_fn=entry.check_fn,
                    requires_env=entry.requires_env,
                    is_async=entry.is_async,
                    emoji=entry.emoji,
                    max_result_size_chars=entry.max_result_size_chars,
                )
                self._register_tool_metadata(metadata)
                count += 1

            logger.info("Loaded %d built-in tools", count)
            return count

    def load_mcp_tools(self, mcp_tools_dict: Dict[str, Dict[str, Any]]) -> int:
        """Load tools from MCP servers.

        Args:
            mcp_tools_dict: {tool_name: {schema, mcp_server_id, ...}}

        Returns count of tools loaded.
        """
        with self._lock:
            count = 0
            for tool_name, tool_info in mcp_tools_dict.items():
                schema = tool_info.get("schema", {})
                server_id = tool_info.get("mcp_server_id", "unknown")

                metadata = ToolMetadata(
                    name=tool_name,
                    source=ToolSource.MCP,
                    toolset=f"mcp-{server_id}",
                    description=schema.get("description", ""),
                    schema=schema,
                    handler=None,  # MCP handlers are remote
                    mcp_server_id=server_id,
                    tags=tool_info.get("tags", []),
                    capability_categories=tool_info.get("capability_categories", []),
                )
                self._register_tool_metadata(metadata)
                count += 1

            logger.info("Loaded %d MCP tools from %d servers",
                       count, len({t.get("mcp_server_id") for t in mcp_tools_dict.values()}))
            return count

    def load_skill_tools(self, skill_tools_dict: Dict[str, Dict[str, Any]]) -> int:
        """Load tools registered by skills.

        Args:
            skill_tools_dict: {tool_name: {skill_name, schema, ...}}

        Returns count of tools loaded.
        """
        with self._lock:
            count = 0
            for tool_name, tool_info in skill_tools_dict.items():
                skill_name = tool_info.get("skill_name", "unknown")
                schema = tool_info.get("schema", {})

                metadata = ToolMetadata(
                    name=tool_name,
                    source=ToolSource.SKILL,
                    toolset=f"skill-{skill_name}",
                    description=schema.get("description", ""),
                    schema=schema,
                    skill_name=skill_name,
                    tags=tool_info.get("tags", []),
                )
                self._register_tool_metadata(metadata)
                count += 1

            logger.info("Loaded %d skill tools from %d skills",
                       count, len({t.get("skill_name") for t in skill_tools_dict.values()}))
            return count

    def load_voice_tools(self, voice_tools_dict: Dict[str, Dict[str, Any]]) -> int:
        """Load voice-specific tools.

        Args:
            voice_tools_dict: {tool_name: {schema, voice_aliases, ...}}

        Returns count of tools loaded.
        """
        with self._lock:
            count = 0
            for tool_name, tool_info in voice_tools_dict.items():
                schema = tool_info.get("schema", {})
                aliases = tool_info.get("voice_aliases", [])

                metadata = ToolMetadata(
                    name=tool_name,
                    source=ToolSource.VOICE,
                    toolset="voice",
                    description=schema.get("description", ""),
                    schema=schema,
                    voice_aliases=aliases,
                    preferred_in_voice=tool_info.get("preferred_in_voice", False),
                )
                self._register_tool_metadata(metadata)
                count += 1

            logger.info("Loaded %d voice tools", count)
            return count

    # =========================================================================
    # Internal Registration
    # =========================================================================

    def _register_tool_metadata(self, metadata: ToolMetadata) -> None:
        """Register a single tool (internal, called by load_* methods).

        Thread-unsafe — call within self._lock.
        """
        existing = self._tools.get(metadata.name)
        if existing:
            # Allow MCP→MCP overwrites; otherwise log as collision
            if existing.source == ToolSource.MCP and metadata.source == ToolSource.MCP:
                logger.debug("MCP tool '%s' overwriting previous MCP version", metadata.name)
            else:
                logger.warning(
                    "Tool '%s' (source=%s) shadows existing tool (source=%s)",
                    metadata.name, metadata.source.value, existing.source.value
                )

        self._tools[metadata.name] = metadata
        self._sources.add(metadata.source)

        # Update toolset index
        if metadata.toolset not in self._toolsets:
            self._toolsets[metadata.toolset] = set()
        self._toolsets[metadata.toolset].add(metadata.name)

        # Update voice index
        for alias in metadata.voice_aliases:
            self._voice_index[alias] = metadata.name

        self._generation += 1
        self._record_mutation("register", metadata.name, metadata.source.value)

    def _record_mutation(self, op: str, tool_name: str, source: str) -> None:
        """Record a registry mutation for auditing."""
        self._mutations.append({
            "op": op,
            "tool_name": tool_name,
            "source": source,
            "timestamp": time.time(),
            "generation": self._generation,
        })
        if len(self._mutations) > self._max_mutations_to_track:
            self._mutations.pop(0)

    # =========================================================================
    # Query API
    # =========================================================================

    def get_tool(self, name: str) -> Optional[ToolMetadata]:
        """Return tool metadata by name, or None."""
        with self._lock:
            return self._tools.get(name)

    def get_all_tools(self) -> List[ToolMetadata]:
        """Return all registered tools as a list."""
        with self._lock:
            return list(self._tools.values())

    def get_tools_by_source(self, source: ToolSource) -> List[ToolMetadata]:
        """Return all tools from a specific source."""
        with self._lock:
            return [t for t in self._tools.values() if t.source == source]

    def get_tools_by_toolset(self, toolset: str) -> List[ToolMetadata]:
        """Return all tools in a given toolset."""
        with self._lock:
            tool_names = self._toolsets.get(toolset, set())
            return [self._tools[name] for name in sorted(tool_names)]

    def get_toolset_names(self) -> List[str]:
        """Return sorted list of all toolset names."""
        with self._lock:
            return sorted(self._toolsets.keys())

    def get_tool_for_voice_alias(self, alias: str) -> Optional[ToolMetadata]:
        """Lookup tool by voice alias."""
        with self._lock:
            name = self._voice_index.get(alias)
            return self._tools.get(name) if name else None

    def get_available_tools(self) -> List[ToolMetadata]:
        """Return only tools with AVAILABLE status."""
        with self._lock:
            return [
                t for t in self._tools.values()
                if t.availability == ToolAvailability.AVAILABLE
            ]

    def get_available_toolsets(self) -> Dict[str, List[ToolMetadata]]:
        """Return toolsets → available tools mapping."""
        with self._lock:
            result = {}
            for toolset in self._toolsets.keys():
                available = [
                    self._tools[name] for name in self._toolsets[toolset]
                    if self._tools[name].availability == ToolAvailability.AVAILABLE
                ]
                if available:
                    result[toolset] = available
            return result

    def get_preferred_voice_tools(self) -> List[ToolMetadata]:
        """Return tools marked as preferred_in_voice."""
        with self._lock:
            return [t for t in self._tools.values() if t.preferred_in_voice]

    # =========================================================================
    # Schema Generation
    # =========================================================================

    def get_tool_definitions(
        self,
        tool_names: Set[str],
        quiet: bool = False,
    ) -> List[dict]:
        """Return OpenAI-format tool schemas for requested tool names.

        Only includes tools with check_fn() returning AVAILABLE.
        """
        with self._lock:
            result = []
            for name in sorted(tool_names):
                tool = self._tools.get(name)
                if not tool:
                    continue

                # Evaluate availability
                if tool.check_fn:
                    avail, reason = _check_fn_cached(tool.check_fn, name)
                    if avail != ToolAvailability.AVAILABLE:
                        if not quiet:
                            logger.debug(
                                "Tool %s unavailable: %s", name, reason or avail.value
                            )
                        continue

                # Build schema with name
                schema_with_name = {**tool.schema, "name": name}
                result.append({
                    "type": "function",
                    "function": schema_with_name,
                })

            return result

    def get_tool_names(self) -> List[str]:
        """Return all tool names."""
        with self._lock:
            return sorted(self._tools.keys())

    def count_tools_by_source(self) -> Dict[str, int]:
        """Return tool count per source."""
        with self._lock:
            result = {}
            for tool in self._tools.values():
                key = tool.source.value
                result[key] = result.get(key, 0) + 1
            return result

    # =========================================================================
    # Dispatch
    # =========================================================================

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Execute a tool handler by name.

        Returns JSON string (error if handler missing or dispatch fails).
        """
        tool = self.get_tool(name)
        if not tool:
            return json.dumps({"error": f"Unknown tool: {name}"})

        if not tool.handler:
            return json.dumps({
                "error": f"Tool {name} has no handler (MCP/remote tool?)"
            })

        try:
            if tool.is_async:
                from model_tools import _run_async
                return _run_async(tool.handler(args, **kwargs))
            return tool.handler(args, **kwargs)
        except Exception as e:
            logger.exception("Tool %s dispatch error", name)
            return json.dumps({
                "error": f"Tool execution failed: {type(e).__name__}: {e}"
            })

    # =========================================================================
    # Snapshots & Introspection
    # =========================================================================

    def get_snapshot(self) -> ToolRegistrySnapshot:
        """Return coherent snapshot of current registry state."""
        with self._lock:
            toolsets_snapshot = {
                ts: sorted(names)
                for ts, names in self._toolsets.items()
            }
            return ToolRegistrySnapshot(
                tools=dict(self._tools),
                toolsets=toolsets_snapshot,
                sources_present=set(self._sources),
                generation=self._generation,
                timestamp=time.time(),
            )

    def get_statistics(self) -> Dict[str, Any]:
        """Return registry statistics."""
        with self._lock:
            return {
                "total_tools": len(self._tools),
                "total_toolsets": len(self._toolsets),
                "sources_present": sorted(s.value for s in self._sources),
                "tools_by_source": self.count_tools_by_source(),
                "generation": self._generation,
                "available_tools_count": sum(
                    1 for t in self._tools.values()
                    if t.availability == ToolAvailability.AVAILABLE
                ),
                "deprecated_tools": [
                    t.name for t in self._tools.values()
                    if t.deprecated
                ],
                "mutation_history_count": len(self._mutations),
            }

    def get_mutation_history(self) -> List[Dict[str, Any]]:
        """Return recent mutations (for audit/debugging)."""
        with self._lock:
            return list(self._mutations)

    # =========================================================================
    # Mutation Methods (for runtime updates)
    # =========================================================================

    def deregister_tool(self, name: str) -> bool:
        """Remove a tool from the registry.

        Returns True if tool was removed, False if not found.
        """
        with self._lock:
            tool = self._tools.pop(name, None)
            if not tool:
                return False

            # Remove from toolsets
            if tool.toolset in self._toolsets:
                self._toolsets[tool.toolset].discard(name)
                if not self._toolsets[tool.toolset]:
                    del self._toolsets[tool.toolset]

            # Remove from voice index
            for alias in tool.voice_aliases:
                self._voice_index.pop(alias, None)

            # Mark source as missing if no tools left
            if not any(t.source == tool.source for t in self._tools.values()):
                self._sources.discard(tool.source)

            self._generation += 1
            self._record_mutation("deregister", name, tool.source.value)
            logger.info("Deregistered tool: %s", name)
            return True

    def mutate_tool_metadata(self, name: str, **kwargs) -> Optional[ToolMetadata]:
        """Update tool metadata fields (availability, tags, etc.).

        Cannot mutate: name, source, handler (use deregister/re-register for those).
        Returns updated metadata or None if tool not found.
        """
        with self._lock:
            tool = self._tools.get(name)
            if not tool:
                return None

            # Whitelist of mutable fields
            mutable = {
                "availability", "availability_reason", "deprecated",
                "preferred_in_voice", "tags", "capability_categories",
                "voice_aliases", "description", "emoji",
            }

            # Build updated metadata
            updates = {k: v for k, v in kwargs.items() if k in mutable}
            if not updates:
                return tool

            # Create new (frozen) metadata with updates
            updated = ToolMetadata(
                name=tool.name,
                source=tool.source,
                toolset=tool.toolset,
                description=updates.get("description", tool.description),
                schema=tool.schema,
                handler=tool.handler,
                check_fn=tool.check_fn,
                requires_env=tool.requires_env,
                is_async=tool.is_async,
                emoji=updates.get("emoji", tool.emoji),
                max_result_size_chars=tool.max_result_size_chars,
                voice_aliases=updates.get("voice_aliases", tool.voice_aliases),
                mcp_server_id=tool.mcp_server_id,
                skill_name=tool.skill_name,
                tags=updates.get("tags", tool.tags),
                capability_categories=updates.get(
                    "capability_categories", tool.capability_categories
                ),
                availability=updates.get("availability", tool.availability),
                availability_reason=updates.get(
                    "availability_reason", tool.availability_reason
                ),
                deprecated=updates.get("deprecated", tool.deprecated),
                preferred_in_voice=updates.get("preferred_in_voice", tool.preferred_in_voice),
            )

            self._tools[name] = updated
            self._generation += 1
            self._record_mutation("mutate", name, tool.source.value)
            return updated

    # =========================================================================
    # Backward Compatibility
    # =========================================================================

    def to_legacy_dict(self) -> Dict[str, Any]:
        """Export as legacy dict format for backward compatibility.

        Used by code expecting:
          {"tool_name": {"toolset": "...", "schema": {...}, ...}}
        """
        with self._lock:
            return {
                tool.name: tool.to_dict()
                for tool in self._tools.values()
            }

    # =========================================================================
    # Export for Integration
    # =========================================================================

    def export_schema_for_voice(self) -> Dict[str, Any]:
        """Export schemas optimized for voice interaction.

        Includes only preferred_in_voice tools and their aliases.
        """
        with self._lock:
            voice_tools = [t for t in self._tools.values() if t.preferred_in_voice]
            return {
                "tools": [t.to_dict() for t in voice_tools],
                "voice_aliases": self._voice_index,
                "preferred_count": len(voice_tools),
            }

    def export_for_api_spec(self) -> Dict[str, Any]:
        """Export complete registry state for API specification."""
        with self._lock:
            stats = self.get_statistics()
            return {
                "version": "1.0.0",
                "generated_at": time.time(),
                "statistics": stats,
                "tools": [t.to_dict() for t in self._tools.values()],
                "toolsets": {
                    ts: sorted(names)
                    for ts, names in self._toolsets.items()
                },
                "sources": sorted(s.value for s in self._sources),
            }


# =============================================================================
# Module-level singleton
# =============================================================================

unified_registry = UnifiedToolRegistry()


def get_unified_registry() -> UnifiedToolRegistry:
    """Return the module-level unified registry instance."""
    return unified_registry


def initialize_unified_registry() -> None:
    """Initialize the unified registry with all tool sources.

    Call this once at startup to populate the registry.
    """
    try:
        count_builtin = unified_registry.load_builtin_tools()
        logger.info("Initialized unified registry: %d built-in tools loaded",
                   count_builtin)
    except Exception as e:
        logger.error("Failed to initialize unified registry: %s", e)
        raise


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.DEBUG)
    initialize_unified_registry()
    registry = get_unified_registry()
    stats = registry.get_statistics()
    print(json.dumps(stats, indent=2))
