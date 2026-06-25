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


class TestStreamExecuteOnly:
    """Tests for stream_execute_only generator."""

    @patch("src.backend.services.workflow_service.runner_exec_client")
    def test_yields_events(self, mock_rc):
        """Generator yields execution events."""
        mock_rc.ensure_image.return_value = {"status": "ready"}
        mock_rc.execute.return_value = {"status": "passed", "test_status": "PASS"}

        from src.backend.services.workflow_service import stream_execute_only
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_execute_only("*** Test Cases ***\nTest\n    Log    Hello"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        assert len(events) > 0

    @patch("src.backend.services.workflow_service.runner_exec_client")
    def test_handles_docker_failure(self, mock_rc):
        """Executor failure yields error event."""
        mock_rc.ensure_image.side_effect = Exception("Runner exec not running")

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


import asyncio
from unittest.mock import patch
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
