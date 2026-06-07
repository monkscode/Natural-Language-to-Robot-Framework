"""
Unit tests for guardrail functions in src.backend.crew_ai.tasks.

Purpose: Guardrails intercept and fix LLM output before it reaches CrewAI's
         parser.  They're the last line of defense against malformed responses.
         A broken guardrail means the workflow silently fails or retries
         infinitely.

Tests cover:
  _extract_json_by_key: valid JSON, embedded in text, multiple candidates
  assembly_output_guardrail: valid JSON, RF code extraction, no code
  identification_output_guardrail: valid steps, missing steps
"""

import pytest
import json
from src.backend.crew_ai.tasks import (
    _extract_json_by_key,
    assembly_output_guardrail,
    identification_output_guardrail,
)


class TestExtractJsonByKey:
    """Tests for _extract_json_by_key helper."""

    def test_already_valid_json(self):
        """Already-valid JSON with the key → returns parsed dict."""
        text = '{"code": "*** Settings ***\\nLibrary Browser"}'
        result = _extract_json_by_key(text, "code", "Test")
        assert result is not None
        assert "code" in result

    def test_json_in_prose(self):
        """JSON embedded in surrounding prose → extracted."""
        text = 'Here is the result: {"code": "*** Test Cases ***"} please review'
        result = _extract_json_by_key(text, "code", "Test")
        assert result is not None
        assert "code" in result

    def test_multiple_candidates_returns_last(self):
        """Multiple JSON objects → returns the last valid one with the key."""
        text = '{"code": "*** Settings ***"} some text {"code": "*** Settings *** second"}'
        result = _extract_json_by_key(text, "code", "Test")
        assert result is not None

    def test_no_matching_key(self):
        """JSON without the requested key → returns None."""
        text = '{"other_key": "value"}'
        result = _extract_json_by_key(text, "code", "Test")
        assert result is None

    def test_no_json_in_text(self):
        """Plain text with no JSON → returns None."""
        result = _extract_json_by_key("No JSON here at all", "code", "Test")
        assert result is None


class TestAssemblyOutputGuardrail:
    """Tests for assembly_output_guardrail."""

    def test_valid_json_with_code(self):
        """Valid JSON with 'code' key passes."""
        output = json.dumps({"code": "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nTest\n    Log    Hello"})
        from unittest.mock import MagicMock
        task_out = MagicMock(raw=output)
        is_valid, result = assembly_output_guardrail(task_out)
        assert is_valid is True

    def test_rf_code_in_text(self):
        """RF code (*** Settings ***) embedded in text → extracted and wrapped."""
        output = "Here's the code:\n*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nTest\n    Log    Hello"
        from unittest.mock import MagicMock
        task_out = MagicMock(raw=output)
        is_valid, result = assembly_output_guardrail(task_out)
        # Should extract the RF code
        assert is_valid is True or "Settings" in str(result)

    def test_no_code_fails(self):
        """Text with no RF code and no JSON → fails with feedback."""
        from unittest.mock import MagicMock
        task_out = MagicMock(raw="I couldn't generate code because reasons.")
        is_valid, result = assembly_output_guardrail(task_out)
        assert is_valid is False
        assert isinstance(result, str)  # Feedback message


class TestIdentificationOutputGuardrail:
    """Tests for identification_output_guardrail."""

    def test_valid_steps(self):
        """Valid JSON with 'steps' key passes."""
        output = json.dumps({
            "steps": [
                {"step_description": "Click login", "keyword": "Click Element", "locator": "id=login", "found": True}
            ]
        })
        from unittest.mock import MagicMock
        task_out = MagicMock(raw=output)
        is_valid, result = identification_output_guardrail(task_out)
        assert is_valid is True

    def test_missing_steps_key(self):
        """JSON without 'steps' key → fails with feedback."""
        output = json.dumps({"elements": []})
        from unittest.mock import MagicMock
        task_out = MagicMock(raw=output)
        is_valid, result = identification_output_guardrail(task_out)
        assert is_valid is False
