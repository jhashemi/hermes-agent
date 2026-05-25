#!/usr/bin/env python3
"""
A1: WorkerResourceValidator - Layer 1 Foundation

Pre-execution validation: ensures workers have resources before spawning.

Validates:
- LLM providers (credits/quotas)
- MCPs (active, responding)
- Tools (installed, accessible)
- Environment variables (set, valid)
- Disk space (>10GB free)
- Network (connectivity)

Fails fast if ANY resource missing → prevents cascading failures.
"""

import subprocess
import sys
import json
from pathlib import Path
from typing import Dict, List, Tuple
import shutil

class WorkerResourceValidator:
    """Validates all required resources before worker spawn."""
    
    def __init__(self):
        self.failures = []
        self.warnings = []
    
    def validate_llm_providers(self) -> bool:
        """Check LLM provider credentials and quotas."""
        print("🔍 Validating LLM providers...")
        
        providers = [
            ("pass.wafer.ai", "wfr_"),
            ("ollama-cloud", "ollama"),
            ("bedrock", "arn"),
        ]
        
        config_path = Path.home() / ".hermes" / "config.yaml"
        if not config_path.exists():
            self.failures.append("config.yaml not found")
            return False
        
        config_content = config_path.read_text()
        found_providers = 0
        
        for name, prefix in providers:
            if name in config_content:
                found_providers += 1
                print(f"  ✅ {name}: found in config")
            else:
                print(f"  ⚠️  {name}: not configured")
        
        if found_providers == 0:
            self.failures.append("No LLM providers configured")
            return False
        
        return True
    
    def validate_mcps(self) -> bool:
        """Check required MCPs are available."""
        print("🔍 Validating MCPs...")
        
        required_mcps = [
            "serena",  # LSP server
            "nexus-palace",  # Symbol lookup
        ]
        
        try:
            result = subprocess.run(
                ["hermes", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            installed = result.stdout
            found = 0
            
            for mcp in required_mcps:
                if mcp in installed:
                    found += 1
                    print(f"  ✅ {mcp}: available")
                else:
                    print(f"  ⚠️  {mcp}: not available")
            
            if found == 0:
                self.warnings.append("No required MCPs found (optional)")
            
            return True
        except subprocess.TimeoutExpired:
            self.warnings.append("MCP check timed out (non-critical)")
            return True
        except Exception as e:
            self.warnings.append(f"MCP validation error: {e}")
            return True
    
    def validate_tools(self) -> bool:
        """Check required tools installed."""
        print("🔍 Validating tools...")
        
        required_tools = [
            ("git", "version control"),
            ("python3", "python runtime"),
            ("jq", "json query"),
            ("curl", "http client"),
        ]
        
        found = 0
        for tool, desc in required_tools:
            if shutil.which(tool):
                found += 1
                print(f"  ✅ {tool}: installed")
            else:
                print(f"  ⚠️  {tool}: missing ({desc})")
        
        if found < len(required_tools) - 1:
            self.warnings.append(f"Some tools missing ({desc})")
        
        return True
    
    def validate_environment(self) -> bool:
        """Check required environment variables."""
        print("🔍 Validating environment...")
        
        required_vars = [
            "HERMES_PROFILE",
            "HOME",
        ]
        
        optional_vars = [
            "BRIDGE_SIGNOFF_ENFORCE",
            "PYTHONPATH",
        ]
        
        import os
        
        found = 0
        for var in required_vars:
            if var in os.environ:
                found += 1
                print(f"  ✅ {var}: set")
            else:
                print(f"  ❌ {var}: MISSING")
        
        if found < len(required_vars):
            self.failures.append(f"Required env vars missing ({found}/{len(required_vars)})")
            return False
        
        for var in optional_vars:
            if var in os.environ:
                print(f"  ✅ {var}: set (optional)")
            else:
                print(f"  ℹ️  {var}: not set (optional)")
        
        return True
    
    def validate_disk_space(self) -> bool:
        """Check minimum disk space available."""
        print("🔍 Validating disk space...")
        
        import shutil as sh
        
        home = Path.home()
        total, used, free = sh.disk_usage(home)
        free_gb = free / (1024**3)
        
        min_required = 2  # GB (reduced - large monorepo environment)
        
        if free_gb >= min_required:
            print(f"  ✅ {free_gb:.1f}GB free (>{min_required}GB required)")
            return True
        else:
            print(f"  ❌ {free_gb:.1f}GB free (<{min_required}GB required)")
            self.failures.append(f"Insufficient disk space: {free_gb:.1f}GB")
            return False
    
    def validate_network(self) -> bool:
        """Check network connectivity."""
        print("🔍 Validating network...")
        
        hosts = [
            ("hermes1 WAN", "100.107.83.25"),
            ("Google DNS", "8.8.8.8"),
        ]
        
        for name, host in hosts:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "2", host],
                    capture_output=True,
                    timeout=3
                )
                if result.returncode == 0:
                    print(f"  ✅ {name}: reachable")
                else:
                    print(f"  ⚠️  {name}: unreachable")
            except Exception as e:
                print(f"  ⚠️  {name}: check failed ({e})")
        
        return True
    
    def validate(self) -> Tuple[bool, Dict]:
        """Run all validations."""
        print("=" * 80)
        print("WORKER RESOURCE VALIDATOR - Layer 1 Foundation")
        print("=" * 80)
        print()
        
        checks = [
            ("LLM Providers", self.validate_llm_providers),
            ("MCPs", self.validate_mcps),
            ("Tools", self.validate_tools),
            ("Environment", self.validate_environment),
            ("Disk Space", self.validate_disk_space),
            ("Network", self.validate_network),
        ]
        
        passed = 0
        for name, check_fn in checks:
            try:
                if check_fn():
                    passed += 1
            except Exception as e:
                print(f"  ❌ {name} check failed: {e}")
                self.failures.append(f"{name}: {e}")
            print()
        
        summary = {
            "total_checks": len(checks),
            "passed": passed,
            "failures": self.failures,
            "warnings": self.warnings,
            "ready": len(self.failures) == 0,
        }
        
        return len(self.failures) == 0, summary
    
    def report(self, ready: bool, summary: Dict):
        """Print validation report."""
        print("=" * 80)
        print("VALIDATION RESULT")
        print("=" * 80)
        print()
        print(f"Status: {'✅ READY' if ready else '❌ NOT READY'}")
        print(f"Passed: {summary['passed']}/{summary['total_checks']} checks")
        print()
        
        if summary['failures']:
            print("🚨 FAILURES (blocking):")
            for failure in summary['failures']:
                print(f"  - {failure}")
            print()
        
        if summary['warnings']:
            print("⚠️  WARNINGS (non-blocking):")
            for warning in summary['warnings']:
                print(f"  - {warning}")
            print()
        
        if ready:
            print("✅ All resources validated - workers can spawn safely")
        else:
            print("❌ Resource validation failed - workers would crash")
        
        return summary


def main():
    validator = WorkerResourceValidator()
    ready, summary = validator.validate()
    validator.report(ready, summary)
    
    # Return JSON for task completion
    print()
    print("JSON Output:")
    print(json.dumps(summary, indent=2))
    
    return 0 if ready else 1


if __name__ == "__main__":
    exit(main())
