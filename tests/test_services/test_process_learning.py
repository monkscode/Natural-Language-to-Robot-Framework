"""
Unit tests for _process_learning() and helper logic in workflow_service.py.

These tests verify the glue between Docker execution results and the adaptive
learning system, including:
  - Guard: empty user_query is skipped (paste-and-execute mode)
  - Guard: feedback_loop=None is skipped silently
  - Hint metadata is correctly retrieved from _hint_metadata_cache and summed
  - process_execution() receives the right arguments
  - Exceptions are swallowed (non-blocking contract)
  - _hint_metadata_cache is consumed (pop semantics — no stale data)
"""

import pytest
from unittest.mock import patch, MagicMock, call


class TestProcessLearningGuards:
    """Guard conditions that skip learning without calling process_execution."""

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_skips_when_feedback_loop_is_none(self, mock_get_fl):
        """If learning system is disabled, return early without error."""
        mock_get_fl.return_value = None

        from src.backend.services.workflow_service import _process_learning
        # Should not raise, just return quietly
        _process_learning("run-1", "search on amazon.com", "*** Test Cases ***", {"test_status": "passed"})

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_skips_when_user_query_is_empty(self, mock_get_fl):
        """Empty user_query guard: skips learning (paste-and-execute mode)."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl

        from src.backend.services.workflow_service import _process_learning
        _process_learning("run-2", "", "*** Test Cases ***", {"test_status": "passed"})

        mock_fl.process_execution.assert_not_called()

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_skips_when_user_query_is_whitespace_only(self, mock_get_fl):
        """Whitespace-only query also triggers the empty guard."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl

        from src.backend.services.workflow_service import _process_learning
        _process_learning("run-3", "   ", "*** Test Cases ***", {"test_status": "passed"})

        mock_fl.process_execution.assert_not_called()


class TestProcessLearningCallsProcessExecution:
    """Verify correct arguments are forwarded to process_execution()."""

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_passes_basic_fields(self, mock_get_fl, mock_extract_url):
        """Verify workflow_id, user_query, robot_code, test_status are passed."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://amazon.com"

        from src.backend.services.workflow_service import _process_learning
        _process_learning(
            "run-abc",
            "search on amazon.com for laptops",
            "*** Test Cases ***\nTest\n    Log    Hi",
            {"test_status": "passed", "output_xml_path": "/tmp/output.xml"},
        )

        mock_fl.process_execution.assert_called_once()
        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["workflow_id"] == "run-abc"
        assert kwargs["user_query"] == "search on amazon.com for laptops"
        assert kwargs["robot_code"] == "*** Test Cases ***\nTest\n    Log    Hi"
        assert kwargs["test_status"] == "passed"
        assert kwargs["output_xml_path"] == "/tmp/output.xml"
        assert kwargs["metrics"] is None

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_passes_url_extracted_from_query(self, mock_get_fl, mock_extract_url):
        """URL is extracted from query and forwarded."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        _process_learning("run-url", "go to example.com", "code", {"test_status": "failed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["url"] == "https://example.com"

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_passes_failed_status(self, mock_get_fl, mock_extract_url):
        """test_status=failed is passed correctly."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import _process_learning
        _process_learning("run-fail", "test on x.com", "code", {"test_status": "failed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["test_status"] == "failed"

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_default_status_when_missing(self, mock_get_fl, mock_extract_url):
        """Missing test_status key falls back to 'unknown'."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import _process_learning
        _process_learning("run-nostat", "test on x.com", "code", {})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["test_status"] == "unknown"


class TestProcessLearningHintMetadata:
    """Hint metadata is consumed from _hint_metadata_cache and summed."""

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_no_hint_metadata_yields_zero_counts(self, mock_get_fl, mock_extract_url):
        """When cache has no entry for run_id, hints_injected=0."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import _process_learning, _hint_metadata_cache
        _hint_metadata_cache.clear()  # ensure clean state

        _process_learning("no-hints-run", "test on x.com", "code", {"test_status": "passed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["hints_injected"] == 0
        assert kwargs["hints_available"] == 0
        assert kwargs["hint_sources"] == []

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_hint_metadata_is_summed_correctly(self, mock_get_fl, mock_extract_url):
        """Total hints = sum of counts across all agents."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import _process_learning, _hint_metadata_cache
        run_id = "hint-sum-run"
        _hint_metadata_cache[run_id] = {
            "agents": {
                "planner":   {"count": 3, "available": 3, "sources": ["structural_rules", "anti_patterns"]},
                "assembler": {"count": 2, "available": 2, "sources": ["keyword_corrections"]},
            },
            "nl_injected_ids": [],
        }

        _process_learning(run_id, "test on x.com", "code", {"test_status": "passed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["hints_injected"] == 5
        assert kwargs["hints_available"] == 5
        assert set(kwargs["hint_sources"]) == {"structural_rules", "anti_patterns", "keyword_corrections"}

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_hint_metadata_is_consumed_from_cache(self, mock_get_fl, mock_extract_url):
        """Cache entry is popped after _process_learning — no stale data."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import _process_learning, _hint_metadata_cache
        run_id = "consume-run"
        _hint_metadata_cache[run_id] = {
            "agents": {"planner": {"count": 1, "available": 1, "sources": ["s1"]}},
            "nl_injected_ids": [],
        }

        _process_learning(run_id, "test on x.com", "code", {"test_status": "passed"})

        # Entry should be consumed
        assert run_id not in _hint_metadata_cache


class TestProcessLearningNonBlocking:
    """Exceptions from the learning system are swallowed (non-blocking contract)."""

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_exception_does_not_propagate(self, mock_get_fl):
        """Any exception from process_execution is caught and swallowed."""
        mock_fl = MagicMock()
        mock_fl.process_execution.side_effect = RuntimeError("DB error")
        mock_get_fl.return_value = mock_fl

        from src.backend.services.workflow_service import _process_learning
        # Must not raise
        _process_learning("err-run", "test on example.com", "code", {"test_status": "failed"})

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_get_feedback_loop_exception_does_not_propagate(self, mock_get_fl):
        """Even if get_feedback_loop() itself throws, pipeline is unaffected."""
        mock_get_fl.side_effect = RuntimeError("registry failed")

        from src.backend.services.workflow_service import _process_learning
        _process_learning("err-fl", "test on example.com", "code", {"test_status": "passed"})


class TestProcessLearningHoldout:
    """R7 holdout flag is read from hint metadata and forwarded."""

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_was_holdout_forwarded_from_metadata(self, mock_get_fl, mock_extract_url):
        """was_holdout=True in hint_metadata reaches process_execution."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        run_id = "holdout-run"
        _hint_metadata_cache[run_id] = {
            "agents": {"planner": {"count": 0, "available": 4, "sources": []}},
            "nl_injected_ids": [],
            "was_holdout": True,
        }

        _process_learning(run_id, "test on x.com", "code", {"test_status": "passed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["was_holdout"] is True

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_was_holdout_defaults_false_when_absent(self, mock_get_fl, mock_extract_url):
        """No was_holdout key (or no metadata at all) → False."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        _hint_metadata_cache.clear()

        _process_learning("no-holdout-run", "test on x.com", "code",
                          {"test_status": "passed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["was_holdout"] is False


class TestProcessLearningRerunInjectedHintIdsFallback:
    """Re-run cache-miss recovers injected_hint_ids from the DB record.

    Fixes a bug where success_count never incremented on edit-then-pass
    re-runs: the v1 _process_learning call consumed _hint_metadata_cache
    via .pop(), so the v2 re-run saw nl_injected_ids=[] and passed
    injected_hint_ids='[]' to update_hint_effectiveness, which early-exits
    before touching counters. The fallback recovers the original v1
    injected_hint_ids from execution_records (preserved across
    _update_to_passing_state per schema v10 contract).
    """

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_recovers_injected_hint_ids_from_db_on_rerun(
        self, mock_get_fl, mock_extract_url,
    ):
        """Cache miss + pre_run_record with IDs → recover and forward."""
        mock_fl = MagicMock()
        pre_run = MagicMock()
        pre_run.injected_hint_ids = "[5, 12]"
        pre_run.test_status = "failed"
        pre_run.robot_code = "v1 code"
        mock_fl.execution_memory.get.return_value = pre_run
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        _hint_metadata_cache.clear()

        _process_learning(
            "rerun-recover", "q on x.com", "v2 code",
            {"test_status": "passed"},
        )

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["injected_hint_ids"] == "[5, 12]"

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_cache_present_wins_over_db_record(
        self, mock_get_fl, mock_extract_url,
    ):
        """Cache hit → use cache; do NOT consult pre_run_record.injected_hint_ids."""
        mock_fl = MagicMock()
        pre_run = MagicMock()
        pre_run.injected_hint_ids = "[99]"  # would be wrong to use
        mock_fl.execution_memory.get.return_value = pre_run
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        run_id = "cache-wins"
        _hint_metadata_cache[run_id] = {
            "agents": {},
            "nl_injected_ids": [5, 12],
        }

        _process_learning(run_id, "q on x.com", "code", {"test_status": "passed"})

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["injected_hint_ids"] == "[5, 12]"

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_no_fallback_for_legacy_null_record(
        self, mock_get_fl, mock_extract_url,
    ):
        """Pre-v10 row with injected_hint_ids=None → no fallback, '[]' preserved."""
        mock_fl = MagicMock()
        pre_run = MagicMock()
        pre_run.injected_hint_ids = None
        mock_fl.execution_memory.get.return_value = pre_run
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        _hint_metadata_cache.clear()

        _process_learning(
            "legacy-null", "q on x.com", "code", {"test_status": "passed"},
        )

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["injected_hint_ids"] == "[]"

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_db_record_empty_list_passes_through(
        self, mock_get_fl, mock_extract_url,
    ):
        """Generation truly had no hints (DB stores '[]') → still no-op."""
        mock_fl = MagicMock()
        pre_run = MagicMock()
        pre_run.injected_hint_ids = "[]"
        mock_fl.execution_memory.get.return_value = pre_run
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        _hint_metadata_cache.clear()

        _process_learning(
            "empty-original", "q on x.com", "code", {"test_status": "passed"},
        )

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["injected_hint_ids"] == "[]"

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_no_fallback_on_first_attempt(self, mock_get_fl, mock_extract_url):
        """First attempt (pre_run_record=None) → no fallback path entered."""
        mock_fl = MagicMock()
        mock_fl.execution_memory.get.return_value = None  # first attempt
        mock_get_fl.return_value = mock_fl
        mock_extract_url.return_value = "https://x.com"

        from src.backend.services.workflow_service import (
            _process_learning, _hint_metadata_cache,
        )
        _hint_metadata_cache.clear()

        _process_learning(
            "first-attempt", "q on x.com", "code", {"test_status": "passed"},
        )

        kwargs = mock_fl.process_execution.call_args.kwargs
        assert kwargs["injected_hint_ids"] == "[]"


class TestProcessLearningUsageAttribution:
    """Phase 4 — a passing run with NL hints routes to fire_usage_attribution
    (standalone, or merged on Case B); everything else skips it. The atomic
    claim inside apply_hint_attribution is the real once-guard; this gate is a
    cost early-out."""

    _FIRE = "src.backend.crew_ai.optimization.conflict_detection.fire_usage_attribution"

    @staticmethod
    def _set_cache(run_id, nl_ids):
        from src.backend.services.workflow_service import _hint_metadata_cache
        _hint_metadata_cache.clear()
        _hint_metadata_cache[run_id] = {
            "agents": {"planner": {"count": len(nl_ids), "available": len(nl_ids),
                                   "sources": ["nl_feedback"]}},
            "nl_injected_ids": list(nl_ids),
        }

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_first_pass_with_hints_fires_standalone(self, mock_get_fl, mock_url, mock_fire):
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_fl.execution_memory.get.return_value = None  # first pass
        mock_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        self._set_cache("attr-s", [5, 12])
        _process_learning("attr-s", "search on example.com", "v2", {"test_status": "passed"})

        mock_fire.assert_called_once()
        kw = mock_fire.call_args.kwargs
        assert kw["case_b"] is False
        assert kw["injected_hint_ids"] == "[5, 12]"
        assert kw["workflow_id"] == "attr-s"
        assert callable(kw["prompt_builder"])

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_case_b_fires_merged(self, mock_get_fl, mock_url, mock_fire):
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        pre = MagicMock()
        pre.test_status = "failed"
        pre.robot_code = "v1 code"
        pre.injected_hint_ids = "[5, 12]"
        pre.hint_attribution_done = 0
        mock_fl.execution_memory.get.return_value = pre
        mock_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        self._set_cache("attr-b", [5, 12])
        _process_learning("attr-b", "search on example.com", "v2 DIFFERENT",
                          {"test_status": "passed"})

        mock_fire.assert_called_once()
        assert mock_fire.call_args.kwargs["case_b"] is True

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_skips_when_not_passed(self, mock_get_fl, mock_url, mock_fire):
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_fl.execution_memory.get.return_value = None
        mock_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        self._set_cache("attr-fail", [5, 12])
        _process_learning("attr-fail", "search on example.com", "code",
                          {"test_status": "failed"})

        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_skips_when_no_hints(self, mock_get_fl, mock_url, mock_fire):
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_fl.execution_memory.get.return_value = None
        mock_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        self._set_cache("attr-none", [])  # '[]' → gate skips
        _process_learning("attr-none", "search on example.com", "code",
                          {"test_status": "passed"})

        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_skips_when_already_attributed(self, mock_get_fl, mock_url, mock_fire):
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        pre = MagicMock()
        pre.test_status = "passed"
        pre.hint_attribution_done = 1  # done=1 → cost early-out skips the LLM
        pre.injected_hint_ids = "[5, 12]"
        mock_fl.execution_memory.get.return_value = pre
        mock_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        self._set_cache("attr-done", [5, 12])
        _process_learning("attr-done", "search on example.com", "code",
                          {"test_status": "passed"})

        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_malformed_recovered_ids_skip(self, mock_get_fl, mock_url, mock_fire):
        """A malformed injected_hint_ids recovered from the DB → F3 parse fails
        → skip (do not fire)."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        pre = MagicMock()
        pre.test_status = "passed"
        pre.hint_attribution_done = 0
        pre.injected_hint_ids = "{bad json"  # malformed, recovered on a cache miss
        mock_fl.execution_memory.get.return_value = pre
        mock_url.return_value = "https://example.com"

        from src.backend.services.workflow_service import _process_learning
        self._set_cache("attr-bad", [])  # empty cache → recovery uses pre.injected_hint_ids
        _process_learning("attr-bad", "search on example.com", "code",
                          {"test_status": "passed"})

        mock_fire.assert_not_called()


class TestProcessLearningSelectionTrace:
    """F2d — the reconciled selection trace is queued for a writer-thread
    INSERT (store_hint_workflow_trace), gated by HINT_TRACE_ENABLED. Runs on
    every outcome; test_status='failed' keeps the attribution path out of the way.
    """

    @staticmethod
    def _set_cache(run_id, trace):
        from src.backend.services.workflow_service import _hint_metadata_cache
        _hint_metadata_cache.clear()
        _hint_metadata_cache[run_id] = {
            "agents": {}, "nl_injected_ids": [], "selection_trace": trace,
        }

    @staticmethod
    def _trace_submits(mock_fl):
        return [
            c for c in mock_fl.write_queue.submit.call_args_list
            if c.args
            and c.args[0] is mock_fl.execution_memory.store_hint_workflow_trace
        ]

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_trace_submitted_to_write_queue(self, mock_get_fl, mock_url):
        mock_fl = MagicMock()
        mock_fl.execution_memory.get.return_value = None       # first attempt
        mock_get_fl.return_value = mock_fl
        mock_url.return_value = "https://example.com"
        trace = {5: {"scope": "global", "source": "nl", "priority": "high",
                     "similarity_score": 0.8, "available": 1, "injected": 0,
                     "drop_reason": "cap"}}
        self._set_cache("trace-1", trace)

        from src.backend.services.workflow_service import _process_learning
        _process_learning("trace-1", "search on example.com", "code",
                          {"test_status": "failed"})

        calls = self._trace_submits(mock_fl)
        assert len(calls) == 1
        assert calls[0].args[1] == "trace-1"
        assert calls[0].args[2] == trace

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_no_submit_when_trace_absent(self, mock_get_fl, mock_url):
        mock_fl = MagicMock()
        mock_fl.execution_memory.get.return_value = None
        mock_get_fl.return_value = mock_fl
        mock_url.return_value = "https://example.com"
        self._set_cache("trace-2", None)                       # trace off → None

        from src.backend.services.workflow_service import _process_learning
        _process_learning("trace-2", "search on example.com", "code",
                          {"test_status": "failed"})

        assert self._trace_submits(mock_fl) == []

    @patch("src.backend.services.workflow_service.extract_url_from_query")
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_no_submit_when_hint_trace_disabled(self, mock_get_fl, mock_url):
        from src.backend.core.config import settings
        mock_fl = MagicMock()
        mock_fl.execution_memory.get.return_value = None
        mock_get_fl.return_value = mock_fl
        mock_url.return_value = "https://example.com"
        trace = {5: {"available": 1, "injected": 1, "drop_reason": None}}
        self._set_cache("trace-3", trace)

        from src.backend.services.workflow_service import _process_learning
        with patch.object(settings, "HINT_TRACE_ENABLED", False):
            _process_learning("trace-3", "search on example.com", "code",
                              {"test_status": "failed"})

        assert self._trace_submits(mock_fl) == []
