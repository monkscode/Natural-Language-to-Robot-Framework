"""
Tests for the Healing API endpoints.

This module contains comprehensive tests for all healing-related API endpoints
including status, configuration, sessions, reports, and real-time updates.
"""

import asyncio
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.backend.api.healing_endpoints import router as healing_router
from src.backend.core.models import (
    HealingConfiguration, HealingSession, HealingStatus, 
    FailureContext, LocatorAttempt, LocatorStrategy
)


@pytest.fixture
def app():
    """Create FastAPI app with healing router for testing."""
    app = FastAPI()
    app.include_router(healing_router)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_healing_config():
    """Mock healing configuration."""
    return HealingConfiguration(
        enabled=True,
        max_attempts_per_locator=3,
        chrome_session_timeout=30,
        healing_timeout=300,
        max_concurrent_sessions=3,
        confidence_threshold=0.7,
        max_alternatives=5
    )


@pytest.fixture
def mock_failure_context():
    """Mock failure context."""
    return FailureContext(
        test_file="test_example.robot",
        test_case="Test Login",
        failing_step="Click Element    id=login-button",
        original_locator="id=login-button",
        target_url="https://example.com/login",
        exception_type="NoSuchElementException",
        exception_message="Element not found",
        timestamp=datetime.now(),
        run_id="test-run-123"
    )


@pytest.fixture
def mock_healing_session(mock_failure_context):
    """Mock healing session."""
    return HealingSession(
        session_id="session-123",
        failure_context=mock_failure_context,
        status=HealingStatus.IN_PROGRESS,
        started_at=datetime.now(),
        progress=0.5,
        current_phase="validation",
        attempts=[
            LocatorAttempt(
                locator="css=#login-button",
                strategy=LocatorStrategy.CSS,
                success=True,
                confidence_score=0.8,
                timestamp=datetime.now()
            )
        ]
    )


class TestHealingStatusEndpoint:
    """Tests for the healing status endpoint."""

    @patch('src.backend.api.healing_endpoints.get_healing_config')
    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_docker_client')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_status_success(self, mock_user, mock_docker, mock_orchestrator, mock_config, client, mock_healing_config):
        """Test successful healing status retrieval."""
        # Setup mocks
        mock_user.return_value = {"username": "test", "role": "user"}
        mock_config.return_value = mock_healing_config
        
        mock_orch = AsyncMock()
        mock_orch.active_sessions = {"session-1": Mock(status=HealingStatus.IN_PROGRESS)}
        mock_orchestrator.return_value = mock_orch
        
        mock_docker.return_value = Mock()
        
        with patch('src.backend.api.healing_endpoints.get_healing_container_status') as mock_container_status:
            mock_container_status.return_value = {"running": 2, "stopped": 0}
            
            response = client.get("/healing/status")
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["healing_enabled"] is True
            assert data["active_sessions"] == 1
            assert "configuration" in data
            assert "containers" in data

    @patch('src.backend.api.healing_endpoints.get_healing_config')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_status_error(self, mock_user, mock_config, client):
        """Test healing status endpoint error handling."""
        mock_user.return_value = {"username": "test", "role": "user"}
        mock_config.side_effect = Exception("Config error")
        
        response = client.get("/healing/status")
        
        assert response.status_code == 500
        assert "Failed to get healing status" in response.json()["detail"]


class TestHealingConfigEndpoint:
    """Tests for the healing configuration endpoint."""

    @patch('src.backend.api.healing_endpoints.get_healing_config')
    @patch('src.backend.api.healing_endpoints.save_healing_config')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_update_healing_config_success(self, mock_user, mock_save, mock_get, client, mock_healing_config):
        """Test successful healing configuration update."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        mock_get.return_value = mock_healing_config
        
        update_data = {
            "enabled": False,
            "max_attempts_per_locator": 5,
            "confidence_threshold": 0.8
        }
        
        response = client.post("/healing/config", json=update_data)
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["configuration"]["enabled"] is False
        assert data["configuration"]["max_attempts_per_locator"] == 5
        assert data["configuration"]["confidence_threshold"] == 0.8
        
        mock_save.assert_called_once()

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_update_healing_config_validation_error(self, mock_user, client):
        """Test healing configuration update with validation error."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        
        # Invalid data - max_attempts_per_locator out of range
        update_data = {
            "max_attempts_per_locator": 15,  # Should be 1-10
            "confidence_threshold": 1.5      # Should be 0.0-1.0
        }
        
        response = client.post("/healing/config", json=update_data)
        
        assert response.status_code == 422  # Validation error


class TestHealingSessionsEndpoint:
    """Tests for the healing sessions endpoints."""

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_sessions_success(self, mock_user, mock_orchestrator, client, mock_healing_session):
        """Test successful healing sessions retrieval."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        mock_orch = AsyncMock()
        mock_orch.active_sessions = {"session-123": mock_healing_session}
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/sessions")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["session_id"] == "session-123"
        assert data["total_sessions"] == 1

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_sessions_with_status_filter(self, mock_user, mock_orchestrator, client, mock_healing_session):
        """Test healing sessions retrieval with status filter."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        # Create sessions with different statuses
        session1 = mock_healing_session
        session1.status = HealingStatus.IN_PROGRESS
        
        session2 = Mock()
        session2.session_id = "session-456"
        session2.status = HealingStatus.SUCCESS
        session2.failure_context = mock_healing_session.failure_context
        session2.started_at = datetime.now()
        session2.completed_at = datetime.now()
        session2.progress = 1.0
        session2.current_phase = "complete"
        session2.attempts = []
        session2.error_message = None
        
        mock_orch = AsyncMock()
        mock_orch.active_sessions = {
            "session-123": session1,
            "session-456": session2
        }
        mock_orchestrator.return_value = mock_orch
        
        # Filter by IN_PROGRESS status
        response = client.get("/healing/sessions?status=in_progress")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["status"] == "IN_PROGRESS"

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_session_by_id_success(self, mock_user, mock_orchestrator, client, mock_healing_session):
        """Test successful individual healing session retrieval."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        mock_orch = AsyncMock()
        mock_orch.get_session_status.return_value = mock_healing_session
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/sessions/session-123")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["session"]["session_id"] == "session-123"
        assert len(data["attempts"]) == 1

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_session_not_found(self, mock_user, mock_orchestrator, client):
        """Test healing session not found."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        mock_orch = AsyncMock()
        mock_orch.get_session_status.return_value = None
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/sessions/nonexistent")
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_cancel_healing_session_success(self, mock_user, mock_orchestrator, client):
        """Test successful healing session cancellation."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        
        mock_orch = AsyncMock()
        mock_orch.cancel_session.return_value = True
        mock_orchestrator.return_value = mock_orch
        
        response = client.post("/healing/sessions/session-123/cancel")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "cancelled successfully" in data["message"]

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_cancel_healing_session_not_found(self, mock_user, mock_orchestrator, client):
        """Test healing session cancellation when session not found."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        
        mock_orch = AsyncMock()
        mock_orch.cancel_session.return_value = False
        mock_orchestrator.return_value = mock_orch
        
        response = client.post("/healing/sessions/nonexistent/cancel")
        
        assert response.status_code == 404
        assert "not found or already completed" in response.json()["detail"]


class TestHealingReportsEndpoint:
    """Tests for the healing reports endpoints."""

    @patch('src.backend.api.healing_endpoints.healing_reports')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_report_success(self, mock_user, mock_reports, client):
        """Test successful healing report retrieval."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        mock_report = {
            "run_id": "test-run-123",
            "test_file": "test_example.robot",
            "healing_attempts": [
                {
                    "session_id": "session-123",
                    "test_case": "Test Login",
                    "original_locator": "id=login-button",
                    "status": "SUCCESS",
                    "attempts": 2,
                    "started_at": datetime.now().isoformat(),
                    "completed_at": datetime.now().isoformat(),
                    "error_message": None
                }
            ],
            "total_attempts": 1,
            "successful_healings": 1,
            "failed_healings": 0,
            "total_time": 45.5,
            "generated_at": datetime.now()
        }
        
        mock_reports.__getitem__ = Mock(return_value=mock_report)
        mock_reports.get = Mock(return_value=mock_report)
        
        response = client.get("/healing/reports/test-run-123")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["report"]["run_id"] == "test-run-123"
        assert data["report"]["successful_healings"] == 1

    @patch('src.backend.api.healing_endpoints.healing_reports')
    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_report_generate_from_sessions(self, mock_user, mock_orchestrator, mock_reports, client, mock_healing_session):
        """Test healing report generation from active sessions."""
        mock_user.return_value = {"username": "test", "role": "user"}
        mock_reports.get = Mock(return_value=None)  # No existing report
        
        # Mock session with matching run_id
        mock_healing_session.failure_context.run_id = "test-run-123"
        mock_healing_session.status = HealingStatus.SUCCESS
        mock_healing_session.completed_at = datetime.now()
        
        mock_orch = AsyncMock()
        mock_orch.active_sessions = {"session-123": mock_healing_session}
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/reports/test-run-123")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["report"]["run_id"] == "test-run-123"

    @patch('src.backend.api.healing_endpoints.healing_reports')
    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_report_not_found(self, mock_user, mock_orchestrator, mock_reports, client):
        """Test healing report not found."""
        mock_user.return_value = {"username": "test", "role": "user"}
        mock_reports.get = Mock(return_value=None)
        
        mock_orch = AsyncMock()
        mock_orch.active_sessions = {}
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/reports/nonexistent")
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    @patch('src.backend.api.healing_endpoints.healing_reports')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_reports_list(self, mock_user, mock_reports, client):
        """Test healing reports list retrieval."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        # Mock reports dictionary
        mock_reports_data = {
            "run-1": {
                "run_id": "run-1",
                "test_file": "test1.robot",
                "total_attempts": 2,
                "successful_healings": 1,
                "failed_healings": 1,
                "generated_at": datetime.now()
            },
            "run-2": {
                "run_id": "run-2", 
                "test_file": "test2.robot",
                "total_attempts": 1,
                "successful_healings": 1,
                "failed_healings": 0,
                "generated_at": datetime.now() - timedelta(days=1)
            }
        }
        
        mock_reports.items = Mock(return_value=mock_reports_data.items())
        mock_reports.__len__ = Mock(return_value=len(mock_reports_data))
        
        response = client.get("/healing/reports")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert len(data["reports"]) == 2
        assert data["total_reports"] == 2


class TestHealingStatisticsEndpoint:
    """Tests for the healing statistics endpoint."""

    @patch('src.backend.api.healing_endpoints.healing_statistics')
    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_get_healing_statistics_success(self, mock_user, mock_orchestrator, mock_stats, client, mock_healing_session):
        """Test successful healing statistics retrieval."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        # Mock statistics
        mock_stats.__getitem__ = Mock(side_effect=lambda key: {
            "total_attempts": 10,
            "successful_healings": 7,
            "failed_healings": 3,
            "success_rate": 70.0,
            "average_healing_time": 45.2
        }[key])
        
        # Mock recent sessions
        recent_sessions = [mock_healing_session] * 5
        for i, session in enumerate(recent_sessions):
            session.started_at = datetime.now() - timedelta(hours=i)
            session.status = HealingStatus.SUCCESS if i < 3 else HealingStatus.FAILED
        
        mock_orch = AsyncMock()
        mock_orch.active_sessions = {f"session-{i}": session for i, session in enumerate(recent_sessions)}
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/statistics")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "statistics" in data
        stats = data["statistics"]
        assert stats["total_attempts"] == 10
        assert stats["success_rate"] == 70.0
        assert "last_24h_attempts" in stats
        assert "healing_trends" in stats
        assert "top_failure_types" in stats


class TestHealingProgressEndpoint:
    """Tests for the healing progress streaming endpoint."""

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_stream_healing_progress_session_not_found(self, mock_user, mock_orchestrator, client):
        """Test progress streaming when session not found."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        mock_orch = AsyncMock()
        mock_orch.get_session_status.return_value = None
        mock_orchestrator.return_value = mock_orch
        
        response = client.get("/healing/progress/nonexistent")
        
        assert response.status_code == 200
        # For SSE, we need to check the content
        content = response.content.decode()
        assert "Session nonexistent not found" in content


class TestHealingContainerCleanupEndpoint:
    """Tests for the healing container cleanup endpoint."""

    @patch('src.backend.api.healing_endpoints.get_docker_client')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    @patch('src.backend.api.healing_endpoints.rate_limit_healing')
    def test_cleanup_healing_containers_success(self, mock_rate_limit, mock_user, mock_docker, client):
        """Test successful healing container cleanup."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        mock_rate_limit.return_value = None
        
        # Mock Docker containers
        mock_container1 = Mock()
        mock_container1.name = "chrome-healing-1"
        mock_container1.stop = Mock()
        mock_container1.remove = Mock()
        
        mock_container2 = Mock()
        mock_container2.name = "chrome-healing-2"
        mock_container2.stop = Mock()
        mock_container2.remove = Mock()
        
        mock_client = Mock()
        mock_client.containers.list.return_value = [mock_container1, mock_container2]
        mock_docker.return_value = mock_client
        
        response = client.delete("/healing/containers/cleanup")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["containers_cleaned"] == 2
        assert "Cleaned up 2 healing containers" in data["message"]

    @patch('src.backend.api.healing_endpoints.get_docker_client')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    @patch('src.backend.api.healing_endpoints.rate_limit_healing')
    def test_cleanup_healing_containers_with_errors(self, mock_rate_limit, mock_user, mock_docker, client):
        """Test healing container cleanup with some failures."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        mock_rate_limit.return_value = None
        
        # Mock containers - one succeeds, one fails
        mock_container1 = Mock()
        mock_container1.name = "chrome-healing-1"
        mock_container1.stop = Mock()
        mock_container1.remove = Mock()
        
        mock_container2 = Mock()
        mock_container2.name = "chrome-healing-2"
        mock_container2.stop = Mock(side_effect=Exception("Stop failed"))
        mock_container2.remove = Mock()
        
        mock_client = Mock()
        mock_client.containers.list.return_value = [mock_container1, mock_container2]
        mock_docker.return_value = mock_client
        
        response = client.delete("/healing/containers/cleanup")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["containers_cleaned"] == 1
        assert data["errors"] is not None
        assert len(data["errors"]) == 1


class TestHealingTestTriggerEndpoint:
    """Tests for the healing test trigger endpoint."""

    @patch('src.backend.api.healing_endpoints.get_healing_orchestrator')
    @patch('src.backend.api.healing_endpoints.get_current_user')
    @patch('src.backend.api.healing_endpoints.rate_limit_healing')
    def test_trigger_test_healing_success(self, mock_rate_limit, mock_user, mock_orchestrator, client, mock_healing_session):
        """Test successful test healing trigger."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        mock_rate_limit.return_value = None
        
        mock_orch = AsyncMock()
        mock_orch.initiate_healing.return_value = mock_healing_session
        mock_orchestrator.return_value = mock_orch
        
        test_data = {
            "test_file": "test_example.robot",
            "test_case": "Test Login",
            "failing_locator": "id=login-button",
            "target_url": "https://example.com/login"
        }
        
        response = client.post("/healing/test", json=test_data)
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["session_id"] == "session-123"
        assert "progress_url" in data


@pytest.mark.asyncio
class TestAsyncEndpoints:
    """Tests for async endpoint functionality."""

    async def test_healing_orchestrator_lifecycle(self):
        """Test healing orchestrator creation and lifecycle."""
        from src.backend.api.healing_endpoints import get_healing_orchestrator
        
        with patch('src.backend.api.healing_endpoints.get_healing_config') as mock_config:
            mock_config.return_value = HealingConfiguration()
            
            with patch('src.backend.services.healing_orchestrator.HealingOrchestrator') as mock_orch_class:
                mock_orch = AsyncMock()
                mock_orch.start = AsyncMock()
                mock_orch_class.return_value = mock_orch
                
                orchestrator = await get_healing_orchestrator()
                
                assert orchestrator is not None
                mock_orch.start.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])