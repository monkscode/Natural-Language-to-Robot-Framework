"""
Unit tests for get_llm factory in src.backend.crew_ai.cleaned_llm_wrapper.

Purpose: get_llm is the factory that creates the correct LLM wrapper based
         on MODEL_PROVIDER.
"""

import os
import pytest
from unittest.mock import patch


class TestGetLlm:
    """Tests for get_llm factory function."""

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_gemini_returns_cleaned_wrapper(self, MockCleanedLLMWrapper):
        """Gemini provider creates CleanedLLMWrapper."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"GEMINI_API_KEY": "test-key"}):
            llm = get_llm(model_provider="gemini", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                api_key="test-key",
                model="gemini/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_local_returns_ollama_wrapper(self, MockCleanedLLMWrapper):
        """Local provider creates CleanedLLMWrapper with ollama/ prefix and default base URL."""
        import os
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        # Remove OLLAMA_API_BASE so the default "http://localhost:11434" is used
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop("OLLAMA_API_BASE", None)
            llm = get_llm(model_provider="local", model_name="llama3")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                model="ollama/llama3",
                base_url="http://localhost:11434",
                is_litellm=True,
                num_retries=3,
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_uses_env_api_key(self, MockCleanedLLMWrapper):
        """Falls back to GEMINI_API_KEY from settings when no explicit key."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"GEMINI_API_KEY": "env-key-123"}):
            llm = get_llm(model_provider="gemini", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                api_key="env-key-123",
                model="gemini/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_strips_gemini_prefix(self, MockCleanedLLMWrapper):
        """Verify get_llm(model_provider='vertex') strips gemini/ and prepends vertex_ai/."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini/gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                model="vertex_ai/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True,
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_handles_bare_model_name(self, MockCleanedLLMWrapper):
        """Verify get_llm(model_provider='vertex') works with no prefix in model name."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                model="vertex_ai/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True,
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_does_not_pass_safety_settings_while_parked(self, MockCleanedLLMWrapper):
        """safety_settings is parked (commented out in cleaned_llm_wrapper.py).

        If this test fails it means safety_settings has been re-enabled — update the
        test along with the change and document why in the PR.
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            get_llm(model_provider="vertex", model_name="gemini-2.5-flash")

        assert "safety_settings" not in MockCleanedLLMWrapper.call_args.kwargs

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_does_not_pass_api_key(self, MockCleanedLLMWrapper):
        """Verify vertex provider does not inject an api_key (auth is via service account)."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini/gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once()
            call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
            assert call_kwargs["model"] == "vertex_ai/gemini-2.5-flash"
            assert "api_key" not in call_kwargs

    def test_get_llm_vertex_disables_thinking_budget(self):
        """Vertex flipped gemini-3.5-flash to server-side thinking-ON (2026-07-18),
        inflating completion tokens 4-6x and burning TPM/RPD quota. crewai's own
        LLM.__init__ has a same-named `thinking` constructor param that is never
        stored or forwarded (verified against the pinned crewai==1.8.1 source), so
        the override must land in additional_params post-construction — that's
        the only path LLM._prepare_completion_params forwards untouched to LiteLLM.

        The guard carries Vertex's own thinkingConfig, not LiteLLM's `thinking`
        shorthand; see test_vertex_thinking_guard_maps_to_a_zero_budget_on_the_wire
        for why, and prefer that test — it asserts the outcome rather than this
        key, so it survives the next mapping change.
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-3.5-flash")

        assert llm.additional_params["thinkingConfig"] == {"thinkingBudget": 0}

    @pytest.mark.parametrize("model_name", ["gemini-3.5-flash", "gemini-2.5-flash"])
    def test_vertex_thinking_guard_maps_to_a_zero_budget_on_the_wire(self, model_name):
        """The guard is only worth what LiteLLM actually puts on the wire.

        Asserting on additional_params (the tests above) checks our half of the
        contract and cannot see the other half. litellm 1.94.1 silently stopped
        honouring thinking={'budget_tokens': 0} for gemini-3.5-flash: its
        _map_thinking_param treats any "Gemini 3 or newer" model as thinkingLevel-
        based and emits only includeThoughts=False, so Vertex fell back to
        server-side thinking-ON. Measured cost of that gap: reasoning tokens ate
        the 65,535-token output allowance, truncating the planner's JSON to its
        first step (0 elements identified, vacuous passing tests) or past
        parsing entirely (max_tokens ConverterError, dead run).

        So assert the OUTCOME, not the mechanism — whatever key we use, the
        mapped provider params must carry a zero thinking budget for the models
        we actually run.
        """
        from litellm.utils import get_optional_params

        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name=model_name)

        mapped = get_optional_params(
            model=model_name,
            custom_llm_provider="vertex_ai",
            **llm.additional_params,
        )

        assert mapped.get("thinkingConfig", {}).get("thinkingBudget") == 0, (
            f"{model_name}: guard did not reach the wire as a zero thinking "
            f"budget — mapped to {mapped.get('thinkingConfig')!r}"
        )

    def test_get_llm_gemini_does_not_disable_thinking_budget(self):
        """The thinking-ON flip is Vertex-specific (probe-verified 2026-07-18) —
        Google AI Studio isn't touched, so gemini provider must stay untouched."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            llm = get_llm(model_provider="gemini", model_name="gemini-3.5-flash")

        assert "thinking" not in llm.additional_params

    def test_get_llm_local_does_not_disable_thinking_budget(self):
        """Ollama models have no Vertex thinking-budget concept — must stay untouched."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        llm = get_llm(model_provider="local", model_name="llama3")

        assert "thinking" not in llm.additional_params

    @pytest.mark.parametrize("bad_provider", ["openai", "anthropic", "gemni", "", "GEMINI", "gpt-4"])
    def test_unsupported_provider_raises_value_error(self, bad_provider):
        """Unknown model_provider values must raise ValueError immediately, not silently route."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with pytest.raises(ValueError, match="Unsupported model_provider"):
            get_llm(model_provider=bad_provider, model_name="some-model")


class TestGetLlmResponseFormat:
    """Tests for get_llm's response_format pass-through with provider gating (Task 22).

    Contract: response_format (a Pydantic model class) is forwarded to
    CleanedLLMWrapper ONLY when LiteLLM's capability table says the routed
    model supports response schemas. Unsupported or unknown models silently
    fall back to the legacy free-text contract (Option A: Ollama keeps the
    guardrail path).
    """

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_response_format_forwarded_when_supported(self, MockCleanedLLMWrapper):
        """Supported model (vertex gemini): response_format reaches the wrapper."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import PlanOutput

        with patch("litellm.utils.supports_response_schema", return_value=True):
            get_llm(model_provider="vertex", model_name="gemini-2.5-flash",
                    response_format=PlanOutput)

        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert call_kwargs["response_format"] is PlanOutput

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_response_format_gated_off_when_unsupported(self, MockCleanedLLMWrapper):
        """Unsupported model (ollama): wrapper is built WITHOUT response_format."""
        import os
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import PlanOutput

        os.environ.pop("OLLAMA_API_BASE", None)
        with patch("litellm.utils.supports_response_schema", return_value=False):
            get_llm(model_provider="local", model_name="qwen2.5-coder:14b",
                    response_format=PlanOutput)

        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert "response_format" not in call_kwargs

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_response_format_gated_off_on_capability_check_error(self, MockCleanedLLMWrapper):
        """Capability check blowing up (unknown model) must NOT break get_llm —
        falls back to legacy contract instead of raising."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import PlanOutput

        with patch("litellm.utils.supports_response_schema",
                   side_effect=Exception("model not in DB")):
            llm = get_llm(model_provider="vertex", model_name="unknown-model-xyz",
                          response_format=PlanOutput)

        assert llm == MockCleanedLLMWrapper.return_value
        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert "response_format" not in call_kwargs

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_no_response_format_keeps_legacy_call_shape(self, MockCleanedLLMWrapper):
        """Callers that don't pass response_format get the exact legacy kwargs
        (no stray response_format=None leaking into the wrapper)."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"VERTEXAI_PROJECT": "p"}):
            get_llm(model_provider="vertex", model_name="gemini-2.5-flash")

        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert "response_format" not in call_kwargs


# ═══════════════════════════════════════════════════════════════════════════
# crewai >=1.10 response_model reroute
# ═══════════════════════════════════════════════════════════════════════════

VERTEX_ENV = {
    "VERTEXAI_CREDENTIALS": "creds.json",
    "VERTEXAI_PROJECT": "test-project",
    "VERTEXAI_LOCATION": "us-central1",
}

ASSEMBLED_CODE_JSON = '{"code": "*** Settings ***\\nLibrary    Browser\\n"}'


def _fake_model_response(content: str, model: str, tool_name: str | None = None):
    """Build a ModelResponse shaped like a real LiteLLM completion, with usage.

    A real provider only emits tool_calls when the request offered `tools`, so
    tool_name is set only for instructor's TOOLS mode. That keeps the native
    path honest (plain content) while letting the instructor path parse.
    """
    from litellm import ModelResponse
    from litellm.types.utils import (
        ChatCompletionMessageToolCall,
        Choices,
        Function,
        Message,
        Usage,
    )

    tool_calls = None
    if tool_name:
        tool_calls = [
            ChatCompletionMessageToolCall(
                id="call_1",
                type="function",
                function=Function(name=tool_name, arguments=content),
            )
        ]

    return ModelResponse(
        id="chatcmpl-test",
        choices=[
            Choices(
                finish_reason="tool_calls" if tool_name else "stop",
                index=0,
                message=Message(content=content, role="assistant", tool_calls=tool_calls),
            )
        ],
        created=0,
        model=model,
        object="chat.completion",
        usage=Usage(prompt_tokens=111, completion_tokens=22, total_tokens=133),
    )


class _CompletionSpy:
    """Patches litellm.completion and records every kwargs dict that reached it.

    crewai calls `litellm.completion(**params)` and InternalInstructor resolves
    `completion` from the same module, so patching the module attribute captures
    both the agent-executor path and the instructor path.
    """

    def __init__(self, content: str = ASSEMBLED_CODE_JSON):
        self.content = content
        self.calls: list[dict] = []
        self._patcher = None

    def __enter__(self):
        def _fake(*args, **kwargs):
            self.calls.append(kwargs)
            tools = kwargs.get("tools")
            tool_name = None
            if tools:
                tool_name = tools[0]["function"]["name"]
            return _fake_model_response(
                self.content,
                kwargs.get("model", "vertex_ai/gemini-3.5-flash"),
                tool_name,
            )

        self._patcher = patch("litellm.completion", side_effect=_fake)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False

    @property
    def first(self) -> dict:
        assert self.calls, "litellm.completion was never called"
        return self.calls[0]


def _single_task_crew(llm, output_pydantic, description="Return the robot code as JSON."):
    """Build the repo's real shape: one tool-less agent, one task, one crew."""
    from crewai import Agent, Crew, Task

    agent = Agent(
        role="Robot Framework Code Generator",
        goal="Generate Robot Framework code and return it as JSON.",
        backstory="You reply with one JSON object holding a 'code' key.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )
    task = Task(
        description=description,
        expected_output='A JSON object with a "code" key.',
        agent=agent,
        output_pydantic=output_pydantic,
    )
    return Crew(agents=[agent], tasks=[task], verbose=False)


class TestResponseModelReroute:
    """crewai >=1.10 maps task.output_pydantic onto response_model
    (agent/core.py:1170-1171), which routes the agent loop through `instructor`
    (llm.py:1242). On that path nothing but model/response_model/messages reaches
    LiteLLM — the Vertex thinking guard, num_retries, api_key and base_url are
    dropped, native response_format becomes TOOLS-mode function calling, and
    _token_usage stays zero so workflow metrics and pricing silently read 0.

    The existing characterization tests cannot see this: they assert on
    llm.additional_params and call _prepare_completion_params directly, and the
    broken path never reaches that method. These four drive a real Crew.kickoff()
    and assert on what actually reached the wire.
    """

    def test_executor_path_keeps_native_completion(self):
        """The agent-executor path must reach LiteLLM natively: thinking guard,
        num_retries, native response_format, no instructor TOOLS-mode, real usage."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import AssemblyOutput

        with patch.dict(os.environ, VERTEX_ENV):
            llm = get_llm("vertex", "gemini-3.5-flash", response_format=AssemblyOutput)

        crew = _single_task_crew(llm, AssemblyOutput)

        with _CompletionSpy() as spy:
            crew.kickoff()

        wire = spy.first
        assert wire.get("thinkingConfig") == {"thinkingBudget": 0}, (
            "Vertex thinking guard did not reach LiteLLM — the call was rerouted "
            "through instructor, which forwards only model/response_model/messages."
        )
        assert wire.get("num_retries") == 3
        assert [m["role"] for m in wire["messages"]] == ["system", "user"]
        assert "response_format" in wire, "native schema enforcement was replaced"
        assert "tools" not in wire, (
            "instructor TOOLS-mode function calling reached the wire instead of "
            "native response_format"
        )
        assert crew.calculate_usage_metrics().total_tokens > 0, (
            "token accumulator read zero — workflow metrics and pricing are blind"
        )

    def test_salvage_path_keeps_response_model(self):
        """crewai's Converter — the salvage step when raw text fails validation —
        passes response_model with NO from_task/from_agent and has relied on
        instructor since 1.8.1. Stripping it there costs 3 extra LLM calls and then
        raises ConverterError. This is the test a blanket pop fails."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import AssemblyOutput

        with patch.dict(os.environ, VERTEX_ENV):
            llm = get_llm("vertex", "gemini-3.5-flash", response_format=AssemblyOutput)

        with _CompletionSpy() as spy:
            result = llm.call(
                messages=[{"role": "user", "content": "convert this"}],
                response_model=AssemblyOutput,
            )

        # crewai's instructor branch returns result.model_dump_json() — a JSON
        # string, not a model instance (llm.py:1258) — so the return type cannot
        # tell the two paths apart. The wire can: instructor uses TOOLS-mode
        # function calling, the plain completion path does not.
        assert "tools" in spy.first, (
            "response_model was stripped from the Converter path — the call went "
            "out as a plain completion, so crewai falls back to coercing raw text. "
            "That is the blanket-pop regression: 3 extra calls, then ConverterError."
        )
        assert len(spy.calls) == 1, f"expected one salvage call, got {len(spy.calls)}"
        assert AssemblyOutput.model_validate_json(result).code

    def test_ollama_path_is_not_forced_through_instructor(self):
        """On providers without schema support get_llm drops response_format and the
        free-text guardrail/salvage net is the live contract. output_pydantic must
        not smuggle the call into instructor TOOLS-mode behind that gate."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import AssemblyOutput

        os.environ.pop("OLLAMA_API_BASE", None)
        with patch("litellm.utils.supports_response_schema", return_value=False):
            llm = get_llm("local", "llama3", response_format=AssemblyOutput)

        crew = _single_task_crew(llm, AssemblyOutput)

        with _CompletionSpy(ASSEMBLED_CODE_JSON) as spy:
            crew.kickoff()

        assert "tools" not in spy.first, (
            "the free-text contract was replaced by instructor TOOLS-mode "
            "function calling"
        )

    def test_repair_crew_usage_is_non_zero(self):
        """dryrun_service._repair_usage_dict folds the repair crew's usage into
        crewai_metrics. A zeroed accumulator makes repairs invisible to metrics."""
        from crewai import Crew
        from src.backend.crew_ai.agents import RobotAgents
        from src.backend.crew_ai.tasks import RobotTasks

        with patch.dict(os.environ, VERTEX_ENV):
            agents = RobotAgents("vertex", "gemini-3.5-flash")

        assembler = agents.code_assembler_agent()
        task = RobotTasks().repair_code_task(
            assembler,
            robot_code="*** Settings ***\nLibrary    Browser\n",
            dryrun_errors="No keyword with name 'Clik' found.",
        )
        crew = Crew(agents=[assembler], tasks=[task], verbose=False)

        with _CompletionSpy():
            crew.kickoff()

        assert crew.calculate_usage_metrics().total_tokens > 0, (
            "repair crew reported zero tokens — _repair_usage_dict folds zeros "
            "into crewai_metrics"
        )
