"""Phase 3: the browser tool reads identity from contextvars, not the LLM."""

import structlog

from tools.browser_use_tool import _identity_from_context


def test_identity_from_context_reads_bound_values():
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(workflow_id="wf-1", org_id="org-A", user_id="user-1")
    wid, org, uid = _identity_from_context()
    assert (wid, org, uid) == ("wf-1", "org-A", "user-1")
    structlog.contextvars.clear_contextvars()


def test_identity_from_context_empty_when_unbound():
    structlog.contextvars.clear_contextvars()
    assert _identity_from_context() == (None, None, None)


def test_element_payload_does_not_carry_workflow_id():
    # The tool payload must never be the source of workflow_id — the tool reads
    # identity from contextvars. Task 16 replaced the identify LLM task with the
    # deterministic builder, so the guard now sits on the element specs it builds.
    from src.backend.crew_ai.element_identification import build_elements
    steps = [
        {"keyword": "Open Browser", "value": "https://example.com", "step_description": "open"},
        {"keyword": "Input Text", "element_description": "search box",
         "value": "shoes", "step_description": "type"},
    ]
    elements, _ = build_elements(steps)
    assert elements, "expected at least one element spec"
    for element in elements:
        assert "workflow_id" not in element
