"""
Mocked integration tests for src.backend.api.endpoints — FastAPI route handlers.

Purpose: These test the FastAPI endpoint logic with mocked services.
         Every endpoint streams responses via StreamingResponse, so we
         verify the routing, parameter handling, and error responses.

Tests:
  - generate-test: valid query streams, missing key returns 400
  - execute-test: valid code streams
  - generate-and-run: valid query + execute
  - rebuild-docker: triggers rebuild
  - docker-status: returns status
  - submit-feedback: success / disabled
  - learning-stats: returns stats
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient


def _admin_headers() -> dict:
    """Bearer header with a real admin JWT — require_admin is always strict."""
    from src.backend.auth.jwt_utils import create_access_token
    token = create_access_token(
        {"id": "test-admin", "email": "admin@test.local", "role": "admin", "display_name": "Admin"}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api_client():
    """FastAPI test client with mocked dependencies."""
    with patch("src.backend.api.endpoints.settings") as mock_settings, \
         patch("src.backend.api.endpoints.stream_generate_only") as mock_gen, \
         patch("src.backend.api.endpoints.stream_execute_only") as mock_exec, \
         patch("src.backend.api.endpoints.stream_generate_and_run") as mock_gen_run, \
         patch("src.backend.api.endpoints.runner_exec_client") as mock_rc, \
         patch("src.backend.api.endpoints.get_feedback_loop") as mock_feedback:

        mock_settings.MODEL_PROVIDER = "gemini"
        mock_settings.ONLINE_MODEL = "gemini-2.5-flash"
        mock_settings.LOCAL_MODEL = "llama3"
        mock_settings.GEMINI_API_KEY = "test-key"

        # Mock stream generators to yield test data
        mock_gen.return_value = iter(["data: test\n\n"])
        mock_exec.return_value = iter(["data: executing\n\n"])
        mock_gen_run.return_value = iter(["data: running\n\n"])
        mock_rc.docker_status.return_value = {"status": "ready", "image": "robot-test-runner:latest"}
        mock_rc.rebuild_image.return_value = {"status": "success", "message": "Docker image 'robot-test-runner:latest' rebuilt successfully."}

        mock_feedback_instance = MagicMock()
        mock_feedback_instance.enabled = True
        mock_feedback_instance.submit_feedback.return_value = {"status": "accepted"}
        mock_feedback_instance.get_stats.return_value = {"total_feedback": 0}
        mock_feedback.return_value = mock_feedback_instance

        from src.backend.api.endpoints import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)

        with TestClient(app) as client:
            yield client, mock_settings, mock_feedback_instance


class TestGenerateEndpoint:
    """Tests for POST /generate-test."""

    def test_valid_query_streams(self, api_client):
        """Valid query returns streaming response."""
        client, _, _ = api_client
        resp = client.post("/generate-test", json={"query": "login to website"})
        assert resp.status_code == 200

    def test_empty_query_returns_400(self, api_client):
        """Empty query raises 400."""
        client, _, _ = api_client
        resp = client.post("/generate-test", json={"query": ""})
        assert resp.status_code == 400


class TestExecuteEndpoint:
    """Tests for POST /execute-test."""

    def test_valid_code_streams(self, api_client):
        """Valid RF code returns streaming response."""
        client, _, _ = api_client
        resp = client.post("/execute-test", json={
            "robot_code": "*** Test Cases ***\nTest\n    Log    Hello"
        })
        assert resp.status_code == 200


class TestDockerEndpoints:
    """Tests for Docker-related endpoints."""

    def test_docker_status(self, api_client):
        """GET /docker-status returns status."""
        client, _, _ = api_client
        resp = client.get("/docker-status")
        assert resp.status_code == 200

    def test_rebuild_docker(self, api_client):
        """POST /rebuild-docker-image triggers rebuild."""
        client, _, _ = api_client
        resp = client.post("/rebuild-docker-image", headers=_admin_headers())
        assert resp.status_code == 200

    def test_docker_status_reports_unavailable_when_executor_down(self, api_client):
        """GET /docker-status returns docker_available=False when executor is down."""
        from src.backend.runner_exec.client import RunnerExecUnavailable
        client, _, _ = api_client
        with patch("src.backend.api.endpoints.runner_exec_client") as mock_rc:
            mock_rc.docker_status.side_effect = RunnerExecUnavailable("down")
            r = client.get("/docker-status")
        assert r.status_code == 200
        assert r.json()["docker_available"] is False

    def test_rebuild_proxies_to_executor(self, api_client):
        """POST /rebuild-docker-image proxies call through runner_exec_client."""
        client, _, _ = api_client
        with patch("src.backend.api.endpoints.runner_exec_client") as mock_rc:
            mock_rc.rebuild_image.return_value = {"status": "success"}
            r = client.post("/rebuild-docker-image", headers=_admin_headers())
        assert r.status_code == 200
        assert r.json()["status"] == "success"


class TestFeedbackEndpoints:
    """Tests for learning/feedback endpoints."""

    def test_submit_feedback_success(self, api_client):
        """POST /api/feedback with valid data succeeds."""
        client, _, mock_fb = api_client
        
        # Need to mock the return of process_user_feedback since get_feedback_loop returns mock_fb
        mock_fb.process_user_feedback.return_value = {
            "action": "learning_recorded", "outcome": "processed",
        }
        
        resp = client.post("/api/feedback", json={
            "workflow_id": "wf-001",
            "feedback_type": "close_enough",
            "feedback_text": "Great",
        })
        assert resp.status_code == 200

    def test_submit_feedback_text_over_500_returns_400(self, api_client):
        """POST /api/feedback with feedback_text > 500 chars returns 400."""
        client, _, _ = api_client
        resp = client.post("/api/feedback", json={
            "workflow_id": "wf-001",
            "feedback_type": "completely_wrong",
            "feedback_text": "x" * 501,
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Feedback must be 500 characters or fewer"

    def test_submit_feedback_exactly_500_chars_succeeds(self, api_client):
        """POST /api/feedback with feedback_text of exactly 500 chars is accepted."""
        client, _, mock_fb = api_client
        mock_fb.process_user_feedback.return_value = {
            "action": "learning_recorded", "outcome": "processed",
        }
        resp = client.post("/api/feedback", json={
            "workflow_id": "wf-001",
            "feedback_type": "completely_wrong",
            "feedback_text": "x" * 500,
        })
        assert resp.status_code == 200

    def test_learning_stats(self, api_client):
        """GET /api/learning-stats returns stats."""
        client, _, mock_fb = api_client
        mock_fb.get_learning_stats.return_value = {"total_records": 10}

        resp = client.get("/api/learning-stats", headers=_admin_headers())
        assert resp.status_code == 200


class TestHealthPins:
    """Bench preflight reads its comparability pins from /health."""

    def test_health_exposes_bench_pins(self, health_app_client):
        with patch("src.backend.api.health.get_active_workflow_count", return_value=0), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            mock_settings.OPTIMIZATION_ENABLED = False
            mock_settings.MODEL_PROVIDER = "gemini"
            mock_settings.ONLINE_MODEL = "gemini-3.5-flash"
            mock_settings.DRYRUN_ENABLED = True
            resp = health_app_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["pins"] == {
            "optimization_enabled": False,
            "model_provider": "gemini",
            "online_model": "gemini-3.5-flash",
            "dryrun_enabled": True,
        }

    def test_api_health_exposes_same_pins(self, health_app_client):
        with patch("src.backend.api.health.get_active_workflow_count", return_value=0), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            mock_settings.OPTIMIZATION_ENABLED = True
            mock_settings.MODEL_PROVIDER = "vertex"
            mock_settings.ONLINE_MODEL = "gemini-3.5-pro"
            mock_settings.DRYRUN_ENABLED = False
            resp = health_app_client.get("/api/health")
        assert resp.json()["pins"]["optimization_enabled"] is True


@pytest.fixture(scope="module")
def health_app_client():
    """FastAPI test client with only the health endpoints, no main.py import.

    Mounts the same callables used in production (src.backend.api.health) onto a
    fresh FastAPI app so we can test them without importing src.backend.main (which
    reconfigures sys.stdout/sys.stderr at module load time, breaking pytest's output
    capture). Using the real handlers means regressions in main.py are caught here.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.backend.api.health import health_check, api_health_check

    app = FastAPI()
    app.get("/health")(health_check)
    app.get("/api/health")(api_health_check)

    with TestClient(app) as client:
        yield client


class TestHealthCapacityFields:
    """Tests for capacity fields in FastAPI /health and /api/health endpoints."""

    def test_idle_health_shows_zero_active_workflows(self, health_app_client):
        """/health with no active workflows returns expected capacity fields."""
        # Patch in health.py's namespace — the handlers import both names at module
        # level, so patching the source module has no effect on the bound references.
        with patch("src.backend.api.health.get_active_workflow_count", return_value=0), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            resp = health_app_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_workflows"] == 0
        assert data["max_workflows"] == 10
        assert data["available_slots"] == 10

    def test_under_load_shows_correct_available_slots(self, health_app_client):
        """/health with 5 active workflows shows available_slots = max - 5."""
        with patch("src.backend.api.health.get_active_workflow_count", return_value=5), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            resp = health_app_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_workflows"] == 5
        assert data["available_slots"] == 5

    def test_overload_clamps_available_slots_to_zero(self, health_app_client):
        """/health clamps available_slots to 0 when active exceeds max."""
        with patch("src.backend.api.health.get_active_workflow_count", return_value=12), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            resp = health_app_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_workflows"] == 12
        assert data["available_slots"] == 0, (
            f"expected available_slots=0 when active > max, got {data['available_slots']}"
        )

    def test_health_response_has_all_required_fields(self, health_app_client):
        """/health response contains all 5 required fields."""
        with patch("src.backend.api.health.get_active_workflow_count", return_value=0), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            resp = health_app_client.get("/health")
        data = resp.json()
        for field in ("status", "service", "active_workflows", "max_workflows", "available_slots"):
            assert field in data, f"Missing field: {field}"

    def test_api_health_parity_with_health(self, health_app_client):
        """/api/health returns identical capacity values to /health."""
        with patch("src.backend.api.health.get_active_workflow_count", return_value=3), \
             patch("src.backend.api.health.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            health = health_app_client.get("/health").json()
            api_health = health_app_client.get("/api/health").json()
        for field in ("active_workflows", "max_workflows", "available_slots"):
            assert health[field] == api_health[field], (
                f"/health and /api/health disagree on '{field}': "
                f"{health[field]!r} vs {api_health[field]!r}"
            )


