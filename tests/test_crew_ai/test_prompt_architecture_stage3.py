"""
TASK-24R Stage 3 — assembler structured output (decoder-level enforcement).

Target state under test:
- RobotAgents wires the assembler LLM through the SAME gated get_llm path the
  planner uses (Task 22): ``response_format=AssemblyOutput`` on the assembler
  instance (``self.llm``). get_llm's supports_response_schema gate keeps
  ollama on the legacy free-text contract, where the guardrail salvage net is
  the LIVE contract — the net must never be deleted while that path exists.
- The ONE-monitor / ONE-_token_usage invariant survives the construction
  change (agents.py; the sharing broke once — caught 2026-07-11, 14fe23a).
- Stage 3 rider (owner decision 2026-07-15): LOCATOR_RULES pins the Task 12
  placeholder contract — a found:false step's action line stays a LIVE
  keyword call; commenting the step out is forbidden (the 2026-07-13 ASTPP
  smoke showed the assembler commenting out a found:false popup-close Click,
  erasing the user's repair surface).

Referenced by: (test module)
Depends on: src/backend/crew_ai/agents.py,
    src/backend/crew_ai/tasks.py (AssemblyOutput),
    src/backend/crew_ai/prompts/components.py (LOCATOR_RULES)
"""

from unittest.mock import MagicMock, patch


def _build_agents():
    """RobotAgents with spec'd LLM mocks (test_agents.py pattern).

    Returns (agents, assembler_llm_mock, planner_llm_mock, get_llm_mock).
    """
    from src.backend.crew_ai.agents import RobotAgents
    from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper

    assembler_llm = MagicMock(name="assembler", spec=CleanedLLMWrapper)
    planner_llm = MagicMock(name="planner", spec=CleanedLLMWrapper)
    # _monitor and _token_usage are instance attributes (set in __init__),
    # invisible to spec= — seed them so the sharing wiring can read them.
    assembler_llm._monitor = MagicMock(name="monitor")
    assembler_llm._token_usage = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "successful_requests": 0, "cached_prompt_tokens": 0,
    }
    with patch("src.backend.crew_ai.agents.get_llm",
               side_effect=[assembler_llm, planner_llm]) as mock_get_llm:
        agents = RobotAgents("vertex", "gemini-2.5-flash")
    return agents, assembler_llm, planner_llm, mock_get_llm


# ═══════════════════════════════════════════════════════════════════════════
# Assembler LLM — provider-enforced AssemblyOutput schema
# ═══════════════════════════════════════════════════════════════════════════

class TestAssemblerStructuredOutput:

    def test_assembler_llm_gets_assembly_output_schema(self):
        """The assembler instance (self.llm) is created through the gated
        get_llm path with response_format=AssemblyOutput — decoder-level
        enforcement of the {"code": ...} contract on schema-capable
        providers (vertex/AI Studio); get_llm silently drops it on ollama."""
        from src.backend.crew_ai.tasks import AssemblyOutput

        agents, assembler_llm, _, mock_get_llm = _build_agents()

        first_kwargs = mock_get_llm.call_args_list[0].kwargs
        assert first_kwargs["response_format"] is AssemblyOutput
        assert agents.llm is assembler_llm
        assert agents.code_assembler_agent().llm is assembler_llm

    def test_planner_llm_keeps_plan_output_schema(self):
        """Regression (Task 22): the planner's own schema is untouched."""
        from src.backend.crew_ai.tasks import PlanOutput

        agents, _, planner_llm, mock_get_llm = _build_agents()

        assert mock_get_llm.call_count == 2
        second_kwargs = mock_get_llm.call_args_list[1].kwargs
        assert second_kwargs["response_format"] is PlanOutput
        assert agents.planner_llm is planner_llm
        assert agents.step_planner_agent().llm is planner_llm

    def test_shared_monitor_and_token_usage_survive(self):
        """Invariant: ONE LLMFormattingMonitor and ONE _token_usage dict
        across both wrappers — crew.py reads agents.llm._monitor and
        workflow metrics read agents.llm._token_usage exactly once."""
        agents, assembler_llm, _, _ = _build_agents()

        assert agents.planner_llm._monitor is agents.llm._monitor
        assert agents.planner_llm._token_usage is agents.llm._token_usage
        assert agents.llm._monitor is assembler_llm._monitor
        assert agents.llm._token_usage is assembler_llm._token_usage


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3 rider — found:false placeholder step must stay LIVE
# ═══════════════════════════════════════════════════════════════════════════

class TestPlaceholderStepStaysLive:

    def test_locator_rules_forbid_commenting_out_the_step(self):
        """Task 12 contract pin: the placeholder ACTION step is emitted as a
        live keyword call; only the WARNING lines are comments."""
        from src.backend.crew_ai.prompts.components import PromptComponents

        body = PromptComponents.LOCATOR_RULES
        assert "NEVER comment out the step" in body
        assert "LIVE" in body

    def test_locator_rules_keep_existing_placeholder_contract(self):
        """The pin tightens the contract — it must not lose the original
        found:false semantics (WARNING comment + PLACEHOLDER_FOR + valid
        code)."""
        from src.backend.crew_ai.prompts.components import PromptComponents

        body = PromptComponents.LOCATOR_RULES
        assert "PLACEHOLDER_FOR" in body
        assert "# WARNING: Locator not found" in body
        assert "Still generate syntactically valid code" in body
