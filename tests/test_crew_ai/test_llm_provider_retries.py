"""
Tests for CleanedLLMWrapper's provider retry loop (W5, owner design D3).

crewai's LLM.call is patched where the test only needs the loop's decisions;
the token tests go through the REAL crewai LLM.call and the real
litellm.completion, driven offline by LiteLLM's mock_timeout / mock_response.
"""

import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import litellm
import pytest

from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper
from src.backend.crew_ai.provider_retry import ProviderRetryPolicy, RetryBudget, report_of

MODEL = "vertex_ai/gemini-3.5-flash"


def _wrapper(base=60.0):
    w = CleanedLLMWrapper(model=MODEL, num_retries=0, is_litellm=True)
    w._provider_retry = ProviderRetryPolicy(base)
    return w


def _settings(empty_retries=2):
    s = MagicMock()
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = empty_retries
    return patch("src.backend.core.config.settings", s)


def _timeout():
    return litellm.Timeout(message="timed out", model="gemini-3.5-flash", llm_provider="vertex_ai")


def _rate_limited():
    return litellm.RateLimitError(message="RESOURCE_EXHAUSTED", llm_provider="vertex_ai",
                                  model="gemini-3.5-flash")


def _recording(wrapper, outcomes):
    """side_effect for LLM.call: records wrapper.timeout per try, then raises or returns."""
    seen = []

    def fake_call(*args, **kwargs):
        seen.append(wrapper.timeout)
        outcome = outcomes[len(seen) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return fake_call, seen


class TestTimeoutSchedule:
    def test_two_timeouts_then_success_returns_the_answer(self):
        w = _wrapper()
        fake, seen = _recording(w, [_timeout(), _timeout(), "the plan"])
        with _settings(), patch("time.sleep") as sleep, patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) == "the plan"
        assert seen == [60.0, 120.0, 240.0]
        sleep.assert_not_called()
        assert w._monitor.provider_timeouts == 2
        assert w._monitor.provider_failures == 0

    def test_final_timeout_is_the_original_litellm_exception(self):
        w = _wrapper()
        errors = [_timeout(), _timeout(), _timeout()]
        fake, _ = _recording(w, errors)
        with _settings(), patch("time.sleep"), patch("crewai.llm.LLM.call", side_effect=fake):
            with pytest.raises(litellm.Timeout) as raised:
                w.call(["msg"])
        assert raised.value is errors[2]
        assert type(raised.value).__module__.startswith("litellm")
        report = report_of(raised.value)
        assert report.tries == 3
        assert report.timed_out_after_s == (60.0, 120.0, 240.0)
        assert report.model == MODEL
        assert w._monitor.provider_timeouts == 3
        assert w._monitor.provider_failures == 1


class TestRejections:
    def test_rate_limits_back_off_and_keep_the_first_timeout(self):
        w = _wrapper()
        fake, seen = _recording(w, [_rate_limited(), _rate_limited(), "ok"])
        with _settings(), patch("time.sleep") as sleep, \
             patch("src.backend.crew_ai.provider_retry._SYSTEM_RANDOM.random", return_value=0.0), \
             patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) == "ok"
        assert seen == [60.0, 60.0, 60.0]
        assert [c.args[0] for c in sleep.call_args_list] == [0.5, 1.0]
        assert w._monitor.provider_rejections == 2

    def test_fifth_rejection_raises_the_rate_limit(self):
        w = _wrapper()
        errors = [_rate_limited() for _ in range(5)]
        fake, seen = _recording(w, errors)
        with _settings(), patch("time.sleep"), patch("crewai.llm.LLM.call", side_effect=fake):
            with pytest.raises(litellm.RateLimitError) as raised:
                w.call(["msg"])
        assert raised.value is errors[4]
        assert len(seen) == 5
        assert report_of(raised.value).rejections == 5


def _ai_studio_429(quota_id, retry_delay):
    """A Google AI Studio 429 as litellm 1.75.3 wraps it (body shape: vercel/ai#18627)."""
    import json
    body = {"error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": quota_id}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
        ],
    }}
    return litellm.RateLimitError(message="VertexAIException - " + json.dumps(body, indent=2),
                                  llm_provider="gemini", model="gemini-3.5-flash")


class TestGoogleAiStudio429s:
    def test_per_minute_429_waits_the_delay_google_names(self):
        w = _wrapper()
        exc = _ai_studio_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "34.4s")
        fake, seen = _recording(w, [exc, "ok"])
        with _settings(), patch("time.sleep") as sleep, \
             patch("src.backend.crew_ai.provider_retry._SYSTEM_RANDOM.random", return_value=0.0), \
             patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) == "ok"
        assert [c.args[0] for c in sleep.call_args_list] == [34.4]
        assert seen == [60.0, 60.0]

    def test_per_day_429_fails_at_once(self):
        w = _wrapper()
        exc = _ai_studio_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "34s")
        fake, seen = _recording(w, [exc])
        with _settings(), patch("time.sleep") as sleep, patch("crewai.llm.LLM.call", side_effect=fake):
            with pytest.raises(litellm.RateLimitError) as raised:
                w.call(["msg"])
        assert raised.value is exc
        assert len(seen) == 1
        sleep.assert_not_called()


class TestNoRetry:
    @pytest.mark.parametrize("exc", [
        litellm.BadRequestError(message="400", model="m", llm_provider="vertex_ai"),
        litellm.AuthenticationError(message="401", llm_provider="vertex_ai", model="m"),
        ValueError("crewai-side"),
    ])
    def test_fatal_error_is_raised_at_once_and_unchanged(self, exc):
        w = _wrapper()
        fake, seen = _recording(w, [exc])
        with _settings(), patch("time.sleep") as sleep, patch("crewai.llm.LLM.call", side_effect=fake):
            with pytest.raises(type(exc)) as raised:
                w.call(["msg"])
        assert raised.value is exc
        assert len(seen) == 1
        sleep.assert_not_called()
        assert report_of(exc) is None

    def test_a_wrapper_without_a_policy_never_retries(self):
        """The local (Ollama) wrapper: no timeout set, the first error propagates."""
        w = CleanedLLMWrapper(model="ollama/llama3", num_retries=3, is_litellm=True)
        assert w._provider_retry is None
        fake, seen = _recording(w, [_timeout()])
        with _settings(), patch("time.sleep") as sleep, patch("crewai.llm.LLM.call", side_effect=fake):
            with pytest.raises(litellm.Timeout):
                w.call(["msg"])
        assert seen == [None]
        sleep.assert_not_called()


class TestEmptyResponses:
    def test_empty_then_timeout_then_answer_share_one_budget(self):
        w = _wrapper()
        fake, seen = _recording(w, ["", _timeout(), "recovered"])
        with _settings(), patch("time.sleep"), patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) == "recovered"
        assert seen == [60.0, 60.0, 120.0]
        assert w._monitor.empty_response_retries == 1
        assert w._monitor.provider_timeouts == 1

    def test_empty_response_with_no_time_left_is_not_retried(self):
        w = _wrapper()
        fake, seen = _recording(w, ["", "never reached"])
        with _settings(), patch("time.sleep"), \
             patch.object(RetryBudget, "next_timeout_s", side_effect=[60.0, None]), \
             patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) == ""
        assert len(seen) == 1
        assert w._monitor.empty_response_failures == 1


class _FakeClock:
    """The budget's clock, injected: only tries and sleeps move it; a sleep runs overshoot_s long."""

    def __init__(self, overshoot_s=0.0):
        self.now = 1000.0
        self.overshoot_s = overshoot_s
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds + self.overshoot_s

    def installed(self):
        """Patch the clock the budget binds when call() creates it, and the wrapper's sleep."""
        stack = ExitStack()
        stack.enter_context(patch("src.backend.crew_ai.provider_retry.time.monotonic",
                                  side_effect=lambda: self.now))
        stack.enter_context(patch("src.backend.crew_ai.cleaned_llm_wrapper.time.sleep",
                                  side_effect=self.sleep))
        return stack


def _timed(wrapper, clock, tries):
    """side_effect for LLM.call: each (seconds, outcome) try records wrapper.timeout,
    moves the clock by its seconds, then raises or returns."""
    seen = []

    def fake_call(*args, **kwargs):
        seen.append(wrapper.timeout)
        seconds, outcome = tries[len(seen) - 1]
        clock.now += seconds
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return fake_call, seen


class TestNoUntimedTry:
    """No sleep may sit between the budget allowing a try and the try (review fix round 1)."""

    def test_empty_retry_backoff_that_spends_the_budget_gives_up(self):
        w = _wrapper()  # deadline 60 + 120 + 240 + 30 = 450 s
        clock = _FakeClock()
        fake, seen = _timed(w, clock, [(60.0, _timeout()), (120.0, _timeout()), (239.0, ""),
                                       (25.3, ""), (1.0, "must not be reached")])
        with _settings(), clock.installed(), patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) == ""
        # 5.2 s were left before the 1.0 s backoff, 4.2 s (< MIN_TRY_S) after it.
        assert None not in seen
        assert seen == [60.0, 120.0, 240.0, 30.5]
        assert clock.sleeps == [0.5, 1.0]
        assert w._monitor.empty_response_failures == 1
        assert w._monitor.empty_response_retries == 1

    def test_rejection_backoff_that_overshoots_the_budget_raises_the_original_error(self):
        w = _wrapper()
        clock = _FakeClock(overshoot_s=0.05)
        errors = [_timeout(), _timeout(), _rate_limited(), _rate_limited()]
        fake, seen = _timed(w, clock, [(60.0, errors[0]), (120.0, errors[1]), (239.0, errors[2]),
                                       (24.425, errors[3]), (1.0, "must not be reached")])
        with _settings(), clock.installed(), \
             patch("src.backend.crew_ai.provider_retry._SYSTEM_RANDOM.random", return_value=0.0), \
             patch("crewai.llm.LLM.call", side_effect=fake):
            with pytest.raises(litellm.RateLimitError) as raised:
                w.call(["msg"])
        # 6.025 s were left when the 1.0 s backoff was granted; the sleep ran 1.05 s.
        assert raised.value is errors[3]
        assert None not in seen
        assert seen == pytest.approx([60.0, 120.0, 240.0, 30.45])
        assert clock.sleeps == [0.5, 1.0]
        report = report_of(raised.value)
        assert (report.tries, report.timeouts, report.rejections) == (4, 2, 2)
        assert w._monitor.provider_failures == 1

    def test_a_try_the_budget_refuses_is_never_sent(self):
        """Defensive: the clock crosses MIN_TRY_S between call()'s check and the next try."""
        w = _wrapper()
        fake, seen = _recording(w, ["", "must not be reached"])
        with _settings(), patch("time.sleep"), \
             patch.object(RetryBudget, "next_timeout_s", side_effect=[60.0, 31.0, 31.0, None, None]), \
             patch("crewai.llm.LLM.call", side_effect=fake):
            assert w.call(["msg"]) is None
        assert seen == [60.0]
        assert w._monitor.empty_response_failures == 1


class TestThroughRealCrewaiAndLitellm:
    """No patch on LLM.call: real crewai params, real litellm.completion, offline mocks."""

    def _spy(self, mocks):
        real = litellm.completion
        calls = []

        def spy(*args, **kwargs):
            calls.append({"timeout": kwargs.get("timeout"), "num_retries": kwargs.get("num_retries")})
            if kwargs.get("timeout") is None:
                # Fail fast: without a per-try timeout, LiteLLM's mock_timeout
                # sleeps its 600 s default (found by the plan's dry run).
                raise AssertionError("no per-try timeout reached litellm.completion")
            kwargs.update(mocks[len(calls) - 1])
            return real(*args, **kwargs)

        return spy, calls

    def test_timed_out_tries_bill_no_tokens(self):
        w = _wrapper(base=0.05)
        spy, calls = self._spy([{"mock_timeout": True}, {"mock_timeout": True},
                                {"mock_response": "hello"}])
        before = dict(w._token_usage)
        with _settings(), patch("litellm.completion", side_effect=spy):
            assert w.call([{"role": "user", "content": "hi"}]) == "hello"
        assert calls == [{"timeout": 0.05, "num_retries": 0},
                         {"timeout": 0.1, "num_retries": 0},
                         {"timeout": 0.2, "num_retries": 0}]
        delta = {k: w._token_usage[k] - before.get(k, 0)
                 for k in ("prompt_tokens", "completion_tokens", "successful_requests")}
        assert delta == {"prompt_tokens": 10, "completion_tokens": 20, "successful_requests": 1}

    def test_all_tries_time_out_through_crewai(self):
        w = _wrapper(base=0.05)
        spy, calls = self._spy([{"mock_timeout": True}] * 3)
        before = dict(w._token_usage)
        with _settings(), patch("litellm.completion", side_effect=spy):
            with pytest.raises(litellm.Timeout) as raised:
                w.call([{"role": "user", "content": "hi"}])
        assert len(calls) == 3
        assert report_of(raised.value).timed_out_after_s == (0.05, 0.1, 0.2)
        assert w._token_usage == before


class TestSharedPlannerAssemblerState:
    def test_both_wrappers_get_the_policy_and_still_share_monitor_and_usage(self):
        from src.backend.crew_ai.agents import RobotAgents

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "p", "VERTEXAI_LOCATION": "global"}):
            agents = RobotAgents("vertex", "gemini-3.5-flash", MagicMock())
        assert agents.llm._provider_retry is not None
        assert agents.planner_llm._provider_retry == agents.llm._provider_retry
        assert agents.planner_llm._monitor is agents.llm._monitor
        assert agents.planner_llm._token_usage is agents.llm._token_usage
