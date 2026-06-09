"""
Unit tests for src.backend.core.workflow_metrics — WorkflowMetricsCollector.
"""

import pytest
from unittest.mock import patch, MagicMock
from src.backend.core.workflow_metrics import (
    WorkflowMetricsCollector,
    count_tokens,
    calculate_crewai_cost,
)
from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
from datetime import datetime, timedelta

class TestWorkflowMetricsCollector:
    """Tests for the metrics collector."""

    @pytest.fixture
    def collector(self):
        """Fresh collector on an isolated Postgres schema, dropped after the test."""
        import psycopg
        from src.backend.core.config import settings
        schema = "wf_metrics_test"
        admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.execute(f"CREATE SCHEMA {schema}")
        dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{schema}"
        c = WorkflowMetricsCollector(dsn=dsn)
        yield c
        c.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()

    def _make_metric(self, wf_id):
        return WorkflowMetrics(
            workflow_id=wf_id,
            url="http://x.com",
            total_llm_calls=0,
            total_cost=0.0,
            execution_time=0.0,
            timestamp=datetime.now()
        )

    def test_record_and_get_all(self, collector):
        """record_workflow adds entry and get_all_metrics retrieves it."""
        metric = self._make_metric("wf-001")
        collector.record_workflow(metric)
        metrics = collector.get_all_metrics()
        assert len(metrics) >= 1
        assert any(m.workflow_id == "wf-001" for m in metrics)

    def test_record_multiple(self, collector):
        """Multiple records accumulate."""
        for i in range(3):
            collector.record_workflow(self._make_metric(f"wf-{i}"))
        metrics = collector.get_all_metrics()
        assert len(metrics) >= 3

    def test_get_all_with_limit(self, collector):
        """get_all_metrics with limit returns N most recent."""
        for i in range(5):
            collector.record_workflow(self._make_metric(f"wf-{i}"))
        metrics = collector.get_all_metrics(limit=2)
        assert len(metrics) <= 2

    def test_get_aggregate_metrics(self, collector):
        """get_aggregate_metrics returns correctly aggregated dict."""
        collector.record_workflow(self._make_metric("wf-1"))
        agg = collector.get_aggregate_metrics()
        assert agg["total_workflows"] == 1


class TestCountTokens:
    """Tests for the count_tokens utility."""

    def test_empty_text(self):
        """Empty/None text → 0 tokens."""
        assert count_tokens("") == 0
        assert count_tokens(None) == 0

    def test_standard_text(self):
        """Standard text returns non-zero token count."""
        # Simple word-based estimation
        result = count_tokens("Hello world this is a test sentence")
        assert result > 0


class TestCalculateCrewaiCost:
    """Tests for cost calculation."""

    def test_with_token_counts(self):
        """Calculates cost from input/output tokens + model pricing."""
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "successful_requests": 3
        }
        with patch('litellm.completion_cost', return_value=0.005) as mock_cost:
            cost = calculate_crewai_cost(
                usage_metrics=usage,
                model_name="gemini-2.5-flash",
            )
            assert cost["cost"] == 0.005
            assert cost["llm_calls"] == 3

    def test_with_total_cost_provided(self):
        """When total_cost is already provided, uses it directly."""
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "successful_requests": 2,
            "total_cost": 0.05
        }
        cost = calculate_crewai_cost(
            usage_metrics=usage,
            model_name="gemini-2.5-flash",
        )
        assert cost["cost"] == pytest.approx(0.05, abs=0.01)
