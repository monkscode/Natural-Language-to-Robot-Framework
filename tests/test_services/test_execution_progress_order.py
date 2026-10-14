"""The client must hear about the wait BEFORE the wait, not after it.

Measured on a cold machine (2026-08-17): the first Run Test took 98.8s because
ensure_image was downloading the runner image, and the client received nothing at
all for that whole time — the 'Preparing execution environment' event was yielded
only once ensure_image had returned. The run succeeded, but 99 seconds of silence
after a click reads as a hang.
"""
import asyncio
import json
from unittest.mock import patch

from src.backend.services import workflow_service as ws


def _drain(agen):
    async def _run():
        out = []
        async for chunk in agen:
            out.append(chunk)
        return out
    return asyncio.run(_run())


def _events(chunks):
    parsed = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data: "):
                parsed.append(json.loads(line[6:]))
    return parsed


class TestProgressPrecedesImageProvisioning:
    def test_preparing_event_is_emitted_before_ensure_image_runs(self, tmp_path):
        """Ordering, asserted by recording when ensure_image was called."""
        order = []

        def fake_ensure_image():
            order.append("ensure_image")

        def fake_execute(run_id, test_filename):
            order.append("execute")
            return {"test_status": "passed", "logs": ""}

        original_json_dumps = json.dumps

        def recording_dumps(obj, *a, **kw):
            if isinstance(obj, dict) and obj.get("stage") == "execution":
                order.append(f"yield:{obj.get('message', '')[:20]}")
            return original_json_dumps(obj, *a, **kw)

        with patch.object(ws.runner_exec_client, "ensure_image", fake_ensure_image), \
             patch.object(ws.runner_exec_client, "execute", fake_execute), \
             patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
             patch.object(ws, "_process_learning_record", return_value=None), \
             patch.object(ws.json, "dumps", recording_dumps):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", return_value=tmp_path):
                _drain(ws._stream_docker_execution("run-1", "*** Test Cases ***", None, lambda: None))

        assert "ensure_image" in order, "ensure_image was never called"
        first_yield = next((i for i, o in enumerate(order) if o.startswith("yield:")), None)
        assert first_yield is not None, "no execution-stage event was emitted"
        assert first_yield < order.index("ensure_image"), (
            "the client must be told work has started BEFORE ensure_image blocks on "
            f"a potentially multi-minute image download; order was {order}"
        )

    def test_preparing_message_mentions_the_first_run_download(self, tmp_path):
        """A bare 'Preparing…' does not explain a two-minute wait."""
        captured = []

        def fake_execute(run_id, test_filename):
            return {"test_status": "passed", "logs": ""}

        with patch.object(ws.runner_exec_client, "ensure_image", return_value=None), \
             patch.object(ws.runner_exec_client, "execute", fake_execute), \
             patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
             patch.object(ws, "_process_learning_record", return_value=None):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", return_value=tmp_path):
                captured = _drain(
                    ws._stream_docker_execution("run-1", "*** Test Cases ***", None, lambda: None))

        messages = [e.get("message", "") for e in _events(captured)
                    if e.get("stage") == "execution"]
        assert any("first run" in m.lower() for m in messages), messages


class TestExecutionErrorsAreAlwaysReadable:
    """An execution error the user cannot read is the failure mode this branch
    exists to remove — and one that quotes a credential is worse than useless."""

    def test_an_error_with_no_text_still_names_its_cause(self, tmp_path):
        """Several exception types stringify to '' (a no-arg TimeoutError, a
        bare DockerException). Forwarding that verbatim shows the client
        status='error' with nothing to display."""
        def blank_failure():
            raise TimeoutError()

        with patch.object(ws.runner_exec_client, "ensure_image", blank_failure), \
             patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", return_value=tmp_path):
                captured = _drain(
                    ws._stream_docker_execution("run-1", "*** Test Cases ***", None, lambda: None))

        errors = [e for e in _events(captured) if e.get("status") == "error"]
        assert errors, "no error event was emitted"
        assert errors[-1]["message"].strip(), "error event carried an empty message"
        assert "TimeoutError" in errors[-1]["message"]

    def test_a_failed_test_file_write_does_not_echo_a_credential(self, tmp_path):
        """The save-failure branch is the one client-facing error message the
        redaction pass missed; every other one goes through redact_secrets."""
        key = "AIzaSy" + "D" * 33

        def leaky_open(*_a, **_kw):
            raise OSError(f"cannot write /run/secrets/env?key={key}")

        with patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
             patch("builtins.open", leaky_open):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", return_value=tmp_path):
                captured = _drain(
                    ws._stream_docker_execution("run-1", "*** Test Cases ***", None, lambda: None))

        errors = [e for e in _events(captured) if e.get("status") == "error"]
        assert errors, "no error event was emitted"
        assert key not in errors[-1]["message"]
        assert "[REDACTED]" in errors[-1]["message"]


class TestRunIdRidesTheExecutionStream:
    """Owner ruling R7-5: the SPA learns its new run's id from the stream.

    /execute-test is an SSE stream with no response body for an id to come
    back in, so both SPA clients read `run_id` off the first execution event
    — TestsPage keys its in-flight refresh and its error copy on it. Nothing
    on either side of the wire held that: the server could stop sending the
    key with 60 tests green, and the client could switch to `ev.stage` with
    65 green, so the two halves could drift to green independently. These are
    the server half; the client half is in TestsPage.test.tsx.
    """

    def test_the_first_execution_event_carries_the_run_id(self, tmp_path):
        """Not "an" event — the FIRST one. A client that has to wait for a
        later event cannot show the run while it is still running, which is
        the whole reason the key is on this event rather than the result."""
        def fake_execute(run_id, test_filename):
            return {"test_status": "passed", "logs": ""}

        with patch.object(ws.runner_exec_client, "ensure_image", return_value=None), \
             patch.object(ws.runner_exec_client, "execute", fake_execute), \
             patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
             patch.object(ws, "_process_learning_record", return_value=None):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", return_value=tmp_path):
                captured = _drain(ws._stream_docker_execution(
                    "run-abc", "*** Test Cases ***", None, lambda: None))

        execution = [e for e in _events(captured) if e.get("stage") == "execution"]
        assert execution, "no execution-stage event was emitted"
        assert execution[0].get("run_id") == "run-abc", (
            "the first execution event must name its run; the SPA has no "
            f"other way to learn it — got {execution[0]}")

    def test_the_file_save_error_carries_the_run_id_too(self, tmp_path):
        """The one execution event that can arrive BEFORE the first one. A
        client that never sees another event still has to learn which run
        failed, so this branch carries the key for the same reason."""
        def failing_open(*_a, **_kw):
            raise OSError("disk full")

        with patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
             patch("builtins.open", failing_open):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", return_value=tmp_path):
                captured = _drain(ws._stream_docker_execution(
                    "run-xyz", "*** Test Cases ***", None, lambda: None))

        events = _events(captured)
        assert events, "the save-failure branch emitted nothing at all"
        assert events[0].get("run_id") == "run-xyz", events[0]
