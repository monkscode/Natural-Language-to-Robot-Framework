"""Unit tests for progress_events.py — the CrewAI event bus bridge.

Tests the register/unregister lifecycle, event routing, progress monotonicity,
LLM call deduplication, tool event filtering, exception isolation, and
concurrent workflow isolation.

All tests are self-contained — they mock the crewai event bus and test the
handler functions directly, without importing crewai.events (avoids triggering
event bus initialization in test runs).
"""

import threading
from queue import Queue, Empty
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module under test — imported AFTER mocking crewai_event_bus to avoid
# registering real handlers during test collection
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset all module-level dicts before each test to prevent cross-contamination."""
    from src.backend.crew_ai import progress_events as pe

    with pe._lock:
        pe._task_to_workflow.clear()
        pe._workflow_queues.clear()
        pe._workflow_task_map.clear()
        pe._current_progress.clear()
        pe._llm_call_seen.clear()
        pe._tool_usage_seen.clear()
        pe._tool_finished_seen.clear()
    yield
    # Cleanup after test
    with pe._lock:
        pe._task_to_workflow.clear()
        pe._workflow_queues.clear()
        pe._workflow_task_map.clear()
        pe._current_progress.clear()
        pe._llm_call_seen.clear()
        pe._tool_usage_seen.clear()
        pe._tool_finished_seen.clear()


class TestRegisterUnregister:
    """Test the register/unregister lifecycle."""

    def test_register_creates_mappings(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _task_to_workflow,
            _workflow_queues,
            _workflow_task_map,
            _current_progress,
            _llm_call_seen,
            _tool_usage_seen,
            _tool_finished_seen,
        )

        q = Queue()
        task_id_map = {"task-a": 0, "task-b": 1, "task-c": 2}
        register_workflow("wf-1", q, task_id_map)

        assert _workflow_queues["wf-1"] is q
        assert _workflow_task_map["wf-1"] == task_id_map
        assert _task_to_workflow["task-a"] == "wf-1"
        assert _task_to_workflow["task-c"] == "wf-1"
        assert _current_progress["wf-1"] == 0
        assert _llm_call_seen["wf-1"] == set()
        assert _tool_usage_seen["wf-1"] == set()
        assert _tool_finished_seen["wf-1"] == set()

    def test_unregister_cleans_up(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            unregister_workflow,
            _task_to_workflow,
            _workflow_queues,
            _workflow_task_map,
            _current_progress,
            _llm_call_seen,
        )

        q = Queue()
        task_id_map = {"task-a": 0, "task-b": 1}
        register_workflow("wf-1", q, task_id_map)
        unregister_workflow("wf-1")

        assert "wf-1" not in _workflow_queues
        assert "wf-1" not in _workflow_task_map
        assert "task-a" not in _task_to_workflow
        assert "task-b" not in _task_to_workflow
        assert "wf-1" not in _current_progress
        assert "wf-1" not in _llm_call_seen

    def test_unregister_nonexistent_is_noop(self):
        """Unregistering a workflow that doesn't exist should not raise."""
        from src.backend.crew_ai.progress_events import unregister_workflow

        # Should not raise
        unregister_workflow("nonexistent-workflow")


class TestTaskStartedEvent:
    """Test TaskStartedEvent routing."""

    def test_routes_to_correct_queue(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})

        # Create a mock TaskStartedEvent
        event = MagicMock()
        event.task = MagicMock()
        event.task.id = "task-0"

        _on_task_started(source=None, event=event)

        msg = q.get(timeout=1)
        assert msg["status"] == "running"
        assert msg["progress"] == 5
        assert "🧠" in msg["message"]
        assert "Analyzing" in msg["message"]

    def test_unknown_task_id_ignored(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})

        # Event with unknown task_id
        event = MagicMock()
        event.task = MagicMock()
        event.task.id = "unknown-task"

        _on_task_started(source=None, event=event)

        # Queue should be empty
        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_none_task_ignored(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})

        event = MagicMock()
        event.task = None

        _on_task_started(source=None, event=event)

        with pytest.raises(Empty):
            q.get(timeout=0.1)


class TestProgressMonotonicity:
    """Test that progress only moves forward."""

    def test_progress_only_moves_forward(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_started,
            _on_task_completed,
        )

        q = Queue()
        register_workflow("wf-1", q, {
            "task-0": 0, "task-1": 1, "task-2": 2,
        })

        # Fire task 2 completed (80%) first
        event_2_done = MagicMock()
        event_2_done.task = MagicMock()
        event_2_done.task.id = "task-2"
        _on_task_completed(source=None, event=event_2_done)

        msg = q.get(timeout=1)
        assert msg["progress"] == 80

        # Now fire task 0 started (5%) — should be discarded
        event_0_start = MagicMock()
        event_0_start.task = MagicMock()
        event_0_start.task.id = "task-0"
        _on_task_started(source=None, event=event_0_start)

        with pytest.raises(Empty):
            q.get(timeout=0.1)


class TestLLMCallDeduplication:
    """Test that only the first LLM call per task produces a message."""

    def test_only_first_llm_call_per_task_shows_message(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_llm_call_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})

        event = MagicMock()
        event.task_id = "task-0"

        # First call — should produce message
        _on_llm_call_started(source=None, event=event)
        msg = q.get(timeout=1)
        assert "📋" in msg["message"]

        # Second call — should be silently skipped
        _on_llm_call_started(source=None, event=event)
        with pytest.raises(Empty):
            q.get(timeout=0.1)


class TestToolEvents:
    """Test tool event handling."""

    def test_non_browser_tool_ignored(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_tool_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-1": 1})

        event = MagicMock()
        event.tool_name = "keyword_search"
        event.task_id = "task-1"

        _on_tool_started(source=None, event=event)

        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_browser_tool_started_produces_message(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_tool_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-1": 1})

        event = MagicMock()
        event.tool_name = "batch_browser_automation"
        event.task_id = "task-1"

        _on_tool_started(source=None, event=event)

        msg = q.get(timeout=1)
        assert msg["progress"] == 30
        assert "🌐" in msg["message"]

    def test_tool_finished_extracts_element_count_from_string(self):
        """Element count extraction works when output is a stringified dict
        (the text-based tool path in CrewAI)."""
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_tool_finished,
            _current_progress,
            _lock,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-1": 1})

        # Simulate text-based path: output is str(dict)
        tool_output = str({
            "status": "success",
            "summary": {"total_elements": 8, "successful": 7, "failed": 1},
        })

        event = MagicMock()
        event.tool_name = "batch_browser_automation"
        event.task_id = "task-1"
        event.output = tool_output

        # Set progress to a value below 55 so the message goes through
        with _lock:
            _current_progress["wf-1"] = 0

        _on_tool_finished(source=None, event=event)

        msg = q.get(timeout=1)
        assert "8" in msg["message"]
        assert "📍" in msg["message"]
        assert msg["progress"] == 55

    def test_tool_finished_fallback_when_no_count(self):
        """Fallback message when element count cannot be extracted."""
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_tool_finished,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-1": 1})

        event = MagicMock()
        event.tool_name = "batch_browser_automation"
        event.task_id = "task-1"
        event.output = "some unparseable string"

        _on_tool_finished(source=None, event=event)

        msg = q.get(timeout=1)
        assert "Page elements detected" in msg["message"]
        assert "📍" in msg["message"]


class TestHandlerExceptionIsolation:
    """Test that handler exceptions don't propagate."""

    def test_handler_exception_does_not_propagate(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_started,
        )

        # Register with a queue that raises on put()
        broken_q = MagicMock(spec=Queue)
        broken_q.put.side_effect = RuntimeError("Queue exploded")

        register_workflow("wf-1", broken_q, {"task-0": 0})

        event = MagicMock()
        event.task = MagicMock()
        event.task.id = "task-0"

        # Should NOT raise — exception is caught inside handler
        _on_task_started(source=None, event=event)

        # Verify put was attempted
        broken_q.put.assert_called_once()


class TestConcurrentWorkflows:
    """Test that concurrent workflows are isolated."""

    def test_events_route_to_correct_queues(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_started,
        )

        q_a = Queue()
        q_b = Queue()

        register_workflow("wf-a", q_a, {"task-a0": 0, "task-a1": 1})
        register_workflow("wf-b", q_b, {"task-b0": 0, "task-b1": 1})

        # Fire event for workflow A
        event_a = MagicMock()
        event_a.task = MagicMock()
        event_a.task.id = "task-a0"
        _on_task_started(source=None, event=event_a)

        # Fire event for workflow B
        event_b = MagicMock()
        event_b.task = MagicMock()
        event_b.task.id = "task-b1"
        _on_task_started(source=None, event=event_b)

        # Verify isolation
        msg_a = q_a.get(timeout=1)
        assert msg_a["progress"] == 5  # Task 0 started

        msg_b = q_b.get(timeout=1)
        assert msg_b["progress"] == 22  # Task 1 started (scanning elements)

        # Each queue should only have its own event
        with pytest.raises(Empty):
            q_a.get(timeout=0.1)
        with pytest.raises(Empty):
            q_b.get(timeout=0.1)
