"""
Unit tests for run_crew() in src.backend.crew_ai.crew.

Tests the non-optimization code path (OPTIMIZATION_ENABLED=False) by mocking
all heavy dependencies (Crew, RobotAgents, RobotTasks).  The optimization path
is not tested here — it lives in test_optimization/.

Coverage targets:
  - run_crew() success path: 5-tuple return structure
  - run_crew() exception path: LLMOutputCleaner.is_formatting_error detection
  - run_crew() hint_metadata is empty when optimization is disabled
  - run_crew() progress_queue=None skips workflow registration
  - run_crew() progress_queue provided triggers registration/unregistration
"""

import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agents_mock():
    """Build a minimal RobotAgents mock instance."""
    agents = MagicMock()
    agents.step_planner_agent.return_value = MagicMock()
    agents.element_identifier_agent.return_value = MagicMock()
    agents.code_assembler_agent.return_value = MagicMock()
    agents.llm._monitor.get_stats.return_value = {"calls": 5, "errors": 0}
    agents.llm._monitor.log_formatting_error = MagicMock()
    return agents


def _run_crew_with_mocks(optimization_enabled=False, kickoff_result=None,
                          kickoff_side_effect=None, agents_mock=None,
                          progress_queue=None):
    """
    Call run_crew() with all heavy dependencies mocked.

    Returns (result_tuple, mock_crew_cls, agents_mock) so callers can
    make assertions on the mocks.
    """
    if agents_mock is None:
        agents_mock = _make_agents_mock()

    mock_settings = MagicMock()
    mock_settings.OPTIMIZATION_ENABLED = optimization_enabled
    mock_settings.ROBOT_LIBRARY = "browser"
    mock_settings.MAX_AGENT_ITERATIONS = 3

    with ExitStack() as stack:
        mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
        stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                  return_value=agents_mock))
        stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks"))
        stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                  return_value=(MagicMock(), MagicMock())))
        stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
        stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                  return_value=MagicMock(library_name="Browser")))
        stack.enter_context(patch("src.backend.core.config.settings", mock_settings))

        if kickoff_side_effect is not None:
            mock_crew_cls.return_value.kickoff.side_effect = kickoff_side_effect
        else:
            mock_crew_cls.return_value.kickoff.return_value = (
                kickoff_result if kickoff_result is not None else MagicMock()
            )

        from src.backend.crew_ai.crew import run_crew
        result = run_crew(
            "search on github.com", "gemini", "gemini-2.5-flash",
            progress_queue=progress_queue
        )

    return result, mock_crew_cls, agents_mock


# ---------------------------------------------------------------------------
# Tests: success path return shape
# ---------------------------------------------------------------------------

class TestRunCrewReturnShape:
    """run_crew() must return a 5-tuple with the correct element types."""

    def test_returns_five_tuple(self):
        """Success path: result is a 5-element tuple."""
        result, _, _ = _run_crew_with_mocks()
        assert isinstance(result, tuple)
        assert len(result) == 5

    def test_first_element_is_kickoff_result(self):
        """result[0] is exactly what crew.kickoff() returned."""
        kickoff_result = MagicMock(name="kickoff_result")
        out, _, _ = _run_crew_with_mocks(kickoff_result=kickoff_result)
        assert out[0] is kickoff_result

    def test_third_element_is_none_when_optimization_disabled(self):
        """optimization_metrics (index 2) is None when OPTIMIZATION_ENABLED=False."""
        out, _, _ = _run_crew_with_mocks(optimization_enabled=False)
        assert out[2] is None

    def test_fourth_element_is_empty_dict_when_optimization_disabled(self):
        """hint_metadata (index 3) is {} when optimization is off."""
        out, _, _ = _run_crew_with_mocks(optimization_enabled=False)
        assert out[3] == {}

    def test_fifth_element_is_llm_monitor(self):
        """result[4] is agents.llm._monitor."""
        agents_mock = _make_agents_mock()
        out, _, _ = _run_crew_with_mocks(agents_mock=agents_mock)
        assert out[4] is agents_mock.llm._monitor

    def test_kickoff_called_exactly_once(self):
        """crew.kickoff() is invoked exactly once per run_crew() call."""
        _, mock_crew_cls, _ = _run_crew_with_mocks()
        mock_crew_cls.return_value.kickoff.assert_called_once()

    def test_log_rotation_called_before_kickoff(self):
        """_rotate_crewai_log() must fire before crew.kickoff()."""
        call_order = []

        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3
        agents_mock = _make_agents_mock()

        with ExitStack() as stack:
            mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks"))
            stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                      return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                      return_value=MagicMock(library_name="Browser")))
            stack.enter_context(patch("src.backend.core.config.settings", mock_settings))
            stack.enter_context(
                patch("src.backend.crew_ai.crew._rotate_crewai_log",
                      side_effect=lambda: call_order.append("rotate"))
            )
            mock_crew_cls.return_value.kickoff.side_effect = lambda: (
                call_order.append("kickoff") or MagicMock()
            )

            from src.backend.crew_ai.crew import run_crew
            run_crew("test query", "gemini", "gemini-2.5-flash")

        assert call_order == ["rotate", "kickoff"]


# ---------------------------------------------------------------------------
# Tests: exception path
# ---------------------------------------------------------------------------

class TestRunCrewExceptionHandling:
    """run_crew() re-raises after logging; formatting errors are flagged."""

    def test_non_formatting_exception_is_reraised(self):
        """A RuntimeError from kickoff() bubbles up."""
        with pytest.raises(RuntimeError, match="network timeout"):
            _run_crew_with_mocks(kickoff_side_effect=RuntimeError("network timeout"))

    def test_formatting_error_logs_and_reraises(self):
        """A formatting error triggers log_formatting_error(was_recovered=False) then re-raises."""
        formatting_err = "Action Input is not valid JSON: unexpected character"
        agents_mock = _make_agents_mock()

        with pytest.raises(Exception, match="not valid JSON"):
            _run_crew_with_mocks(
                kickoff_side_effect=Exception(formatting_err),
                agents_mock=agents_mock,
            )

        agents_mock.llm._monitor.log_formatting_error.assert_called_once_with(was_recovered=False)

    def test_generic_exception_does_not_call_log_formatting_error(self):
        """A plain exception does NOT call log_formatting_error."""
        agents_mock = _make_agents_mock()

        with pytest.raises(RuntimeError):
            _run_crew_with_mocks(
                kickoff_side_effect=RuntimeError("plain error"),
                agents_mock=agents_mock,
            )

        agents_mock.llm._monitor.log_formatting_error.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: progress queue registration
# ---------------------------------------------------------------------------

class TestRunCrewProgressQueue:
    """register_workflow / unregister_workflow are called iff progress_queue is provided."""

    def test_no_progress_queue_skips_registration(self):
        """register_workflow is NOT called when progress_queue=None."""
        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3
        agents_mock = _make_agents_mock()

        with ExitStack() as stack:
            mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks"))
            stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                      return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
            stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                      return_value=MagicMock(library_name="Browser")))
            stack.enter_context(patch("src.backend.core.config.settings", mock_settings))
            mock_reg = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.register_workflow")
            )
            mock_crew_cls.return_value.kickoff.return_value = MagicMock()

            from src.backend.crew_ai.crew import run_crew
            run_crew("test query", "gemini", "gemini-2.5-flash", progress_queue=None)

        mock_reg.assert_not_called()

    def test_progress_queue_triggers_registration_and_unregistration(self):
        """When progress_queue is provided, register and unregister are both called."""
        from queue import Queue
        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3
        agents_mock = _make_agents_mock()

        with ExitStack() as stack:
            mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks"))
            stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                      return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
            stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                      return_value=MagicMock(library_name="Browser")))
            stack.enter_context(patch("src.backend.core.config.settings", mock_settings))
            mock_reg = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.register_workflow")
            )
            mock_unreg = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.unregister_workflow")
            )
            mock_crew_cls.return_value.kickoff.return_value = MagicMock()

            from src.backend.crew_ai.crew import run_crew
            run_crew("test query", "gemini", "gemini-2.5-flash", progress_queue=Queue())

        mock_reg.assert_called_once()
        mock_unreg.assert_called_once()

    def test_run_crew_hint_metadata_agents_subkey_sums_correctly(self):
        """Regression (Finding 2 / F-10): agent dicts live under hint_metadata["agents"];
        nl_injected_ids is a sibling key. Iterating agents.values() needs no
        isinstance guard because all values are {count, available, sources} dicts."""
        hint_metadata = {
            "agents": {
                "planner":   {"count": 2, "available": 5, "sources": ("nl",)},
                "assembler": {"count": 1, "available": 3, "sources": ("nl",)},
            },
            "nl_injected_ids": [5, 12],
        }
        total = sum(r["count"] for r in hint_metadata["agents"].values())
        assert total == 3
        assert hint_metadata["nl_injected_ids"] == [5, 12]

    def test_run_crew_optimization_path_does_not_raise_with_nl_ids(self):
        """Mutation guard (Finding 2 / F-10): exercises the hint_metadata["agents"]
        structure through the actual run_crew production code path with
        OPTIMIZATION_ENABLED=True.

        The companion test above checks a local dict copy; this one calls run_crew
        so that reverting to a flat hint_metadata shape (no "agents" sub-key) would
        make this test fail with a KeyError or wrong count instead of silently passing."""
        from src.backend.crew_ai.optimization.smart_keyword_provider import AgentContextResult

        agents_mock = _make_agents_mock()
        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = True
        mock_settings.OPTIMIZATION_CONTEXT_PRUNING_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3

        fake_result = AgentContextResult(
            context="ctx",
            hints_count=2,
            hints_available=5,
            hint_sources=("nl",),
            hint_text="hint",
            nl_injected_ids=(5, 12),
        )
        mock_provider = MagicMock()
        mock_provider.get_agent_context.return_value = fake_result
        mock_provider.get_keyword_search_tool.return_value = None

        with ExitStack() as stack:
            stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks"))
            stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                      return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
            stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                      return_value=MagicMock(library_name="Browser")))
            stack.enter_context(patch("src.backend.core.config.settings", mock_settings))
            stack.enter_context(patch("src.backend.crew_ai.crew.count_tokens", return_value=100))
            stack.enter_context(patch(
                "src.backend.crew_ai.optimization.learning_registry.get_feedback_loop",
                return_value=None,
            ))
            stack.enter_context(patch("src.backend.crew_ai.optimization.KeywordVectorStore"))
            stack.enter_context(patch("src.backend.crew_ai.optimization.QueryPatternMatcher"))
            stack.enter_context(patch("src.backend.crew_ai.optimization.SmartKeywordProvider",
                                      return_value=mock_provider))
            stack.enter_context(patch("src.backend.crew_ai.optimization.ContextPruner"))

            from src.backend.crew_ai.crew import run_crew
            out = run_crew("search on example.com", "gemini", "gemini-2.5-flash")

        hint_metadata = out[3]
        # nl_injected_ids is the sorted union across both agents: {5,12}|{5,12}
        assert hint_metadata["nl_injected_ids"] == [5, 12]
        # Agent dicts live under hint_metadata["agents"] — no isinstance guard needed.
        # Crew is now a 2-agent context pipeline (planner + assembler); the
        # validator agent was removed in favour of the dryrun gate.
        total = sum(r["count"] for r in hint_metadata["agents"].values())
        assert total == 4  # 2 agents × hints_count=2

    def test_unregister_called_even_when_kickoff_raises(self):
        """unregister_workflow() is in the finally block — fires on exception too."""
        from queue import Queue
        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3
        agents_mock = _make_agents_mock()

        with ExitStack() as stack:
            mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks"))
            stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                      return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
            stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                      return_value=MagicMock(library_name="Browser")))
            stack.enter_context(patch("src.backend.core.config.settings", mock_settings))
            stack.enter_context(
                patch("src.backend.crew_ai.progress_events.register_workflow")
            )
            mock_unreg = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.unregister_workflow")
            )
            mock_crew_cls.return_value.kickoff.side_effect = RuntimeError("boom")

            from src.backend.crew_ai.crew import run_crew
            with pytest.raises(RuntimeError):
                run_crew("test query", "gemini", "gemini-2.5-flash", progress_queue=Queue())

        mock_unreg.assert_called_once()
