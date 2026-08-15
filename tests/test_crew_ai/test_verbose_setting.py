"""
Unit tests for the CREWAI_VERBOSE setting in src.backend.core.config.

Purpose: CrewAI's verbose mode echoes the whole prompt to stdout inside
         box-drawing frames. Measured 2026-08-14, that echo was 30,981 of the
         42,045 lines the fastapi container shipped to Loki over five days —
         73.7% of the stream — and none of it carries a workflow_id, so it
         cannot be filtered to a run. The same prompts are already captured
         in llm_traces.prompt_text on 100% of the 2,002 model-call spans.

         CLAUDE.md documented `CREWAI_VERBOSE=true` as the debug lever long
         before the setting existed; run.sh:92 still carries it as a
         commented-out export that sets nothing. These tests make the lever
         real and keep it off by default.

Tests:
  - The setting exists and defaults to off
  - Both agents and both crews read it rather than hardcoding True
"""

from unittest.mock import MagicMock, patch

import pytest


class TestCrewaiVerboseSetting:

    def test_setting_exists_and_defaults_off(self):
        """Verbose is opt-in: the default must not re-flood the log stream."""
        from src.backend.core.config import Settings

        field = Settings.model_fields.get("CREWAI_VERBOSE")
        assert field is not None, (
            "CREWAI_VERBOSE is not a setting; CLAUDE.md and run.sh:92 both "
            "name it as the debug lever, so it must exist rather than being "
            "a comment that sets nothing")
        assert field.default is False, (
            f"CREWAI_VERBOSE defaults to {field.default!r}; it must default "
            f"to False or every run echoes the full prompt to the log stream")


class TestAgentsHonourTheSetting:
    """Both agents must read the setting, not hardcode verbose=True."""

    def _agents(self):
        from src.backend.crew_ai.agents import RobotAgents
        agents = RobotAgents.__new__(RobotAgents)
        agents.llm = MagicMock()
        agents.planner_llm = MagicMock()
        agents.library_context = MagicMock()
        agents.assembler_context = None
        return agents

    @pytest.mark.parametrize("verbose", [True, False])
    @pytest.mark.parametrize(
        "method", ["step_planner_agent", "code_assembler_agent"])
    def test_agent_verbose_follows_setting(self, method, verbose):
        agents = self._agents()

        with patch("src.backend.crew_ai.agents.Agent") as agent_cls, \
             patch("src.backend.crew_ai.agents.settings") as cfg:
            cfg.CREWAI_VERBOSE = verbose
            getattr(agents, method)()

        assert agent_cls.call_args.kwargs["verbose"] is verbose, (
            f"{method} passed verbose="
            f"{agent_cls.call_args.kwargs['verbose']!r} when the setting was "
            f"{verbose!r}; it is hardcoded rather than read from config")


class TestCrewsHonourTheSetting:
    """The generation crew and the dryrun repair crew both echo prompts."""

    @pytest.mark.parametrize("verbose", [True, False])
    def test_repair_crew_verbose_follows_setting(self, verbose):
        """dryrun_service builds its own Crew on the repair path.

        `Crew` is imported INSIDE repair_robot_code, so the patch target is
        `crewai.Crew` rather than a module attribute — the name is resolved
        fresh on each call. kickoff() is then the mock's, so no model is
        reached and no trace is written.
        """
        import crewai
        import src.backend.services.dryrun_service as mod

        with patch.object(crewai, "Crew") as crew_cls, \
             patch.object(mod.settings, "CREWAI_VERBOSE", verbose):
            mod.repair_robot_code(
                "run-id", "*** Settings ***",
                ["No keyword with name 'Clik' found."],
                "vertex", "gemini-3.5-flash")

        assert crew_cls.call_args.kwargs["verbose"] is verbose, (
            f"the repair crew was built with verbose="
            f"{crew_cls.call_args.kwargs['verbose']!r} when the setting was "
            f"{verbose!r}; it echoes the assembler prompt on every repair")

    def test_generation_crew_reads_the_setting(self):
        """crew.py is checked by source, not by construction, on purpose.

        Its Crew is built in `_make_crew`, a closure inside `run_crew`
        (crew.py:487). There is no way in without invoking the whole
        generation pipeline — planner LLM, element stage, progress queue —
        which is an integration test's job, not this one's.

        The substring scan is weaker in one direction only: an expression
        that mentions the setting but always evaluates true, such as
        `settings.CREWAI_VERBOSE or True`, would pass. It is stronger in
        another — it catches a hardcoded `verbose=True` at ANY site in the
        file, including one added later, which a single-call-site assertion
        would miss.
        """
        import src.backend.crew_ai.crew as mod

        with open(mod.__file__, encoding="utf-8") as handle:
            text = handle.read()

        assert "verbose=True" not in text, (
            "crew.py still hardcodes verbose=True")
        assert "settings.CREWAI_VERBOSE" in text, (
            "crew.py does not read CREWAI_VERBOSE")
