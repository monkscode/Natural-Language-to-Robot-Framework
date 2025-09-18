"""Configuration loading and validation utilities for self-healing."""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
import logging

from .models.healing_models import HealingConfiguration, LocatorStrategy
from .config import settings

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""
    pass


class SelfHealingConfigLoader:
    """Loads and validates self-healing configuration."""

    DEFAULT_CONFIG = {
        "self_healing": {
            "enabled": True,
            "max_attempts_per_locator": 3,
            "chrome_session_timeout": 30,
            "healing_timeout": 300,
            "max_concurrent_sessions": 3,
            "backup_retention_days": 7,
            "failure_detection": {
                "enable_fingerprinting": True,
                "confidence_threshold": 0.7
            },
            "locator_generation": {
                "strategies": ["id", "name", "css", "xpath", "link_text"],
                "max_alternatives": 5
            },
            "validation": {
                "element_wait_timeout": 10,
                "interaction_test": True
            }
        }
    }

    def __init__(self, config_path: Optional[str] = None):
        """Initialize config loader with optional custom path."""
        self.config_path = Path(
            config_path or settings.SELF_HEALING_CONFIG_PATH)
        self._config_cache: Optional[HealingConfiguration] = None
        self._config_file_mtime: Optional[float] = None

    def load_config(self, force_reload: bool = False) -> HealingConfiguration:
        """Load and validate self-healing configuration.

        Args:
            force_reload: Force reload even if cached config exists

        Returns:
            HealingConfiguration: Validated configuration object

        Raises:
            ConfigurationError: If configuration is invalid
        """
        # Check if we need to reload
        if not force_reload and self._config_cache and self._is_config_current():
            return self._config_cache

        try:
            config_data = self._load_config_file()
            healing_config = self._parse_healing_config(config_data)
            self._validate_config(healing_config)

            # Cache the config and file modification time
            self._config_cache = healing_config
            if self.config_path.exists():
                self._config_file_mtime = self.config_path.stat().st_mtime

            logger.info(
                f"Loaded self-healing configuration from {self.config_path}")
            return healing_config

        except Exception as e:
            logger.error(f"Failed to load self-healing configuration: {e}")
            raise ConfigurationError(
                f"Configuration loading failed: {e}") from e

    def save_config(self, config: HealingConfiguration) -> None:
        """Save configuration to file.

        Args:
            config: Configuration to save

        Raises:
            ConfigurationError: If saving fails
        """
        try:
            self._validate_config(config)

            # Ensure directory exists
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            # Convert to dict and wrap in expected structure
            config_data = {
                "self_healing": self._config_to_dict(config)
            }

            # Write to file
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config_data, f, default_flow_style=False, indent=2)

            # Update cache
            self._config_cache = config
            self._config_file_mtime = self.config_path.stat().st_mtime

            logger.info(
                f"Saved self-healing configuration to {self.config_path}")

        except Exception as e:
            logger.error(f"Failed to save self-healing configuration: {e}")
            raise ConfigurationError(
                f"Configuration saving failed: {e}") from e

    def _load_config_file(self) -> Dict[str, Any]:
        """Load configuration from file or return defaults."""
        if not self.config_path.exists():
            logger.info(
                f"Config file {self.config_path} not found, using defaults")
            return self.DEFAULT_CONFIG.copy()

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f) or {}

            # Merge with defaults to ensure all keys exist
            merged_config = self._deep_merge(
                self.DEFAULT_CONFIG.copy(), config_data)
            return merged_config

        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML in config file: {e}")
        except Exception as e:
            raise ConfigurationError(f"Failed to read config file: {e}")

    def _parse_healing_config(self, config_data: Dict[str, Any]) -> HealingConfiguration:
        """Parse configuration data into HealingConfiguration object."""
        healing_section = config_data.get("self_healing", {})

        # Extract nested sections
        failure_detection = healing_section.get("failure_detection", {})
        locator_generation = healing_section.get("locator_generation", {})
        validation = healing_section.get("validation", {})

        # Parse strategies
        strategies_list = locator_generation.get(
            "strategies", ["id", "name", "css", "xpath", "link_text"])
        try:
            strategies = [LocatorStrategy(s) for s in strategies_list]
        except ValueError as e:
            raise ConfigurationError(f"Invalid locator strategy: {e}")

        return HealingConfiguration(
            enabled=healing_section.get("enabled", True),
            max_attempts_per_locator=healing_section.get(
                "max_attempts_per_locator", 3),
            chrome_session_timeout=healing_section.get(
                "chrome_session_timeout", 30),
            healing_timeout=healing_section.get("healing_timeout", 300),
            max_concurrent_sessions=healing_section.get(
                "max_concurrent_sessions", 3),
            backup_retention_days=healing_section.get(
                "backup_retention_days", 7),
            enable_fingerprinting=failure_detection.get(
                "enable_fingerprinting", True),
            confidence_threshold=failure_detection.get(
                "confidence_threshold", 0.7),
            strategies=strategies,
            max_alternatives=locator_generation.get("max_alternatives", 5),
            element_wait_timeout=validation.get("element_wait_timeout", 10),
            interaction_test=validation.get("interaction_test", True)
        )

    def _config_to_dict(self, config: HealingConfiguration) -> Dict[str, Any]:
        """Convert HealingConfiguration to nested dictionary structure."""
        return {
            "enabled": config.enabled,
            "max_attempts_per_locator": config.max_attempts_per_locator,
            "chrome_session_timeout": config.chrome_session_timeout,
            "healing_timeout": config.healing_timeout,
            "max_concurrent_sessions": config.max_concurrent_sessions,
            "backup_retention_days": config.backup_retention_days,
            "failure_detection": {
                "enable_fingerprinting": config.enable_fingerprinting,
                "confidence_threshold": config.confidence_threshold
            },
            "locator_generation": {
                "strategies": [s.value for s in config.strategies],
                "max_alternatives": config.max_alternatives
            },
            "validation": {
                "element_wait_timeout": config.element_wait_timeout,
                "interaction_test": config.interaction_test
            }
        }

    def _validate_config(self, config: HealingConfiguration) -> None:
        """Validate configuration values.

        Args:
            config: Configuration to validate

        Raises:
            ConfigurationError: If validation fails
        """
        errors = []

        # Validate numeric ranges
        if config.max_attempts_per_locator < 1 or config.max_attempts_per_locator > 10:
            errors.append("max_attempts_per_locator must be between 1 and 10")

        if config.chrome_session_timeout < 5 or config.chrome_session_timeout > 300:
            errors.append(
                "chrome_session_timeout must be between 5 and 300 seconds")

        if config.healing_timeout < 30 or config.healing_timeout > 1800:
            errors.append(
                "healing_timeout must be between 30 and 1800 seconds")

        if config.max_concurrent_sessions < 1 or config.max_concurrent_sessions > 10:
            errors.append("max_concurrent_sessions must be between 1 and 10")

        if config.backup_retention_days < 1 or config.backup_retention_days > 365:
            errors.append("backup_retention_days must be between 1 and 365")

        if config.confidence_threshold < 0.0 or config.confidence_threshold > 1.0:
            errors.append("confidence_threshold must be between 0.0 and 1.0")

        if config.max_alternatives < 1 or config.max_alternatives > 20:
            errors.append("max_alternatives must be between 1 and 20")

        if config.element_wait_timeout < 1 or config.element_wait_timeout > 60:
            errors.append(
                "element_wait_timeout must be between 1 and 60 seconds")

        # Validate strategies list
        if not config.strategies:
            errors.append("At least one locator strategy must be specified")

        if len(config.strategies) != len(set(config.strategies)):
            errors.append("Duplicate locator strategies are not allowed")

        if errors:
            raise ConfigurationError(
                "Configuration validation failed: " + "; ".join(errors))

    def _is_config_current(self) -> bool:
        """Check if cached config is still current."""
        if not self.config_path.exists():
            return self._config_file_mtime is None

        current_mtime = self.config_path.stat().st_mtime
        return self._config_file_mtime == current_mtime

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two dictionaries."""
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result


# Global config loader instance
config_loader = SelfHealingConfigLoader()


def get_healing_config(force_reload: bool = False) -> HealingConfiguration:
    """Get the current self-healing configuration.

    Args:
        force_reload: Force reload from file

    Returns:
        HealingConfiguration: Current configuration
    """
    # Check global enable/disable setting first
    if not settings.SELF_HEALING_ENABLED:
        config = config_loader.load_config(force_reload)
        config.enabled = False
        return config

    return config_loader.load_config(force_reload)


def save_healing_config(config: HealingConfiguration) -> None:
    """Save self-healing configuration.

    Args:
        config: Configuration to save
    """
    config_loader.save_config(config)


def create_default_config_file() -> None:
    """Create a default configuration file if it doesn't exist."""
    if not config_loader.config_path.exists():
        default_config = HealingConfiguration()
        config_loader.save_config(default_config)
        logger.info(
            f"Created default self-healing config at {config_loader.config_path}")
