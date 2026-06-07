"""
Unit tests for the dryrun repair mini-crew (dryrun_service.repair_robot_code).

The repair step builds a FRESH single-agent Assembler crew (NOT the main crew, NOT
in-crew delegation) and reads the corrected code directly from crew.tasks[0].output.

Covered (§5 / §8.5):
  - single assembler agent with allow_delegation=False (no delegation tools — a lone
    sequential-crew agent never gets them; this is defensive hygiene)
  - reads crew.tasks[0].output directly (no delegation round-trip)
  - repair-crew hygiene: output_log_file=None, NO step/task callbacks, not registered
  - the fix_task description carries the dryrun error text (conservative repair input)
  - returns (task_output, usage_dict) where usage_dict has the calculate_crewai_cost shape
"""

import types
from unittest.mock import patch, MagicMock

import src.backend.services.dryrun_service as ds


def _fake_library_context():
    """Minimal stand-in for a LibraryContext — only the attributes RobotTasks
    and RobotAgents read during construction."""
    return types.SimpleNamespace(
        library_name="Browser",
        planning_context="PLANNING CONTEXT",
        code_assembly_context="CODE ASSEMBLY CONTEXT",
        planning_rules="PLANNING RULES",
        browser_init_params={"browser": "chromium", "headless": "True"},
        requires_viewport_config=False,
    )


def _mock_crew_cls():
    """A patched crewai.Crew whose instance exposes tasks[0].output + usage."""
    mock_cls = MagicMock()
    crew = mock_cls.return_value
    task = MagicMock()
    task.output = MagicMock(name="repaired_task_output")
    crew.tasks = [task]
    usage = MagicMock(total_tokens=120, prompt_tokens=90,
                      completion_tokens=30, successful_requests=2,
                      total_cost=0.003)
    crew.calculate_usage_metrics.return_value = usage
    return mock_cls, crew, task


def _run_repair(errors="No keyword with name 'Cilck' found. Did you mean: Browser.Click"):
    mock_cls, crew, task = _mock_crew_cls()
    with patch("crewai.Crew", mock_cls), \
         patch("src.backend.crew_ai.library_context.get_library_context",
               return_value=_fake_library_context()):
        out, usage = ds.repair_robot_code(
            "rid", "*** Settings ***\nLibrary    Browser\n", errors,
            "gemini", "gemini-2.5-flash",
        )
    return out, usage, mock_cls, crew, task


class TestRepairCrew:
    def test_returns_task_output_and_usage(self):
        out, usage, mock_cls, crew, task = _run_repair()
        # Result read DIRECTLY from crew.tasks[0].output (no delegation).
        assert out is task.output
        # usage dict in calculate_crewai_cost shape (folds into crewai_* in Step 5).
        for k in ("llm_calls", "cost", "tokens", "prompt_tokens", "completion_tokens"):
            assert k in usage
        assert usage["llm_calls"] == 2
        assert usage["cost"] == 0.003  # LiteLLM-computed total_cost passthrough

    def test_single_agent_no_delegation(self):
        out, usage, mock_cls, crew, task = _run_repair()
        kwargs = mock_cls.call_args.kwargs
        agents = kwargs["agents"]
        assert len(agents) == 1
        assert agents[0].allow_delegation is False

    def test_repair_agent_max_iter_bounded(self):
        from src.backend.core.config import settings
        out, usage, mock_cls, crew, task = _run_repair()
        agents = mock_cls.call_args.kwargs["agents"]
        assert agents[0].max_iter == settings.MAX_AGENT_ITERATIONS

    def test_crew_hygiene_no_callbacks_no_logfile(self):
        out, usage, mock_cls, crew, task = _run_repair()
        kwargs = mock_cls.call_args.kwargs
        # §8.5 — no extra crewai.log noise, no callbacks (not registered in progress_events)
        assert kwargs.get("output_log_file") is None
        assert "step_callback" not in kwargs
        assert "task_callback" not in kwargs

    def test_fix_task_carries_dryrun_errors(self):
        errors = "No keyword with name 'Cilck' found. Did you mean: Browser.Click"
        out, usage, mock_cls, crew, task = _run_repair(errors=errors)
        tasks = mock_cls.call_args.kwargs["tasks"]
        assert len(tasks) == 1
        # Conservative repair: the exact RF error (incl. the suggestion) is in the prompt.
        assert errors in tasks[0].description
        # And the conservative-repair guardrails are present.
        assert "verbatim" in tasks[0].description.lower()

    def test_kickoff_called_once(self):
        out, usage, mock_cls, crew, task = _run_repair()
        crew.kickoff.assert_called_once()
