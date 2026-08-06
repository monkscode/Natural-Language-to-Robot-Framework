"""Detachment survives a mid-stream failure, and preflight logs honestly.

bench/run_bench.py's docstring makes detachment unconditional: bench data must
never reach History, the metrics dashboards or pricing. The happy path honours
that. These tests hold the two paths where it used to slip — a request that
dies after the server already created the workflow, and a preflight that logs
OK for a run its own violations made non-comparable.

The failed run is queued, not detached where it failed: the stream dying is not
the run dying, and deleting a run the server is still executing takes its
bind-mounted robot_tests/<id> out from under a live container.
"""

from types import SimpleNamespace
from unittest.mock import patch

import requests

from bench.run_bench import drain_deferred_detach, gate_pins, run_once

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


def _timeout_after(events):
    """A stream that delivers `events` and then dies, like a read timeout."""
    def blow_up(base_url, query, token, sink=None):
        sink.extend(events)
        raise requests.ReadTimeout("read timed out")
    return blow_up


class TestMidStreamFailureQueuesDetachment:
    """A dead stream does not mean a dead RUN.

    The id only reaches the client on the terminal generation event, so the
    failure we can detach by is one during the EXECUTION phase — where the
    server is still inside runner_exec_client.execute and the container holds
    robot_tests/<id> as a read-write bind mount. Detaching there would rmtree a
    live run directory and save a half-finished snapshot as that run's evidence.
    The id is queued instead, and drained once the sweep is over.
    """

    def _run(self, stream_side_effect):
        captured, detached, pending = [], [], []
        with patch("bench.run_bench.stream_generate_and_run",
                   side_effect=stream_side_effect), \
             patch("bench.run_bench.capture_evidence",
                   side_effect=lambda conn, wid: captured.append(wid) or True), \
             patch("bench.run_bench.detach_run",
                   side_effect=lambda conn, wid: detached.append(wid)):
            row = run_once("http://nlrf", None, "q01", "a query", 1, None, None,
                           pending)
        return row, captured, detached, pending

    def test_a_partial_stream_is_queued_not_detached_on_the_spot(self):
        row, captured, detached, pending = self._run(_timeout_after(PARTIAL_EVENTS))
        assert pending == [WF], "the failed run was not queued for detachment"
        assert captured == [] and detached == [], (
            "the run was detached while the server may still be executing it")

    def test_failure_row_records_the_workflow_id_it_queued(self):
        """An error row with no id cannot be traced back to its evidence dir."""
        row, _, _, _ = self._run(_timeout_after(PARTIAL_EVENTS))
        assert row["workflow_id"] == WF
        assert row["generation_status"] == "error", (
            "the run failed — the partial stream's status must not overwrite it")

    def test_failure_before_any_event_queues_nothing(self):
        """No workflow_id means nothing the client can detach BY — and
        capture_evidence(None) would write a bench/runs/None directory."""
        def blow_up(base_url, query, token, sink=None):
            raise requests.ConnectionError("connection refused")

        row, captured, detached, pending = self._run(blow_up)
        assert pending == [] and captured == [] and detached == []
        assert row["generation_status"] == "error"

    def test_failure_before_generation_completes_warns_it_is_undetachable(
            self, capsys):
        """In-progress SSE events carry no workflow_id, so a timeout during
        generation leaves llm_traces rows the client cannot find. It cannot be
        fixed here — it must not be silent."""
        row, captured, detached, pending = self._run(_timeout_after(
            [(0.0, {"stage": "generation", "status": "running", "progress": 22})]))
        assert pending == [] and captured == [] and detached == []
        assert row["workflow_id"] == ""      # build_csv_row writes None as ''
        assert "could not be detached" in capsys.readouterr().err

    def test_a_healthy_run_is_never_queued(self):
        """Deferral is for the failure path only — a run whose stream completed
        is detached inline, as it always was."""
        def clean(base_url, query, token, sink=None):
            sink.extend(PARTIAL_EVENTS)
            sink.append((3.0, {"stage": "execution", "status": "passed"}))

        # The generation reached "complete", so run_once polls for the metrics
        # row; this test is about the queue, not the metrics columns.
        with patch("bench.run_bench.fetch_metrics_data", return_value=None):
            row, captured, detached, pending = self._run(clean)
        assert pending == [], "a completed run must not wait for the sweep to end"
        assert captured == [WF] and detached == [WF]


class TestDrainingTheQueue:
    """What the sweep does with the queue once every run is over."""

    def _drain(self, pending, *, capture_ok=True):
        captured, detached = [], []
        with patch("bench.run_bench.capture_evidence",
                   side_effect=lambda conn, wid: captured.append(wid) or capture_ok), \
             patch("bench.run_bench.detach_run",
                   side_effect=lambda conn, wid: detached.append(wid)):
            drain_deferred_detach(None, pending)
        return captured, detached

    def test_every_queued_run_is_captured_then_detached(self):
        other = "99999999-8888-7777-6666-555555555555"
        captured, detached = self._drain([WF, other])
        assert captured == [WF, other]
        assert detached == [WF, other], "a queued run was left attached to History"

    def test_capture_failure_leaves_rows_alone_and_warns(self, capsys):
        """The capture-before-delete contract holds on this path too."""
        captured, detached = self._drain([WF], capture_ok=False)
        assert detached == [], "rows were deleted without a successful capture"
        assert WF in capsys.readouterr().err

    def test_one_failure_does_not_strand_the_rest_of_the_queue(self, capsys):
        """detach_run talks to Postgres. If it raises for one id, the remaining
        ids must still be drained — otherwise a single DB hiccup at the end of a
        sweep leaves every later run attached."""
        other = "99999999-8888-7777-6666-555555555555"
        detached = []

        def flaky(conn, wid):
            if wid == WF:
                raise RuntimeError("connection reset")
            detached.append(wid)

        with patch("bench.run_bench.capture_evidence", return_value=True), \
             patch("bench.run_bench.detach_run", side_effect=flaky):
            drain_deferred_detach(None, [WF, other])

        assert detached == [other]
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


class TestTheSweepAlwaysDrainsWhatItQueued:
    """The queue only helps if it is actually emptied — including when the
    sweep is abandoned. Ctrl-C during a 90-run bench is the ordinary way this
    ends, and the runs queued before it must not stay attached."""

    def _sweep(self, run_once_side_effect, tmp_path):
        import bench.run_bench as rb

        queries = tmp_path / "q.json"
        queries.write_text('{"queries": [{"id": "q01", "query": "a"},'
                           ' {"id": "q02", "query": "b"}]}', encoding="utf-8")
        argv = ["run_bench", "--queries", str(queries), "--repeats", "1",
                "--out", str(tmp_path / "out.csv")]
        drained = []
        with patch.object(rb.sys, "argv", argv), \
             patch.object(rb.psycopg, "connect"), \
             patch.object(rb, "gate_schema"), patch.object(rb, "gate_pins"), \
             patch.object(rb, "append_row"), \
             patch.object(rb, "run_once", side_effect=run_once_side_effect), \
             patch.object(rb, "drain_deferred_detach",
                          side_effect=lambda conn, p: drained.extend(p)):
            try:
                rb.main()
            except KeyboardInterrupt:
                pass
        return drained

    @staticmethod
    def _queue_then(exc):
        def _run_once(base_url, token, qid, query, repeat, log, conn,
                      pending_detach=None):
            pending_detach.append(f"wf-{qid}")
            if exc is not None and qid == "q02":
                raise exc
            return {"generation_status": "error", "test_status": "",
                    "total_s": None}
        return _run_once

    def test_a_completed_sweep_drains_the_queue(self, tmp_path):
        assert self._sweep(self._queue_then(None), tmp_path) == ["wf-q01", "wf-q02"]

    def test_an_interrupted_sweep_still_drains_what_it_queued(self, tmp_path):
        """RED before the `finally`: a Ctrl-C left every queued run attached."""
        drained = self._sweep(self._queue_then(KeyboardInterrupt()), tmp_path)
        assert drained == ["wf-q01", "wf-q02"], (
            "the sweep was abandoned with runs still attached to History/metrics")


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
