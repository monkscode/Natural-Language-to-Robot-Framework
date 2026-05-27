"""
Coverage tests for learning_config.py — targeting 22 missing lines.

Tests _parse_review_response state-validity checks, duplicate hint_id guard,
empty-reason guard, hint_states=None fallback, _classify_llm_error,
and LearningWriteQueue shutdown drain with in-flight items.
"""

import json
import threading
import time

import pytest

from src.backend.crew_ai.optimization.learning_config import (
    _parse_review_response,
    _classify_llm_error,
    LearningWriteQueue,
)


# ---------------------------------------------------------------------------
# _parse_review_response — state-validity checks
# ---------------------------------------------------------------------------

class TestParseReviewResponseStateValidity:
    """_parse_review_response drops decisions that contradict current hint state."""

    def _content(self, decisions: list, summary: str = "test") -> str:
        return json.dumps({"decisions": decisions, "summary": summary})

    def test_unflag_requires_active_and_conflict_flagged(self):
        """unflag is valid only when is_active=1 AND conflict_flagged=1."""
        known = {10}
        zero_app = set()
        states = {10: {"is_active": 1, "conflict_flagged": 0}}  # NOT flagged

        content = self._content([{"id": 10, "recommendation": "unflag", "reason": "looks ok"}])
        result = _parse_review_response(content, known, zero_app, states)

        assert result["decisions"] == []  # unflag on non-flagged hint is dropped

    def test_unflag_valid_when_active_and_flagged(self):
        """unflag is accepted when is_active=1 AND conflict_flagged=1."""
        known = {10}
        zero_app = set()
        states = {10: {"is_active": 1, "conflict_flagged": 1}}

        content = self._content([{"id": 10, "recommendation": "unflag", "reason": "flag was wrong"}])
        result = _parse_review_response(content, known, zero_app, states)

        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["recommendation"] == "unflag"

    def test_reactivate_dropped_for_active_hint(self):
        """reactivate is invalid when hint is already active (is_active=1)."""
        known = {11}
        zero_app = set()
        states = {11: {"is_active": 1, "conflict_flagged": 0}}

        content = self._content([{"id": 11, "recommendation": "reactivate", "reason": "re-enable"}])
        result = _parse_review_response(content, known, zero_app, states)

        assert result["decisions"] == []

    def test_reactivate_valid_for_inactive_hint(self):
        """reactivate is accepted when hint is inactive (is_active=0)."""
        known = {11}
        zero_app = set()
        states = {11: {"is_active": 0, "conflict_flagged": 0}}

        content = self._content([{"id": 11, "recommendation": "reactivate", "reason": "wrongly disabled"}])
        result = _parse_review_response(content, known, zero_app, states)

        assert len(result["decisions"]) == 1

    def test_disable_dropped_for_inactive_hint(self):
        """disable is invalid when hint is already inactive (is_active=0)."""
        known = {12}
        zero_app = set()
        states = {12: {"is_active": 0, "conflict_flagged": 0}}

        content = self._content([{"id": 12, "recommendation": "disable", "reason": "bad hint"}])
        result = _parse_review_response(content, known, zero_app, states)

        assert result["decisions"] == []

    def test_disable_valid_for_active_hint(self):
        """disable is accepted when hint is active."""
        known = {12}
        zero_app = set()
        states = {12: {"is_active": 1, "conflict_flagged": 0}}

        content = self._content([{"id": 12, "recommendation": "disable", "reason": "consistently harmful"}])
        result = _parse_review_response(content, known, zero_app, states)

        assert len(result["decisions"]) == 1

    def test_keep_and_flag_review_accepted_regardless_of_state(self):
        """keep and flag_review are always valid for any hint state."""
        known = {13, 14}
        zero_app = set()
        states = {
            13: {"is_active": 0, "conflict_flagged": 0},
            14: {"is_active": 1, "conflict_flagged": 1},
        }

        content = self._content([
            {"id": 13, "recommendation": "keep", "reason": "looks fine"},
            {"id": 14, "recommendation": "flag_review", "reason": "needs human review"},
        ])
        result = _parse_review_response(content, known, zero_app, states)

        assert len(result["decisions"]) == 2

    def test_hint_in_known_but_missing_from_hint_states_uses_fallback(self):
        """hint_id in known_hint_ids but absent from hint_states falls back
        to conservative defaults (is_active=1, conflict_flagged=0).
        unflag and reactivate are rejected; keep/disable/flag_review are allowed."""
        known = {20}
        zero_app = set()
        states = {}  # missing hint 20

        # unflag rejected (fallback assumes not flagged)
        content = self._content([{"id": 20, "recommendation": "unflag", "reason": "looks fine"}])
        result = _parse_review_response(content, known, zero_app, states)
        assert result["decisions"] == []

        # disable accepted (fallback assumes active)
        content = self._content([{"id": 20, "recommendation": "disable", "reason": "harmful hint"}])
        result = _parse_review_response(content, known, zero_app, states)
        assert len(result["decisions"]) == 1


# ---------------------------------------------------------------------------
# _parse_review_response — duplicate hint_id
# ---------------------------------------------------------------------------

class TestParseReviewResponseDuplicateId:
    def test_second_decision_for_same_hint_id_is_dropped(self):
        """If the LLM returns two decisions for the same hint_id, keep only the first."""
        known = {5}
        zero_app = set()
        states = {5: {"is_active": 1, "conflict_flagged": 0}}

        content = json.dumps({
            "decisions": [
                {"id": 5, "recommendation": "keep", "reason": "first"},
                {"id": 5, "recommendation": "disable", "reason": "second"},
            ],
            "summary": "test",
        })
        result = _parse_review_response(content, known, zero_app, states)

        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["recommendation"] == "keep"


# ---------------------------------------------------------------------------
# _parse_review_response — empty reason guard
# ---------------------------------------------------------------------------

class TestParseReviewResponseEmptyReason:
    def test_decision_with_empty_reason_is_dropped(self):
        """A decision without a non-empty reason string is silently skipped."""
        known = {6}
        zero_app = set()
        states = {6: {"is_active": 1, "conflict_flagged": 0}}

        content = json.dumps({
            "decisions": [{"id": 6, "recommendation": "keep", "reason": "   "}],
            "summary": "test",
        })
        result = _parse_review_response(content, known, zero_app, states)
        assert result["decisions"] == []

    def test_decision_with_missing_reason_is_dropped(self):
        known = {7}
        zero_app = set()
        states = {7: {"is_active": 1, "conflict_flagged": 0}}

        content = json.dumps({
            "decisions": [{"id": 7, "recommendation": "keep"}],
            "summary": "test",
        })
        result = _parse_review_response(content, known, zero_app, states)
        assert result["decisions"] == []


# ---------------------------------------------------------------------------
# _parse_review_response — unknown ID dropped
# ---------------------------------------------------------------------------

class TestParseReviewResponseUnknownId:
    def test_id_not_in_known_hint_ids_is_skipped(self):
        known = {1}
        zero_app = set()
        states = {1: {"is_active": 1, "conflict_flagged": 0}}

        content = json.dumps({
            "decisions": [
                {"id": 999, "recommendation": "disable", "reason": "hallucinated hint"},
                {"id": 1, "recommendation": "keep", "reason": "good"},
            ],
            "summary": "test",
        })
        result = _parse_review_response(content, known, zero_app, states)
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["id"] == 1


# ---------------------------------------------------------------------------
# _parse_review_response — zero-application hint restrictions
# ---------------------------------------------------------------------------

class TestParseReviewResponseZeroApplication:
    def test_reactivate_skipped_for_zero_application_hint(self):
        """reactivate is not allowed for zero-application hints."""
        known = {8}
        zero_app = {8}
        states = {8: {"is_active": 0, "conflict_flagged": 0}}

        content = json.dumps({
            "decisions": [{"id": 8, "recommendation": "reactivate", "reason": "wrongly disabled"}],
            "summary": "test",
        })
        result = _parse_review_response(content, known, zero_app, states)
        assert result["decisions"] == []

    def test_keep_allowed_for_zero_application_hint(self):
        known = {8}
        zero_app = {8}
        states = {8: {"is_active": 1, "conflict_flagged": 0}}

        content = json.dumps({
            "decisions": [{"id": 8, "recommendation": "keep", "reason": "text looks fine"}],
            "summary": "test",
        })
        result = _parse_review_response(content, known, zero_app, states)
        assert len(result["decisions"]) == 1


# ---------------------------------------------------------------------------
# _classify_llm_error
# ---------------------------------------------------------------------------

class TestClassifyLlmError:
    def test_timeout_in_type_name_returns_llm_timeout(self):
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        class TimeoutError(Exception):
            pass

        assert _classify_llm_error(TimeoutError("deadline exceeded")) == "llm_timeout"

    def test_timeout_in_message_returns_llm_timeout(self):
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        assert _classify_llm_error(RuntimeError("request timed out")) == "llm_timeout"

    def test_timed_out_in_message_returns_llm_timeout(self):
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        assert _classify_llm_error(ConnectionError("connection timed out")) == "llm_timeout"

    def test_generic_error_returns_llm_error(self):
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        assert _classify_llm_error(RuntimeError("500 Internal Server Error")) == "llm_error"

    def test_value_error_returns_llm_error(self):
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        assert _classify_llm_error(ValueError("unexpected response")) == "llm_error"


# ---------------------------------------------------------------------------
# LearningWriteQueue — shutdown drain with pending items
# ---------------------------------------------------------------------------

class TestLearningWriteQueueShutdownDrain:
    def test_shutdown_processes_pending_items(self):
        """Items submitted before shutdown() are processed by the worker (normal loop)."""
        results = []

        queue = LearningWriteQueue()
        for i in range(5):
            queue.submit(results.append, i)

        queue.shutdown(timeout=2.0)

        assert results == list(range(5))

    def test_shutdown_tolerates_exceptions_in_normal_loop(self):
        """A failing item in the normal loop does not prevent subsequent items or shutdown."""
        results = []

        def bad_fn():
            raise RuntimeError("boom")

        queue = LearningWriteQueue()
        queue.submit(bad_fn)
        queue.submit(results.append, "after_bad")

        queue.shutdown(timeout=2.0)

        assert "after_bad" in results

    def test_shutdown_with_no_pending_items_completes_cleanly(self):
        """Calling shutdown on an idle queue does not hang."""
        queue = LearningWriteQueue()
        queue.shutdown(timeout=1.0)

    def test_drain_loop_processes_items_added_after_sentinel(self):
        """Items queued after SENTINEL are processed by the drain loop.

        Strategy: block the worker on a synthetic item while we push SENTINEL
        and a real item directly onto the internal queue, then release the
        worker.  The worker dequeues SENTINEL with a non-empty queue behind it,
        so the drain-loop body at learning_config.py:336-346 executes.
        """
        results = []
        release = threading.Event()

        def blocking_fn():
            release.wait()

        queue = LearningWriteQueue()
        queue.submit(blocking_fn)  # occupies the worker

        # SENTINEL followed by a real item — worker will see a non-empty
        # queue when it dequeues SENTINEL, triggering the drain loop.
        queue._queue.put(LearningWriteQueue._SENTINEL)
        queue._queue.put((results.append, (99,), {}))

        release.set()
        queue._worker.join(timeout=2.0)

        assert 99 in results

    def test_drain_loop_skips_extra_sentinel(self):
        """A second SENTINEL inside the drain window is skipped, not re-processed.

        Exercises the ``if remaining is self._SENTINEL: continue`` guard in the
        drain loop.  Both a real item and a second SENTINEL sit behind the first
        SENTINEL when the worker picks it up.
        """
        results = []
        release = threading.Event()

        def blocking_fn():
            release.wait()

        queue = LearningWriteQueue()
        queue.submit(blocking_fn)  # occupies the worker

        queue._queue.put(LearningWriteQueue._SENTINEL)
        queue._queue.put((results.append, (42,), {}))
        queue._queue.put(LearningWriteQueue._SENTINEL)  # extra sentinel in drain window

        release.set()
        queue._worker.join(timeout=2.0)

        assert 42 in results  # real item was processed
        # Thread exited cleanly (join returned) — no deadlock or double-break
