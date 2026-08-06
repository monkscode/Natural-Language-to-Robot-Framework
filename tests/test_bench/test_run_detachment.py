"""Detachment survives a mid-stream failure, and preflight logs honestly.

bench/run_bench.py's docstring makes detachment unconditional: bench data must
never reach History, the metrics dashboards or pricing. The happy path honours
that. These tests hold the two paths where it used to slip — a request that
dies after the server already created the workflow, and a preflight that logs
OK for a run its own violations made non-comparable.
"""

from types import SimpleNamespace
from unittest.mock import patch

import requests

from bench.run_bench import gate_pins, run_once

PINNED_NLRF = {"status": "healthy", "pins": {
    "optimization_enabled": False, "model_provider": "gemini",
    "online_model": "gemini-3.5-flash", "dryrun_enabled": True}}
UNPINNED_NLRF = {"status": "healthy", "pins": {
    "optimization_enabled": True, "model_provider": "gemini",
    "online_model": "gemini-3.5-flash", "dryrun_enabled": True}}
BROWSER_HEALTH = {"status": "healthy", "model_provider": "gemini", "headless": True}

WF = "11111111-2222-3333-4444-555555555555"

# The id reaches the client only on the terminal generation event
# (workflow_service.py: in-progress events carry "progress", never
# "workflow_id"), so this is what a detachable partial stream looks like: the
# generation finished, and the connection died during the execution phase.
PARTIAL_EVENTS = [
    (0.0, {"stage": "generation", "status": "running", "progress": 62}),
    (1.0, {"stage": "generation", "status": "complete", "workflow_id": WF}),
    (2.0, {"stage": "execution", "status": "running"}),
]


class TestMidStreamFailureStillDetaches:
    """READ_TIMEOUT_S is finite and Docker execution is the long phase, so a
    read timeout after the generation completed is the realistic trigger. The
    run's workflow_metrics / llm_traces / test_runs rows exist by then."""

    def _run(self, stream_side_effect):
        captured, detached = [], []
        with patch("bench.run_bench.stream_generate_and_run",
                   side_effect=stream_side_effect), \
             patch("bench.run_bench.capture_evidence",
                   side_effect=lambda conn, wid: captured.append(wid) or True), \
             patch("bench.run_bench.detach_run",
                   side_effect=lambda conn, wid: detached.append(wid)):
            row = run_once("http://nlrf", None, "q01", "a query", 1, None, None)
        return row, captured, detached

    def test_partial_stream_is_captured_and_detached(self):
        def blow_up(base_url, query, token, sink=None):
            sink.extend(PARTIAL_EVENTS)
            raise requests.ReadTimeout("read timed out")

        row, captured, detached = self._run(blow_up)
        assert captured == [WF], "capture_evidence never ran on the failure path"
        assert detached == [WF], "the run's rows were left attached to History"

    def test_failure_row_records_the_workflow_id_it_detached(self):
        """An error row with no id cannot be traced back to its evidence dir."""
        def blow_up(base_url, query, token, sink=None):
            sink.extend(PARTIAL_EVENTS)
            raise requests.ReadTimeout("read timed out")

        row, _, _ = self._run(blow_up)
        assert row["workflow_id"] == WF
        assert row["generation_status"] == "error", (
            "the run failed — the partial stream's status must not overwrite it")

    def test_failure_before_any_event_detaches_nothing(self):
        """No workflow_id means nothing the client can detach BY — and
        capture_evidence(None) would write a bench/runs/None directory."""
        def blow_up(base_url, query, token, sink=None):
            raise requests.ConnectionError("connection refused")

        row, captured, detached = self._run(blow_up)
        assert captured == [] and detached == []
        assert row["generation_status"] == "error"

    def test_failure_before_generation_completes_warns_it_is_undetachable(
            self, capsys):
        """In-progress SSE events carry no workflow_id, so a timeout during
        generation leaves llm_traces rows the client cannot find. It cannot be
        fixed here — it must not be silent."""
        def blow_up(base_url, query, token, sink=None):
            sink.append((0.0, {"stage": "generation", "status": "running",
                               "progress": 22}))
            raise requests.ReadTimeout("read timed out")

        row, captured, detached = self._run(blow_up)
        assert captured == [] and detached == []
        assert row["workflow_id"] == ""      # build_csv_row writes None as ''
        assert "could not be detached" in capsys.readouterr().err

    def test_capture_failure_leaves_rows_alone_and_warns(self, capsys):
        """The capture-before-delete contract holds on this path too."""
        def blow_up(base_url, query, token, sink=None):
            sink.extend(PARTIAL_EVENTS)
            raise requests.ReadTimeout("read timed out")

        detached = []
        with patch("bench.run_bench.stream_generate_and_run", side_effect=blow_up), \
             patch("bench.run_bench.capture_evidence", return_value=False), \
             patch("bench.run_bench.detach_run",
                   side_effect=lambda conn, wid: detached.append(wid)):
            run_once("http://nlrf", None, "q01", "a query", 1, None, None)
        assert detached == [], "rows were deleted without a successful capture"
        assert WF in capsys.readouterr().err


class TestStreamSink:
    """The failure path can only detach what the stream handed back."""

    def test_sink_is_filled_in_place(self):
        import bench.run_bench as rb

        class _Resp:
            encoding = "utf-8"
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_lines(self, decode_unicode=False):
                yield 'data: {"stage": "generation", "workflow_id": "%s"}' % WF
                raise requests.ReadTimeout("read timed out")

        sink = []
        with patch.object(rb.requests, "post", return_value=_Resp()):
            try:
                rb.stream_generate_and_run("http://nlrf", "q", None, sink)
            except requests.ReadTimeout:
                pass
        assert [ev for _, ev in sink] == [{"stage": "generation",
                                           "workflow_id": WF}]


class TestPreflightLogIsHonest:
    """The bench log is the evidence record for a run. A false OK line makes a
    non-comparable run look comparable during a later analysis."""

    def _gate(self, health, allow_unpinned, tmp_path):
        args = SimpleNamespace(base_url="http://nlrf", browser_url="http://bs",
                               allow_unpinned=allow_unpinned)
        with patch("bench.run_bench.fetch_health",
                   side_effect=[health, BROWSER_HEALTH]):
            gate_pins(args, tmp_path / "run.csv")

    def test_pinned_run_logs_ok(self, tmp_path, capsys):
        self._gate(PINNED_NLRF, False, tmp_path)
        assert "preflight OK" in capsys.readouterr().out

    def test_ignored_violations_do_not_log_ok(self, tmp_path, capsys):
        self._gate(UNPINNED_NLRF, True, tmp_path)
        out = capsys.readouterr().out
        assert "preflight OK" not in out
        assert "UNPINNED" in out
