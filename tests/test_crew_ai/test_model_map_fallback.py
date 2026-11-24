"""The model-list fallback after the litellm 1.105.3 upgrade.

litellm 1.105.3 bundles gemini-3.5-flash and gemini-3.8-flash, so a failed
model-list download at import no longer costs us schema enforcement or prices:
the fallback is a no-op for both models (owner decision 5). What stays worth
testing: that no-op, the fallback's mechanics for a model the map really lacks
(a made-up non-gemini name, which litellm leaves unmapped), the startup ERROR
for such a model, and the never-raises / retry rules.

Referenced by: none (leaf test module).
Depends on: src/backend/crew_ai/model_map_fallback.py, litellm 1.105.3.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import litellm
import pytest
from litellm.utils import supports_response_schema

from src.backend.crew_ai import model_map_fallback as fallback

KEYS = (
    "vertex_ai/gemini-3.5-flash", "gemini/gemini-3.5-flash", "gemini-3.5-flash",
    "vertex_ai/gemini-3.8-flash", "gemini/gemini-3.8-flash", "gemini-3.8-flash",
)
UNMAPPED = "vertex_ai/foo-bar"  # litellm 1.105.3 leaves a non-gemini name unmapped


@pytest.fixture
def keys_removed():
    """litellm.model_cost without our six keys; restored afterwards.

    NOT a reproduction of the old failed-download defect: litellm 1.105.3
    answers an absent gemini-named model itself (schema support; a price that
    depends on its own lookup caches), so the fallback does not fire for it.
    """
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        for key in KEYS:
            litellm.model_cost.pop(key, None)
        yield


@pytest.fixture
def fresh_checks():
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        yield


def _cost(model):
    return litellm.cost_per_token(model=model, prompt_tokens=1000, completion_tokens=100)


def _pin_doc(model_name="foo-bar"):
    """A pin file holding one entry for a model litellm does not map (copy of a real entry)."""
    doc = json.loads(fallback.PINNED_FILE.read_text(encoding="utf-8"))
    template = doc["entries"]["vertex_ai/gemini-3.8-flash"]
    return {"pinned_on": "2026-10-01", "entries": {f"vertex_ai/{model_name}": template}}


@pytest.mark.parametrize("provider, model, price", [
    ("vertex", "vertex_ai/gemini-3.5-flash", (0.0015, 0.0009)),
    ("gemini", "gemini/gemini-3.5-flash", (0.0015, 0.0009)),
    ("vertex", "vertex_ai/gemini-3.8-flash", (0.00075, 0.000375)),
    ("gemini", "gemini/gemini-3.8-flash", (0.00075, 0.000375)),
])
def test_the_bundled_map_gives_schema_and_cost_and_the_fallback_does_nothing(
        fresh_checks, caplog, provider, model, price):
    """The behaviour that matters: schema enforcement and the price are there without us."""
    assert supports_response_schema(model=model) is True
    assert _cost(model) == pytest.approx(price)
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.DEBUG, logger=fallback.__name__):
        fallback.ensure_model_entry(provider, model)
    assert litellm.model_cost == before
    assert caplog.records == []


def test_an_unmapped_model_gets_the_pinned_entry_and_a_warning(fresh_checks, caplog, tmp_path):
    """The fallback's mechanics, on a model the bundled map really lacks."""
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(_pin_doc()), encoding="utf-8")
    with patch.object(fallback, "PINNED_FILE", pin), \
         caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", UNMAPPED)
    assert supports_response_schema(model=UNMAPPED) is True
    assert _cost(UNMAPPED) == pytest.approx((0.00075, 0.000375))
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "2026-10-01" in caplog.records[0].getMessage()


def test_a_known_model_is_left_alone(caplog):
    """gemini-2.5-flash is in both the bundled and the online list."""
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        before = dict(litellm.model_cost)
        with caplog.at_level(logging.WARNING, logger=fallback.__name__):
            fallback.ensure_model_entry("vertex", "vertex_ai/gemini-2.5-flash")
        assert litellm.model_cost == before
    assert caplog.records == []


def test_an_unknown_cloud_model_logs_one_error(fresh_checks, caplog):
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", UNMAPPED)
        fallback.ensure_model_entry("vertex", UNMAPPED)
    assert litellm.model_cost == before
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "cost reads $0" in caplog.records[0].getMessage()


def test_local_is_ignored(fresh_checks, caplog):
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.DEBUG, logger=fallback.__name__):
        fallback.ensure_model_entry("local", "ollama/gemini-3.5-flash")
    assert litellm.model_cost == before
    assert caplog.records == []


def test_present_keys_are_never_overwritten(fresh_checks, tmp_path):
    """A key LiteLLM already has is kept; only the missing ones are added."""
    doc = _pin_doc()
    doc["entries"]["gemini/foo-bar"] = doc["entries"]["vertex_ai/foo-bar"]
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(doc), encoding="utf-8")
    litellm.model_cost["gemini/foo-bar"] = {"sentinel": True}
    with patch.object(fallback, "PINNED_FILE", pin):
        fallback.ensure_model_entry("vertex", UNMAPPED)
    assert litellm.model_cost["gemini/foo-bar"] == {"sentinel": True}
    assert "vertex_ai/foo-bar" in litellm.model_cost
    assert supports_response_schema(model=UNMAPPED) is True


def test_a_known_but_incomplete_entry_is_kept_and_reported(keys_removed, caplog):
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


def test_a_broken_pin_file_never_raises(fresh_checks, caplog, tmp_path):
    broken = tmp_path / "pinned_model_entries.json"
    broken.write_text("{not json", encoding="utf-8")
    with patch.object(fallback, "PINNED_FILE", broken), \
         caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", UNMAPPED)
    assert any(r.exc_info for r in caplog.records)
    failure = [r for r in caplog.records if r.exc_info]
    assert [r.levelname for r in failure] == ["ERROR"]
    assert "may be off" in failure[0].getMessage()


def test_a_failed_attempt_is_retried_on_the_next_call(fresh_checks, caplog):
    """A one-off failure (here: the pin file cannot be read once) must not leave the
    model unknown for the life of the process: the next call retries."""
    real_text = json.dumps(_pin_doc())

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
        fallback.ensure_model_entry("vertex", UNMAPPED)
        fallback.ensure_model_entry("vertex", UNMAPPED)
    assert flaky.reads == 2
    assert supports_response_schema(model=UNMAPPED) is True
    assert [r.levelname for r in caplog.records if r.name == fallback.__name__] == ["ERROR", "WARNING"]


def test_the_pin_file_holds_both_models_entries():
    doc = json.loads(fallback.PINNED_FILE.read_text(encoding="utf-8"))
    assert doc["pinned_on"] == "2026-10-01"
    assert sorted(doc["entries"]) == sorted(KEYS)
    for key, entry in doc["entries"].items():
        assert entry["supports_response_schema"] is True
        if key.endswith("gemini-3.8-flash"):
            assert entry["input_cost_per_token"] == 7.5e-07
            assert entry["output_cost_per_token"] == 3.75e-06
        else:
            assert entry["input_cost_per_token"] == 1.5e-06
            assert entry["output_cost_per_token"] == 9e-06


@patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
def test_get_llm_keeps_the_schema_when_the_models_keys_are_absent(MockCleanedLLMWrapper, keys_removed):
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
