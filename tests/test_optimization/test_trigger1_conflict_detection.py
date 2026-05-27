"""
Tests for Trigger 1 — LLM conflict detection in _fire_llm_conflict_detection
(workflow_service.py) and the Case B detection logic in _process_learning.

Validates:
1. timeout=30 is actually passed to litellm.completion
2. LiteLLM timeout → no hints flagged, non-blocking
3. LiteLLM success → correct hints flagged via conflict_flag_hints
4. No active hints → LiteLLM never called
5. Case B detection conditions: domain extraction, url=None, etc.

Patching strategy:
- litellm is imported locally inside _fire_llm_conflict_detection, so we
  patch at "litellm.completion" (the module-level attribute), not at
  "workflow_service.litellm" (which doesn't exist).
- _get_conflict_detection_model / _get_conflict_detection_completion_kwargs
  are imported locally via "from learning_config import ...", so we patch
  at their source module: src.backend.crew_ai.optimization.learning_config.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services.workflow_service import _fire_llm_conflict_detection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODEL_PATCH = "src.backend.crew_ai.optimization.learning_config._get_conflict_detection_model"
_KWARGS_PATCH = "src.backend.crew_ai.optimization.learning_config._get_conflict_detection_completion_kwargs"
_LITELLM_COMPLETION_PATCH = "litellm.completion"
_STREAM_CHUNK_BUILDER_PATCH = "litellm.stream_chunk_builder"


class SynchronousWriteQueue:
    """Executes submitted work immediately — no background thread."""
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _make_feedback_loop(active_hints: list):
    """
    Build a minimal FeedbackLoop-like object for _fire_llm_conflict_detection.

    active_hints: list of {"id": int, "feedback_text": str} — what
        get_active_hints_raw() returns.  Pass [] to exercise the no-hints path.

    conflict_flag_hints' default side_effect returns the recommendation dict's
    keys — schema v12 expects it to return the actually-flagged ID list, which
    is then passed by the combined closure to write_trigger_event as
    actually_flagged_hint_ids. Tests exercising the strong-history-suppression
    path override the side_effect to return a narrower list.
    """
    nl_engine = MagicMock()
    nl_engine.get_active_hints_raw.return_value = active_hints
    nl_engine.conflict_flag_hints = MagicMock(
        side_effect=lambda reasons, trigger_type=None: list(reasons.keys()),
    )

    feedback_loop = MagicMock()
    feedback_loop.nl_engine = nl_engine
    feedback_loop.write_queue = SynchronousWriteQueue()
    feedback_loop.write_trigger_event = MagicMock()

    return feedback_loop


def _make_feedback_loop_with_captured_submits(active_hints: list):
    """Like _make_feedback_loop but captures telemetry kwargs from write_trigger_event calls.

    After the schema-v12 refactor, write_trigger_event is no longer submitted
    directly to the write queue — it is invoked from inside a closure that
    also calls conflict_flag_hints first (so the telemetry write knows the
    actually-flagged set). The closure is what gets submitted. Telemetry
    kwargs are therefore captured by side-effecting on the write_trigger_event
    MagicMock itself, not by inspecting submit()'s positional fn argument.

    conflict_flag_hints returns the actually-flagged hint IDs (schema v12).
    The mock defaults to returning the input dict's keys so tests that don't
    explicitly configure return_value see "all flagged" behavior (no
    strong-history suppression mocked).
    """
    submitted_telemetry = []

    nl_engine = MagicMock()
    nl_engine.get_active_hints_raw.return_value = active_hints
    # Default: every recommended hint is actually flagged (no suppression).
    # Tests exercising the strong-history guard override this.
    nl_engine.conflict_flag_hints = MagicMock(
        side_effect=lambda reasons, trigger_type=None: list(reasons.keys()),
    )

    feedback_loop = MagicMock()
    feedback_loop.nl_engine = nl_engine

    def _capture_telemetry(**kwargs):
        submitted_telemetry.append(kwargs)
    feedback_loop.write_trigger_event = MagicMock(side_effect=_capture_telemetry)

    def sync_submit(fn, *args, **kwargs):
        fn(*args, **kwargs)

    write_queue = MagicMock()
    write_queue.submit.side_effect = sync_submit
    feedback_loop.write_queue = write_queue

    return feedback_loop, submitted_telemetry


def _make_litellm_response(flag_ids: list, reason: str = "test reason"):
    """Return a mock litellm.completion response with the given flag list."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps({"flag": flag_ids, "reason": reason})
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = 100
    resp.usage.completion_tokens = 30
    return resp


# ---------------------------------------------------------------------------
# Category 1: LiteLLM call contract
# ---------------------------------------------------------------------------

class TestLiteLLMCallContract:
    """Verify what _fire_llm_conflict_detection sends to litellm.completion."""

    ACTIVE_HINTS = [{"id": 1, "feedback_text": "use xpath not text locators"}]
    FAILED_CODE = "*** Test Cases ***\nOpen Browser\n    New Context    viewport=None"
    WORKING_CODE = "*** Test Cases ***\nOpen Browser"

    def test_timeout_is_30_seconds(self):
        """timeout=30 and stream=True must be passed explicitly to litellm.completion."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        response = _make_litellm_response(flag_ids=[])

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]) as mock_completion, \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-timeout-check",
                failed_code=self.FAILED_CODE,
                working_code=self.WORKING_CODE,
                url="https://demoqa.com/elements",
            )

        _, kwargs = mock_completion.call_args
        assert kwargs.get("timeout") == 30, (
            f"Expected timeout=30 passed to litellm.completion, got {kwargs.get('timeout')!r}"
        )
        assert kwargs.get("stream") is True, (
            f"Expected stream=True passed to litellm.completion, got {kwargs.get('stream')!r}"
        )

    def test_model_string_from_config(self):
        """The model string returned by _get_conflict_detection_model is forwarded."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        expected_model = "gemini/gemini-2.5-flash"

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]) as mock_completion, \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=_make_litellm_response([])), \
             patch(_MODEL_PATCH, return_value=expected_model), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-model-check",
                failed_code=self.FAILED_CODE,
                working_code=self.WORKING_CODE,
                url="https://demoqa.com/elements",
            )

        _, kwargs = mock_completion.call_args
        assert kwargs.get("model") == expected_model

    def test_extra_kwargs_are_forwarded(self):
        """Per-provider extra kwargs (e.g. Ollama api_base) reach litellm.completion."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        extra = {"api_base": "http://localhost:11434"}

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]) as mock_completion, \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=_make_litellm_response([])), \
             patch(_MODEL_PATCH, return_value="ollama/llama3"), \
             patch(_KWARGS_PATCH, return_value=extra):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-extra-kwargs",
                failed_code=self.FAILED_CODE,
                working_code=self.WORKING_CODE,
                url="https://example.com",
            )

        _, kwargs = mock_completion.call_args
        assert kwargs.get("api_base") == "http://localhost:11434"


# ---------------------------------------------------------------------------
# Category 2: No active hints — LiteLLM must NOT be called
# ---------------------------------------------------------------------------

class TestNoActiveHints:
    """When get_active_hints_raw returns [], the LLM call must be skipped."""

    def test_no_llm_call_when_no_active_hints(self):
        feedback_loop = _make_feedback_loop(active_hints=[])

        with patch(_LITELLM_COMPLETION_PATCH) as mock_completion, \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-no-hints",
                failed_code="*** Test Cases ***\nFailed Step",
                working_code="*** Test Cases ***\nFixed Step",
                url="https://demoqa.com",
            )

        mock_completion.assert_not_called()

    def test_conflict_flag_not_called_when_no_active_hints(self):
        feedback_loop = _make_feedback_loop(active_hints=[])

        with patch(_LITELLM_COMPLETION_PATCH), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-no-hints-flag",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        feedback_loop.nl_engine.conflict_flag_hints.assert_not_called()

    def test_telemetry_written_for_no_hints_path(self):
        """Even when skipping the LLM call, a telemetry submit must occur."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(active_hints=[])

        with patch(_LITELLM_COMPLETION_PATCH), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-telemetry-no-hints",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        assert feedback_loop.write_queue.submit.called, "Expected write_queue.submit called for telemetry"
        statuses = [kw.get("status") for kw in submitted]
        assert "no_active_hints" in statuses, (
            f"Expected 'no_active_hints' telemetry status, got: {statuses}"
        )


# ---------------------------------------------------------------------------
# Category 3: LiteLLM timeout — non-blocking, no hints flagged
# ---------------------------------------------------------------------------

class TestLiteLLMTimeout:
    """When litellm.completion raises any timeout-related exception,
    the function must be non-blocking and must NOT flag any hints."""

    ACTIVE_HINTS = [
        {"id": 10, "feedback_text": "use New Context    viewport=None"},
        {"id": 11, "feedback_text": "prefer text locators over xpath"},
    ]

    # Use a plain Exception whose str contains "timeout" — matches the
    # `"timeout" in str(e).lower()` check in _fire_llm_conflict_detection.
    TIMEOUT_EXC = Exception("Connection timed out after None seconds")

    def test_timeout_does_not_raise(self):
        """LiteLLM timeout must be caught — the function must return normally."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)

        with patch(_LITELLM_COMPLETION_PATCH, side_effect=self.TIMEOUT_EXC), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            # Must not raise
            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-timeout",
                failed_code="v1-with-viewport",
                working_code="v2-without-viewport",
                url="https://demoqa.com/forms",
            )

    def test_timeout_does_not_flag_any_hints(self):
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)

        with patch(_LITELLM_COMPLETION_PATCH, side_effect=self.TIMEOUT_EXC), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-timeout-no-flag",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com/forms",
            )

        feedback_loop.nl_engine.conflict_flag_hints.assert_not_called()

    def test_timeout_writes_telemetry_with_llm_timeout_status(self):
        """Telemetry row must be written with status='llm_timeout'."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(self.ACTIVE_HINTS)

        with patch(_LITELLM_COMPLETION_PATCH, side_effect=self.TIMEOUT_EXC), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-timeout-telemetry",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        statuses = [kw.get("status") for kw in submitted]
        assert "llm_timeout" in statuses, (
            f"Expected 'llm_timeout' in telemetry statuses, got: {statuses}"
        )

    def test_generic_exception_classified_as_llm_error(self):
        """A non-timeout exception must produce status='llm_error', not 'llm_timeout'."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(self.ACTIVE_HINTS)
        # This string does NOT contain "timeout"
        network_err = Exception("ConnectionResetError: peer closed the socket")

        with patch(_LITELLM_COMPLETION_PATCH, side_effect=network_err), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-generic-error",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        statuses = [kw.get("status") for kw in submitted]
        assert "llm_error" in statuses, (
            f"Expected 'llm_error' in telemetry statuses, got: {statuses}"
        )
        assert "llm_timeout" not in statuses


# ---------------------------------------------------------------------------
# Category 4: LiteLLM success — hints correctly flagged
# ---------------------------------------------------------------------------

class TestLiteLLMSuccess:
    """When litellm.completion returns valid JSON with flagged IDs,
    those hints must be passed to conflict_flag_hints."""

    ACTIVE_HINTS = [
        {"id": 20, "feedback_text": "add viewport=None to New Context"},
        {"id": 21, "feedback_text": "use xpath locators"},
        {"id": 22, "feedback_text": "always close browser at end"},
    ]

    def test_flagged_hints_sent_to_conflict_flag(self):
        """IDs returned by the LLM must be forwarded to conflict_flag_hints."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        response = _make_litellm_response(
            flag_ids=[20, 21],
            reason="viewport and xpath hints caused the failure",
        )

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-success-flag",
                failed_code="v1-with-viewport-and-xpath",
                working_code="v2-without-viewport-with-button2",
                url="https://demoqa.com/forms",
            )

        feedback_loop.nl_engine.conflict_flag_hints.assert_called_once()
        flagged_ids = feedback_loop.nl_engine.conflict_flag_hints.call_args[0][0]
        assert set(flagged_ids) == {20, 21}, (
            f"Expected hints {{20, 21}} flagged, got: {set(flagged_ids)}"
        )

    def test_unflagged_hint_not_in_conflict_call(self):
        """Hint 22 (not in LLM response) must NOT appear in conflict_flag_hints args."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        response = _make_litellm_response(flag_ids=[20])

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-partial-flag",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        flagged_ids = feedback_loop.nl_engine.conflict_flag_hints.call_args[0][0]
        assert 22 not in flagged_ids, f"Hint 22 should not have been flagged"

    def test_empty_flag_list_does_not_call_conflict_flag(self):
        """When LLM returns flag=[], conflict_flag_hints must NOT be called."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        response = _make_litellm_response(flag_ids=[], reason="no conflicts detected")

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-success-no-flag",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        feedback_loop.nl_engine.conflict_flag_hints.assert_not_called()

    def test_null_flag_value_does_not_call_conflict_flag(self):
        """If the LLM returns flag: null, do not crash and do not flag."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = json.dumps({"flag": None, "reason": "oops"})
        resp.usage = None

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=resp), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-null-flag",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        feedback_loop.nl_engine.conflict_flag_hints.assert_not_called()

    def test_success_telemetry_status_is_succeeded(self):
        """On success, telemetry must carry status='succeeded'."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(self.ACTIVE_HINTS)
        response = _make_litellm_response(flag_ids=[20])

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-telemetry-ok",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        statuses = [kw.get("status") for kw in submitted]
        assert "succeeded" in statuses, (
            f"Expected 'succeeded' in telemetry statuses, got: {statuses}"
        )

    def test_telemetry_carries_recommendation_and_enforcement_separately(self):
        """Schema v12: telemetry kwargs must include both flagged_hint_ids
        (LLM recommendation) and actually_flagged_hint_ids (enforcement set
        returned by conflict_flag_hints), populated independently."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(self.ACTIVE_HINTS)
        response = _make_litellm_response(flag_ids=[20, 21])
        # Simulate strong-history guard protecting hint 21 — only 20 is
        # actually flagged. Wins over the helper's default side_effect.
        feedback_loop.nl_engine.conflict_flag_hints.side_effect = None
        feedback_loop.nl_engine.conflict_flag_hints.return_value = [20]

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-rec-vs-enforce",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        succeeded = [kw for kw in submitted if kw.get("status") == "succeeded"]
        assert len(succeeded) == 1, f"Expected 1 succeeded row, got {len(succeeded)}: {submitted}"
        kw = succeeded[0]
        assert set(kw.get("flagged_hint_ids", [])) == {20, 21}, (
            f"flagged_hint_ids must record the full LLM recommendation; "
            f"got {kw.get('flagged_hint_ids')!r}"
        )
        assert kw.get("actually_flagged_hint_ids") == [20], (
            f"actually_flagged_hint_ids must record only the post-guard "
            f"enforcement set; got {kw.get('actually_flagged_hint_ids')!r}"
        )

    def test_no_active_hints_path_writes_empty_actually_flagged(self):
        """Early-return paths must populate actually_flagged_hint_ids=[]
        (not None) so the column is queryable as a JSON array on every row."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(active_hints=[])

        with patch(_LITELLM_COMPLETION_PATCH), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-empty-actually-flagged",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        no_hints_rows = [kw for kw in submitted if kw.get("status") == "no_active_hints"]
        assert no_hints_rows, f"Expected a no_active_hints telemetry row; got {submitted}"
        for kw in no_hints_rows:
            assert kw.get("actually_flagged_hint_ids") == [], (
                f"Early-return path must pass actually_flagged_hint_ids=[]; "
                f"got {kw.get('actually_flagged_hint_ids')!r}"
            )


# ---------------------------------------------------------------------------
# Category 5: JSON parse failure — non-blocking, no hints flagged
# ---------------------------------------------------------------------------

class TestLiteLLMJsonParseFail:

    ACTIVE_HINTS = [{"id": 30, "feedback_text": "use specific locator"}]

    def _run_with_bad_json(self):
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "This is not JSON at all"
        resp.usage = None

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=resp), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-bad-json",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )
        return feedback_loop

    def test_invalid_json_does_not_raise(self):
        self._run_with_bad_json()  # passes if no exception

    def test_invalid_json_does_not_flag_hints(self):
        feedback_loop = self._run_with_bad_json()
        feedback_loop.nl_engine.conflict_flag_hints.assert_not_called()

    def test_invalid_json_telemetry_status_is_json_parse_failed(self):
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(self.ACTIVE_HINTS)
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "not json"
        resp.usage = None

        with patch(_LITELLM_COMPLETION_PATCH, return_value=[]), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=resp), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-parse-fail-telemetry",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        statuses = [kw.get("status") for kw in submitted]
        assert "json_parse_failed" in statuses, (
            f"Expected 'json_parse_failed' in telemetry, got: {statuses}"
        )


# ---------------------------------------------------------------------------
# Category 6: Domain extraction and scope
# ---------------------------------------------------------------------------

class TestDomainExtraction:

    def test_url_none_calls_get_active_hints_with_none_none(self):
        """When url=None, get_active_hints_raw must be called with (None, None)."""
        feedback_loop = _make_feedback_loop(active_hints=[])

        with patch(_LITELLM_COMPLETION_PATCH), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-no-url",
                failed_code="v1", working_code="v2",
                url=None,
            )

        feedback_loop.nl_engine.get_active_hints_raw.assert_called_once_with(None, None)

    def test_domain_strips_www_and_subpath(self):
        """Domain extraction must strip www. prefix and sub-paths."""
        feedback_loop = _make_feedback_loop(active_hints=[])

        with patch(_LITELLM_COMPLETION_PATCH), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-domain",
                failed_code="v1", working_code="v2",
                url="https://www.demoqa.com/forms/create-employee",
            )

        call_args = feedback_loop.nl_engine.get_active_hints_raw.call_args[0]
        domain = call_args[0]
        assert domain == "demoqa.com", f"Expected 'demoqa.com', got '{domain}'"

    def test_url_passed_unchanged_to_get_active_hints(self):
        """The full URL must also be forwarded as second arg to get_active_hints_raw."""
        feedback_loop = _make_feedback_loop(active_hints=[])
        url = "https://demoqa.com/forms/create-employee"

        with patch(_LITELLM_COMPLETION_PATCH), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-url-forwarded",
                failed_code="v1", working_code="v2",
                url=url,
            )

        call_args = feedback_loop.nl_engine.get_active_hints_raw.call_args[0]
        assert call_args[1] == url, f"Expected full URL as second arg, got '{call_args[1]}'"


# ---------------------------------------------------------------------------
# Category 7: stream_chunk_builder empty-content fallback
#   Covers the LiteLLM 1.75.3 bug where extended-thinking tokens cause
#   stream_chunk_builder to return an empty choices[0].message.content.
#   _call_conflict_detection_llm must fall back to manually accumulated
#   delta.content and still produce a usable response.
# ---------------------------------------------------------------------------

class TestStreamChunkBuilderEmptyContentFallback:

    ACTIVE_HINTS = [
        {"id": 20, "feedback_text": "add viewport=None to New Context"},
        {"id": 21, "feedback_text": "use xpath locators"},
    ]

    def _make_chunk(self, content_fragment: str | None):
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = content_fragment
        return chunk

    def test_fallback_assembles_content_and_flags_hints(self):
        """When stream_chunk_builder returns empty content but delta.content
        fragments were present in the stream, hints must still be flagged."""
        feedback_loop = _make_feedback_loop(self.ACTIVE_HINTS)
        json_payload = json.dumps({"flag": [20, 21], "reason": "thinking mode bug"})
        mid = len(json_payload) // 2
        chunks = [
            self._make_chunk(json_payload[:mid]),
            self._make_chunk(json_payload[mid:]),
        ]

        broken_response = MagicMock()
        broken_response.choices = [MagicMock()]
        broken_response.choices[0].message.content = ""
        broken_response.usage = None

        with patch(_LITELLM_COMPLETION_PATCH, return_value=chunks), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=broken_response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-thinking-fallback",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com/forms",
            )

        feedback_loop.nl_engine.conflict_flag_hints.assert_called_once()
        flagged_ids = feedback_loop.nl_engine.conflict_flag_hints.call_args[0][0]
        assert set(flagged_ids) == {20, 21}

    def test_both_empty_produces_llm_error_not_json_parse_failed(self):
        """When both stream_chunk_builder content and delta.content are empty,
        the RuntimeError must be caught as llm_error — not json_parse_failed."""
        feedback_loop, submitted = _make_feedback_loop_with_captured_submits(self.ACTIVE_HINTS)
        chunks = [self._make_chunk(None)]

        empty_response = MagicMock()
        empty_response.choices = [MagicMock()]
        empty_response.choices[0].message.content = ""
        empty_response.usage = None

        with patch(_LITELLM_COMPLETION_PATCH, return_value=chunks), \
             patch(_STREAM_CHUNK_BUILDER_PATCH, return_value=empty_response), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):

            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id="wf-both-empty",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        statuses = [kw.get("status") for kw in submitted]
        assert "json_parse_failed" not in statuses, (
            "Empty content must not produce json_parse_failed"
        )
        assert "llm_error" in statuses, (
            f"Expected llm_error in telemetry statuses, got: {statuses}"
        )


# ---------------------------------------------------------------------------
# Category 7: Timeout classification parity — Trigger 1 vs Trigger 2
# ---------------------------------------------------------------------------

_CALL_LLM_PATCH = "src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm"


class TestTimeoutClassificationParity:
    """Trigger 1 and Trigger 2 must both classify a litellm.Timeout whose class
    name is 'Timeout' and message contains 'timed out' (two words, not one) as
    'llm_timeout'. Before Finding 7 fix, Trigger 2 used only str(e).lower() and
    missed the class name, so 'Timeout("Request timed out after 30s")' was
    wrongly classified as 'llm_error'."""

    def test_trigger2_timeout_classification_matches_trigger1(self):
        """FakeTimeout with class name 'Timeout' and message 'Request timed out
        after 30s' must produce status='llm_timeout' from both Trigger 1 and
        Trigger 2 code paths."""
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker

        # litellm.Timeout: class name supplies "timeout", message supplies
        # "timed out" — but NOT the string "timeout". Pre-fix Trigger 2 only
        # checked str(e).lower(), so it missed the class name and returned
        # "llm_error" instead of "llm_timeout".
        class Timeout(Exception):
            pass

        timeout_exc = Timeout("Request timed out after 30s")

        # ---- Trigger 1 -------------------------------------------------------
        active_hints = [{"id": 1, "feedback_text": "use xpath not text locators"}]
        feedback_loop_t1, submitted_t1 = _make_feedback_loop_with_captured_submits(active_hints)

        with patch(_LITELLM_COMPLETION_PATCH, side_effect=timeout_exc), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):
            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop_t1,
                workflow_id="wf-t1-timed-out",
                failed_code="v1", working_code="v2",
                url="https://demoqa.com",
            )

        statuses_t1 = [kw.get("status") for kw in submitted_t1]
        assert "llm_timeout" in statuses_t1, (
            f"Trigger 1 misclassified Timeout('timed out'): got {statuses_t1!r}"
        )

        # ---- Trigger 2 -------------------------------------------------------
        # Build a minimal FeedbackLoop with all real DB dependencies mocked out.
        mock_em = MagicMock()
        mock_record = MagicMock()
        mock_record.robot_code = "*** Test Cases ***\nOpen Browser"
        mock_record.injected_hint_ids = None
        mock_record.url = "https://demoqa.com"
        mock_record.workflow_id = "wf-t2-timed-out"
        mock_record.user_query = "click a button"
        mock_record.working_code = None
        mock_em.get.return_value = mock_record

        mock_nl = MagicMock()
        mock_nl.get_active_hints_raw.return_value = [
            {"id": 1, "feedback_text": "use xpath not text locators"},
        ]
        mock_nl.process_feedback.return_value = {
            "category": "locator", "specific_type": "test",
            "confidence": 0.9, "taxonomy_code": "L1", "matched_patterns": [],
        }

        write_trigger_event_mock = MagicMock()

        class SyncQueue:
            def submit(self, fn, *args, **kwargs):
                fn(*args, **kwargs)

        fl = FeedbackLoop(
            execution_memory=mock_em,
            failure_analyzer=MagicMock(),
            structural_engine=MagicMock(),
            keyword_engine=MagicMock(),
            anti_pattern_engine=MagicMock(),
            pattern_learner=MagicMock(),
            metrics_tracker=MagicMock(),
            contradiction_detector=MagicMock(),
            write_queue=SyncQueue(),
            circuit_breaker=LearningCircuitBreaker(),
        )
        fl.nl_engine = mock_nl
        fl.write_trigger_event = write_trigger_event_mock

        with patch(_CALL_LLM_PATCH, side_effect=timeout_exc), \
             patch(_MODEL_PATCH, return_value="gemini/gemini-2.5-flash"), \
             patch(_KWARGS_PATCH, return_value={}):
            fl.process_user_feedback(
                workflow_id="wf-t2-timed-out",
                feedback_text="locators are wrong",
                feedback_type="completely_wrong",
            )

        write_trigger_event_mock.assert_called()
        t2_status = write_trigger_event_mock.call_args.kwargs.get("status")
        assert t2_status == "llm_timeout", (
            f"Trigger 2 misclassified Timeout('timed out'): got {t2_status!r}"
        )


# ---------------------------------------------------------------------------
# Category 8: _classify_llm_error unit tests
# ---------------------------------------------------------------------------


class TestClassifyLlmError:
    """Unit tests for the shared _classify_llm_error helper in learning_config.py."""

    def test_class_name_timeout_returns_llm_timeout(self):
        """Exception whose class name contains 'Timeout' → 'llm_timeout'."""
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        class ReadTimeout(Exception):
            pass

        assert _classify_llm_error(ReadTimeout("connect error")) == "llm_timeout"

    def test_message_timed_out_returns_llm_timeout(self):
        """Exception with 'timed out' (two words) in message → 'llm_timeout'.

        litellm.Timeout uses this phrasing; checking only the class name would
        miss a bare Exception("Request timed out after 30s").
        """
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        assert _classify_llm_error(Exception("Request timed out after 30s")) == "llm_timeout"

    def test_generic_error_returns_llm_error(self):
        """Exception unrelated to timeouts → 'llm_error'."""
        from src.backend.crew_ai.optimization.learning_config import _classify_llm_error

        assert _classify_llm_error(Exception("rate limit exceeded")) == "llm_error"
