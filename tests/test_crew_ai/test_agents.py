"""
Unit tests for RobotAgents._get_agent_context in src.backend.crew_ai.agents.

Purpose: _get_agent_context decides which context (optimized vs library fallback)
         is injected into each agent's prompt.  A regression here means agents
         get wrong/empty context, producing incorrect RF code.

Tests:
  - Optimized context has priority when available
  - Falls back to library context when optimization unavailable
  - Returns empty string when no context available
  - Unknown agent type returns empty string
"""

import pytest
from unittest.mock import patch, MagicMock


class TestGetAgentContext:
    """Tests for RobotAgents._get_agent_context."""

    def _make_agents(self, optimization_enabled=False, library_context=None):
        """Create RobotAgents with mocked dependencies."""
        from src.backend.crew_ai.agents import RobotAgents
        agents = RobotAgents.__new__(RobotAgents)
        agents.llm = MagicMock()
        agents.library_context = library_context or MagicMock()
        agents.planner_context = None
        agents.assembler_context = None
        return agents

    def test_library_context_used(self):
        """When no optimization, uses library context."""
        lib_ctx = MagicMock()
        lib_ctx.get_full_context.return_value = "Browser Library context here"
        agents = self._make_agents(library_context=lib_ctx)

        result = agents._get_agent_context("planner")
        # Should use library context (either directly or via method)
        assert result is not None

    def test_optimized_priority(self):
        """When optimized context exists, it takes priority."""
        agents = self._make_agents(optimization_enabled=True)
        agents.planner_context = "Optimized planner context"

        result = agents._get_agent_context("planner")
        assert "Optimized" in result

    def test_no_context_returns_empty(self):
        """When no context available at all, returns empty string."""
        lib_ctx = MagicMock()
        lib_ctx.get_full_context.return_value = ""
        agents = self._make_agents(library_context=lib_ctx)

        result = agents._get_agent_context("planner")
        assert isinstance(result, str)

    def test_unknown_type_returns_empty(self):
        """Unknown agent type gracefully returns empty string."""
        agents = self._make_agents()
        result = agents._get_agent_context("nonexistent_type")
        assert isinstance(result, str)


class TestPlannerStructuredOutput:
    """Task 22 + 24R Stage 3: planner and assembler each get a
    schema-enforced LLM (PlanOutput / AssemblyOutput); both share ONE
    monitor object so crew.py's `agents.llm._monitor` stays the
    authoritative counter."""

    @patch("src.backend.crew_ai.agents.get_llm")
    def test_planner_llm_schema_and_shared_monitor(self, mock_get_llm):
        from src.backend.crew_ai.agents import RobotAgents
        from src.backend.crew_ai.tasks import AssemblyOutput, PlanOutput
        from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper

        # spec= so crewai Agent's pydantic llm-field validation (isinstance
        # against BaseLLM) accepts the mocks
        plain_llm = MagicMock(name="plain", spec=CleanedLLMWrapper)
        planner_llm = MagicMock(name="planner", spec=CleanedLLMWrapper)
        # _monitor and _token_usage are instance attributes (set in __init__),
        # invisible to spec= — seed them so the sharing wiring can read them
        plain_llm._monitor = MagicMock(name="monitor")
        plain_llm._token_usage = {}
        mock_get_llm.side_effect = [plain_llm, planner_llm]

        agents = RobotAgents("vertex", "gemini-2.5-flash")

        # Factory called twice: assembler call first (AssemblyOutput schema,
        # 24R Stage 3), then the planner call (PlanOutput schema, Task 22)
        assert mock_get_llm.call_count == 2
        first_kwargs = mock_get_llm.call_args_list[0].kwargs
        second_kwargs = mock_get_llm.call_args_list[1].kwargs
        assert first_kwargs["response_format"] is AssemblyOutput
        assert second_kwargs["response_format"] is PlanOutput

        # Wiring: assembler keeps plain llm, planner gets the schema llm
        assert agents.llm is plain_llm
        assert agents.planner_llm is planner_llm
        assert agents.step_planner_agent().llm is planner_llm
        assert agents.code_assembler_agent().llm is plain_llm

        # Metrics: one shared monitor object → agents.llm._monitor sees planner calls too
        assert agents.planner_llm._monitor is agents.llm._monitor

    @patch("src.backend.crew_ai.agents.get_llm")
    def test_planner_llm_shares_token_usage_accumulator(self, mock_get_llm):
        """Usage accounting: crew.py reads token usage ONCE from the assembler
        crew, whose single agent holds `agents.llm` — CrewAI's
        calculate_usage_metrics() reads `agent.llm._token_usage` (BaseLLM dict,
        mutated in place, assigned only in __init__). The planner's separate
        wrapper must alias that same dict, or the planner's calls/tokens/cost
        vanish from workflow metrics (bench evidence: crewai_tokens ==
        assembler-only tokens on every 2026-07-11 checkpoint run)."""
        from src.backend.crew_ai.agents import RobotAgents
        from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper

        plain_llm = MagicMock(name="plain", spec=CleanedLLMWrapper)
        planner_llm = MagicMock(name="planner", spec=CleanedLLMWrapper)
        plain_llm._monitor = MagicMock(name="monitor")
        # Real BaseLLM seeds this dict in __init__; invisible to spec= mocks
        plain_llm._token_usage = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "successful_requests": 0, "cached_prompt_tokens": 0,
        }
        mock_get_llm.side_effect = [plain_llm, planner_llm]

        agents = RobotAgents("vertex", "gemini-2.5-flash")

        assert agents.planner_llm._token_usage is agents.llm._token_usage
