"""
Per-try timeouts and retry decisions for cloud LLM calls.

Owner design D3 (2026-09-24): a cloud LLM call gets growing per-try timeouts
(base x 1, 2, 4 -> 60 s, 120 s, 240 s at the default base) and
CleanedLLMWrapper -- not LiteLLM -- decides what to retry:

- a timeout moves to the next, longer try at once (the wait already happened);
- a provider rejection (429, 5xx, a dropped connection) is retried after an
  exponential backoff with jitter, on its own budget of REJECTION_RETRIES, and
  does not move the schedule on, because the provider did answer;
- a Google AI Studio 429 that names a per-day quota is never retried (it
  resets at midnight Pacific); one that names a retryDelay waits that long
  (capped at 60 s) instead of the backoff;
- anything else (bad request, auth, context window, schema) is never retried;
- one deadline, sum(schedule) + BACKOFF_ALLOWANCE_S (450 s at the default
  base), bounds the whole CleanedLLMWrapper.call(), empty-response retries
  included.

Why the wrapper and not LiteLLM: LiteLLM 1.75.3's num_retries path (the client
wrapper in litellm/utils.py, then completion_with_retries in litellm/main.py)
retries every openai.APIError subclass, 400 and 401 included, with no wait
between tries. On 2026-09-24 that lost q03 to four 429s in 3.0 s and cost q10
rep 3 four ~598 s tries (2,388 s in all).

Pure: no crewai import and no I/O. The clock and the jitter source are
injectable for tests.

Referenced by: crew_ai/cleaned_llm_wrapper.py (CleanedLLMWrapper.call, get_llm),
               core/provider_errors.py (friendly_provider_error reads the report).
Depends on: litellm (exception classes only).
"""

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import litellm

logger = logging.getLogger(__name__)

# Per-try timeout = base x multiplier (owner D3, 2026-09-24).
TIMEOUT_MULTIPLIERS: tuple[int, ...] = (1, 2, 4)
# Rejections (429 / 5xx / dropped connection) get their own budget. Equal
# jitter: each wait is drawn from [d/2, d] with d = 1, 2, 4, 8 s. The widest
# 429 window measured (2026-09-16, 2026-09-24) was about 2 s.
REJECTION_RETRIES = 4
REJECTION_BACKOFF_BASE_S = 1.0
REJECTION_BACKOFF_CAP_S = 8.0
# Deadline headroom beyond sum(schedule): twice the worst-case plain backoff
# (1 + 2 + 4 + 8 = 15 s). A wait the provider names (AI Studio retryDelay, up to
# 61 s each) is not covered: it spends schedule time, and one that would pass the
# deadline gives up at once instead of sleeping.
BACKOFF_ALLOWANCE_S = 30.0
# Never start a try with less than this left before the deadline.
MIN_TRY_S = 5.0
# Attribute carried by the exception a call() finally raises.
REPORT_ATTR = "nlrf_provider_retry"
# Google AI Studio (gemini provider) 429 bodies name the exhausted quota and the
# wait: QuotaFailure "quotaId" and RetryInfo "retryDelay" (published real bodies:
# vercel/ai#18627, inspect_ai#5526; both survive into str(RateLimitError) in
# litellm 1.75.3). Every Vertex 429 captured carries neither.
_PER_DAY_QUOTA = re.compile(r'"quotaId"\s*:\s*"[^"]*PerDay', re.IGNORECASE)
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')
# A per-minute quota window never needs more than a minute.
RETRY_DELAY_CAP_S = 60.0

FailureKind = Literal["timeout", "rejected", "fatal"]

# Jitter only, not security; SystemRandom keeps static analysis quiet.
_SYSTEM_RANDOM = random.SystemRandom()


def names_per_day_quota(text: str) -> bool:
    """True when a provider error names a per-day quota (resets at midnight Pacific)."""
    return bool(_PER_DAY_QUOTA.search(text))


def provider_retry_delay_s(exc: BaseException) -> float | None:
    """The wait the provider asked for, capped at RETRY_DELAY_CAP_S; None if it names none."""
    match = _RETRY_DELAY.search(str(exc))
    return min(float(match.group(1)), RETRY_DELAY_CAP_S) if match else None


def classify_provider_error(exc: BaseException) -> FailureKind:
    """Sort one failed try: 'timeout', 'rejected' (retry after a backoff) or 'fatal'.

    Timeout is checked first because litellm.Timeout also descends from
    openai.APIConnectionError. LiteLLM maps Vertex/Gemini 408 and 504 to
    Timeout, 429 to RateLimitError, 500 and "The model is overloaded." to
    InternalServerError, 503 to ServiceUnavailableError, and anything it cannot
    map to APIConnectionError (litellm_core_utils/exception_mapping_utils.py).
    A 429 on a per-day quota is fatal: nothing succeeds before the daily reset.
    """
    if isinstance(exc, litellm.Timeout):
        return "timeout"
    if isinstance(exc, litellm.RateLimitError) and names_per_day_quota(str(exc)):
        return "fatal"
    if isinstance(exc, (litellm.RateLimitError, litellm.ServiceUnavailableError,
                        litellm.InternalServerError, litellm.APIConnectionError)):
        return "rejected"
    if isinstance(exc, litellm.APIError) and (getattr(exc, "status_code", None) or 0) >= 500:
        return "rejected"
    return "fatal"


@dataclass(frozen=True)
class RetryReport:
    """What one call() went through before it gave up. Read for the SSE message.

    timed_out_after_s: the per-try LIMIT of each try that timed out, in order —
    not the time the try actually took. A fast HTTP 408 or 504 also counts as a
    timeout, so a value here does not mean the try ran that long.
    """
    model: str
    tries: int
    timeouts: int
    rejections: int
    timed_out_after_s: tuple[float, ...]
    elapsed_s: float
    last_error: str


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Per-wrapper constants. Budgets, not policies, carry per-call state."""
    base_timeout_s: float

    @property
    def schedule_s(self) -> tuple[float, ...]:
        return tuple(float(self.base_timeout_s) * m for m in TIMEOUT_MULTIPLIERS)

    @property
    def deadline_s(self) -> float:
        return sum(self.schedule_s) + BACKOFF_ALLOWANCE_S

    def new_budget(self, clock: Callable[[], float] | None = None,
                   jitter: Callable[[], float] | None = None) -> "RetryBudget":
        return RetryBudget(self, clock or time.monotonic, jitter or _SYSTEM_RANDOM.random)


class RetryBudget:
    """Retry state for ONE CleanedLLMWrapper.call(). Never shared across calls."""

    def __init__(self, policy: ProviderRetryPolicy, clock: Callable[[], float],
                 jitter: Callable[[], float]):
        self._policy = policy
        self._clock = clock
        self._jitter = jitter
        self._started = clock()
        self._current_timeout_s = 0.0
        self._timed_out_after: list[float] = []
        self.tries = 0
        self.timeouts = 0
        self.rejections = 0

    def _remaining_s(self) -> float:
        return self._policy.deadline_s - (self._clock() - self._started)

    def next_timeout_s(self) -> float | None:
        """Timeout for the next try, or None when no further try may start."""
        if self.timeouts >= len(TIMEOUT_MULTIPLIERS):
            return None
        remaining = self._remaining_s()
        if remaining < MIN_TRY_S:
            return None
        return min(self._policy.schedule_s[self.timeouts], remaining)

    def start_try(self, timeout_s: float) -> None:
        self.tries += 1
        self._current_timeout_s = timeout_s

    def record_failure(self, kind: FailureKind, retry_delay_s: float | None = None) -> float | None:
        """Count one failed try.

        Returns the seconds to wait before the next try, or None when the call
        must give up: a fatal error, a spent budget, or too little time left.
        A rejection whose provider named a wait (retry_delay_s) waits that long
        plus up to 1 s of jitter; any other rejection uses the equal-jitter backoff.
        """
        if kind == "fatal":
            return None
        if kind == "timeout":
            self.timeouts += 1
            self._timed_out_after.append(self._current_timeout_s)
            wait_s = 0.0
        else:
            self.rejections += 1
            if self.rejections > REJECTION_RETRIES:
                return None
            if retry_delay_s is not None:
                wait_s = retry_delay_s + self._jitter()
            else:
                ceiling = min(REJECTION_BACKOFF_CAP_S,
                              REJECTION_BACKOFF_BASE_S * 2 ** (self.rejections - 1))
                wait_s = ceiling / 2 + self._jitter() * ceiling / 2
        if self.timeouts >= len(TIMEOUT_MULTIPLIERS):
            return None
        if self._remaining_s() - wait_s < MIN_TRY_S:
            return None
        return wait_s

    def report(self, model: str, exc: BaseException) -> RetryReport:
        return RetryReport(
            model=model,
            tries=self.tries,
            timeouts=self.timeouts,
            rejections=self.rejections,
            timed_out_after_s=tuple(self._timed_out_after),
            elapsed_s=round(self._clock() - self._started, 1),
            last_error=type(exc).__name__,
        )


def attach_report(exc: BaseException, report: RetryReport) -> None:
    """Carry the report on the exception itself. Never raises.

    LiteLLM does the same with litellm_response_headers. The exception object
    is re-raised unchanged, so crewai still passes it through as a litellm error.
    """
    try:
        setattr(exc, REPORT_ATTR, report)
    except Exception:
        logger.debug("could not attach the retry report to %s", type(exc).__name__, exc_info=True)


def report_of(exc: BaseException) -> RetryReport | None:
    report = getattr(exc, REPORT_ATTR, None)
    return report if isinstance(report, RetryReport) else None
