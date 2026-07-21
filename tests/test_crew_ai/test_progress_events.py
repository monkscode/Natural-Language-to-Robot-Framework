"""Unit tests for progress_events.py — the CrewAI event bus bridge.

Tests the register/unregister lifecycle, event routing, progress monotonicity,
LLM call deduplication, exception isolation, concurrent workflow isolation,
and the Task 16 additions: register_task (late-built assembler task) and
push_stage_progress (the deterministic python stage's SSE channel).

The CrewAI tool-usage handlers were REMOVED in Task 16 — the batch browser
tool is now called directly by the python stage (element_identification), so
no ToolUsage events ever fire for it; the stage pushes 22/30/55/60 itself via
push_stage_progress.

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

    def _clear():
        with pe._lock:
            pe._task_to_workflow.clear()
            pe._workflow_queues.clear()
            pe._workflow_task_map.clear()
            pe._current_progress.clear()
            pe._llm_call_seen.clear()

    _clear()
    yield
    _clear()


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
        )

        q = Queue()
        task_id_map = {"task-a": 0, "task-c": 2}
        register_workflow("wf-1", q, task_id_map)

        assert _workflow_queues["wf-1"] is q
        assert _workflow_task_map["wf-1"] == task_id_map
        assert _task_to_workflow["task-a"] == "wf-1"
        assert _task_to_workflow["task-c"] == "wf-1"
        assert _current_progress["wf-1"] == 0
        assert _llm_call_seen["wf-1"] == set()

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


class TestRegisterTask:
    """Task 16: the assembler task is built AFTER the python stage (its
    description embeds the merged steps), so it is registered incrementally
    into the already-registered workflow."""

    def test_registered_task_routes_events(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            register_task,
            _on_task_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})
        register_task("wf-1", "task-2", 2)

        event = MagicMock()
        event.task = MagicMock()
        event.task.id = "task-2"
        _on_task_started(source=None, event=event)

        # Ladder: index-1 completion (60) synthesized, then index-2 start (62).
        assert q.get(timeout=1)["progress"] == 60
        assert q.get(timeout=1)["progress"] == 62

    def test_register_task_unknown_workflow_is_noop(self):
        from src.backend.crew_ai.progress_events import (
            register_task,
            _task_to_workflow,
        )

        register_task("nonexistent-wf", "task-2", 2)  # must not raise
        assert "task-2" not in _task_to_workflow

    def test_unregister_cleans_incrementally_registered_task(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            register_task,
            unregister_workflow,
            _task_to_workflow,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})
        register_task("wf-1", "task-2", 2)
        unregister_workflow("wf-1")

        assert "task-0" not in _task_to_workflow
        assert "task-2" not in _task_to_workflow


class TestPushStageProgress:
    """Task 16: the deterministic element stage has no CrewAI task, so it
    pushes its own SSE events (22/30/55/60) through the same forward-only
    bookkeeping the event handlers use."""

    def test_pushes_to_registered_workflow_queue(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            push_stage_progress,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})
        push_stage_progress("wf-1", "🔍 Scanning webpage for interactive elements...", 22)

        msg = q.get(timeout=1)
        assert msg == {
            "status": "running",
            "message": "🔍 Scanning webpage for interactive elements...",
            "progress": 22,
        }

    def test_forward_only(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            push_stage_progress,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})
        push_stage_progress("wf-1", "at 55", 55)
        q.get(timeout=1)
        push_stage_progress("wf-1", "stale 30", 30)

        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_stage_push_interleaves_with_task_ladder(self):
        """Stage pushes share _current_progress with the handlers: after the
        stage pushed 60, the assembler's task-started ladder must not
        re-emit the synthesized index-1 completion (60)."""
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            register_task,
            push_stage_progress,
            _on_task_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})
        register_task("wf-1", "task-2", 2)

        push_stage_progress("wf-1", "✅ All page elements identified", 60)
        assert q.get(timeout=1)["progress"] == 60

        event = MagicMock()
        event.task = MagicMock()
        event.task.id = "task-2"
        _on_task_started(source=None, event=event)

        # Only the 62 start — the 60 rung is deduped.
        assert q.get(timeout=1)["progress"] == 62
        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_unknown_workflow_is_noop(self):
        from src.backend.crew_ai.progress_events import push_stage_progress

        push_stage_progress("nonexistent-wf", "message", 30)  # must not raise


class TestToolHandlersRemoved:
    """Task 16 removed the ToolUsage handlers — the batch tool never fires
    CrewAI tool events anymore (it is called directly by the python stage),
    and no other tool ever produced messages."""

    def test_tool_handlers_are_gone(self):
        from src.backend.crew_ai import progress_events as pe

        assert not hasattr(pe, "_on_tool_started")
        assert not hasattr(pe, "_on_tool_finished")
        assert not hasattr(pe, "_tool_usage_seen")
        assert not hasattr(pe, "_tool_finished_seen")


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

        # Fire task 1 completed (60%) first. Not task 2: the assembler has no
        # completion entry — 80 belongs solely to the dryrun gate (see
        # TestCompletionSynthesis.test_assembler_completion_pushes_nothing).
        event_1_done = MagicMock()
        event_1_done.task = MagicMock()
        event_1_done.task.id = "task-1"
        _on_task_completed(source=None, event=event_1_done)

        msg = q.get(timeout=1)
        assert msg["progress"] == 60

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

        # First call — pushes the task-start ladder step (5), then the message
        _on_llm_call_started(source=None, event=event)
        ladder = q.get(timeout=1)
        assert ladder["progress"] == 5
        msg = q.get(timeout=1)
        assert "📋" in msg["message"]

        # Second call — silently skipped (ladder already at 8, message deduped)
        _on_llm_call_started(source=None, event=event)
        with pytest.raises(Empty):
            q.get(timeout=0.1)


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

        # Task 1 starting synthesizes task 0's completion first (see
        # TestCompletionSynthesis), then pushes its own start.
        msg_b_completion = q_b.get(timeout=1)
        assert msg_b_completion["progress"] == 20
        msg_b = q_b.get(timeout=1)
        assert msg_b["progress"] == 22  # Task 1 started (scanning elements)

        # Each queue should only have its own event(s)
        with pytest.raises(Empty):
            q_a.get(timeout=0.1)
        with pytest.raises(Empty):
            q_b.get(timeout=0.1)

    def test_stage_pushes_are_isolated_per_workflow(self):
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            push_stage_progress,
        )

        q_a = Queue()
        q_b = Queue()
        register_workflow("wf-a", q_a, {"task-a0": 0})
        register_workflow("wf-b", q_b, {"task-b0": 0})

        push_stage_progress("wf-a", "stage msg", 30)

        assert q_a.get(timeout=1)["progress"] == 30
        with pytest.raises(Empty):
            q_b.get(timeout=0.1)


class TestCompletionSynthesis:
    """Task-completion checkpoints are synthesized from the NEXT task's start.

    CrewAI's event bus runs sync handlers in a ThreadPoolExecutor, so the
    TaskCompletedEvent handler races the next task's TaskStartedEvent handler.
    When the start wins (observed: always in practice), the completion's lower
    progress (20 < 22, 60 < 62) is discarded forever by the forward-only
    guard — users never see the ✅ messages. Deriving the completion push from
    the next start (same handler → deterministic queue order) fixes 20/60;
    the assembler's 80 is pushed at the dryrun-gate entry (dryrun_service).

    Task 16 note: index 1 has no CrewAI task anymore (the python stage pushes
    22/30/55/60 itself), but the ladder still covers the crash/degraded case
    where the assembler task starts before the stage pushed 60.
    """

    def _fire_start(self, task_id):
        from src.backend.crew_ai.progress_events import _on_task_started
        event = MagicMock()
        event.task = MagicMock()
        event.task.id = task_id
        _on_task_started(source=None, event=event)

    def test_task1_start_emits_task0_completion_first(self):
        from src.backend.crew_ai.progress_events import register_workflow

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0, "task-1": 1})

        self._fire_start("task-1")

        completion = q.get(timeout=1)
        assert completion["progress"] == 20
        assert "planned successfully" in completion["message"]
        start = q.get(timeout=1)
        assert start["progress"] == 22

    def test_task2_start_emits_task1_completion_first(self):
        from src.backend.crew_ai.progress_events import register_workflow

        q = Queue()
        register_workflow("wf-1", q, {"task-1": 1, "task-2": 2})

        self._fire_start("task-2")

        completion = q.get(timeout=1)
        assert completion["progress"] == 60
        assert "elements identified" in completion["message"]
        start = q.get(timeout=1)
        assert start["progress"] == 62

    def test_assembler_completion_pushes_nothing(self):
        """80 has exactly ONE emitter: the dryrun gate (dryrun_service).

        The gate pushes 80 with a raw queue.put AFTER run_crew already called
        unregister_workflow(), so it bypasses the forward-only dedup here.
        If this handler ALSO carried an index-2 entry, a TaskCompletedEvent
        that won its race would emit the same '✅ Test code assembled' line a
        second time. Index 2 is the last task, so nothing synthesizes it via
        the ladder either — dropping the entry is what makes 80 unambiguous.
        """
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_completed,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-2": 2})

        event = MagicMock()
        event.task = MagicMock()
        event.task.id = "task-2"
        _on_task_completed(source=None, event=event)

        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_task0_start_synthesizes_nothing(self):
        from src.backend.crew_ai.progress_events import register_workflow

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0})

        self._fire_start("task-0")

        only = q.get(timeout=1)
        assert only["progress"] == 5
        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_late_real_completion_is_still_deduped(self):
        """The racy real TaskCompletedEvent arriving AFTER the synthesized push
        must be dropped by the forward-only guard (no duplicate ✅ message)."""
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_task_completed,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-0": 0, "task-1": 1})

        self._fire_start("task-1")  # synthesizes 20, then pushes 22
        q.get(timeout=1)
        q.get(timeout=1)

        late = MagicMock()
        late.task = MagicMock()
        late.task.id = "task-0"
        _on_task_completed(source=None, event=late)

        with pytest.raises(Empty):
            q.get(timeout=0.1)

    def test_llm_started_winning_the_race_still_emits_the_ladder(self):
        """Observed live: llm-started(65) can beat task-started(62) in the
        thread pool, silently killing 60 AND 62. Every handler must emit its
        task's ladder (prev completion → task start → own event) itself."""
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            _on_llm_call_started,
        )

        q = Queue()
        register_workflow("wf-1", q, {"task-1": 1, "task-2": 2})

        event = MagicMock()
        event.task_id = "task-2"
        _on_llm_call_started(source=None, event=event)

        assert q.get(timeout=1)["progress"] == 60
        assert q.get(timeout=1)["progress"] == 62
        assert q.get(timeout=1)["progress"] == 65
        with pytest.raises(Empty):
            q.get(timeout=0.1)
