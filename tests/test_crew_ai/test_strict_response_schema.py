"""
The planner and assembler schemas reach Gemini in their STRICT form.

crewai hands our response_format CLASS (PlanOutput / AssemblyOutput) to
litellm.completion. For a Gemini route litellm 1.105.3 builds the schema with its
own override (VertexGeminiConfig.get_json_schema_from_pydantic_object) = pydantic's
raw model_json_schema(): PlannedStep requires 2 of 9 fields, 7 carry
"default": null. gemini-3.8-flash then folds the plan into one step. The wrapper
sends litellm's strict dict instead (every field required, nullable), built at
call time from self.response_format, so the salvage path's swap is covered too.

Asserted on the wire (CLAUDE.md: assert on the wire, parametrize over every model
family we run). No provider call: params are built, never sent, and the salvage
tests answer litellm.completion with LiteLLM's mock_response.

Referenced by: nothing (test module).
Depends on: src/backend/crew_ai/cleaned_llm_wrapper.py, src/backend/crew_ai/tasks.py,
            litellm.utils, litellm.llms.vertex_ai.gemini.transformation,
            tests/test_crew_ai/test_llm_salvage_routing.py (salvage helpers).
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from litellm.llms.vertex_ai.gemini.transformation import _transform_request_body
from litellm.utils import get_optional_params, type_to_response_format_param

from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper, get_llm
from src.backend.crew_ai.tasks import AssemblyOutput, PlannedStep, PlanOutput
from tests.test_crew_ai.test_llm_salvage_routing import (
    STEPS,
    VALID,
    Other,
    Out,
    _convert,
    _no_instructor,
    _spy,
)

MODELS = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
PROVIDERS = {"vertex": "vertex_ai", "gemini": "gemini"}
ENV = {"GEMINI_API_KEY": "k", "VERTEXAI_PROJECT": "p", "VERTEXAI_LOCATION": "global"}
MESSAGES = [{"role": "user", "content": "x"}]


def _settings():
    s = MagicMock()
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = 2
    s.LLM_REQUEST_TIMEOUT_S = 60
    return patch("src.backend.core.config.settings", s)


def _llm(provider, model, response_format=None):
    with _settings(), patch.dict(os.environ, ENV):
        return get_llm(provider, model, response_format=response_format)


def _strict(model_cls):
    return type_to_response_format_param(model_cls)


routes = pytest.mark.parametrize("provider", sorted(PROVIDERS))
models = pytest.mark.parametrize("model", MODELS)
schemas = pytest.mark.parametrize("schema", [PlanOutput, AssemblyOutput], ids=["plan", "assembly"])


class TestTheParamsCarryTheStrictDict:
    @routes
    @models
    @schemas
    def test_response_format_is_the_strict_dict(self, provider, model, schema):
        llm = _llm(provider, model, schema)
        assert llm.model == f"{PROVIDERS[provider]}/{model}"
        params = llm._prepare_completion_params(MESSAGES)
        assert params["response_format"] == _strict(schema)
        # crewai's own support check (llm.py:2568) reads the attribute: left alone.
        assert llm.response_format is schema

    def test_the_plan_schema_requires_every_step_field_and_has_no_defaults(self):
        sent = _llm("vertex", "gemini-3.8-flash", PlanOutput)._prepare_completion_params(MESSAGES)
        schema = sent["response_format"]["json_schema"]["schema"]
        step = schema["$defs"]["PlannedStep"]
        assert sorted(step["required"]) == sorted(PlannedStep.model_fields)
        assert sorted(step["properties"]) == sorted(PlannedStep.model_fields)
        assert step["additionalProperties"] is False
        assert '"default"' not in json.dumps(schema)


class TestTheRequestBody:
    @routes
    @models
    @schemas
    def test_the_body_carries_the_strict_json_schema(self, provider, model, schema):
        """The litellm stages after crewai: get_optional_params maps response_format,
        _transform_request_body builds generationConfig. Fed the class (no override),
        litellm's Gemini override puts the raw model_json_schema() here instead."""
        llm = _llm(provider, model, schema)
        optional = get_optional_params(
            model=model, custom_llm_provider=PROVIDERS[provider],
            response_format=llm._prepare_completion_params(MESSAGES)["response_format"],
        )
        body = _transform_request_body(
            messages=[{"role": "user", "content": "ping"}], model=model,
            optional_params=optional, custom_llm_provider=PROVIDERS[provider],
            litellm_params={}, cached_content=None,
        )
        generation_config = body["generationConfig"]
        assert generation_config["response_json_schema"] == _strict(schema)["json_schema"]["schema"]
        assert "response_schema" not in generation_config


class TestTheSalvageCall:
    @routes
    @models
    def test_a_salvage_sends_the_salvage_models_strict_dict(self, provider, model):
        llm = _llm(provider, model, PlanOutput)
        spy, seen = _spy([{"mock_response": VALID}])
        with _settings(), _no_instructor(), patch("litellm.completion", side_effect=spy):
            assert _convert(llm) == Out(steps=STEPS)
        assert len(seen) == 1
        assert seen[0]["response_format"] == _strict(Out)
        assert llm.response_format is PlanOutput
        assert llm._prepare_completion_params(MESSAGES)["response_format"] == _strict(PlanOutput)

    def test_a_salvage_for_another_model_sends_that_models_dict(self):
        llm = _llm("vertex", "gemini-3.8-flash", PlanOutput)
        other = json.dumps({"name": "n"})
        spy, seen = _spy([{"mock_response": VALID}, {"mock_response": other}])
        with _settings(), _no_instructor(), patch("litellm.completion", side_effect=spy):
            assert llm.call(MESSAGES, response_model=Out) == VALID
            assert llm.call(MESSAGES, response_model=Other) == other
        assert [c["response_format"] for c in seen] == [_strict(Out), _strict(Other)]
        assert llm.response_format is PlanOutput


class TestUnchangedPaths:
    def test_an_ollama_wrapper_keeps_the_class(self):
        llm = CleanedLLMWrapper(model="ollama/llama3", is_litellm=True, response_format=PlanOutput)
        assert llm._prepare_completion_params(MESSAGES)["response_format"] is PlanOutput

    @pytest.mark.parametrize("model", [
        "vertex_ai/claude-sonnet-4@20250514",
        "vertex_ai/meta/llama-3.1-405b-instruct-maas",
    ])
    def test_a_non_gemini_vertex_route_keeps_the_class(self, model):
        """Vertex also serves Claude / Llama; litellm builds their schema its own way."""
        llm = CleanedLLMWrapper(model=model, is_litellm=True, response_format=PlanOutput)
        assert llm._prepare_completion_params(MESSAGES)["response_format"] is PlanOutput

    def test_the_family_check_reads_the_routed_string_not_the_bare_name(self):
        llm = _llm("vertex", "gemini/claude-sonnet-4@20250514", PlanOutput)
        assert llm.model == "vertex_ai/claude-sonnet-4@20250514"
        assert llm._prepare_completion_params(MESSAGES)["response_format"] is PlanOutput

    @routes
    def test_no_response_format_sends_no_key(self, provider):
        assert "response_format" not in _llm(provider, "gemini-3.8-flash")._prepare_completion_params(MESSAGES)

    @routes
    def test_a_dict_response_format_is_unchanged(self, provider):
        llm = _llm(provider, "gemini-3.8-flash")
        llm.response_format = {"type": "json_object"}
        assert llm._prepare_completion_params(MESSAGES)["response_format"] == {"type": "json_object"}

    def test_a_schema_litellm_cannot_make_strict_falls_back_to_the_class(self, caplog):
        """The old path sent the class; a conversion error must not fail the call."""
        llm = _llm("vertex", "gemini-3.8-flash", PlanOutput)
        with patch("litellm.utils.type_to_response_format_param", side_effect=TypeError("boom")):
            assert llm._prepare_completion_params(MESSAGES)["response_format"] is PlanOutput
        assert any("strict" in r.getMessage() for r in caplog.records)


class TestStrictShapedAnswersStillParse:
    def test_a_plan_with_every_key_and_nulls_validates(self):
        step = {name: None for name in PlannedStep.model_fields}
        step.update(step_description="Open the page", keyword="New Page", value="https://example.com")
        plan = PlanOutput.model_validate_json(json.dumps({"steps": [step]}))
        assert plan.steps[0].value == "https://example.com"
        assert plan.steps[0].element_description is None

    def test_an_assembly_answer_validates(self):
        code = "*** Test Cases ***\nT\n    Log    x\n"
        assert AssemblyOutput.model_validate_json(json.dumps({"code": code})).code == code
