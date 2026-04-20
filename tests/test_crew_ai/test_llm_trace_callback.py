"""
Tests for src.backend.crew_ai.cleaned_llm_wrapper — _litellm_trace_callback,
_register_litellm_callback, get_llm, and CleanedLLMWrapper.call / get_context_window_size.

All tests mock CleanedLLMWrapper instantiation (which requires a live LiteLLM/CrewAI
setup) and the trace store so no external services are contacted.
"""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(tz=timezone.utc)


def _make_completion_response(
    content="test response",
    prompt_tokens=10,
    completion_tokens=5,
    total_tokens=15,
    cost=0.001,
):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = total_tokens
    resp._hidden_params = {"response_cost": cost}
    return resp


# ---------------------------------------------------------------------------
# _litellm_trace_callback
# ---------------------------------------------------------------------------

class TestLitellmTraceCallback:
    def _call(self, kwargs=None, completion_response=None, store=None):
        from src.backend.crew_ai.cleaned_llm_wrapper import _litellm_trace_callback
        kwargs = kwargs or {"model": "gemini/gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}]}
        completion_response = completion_response or _make_completion_response()
        store = store or MagicMock()
        with patch("src.backend.core.trace_store.get_trace_store", return_value=store):
            _litellm_trace_callback(kwargs, completion_response, _now(), _now())
        return store

    def test_calls_insert_litellm_call(self):
        store = self._call()
        store.insert_litellm_call.assert_called_once()

    def test_model_passed_correctly(self):
        store = self._call(kwargs={"model": "gemini/gemini-2.5-flash", "messages": []})
        _, kw = store.insert_litellm_call.call_args
        assert kw["model"] == "gemini/gemini-2.5-flash"

    def test_tokens_passed_correctly(self):
        resp = _make_completion_response(prompt_tokens=20, completion_tokens=10, total_tokens=30)
        store = self._call(completion_response=resp)
        _, kw = store.insert_litellm_call.call_args
        assert kw["prompt_tokens"] == 20
        assert kw["completion_tokens"] == 10
        assert kw["total_tokens"] == 30

    def test_cost_extracted_from_hidden_params(self):
        resp = _make_completion_response(cost=0.0042)
        store = self._call(completion_response=resp)
        _, kw = store.insert_litellm_call.call_args
        assert kw["cost_usd"] == pytest.approx(0.0042)

    def test_prompt_text_serialised_as_json(self):
        kwargs = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
        store = self._call(kwargs=kwargs)
        _, kw = store.insert_litellm_call.call_args
        import json
        parsed = json.loads(kw["prompt_text"])
        assert parsed[0]["content"] == "hello"

    def test_empty_messages_produces_none_prompt(self):
        kwargs = {"model": "m"}  # no messages key
        store = self._call(kwargs=kwargs)
        _, kw = store.insert_litellm_call.call_args
        assert kw["prompt_text"] is None

    def test_response_text_extracted(self):
        resp = _make_completion_response(content="the answer")
        store = self._call(completion_response=resp)
        _, kw = store.insert_litellm_call.call_args
        assert kw["response_text"] == "the answer"

    def test_no_choices_gives_none_response_text(self):
        resp = MagicMock()
        resp.choices = []
        resp.usage = MagicMock(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        resp._hidden_params = {}
        store = self._call(completion_response=resp)
        _, kw = store.insert_litellm_call.call_args
        assert kw["response_text"] is None

    def test_store_none_does_not_raise(self):
        from src.backend.crew_ai.cleaned_llm_wrapper import _litellm_trace_callback
        with patch("src.backend.core.trace_store.get_trace_store", return_value=None):
            _litellm_trace_callback({"model": "m"}, _make_completion_response(), _now(), _now())

    def test_broad_exception_does_not_propagate(self):
        """The callback must swallow all errors to avoid aborting LLM calls."""
        from src.backend.crew_ai.cleaned_llm_wrapper import _litellm_trace_callback
        bad_store = MagicMock()
        bad_store.insert_litellm_call.side_effect = RuntimeError("DB down")
        with patch("src.backend.core.trace_store.get_trace_store", return_value=bad_store):
            _litellm_trace_callback({"model": "m"}, _make_completion_response(), _now(), _now())

    def test_duration_is_positive(self):
        from src.backend.crew_ai.cleaned_llm_wrapper import _litellm_trace_callback
        store = MagicMock()
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        with patch("src.backend.core.trace_store.get_trace_store", return_value=store):
            _litellm_trace_callback({"model": "m"}, _make_completion_response(), start, end)
        _, kw = store.insert_litellm_call.call_args
        assert kw["duration_ms"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# _register_litellm_callback
# ---------------------------------------------------------------------------

class TestRegisterLitellmCallback:
    def test_skips_when_observability_none(self):
        mock_settings = MagicMock()
        mock_settings.OBSERVABILITY_BACKEND = "none"
        import litellm
        original = litellm.success_callback[:]
        try:
            with patch("src.backend.crew_ai.cleaned_llm_wrapper.settings", mock_settings, create=True):
                from src.backend.crew_ai.cleaned_llm_wrapper import (
                    _register_litellm_callback,
                    _litellm_trace_callback,
                )
                # Temporarily remove callback if present
                if _litellm_trace_callback in litellm.success_callback:
                    litellm.success_callback.remove(_litellm_trace_callback)
                with patch("src.backend.core.config.settings", mock_settings):
                    _register_litellm_callback()
                assert _litellm_trace_callback not in litellm.success_callback
        finally:
            litellm.success_callback = original

    def test_no_duplicate_registration(self):
        import litellm
        from src.backend.crew_ai.cleaned_llm_wrapper import (
            _register_litellm_callback,
            _litellm_trace_callback,
        )
        original = litellm.success_callback[:]
        try:
            mock_settings = MagicMock()
            mock_settings.OBSERVABILITY_BACKEND = "sqlite"
            with patch("src.backend.core.config.settings", mock_settings):
                _register_litellm_callback()
                _register_litellm_callback()
            count = litellm.success_callback.count(_litellm_trace_callback)
            assert count <= 1
        finally:
            litellm.success_callback = original


# ---------------------------------------------------------------------------
# get_llm
# ---------------------------------------------------------------------------

class TestGetLLM:
    def _get_llm(self, provider, model="gemini-2.5-flash", api_key=None):
        with patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper") as mock_cls:
            mock_cls.return_value = MagicMock()
            from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
            result = get_llm(provider, model, api_key=api_key)
            return mock_cls, result

    def test_gemini_provider_creates_wrapper(self):
        mock_cls, result = self._get_llm("gemini")
        mock_cls.assert_called_once()
        _, kw = mock_cls.call_args
        assert "gemini/" in kw.get("model", mock_cls.call_args[0][0] if mock_cls.call_args[0] else "")

    def test_gemini_model_has_provider_prefix(self):
        mock_cls, _ = self._get_llm("gemini", "gemini-2.5-flash")
        args, kw = mock_cls.call_args
        model_arg = kw.get("model") or (args[0] if args else "")
        assert model_arg.startswith("gemini/")

    def test_vertex_provider_creates_wrapper(self):
        mock_cls, _ = self._get_llm("vertex", "gemini-2.5-flash")
        mock_cls.assert_called_once()
        args, kw = mock_cls.call_args
        model_arg = kw.get("model") or (args[0] if args else "")
        assert model_arg.startswith("vertex_ai/")

    def test_vertex_strips_existing_prefix(self):
        mock_cls, _ = self._get_llm("vertex", "vertex_ai/gemini-2.5-flash")
        args, kw = mock_cls.call_args
        model_arg = kw.get("model") or (args[0] if args else "")
        # Should not double-prefix
        assert not model_arg.startswith("vertex_ai/vertex_ai/")

    def test_local_provider_uses_ollama_prefix(self):
        with patch.dict(os.environ, {"OLLAMA_API_BASE": "http://localhost:11434"}):
            mock_cls, _ = self._get_llm("local", "llama3")
        args, kw = mock_cls.call_args
        model_arg = kw.get("model") or (args[0] if args else "")
        assert model_arg.startswith("ollama/")

    def test_local_provider_reads_ollama_base_url(self):
        with patch.dict(os.environ, {"OLLAMA_API_BASE": "http://custom:11434"}):
            mock_cls, _ = self._get_llm("local", "llama3")
        _, kw = mock_cls.call_args
        assert kw.get("base_url") == "http://custom:11434"

    def test_invalid_provider_raises_value_error(self):
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        with pytest.raises(ValueError, match="Unsupported model_provider"):
            get_llm("aws", "some-model")

    def test_gemini_api_key_passed(self):
        mock_cls, _ = self._get_llm("gemini", api_key="my-key")
        _, kw = mock_cls.call_args
        assert kw.get("api_key") == "my-key"

    def test_num_retries_set_for_gemini(self):
        mock_cls, _ = self._get_llm("gemini")
        _, kw = mock_cls.call_args
        assert kw.get("num_retries") == 3

    def test_num_retries_set_for_vertex(self):
        mock_cls, _ = self._get_llm("vertex")
        _, kw = mock_cls.call_args
        assert kw.get("num_retries") == 3


# ---------------------------------------------------------------------------
# CleanedLLMWrapper.call — output cleaning
# ---------------------------------------------------------------------------

class TestCleanedLLMWrapperCall:
    def _make_wrapper(self):
        """Instantiate wrapper with mocked BaseLLM to avoid LiteLLM calls."""
        from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper
        from src.backend.crew_ai.llm_output_cleaner import LLMFormattingMonitor

        with patch("src.backend.crew_ai.cleaned_llm_wrapper.LLMFormattingMonitor"):
            with patch("crewai.llm.BaseLLM.__init__", return_value=None):
                instance = object.__new__(CleanedLLMWrapper)
                instance.model = "gemini/gemini-2.5-flash"
                instance.context_window_size = 0
                instance._monitor = LLMFormattingMonitor()
                instance.base_url = None
                return instance

    def test_non_string_result_returned_unchanged(self):
        wrapper = self._make_wrapper()
        non_str = {"key": "value"}
        with patch("crewai.llm.LLM.call", return_value=non_str):
            result = wrapper.call([], )
            assert result is non_str

    def test_clean_output_called_on_string(self):
        wrapper = self._make_wrapper()
        with patch("crewai.llm.LLM.call", return_value="Action: tool\nExtra text"):
            with patch(
                "src.backend.crew_ai.cleaned_llm_wrapper.LLMOutputCleaner.clean_output",
                return_value="Action: tool",
            ) as mock_clean:
                wrapper.call([])
                mock_clean.assert_called_once()

    def test_uncleaned_string_returned_as_is(self):
        wrapper = self._make_wrapper()
        with patch("crewai.llm.LLM.call", return_value="clean output"):
            with patch(
                "src.backend.crew_ai.cleaned_llm_wrapper.LLMOutputCleaner.clean_output",
                return_value="clean output",
            ):
                result = wrapper.call([])
                assert result == "clean output"
