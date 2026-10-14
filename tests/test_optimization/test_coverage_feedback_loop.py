"""
Coverage tests for feedback_loop.py — targeting 14 missing lines.

Tests:
- get_health_status: FAILED when ChromaDB init failed
- get_health_status: FAILED when circuit breaker is open
- get_health_status: DEGRADED when last_reconcile.missing_count > 0
- get_health_status: DEGRADED when active hints have NULL anchor_query
- process_user_feedback: returns fallback triage when circuit breaker is disabled
- _get_with_retry: retries until record appears and returns it
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization.postgres_execution_memory import (
    PostgresExecutionMemory,
)
from src.backend.crew_ai.optimization.feedback_loop import (
    FeedbackLoop,
    _get_with_retry,
)
from src.backend.crew_ai.optimization.learning_config import (
    LearningCircuitBreaker,
    LearningWriteQueue,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _SyncQueue:
    """Executes submitted work immediately — no background thread."""
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def submit_and_wait(self, fn, *args, timeout=None, **kwargs):
        fn(*args, **kwargs)
        return ("ok", None)


def _make_minimal_fb(in_memory_em):
    """Build a FeedbackLoop with all deps mocked out except execution_memory."""
    fb = FeedbackLoop.__new__(FeedbackLoop)
    fb.execution_memory = in_memory_em
    fb.circuit_breaker = LearningCircuitBreaker()
    fb.write_queue = _SyncQueue()
    fb.nl_engine = MagicMock()
    fb.structural_engine = MagicMock()
    fb.keyword_engine = MagicMock()
    fb.anti_pattern_engine = MagicMock()
    fb.pattern_learner = None
    fb.metrics_tracker = MagicMock()
    fb.failure_analyzer = MagicMock()
    fb.failure_analyzer.analyze.return_value = None
    fb.contradiction_detector = MagicMock()
    fb._optimization_init_ok = True
    fb._optimization_init_failures = 0
    fb.last_reconcile = None
    return fb


# ---------------------------------------------------------------------------
# get_health_status — FAILED paths
# ---------------------------------------------------------------------------

class TestGetHealthStatusFailed:
    def test_chromadb_init_failed_returns_failed(self, in_memory_em):
        """When ChromaDB sentinel is set, get_health_status should return FAILED."""
        fb = _make_minimal_fb(in_memory_em)
        in_memory_em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
        in_memory_em._chroma_failed_at = time.monotonic()

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = True
            status = fb.get_health_status()

        assert status == "FAILED"

    def test_open_circuit_breaker_returns_failed(self, in_memory_em):
        """A tripped circuit breaker makes get_health_status return FAILED."""
        fb = _make_minimal_fb(in_memory_em)
        in_memory_em._chroma_client = None  # not failed, just uninitialized

        # Trip the breaker by force
        fb.circuit_breaker._state = LearningCircuitBreaker.OPEN
        fb.circuit_breaker._opened_at = time.monotonic()
        fb.circuit_breaker._recovery_timeout = 9999  # won't recover during test

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = True
            status = fb.get_health_status()

        assert status == "FAILED"


# ---------------------------------------------------------------------------
# get_health_status — DEGRADED paths
# ---------------------------------------------------------------------------

class TestGetHealthStatusDegraded:
    def test_missing_count_gt_zero_returns_degraded(self, in_memory_em):
        """If the last reconcile reported missing anchors, status is DEGRADED."""
        fb = _make_minimal_fb(in_memory_em)
        fb.last_reconcile = {"ran_at": "now", "checked": 5, "missing_count": 2}
        in_memory_em._chroma_client = None  # not FAILED

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = True
            status = fb.get_health_status()

        assert status == "DEGRADED"

    def test_null_anchor_query_on_active_hint_returns_degraded(self, in_memory_em):
        """An active hint with NULL anchor_query causes DEGRADED status."""
        # Insert an active hint with NULL anchor_query
        in_memory_em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, evidence_count, "
            " anchor_query, created_at, last_seen) "
            "VALUES ('use xpath', 'locator', 'global', NULL, NULL, 1, "
            " NULL, datetime('now'), datetime('now'))",
        )
        in_memory_em._writer_conn.commit()

        fb = _make_minimal_fb(in_memory_em)
        fb.last_reconcile = None
        in_memory_em._chroma_client = None  # not FAILED

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = True
            status = fb.get_health_status()

        assert status == "DEGRADED"

    def test_optimization_init_not_ok_returns_degraded(self, in_memory_em):
        """If crew.py recorded an init failure, status is DEGRADED."""
        fb = _make_minimal_fb(in_memory_em)
        fb._optimization_init_ok = False
        fb.last_reconcile = None
        in_memory_em._chroma_client = None

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = True
            status = fb.get_health_status()

        assert status == "DEGRADED"


# ---------------------------------------------------------------------------
# get_health_status — DISABLED
# ---------------------------------------------------------------------------

class TestGetHealthStatusDisabled:
    def test_optimization_disabled_returns_disabled(self, in_memory_em):
        fb = _make_minimal_fb(in_memory_em)

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = False
            status = fb.get_health_status()

        assert status == "DISABLED"


# ---------------------------------------------------------------------------
# get_health_status — OK
# ---------------------------------------------------------------------------

class TestGetHealthStatusOk:
    def test_all_checks_pass_returns_ok(self, in_memory_em):
        fb = _make_minimal_fb(in_memory_em)
        fb.last_reconcile = {"ran_at": "now", "checked": 5, "missing_count": 0}
        in_memory_em._chroma_client = None  # not FAILED, just uninitialized (transient)

        with patch("src.backend.core.config.settings") as mock_settings:
            mock_settings.OPTIMIZATION_ENABLED = True
            status = fb.get_health_status()

        assert status == "OK"


# ---------------------------------------------------------------------------
# process_user_feedback — circuit breaker disabled
# ---------------------------------------------------------------------------

class TestProcessUserFeedbackCircuitBreakerDisabled:
    def test_returns_fallback_triage_when_breaker_is_open(self, in_memory_em):
        """When the circuit breaker is open, process_user_feedback returns
        the fallback triage dict without calling any engine or storing data."""
        fb = _make_minimal_fb(in_memory_em)

        # Force breaker open
        fb.circuit_breaker._state = LearningCircuitBreaker.OPEN
        fb.circuit_breaker._opened_at = time.monotonic()
        fb.circuit_breaker._recovery_timeout = 9999

        result = fb.process_user_feedback("wf-1", "bad click", "completely_wrong")

        assert result["category"] == "uncategorized"
        assert result["taxonomy_code"] == "X0"
        fb.nl_engine.process_feedback.assert_not_called()


# ---------------------------------------------------------------------------
# _get_with_retry
# ---------------------------------------------------------------------------

class TestGetWithRetry:
    """T7 rewrote this from a 3-attempt retry into a budgeted poll returning
    (record, outcome).  ceiling_ms/base_ms are passed explicitly here so the
    arithmetic stays readable; the production defaults (3s, 100ms, capped at
    500ms a step) are pinned in test_feedback_ordering.py."""

    def test_returns_record_on_first_attempt(self):
        """If em.get() returns a record immediately, no retry is needed."""
        mock_em = MagicMock()
        mock_em.get.return_value = "record"

        record, outcome = _get_with_retry(mock_em, "wf-1", ceiling_ms=300, base_ms=100)

        assert record == "record"
        assert outcome == "processed"
        assert mock_em.get.call_count == 1

    def test_retries_until_record_appears(self):
        """If em.get() returns None on the first call but succeeds on the second,
        _get_with_retry returns the record."""
        mock_em = MagicMock()
        mock_em.get.side_effect = [None, "record"]

        with patch("src.backend.crew_ai.optimization.feedback_loop.time.sleep"):
            record, outcome = _get_with_retry(
                mock_em, "wf-1", ceiling_ms=300, base_ms=100)

        assert record == "record"
        assert outcome == "processed"
        assert mock_em.get.call_count == 2

    def test_returns_no_record_after_the_budget_is_spent(self):
        """If em.get() always returns None, the poll gives up and says so."""
        mock_em = MagicMock()
        mock_em.get.return_value = None

        with patch("src.backend.crew_ai.optimization.feedback_loop.time.sleep"):
            record, outcome = _get_with_retry(
                mock_em, "wf-1", ceiling_ms=300, base_ms=100)

        assert record is None
        assert outcome == "no_record"
        assert mock_em.get.call_count == 3   # 100ms + 200ms of budget, then stop
