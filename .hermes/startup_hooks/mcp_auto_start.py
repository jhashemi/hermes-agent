"""
MCP Initialization: Serena LSP Auto-Start

If Serena MCP server isn't responding on port 9430, start it automatically.
This replaces the need for a systemd service.
"""

import subprocess
import socket
import time
import sys
from pathlib import Path


def is_serena_running(host: str = "127.0.0.1", port: int = 9430, timeout: float = 2.0) -> bool:
    """Check if Serena MCP server is reachable on the configured port."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def start_serena(host: str = "127.0.0.1", port: int = 9430) -> bool:
    """
    Start Serena MCP server if not already running.
    
    Returns True if started or already running, False if startup failed.
    """
    if is_serena_running(host, port):
        return True  # Already running
    
    serena_bin = Path.home() / ".local" / "bin" / "serena"
    if not serena_bin.exists():
        print(f"⚠️  Serena binary not found: {serena_bin}")
        return False
    
    print(f"🚀 Starting Serena MCP server ({host}:{port})...")
    
    try:
        # Start Serena as subprocess
        proc = subprocess.Popen(
            [str(serena_bin), "start-mcp-server", "--host", host, "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from parent process
        )
        
        # Wait for server to become ready
        for attempt in range(10):
            time.sleep(0.5)
            if is_serena_running(host, port):
                print(f"✅ Serena MCP ready ({host}:{port})")
                return True
        
        print(f"❌ Serena MCP failed to start (timeout after 5s)")
        return False
    
    except Exception as e:
        print(f"❌ Serena MCP startup error: {e}")
        return False


def check_mcps_on_startup() -> None:
    """
    Called during Hermes startup. Ensures critical MCPs are available.
    """
    # Check Serena
    if not is_serena_running():
        started = start_serena()
        if not started:
            print("⚠️  Warning: Serena MCP unavailable (optional, non-blocking)")


if __name__ == "__main__":
    check_mcps_on_startup()
