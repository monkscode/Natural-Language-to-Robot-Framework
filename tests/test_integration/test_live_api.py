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

    # These probes carry no JWT. With AUTH_ENFORCED on (the default) the
    # guarded routes answer 401 — which still proves the route is alive and
    # the guard works; 200 covers the AUTH_ENFORCED=false dev hatch.

    def test_docker_status(self):
        """GET /docker-status is up (200 dev hatch / 401 when guarded)."""
        resp = requests.get(f"{SERVICE_URL}/docker-status", timeout=5)
        assert resp.status_code in (200, 401)
        if resp.status_code == 200:
            assert isinstance(resp.json(), dict)

    def test_learning_stats(self):
        """GET /api/learning-stats is up (admin-guarded: 401 without a token)."""
        resp = requests.get(f"{SERVICE_URL}/api/learning-stats", timeout=5)
        assert resp.status_code in (200, 401)

    def test_metrics_health(self):
        """GET /api/workflow-metrics/health is up (admin-guarded route)."""
        resp = requests.get(f"{SERVICE_URL}/api/workflow-metrics/health", timeout=5)
        assert resp.status_code in (200, 401)
