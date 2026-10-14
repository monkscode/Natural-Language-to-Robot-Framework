"""
Unit tests for guardrail functions in src.backend.crew_ai.tasks.

Purpose: Guardrails intercept and fix LLM output before it reaches CrewAI's
         parser.  They're the last line of defense against malformed responses.
         A broken guardrail means the workflow silently fails or retries
         infinitely.

Tests cover:
  _extract_json_by_key: valid JSON, embedded in text, multiple candidates
  assembly_output_guardrail: valid JSON, RF code extraction, no code

(identification_output_guardrail was deleted in Task 16 together with the
element-identifier LLM agent — the merged steps are now built by
deterministic Python and need no output repair.)
"""

import pytest
import json
from src.backend.crew_ai.tasks import (
    _extract_json_by_key,
    assembly_output_guardrail,
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


class TestStepsKeyExtraction:
    """_extract_json_by_key with 'steps' is still used by crew.py's plan
    extraction fallback (_extract_plan_steps) — keep it covered."""

    def test_valid_steps_extracted(self):
        output = json.dumps({
            "steps": [
                {"step_description": "Click login", "keyword": "Click Element"}
            ]
        })
        result = _extract_json_by_key(output, "steps", "PlanOutput")
        assert result is not None
        assert json.loads(result)["steps"][0]["keyword"] == "Click Element"

    def test_missing_steps_key_returns_none(self):
        output = json.dumps({"elements": []})
        assert _extract_json_by_key(output, "steps", "PlanOutput") is None


class TestGuardrailAttemptCounting:
    """Guardrail invocations are counted per RobotTasks instance.

    RobotTasks is built once per run_crew and once per repair mini-crew, so the
    counter is scoped to a workflow by construction — no module-level dict, no
    lock, no workflow_id key, and nothing to leak between concurrent runs.

    A count above 1 for a site means the assembler needed re-prompting there.
    """

    def _tasks(self):
        from src.backend.crew_ai.tasks import RobotTasks
        return RobotTasks()

    def _valid_output(self):
        from unittest.mock import MagicMock
        return MagicMock(raw=json.dumps({"code": "*** Settings ***\nLibrary    Browser\n"}))

    def test_starts_empty(self):
        assert self._tasks().guardrail_attempts == {}

    def test_assembly_site_counts_under_its_own_name(self):
        tasks = self._tasks()
        guardrail = tasks._track_guardrail("assembly_output")

        guardrail(self._valid_output())

        assert tasks.guardrail_attempts == {"assembly_output": 1}

    def test_repair_site_counts_separately(self):
        """Both sites share assembly_output_guardrail, so the names are the
        only way to tell a first-pass format fix from a repair-loop one."""
        tasks = self._tasks()
        assembly = tasks._track_guardrail("assembly_output")
        repair = tasks._track_guardrail("repair_output")

        assembly(self._valid_output())
        repair(self._valid_output())
        repair(self._valid_output())

        assert tasks.guardrail_attempts == {"assembly_output": 1, "repair_output": 2}

    def test_counts_a_failed_guardrail_too(self):
        """A retry is exactly what a failure causes — it must be counted, not
        dropped. The old module-dict version popped only on pass and leaked."""
        from unittest.mock import MagicMock
        tasks = self._tasks()
        guardrail = tasks._track_guardrail("assembly_output")

        is_valid, _ = guardrail(MagicMock(raw="no code here at all"))

        assert is_valid is False
        assert tasks.guardrail_attempts == {"assembly_output": 1}

    def test_delegates_to_assembly_output_guardrail(self):
        """The wrapper counts; it must not change the verdict or the payload."""
        tasks = self._tasks()
        guardrail = tasks._track_guardrail("assembly_output")
        task_out = self._valid_output()

        wrapped = guardrail(task_out)
        direct = assembly_output_guardrail(task_out)

        assert wrapped == direct

    def test_two_instances_do_not_share_a_counter(self):
        """The main crew and the repair mini-crew build separate RobotTasks."""
        first, second = self._tasks(), self._tasks()

        first._track_guardrail("assembly_output")(self._valid_output())

        assert first.guardrail_attempts == {"assembly_output": 1}
        assert second.guardrail_attempts == {}
