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
            "invalid",                # Single label (not "localhost") — not a valid FQDN
            "192.168.1.a",            # Partial IP with alpha suffix — rejected by letter-TLD rule
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
        """Test that invalid hostname raises ValueError at construction time (DoD #4)."""
        # After P2-003 (t_fcc68f00), invalid hostnames are rejected at
        # __init__ — before, they only failed at set_current_instance/
        # execute_on_instance time. We assert construction itself blows up.
        with pytest.raises(ValueError, match="Invalid hostname"):
            RemoteHermesInstance(
                name="invalid_test",
                hostname="256.256.256.256",  # Invalid IP
                ip="100.0.0.1",
                http_port=8000,
            )

    def test_set_current_instance_invalid_port_should_raise(self):
        """Test that invalid port raises ValueError at construction time (DoD #4)."""
        with pytest.raises(ValueError, match="Invalid http_port"):
            RemoteHermesInstance(
                name="invalid_port_test",
                hostname="example.com",
                ip="100.0.0.1",
                http_port=99999,  # Invalid port
            )

    def test_set_current_instance_call_site_guard_survives_registry_mutation(self):
        """Regression guard: set_current_instance re-validates so hot-mutation
        of an already-constructed instance still fails cleanly. Prevents a
        bad actor from bypassing __init__ by patching fields post-hoc."""
        orchestrator = InstanceOrchestrator()
        good = RemoteHermesInstance(
            name="mutated_test",
            hostname="example.com",
            ip="100.0.0.1",
            http_port=8000,
        )
        # Bypass __init__ validation by patching the attribute directly.
        good.hostname = "256.256.256.256"
        HERMES_INSTANCES["mutated_test"] = good
        try:
            with pytest.raises(ValueError, match="Invalid hostname"):
                orchestrator.set_current_instance("mutated_test")
        finally:
            del HERMES_INSTANCES["mutated_test"]

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
        """Regression guard: execute_on_instance re-validates so registry
        mutation post-construction still fails cleanly."""
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()

        # Construct a valid instance, then mutate the field to bypass __init__.
        good = RemoteHermesInstance(
            name="invalid_exec_test",
            hostname="example.com",
            ip="100.0.0.1",
            http_port=8000,
        )
        good.hostname = "invalid..hostname"
        HERMES_INSTANCES["invalid_exec_test"] = good

        try:
            with pytest.raises(ValueError, match="Invalid hostname"):
                await orchestrator.execute_on_instance("invalid_exec_test", "test")
        finally:
            del HERMES_INSTANCES["invalid_exec_test"]
            await orchestrator.close()

    @pytest.mark.asyncio
    async def test_execute_on_instance_invalid_port_raises(self):
        """Regression guard: execute_on_instance re-validates so registry
        mutation post-construction still fails cleanly."""
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()

        good = RemoteHermesInstance(
            name="invalid_port_exec_test",
            hostname="example.com",
            ip="100.0.0.1",
            http_port=8000,
        )
        good.http_port = 0  # bypass __init__ validation
        HERMES_INSTANCES["invalid_port_exec_test"] = good

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


class TestRemoteHermesInstanceInit:
    """DoD #4 (t_fcc68f00): validate at construction time.

    Explicit coverage that RemoteHermesInstance.__init__ rejects invalid
    IPs and ports and accepts the full valid space (IPv4, IPv6, FQDN,
    port range).
    """

    # --- rejects invalid IPs -------------------------------------------------

    @pytest.mark.parametrize("bad", [
        "999.999.999.999",
        "256.256.256.256",
        "192.168.1",        # partial IP
        "192.168.1.1.1",    # too many octets
        "192.168.1.a",      # letter-suffixed partial IP
        "invalid",          # bare label, not a valid FQDN either
        "invalid..hostname",  # double dot
        "",                 # empty string
        " ",                # whitespace
    ])
    def test_init_rejects_invalid_hostname(self, bad):
        with pytest.raises(ValueError, match="Invalid hostname|hostname"):
            RemoteHermesInstance(
                name="t", hostname=bad, ip="127.0.0.1", http_port=8000,
            )

    @pytest.mark.parametrize("bad", [
        "999.999.999.999", "256.256.256.256", "192.168.1", "invalid..hostname",
    ])
    def test_init_rejects_invalid_ip_field(self, bad):
        with pytest.raises(ValueError, match="Invalid ip|ip "):
            RemoteHermesInstance(
                name="t", hostname="example.com", ip=bad, http_port=8000,
            )

    # --- rejects invalid ports -----------------------------------------------

    @pytest.mark.parametrize("bad_port", [0, -1, 70000, 65536, 99999])
    def test_init_rejects_invalid_port(self, bad_port):
        with pytest.raises(ValueError, match="Invalid http_port"):
            RemoteHermesInstance(
                name="t", hostname="example.com", ip="127.0.0.1",
                http_port=bad_port,
            )

    def test_init_rejects_non_int_port(self):
        # validate_port raises ValueError on non-int; __init__ propagates.
        with pytest.raises(ValueError):
            RemoteHermesInstance(
                name="t", hostname="example.com", ip="127.0.0.1",
                http_port="8000",  # type: ignore[arg-type]  # str, not int — testing runtime guard
            )

    # --- accepts valid combos ------------------------------------------------

    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "192.168.1.1", "10.0.0.1", "8.8.8.8",
        "0.0.0.0", "255.255.255.255",
        "::1", "2001:db8::1", "fe80::1",  # IPv6
    ])
    def test_init_accepts_valid_ipv4_and_ipv6(self, ip):
        inst = RemoteHermesInstance(
            name="t", hostname=ip, ip=ip, http_port=8000,
        )
        assert inst.hostname == ip
        assert inst.ip == ip

    @pytest.mark.parametrize("host", [
        "example.com", "hermes2.flounder-snake.ts.net",
        "sub.domain.example.co", "localhost",
    ])
    def test_init_accepts_valid_fqdn(self, host):
        inst = RemoteHermesInstance(
            name="t", hostname=host, ip="127.0.0.1", http_port=8000,
        )
        assert inst.hostname == host

    @pytest.mark.parametrize("port", [1, 22, 80, 443, 8000, 8080, 65535])
    def test_init_accepts_valid_port_range(self, port):
        inst = RemoteHermesInstance(
            name="t", hostname="example.com", ip="127.0.0.1", http_port=port,
        )
        assert inst.http_port == port

    # --- error messages are clear (DoD acceptance criterion) ----------------

    def test_error_message_names_the_field_and_bad_value(self):
        try:
            RemoteHermesInstance(
                name="edge_node", hostname="999.999.999.999",
                ip="127.0.0.1", http_port=8000,
            )
        except ValueError as e:
            msg = str(e)
            assert "hostname" in msg
            assert "edge_node" in msg
            assert "999.999.999.999" in msg
        else:
            pytest.fail("expected ValueError")

    def test_error_message_for_bad_port(self):
        try:
            RemoteHermesInstance(
                name="edge_node", hostname="example.com",
                ip="127.0.0.1", http_port=70000,
            )
        except ValueError as e:
            msg = str(e)
            assert "http_port" in msg
            assert "70000" in msg
            assert "1" in msg and "65535" in msg  # range in message
        else:
            pytest.fail("expected ValueError")


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
