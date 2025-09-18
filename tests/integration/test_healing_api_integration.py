"""
Integration tests for the Healing API endpoints.

This module contains integration tests that test the complete healing API
workflow including real database interactions and service integrations.
"""

import asyncio
import json
import pytest
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock
from fastapi.testclient import TestClient

from src.backend.main import app
from src.backend.core.models import HealingConfiguration, FailureContext, HealingStatus
from src.backend.core.config_loader import SelfHealingConfigLoader


@pytest.fixture
def client():
    """Create test client with full app."""
    return TestClient(app)


@pytest.fixture
def temp_config_file():
    """Create temporary config file for testing."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        config_content = """
self_healing:
  enabled: true
  max_attempts_per_locator: 3
  chrome_session_timeout: 30
  healing_timeout: 300
  max_concurrent_sessions: 3
  backup_retention_days: 7
  failure_detection:
    enable_fingerprinting: true
    confidence_threshold: 0.7
  locator_generation:
    strategies: ["id", "name", "css", "xpath", "link_text"]
    max_alternatives: 5
  validation:
    element_wait_timeout: 10
    interaction_test: true
"""
        f.write(config_content)
        f.flush()
        yield f.name
    
    # Cleanup
    os.unlink(f.name)


@pytest.fixture
def mock_docker_client():
    """Mock Docker client for container operations."""
    mock_client = Mock()
    mock_container = Mock()
    mock_container.name = "chrome-healing-test"
    mock_container.stop = Mock()
    mock_container.remove = Mock()
    mock_client.containers.list.return_value = [mock_container]
    return mock_client


class TestHealingAPIIntegration:
    """Integration tests for healing API endpoints."""

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_healing_status_endpoint_integration(self, mock_user, client, temp_config_file):
        """Test healing status endpoint with real configuration loading."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        with patch('src.backend.core.config.settings.SELF_HEALING_CONFIG_PATH', temp_config_file):
            with patch('src.backend.api.healing_endpoints.get_docker_client') as mock_docker:
                with patch('src.backend.api.healing_endpoints.get_healing_container_status') as mock_container_status:
                    mock_docker.return_value = Mock()
                    mock_container_status.return_value = {"running": 1, "stopped": 0}
                    
                    response = client.get("/api/healing/status")
                    
                    assert response.status_code == 200
                    data = response.json()
                    assert data["status"] == "success"
                    assert data["healing_enabled"] is True
                    assert data["configuration"]["max_attempts_per_locator"] == 3
                    assert data["configuration"]["confidence_threshold"] == 0.7

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_healing_config_update_integration(self, mock_user, client, temp_config_file):
        """Test healing configuration update with real file operations."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        
        with patch('src.backend.core.config.settings.SELF_HEALING_CONFIG_PATH', temp_config_file):
            # Update configuration
            update_data = {
                "enabled": False,
                "max_attempts_per_locator": 5,
                "confidence_threshold": 0.8
            }
            
            response = client.post("/api/healing/config", json=update_data)
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["configuration"]["enabled"] is False
            assert data["configuration"]["max_attempts_per_locator"] == 5
            assert data["configuration"]["confidence_threshold"] == 0.8
            
            # Verify configuration was actually saved
            config_loader = SelfHealingConfigLoader(temp_config_file)
            loaded_config = config_loader.load_config()
            assert loaded_config.enabled is False
            assert loaded_config.max_attempts_per_locator == 5
            assert loaded_config.confidence_threshold == 0.8

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_healing_sessions_workflow_integration(self, mock_user, client):
        """Test complete healing sessions workflow."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        with patch('src.backend.api.healing_endpoints.get_healing_orchestrator') as mock_get_orch:
            # Mock orchestrator with realistic session data
            mock_orch = AsyncMock()
            
            # Create mock session
            failure_context = FailureContext(
                test_file="integration_test.robot",
                test_case="Test Integration",
                failing_step="Click Element    id=submit-btn",
                original_locator="id=submit-btn",
                target_url="https://example.com/form",
                exception_type="NoSuchElementException",
                exception_message="Element not found",
                timestamp=datetime.now(),
                run_id="integration-run-123"
            )
            
            from src.backend.core.models import HealingSession
            mock_session = HealingSession(
                session_id="integration-session-123",
                failure_context=failure_context,
                status=HealingStatus.IN_PROGRESS,
                started_at=datetime.now(),
                progress=0.3,
                current_phase="analysis"
            )
            
            mock_orch.active_sessions = {"integration-session-123": mock_session}
            mock_orch.get_session_status.return_value = mock_session
            mock_orch.cancel_session.return_value = True
            mock_get_orch.return_value = mock_orch
            
            # Test getting all sessions
            response = client.get("/api/healing/sessions")
            assert response.status_code == 200
            data = response.json()
            assert len(data["sessions"]) == 1
            assert data["sessions"][0]["session_id"] == "integration-session-123"
            
            # Test getting specific session
            response = client.get("/api/healing/sessions/integration-session-123")
            assert response.status_code == 200
            data = response.json()
            assert data["session"]["session_id"] == "integration-session-123"
            assert data["session"]["status"] == "IN_PROGRESS"
            
            # Test cancelling session
            response = client.post("/api/healing/sessions/integration-session-123/cancel")
            assert response.status_code == 200
            data = response.json()
            assert "cancelled successfully" in data["message"]

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_healing_reports_integration(self, mock_user, client):
        """Test healing reports functionality."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        # Mock reports data
        with patch('src.backend.api.healing_endpoints.healing_reports') as mock_reports:
            mock_report_data = {
                "integration-run-123": {
                    "run_id": "integration-run-123",
                    "test_file": "integration_test.robot",
                    "healing_attempts": [
                        {
                            "session_id": "session-123",
                            "test_case": "Test Integration",
                            "original_locator": "id=submit-btn",
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
            }
            
            mock_reports.get = Mock(return_value=mock_report_data["integration-run-123"])
            mock_reports.items = Mock(return_value=mock_report_data.items())
            mock_reports.__len__ = Mock(return_value=1)
            
            # Test getting specific report
            response = client.get("/api/healing/reports/integration-run-123")
            assert response.status_code == 200
            data = response.json()
            assert data["report"]["run_id"] == "integration-run-123"
            assert data["report"]["successful_healings"] == 1
            
            # Test getting reports list
            response = client.get("/api/healing/reports")
            assert response.status_code == 200
            data = response.json()
            assert len(data["reports"]) == 1
            assert data["total_reports"] == 1

    @patch('src.backend.api.healing_endpoints.get_current_user')
    @patch('src.backend.api.healing_endpoints.rate_limit_healing')
    def test_container_cleanup_integration(self, mock_rate_limit, mock_user, client, mock_docker_client):
        """Test container cleanup integration."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        mock_rate_limit.return_value = None
        
        with patch('src.backend.api.healing_endpoints.get_docker_client') as mock_get_docker:
            mock_get_docker.return_value = mock_docker_client
            
            response = client.delete("/api/healing/containers/cleanup")
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["containers_cleaned"] == 1
            
            # Verify Docker operations were called
            mock_docker_client.containers.list.assert_called_once()
            mock_docker_client.containers.list.return_value[0].stop.assert_called_once()
            mock_docker_client.containers.list.return_value[0].remove.assert_called_once()

    @patch('src.backend.api.healing_endpoints.get_current_user')
    @patch('src.backend.api.healing_endpoints.rate_limit_healing')
    def test_test_healing_trigger_integration(self, mock_rate_limit, mock_user, client):
        """Test healing trigger integration."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        mock_rate_limit.return_value = None
        
        with patch('src.backend.api.healing_endpoints.get_healing_orchestrator') as mock_get_orch:
            # Mock successful healing initiation
            mock_orch = AsyncMock()
            
            from src.backend.core.models import HealingSession
            mock_session = HealingSession(
                session_id="test-trigger-session-123",
                failure_context=Mock(),
                status=HealingStatus.PENDING,
                started_at=datetime.now()
            )
            
            mock_orch.initiate_healing.return_value = mock_session
            mock_get_orch.return_value = mock_orch
            
            test_data = {
                "test_file": "integration_test.robot",
                "test_case": "Test Trigger",
                "failing_locator": "id=trigger-btn",
                "target_url": "https://example.com/trigger"
            }
            
            response = client.post("/api/healing/test", json=test_data)
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["session_id"] == "test-trigger-session-123"
            assert "/api/healing/progress/" in data["progress_url"]
            
            # Verify healing was initiated with correct parameters
            mock_orch.initiate_healing.assert_called_once()
            call_args = mock_orch.initiate_healing.call_args[0][0]
            assert call_args.test_file == "integration_test.robot"
            assert call_args.test_case == "Test Trigger"
            assert call_args.original_locator == "id=trigger-btn"
            assert call_args.target_url == "https://example.com/trigger"

    def test_healing_statistics_integration(self, client):
        """Test healing statistics integration."""
        with patch('src.backend.api.healing_endpoints.get_current_user') as mock_user:
            mock_user.return_value = {"username": "test", "role": "user"}
            
            with patch('src.backend.api.healing_endpoints.healing_statistics') as mock_stats:
                with patch('src.backend.api.healing_endpoints.get_healing_orchestrator') as mock_get_orch:
                    # Mock statistics data
                    mock_stats.__getitem__ = Mock(side_effect=lambda key: {
                        "total_attempts": 25,
                        "successful_healings": 18,
                        "failed_healings": 7,
                        "success_rate": 72.0,
                        "average_healing_time": 42.3
                    }[key])
                    
                    # Mock orchestrator with session data
                    mock_orch = AsyncMock()
                    mock_sessions = {}
                    
                    # Create mock sessions for trend analysis
                    for i in range(10):
                        session_id = f"stats-session-{i}"
                        mock_session = Mock()
                        mock_session.started_at = datetime.now() - timedelta(hours=i * 2)
                        mock_session.status = HealingStatus.SUCCESS if i < 7 else HealingStatus.FAILED
                        mock_session.failure_context = Mock()
                        mock_session.failure_context.failure_type = "locator_not_found"
                        mock_sessions[session_id] = mock_session
                    
                    mock_orch.active_sessions = mock_sessions
                    mock_get_orch.return_value = mock_orch
                    
                    response = client.get("/api/healing/statistics")
                    
                    assert response.status_code == 200
                    data = response.json()
                    assert data["status"] == "success"
                    
                    stats = data["statistics"]
                    assert stats["total_attempts"] == 25
                    assert stats["success_rate"] == 72.0
                    assert stats["average_healing_time"] == 42.3
                    assert "last_24h_attempts" in stats
                    assert "healing_trends" in stats
                    assert len(stats["healing_trends"]) == 7  # 7 days of trends
                    assert "top_failure_types" in stats


class TestHealingAPIErrorHandling:
    """Integration tests for error handling in healing API."""

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_config_validation_errors(self, mock_user, client):
        """Test configuration validation error handling."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        
        # Test invalid configuration values
        invalid_configs = [
            {"max_attempts_per_locator": 0},  # Too low
            {"max_attempts_per_locator": 15},  # Too high
            {"confidence_threshold": -0.1},  # Too low
            {"confidence_threshold": 1.5},  # Too high
            {"chrome_session_timeout": 2},  # Too low
            {"healing_timeout": 10},  # Too low
        ]
        
        for invalid_config in invalid_configs:
            response = client.post("/api/healing/config", json=invalid_config)
            assert response.status_code == 422  # Validation error

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_service_unavailable_errors(self, mock_user, client):
        """Test handling of service unavailable errors."""
        mock_user.return_value = {"username": "test", "role": "user"}
        
        # Test Docker service unavailable
        with patch('src.backend.api.healing_endpoints.get_docker_client') as mock_docker:
            mock_docker.side_effect = ConnectionError("Docker daemon not available")
            
            response = client.get("/api/healing/status")
            # Should still return 200 but with error in containers section
            assert response.status_code == 200
            data = response.json()
            assert "error" in str(data["containers"])

    @patch('src.backend.api.healing_endpoints.get_current_user')
    def test_rate_limiting_integration(self, mock_user, client):
        """Test rate limiting integration."""
        mock_user.return_value = {"username": "admin", "role": "admin"}
        
        with patch('src.backend.api.healing_endpoints.get_docker_client') as mock_docker:
            mock_docker.return_value = Mock()
            mock_docker.return_value.containers.list.return_value = []
            
            # Make multiple requests quickly to trigger rate limiting
            responses = []
            for i in range(15):  # More than the limit of 10
                response = client.delete("/api/healing/containers/cleanup")
                responses.append(response)
            
            # Some requests should be rate limited
            rate_limited_responses = [r for r in responses if r.status_code == 429]
            assert len(rate_limited_responses) > 0


class TestHealingAPIAuthentication:
    """Integration tests for authentication in healing API."""

    def test_unauthenticated_requests(self, client):
        """Test that unauthenticated requests are handled properly."""
        # For development, unauthenticated requests should be allowed
        # In production, these would return 401
        
        response = client.get("/api/healing/status")
        # Should work in development mode
        assert response.status_code == 200

    def test_authenticated_requests_with_token(self, client):
        """Test authenticated requests with bearer token."""
        headers = {"Authorization": "Bearer admin-token"}
        
        response = client.get("/api/healing/status", headers=headers)
        assert response.status_code == 200
        
        # Test admin operations
        response = client.delete("/api/healing/containers/cleanup", headers=headers)
        # May fail due to Docker not being available, but should not be auth error
        assert response.status_code != 401

    def test_invalid_token_requests(self, client):
        """Test requests with invalid tokens."""
        headers = {"Authorization": "Bearer invalid-token"}
        
        response = client.get("/api/healing/status", headers=headers)
        assert response.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])