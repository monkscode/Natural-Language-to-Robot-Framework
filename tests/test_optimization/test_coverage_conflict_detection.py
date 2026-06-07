"""
Coverage tests for conflict_detection.py — targeting 50 missing lines.

Tests the branches in fire_conflict_detection that are not hit by
test_trigger1_conflict_detection.py: legacy None branch, malformed JSON,
non-list parsed value, bare-int LLM entries, out-of-scope IDs, empty
reason fallback, json_parse_failed, and LLM exception classification.

Also covers _hint_line metadata rendering (applied/success/failure/age).
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization.conflict_detection import (
    _hint_line,
    _build_context_prefix,
    fire_conflict_detection,
)


# ---------------------------------------------------------------------------
# _hint_line — metadata rendering
# ---------------------------------------------------------------------------

class TestHintLine:
    def test_no_metadata_simple_form(self):
        h = {"id": 3, "feedback_text": "use XPath locators"}
        result = _hint_line(h)
        assert result == "  [3] use XPath locators"

    def test_with_zero_applied_count(self):
        """applied_count=0 should still render the metadata block."""
        h = {
            "id": 5,
            "feedback_text": "never click header",
            "applied_count": 0,
            "success_count": None,
            "failure_count": None,
        }
        result = _hint_line(h)
        assert "applied=0" in result
        assert "success=0" in result
        assert "failure=0" in result

    def test_with_counts_and_no_created_at(self):
        h = {
            "id": 7,
            "feedback_text": "wait for modal",
            "applied_count": 10,
            "success_count": 8,
            "failure_count": 2,
            "created_at": None,
        }
        result = _hint_line(h)
        assert "applied=10" in result
        assert "success=8" in result
        assert "failure=2" in result
        # No age string when created_at is None
        assert "age=" not in result

    def test_with_valid_created_at_shows_age(self):
        """A real ISO timestamp renders age=Nd."""
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        h = {
            "id": 9,
            "feedback_text": "scroll before click",
            "applied_count": 3,
            "success_count": 2,
            "failure_count": 1,
            "created_at": ts,
        }
        result = _hint_line(h)
        assert "age=5d" in result

    def test_with_malformed_created_at_omits_age(self):
        """Unparseable created_at: age block is omitted but line still renders."""
        h = {
            "id": 11,
            "feedback_text": "use stable state",
            "applied_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "created_at": "not-a-date",
        }
        result = _hint_line(h)
        assert "[11]" in result
        assert "age=" not in result


# ---------------------------------------------------------------------------
# _build_context_prefix
# ---------------------------------------------------------------------------

class TestBuildContextPrefix:
    def test_all_fields_present(self):
        result = _build_context_prefix("example.com", "https://example.com/page", "login test")
        assert "Domain: example.com" in result
        assert "URL: https://example.com/page" in result
        assert '"login test"' in result

    def test_none_fields_show_unknown(self):
        result = _build_context_prefix(None, None, None)
        assert "Domain: unknown" in result
        assert "URL: unknown" in result
        assert '"unknown"' in result

    def test_user_query_truncated_at_200(self):
        long_query = "x" * 300
        result = _build_context_prefix("d.com", "https://d.com", long_query)
        # The query in the result should be at most 200 chars (plus quotes)
        # Find the quoted portion
        start = result.index('"') + 1
        end = result.index('"', start)
        assert len(result[start:end]) == 200


# ---------------------------------------------------------------------------
# Helpers for fire_conflict_detection tests
# ---------------------------------------------------------------------------

class _SyncQueue:
    """Executes submitted work immediately — no background thread."""
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _make_fb(active_hints=None, hints_by_id=None):
    """Minimal feedback_loop-like mock."""
    fb = MagicMock()
    fb.write_queue = _SyncQueue()
    fb.nl_engine.get_active_hints_raw.return_value = active_hints or []
    fb.nl_engine.get_hints_by_id.return_value = hints_by_id if hints_by_id is not None else []
    fb.nl_engine.conflict_flag_hints.return_value = []
    fb.write_trigger_event = MagicMock()
    return fb


def _llm_response(content: str):
    """Build a minimal litellm-style response object."""
    r = MagicMock()
    r.choices[0].message.content = content
    r.usage.prompt_tokens = 10
    r.usage.completion_tokens = 5
    return r


# ---------------------------------------------------------------------------
# fire_conflict_detection — injected_hint_ids=None (legacy fallback)
# ---------------------------------------------------------------------------

class TestFireConflictDetectionLegacyNoneBranch:
    """injected_hint_ids=None → domain-wide get_active_hints_raw fallback."""

    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_none_branch_calls_get_active_hints_raw(self, mock_llm, _kw, _model):
        hint = {"id": 1, "feedback_text": "use xpath", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response('{"flag": []}')
        fb = _make_fb(active_hints=[hint])

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-1",
            domain="example.com",
            url="https://example.com",
            feedback_text=None,
            injected_hint_ids=None,     # legacy: None triggers domain fallback
            prompt_builder=lambda hints: "prompt",
        )

        fb.nl_engine.get_active_hints_raw.assert_called_once_with("example.com", "https://example.com")
        # LLM was called (hints were non-empty)
        mock_llm.assert_called_once()


# ---------------------------------------------------------------------------
# fire_conflict_detection — malformed JSON in injected_hint_ids
# ---------------------------------------------------------------------------

class TestFireConflictDetectionMalformedInjectedIds:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_malformed_json_falls_back_to_domain_query(self, mock_llm, _kw, _model):
        """Non-parseable injected_hint_ids → warning + fallback to domain query."""
        hint = {"id": 2, "feedback_text": "use css", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response('{"flag": []}')
        fb = _make_fb(active_hints=[hint])

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_2",
            workflow_id="wf-2",
            domain="d.com",
            url="https://d.com",
            feedback_text="wrong click",
            injected_hint_ids="{not valid json",   # malformed
            prompt_builder=lambda hints: "prompt",
        )

        fb.nl_engine.get_active_hints_raw.assert_called_once()


# ---------------------------------------------------------------------------
# fire_conflict_detection — non-list decoded value
# ---------------------------------------------------------------------------

class TestFireConflictDetectionNonListInjectedIds:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_non_list_json_falls_back_to_domain_query(self, mock_llm, _kw, _model):
        """JSON decodes to a non-list → fallback to domain query."""
        hint = {"id": 3, "feedback_text": "wait state", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response('{"flag": []}')
        fb = _make_fb(active_hints=[hint])

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-3",
            domain="d.com",
            url=None,
            feedback_text=None,
            injected_hint_ids='{"key": "value"}',   # valid JSON but not a list
            prompt_builder=lambda hints: "prompt",
        )

        fb.nl_engine.get_active_hints_raw.assert_called_once()


# ---------------------------------------------------------------------------
# fire_conflict_detection — empty list '[]' early return
# ---------------------------------------------------------------------------

class TestFireConflictDetectionEmptyList:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_empty_injected_ids_writes_telemetry_and_returns(self, mock_llm, _model):
        """'[]' means no hints were injected → write telemetry, no LLM call."""
        fb = _make_fb()

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-4",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[]",
            prompt_builder=lambda hints: "prompt",
        )

        mock_llm.assert_not_called()
        fb.write_trigger_event.assert_called_once()
        call_kwargs = fb.write_trigger_event.call_args.kwargs
        assert call_kwargs["status"] == "no_active_hints"
        assert call_kwargs["active_hint_ids"] == []


# ---------------------------------------------------------------------------
# fire_conflict_detection — no active hints after get_hints_by_id
# ---------------------------------------------------------------------------

class TestFireConflictDetectionNoActiveHintsAfterLookup:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_hints_deactivated_since_injection_writes_telemetry(self, mock_llm, _model):
        """Injected IDs resolve to no active hints (deactivated between injection
        and trigger-fire) → write telemetry with no_active_hints, no LLM call."""
        fb = _make_fb(hints_by_id=[])   # all hints inactive/flagged by now

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-5",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[10, 11]",
            prompt_builder=lambda hints: "prompt",
        )

        mock_llm.assert_not_called()
        fb.write_trigger_event.assert_called_once()
        call_kwargs = fb.write_trigger_event.call_args.kwargs
        assert call_kwargs["status"] == "no_active_hints"
        assert call_kwargs["active_hint_ids"] == [10, 11]


# ---------------------------------------------------------------------------
# fire_conflict_detection — LLM response: bare int entries (backward compat)
# ---------------------------------------------------------------------------

class TestFireConflictDetectionBareIntEntries:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_bare_int_entry_uses_shared_reason(self, mock_llm, _kw, _model):
        """Old LLM format: flag=[int, ...] with top-level reason."""
        hint = {"id": 7, "feedback_text": "use browser lib", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response('{"flag": [7], "reason": "browser library issue"}')
        fb = _make_fb(hints_by_id=[hint])
        fb.nl_engine.conflict_flag_hints.return_value = [7]

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-6",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[7]",
            prompt_builder=lambda hints: "prompt",
        )

        fb.nl_engine.conflict_flag_hints.assert_called_once()
        per_hint = fb.nl_engine.conflict_flag_hints.call_args[0][0]
        assert 7 in per_hint
        assert per_hint[7] == "browser library issue"


# ---------------------------------------------------------------------------
# fire_conflict_detection — out-of-scope hint ID dropped
# ---------------------------------------------------------------------------

class TestFireConflictDetectionOutOfScopeId:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_stray_id_not_in_active_hints_is_dropped(self, mock_llm, _kw, _model):
        """LLM returns an ID not in the current hint set — must be ignored."""
        hint = {"id": 8, "feedback_text": "use stable state", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        # LLM flags id=99 which was NOT in the injected set
        mock_llm.return_value = _llm_response(
            '{"flag": [{"id": 99, "reason": "wrong"}, {"id": 8, "reason": "correct reason"}]}'
        )
        fb = _make_fb(hints_by_id=[hint])
        fb.nl_engine.conflict_flag_hints.return_value = [8]

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-7",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[8]",
            prompt_builder=lambda hints: "prompt",
        )

        per_hint = fb.nl_engine.conflict_flag_hints.call_args[0][0]
        assert 99 not in per_hint     # stray id was dropped
        assert 8 in per_hint          # valid id was kept


# ---------------------------------------------------------------------------
# fire_conflict_detection — empty reason gets explicit marker
# ---------------------------------------------------------------------------

class TestFireConflictDetectionEmptyReason:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_empty_reason_in_flag_entry_gets_marker(self, mock_llm, _kw, _model):
        """A dict flag entry with an empty/whitespace reason stores the explicit marker."""
        hint = {"id": 10, "feedback_text": "wait for load", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response(
            '{"flag": [{"id": 10, "reason": ""}]}'
        )
        fb = _make_fb(hints_by_id=[hint])
        fb.nl_engine.conflict_flag_hints.return_value = [10]

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-8",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[10]",
            prompt_builder=lambda hints: "prompt",
        )

        per_hint = fb.nl_engine.conflict_flag_hints.call_args[0][0]
        assert per_hint[10] == "no specific reason returned by the model"


# ---------------------------------------------------------------------------
# fire_conflict_detection — non-dict non-int entry is ignored
# ---------------------------------------------------------------------------

class TestFireConflictDetectionNonDictNonIntEntry:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_list_entry_that_is_neither_dict_nor_int_is_skipped(self, mock_llm, _kw, _model):
        """A string in the flag list is neither int nor dict — skip it."""
        hint = {"id": 12, "feedback_text": "use aria-label", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response(
            '{"flag": ["oops", {"id": 12, "reason": "bad advice"}]}'
        )
        fb = _make_fb(hints_by_id=[hint])
        fb.nl_engine.conflict_flag_hints.return_value = [12]

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-9",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[12]",
            prompt_builder=lambda hints: "prompt",
        )

        per_hint = fb.nl_engine.conflict_flag_hints.call_args[0][0]
        # Only hint 12 should be in per_hint (not the string "oops")
        assert list(per_hint.keys()) == [12]


# ---------------------------------------------------------------------------
# fire_conflict_detection — json_parse_failed path
# ---------------------------------------------------------------------------

class TestFireConflictDetectionJsonParseFailed:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_unparseable_llm_response_writes_json_parse_failed(self, mock_llm, _kw, _model):
        """If LLM returns non-JSON, status is json_parse_failed, no hints flagged."""
        hint = {"id": 15, "feedback_text": "avoid iframe", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response("Sorry, I cannot help with that.")
        fb = _make_fb(hints_by_id=[hint])

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-10",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[15]",
            prompt_builder=lambda hints: "prompt",
        )

        fb.nl_engine.conflict_flag_hints.assert_not_called()
        call_kwargs = fb.write_trigger_event.call_args.kwargs
        assert call_kwargs["status"] == "json_parse_failed"
        assert call_kwargs["flagged_hint_ids"] == []


# ---------------------------------------------------------------------------
# fire_conflict_detection — LLM exception → llm_timeout or llm_error
# ---------------------------------------------------------------------------

class TestFireConflictDetectionLlmException:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_timeout_exception_writes_llm_timeout_status(self, mock_llm, _kw, _model):
        """A timeout exception maps to status='llm_timeout'."""
        hint = {"id": 20, "feedback_text": "use stable state", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.side_effect = TimeoutError("Request timed out")
        fb = _make_fb(hints_by_id=[hint])

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-11",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[20]",
            prompt_builder=lambda hints: "prompt",
        )

        call_kwargs = fb.write_trigger_event.call_args.kwargs
        assert call_kwargs["status"] == "llm_timeout"
        fb.nl_engine.conflict_flag_hints.assert_not_called()

    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_generic_exception_writes_llm_error_status(self, mock_llm, _kw, _model):
        """A non-timeout exception maps to status='llm_error'."""
        hint = {"id": 21, "feedback_text": "click button", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.side_effect = RuntimeError("Connection refused")
        fb = _make_fb(hints_by_id=[hint])

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-12",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[21]",
            prompt_builder=lambda hints: "prompt",
        )

        call_kwargs = fb.write_trigger_event.call_args.kwargs
        assert call_kwargs["status"] == "llm_error"


# ---------------------------------------------------------------------------
# fire_conflict_detection — dict entry with missing id
# ---------------------------------------------------------------------------

class TestFireConflictDetectionMissingIdInEntry:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs", return_value={})
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_dict_entry_without_id_key_is_skipped(self, mock_llm, _kw, _model):
        """A dict flag entry without 'id' field is silently skipped."""
        hint = {"id": 30, "feedback_text": "fill form", "applied_count": 0,
                "success_count": 0, "failure_count": 0, "created_at": None}
        mock_llm.return_value = _llm_response(
            '{"flag": [{"reason": "no id here"}, {"id": 30, "reason": "valid"}]}'
        )
        fb = _make_fb(hints_by_id=[hint])
        fb.nl_engine.conflict_flag_hints.return_value = [30]

        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-13",
            domain="d.com",
            url="https://d.com",
            feedback_text=None,
            injected_hint_ids="[30]",
            prompt_builder=lambda hints: "prompt",
        )

        per_hint = fb.nl_engine.conflict_flag_hints.call_args[0][0]
        assert list(per_hint.keys()) == [30]


# ---------------------------------------------------------------------------
# fire_conflict_detection — telemetry submit failure is non-blocking
# ---------------------------------------------------------------------------

class TestFireConflictDetectionTelemetryFailure:
    @patch("src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model", return_value="gemini/test")
    @patch("src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm")
    def test_telemetry_submit_failure_does_not_propagate(self, mock_llm, _model):
        """If write_queue.submit raises for the combined closure, it is swallowed."""

        class FailingQueue:
            def submit(self, fn, *args, **kwargs):
                raise RuntimeError("queue full")

        fb = MagicMock()
        fb.write_queue = FailingQueue()
        fb.nl_engine.get_hints_by_id.return_value = []

        # Should not raise even though the queue fails
        fire_conflict_detection(
            feedback_loop=fb,
            trigger_type="trigger_1",
            workflow_id="wf-14",
            domain="d.com",
            url=None,
            feedback_text=None,
            injected_hint_ids="[]",
            prompt_builder=lambda hints: "prompt",
        )
