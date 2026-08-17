"""Guards over the executor's logging wiring and run correlation.

The executor was invisible three ways at once and each cause hid the others:
its INFO records were discarded in-process (no setup_logging anywhere),
Alloy dropped the container before collection, and the obvious one-line fix
would have put a second writer on the API's rotating application.log. What
made it hard to notice is that `docker logs nlrf-runner-exec` is not empty —
uvicorn configures its own loggers and keeps printing, so the service looks
like it is logging fine.

These tests guard the wiring, not the log text: ordering (logging before the
warm-up that needs it) and correlation (a run_id bound so the executor's
lines join the run in Loki). Both fail silently in production.

The binding tests call the endpoint functions directly rather than through
TestClient. A sync endpoint runs on an anyio worker thread, so contextvars
set inside it are invisible from the test thread and a "was it cleared?"
assertion would pass vacuously.

Referenced by: nothing — pytest entry point.
Depends on: src/backend/runner_exec/app.py.
"""
from unittest.mock import MagicMock, patch

import pytest
import structlog

from src.backend.runner_exec.app import (
    DryrunRequest,
    ExecuteRequest,
    app,
    dryrun,
    execute,
)

RUN_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture(autouse=True)
def clear_context():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _bound_workflow_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("workflow_id")


def test_startup_configures_logging_before_the_warm_up_runs():
    """Order is the whole point: the warm-up is what we want to see.

    warm_image_cache reports all five of its outcomes through logging — four
    at INFO. Start it before logging is configured and every one is discarded
    by root's default WARNING level, which is the state this change fixes.
    """
    from fastapi.testclient import TestClient

    calls = MagicMock()
    with patch("src.backend.runner_exec.app.setup_logging") as setup, \
         patch("src.backend.runner_exec.app._start_image_warmup") as warmup:
        calls.attach_mock(setup, "setup_logging")
        calls.attach_mock(warmup, "warmup")
        with TestClient(app):
            pass

    assert [name for name, _, _ in calls.mock_calls] == ["setup_logging", "warmup"], (
        "logging must be configured before the warm-up thread starts, or the "
        "warm-up's own report is thrown away")


def test_startup_asks_for_stdout_only_logging():
    """log_dir=None is load-bearing, not a default.

    Any other value puts this process on logs/application.log, which the API
    container already owns and rotates through the shared ./logs bind mount.
    """
    from fastapi.testclient import TestClient

    with patch("src.backend.runner_exec.app.setup_logging") as setup, \
         patch("src.backend.runner_exec.app._start_image_warmup"):
        with TestClient(app):
            pass

    setup.assert_called_once_with(log_dir=None)


def test_execute_binds_the_run_id_so_its_lines_join_the_run():
    """run_id IS the workflow id — workflow_service normalises it via uuid.UUID.

    Without this bind the executor's lines reach Loki carrying no workflow_id,
    and the one thing the log pipeline exists for — paste an id, see the whole
    run — does not include the stage that actually ran the test.
    """
    seen = {}

    def capture(*_args, **_kwargs):
        seen["workflow_id"] = _bound_workflow_id()
        return {"status": "complete", "test_status": "passed"}

    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.run_test_in_container", side_effect=capture):
        execute(ExecuteRequest(run_id=RUN_ID, test_filename="test.robot"))

    assert seen["workflow_id"] == RUN_ID


def test_dryrun_binds_the_run_id_too():
    seen = {}

    def capture(*_args, **_kwargs):
        seen["workflow_id"] = _bound_workflow_id()
        return {"status": "PASSED"}

    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.run_dryrun_in_container", side_effect=capture):
        dryrun(DryrunRequest(run_id=RUN_ID, code="*** Test Cases ***"))

    assert seen["workflow_id"] == RUN_ID


def test_the_binding_is_cleared_when_the_request_finishes():
    """The contract is "bound during, gone after", whatever dispatches it.

    anyio hands each sync endpoint a freshly copied context, so nothing in the
    running system would break today if the clear were dropped — which is
    exactly why it needs its own guard. The contract would rot silently until
    something ran the handler in a shared context: a direct call like this one,
    a background task, or a change to the dispatch path.
    """
    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.run_test_in_container",
               return_value={"status": "complete"}):
        execute(ExecuteRequest(run_id=RUN_ID, test_filename="test.robot"))

    assert _bound_workflow_id() is None


def test_the_binding_is_cleared_even_when_the_run_fails():
    """The failure path is the one that leaves a thread poisoned for hours."""
    from fastapi import HTTPException

    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.run_test_in_container",
               side_effect=RuntimeError("container died")):
        with pytest.raises(HTTPException):
            execute(ExecuteRequest(run_id=RUN_ID, test_filename="test.robot"))

    assert _bound_workflow_id() is None


def test_a_rejected_run_id_is_never_bound():
    """Validation runs first, so a path-traversal attempt cannot reach the log
    context — where it would be written into every subsequent line."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        execute(ExecuteRequest(run_id="../etc", test_filename="test.robot"))

    assert _bound_workflow_id() is None
