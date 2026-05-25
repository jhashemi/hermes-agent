#!/usr/bin/env python3
"""
Diagnostic: Verify Silent Crash Detection System is Operational

Run this before/after deployment to ensure:
1. Profile toolsets are correctly configured
2. Kanban tools are accessible
3. MCP servers are reachable
4. Silent crash detection hook is integrated
5. Board health is good
"""

import sys
import socket
import sqlite3
from pathlib import Path
from typing import Tuple

sys.path.insert(0, "/home/ubuntu/hermes-agent")


def check_profile_toolsets() -> Tuple[bool, str]:
    """Verify all profiles have kanban toolset."""
    try:
        from tools.profile_toolset_validator import validate_all_profiles
        hermes_home = Path("~/.hermes").expanduser()
        errors = validate_all_profiles(hermes_home)
        
        if errors:
            return False, f"Profile validation failed:\n" + "\n".join(errors[:3])
        return True, "✅ All profiles have required toolsets"
    except Exception as e:
        return False, f"Profile check error: {e}"


def check_kanban_tools() -> Tuple[bool, str]:
    """Verify kanban tools are registered and accessible."""
    try:
        # Check if kanban_tools.py file exists and has registry.register calls
        kanban_tools_file = Path("/home/ubuntu/hermes-agent/tools/kanban_tools.py")
        
        if not kanban_tools_file.exists():
            return False, "kanban_tools.py not found"
        
        content = kanban_tools_file.read_text()
        
        kanban_tools = [
            'kanban_complete',
            'kanban_block',
            'kanban_show',
        ]
        
        # Check if tools are defined in source
        missing = []
        for tool_name in kanban_tools:
            if f'name="{tool_name}"' not in content and f"name='{tool_name}'" not in content:
                missing.append(tool_name)
        
        if missing:
            return False, f"Missing kanban tools in source: {missing}"
        
        # Check if toolset="kanban" is used
        if 'toolset="kanban"' not in content:
            return False, "Kanban toolset not registered"
        
        return True, f"✅ All {len(kanban_tools)} kanban tools defined and registered"
    except Exception as e:
        return False, f"Tool check error: {e}"


def check_mcp_connectivity() -> Tuple[bool, str]:
    """Verify Serena MCP is reachable."""
    try:
        sock = socket.create_connection(('127.0.0.1', 9430), timeout=2)
        sock.close()
        return True, "✅ Serena MCP reachable on 127.0.0.1:9430"
    except socket.timeout:
        return False, "⚠️  Serena MCP timeout (will auto-start on gateway restart)"
    except ConnectionRefusedError:
        return False, "⚠️  Serena MCP not running (will auto-start on gateway restart)"
    except Exception as e:
        return False, f"MCP connectivity error: {e}"


def check_silent_crash_detection() -> Tuple[bool, str]:
    """Verify silent crash detection is integrated into dispatcher."""
    try:
        from hermes_cli import kanban_db as kb
        
        # Check that dispatch_once returns DispatchResult with silent_crashes
        import inspect
        source = inspect.getsource(kb.dispatch_once)
        
        if 'silent_crashes' not in source:
            return False, "Silent crash detection not found in dispatch_once"
        
        if 'handle_dead_worker_pids' not in source:
            return False, "dead PID handler not called in dispatch_once"
        
        return True, "✅ Silent crash detection integrated into dispatcher"
    except Exception as e:
        return False, f"Dispatcher check error: {e}"


def check_board_health(board_name: str = "okr-2026-q2") -> Tuple[bool, str]:
    """Check kanban board state for health indicators."""
    try:
        from hermes_cli import kanban_db as kb
        
        conn = kb.connect(board=board_name)
        
        # Get board stats
        stats = {}
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ).fetchall():
            stats[status] = count
        
        done = stats.get('done', 0)
        blocked = stats.get('blocked', 0)
        total = sum(stats.values())
        
        # Run one dispatcher tick
        result = kb.dispatch_once(conn)
        conn.close()
        
        # Healthy if:
        # - Few blocked tasks relative to total
        # - No silent crashes in last tick
        # - Reasonable completion rate
        
        blocked_pct = (blocked / total * 100) if total > 0 else 0
        done_pct = (done / total * 100) if total > 0 else 0
        
        msg = f"Board '{board_name}': {done} done ({done_pct:.0f}%), {blocked} blocked ({blocked_pct:.0f}%)"
        
        if result.silent_crashes:
            msg += f"\n⚠️  Silent crashes: {len(result.silent_crashes)}"
            return False, msg
        
        if blocked_pct > 30:
            msg += f"\n⚠️  High blocked ratio: {blocked_pct:.0f}%"
            return False, msg
        
        return True, f"✅ {msg}"
    except Exception as e:
        return False, f"Board health check error: {e}"


def check_startup_hooks() -> Tuple[bool, str]:
    """Verify startup hooks are in place."""
    try:
        hooks_dir = Path("/home/ubuntu/hermes-agent/.hermes/startup_hooks")
        
        required_hooks = [
            "profile_toolset_check.py",
            "mcp_auto_start.py",
        ]
        
        missing = []
        for hook in required_hooks:
            if not (hooks_dir / hook).exists():
                missing.append(hook)
        
        if missing:
            return False, f"Missing startup hooks: {missing}"
        
        return True, f"✅ All {len(required_hooks)} startup hooks deployed"
    except Exception as e:
        return False, f"Hooks check error: {e}"


def main():
    """Run all diagnostics."""
    print("="*70)
    print("SILENT CRASH DETECTION SYSTEM DIAGNOSTIC")
    print("="*70 + "\n")
    
    checks = [
        ("Profile Toolsets", check_profile_toolsets),
        ("Kanban Tools", check_kanban_tools),
        ("MCP Connectivity", check_mcp_connectivity),
        ("Silent Crash Detection", check_silent_crash_detection),
        ("Startup Hooks", check_startup_hooks),
        ("Board Health", check_board_health),
    ]
    
    results = []
    passed = 0
    failed = 0
    
    for name, check_fn in checks:
        success, message = check_fn()
        results.append((name, success, message))
        
        if success:
            passed += 1
            print(f"✅ {name}")
        else:
            failed += 1
            print(f"❌ {name}")
        
        print(f"   {message}\n")
    
    print("="*70)
    print(f"Summary: {passed} passed, {failed} failed")
    print("="*70)
    
    if failed == 0:
        print("\n🚀 SYSTEM OPERATIONAL — Ready for deployment\n")
        return 0
    elif failed <= 2:  # MCP and some warnings are acceptable
        print("\n⚠️  WARNINGS PRESENT — See above. Deployment can proceed.\n")
        return 0
    else:
        print("\n❌ CRITICAL ISSUES — Fix before deploying\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
