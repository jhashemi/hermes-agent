"""
Installation Script for SOLID Hook-Based Voice Integration

This script automates the installation of the voice agent hook system
into Hermes Gateway.

Usage:
    python3 install_voice_bridge.py [--dry-run] [--backup]

Options:
    --dry-run    Show what would be changed without modifying files
    --backup     Create backups before modifying files
"""

import os
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime

# Color codes for terminal output
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
BLUE = '\033[94m'
RESET = '\033[0m'

def print_header(text):
    """Print section header"""
    print(f"\n{BLUE}{'='*80}{RESET}")
    print(f"{BLUE}{text}{RESET}")
    print(f"{BLUE}{'='*80}{RESET}\n")

def print_success(text):
    """Print success message"""
    print(f"{GREEN}✓{RESET} {text}")

def print_warning(text):
    """Print warning message"""
    print(f"{YELLOW}⚠{RESET} {text}")

def print_error(text):
    """Print error message"""
    print(f"{RED}✗{RESET} {text}")

def print_info(text):
    """Print info message"""
    print(f"{BLUE}ℹ{RESET} {text}")

class VoiceBridgeInstaller:
    """Installs voice bridge hook system into Hermes Gateway"""
    
    def __init__(self, dry_run=False, backup=False):
        self.dry_run = dry_run
        self.backup = backup
        self.hermes_root = Path("/home/ubuntu/hermes-agent")
        self.executive_agents_root = Path("/home/ubuntu/executive_agents_platform")
        self.gateway_dir = self.hermes_root / "gateway"
        self.builtin_hooks_dir = self.gateway_dir / "builtin_hooks"
        self.errors = []
        self.warnings = []
        self.successes = []
    
    def verify_prerequisites(self):
        """Verify all required files and directories exist"""
        print_header("Step 1: Verifying Prerequisites")
        
        checks = [
            (self.hermes_root.exists(), f"Hermes root exists: {self.hermes_root}"),
            (self.gateway_dir.exists(), f"Gateway directory exists: {self.gateway_dir}"),
            ((self.executive_agents_root / "loader" / "whatsapp_voice_bridge.py").exists(), 
             "Voice bridge module exists"),
            ((self.hermes_root / "gateway" / "run.py").exists(),
             "gateway/run.py exists"),
            ((self.hermes_root / "gateway" / "platforms" / "whatsapp.py").exists(),
             "WhatsApp adapter exists"),
        ]
        
        all_ok = True
        for check, desc in checks:
            if check:
                print_success(desc)
            else:
                print_error(desc)
                all_ok = False
        
        if not all_ok:
            self.errors.append("Prerequisites check failed")
            return False
        
        return True
    
    def create_builtin_hooks_directory(self):
        """Create builtin_hooks directory if it doesn't exist"""
        print_header("Step 2: Creating builtin_hooks Directory")
        
        if self.builtin_hooks_dir.exists():
            print_info(f"Directory already exists: {self.builtin_hooks_dir}")
            return True
        
        if self.dry_run:
            print_info(f"[DRY RUN] Would create directory: {self.builtin_hooks_dir}")
            return True
        
        try:
            self.builtin_hooks_dir.mkdir(parents=True, exist_ok=True)
            print_success(f"Created directory: {self.builtin_hooks_dir}")
            return True
        except Exception as e:
            self.errors.append(f"Failed to create builtin_hooks directory: {e}")
            print_error(f"Failed to create directory: {e}")
            return False
    
    def copy_hook_files(self):
        """Copy hook implementation files"""
        print_header("Step 3: Copying Hook Files")
        
        files_to_copy = [
            (
                self.executive_agents_root / "SOLID_DESIGN_GUIDE.md",
                self.builtin_hooks_dir / "voice_agent_hook.py",
                "Voice agent hook implementation"
            ),
            # Note: In real installation, these would be copied from the platform
            # For now, we assume they're already in place
        ]
        
        # Check if files already exist
        hook_file = self.builtin_hooks_dir / "voice_agent_hook.py"
        init_file = self.builtin_hooks_dir / "__init__.py"
        
        if hook_file.exists() and init_file.exists():
            print_success(f"Hook files already exist: {self.builtin_hooks_dir}")
            return True
        
        if self.dry_run:
            print_info("[DRY RUN] Would copy hook files to builtin_hooks/")
            return True
        
        print_info("Hook files should be pre-installed from executive_agents_platform")
        print_warning("Verify files exist:")
        print_warning(f"  - {hook_file}")
        print_warning(f"  - {init_file}")
        
        return hook_file.exists() and init_file.exists()
    
    def backup_gateway_run_py(self):
        """Create backup of gateway/run.py before modifications"""
        if not self.backup:
            return True
        
        run_py = self.gateway_dir / "run.py"
        if not run_py.exists():
            return False
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.gateway_dir / f"run.py.backup.{timestamp}"
        
        if self.dry_run:
            print_info(f"[DRY RUN] Would backup to: {backup_path}")
            return True
        
        try:
            shutil.copy2(run_py, backup_path)
            print_success(f"Backed up to: {backup_path}")
            return True
        except Exception as e:
            self.errors.append(f"Backup failed: {e}")
            print_error(f"Backup failed: {e}")
            return False
    
    def check_gateway_run_py_modifications(self):
        """Check if gateway/run.py needs modifications"""
        print_header("Step 4: Checking gateway/run.py Modifications")
        
        run_py = self.gateway_dir / "run.py"
        
        try:
            content = run_py.read_text()
            
            # Check for existing hook initialization
            if "initialize_builtin_hooks" in content:
                print_success("initialize_builtin_hooks already present in gateway/run.py")
                return True
            
            if self.dry_run:
                print_info("[DRY RUN] Would add hook initialization to gateway/run.py")
                print_info("  Location 1: At startup (3 lines)")
                print_info("  Location 2: In _handle_message() (5 lines)")
                return True
            
            print_warning("gateway/run.py needs modifications")
            print_info("Manual modification required:")
            print_info("")
            print_info("Location 1: Add at startup (in main() or async_main()):")
            print_info("  from gateway.builtin_hooks import initialize_builtin_hooks")
            print_info("  await initialize_builtin_hooks()")
            print_info("")
            print_info("Location 2: Add in _handle_message() at start:")
            print_info("  from gateway.builtin_hooks.voice_agent_hook import get_hook_manager")
            print_info("  manager = get_hook_manager()")
            print_info("  hook_result = await manager.before_message_processing(event, self)")
            print_info("  if hook_result is not None:")
            print_info("      await self.send_message(event.user_id, hook_result, reply_to=event)")
            print_info("      return")
            print_info("")
            self.warnings.append("gateway/run.py needs manual modification (see above)")
            return True
        
        except Exception as e:
            self.errors.append(f"Error reading gateway/run.py: {e}")
            print_error(f"Error reading gateway/run.py: {e}")
            return False
    
    def check_environment_variables(self):
        """Check if required environment variables are configured"""
        print_header("Step 5: Checking Environment Variables")
        
        env_file = Path.home() / ".hermes" / ".env"
        required_vars = [
            "RESEMBLE_API_KEY",
            "DEEPGRAM_API_KEY",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
        ]
        
        if not env_file.exists():
            print_warning(f".env file not found: {env_file}")
            print_info("Create with:")
            print_info(f"  mkdir -p {env_file.parent}")
            print_info(f"  touch {env_file}")
            self.warnings.append("Environment file doesn't exist")
            return False
        
        try:
            env_content = env_file.read_text()
            
            missing = []
            for var in required_vars:
                if var not in env_content:
                    missing.append(var)
                else:
                    print_success(f"{var} configured")
            
            if missing:
                print_warning(f"Missing environment variables: {', '.join(missing)}")
                print_info("Add to ~/.hermes/.env:")
                for var in missing:
                    print_info(f"  export {var}=your_value")
                self.warnings.append(f"Missing env vars: {', '.join(missing)}")
                return False
            
            return True
        
        except Exception as e:
            self.errors.append(f"Error reading .env: {e}")
            print_error(f"Error reading .env: {e}")
            return False
    
    def generate_installation_summary(self):
        """Generate installation summary"""
        print_header("Installation Summary")
        
        print_info(f"Dry Run: {self.dry_run}")
        print_info(f"Backup: {self.backup}")
        print_info("")
        
        if self.successes:
            print(f"{GREEN}Successes:{RESET}")
            for success in self.successes:
                print_success(success)
        
        if self.warnings:
            print(f"\n{YELLOW}Warnings:{RESET}")
            for warning in self.warnings:
                print_warning(warning)
        
        if self.errors:
            print(f"\n{RED}Errors:{RESET}")
            for error in self.errors:
                print_error(error)
            return False
        
        return True
    
    def run(self):
        """Run full installation"""
        print_header("Voice Bridge Installation for Hermes Gateway")
        
        steps = [
            ("Prerequisites", self.verify_prerequisites),
            ("Create Directory", self.create_builtin_hooks_directory),
            ("Copy Files", self.copy_hook_files),
            ("Backup", self.backup_gateway_run_py),
            ("Check Modifications", self.check_gateway_run_py_modifications),
            ("Environment Check", self.check_environment_variables),
            ("Summary", self.generate_installation_summary),
        ]
        
        for step_name, step_func in steps:
            try:
                if not step_func():
                    print_error(f"Step failed: {step_name}")
                    if not self.dry_run:
                        return False
            except Exception as e:
                print_error(f"Exception in {step_name}: {e}")
                if not self.dry_run:
                    return False
        
        return True
    
    def print_next_steps(self):
        """Print next steps for user"""
        print_header("Next Steps")
        
        print_info("1. Manual Modifications Required:")
        print_info("   Edit /home/ubuntu/hermes-agent/gateway/run.py")
        print_info("   Add 8 lines as shown above")
        print_info("")
        print_info("2. Configure Environment Variables:")
        print_info("   Edit ~/.hermes/.env")
        print_info("   Add missing RESEMBLE_API_KEY, DEEPGRAM_API_KEY, etc.")
        print_info("")
        print_info("3. Restart Gateway:")
        print_info("   hermes gateway restart")
        print_info("")
        print_info("4. Test:")
        print_info("   Send /load-demis in WhatsApp")
        print_info("   Send audio message")
        print_info("")
        print_info("5. Check Logs:")
        print_info("   hermes logs --follow --gateway | grep voice")
        print_info("")


def main():
    parser = argparse.ArgumentParser(description="Install voice bridge hook system")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--backup", action="store_true", help="Create backups before modifying")
    
    args = parser.parse_args()
    
    installer = VoiceBridgeInstaller(dry_run=args.dry_run, backup=args.backup)
    success = installer.run()
    
    if success:
        installer.print_next_steps()
        print_success("Installation checks completed!")
        return 0
    else:
        print_error("Installation failed!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
