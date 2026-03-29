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

import os
import time
import queue
import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

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
            target=self._process_writes, daemon=True, name="learning-writer"
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
