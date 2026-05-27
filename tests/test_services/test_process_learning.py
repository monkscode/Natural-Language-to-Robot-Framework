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
                "validator": {"count": 0, "available": 0, "sources": []},
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
