"""
crewai 1.15.44 wraps every subclass-defined LLM.call in its own 429 retry loop
(crewai/llms/base_llm.py __init_subclass__ -> crewai/llms/retry.py). Owner
decision (2026-11-24): the wrapper's own retry (provider_retry.py) is the ONLY
429 retry, so one always-429 call sends 5 requests, not 15, and the report's
"5 tries" is true.

The request counts go through the REAL CleanedLLMWrapper, the REAL crewai
LLM.call and a mocked litellm.completion -- nothing between them is patched.
"""

import contextvars
import json
from unittest.mock import MagicMock, patch

import litellm
import pytest
from crewai.llms import retry as crewai_retry
from crewai.llms.base_llm import BaseLLM

from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper
from src.backend.crew_ai.provider_retry import ProviderRetryPolicy, report_of

MODEL = "vertex_ai/gemini-3.5-flash"
MSGS = [{"role": "user", "content": "hi"}]


def _wrapper():
    w = CleanedLLMWrapper(model=MODEL, num_retries=0, is_litellm=True)
    w._provider_retry = ProviderRetryPolicy(60.0)
    return w


def _settings():
    s = MagicMock()
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = 2
    return patch("src.backend.core.config.settings", s)


def _always_429(exc):
    calls = []

    def completion(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise exc

    return completion, calls


def _per_day_429():
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "You exceeded your current quota.",
                      "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                   "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    return litellm.RateLimitError(message="VertexAIException - " + json.dumps(body),
                                  llm_provider="gemini", model="gemini-3.5-flash")


class TestRequestCountsThroughTheRealWrapper:
    def test_always_429_sends_five_requests_and_reports_five_tries(self):
        w = _wrapper()
        exc = litellm.RateLimitError(message="RESOURCE_EXHAUSTED", llm_provider="vertex_ai",
                                     model="gemini-3.5-flash")
        completion, calls = _always_429(exc)
        with _settings(), patch("time.sleep") as sleep, patch("litellm.completion", side_effect=completion):
            with pytest.raises(litellm.RateLimitError) as raised:
                w.call(MSGS)
        assert len(calls) == 5
        assert report_of(raised.value).tries == 5
        assert report_of(raised.value).rejections == 5
        # The wrapper's four backoffs. crewai's own loop binds time.sleep as a default argument,
        # so this mock never sees it: the request count above is the real guard.
        assert len(sleep.call_args_list) == 4

    def test_per_day_quota_429_sends_one_request(self):
        w = _wrapper()
        completion, calls = _always_429(_per_day_429())
        with _settings(), patch("time.sleep") as sleep, patch("litellm.completion", side_effect=completion):
            with pytest.raises(litellm.RateLimitError):
                w.call(MSGS)
        assert len(calls) == 1
        sleep.assert_not_called()


class TestCrewaiHooksStillExist:
    """Fails loudly when a crewai upgrade renames what the fix relies on."""

    def test_the_contextvar_exists(self):
        var = crewai_retry._active_llm_rate_limit_retry
        assert isinstance(var, contextvars.ContextVar)
        assert var.get() is False

    def test_the_marker_decides_whether_crewai_wraps_a_subclass_call(self):
        class Marked(BaseLLM):
            def call(self, *a, **k):
                return "x"
            call._crewai_rate_limit_wrapped = True

        class Plain(BaseLLM):
            def call(self, *a, **k):
                return "x"

        assert not hasattr(Marked.__dict__["call"], "__wrapped__")  # left as defined
        assert hasattr(Plain.__dict__["call"], "__wrapped__")  # crewai wrapped it
        assert Plain.__dict__["call"]._crewai_rate_limit_wrapped is True

    def test_our_call_is_not_crewais_wrapper(self):
        call = CleanedLLMWrapper.__dict__["call"]
        assert call._crewai_rate_limit_wrapped is True
        assert not hasattr(call, "__wrapped__")
        assert call.__qualname__ == "CleanedLLMWrapper.call"

    def test_the_wrapper_defines_no_acall(self):
        """Not wrapped by us, so crewai's own acall wrapper stays; the pipeline never awaits it."""
        assert "acall" not in CleanedLLMWrapper.__dict__
