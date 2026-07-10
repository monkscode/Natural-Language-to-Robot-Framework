"""
Unit tests for run_crew() in src.backend.crew_ai.crew.

Tests the non-optimization code path (OPTIMIZATION_ENABLED=False) by mocking
all heavy dependencies (Crew, RobotAgents, RobotTasks, identify_elements).
The optimization path is not tested here — it lives in test_optimization/.

Task 16 pipeline: run_crew is now TWO single-task kickoffs (planner crew →
deterministic element stage → assembler crew). The element-identifier LLM
agent is gone; identify_elements() (element_identification.py) runs between
the kickoffs and the assembler task embeds its merged steps.

Coverage targets:
  - run_crew() success path: 5-tuple return structure; result[1] is the
    ASSEMBLER crew (workflow_service reads tasks[-1] + usage metrics from it)
  - two Crew instantiations, one kickoff each
  - identify_elements receives the plan steps and the user query VERBATIM
  - assemble_code_task receives the merged steps as JSON
  - run_crew() exception path: LLMOutputCleaner.is_formatting_error detection
  - run_crew() hint_metadata is empty when optimization is disabled
  - run_crew() progress_queue=None skips workflow registration
  - progress_queue provided → register_workflow (planner, index 0) +
    register_task (assembler, index 2) + unregistration
"""

import json
import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDENTIFICATION_RESULT = {
    "steps": [
        {"keyword": "Open Browser", "value": "https://github.com"},
        {"keyword": "Input Text", "element_description": "search box",
         "value": "shoes", "locator": "name=q", "found": True},
    ],
    "summary": {"total_elements": 1, "found": 1, "not_found": 0},
}


def _make_agents_mock():
    """Build a minimal RobotAgents mock instance."""
    agents = MagicMock()
    agents.step_planner_agent.return_value = MagicMock()
    agents.code_assembler_agent.return_value = MagicMock()
    agents.llm._monitor.get_stats.return_value = {"calls": 5, "errors": 0}
    agents.llm._monitor.log_formatting_error = MagicMock()
    return agents


def _make_tasks_mock():
    """RobotTasks mock whose task objects carry distinct ids."""
    tasks = MagicMock()
    plan_task = MagicMock(name="plan_task")
    plan_task.id = "plan-task-id"
    # run_crew extracts the plan steps from the task output's pydantic model.
    plan_task.output.pydantic.steps = [
        {"keyword": "Open Browser", "value": "https://github.com"},
        {"keyword": "Input Text", "element_description": "search box", "value": "shoes"},
    ]
    assemble_task = MagicMock(name="assemble_task")
    assemble_task.id = "assemble-task-id"
    tasks.plan_steps_task.return_value = plan_task
    tasks.assemble_code_task.return_value = assemble_task
    return tasks, plan_task, assemble_task


def _run_crew_with_mocks(optimization_enabled=False, kickoff_result=None,
                          kickoff_side_effect=None, agents_mock=None,
                          progress_queue=None, identify_side_effect=None):
    """
    Call run_crew() with all heavy dependencies mocked.

    Returns (result_tuple, mocks dict) so callers can make assertions.
    """
    if agents_mock is None:
        agents_mock = _make_agents_mock()

    tasks_mock, plan_task, assemble_task = _make_tasks_mock()

    mock_settings = MagicMock()
    mock_settings.OPTIMIZATION_ENABLED = optimization_enabled
    mock_settings.ROBOT_LIBRARY = "browser"
    mock_settings.MAX_AGENT_ITERATIONS = 3

    with ExitStack() as stack:
        mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
        stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                  return_value=agents_mock))
        stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks",
                                  return_value=tasks_mock))
        mock_identify = stack.enter_context(
            patch("src.backend.crew_ai.crew.identify_elements",
                  return_value=_IDENTIFICATION_RESULT,
                  side_effect=identify_side_effect))
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

    mocks = {
        "crew_cls": mock_crew_cls,
        "agents": agents_mock,
        "tasks": tasks_mock,
        "plan_task": plan_task,
        "assemble_task": assemble_task,
        "identify": mock_identify,
    }
    return result, mocks


# ---------------------------------------------------------------------------
# Tests: success path return shape
# ---------------------------------------------------------------------------

class TestRunCrewReturnShape:
    """run_crew() must return a 5-tuple with the correct element types."""

    def test_returns_five_tuple(self):
        """Success path: result is a 5-element tuple."""
        result, _ = _run_crew_with_mocks()
        assert isinstance(result, tuple)
        assert len(result) == 5

    def test_first_element_is_assembler_kickoff_result(self):
        """result[0] is what the assembler crew's kickoff() returned."""
        kickoff_result = MagicMock(name="kickoff_result")
        out, _ = _run_crew_with_mocks(kickoff_result=kickoff_result)
        assert out[0] is kickoff_result

    def test_second_element_is_the_assembler_crew(self):
        """result[1] is a Crew instance whose tasks[-1] is the assemble task
        (workflow_service extracts delivered code from tasks[-1].output)."""
        out, mocks = _run_crew_with_mocks()
        assert out[1] is mocks["crew_cls"].return_value
        # The assembler crew was constructed with tasks=[assemble_task]
        last_ctor_kwargs = mocks["crew_cls"].call_args_list[-1].kwargs
        assert last_ctor_kwargs["tasks"] == [mocks["assemble_task"]]

    def test_third_element_is_none_when_optimization_disabled(self):
        """optimization_metrics (index 2) is None when OPTIMIZATION_ENABLED=False."""
        out, _ = _run_crew_with_mocks(optimization_enabled=False)
        assert out[2] is None

    def test_fourth_element_is_empty_dict_when_optimization_disabled(self):
        """hint_metadata (index 3) is {} when optimization is off."""
        out, _ = _run_crew_with_mocks(optimization_enabled=False)
        assert out[3] == {}

    def test_fifth_element_is_llm_monitor(self):
        """result[4] is agents.llm._monitor."""
        agents_mock = _make_agents_mock()
        out, _ = _run_crew_with_mocks(agents_mock=agents_mock)
        assert out[4] is agents_mock.llm._monitor

    def test_two_crews_one_kickoff_each(self):
        """Task 16: planner crew + assembler crew, each kicked off once."""
        _, mocks = _run_crew_with_mocks()
        assert mocks["crew_cls"].call_count == 2
        assert mocks["crew_cls"].return_value.kickoff.call_count == 2

    def test_planner_crew_has_single_planner_task(self):
        _, mocks = _run_crew_with_mocks()
        first_ctor_kwargs = mocks["crew_cls"].call_args_list[0].kwargs
        assert first_ctor_kwargs["tasks"] == [mocks["plan_task"]]

    def test_log_rotation_called_before_first_kickoff(self):
        """_rotate_crewai_log() must fire before any kickoff."""
        call_order = []

        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3
        agents_mock = _make_agents_mock()
        tasks_mock, _, _ = _make_tasks_mock()

        with ExitStack() as stack:
            mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks",
                                      return_value=tasks_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.identify_elements",
                                      return_value=_IDENTIFICATION_RESULT))
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

        assert call_order == ["rotate", "kickoff", "kickoff"]


# ---------------------------------------------------------------------------
# Tests: the deterministic element stage hand-off
# ---------------------------------------------------------------------------

class TestRunCrewElementStage:

    def test_identify_elements_receives_verbatim_user_query(self):
        """Port checklist #6: user_query is the user's original query."""
        _, mocks = _run_crew_with_mocks()
        assert mocks["identify"].call_count == 1
        _, kwargs = mocks["identify"].call_args
        args, _ = mocks["identify"].call_args
        query_arg = kwargs.get("user_query", args[1] if len(args) > 1 else None)
        assert query_arg == "search on github.com"

    def test_assemble_task_receives_merged_steps_json(self):
        """The assembler task description embeds the merged steps (there is
        no CrewAI context chain across the two crews)."""
        _, mocks = _run_crew_with_mocks()
        call = mocks["tasks"].assemble_code_task.call_args
        steps_json = call.kwargs.get("identified_steps_json") or call.args[1]
        assert json.loads(steps_json) == {"steps": _IDENTIFICATION_RESULT["steps"]}

    def test_identify_elements_failure_propagates(self):
        """identify_elements itself never raises on tool errors (contract),
        so an exception out of it is a real bug — run_crew must not hide it."""
        with pytest.raises(RuntimeError, match="stage bug"):
            _run_crew_with_mocks(identify_side_effect=RuntimeError("stage bug"))


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
    """register_workflow / register_task / unregister_workflow wiring."""

    def _run(self, progress_queue, kickoff_side_effect=None):
        agents_mock = _make_agents_mock()
        tasks_mock, plan_task, assemble_task = _make_tasks_mock()
        mock_settings = MagicMock()
        mock_settings.OPTIMIZATION_ENABLED = False
        mock_settings.ROBOT_LIBRARY = "browser"
        mock_settings.MAX_AGENT_ITERATIONS = 3

        with ExitStack() as stack:
            mock_crew_cls = stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotAgents",
                                      return_value=agents_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks",
                                      return_value=tasks_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.identify_elements",
                                      return_value=_IDENTIFICATION_RESULT))
            stack.enter_context(patch("src.backend.crew_ai.crew.get_crew_callbacks",
                                      return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
            stack.enter_context(patch("src.backend.crew_ai.library_context.get_library_context",
                                      return_value=MagicMock(library_name="Browser")))
            stack.enter_context(patch("src.backend.core.config.settings", mock_settings))
            mock_reg = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.register_workflow"))
            mock_reg_task = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.register_task"))
            mock_unreg = stack.enter_context(
                patch("src.backend.crew_ai.progress_events.unregister_workflow"))
            if kickoff_side_effect is not None:
                mock_crew_cls.return_value.kickoff.side_effect = kickoff_side_effect
            else:
                mock_crew_cls.return_value.kickoff.return_value = MagicMock()

            from src.backend.crew_ai.crew import run_crew
            error = None
            try:
                run_crew("test query", "gemini", "gemini-2.5-flash",
                         workflow_id="wf-test", progress_queue=progress_queue)
            except Exception as e:  # noqa: BLE001 — assertions inspect mocks
                error = e

        return {
            "register": mock_reg, "register_task": mock_reg_task,
            "unregister": mock_unreg, "plan_task": plan_task,
            "assemble_task": assemble_task, "error": error,
        }

    def test_no_progress_queue_skips_registration(self):
        """register_workflow is NOT called when progress_queue=None."""
        out = self._run(progress_queue=None)
        out["register"].assert_not_called()
        out["register_task"].assert_not_called()

    def test_progress_queue_triggers_full_registration_lifecycle(self):
        """Planner task registered at index 0 up front; assembler task
        registered at index 2 after the element stage; unregister at the end."""
        from queue import Queue
        out = self._run(progress_queue=Queue())
        assert out["error"] is None

        out["register"].assert_called_once()
        reg_args = out["register"].call_args.args
        assert reg_args[0] == "wf-test"
        assert reg_args[2] == {"plan-task-id": 0}

        out["register_task"].assert_called_once_with("wf-test", "assemble-task-id", 2)
        out["unregister"].assert_called_once()

    def test_unregister_called_even_when_kickoff_raises(self):
        """unregister_workflow() is in the finally block — fires on exception too."""
        from queue import Queue
        out = self._run(progress_queue=Queue(),
                        kickoff_side_effect=RuntimeError("boom"))
        assert isinstance(out["error"], RuntimeError)
        out["unregister"].assert_called_once()

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
        tasks_mock, _, _ = _make_tasks_mock()
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
            stack.enter_context(patch("src.backend.crew_ai.crew.RobotTasks",
                                      return_value=tasks_mock))
            stack.enter_context(patch("src.backend.crew_ai.crew.identify_elements",
                                      return_value=_IDENTIFICATION_RESULT))
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
            # crew.py resolves get_keyword_vector_store on the optimization
            # package at call time — patching the accessor (not the class)
            # keeps the real pgvector store (DB + libdoc bootstrap) out of
            # this test and leaves the process-wide singleton untouched.
            stack.enter_context(
                patch("src.backend.crew_ai.optimization.get_keyword_vector_store"))
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
        # Crew is a 2-agent context pipeline (planner + assembler); the element
        # identifier is deterministic Python and receives no context.
        total = sum(r["count"] for r in hint_metadata["agents"].values())
        assert total == 4  # 2 agents × hints_count=2


# ---------------------------------------------------------------------------
# Tests: _extract_plan_steps — fallback-path validation (fail fast, pre-browser)
# ---------------------------------------------------------------------------

class TestExtractPlanSteps:
    """The pydantic path is CrewAI-validated; the json_dict/raw fallbacks are
    NOT — they must be re-validated HERE so a drifted plan fails before the
    ~40s browser call, never mid-merge after it."""

    @staticmethod
    def _output(pydantic=None, json_dict=None, raw=""):
        out = MagicMock()
        out.pydantic = pydantic
        out.json_dict = json_dict
        out.raw = raw
        return out

    def test_pydantic_path_returns_steps_as_is(self):
        from src.backend.crew_ai.crew import _extract_plan_steps
        from src.backend.crew_ai.tasks import PlanOutput, PlannedStep
        plan = PlanOutput(steps=[PlannedStep(step_description="open", keyword="Open Browser")])
        assert _extract_plan_steps(self._output(pydantic=plan)) == list(plan.steps)

    def test_json_dict_path_validates_and_returns_models(self):
        from src.backend.crew_ai.crew import _extract_plan_steps
        from src.backend.crew_ai.tasks import PlannedStep
        steps = _extract_plan_steps(self._output(json_dict={"steps": [
            {"step_description": "open", "keyword": "Open Browser",
             "value": "https://example.com"},
        ]}))
        assert isinstance(steps[0], PlannedStep)
        assert steps[0].keyword == "Open Browser"

    def test_json_dict_path_missing_required_field_raises_before_browser(self):
        from src.backend.crew_ai.crew import _extract_plan_steps
        with pytest.raises(ValueError, match="failed validation"):
            _extract_plan_steps(self._output(json_dict={"steps": [
                {"step_description": "no keyword here"},
            ]}))

    def test_raw_path_numeric_value_raises_before_browser(self):
        from src.backend.crew_ai.crew import _extract_plan_steps
        raw = json.dumps({"steps": [
            {"step_description": "type", "keyword": "Input Text",
             "element_description": "qty field", "value": 123},
        ]})
        with pytest.raises(ValueError, match="failed validation"):
            _extract_plan_steps(self._output(raw=raw))

    def test_raw_path_drift_keys_are_dropped_by_validation(self):
        """Hallucinated locator-contract keys never reach merge_locators."""
        from src.backend.crew_ai.crew import _extract_plan_steps
        raw = json.dumps({"steps": [
            {"step_description": "click", "keyword": "Click",
             "element_description": "save button", "locator": "id=WRONG",
             "found": True},
        ]})
        steps = _extract_plan_steps(self._output(raw=raw))
        dumped = steps[0].model_dump()
        assert "locator" not in dumped
        assert "found" not in dumped

    def test_no_parsable_steps_raises(self):
        from src.backend.crew_ai.crew import _extract_plan_steps
        with pytest.raises(ValueError, match="no parsable steps"):
            _extract_plan_steps(self._output(raw="total garbage"))
