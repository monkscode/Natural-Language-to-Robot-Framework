"""
W5.1 (owner D13): a provider failure that ends crewai's salvage call reaches the user
as the plain provider sentence, not as a parse error.

crewai 1.15.44 swallows the Converter's give-up and keeps the agent's raw answer
(crewai/utilities/converter.py ~421-431), so the stage ends with no usable output.
The stage's wrapper still holds the give-up (Task 7's B-lite `_salvage_give_up`);
run_crew raises that same exception at the planner's and the assembler's failure
sites, and workflow_service's generic handler gives it the W5 sentence.

Offline, at the workflow level: a REAL run_agentic_workflow -> run_crew -> RobotAgents /
RobotTasks -> Crew.kickoff() on vertex_ai/gemini-3.8-flash, Converter included. Faked:
litellm.completion (answered in order), instructor, sleeps, identify_elements, the
dryrun gate and the metrics storage.

Referenced by: nothing (test module).
Depends on: src/backend/crew_ai/crew.py, src/backend/services/dryrun_service.py,
src/backend/services/workflow_service.py, src/backend/core/provider_errors.py.
"""

import json
import os
from unittest.mock import patch

import litellm

from src.backend.core.config import settings
from src.backend.core.provider_errors import friendly_provider_error
from src.backend.crew_ai.provider_retry import report_of
from tests.test_crew_ai.test_llm_salvage_routing import (
    _bad_request, _connection_error, _no_crewai_telemetry, _no_instructor, _no_waits, _spy, _timeout,
)

PLAN = json.dumps({"steps": [{"step_description": "open", "keyword": "New Browser", "value": "chromium"}]})
CODE = json.dumps({"code": "*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n    New Browser    chromium"})
NOT_JSON = "Final Answer: this is not JSON at all"
NULL_CODE = '{"code": null}'
# Fails the assembler guardrail (no JSON, no Robot section, no Library line) and AssemblyOutput.
NO_CODE = "Final Answer: I could not write the test."
PARSE_ERROR = "Failed to generate valid Robot Framework code: "


def _ok(text):
    return {"mock_response": text}


def _drive(answers, tmp_path):
    """Run one workflow. Returns (its last error/complete event, what run_crew raised or
    None, the run's RobotAgents, the requests sent)."""
    import src.backend.crew_ai.crew as crew_mod
    import src.backend.services.workflow_service as ws

    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    real_run_crew, real_agents = crew_mod.run_crew, crew_mod.RobotAgents
    raised, built = [], []

    def run_crew_rec(*a, **k):
        try:
            return real_run_crew(*a, **k)
        except BaseException as exc:
            raised.append(exc)
            raise

    def agents_rec(*a, **k):
        built.append(real_agents(*a, **k))
        return built[-1]

    spy, seen = _spy(answers)
    with _no_crewai_telemetry(), _no_instructor(), _no_waits(), \
         patch.object(settings, "OPTIMIZATION_ENABLED", False), \
         patch.object(settings, "VERTEXAI_PROJECT", "p"), \
         patch.object(settings, "VERTEXAI_LOCATION", "global"), \
         patch.object(settings, "LLM_REQUEST_TIMEOUT_S", 60), \
         patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": str(creds), "VERTEXAI_PROJECT": "p",
                                 "VERTEXAI_LOCATION": "global"}), \
         patch("litellm.completion", side_effect=spy), \
         patch.object(ws, "run_crew", side_effect=run_crew_rec), \
         patch.object(crew_mod, "RobotAgents", side_effect=agents_rec), \
         patch.object(crew_mod, "identify_elements", return_value={
             "steps": [{"keyword": "New Browser", "value": "chromium"}], "summary": {}}), \
         patch.object(ws, "validate_and_repair", side_effect=lambda wid, code, *a, **k: {
             "code": code, "dryrun_status": "passed", "repair_usage": {}}), \
         patch.object(ws, "get_temp_metrics_storage"), patch.object(ws, "get_workflow_metrics_collector"), \
         patch.object(ws, "get_run_registry"):
        events = [e for e in ws.run_agentic_workflow("open example.com", "vertex", "gemini-3.8-flash")
                  if e.get("status") in ("error", "complete")]
    return events[-1], (raised[0] if raised else None), built[0], seen


def _assert_provider_sentence(event, exc, tries=3):
    sentence = friendly_provider_error(exc)
    assert sentence.startswith("The model provider did not answer.")
    assert f"after {tries} tries" in sentence
    assert event["status"] == "error"
    assert event["message"] == sentence


class TestPlanner:
    def test_a_timed_out_salvage_reaches_the_user_as_the_provider_sentence(self, tmp_path):
        event, exc, agents, seen = _drive([_ok(NOT_JSON)] + [_timeout] * 3, tmp_path)
        # The agent's call, then ONE salvage budget (B-lite skips the Converter's attempts 2-3).
        assert len(seen) == 4
        assert exc is agents.planner_llm._salvage_give_up[1]
        assert isinstance(exc, litellm.Timeout)
        assert report_of(exc).tries == 3
        _assert_provider_sentence(event, exc)

    def test_a_rejected_salvage_reaches_the_generic_handler_as_the_providers_error(self, tmp_path):
        """APIConnectionError gets no sentence (LiteLLM's catch-all; provider_errors.py), so the
        user reads the provider's own redacted text instead of the planner parse error."""
        event, exc, agents, seen = _drive([_ok(NOT_JSON)] + [_connection_error] * 5, tmp_path)
        assert len(seen) == 6
        assert exc is agents.planner_llm._salvage_give_up[1]
        assert isinstance(exc, litellm.APIConnectionError)
        # The shape bench/gate_inputs.py counts as a provider miss (TEMPORARY_LITELLM_ERRORS).
        assert event["message"].startswith("An error occurred: litellm.APIConnectionError: ")

    def test_the_agents_own_provider_failure_propagates_as_before(self, tmp_path):
        event, exc, agents, seen = _drive([_timeout] * 3, tmp_path)
        assert len(seen) == 3
        assert isinstance(exc, litellm.Timeout)
        assert agents.planner_llm._salvage_give_up is None
        _assert_provider_sentence(event, exc)

    def test_a_salvage_that_answers_keeps_the_normal_path(self, tmp_path):
        event, exc, agents, seen = _drive([_ok(NOT_JSON), _ok(PLAN), _ok(CODE)], tmp_path)
        assert len(seen) == 3
        assert exc is None
        assert event["status"] == "complete"

    def test_a_fatal_salvage_error_keeps_todays_message(self, tmp_path):
        """A 400 carries no retry report, so nothing is remembered: 3 quick Converter attempts."""
        event, exc, agents, seen = _drive([_ok(NOT_JSON)] + [_bad_request] * 3, tmp_path)
        assert len(seen) == 4
        assert agents.planner_llm._salvage_give_up is None
        assert event["message"] == PARSE_ERROR + "Planner output contained no parsable steps"


class TestAssembler:
    def test_a_timed_out_salvage_reaches_the_user_as_the_provider_sentence(self, tmp_path):
        event, exc, agents, seen = _drive([_ok(PLAN), _ok(NULL_CODE)] + [_timeout] * 3, tmp_path)
        assert len(seen) == 5
        assert exc is agents.llm._salvage_give_up[1]
        assert isinstance(exc, litellm.Timeout)
        _assert_provider_sentence(event, exc)

    def test_a_null_code_with_no_provider_failure_gets_a_plain_message(self, tmp_path):
        """The salvage answers, but its answer fails validation 3 times: no give-up."""
        event, exc, agents, seen = _drive([_ok(PLAN), _ok(NULL_CODE)] + [_ok(NULL_CODE)] * 3, tmp_path)
        assert len(seen) == 5
        assert agents.llm._salvage_give_up is None
        assert event["message"] == PARSE_ERROR + "Assembler output contained no code"

    def test_a_give_up_cleared_by_a_later_agent_call_keeps_the_normal_path(self, tmp_path):
        """Guardrail retry: the 2nd answer's export (crewai task.py:1602) gives up, the guardrail
        still fails, and the 3rd agent call clears the memory before a clean export."""
        event, exc, agents, seen = _drive(
            [_ok(PLAN), _ok(NO_CODE), _ok(NO_CODE)] + [_timeout] * 3 + [_ok(CODE)], tmp_path)
        assert len(seen) == 7
        assert exc is None
        assert agents.llm._salvage_give_up is None
        assert event["status"] == "complete"

    def test_the_second_export_after_a_guardrail_retry_re_raises_without_a_request(self, tmp_path):
        """The 2nd answer's export (task.py:1602) gives up; the guardrail passes it, and the
        export at task.py:1549 is answered by the memory at once: no 7th request."""
        event, exc, agents, seen = _drive(
            [_ok(PLAN), _ok(NO_CODE), _ok(NULL_CODE)] + [_timeout] * 3, tmp_path)
        assert len(seen) == 6
        assert exc is agents.llm._salvage_give_up[1]
        _assert_provider_sentence(event, exc)
