"""
CrewAI task callbacks for task-level timing observability.

Supplements crewai.log.txt with a rotating log that timestamps every task
completion, so per-task latency can be measured by diffing consecutive entries.

The log carries task-level entries only. Per-LLM-call latency lives in
llm_traces.duration_ms — crewai 1.15's Flow AgentExecutor invokes step_callback
only from its native-tool-calling and tool-execution branches
(experimental/agent_executor.py:1544/1555/1566/1674), and both our agents are
tool-less, so a step callback can never fire (probe-verified 2026-08-01:
0 step firings, 1 task firing).

Log file: logs/crewai_steps.log
"""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

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


def make_task_callback(step_logger: logging.Logger):
    """Return a callback that logs task completion with elapsed time.

    Fires once per task after the final output is produced. Also emits an
    INFO entry to application.log so task timing is visible without opening
    the step log.

    state["last_ts"] is seeded to datetime.now() at callback creation so
    the first task's elapsed measures time from kickoff start to first
    task completion (i.e., the planner agent's actual duration).
    """
    state = {"last_ts": datetime.now()}

    def _callback(task_output):
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        elapsed_str = ""
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

    return _callback


def get_crew_callbacks():
    """Return (step_callback, task_callback) ready to pass to Crew().

    Single call site so crew.py doesn't need to know about the logger.

    step_callback is always None: the Flow AgentExecutor can only fire it from
    its tool-calling branches and both our agents are tool-less. The 2-tuple
    shape is kept so crew.py's unpack and the tests that patch this function
    stay unchanged; Crew(step_callback=None) is valid.
    """
    step_logger = _get_step_logger()
    return None, make_task_callback(step_logger)
