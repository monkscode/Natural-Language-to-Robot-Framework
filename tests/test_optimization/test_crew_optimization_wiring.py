"""run_crew optimization-path wiring: the planner retrieves HINTS ONLY.

The planner agent runs on static minimal context by design
(RobotAgents.step_planner_agent) — there is no planner context slot, so a
Tier-1/2 context string built for it is dead weight: a vector search plus
keyword-doc fetches per production run for a string nothing ships, and a
misleading "Context sizes: Planner=N" log (2026-07-16 finding). run_crew
must request the planner's learning hints with hints_only=True, pass no
planner context anywhere, and still route the planner's hint_text into
RobotTasks(hint_context=...).

All heavy dependencies are mocked at their seams (test_crew_run.py
pattern); no live Postgres is needed.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from src.backend.crew_ai.optimization.smart_keyword_provider import (
    AgentContextResult,
)


_IDENTIFICATION_RESULT = {
    "steps": [{"keyword": "Open Browser", "value": "https://example.com"}],
    "summary": {"total_elements": 0, "found": 0, "not_found": 0},
}


def _provider_mock():
    """SmartKeywordProvider mock returning role-appropriate results."""
    provider = MagicMock(name="smart_provider")
    provider.was_holdout = False

    def _ctx(query, agent_role, url=None, hints_only=False):
        if agent_role == "planner":
            return AgentContextResult(
                context="", hints_count=1, hints_available=1,
                hint_sources=("structural",), hint_text="PLANNER_HINT")
        return AgentContextResult(context="ASSEMBLER_CTX")

    provider.get_agent_context.side_effect = _ctx
    return provider


def _run_crew_optimized(provider):
    """Call run_crew() on the optimization path with all seams mocked.

    Returns (RobotAgents class mock, RobotTasks class mock).
    """
    agents_mock = MagicMock()
    agents_mock.llm._monitor.get_stats.return_value = {}
    tasks_mock = MagicMock()
    plan_task = MagicMock(name="plan_task")
    plan_task.id = "plan-task-id"
    plan_task.output.pydantic.steps = [
        {"keyword": "Open Browser", "value": "https://example.com"}]
    tasks_mock.plan_steps_task.return_value = plan_task
    assemble_task = MagicMock(name="assemble_task")
    assemble_task.id = "assemble-task-id"
    tasks_mock.assemble_code_task.return_value = assemble_task

    mock_settings = MagicMock()
    mock_settings.OPTIMIZATION_ENABLED = True
    mock_settings.OPTIMIZATION_CONTEXT_PRUNING_ENABLED = False
    mock_settings.ROBOT_LIBRARY = "browser"

    import src.backend.crew_ai.optimization as opt

    with ExitStack() as stack:
        stack.enter_context(patch("src.backend.crew_ai.crew.Crew"))
        robot_agents_cls = stack.enter_context(patch(
            "src.backend.crew_ai.crew.RobotAgents", return_value=agents_mock))
        robot_tasks_cls = stack.enter_context(patch(
            "src.backend.crew_ai.crew.RobotTasks", return_value=tasks_mock))
        stack.enter_context(patch(
            "src.backend.crew_ai.crew.identify_elements",
            return_value=_IDENTIFICATION_RESULT))
        stack.enter_context(patch(
            "src.backend.crew_ai.crew.get_crew_callbacks",
            return_value=(MagicMock(), MagicMock())))
        stack.enter_context(patch("src.backend.crew_ai.crew._rotate_crewai_log"))
        stack.enter_context(patch(
            "src.backend.crew_ai.library_context.get_library_context",
            return_value=MagicMock(library_name="Browser")))
        stack.enter_context(patch("src.backend.core.config.settings",
                                  mock_settings))
        stack.enter_context(patch("src.backend.crew_ai.crew.count_tokens",
                                  return_value=42))
        # Optimization seams — run_crew imports these from the package at
        # call time, so patching the package attributes is sufficient.
        stack.enter_context(patch.object(
            opt, "get_keyword_vector_store", return_value=MagicMock()))
        stack.enter_context(patch.object(opt, "QueryPatternMatcher",
                                         MagicMock()))
        stack.enter_context(patch.object(opt, "SmartKeywordProvider",
                                         return_value=provider))
        stack.enter_context(patch(
            "src.backend.crew_ai.optimization.learning_registry.get_feedback_loop",
            return_value=None))

        from src.backend.crew_ai.crew import run_crew
        run_crew("click the button", "gemini", "gemini-2.5-flash")

    return robot_agents_cls, robot_tasks_cls


class TestPlannerHintsOnlyWiring:

    def test_planner_request_is_hints_only(self):
        provider = _provider_mock()
        _run_crew_optimized(provider)
        planner_calls = [
            c for c in provider.get_agent_context.call_args_list
            if "planner" in c.args]
        assert len(planner_calls) == 1
        assert planner_calls[0].kwargs.get("hints_only") is True

    def test_assembler_request_unchanged(self):
        provider = _provider_mock()
        _run_crew_optimized(provider)
        assembler_calls = [
            c for c in provider.get_agent_context.call_args_list
            if "assembler" in c.args]
        assert len(assembler_calls) == 1
        assert not assembler_calls[0].kwargs.get("hints_only")

    def test_no_planner_context_reaches_agents(self):
        provider = _provider_mock()
        robot_agents_cls, _ = _run_crew_optimized(provider)
        kwargs = robot_agents_cls.call_args.kwargs
        assert "planner_context" not in kwargs
        assert kwargs["assembler_context"] == "ASSEMBLER_CTX"

    def test_planner_hints_still_reach_task_layer(self):
        """The point of the planner call is Tier 0 — its hint_text must
        keep flowing into the task-description injection."""
        provider = _provider_mock()
        _, robot_tasks_cls = _run_crew_optimized(provider)
        hint_context = robot_tasks_cls.call_args.kwargs["hint_context"]
        assert hint_context["planner"] == "PLANNER_HINT"
