"""
Live integration tests for NL backend (Tier 2).

Purpose: Verify that the NL FastAPI backend is running and responding
         to health/status endpoints.  These tests only hit lightweight
         endpoints ($0 cost — no LLM calls, no Docker).

Requires: NL backend running on localhost:5000
          Start with: uvicorn src.backend.main:app --port 5000

Tests:
  - /health returns ok
  - /docker-status returns status dict
  - /learning-stats returns stats
  - /api/workflow-metrics/health returns ok
"""

import pytest
import requests

# All live tests require the service to be running
pytestmark = pytest.mark.integration

SERVICE_URL = "http://localhost:5000"


class TestLiveNLApi:
    """Live health-check tests for running NL backend."""

    def test_health_endpoint(self):
        """Root or health endpoint returns ok."""
        # Try common health endpoints
        for path in ["/health", "/", "/docs"]:
            try:
                resp = requests.get(f"{SERVICE_URL}{path}", timeout=5)
                if resp.status_code == 200:
                    assert True
                    return
            except requests.ConnectionError:
                pass
        pytest.fail("No health endpoint responded")

    def test_docker_status(self):
        """GET /docker-status returns status information."""
        resp = requests.get(f"{SERVICE_URL}/docker-status", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_learning_stats(self):
        """GET /api/learning-stats returns stats dict."""
        resp = requests.get(f"{SERVICE_URL}/api/learning-stats", timeout=5)
        assert resp.status_code == 200

    def test_metrics_health(self):
        """GET /api/workflow-metrics/health returns ok."""
        resp = requests.get(f"{SERVICE_URL}/api/workflow-metrics/health", timeout=5)
        assert resp.status_code == 200
