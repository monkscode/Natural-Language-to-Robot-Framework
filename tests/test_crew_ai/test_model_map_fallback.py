"""The model-list fallback after the litellm 1.105.3 upgrade.

litellm 1.105.3 bundles gemini-3.5-flash and gemini-3.8-flash, so the fallback is
a no-op for both (owner decision 5). For an UNMAPPED gemini name litellm builds a
schema-True, price-0 entry instead of raising, so "known" means "has a price" and
the startup ERROR fires on a missing price too. Exact prices come from the repo's
pin file, never from the loaded table (in CI that is the live GitHub list).

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


def _invalidate():
    """litellm caches get_model_info lookups; model_cost edits are invisible until they are cleared."""
    litellm.utils._invalidate_model_cost_lowercase_map()


@pytest.fixture
def keys_removed():
    """litellm.model_cost without our six keys; restored (and caches cleared) afterwards."""
    _invalidate()
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        for key in KEYS:
            litellm.model_cost.pop(key, None)
        _invalidate()
        yield
    _invalidate()


@pytest.fixture
def fresh_checks():
    _invalidate()
    with patch.dict(litellm.model_cost), patch.object(fallback, "_checked", set()):
        yield
    _invalidate()


def _cost(model):
    return litellm.cost_per_token(model=model, prompt_tokens=1000, completion_tokens=100)


def _pin_doc(model_name="foo-bar"):
    """A pin file holding one entry for a model litellm does not map (copy of a real entry)."""
    doc = json.loads(fallback.PINNED_FILE.read_text(encoding="utf-8"))
    template = doc["entries"]["vertex_ai/gemini-3.8-flash"]
    return {"pinned_on": "2026-10-01", "entries": {f"vertex_ai/{model_name}": template}}


@pytest.mark.parametrize("provider, model", [
    ("vertex", "vertex_ai/gemini-3.5-flash"),
    ("gemini", "gemini/gemini-3.5-flash"),
    ("vertex", "vertex_ai/gemini-3.8-flash"),
    ("gemini", "gemini/gemini-3.8-flash"),
])
def test_the_loaded_map_gives_schema_and_cost_and_the_fallback_does_nothing(
        fresh_checks, caplog, provider, model):
    """Schema enforcement and a price are there without us. Asserts only 'schema True, price > 0':
    the loaded table is the live GitHub list in CI, so exact prices would turn CI red on an upstream change."""
    assert supports_response_schema(model=model) is True
    assert all(price > 0 for price in _cost(model))
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.DEBUG, logger=fallback.__name__):
        fallback.ensure_model_entry(provider, model)
    assert litellm.model_cost == before
    assert caplog.records == []


@pytest.mark.parametrize("provider, model", [
    ("vertex", "vertex_ai/gemini-3.8-flash"),
    ("gemini", "gemini/gemini-3.8-flash"),
])
def test_a_pinned_gemini_model_missing_from_the_map_gets_its_pin_back(keys_removed, caplog, provider, model):
    """litellm answers an absent gemini name with price 0; the fallback must still put the pinned price in."""
    entry = json.loads(fallback.PINNED_FILE.read_text(encoding="utf-8"))["entries"][model]
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry(provider, model)
    assert supports_response_schema(model=model) is True
    assert _cost(model) == pytest.approx((entry["input_cost_per_token"] * 1000,
                                          entry["output_cost_per_token"] * 100))
    assert [r.levelname for r in caplog.records] == ["WARNING"]


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


@pytest.mark.parametrize("model, schema_off", [
    ("vertex_ai/gemini-9-flash", False),  # litellm builds a schema-True, price-0 entry
    (UNMAPPED, True),                     # litellm raises "isn't mapped"
])
def test_an_unknown_cloud_model_logs_one_error(fresh_checks, caplog, model, schema_off):
    before = dict(litellm.model_cost)
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", model)
        fallback.ensure_model_entry("vertex", model)
    assert litellm.model_cost == before
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    message = caplog.records[0].getMessage()
    assert "cost reads $0" in message
    assert ("WITHOUT schema enforcement" in message) is schema_off


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


def test_an_incomplete_entry_without_a_pin_is_kept_and_reported(fresh_checks, caplog):
    """An entry with no price or schema flag for a model we hold no pin for: nothing is
    overwritten, and the ERROR says both schema enforcement and the price are off."""
    litellm.model_cost["vertex_ai/foo-bar"] = {"sentinel": True}
    _invalidate()
    with caplog.at_level(logging.WARNING, logger=fallback.__name__):
        fallback.ensure_model_entry("vertex", UNMAPPED)
    assert litellm.model_cost["vertex_ai/foo-bar"] == {"sentinel": True}
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    message = caplog.records[0].getMessage()
    assert "WITHOUT schema enforcement" in message and "cost reads $0" in message


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

    # the first import of main resets the root logger's handlers (structured logging): put caplog's back,
    # or this test passes only when an earlier test already imported main
    logging.getLogger().addHandler(caplog.handler)
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
