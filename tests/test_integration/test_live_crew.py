"""
Live integration tests for CrewAI workflow (Tier 2).

Purpose: Verify that run_crew() produces valid Robot Framework code when
         executed with a real LLM provider.

Requires (matching MODEL_PROVIDER in src/backend/.env):
  - vertex: VERTEXAI_CREDENTIALS pointing at an existing service-account JSON
            + VERTEXAI_PROJECT set
  - gemini: GEMINI_API_KEY set

Run with:
  pytest tests/test_integration/test_live_crew.py -m integration -v

Skip expensive LLM tests via:
  pytest tests/test_integration/test_live_crew.py -m integration -k "not TestLiveLLM"
"""

import os
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_provider() -> "tuple[str, str] | None":
    """(provider, model) for live runs, from the SAME settings production uses.

    None when the configured provider has no usable credentials — the live
    class then skips instead of failing. Importing settings here also loads
    src/backend/.env (python-dotenv), so the gate behaves identically whether
    this module runs solo or after other tests already imported config.
    """
    from src.backend.core.config import settings
    if settings.MODEL_PROVIDER == "vertex":
        creds = os.getenv("VERTEXAI_CREDENTIALS")
        if creds and os.path.exists(creds) and settings.VERTEXAI_PROJECT:
            return ("vertex", settings.ONLINE_MODEL)
        return None
    if settings.MODEL_PROVIDER == "gemini":
        if os.getenv("GEMINI_API_KEY"):
            return ("gemini", settings.ONLINE_MODEL)
        return None
    return None  # local/ollama is not exercised by the online live suite


_LIVE = _live_provider()
_PROVIDER, _MODEL = _LIVE if _LIVE else ("", "")


def _is_valid_robot_code(code: str) -> bool:
    """Very lightweight check: Robot Framework code has a *** section header."""
    return "*** Test Cases ***" in code or "*** Settings ***" in code


# ---------------------------------------------------------------------------
# Tests for extract_url_from_query() — pure function, no LLM needed
# ---------------------------------------------------------------------------

class TestExtractUrlFromQueryLive:
    """Validate URL extraction logic (no external dependency)."""

    def test_full_https_url(self):
        from src.backend.crew_ai.crew import extract_url_from_query
        assert extract_url_from_query("search on https://amazon.com") == "https://amazon.com"

    def test_domain_only(self):
        from src.backend.crew_ai.crew import extract_url_from_query
        result = extract_url_from_query("login to flipkart.com and search")
        assert "flipkart" in result

    def test_bare_site_name_not_guessed(self):
        """Task 14: no TLD means no guess — 'wikipedia' could be .org, .com,
        or a staging host; fabricating one poisons learning-store domain keys."""
        from src.backend.crew_ai.crew import extract_url_from_query
        assert extract_url_from_query("go to wikipedia and search for python") is None

    def test_no_url_returns_none(self):
        from src.backend.crew_ai.crew import extract_url_from_query
        assert extract_url_from_query("click the submit button") is None


# ---------------------------------------------------------------------------
# Tests for run_crew() return contract — requires LLM
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    _LIVE is None,
    reason="No usable live LLM credentials for the configured MODEL_PROVIDER "
           "(vertex: VERTEXAI_CREDENTIALS file + VERTEXAI_PROJECT; gemini: GEMINI_API_KEY)",
)
class TestLiveLLMCrewOnline:
    """Tests that invoke the real configured LLM (vertex or gemini)."""

    def test_run_crew_returns_eight_values(self):
        """run_crew() returns a RunCrewResult: the original five members plus
        stage_metrics, shared_llm and guardrail_attempts."""
        from src.backend.crew_ai.crew import run_crew
        result = run_crew(
            "click the login button on example.com",
            model_provider=_PROVIDER,
            model_name=_MODEL,
            workflow_id="live-test-001",
        )
        assert len(result) == 8

    def test_run_crew_output_is_non_empty_string(self):
        """The assembler task's raw output is a non-empty string.

        Contract: Robot code is extracted from crew.tasks[-1].output.raw —
        since Task 16 the returned crew is the single-task ASSEMBLER crew.
        The first return value is the CrewOutput object, not a string (the
        old string contract predates the dryrun redesign that removed the
        validator agent).
        """
        from src.backend.crew_ai.crew import run_crew
        _, crew_obj, *_ = run_crew(
            "click the login button on example.com",
            model_provider=_PROVIDER,
            model_name=_MODEL,
            workflow_id="live-test-002",
        )
        assembler_raw = crew_obj.tasks[-1].output.raw
        assert isinstance(assembler_raw, str)
        assert len(assembler_raw) > 0

    def test_run_crew_output_contains_robot_sections(self):
        """Generated code (tasks[-1].output.raw) has Robot Framework section markers."""
        from src.backend.crew_ai.crew import run_crew
        _, crew_obj, *_ = run_crew(
            "navigate to google.com and search for python",
            model_provider=_PROVIDER,
            model_name=_MODEL,
            workflow_id="live-test-003",
        )
        assembler_raw = crew_obj.tasks[-1].output.raw
        assert _is_valid_robot_code(assembler_raw), (
            f"Output does not look like Robot Framework code:\n{assembler_raw[:500]}"
        )

    def test_hint_metadata_is_dict(self):
        """Fourth return value is always a dict (possibly empty)."""
        from src.backend.crew_ai.crew import run_crew
        _, _, _, hint_metadata, *_ = run_crew(
            "click the submit button on example.com",
            model_provider=_PROVIDER,
            model_name=_MODEL,
            workflow_id="live-test-004",
        )
        assert isinstance(hint_metadata, dict)

    def test_optimization_metrics_structure(self):
        """optimization_metrics is None or has expected numeric fields."""
        from src.backend.crew_ai.crew import run_crew
        _, _, optimization_metrics, *_ = run_crew(
            "open the home page of example.com",
            model_provider=_PROVIDER,
            model_name=_MODEL,
            workflow_id="live-test-005",
        )
        if optimization_metrics is not None:
            assert hasattr(optimization_metrics, "total_llm_calls")
            assert hasattr(optimization_metrics, "total_cost")

    def test_llm_monitor_is_per_workflow(self):
        """Fifth return value is an LLMFormattingMonitor scoped to this workflow only."""
        from src.backend.crew_ai.crew import run_crew
        from src.backend.crew_ai.llm_output_cleaner import LLMFormattingMonitor
        _, _, _, _, llm_monitor, *_ = run_crew(
            "click the submit button on example.com",
            model_provider=_PROVIDER,
            model_name=_MODEL,
            workflow_id="live-test-006",
        )
        assert isinstance(llm_monitor, LLMFormattingMonitor)
        # A real workflow always makes at least one LLM call
        assert llm_monitor.total_responses >= 1


class TestLiveCrewFallbackBehaviour:
    """Verify graceful error handling in run_crew() — no LLM required."""

    def test_hint_metadata_type_from_crew_signature(self):
        """hint_metadata is always initialised as dict before any logic runs.

        This test checks the import-level default without calling the full LLM
        pipeline by inspecting module-level defaults.
        """
        import inspect
        from src.backend.crew_ai import crew as crew_module
        src = inspect.getsource(crew_module.run_crew)
        # The source must contain `hint_metadata = {}`
        assert "hint_metadata = {}" in src
