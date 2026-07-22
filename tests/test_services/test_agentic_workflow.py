"""
Tests for run_agentic_workflow() and supporting utilities in workflow_service.py.

Scope:
  - _SlotReleaser countdown-latch (releases slot only when all participants finish)
  - _safe_delete_temp_metrics / _safe_evict_hint_metadata (exception-swallowing helpers)
  - run_workflow_in_thread (queue relay, exception handling, releaser.done())
  - Robot Framework code extraction from task[2] output (via the shared
    dryrun_service.extract_and_normalize_robot_code helper):
      Strategy 1 — pydantic.code
      Strategy 2 — json_dict['code']
      Strategy 3a — raw JSON {"code": "..."}
      Strategy 3b — raw non-JSON (legacy)
      Escaped-newline normalisation (\\n → actual newlines)
      Multiple *** Settings *** blocks → last one wins
      Single *** Settings *** → prefix stripped
      No *** Settings *** → *** Test Cases *** fallback
      Trailing JSON artifact ("}) stripped
  - Dryrun gate wiring (validate_and_repair is patched here; its internals are
    covered in test_dryrun_service.py):
      passed/skipped → clean complete event
      failed/unverified → complete event carries dryrun_status + dryrun_errors
      repair cost folded into crewai_*/total_* WorkflowMetrics
  - Workflow completion: success → complete event
  - run_agentic_workflow gemini key missing → early error event

Task 16: run_crew returns the single-task ASSEMBLER crew (the pipeline is two
single-task kickoffs around the deterministic element stage); delivered code
comes from tasks[-1]. The LLM validator was replaced earlier by the
deterministic robot --dryrun gate.
"""

import os
import json
import asyncio
import threading
import uuid
import pytest
from queue import Queue
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

VALID_ROBOT_CODE = (
    "*** Settings ***\n"
    "Library    Browser\n\n"
    "*** Test Cases ***\n"
    "Search Test\n"
    "    New Page    https://example.com\n"
)

# Default dryrun gate stub — passthrough that delivers the code unchanged.
# The gate's real behaviour (Docker, repair loop, cost) is covered in
# test_dryrun_service.py; here we only verify run_agentic_workflow's wiring.
def _passthrough_gate(workflow_id, code, *args, **kwargs):
    return {"code": code, "dryrun_status": "passed", "repair_usage": {}}


def _make_run_crew_result(
    *,
    pydantic_code=None,
    json_dict_code=None,
    raw_code=None,
):
    """Build the 5-tuple returned by run_crew() — the single-task ASSEMBLER
    crew (Task 16). Delivered code comes from tasks[-1] (the assembler)."""
    # ---- tasks[-1]: robot code output ----
    task2 = MagicMock()
    if pydantic_code is not None:
        task2.output.pydantic = MagicMock(code=pydantic_code)
        task2.output.json_dict = None
        task2.output.raw = None
    elif json_dict_code is not None:
        task2.output.pydantic = None
        task2.output.json_dict = json_dict_code      # e.g. {'code': '*** Settings ***...'}
        task2.output.raw = None
    else:
        task2.output.pydantic = None
        task2.output.json_dict = None
        task2.output.raw = raw_code if raw_code is not None else VALID_ROBOT_CODE

    # ---- assembler crew (one task) ----
    crew = MagicMock()
    crew.tasks = [task2]
    usage = MagicMock(
        total_tokens=200, prompt_tokens=160,
        completion_tokens=40, successful_requests=8
    )
    crew.calculate_usage_metrics.return_value = usage

    llm_monitor = MagicMock()
    llm_monitor.get_numeric_stats.return_value = {}

    return (MagicMock(), crew, None, {}, llm_monitor)


def _run_workflow(query="login to github.com", provider="gemini", model="gemini-2.5-flash",
                  crew_result=None, gemini_key="test-key", gate=None):
    """
    Run run_agentic_workflow with standard mocks and collect all events as a list.
    The dryrun gate (validate_and_repair) is patched to a passthrough by default so
    no real Docker call happens; pass `gate` to override its return/side_effect.
    Returns: list[dict]
    """
    if crew_result is None:
        crew_result = _make_run_crew_result()
    gate = gate if gate is not None else _passthrough_gate

    with patch("src.backend.services.workflow_service.run_crew",
               return_value=crew_result), \
         patch("src.backend.services.workflow_service.validate_and_repair",
               side_effect=gate), \
         patch("src.backend.services.workflow_service.get_temp_metrics_storage") as mock_storage, \
         patch("src.backend.services.workflow_service.get_workflow_metrics_collector"), \
         patch.dict(os.environ, {"GEMINI_API_KEY": gemini_key}):

        mock_storage.return_value.read_browser_metrics.return_value = {}

        from src.backend.services.workflow_service import run_agentic_workflow
        return list(run_agentic_workflow(query, provider, model))


def _event_statuses(events):
    return [e.get("status") for e in events]


# ---------------------------------------------------------------------------
# _SlotReleaser
# ---------------------------------------------------------------------------

class TestSlotReleaser:
    """Countdown latch that releases the workflow slot only when all participants finish."""

    def setup_method(self):
        import src.backend.services.workflow_service as ws
        with ws._active_workflow_lock:
            ws._active_workflow_count = 0

    def teardown_method(self):
        import src.backend.services.workflow_service as ws
        with ws._active_workflow_lock:
            ws._active_workflow_count = 0

    def test_slot_not_released_until_last_done(self):
        """With 2 participants, slot is held after first done() and freed after second."""
        from src.backend.services.workflow_service import _SlotReleaser, _acquire_workflow_slot, get_active_workflow_count
        with patch("src.backend.services.workflow_service.settings") as mock_s:
            mock_s.MAX_CONCURRENT_WORKFLOWS = 10
            _acquire_workflow_slot()
            assert get_active_workflow_count() == 1

            releaser = _SlotReleaser(participant_count=2)
            releaser.done()  # first participant — count 2→1, slot still held
            assert get_active_workflow_count() == 1

            releaser.done()  # second participant — count 1→0, slot released
            assert get_active_workflow_count() == 0

    def test_single_participant_releases_on_first_done(self):
        """participant_count=1 releases slot on the single done() call."""
        from src.backend.services.workflow_service import _SlotReleaser, _acquire_workflow_slot, get_active_workflow_count
        with patch("src.backend.services.workflow_service.settings") as mock_s:
            mock_s.MAX_CONCURRENT_WORKFLOWS = 10
            _acquire_workflow_slot()
            assert get_active_workflow_count() == 1

            releaser = _SlotReleaser(participant_count=1)
            releaser.done()
            assert get_active_workflow_count() == 0

    def test_concurrent_done_releases_exactly_once(self):
        """20 threads each call done() once on a 20-participant latch — slot released once."""
        from src.backend.services.workflow_service import _SlotReleaser, _acquire_workflow_slot, get_active_workflow_count
        with patch("src.backend.services.workflow_service.settings") as mock_s:
            mock_s.MAX_CONCURRENT_WORKFLOWS = 100
            _acquire_workflow_slot()
            assert get_active_workflow_count() == 1

            n = 20
            releaser = _SlotReleaser(participant_count=n)
            errors = []

            def worker():
                try:
                    releaser.done()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            assert get_active_workflow_count() == 0

    def test_done_idempotent_when_over_called(self):
        """done() releases the slot exactly once even when called more times
        than participant_count — the early-release + finally-fallback pattern
        used by _stream_docker_execution. Extra calls must not release another
        workflow's slot, and the count must not underflow."""
        from src.backend.services.workflow_service import _SlotReleaser, _acquire_workflow_slot, get_active_workflow_count
        with patch("src.backend.services.workflow_service.settings") as mock_s:
            mock_s.MAX_CONCURRENT_WORKFLOWS = 10
            _acquire_workflow_slot()  # workflow A
            _acquire_workflow_slot()  # workflow B — a different, concurrent workflow
            assert get_active_workflow_count() == 2

            releaser = _SlotReleaser(participant_count=1)
            releaser.done()  # A's early release (from _stream_docker_execution) — 2→1
            assert get_active_workflow_count() == 1

            releaser.done()  # A's finally-block fallback — must be a no-op
            releaser.done()  # extra over-call — still a no-op
            assert get_active_workflow_count() == 1  # B's slot untouched
            assert releaser._count == 0  # guard floored the count; no underflow


# ---------------------------------------------------------------------------
# _safe_delete_temp_metrics / _safe_evict_hint_metadata
# ---------------------------------------------------------------------------

class TestSafeCleanupHelpers:
    """Both helpers swallow all exceptions and never raise."""

    def test_safe_delete_swallows_storage_exception(self):
        """get_temp_metrics_storage().delete_temp_file() error is silently swallowed."""
        with patch("src.backend.services.workflow_service.get_temp_metrics_storage") as mock_s:
            mock_s.return_value.delete_temp_file.side_effect = OSError("disk full")

            from src.backend.services.workflow_service import _safe_delete_temp_metrics
            # Must not raise
            _safe_delete_temp_metrics("any-workflow-id")

    def test_safe_delete_calls_delete_with_correct_id(self):
        """_safe_delete_temp_metrics passes the workflow_id to delete_temp_file."""
        with patch("src.backend.services.workflow_service.get_temp_metrics_storage") as mock_s:
            from src.backend.services.workflow_service import _safe_delete_temp_metrics
            _safe_delete_temp_metrics("wf-123")

        mock_s.return_value.delete_temp_file.assert_called_once_with("wf-123")

    def test_safe_evict_removes_existing_key(self):
        """_safe_evict_hint_metadata pops the workflow_id entry from the cache."""
        import src.backend.services.workflow_service as ws

        with ws._hint_metadata_lock:
            ws._hint_metadata_cache["wf-evict"] = {"count": 3}

        from src.backend.services.workflow_service import _safe_evict_hint_metadata
        _safe_evict_hint_metadata("wf-evict")

        with ws._hint_metadata_lock:
            assert "wf-evict" not in ws._hint_metadata_cache

    def test_safe_evict_missing_key_does_not_raise(self):
        """_safe_evict_hint_metadata on an absent key is a no-op."""
        from src.backend.services.workflow_service import _safe_evict_hint_metadata
        _safe_evict_hint_metadata("nonexistent-workflow-id")  # must not raise


# ---------------------------------------------------------------------------
# run_workflow_in_thread
# ---------------------------------------------------------------------------

class TestRunWorkflowInThread:
    """Thread function relays events to queue and signals the latch."""

    def test_puts_all_events_in_queue(self):
        """Events yielded by run_agentic_workflow are put onto the queue."""
        events = [
            {"status": "running", "message": "start"},
            {"status": "complete", "robot_code": "*** Test Cases ***"},
        ]

        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   return_value=iter(events)):
            from src.backend.services.workflow_service import run_workflow_in_thread
            q = Queue()
            run_workflow_in_thread(q, "query", "gemini", "model")

        collected = []
        while not q.empty():
            collected.append(q.get_nowait())

        assert collected == events

    def test_exception_puts_error_event_in_queue(self):
        """If the workflow raises, an error event is put and the thread does not crash."""
        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   side_effect=RuntimeError("boom")):
            from src.backend.services.workflow_service import run_workflow_in_thread
            q = Queue()
            run_workflow_in_thread(q, "query", "gemini", "model")

        event = q.get_nowait()
        assert event["status"] == "error"
        assert "boom" in event["message"]

    def test_releaser_done_called_in_finally(self):
        """releaser.done() is called regardless of success or failure."""
        releaser = MagicMock()

        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   return_value=iter([])):
            from src.backend.services.workflow_service import run_workflow_in_thread
            run_workflow_in_thread(Queue(), "query", "gemini", "model", releaser=releaser)

        releaser.done.assert_called_once()

    def test_releaser_done_called_on_exception(self):
        """releaser.done() fires even when the workflow raises."""
        releaser = MagicMock()

        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   side_effect=Exception("crash")):
            from src.backend.services.workflow_service import run_workflow_in_thread
            run_workflow_in_thread(Queue(), "query", "gemini", "model", releaser=releaser)

        releaser.done.assert_called_once()

    def test_no_releaser_does_not_raise(self):
        """Omitting releaser (None) must not raise."""
        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   return_value=iter([])):
            from src.backend.services.workflow_service import run_workflow_in_thread
            run_workflow_in_thread(Queue(), "query", "gemini", "model", releaser=None)


# ---------------------------------------------------------------------------
# Robot code extraction strategies
# ---------------------------------------------------------------------------

class TestCodeExtractionStrategies:
    """run_agentic_workflow extracts robot code from task[2] via three strategies."""

    def test_strategy1_pydantic_code(self):
        """Strategy 1: code read from output.pydantic.code."""
        result = _make_run_crew_result(pydantic_code=VALID_ROBOT_CODE)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert "*** Settings ***" in complete["robot_code"]

    def test_strategy2_json_dict_code(self):
        """Strategy 2: code read from output.json_dict['code']."""
        result = _make_run_crew_result(json_dict_code={"code": VALID_ROBOT_CODE})
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert "*** Settings ***" in complete["robot_code"]

    def test_strategy3a_raw_json_with_code_key(self):
        """Strategy 3a: raw is JSON string {"code": "...robot..."}."""
        raw = json.dumps({"code": VALID_ROBOT_CODE})
        result = _make_run_crew_result(raw_code=raw)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert "*** Settings ***" in complete["robot_code"]

    def test_strategy3b_raw_legacy_non_json(self):
        """Strategy 3b: raw is plain Robot Framework text (not JSON)."""
        result = _make_run_crew_result(raw_code=VALID_ROBOT_CODE)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert "*** Settings ***" in complete["robot_code"]

    def test_escaped_newlines_are_normalised(self):
        """Literal \\n in LLM output is converted to actual newlines."""
        escaped = "*** Settings ***\\nLibrary    Browser\\n\\n*** Test Cases ***\\nTest\\n    Log  hi"
        result = _make_run_crew_result(raw_code=escaped)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert "\n" in complete["robot_code"]
        assert "\\n" not in complete["robot_code"]

    def test_escaped_tabs_are_normalised(self):
        """Literal \\t in LLM output is converted to actual tab characters."""
        escaped = "*** Settings ***\\nLibrary    Browser\\n\\n*** Test Cases ***\\nTest\\n\\tLog\\thi"
        result = _make_run_crew_result(raw_code=escaped)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert "\t" in complete["robot_code"]

    def test_multiple_settings_blocks_uses_last(self):
        """When multiple *** Settings *** sections appear, the last one is used."""
        code_with_dups = (
            "*** Settings ***\nLibrary    OldLib\n\n"
            "*** Settings ***\nLibrary    Browser\n\n"
            "*** Test Cases ***\nTest\n    Log    hi\n"
        )
        result = _make_run_crew_result(raw_code=code_with_dups)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        # Only the second Settings block should remain
        assert complete["robot_code"].count("*** Settings ***") == 1
        assert "Browser" in complete["robot_code"]
        assert "OldLib" not in complete["robot_code"]

    def test_prefix_text_before_settings_is_stripped(self):
        """Any text before *** Settings *** is removed."""
        code_with_prefix = "Here is the code:\n\n" + VALID_ROBOT_CODE
        result = _make_run_crew_result(raw_code=code_with_prefix)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert not complete["robot_code"].startswith("Here")
        assert complete["robot_code"].startswith("***")

    def test_no_settings_falls_back_to_test_cases(self):
        """No *** Settings *** found → code starts from *** Test Cases ***."""
        code_no_settings = (
            "Preamble text\n\n"
            "*** Test Cases ***\n"
            "Test\n    Log    hi\n"
        )
        result = _make_run_crew_result(raw_code=code_no_settings)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert complete["robot_code"].startswith("*** Test Cases ***")

    def test_trailing_json_artifact_stripped(self):
        """Trailing '\"} artifact (JSON closing) is stripped from robot code."""
        code_with_artifact = VALID_ROBOT_CODE + '\"}'
        result = _make_run_crew_result(raw_code=code_with_artifact)
        events = _run_workflow(crew_result=result)
        complete = next(e for e in events if e.get("status") == "complete")
        assert not complete["robot_code"].endswith('"}"')
        assert not complete["robot_code"].endswith('"}')


# ---------------------------------------------------------------------------
# Workflow completion paths
# ---------------------------------------------------------------------------

class TestWorkflowCompletionPaths:
    """Success, dryrun-gate soft delivery, repair-cost fold, and exception paths."""

    def test_success_yields_complete_event_with_robot_code(self):
        """Passed gate → final event has status=complete with robot_code key."""
        events = _run_workflow()
        complete_events = [e for e in events if e.get("status") == "complete"]
        assert len(complete_events) == 1
        assert "robot_code" in complete_events[0]
        assert complete_events[0]["robot_code"]

    def test_success_yields_workflow_id(self):
        """complete event includes a workflow_id."""
        events = _run_workflow()
        complete = next(e for e in events if e.get("status") == "complete")
        assert "workflow_id" in complete
        assert complete["workflow_id"]

    def test_passed_gate_complete_event_has_no_dryrun_fields(self):
        """On a passed gate, the complete event carries NO dryrun_status/errors
        (clean happy path — the fields appear only when the gate did not pass)."""
        events = _run_workflow()
        complete = next(e for e in events if e.get("status") == "complete")
        assert "dryrun_status" not in complete
        assert "dryrun_errors" not in complete

    def test_dryrun_failed_still_delivers_with_dryrun_fields(self):
        """SOFT gate: a failed dryrun STILL yields status:'complete' with robot_code
        plus dryrun_status:'failed' + dryrun_errors (no new top-level status)."""
        def failed_gate(workflow_id, code, *a, **k):
            return {"code": code, "dryrun_status": "failed",
                    "dryrun_errors": "No keyword with name 'Cilck' found. Did you mean: Browser.Click",
                    "repair_usage": {}}
        events = _run_workflow(gate=failed_gate)
        complete = next((e for e in events if e.get("status") == "complete"), None)
        assert complete is not None, "soft gate must still deliver a complete event"
        assert complete["robot_code"]
        assert complete["dryrun_status"] == "failed"
        assert "Did you mean: Browser.Click" in complete["dryrun_errors"]
        # It is NOT an error event — delivery succeeds.
        assert "error" not in _event_statuses(events)

    def test_docker_down_degrades_to_unverified_and_delivers(self):
        """Graceful degrade: Docker-unavailable gate → complete with
        dryrun_status:'unverified', code still delivered."""
        def unverified_gate(workflow_id, code, *a, **k):
            return {"code": code, "dryrun_status": "unverified",
                    "message": "Docker unavailable", "repair_usage": {}}
        events = _run_workflow(gate=unverified_gate)
        complete = next((e for e in events if e.get("status") == "complete"), None)
        assert complete is not None
        assert complete["dryrun_status"] == "unverified"
        assert complete["robot_code"]
        assert "error" not in _event_statuses(events)

    def test_repair_cost_folded_into_crewai_and_total_metrics(self):
        """The gate's repair_usage is added into crewai_*/total_* WorkflowMetrics
        and the main-crew calls are NOT double-counted (decision 5 / §5)."""
        from src.backend.core.workflow_metrics import calculate_crewai_cost

        # Baseline crewai metrics from the mocked crew usage (200 tokens, 8 calls).
        base = calculate_crewai_cost(
            {'total_tokens': 200, 'prompt_tokens': 160, 'completion_tokens': 40,
             'successful_requests': 8},
            model_name="gemini-2.5-flash",
        )
        repair_usage = {'llm_calls': 2, 'cost': 0.004, 'tokens': 50,
                        'prompt_tokens': 40, 'completion_tokens': 10}

        def repaired_gate(workflow_id, code, *a, **k):
            return {"code": code, "dryrun_status": "passed", "repair_usage": repair_usage}

        captured = {}

        def _capture(metrics, **kwargs):
            captured["m"] = metrics

        crew_result = _make_run_crew_result()
        with patch("src.backend.services.workflow_service.run_crew",
                   return_value=crew_result), \
             patch("src.backend.services.workflow_service.validate_and_repair",
                   side_effect=repaired_gate), \
             patch("src.backend.services.workflow_service.get_temp_metrics_storage") as mock_s, \
             patch("src.backend.services.workflow_service.get_workflow_metrics_collector") as mock_coll, \
             patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
            mock_s.return_value.read_browser_metrics.return_value = {}
            mock_coll.return_value.record_workflow.side_effect = _capture

            from src.backend.services.workflow_service import run_agentic_workflow
            list(run_agentic_workflow("q", "gemini", "gemini-2.5-flash"))

        m = captured["m"]
        # Repair calls/cost/tokens folded into the CrewAI bucket (no double count of
        # the main crew — its usage is counted exactly once via the mocked metrics).
        assert m.crewai_llm_calls == base['llm_calls'] + 2
        assert abs(m.crewai_cost - round(base['cost'] + 0.004, 6)) < 1e-9
        assert m.crewai_tokens == base['tokens'] + 50
        # Totals derive from the CrewAI bucket, so they include the repair cost too.
        assert m.total_llm_calls == base['llm_calls'] + 2
        assert abs(m.total_cost - round(base['cost'] + 0.004, 6)) < 1e-9

    def test_run_crew_exception_yields_error_event(self):
        """If run_crew() raises, the generator yields an error event."""
        with patch("src.backend.services.workflow_service.run_crew",
                   side_effect=RuntimeError("LLM offline")), \
             patch("src.backend.services.workflow_service.get_temp_metrics_storage"), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):

            from src.backend.services.workflow_service import run_agentic_workflow
            events = list(run_agentic_workflow("query", "gemini", "model"))

        assert any(e.get("status") == "error" for e in events)

    @pytest.mark.parametrize("exc", [RuntimeError("LLM offline"), ValueError("bad json")])
    def test_error_event_carries_workflow_id(self, exc):
        """Error events carry workflow_id so the bench can detach failed runs —
        their pre-failure LLM calls are already recorded in llm_traces."""
        with patch("src.backend.services.workflow_service.run_crew",
                   side_effect=exc), \
             patch("src.backend.services.workflow_service.get_temp_metrics_storage"), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):

            from src.backend.services.workflow_service import run_agentic_workflow
            events = list(run_agentic_workflow("query", "gemini", "model"))

        error = next(e for e in events if e.get("status") == "error")
        uuid.UUID(error["workflow_id"])  # present and a real UUID

    def test_gemini_missing_api_key_yields_early_error(self):
        """When GEMINI_API_KEY is absent, an error event is yielded before run_crew()."""
        with patch("src.backend.services.workflow_service.run_crew") as mock_run_crew, \
             patch.dict(os.environ, {}, clear=True):
            # Ensure the key is absent
            os.environ.pop("GEMINI_API_KEY", None)

            from src.backend.services.workflow_service import run_agentic_workflow
            events = list(run_agentic_workflow("query", "gemini", "model"))

        mock_run_crew.assert_not_called()
        assert any(e.get("status") == "error" for e in events)

    def test_hint_metadata_stored_in_cache_during_generation(self):
        """When run_crew returns hint_metadata, it is placed in _hint_metadata_cache.

        run_agentic_workflow (generate-only path) stores hint_metadata but does NOT
        consume it — that happens later in stream_execute_only via _process_learning.
        The cache entry must exist under the workflow_id emitted in the complete event,
        and its data (minus the internal _stored_at timestamp) must match hint_data.
        """
        import src.backend.services.workflow_service as ws
        # Full production schema: count (injected), available (candidates before cap), sources.
        # _process_learning reads both count and available; omitting available causes it to
        # silently fall back to count, masking a regression in the generation contract.
        hint_data = {"planner": {"count": 3, "available": 7, "sources": ["structural"]}}

        # Build a run_crew mock that returns non-empty hint_metadata
        crew_result = _make_run_crew_result()
        # Override the hint_metadata element (4th in the 5-tuple)
        crew_result_with_hints = (crew_result[0], crew_result[1], crew_result[2],
                                  hint_data, crew_result[4])

        with patch("src.backend.services.workflow_service.run_crew",
                   return_value=crew_result_with_hints), \
             patch("src.backend.services.workflow_service.validate_and_repair",
                   side_effect=_passthrough_gate), \
             patch("src.backend.services.workflow_service.get_temp_metrics_storage") as mock_s, \
             patch("src.backend.services.workflow_service.get_workflow_metrics_collector"), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):

            mock_s.return_value.read_browser_metrics.return_value = {}

            from src.backend.services.workflow_service import run_agentic_workflow
            events = list(run_agentic_workflow("query", "gemini", "model"))

        complete_event = next((e for e in events if e.get("status") == "complete"), None)
        assert complete_event is not None, "Expected a complete event"

        workflow_id = complete_event.get("workflow_id")
        assert workflow_id is not None, "complete event must carry workflow_id"

        # The cache entry must exist and contain the hint_data written during generation.
        # _stored_at is an internal timestamp added by _store_hint_metadata — strip it.
        cached = ws._hint_metadata_cache.get(workflow_id)
        assert cached is not None, (
            f"_hint_metadata_cache has no entry for workflow_id={workflow_id!r}; "
            "hint metadata was not stored"
        )
        cached_data = {k: v for k, v in cached.items() if k != "_stored_at"}
        assert cached_data == hint_data

        # Clean up so the module-level cache does not leak between tests
        ws._hint_metadata_cache.pop(workflow_id, None)

    def test_metrics_collection_failure_does_not_abort_workflow(self):
        """If calculate_usage_metrics() fails, the workflow still completes."""
        result = _make_run_crew_result()
        # Make calculate_usage_metrics raise
        result[1].calculate_usage_metrics.side_effect = Exception("metrics error")

        events = _run_workflow(crew_result=result)
        # Should still complete — metrics failures are non-fatal
        assert any(e.get("status") == "complete" for e in events)
