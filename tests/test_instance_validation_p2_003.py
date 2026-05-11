"""
Test suite for P2-003: IP/Port Validation

Tests hostname validation (valid IPs, FQDNs, invalid combinations) and
port range validation (1-65535).
"""

import pytest
from gateway.instance_orchestrator import (
    validate_hostname,
    validate_port,
    InstanceOrchestrator,
    RemoteHermesInstance,
    HERMES_INSTANCES,
)


class TestValidateHostname:
    """Test hostname validation for IPs and FQDNs."""

    def test_valid_ipv4_addresses(self):
        """Test valid IPv4 addresses."""
        valid_ips = [
            "127.0.0.1",
            "192.168.1.1",
            "10.0.0.1",
            "172.16.0.1",
            "8.8.8.8",
            "255.255.255.255",
            "0.0.0.0",
            "100.79.15.66",  # Tailscale example from code
        ]
        for ip in valid_ips:
            assert validate_hostname(ip) is True, f"Failed for valid IP: {ip}"

    def test_invalid_ipv4_addresses(self):
        """Test invalid IPv4 addresses."""
        invalid_ips = [
            "256.1.1.1",        # Octet > 255
            "192.168.1",        # Missing octet
            "192.168.1.1.1",    # Too many octets
            "192.168.-1.1",     # Negative octet
            "192.168.1.a",      # Non-numeric
            "....",             # Just dots
            "1.1.1",            # Too few octets
        ]
        for ip in invalid_ips:
            assert validate_hostname(ip) is False, f"Should reject invalid IP: {ip}"

    def test_valid_fqdns(self):
        """Test valid FQDNs."""
        valid_fqdns = [
            "localhost",
            "example.com",
            "sub.example.com",
            "my-server.example.com",
            "hermes2.flounder-snake.ts.net",
            "a.b.c.d.e.f",
            "test-123.example-456.com",
            "x.co",
        ]
        for fqdn in valid_fqdns:
            assert validate_hostname(fqdn) is True, f"Failed for valid FQDN: {fqdn}"

    def test_invalid_fqdns(self):
        """Test invalid FQDNs."""
        invalid_fqdns = [
            "-invalid.com",           # Starts with hyphen
            "invalid-.com",           # Label ends with hyphen
            "invalid..com",           # Double dot
            ".invalid.com",           # Starts with dot
            "invalid.com.",           # Ends with dot (technically valid in DNS, but reject for safety)
            "invalid!.com",           # Invalid character
            "invalid@.com",           # Invalid character
            "invalid .com",           # Space
            "",                       # Empty string
            " ",                      # Just whitespace
        ]
        for fqdn in invalid_fqdns:
            assert validate_hostname(fqdn) is False, f"Should reject invalid FQDN: {fqdn}"

    def test_ipv6_addresses(self):
        """Test IPv6 addresses (basic validation)."""
        valid_ipv6 = [
            "::1",
            "2001:db8::1",
            "fe80::1",
        ]
        for ipv6 in valid_ipv6:
            assert validate_hostname(ipv6) is True, f"Failed for valid IPv6: {ipv6}"

    def test_hostname_type_error(self):
        """Test that non-string hostnames raise ValueError."""
        with pytest.raises(ValueError, match="hostname must be a string"):
            validate_hostname(123)
        with pytest.raises(ValueError, match="hostname must be a string"):
            validate_hostname(None)
        with pytest.raises(ValueError, match="hostname must be a string"):
            validate_hostname(["localhost"])

    def test_empty_hostname_raises_error(self):
        """Test that empty hostname raises ValueError."""
        with pytest.raises(ValueError, match="hostname cannot be empty"):
            validate_hostname("")
        with pytest.raises(ValueError, match="hostname cannot be empty"):
            validate_hostname("   ")


class TestValidatePort:
    """Test port number validation."""

    def test_valid_ports(self):
        """Test valid port numbers."""
        valid_ports = [
            1,          # Minimum
            8000,       # HTTP alt
            8080,       # HTTP alt
            443,        # HTTPS
            22,         # SSH
            65535,      # Maximum
            3306,       # MySQL
            5432,       # PostgreSQL
        ]
        for port in valid_ports:
            assert validate_port(port) is True, f"Failed for valid port: {port}"

    def test_invalid_ports(self):
        """Test invalid port numbers."""
        invalid_ports = [
            0,          # Too low
            -1,         # Negative
            65536,      # Too high
            100000,     # Way too high
        ]
        for port in invalid_ports:
            assert validate_port(port) is False, f"Should reject invalid port: {port}"

    def test_port_type_error(self):
        """Test that non-integer ports raise ValueError."""
        with pytest.raises(ValueError, match="port must be an integer"):
            validate_port("8000")
        with pytest.raises(ValueError, match="port must be an integer"):
            validate_port(8000.5)
        with pytest.raises(ValueError, match="port must be an integer"):
            validate_port(None)


class TestInstanceOrchestratorValidation:
    """Test validation integration in InstanceOrchestrator methods."""

    def test_set_current_instance_with_valid_instance(self):
        """Test setting current instance with valid parameters."""
        orchestrator = InstanceOrchestrator()
        # Should not raise
        result = orchestrator.set_current_instance("local")
        assert result is True
        assert orchestrator.current_instance == "local"

    def test_set_current_instance_invalid_hostname_should_raise(self):
        """Test that invalid hostname raises ValueError."""
        orchestrator = InstanceOrchestrator()
        
        # Create a temporary instance with invalid hostname
        HERMES_INSTANCES["invalid_test"] = RemoteHermesInstance(
            name="invalid_test",
            hostname="256.256.256.256",  # Invalid IP
            ip="100.0.0.1",
            http_port=8000,
        )
        
        try:
            with pytest.raises(ValueError, match="Invalid hostname"):
                orchestrator.set_current_instance("invalid_test")
        finally:
            # Cleanup
            del HERMES_INSTANCES["invalid_test"]

    def test_set_current_instance_invalid_port_should_raise(self):
        """Test that invalid port raises ValueError."""
        orchestrator = InstanceOrchestrator()
        
        # Create a temporary instance with invalid port
        HERMES_INSTANCES["invalid_port_test"] = RemoteHermesInstance(
            name="invalid_port_test",
            hostname="example.com",
            ip="100.0.0.1",
            http_port=99999,  # Invalid port
        )
        
        try:
            with pytest.raises(ValueError, match="Invalid port"):
                orchestrator.set_current_instance("invalid_port_test")
        finally:
            # Cleanup
            del HERMES_INSTANCES["invalid_port_test"]

    def test_set_current_instance_with_chat_id(self):
        """Test setting instance with chat_id."""
        orchestrator = InstanceOrchestrator()
        result = orchestrator.set_current_instance("local", chat_id="user123")
        assert result is True

    def test_set_current_instance_nonexistent_returns_false(self):
        """Test that nonexistent instance returns False without raising."""
        orchestrator = InstanceOrchestrator()
        result = orchestrator.set_current_instance("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_on_instance_with_valid_instance(self):
        """Test execute_on_instance with valid parameters."""
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        try:
            # Local instance should return None (delegates to gateway)
            result = await orchestrator.execute_on_instance("local", "test prompt")
            assert result is None
        finally:
            await orchestrator.close()

    @pytest.mark.asyncio
    async def test_execute_on_instance_invalid_hostname_raises(self):
        """Test that invalid hostname raises ValueError."""
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        # Create a temporary instance with invalid hostname
        HERMES_INSTANCES["invalid_exec_test"] = RemoteHermesInstance(
            name="invalid_exec_test",
            hostname="invalid..hostname",  # Invalid
            ip="100.0.0.1",
            http_port=8000,
        )
        
        try:
            with pytest.raises(ValueError, match="Invalid hostname"):
                await orchestrator.execute_on_instance("invalid_exec_test", "test")
        finally:
            del HERMES_INSTANCES["invalid_exec_test"]
            await orchestrator.close()

    @pytest.mark.asyncio
    async def test_execute_on_instance_invalid_port_raises(self):
        """Test that invalid port raises ValueError."""
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        # Create a temporary instance with invalid port
        HERMES_INSTANCES["invalid_port_exec_test"] = RemoteHermesInstance(
            name="invalid_port_exec_test",
            hostname="example.com",
            ip="100.0.0.1",
            http_port=0,  # Invalid
        )
        
        try:
            with pytest.raises(ValueError, match="Invalid port"):
                await orchestrator.execute_on_instance("invalid_port_exec_test", "test")
        finally:
            del HERMES_INSTANCES["invalid_port_exec_test"]
            await orchestrator.close()

    @pytest.mark.asyncio
    async def test_execute_on_instance_nonexistent_returns_error(self):
        """Test that nonexistent instance returns error message."""
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        try:
            result = await orchestrator.execute_on_instance("nonexistent", "test")
            assert "not found" in result.lower()
        finally:
            await orchestrator.close()


class TestExistingInstancesValidation:
    """Validate that existing instances in HERMES_INSTANCES pass validation."""

    def test_all_existing_instances_have_valid_hostnames(self):
        """Verify all registered instances have valid hostnames."""
        for name, instance in HERMES_INSTANCES.items():
            assert validate_hostname(instance.hostname) is True, \
                f"Instance '{name}' has invalid hostname: {instance.hostname}"

    def test_all_existing_instances_have_valid_ports(self):
        """Verify all registered instances have valid port numbers."""
        for name, instance in HERMES_INSTANCES.items():
            assert validate_port(instance.http_port) is True, \
                f"Instance '{name}' has invalid port: {instance.http_port}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
