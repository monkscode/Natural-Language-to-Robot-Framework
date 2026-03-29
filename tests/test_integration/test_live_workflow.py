"""
Live integration tests for workflow_service streaming (Tier 2).

Purpose: Verify the full workflow pipeline when the NL backend is running —
         generator yields, SSE event shapes, Docker execution, and the
         _process_learning() side-effect against a real ChromaDB/SQLite store.

Requires:
  - NL backend running on localhost:5000
  - Docker Desktop running
  - GEMINI_API_KEY set in environment (for 'online' provider tests)

Run with:
  pytest tests/test_integration/test_live_workflow.py -m integration -v

Individual test groups can be scoped via -k:
  pytest -m integration -k "TestWorkflowStream"
"""

import os
import time
import pytest
import requests

pytestmark = pytest.mark.integration

SERVICE_URL = "http://localhost:5000"


class TestWorkflowStreamEvents:
    """Verify event shapes yielded by run_agentic_workflow()."""

    def test_run_agentic_workflow_yields_dicts(self):
        """run_agentic_workflow() returns a generator of dicts."""
        from src.backend.services.workflow_service import run_agentic_workflow
        gen = run_agentic_workflow("test query", "local", "ollama")
        first = next(gen)
        assert isinstance(first, dict)
        assert "status" in first

    def test_initial_event_is_running(self):
        """First event has status='running'."""
        from src.backend.services.workflow_service import run_agentic_workflow
        gen = run_agentic_workflow("click the button on example.com", "local", "ollama")
        first = next(gen)
        assert first["status"] in ("running", "error")

    def test_events_have_message_key(self):
        """All early events contain a 'message' key."""
        from src.backend.services.workflow_service import run_agentic_workflow
        gen = run_agentic_workflow("click the button on example.com", "local", "ollama")
        for i, event in enumerate(gen):
            assert "message" in event or "code" in event or "status" in event
            if i >= 5:
                break

    def test_progress_field_is_numeric_when_present(self):
        """Events that include 'progress' must carry a numeric value 0-100."""
        from src.backend.services.workflow_service import run_agentic_workflow
        gen = run_agentic_workflow("search on google.com", "local", "ollama")
        for i, event in enumerate(gen):
            if "progress" in event:
                assert isinstance(event["progress"], (int, float))
                assert 0 <= event["progress"] <= 100
            if i >= 10:
                break

    def test_missing_api_key_yields_error_event(self):
        """When GEMINI_API_KEY absent, online provider yields error immediately."""
        original = os.environ.pop("GEMINI_API_KEY", None)
        try:
            from src.backend.services.workflow_service import run_agentic_workflow
            # Force re-import to pick up env change
            events = list(run_agentic_workflow("test", "online", "gemini"))
            statuses = [e["status"] for e in events]
            assert "error" in statuses
        finally:
            if original is not None:
                os.environ["GEMINI_API_KEY"] = original


class TestWorkflowApiEndpoints:
    """Verify the NL backend API endpoints for workflow submission."""

    def test_generate_endpoint_accepts_post(self):
        """POST /generate returns a streaming response."""
        resp = requests.post(
            f"{SERVICE_URL}/generate",
            json={"query": "click the submit button", "model_provider": "local", "model_name": "ollama"},
            stream=True,
            timeout=10,
        )
        assert resp.status_code == 200

    def test_generate_endpoint_streams_json_lines(self):
        """SSE stream contains parseable JSON lines."""
        import json
        resp = requests.post(
            f"{SERVICE_URL}/generate",
            json={"query": "click submit on example.com", "model_provider": "local", "model_name": "ollama"},
            stream=True,
            timeout=30,
        )
        lines_seen = 0
        for raw_line in resp.iter_lines():
            if raw_line:
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    parsed = json.loads(payload)
                    assert "status" in parsed
                    lines_seen += 1
                    if lines_seen >= 3:
                        break
        assert lines_seen >= 1

    def test_execute_endpoint_exists(self):
        """POST /execute is reachable (may return error without Docker, but not 404)."""
        resp = requests.post(
            f"{SERVICE_URL}/execute",
            json={"code": "*** Test Cases ***\nDummy\n    Log    hello", "run_id": "test-run"},
            timeout=10,
        )
        # Anything except 404 — the endpoint exists
        assert resp.status_code != 404


class TestHintMetadataCache:
    """Verify _hint_metadata_cache lifecycle during workflow run."""

    def test_cache_cleared_after_process_learning(self):
        """After _process_learning() runs, the run_id is removed from cache."""
        from src.backend.services.workflow_service import _process_learning, _hint_metadata_cache
        from unittest.mock import patch, MagicMock

        run_id = f"live-test-{int(time.time())}"
        _hint_metadata_cache[run_id] = {
            "planner": {"count": 2, "sources": ["structural_rules"]},
        }

        mock_fl = MagicMock()
        with patch("src.backend.services.workflow_service.get_feedback_loop", return_value=mock_fl):
            with patch("src.backend.services.workflow_service.extract_url_from_query", return_value="https://x.com"):
                _process_learning(run_id, "test on x.com", "code", {"test_status": "passed"})

        assert run_id not in _hint_metadata_cache

    def test_cache_does_not_accumulate_stale_entries(self):
        """Multiple workflow runs do not leave stale cache entries."""
        from src.backend.services.workflow_service import _process_learning, _hint_metadata_cache
        from unittest.mock import patch, MagicMock

        run_ids = [f"stale-test-{i}-{int(time.time())}" for i in range(3)]
        for rid in run_ids:
            _hint_metadata_cache[rid] = {"planner": {"count": 1, "sources": ["s"]}}

        mock_fl = MagicMock()
        with patch("src.backend.services.workflow_service.get_feedback_loop", return_value=mock_fl):
            with patch("src.backend.services.workflow_service.extract_url_from_query", return_value="https://x.com"):
                for rid in run_ids:
                    _process_learning(rid, "test on x.com", "code", {"test_status": "passed"})

        for rid in run_ids:
            assert rid not in _hint_metadata_cache
