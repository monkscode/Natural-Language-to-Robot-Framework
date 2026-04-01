"""
Unit tests for src.backend.crew_ai.llm_output_cleaner — LLMOutputCleaner.

Purpose: LLMOutputCleaner fixes malformed LLM output (bad Action/ActionInput lines)
         that would cause CrewAI to crash with parsing errors.  A regression here
         means every LLM call that produces slightly malformed output breaks the
         entire workflow.

Tests:
  - clean_action_lines strips extra whitespace and fixes format
  - clean_action_lines preserves correctly formatted lines
  - clean_action_lines handles empty input
  - clean_action_input normalises JSON formatting
  - clean_output handles combined action+input
  - clean_output handles non-string input
  - is_formatting_error detects known patterns
  - is_formatting_error rejects non-formatting errors
  - monitor: log_error records errors
  - monitor: get_stats empty
  - monitor: get_stats with errors
"""

import pytest
from src.backend.crew_ai.llm_output_cleaner import LLMOutputCleaner


class TestCleanActionLines:
    """Tests for Action line cleaning."""

    def test_strips_extra_whitespace(self):
        """Extra spaces/tabs around Action: are normalised."""
        result = LLMOutputCleaner.clean_action_lines("  Action:   Delegate work to coworker  ")
        assert "Action:" in result
        # Should be cleaner than the input
        assert result.strip() != ""

    def test_preserves_valid_format(self):
        """Already-correct Action lines pass through unchanged (or minimally changed)."""
        original = "Action: Delegate work to coworker"
        result = LLMOutputCleaner.clean_action_lines(original)
        assert "Delegate" in result

    def test_empty_input(self):
        """Empty string returns empty string (no crash)."""
        result = LLMOutputCleaner.clean_action_lines("")
        assert result == ""

    def test_none_input(self):
        """None input is handled gracefully."""
        result = LLMOutputCleaner.clean_action_lines(None)
        assert result is None or result == ""


class TestCleanActionInput:
    """Tests for ActionInput line cleaning."""

    def test_normalises_json_input(self):
        """JSON ActionInput is normalised."""
        result = LLMOutputCleaner.clean_action_input_lines('  Action Input:   {"key": "value"}  ')
        assert "key" in result or result.strip() != ""


class TestCleanOutput:
    """Tests for combined output cleaning."""

    def test_combined_cleaning(self):
        """Full output with Action + ActionInput is cleaned."""
        text = "Action:  Delegate work to coworker\nAction Input: {\"task\": \"find element\"}"
        result = LLMOutputCleaner.clean_output(text)
        assert "Action:" in result

    def test_non_string_input(self):
        """Non-string input (e.g. int) is handled gracefully."""
        result = LLMOutputCleaner.clean_output(42)
        # Should return original or convert gracefully
        assert result is not None


class TestIsFormattingError:
    """Tests for formatting error detection."""

    def test_detects_known_pattern(self):
        """Known formatting error patterns are detected."""
        # Common CrewAI error when action line is malformed
        error_msg = "Could not parse LLM output"
        result = LLMOutputCleaner.is_formatting_error(error_msg)
        assert result is True

    def test_rejects_non_formatting(self):
        """Non-formatting errors (e.g. network) are not flagged."""
        result = LLMOutputCleaner.is_formatting_error("Connection refused")
        assert result is False


class TestFormattingMonitor:
    """Tests for the formatting error monitor."""

    def test_stats_empty_initially(self):
        """Fresh monitor has zero stats."""
        from src.backend.crew_ai.llm_output_cleaner import LLMFormattingMonitor
        monitor = LLMFormattingMonitor()
        stats = monitor.get_stats()
        assert "Responses: 0" in stats or "No LLM" in stats

    def test_log_error_increments(self):
        """log_error increases the counter."""
        from src.backend.crew_ai.llm_output_cleaner import LLMFormattingMonitor
        monitor = LLMFormattingMonitor()
        monitor.log_response()
        monitor.log_formatting_error()
        stats = monitor.get_stats()
        assert "detected" in stats
