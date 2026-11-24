"""
W5.1: crewai's salvage (Converter) call is sent as a schema call through the wrapper.

When an agent's answer fails output_pydantic validation, crewai 1.15.44's Converter
calls llm.call(messages=..., response_model=Model) with no from_task/from_agent
(crewai/utilities/converter.py:103-106). crewai sends that through instructor: no
timeout, no thinking guard, no workflow metadata, and instructor retries itself.
The wrapper now sends it as a normal response_format call inside its W5 retry
budget, and a salvage that gave up after W5's retries is not sent again by the
Converter's next attempts (owner D12, "B-lite"): one budget per salvage.

Offline: litellm.completion is spied and answered with LiteLLM's mock_response /
mock_timeout, or made to raise a provider error; instructor is patched to fail
loudly if it is ever reached.

Referenced by: nothing (test module).
Depends on: src/backend/crew_ai/cleaned_llm_wrapper.py, crewai.utilities.converter.
"""

import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import litellm
import pytest
from crewai import Agent, Crew, Process, Task
from crewai.utilities.converter import Converter, ConverterError
from pydantic import BaseModel

from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper, get_llm
from src.backend.crew_ai.provider_retry import ProviderRetryPolicy, report_of

# Production's model. Whether a model enforces a response schema, and whether the
# Converter takes its response_model branch at all (supports_function_calling),
# comes from LiteLLM's cost map; litellm 1.105.3's bundled map (used offline, or
# with LITELLM_LOCAL_MODEL_COST_MAP=True) answers True for both on gemini-3.8-flash.
MODEL = "vertex_ai/gemini-3.8-flash"
STEPS = ["open the page", "search"]
VALID = json.dumps({"steps": STEPS})
MESSAGES = [{"role": "user", "content": "x"}]


class Out(BaseModel):
    steps: list[str]


class Other(BaseModel):
    name: str


def _wrapper(base=60.0, response_format=None):
    extra = {"response_format": response_format} if response_format is not None else {}
    w = CleanedLLMWrapper(model=MODEL, num_retries=0, is_litellm=True, **extra)
    w._provider_retry = ProviderRetryPolicy(base)
    return w


def _settings():
    # Explicit, not the config default: the empty-answer test's request count follows from it.
    s = MagicMock()
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = 2
    s.LLM_REQUEST_TIMEOUT_S = 60
    return patch("src.backend.core.config.settings", s)


def _no_instructor():
    # crewai 1.15.44 sends a non-streaming response_model call through this method
    # (crewai/llm.py:1329).
    return patch("crewai.utilities.internal_instructor.InternalInstructor.to_pydantic_with_completion",
                 side_effect=AssertionError("the instructor route was used"))


def _no_waits():
    """W5's backoffs and the empty-response backoff sleep; the tests need not."""
    return patch("time.sleep")


@contextmanager
def _no_crewai_telemetry():
    with patch.dict(os.environ, {"CREWAI_DISABLE_TELEMETRY": "true", "OTEL_SDK_DISABLED": "true",
                                 "CREWAI_TRACING_ENABLED": "false"}):
        yield


# Provider failures as LiteLLM 1.105.3 raises them. A factory, not an instance:
# a real provider raises a NEW exception object on every request.
def _timeout():
    return litellm.Timeout(message="litellm.Timeout: t", model="gemini-3.8-flash",
                           llm_provider="vertex_ai")


def _unavailable():
    return litellm.ServiceUnavailableError(message="VertexAIException - 503 UNAVAILABLE",
                                           llm_provider="vertex_ai", model="gemini-3.8-flash")


def _server_error():
    return litellm.InternalServerError(message="VertexAIException - 500 INTERNAL",
                                       llm_provider="vertex_ai", model="gemini-3.8-flash")


def _gateway_504():
    # litellm 1.105.3 maps a vertex/gemini 504 to Timeout (_map_exception_by_status;
    # TestRealLiteLLMMapping in tests/test_crew_ai/test_provider_retry.py). 1.75.3
    # raised APIConnectionError.
    return litellm.Timeout(message="VertexAIException - 504 Gateway Timeout", model="gemini-3.8-flash",
                           llm_provider="vertex_ai", exception_status_code=504)


def _connection_error():
    # LiteLLM's catch-all for an error it cannot map; W5 retries it as a rejection.
    return litellm.APIConnectionError(message="VertexAIException - connection reset",
                                      llm_provider="vertex_ai", model="gemini-3.8-flash")


def _rate_limited():
    return litellm.RateLimitError(message="VertexAIException - 429 Resource exhausted, quota exceeded",
                                  llm_provider="vertex_ai", model="gemini-3.8-flash")


def _bad_request():
    # status_code defaults to 400: fatal (provider_retry._FATAL_BAD_REQUEST_STATUSES).
    return litellm.BadRequestError(message="VertexAIException - 400 INVALID_ARGUMENT",
                                   model="gemini-3.8-flash", llm_provider="vertex_ai")


def _per_day_429():
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    return litellm.RateLimitError(message="VertexAIException - " + json.dumps(body),
                                  llm_provider="gemini", model="gemini-3.8-flash")


def _spy(answers):
    """Answer the i-th litellm.completion with answers[i]: a dict of LiteLLM mock
    kwargs, or a factory whose exception is raised in place of the request."""
    real = litellm.completion
    seen = []

    def spy(*args, **kwargs):
        seen.append(dict(kwargs))
        if kwargs.get("timeout") is None:
            # Fail fast: without a per-try timeout, LiteLLM's mock_timeout
            # sleeps its 600 s default.
            raise AssertionError("no per-try timeout reached litellm.completion")
        answer = answers[len(seen) - 1]
        if callable(answer):
            raise answer()
        if not (answer.get("mock_response") or answer.get("mock_timeout")):
            # LiteLLM treats a falsy mock_response ("") as no mock at all and sends
            # the request for real (litellm 1.105.3 main.py:5765): refuse here
            # instead of reaching the provider.
            raise AssertionError(f"spy answer {answer!r} has no mock; it would reach the provider")
        return real(*args, **{**kwargs, **answer})

    return spy, seen


def _record_calls(log):
    """Log what every CleanedLLMWrapper.call() raised (None when it returned)."""
    original = CleanedLLMWrapper.call

    def recording(self, *args, **kwargs):
        try:
            result = original(self, *args, **kwargs)
        except Exception as exc:
            log.append(exc)
            raise
        log.append(None)
        return result

    return patch.object(CleanedLLMWrapper, "call", autospec=True, side_effect=recording)


def _convert(llm, **kwargs):
    return Converter(llm=llm, text="this is not JSON", model=Out,
                     instructions="Return the steps as JSON.", **kwargs).to_pydantic()


class TestWhichCallsAreRouted:
    def test_only_a_call_without_task_or_agent_keys_is_routed(self):
        w = _wrapper()
        assert w._salvage_response_model({"response_model": Out}) is Out
        assert w._salvage_response_model({"response_model": Out, "from_task": object()}) is None
        assert w._salvage_response_model({"response_model": Out, "from_agent": object()}) is None
        # Key PRESENCE decides, not the value: the exact complement of the scoped
        # response_model pop in _call_cleaned. crewai's own stop re-entry passes None.
        assert w._salvage_response_model({"response_model": Out, "from_task": None}) is None
        assert w._salvage_response_model({"response_model": None, "from_task": None}) is None
        assert w._salvage_response_model({}) is None

    def test_a_model_without_schema_support_is_never_routed(self):
        w = CleanedLLMWrapper(model="ollama/llama3", is_litellm=True)
        assert w._salvage_response_model({"response_model": Out}) is None

    def test_an_ollama_salvage_takes_the_converters_plain_branch(self):
        """supports_function_calling is False for ollama models, so crewai's Converter
        never passes response_model there (converter.py:102-108): one plain call, as before."""
        with _settings():
            w = get_llm("local", "llama3", response_format=Out)
        assert w.response_format is None
        assert w.supports_function_calling() is False
        seen = []

        def fake(self, messages, *args, **kwargs):
            seen.append((self.response_format, sorted(kwargs)))
            return VALID

        with _settings(), patch("crewai.llm.LLM.call", autospec=True, side_effect=fake):
            assert _convert(w) == Out(steps=STEPS)
        assert seen == [(None, [])]

    def test_the_memory_starts_empty_and_is_per_wrapper(self):
        """On crewai 1.15 the class default becomes a Pydantic private attribute that
        BaseLLM.__init__ (called by CleanedLLMWrapper.__new__) sets on each instance.
        Without the default, reading it raises AttributeError."""
        first, second = _wrapper(), _wrapper()
        assert first._salvage_give_up is None
        first._salvage_give_up = (Out, _timeout())
        assert second._salvage_give_up is None


class TestTheRoutedCall:
    @pytest.mark.parametrize("own_format", [None, Out])
    def test_a_salvage_call_is_one_schema_call_inside_the_budget(self, own_format):
        w = _wrapper(response_format=own_format)
        spy, seen = _spy([{"mock_response": VALID}])
        before = w._token_usage["successful_requests"]
        with _settings(), _no_instructor(), patch("litellm.completion", side_effect=spy):
            assert _convert(w) == Out(steps=STEPS)
        assert len(seen) == 1
        assert seen[0]["response_format"] is Out
        assert seen[0]["timeout"] == 60.0
        assert seen[0]["num_retries"] == 0
        assert w._token_usage["successful_requests"] - before == 1
        assert w.response_format is own_format
        assert w._salvage_give_up is None

    def test_the_wire_carries_the_thinking_guard_and_the_workflow_id(self):
        spy, seen = _spy([{"mock_response": VALID}])
        with _settings(), _no_instructor(), \
             patch.dict(os.environ, {"VERTEXAI_PROJECT": "p", "VERTEXAI_LOCATION": "global"}):
            w = get_llm("vertex", "gemini-3.8-flash")
            w.set_workflow_id("wf-w51")
            with patch("litellm.completion", side_effect=spy):
                assert _convert(w) == Out(steps=STEPS)
        assert len(seen) == 1
        assert seen[0]["response_format"] is Out
        assert seen[0]["timeout"] == 60.0
        assert seen[0]["num_retries"] == 0
        assert seen[0]["thinkingConfig"] == {"thinkingBudget": 0}
        assert seen[0]["metadata"]["workflow_id"] == "wf-w51"
        assert w._token_usage["successful_requests"] == 1

    def test_response_format_is_restored_when_the_salvage_call_fails(self):
        w = _wrapper()
        bad = _bad_request()
        during = []

        def boom(messages, *args, **kwargs):
            during.append((w.response_format, "response_model" in kwargs))
            raise bad

        with _settings(), _no_instructor(), patch("crewai.llm.LLM.call", side_effect=boom):
            with pytest.raises(litellm.BadRequestError) as raised:
                w.call(MESSAGES, response_model=Out)
        assert raised.value is bad
        assert during == [(Out, False)]
        assert w.response_format is None

    def test_an_empty_salvage_answer_is_retried_inside_the_same_budget(self):
        """crewai's LLM.call is faked here: LiteLLM cannot mock an empty answer
        (mock_response="" is treated as no mock and goes to the provider)."""
        w = _wrapper()
        seen, log = [], []
        answers = ["", VALID]

        def fake(self, messages, *args, **kwargs):
            seen.append((self.response_format, self.timeout, "response_model" in kwargs))
            return answers[len(seen) - 1]

        with _settings(), _no_instructor(), _no_waits(), _record_calls(log), \
                patch("crewai.llm.LLM.call", autospec=True, side_effect=fake):
            assert _convert(w) == Out(steps=STEPS)
        # ONE Converter attempt (one call() that returned): the empty answer was
        # retried by the wrapper's own empty-response loop, inside the same budget.
        assert log == [None]
        assert seen == [(Out, 60.0, False), (Out, 60.0, False)]
        assert w.response_format is None


class TestOneBudgetPerSalvage:
    """Owner decision D12 = B-lite. The Converter retries any error up to
    max_attempts (3), back to back. Without the routing each attempt is an
    instructor call with no timeout; with routing alone, a fresh W5 budget per
    attempt. After W5.1: one W5 budget per salvage."""

    @pytest.mark.parametrize("answer", [{"mock_timeout": True}, _gateway_504], ids=["hung", "504"])
    def test_a_hung_salvage_costs_one_budget_not_three(self, answer):
        w = _wrapper(base=0.05)
        spy, seen = _spy([answer] * 3)
        log = []
        with _settings(), _no_instructor(), _record_calls(log), \
                patch("litellm.completion", side_effect=spy):
            with pytest.raises(ConverterError) as raised:
                _convert(w)
        assert [c["timeout"] for c in seen] == [0.05, 0.1, 0.2]
        assert len(log) == 3
        assert log[0] is log[1] is log[2]
        assert isinstance(log[0], litellm.Timeout)
        assert report_of(log[0]).tries == 3
        assert raised.value.__cause__ is log[0]

    @pytest.mark.parametrize("error", [_unavailable, _server_error, _connection_error, _rate_limited],
                             ids=["503", "500", "APIConnectionError", "429"])
    def test_a_rejected_salvage_is_not_sent_again(self, error):
        w = _wrapper()
        spy, seen = _spy([error] * 5)
        log = []
        with _settings(), _no_instructor(), _no_waits(), _record_calls(log), \
                patch("litellm.completion", side_effect=spy):
            with pytest.raises(ConverterError) as raised:
                _convert(w)
        assert len(seen) == 5
        assert len(log) == 3
        assert log[0] is log[1] is log[2]
        assert report_of(log[0]).rejections == 5
        assert raised.value.__cause__ is log[0]

    @pytest.mark.parametrize("error", [_bad_request, _per_day_429], ids=["400", "per-day-429"])
    def test_a_fatal_salvage_error_is_not_remembered(self, error):
        """A fatal error carries no retry report (W5 re-raises it at once), so nothing
        is stored: the Converter's 3 attempts are 3 quick requests, as today."""
        w = _wrapper()
        spy, seen = _spy([error] * 3)
        log = []
        with _settings(), _no_instructor(), _no_waits(), _record_calls(log), \
                patch("litellm.completion", side_effect=spy):
            with pytest.raises(ConverterError):
                _convert(w)
        assert len(seen) == 3
        assert len({id(exc) for exc in log}) == 3
        assert all(report_of(exc) is None for exc in log)
        assert w._salvage_give_up is None

    def test_a_salvage_answer_that_fails_validation_is_not_remembered(self):
        w = _wrapper()
        spy, seen = _spy([{"mock_response": json.dumps({"steps": "not a list"})},
                          {"mock_response": VALID}])
        with _settings(), _no_instructor(), patch("litellm.completion", side_effect=spy):
            assert _convert(w) == Out(steps=STEPS)
        assert len(seen) == 2
        assert w._salvage_give_up is None

    def test_any_other_call_clears_the_memory(self):
        w = _wrapper(base=0.05)
        spy, seen = _spy([{"mock_timeout": True}] * 3
                         + [{"mock_response": "plain"}, {"mock_response": VALID}])
        with _settings(), _no_instructor(), patch("litellm.completion", side_effect=spy):
            with pytest.raises(litellm.Timeout):
                w.call(MESSAGES, response_model=Out)
            assert w._salvage_give_up is not None
            # The executor's shape (agent_utils.py:766-773): from_task/from_agent keys present.
            assert w.call(MESSAGES, callbacks=[], from_task=None, from_agent=None,
                          response_model=None) == "plain"
            assert w._salvage_give_up is None
            assert w.call(MESSAGES, response_model=Out) == VALID
        assert len(seen) == 5

    def test_a_salvage_for_another_model_is_sent(self):
        w = _wrapper(base=0.05)
        other = json.dumps({"name": "n"})
        spy, seen = _spy([{"mock_timeout": True}] * 3 + [{"mock_response": other}])
        with _settings(), _no_instructor(), patch("litellm.completion", side_effect=spy):
            with pytest.raises(litellm.Timeout):
                w.call(MESSAGES, response_model=Out)
            assert w.call(MESSAGES, response_model=Other) == other
        assert len(seen) == 4
        assert seen[3]["response_format"] is Other
        assert w._salvage_give_up is None

    def test_crewais_stop_reentry_is_not_routed_and_the_outer_give_up_is_kept(self):
        """crewai/llm.py:2072-2080 re-enters self.call(..., from_task=None, from_agent=None,
        response_model=None) after an "Unsupported parameter 'stop'" error. That inner
        call is not a salvage (the keys are present): it clears the memory and runs its
        own W5 budget; the OUTER routed call still stores its own give-up."""
        w = _wrapper(base=0.05)

        def unsupported_stop():
            return litellm.BadRequestError(message="Unsupported parameter: 'stop'",
                                           model="gemini-3.8-flash", llm_provider="vertex_ai")

        spy, seen = _spy([unsupported_stop] + [{"mock_timeout": True}] * 5)
        log = []
        with _settings(), _no_instructor(), _record_calls(log), \
                patch("litellm.completion", side_effect=spy):
            with pytest.raises(ConverterError) as raised:
                _convert(w)
        # Outer try 1 (0.05 s) hits the stop error; the inner call's own budget
        # times out at 0.05/0.1/0.2 s; the outer budget then counts that as its
        # first timeout and tries 0.1 and 0.2 s.
        assert [c["timeout"] for c in seen] == [0.05, 0.05, 0.1, 0.2, 0.1, 0.2]
        assert [c["response_format"] for c in seen] == [Out] * 6
        inner, outer = log[0], log[1]
        assert inner is not outer
        assert log[1:] == [outer, outer, outer]
        assert raised.value.__cause__ is outer
        assert report_of(outer).tries == 3


class TestARealCrew:
    @staticmethod
    def _crew(w):
        agent = Agent(role="r", goal="g", backstory="b", llm=w, verbose=False, allow_delegation=False)
        task = Task(description="d", expected_output="e", agent=agent, output_pydantic=Out)
        return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)

    def test_a_real_crew_salvages_a_bad_answer_with_one_timed_schema_call(self):
        w = _wrapper()
        spy, seen = _spy([{"mock_response": "Final Answer: this is not JSON at all"},
                          {"mock_response": VALID}])
        with _no_crewai_telemetry(), _settings(), _no_instructor(), \
                patch("litellm.completion", side_effect=spy):
            result = self._crew(w).kickoff()
        assert result.pydantic == Out(steps=STEPS)
        assert len(seen) == 2
        assert seen[0].get("response_format") is None      # the agent's own call: unchanged
        assert seen[1]["response_format"] is Out
        assert seen[1]["timeout"] == 60.0
        # crewai 1.15 scopes the executor's stop words to its own call
        # (agent_utils.py:367-370, call_stop_override): the salvage carries none.
        assert seen[0]["stop"] == ["\nObservation:"]
        assert "stop" not in seen[1]
        assert w._token_usage["successful_requests"] == 2
        assert w.response_format is None

    def test_the_agents_own_call_clears_a_remembered_give_up(self):
        w = _wrapper()
        w._salvage_give_up = (Out, _timeout())
        spy, seen = _spy([{"mock_response": "Final Answer: this is not JSON at all"},
                          {"mock_response": VALID}])
        with _no_crewai_telemetry(), _settings(), _no_instructor(), \
                patch("litellm.completion", side_effect=spy):
            result = self._crew(w).kickoff()
        assert result.pydantic == Out(steps=STEPS)
        assert len(seen) == 2
        assert w._salvage_give_up is None
