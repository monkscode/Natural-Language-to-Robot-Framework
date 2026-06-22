"""Wiring test: authenticated user's org reaches run_crew during generation.

Task 9 fix: source org from the authenticated user (not the unrecorded run).

These tests prove the WIRING gap is closed — that org_id threads from the entry
points (stream_generate_only / stream_generate_and_run) down through
_start_workflow_thread → run_workflow_in_thread → run_agentic_workflow → run_crew.

The data-layer org-dimension isolation is already proven in
tests/test_optimization/test_provider_org_threading.py.  This file only asserts
propagation, not storage-level behaviour, so run_crew is mocked at the boundary.
"""

import os
import pytest
from queue import Queue
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# Shared stub builders — mirrors _make_run_crew_result in test_agentic_workflow.py
# ---------------------------------------------------------------------------

VALID_ROBOT = (
    "*** Settings ***\nLibrary    Browser\n\n"
    "*** Test Cases ***\nLogin\n    New Page    https://example.com\n"
)


def _make_crew_result():
    task2 = MagicMock()
    task2.output.pydantic = None
    task2.output.json_dict = None
    task2.output.raw = VALID_ROBOT

    crew = MagicMock()
    crew.tasks = [MagicMock(), MagicMock(), task2]
    usage = MagicMock(
        total_tokens=100, prompt_tokens=80,
        completion_tokens=20, successful_requests=4,
    )
    crew.calculate_usage_metrics.return_value = usage

    llm_monitor = MagicMock()
    llm_monitor.get_numeric_stats.return_value = {}
    return (MagicMock(), crew, None, {}, llm_monitor)


def _passthrough_gate(workflow_id, code, *args, **kwargs):
    return {"code": code, "dryrun_status": "passed", "repair_usage": {}}


# ---------------------------------------------------------------------------
# Wiring: run_agentic_workflow → run_crew
# ---------------------------------------------------------------------------

class TestRunAgenticWorkflowOrgPropagation:
    """run_agentic_workflow passes org_id into run_crew."""

    def _run(self, **extra_kwargs):
        """Drive run_agentic_workflow with standard mocks; return (events, mock_run_crew)."""
        mock_run_crew = MagicMock(return_value=_make_crew_result())
        with patch("src.backend.services.workflow_service.run_crew", mock_run_crew), \
             patch("src.backend.services.workflow_service.validate_and_repair",
                   side_effect=_passthrough_gate), \
             patch("src.backend.services.workflow_service.get_temp_metrics_storage") as ms, \
             patch("src.backend.services.workflow_service.get_workflow_metrics_collector"), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            ms.return_value.read_browser_metrics.return_value = {}
            from src.backend.services.workflow_service import run_agentic_workflow
            events = list(run_agentic_workflow(
                "login to app", "gemini", "gemini-2.5-flash", **extra_kwargs
            ))
        return events, mock_run_crew

    def test_org_id_forwarded_to_run_crew(self):
        """run_crew receives the org_id passed to run_agentic_workflow."""
        _, mock_run_crew = self._run(org_id="org-B")
        assert mock_run_crew.called, "run_crew was never called"
        _, kwargs = mock_run_crew.call_args
        assert kwargs.get("org_id") == "org-B", (
            f"expected org_id='org-B', got org_id={kwargs.get('org_id')!r}"
        )

    def test_none_org_id_forwarded_to_run_crew(self):
        """Coexistence: no org_id (legacy) → run_crew receives org_id=None."""
        _, mock_run_crew = self._run()
        assert mock_run_crew.called
        _, kwargs = mock_run_crew.call_args
        assert kwargs.get("org_id") is None, (
            f"expected org_id=None for legacy caller, got {kwargs.get('org_id')!r}"
        )
