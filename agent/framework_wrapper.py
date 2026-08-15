"""framework_wrapper.py — Thin bridge from hermes-agent to executive-agents-framework.

Provides hermes-agent-facing config and factory functions for LLDAP integration.
Lazy-imports framework classes so this module can be imported even when the
framework package is not installed (it fails at call-time instead of import time).

Canonical framework path:
  executive_agents.infrastructure.adapters.directory.lldap_adapter
  - LldapConfig
  - LldapAdapter
  executive_agents.ports.directory.ldap_directory_port
  - LdapDirectoryPort

Config resolution order (12-factor precedence):
  1. Explicit kwargs to LldapConfig()
  2. LLDAP_* / EAF_LLDAP_* environment variables
  3. Values from ~/.hermes/.env
  4. Values from lldap: section in ~/.hermes/config.yaml
  5. Compile-time defaults
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---- Sentinel for "argument not supplied" --------------------------------
_UNSET = object()


# ---- Lazy import bookkeeping -------------------------------------------
_LldapAdapter_cls: Any = None
_LldapConfig_cls: Any = None
_LdapDirectoryPort_cls: Any = None


def _ensure_imports() -> None:
    """Load framework classes on first use (lazy import)."""
    global _LldapAdapter_cls, _LldapConfig_cls, _LdapDirectoryPort_cls
    if _LldapAdapter_cls is not None:
        return
    try:
        from executive_agents.infrastructure.adapters.directory.lldap_adapter import (  # noqa: PLC0415
            LldapAdapter as _A,
            LldapConfig as _C,
        )
        from executive_agents.ports.directory.ldap_directory_port import (  # noqa: PLC0415
            LdapDirectoryPort as _P,
        )
        _LldapAdapter_cls = _A
        _LldapConfig_cls = _C
        _LdapDirectoryPort_cls = _P
    except ImportError as exc:
        raise ImportError(
            "executive-agents-framework is not installed or not on sys.path. "
            "Install it with: pip install -e /home/ubuntu/executive_agents_framework"
        ) from exc


# ---- Hermes-facing config class ----------------------------------------

class LldapConfig:
    """Hermes-facing LLDAP configuration with 12-factor precedence.

    Resolution order for each field:
      1. Explicit kwarg (not _UNSET)
      2. Environment variable (LLDAP_HOST, LLDAP_PORT, etc.)
      3. Default value

    This class does NOT extend the framework's LldapConfig — it is
    a plain data holder that get_lldap_adapter() converts into the
    framework's LldapConfig before instantiating LldapAdapter.
    """

    def __init__(
        self,
        host: Any = _UNSET,
        port: Any = _UNSET,
        bind_dn: Any = _UNSET,
        admin_password: Any = _UNSET,
        base_dn: Any = _UNSET,
    ) -> None:
        self.host: str = (
            host if host is not _UNSET
            else os.environ.get("LLDAP_HOST", "127.0.0.1")
        )
        self.port: int = int(
            port if port is not _UNSET
            else os.environ.get("LLDAP_PORT", 3890)
        )
        self.bind_dn: Optional[str] = (
            bind_dn if bind_dn is not _UNSET
            else os.environ.get("LLDAP_BIND_DN") or os.environ.get("EAF_LLDAP_BIND_DN")
        )
        self.admin_password: Optional[str] = (
            admin_password if admin_password is not _UNSET
            else (
                os.environ.get("LLDAP_ADMIN_PASSWORD")
                or os.environ.get("EAF_LLDAP_ADMIN_PASSWORD")
            )
        )
        self.base_dn: Optional[str] = (
            base_dn if base_dn is not _UNSET
            else os.environ.get("LLDAP_BASE_DN")
        )

    def __repr__(self) -> str:
        return (
            f"LldapConfig(host={self.host!r}, port={self.port}, "
            f"bind_dn={self.bind_dn!r}, base_dn={self.base_dn!r})"
        )


# ---- Public factory functions ------------------------------------------

def get_lldap_adapter(config: Optional[LldapConfig] = None) -> Any:
    """Create and return a framework LldapAdapter instance.

    Args:
        config: Optional LldapConfig. Defaults to LldapConfig() which
                reads LLDAP_* env vars.

    Returns:
        LldapAdapter instance (from executive_agents framework).

    Raises:
        ImportError: If executive-agents-framework is not installed.
        RuntimeError: If the LDAP connection fails.
    """
    _ensure_imports()
    if config is None:
        config = LldapConfig()

    # Build the framework's LldapConfig from our hermes-facing config.
    # Pass explicit values only where we have them so framework's own
    # env-var resolution handles the rest.
    kwargs: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
    }
    if config.bind_dn is not None:
        kwargs["bind_dn"] = config.bind_dn
    if config.admin_password is not None:
        kwargs["admin_password"] = config.admin_password
    if config.base_dn is not None:
        kwargs["base_dn"] = config.base_dn

    framework_config = _LldapConfig_cls(**kwargs)
    logger.info(
        "Instantiating LldapAdapter via framework_wrapper "
        "(host=%s port=%d)", config.host, config.port
    )
    return _LldapAdapter_cls(config=framework_config)


def get_directory_port_type() -> Any:
    """Return the LdapDirectoryPort abstract class (lazy access).

    Useful for isinstance() checks and type annotations without a
    hard import of the framework at module load time.
    """
    _ensure_imports()
    return _LdapDirectoryPort_cls


__all__ = [
    "LldapConfig",
    "get_directory_port_type",
    "get_lldap_adapter",
]
