"""
CrewAI step and task callbacks for per-step timing observability.

Supplements crewai.log.txt (which only has task start/end entries) with a
rotating step log that timestamps every agent thought/action/observation cycle.
This lets you measure per-agent latency by diffing consecutive entries.

Log file: logs/crewai_steps.log
"""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

from src.backend.core.grafana_events import emit_event

logger = logging.getLogger(__name__)

# Agent role → dashboard stage name. Keys are matched as substrings of the
# TaskOutput.agent role string (see agents.py role= definitions).
# Only two LLM agents remain since Task 16 — the element-identification stage
# is deterministic now and never fires a task callback, so there is no
# "identifier" stage here.
_AGENT_STAGE_MAP = {
    "Planner": "planner",
    "Code Generator": "assembler",
}


def _stage_for_agent(agent_role: str) -> str:
    for needle, stage in _AGENT_STAGE_MAP.items():
        if needle in agent_role:
            return stage
    return "unknown"

CREWAI_STEP_LOG_FILE = "logs/crewai_steps.log"
CREWAI_STEP_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20MB per file
CREWAI_STEP_LOG_BACKUP_COUNT = 5               # 5 backups = 100MB max


def _get_step_logger() -> logging.Logger:
    """Return a dedicated rotating-file logger for step-level timing.

    Uses RotatingFileHandler so rotation happens on-the-fly during kickoff
    (not just at startup) and file writes are thread-safe. Propagation is
    disabled so step details don't flood application.log.
    """
    step_logger = logging.getLogger("crewai.steps")
    if step_logger.handlers:
        return step_logger  # Already configured for this process

    os.makedirs(os.path.dirname(CREWAI_STEP_LOG_FILE), exist_ok=True)
    handler = RotatingFileHandler(
        CREWAI_STEP_LOG_FILE,
        maxBytes=CREWAI_STEP_LOG_MAX_BYTES,
        backupCount=CREWAI_STEP_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    step_logger.addHandler(handler)
    step_logger.setLevel(logging.DEBUG)
    step_logger.propagate = False
    return step_logger


def make_step_callback(step_logger: logging.Logger):
    """Return a callback that logs a timestamped entry per agent step.

    Fires once per thought/action/observation cycle. Each entry includes
    elapsed time from the previous step so you can measure per-LLM-call
    latency by diffing consecutive lines.

    Handles both AgentAction-style objects (newer CrewAI) and plain strings
    (older versions) via graceful attribute access.
    """
    state = {"last_ts": datetime.now()}

    def _callback(step_output):
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        elapsed_str = ""
        if state["last_ts"] is not None:
            delta = (now - state["last_ts"]).total_seconds()
            elapsed_str = f", elapsed={delta:.1f}s"
        state["last_ts"] = now

        try:
            if hasattr(step_output, "tool"):
                agent = getattr(step_output, "agent", "unknown")
                tool = getattr(step_output, "tool", "")
                # 'thought' in newer CrewAI, 'log' in older versions
                thought_raw = getattr(step_output, "thought", getattr(step_output, "log", ""))
                thought = str(thought_raw)[:80]
                result = str(getattr(step_output, "result", ""))[:120]
                line = (
                    f"{ts}{elapsed_str}: agent={agent!r}, tool={tool!r}, "
                    f"thought={thought!r}, result={result!r}"
                )
            else:
                line = f"{ts}{elapsed_str}: [step] {str(step_output)[:200]}"
        except Exception:
            line = f"{ts}{elapsed_str}: [step_callback] Could not parse step output"

        step_logger.info(line)

    return _callback


def make_task_callback(step_logger: logging.Logger, llm=None):
    """Return a callback that logs task completion with elapsed time.

    Fires once per task after the final output is produced. Also emits an
    INFO entry to application.log so task timing is visible without opening
    the step log.

    state["last_ts"] is seeded to datetime.now() at callback creation so
    the first task's elapsed measures time from kickoff start to first
    task completion (i.e., the planner agent's actual duration).

    Since Task 16 the two crew kickoffs are NOT contiguous: the deterministic
    element stage (one batch browser-automation call, often tens of seconds)
    runs between them. Measuring plain wall time between callbacks would bill
    that entirely to the assembler. crew.py therefore calls
    `task_callback.mark_stage_start()` immediately before the assembler kickoff
    to restart the clock, so duration_s stays agent time.

    llm: the workflow's shared CleanedLLMWrapper. Its per-stage usage
    accumulator is drained at every task boundary — sequential execution means
    everything accumulated since the previous task completed belongs to this
    task — giving real per-agent tokens AND cost on task_completed events
    (TaskOutput.token_usage is not populated in this CrewAI version).
    Token attribution is unaffected by mark_stage_start: the element stage
    makes no LLM calls through this wrapper, so nothing is discarded.
    """
    state = {"last_ts": datetime.now()}

    def _callback(task_output):
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        elapsed_str = ""
        delta = None
        if state["last_ts"] is not None:
            delta = (now - state["last_ts"]).total_seconds()
            elapsed_str = f", elapsed={delta:.1f}s"
        state["last_ts"] = now

        try:
            desc = str(getattr(task_output, "description", task_output))[:80]
            agent = getattr(task_output, "agent", "unknown")
            line = f"{ts}: [TASK DONE] agent={agent!r}{elapsed_str}, task={desc!r}"
        except Exception:
            line = f"{ts}: [TASK DONE]{elapsed_str}"

        step_logger.info(line)
        # Surface task timing in application.log for quick cross-log correlation
        logger.info(f"[TASK DONE] {ts}{elapsed_str}: {getattr(task_output, 'description', '')[:60]!r}")

        # Grafana per-stage panels (duration/tokens/cost by stage). Primary
        # source: the shared wrapper's usage accumulator (real per-call token
        # diffs + LiteLLM pricing). Fallback: TaskOutput.token_usage, which is
        # not populated in this CrewAI version (arrives as 0s).
        try:
            agent_role = str(getattr(task_output, "agent", "unknown"))
            stage_usage = llm.pop_stage_usage() if llm is not None else {}
            fallback = getattr(task_output, "token_usage", None)
            emit_event(
                "task_completed",
                stage=_stage_for_agent(agent_role),
                agent=agent_role,
                duration_s=round(delta, 2) if delta is not None else 0.0,
                output_len=len(str(getattr(task_output, "raw", ""))),
                tokens=stage_usage.get("tokens") or int(getattr(fallback, "total_tokens", 0) or 0),
                prompt_tokens=stage_usage.get("prompt_tokens") or int(getattr(fallback, "prompt_tokens", 0) or 0),
                completion_tokens=stage_usage.get("completion_tokens") or int(getattr(fallback, "completion_tokens", 0) or 0),
                llm_calls=stage_usage.get("llm_calls", 0),
                cost=stage_usage.get("cost", 0.0),
            )
        except Exception:
            logger.debug("task_completed event emission failed", exc_info=True)

    def mark_stage_start():
        """Restart the duration clock. Call right before a crew kickoff that is
        NOT contiguous with the previous task callback, so wall time spent
        between stages is not billed to the next agent."""
        state["last_ts"] = datetime.now()

    _callback.mark_stage_start = mark_stage_start
    return _callback


def get_crew_callbacks(llm=None):
    """Return (step_callback, task_callback) ready to pass to Crew().

    Single call site so crew.py doesn't need to know about the logger.
    Pass the workflow's shared CleanedLLMWrapper as llm to enable per-agent
    token/cost attribution on task_completed events.
    """
    step_logger = _get_step_logger()
    return make_step_callback(step_logger), make_task_callback(step_logger, llm=llm)
