"""
CrewAI task callbacks for task-level timing observability.

Supplements crewai.log.txt with a rotating log that timestamps every task
completion, so per-task latency can be measured by diffing consecutive entries.

The log carries task-level entries only. Per-LLM-call latency lives in
llm_traces.duration_ms. crewai 1.15's Flow AgentExecutor invokes step_callback
only from its native-tool-calling and tool-execution branches
(crewai/agents/agent_executor.py: call_llm_native_tools, execute_tool_action),
and both our agents are tool-less, so a step callback can never fire (measured
on 1.15.44: 0 step firings, 1 task firing).

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

    step_callback is always None: the Flow AgentExecutor can only fire it from
    its tool-calling branches and both our agents are tool-less. The 2-tuple
    shape is kept so crew.py's unpack and the tests that patch this function
    stay unchanged; Crew(step_callback=None) is valid.
    """
    step_logger = _get_step_logger()
    return None, StageMetricsCallback(step_logger, llm=llm)
