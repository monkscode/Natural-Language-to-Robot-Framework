"""
Unit tests for the per-stage metrics task callback in src.backend.crew_ai.callbacks.

Purpose: verify that the task callback attributes LLM usage to the right crew
         stage, that mark_stage() keeps the deterministic element stage off the
         assembler's clock, and — most importantly — that a metrics failure can
         never abort a generation run.

The stage is declared by crew.py, which knows which kickoff it is running; it is
NOT read back off TaskOutput.agent. That means no agent rename can silently
misattribute or discard a stage's tokens.

The callback fires inside crew.kickoff(). Anything it raises propagates into
the pipeline, so "never raises" is a correctness requirement, not politeness.
"""

from unittest.mock import Mock

import pytest


PLANNER_ROLE = "Test Automation Planner"
ASSEMBLER_ROLE = "Robot Framework Code Generator"


def _task_output(role: str = PLANNER_ROLE):
    """Minimal stand-in for crewai's TaskOutput. `agent` is a plain str.

    The role only reaches the log line now — stage attribution does not read it.
    """
    out = Mock()
    out.agent = role
    out.description = "some task description"
    return out


def _llm_returning(**usage):
    """A fake shared LLM wrapper whose pop_stage_usage() returns `usage`."""
    llm = Mock()
    llm.pop_stage_usage.return_value = {
        "llm_calls": usage.get("llm_calls", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "tokens": usage.get("tokens", 0),
        "cost": usage.get("cost", 0.0),
    }
    return llm


class TestStageAttribution:
    """Usage lands under the stage crew.py declared, not one inferred from a role."""

    def test_first_task_is_the_planner_stage(self):
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        llm = _llm_returning(llm_calls=1, prompt_tokens=1000,
                             completion_tokens=200, tokens=1200, cost=0.0008)
        cb = StageMetricsCallback(step_logger=Mock(), llm=llm)

        cb(_task_output())

        assert "planner" in cb.stage_metrics
        stage = cb.stage_metrics["planner"]
        assert stage["llm_calls"] == 1
        assert stage["prompt_tokens"] == 1000
        assert stage["completion_tokens"] == 200
        assert stage["tokens"] == 1200
        assert stage["cost"] == 0.0008
        assert "duration_s" in stage

    def test_mark_stage_switches_attribution(self):
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        llm = _llm_returning(llm_calls=2, tokens=5300)
        cb = StageMetricsCallback(step_logger=Mock(), llm=llm)

        cb.mark_stage("assembler")
        cb(_task_output())

        assert "assembler" in cb.stage_metrics
        assert cb.stage_metrics["assembler"]["tokens"] == 5300

    def test_the_agent_role_does_not_decide_the_stage(self):
        """The regression this design exists to prevent.

        The role used to be looked up in a map, and a role the map did not know
        drained the accumulator and then DISCARDED the usage — silently breaking
        the invariant that the stages sum to the workflow total. Renaming an
        agent must now be incapable of losing a token.
        """
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        llm = _llm_returning(tokens=99)
        cb = StageMetricsCallback(step_logger=Mock(), llm=llm)

        cb(_task_output("Some Agent Nobody Mapped"))

        assert cb.stage_metrics["planner"]["tokens"] == 99
        llm.pop_stage_usage.assert_called_once()

    def test_no_output_len_key(self):
        """output_len was dropped — it measured nothing anyone reads."""
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        cb = StageMetricsCallback(step_logger=Mock(), llm=_llm_returning())
        cb(_task_output())

        assert "output_len" not in cb.stage_metrics["planner"]


class TestMarkStage:
    """The deterministic element stage runs between the two kickoffs and must
    not be billed to the assembler."""

    def test_mark_stage_resets_the_clock(self):
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        cb = StageMetricsCallback(step_logger=Mock(), llm=_llm_returning())
        cb(_task_output())

        # Simulate the element stage burning wall time, then re-marking.
        cb._stage_started -= 100.0  # 100s ago
        cb.mark_stage("assembler")
        cb(_task_output())

        assert cb.stage_metrics["assembler"]["duration_s"] < 5.0

    def test_without_mark_the_gap_is_billed(self):
        """Control for the test above: without mark_stage() the elapsed time
        really does accumulate, so the assertion above is meaningful."""
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        cb = StageMetricsCallback(step_logger=Mock(), llm=_llm_returning())
        cb(_task_output())

        cb._stage_started -= 100.0
        cb(_task_output())

        assert cb.stage_metrics["planner"]["duration_s"] >= 100.0


class TestNeverRaises:
    """A metrics failure must never cost a generation run."""

    def test_llm_raising_does_not_propagate(self):
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        llm = Mock()
        llm.pop_stage_usage.side_effect = RuntimeError("accumulator exploded")
        cb = StageMetricsCallback(step_logger=Mock(), llm=llm)

        cb(_task_output(PLANNER_ROLE))  # must not raise

    def test_missing_llm_does_not_propagate(self):
        """llm=None is legal — the callback then logs only."""
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        cb = StageMetricsCallback(step_logger=Mock(), llm=None)

        cb(_task_output(PLANNER_ROLE))  # must not raise
        assert cb.stage_metrics == {}

    def test_unparseable_task_output_does_not_propagate(self):
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        class Hostile:
            @property
            def agent(self):
                raise ValueError("no agent for you")

        cb = StageMetricsCallback(step_logger=Mock(), llm=_llm_returning())

        cb(Hostile())  # must not raise

    def test_step_logger_failure_does_not_propagate(self):
        from src.backend.crew_ai.callbacks import StageMetricsCallback

        step_logger = Mock()
        step_logger.info.side_effect = OSError("disk full")
        cb = StageMetricsCallback(step_logger=step_logger, llm=_llm_returning())

        cb(_task_output(PLANNER_ROLE))  # must not raise


class TestGetCrewCallbacks:
    """crew.py's single wiring point."""

    def test_returns_step_callback_and_stage_collector(self):
        from src.backend.crew_ai.callbacks import (
            StageMetricsCallback,
            get_crew_callbacks,
        )

        llm = _llm_returning()
        step_callback, task_callback = get_crew_callbacks(llm=llm)

        assert callable(step_callback)
        assert isinstance(task_callback, StageMetricsCallback)
        assert task_callback.stage_metrics == {}

    def test_llm_is_optional(self):
        """Existing call sites pass nothing; they must keep working."""
        from src.backend.crew_ai.callbacks import get_crew_callbacks

        step_callback, task_callback = get_crew_callbacks()

        assert callable(step_callback)
        assert callable(task_callback)
