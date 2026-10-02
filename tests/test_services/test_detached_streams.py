"""A run's terminal writes must not depend on the client still being connected.

Every terminal write of a run (status, learning record, artifacts) lives inside
the body of a streaming generator. Starlette cancels the response when the
client leaves, and the generator then dies at its parked await, so the row
stayed 'running' forever while the work finished unseen. `_detached` runs the
body in its own task and makes the response a pure relay; these tests pin that.

Referenced by: src/backend/services/workflow_service.py (_detached)
Depends on: tests/test_services/test_execution_progress_order.py (mock pattern)
"""
import asyncio
import json
import threading
from unittest.mock import patch

import pytest

from src.backend.services import workflow_service as ws

_WF_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_CODE = "*** Test Cases ***\nT\n    Log    hi"


def _execute_mocks(tmp_path, execute, statuses, learning, releases):
    """Patches shared by every test that drives the real stream_execute_only."""
    store = ws.get_artifact_store()
    return [
        patch.object(ws.runner_exec_client, "ensure_image", return_value=None),
        patch.object(ws.runner_exec_client, "execute", execute),
        patch.object(ws, "_set_run_status",
                     side_effect=lambda run_id, status, *a, **k: statuses.append(status)),
        patch.object(ws, "_process_learning_record",
                     side_effect=lambda *a, **k: learning.append(1)),
        patch.object(ws, "_record_run", return_value=None),
        patch.object(ws, "_run_owner", return_value=None),
        patch.object(ws, "_safe_evict_hint_metadata", return_value=None),
        patch.object(ws, "_acquire_workflow_slot", return_value=True),
        patch.object(ws, "_release_workflow_slot",
                     side_effect=lambda: releases.append(list(statuses))),
        patch.object(type(store), "run_dir", return_value=tmp_path),
        patch.object(type(store), "persist_run", return_value=None),
    ]


def _enter(patches):
    for p in patches:
        p.start()
    return patches


def _exit(patches):
    for p in reversed(patches):
        p.stop()


class TestExecutionSurvivesTheReader:
    def test_status_is_written_after_the_reader_is_cancelled(self, tmp_path):
        statuses, learning, releases = [], [], []
        gate = threading.Event()

        def blocking_execute(run_id, test_filename):
            gate.wait(10)
            return {"test_status": "passed", "logs": ""}

        patches = _enter(_execute_mocks(
            tmp_path, blocking_execute, statuses, learning, releases))
        try:
            async def scenario():
                got_first = asyncio.Event()

                async def reader():
                    async for sse in ws.stream_execute_only(
                            _CODE, "q", workflow_id=_WF_ID, is_platform_admin=False):
                        if '"stage": "execution"' in sse:
                            got_first.set()

                task = asyncio.create_task(reader())
                await asyncio.wait_for(got_first.wait(), 10)
                task.cancel()          # what Starlette does on a disconnect
                with pytest.raises(asyncio.CancelledError):
                    await task
                at_disconnect = (list(statuses), list(releases))
                gate.set()
                await asyncio.gather(*ws._detached_jobs)
                return at_disconnect

            at_disconnect = asyncio.run(scenario())
        finally:
            gate.set()
            _exit(patches)

        assert at_disconnect == ([], []), "nothing may be written or released at the disconnect"
        assert statuses == ["passed"]
        assert len(learning) == 1
        assert releases == [["passed"]], "slot released once, after the status write"


class TestGenerationSurvivesTheReader:
    def _leave_after_first_id(self, events):
        calls = []

        def _capture(run_id, user, user_query, status, **kw):
            calls.append(status)

        with patch.object(ws, "run_agentic_workflow", return_value=iter(events)), \
             patch.object(ws, "_record_run", side_effect=_capture), \
             patch.object(ws, "_acquire_workflow_slot", return_value=True):

            async def scenario():
                agen = ws.stream_generate_only("login to github", "gemini", "gemini-2.5-flash")
                async for sse in agen:
                    if _WF_ID in sse:
                        break
                await agen.aclose()
                await asyncio.gather(*ws._detached_jobs)

            asyncio.run(scenario())
        return calls

    def test_generated_row_is_written_after_the_reader_leaves(self):
        calls = self._leave_after_first_id([
            {"status": "running", "message": "planning", "workflow_id": _WF_ID},
            {"status": "complete", "robot_code": _CODE, "workflow_id": _WF_ID},
        ])
        assert calls == ["running", "generated"]

    def test_error_row_is_written_after_the_reader_leaves(self):
        calls = self._leave_after_first_id([
            {"status": "running", "message": "planning", "workflow_id": _WF_ID},
            {"status": "error", "message": "LLM offline", "workflow_id": _WF_ID},
        ])
        assert calls == ["running", "error"]


class TestReaderThatStaysSeesTheSameEvents:
    def test_decorated_stream_yields_what_the_undecorated_job_yields(self, tmp_path):
        def run(stream_fn):
            statuses, learning, releases = [], [], []
            patches = _enter(_execute_mocks(
                tmp_path,
                lambda run_id, f: {"test_status": "passed", "logs": ""},
                statuses, learning, releases))
            try:
                async def collect():
                    return [sse async for sse in stream_fn(
                        _CODE, "q", workflow_id=_WF_ID, is_platform_admin=False)]
                return asyncio.run(collect())
            finally:
                _exit(patches)

        direct = run(ws.stream_execute_only.__wrapped__)
        relayed = run(ws.stream_execute_only)
        assert relayed == direct
        assert len(direct) >= 2


class TestDetachedHelper:
    @staticmethod
    def _boom_job(gate=None):
        async def job():
            yield "data: one\n\n"
            if gate is not None:
                await gate.wait()
            raise RuntimeError("boom")
        return ws._detached(job)

    def test_a_reader_that_stays_gets_the_event_then_the_error(self):
        stream = self._boom_job()

        async def scenario():
            got = []
            with pytest.raises(RuntimeError, match="boom"):
                async for sse in stream():
                    got.append(sse)
            return got

        assert asyncio.run(scenario()) == ["data: one\n\n"]

    def test_a_reader_that_left_gets_no_error_but_the_failure_is_logged(self, caplog):
        async def scenario():
            gate = asyncio.Event()
            stream = self._boom_job(gate)
            agen = stream()
            first = await agen.__anext__()
            await agen.aclose()
            gate.set()
            await asyncio.gather(*ws._detached_jobs)   # must not raise
            return first

        with caplog.at_level("ERROR"):
            first = asyncio.run(scenario())
        assert first == "data: one\n\n"
        assert any("stream job failed" in r.getMessage() and r.exc_info
                   for r in caplog.records)

    def test_concurrent_streams_do_not_cross_events(self):
        def make(name):
            async def job():
                for i in range(3):
                    await asyncio.sleep(0)
                    yield f"{name}{i}"
            return ws._detached(job)

        async def collect(stream):
            return [x async for x in stream()]

        async def scenario():
            return await asyncio.gather(collect(make("a")), collect(make("b")))

        a, b = asyncio.run(scenario())
        assert a == ["a0", "a1", "a2"]
        assert b == ["b0", "b1", "b2"]
