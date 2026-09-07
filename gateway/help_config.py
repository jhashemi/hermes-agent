"""Help configuration loader for dynamic help system.

Loads help content from YAML config with validation.
Supports runtime reloading and fallback to hardcoded defaults.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional, Any
import yaml


class HelpConfigError(Exception):
    """Error loading or validating help configuration."""
    pass


class HelpConfigLoader:
    """Load and validate help configuration from YAML."""

    # Expected YAML structure for validation.
    #
    # ``commands`` is intentionally NOT required here — per-command
    # descriptions are derived from ``hermes_cli.commands.COMMAND_REGISTRY``
    # at runtime (see ``gateway/help_menu.py``).  This yaml only carries
    # authored copy: topic titles, prose descriptions, examples, and the
    # welcome-card quick reference.
    REQUIRED_SECTIONS = {"agents", "instances", "general"}
    REQUIRED_TOPIC_KEYS = {"title", "description", "example"}
    REQUIRED_TOP_LEVEL_KEYS = {"categories", "quick_reference"}

    def __init__(self, config_path: Optional[str] = None):
        """Initialize loader with optional custom config path.
        
        Args:
            config_path: Path to help.yaml. If None, uses default in gateway dir.
        """
        if config_path is None:
            # Default: help.yaml in same directory as this module
            gateway_dir = Path(__file__).parent
            config_path = gateway_dir / "help.yaml"
        else:
            config_path = Path(config_path)

        self.config_path = config_path
        self._config: Optional[Dict[str, Any]] = None
        self._loaded = False
        self._last_error: Optional[str] = None

    def load(self) -> Dict[str, Any]:
        """Load and validate help configuration.
        
        Returns:
            Validated configuration dictionary.
            
        Raises:
            HelpConfigError: If loading or validation fails.
        """
        if not self.config_path.exists():
            raise HelpConfigError(
                f"Help config not found: {self.config_path}"
            )

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise HelpConfigError(f"Invalid YAML in help config: {e}")
        except Exception as e:
            raise HelpConfigError(f"Error reading help config: {e}")

        # Validate structure
        self._validate_config(config)
        
        self._config = config
        self._loaded = True
        return config

    def reload(self) -> Dict[str, Any]:
        """Reload configuration from disk.
        
        Returns:
            Reloaded configuration dictionary.
        """
        self._config = None
        self._loaded = False
        return self.load()

    def get_config(self) -> Dict[str, Any]:
        """Get loaded config, loading if necessary.
        
        Returns:
            Configuration dictionary.
            
        Raises:
            HelpConfigError: If loading fails.
        """
        if not self._loaded:
            self.load()
        return self._config

    def _validate_config(self, config: Any) -> None:
        """Validate configuration structure.
        
        Args:
            config: Configuration to validate.
            
        Raises:
            HelpConfigError: If validation fails.
        """
        if not isinstance(config, dict):
            raise HelpConfigError("Help config must be a YAML dictionary")

        # Check required top-level keys (help topics)
        missing_sections = self.REQUIRED_SECTIONS - set(config.keys())
        if missing_sections:
            raise HelpConfigError(
                f"Missing required help sections: {missing_sections}"
            )

        # Validate each topic
        for section_name in self.REQUIRED_SECTIONS:
            topic = config[section_name]
            self._validate_topic(topic, section_name)

        # Validate top-level metadata
        missing_keys = self.REQUIRED_TOP_LEVEL_KEYS - set(config.keys())
        if missing_keys:
            raise HelpConfigError(
                f"Missing required top-level keys: {missing_keys}"
            )

        # Validate categories list
        categories = config.get("categories", [])
        if not isinstance(categories, list):
            raise HelpConfigError("'categories' must be a list")
        if not all(cat in self.REQUIRED_SECTIONS for cat in categories):
            raise HelpConfigError(
                f"Invalid categories. Must be subset of {self.REQUIRED_SECTIONS}"
            )

        # Validate quick_reference
        quick_ref = config.get("quick_reference")
        if not isinstance(quick_ref, list):
            raise HelpConfigError("'quick_reference' must be a list")

    def _validate_topic(self, topic: Any, topic_name: str) -> None:
        """Validate individual help topic structure.
        
        Args:
            topic: Topic configuration to validate.
            topic_name: Name of the topic (for error messages).
            
        Raises:
            HelpConfigError: If validation fails.
        """
        if not isinstance(topic, dict):
            raise HelpConfigError(
                f"Help topic '{topic_name}' must be a dictionary"
            )

        missing_keys = self.REQUIRED_TOPIC_KEYS - set(topic.keys())
        if missing_keys:
            raise HelpConfigError(
                f"Topic '{topic_name}' missing keys: {missing_keys}"
            )

        # Validate each key type
        if not isinstance(topic.get("title"), str):
            raise HelpConfigError(
                f"Topic '{topic_name}': 'title' must be a string"
            )

        if not isinstance(topic.get("description"), str):
            raise HelpConfigError(
                f"Topic '{topic_name}': 'description' must be a string"
            )

        if not isinstance(topic.get("example"), str):
            raise HelpConfigError(
                f"Topic '{topic_name}': 'example' must be a string"
            )

        # Validate commands dictionary IF present (optional now — the
        # dynamic help system reads command descriptions from
        # COMMAND_REGISTRY, not from yaml.  Left here for backwards
        # compatibility with any external help.yaml files that still carry
        # the old ``commands`` block: we type-check but don't require it.)
        commands = topic.get("commands")
        if commands is None:
            return
        if not isinstance(commands, dict):
            raise HelpConfigError(
                f"Topic '{topic_name}': 'commands' must be a dictionary"
            )

        # Validate each command
        for cmd_name, cmd_desc in commands.items():
            if not isinstance(cmd_name, str):
                raise HelpConfigError(
                    f"Topic '{topic_name}': command name must be string"
                )
            if not isinstance(cmd_desc, str):
                raise HelpConfigError(
                    f"Topic '{topic_name}': command '{cmd_name}' description must be string"
                )

    def is_loaded(self) -> bool:
        """Check if configuration has been loaded."""
        return self._loaded

    def get_error(self) -> Optional[str]:
        """Get last error message if loading failed."""
        return self._last_error


# Global loader instance
_loader: Optional[HelpConfigLoader] = None


def get_help_config_loader() -> HelpConfigLoader:
    """Get or create global help config loader instance."""
    global _loader
    if _loader is None:
        _loader = HelpConfigLoader()
    return _loader


def load_help_config() -> Dict[str, Any]:
    """Load help configuration using global loader.
    
    Returns:
        Validated help configuration.
        
    Raises:
        HelpConfigError: If loading or validation fails.
    """
    loader = get_help_config_loader()
    return loader.load()


def get_help_config() -> Dict[str, Any]:
    """Get help configuration, loading if necessary.
    
    Returns:
        Help configuration dictionary.
        
    Raises:
        HelpConfigError: If loading fails.
    """
    loader = get_help_config_loader()
    return loader.get_config()
