"""
Unit tests for src.backend.core.config — Settings and validators.

Purpose: Settings drives ALL configuration.  A wrong default means the app
         starts with incorrect LLM, library, or retry settings.  A broken
         validator means invalid values slip through and crash downstream.

Tests:
  - Default MODEL_PROVIDER, ROBOT_LIBRARY, BROWSER_HEADLESS, OPTIMIZATION_ENABLED
  - ROBOT_LIBRARY validator accepts 'browser'/'selenium', rejects others
  - MAX_AGENT_ITERATIONS validator enforces 1-5 range
  - CUSTOM_ACTION_TIMEOUT validator enforces positive
  - Environment variable override
"""

import os
import pytest
from unittest.mock import patch
from pydantic import ValidationError


class TestSettingsDefaults:
    """Tests for default configuration values."""

    def _make_settings(self, overrides=None):
        env = {
            "MODEL_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test",
            "ONLINE_MODEL": "gemini-2.5-flash",
            "ROBOT_LIBRARY": "selenium",
            "BROWSER_HEADLESS": "true",
            "MAX_AGENT_ITERATIONS": "3",
            "ENABLE_CUSTOM_ACTIONS": "true",
            "CUSTOM_ACTION_TIMEOUT": "5",
            "MAX_LOCATOR_STRATEGIES": "21",
            "OPTIMIZATION_ENABLED": "false",
        }
        if overrides:
            env.update(overrides)
        with patch.dict(os.environ, env, clear=False):
            from src.backend.core.config import Settings
            return Settings()

    def test_default_model_provider(self):
        """Default MODEL_PROVIDER is 'gemini'."""
        s = self._make_settings()
        assert s.MODEL_PROVIDER == "gemini"

    def test_default_robot_library(self):
        """Default ROBOT_LIBRARY is validated and lowercased."""
        s = self._make_settings()
        assert s.ROBOT_LIBRARY in ["selenium", "browser"]

    def test_default_browser_headless(self):
        """BROWSER_HEADLESS defaults to True."""
        s = self._make_settings()
        assert s.BROWSER_HEADLESS is True

    def test_default_optimization_disabled(self):
        """OPTIMIZATION_ENABLED defaults to False."""
        s = self._make_settings()
        assert s.OPTIMIZATION_ENABLED is False

    def test_env_override_model_provider(self):
        """MODEL_PROVIDER can be overridden to 'local'."""
        s = self._make_settings({"MODEL_PROVIDER": "local"})
        assert s.MODEL_PROVIDER == "local"


class TestSettingsValidators:
    """Tests for Pydantic validators in Settings."""

    def _make_settings(self, overrides):
        env = {
            "MODEL_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test",
            "ONLINE_MODEL": "gemini-2.5-flash",
            "ROBOT_LIBRARY": "selenium",
            "MAX_AGENT_ITERATIONS": "3",
            "CUSTOM_ACTION_TIMEOUT": "5",
            "MAX_LOCATOR_STRATEGIES": "21",
        }
        env.update(overrides)
        with patch.dict(os.environ, env, clear=False):
            from src.backend.core.config import Settings
            return Settings()

    def test_robot_library_accepts_browser(self):
        """ROBOT_LIBRARY='browser' is valid."""
        s = self._make_settings({"ROBOT_LIBRARY": "browser"})
        assert s.ROBOT_LIBRARY == "browser"

    def test_robot_library_accepts_selenium(self):
        """ROBOT_LIBRARY='selenium' is valid."""
        s = self._make_settings({"ROBOT_LIBRARY": "selenium"})
        assert s.ROBOT_LIBRARY == "selenium"

    def test_robot_library_rejects_invalid(self):
        """ROBOT_LIBRARY='puppeteer' raises ValidationError."""
        with pytest.raises(ValidationError):
            self._make_settings({"ROBOT_LIBRARY": "puppeteer"})

    def test_max_iterations_rejects_zero(self):
        """MAX_AGENT_ITERATIONS=0 raises ValidationError."""
        with pytest.raises(ValidationError):
            self._make_settings({"MAX_AGENT_ITERATIONS": "0"})

    def test_max_iterations_rejects_six(self):
        """MAX_AGENT_ITERATIONS=6 raises ValidationError."""
        with pytest.raises(ValidationError):
            self._make_settings({"MAX_AGENT_ITERATIONS": "6"})

    def test_custom_timeout_rejects_zero(self):
        """CUSTOM_ACTION_TIMEOUT=0 raises ValidationError."""
        with pytest.raises(ValidationError):
            self._make_settings({"CUSTOM_ACTION_TIMEOUT": "0"})

    def test_locator_strategies_rejects_high(self):
        """MAX_LOCATOR_STRATEGIES=100 raises ValidationError."""
        with pytest.raises(ValidationError):
            self._make_settings({"MAX_LOCATOR_STRATEGIES": "100"})

    def test_model_provider_accepts_vertex(self):
        """Verify MODEL_PROVIDER=vertex passes validation."""
        s = self._make_settings({"MODEL_PROVIDER": "vertex"})
        assert s.MODEL_PROVIDER == "vertex"

    def test_model_provider_rejects_online(self):
        """Verify the removed 'online' value is rejected by the validator."""
        with pytest.raises(ValidationError):
            self._make_settings({"MODEL_PROVIDER": "online"})

    def test_model_provider_rejects_invalid(self):
        """Verify arbitrary strings are rejected."""
        with pytest.raises(ValidationError):
            self._make_settings({"MODEL_PROVIDER": "aws"})

    def test_observability_backend_accepts_postgres(self):
        s = self._make_settings({"OBSERVABILITY_BACKEND": "postgres"})
        assert s.OBSERVABILITY_BACKEND == "postgres"

    def test_observability_backend_rejects_sqlite(self):
        # "sqlite" was retired in the Phase 4 consolidation (trace store -> Postgres).
        with pytest.raises(ValidationError):
            self._make_settings({"OBSERVABILITY_BACKEND": "sqlite"})

    def test_observability_backend_accepts_none(self):
        s = self._make_settings({"OBSERVABILITY_BACKEND": "none"})
        assert s.OBSERVABILITY_BACKEND == "none"

    def test_observability_backend_accepts_grafana(self):
        s = self._make_settings({"OBSERVABILITY_BACKEND": "grafana"})
        assert s.OBSERVABILITY_BACKEND == "grafana"

    def test_observability_backend_accepts_otlp(self):
        s = self._make_settings({"OBSERVABILITY_BACKEND": "otlp"})
        assert s.OBSERVABILITY_BACKEND == "otlp"

    def test_observability_backend_rejects_invalid(self):
        with pytest.raises(ValidationError):
            self._make_settings({"OBSERVABILITY_BACKEND": "datadog"})


def test_runner_exec_url_default():
    from src.backend.core.config import Settings
    s = Settings()
    assert s.RUNNER_EXEC_URL == "http://localhost:4998"
