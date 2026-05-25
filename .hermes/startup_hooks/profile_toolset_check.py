"""
Startup hook: Profile toolset validation

Runs during agent initialization to catch missing toolset configurations
before workers spawn. Prevents silent crashes due to missing kanban tools.
"""

import sys
from pathlib import Path
from typing import Dict, Set, List

def check_profile_toolsets() -> None:
    """
    Validate profile toolsets at startup.
    Raises SystemExit if validation fails.
    """
    from tools.profile_toolset_validator import validate_all_profiles
    
    hermes_home = Path("~/.hermes").expanduser()
    errors = validate_all_profiles(hermes_home)
    
    if errors:
        print("\n" + "="*70)
        print("⚠️  PROFILE TOOLSET VALIDATION FAILED")
        print("="*70 + "\n")
        for error in errors:
            print(error)
        print("\n" + "="*70)
        print("Action: Add missing toolsets to profile config.yaml and restart")
        print("="*70 + "\n")
        sys.exit(1)


if __name__ == "__main__":
    check_profile_toolsets()
    print("✅ Profile toolsets validated")
