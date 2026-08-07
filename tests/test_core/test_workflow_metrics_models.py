"""
Unit tests for WorkflowMetricsModels — Pydantic models for metrics.
"""

import pytest
from datetime import datetime


class TestWorkflowMetricsModel:
    """Tests for the WorkflowMetrics Pydantic model."""

    def _get_model(self):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        return WorkflowMetrics

    def _get_valid_data(self):
        return {
            "workflow_id": "wf-001",
            "url": "https://example.com",
            "total_llm_calls": 0,
            "total_cost": 0.0,
            "execution_time": 1.5,
            "timestamp": datetime.now()
        }

    def test_construction_with_required_fields(self):
        """Model constructs with required fields."""
        Model = self._get_model()
        data = self._get_valid_data()
        m = Model(**data)
        assert m.workflow_id == "wf-001"

    def test_default_dicts_are_independent(self):
        """Default mutable fields (dicts) are independent across instances."""
        Model = self._get_model()
        m1 = Model(**self._get_valid_data())
        m2 = Model(**self._get_valid_data())
        # Mutating one shouldn't affect the other
        if hasattr(m1, 'token_usage'):
            assert m1.token_usage is not m2.token_usage

    def test_to_dict(self):
        """to_dict returns serializable dictionary."""
        Model = self._get_model()
        data = self._get_valid_data()
        data["workflow_id"] = "wf-to-dict"
        m = Model(**data)
        d = m.to_dict()
        assert isinstance(d, dict)
        assert d["workflow_id"] == "wf-to-dict"

    def test_from_dict(self):
        """from_dict reconstructs model from dictionary."""
        Model = self._get_model()
        data = self._get_valid_data()
        data["workflow_id"] = "wf-from-dict"
        data["total_input_tokens"] = 500  # Obsolete field to be ignored
        # Convert datetime to string for from_dict as it would come from JSON
        data["timestamp"] = data["timestamp"].isoformat()
        
        m = Model.from_dict(data)
        assert m.workflow_id == "wf-from-dict"

    def test_timestamp_string_accepted(self):
        """Timestamp as ISO string is accepted."""
        Model = self._get_model()
        data = self._get_valid_data()
        data["workflow_id"] = "wf-ts"
        data["timestamp"] = "2026-03-12T10:00:00"
        m = Model(**data)
        assert m.timestamp is not None

    def test_timestamp_datetime_accepted(self):
        """Timestamp as datetime object is accepted."""
        Model = self._get_model()
        data = self._get_valid_data()
        data["workflow_id"] = "wf-dt"
        m = Model(**data)
        assert m.timestamp is not None

    def test_track_token_usage(self):
        """track_token_usage updates running totals."""
        Model = self._get_model()
        data = self._get_valid_data()
        data["workflow_id"] = "wf-tokens"
        m = Model(**data)
        if hasattr(m, 'track_token_usage'):
            m.track_token_usage(agent_name="step_planner", token_count=100)
            assert m.token_usage["step_planner"] >= 100
            assert m.token_usage["total"] >= 100

    def test_response_conversion(self):
        """Model can be converted for API response."""
        Model = self._get_model()
        data = self._get_valid_data()
        data["workflow_id"] = "wf-resp"
        m = Model(**data)
        d = m.to_dict()
        # Should be JSON-serializable
        import json
        json_str = json.dumps(d, default=str)
        assert "wf-resp" in json_str


class TestUrlOptional:
    """Task 14: extract_url_from_query returns None when the query names no
    URL, and both WorkflowMetrics call sites pass that value straight in —
    so url=None must construct and round-trip."""

    def _valid_data(self, **overrides):
        data = {
            "workflow_id": "wf-nourl",
            "url": None,
            "total_llm_calls": 0,
            "total_cost": 0.0,
            "execution_time": 1.0,
            "timestamp": datetime.now(),
        }
        data.update(overrides)
        return data

    def test_construction_with_url_none(self):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        m = WorkflowMetrics(**self._valid_data())
        assert m.url is None

    def test_url_none_round_trips_through_dict(self):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        m = WorkflowMetrics(**self._valid_data())
        d = m.to_dict()
        assert d["url"] is None
        m2 = WorkflowMetrics.from_dict(
            {**d, "timestamp": datetime.now().isoformat()}
        )
        assert m2.url is None

    def test_response_conversion_with_url_none(self):
        from src.backend.core.models.workflow_metrics_models import (
            WorkflowMetrics,
            WorkflowMetricsResponse,
        )
        m = WorkflowMetrics(**self._valid_data())
        resp = WorkflowMetricsResponse.from_workflow_metrics(m)
        assert resp.url is None


# ===================================================================
# C4 — optimization_fallback_used field
# ===================================================================

class TestOptimizationFallbackUsed:
    """C4: WorkflowMetrics.optimization_fallback_used signals a silent init failure.

    Concrete scenario: SmartKeywordProvider raises on startup (DB lock or schema
    mismatch). Without C4 this run silently uses no hints and the metrics dashboard
    shows no sign anything went wrong. With C4, the per-run record has
    optimization_fallback_used=True so a dashboard alert or a log query can catch it.
    """

    def _make_metrics(self, **kwargs):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        defaults = {
            "workflow_id": "wf-c4",
            "url": "https://example.com",
            "total_llm_calls": 0,
            "total_cost": 0.0,
            "execution_time": 1.0,
            "timestamp": datetime.now(),
        }
        defaults.update(kwargs)
        return WorkflowMetrics(**defaults)

    def test_defaults_to_false(self):
        """optimization_fallback_used defaults to False for normal runs."""
        m = self._make_metrics()
        assert m.optimization_fallback_used is False

    def test_can_be_set_to_true(self):
        """optimization_fallback_used can be set to True (e.g., by workflow_service)."""
        m = self._make_metrics()
        m.optimization_fallback_used = True
        assert m.optimization_fallback_used is True

    def test_old_records_without_field_round_trip(self):
        """JSONL records written before C4 (no optimization_fallback_used key)
        must still deserialize correctly — the default of False is applied."""
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        old_record = {
            "workflow_id": "wf-old",
            "url": "https://example.com",
            "total_llm_calls": 5,
            "total_cost": 0.02,
            "execution_time": 3.0,
            "timestamp": datetime.now().isoformat(),
            # optimization_fallback_used is absent — simulates a pre-C4 record
        }
        m = WorkflowMetrics.from_dict(old_record)
        assert m.optimization_fallback_used is False


# ===================================================================
# identify_s phase instrumentation (2026-07-26 efficiency check)
# ===================================================================

class TestPhaseTimings:
    """The model sets extra='ignore', so an undeclared key is dropped silently —
    no error, just an empty CSV column six steps downstream. These are the guard.
    """

    def _make_metrics(self, **kwargs):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        defaults = {
            "workflow_id": "wf-1",
            "url": "https://example.com",
            "total_llm_calls": 5,
            "total_cost": 0.0586,
            "execution_time": 20.76,
            "timestamp": datetime.now(),
        }
        defaults.update(kwargs)
        return WorkflowMetrics(**defaults)

    def test_phase_timings_survives_round_trip(self):
        """extra='ignore' silently drops undeclared keys — this is the guard."""
        timings = {
            "submit_s": 0.05, "queue_s": 0.01, "session_setup_s": 3.2,
            "agent_setup_s": 0.4, "agent_run_s": 20.76, "postprocess_s": 0.9,
            "poll_wait_s": 4.37,
        }
        diagnostics = {
            "agent_steps": 7, "dom_elements_max": 2143, "dom_elements_median": 1876,
            "llm_429_count": 2, "retry_lost_s": 3.4,
        }
        m = self._make_metrics(
            phase_timings=timings, agent_diagnostics=diagnostics
        )
        data = m.to_dict()
        assert data["phase_timings"] == timings
        assert data["agent_diagnostics"] == diagnostics

    def test_phase_timings_defaults_to_none_when_absent(self):
        """Older rows and failed runs carry no timings — must not raise."""
        m = self._make_metrics(
            workflow_id="wf-2", url=None, total_llm_calls=0,
            total_cost=0.0, execution_time=0.0,
        )
        assert m.phase_timings is None
        assert m.agent_diagnostics is None
        assert m.to_dict()["phase_timings"] is None

    def test_a_none_span_does_not_discard_the_whole_metrics_row(self):
        """The browser service is versioned separately and already uses None
        for "not measured" (llm_coverage_gap). Dict[str, float] raised
        ValidationError on such a span — and because WorkflowMetrics is built
        inside a try/except that swallows it, the run lost cost, tokens and
        element counts too, not just the timings."""
        m = self._make_metrics(phase_timings={"queue_s": None, "agent_run_s": 20.7})
        assert m.phase_timings["queue_s"] is None
        assert m.phase_timings["agent_run_s"] == 20.7

    def test_the_response_model_carries_both_new_dicts(self):
        """WorkflowMetricsResponse inherits both fields, but
        from_workflow_metrics enumerates fields explicitly — omitting them
        made GET /api/workflow-metrics/ always return null for a field the
        schema advertises."""
        from src.backend.core.models.workflow_metrics_models import WorkflowMetricsResponse

        timings = {"submit_s": 0.05, "agent_run_s": 20.76}
        diagnostics = {"llm_calls_actual": 6, "llm_coverage_gap": None}
        m = self._make_metrics(phase_timings=timings, agent_diagnostics=diagnostics)
        resp = WorkflowMetricsResponse.from_workflow_metrics(m)
        assert resp.phase_timings == timings
        assert resp.agent_diagnostics == diagnostics


# ---------------------------------------------------------------------------
# Run-shape fields: workflow duration, dryrun gate outcome, per-stage crew
# metrics and guardrail attempts.
#
# These carry the signal a Grafana dashboard needs straight into the
# workflow_metrics row rather than into a parallel log stream. workflow_metrics
# is already org-scoped and already detached by bench/run_bench.py, so both
# tenancy and bench isolation come for free — a second sink would have to
# re-solve them.
# ---------------------------------------------------------------------------

class TestModelAttribution:
    """A cost figure is not interpretable without the model that produced it.

    llm_traces.model recovers the model NAME, but LiteLLM strips the provider
    prefix before the success callback sees it — archived rows read
    'gemini-3.5-flash', never 'vertex_ai/gemini-3.5-flash'. The provider is
    therefore recoverable from nowhere in Postgres unless this row carries it.
    """

    def _valid_data(self, **overrides):
        data = {
            "workflow_id": "wf-model",
            "url": None,
            "total_llm_calls": 0,
            "total_cost": 0.0,
            "execution_time": 1.0,
            "timestamp": datetime.now(),
        }
        data.update(overrides)
        return data

    def test_model_fields_round_trip(self):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics

        m = WorkflowMetrics(**self._valid_data(
            model_provider="vertex", model_name="gemini-3.5-flash"))
        data = m.to_dict()

        assert data["model_provider"] == "vertex"
        assert data["model_name"] == "gemini-3.5-flash"

    def test_model_fields_absent_on_older_rows(self):
        """Rows written before these fields existed must still deserialize."""
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics

        m = WorkflowMetrics(**self._valid_data())

        assert m.model_provider is None
        assert m.model_name is None


class TestRunShapeFields:
    """duration / dryrun / crew_stage_metrics / guardrail_attempts."""

    def _make_metrics(self, **kwargs):
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        defaults = {
            "workflow_id": "wf-run-shape",
            "url": "https://example.com",
            "total_llm_calls": 5,
            "total_cost": 0.0586,
            "execution_time": 20.76,
            "timestamp": datetime.now(),
        }
        defaults.update(kwargs)
        return WorkflowMetrics(**defaults)

    def test_all_run_shape_fields_survive_the_round_trip(self):
        """extra='ignore' silently drops undeclared keys — this is the guard."""
        stages = {
            "planner": {"duration_s": 4.1, "llm_calls": 1, "prompt_tokens": 3200,
                        "completion_tokens": 410, "tokens": 3610, "cost": 0.0021},
            "assembler": {"duration_s": 9.8, "llm_calls": 2, "prompt_tokens": 8100,
                          "completion_tokens": 1220, "tokens": 9320, "cost": 0.0074},
        }
        guardrails = {"assembly_output": 1, "repair_output": 2}
        m = self._make_metrics(
            workflow_duration_s=41.2, dryrun_status="passed",
            dryrun_attempts=2, dryrun_repairs=1,
            crew_stage_metrics=stages, guardrail_attempts=guardrails,
        )
        data = m.to_dict()
        assert data["workflow_duration_s"] == 41.2
        assert data["dryrun_status"] == "passed"
        assert data["dryrun_attempts"] == 2
        assert data["dryrun_repairs"] == 1
        assert data["crew_stage_metrics"] == stages
        assert data["guardrail_attempts"] == guardrails

    def test_historical_rows_without_the_fields_still_deserialize(self):
        """Every row written before this change lacks all six keys. from_dict
        must keep loading them or the History page and the metrics API lose
        their entire back-catalogue."""
        from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
        legacy = {
            "workflow_id": "wf-legacy", "url": None, "total_llm_calls": 5,
            "total_cost": 0.05, "execution_time": 20.0,
            "timestamp": datetime.now().isoformat(),
        }
        m = WorkflowMetrics.from_dict(legacy)
        assert m.workflow_duration_s is None
        assert m.dryrun_status is None
        assert m.dryrun_attempts is None
        assert m.dryrun_repairs is None
        assert m.crew_stage_metrics is None
        assert m.guardrail_attempts is None

    def test_workflow_duration_is_not_the_browser_use_execution_time(self):
        """execution_time is browser_metrics['execution_time'] — the browser-use
        figure only (workflow_service passes it straight through). The wall time
        of the whole run had no home before this field, so the two must stay
        independently settable."""
        m = self._make_metrics(execution_time=20.76, workflow_duration_s=41.2)
        assert m.execution_time == 20.76
        assert m.workflow_duration_s == 41.2

    def test_the_response_model_carries_the_run_shape_fields(self):
        """from_workflow_metrics enumerates fields by hand, so inheriting them
        on the response model is not enough — the same trap the phase_timings
        test above guards."""
        from src.backend.core.models.workflow_metrics_models import WorkflowMetricsResponse

        stages = {"planner": {"duration_s": 4.1, "cost": 0.0021}}
        m = self._make_metrics(
            workflow_duration_s=41.2, dryrun_status="failed",
            dryrun_attempts=3, dryrun_repairs=2,
            crew_stage_metrics=stages, guardrail_attempts={"assembly_output": 1},
        )
        resp = WorkflowMetricsResponse.from_workflow_metrics(m)
        assert resp.workflow_duration_s == 41.2
        assert resp.dryrun_status == "failed"
        assert resp.dryrun_attempts == 3
        assert resp.dryrun_repairs == 2
        assert resp.crew_stage_metrics == stages
        assert resp.guardrail_attempts == {"assembly_output": 1}

    def test_a_none_inside_a_stage_does_not_discard_the_whole_row(self):
        """Same reasoning as phase_timings: WorkflowMetrics is built inside a
        try/except that swallows ValidationError, so a strict value type would
        silently cost the run its cost, tokens and element counts too."""
        m = self._make_metrics(
            crew_stage_metrics={"planner": {"duration_s": None, "cost": 0.0}},
        )
        assert m.crew_stage_metrics["planner"]["duration_s"] is None
