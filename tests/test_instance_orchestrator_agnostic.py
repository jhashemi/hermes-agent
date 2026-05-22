"""Tests for the platform-agnostic instance orchestrator.

Covers:
- Dynamic instance loading from env vars and config.yaml
- Platform-agnostic command routing
- Instance switching for any adapter (Telegram, WhatsApp, Discord, etc.)
- reload_hermes_instances() live config updates
- Backward compatibility with existing InstanceOrchestrator API
"""

import pytest
import asyncio
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from gateway.instance_orchestrator import (
    InstanceOrchestrator,
    RemoteHermesInstance,
    HERMES_INSTANCES,
    load_hermes_instances,
    reload_hermes_instances,
    _load_instances_from_env,
    _load_instances_from_config_yaml,
    _parse_instance_url,
    validate_hostname,
    validate_port,
    get_instance_config,
    MAX_CHAT_ID_LENGTH,
)
import hashlib


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def clean_env():
    """Remove all HERMES_INSTANCE_* env vars before each test."""
    to_remove = [k for k in os.environ if k.startswith("HERMES_INSTANCE_")]
    saved = {}
    for k in to_remove:
        saved[k] = os.environ.pop(k)
    yield
    # Restore
    for k in to_remove:
        os.environ.pop(k, None)
    os.environ.update(saved)


@pytest.fixture
def orchestrator():
    """Create a fresh InstanceOrchestrator with default instances."""
    orch = InstanceOrchestrator()
    yield orch
    try:
        asyncio.get_event_loop().run_until_complete(orch.close())
    except Exception:
        pass


@pytest.fixture
async def async_orchestrator():
    """Create an async-initialized orchestrator."""
    orch = InstanceOrchestrator()
    await orch.init()
    yield orch
    await orch.close()


# ============================================================================
# Test: _parse_instance_url
# ============================================================================

def test_parse_instance_url_with_scheme_and_port():
    """Test parsing http://host:port."""
    result = _parse_instance_url("http://100.79.15.66:9000")
    assert result["hostname"] == "100.79.15.66"
    assert result["ip"] == "100.79.15.66"
    assert result["port"] == 9000


def test_parse_instance_url_no_scheme():
    """Test parsing host:port without scheme."""
    result = _parse_instance_url("myhost.example.com:8080")
    assert result["hostname"] == "myhost.example.com"
    assert result["port"] == 8080


def test_parse_instance_url_no_port():
    """Test parsing URL without port defaults to 8000."""
    result = _parse_instance_url("http://192.168.1.1")
    assert result["hostname"] == "192.168.1.1"
    assert result["port"] == 8000


def test_parse_instance_url_https():
    """Test parsing https:// URL."""
    result = _parse_instance_url("https://secure.example.com:443")
    assert result["hostname"] == "secure.example.com"
    assert result["port"] == 443


# ============================================================================
# Test: _load_instances_from_env
# ============================================================================

def test_load_instances_from_env_basic():
    """Test loading a single instance from env vars."""
    os.environ["HERMES_INSTANCE_LOCAL"] = "http://127.0.0.1:8000"
    os.environ["HERMES_INSTANCE_LOCAL_KEY"] = "test-key"
    os.environ["HERMES_INSTANCE_LOCAL_USERNAME"] = "testuser"

    result = _load_instances_from_env()
    assert "local" in result
    assert result["local"]["hostname"] == "127.0.0.1"
    assert result["local"]["http_key"] == "test-key"
    assert result["local"]["username"] == "testuser"


def test_load_instances_from_env_multiple():
    """Test loading multiple instances from env vars."""
    os.environ["HERMES_INSTANCE_LOCAL"] = "http://127.0.0.1:8000"
    os.environ["HERMES_INSTANCE_HERMES2"] = "http://100.79.15.66:8000"
    os.environ["HERMES_INSTANCE_HERMES2_KEY"] = "remote-key"
    os.environ["HERMES_INSTANCE_HERMES2_USERNAME"] = "ubuntu"

    result = _load_instances_from_env()
    assert "local" in result
    assert "hermes2" in result
    assert result["hermes2"]["http_key"] == "remote-key"


def test_load_instances_from_env_description():
    """Test loading instance description from env var."""
    os.environ["HERMES_INSTANCE_WORKER1"] = "http://10.0.0.5:8000"
    os.environ["HERMES_INSTANCE_WORKER1_DESCRIPTION"] = "GPU worker node"

    result = _load_instances_from_env()
    assert "worker1" in result
    assert result["worker1"]["description"] == "GPU worker node"


def test_load_instances_from_env_no_vars():
    """Test that empty env returns empty dict."""
    result = _load_instances_from_env()
    assert isinstance(result, dict)
    assert len(result) == 0


def test_load_instances_from_env_key_without_url():
    """Test that a _KEY var without a corresponding URL still creates entry."""
    os.environ["HERMES_INSTANCE_LONE_KEY"] = "some-key"
    result = _load_instances_from_env()
    assert "lone" in result
    assert result["lone"]["http_key"] == "some-key"


# ============================================================================
# Test: load_hermes_instances (integration)
# ============================================================================

def test_load_hermes_instances_default_local():
    """Test that 'local' instance always exists as default."""
    instances = load_hermes_instances()
    assert "local" in instances
    assert instances["local"].is_local is True
    assert instances["local"].hostname == "127.0.0.1"


def test_load_hermes_instances_from_env():
    """Test that env vars create instances and override defaults."""
    os.environ["HERMES_INSTANCE_REMOTE1"] = "http://10.0.0.1:8000"
    os.environ["HERMES_INSTANCE_REMOTE1_KEY"] = "env-key-123"
    os.environ["HERMES_INSTANCE_REMOTE1_USERNAME"] = "deploy"

    instances = load_hermes_instances()
    assert "remote1" in instances
    assert instances["remote1"].http_key == "env-key-123"
    assert instances["remote1"].username == "deploy"
    assert instances["remote1"].http_port == 8000


def test_load_hermes_instances_env_priority_over_yaml():
    """Test that env vars take priority over config.yaml."""
    # Write a temp config.yaml with hermes2
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("""
hermes_instances:
  - name: hermes2
    hostname: yaml-host.example.com
    ip: yaml-host.example.com
    http_port: 9000
    http_key: yaml-key
    username: yaml-user
    description: From YAML
""")
        yaml_path = f.name

    # Set env var for hermes2 (should override)
    os.environ["HERMES_INSTANCE_HERMES2"] = "http://env-host.example.com:7000"
    os.environ["HERMES_INSTANCE_HERMES2_KEY"] = "env-key"
    os.environ["HERMES_INSTANCE_HERMES2_USERNAME"] = "env-user"

    with patch('gateway.instance_orchestrator._load_instances_from_config_yaml') as mock_yaml:
        mock_yaml.return_value = {
            "hermes2": {
                "hostname": "yaml-host.example.com",
                "ip": "yaml-host.example.com",
                "port": 9000,
                "http_key": "yaml-key",
                "username": "yaml-user",
                "description": "From YAML",
                "is_local": False,
            }
        }
        instances = load_hermes_instances()
        assert "hermes2" in instances
        # Env var should win
        assert instances["hermes2"].http_key == "env-key"
        assert instances["hermes2"].username == "env-user"

    os.unlink(yaml_path)


def test_load_hermes_instances_no_putty_key_here():
    """Test that no instance has a placeholder 'putty_key_here' API key."""
    instances = load_hermes_instances()
    for name, inst in instances.items():
        assert inst.http_key != "putty_key_here", (
            f"Instance '{name}' still has placeholder 'putty_key_here' API key"
        )


def test_load_hermes_instances_no_whatsapp_in_descriptions():
    """Test that no instance description contains 'WhatsApp'."""
    instances = load_hermes_instances()
    for name, inst in instances.items():
        assert "whatsapp" not in inst.description.lower(), (
            f"Instance '{name}' description still mentions WhatsApp: {inst.description}"
        )


def test_load_hermes_instances_invalid_port_skipped():
    """Test that env vars with invalid port are skipped."""
    os.environ["HERMES_INSTANCE_BAD"] = "http://10.0.0.1:99999"
    instances = load_hermes_instances()
    assert "bad" not in instances


# ============================================================================
# Test: reload_hermes_instances
# ============================================================================

def test_reload_hermes_instances():
    """Test that reload_hermes_instances updates the module-level dict."""
    import gateway.instance_orchestrator as mod

    os.environ["HERMES_INSTANCE_RELOADTEST"] = "http://1.2.3.4:8000"
    result = reload_hermes_instances()
    assert "reloadtest" in result
    assert "reloadtest" in mod.HERMES_INSTANCES


# ============================================================================
# Test: InstanceOrchestrator with dynamic instances
# ============================================================================

def test_orchestrator_uses_default_instances():
    """Test that InstanceOrchestrator picks up module-level instances."""
    orch = InstanceOrchestrator()
    assert orch.get_instance("local") is not None


def test_orchestrator_custom_instances():
    """Test InstanceOrchestrator with custom instance dict."""
    custom = {
        "custom_local": RemoteHermesInstance(
            name="custom_local",
            hostname="10.0.0.1",
            ip="10.0.0.1",
            http_port=8000,
            description="Custom local instance",
            is_local=False,
        )
    }
    orch = InstanceOrchestrator(instances=custom)
    assert orch.get_instance("custom_local") is not None
    assert orch.get_instance("local") is None  # Not in custom dict


def test_orchestrator_reload_instances():
    """Test that reload_instances() refreshes the registry."""
    orch = InstanceOrchestrator()
    # Initially has whatever is in HERMES_INSTANCES
    initial_count = len(orch._get_registry())

    # Add an env var and reload
    os.environ["HERMES_INSTANCE_NEWGUY"] = "http://9.8.7.6:8000"
    orch.reload_instances()
    assert "newguy" in orch._get_registry()


# ============================================================================
# Test: Platform-agnostic behavior
# ============================================================================

def test_set_current_instance_telegram_chat_id():
    """Test instance switching with a Telegram-style chat_id."""
    orch = InstanceOrchestrator()
    chat_id = "telegram_123456789"
    result = orch.set_current_instance("local", chat_id=chat_id)
    assert result is True
    assert orch.get_current_instance(chat_id=chat_id) == "local"


def test_set_current_instance_discord_chat_id():
    """Test instance switching with a Discord-style chat_id."""
    orch = InstanceOrchestrator()
    chat_id = "discord_channel_987654321"
    result = orch.set_current_instance("local", chat_id=chat_id)
    assert result is True
    assert orch.get_current_instance(chat_id=chat_id) == "local"


def test_set_current_instance_slack_chat_id():
    """Test instance switching with a Slack-style chat_id."""
    orch = InstanceOrchestrator()
    chat_id = "C0123456789"
    result = orch.set_current_instance("local", chat_id=chat_id)
    assert result is True
    assert orch.get_current_instance(chat_id=chat_id) == "local"


def test_set_current_instance_signal_chat_id():
    """Test instance switching with a Signal-style UUID chat_id."""
    orch = InstanceOrchestrator()
    chat_id = "signal_uuid_abc-123-def"
    result = orch.set_current_instance("local", chat_id=chat_id)
    assert result is True
    assert orch.get_current_instance(chat_id=chat_id) == "local"


# ============================================================================
# Test: Backward compatibility — existing API still works
# ============================================================================

def test_set_current_instance_valid():
    """Test switching to a valid instance (backward compat)."""
    orch = InstanceOrchestrator()
    result = orch.set_current_instance("local")
    assert result is True
    assert orch.current_instance == "local"


def test_set_current_instance_invalid():
    """Test switching to an invalid instance (backward compat)."""
    orch = InstanceOrchestrator()
    result = orch.set_current_instance("nonexistent")
    assert result is False
    assert orch.current_instance == "local"


def test_get_current_instance_default():
    """Test getting the default instance (backward compat)."""
    orch = InstanceOrchestrator()
    instance = orch.get_current_instance()
    assert instance == "local"


def test_get_instance_valid():
    """Test getting a valid instance object (backward compat)."""
    orch = InstanceOrchestrator()
    instance = orch.get_instance("local")
    assert instance is not None
    assert isinstance(instance, RemoteHermesInstance)
    assert instance.name == "local"


def test_get_instance_invalid():
    """Test getting an invalid instance (backward compat)."""
    orch = InstanceOrchestrator()
    instance = orch.get_instance("nonexistent")
    assert instance is None


def test_list_instances_formatting():
    """Test that list_instances returns properly formatted string."""
    orch = InstanceOrchestrator()
    listing = orch.list_instances()
    assert isinstance(listing, str)
    assert "Available Hermes Instances" in listing


# ============================================================================
# Test: Per-chat instance tracking (adapter-agnostic)
# ============================================================================

def test_per_chat_instance_telegram_vs_discord():
    """Test that Telegram and Discord chat IDs don't interfere."""
    orch = InstanceOrchestrator()
    tg_chat = "tg_123"
    dc_chat = "dc_456"

    orch.set_current_instance("local", chat_id=tg_chat)
    orch.set_current_instance("local", chat_id=dc_chat)

    assert orch.get_current_instance(chat_id=tg_chat) == "local"
    assert orch.get_current_instance(chat_id=dc_chat) == "local"


def test_per_chat_instance_isolation():
    """Test that per-chat instances don't interfere (backward compat)."""
    orch = InstanceOrchestrator()
    chat1 = "chat_1"
    chat2 = "chat_2"

    orch.set_current_instance("local", chat_id=chat1)
    orch.set_current_instance("local", chat_id=chat2)

    assert orch.get_current_instance(chat_id=chat1) == "local"
    assert orch.get_current_instance(chat_id=chat2) == "local"


def test_get_current_instance_chat_id_not_found():
    """Test getting instance for chat_id that doesn't exist."""
    orch = InstanceOrchestrator()
    instance = orch.get_current_instance(chat_id="nonexistent")
    assert instance == "local"


# ============================================================================
# Test: Chat ID validation (DoS prevention)
# ============================================================================

def test_set_current_instance_chat_id_too_long():
    """Test that oversized chat IDs are rejected."""
    orch = InstanceOrchestrator()
    long_chat_id = "x" * 300

    with pytest.raises(ValueError, match="exceeds maximum"):
        orch.set_current_instance("local", chat_id=long_chat_id)


def test_set_current_instance_chat_id_max_length():
    """Test chat ID at max length is accepted."""
    orch = InstanceOrchestrator()
    max_chat_id = "x" * 256
    result = orch.set_current_instance("local", chat_id=max_chat_id)
    assert result is True


def test_set_current_instance_chat_id_not_string():
    """Test that non-string chat IDs are rejected."""
    orch = InstanceOrchestrator()

    with pytest.raises(ValueError, match="must be a string"):
        orch.set_current_instance("local", chat_id=123)


# ============================================================================
# Test: RemoteHermesInstance
# ============================================================================

def test_remote_instance_get_base_url():
    """Test RemoteHermesInstance.get_base_url() for remote."""
    inst = RemoteHermesInstance(
        name="test",
        hostname="test.example.com",
        ip="1.2.3.4",
        http_port=8000,
        is_local=False
    )
    assert inst.get_base_url() == "http://1.2.3.4:8000"


def test_remote_instance_get_base_url_local():
    """Test RemoteHermesInstance.get_base_url() for local."""
    inst = RemoteHermesInstance(
        name="local",
        hostname="127.0.0.1",
        ip="127.0.0.1",
        http_port=8000,
        is_local=True
    )
    assert inst.get_base_url() == "http://127.0.0.1:8000"


def test_remote_instance_get_api_headers():
    """Test RemoteHermesInstance.get_api_headers()."""
    inst = RemoteHermesInstance(
        name="test",
        hostname="test.example.com",
        ip="1.2.3.4",
        http_key="test_key",
        username="testuser"
    )
    headers = inst.get_api_headers()
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Hermes-Key"] == "test_key"
    assert headers["X-Hermes-User"] == "testuser"


def test_remote_instance_get_api_headers_no_auth():
    """Test RemoteHermesInstance.get_api_headers() with no auth."""
    inst = RemoteHermesInstance(
        name="local",
        hostname="127.0.0.1",
        ip="127.0.0.1"
    )
    headers = inst.get_api_headers()
    assert headers["Content-Type"] == "application/json"
    assert "X-Hermes-Key" not in headers


def test_remote_instance_repr():
    """Test RemoteHermesInstance.__repr__()."""
    local_inst = RemoteHermesInstance(
        name="local", hostname="127.0.0.1", ip="127.0.0.1", is_local=True
    )
    assert "LOCAL" in repr(local_inst)

    remote_inst = RemoteHermesInstance(
        name="remote", hostname="1.2.3.4", ip="1.2.3.4", is_local=False
    )
    assert "REMOTE" in repr(remote_inst)


# ============================================================================
# Test: execute_on_instance with mocked HTTP client
# ============================================================================

@pytest.mark.asyncio
async def test_execute_on_instance_local():
    """Test executing on local instance returns None."""
    orch = InstanceOrchestrator()
    await orch.init()

    result = await orch.execute_on_instance("local", "test prompt")
    assert result is None


@pytest.mark.asyncio
async def test_execute_on_instance_invalid():
    """Test executing on invalid instance returns error."""
    orch = InstanceOrchestrator()
    await orch.init()

    result = await orch.execute_on_instance("invalid", "test prompt")
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_execute_on_instance_auth_failure():
    """Test handling of auth failure (401)."""
    orch = InstanceOrchestrator()

    # Add a remote instance for testing
    orch._instances["test_remote"] = RemoteHermesInstance(
        name="test_remote",
        hostname="test.example.com",
        ip="1.2.3.4",
        http_port=8000,
        http_key="test-key",
        is_local=False,
    )
    await orch.init()

    mock_response = AsyncMock()
    mock_response.status = 401

    with patch.object(orch._http_client, 'post', return_value=mock_response):
        result = await orch.execute_on_instance("test_remote", "test prompt")
        assert "Authentication failed" in result


# ============================================================================
# Test: Health check
# ============================================================================

@pytest.mark.asyncio
async def test_health_check_local_always_healthy():
    """Test that local instance is always healthy."""
    orch = InstanceOrchestrator()
    result = await orch.health_check("local")
    assert result is True


@pytest.mark.asyncio
async def test_health_check_caching():
    """Test that health check results are cached."""
    orch = InstanceOrchestrator()
    orch._instances["cache_remote"] = RemoteHermesInstance(
        name="cache_remote",
        hostname="test.example.com",
        ip="1.2.3.4",
        http_port=8000,
        is_local=False,
    )
    await orch.init()

    mock_response = AsyncMock()
    mock_response.status = 200

    call_count = 0
    async def counting_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_response

    with patch.object(orch._http_client, 'get', side_effect=counting_get):
        result1 = await orch.health_check("cache_remote")
        assert result1 is True
        assert call_count == 1

        result2 = await orch.health_check("cache_remote")
        assert result2 is True
        assert call_count == 1  # No additional call


# ============================================================================
# Test: Hostname and port validation
# ============================================================================

def test_validate_hostname_valid_localhost():
    assert validate_hostname("localhost") is True

def test_validate_hostname_valid_ipv4():
    assert validate_hostname("127.0.0.1") is True
    assert validate_hostname("192.168.1.1") is True

def test_validate_hostname_invalid_ipv4():
    assert validate_hostname("256.1.1.1") is False
    assert validate_hostname("192.168.1") is False

def test_validate_hostname_valid_fqdn():
    assert validate_hostname("example.com") is True
    assert validate_hostname("sub.example.com") is True

def test_validate_hostname_empty():
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_hostname("")

def test_validate_hostname_not_string():
    with pytest.raises(ValueError, match="must be a string"):
        validate_hostname(123)

def test_validate_port_valid():
    assert validate_port(1) is True
    assert validate_port(8000) is True
    assert validate_port(65535) is True

def test_validate_port_invalid():
    assert validate_port(0) is False
    assert validate_port(65536) is False
    assert validate_port(-1) is False

def test_validate_port_not_integer():
    with pytest.raises(ValueError, match="must be an integer"):
        validate_port("8000")


# ============================================================================
# Test: get_instance_config
# ============================================================================

def test_get_instance_config_defaults():
    """Test default configuration values."""
    old_env = {}
    for key in ["HERMES_REMOTE_API_KEY", "HERMES_INSTANCE_A_HOSTNAME", "HERMES_INSTANCE_A_PORT"]:
        old_env[key] = os.environ.pop(key, None)

    try:
        config = get_instance_config()
        assert config["instance_a_hostname"] == "localhost"
        assert config["instance_a_port"] == 8000
    finally:
        for key, value in old_env.items():
            if value is not None:
                os.environ[key] = value


# ============================================================================
# Test: No WhatsApp-specific content
# ============================================================================

def test_module_docstring_not_whatsapp_specific():
    """Test that the module docstring doesn't mention WhatsApp as the only platform."""
    import gateway.instance_orchestrator as mod
    doc = mod.__doc__
    if doc:
        # The docstring should NOT say "for WhatsApp gateway"
        assert "for WhatsApp gateway" not in doc
        # Should mention multiple adapters or be generic
        assert "any messaging adapter" in doc or "any adapter" in doc


def test_local_instance_description_not_whatsapp():
    """Test that the local instance doesn't say 'WhatsApp gateway' in its description."""
    instances = load_hermes_instances()
    local = instances.get("local")
    assert local is not None
    assert "whatsapp" not in local.description.lower()


def test_no_hermes2_hardcoded_critical():
    """Test that get_instance_status doesn't hardcode 'hermes2' as special."""
    import inspect
    from gateway.instance_orchestrator import InstanceOrchestrator
    source = inspect.getsource(InstanceOrchestrator.get_instance_status)
    # Should NOT have hardcoded 'if instance_name == "hermes2"' pattern
    assert 'instance_name == "hermes2"' not in source
    # Should have generic remote instance handling
    assert "is unreachable" in source


# ============================================================================
# Test: Thread safety
# ============================================================================

import threading

def test_thread_safety_concurrent_switches():
    """Test concurrent instance switches are thread-safe."""
    orch = InstanceOrchestrator()
    results = []
    errors = []

    def switch_instance(instance_name):
        try:
            result = orch.set_current_instance(instance_name)
            results.append((instance_name, result))
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(50):
        instance = "local" if i % 2 == 0 else "nonexistent"
        t = threading.Thread(target=switch_instance, args=(instance,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert len(errors) == 0, f"Errors occurred: {errors}"
    assert len(results) == 50


# ============================================================================
# Test: Init and close
# ============================================================================

@pytest.mark.asyncio
async def test_init_creates_http_client():
    """Test that init() creates HTTP client."""
    orch = InstanceOrchestrator()
    assert orch._http_client is None
    await orch.init()
    assert orch._http_client is not None
    await orch.close()


@pytest.mark.asyncio
async def test_close_cleanup():
    """Test that close() properly closes the HTTP client."""
    orch = InstanceOrchestrator()
    await orch.init()
    assert orch._http_client is not None

    close_called = False
    original_aclose = orch._http_client.aclose

    async def mock_aclose():
        nonlocal close_called
        close_called = True

    orch._http_client.aclose = mock_aclose
    await orch.close()

    assert close_called


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
