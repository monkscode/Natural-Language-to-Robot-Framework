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

    def test_empty_response_logs_finish_reason_warning(self, caplog):
        """Diagnostic: when content is None/empty, callback must log finish_reason.

        This is the one signal that distinguishes a Vertex AI content-filter block
        (finish_reason='content_filter') from MAX_TOKENS, RECITATION, or a genuine
        empty completion.  Without this log the only visible symptom is CrewAI's
        generic 'Invalid response from LLM call - None or empty.' which is the
        bug we were chasing for hours.
        """
        import logging
        resp = MagicMock()
        choice = MagicMock()
        choice.message.content = None
        choice.finish_reason = "content_filter"
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=0, total_tokens=10)
        resp._hidden_params = {}
        with caplog.at_level(logging.WARNING, logger="src.backend.crew_ai.cleaned_llm_wrapper"):
            self._call(completion_response=resp)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Empty response" in m and "content_filter" in m for m in msgs), (
            f"Expected warning with finish_reason=content_filter, got: {msgs}"
        )

    def test_non_empty_response_does_not_log_warning(self, caplog):
        """Diagnostic warning must only fire when content is empty — not on every call."""
        import logging
        resp = _make_completion_response(content="real content here")
        with caplog.at_level(logging.WARNING, logger="src.backend.crew_ai.cleaned_llm_wrapper"):
            self._call(completion_response=resp)
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("Empty response" in m for m in msgs), (
            f"Diagnostic should NOT fire on non-empty content, got: {msgs}"
        )

    def test_empty_string_content_also_triggers_warning(self, caplog):
        """Empty-string content (not just None) must also trigger the diagnostic."""
        import logging
        resp = MagicMock()
        choice = MagicMock()
        choice.message.content = ""
        choice.finish_reason = "stop"
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=0, total_tokens=10)
        resp._hidden_params = {}
        with caplog.at_level(logging.WARNING, logger="src.backend.crew_ai.cleaned_llm_wrapper"):
            self._call(completion_response=resp)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Empty response" in m and "stop" in m for m in msgs)

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
            mock_settings.OBSERVABILITY_BACKEND = "postgres"
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


# ---------------------------------------------------------------------------
# CleanedLLMWrapper.call — empty-response retry
# ---------------------------------------------------------------------------

class TestCleanedLLMWrapperEmptyRetry:
    """Empty-response retry loop in CleanedLLMWrapper.call.

    Retries trigger on None / empty / whitespace-only string returns.
    Exceptions and non-string non-None returns are pass-through (no retry).
    Reuses the same `messages` so tool results in the conversation aren't re-invoked.
    """

    def _make_wrapper(self):
        """Live wrapper with real LLMFormattingMonitor so we can assert counters."""
        from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper
        from src.backend.crew_ai.llm_output_cleaner import LLMFormattingMonitor
        with patch("crewai.llm.BaseLLM.__init__", return_value=None):
            instance = object.__new__(CleanedLLMWrapper)
            instance.model = "vertex_ai/gemini-3.5-flash"
            instance.context_window_size = 0
            instance._monitor = LLMFormattingMonitor()
            instance.base_url = None
            return instance

    def _patch_settings(self, max_retries: int):
        """Patch settings.LLM_EMPTY_RESPONSE_MAX_RETRIES for the wrapper's lazy import."""
        mock_settings = MagicMock()
        mock_settings.LLM_EMPTY_RESPONSE_MAX_RETRIES = max_retries
        return patch("src.backend.core.config.settings", mock_settings)

    # ── Happy path: no retry needed ───────────────────────────────────────

    def test_non_empty_string_no_retry(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", return_value="real content") as mock_call:
            result = wrapper.call(["msg"])
        assert result == "real content"
        assert mock_call.call_count == 1
        mock_sleep.assert_not_called()
        assert wrapper._monitor.empty_response_retries == 0
        assert wrapper._monitor.empty_response_recoveries == 0
        assert wrapper._monitor.empty_response_failures == 0

    def test_zero_string_is_valid_content_not_empty(self):
        """A response of '0' is content, not empty — must not trigger retry."""
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", return_value="0") as mock_call:
            result = wrapper.call(["msg"])
        assert result == "0"
        assert mock_call.call_count == 1
        mock_sleep.assert_not_called()

    def test_false_string_is_valid_content_not_empty(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", return_value="False") as mock_call:
            result = wrapper.call(["msg"])
        assert result == "False"
        assert mock_call.call_count == 1

    # ── Retry triggers ────────────────────────────────────────────────────

    def test_none_triggers_retry_then_recovers(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", side_effect=[None, "recovered"]) as mock_call:
            result = wrapper.call(["msg"])
        assert result == "recovered"
        assert mock_call.call_count == 2
        mock_sleep.assert_called_once_with(0.5)  # first backoff
        assert wrapper._monitor.empty_response_retries == 1
        assert wrapper._monitor.empty_response_recoveries == 1
        assert wrapper._monitor.empty_response_failures == 0

    def test_empty_string_triggers_retry_then_recovers(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", "recovered"]):
            result = wrapper.call(["msg"])
        assert result == "recovered"
        assert wrapper._monitor.empty_response_retries == 1
        assert wrapper._monitor.empty_response_recoveries == 1

    def test_whitespace_only_triggers_retry(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["  \n\t", "recovered"]):
            result = wrapper.call(["msg"])
        assert result == "recovered"
        assert wrapper._monitor.empty_response_retries == 1
        assert wrapper._monitor.empty_response_recoveries == 1

    def test_two_empties_then_recovery_within_budget(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", side_effect=["", "", "recovered"]) as mock_call:
            result = wrapper.call(["msg"])
        assert result == "recovered"
        assert mock_call.call_count == 3
        # Exponential backoff: 0.5s, then 1.0s
        assert [c.args[0] for c in mock_sleep.call_args_list] == [0.5, 1.0]
        assert wrapper._monitor.empty_response_retries == 2
        assert wrapper._monitor.empty_response_recoveries == 1
        assert wrapper._monitor.empty_response_failures == 0

    # ── Retry exhaustion ──────────────────────────────────────────────────

    def test_all_empties_returns_empty_and_records_failure(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", "", ""]) as mock_call:
            result = wrapper.call(["msg"])
        assert result == ""  # caller (CrewAI) will raise on empty
        assert mock_call.call_count == 3
        assert wrapper._monitor.empty_response_retries == 2
        assert wrapper._monitor.empty_response_recoveries == 0
        assert wrapper._monitor.empty_response_failures == 1

    def test_all_none_returns_none_and_records_failure(self):
        """All-None case must return None unchanged so CrewAI's None check triggers."""
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=[None, None, None]):
            result = wrapper.call(["msg"])
        assert result is None
        assert wrapper._monitor.empty_response_failures == 1

    # ── max_retries=0 disables retry ──────────────────────────────────────

    def test_max_retries_zero_disables_retry_on_empty(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(0), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", side_effect=["", "would-recover"]) as mock_call:
            result = wrapper.call(["msg"])
        assert result == ""
        assert mock_call.call_count == 1  # no retry
        mock_sleep.assert_not_called()
        assert wrapper._monitor.empty_response_retries == 0
        assert wrapper._monitor.empty_response_failures == 1

    def test_max_retries_zero_happy_path_unchanged(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(0), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", return_value="content") as mock_call:
            result = wrapper.call(["msg"])
        assert result == "content"
        assert mock_call.call_count == 1
        mock_sleep.assert_not_called()

    # ── Non-string non-None pass-through ──────────────────────────────────

    def test_dict_result_pass_through_no_retry(self):
        wrapper = self._make_wrapper()
        tool_call = {"tool": "x", "args": {}}
        with self._patch_settings(2), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", return_value=tool_call) as mock_call:
            result = wrapper.call(["msg"])
        assert result is tool_call
        assert mock_call.call_count == 1
        mock_sleep.assert_not_called()

    def test_empty_then_dict_counts_as_recovery(self):
        """Empty then tool-call object: retry happened, treated as recovered."""
        wrapper = self._make_wrapper()
        tool_call = {"tool": "x"}
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", tool_call]):
            result = wrapper.call(["msg"])
        assert result is tool_call
        assert wrapper._monitor.empty_response_retries == 1
        assert wrapper._monitor.empty_response_recoveries == 1
        assert wrapper._monitor.empty_response_failures == 0

    # ── Exceptions propagate ──────────────────────────────────────────────

    def test_exception_propagates_no_retry(self):
        """Exceptions are LiteLLM's job (num_retries); we must not catch them."""
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", side_effect=RuntimeError("boom")) as mock_call:
            with pytest.raises(RuntimeError, match="boom"):
                wrapper.call(["msg"])
        assert mock_call.call_count == 1
        mock_sleep.assert_not_called()
        assert wrapper._monitor.empty_response_retries == 0

    # ── Backoff schedule ──────────────────────────────────────────────────

    def test_backoff_is_exponential(self):
        wrapper = self._make_wrapper()
        with self._patch_settings(5), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", side_effect=["", "", "", "", "", "ok"]):
            wrapper.call(["msg"])
        # 0.5, 1.0, 2.0, 4.0, 5.0 (capped at 5.0)
        sleeps = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleeps == [0.5, 1.0, 2.0, 4.0, 5.0]

    def test_backoff_capped_at_5_seconds(self):
        """Even at the max retry index, backoff never exceeds 5s."""
        wrapper = self._make_wrapper()
        with self._patch_settings(5), patch("time.sleep") as mock_sleep, \
             patch("crewai.llm.LLM.call", side_effect=["", "", "", "", "", ""]):
            wrapper.call(["msg"])
        sleeps = [c.args[0] for c in mock_sleep.call_args_list]
        assert max(sleeps) <= 5.0

    # ── Logging ───────────────────────────────────────────────────────────

    def test_retry_emits_warning_log(self, caplog):
        import logging
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", "ok"]):
            with caplog.at_level(logging.WARNING, logger="src.backend.crew_ai.cleaned_llm_wrapper"):
                wrapper.call(["msg"])
        msgs = [r.getMessage() for r in caplog.records]
        assert any("[LLM_RETRY]" in m and "Empty response" in m and "retry 1/2" in m for m in msgs)

    def test_recovery_emits_info_log(self, caplog):
        import logging
        wrapper = self._make_wrapper()
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", "ok"]):
            with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.cleaned_llm_wrapper"):
                wrapper.call(["msg"])
        msgs = [r.getMessage() for r in caplog.records]
        assert any("[LLM_RETRY] Recovered" in m for m in msgs)

    def test_exhaustion_emits_error_log(self, caplog):
        import logging
        wrapper = self._make_wrapper()
        with self._patch_settings(1), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", ""]):
            with caplog.at_level(logging.ERROR, logger="src.backend.crew_ai.cleaned_llm_wrapper"):
                wrapper.call(["msg"])
        msgs = [r.getMessage() for r in caplog.records]
        assert any("[LLM_RETRY]" in m and "giving up" in m for m in msgs)

    # ── Messages reused on retry (cost-saving invariant) ──────────────────

    def test_retry_reuses_same_messages_no_tool_reinvoke(self):
        """The same `messages` list (containing tool results) must be sent every retry.

        This is the invariant that prevents browser-use from being re-invoked.
        """
        wrapper = self._make_wrapper()
        messages = [{"role": "user", "content": "find element"},
                    {"role": "assistant", "content": "[tool result with locator]"}]
        with self._patch_settings(2), patch("time.sleep"), \
             patch("crewai.llm.LLM.call", side_effect=["", "ok"]) as mock_call:
            wrapper.call(messages)
        # Both calls must receive the identical messages list (same tool results).
        assert mock_call.call_count == 2
        for call in mock_call.call_args_list:
            assert call.args[0] is messages
