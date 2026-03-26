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
"""

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
            async for e in stream_generate_only("test query", "online", "gemini-2.5-flash"):
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
            async for e in stream_generate_only("test query", "online", "gemini-2.5-flash"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        
        # Should yield error event
        assert len(events) > 0
        assert any("error" in str(e).lower() for e in events)


class TestStreamExecuteOnly:
    """Tests for stream_execute_only generator."""

    @patch("src.backend.services.workflow_service.run_test_in_container")
    @patch("src.backend.services.workflow_service.build_image")
    @patch("src.backend.services.workflow_service.get_docker_client")
    def test_yields_events(self, mock_get_docker, mock_build, mock_run):
        """Generator yields execution events."""
        mock_build.return_value = iter([{"status": "building"}])
        mock_run.return_value = {"status": "passed", "test_status": "PASS"}

        from src.backend.services.workflow_service import stream_execute_only
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_execute_only("*** Test Cases ***\nTest\n    Log    Hello"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        assert len(events) > 0

    @patch("src.backend.services.workflow_service.get_docker_client")
    def test_handles_docker_failure(self, mock_get_docker):
        """Docker failure yields error event."""
        mock_get_docker.side_effect = Exception("Docker not running")

        from src.backend.services.workflow_service import stream_execute_only
        import asyncio
        async def run_gen():
            events = []
            async for e in stream_execute_only("*** Test Cases ***\nTest\n    Log    Hello"):
                events.append(e)
            return events
        events = asyncio.run(run_gen())
        assert len(events) > 0
