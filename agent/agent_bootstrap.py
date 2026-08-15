"""agent_bootstrap.py — Bootstrap LLDAP connection for hermes-agent.

Entry point for wiring executive-agents-framework's LldapAdapter into
hermes-agent. Handles config resolution from multiple sources.

Config resolution order (highest to lowest priority):
  1. Runtime environment variables (LLDAP_*, EAF_LLDAP_*)
  2. ~/.hermes/.env file (LLDAP_* vars)
  3. lldap: section in ~/.hermes/config.yaml
  4. Compile-time defaults (127.0.0.1:3890, dc=eaf,dc=hermes2,dc=internal)

Usage (programmatic):
    from agent.agent_bootstrap import bootstrap_lldap
    adapter = bootstrap_lldap()
    if adapter:
        # use adapter for LDAP operations

Usage (CLI connection test):
    python -m agent.agent_bootstrap --test
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Hermes config dirs
_HERMES_DIR = Path.home() / ".hermes"
_ENV_FILE = _HERMES_DIR / ".env"
_CONFIG_YAML = _HERMES_DIR / "config.yaml"


# ---- Config resolution helpers -----------------------------------------

def _load_config_from_env_file() -> dict[str, str]:
    """Parse LLDAP_* variables from ~/.hermes/.env.

    Returns a dict of {key: value} for any LLDAP_* or EAF_LLDAP_* keys
    found in the .env file. Does not mutate os.environ.
    """
    result: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return result
    try:
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key.startswith("LLDAP_") or key.startswith("EAF_LLDAP_"):
                result[key] = value
    except Exception as exc:
        logger.warning("Could not read %s: %s", _ENV_FILE, exc)
    return result


def _load_config_from_yaml() -> dict[str, Any]:
    """Read the lldap: section from ~/.hermes/config.yaml.

    Returns a flat dict of the lldap block, or {} if not present.
    """
    if not _CONFIG_YAML.exists():
        return {}
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        logger.debug("PyYAML not installed; skipping config.yaml LLDAP config")
        return {}
    try:
        data = yaml.safe_load(_CONFIG_YAML.read_text()) or {}
        return data.get("lldap") or {}
    except Exception as exc:
        logger.warning("Could not parse %s: %s", _CONFIG_YAML, exc)
        return {}


def _resolve_config() -> "LldapConfig":  # noqa: F821
    """Merge LLDAP config from all sources into an LldapConfig.

    Precedence: runtime env > .env file > config.yaml > defaults.
    """
    from agent.framework_wrapper import LldapConfig  # noqa: PLC0415

    env_file = _load_config_from_env_file()
    yaml_cfg = _load_config_from_yaml()

    def get(env_key: str, yaml_key: str, default: Any = None) -> Any:
        """Look up a value with env > .env > yaml > default."""
        # 1. Runtime env
        val = os.environ.get(env_key)
        if val:
            return val
        # 2. .env file
        val = env_file.get(env_key)
        if val:
            return val
        # 3. config.yaml lldap section
        val = yaml_cfg.get(yaml_key)
        if val:
            return val
        return default

    host = get("LLDAP_HOST", "host", "127.0.0.1")
    port_str = get("LLDAP_PORT", "port", "3890")
    bind_dn = get("LLDAP_BIND_DN", "bind_dn") or get("EAF_LLDAP_BIND_DN", "bind_dn")
    admin_password = (
        get("LLDAP_ADMIN_PASSWORD", "admin_password")
        or get("EAF_LLDAP_ADMIN_PASSWORD", "admin_password")
    )
    base_dn = get("LLDAP_BASE_DN", "base_dn")

    try:
        port = int(port_str)
    except (TypeError, ValueError):
        port = 3890

    kwargs: dict[str, Any] = {"host": host, "port": port}
    if bind_dn:
        kwargs["bind_dn"] = bind_dn
    if admin_password:
        kwargs["admin_password"] = admin_password
    if base_dn:
        kwargs["base_dn"] = base_dn

    return LldapConfig(**kwargs)


# ---- Connection test helper --------------------------------------------

def test_lldap_connection(adapter: Any) -> bool:
    """Verify the LLDAP connection by performing a base-DN search.

    Args:
        adapter: LldapAdapter instance.

    Returns:
        True if the search succeeded, False otherwise.
    """
    try:
        # list_agents is a lightweight search on the base DN
        if hasattr(adapter, "list_agents"):
            result = adapter.list_agents()
            logger.info("Connection test: list_agents returned %d entries", len(result) if result else 0)
        elif hasattr(adapter, "_connection") and adapter._connection:
            adapter._connection.search(
                adapter.config.base_dn,
                "(objectClass=*)",
                attributes=["dn"],
                size_limit=1,
            )
            logger.info(
                "Connection test: base-DN search on %s succeeded",
                adapter.config.base_dn,
            )
        else:
            logger.warning("Connection test: adapter has no connection")
            return False
        return True
    except Exception as exc:
        logger.error("Connection test failed: %s", exc)
        return False


# ---- Public bootstrap entry point --------------------------------------

def bootstrap_lldap(
    config: Optional[Any] = None,
    skip_connection: bool = False,
) -> Optional[Any]:
    """Instantiate LldapAdapter with resolved hermes-agent configuration.

    Args:
        config: Optional LldapConfig override. If None, config is resolved
                from environment / .env / config.yaml automatically.
        skip_connection: If True, return None instead of raising when LLDAP
                         is not reachable (useful in CI / no-LDAP envs).

    Returns:
        LldapAdapter on success, None if skip_connection=True and LLDAP
        is unavailable.

    Raises:
        RuntimeError: If LLDAP connection fails and skip_connection=False.
        ImportError:  If executive-agents-framework is not installed.
    """
    from agent.framework_wrapper import get_lldap_adapter  # noqa: PLC0415

    if config is None:
        config = _resolve_config()

    logger.info("Bootstrapping LLDAP adapter (host=%s port=%d)", config.host, config.port)

    try:
        adapter = get_lldap_adapter(config)
        logger.info("LldapAdapter instantiated successfully")
        return adapter
    except Exception as exc:
        if skip_connection:
            logger.warning(
                "LLDAP bootstrap failed (skip_connection=True, returning None): %s", exc
            )
            return None
        raise


# ---- CLI entry point ---------------------------------------------------

def _run_cli() -> int:
    """Run connection test from CLI: python -m agent.agent_bootstrap --test"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import argparse
    parser = argparse.ArgumentParser(description="hermes-agent LLDAP bootstrap")
    parser.add_argument("--test", action="store_true", help="Run LLDAP connection test")
    args = parser.parse_args()

    if args.test:
        try:
            adapter = bootstrap_lldap()
            ok = test_lldap_connection(adapter)
            if ok:
                print("LLDAP connection test: PASSED")
                return 0
            else:
                print("LLDAP connection test: FAILED (search returned no result)")
                return 1
        except Exception as exc:
            print(f"LLDAP connection test: FAILED ({exc})")
            return 2
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
