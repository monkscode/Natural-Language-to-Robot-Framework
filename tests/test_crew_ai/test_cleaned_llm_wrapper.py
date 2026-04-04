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

    @pytest.mark.parametrize("bad_provider", ["openai", "anthropic", "gemni", "", "GEMINI", "gpt-4"])
    def test_unsupported_provider_raises_value_error(self, bad_provider):
        """Unknown model_provider values must raise ValueError immediately, not silently route."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with pytest.raises(ValueError, match="Unsupported model_provider"):
            get_llm(model_provider=bad_provider, model_name="some-model")
