"""gemini-3.5-flash stays known to LiteLLM when its model-list download fails.

LiteLLM downloads its model list at import and, on any failure, silently loads
the copy bundled in the package, which lacks gemini-3.5-flash: schema
enforcement then turns off and the model's cost reads $0. The fixture below
recreates that state by removing the three keys; patch.dict restores the map.

Referenced by: none (leaf test module).
Depends on: src/backend/crew_ai/model_map_fallback.py, litellm 1.75.3.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import litellm
import pytest
from litellm.utils import supports_response_schema

from src.backend.crew_ai import model_map_fallback as fallback

KEYS = ("vertex_ai/gemini-3.5-flash", "gemini/gemini-3.5-flash", "gemini-3.5-flash")


@pytest.fixture
def failed_download():
    """litellm.model_cost as after a failed download; restored afterwards."""
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        for key in KEYS:
            litellm.model_cost.pop(key, None)
        yield


def _cost(model):
    return litellm.cost_per_token(model=model, prompt_tokens=1000, completion_tokens=100)


def test_the_fixture_reproduces_the_defect(failed_download):
    """Control: without the fallback the model has no schema support and no price."""
    assert supports_response_schema(model="vertex_ai/gemini-3.5-flash") is False
    with pytest.raises(Exception, match="isn't mapped yet"):
        _cost("vertex_ai/gemini-3.5-flash")


def test_vertex_gets_schema_and_cost_back(failed_download, caplog):
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-3.5-flash")
    assert supports_response_schema(model="vertex_ai/gemini-3.5-flash") is True
    assert _cost("vertex_ai/gemini-3.5-flash") == pytest.approx((0.0015, 0.0009))
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "2026-09-27" in caplog.records[0].getMessage()


def test_gemini_gets_schema_and_cost_back(failed_download, caplog):
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("gemini", "gemini/gemini-3.5-flash")
    assert supports_response_schema(model="gemini/gemini-3.5-flash") is True
    assert _cost("gemini/gemini-3.5-flash") == pytest.approx((0.0015, 0.0009))
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_a_known_model_is_left_alone(caplog):
    """gemini-2.5-flash is in both the bundled and the online list."""
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        before = dict(litellm.model_cost)
        with caplog.at_level(logging.WARNING, logger=fallback.__name__):
            fallback.ensure_model_entry("vertex", "vertex_ai/gemini-2.5-flash")
        assert litellm.model_cost == before
    assert caplog.records == []


def test_an_unknown_cloud_model_logs_one_error(failed_download, caplog):
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-9-flash")
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-9-flash")
    assert litellm.model_cost == before
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "cost reads $0" in caplog.records[0].getMessage()


def test_local_is_ignored(failed_download, caplog):
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.DEBUG, logger=fallback.__name__):
        fallback.ensure_model_entry("local", "ollama/gemini-3.5-flash")
    assert litellm.model_cost == before
    assert caplog.records == []


def test_present_keys_are_never_overwritten(failed_download):
    """A key LiteLLM already has is kept; only the missing ones are added."""
    litellm.model_cost["gemini/gemini-3.5-flash"] = {"sentinel": True}
    fallback.ensure_model_entry("vertex", "vertex_ai/gemini-3.5-flash")
    assert litellm.model_cost["gemini/gemini-3.5-flash"] == {"sentinel": True}
    assert supports_response_schema(model="vertex_ai/gemini-3.5-flash") is True


def test_a_known_but_incomplete_entry_is_kept_and_reported(failed_download, caplog):
    """LiteLLM resolves vertex_ai/gemini-3.5-flash to the bare key when present, so an
    entry there without schema support makes the model "known": nothing is
    overwritten, and the ERROR says schema enforcement is off."""
    litellm.model_cost["gemini-3.5-flash"] = {"sentinel": True}
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-3.5-flash")
    assert litellm.model_cost["gemini-3.5-flash"] == {"sentinel": True}
    assert "vertex_ai/gemini-3.5-flash" not in litellm.model_cost
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "WITHOUT schema enforcement." in caplog.records[0].getMessage()


def test_a_broken_pin_file_never_raises(failed_download, caplog, tmp_path):
    broken = tmp_path / "pinned_model_entries.json"
    broken.write_text("{not json", encoding="utf-8")
    with patch.object(fallback, "PINNED_FILE", broken), \
         caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-3.5-flash")
    assert any(r.exc_info for r in caplog.records)
    failure = [r for r in caplog.records if r.exc_info]
    assert [r.levelname for r in failure] == ["ERROR"]
    assert "may be off" in failure[0].getMessage()


def test_a_failed_attempt_is_retried_on_the_next_call(failed_download, caplog):
    """A one-off failure (here: the pin file cannot be read once) must not leave the
    model unknown for the life of the process: the next call retries."""
    real_text = fallback.PINNED_FILE.read_text(encoding="utf-8")

    class FlakyFile:
        reads = 0

        def read_text(self, encoding=None):
            self.reads += 1
            if self.reads == 1:
                raise OSError("simulated transient read failure")
            return real_text

    flaky = FlakyFile()
    with patch.object(fallback, "PINNED_FILE", flaky), \
         caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-3.5-flash")
        fallback.ensure_model_entry("vertex", "vertex_ai/gemini-3.5-flash")
    assert flaky.reads == 2
    assert supports_response_schema(model="vertex_ai/gemini-3.5-flash") is True
    assert [r.levelname for r in caplog.records if r.name == fallback.__name__] == ["ERROR", "WARNING"]


def test_the_pin_file_holds_the_three_entries():
    doc = json.loads(fallback.PINNED_FILE.read_text(encoding="utf-8"))
    assert doc["pinned_on"] == "2026-09-27"
    assert sorted(doc["entries"]) == sorted(KEYS)
    for entry in doc["entries"].values():
        assert entry["supports_response_schema"] is True
        assert entry["input_cost_per_token"] == 1.5e-06
        assert entry["output_cost_per_token"] == 9e-06


@patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
def test_get_llm_keeps_the_schema_after_a_failed_download(MockCleanedLLMWrapper, failed_download):
    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
    from src.backend.crew_ai.tasks import PlanOutput

    get_llm(model_provider="vertex", model_name="gemini-3.5-flash", response_format=PlanOutput)
    assert MockCleanedLLMWrapper.call_args.kwargs["response_format"] is PlanOutput


def test_startup_checks_the_configured_model():
    import asyncio

    from src.backend import main

    seen = []
    with patch("src.backend.auth.security_posture.validate_security_posture"), \
         patch("src.backend.core.artifact_store.get_artifact_store"), \
         patch.object(main, "_check_learning_health", side_effect=RuntimeError("stop here")), \
         patch("src.backend.crew_ai.model_map_fallback.ensure_model_entry",
               side_effect=lambda p, m: seen.append((p, m))), \
         patch.object(main.settings, "MODEL_PROVIDER", "vertex"), \
         patch.object(main.settings, "ONLINE_MODEL", "gemini-3.5-flash"):
        startup = main.startup_event()
        with pytest.raises(RuntimeError, match="stop here"):
            asyncio.run(startup)
    assert seen == [("vertex", "vertex_ai/gemini-3.5-flash")]


def test_a_failing_check_never_blocks_startup(caplog):
    import asyncio

    from src.backend import main

    health = MagicMock(side_effect=RuntimeError("stop here"))
    with patch("src.backend.auth.security_posture.validate_security_posture"), \
         patch("src.backend.core.artifact_store.get_artifact_store"), \
         patch.object(main, "_check_learning_health", health), \
         patch("src.backend.crew_ai.model_map_fallback.ensure_model_entry",
               side_effect=RuntimeError("boom")), \
         patch.object(main.settings, "MODEL_PROVIDER", "vertex"), \
         patch.object(main.settings, "ONLINE_MODEL", "gemini-3.5-flash"), \
         caplog.at_level(logging.WARNING):
        startup = main.startup_event()
        with pytest.raises(RuntimeError, match="stop here"):
            asyncio.run(startup)
    health.assert_called_once()
    assert any("model-list check skipped" in r.getMessage() for r in caplog.records)
