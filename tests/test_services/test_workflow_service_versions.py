"""stream_generate_only regenerating a NAMED test, and run_id on execute.

Task 7's half of the workflow service. Three things it has to get right, each
one a place the user is told something untrue if it does not:

- the target reaches ALL THREE writes a generation makes -- the opening row,
  the failure row and the success row -- or a regeneration that dies halfway
  is invisible on the test it was regenerating, and one that succeeds mints a
  SECOND test instead of appending to the first;
- the stream tells the client what actually landed. record_start swallows
  every failure it meets, deadlocks included (no lock order avoids one with
  both folder-move routes -- measured), so success is a READ-BACK, retried
  once, and never the generation's own 'complete' event;
- when the read-back still finds nothing, the message says the version could
  not be CONFIRMED. It cannot distinguish "not saved" from "could not
  check", and telling a user their work was lost when it may be sitting in
  the database is the one wrong answer here.

Referenced by: none (service tests only).
Depends on: src/backend/services/workflow_service.py (stream_generate_only,
stream_execute_only, _stream_docker_execution, _read_run_version).
"""
import asyncio
import contextlib
import json
from unittest.mock import patch

from src.backend.services import workflow_service as ws

_WF_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_TEST_ID = "8c1f0a52-6d4e-4a1b-9f33-2b7c5e0d4a10"
_ADMIN = {"user_id": "root", "org_id": "org-a", "email": "r@x.com"}

_OK_EVENTS = [
    {"status": "running", "message": "planning", "workflow_id": _WF_ID},
    {"status": "complete", "robot_code": "*** Tasks *** v2",
     "workflow_id": _WF_ID},
]
_ERR_EVENTS = [
    {"status": "running", "message": "planning", "workflow_id": _WF_ID},
    {"status": "error", "message": "LLM offline", "workflow_id": _WF_ID},
]


def _events(chunks):
    out = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _drive(events, *, read_back=(_TEST_ID, 2), user=None, **kwargs):
    """Run stream_generate_only over a scripted generation, collecting every
    _record_run call and every SSE event."""
    calls = []

    def _capture(run_id, user_arg, user_query, status, **kw):
        calls.append({"run_id": run_id, "status": status, **kw})

    reader = read_back if callable(read_back) else (lambda _run_id: read_back)
    with patch.object(ws, "run_agentic_workflow", return_value=iter(events)), \
         patch.object(ws, "_record_run", side_effect=_capture), \
         patch.object(ws, "_read_run_version", side_effect=reader), \
         patch.object(ws, "_acquire_workflow_slot", return_value=True):
        async def _run():
            return [sse async for sse in ws.stream_generate_only(
                "search boots", "gemini", "gemini-2.5-flash", user=user,
                **kwargs)]
        chunks = asyncio.run(_run())
    return calls, _events(chunks)


def _generated(calls):
    return [c for c in calls if c["status"] == "generated"]


# ---------------------------------------------------------------------------
# The target reaches every write
# ---------------------------------------------------------------------------

def test_the_opening_row_names_the_test_being_regenerated():
    """Without this a regeneration that never finishes leaves no mark on the
    test it was regenerating -- the whole point of an opening row."""
    calls, _ = _drive(_OK_EVENTS, regenerate_test_id=_TEST_ID,
                      version_reason="edited", report_version=True)
    opening = next(c for c in calls if c["status"] == "running")
    assert opening["test_id"] == _TEST_ID
    # No reason on the opening write: it carries no code, so appending is not
    # what it is for (spec case 10 / D8 (b)).
    assert opening.get("version_reason") is None


def test_the_success_write_carries_the_target_and_the_reason():
    calls, _ = _drive(_OK_EVENTS, regenerate_test_id=_TEST_ID,
                      version_reason="regenerated", report_version=True)
    success = _generated(calls)[0]
    assert success["test_id"] == _TEST_ID
    assert success["version_reason"] == "regenerated"
    assert success["robot_code"] == "*** Tasks *** v2"


def test_a_failed_regeneration_still_names_its_test():
    """Spec case 10: no version is written, but the failed run attaches, so
    "this test failed to regenerate" is visible ON the test."""
    calls, _ = _drive(_ERR_EVENTS, regenerate_test_id=_TEST_ID,
                      version_reason="edited", report_version=True)
    failure = next(c for c in calls if c["status"] == "error")
    assert failure["test_id"] == _TEST_ID
    assert failure.get("version_reason") is None


def test_generate_test_writes_no_target_and_reports_no_version():
    """POST /generate-test passes none of the three, and must behave exactly
    as it did before Task 7."""
    calls, events = _drive(_OK_EVENTS)
    assert all(c.get("test_id") is None for c in calls)
    assert all(c.get("version_reason") is None for c in calls)
    assert [e for e in events if e.get("stage") == "version"] == []


# ---------------------------------------------------------------------------
# The terminal version event
# ---------------------------------------------------------------------------

def test_the_stream_ends_with_the_version_the_read_back_found():
    _, events = _drive(_OK_EVENTS, regenerate_test_id=_TEST_ID,
                       version_reason="edited", report_version=True)
    assert events[-1] == {"stage": "version", "status": "complete",
                          "test_id": _TEST_ID, "n": 2, "run_id": _WF_ID}


def test_new_test_mode_reports_the_test_that_was_minted():
    """No target goes in, so the event's test_id can only come from the
    read-back -- which is what makes it the NEW test's id."""
    minted = "11111111-2222-3333-4444-555555555555"
    _, events = _drive(_OK_EVENTS, read_back=(minted, 1), report_version=True)
    assert events[-1] == {"stage": "version", "status": "complete",
                          "test_id": minted, "n": 1, "run_id": _WF_ID}


def test_a_generation_error_ends_with_no_version_event():
    """Nothing was generated, so there is nothing to confirm. The client's
    last word is generation's own error event."""
    _, events = _drive(_ERR_EVENTS, regenerate_test_id=_TEST_ID,
                       version_reason="edited", report_version=True)
    assert [e for e in events if e.get("stage") == "version"] == []
    assert events[-1]["stage"] == "generation"
    assert events[-1]["status"] == "error"


# ---------------------------------------------------------------------------
# The one retry, and the honest failure
# ---------------------------------------------------------------------------

def _one_miss_then_found():
    seen = {"n": 0}

    def _read(_run_id):
        seen["n"] += 1
        return (None, None) if seen["n"] == 1 else (_TEST_ID, 3)
    return _read


def test_the_success_write_is_retried_once_when_no_version_is_found():
    """record_start swallows a deadlock, so the first success write can be
    lost with no error anywhere. One retry, and it is safe to repeat: the
    registry branch never appends to a run that already names a version."""
    calls, events = _drive(_OK_EVENTS, read_back=_one_miss_then_found(),
                           regenerate_test_id=_TEST_ID,
                           version_reason="edited", report_version=True)
    assert [c["status"] for c in calls] == ["running", "generated", "generated"]
    assert events[-1]["status"] == "complete"
    assert events[-1]["n"] == 3


def test_the_retry_carries_the_same_platform_admin_flag():
    """A NEW _record_run call site. ran_as_platform_admin is write-once on
    the row that CREATED it, and the retry can BE that row when the first
    write was lost outright -- so omitting it here would silently record an
    admin's regeneration as an ordinary one."""
    calls, _ = _drive(_OK_EVENTS, read_back=_one_miss_then_found(),
                      user=_ADMIN, regenerate_test_id=_TEST_ID,
                      version_reason="edited", report_version=True)
    first, retry = _generated(calls)
    assert "is_platform_admin" in retry
    assert retry["is_platform_admin"] == first["is_platform_admin"]


def test_the_retry_repeats_the_target_and_the_reason():
    """A retry that dropped either would attach the run to no test, or
    attach it with no version -- the failure it exists to repair."""
    calls, _ = _drive(_OK_EVENTS, read_back=_one_miss_then_found(),
                      regenerate_test_id=_TEST_ID, version_reason="edited",
                      report_version=True)
    first, retry = _generated(calls)
    assert (retry["test_id"], retry["version_reason"]) == (_TEST_ID, "edited")
    assert retry["robot_code"] == first["robot_code"]


def test_an_unconfirmed_version_is_reported_as_unconfirmed():
    """The read-back cannot tell "not saved" from "could not check", so the
    message must not claim the first."""
    calls, events = _drive(_OK_EVENTS, read_back=(None, None),
                           regenerate_test_id=_TEST_ID,
                           version_reason="edited", report_version=True)
    assert [c["status"] for c in calls] == ["running", "generated", "generated"]
    last = events[-1]
    assert last["stage"] == "version"
    assert last["status"] == "error"
    assert last["run_id"] == _WF_ID
    assert "could not be confirmed" in last["message"]
    assert "not saved" not in last["message"]


def test_the_success_write_is_retried_at_most_once():
    calls, _ = _drive(_OK_EVENTS, read_back=(None, None),
                      regenerate_test_id=_TEST_ID, version_reason="edited",
                      report_version=True)
    assert len(_generated(calls)) == 2


# ---------------------------------------------------------------------------
# run_id on the execution stream (owner ruling R7-5)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _execution_succeeds():
    with patch.object(ws.runner_exec_client, "ensure_image",
                      return_value=None), \
         patch.object(ws.runner_exec_client, "execute",
                      return_value={"test_status": "passed", "logs": ""}), \
         patch.object(type(ws.get_artifact_store()), "persist_run",
                      return_value=None):
        yield


@contextlib.contextmanager
def _the_test_file_cannot_be_written():
    def _boom(*_a, **_kw):
        raise OSError("disk full")
    with patch("builtins.open", _boom):
        yield


def _drain_execution(tmp_path, outer):
    store = ws.get_artifact_store()
    with patch.object(type(store), "run_dir", return_value=tmp_path), \
         patch.object(ws, "_set_run_status", return_value=None), \
         patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
         patch.object(ws, "_process_learning_record", return_value=None), \
         outer():
        async def _run():
            return [c async for c in ws._stream_docker_execution(
                "run-1", "*** Test Cases ***", None, lambda: None)]
        return _events(asyncio.run(_run()))


def test_the_first_execution_event_carries_the_run_id(tmp_path):
    """The SPA has no other way to learn the id of the run it just started:
    /execute-test is an SSE stream, so there is no response body to read it
    from, and the History page blind-reloads the whole list instead today."""
    events = _drain_execution(tmp_path, _execution_succeeds)
    assert events[0]["stage"] == "execution"
    assert events[0]["run_id"] == "run-1"


def test_the_save_error_event_carries_the_run_id(tmp_path):
    """The only execution event that can come BEFORE the first one, so it
    needs the id too -- or a failed save leaves the client with no run to
    look up."""
    events = _drain_execution(tmp_path, _the_test_file_cannot_be_written)
    assert events[0]["status"] == "error"
    assert events[0]["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# stream_execute_only carries the named test onto the run row
# ---------------------------------------------------------------------------

def test_execute_records_the_named_test_and_version(tmp_path):
    calls = []

    def _capture(run_id, user_arg, user_query, status, **kw):
        calls.append({"status": status, "user_query": user_query, **kw})

    async def _no_events(*_a, **_kw):
        return
        yield  # pragma: no cover - makes this an async generator

    version_id = "99999999-8888-7777-6666-555555555555"
    with patch.object(ws, "_record_run", side_effect=_capture), \
         patch.object(ws, "_acquire_workflow_slot", return_value=True), \
         patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
         patch.object(ws, "_stream_docker_execution", _no_events):
        async def _run():
            return [c async for c in ws.stream_execute_only(
                "*** Tasks ***", None, None, user=None,
                history_query="search shoes", is_platform_admin=False,
                test_id=_TEST_ID, test_version_id=version_id)]
        asyncio.run(_run())

    assert len(calls) == 1
    assert calls[0]["test_id"] == _TEST_ID
    assert calls[0]["test_version_id"] == version_id
    # The description shown in History comes from the TEST; learning still
    # sees no user_query, so a Tests-page Run records none (spec section 9).
    assert calls[0]["user_query"] == "search shoes"
