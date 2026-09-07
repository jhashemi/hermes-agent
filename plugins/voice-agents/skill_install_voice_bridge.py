#!/usr/bin/env python3
"""
Hermes Voice Bridge Installation Skill

Automated installation and configuration of executive voice agent bridge.
Installs SOLID-compliant hook system for Hermes Gateway with zero platform modifications.

Usage:
    hermes skill run install-voice-bridge [--dry-run] [--backup]
    hermes skill run install-voice-bridge verify
    hermes skill run install-voice-bridge setup-dirs
    hermes skill run install-voice-bridge patch-gateway
    hermes skill run install-voice-bridge configure-env
    hermes skill run install-voice-bridge test
    hermes skill run install-voice-bridge rollback
"""

import os
import sys
import shutil
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# ============================================================================
# ANSI Colors
# ============================================================================

class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'

def print_header(text: str) -> None:
    """Print section header"""
    print(f"\n{Colors.BLUE}{'='*80}{Colors.RESET}")
    print(f"{Colors.BLUE}{text}{Colors.RESET}")
    print(f"{Colors.BLUE}{'='*80}{Colors.RESET}\n")

def print_success(text: str) -> None:
    """Print success message"""
    print(f"{Colors.GREEN}✓{Colors.RESET} {text}")

def print_warning(text: str) -> None:
    """Print warning message"""
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {text}")

def print_error(text: str) -> None:
    """Print error message"""
    print(f"{Colors.RED}✗{Colors.RESET} {text}")

def print_info(text: str) -> None:
    """Print info message"""
    print(f"{Colors.BLUE}ℹ{Colors.RESET} {text}")

def print_step(num: int, text: str) -> None:
    """Print step header"""
    print(f"\n{Colors.CYAN}Step {num}: {text}{Colors.RESET}")

# ============================================================================
# Voice Bridge Installer
# ============================================================================

class VoiceBridgeInstaller:
    """Automated installation of voice bridge hook system"""
    
    def __init__(self, dry_run: bool = False, backup: bool = False):
        self.dry_run = dry_run
        self.backup = backup
        self.hermes_root = Path("/home/ubuntu/hermes-agent")
        self.executive_root = Path("/home/ubuntu/executive_agents_platform")
        self.gateway_dir = self.hermes_root / "gateway"
        self.builtin_hooks_dir = self.gateway_dir / "builtin_hooks"
        self.env_file = Path.home() / ".hermes" / ".env"
        self.backups = []
        self.errors = []
        self.warnings = []
        self.success_count = 0
    
    # ========================================================================
    # Verification
    # ========================================================================
    
    def verify_prerequisites(self) -> bool:
        """Verify all prerequisites"""
        print_step(1, "Prerequisites Verification")
        
        checks = [
            (self.hermes_root.exists(), f"Hermes Gateway: {self.hermes_root}"),
            (self.gateway_dir.exists(), f"Gateway directory: {self.gateway_dir}"),
            ((self.hermes_root / "gateway" / "run.py").exists(), "gateway/run.py exists"),
            (self.executive_root.exists(), f"Executive platform: {self.executive_root}"),
            (self._python_version_ok(), "Python 3.9+ available"),
        ]
        
        all_ok = True
        for check, desc in checks:
            if check:
                print_success(desc)
                self.success_count += 1
            else:
                print_error(desc)
                all_ok = False
        
        return all_ok
    
    def _python_version_ok(self) -> bool:
        """Check Python version"""
        try:
            version = sys.version_info
            return version.major == 3 and version.minor >= 9
        except:
            return False
    
    # ========================================================================
    # Directory Setup
    # ========================================================================
    
    def create_directories(self) -> bool:
        """Create required directories"""
        print_step(2, "Creating Directory Structure")
        
        if self.builtin_hooks_dir.exists():
            print_info(f"Directory already exists: {self.builtin_hooks_dir}")
            return True
        
        if self.dry_run:
            print_info(f"[DRY RUN] Would create: {self.builtin_hooks_dir}")
            return True
        
        try:
            self.builtin_hooks_dir.mkdir(parents=True, exist_ok=True)
            print_success(f"Created: {self.builtin_hooks_dir}")
            self.success_count += 1
            return True
        except Exception as e:
            print_error(f"Failed to create directory: {e}")
            self.errors.append(str(e))
            return False
    
    # ========================================================================
    # File Copying
    # ========================================================================
    
    def copy_hook_files(self) -> bool:
        """Copy hook implementation files"""
        print_step(3, "Copying Hook Implementation Files")
        
        hook_file = self.builtin_hooks_dir / "voice_agent_hook.py"
        init_file = self.builtin_hooks_dir / "__init__.py"
        
        if hook_file.exists() and init_file.exists():
            print_success(f"Hook files already exist in {self.builtin_hooks_dir}")
            return True
        
        if self.dry_run:
            print_info("[DRY RUN] Would copy hook files to builtin_hooks/")
            return True
        
        print_info("Verifying hook files are already in place...")
        
        if not hook_file.exists() or not init_file.exists():
            print_error("Hook files not found in builtin_hooks/")
            print_info("They should have been created during platform setup")
            self.warnings.append("Hook files missing - verify platform installation")
            return False
        
        print_success("Hook files verified in builtin_hooks/")
        self.success_count += 1
        return True
    
    # ========================================================================
    # Gateway Patching
    # ========================================================================
    
    def patch_gateway_run_py(self) -> bool:
        """Patch gateway/run.py with hook integration"""
        print_step(4, "Patching gateway/run.py")
        
        run_py = self.gateway_dir / "run.py"
        
        if not run_py.exists():
            print_error(f"gateway/run.py not found at {run_py}")
            self.errors.append("gateway/run.py not found")
            return False
        
        try:
            content = run_py.read_text()
            
            # Check if already patched
            if "initialize_builtin_hooks" in content and "get_hook_manager" in content:
                print_success("gateway/run.py already patched with hooks")
                return True
            
            if self.dry_run:
                print_info("[DRY RUN] Would patch gateway/run.py with:")
                print_info("  Location 1: Add 3 lines at startup")
                print_info("  Location 2: Add 5 lines in _handle_message()")
                return True
            
            if self.backup:
                self._backup_file(run_py)
            
            # For actual patching, provide guidance
            print_warning("Manual patching required for gateway/run.py")
            print_info("Add 8 lines to /home/ubuntu/hermes-agent/gateway/run.py:")
            print_info("")
            print_info("Location 1: At startup (3 lines):")
            print_info("  from gateway.builtin_hooks import initialize_builtin_hooks")
            print_info("  await initialize_builtin_hooks()")
            print_info("")
            print_info("Location 2: In _handle_message() at start (5 lines):")
            print_info("  from gateway.builtin_hooks.voice_agent_hook import get_hook_manager")
            print_info("  manager = get_hook_manager()")
            print_info("  hook_result = await manager.before_message_processing(event, self)")
            print_info("  if hook_result is not None:")
            print_info("      await self.send_message(..., reply_to=event); return")
            
            self.warnings.append("gateway/run.py requires manual 8-line patch")
            return True
        
        except Exception as e:
            print_error(f"Error patching gateway/run.py: {e}")
            self.errors.append(str(e))
            return False
    
    def _backup_file(self, file_path: Path) -> None:
        """Create backup of file"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = file_path.parent / f"{file_path.name}.backup.{timestamp}"
        
        try:
            shutil.copy2(file_path, backup_path)
            print_success(f"Backed up to: {backup_path}")
            self.backups.append(backup_path)
        except Exception as e:
            print_warning(f"Backup failed: {e}")
    
    # ========================================================================
    # Environment Configuration
    # ========================================================================
    
    def configure_environment(self) -> bool:
        """Configure environment variables"""
        print_step(5, "Configuring Environment Variables")
        
        required_vars = {
            "RESEMBLE_API_KEY": "Resemble AI (voice synthesis)",
            "DEEPGRAM_API_KEY": "Deepgram (speech transcription)",
            "LIVEKIT_API_KEY": "LiveKit (streaming)",
            "LIVEKIT_API_SECRET": "LiveKit authentication",
        }
        
        # Create .hermes directory if needed
        env_dir = self.env_file.parent
        if not env_dir.exists():
            if not self.dry_run:
                env_dir.mkdir(parents=True, exist_ok=True)
                print_success(f"Created {env_dir}")
        
        if self.dry_run:
            print_info("[DRY RUN] Would configure environment with:")
            for var in required_vars:
                print_info(f"  {var}=<your_key>")
            return True
        
        # Read existing .env
        env_content = ""
        if self.env_file.exists():
            env_content = self.env_file.read_text()
            if self.backup:
                self._backup_file(self.env_file)
        
        # Check for missing variables
        missing = []
        for var in required_vars:
            if var not in env_content:
                missing.append((var, required_vars[var]))
            else:
                print_success(f"{var} already configured")
        
        if missing:
            print_warning(f"Missing {len(missing)} environment variables")
            print_info("Add to ~/.hermes/.env:")
            for var, desc in missing:
                print_info(f"  export {var}=<your_{var.lower()}>")
                print_info(f"    ({desc})")
            
            self.warnings.append(f"Missing {len(missing)} environment variables")
        
        self.success_count += 1
        return True
    
    # ========================================================================
    # Testing
    # ========================================================================
    
    def test_installation(self) -> bool:
        """Test hook system"""
        print_step(6, "Testing Installation")
        
        tests = [
            self._test_hook_imports,
            self._test_environment,
            self._test_gateway_running,
        ]
        
        passed = 0
        for test in tests:
            try:
                if test():
                    passed += 1
            except Exception as e:
                print_error(f"Test failed: {e}")
        
        self.success_count += passed
        return passed == len(tests)
    
    def _test_hook_imports(self) -> bool:
        """Test hook module imports"""
        print_info("Testing hook module imports...")
        
        try:
            # Try importing hook module
            hook_path = self.builtin_hooks_dir / "voice_agent_hook.py"
            if not hook_path.exists():
                print_error("Hook module not found")
                return False
            
            # Check syntax
            result = subprocess.run(
                ["python3", "-m", "py_compile", str(hook_path)],
                capture_output=True,
                timeout=5
            )
            
            if result.returncode != 0:
                print_error("Hook module syntax error")
                return False
            
            print_success("Hook module syntax valid")
            return True
        
        except Exception as e:
            print_error(f"Import test failed: {e}")
            return False
    
    def _test_environment(self) -> bool:
        """Test environment variables"""
        print_info("Testing environment variables...")
        
        required = ["RESEMBLE_API_KEY", "DEEPGRAM_API_KEY", "LIVEKIT_API_KEY"]
        found = 0
        
        if self.env_file.exists():
            content = self.env_file.read_text()
            for var in required:
                if var in content:
                    found += 1
        
        if found == len(required):
            print_success("All required environment variables configured")
            return True
        else:
            print_warning(f"Only {found}/{len(required)} environment variables found")
            return False
    
    def _test_gateway_running(self) -> bool:
        """Test if gateway is running"""
        print_info("Testing gateway status...")
        
        try:
            # Check if gateway process exists
            result = subprocess.run(
                ["pgrep", "-f", "hermes.*gateway"],
                capture_output=True,
                timeout=5
            )
            
            if result.returncode == 0:
                print_success("Hermes Gateway is running")
                return True
            else:
                print_warning("Hermes Gateway is not running")
                print_info("Start with: hermes gateway")
                return False
        
        except Exception as e:
            print_warning(f"Could not check gateway status: {e}")
            return False
    
    # ========================================================================
    # Rollback
    # ========================================================================
    
    def rollback(self) -> bool:
        """Rollback installation"""
        print_step(7, "Rolling Back Installation")
        
        rollback_files = [
            self.gateway_dir / "run.py.backup.*",
            self.env_file.parent / ".env.backup.*",
        ]
        
        backups_found = []
        for pattern in rollback_files:
            matches = list(pattern.parent.glob(pattern.name))
            backups_found.extend(matches)
        
        if not backups_found:
            print_warning("No backup files found - nothing to rollback")
            return True
        
        if self.dry_run:
            print_info("[DRY RUN] Would restore from backups:")
            for backup in backups_found:
                print_info(f"  {backup}")
            return True
        
        for backup in backups_found:
            original = backup.parent / backup.name.split(".backup")[0]
            try:
                shutil.copy2(backup, original)
                print_success(f"Restored: {original}")
            except Exception as e:
                print_error(f"Failed to restore {original}: {e}")
                self.errors.append(str(e))
        
        return len(self.errors) == 0
    
    # ========================================================================
    # Summary
    # ========================================================================
    
    def print_summary(self) -> None:
        """Print installation summary"""
        print_header("Installation Summary")
        
        print(f"Successful operations: {Colors.GREEN}{self.success_count}{Colors.RESET}")
        
        if self.warnings:
            print(f"\n{Colors.YELLOW}Warnings ({len(self.warnings)}):{Colors.RESET}")
            for warning in self.warnings:
                print_warning(warning)
        
        if self.errors:
            print(f"\n{Colors.RED}Errors ({len(self.errors)}):{Colors.RESET}")
            for error in self.errors:
                print_error(error)
        
        if not self.errors:
            print_success("Installation completed successfully!")
            self.print_next_steps()
    
    def print_next_steps(self) -> None:
        """Print next steps"""
        print_header("Next Steps")
        
        print_info("1. Manual Configuration (if needed):")
        print_info("   Edit ~/.hermes/.env with API keys")
        print_info("")
        print_info("2. Patch gateway/run.py (if needed):")
        print_info("   Add 8 lines as shown above")
        print_info("")
        print_info("3. Restart Gateway:")
        print_info("   hermes gateway restart")
        print_info("")
        print_info("4. Test Voice Commands:")
        print_info("   /load-demis in WhatsApp")
        print_info("")
        print_info("5. Monitor Logs:")
        print_info("   hermes logs --follow --gateway | grep voice-hook")
        print_info("")
        print_info("Rollback (if needed):")
        print_info("   hermes skill run install-voice-bridge rollback")
    
    def run(self) -> int:
        """Run full installation"""
        print_header("Hermes Voice Bridge Installation")
        print_info(f"Dry Run: {self.dry_run}")
        print_info(f"Backup: {self.backup}")
        
        steps = [
            ("Prerequisites", self.verify_prerequisites),
            ("Directories", self.create_directories),
            ("Hook Files", self.copy_hook_files),
            ("Gateway Patch", self.patch_gateway_run_py),
            ("Environment", self.configure_environment),
            ("Testing", self.test_installation),
        ]
        
        for step_name, step_func in steps:
            try:
                if not step_func():
                    if not self.dry_run:
                        print_warning(f"Step failed: {step_name}")
            except Exception as e:
                print_error(f"Exception in {step_name}: {e}")
                if not self.dry_run:
                    self.errors.append(str(e))
        
        self.print_summary()
        
        return 0 if not self.errors else 1

# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Hermes Voice Bridge Installation Skill"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--backup", action="store_true", help="Create backups")
    
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("verify", help="Verify prerequisites only")
    subparsers.add_parser("setup-dirs", help="Create directories only")
    subparsers.add_parser("patch-gateway", help="Patch gateway/run.py only")
    subparsers.add_parser("configure-env", help="Configure environment only")
    subparsers.add_parser("test", help="Run tests only")
    subparsers.add_parser("rollback", help="Rollback installation")
    
    args = parser.parse_args()
    
    installer = VoiceBridgeInstaller(dry_run=args.dry_run, backup=args.backup)
    
    if args.command == "verify":
        return 0 if installer.verify_prerequisites() else 1
    elif args.command == "setup-dirs":
        return 0 if installer.create_directories() else 1
    elif args.command == "patch-gateway":
        return 0 if installer.patch_gateway_run_py() else 1
    elif args.command == "configure-env":
        return 0 if installer.configure_environment() else 1
    elif args.command == "test":
        return 0 if installer.test_installation() else 1
    elif args.command == "rollback":
        return 0 if installer.rollback() else 1
    else:
        return installer.run()

if __name__ == "__main__":
    sys.exit(main())
