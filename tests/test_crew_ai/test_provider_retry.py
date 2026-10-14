"""
Tests for src.backend.crew_ai.provider_retry — the per-try timeout schedule,
the retry budget and the error classifier (owner design D3, 2026-09-24).
"""

import json

import httpx
import litellm
import pytest
from litellm import exceptions as E

from src.backend.crew_ai.provider_retry import (
    MIN_TRY_S,
    REJECTION_RETRIES,
    RETRY_DELAY_CAP_S,
    ProviderRetryPolicy,
    RetryReport,
    attach_report,
    classify_provider_error,
    names_per_day_quota,
    provider_retry_delay_s,
    report_of,
)

_REQ = httpx.Request("POST", "https://example.invalid/v1")


def _ai_studio_429(quota_id, retry_delay):
    """A Google AI Studio 429 as litellm 1.75.3 wraps it (body shape: vercel/ai#18627)."""
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
    return E.RateLimitError(message="VertexAIException - " + json.dumps(body, indent=2),
                            llm_provider="gemini", model="gemini-3.5-flash")


# Every Vertex 429 captured (2026-09-16, 2026-09-24) looks like this: no quotaId, no delay.
_VERTEX_429 = E.RateLimitError(
    message=('VertexAIException - {\n  "error": {\n    "code": 429,\n    "message": "Resource '
             'exhausted. Please try again later.",\n    "status": "RESOURCE_EXHAUSTED"\n  }\n}\n'),
    llm_provider="vertex_ai", model="gemini-3.5-flash")


def _resp(code):
    return httpx.Response(code, request=_REQ)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _budget(base=60.0, jitter=0.0):
    clock = FakeClock()
    budget = ProviderRetryPolicy(base).new_budget(clock=clock, jitter=lambda: jitter)
    return budget, clock


class TestClassify:
    def test_timeouts(self):
        assert classify_provider_error(E.Timeout(message="t", model="m", llm_provider="vertex_ai")) == "timeout"

    @pytest.mark.parametrize("exc", [
        E.RateLimitError(message="429", llm_provider="vertex_ai", model="m"),
        E.ServiceUnavailableError(message="503", llm_provider="vertex_ai", model="m"),
        E.InternalServerError(message="500", llm_provider="vertex_ai", model="m"),
        E.APIConnectionError(message="reset", llm_provider="vertex_ai", model="m"),
        E.APIError(status_code=500, message="500", llm_provider="vertex_ai", model="m"),
        E.APIError(status_code=502, message="502", llm_provider="vertex_ai", model="m"),
    ])
    def test_rejections(self, exc):
        assert classify_provider_error(exc) == "rejected"

    @pytest.mark.parametrize("exc", [
        E.BadRequestError(message="400", model="m", llm_provider="vertex_ai"),
        E.ContextWindowExceededError(message="too long", model="m", llm_provider="vertex_ai"),
        E.ContentPolicyViolationError(message="blocked", model="m", llm_provider="vertex_ai"),
        E.UnsupportedParamsError(message="bad param", llm_provider="vertex_ai", model="m"),
        E.AuthenticationError(message="401", llm_provider="vertex_ai", model="m"),
        E.PermissionDeniedError(message="403", llm_provider="vertex_ai", model="m", response=_resp(403)),
        E.NotFoundError(message="404", model="m", llm_provider="vertex_ai"),
        E.UnprocessableEntityError(message="422", model="m", llm_provider="vertex_ai", response=_resp(422)),
        E.JSONSchemaValidationError(model="m", llm_provider="vertex_ai", raw_response="{}", schema="{}"),
        E.BudgetExceededError(current_cost=2.0, max_budget=1.0),
        E.APIError(status_code=400, message="400", llm_provider="vertex_ai", model="m"),
        ValueError("not a provider error"),
        RuntimeError("crewai or our own bug"),
    ])
    def test_fatal(self, exc):
        assert classify_provider_error(exc) == "fatal"

    def test_timeout_is_checked_before_connection_errors(self):
        """litellm.Timeout also descends from openai.APIConnectionError."""
        import openai
        exc = litellm.Timeout(message="t", model="m", llm_provider="gemini")
        assert isinstance(exc, openai.APIConnectionError)
        assert classify_provider_error(exc) == "timeout"


class TestSchedule:
    def test_default_schedule_and_deadline(self):
        policy = ProviderRetryPolicy(60)
        assert policy.schedule_s == (60.0, 120.0, 240.0)
        assert policy.deadline_s == 450.0

    def test_three_timeouts_walk_the_schedule_then_give_up(self):
        budget, clock = _budget()
        seen = []
        for _ in range(3):
            timeout = budget.next_timeout_s()
            seen.append(timeout)
            budget.start_try(timeout)
            clock.advance(timeout)
            wait = budget.record_failure("timeout")
        assert seen == [60.0, 120.0, 240.0]
        assert wait is None
        assert budget.next_timeout_s() is None
        report = budget.report("vertex_ai/gemini-3.5-flash", litellm.Timeout("t", "m", "vertex_ai"))
        assert report.tries == 3
        assert report.timeouts == 3
        assert report.timed_out_after_s == (60.0, 120.0, 240.0)
        assert report.elapsed_s == 420.0
        assert report.last_error == "Timeout"

    def test_a_timeout_is_retried_without_waiting(self):
        budget, clock = _budget()
        budget.start_try(budget.next_timeout_s())
        clock.advance(60)
        assert budget.record_failure("timeout") == 0.0


class TestRejections:
    def test_backoff_doubles_with_equal_jitter_bounds(self):
        low, _ = _budget(jitter=0.0)
        high, _ = _budget(jitter=0.999999)
        waits_low, waits_high = [], []
        for _ in range(REJECTION_RETRIES):
            low.start_try(low.next_timeout_s())
            waits_low.append(low.record_failure("rejected"))
            high.start_try(high.next_timeout_s())
            waits_high.append(high.record_failure("rejected"))
        assert waits_low == [0.5, 1.0, 2.0, 4.0]
        assert [round(w, 3) for w in waits_high] == [1.0, 2.0, 4.0, 8.0]

    def test_fifth_rejection_gives_up(self):
        budget, _ = _budget()
        for _ in range(REJECTION_RETRIES):
            budget.start_try(budget.next_timeout_s())
            assert budget.record_failure("rejected") is not None
        budget.start_try(budget.next_timeout_s())
        assert budget.record_failure("rejected") is None
        assert budget.tries == REJECTION_RETRIES + 1

    def test_a_rejection_does_not_advance_the_schedule(self):
        budget, clock = _budget()
        budget.start_try(budget.next_timeout_s())
        clock.advance(1)
        budget.record_failure("rejected")
        assert budget.next_timeout_s() == 60.0

    def test_fatal_never_retries_and_counts_nothing(self):
        budget, _ = _budget()
        budget.start_try(budget.next_timeout_s())
        assert budget.record_failure("fatal") is None
        assert (budget.timeouts, budget.rejections) == (0, 0)


class TestDeadline:
    def test_slow_rejections_cap_the_last_try(self):
        """Two timeouts (180 s) + a 503 that took 230 s leave 39.5 s, not 240 s."""
        budget, clock = _budget()
        for _ in range(2):
            budget.start_try(budget.next_timeout_s())
            clock.advance(budget.next_timeout_s())
            budget.record_failure("timeout")
        budget.start_try(budget.next_timeout_s())
        clock.advance(230)
        wait = budget.record_failure("rejected")
        assert wait == 0.5
        clock.advance(wait)
        assert budget.next_timeout_s() == pytest.approx(450 - 410.5)

    def test_no_try_starts_with_under_min_try_left(self):
        budget, clock = _budget()
        budget.start_try(budget.next_timeout_s())
        clock.advance(450 - MIN_TRY_S + 0.1)
        assert budget.record_failure("rejected") is None
        assert budget.next_timeout_s() is None


class TestGoogleAiStudio429:
    def test_per_day_quota_is_fatal(self):
        exc = _ai_studio_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "34s")
        assert classify_provider_error(exc) == "fatal"
        assert names_per_day_quota(str(exc))

    def test_per_minute_quota_is_retried_after_the_named_delay(self):
        exc = _ai_studio_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "34.4s")
        assert classify_provider_error(exc) == "rejected"
        assert provider_retry_delay_s(exc) == 34.4
        assert not names_per_day_quota(str(exc))

    def test_a_named_delay_is_capped_at_a_minute(self):
        exc = _ai_studio_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "300s")
        assert provider_retry_delay_s(exc) == RETRY_DELAY_CAP_S

    def test_a_vertex_429_names_no_delay_and_keeps_the_backoff(self):
        assert classify_provider_error(_VERTEX_429) == "rejected"
        assert provider_retry_delay_s(_VERTEX_429) is None

    def test_the_budget_waits_the_named_delay_plus_jitter(self):
        low, _ = _budget(jitter=0.0)
        high, _ = _budget(jitter=0.999999)
        low.start_try(low.next_timeout_s())
        high.start_try(high.next_timeout_s())
        assert low.record_failure("rejected", retry_delay_s=34.4) == 34.4
        assert round(high.record_failure("rejected", retry_delay_s=34.4), 3) == 35.4

    def test_a_named_delay_past_the_deadline_gives_up_now(self):
        budget, clock = _budget()
        budget.start_try(budget.next_timeout_s())
        clock.advance(420)
        assert budget.record_failure("rejected", retry_delay_s=34.4) is None


class TestReportAttachment:
    def test_round_trip(self):
        exc = litellm.Timeout(message="t", model="m", llm_provider="vertex_ai")
        report = RetryReport("vertex_ai/m", 3, 3, 0, (60.0, 120.0, 240.0), 420.0, "Timeout")
        attach_report(exc, report)
        assert report_of(exc) is report

    def test_absent_report_is_none(self):
        assert report_of(ValueError("x")) is None


class TestRealLiteLLMMapping:
    """Pins what LiteLLM 1.75.3 really raises for provider statuses, and how W5 sorts it.

    PR #115 review (2026-09-25): a chain-based "transport only" rule for
    APIConnectionError was declined because these gateway statuses arrive as a
    cause-free APIConnectionError and must stay retryable. A local HTTP server
    answers each status; only 127.0.0.1 is contacted.
    """

    @staticmethod
    def _serve(status, content_type, body):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Reply(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Reply)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @pytest.mark.parametrize("status,content_type,body,kind", [
        (408, "application/json", b'{"error": {"code": 408, "message": "t", "status": "DEADLINE_EXCEEDED"}}', "timeout"),
        (502, "application/json", b'{"error": {"code": 502, "message": "Bad Gateway"}}', "rejected"),
        (502, "text/html", b"<html>502 Bad Gateway</html>", "rejected"),
        (504, "application/json", b'{"error": {"code": 504, "message": "d", "status": "DEADLINE_EXCEEDED"}}', "rejected"),
        (520, "text/html", b"<html>520</html>", "rejected"),
        (200, "text/html", b"<html>not json</html>", "rejected"),
        (503, "application/json", b'{"error": {"code": 503, "message": "u", "status": "UNAVAILABLE"}}', "rejected"),
        (400, "application/json", b'{"error": {"code": 400, "message": "b", "status": "INVALID_ARGUMENT"}}', "fatal"),
    ])
    def test_gemini_statuses_are_sorted_from_the_real_mapping(self, status, content_type, body, kind):
        server = self._serve(status, content_type, body)
        try:
            with pytest.raises(Exception) as raised:
                litellm.completion(model="gemini/gemini-3.5-flash", api_key="k", num_retries=0, timeout=5,
                                   messages=[{"role": "user", "content": "hi"}],
                                   api_base=f"http://127.0.0.1:{server.server_address[1]}/v1/models/gemini-3.5-flash")
        finally:
            server.shutdown()
            server.server_close()
        assert classify_provider_error(raised.value) == kind, (
            f"HTTP {status} -> {type(raised.value).__name__} sorted as {classify_provider_error(raised.value)}")
