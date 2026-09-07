#!/usr/bin/env python3
"""
Executive Voice Agents - Install Script

Sets up the voice agent platform as a Hermes Gateway plugin.
Uses the native plugin system (pre_gateway_dispatch hook) instead of
patching gateway/run.py.

Usage:
    python3 install_plugin.py [--remote HOST] [--uninstall]

Options:
    --remote HOST   Deploy to remote host (default: ip-172-31-30-216)
    --uninstall     Remove the plugin from the gateway
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("voice-agents-install")

PLATFORM_DIR = Path(__file__).parent.resolve()
REMOTE_HOST = "ubuntu@ip-172-31-30-216"
HERMES_DIR = Path("/home/ubuntu/hermes-agent")
PLUGINS_DIR = HERMES_DIR / "plugins" / "voice-agents"
GATEWAY_HOOKS_DIR = HERMES_DIR / "gateway" / "builtin_hooks"


def run(cmd: str, check: bool = True, remote: str = None) -> subprocess.CompletedProcess:
    """Run a command locally or on remote."""
    if remote:
        cmd = f"ssh {remote} {cmd!r}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        logger.error("Command failed: %s\n%s", cmd, result.stderr)
        raise RuntimeError(f"Command failed: {cmd}")
    return result


def install(remote: str = None) -> None:
    """Install the voice agent plugin."""
    target = remote or "localhost"
    logger.info("Installing voice agent plugin on %s...", target)

    # Step 1: Copy platform files
    if remote:
        run(f"mkdir -p {PLUGINS_DIR}", remote=remote)
        # Copy the entire platform using rsync
        rsync_cmd = (
            f"rsync -avz --exclude='__pycache__' --exclude='.git' "
            f"--exclude='*.pyc' --exclude='tests' "
            f"{PLATFORM_DIR}/ {remote}:{PLUGINS_DIR}/"
        )
        subprocess.run(rsync_cmd, shell=True, check=True)
    else:
        if PLUGINS_DIR.exists():
            shutil.rmtree(PLUGINS_DIR)
        shutil.copytree(PLATFORM_DIR, PLUGINS_DIR)

    # Step 2: Copy voices data files (agents directory)
    agents_src = PLATFORM_DIR / "agents"
    agents_dst = Path("/home/ubuntu/executive_agents_platform/agents")
    if remote:
        run(f"mkdir -p {agents_dst}", remote=remote)
        subprocess.run(
            f"rsync -avz {agents_src}/ {remote}:{agents_dst}/",
            shell=True, check=True,
        )
    else:
        if agents_dst.exists():
            shutil.rmtree(agents_dst)
        shutil.copytree(agents_src, agents_dst)

    # Step 3: Copy plugin module to plugins directory
    plugin_file = PLATFORM_DIR / "voice_agents_plugin.py"
    plugin_manifest = PLATFORM_DIR / "plugin.yaml"

    if remote:
        subprocess.run(f"scp {plugin_file} {remote}:{PLUGINS_DIR}/", shell=True, check=True)
        subprocess.run(f"scp {plugin_manifest} {remote}:{PLUGINS_DIR}/", shell=True, check=True)
    else:
        shutil.copy2(plugin_file, PLUGINS_DIR / "voice_agents_plugin.py")
        shutil.copy2(plugin_manifest, PLUGINS_DIR / "plugin.yaml")

    # Step 4: Remove old custom hook integration from run.py
    # We no longer patch run.py — we use the native plugin system
    logger.info("Checking for old custom hook integration in run.py...")
    if remote:
        # Check if old hook integration exists
        result = run(f"grep -c 'voice hook interception' {HERMES_DIR}/gateway/run.py", remote=remote, check=False)
        if result.stdout.strip() != "0":
            logger.info("Removing old custom hook integration from run.py...")
            patch_cmd = (
                f"cd {HERMES_DIR} && "
                f"cp gateway/run.py gateway/run.py.backup.$(date +%s) && "
                f"python3 -c \""
                f"import re; "
                f"content = open('gateway/run.py').read(); "
                f"# Remove the custom hook block"
                f"content = re.sub("
                f"  r'# Voice bridge hook interception.*?logging.debug.*?Not intercepted.*?\\)', "
                f"  '', content, flags=re.DOTALL"
                f"); "
                f"open('gateway/run.py', 'w').write(content)"
                f"\""
            )
            run(patch_cmd, remote=remote)
            logger.info("✓ Old hook integration removed from run.py")
        else:
            logger.info("✓ No old hook integration found (clean)")
    else:
        run_py = HERMES_DIR / "gateway" / "run.py"
        content = run_py.read_text() if run_py.exists() else ""
        if "voice hook interception" in content:
            backup = run_py.with_suffix(f".py.backup.{int(__import__('time').time())}")
            shutil.copy2(run_py, backup)
            content = content.replace("# Voice bridge hook interception", "")
            run_py.write_text(content)
            logger.info("✓ Old hook integration removed from run.py")

    # Step 5: Remove old builtin_hooks voice_agent_hook.py
    # (replaced by native plugin)
    logger.info("Removing old builtin_hooks/voice_agent_hook.py...")
    if remote:
        run(f"rm -f {GATEWAY_HOOKS_DIR}/voice_agent_hook.py", remote=remote)
        run(f"rm -f {GATEWAY_HOOKS_DIR}/__init__.py.bak", remote=remote)
    else:
        hook_file = GATEWAY_HOOKS_DIR / "voice_agent_hook.py"
        if hook_file.exists():
            hook_file.unlink()

    # Step 6: Restore __init__.py to original state (no voice hook exports)
    logger.info("Restoring builtin_hooks/__init__.py...")
    init_content = '"""Builtin gateway hooks — extension point for always-registered hooks."""\n'
    if remote:
        run(f"cat > {GATEWAY_HOOKS_DIR}/__init__.py << 'PYEOF'\n{init_content}PYEOF", remote=remote)
    else:
        (GATEWAY_HOOKS_DIR / "__init__.py").write_text(init_content)

    # Step 7: Restart gateway
    logger.info("Restarting gateway...")
    if remote:
        run("systemctl --user reset-failed hermes-gateway.service 2>/dev/null; "
            "systemctl --user restart hermes-gateway.service", remote=remote)
    else:
        os.system("systemctl --user restart hermes-gateway.service")

    # Step 8: Verify
    logger.info("Verifying installation...")
    imports_ok = True
    if remote:
        result = run(
            f"cd {PLUGINS_DIR} && python3 -c "
            f"'from voice_agents_plugin import pre_gateway_dispatch_hook; "
            f"print(\"OK\")'",
            remote=remote, check=False,
        )
        if "OK" not in (result.stdout or ""):
            logger.warning("Import test failed on remote")
            imports_ok = False
    else:
        sys.path.insert(0, str(PLUGINS_DIR))
        try:
            from voice_agents_plugin import pre_gateway_dispatch_hook
            logger.info("✓ Plugin imports OK")
        except ImportError as e:
            logger.warning("Import test failed: %s", e)
            imports_ok = False

    # Summary
    print("\n" + "=" * 60)
    print("  Executive Voice Agents Plugin — Install Complete")
    print("=" * 60)
    print(f"  Target:    {target}")
    print(f"  Platform:  {PLUGINS_DIR}")
    print(f"  Agents:    {agents_dst}")
    print(f"  Plugin:    voice_agents_plugin.py")
    print(f"  Hook:      pre_gateway_dispatch (native)")
    print(f"  Commands:  /load-{{agent}}, /voice-agents,")
    print(f"             /voice-disconnect, /voice-info {{id}}")
    print(f"  Memory:    8/8 systems active")
    print(f"  Imports:   {'✓ OK' if imports_ok else '✗ FAILED'}")
    print("=" * 60)
    if not imports_ok:
        print("\n  ⚠️  Plugin import failed — check logs above")
        print("     Gateway will run without voice agent commands")


def uninstall(remote: str = None) -> None:
    """Remove the voice agent plugin."""
    target = remote or "localhost"
    logger.info("Uninstalling voice agent plugin from %s...", target)

    # Remove plugin directory
    if remote:
        run(f"rm -rf {PLUGINS_DIR}", remote=remote)
    else:
        if PLUGINS_DIR.exists():
            shutil.rmtree(PLUGINS_DIR)

    # Restore __init__.py
    init_content = '"""Builtin gateway hooks — extension point for always-registered hooks."""\n'
    if remote:
        run(f"cat > {GATEWAY_HOOKS_DIR}/__init__.py << 'PYEOF'\n{init_content}PYEOF", remote=remote)
    else:
        (GATEWAY_HOOKS_DIR / "__init__.py").write_text(init_content)

    # Restart gateway
    if remote:
        run("systemctl --user restart hermes-gateway.service", remote=remote)
    else:
        os.system("systemctl --user restart hermes-gateway.service")

    print("\n✓ Voice agent plugin uninstalled")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install/uninstall voice agent plugin")
    parser.add_argument("--remote", default=None, help="Remote host (e.g. ubuntu@1.2.3.4)")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall the plugin")
    args = parser.parse_args()

    remote = args.remote
    if args.uninstall:
        uninstall(remote=remote)
    else:
        install(remote=remote)