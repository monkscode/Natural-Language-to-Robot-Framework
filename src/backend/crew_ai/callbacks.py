"""
CrewAI step and task callbacks for per-step timing observability.

Supplements crewai.log.txt (which only has task start/end entries) with a
rotating step log that timestamps every agent thought/action/observation cycle.
This lets you measure per-agent latency by diffing consecutive entries.

Log file: logs/crewai_steps.log
"""

import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

CREWAI_STEP_LOG_FILE = "logs/crewai_steps.log"
CREWAI_STEP_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20MB per file
CREWAI_STEP_LOG_BACKUP_COUNT = 5               # 5 backups = 100MB max

# The stage a task callback is attributed to before crew.py says otherwise.
# run_crew's first kickoff is always the planner.
_FIRST_STAGE = "planner"


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


class StageMetricsCallback:
    """Task callback: logs task completion and records per-stage metrics.

    Fires once per task after the final output is produced, doing two jobs:

    1. The timing log line (step log + an application.log entry, so task
       timing is visible without opening the step log).
    2. Per-stage duration and LLM usage, drained from the shared wrapper's
       accumulator at the task boundary.

    The stage is DECLARED by crew.py via mark_stage(), not inferred from
    TaskOutput.agent. crew.py builds each single-task crew and therefore already
    knows which kickoff is running; reading the stage back out of a role string
    crewai copied from the agent was a round trip through data we control at the
    source. It also had a silent failure mode: a role the lookup table did not
    recognise drained the accumulator and then discarded the usage, breaking the
    invariant that the per-stage figures sum to the workflow total (pinned by
    test_stages_sum_to_the_workflow_total). No stage can be unknown now.

    Nothing here may raise. crewai invokes this inside kickoff(), so an
    exception would abort a generation run for the sake of a metric; both
    halves are therefore independently guarded.
    """

    def __init__(self, step_logger: logging.Logger, llm=None):
        self._step_logger = step_logger
        self._llm = llm
        # Seeded at construction so the first task's elapsed measures from
        # kickoff start to first task completion (the planner's real duration).
        self._last_ts = datetime.now()
        self._stage_started = time.monotonic()
        self._stage = _FIRST_STAGE
        self.stage_metrics: dict[str, dict] = {}

    def mark_stage(self, stage: str) -> None:
        """Name the stage the next task callback belongs to, and restart its clock.

        Called before the assembler kickoff: the deterministic element stage
        runs between the two kickoffs and is neither agent's time.
        """
        self._stage = stage
        self._stage_started = time.monotonic()

    def __call__(self, task_output) -> None:
        try:
            self._log(task_output)
        except Exception:
            logger.debug("[TASK DONE] logging failed", exc_info=True)
        try:
            self._record()
        except Exception:
            logger.debug("stage metrics collection failed", exc_info=True)

    def _log(self, task_output) -> None:
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        elapsed_str = ""
        if self._last_ts is not None:
            delta = (now - self._last_ts).total_seconds()
            elapsed_str = f", elapsed={delta:.1f}s"
        self._last_ts = now

        try:
            desc = str(getattr(task_output, "description", task_output))[:80]
            agent = getattr(task_output, "agent", "unknown")
            line = f"{ts}: [TASK DONE] agent={agent!r}{elapsed_str}, task={desc!r}"
        except Exception:
            line = f"{ts}: [TASK DONE]{elapsed_str}"

        self._step_logger.info(line)
        # Surface task timing in application.log for quick cross-log correlation
        # str() before the slice, matching the line above: this one sits
        # OUTSIDE that try/except, so a non-str description would raise
        # TypeError straight out of a callback that runs on every task.
        logger.info(f"[TASK DONE] {ts}{elapsed_str}: {str(getattr(task_output, 'description', ''))[:60]!r}")

    def _record(self) -> None:
        duration_s = round(time.monotonic() - self._stage_started, 3)
        self._stage_started = time.monotonic()
        if self._llm is None:
            return

        # pop_stage_usage() moves a watermark, so the drain and the record must
        # stay together: everything accrued since the last drain belongs to the
        # stage that just finished.
        usage = self._llm.pop_stage_usage()
        self.stage_metrics[self._stage] = {"duration_s": duration_s, **usage}


def get_crew_callbacks(llm=None):
    """Return (step_callback, task_callback) ready to pass to Crew().

    Single call site so crew.py doesn't need to know about the logger. Pass the
    shared LLM wrapper to collect per-stage metrics; omit it and the task
    callback logs only.
    """
    step_logger = _get_step_logger()
    return make_step_callback(step_logger), StageMetricsCallback(step_logger, llm=llm)
