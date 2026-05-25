"""
Profile toolset validator — ensures profiles have required toolsets for their role.

Prevents misconfiguration where profiles lack essential tools (kanban, web, etc.)
that are needed for autonomous execution.
"""

import sys
from pathlib import Path
from typing import Set, Dict, List

def validate_profile_toolsets(profile_path: Path, required_by_role: Dict[str, Set[str]]) -> List[str]:
    """
    Validate that a profile has all required toolsets for its declared role.
    
    Args:
        profile_path: Path to profile config.yaml
        required_by_role: Mapping of role -> required toolsets
            E.g. {"kanban_worker": {"kanban", "hermes-cli"}, "embodied_agent": {"web", ...}}
    
    Returns:
        List of validation errors (empty if valid)
    """
    import yaml
    
    errors = []
    
    try:
        with open(profile_path) as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        return [f"Failed to parse {profile_path}: {e}"]
    
    profile_name = config.get('name', profile_path.stem)
    role = config.get('role', 'default')
    declared_toolsets = set(config.get('toolsets', []))
    
    # Check against role requirements
    required = required_by_role.get(role, set()) or required_by_role.get('default', set())
    
    if required:
        missing = required - declared_toolsets
        if missing:
            errors.append(
                f"Profile '{profile_name}' (role: {role}) missing toolsets: {sorted(missing)}\n"
                f"  Declared: {sorted(declared_toolsets)}\n"
                f"  Required: {sorted(required)}\n"
                f"  Add to config.yaml: toolsets: {sorted(declared_toolsets | required)}"
            )
    
    return errors


# Role-based toolset requirements
REQUIRED_TOOLSETS_BY_ROLE: Dict[str, Set[str]] = {
    "default": {"hermes-cli", "kanban"},  # All profiles need kanban for task lifecycle
    "kanban_worker": {"hermes-cli", "kanban", "terminal"},
    "embodied_agent": {"hermes-cli", "web", "file"},
    "code_execution": {"hermes-cli", "terminal", "file"},
}


def validate_all_profiles(hermes_home: Path) -> List[str]:
    """Validate all profiles in ~/.hermes/profiles/"""
    
    profiles_dir = hermes_home / "profiles"
    if not profiles_dir.exists():
        return []
    
    all_errors = []
    for profile_dir in profiles_dir.iterdir():
        if profile_dir.is_dir():
            config_file = profile_dir / "config.yaml"
            if config_file.exists():
                errors = validate_profile_toolsets(config_file, REQUIRED_TOOLSETS_BY_ROLE)
                all_errors.extend(errors)
    
    return all_errors


def main():
    """CLI: validate toolsets in ~/.hermes/profiles"""
    import os
    hermes_home = Path(os.path.expanduser("~/.hermes"))
    
    errors = validate_all_profiles(hermes_home)
    
    if errors:
        print("❌ Profile Toolset Validation Failed:\n")
        for error in errors:
            print(f"  {error}\n")
        sys.exit(1)
    else:
        print("✅ All profiles have required toolsets")
        sys.exit(0)


if __name__ == "__main__":
    main()
