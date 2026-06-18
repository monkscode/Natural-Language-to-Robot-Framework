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

    def test_get_metrics_by_date_range_filters_natively(self, collector):
        old = self._make_metric("wf-old")
        old.timestamp = datetime(2026, 1, 1)
        new = self._make_metric("wf-new")
        new.timestamp = datetime(2026, 3, 1)
        collector.record_workflow(old)
        collector.record_workflow(new)
        got = collector.get_metrics_by_date_range(
            start_date=datetime(2026, 2, 1), end_date=datetime(2026, 4, 1))
        assert [m.workflow_id for m in got] == ["wf-new"]
        got = collector.get_metrics_by_date_range(end_date=datetime(2026, 1, 15))
        assert [m.workflow_id for m in got] == ["wf-old"]

    def test_aggregate_metrics_empty_range_returns_zeroes(self, collector):
        agg = collector.get_aggregate_metrics(
            start_date=datetime(1999, 1, 1), end_date=datetime(1999, 12, 31))
        assert agg["total_workflows"] == 0
        assert agg["avg_success_rate"] == 0.0
        assert agg["date_range"]["start"] == "1999-01-01T00:00:00"

    def test_invalid_jsonb_row_is_skipped_not_fatal(self, collector):
        """One corrupt row must not take the whole metrics dashboard down."""
        collector.record_workflow(self._make_metric("wf-good"))
        with collector._pool.connection() as conn:
            conn.execute(
                "INSERT INTO workflow_metrics (workflow_id, ts, data) "
                "VALUES ('wf-corrupt', now(), '{\"not\": \"a metric\"}'::jsonb)")
        metrics = collector.get_all_metrics()
        assert [m.workflow_id for m in metrics if m.workflow_id == "wf-good"]
        assert not [m for m in metrics if m.workflow_id == "wf-corrupt"]

    def test_db_failures_degrade_instead_of_raising(self, collector):
        """Metrics recording/reads are observability — a DB outage must not
        crash the workflow that's being measured."""
        from unittest.mock import MagicMock
        broken = MagicMock()
        broken.connection.side_effect = RuntimeError("postgres down")
        real_pool = collector._pool
        collector._pool = broken
        try:
            collector.record_workflow(self._make_metric("wf-x"))  # no raise
            assert collector.get_all_metrics() == []
            assert collector.get_metrics_by_date_range(start_date=datetime.now()) == []
        finally:
            collector._pool = real_pool

    def test_close_resets_process_singleton(self):
        """close() on the singleton must drop the global so the next getter
        builds a fresh pool instead of reusing a closed one."""
        from src.backend.core import workflow_metrics as wm
        from unittest.mock import MagicMock
        saved = wm._metrics_collector
        try:
            fake_pool = MagicMock()
            inst = WorkflowMetricsCollector.__new__(WorkflowMetricsCollector)
            inst._pool = fake_pool
            wm._metrics_collector = inst
            inst.close()
            fake_pool.close.assert_called_once()
            assert wm._metrics_collector is None
        finally:
            wm._metrics_collector = saved

    def test_get_collector_caches_singleton(self):
        from src.backend.core import workflow_metrics as wm
        from unittest.mock import MagicMock, patch
        saved = wm._metrics_collector
        try:
            wm._metrics_collector = None
            with patch.object(wm, "WorkflowMetricsCollector", MagicMock()) as cls:
                first = wm.get_workflow_metrics_collector()
                assert wm.get_workflow_metrics_collector() is first
                assert cls.call_count == 1
        finally:
            wm._metrics_collector = saved


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

    def test_falls_back_to_word_estimate_when_litellm_fails(self):
        """Token counting feeds cost tracking — a litellm hiccup must degrade
        to the word estimate, not crash the metrics path."""
        with patch("litellm.token_counter", side_effect=RuntimeError("no tokenizer")):
            result = count_tokens("one two three four", model="gemini/gemini-2.5-flash")
        assert result == int(4 * 1.33)

    def test_resolves_model_from_settings_when_not_given(self):
        with patch(
            "src.backend.crew_ai.llm_provider_routing.resolve_model_string",
            return_value="gemini/gemini-2.5-flash",
        ) as resolve, patch("litellm.token_counter", return_value=7):
            assert count_tokens("hello world") == 7
        resolve.assert_called_once()


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

    def test_resolves_model_from_settings_when_not_given(self):
        usage = {"total_tokens": 10, "successful_requests": 1, "total_cost": 0.01}
        with patch(
            "src.backend.crew_ai.llm_provider_routing.resolve_model_string",
            return_value="gemini/gemini-2.5-flash",
        ) as resolve:
            cost = calculate_crewai_cost(usage_metrics=usage)
        resolve.assert_called_once()
        assert cost["cost"] == pytest.approx(0.01)
        assert cost["tokens"] == 10
