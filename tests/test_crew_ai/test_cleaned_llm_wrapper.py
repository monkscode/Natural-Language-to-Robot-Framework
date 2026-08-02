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
        shorthand; see test_vertex_thinking_guard_reaches_the_request_body for
        why, and prefer that test — this one only checks that we set the key,
        which says nothing about whether it survives into the request.
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-3.5-flash")

        assert llm.additional_params["thinkingConfig"] == {"thinkingBudget": 0}

    @pytest.mark.parametrize("model_name", ["gemini-3.5-flash", "gemini-2.5-flash"])
    def test_vertex_thinking_guard_reaches_the_request_body(self, model_name):
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

        Assert on the BUILT REQUEST BODY, not on get_optional_params. Since the
        guard switched from `thinking` to `thinkingConfig` it no longer passes
        through any mapping: `thinkingConfig` is absent from
        get_supported_openai_params(), so LiteLLM echoes it back verbatim the way
        it echoes any unrecognised vertex kwarg (probe: thinkingConfigTYPO and
        totalNonsenseKey come back untouched). A test on that stage is satisfied
        by its own input and cannot fail.

        The stage that CAN drop it is _transform_request_body, which filters
        optional_params against GenerationConfig.__annotations__ before building
        generationConfig. Drop `thinkingConfig` from that TypedDict — exactly the
        shape of the 1.94.1 change — and the body comes back {} while the
        get_optional_params assertion stays green.

        Importing a private LiteLLM helper is deliberate: if a future bump moves
        or renames it the import fails loudly on the version bump, which is when
        the wire needs re-verifying anyway.
        """
        from litellm.llms.vertex_ai.gemini.transformation import (
            _transform_request_body,
        )

        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name=model_name)

        body = _transform_request_body(
            messages=[{"role": "user", "content": "ping"}],
            model=model_name,
            optional_params=dict(llm.additional_params),
            custom_llm_provider="vertex_ai",
            litellm_params={},
            cached_content=None,
        )
        generation_config = body.get("generationConfig", {})

        assert generation_config.get("thinkingConfig", {}).get("thinkingBudget") == 0, (
            f"{model_name}: the guard did not survive into the request body as a "
            f"zero thinking budget — generationConfig was {generation_config!r}"
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
