"""
Unit tests for get_llm factory in src.backend.crew_ai.cleaned_llm_wrapper.

Purpose: get_llm is the factory that creates the correct LLM wrapper based
         on MODEL_PROVIDER.
"""

import pytest
from unittest.mock import patch

class TestGetLlm:
    """Tests for get_llm factory function."""

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_online_returns_cleaned_wrapper(self, MockCleanedLLMWrapper):
        """Online provider creates CleanedLLMWrapper."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"GEMINI_API_KEY": "test-key"}):
            llm = get_llm(model_provider="online", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                api_key="test-key",
                model="gemini-2.5-flash",
                num_retries=3
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedOllamaLLMWrapper")
    def test_local_returns_ollama_wrapper(self, MockCleanedOllamaLLMWrapper):
        """Local provider creates CleanedOllamaLLMWrapper."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        llm = get_llm(model_provider="local", model_name="llama3")
        assert llm == MockCleanedOllamaLLMWrapper.return_value
        MockCleanedOllamaLLMWrapper.assert_called_once_with(model="llama3")

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_uses_env_api_key(self, MockCleanedLLMWrapper):
        """Falls back to GEMINI_API_KEY from settings when no explicit key."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"GEMINI_API_KEY": "env-key-123"}):
            llm = get_llm(model_provider="online", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                api_key="env-key-123",
                model="gemini-2.5-flash",
                num_retries=3
            )
