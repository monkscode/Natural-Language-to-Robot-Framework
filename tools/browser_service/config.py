"""
Configuration management for Browser Service.

This module centralizes all configuration settings including:
- Batch processing configuration
- Locator extraction configuration
- LLM configuration
- Feature flags

All configuration values can be overridden via environment variables.
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class BatchConfig:
    """Batch processing configuration settings."""

    # Stop agent after N steps
    max_agent_steps: int = 15

    # Retry element N times before skipping
    max_retries_per_element: int = 2

    # Max time per element (seconds)
    element_timeout: int = 120


@dataclass
class LocatorConfig:
    """Locator extraction configuration settings."""

    # Content-based search (finds element by visible text)
    # Try content search N times
    content_based_retries: int = 7

    # Coordinate-based search (finds element by screen position)
    # Higher count for coordinate fallback
    coordinate_based_retries: int = 7

    # Element type fallback (finds first visible element of type)
    # Last resort
    element_type_retries: int = 5

    # Coordinate offset attempts (try nearby coordinates if first fails)
    # Try N different offsets
    coordinate_offset_attempts: int = 7

    # Coordinate offsets to try (pixels)
    coordinate_offsets: List[Dict[str, Any]] = field(default_factory=lambda: [
        {"x": 100, "y": 0, "reason": "escape sidebar/left panel"},
        {"x": 200, "y": 0, "reason": "escape wide sidebar"},
        {"x": 50, "y": 0, "reason": "slight right adjustment"},
        {"x": 0, "y": 20, "reason": "move down slightly"},
        {"x": 100, "y": 20, "reason": "diagonal adjustment"}
    ])


@dataclass
class LLMConfig:
    """LLM (Language Model) configuration settings."""

    # Google Gemini API key
    google_api_key: str = ""

    # Google model name (without "gemini/" prefix)
    google_model: str = "gemini-2.5-flash"


class BrowserServiceConfig:
    """
    Centralized configuration for Browser Service.

    Loads configuration from environment variables with sensible defaults.
    Provides validation to ensure configuration is valid before service starts.
    """

    def __init__(self):
        """Initialize configuration from environment variables."""

        # Batch processing configuration
        self.batch = BatchConfig(
            max_agent_steps=int(os.getenv("MAX_AGENT_STEPS", "15")),
            max_retries_per_element=int(os.getenv("MAX_RETRIES_PER_ELEMENT", "2")),
            element_timeout=int(os.getenv("ELEMENT_TIMEOUT", "120"))
        )

        # Locator extraction configuration
        self.locator = LocatorConfig(
            content_based_retries=int(os.getenv("CONTENT_BASED_RETRIES", "7")),
            coordinate_based_retries=int(os.getenv("COORDINATE_BASED_RETRIES", "7")),
            element_type_retries=int(os.getenv("ELEMENT_TYPE_RETRIES", "5")),
            coordinate_offset_attempts=int(os.getenv("COORDINATE_OFFSET_ATTEMPTS", "7"))
        )

        # LLM configuration
        self.llm = LLMConfig(
            google_api_key=os.getenv("GEMINI_API_KEY", ""),
            google_model=self._get_google_model()
        )

        # Robot Framework library type
        self.robot_library = os.getenv("ROBOT_LIBRARY", "browser")

        # Feature flags
        self.enable_custom_actions = os.getenv("ENABLE_CUSTOM_ACTIONS", "true").lower() == "true"

    def _get_google_model(self) -> str:
        """
        Get Google model name from environment or settings.
        Strips "gemini/" prefix if present (ChatGoogle doesn't need it).
        """
        # Try to import settings to get ONLINE_MODEL
        try:
            from src.backend.core.config import settings
            model = settings.ONLINE_MODEL if hasattr(settings, 'ONLINE_MODEL') else None
            if model:
                return model.replace("gemini/", "")
        except ImportError:
            pass

        # Fallback to environment variable or default
        model = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
        return model.replace("gemini/", "")

    def validate(self) -> List[str]:
        """
        Validate configuration and return list of errors.

        Returns:
            List of error messages. Empty list if configuration is valid.
        """
        errors = []

        # Validate LLM configuration
        if not self.llm.google_api_key:
            errors.append("GEMINI_API_KEY environment variable is not set")

        # Validate batch configuration
        if self.batch.max_agent_steps < 1:
            errors.append(f"MAX_AGENT_STEPS must be >= 1, got {self.batch.max_agent_steps}")

        if self.batch.max_retries_per_element < 0:
            errors.append(f"MAX_RETRIES_PER_ELEMENT must be >= 0, got {self.batch.max_retries_per_element}")

        if self.batch.element_timeout < 1:
            errors.append(f"ELEMENT_TIMEOUT must be >= 1, got {self.batch.element_timeout}")

        # Validate locator configuration
        if self.locator.content_based_retries < 0:
            errors.append(f"CONTENT_BASED_RETRIES must be >= 0, got {self.locator.content_based_retries}")

        if self.locator.coordinate_based_retries < 0:
            errors.append(f"COORDINATE_BASED_RETRIES must be >= 0, got {self.locator.coordinate_based_retries}")

        if self.locator.element_type_retries < 0:
            errors.append(f"ELEMENT_TYPE_RETRIES must be >= 0, got {self.locator.element_type_retries}")

        if self.locator.coordinate_offset_attempts < 0:
            errors.append(f"COORDINATE_OFFSET_ATTEMPTS must be >= 0, got {self.locator.coordinate_offset_attempts}")

        # Validate robot library type
        valid_libraries = ["browser", "selenium"]
        if self.robot_library not in valid_libraries:
            errors.append(
                f"ROBOT_LIBRARY must be one of {valid_libraries}, got '{self.robot_library}'"
            )

        return errors


# Global configuration instance
config = BrowserServiceConfig()
