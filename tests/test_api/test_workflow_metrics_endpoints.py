"""
Mocked integration tests for workflow metrics API endpoints.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI
from datetime import datetime

class TestWorkflowMetricsEndpoints:
    """Tests for workflow metrics API endpoints."""

    @pytest.fixture
    def mock_collector(self):
        """Mocked metrics collector."""
        mock = MagicMock()
        mock.get_all_metrics.return_value = []
        mock.get_metrics_by_date_range.return_value = []
        mock.storage_path = "/tmp/fake.jsonl"
        return mock

    @pytest.fixture
    def metrics_client(self, mock_collector):
        """FastAPI test client with mocked metrics collector."""
        with patch("src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector", return_value=mock_collector):
            from src.backend.api.workflow_metrics_endpoints import router
            app = FastAPI()
            app.include_router(router, prefix="/api")
            
            with TestClient(app) as client:
                yield client, mock_collector

    def test_record_metric(self, metrics_client):
        """POST /record accepts metrics data."""
        client, mock_collector = metrics_client
        resp = client.post("/api/workflow-metrics/record", json={
            "workflow_id": "wf-001",
            "url": "https://example.com",
            "total_llm_calls": 0,
            "total_cost": 0.0,
            "execution_time": 1.5
        })
        assert resp.status_code == 200
        assert mock_collector.record_workflow.called

    def test_get_workflow_metrics(self, metrics_client):
        """GET / returns list of metrics."""
        client, mock_collector = metrics_client
        
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        metric = WorkflowMetrics(workflow_id="1", url="http", total_llm_calls=0, total_cost=0, execution_time=0, timestamp=datetime.now())
        mock_collector.get_all_metrics.return_value = [metric]
        
        resp = client.get("/api/workflow-metrics/")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_health_check(self, metrics_client):
        """GET /health returns ok status."""
        client, _ = metrics_client
        resp = client.get("/api/workflow-metrics/health")
        assert resp.status_code == 200
        assert "status" in resp.json()
