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
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-3.5-flash")

        assert llm.additional_params["thinking"] == {"type": "enabled", "budget_tokens": 0}

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
