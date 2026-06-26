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


def test_element_task_prompt_does_not_inject_workflow_id():
    # The LLM must no longer be the source of workflow_id — the brittle prompt
    # injection (tasks.py ~422/~480) is gone now that the tool reads it from contextvars.
    from src.backend.crew_ai.tasks import RobotTasks
    tasks = RobotTasks()
    desc = tasks.identify_elements_task(agent=None).description
    assert "workflow_id" not in desc
