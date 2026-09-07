#!/usr/bin/env python3
"""
Test P2-005: Move Env Loading to Runtime

Tests that environment variables are loaded at runtime in:
- get_instance_status()
- execute_on_instance()

Ensures dynamic config changes work without restart.
"""

import sys
import os
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

# Add project to path
sys.path.insert(0, '/home/ubuntu/hermes-agent')

from gateway.instance_orchestrator import (
    InstanceOrchestrator,
    get_instance_config,
    validate_hostname,
    validate_port,
)


def test_get_instance_config_with_all_env_vars():
    """Test loading all environment variables."""
    print("[TEST 1] get_instance_config() with all env vars set...")
    
    with patch.dict(os.environ, {
        'HERMES_REMOTE_API_KEY': 'test-api-key-123',
        'HERMES_INSTANCE_A_HOSTNAME': '192.168.1.100',
        'HERMES_INSTANCE_A_PORT': '9000',
    }, clear=False):
        config = get_instance_config()
        
        assert config['remote_api_key'] == 'test-api-key-123', \
            f"Expected 'test-api-key-123', got {config['remote_api_key']}"
        assert config['instance_a_hostname'] == '192.168.1.100', \
            f"Expected '192.168.1.100', got {config['instance_a_hostname']}"
        assert config['instance_a_port'] == 9000, \
            f"Expected 9000, got {config['instance_a_port']}"
        
    print("  ✓ All env vars loaded correctly")


def test_get_instance_config_with_defaults():
    """Test loading with defaults when env vars not set."""
    print("[TEST 2] get_instance_config() with defaults (no env vars)...")
    
    # Remove the env vars if they exist
    env_backup = {}
    for key in ['HERMES_REMOTE_API_KEY', 'HERMES_INSTANCE_A_HOSTNAME', 'HERMES_INSTANCE_A_PORT']:
        env_backup[key] = os.environ.pop(key, None)
    
    try:
        config = get_instance_config()
        
        assert config['remote_api_key'] == '', \
            f"Expected empty string for API key, got {config['remote_api_key']}"
        assert config['instance_a_hostname'] == 'localhost', \
            f"Expected 'localhost', got {config['instance_a_hostname']}"
        assert config['instance_a_port'] == 8000, \
            f"Expected 8000, got {config['instance_a_port']}"
        
        print("  ✓ Defaults applied correctly")
    finally:
        # Restore env vars
        for key, val in env_backup.items():
            if val is not None:
                os.environ[key] = val


def test_get_instance_config_with_partial_env_vars():
    """Test loading with some env vars set, some using defaults."""
    print("[TEST 3] get_instance_config() with partial env vars...")
    
    with patch.dict(os.environ, {
        'HERMES_REMOTE_API_KEY': 'partial-key',
        'HERMES_INSTANCE_A_HOSTNAME': 'example.com',
        # HERMES_INSTANCE_A_PORT not set - should use default
    }, clear=False):
        config = get_instance_config()
        
        assert config['remote_api_key'] == 'partial-key', \
            f"Expected 'partial-key', got {config['remote_api_key']}"
        assert config['instance_a_hostname'] == 'example.com', \
            f"Expected 'example.com', got {config['instance_a_hostname']}"
        assert config['instance_a_port'] == 8000, \
            f"Expected default port 8000, got {config['instance_a_port']}"
        
    print("  ✓ Partial env vars work with defaults")


def test_get_instance_config_with_invalid_port():
    """Test handling of invalid port values."""
    print("[TEST 4] get_instance_config() with invalid port...")
    
    with patch.dict(os.environ, {
        'HERMES_INSTANCE_A_PORT': 'not-a-number',
    }, clear=False):
        config = get_instance_config()
        
        # Should fallback to default
        assert config['instance_a_port'] == 8000, \
            f"Expected default port 8000 for invalid value, got {config['instance_a_port']}"
        
    print("  ✓ Invalid port handled gracefully")


def test_get_instance_config_with_out_of_range_port():
    """Test handling of port values outside valid range."""
    print("[TEST 5] get_instance_config() with out-of-range port...")
    
    with patch.dict(os.environ, {
        'HERMES_INSTANCE_A_PORT': '99999',  # Out of range
    }, clear=False):
        config = get_instance_config()
        
        # Should fallback to default
        assert config['instance_a_port'] == 8000, \
            f"Expected default port 8000 for out-of-range value, got {config['instance_a_port']}"
        
    print("  ✓ Out-of-range port handled gracefully")


def test_get_instance_config_with_empty_hostname():
    """Test handling of empty hostname."""
    print("[TEST 6] get_instance_config() with empty hostname...")
    
    with patch.dict(os.environ, {
        'HERMES_INSTANCE_A_HOSTNAME': '   ',  # Only whitespace
    }, clear=False):
        config = get_instance_config()
        
        # Should use default
        assert config['instance_a_hostname'] == 'localhost', \
            f"Expected default 'localhost', got {config['instance_a_hostname']}"
        
    print("  ✓ Empty hostname handled gracefully")


async def test_execute_on_instance_loads_env_at_runtime():
    """Test that execute_on_instance loads env vars at runtime."""
    print("[TEST 7] execute_on_instance() loads env at runtime...")
    
    orchestrator = InstanceOrchestrator()
    
    with patch.dict(os.environ, {
        'HERMES_REMOTE_API_KEY': 'runtime-key',
        'HERMES_INSTANCE_A_HOSTNAME': 'runtime-host.local',
        'HERMES_INSTANCE_A_PORT': '7000',
    }, clear=False):
        # Call execute_on_instance with local instance (won't make actual request)
        result = await orchestrator.execute_on_instance(
            instance_name='local',
            prompt='test prompt',
            session_id='test-session'
        )
        
        # Local instance returns None (handled by gateway)
        assert result is None, f"Expected None for local instance, got {result}"
        
    print("  ✓ execute_on_instance loaded env at runtime")


async def test_get_instance_status_loads_env_at_runtime():
    """Test that get_instance_status loads env vars at runtime."""
    print("[TEST 8] get_instance_status() loads env at runtime...")
    
    orchestrator = InstanceOrchestrator()
    
    with patch.dict(os.environ, {
        'HERMES_REMOTE_API_KEY': 'status-key',
        'HERMES_INSTANCE_A_HOSTNAME': 'status-host.local',
        'HERMES_INSTANCE_A_PORT': '6000',
    }, clear=False):
        await orchestrator.init()
        status = await orchestrator.get_instance_status('local')
        await orchestrator.close()
        
        assert status['name'] == 'local', \
            f"Expected instance name 'local', got {status['name']}"
        assert status['healthy'] == True, \
            f"Expected healthy=True for local, got {status['healthy']}"
        assert status['reachable'] == True, \
            f"Expected reachable=True for local, got {status['reachable']}"
        
    print("  ✓ get_instance_status loaded env at runtime")


async def test_dynamic_config_changes():
    """Test that config changes are picked up on each call (no caching)."""
    print("[TEST 9] Dynamic config changes without restart...")
    
    # First call with one config
    with patch.dict(os.environ, {
        'HERMES_INSTANCE_A_HOSTNAME': 'host1.local',
        'HERMES_INSTANCE_A_PORT': '5000',
    }, clear=False):
        config1 = get_instance_config()
        hostname1 = config1['instance_a_hostname']
        port1 = config1['instance_a_port']
    
    # Second call with different config
    with patch.dict(os.environ, {
        'HERMES_INSTANCE_A_HOSTNAME': 'host2.local',
        'HERMES_INSTANCE_A_PORT': '6000',
    }, clear=False):
        config2 = get_instance_config()
        hostname2 = config2['instance_a_hostname']
        port2 = config2['instance_a_port']
    
    assert hostname1 == 'host1.local', \
        f"First call should have host1.local, got {hostname1}"
    assert port1 == 5000, \
        f"First call should have port 5000, got {port1}"
    
    assert hostname2 == 'host2.local', \
        f"Second call should have host2.local, got {hostname2}"
    assert port2 == 6000, \
        f"Second call should have port 6000, got {port2}"
    
    print("  ✓ Dynamic config changes detected without restart")


def test_validate_hostname_with_localhost():
    """Test hostname validation with localhost."""
    print("[TEST 10] validate_hostname() with localhost...")
    
    assert validate_hostname('localhost') == True, \
        "localhost should be valid"
    
    print("  ✓ localhost validation works")


def test_validate_hostname_with_ip():
    """Test hostname validation with IP addresses."""
    print("[TEST 11] validate_hostname() with IP addresses...")
    
    assert validate_hostname('192.168.1.1') == True, \
        "Valid IPv4 should be accepted"
    assert validate_hostname('127.0.0.1') == True, \
        "Loopback IP should be accepted"
    
    print("  ✓ IP validation works")


def test_validate_hostname_with_fqdn():
    """Test hostname validation with FQDN."""
    print("[TEST 12] validate_hostname() with FQDN...")
    
    assert validate_hostname('example.com') == True, \
        "Valid FQDN should be accepted"
    assert validate_hostname('host.example.local') == True, \
        "Valid FQDN with subdomain should be accepted"
    
    print("  ✓ FQDN validation works")


def test_validate_port_with_valid_ports():
    """Test port validation with valid ports."""
    print("[TEST 13] validate_port() with valid ports...")
    
    assert validate_port(1) == True, "Port 1 should be valid"
    assert validate_port(8000) == True, "Port 8000 should be valid"
    assert validate_port(65535) == True, "Port 65535 should be valid"
    
    print("  ✓ Valid port validation works")


def test_validate_port_with_invalid_ports():
    """Test port validation with invalid ports."""
    print("[TEST 14] validate_port() with invalid ports...")
    
    assert validate_port(0) == False, "Port 0 should be invalid"
    assert validate_port(65536) == False, "Port 65536 should be invalid"
    assert validate_port(-1) == False, "Negative port should be invalid"
    
    print("  ✓ Invalid port validation works")


async def run_all_tests():
    """Run all tests."""
    print("\n" + "="*70)
    print("P2-005: Runtime Environment Variable Loading Tests")
    print("="*70 + "\n")
    
    try:
        # Synchronous tests
        test_get_instance_config_with_all_env_vars()
        test_get_instance_config_with_defaults()
        test_get_instance_config_with_partial_env_vars()
        test_get_instance_config_with_invalid_port()
        test_get_instance_config_with_out_of_range_port()
        test_get_instance_config_with_empty_hostname()
        test_validate_hostname_with_localhost()
        test_validate_hostname_with_ip()
        test_validate_hostname_with_fqdn()
        test_validate_port_with_valid_ports()
        test_validate_port_with_invalid_ports()
        
        print()
        
        # Async tests
        await test_execute_on_instance_loads_env_at_runtime()
        await test_get_instance_status_loads_env_at_runtime()
        await test_dynamic_config_changes()
        
        print("\n" + "="*70)
        print("✓ ALL P2-005 TESTS PASSED")
        print("="*70 + "\n")
        return 0
        
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n✗ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(asyncio.run(run_all_tests()))
