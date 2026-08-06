"""
Unit tests for src.backend.core.config — Settings and validators.

Purpose: Settings drives ALL configuration.  A wrong default means the app
         starts with incorrect LLM, library, or retry settings.  A broken
         validator means invalid values slip through and crash downstream.

Tests:
  - Default MODEL_PROVIDER, ROBOT_LIBRARY, BROWSER_HEADLESS, OPTIMIZATION_ENABLED
  - ROBOT_LIBRARY validator accepts only 'browser'; 'selenium' rejected with a
    migration message (browser-only, fail fast — Task 11/E8)
  - MAX_AGENT_ITERATIONS validator enforces 1-5 range
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
            "ROBOT_LIBRARY": "browser",
            "BROWSER_HEADLESS": "true",
            "MAX_AGENT_ITERATIONS": "3",
            "ENABLE_CUSTOM_ACTIONS": "true",
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
        """Field default is 'browser' — a deployment with no env var gets the
        only library the locator engine actually emits syntax for."""
        from src.backend.core.config import Settings
        assert Settings.__fields__["ROBOT_LIBRARY"].default == "browser"
        s = self._make_settings()
        assert s.ROBOT_LIBRARY == "browser"

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
            "ROBOT_LIBRARY": "browser",
            "MAX_AGENT_ITERATIONS": "3",
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

    def test_robot_library_rejects_selenium_with_migration_message(self):
        """ROBOT_LIBRARY='selenium' fails startup with a clear migration error.

        The pipeline emits Browser Library (Playwright) locator syntax only
        (role=, text=, >>> iframe piercing); selenium mode silently generated
        broken tests, so it now fails fast instead.
        """
        with pytest.raises(ValidationError, match="no longer supported"):
            self._make_settings({"ROBOT_LIBRARY": "selenium"})

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


def test_local_service_urls_use_ipv4_loopback(monkeypatch):
    """Local service hops must default to 127.0.0.1, never localhost.

    On Windows `localhost` resolves to IPv6 ::1 first; these services bind IPv4
    only, so a `localhost` default adds a ~2s connect stall per hop (execute
    makes two hops -> ~4s). Guard against a well-meaning revert to `localhost`.

    The assertion is about the DEFAULTS, so both override sources are cut off:
    the process env and the .env file. docker-compose.yml sets both of these
    vars to service names, so without this the test fails wherever compose's
    environment is present — a false alarm about a default that never changed.
    """
    monkeypatch.delenv("RUNNER_EXEC_URL", raising=False)
    monkeypatch.delenv("BROWSER_USE_SERVICE_URL", raising=False)
    from src.backend.core.config import Settings
    s = Settings(_env_file=None)
    assert s.RUNNER_EXEC_URL == "http://127.0.0.1:4998"
    assert s.BROWSER_USE_SERVICE_URL == "http://127.0.0.1:4999"
    assert "localhost" not in s.RUNNER_EXEC_URL
    assert "localhost" not in s.BROWSER_USE_SERVICE_URL
