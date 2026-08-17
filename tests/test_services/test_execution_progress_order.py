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
             patch.object(ws, "_set_run_status", lambda *a, **k: None), \
             patch.object(ws, "_safe_evict_hint_metadata", lambda *a, **k: None), \
             patch.object(ws, "_process_learning", lambda *a, **k: None), \
             patch.object(ws.json, "dumps", recording_dumps):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", lambda self, rid, create=False: tmp_path):
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

        with patch.object(ws.runner_exec_client, "ensure_image", lambda: None), \
             patch.object(ws.runner_exec_client, "execute", fake_execute), \
             patch.object(ws, "_set_run_status", lambda *a, **k: None), \
             patch.object(ws, "_safe_evict_hint_metadata", lambda *a, **k: None), \
             patch.object(ws, "_process_learning", lambda *a, **k: None):
            store = ws.get_artifact_store()
            with patch.object(type(store), "run_dir", lambda self, rid, create=False: tmp_path):
                captured = _drain(
                    ws._stream_docker_execution("run-1", "*** Test Cases ***", None, lambda: None))

        messages = [e.get("message", "") for e in _events(captured)
                    if e.get("stage") == "execution"]
        assert any("first run" in m.lower() for m in messages), messages
