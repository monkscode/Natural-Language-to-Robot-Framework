"""Real-time progress event bridge between CrewAI's event bus and the SSE queue.

Registers 6 handlers on crewai_event_bus at module import time (once per process).
Handlers are no-ops when no workflow is registered for the event's task_id, so
they carry zero cost when idle.

Per-workflow routing is managed via module-level dicts guarded by a single Lock.
All dict operations are O(1) — lock hold time is microseconds.

Referenced by: src/backend/crew_ai/crew.py (register_workflow, unregister_workflow)
Depends on: crewai.events.crewai_event_bus, queue.Queue

Design decisions:
- Handlers registered ONCE at module import — crewai_event_bus has no remove_handler().
  Per-workflow routing uses dict lookups instead of per-workflow handler registration.
- Task events carry event.task (Task object), NOT event.task_id (which stays None for
  task events). Tool and LLM events carry event.task_id (set from from_task arg).
- Progress only moves forward. Out-of-order or retry events are silently discarded.
- Only the first LLMCallStartedEvent per task produces a message (suppresses retries).
- Only the first ToolUsageStartedEvent and ToolUsageFinishedEvent per task produce
  messages (suppresses multi-call tool retries).
- Every handler body is wrapped in try/except — a handler crash must never propagate
  into CrewAI's pipeline.
- ToolUsageFinishedEvent.output is a string (str(dict)) in the text-based tool path
  (tool_usage.py calls _format_result → str() before emitting). Element count
  extraction uses ast.literal_eval to parse the stringified dict.
"""

import ast
import logging
import threading
from queue import Queue

from crewai.events import crewai_event_bus
from crewai.events.types.task_events import (
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
)
from crewai.events.types.tool_usage_events import (
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)
from crewai.events.types.llm_events import LLMCallStartedEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level routing state — all access guarded by _lock
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# str(task.id) → workflow_id
_task_to_workflow: dict[str, str] = {}

# workflow_id → SSE Queue
_workflow_queues: dict[str, Queue] = {}

# workflow_id → {str(task.id): task_index (0–2)}
_workflow_task_map: dict[str, dict[str, int]] = {}

# workflow_id → last progress value pushed (progress only moves forward)
_current_progress: dict[str, int] = {}

# workflow_id → set of task indices that have already shown an LLM call message
_llm_call_seen: dict[str, set[int]] = {}

# workflow_id → set of task indices that have already shown a tool-started message
_tool_usage_seen: dict[str, set[int]] = {}

# workflow_id → set of task indices that have already shown a tool-finished message
_tool_finished_seen: dict[str, set[int]] = {}

# ---------------------------------------------------------------------------
# User-facing message tables — no internal names, no technical details
# ---------------------------------------------------------------------------

# Crew is a 3-task pipeline (planner=0, identifier=1, assembler=2). The old
# validator (index 3) was replaced by the dryrun gate, which drives its own
# verify/repair/100% progress via direct queue.put from workflow_service.
_TASK_STARTED_MESSAGES: dict[int, tuple[str, int]] = {
    0: ("🧠 Analyzing your test requirements...", 5),
    1: ("🔍 Scanning webpage for interactive elements...", 22),
    2: ("⚡ Writing test automation code...", 62),
}

_LLM_STARTED_MESSAGES: dict[int, tuple[str, int]] = {
    0: ("📋 Breaking down test into steps...", 8),
    # task 1 (Element Identifier) uses a tool — LLM message skipped in favour of tool messages
    2: ("💻 Generating test script...", 65),
}

_TASK_COMPLETED_MESSAGES: dict[int, tuple[str, int]] = {
    0: ("✅ Test steps planned successfully", 20),
    1: ("✅ All page elements identified", 60),
    2: ("✅ Test code assembled", 80),
}

# Only this tool name produces user-facing messages
_BROWSER_TOOL_NAME = "batch_browser_automation"

# ---------------------------------------------------------------------------
# Public API — called from crew.py around crew.kickoff()
# ---------------------------------------------------------------------------


def register_workflow(
    workflow_id: str,
    queue: Queue,
    task_id_map: dict[str, int],
) -> None:
    """Register a workflow's tasks for event routing.

    Args:
        workflow_id: Unique workflow identifier.
        queue: The SSE queue to push progress events to.
        task_id_map: Mapping of str(task.id) → task_index (0–2).
    """
    with _lock:
        _workflow_queues[workflow_id] = queue
        _workflow_task_map[workflow_id] = task_id_map
        for task_id in task_id_map:
            _task_to_workflow[task_id] = workflow_id
        _current_progress[workflow_id] = 0
        _llm_call_seen[workflow_id] = set()
        _tool_usage_seen[workflow_id] = set()
        _tool_finished_seen[workflow_id] = set()
    logger.debug("Registered workflow %s with %d tasks", workflow_id, len(task_id_map))


def unregister_workflow(workflow_id: str) -> None:
    """Remove all mappings for a completed (or failed) workflow."""
    with _lock:
        task_map = _workflow_task_map.pop(workflow_id, {})
        for task_id in task_map:
            _task_to_workflow.pop(task_id, None)
        _workflow_queues.pop(workflow_id, None)
        _current_progress.pop(workflow_id, None)
        _llm_call_seen.pop(workflow_id, None)
        _tool_usage_seen.pop(workflow_id, None)
        _tool_finished_seen.pop(workflow_id, None)
    logger.debug("Unregistered workflow %s", workflow_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve(task_id: str | None) -> tuple[str | None, Queue | None, int | None]:
    """Look up (workflow_id, queue, task_index) for a given task_id.

    Returns (None, None, None) when the task_id is not registered.
    Called under no lock — acquires _lock internally.
    """
    if not task_id:
        return None, None, None
    with _lock:
        workflow_id = _task_to_workflow.get(task_id)
        if workflow_id is None:
            return None, None, None
        queue = _workflow_queues.get(workflow_id)
        task_index = _workflow_task_map.get(workflow_id, {}).get(task_id)
        return workflow_id, queue, task_index


def _push_if_forward(
    workflow_id: str,
    queue: Queue,
    message: str,
    progress: int,
) -> None:
    """Push a progress event only if it advances the current progress value.

    Thread-safe. Discards the event silently if progress would not increase.
    Also ignores events for workflows whose queue has been unregistered — this
    prevents a late async TaskCompletedEvent handler from enqueuing a duplicate
    100% event after crew.py's manual push + unregister_workflow() have run,
    and from re-creating the _current_progress entry (memory leak).
    """
    with _lock:
        if _workflow_queues.get(workflow_id) is not queue:
            return
        current = _current_progress.get(workflow_id, 0)
        if progress <= current:
            return
        _current_progress[workflow_id] = progress

    queue.put({"status": "running", "message": message, "progress": progress})


# ---------------------------------------------------------------------------
# Event handlers — registered once at module import, persist for process lifetime
# ---------------------------------------------------------------------------


@crewai_event_bus.on(TaskStartedEvent)
def _on_task_started(source, event: TaskStartedEvent) -> None:
    """Route TaskStartedEvent to the correct workflow queue.

    NOTE: TaskStartedEvent carries event.task (the Task object), not event.task_id.
    event.task_id is None for task events — must use str(event.task.id) instead.
    """
    try:
        if event.task is None:
            return
        task_id = str(event.task.id)
        workflow_id, queue, task_index = _resolve(task_id)
        if queue is None or task_index is None:
            return

        entry = _TASK_STARTED_MESSAGES.get(task_index)
        if entry is None:
            return
        message, progress = entry
        _push_if_forward(workflow_id, queue, message, progress)
    except Exception:
        logger.exception("Error in _on_task_started handler")


@crewai_event_bus.on(TaskCompletedEvent)
def _on_task_completed(source, event: TaskCompletedEvent) -> None:
    """Route TaskCompletedEvent to the correct workflow queue."""
    try:
        if event.task is None:
            return
        task_id = str(event.task.id)
        workflow_id, queue, task_index = _resolve(task_id)
        if queue is None or task_index is None:
            return

        entry = _TASK_COMPLETED_MESSAGES.get(task_index)
        if entry is None:
            return
        message, progress = entry
        _push_if_forward(workflow_id, queue, message, progress)
    except Exception:
        logger.exception("Error in _on_task_completed handler")


@crewai_event_bus.on(TaskFailedEvent)
def _on_task_failed(source, event: TaskFailedEvent) -> None:
    """Push a generic retry message on task failure. Real error is logged server-side."""
    try:
        if event.task is None:
            return
        task_id = str(event.task.id)
        workflow_id, queue, task_index = _resolve(task_id)
        if queue is None or task_index is None:
            return

        logger.warning(
            "Task %s failed for workflow %s: %s",
            task_index,
            workflow_id,
            event.error,
        )
        # Do not advance progress on failure — stay at current value
        with _lock:
            current = _current_progress.get(workflow_id, 0)
        queue.put({
            "status": "running",
            "message": "⚠️ Encountered an issue, retrying...",
            "progress": current,
        })
    except Exception:
        logger.exception("Error in _on_task_failed handler")


@crewai_event_bus.on(LLMCallStartedEvent)
def _on_llm_call_started(source, event: LLMCallStartedEvent) -> None:
    """Push a 'thinking' message on first LLM call per task. Suppresses retries."""
    try:
        # LLM events have event.task_id set (from from_task arg in LLMEventBase.__init__)
        workflow_id, queue, task_index = _resolve(event.task_id)
        if queue is None or task_index is None:
            return

        # Deduplicate: only first LLM call per task shows a message
        with _lock:
            seen = _llm_call_seen.get(workflow_id)
            if seen is None:
                return
            if task_index in seen:
                return
            seen.add(task_index)

        entry = _LLM_STARTED_MESSAGES.get(task_index)
        if entry is None:
            return
        message, progress = entry
        _push_if_forward(workflow_id, queue, message, progress)
    except Exception:
        logger.exception("Error in _on_llm_call_started handler")


@crewai_event_bus.on(ToolUsageStartedEvent)
def _on_tool_started(source, event: ToolUsageStartedEvent) -> None:
    """Push a navigation message when the browser tool starts. Ignores other tools."""
    try:
        if event.tool_name != _BROWSER_TOOL_NAME:
            return

        # Tool events have event.task_id set (from from_task arg in ToolUsageEvent.__init__)
        workflow_id, queue, task_index = _resolve(event.task_id)
        if queue is None or task_index is None:
            return

        # Only first tool invocation per task produces a message
        with _lock:
            seen = _tool_usage_seen.get(workflow_id)
            if seen is None:
                return
            if task_index in seen:
                return
            seen.add(task_index)

        _push_if_forward(workflow_id, queue, "🌐 Navigating to website and detecting elements...", 30)
    except Exception:
        logger.exception("Error in _on_tool_started handler")


@crewai_event_bus.on(ToolUsageFinishedEvent)
def _on_tool_finished(source, event: ToolUsageFinishedEvent) -> None:
    """Push element count when the browser tool finishes. Ignores other tools."""
    try:
        if event.tool_name != _BROWSER_TOOL_NAME:
            return

        workflow_id, queue, task_index = _resolve(event.task_id)
        if queue is None or task_index is None:
            return

        # Only first finished event per task produces a message
        with _lock:
            seen = _tool_finished_seen.get(workflow_id)
            if seen is None:
                return
            if task_index in seen:
                return
            seen.add(task_index)

        # Extract element count from tool output.
        # In the text-based tool path (our pipeline), CrewAI's tool_usage.py calls
        # _format_result() → str(result) BEFORE emitting ToolUsageFinishedEvent,
        # so event.output is a string representation of the dict, not a dict.
        # We parse it back to extract the element count.
        count: int | None = None
        output = event.output

        # Text-based path: output is str(dict), parse it back
        if isinstance(output, str):
            try:
                output = ast.literal_eval(output)
            except (ValueError, SyntaxError):
                pass

        if isinstance(output, dict):
            summary = output.get("summary", {})
            if isinstance(summary, dict):
                count = summary.get("total_elements")

        message = (
            f"📍 Found {count} elements on the page"
            if count is not None
            else "📍 Page elements detected"
        )
        _push_if_forward(workflow_id, queue, message, 55)
    except Exception:
        logger.exception("Error in _on_tool_finished handler")
