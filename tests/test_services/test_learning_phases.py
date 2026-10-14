"""T6 — learning is split in two halves that run at different points.

The record half (failure analysis, the execution-record store, engine routing)
must run the moment the result SSE has been sent: a feedback POST that follows
the result by milliseconds has to find the row.  The attribution half makes a
5-30s LLM call, so it must stay behind artifact persistence or a durable report
waits on an LLM.

The other contract pinned here is the hand-off between them.  The hint metadata
cache is consumed with a destructive .pop() in the record half
(_process_learning_record in workflow_service.py), and the pre-run record read
there is the only one that predates the store — so the attribution half must
take both as arguments and re-read neither.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

from src.backend.services import workflow_service as ws


def _drain(agen):
    async def _run():
        out = []
        async for chunk in agen:
            out.append(chunk)
        return out
    return asyncio.run(_run())


def _drive_execution(tmp_path, record_return):
    """Run _stream_docker_execution, recording every side effect in order."""
    order = []

    store = MagicMock()
    store.run_dir.return_value = tmp_path
    store.persist_run.side_effect = lambda run_id: order.append("persist_run")

    def fake_execute(run_id, test_filename):
        return {"test_status": "passed", "logs": ""}

    def fake_inline(run_dir):
        order.append("inline_report_screenshots")
        return 0

    def fake_record(run_id, user_query, robot_code, result):
        order.append("record")
        return record_return

    def fake_attribution(run_id, user_query, robot_code, result,
                         pre_run_record, injected_hint_ids_json):
        order.append("attribution")

    original_dumps = json.dumps

    def recording_dumps(obj, *a, **kw):
        # The result SSE is the only execution event carrying a verdict.
        if isinstance(obj, dict) and obj.get("stage") == "execution" and "test_status" in obj:
            order.append("result_sse")
        return original_dumps(obj, *a, **kw)

    with patch.object(ws.runner_exec_client, "ensure_image", return_value=None), \
         patch.object(ws.runner_exec_client, "execute", fake_execute), \
         patch.object(ws, "_set_run_status", return_value=None), \
         patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
         patch.object(ws, "get_artifact_store", return_value=store), \
         patch.object(ws, "inline_report_screenshots", fake_inline), \
         patch.object(ws, "_process_learning_record", fake_record), \
         patch.object(ws, "_process_learning_attribution", fake_attribution), \
         patch.object(ws.json, "dumps", recording_dumps):
        _drain(ws._stream_docker_execution(
            "run-1", "*** Test Cases ***", "search on example.com",
            lambda: order.append("release_slot")))

    return order


class TestPhaseOrder:
    def test_the_record_lands_before_artifacts_and_attribution_after(self, tmp_path):
        """The whole point of T6: the store is submitted while the artifacts are
        still being written, not after them."""
        order = _drive_execution(tmp_path, record_return=("PRE", "[1]"))

        assert order == [
            "result_sse",
            "release_slot",
            "record",
            "inline_report_screenshots",
            "persist_run",
            "attribution",
        ], order

    def test_attribution_is_skipped_when_the_record_phase_skipped(self, tmp_path):
        """No context means learning was disabled, the query was empty, or the
        record phase threw — attribution has nothing to credit against."""
        order = _drive_execution(tmp_path, record_return=None)

        assert "record" in order
        assert "attribution" not in order, order


class TestPhaseHandoff:
    _FIRE = "src.backend.crew_ai.optimization.conflict_detection.fire_usage_attribution"

    @staticmethod
    def _set_cache(run_id, nl_ids):
        ws._hint_metadata_cache.clear()
        ws._hint_metadata_cache[run_id] = {
            "agents": {"planner": {"count": len(nl_ids), "available": len(nl_ids),
                                   "sources": ["nl_feedback"]}},
            "nl_injected_ids": list(nl_ids),
        }

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_record_phase_returns_the_context_and_fires_nothing(self, mock_get_fl, mock_fire):
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_fl.execution_memory.get.return_value = None  # first attempt

        self._set_cache("split-1", [5, 12])
        ctx = ws._process_learning_record(
            "split-1", "search on example.com", "code", {"test_status": "passed"})

        assert ctx == (None, "[5, 12]")
        mock_fl.process_execution.assert_called_once()
        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_attribution_uses_the_ids_the_record_phase_consumed(self, mock_get_fl, mock_fire):
        """The cache pop is destructive.  If attribution re-read it, it would
        find nothing and credit no hint on a passing run."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl
        mock_fl.execution_memory.get.return_value = None

        self._set_cache("split-2", [5, 12])
        ctx = ws._process_learning_record(
            "split-2", "search on example.com", "code", {"test_status": "passed"})
        assert "split-2" not in ws._hint_metadata_cache

        ws._process_learning_attribution(
            "split-2", "search on example.com", "code", {"test_status": "passed"}, *ctx)

        mock_fire.assert_called_once()
        assert mock_fire.call_args.kwargs["injected_hint_ids"] == "[5, 12]"

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_attribution_never_re_reads_the_row_its_own_store_changed(
            self, mock_get_fl, mock_fire):
        """Between the two phases the writer thread stores this run's record.
        Re-reading it would turn a first attempt into a re-run and read back
        counters the store just wrote."""
        mock_fl = MagicMock()
        mock_get_fl.return_value = mock_fl

        ws._process_learning_attribution(
            "split-3", "search on example.com", "code", {"test_status": "passed"},
            None, "[5, 12]")

        mock_fl.execution_memory.get.assert_not_called()
        mock_fire.assert_called_once()


class TestLearningGetsTheTestWithoutTheChangeWaits:
    """F1: the two change-wait lines are the framework's, not the assembler's.

    The executed test (test.robot) keeps them; both learning halves receive the
    test as it was before `insert_change_waits` wrote them.
    """

    BARE = (
        "*** Settings ***\n"
        "Library    Browser    timeout=30s\n"
        "Library    BuiltIn\n"
        "\n"
        "*** Variables ***\n"
        "${url}    https://example.com\n"
        "${btn_locator}    id=go\n"
        "${total_locator}    css=#total\n"
        "\n"
        "*** Test Cases ***\n"
        "Generated Test\n"
        "    New Browser    chromium    headless=True\n"
        "    New Page    ${url}\n"
        "    Click    ${btn_locator}\n"
        "    ${total}=    Get Text    ${total_locator}\n"
        "    Log    ${total}\n"
        "    Close Browser\n"
    )
    WAITS = ({"read_locator": "css=#total", "action_locator": "id=go"},)

    @staticmethod
    def _drive(tmp_path, robot_code):
        """Run _stream_docker_execution on `robot_code`; return what each learning half
        received and the text written to test.robot (the executed test)."""
        seen = {}
        store = MagicMock()
        store.run_dir.return_value = tmp_path

        def fake_record(run_id, user_query, code, result):
            seen["record"] = code
            return ("PRE", "[1]")

        def fake_attribution(run_id, user_query, code, result,
                             pre_run_record, injected_hint_ids_json):
            seen["attribution"] = code

        with patch.object(ws.runner_exec_client, "ensure_image", return_value=None), \
             patch.object(ws.runner_exec_client, "execute",
                          lambda run_id, test_filename: {"test_status": "passed", "logs": ""}), \
             patch.object(ws, "_set_run_status", return_value=None), \
             patch.object(ws, "_safe_evict_hint_metadata", return_value=None), \
             patch.object(ws, "get_artifact_store", return_value=store), \
             patch.object(ws, "inline_report_screenshots", lambda run_dir: 0), \
             patch.object(ws, "_process_learning_record", fake_record), \
             patch.object(ws, "_process_learning_attribution", fake_attribution):
            _drain(ws._stream_docker_execution(
                "run-1", robot_code, "click go and get the total", lambda: None))

        seen["executed"] = (tmp_path / "test.robot").read_text(encoding="utf-8")
        return seen

    def test_both_learning_halves_get_the_test_as_it_was_before_the_insert(self, tmp_path):
        from src.backend.crew_ai.robot_code_normalizer import insert_change_waits

        with_pair = insert_change_waits(self.BARE, self.WAITS)
        assert with_pair != self.BARE  # control: the pair is really in this test

        seen = self._drive(tmp_path, with_pair)

        assert seen["record"] == self.BARE
        assert seen["attribution"] == self.BARE
        assert seen["executed"] == with_pair

    def test_a_test_without_the_pair_reaches_both_halves_unchanged(self, tmp_path):
        seen = self._drive(tmp_path, self.BARE)

        assert seen["record"] == self.BARE
        assert seen["attribution"] == self.BARE
        assert seen["executed"] == self.BARE
