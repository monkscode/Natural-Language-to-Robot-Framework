"""Real-time progress event bridge between CrewAI's event bus and the SSE queue.

Registers 4 handlers on crewai_event_bus at module import time (once per process).
Handlers are no-ops when no workflow is registered for the event's task_id, so
they carry zero cost when idle.

Per-workflow routing is managed via module-level dicts guarded by a single Lock.
All dict operations are O(1) — lock hold time is microseconds.

Referenced by: src/backend/crew_ai/crew.py (register_workflow, register_task,
unregister_workflow, push_stage_progress)
Depends on: crewai.events.crewai_event_bus, queue.Queue

Design decisions:
- Handlers registered ONCE at module import — crewai_event_bus has no remove_handler().
  Per-workflow routing uses dict lookups instead of per-workflow handler registration.
- Task events carry event.task (Task object), NOT event.task_id (which stays None for
  task events). LLM events carry event.task_id (set from from_task arg).
- Progress only moves forward. Out-of-order or retry events are silently discarded.
- Only the first LLMCallStartedEvent per task produces a message (suppresses retries).
- Every handler body is wrapped in try/except — a handler crash must never propagate
  into CrewAI's pipeline.
- Task 16: the element-identification stage is deterministic Python (no CrewAI task,
  no tool events) — it pushes its own index-1 events (22/30/55/60) via
  push_stage_progress, and the assembler task (built after the stage, since its
  description embeds the merged steps) is registered late via register_task. The
  old ToolUsage handlers were removed with the element-identifier agent: the batch
  tool is now called directly, so those events never fire.
"""

import logging
import threading
from queue import Queue

from crewai.events import crewai_event_bus
from crewai.events.types.task_events import (
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
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

# ---------------------------------------------------------------------------
# User-facing message tables — no internal names, no technical details
# ---------------------------------------------------------------------------

# The pipeline keeps its 3 stage indices (planner=0, element stage=1,
# assembler=2) even though only 0 and 2 are CrewAI tasks — index 1 is the
# deterministic python stage (element_identification), which pushes its own
# 22/30/55/60 events via push_stage_progress. The index-1 entries below feed
# the ladder (_push_task_ladder) so the assembler's start still synthesizes
# "elements identified" if the stage's own 60 push was lost. The old
# validator (index 3) was replaced by the dryrun gate, which drives its own
# verify/repair/100% progress via direct queue.put from workflow_service.
_TASK_STARTED_MESSAGES: dict[int, tuple[str, int]] = {
    0: ("🧠 Analyzing your test requirements...", 5),
    1: ("🔍 Scanning webpage for interactive elements...", 22),
    2: ("⚡ Writing test automation code...", 62),
}

_LLM_STARTED_MESSAGES: dict[int, tuple[str, int]] = {
    0: ("📋 Breaking down test into steps...", 8),
    # index 1 is the deterministic element stage — no LLM calls happen there
    2: ("💻 Generating test script...", 65),
}

# Index 2 (the assembler) has NO entry on purpose: 80 is pushed by the dryrun
# gate (dryrun_service.validate_and_repair) with a raw queue.put, which runs
# after unregister_workflow() and so bypasses the forward-only dedup below.
# Keeping an entry here would let a TaskCompletedEvent that won its race emit
# the same line a second time. Index 2 is the last task, so the ladder never
# synthesizes it either — one emitter, no duplicates.
_TASK_COMPLETED_MESSAGES: dict[int, tuple[str, int]] = {
    0: ("✅ Test steps planned successfully", 20),
    1: ("✅ All page elements identified", 60),
}

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
    logger.debug("Registered workflow %s with %d tasks", workflow_id, len(task_id_map))


def register_task(workflow_id: str, task_id: str, task_index: int) -> None:
    """Add one task to an already-registered workflow.

    Task 16: the assembler task is constructed AFTER the deterministic element
    stage (its description embeds the merged steps), so it cannot be part of
    the initial register_workflow call. No-op when the workflow is not
    registered (progress_queue=None path or already unregistered).
    """
    with _lock:
        if workflow_id not in _workflow_queues:
            return
        _workflow_task_map[workflow_id][task_id] = task_index
        _task_to_workflow[task_id] = workflow_id
    logger.debug("Registered task %s (index %d) for workflow %s", task_id, task_index, workflow_id)


def push_stage_progress(workflow_id: str, message: str, progress: int) -> None:
    """Push a progress event from a non-CrewAI pipeline stage.

    Task 16: the deterministic element stage has no CrewAI task, so it pushes
    its own SSE events through the same forward-only bookkeeping the event
    handlers use (a later task-ladder push then dedups cleanly against these).
    No-op for unregistered workflows.
    """
    with _lock:
        queue = _workflow_queues.get(workflow_id)
    if queue is None:
        return
    _push_if_forward(workflow_id, queue, message, progress)


def unregister_workflow(workflow_id: str) -> None:
    """Remove all mappings for a completed (or failed) workflow."""
    with _lock:
        task_map = _workflow_task_map.pop(workflow_id, {})
        for task_id in task_map:
            _task_to_workflow.pop(task_id, None)
        _workflow_queues.pop(workflow_id, None)
        _current_progress.pop(workflow_id, None)
        _llm_call_seen.pop(workflow_id, None)
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


def _push_task_ladder(workflow_id: str, queue: Queue, task_index: int) -> None:
    """Push the checkpoints implied by ANY event of this task: the previous
    task's completion, then this task's start.

    The bus runs sync handlers in a ThreadPoolExecutor, so handlers for
    in-order events race each other; whichever loses is discarded forever by
    the forward-only guard (observed live: completions 20/60/80 always lost,
    task-2's start 62 sometimes lost to its own llm-started 65). Every handler
    calls this before pushing its own event, so the winner of any race emits
    the missing rungs itself — _push_if_forward dedups the repeats.
    """
    prev_entry = _TASK_COMPLETED_MESSAGES.get(task_index - 1)
    if prev_entry is not None:
        _push_if_forward(workflow_id, queue, prev_entry[0], prev_entry[1])
    start_entry = _TASK_STARTED_MESSAGES.get(task_index)
    if start_entry is not None:
        _push_if_forward(workflow_id, queue, start_entry[0], start_entry[1])


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

        # The ladder covers the previous task's completion AND this start; the
        # real (late) TaskCompletedEvent handler stays registered as a no-op
        # dedup. See _push_task_ladder for the race this defeats.
        _push_task_ladder(workflow_id, queue, task_index)
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

        # Emit any rungs this handler may have outraced (see _push_task_ladder).
        _push_task_ladder(workflow_id, queue, task_index)

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


# NOTE: the ToolUsageStartedEvent / ToolUsageFinishedEvent handlers were
# removed in Task 16. They only ever produced messages for
# batch_browser_automation, and that tool is no longer invoked through a
# CrewAI agent — the deterministic element stage calls it directly and pushes
# the equivalent 30/55 events via push_stage_progress.
