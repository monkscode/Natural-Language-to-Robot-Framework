"""
Shared pytest fixtures for NL repo test suite.

Provides:
- mock_settings: Patched Settings with safe test defaults
- sample_robot_code: Reusable RF code snippet
- tmp_metrics_dir: Temporary directory for metrics files
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _disable_auth_rate_limit():
    """Disable the auth rate limiter by default so suites that hammer /login or
    /register aren't throttled. The dedicated rate-limit tests re-enable it."""
    try:
        from src.backend.auth.rate_limit import limiter
    except Exception:
        yield
        return
    saved = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = saved


@pytest.fixture
def mock_settings():
    """Patched Settings with test defaults — no real env vars read."""
    with patch.dict(os.environ, {
        "MODEL_PROVIDER": "gemini",
        "GEMINI_API_KEY": "test-key-123",
        "ONLINE_MODEL": "gemini-2.5-flash",
        "LOCAL_MODEL": "llama3",
        "APP_PORT": "5000",
        "BROWSER_USE_SERVICE_URL": "http://localhost:4999",
        "BROWSER_HEADLESS": "true",
        "ROBOT_LIBRARY": "browser",
        "MAX_AGENT_ITERATIONS": "3",
        "ENABLE_CUSTOM_ACTIONS": "true",
        "OPTIMIZATION_ENABLED": "false",
    }, clear=False):
        from src.backend.core.config import Settings
        yield Settings()


@pytest.fixture
def sample_robot_code():
    """Reusable RF test code snippet."""
    return """*** Settings ***
Library    Browser

*** Test Cases ***
Search For Shoes
    New Browser    chromium    headless=true
    New Page       https://example.com
    Fill Text      id=search-box    shoes
    Click          id=submit-btn
    Get Text       id=results    contains    shoes
"""


@pytest.fixture
def tmp_metrics_dir(tmp_path):
    """Temporary directory for metrics file storage tests."""
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    return metrics_dir
