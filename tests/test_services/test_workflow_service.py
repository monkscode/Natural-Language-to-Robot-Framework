"""
Mocked integration tests for src.backend.services.workflow_service.

Purpose: workflow_service orchestrates generate → execute flows.  It's the
         glue between the API, CrewAI agents, and Docker.  These tests verify
         the streaming generators, error handling, and metrics collection.

Tests:
  - stream_generate_only yields events
  - stream_generate_only handles agent failure
  - stream_execute_only yields events
  - stream_execute_only handles Docker failure
  - stream_generate_and_run yields combined events
  - Workflow metrics collection
  - Concurrency slot management (acquire/release/capacity)
  - Hint metadata cache thread safety
"""

import os
import tempfile
import threading
import asyncio
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestStreamGenerateOnly:
    """Tests for stream_generate_only generator."""

    @patch("src.backend.services.workflow_service.run_agentic_workflow")
    def test_yields_events(self, mock_run_agentic):
        """Generator yields SSE-formatted events."""
        mock_run_agentic.return_value = iter([
            {"status": "running", "message": "generating"},
            {"status": "complete", "robot_code": "*** Test Cases ***"}
        ])

        from src.backend.services.workflow_service import stream_generate_only
        # Since it's async we should use it with async tools or run it, but this is an async generator:
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_generate_only("test query", "gemini", "gemini-2.5-flash"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        assert len(events) > 0

    @patch("src.backend.services.workflow_service.run_agentic_workflow")
    def test_handles_agent_failure(self, mock_run_agentic):
        """Agent failure yields error event, doesn't crash."""
        mock_run_agentic.side_effect = Exception("Agent crashed")

        from src.backend.services.workflow_service import stream_generate_only
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_generate_only("test query", "gemini", "gemini-2.5-flash"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        
        # Should yield error event
        assert len(events) > 0
        assert any("error" in str(e).lower() for e in events)


class TestRunIsRecordedAtStart:
    """A run must be countable from the moment it starts, not only when it ends.

    Both terminal paths write a row — 'generated' on success, 'error' on a
    generation failure — but a run that dies without reaching either (the
    process is killed, the container restarts, the machine OOMs) left no trace
    at all. Nothing could tell "never started" apart from "started and
    vanished", so the failure rate any dashboard computed was optimistic by
    exactly the runs that disappeared.

    An opening row fixes that: a vanished run shows up as a row still sitting
    at 'running' long after it was created. 'running' is an existing status
    (the execute path already uses it), so no vocabulary or UI change.
    """

    _WF_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    def _run(self, events, user=None):
        """Drive stream_generate_only, collecting EVERY _record_run call."""
        calls = []

        def _capture(run_id, user_arg, user_query, status, **kw):
            calls.append({"run_id": run_id, "status": status,
                          "user_query": user_query, "user": user_arg, **kw})

        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   return_value=iter(events)), \
             patch("src.backend.services.workflow_service._record_run",
                   side_effect=_capture), \
             patch("src.backend.services.workflow_service._acquire_workflow_slot",
                   return_value=True):
            from src.backend.services.workflow_service import stream_generate_only

            async def run_gen():
                out = []
                async for e in stream_generate_only("login to github", "gemini",
                                                    "gemini-2.5-flash", user=user):
                    out.append(e)
                return out

            sse = asyncio.run(run_gen())
        return calls, sse

    def test_row_is_opened_before_anything_can_fail(self):
        """The opening row is written from the first event that carries an id.

        A truly vanished run — killed process, container restart — cannot be
        simulated here, because this harness always reaches the generator's
        "finished without generating code" fallback. What it does pin is the
        thing that makes a vanished run visible: the 'running' row exists
        first, written before any terminal path is reached.
        """
        calls, _ = self._run([
            {"status": "running", "message": "planning", "workflow_id": self._WF_ID},
        ])
        assert calls[0]["status"] == "running"
        assert calls[0]["run_id"] == self._WF_ID
        assert calls[0]["user_query"] == "login to github"

    def test_opening_row_precedes_the_terminal_row(self):
        calls, _ = self._run([
            {"status": "running", "message": "planning", "workflow_id": self._WF_ID},
            {"status": "complete", "robot_code": "*** Test Cases ***\nT\n    Log    hi",
             "workflow_id": self._WF_ID},
        ])
        assert [c["status"] for c in calls] == ["running", "generated"]
        assert {c["run_id"] for c in calls} == {self._WF_ID}

    def test_opening_row_precedes_an_error_row(self):
        calls, _ = self._run([
            {"status": "running", "message": "planning", "workflow_id": self._WF_ID},
            {"status": "error", "message": "LLM offline", "workflow_id": self._WF_ID},
        ])
        assert [c["status"] for c in calls] == ["running", "error"]

    def test_written_once_however_many_events_carry_the_id(self):
        calls, _ = self._run([
            {"status": "running", "message": "a", "workflow_id": self._WF_ID},
            {"status": "running", "message": "b", "workflow_id": self._WF_ID},
            {"status": "running", "message": "c", "workflow_id": self._WF_ID},
        ])
        assert len([c for c in calls if c["status"] == "running"]) == 1

    def test_carries_the_user_so_the_row_is_org_scoped(self):
        user = {"user_id": "u-1", "email": "someone@example.com", "org_id": "org-9"}
        calls, _ = self._run(
            [{"status": "running", "message": "planning", "workflow_id": self._WF_ID}],
            user=user,
        )
        assert calls[0]["user"] == user

    def test_no_row_before_the_id_is_known(self):
        """Events without a workflow_id cannot be attributed to a run."""
        calls, _ = self._run([{"status": "running", "message": "planning"}])
        assert [c for c in calls if c["status"] == "running"] == []

    def test_non_uuid_id_is_not_recorded(self):
        calls, _ = self._run([
            {"status": "running", "message": "planning", "workflow_id": "not-a-uuid"},
        ])
        assert calls == []

    def test_opening_row_failure_does_not_break_the_stream(self):
        """History bookkeeping must never cost a run.

        Only the opening write is made to fail: _record_run swallows registry
        errors internally, so in production it does not raise at all — this
        pins the extra guard around the thread hop the opening write adds.
        """
        def _explode_on_open(run_id, user, user_query, status, **kw):
            if status == "running":
                raise RuntimeError("postgres down")

        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   return_value=iter([
                       {"status": "running", "message": "planning", "workflow_id": self._WF_ID},
                       {"status": "complete", "robot_code": "*** Test Cases ***",
                        "workflow_id": self._WF_ID},
                   ])), \
             patch("src.backend.services.workflow_service._record_run",
                   side_effect=_explode_on_open), \
             patch("src.backend.services.workflow_service._acquire_workflow_slot",
                   return_value=True):
            from src.backend.services.workflow_service import stream_generate_only

            async def run_gen():
                return [e async for e in stream_generate_only(
                    "login to github", "gemini", "gemini-2.5-flash")]

            sse = asyncio.run(run_gen())  # must not raise
        assert any("complete" in str(e) for e in sse)


class TestGenerationFailureIsRecorded:
    """A generation failure must leave a row behind.

    Before this, _GenerationError returned before _record_run and the metrics
    block runs only after the dryrun gate — so a run that never produced code
    was invisible to History and to Grafana alike. Status 'error' already
    exists in test_runs and in the SPA's filter list, so nothing new is needed
    downstream.
    """

    _WF_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    def _run(self, events, user=None):
        """Drive stream_generate_only over a scripted generation event list."""
        recorded = {}

        def _capture(run_id, user_arg, user_query, status, **kw):
            recorded.update({"run_id": run_id, "status": status,
                             "user_query": user_query, **kw})

        with patch("src.backend.services.workflow_service.run_agentic_workflow",
                   return_value=iter(events)), \
             patch("src.backend.services.workflow_service._record_run",
                   side_effect=_capture), \
             patch("src.backend.services.workflow_service._acquire_workflow_slot",
                   return_value=True):
            from src.backend.services.workflow_service import stream_generate_only

            async def run_gen():
                out = []
                async for e in stream_generate_only("login to github", "gemini",
                                                    "gemini-2.5-flash", user=user):
                    out.append(e)
                return out

            sse = asyncio.run(run_gen())
        return recorded, sse

    def test_error_event_writes_an_error_row(self):
        recorded, _ = self._run([
            {"status": "running", "message": "planning"},
            {"status": "error", "message": "LLM offline",
             "workflow_id": self._WF_ID},
        ])
        assert recorded["run_id"] == self._WF_ID
        assert recorded["status"] == "error"
        assert recorded["user_query"] == "login to github"

    def test_row_carries_the_failure_reason(self):
        """Angle E: countable is not enough — a failure has to be diagnosable."""
        recorded, _ = self._run([
            {"status": "error", "message": "Vertex 429 Resource exhausted",
             "workflow_id": self._WF_ID},
        ])
        assert recorded["error_message"] == "Vertex 429 Resource exhausted"

    def test_reason_is_truncated(self):
        """A stack-trace-sized message must not bloat every history query."""
        recorded, _ = self._run([
            {"status": "error", "message": "x" * 9000, "workflow_id": self._WF_ID},
        ])
        assert len(recorded["error_message"]) == 2000

    def test_no_row_without_a_workflow_id(self):
        """Nothing to key the row on; the SSE already told the user."""
        recorded, sse = self._run([{"status": "error", "message": "died early"}])
        assert recorded == {}
        assert any("error" in str(e).lower() for e in sse)

    def test_non_uuid_workflow_id_records_nothing(self):
        recorded, _ = self._run([
            {"status": "error", "message": "boom", "workflow_id": "not-a-uuid"},
        ])
        assert recorded == {}

    def test_finished_without_code_is_now_attributable(self):
        """This was a documented limitation and is no longer one.

        The fallback used to have no workflow_id to key a row on, because only
        complete/error events carried one. The opening 'running' event now
        carries it too, so the run is already in result_store by the time this
        path fires and the failure lands on the right row.

        Still true: an event stream with no id at all records nothing — see
        test_no_row_without_a_workflow_id.
        """
        recorded, sse = self._run([
            {"status": "running", "message": "planning", "workflow_id": self._WF_ID},
        ])
        assert recorded["run_id"] == self._WF_ID
        assert recorded["status"] == "error"
        assert "without generating code" in recorded["error_message"]
        assert any("without generating code" in str(e) for e in sse)

    def test_successful_generation_is_not_recorded_as_error(self):
        recorded, _ = self._run([
            {"status": "complete", "robot_code": "*** Test Cases ***\nT\n    Log    hi",
             "workflow_id": self._WF_ID},
        ])
        assert recorded["status"] == "generated"


class TestStreamExecuteOnly:
    """Tests for stream_execute_only generator.

    stream_execute_only drives the real _stream_docker_execution
    (workflow_service.py:1154), which reaches three external dependencies
    imported at module level: get_run_registry() writes a test_runs row
    (_record_run before it, _set_run_status inside it); get_artifact_store()
    writes a test.robot file under robot_tests/
    (run_dir(run_id, create=True)); and, on the success path, _process_learning
    (:1222) calls get_feedback_loop() as the first statement in its try block
    (:361) — before the empty-user_query guard a few lines below it, so it
    runs even though these tests never reach the learning call itself. With
    OPTIMIZATION_ENABLED=true (the real .env), that constructs a real
    FeedbackLoop backed by a live psycopg connection to Postgres. All three
    are real Postgres/disk writes, which CLAUDE.md forbids from tests, so all
    three are patched here (one patch each blocks every write path through
    them, the same reasoning TestRunIsRecordedAtStart uses for _record_run) —
    only runner_exec_client stands in for the thing that is actually
    external: the executor. run_dir is pointed at pytest's tmp_path so
    _write_test_file still succeeds on a real, disposable directory outside
    robot_tests/, which keeps the happy-path and failure-path behaviour
    these tests actually assert.
    """

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    @patch("src.backend.services.workflow_service.get_artifact_store")
    @patch("src.backend.services.workflow_service.get_run_registry")
    @patch("src.backend.services.workflow_service.runner_exec_client")
    def test_yields_events(self, mock_rc, mock_registry, mock_store, mock_feedback, tmp_path):
        """Generator yields execution events; no Postgres or robot_tests/ writes."""
        mock_rc.ensure_image.return_value = {"status": "ready"}
        mock_rc.execute.return_value = {"status": "passed", "test_status": "PASS"}
        mock_store.return_value.run_dir.return_value = tmp_path
        mock_feedback.return_value = None

        from src.backend.services.workflow_service import stream_execute_only
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_execute_only("*** Test Cases ***\nTest\n    Log    Hello"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        assert len(events) > 0

    @patch("src.backend.services.workflow_service.get_feedback_loop")
    @patch("src.backend.services.workflow_service.get_artifact_store")
    @patch("src.backend.services.workflow_service.get_run_registry")
    @patch("src.backend.services.workflow_service.runner_exec_client")
    def test_handles_docker_failure(self, mock_rc, mock_registry, mock_store, mock_feedback, tmp_path):
        """Executor failure yields error event; no Postgres or robot_tests/ writes."""
        mock_rc.ensure_image.side_effect = Exception("Runner exec not running")
        mock_store.return_value.run_dir.return_value = tmp_path
        mock_feedback.return_value = None

        from src.backend.services.workflow_service import stream_execute_only
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_execute_only("*** Test Cases ***\nTest\n    Log    Hello"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        assert len(events) > 0


class TestVertexCredentialValidation:
    """Tests for vertex provider credential validation in run_agentic_workflow."""

    def test_vertex_missing_credentials_yields_error(self):
        """Verify vertex provider yields error when VERTEXAI_CREDENTIALS is empty."""
        from src.backend.services.workflow_service import run_agentic_workflow
        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": ""}, clear=False):
            events = list(run_agentic_workflow("test query", "vertex", "gemini-2.5-flash"))
        assert any("VERTEXAI_CREDENTIALS" in e.get("message", "") for e in events)

    def test_vertex_missing_credentials_file_yields_error(self):
        """Verify vertex provider yields error when credentials file does not exist."""
        from src.backend.services.workflow_service import run_agentic_workflow
        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "/nonexistent/path.json"}, clear=False):
            events = list(run_agentic_workflow("test query", "vertex", "gemini-2.5-flash"))
        assert any("not found" in e.get("message", "").lower() for e in events)

    def test_vertex_missing_project_yields_error(self):
        """Verify vertex provider yields error when VERTEXAI_PROJECT is not set."""
        from src.backend.services.workflow_service import run_agentic_workflow
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b"{}")
            creds_path = f.name
        try:
            with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": creds_path}, clear=False), \
                 patch("src.backend.services.workflow_service.settings") as mock_settings:
                mock_settings.VERTEXAI_PROJECT = None
                mock_settings.VERTEXAI_LOCATION = "us-central1"
                events = list(run_agentic_workflow("test query", "vertex", "gemini-2.5-flash"))
            assert any("VERTEXAI_PROJECT" in e.get("message", "") for e in events)
        finally:
            os.unlink(creds_path)

    def test_vertex_missing_location_yields_error(self):
        """Verify vertex provider yields error when VERTEXAI_LOCATION is not set."""
        from src.backend.services.workflow_service import run_agentic_workflow
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b"{}")
            creds_path = f.name
        try:
            with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": creds_path}, clear=False), \
                 patch("src.backend.services.workflow_service.settings") as mock_settings:
                mock_settings.VERTEXAI_PROJECT = "test-project"
                mock_settings.VERTEXAI_LOCATION = None
                events = list(run_agentic_workflow("test query", "vertex", "gemini-2.5-flash"))
            assert any("VERTEXAI_LOCATION" in e.get("message", "") for e in events)
        finally:
            os.unlink(creds_path)


class TestWorkflowSlotManagement:
    """Tests for _acquire_workflow_slot / _release_workflow_slot / get_active_workflow_count."""

    def setup_method(self):
        """Reset slot counter to 0 before each test."""
        import src.backend.services.workflow_service as ws
        with ws._active_workflow_lock:
            ws._active_workflow_count = 0

    def teardown_method(self):
        """Reset slot counter to 0 after each test (in case test left it dirty)."""
        import src.backend.services.workflow_service as ws
        with ws._active_workflow_lock:
            ws._active_workflow_count = 0

    def test_acquire_fills_to_capacity_then_rejects(self):
        """Acquire MAX_CONCURRENT_WORKFLOWS slots; the next acquire returns False."""
        from src.backend.services.workflow_service import _acquire_workflow_slot
        with patch("src.backend.services.workflow_service.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 3
            assert _acquire_workflow_slot() is True
            assert _acquire_workflow_slot() is True
            assert _acquire_workflow_slot() is True
            # At capacity — next must be rejected
            assert _acquire_workflow_slot() is False

    def test_release_frees_slot_allowing_new_acquire(self):
        """Acquire 3 slots, release 1; a new acquire should succeed."""
        from src.backend.services.workflow_service import _acquire_workflow_slot, _release_workflow_slot
        with patch("src.backend.services.workflow_service.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 3
            _acquire_workflow_slot()
            _acquire_workflow_slot()
            _acquire_workflow_slot()
            assert _acquire_workflow_slot() is False  # at capacity
            _release_workflow_slot()
            assert _acquire_workflow_slot() is True   # slot freed

    def test_release_without_acquire_stays_at_zero(self):
        """Release without a prior acquire must not take count below 0."""
        from src.backend.services.workflow_service import _release_workflow_slot, get_active_workflow_count
        _release_workflow_slot()
        assert get_active_workflow_count() == 0

    def test_get_active_workflow_count_reflects_acquired_slots(self):
        """get_active_workflow_count() returns the number of held slots."""
        from src.backend.services.workflow_service import _acquire_workflow_slot, get_active_workflow_count
        with patch("src.backend.services.workflow_service.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 10
            _acquire_workflow_slot()
            _acquire_workflow_slot()
            _acquire_workflow_slot()
            _acquire_workflow_slot()
            _acquire_workflow_slot()
            assert get_active_workflow_count() == 5

    def test_concurrent_acquire_release_final_count_is_zero(self):
        """20 threads each acquire+release 100 times; final count must be 0."""
        from concurrent.futures import ThreadPoolExecutor
        from src.backend.services.workflow_service import _acquire_workflow_slot, _release_workflow_slot, get_active_workflow_count
        with patch("src.backend.services.workflow_service.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 50

            def worker():
                for _ in range(100):
                    acquired = _acquire_workflow_slot()
                    if acquired:
                        _release_workflow_slot()

            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(worker) for _ in range(20)]
            for f in futures:
                f.result()  # propagates any thread exception to the test

            assert get_active_workflow_count() == 0

    def test_stream_generate_only_yields_capacity_error_when_full(self):
        """When at capacity, stream_generate_only yields a capacity error immediately."""
        from src.backend.services.workflow_service import stream_generate_only
        with patch("src.backend.services.workflow_service.settings") as mock_settings:
            mock_settings.MAX_CONCURRENT_WORKFLOWS = 1
            # Manually fill the one slot
            import src.backend.services.workflow_service as ws
            with ws._active_workflow_lock:
                ws._active_workflow_count = 1

            async def run_gen():
                events = []
                async for e in stream_generate_only("test query", "gemini", "gemini-2.5-flash"):
                    events.append(e)
                return events

            events = asyncio.run(run_gen())
            assert len(events) == 1
            payload = json.loads(events[0].replace("data: ", "").strip())
            assert payload["status"] == "error"
            assert "at capacity" in payload["message"]


class TestHintMetadataCacheConcurrency:
    """Tests for _hint_metadata_cache thread safety under concurrent access."""

    def test_concurrent_write_and_pop_no_exceptions(self):
        """Two threads write/pop from _hint_metadata_cache 1000 times without errors."""
        from concurrent.futures import ThreadPoolExecutor
        import src.backend.services.workflow_service as ws

        def writer():
            for i in range(1000):
                key = f"workflow-writer-{i}"
                with ws._hint_metadata_lock:
                    ws._hint_metadata_cache[key] = {"count": i, "sources": []}

        def popper():
            for i in range(1000):
                key = f"workflow-popper-{i}"
                with ws._hint_metadata_lock:
                    ws._hint_metadata_cache[key] = {"count": i, "sources": []}
                with ws._hint_metadata_lock:
                    ws._hint_metadata_cache.pop(key, {})

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(writer)
                f2 = executor.submit(popper)
            f1.result()  # propagates any thread exception to the test
            f2.result()
        finally:
            with ws._hint_metadata_lock:
                for i in range(1000):
                    ws._hint_metadata_cache.pop(f"workflow-writer-{i}", None)


from src.backend.services import workflow_service


def test_stream_docker_execution_uses_runner_exec_client(tmp_path):
    async def _drive():
        chunks = []
        gen = workflow_service._stream_docker_execution(
            run_id="abc123",
            robot_code="*** Test Cases ***\nT\n    Log    x\n",
            user_query="q",
            release_slot=lambda: None,
        )
        async for chunk in gen:
            chunks.append(chunk)
        return chunks

    with patch("src.backend.services.workflow_service.runner_exec_client") as rc, \
         patch("src.backend.services.workflow_service.get_artifact_store") as gas, \
         patch.object(workflow_service, "_set_run_status"), \
         patch.object(workflow_service, "_process_learning"), \
         patch.object(workflow_service, "inline_report_screenshots", return_value=0), \
         patch.object(workflow_service, "_safe_evict_hint_metadata"):
        gas.return_value.run_dir.return_value = tmp_path
        gas.return_value.persist_run.return_value = None
        rc.ensure_image.return_value = {"status": "ready"}
        rc.execute.return_value = {"status": "complete", "test_status": "passed",
                                   "result": {"logs": ""}}
        chunks = asyncio.run(_drive())

    rc.execute.assert_called_once_with("abc123", "test.robot")
    assert any("passed" in c for c in chunks)
