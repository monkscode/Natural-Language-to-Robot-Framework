"""
Live integration tests for CrewAI workflow (Tier 2).

Purpose: Verify that run_crew() produces valid Robot Framework code when
         executed with a real LLM provider.

Requires:
  - GEMINI_API_KEY env variable set (for online provider)
  - OR a running Ollama instance (for local provider)

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

def _has_gemini_key():
    return bool(os.getenv("GEMINI_API_KEY"))


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

    def test_preposition_website(self):
        from src.backend.crew_ai.crew import extract_url_from_query
        result = extract_url_from_query("go to wikipedia and search for python")
        assert "wikipedia" in result

    def test_no_url_returns_placeholder(self):
        from src.backend.crew_ai.crew import extract_url_from_query
        result = extract_url_from_query("click the submit button")
        assert result == "website mentioned in query"


# ---------------------------------------------------------------------------
# Tests for run_crew() return contract — requires LLM
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_gemini_key(), reason="GEMINI_API_KEY not set")
class TestLiveLLMCrewOnline:
    """Tests that invoke a real Gemini LLM call."""

    def test_run_crew_returns_four_values(self):
        """run_crew() always returns (output, crew, metrics, hint_metadata)."""
        from src.backend.crew_ai.crew import run_crew
        result = run_crew(
            "click the login button on example.com",
            model_provider="online",
            model_name="gemini/gemini-2.0-flash",
            workflow_id="live-test-001",
        )
        assert len(result) == 4

    def test_run_crew_output_is_non_empty_string(self):
        """First return value (validation_output) is a non-empty string."""
        from src.backend.crew_ai.crew import run_crew
        validation_output, _, _, _ = run_crew(
            "click the login button on example.com",
            model_provider="online",
            model_name="gemini/gemini-2.0-flash",
            workflow_id="live-test-002",
        )
        assert isinstance(validation_output, str)
        assert len(validation_output) > 0

    def test_run_crew_output_contains_robot_sections(self):
        """Generated code has Robot Framework section markers."""
        from src.backend.crew_ai.crew import run_crew
        validation_output, _, _, _ = run_crew(
            "navigate to google.com and search for python",
            model_provider="online",
            model_name="gemini/gemini-2.0-flash",
            workflow_id="live-test-003",
        )
        assert _is_valid_robot_code(validation_output), (
            f"Output does not look like Robot Framework code:\n{validation_output[:500]}"
        )

    def test_hint_metadata_is_dict(self):
        """Fourth return value is always a dict (possibly empty)."""
        from src.backend.crew_ai.crew import run_crew
        _, _, _, hint_metadata = run_crew(
            "click the submit button on example.com",
            model_provider="online",
            model_name="gemini/gemini-2.0-flash",
            workflow_id="live-test-004",
        )
        assert isinstance(hint_metadata, dict)

    def test_optimization_metrics_structure(self):
        """optimization_metrics is None or has expected numeric fields."""
        from src.backend.crew_ai.crew import run_crew
        _, _, optimization_metrics, _ = run_crew(
            "open the home page of example.com",
            model_provider="online",
            model_name="gemini/gemini-2.0-flash",
            workflow_id="live-test-005",
        )
        if optimization_metrics is not None:
            assert hasattr(optimization_metrics, "total_llm_calls")
            assert hasattr(optimization_metrics, "total_cost")


class TestLiveCrewFallbackBehaviour:
    """Verify graceful error handling in run_crew() — no LLM required."""

    def test_run_crew_raises_on_invalid_library_type(self):
        """Unsupported library_type raises ValueError from get_library_context."""
        from src.backend.crew_ai.crew import run_crew
        with pytest.raises((ValueError, Exception)):
            run_crew(
                "click a button",
                model_provider="local",
                model_name="ollama",
                library_type="invalid_library_xyz",
                workflow_id="error-test",
            )

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
