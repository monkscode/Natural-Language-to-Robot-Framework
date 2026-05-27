"""
Shared Learning Infrastructure — Used by all learning engines.

This module provides the foundational components for the Adaptive Learning System:

1. LEARNING_CONFIG - Central configuration dictionary
2. EffectivenessScore - MDES scoring (absolute formula from raw counts)
3. LearningCircuitBreaker - Kill switch with auto-disable on high error rate
4. LearningWriteQueue - Non-blocking single-writer thread for SQLite writes
5. LearningEngine - Abstract base class for all learning engines

Referenced by: Every learning engine (DAY_01 through DAY_21).
"""

import json
import time
import queue
import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Literal

logger = logging.getLogger(__name__)


# Single canonical name for the LearningWriteQueue worker thread.
# Engines assert against this in their write methods so any code path that
# bypasses the queue and writes from a request/workflow thread fails loudly.
WRITER_THREAD_NAME = "learning-writer"


# ---------------------------------------------------------------------------
# 1. Central Configuration
# ---------------------------------------------------------------------------

LEARNING_CONFIG = {
    # Database paths
    "EXECUTION_MEMORY_DB": "data/execution_memory.db",
    "CHROMADB_DIR": "data/learning_chromadb",

    # Scoring thresholds
    "INJECTION_THRESHOLD": 0.4,         # Min MDES score for hint injection
    "MIN_OBSERVATIONS": 3,              # Min data points before injection
    "HIGH_PRIORITY_THRESHOLD": 0.7,     # Gets top slot in hint selection

    # Complexity tiers (for hint injection budget)
    "COMPLEXITY_TIERS": {
        "simple":  {"max_steps": 3,  "max_hints": 5,  "tokens_per_hint": 80},
        "medium":  {"max_steps": 5,  "max_hints": 8,  "tokens_per_hint": 100},
        "complex": {"max_steps": 99, "max_hints": 10, "tokens_per_hint": 120},
    },
    "HARD_CAP_HINTS": 10,               # Never exceed

    # Natural comparison
    "NATURAL_COMPARISON_MIN_SAMPLE_SIZE": 20,

    # Circuit breaker
    "ERROR_RATE_THRESHOLD": 0.2,        # Disable if >20% errors
    "MIN_CALLS_BEFORE_CHECK": 10,       # Don't check rate until 10+ calls
    "RECOVERY_TIMEOUT_SECONDS": 300,    # Half-open probe after 5 minutes

    # Performance
    "DEDUPLICATION_THRESHOLD": 5,       # Aggregate after 5 identical runs
}


# ---------------------------------------------------------------------------
# 2. MDES Effectiveness Score
# ---------------------------------------------------------------------------

class EffectivenessScore:
    """
    Multi-Dimensional Effectiveness Score.

    score = base_effectiveness * staleness_weight
    base_effectiveness = successes / (successes + failures + 1)  # Laplace smoothing

    CRITICAL DESIGN RULES:
    - Score is ALWAYS recalculated from raw counts (absolute formula)
    - NO incremental updates (no `score += 0.1`)
    - Decays ONLY on contradicting executions (failures), NOT on time passing
    - Mild staleness penalty only for rules unused >90 days

    Future-proof: Score is directly usable as RL reward signal if reinforcement
    learning is added later.
    """

    INJECTION_THRESHOLD = 0.4
    HIGH_PRIORITY = 0.7
    MIN_OBSERVATIONS = 3

    @staticmethod
    def calculate(successes: int, failures: int,
                  last_used_days_ago: int = 0) -> float:
        """Calculate effectiveness from raw counts. Always absolute, never incremental."""
        base = successes / (successes + failures + 1)

        # Staleness: NOT a daily decay.
        # Rules used within 90 days: full score
        # Rules unused 90-180 days: 5% penalty
        # Rules unused 180+ days: 15% penalty
        if last_used_days_ago > 180:
            staleness = 0.85
        elif last_used_days_ago > 90:
            staleness = 0.95
        else:
            staleness = 1.0

        return round(base * staleness, 4)

    @staticmethod
    def passes_threshold(successes: int, failures: int,
                         last_used_days_ago: int = 0,
                         min_observations: int = 3,
                         threshold: float = 0.4) -> bool:
        """Rule must have enough data AND high enough score."""
        if (successes + failures) < min_observations:
            return False
        score = EffectivenessScore.calculate(
            successes, failures, last_used_days_ago
        )
        return score >= threshold


# ---------------------------------------------------------------------------
# 3. Learning Circuit Breaker
# ---------------------------------------------------------------------------

class LearningCircuitBreaker:
    """
    Kill switch for learning operations with automatic recovery.

    Three states (standard circuit breaker pattern):
    - CLOSED: Normal operation. All requests pass through.
    - OPEN: Error rate exceeded threshold. All requests are blocked.
              After RECOVERY_TIMEOUT_SECONDS, transitions to HALF_OPEN.
    - HALF_OPEN: One probe request is allowed through.
                 If it succeeds → CLOSED (counters reset).
                 If it fails → OPEN (timer restarts).

    Does NOT check OPTIMIZATION_ENABLED master switch directly (handled at call site).

    Usage in every engine:
        from src.backend.core.config import settings
        if not settings.OPTIMIZATION_ENABLED or not circuit_breaker.is_enabled():
            return  # Skip learning, pipeline continues normally
    """

    # State constants
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self):
        self._lock = threading.Lock()
        self._error_count = 0
        self._total_calls = 0
        self._error_threshold = LEARNING_CONFIG["ERROR_RATE_THRESHOLD"]
        self._min_calls = LEARNING_CONFIG["MIN_CALLS_BEFORE_CHECK"]
        self._recovery_timeout = LEARNING_CONFIG["RECOVERY_TIMEOUT_SECONDS"]
        self._state = self.CLOSED
        self._opened_at: Optional[float] = None  # time.monotonic() when OPEN

    def _is_error_rate_exceeded(self) -> bool:
        """Check if error rate exceeds threshold. Must be called with lock held."""
        return (
            self._total_calls >= self._min_calls
            and self._error_count / self._total_calls > self._error_threshold
        )

    def is_enabled(self) -> bool:
        """Check if learning system is enabled.

        Returns False if:
        - Circuit breaker is OPEN and recovery timeout hasn't elapsed
        Returns True if:
        - Circuit breaker is CLOSED
        - Circuit breaker is HALF_OPEN (allows one probe request)
        """
        with self._lock:
            if self._state == self.CLOSED:
                # Check if we should trip open
                if self._is_error_rate_exceeded():
                    self._state = self.OPEN
                    self._opened_at = time.monotonic()
                    logger.warning(
                        "[LEARNING] Circuit breaker OPEN: "
                        "%d/%d errors (%.0f%%). "
                        "Will probe again in %ds.",
                        self._error_count, self._total_calls,
                        (self._error_count / self._total_calls) * 100,
                        self._recovery_timeout,
                    )
                    return False
                return True

            elif self._state == self.OPEN:
                # Check if recovery timeout has elapsed
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self._recovery_timeout:
                    self._state = self.HALF_OPEN
                    logger.info(
                        "[LEARNING] Circuit breaker HALF_OPEN: "
                        "allowing one probe request after %.0fs.",
                        elapsed,
                    )
                    return True  # Allow the probe
                return False

            else:  # HALF_OPEN
                # Already in half-open: only one probe is allowed.
                # If we get here again before record_success/error,
                # block additional requests.
                return False

    def record_success(self):
        """Record a successful learning operation.

        If in HALF_OPEN state, the probe succeeded — reset to CLOSED.
        """
        with self._lock:
            self._total_calls += 1
            if self._state == self.HALF_OPEN:
                # Probe succeeded — reset counters and close
                self._error_count = 0
                self._total_calls = 0
                self._state = self.CLOSED
                self._opened_at = None
                logger.info(
                    "[LEARNING] Circuit breaker CLOSED: "
                    "probe succeeded, counters reset."
                )

    def record_error(self, error: Exception):
        """Record a failed learning operation (non-blocking).

        If in HALF_OPEN state, the probe failed — go back to OPEN.
        """
        with self._lock:
            self._total_calls += 1
            self._error_count += 1
            if self._state == self.HALF_OPEN:
                # Probe failed — re-open with fresh timeout
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "[LEARNING] Circuit breaker re-OPENED: "
                    "probe failed. Will retry in %ds. Error: %s",
                    self._recovery_timeout, error,
                )
                return
        logger.warning(f"[LEARNING] Learning error (non-blocking): {error}")

    def reset(self):
        """Manual reset — force transition to CLOSED."""
        with self._lock:
            self._error_count = 0
            self._total_calls = 0
            self._state = self.CLOSED
            self._opened_at = None
        logger.info("[LEARNING] Circuit breaker reset (manual)")

    def get_stats(self) -> Dict:
        """Return circuit breaker statistics.

        All values are computed inside a single lock scope to ensure
        consistency between counters and state.
        """
        with self._lock:
            total = self._total_calls
            errors = self._error_count
            state = self._state
        is_open = state != self.CLOSED
        return {
            "total_calls": total,
            "error_count": errors,
            "error_rate": (
                errors / total if total > 0 else 0.0
            ),
            "is_open": is_open,
            "state": state,
        }


# ---------------------------------------------------------------------------
# 4. Learning Write Queue
# ---------------------------------------------------------------------------

class LearningWriteQueue:
    """
    Non-blocking write queue for all learning system database writes.

    Principles:
    1. submit() is non-blocking — workflow continues immediately
    2. Single writer thread eliminates SQLITE_BUSY entirely
    3. SQLite WAL mode enables concurrent reads while writes happen
    4. Graceful shutdown drains pending writes before thread exits

    Usage:
        write_queue.submit(execution_memory.store_execution, record)
    """

    _SENTINEL = object()  # Poison pill for graceful shutdown

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._process_writes, daemon=True, name=WRITER_THREAD_NAME
        )
        self._worker.start()

    def submit(self, write_fn, *args, **kwargs):
        """Submit a write operation — never blocks the caller."""
        self._queue.put((write_fn, args, kwargs))

    def shutdown(self, timeout: float = 5.0):
        """Drain pending writes and stop the worker thread.

        Args:
            timeout: Max seconds to wait for pending writes to finish.
        """
        self._queue.put(self._SENTINEL)
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            logger.warning(
                "[LEARNING] Write queue shutdown timed out with %d pending writes",
                self._queue.qsize(),
            )

    def _process_writes(self):
        """Single thread processes all writes sequentially."""
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                # Drain remaining items before exiting
                while not self._queue.empty():
                    remaining = self._queue.get()
                    if remaining is self._SENTINEL:
                        continue
                    write_fn, args, kwargs = remaining
                    try:
                        write_fn(*args, **kwargs)
                    except Exception as e:
                        logger.warning(
                            f"[LEARNING] Write failed during shutdown: {e}"
                        )
                break
            write_fn, args, kwargs = item
            try:
                write_fn(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"[LEARNING] Write failed (non-blocking): {e}"
                )

    @property
    def pending(self) -> int:
        """Number of pending write operations."""
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# 5. LearningEngine Abstract Base Class
# ---------------------------------------------------------------------------

class LearningEngine(ABC):
    """
    Base class for all learning engines.

    Every engine must implement:
    - learn(): Process a new execution record and extract learnings
    - get_hints(): Return relevant hints for a given query context
    - get_stats(): Return engine-specific statistics for monitoring

    Engines MAY implement:
    - learn_from_feedback(): Process user NL feedback
    """

    @abstractmethod
    def learn(self, record) -> None:
        """
        Process an execution record and update internal knowledge.
        Called after EVERY execution (pass or fail).

        Args:
            record: ExecutionRecord from execution_memory
        """
        ...

    @abstractmethod
    def get_hints(self, user_query: str, url: str,
                  agent_role: str) -> Optional[List[str]]:
        """
        Return relevant hints for the given query/url/agent, or None if no hints.

        Hints must:
        - Score above INJECTION_THRESHOLD (0.4)
        - Have MIN_OBSERVATIONS (3+) confirming executions
        - Be relevant to the agent_role

        Args:
            user_query: The natural language test query
            url: Target website URL
            agent_role: "planner" | "identifier" | "assembler" | "validator"

        Returns:
            List of hint strings, or None if no relevant hints
        """
        ...

    @abstractmethod
    def get_stats(self) -> Dict:
        """
        Return engine statistics for monitoring/debugging.

        Should include at minimum:
        - total_rules: Number of stored rules
        - active_rules: Rules passing threshold
        - last_updated: Timestamp of last learning update
        """
        ...

    def learn_from_feedback(self, record, feedback_insight) -> None:
        """
        Optional: Process user NL feedback for this engine.
        Default implementation does nothing.

        Args:
            record: ExecutionRecord for the workflow being given feedback
            feedback_insight: Triaged feedback result from NLFeedbackEngine
        """
        pass


# ---------------------------------------------------------------------------
# 6. Repository Pattern Abstractions
# ---------------------------------------------------------------------------

class ExecutionStore(ABC):
    """
    Abstract execution storage interface.

    Current implementation: SQLite (ExecutionMemory).
    Future: PostgresExecutionStore for multi-tenant SaaS.

    Why this exists:
    - All learning engines depend on storage through this interface
    - Swapping SQLite for Postgres requires zero changes to engines
    """

    @abstractmethod
    def store_execution(self, record) -> None:
        """Store a complete execution record."""
        ...

    @abstractmethod
    def query_by_domain(self, domain: str, limit: int) -> list:
        """Get recent executions for a specific domain."""
        ...

    @abstractmethod
    def query_failures(self, category: str, limit: int) -> list:
        """Get failed executions, optionally filtered by category."""
        ...


class SemanticStore(ABC):
    """
    Abstract semantic search interface.

    Current implementation: ChromaDB (inside ExecutionMemory).
    Future: PineconeSemanticStore for cloud deployment.
    """

    @abstractmethod
    def store_embedding(self, text: str, metadata: dict) -> None:
        """Store text with metadata for semantic search."""
        ...

    @abstractmethod
    def search_similar(self, query: str, top_k: int) -> list:
        """Find semantically similar entries."""
        ...


# ---------------------------------------------------------------------------
# 7. Shared Utilities (re-exported from canonical location)
# ---------------------------------------------------------------------------

from src.backend.core.url_utils import extract_domain  # noqa: F401, E402


# ---------------------------------------------------------------------------
# 8. LLM Conflict Detection — provider routing helpers
# ---------------------------------------------------------------------------
#
# Used by Trigger 1 (workflow_service._fire_llm_conflict_detection) and
# Trigger 2 (feedback_loop.process_user_feedback). Defined here — NOT in
# workflow_service.py or feedback_loop.py — because both files already
# depend on learning_config; placing the helpers in either would force a
# circular import.
#
# Provider routing logic itself lives in llm_provider_routing.py — a
# dependency-free module shared with cleaned_llm_wrapper.get_llm(). These
# wrappers exist to keep settings reads at this layer (call sites use the
# `_get_*` names per the plan and shouldn't import settings themselves).


def _get_conflict_detection_model() -> str:
    """LiteLLM model string for conflict-detection completions.

    Delegates to llm_provider_routing — same prefix mapping as
    cleaned_llm_wrapper.get_llm(), so MODEL_PROVIDER + ONLINE_MODEL
    govern both agent calls and conflict-detection calls.
    """
    from src.backend.core.config import settings
    from src.backend.crew_ai.llm_provider_routing import resolve_model_string
    return resolve_model_string(settings.MODEL_PROVIDER, settings.ONLINE_MODEL)


def _get_conflict_detection_completion_kwargs() -> dict:
    """Per-provider extra kwargs for litellm.completion().

    Currently only Ollama needs api_base injected — litellm.completion()
    does NOT read OLLAMA_API_BASE from the env on its own.
    """
    from src.backend.core.config import settings
    from src.backend.crew_ai.llm_provider_routing import resolve_completion_kwargs
    return resolve_completion_kwargs(settings.MODEL_PROVIDER)


def _parse_conflict_json(content: str) -> dict:
    """Parse the LLM conflict-detection response, tolerating any preamble/postamble.

    Uses json.JSONDecoder.raw_decode() which starts parsing at the first '{'
    and stops when the JSON object closes — so markdown code fences, leading
    text, and trailing text are all ignored.  Raises json.JSONDecodeError if
    no '{' is found or if the JSON itself is malformed.
    """
    idx = content.find('{')
    if idx == -1:
        raise json.JSONDecodeError("No JSON object found in LLM response", content, 0)
    return json.JSONDecoder().raw_decode(content, idx)[0]


def _parse_review_response(
    content: str,
    known_hint_ids: set,
    zero_application_ids: set,
    hint_states: dict | None = None,
) -> dict:
    """Parse and validate the LLM hint review response.

    Returns {"decisions": [...valid...], "summary": str}
    Skips malformed items, unknown IDs, invalid recommendation values,
    reactivate recommendations for zero-application hints, and state-invalid
    recommendations (e.g. unflag on an active hint, disable on a disabled hint).
    Raises json.JSONDecodeError if no JSON object found at all.

    Args:
        hint_states: {hint_id: {"is_active": int, "conflict_flagged": int}}.
                     When provided, state-validity checks are enforced.

    Note: the LLM response includes a "warning" field but it is NOT returned
    here. _run_hint_review computes the authoritative warning from the parsed
    decisions via _compute_warning() — a deterministic Python calculation that
    cannot hallucinate. The LLM "warning" instruction in the prompt acts as a
    reasoning nudge only; its output is discarded.
    """
    ALLOWED_RECOMMENDATIONS = {"keep", "disable", "reactivate", "unflag", "flag_review"}
    ZERO_APP_ALLOWED = {"keep", "disable", "flag_review"}

    raw = _parse_conflict_json(content)

    decisions_raw = raw.get("decisions")
    if not isinstance(decisions_raw, list):
        raise ValueError(f"'decisions' missing or not a list: {raw}")

    valid_decisions = []
    seen_hint_ids: set = set()
    for item in decisions_raw:
        if not isinstance(item, dict):
            logger.warning("[REVIEW] Skipping non-dict decision item: %r", item)
            continue
        hint_id = item.get("id")
        recommendation = item.get("recommendation")
        reason = item.get("reason", "")
        if not isinstance(hint_id, int):
            logger.warning("[REVIEW] Skipping decision with non-int id: %r", item)
            continue
        if hint_id not in known_hint_ids:
            logger.warning("[REVIEW] Skipping decision for unknown hint_id=%d", hint_id)
            continue
        if hint_id in seen_hint_ids:
            logger.warning("[REVIEW] Skipping duplicate decision for hint_id=%d", hint_id)
            continue
        if recommendation not in ALLOWED_RECOMMENDATIONS:
            logger.warning("[REVIEW] Skipping invalid recommendation %r for hint %d",
                           recommendation, hint_id)
            continue
        if hint_id in zero_application_ids and recommendation not in ZERO_APP_ALLOWED:
            logger.warning(
                "[REVIEW] Skipping %r for zero-application hint %d (not allowed)",
                recommendation, hint_id,
            )
            continue
        # State-validity: guard against LLM recommending an action that is
        # a no-op or contradictory given the hint's current DB state.
        if hint_states is not None:
            state = hint_states.get(hint_id)
            if state is None:
                # known_hint_ids and hint_states are built from the same query,
                # so this should never happen. Log the anomaly and fall back to
                # State #1 defaults (is_active=1, conflict_flagged=0): unflag
                # and reactivate are rejected conservatively; disable/keep/
                # flag_review are allowed.
                logger.warning(
                    "[REVIEW] hint_id=%d in known_hint_ids but missing from "
                    "hint_states — falling back to State #1 defaults", hint_id,
                )
                state = {}
            is_active = state.get("is_active", 1)
            conflict_flagged = state.get("conflict_flagged", 0)
            if recommendation == "unflag":
                if not (is_active == 1 and conflict_flagged == 1):
                    logger.warning(
                        "[REVIEW] Skipping unflag for hint %d — "
                        "requires is_active=1 AND conflict_flagged=1 "
                        "(got is_active=%d, conflict_flagged=%d)",
                        hint_id, is_active, conflict_flagged,
                    )
                    continue
            elif recommendation == "reactivate":
                if is_active != 0:
                    logger.warning(
                        "[REVIEW] Skipping reactivate for active hint %d "
                        "(is_active=%d)", hint_id, is_active,
                    )
                    continue
            elif recommendation == "disable":
                if is_active != 1:
                    logger.warning(
                        "[REVIEW] Skipping disable for inactive hint %d "
                        "(is_active=%d)", hint_id, is_active,
                    )
                    continue
        if not isinstance(reason, str) or not reason.strip():
            logger.warning("[REVIEW] Skipping decision with empty reason for hint %d", hint_id)
            continue
        seen_hint_ids.add(hint_id)
        valid_decisions.append({
            "id": hint_id,
            "recommendation": recommendation,
            "reason": reason.strip(),
        })

    return {
        "decisions": valid_decisions,
        "summary": str(raw.get("summary", "")).strip(),
    }


def _call_conflict_detection_llm(
    model_string: str,
    messages: list,
    extra_kwargs: dict,
    timeout: int = 30,
):
    """Stream a conflict-detection completion and return a reassembled ModelResponse.

    Uses stream=True so Gemini 2.5 Flash extended-thinking tokens are sent
    incrementally. Without streaming, the server sends nothing until thinking
    completes (30+ seconds), causing a read timeout even though the connection
    is open. With streaming, the per-chunk read timeout applies instead, so
    the connection stays alive throughout the thinking phase.

    The model still reasons with full depth — stream=True changes how bytes
    are delivered, not what the model does. Thinking tokens arrive in
    delta.reasoning_content; the final JSON output arrives in delta.content.
    stream_chunk_builder assembles them into a standard ModelResponse so
    callers read choices[0].message.content and usage identically to a
    non-streaming response.  Callers must pass the content through
    _parse_conflict_json to handle any markdown wrapping the model may add.

    Raises RuntimeError if stream_chunk_builder returns None (no chunks
    received at all), so callers classify it as llm_error rather than
    json_parse_failed.
    """
    import litellm

    # Deterministic JSON judgment task — temperature=0 removes noise.
    # response_format eliminates the markdown-fence failure mode class;
    # _parse_conflict_json already tolerates fences as defense-in-depth.
    # Override is intentional: conflict detection always requires json_object.
    if "response_format" in extra_kwargs and \
            extra_kwargs["response_format"] != {"type": "json_object"}:
        logger.warning(
            "[CONFLICT_DETECT] caller's response_format=%r overridden "
            "to {'type': 'json_object'} — conflict detection requires JSON",
            extra_kwargs["response_format"],
        )
    extra_kwargs = {**extra_kwargs, "response_format": {"type": "json_object"}}

    chunks = []
    for chunk in litellm.completion(
        model=model_string,
        messages=messages,
        timeout=timeout,
        stream=True,
        temperature=0,
        **extra_kwargs,
    ):
        chunks.append(chunk)

    response = litellm.stream_chunk_builder(chunks, messages=messages)
    if response is None:
        raise RuntimeError(
            "stream_chunk_builder returned None — streaming response yielded no chunks"
        )

    # Fallback: Gemini 2.5 Flash thinking mode occasionally causes
    # stream_chunk_builder to return empty content because reasoning tokens
    # arrive in delta.reasoning_content rather than delta.content.
    # Reassemble the final answer directly from delta.content fragments
    # collected during streaming to fulfil the contract that callers can
    # always read choices[0].message.content.
    if not response.choices[0].message.content:
        assembled = "".join(
            (c.choices[0].delta.content or "")
            for c in chunks
            if c.choices
        )
        if assembled:
            response.choices[0].message.content = assembled
        else:
            raise RuntimeError(
                "stream_chunk_builder returned empty content and no "
                "delta.content fragments found in stream"
            )

    return response


def _classify_llm_error(e: Exception) -> Literal["llm_timeout", "llm_error"]:
    type_name = type(e).__name__.lower()
    message = str(e).lower()
    if "timeout" in type_name or "timeout" in message or "timed out" in message:
        return "llm_timeout"
    return "llm_error"
