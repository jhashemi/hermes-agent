#!/usr/bin/env python3
"""
Registration Script for Built-in Tools in UnifiedToolRegistry

This script registers all Hermes built-in tools in the UnifiedToolRegistry:
  - terminal
  - read_file
  - write_file
  - patch (bonus)
  - search_files
  - web_search
  - web_extract (bonus)
  - browser_navigate
  - browser_snapshot, browser_click, browser_type, etc. (bonus)

The script can be used to:
  1. Verify all built-in tools are accessible via UnifiedToolRegistry
  2. Generate a tool manifest for integration with voice-agents and other systems
  3. Test tool availability before agent startup
  4. Export tool schemas for API documentation

Usage:
  python register_builtin_tools.py              # Show registry summary
  python register_builtin_tools.py --export JSON # Export schemas
  python register_builtin_tools.py --verify      # Run availability checks
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def load_builtin_tools() -> Dict[str, Any]:
    """Load all built-in tools and return statistics."""
    try:
        from unified_tool_registry import (
            get_unified_registry,
            initialize_unified_registry,
            ToolSource
        )
        
        logger.info("Initializing UnifiedToolRegistry...")
        initialize_unified_registry()
        
        registry = get_unified_registry()
        stats = registry.get_statistics()
        
        logger.info(f"Registry initialized successfully")
        logger.info(f"  Total tools: {stats['total_tools']}")
        logger.info(f"  Built-in tools: {stats['tools_by_source'].get('builtin', 0)}")
        logger.info(f"  Available tools: {stats['available_tools_count']}")
        
        return stats
    except ImportError as e:
        logger.error(f"Failed to import UnifiedToolRegistry: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to initialize registry: {e}")
        raise


def verify_required_tools() -> bool:
    """Verify that all required built-in tools are registered."""
    required_tools = {
        "terminal": "terminal",
        "read_file": "file",
        "write_file": "file",
        "search_files": "file",
        "web_search": "web",
        "browser_navigate": "browser",
        # "search_in_projects": "?",  # TODO: Implement this tool
    }
    
    try:
        from unified_tool_registry import get_unified_registry, initialize_unified_registry
        
        initialize_unified_registry()
        registry = get_unified_registry()
        
        logger.info("Verifying required tools...")
        all_present = True
        
        for tool_name, expected_toolset in required_tools.items():
            tool = registry.get_tool(tool_name)
            if tool:
                logger.info(f"  ✓ {tool_name} (toolset: {tool.toolset})")
                if tool.toolset != expected_toolset:
                    logger.warning(f"    ⚠ Expected toolset '{expected_toolset}', got '{tool.toolset}'")
            else:
                logger.error(f"  ✗ {tool_name} NOT FOUND")
                all_present = False
        
        if all_present:
            logger.info("✓ All required tools present")
        else:
            logger.error("✗ Some required tools are missing")
        
        return all_present
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        return False


def export_schemas(output_file: Optional[str] = None) -> Dict[str, Any]:
    """Export tool schemas to JSON file or return dict."""
    try:
        from unified_tool_registry import get_unified_registry, initialize_unified_registry
        
        initialize_unified_registry()
        registry = get_unified_registry()
        
        # Export complete registry state
        export = registry.export_for_api_spec()
        
        # Filter to built-in tools only
        builtin_tools = [
            t for t in export.get("tools", [])
            if t.get("source") == "builtin"
        ]
        
        builtin_export = {
            "version": "1.0.0",
            "timestamp": export.get("generated_at"),
            "builtin_tools": builtin_tools,
            "builtin_count": len(builtin_tools),
            "toolsets": {
                ts: sorted([
                    t["name"] for t in builtin_tools
                    if t["toolset"] == ts
                ])
                for ts in set(t["toolset"] for t in builtin_tools)
            }
        }
        
        if output_file:
            output_path = Path(output_file)
            output_path.write_text(json.dumps(builtin_export, indent=2))
            logger.info(f"Exported {len(builtin_tools)} tool schemas to {output_file}")
        
        return builtin_export
    except Exception as e:
        logger.error(f"Export failed: {e}")
        raise


def print_summary():
    """Print a human-readable summary of the registry."""
    try:
        from unified_tool_registry import get_unified_registry, initialize_unified_registry
        
        initialize_unified_registry()
        registry = get_unified_registry()
        stats = registry.get_statistics()
        
        print("\n" + "=" * 70)
        print("HERMES BUILT-IN TOOLS - REGISTRATION SUMMARY")
        print("=" * 70)
        print(f"Total tools registered:    {stats['total_tools']}")
        print(f"Total toolsets:            {stats['total_toolsets']}")
        print(f"Available tools:           {stats['available_tools_count']}")
        print(f"Generation (cache ver):    {stats['generation']}")
        print("\nTools by Source:")
        for source, count in sorted(stats.get("tools_by_source", {}).items()):
            print(f"  {source:12s}  {count:3d}")
        
        if stats.get("deprecated_tools"):
            print(f"\nDeprecated tools: {', '.join(stats['deprecated_tools'])}")
        
        # List built-in tools by toolset
        print("\nBuilt-in Tools by Toolset:")
        
        # Get all tools
        all_tools = registry.get_all_tools()
        builtin_tools = [t for t in all_tools if t.source.value == "builtin"]
        
        # Group by toolset
        by_toolset: Dict[str, List] = {}
        for tool in builtin_tools:
            if tool.toolset not in by_toolset:
                by_toolset[tool.toolset] = []
            by_toolset[tool.toolset].append(tool)
        
        for toolset in sorted(by_toolset.keys()):
            tools = by_toolset[toolset]
            print(f"\n  {toolset} ({len(tools)} tools)")
            for tool in sorted(tools, key=lambda t: t.name):
                status = "✓" if tool.availability.value == "available" else "✗"
                print(f"    {status} {tool.name}")
                if tool.description:
                    desc = tool.description[:60] + "..." if len(tool.description) > 60 else tool.description
                    print(f"      → {desc}")
        
        print("\n" + "=" * 70 + "\n")
    except Exception as e:
        logger.error(f"Summary failed: {e}")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Register and verify built-in tools in UnifiedToolRegistry"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify that all required tools are registered"
    )
    parser.add_argument(
        "--export",
        metavar="FILE",
        help="Export tool schemas to JSON file"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check tool availability (runs check_fn on each tool)"
    )
    
    args = parser.parse_args()
    
    try:
        # Always load and show summary
        print_summary()
        
        # Run requested checks
        if args.verify:
            if not verify_required_tools():
                sys.exit(1)
        
        if args.export:
            export_schemas(args.export)
        
        if args.check:
            logger.info("Tool availability checks requested (not yet implemented)")
        
        logger.info("✓ Built-in tool registration complete")
        return 0
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
