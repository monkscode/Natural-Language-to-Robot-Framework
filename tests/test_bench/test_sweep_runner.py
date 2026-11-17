"""The held-out sweep runner (bench/sweep.py): target refusals, the judge call,
the per-run loop and its results file.

Every network call and the database connection are patched; nothing here talks to
a stack. The two streams are replaced by fakes that hand back event lists.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

import bench.sweep as sw
from bench.sweep_lib import SweepQuery

WF = "aaaaaaaa-0000-0000-0000-000000000001"
JWF = "bbbbbbbb-0000-0000-0000-000000000002"

ROBOT = (
    "*** Settings ***\n"
    "Library    Browser\n\n"
    "*** Test Cases ***\n"
    "Check page\n"
    "    New Page    https://example.test\n"
    "    Close Browser\n"
)
JUDGE = "Get Text    h1    ==    Hello"

PASS_Q = SweepQuery("p1", "check the page", "local", "sealed", "pass", JUDGE, None)
FAIL_Q = SweepQuery("f1", "planted bug page", "local", "sealed", "fail", None, "BUG")


def _gen_events(test_status="passed", wf=WF, gen="complete"):
    return [
        (0.0, {"stage": "generation", "status": gen, "workflow_id": wf}),
        (1.0, {"stage": "execution", "test_status": test_status}),
    ]


def _judge_events(status="passed", wf=JWF):
    return [
        (0.0, {"stage": "execution", "status": "running", "run_id": wf}),
        (1.0, {"stage": "execution", "test_status": status}),
    ]


class TestCheckTarget:
    OK = {"pins": {"optimization_enabled": True}}

    def test_refuses_production_url(self):
        for url in ("http://127.0.0.1:5000", "http://localhost:5000",
                    "http://127.0.0.1:5000/", "http://localhost:5000/"):
            assert sw.check_target(url, self.OK), url
        assert sw.check_target("http://127.0.0.1:5002", self.OK) == []

    def test_refuses_learning_off(self):
        for health in ({"pins": {"optimization_enabled": False}}, {"pins": {}}, {}):
            assert sw.check_target("http://127.0.0.1:5002", health)


class TestStreamExecute:
    def test_judge_call_sends_robot_code_only(self):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.iter_lines.return_value = iter(
            ['data: {"stage": "execution", "test_status": "passed"}'])
        with patch("bench.sweep.requests.post", return_value=resp) as post:
            events = sw.stream_execute("http://x", "CODE", "tok")
        assert post.call_args.args[0] == "http://x/execute-test"
        assert post.call_args.kwargs["json"] == {"robot_code": "CODE"}
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"
        assert [ev for _, ev in events] == [{"stage": "execution",
                                             "test_status": "passed"}]


class TestJudgeIdentity:
    def test_judge_identity_reads_first_run_id(self):
        events = _judge_events("failed") + [
            (2.0, {"stage": "execution", "status": "running", "run_id": "later"})]
        assert sw.judge_identity(events) == {"run_id": JWF, "judge_status": "failed"}

    def test_judge_error_event_is_error(self):
        events = [(0.0, {"stage": "execution", "status": "running", "run_id": JWF}),
                  (1.0, {"stage": "execution", "status": "error", "message": "boom"})]
        assert sw.judge_identity(events) == {"run_id": JWF, "judge_status": "error"}

    def test_empty_stream(self):
        assert sw.judge_identity([]) == {"run_id": None, "judge_status": None}


class _Rig:
    """Patches both streams and capture; records every call."""

    def __init__(self, tmp_path, gen_events, judge_events=None, robot_code=ROBOT,
                 error_message=None, capture_ok=True, judge_error_message=None):
        self.judge_error_message = judge_error_message
        self.out = tmp_path
        self.gen_events = gen_events
        self.judge_events = judge_events
        self.robot_code = robot_code
        self.error_message = error_message
        self.capture_ok = capture_ok
        self.captures = []
        self.judged = []

    def _gen(self, base_url, query, token, sink=None):
        if isinstance(self.gen_events, Exception):
            raise self.gen_events
        sink.extend(self.gen_events)
        return sink

    def _judge(self, base_url, code, token, sink=None):
        self.judged.append(code)
        if isinstance(self.judge_events, Exception):
            raise self.judge_events
        sink.extend(self.judge_events)
        return sink

    def _capture(self, conn, wf, dest=None):
        self.captures.append((wf, dest))
        if not self.capture_ok:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        is_judge = dest.name == "judge"
        row = {"robot_code": self.robot_code,
               "error_message": (self.judge_error_message if is_judge
                                 else self.error_message)}
        (dest / "test_runs.json").write_text(json.dumps([row]), encoding="utf-8")
        return True

    def run(self, queries, repeats=1):
        with patch("bench.sweep.stream_generate_and_run", side_effect=self._gen), \
             patch("bench.sweep.stream_execute", side_effect=self._judge), \
             patch("bench.sweep.capture_evidence", side_effect=self._capture):
            sw.run_sweep(queries, repeats, "http://x", None, None, self.out)
        lines = (self.out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines]


class TestRunSweep:
    def test_handled_row_has_every_field(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events())
        (row,) = rig.run([PASS_Q])
        assert set(row) == {
            "query_id", "half", "source", "expect", "repeat", "workflow_id",
            "generation_status", "test_status", "judge_run_id", "judge_status",
            "judge_refused", "error_message", "judge_error_message", "outcome",
            "tag", "hollow", "started_at", "total_s"}
        assert (row["outcome"], row["workflow_id"], row["judge_run_id"]) == (
            "handled", WF, JWF)
        assert row["judge_refused"] is None
        assert JUDGE in rig.judged[0]

    def test_planted_query_runs_no_judge(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events("failed"), error_message="BUG here")
        (row,) = rig.run([FAIL_Q])
        assert rig.judged == []
        assert row["outcome"] == "handled"
        assert row["judge_status"] is None and row["judge_run_id"] is None

    def test_hollow_reported_not_scored(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events())
        with patch("bench.sweep.hollow_shapes", return_value={"B_SHAPE", "A_SHAPE"}):
            (row,) = rig.run([PASS_Q])
        assert row["outcome"] == "handled"
        assert row["hollow"] == ["A_SHAPE", "B_SHAPE"]

    def test_judge_refused_reason_is_recorded(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events(),
                   robot_code="*** Test Cases ***\nA\n    Log    x\nB\n    Log    y\n")
        (row,) = rig.run([PASS_Q])
        assert rig.judged == []
        assert "tests" in row["judge_refused"]
        assert row["outcome"] == "judge_error"

    def test_no_captured_robot_code_skips_judge(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events(), capture_ok=False)
        (row,) = rig.run([PASS_Q])
        assert rig.judged == []
        assert row["judge_refused"] == "no robot_code captured"

    def test_failed_generation_runs_no_judge_and_is_tagged(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(gen="error", test_status=None) [:1],
                   error_message="Timeout 5000ms exceeded")
        (row,) = rig.run([PASS_Q])
        assert rig.judged == []
        assert row["outcome"] == "generation_failed"
        assert row["tag"] == "locator_dead"

    def test_handled_row_has_no_tag(self, tmp_path):
        (row,) = _Rig(tmp_path, _gen_events(), _judge_events()).run([PASS_Q])
        assert row["tag"] is None

    def test_stream_death_writes_generation_failed_row_and_continues(self, tmp_path):
        rig = _Rig(tmp_path, requests.ReadTimeout("read timed out"))
        rows = rig.run([PASS_Q, FAIL_Q])
        assert [r["query_id"] for r in rows] == ["p1", "f1"]
        assert all(r["outcome"] == "generation_failed" for r in rows)
        assert rows[0]["generation_status"] == "error"

    def test_stream_death_after_running_event_keeps_id(self, tmp_path):
        rig = _Rig(tmp_path, None)
        calls = []

        def gen(base_url, query, token, sink=None):
            calls.append(query)
            sink.append((0.0, {"stage": "generation", "status": "running",
                               "workflow_id": WF}))
            raise requests.ConnectionError("gone")

        with patch("bench.sweep.stream_generate_and_run", side_effect=gen), \
             patch("bench.sweep.capture_evidence", side_effect=rig._capture):
            sw.run_sweep([PASS_Q, FAIL_Q], 1, "http://x", None, None, tmp_path)
        rows = [json.loads(ln) for ln in
                (tmp_path / "results.jsonl").read_text().splitlines()]
        assert len(calls) == 2
        assert [(r["workflow_id"], r["outcome"], r["generation_status"])
                for r in rows] == [(WF, "generation_failed", "error")] * 2

    def test_checker_error_keeps_row(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events())
        with patch("bench.sweep.hollow_shapes", side_effect=RuntimeError("secret q")):
            rows = rig.run([PASS_Q, PASS_Q])
        assert [r["hollow"] for r in rows] == [["CHECKER_ERROR"]] * 2
        assert all(r["outcome"] == "handled" for r in rows)

    def test_checker_error_warning_names_only_the_type(self, tmp_path, capsys):
        rig = _Rig(tmp_path, _gen_events(), _judge_events())
        with patch("bench.sweep.hollow_shapes", side_effect=RuntimeError("secret q")):
            rig.run([PASS_Q])
        err = capsys.readouterr().err
        assert "RuntimeError" in err and "secret q" not in err

    def test_judge_stream_death_is_judge_error_and_continues(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), requests.ConnectionError("gone"))
        rows = rig.run([PASS_Q], repeats=2)
        assert [r["outcome"] for r in rows] == ["judge_error", "judge_error"]
        assert [r["repeat"] for r in rows] == [1, 2]

    def test_file_order_with_repeats_inside_query(self, tmp_path):
        rows = _Rig(tmp_path, _gen_events("failed"), error_message="BUG").run(
            [FAIL_Q, PASS_Q], repeats=2)
        assert [(r["query_id"], r["repeat"]) for r in rows] == [
            ("f1", 1), ("f1", 2), ("p1", 1), ("p1", 2)]

    def test_evidence_dirs(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events(),
                   judge_error_message="judge said no")
        (row,) = rig.run([PASS_Q])
        assert rig.captures == [(WF, tmp_path / "runs" / WF),
                                (JWF, tmp_path / "runs" / WF / "judge")]
        assert not (tmp_path / "bench").exists()
        assert row["judge_error_message"] == "judge said no"

    def test_judge_failed_tag_from_judge_run(self, tmp_path):
        msg = ("Timeout 10000ms exceeded.\n=========== logs ===========\n"
               "waiting for locator('h1')")
        rig = _Rig(tmp_path, _gen_events(), _judge_events("failed"),
                   judge_error_message=msg)
        (row,) = rig.run([PASS_Q])
        assert row["outcome"] == "judge_failed"
        assert row["error_message"] is None
        assert row["judge_error_message"] == msg
        assert row["tag"] == "locator_dead"

    def test_judge_error_tag_from_judge_run(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events(), _judge_events("error"),
                   judge_error_message="x should be y")
        (row,) = rig.run([PASS_Q])
        assert (row["outcome"], row["tag"]) == ("judge_error", "assertion")

    def test_other_outcomes_tag_from_generation_message(self, tmp_path):
        rig = _Rig(tmp_path, _gen_events("failed"), error_message="x should be y",
                   judge_error_message="Timeout 1ms exceeded")
        (row,) = rig.run([PASS_Q])
        assert (row["outcome"], row["tag"]) == ("test_failed", "assertion")
        assert row["judge_error_message"] is None

    def test_results_file_holds_one_row_per_run(self, tmp_path):
        rows = _Rig(tmp_path, _gen_events("failed"), error_message="BUG").run([FAIL_Q], repeats=2)
        assert len(rows) == 2
        assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2

    def _run_with_empty_capture(self, tmp_path, second_gen):
        """Two queries; the second one's workflow is absent from the capture database."""
        gens = iter([_gen_events("failed", wf=WF), second_gen])

        def gen(base_url, query, token, sink=None):
            sink.extend(next(gens))

        def capture(conn, wf, dest=None):
            dest.mkdir(parents=True, exist_ok=True)
            rows = [{"robot_code": ROBOT, "error_message": "BUG"}] if wf == WF else []
            (dest / "test_runs.json").write_text(json.dumps(rows), encoding="utf-8")
            return True

        with patch("bench.sweep.stream_generate_and_run", side_effect=gen), \
             patch("bench.sweep.capture_evidence", side_effect=capture):
            sw.run_sweep([FAIL_Q, PASS_Q], 1, "http://x", None, None, tmp_path)

    def test_complete_run_missing_from_capture_database_stops_the_sweep(self, tmp_path):
        other = "cccccccc-0000-0000-0000-000000000003"
        with pytest.raises(SystemExit) as stop:
            self._run_with_empty_capture(tmp_path, _gen_events(wf=other))
        assert other in str(stop.value) and "SWEEP_DATABASE_URL" in str(stop.value)
        rows = [json.loads(ln) for ln in
                (tmp_path / "results.jsonl").read_text().splitlines()]
        assert [r["query_id"] for r in rows] == ["f1"]  # earlier row stays flushed

    def test_incomplete_run_missing_from_capture_database_still_writes_row(self, tmp_path):
        self._run_with_empty_capture(
            tmp_path, _gen_events(gen="error", test_status=None)[:1])
        rows = [json.loads(ln) for ln in
                (tmp_path / "results.jsonl").read_text().splitlines()]
        assert [(r["query_id"], r["outcome"]) for r in rows] == [
            ("f1", "handled"), ("p1", "generation_failed")]

    def test_sealed_query_prints_no_id_text_or_outcome(self, tmp_path, capsys):
        _Rig(tmp_path, _gen_events("failed"), error_message="BUG").run([FAIL_Q])
        out = capsys.readouterr()
        text = out.out + out.err
        assert "(1/1) sealed" in text
        for leak in ("f1", "planted bug page", "handled"):
            assert leak not in text

    def test_dev_query_prints_id_text_and_outcome(self, tmp_path, capsys):
        dev_q = SweepQuery("d1", "dev query text", "local", "dev", "fail", None, "BUG")
        _Rig(tmp_path, _gen_events("failed"), error_message="BUG").run([dev_q])
        text = capsys.readouterr().out
        assert "d1" in text and "dev query text" in text and "handled" in text


class TestWarningsKeepSealedIdsOffTheConsole:
    """The three _warn paths name the run like the _log lines: dev id + repeat, sealed `(n/total) sealed`."""

    DEV_Q = SweepQuery("d1", "dev query text", "local", "dev", "pass", JUDGE, None)
    SEALED_Q = SweepQuery("s1", "sealed text", "local", "sealed", "pass", JUDGE, None)

    def _paths(self, tmp_path, q):
        gen_dies = _Rig(tmp_path / "a", requests.ConnectionError("gone"))
        judge_dies = _Rig(tmp_path / "b", _gen_events(), requests.ConnectionError("gone"))
        checker = _Rig(tmp_path / "c", _gen_events(), _judge_events())
        gen_dies.run([q])
        judge_dies.run([q])
        with patch("bench.sweep.hollow_shapes", side_effect=RuntimeError("x")):
            checker.run([q])

    def test_sealed_id_never_reaches_the_console(self, tmp_path, capsys):
        self._paths(tmp_path, self.SEALED_Q)
        out = capsys.readouterr()
        assert "s1" not in out.out + out.err
        assert out.err.count("(1/1) sealed:") == 3  # all three warnings fired, label only

    def test_dev_id_still_reaches_the_console(self, tmp_path, capsys):
        self._paths(tmp_path, self.DEV_Q)
        err = capsys.readouterr().err
        assert err.count("d1 repeat 1") == 3


class TestMain:
    def _argv(self, tmp_path, *extra):
        qf = tmp_path / "q.json"
        qf.write_text(json.dumps({"queries": [
            {"id": "f1", "query": "q", "source": "local", "half": "sealed",
             "expect": "fail", "bug_signature": "BUG"},
            {"id": "f2", "query": "q", "source": "local", "half": "sealed",
             "expect": "fail", "bug_signature": "BUG"}]}), encoding="utf-8")
        return ["--queries", str(qf), "--out-dir", str(tmp_path / "out"), *extra]

    def test_production_url_exits_nonzero_before_any_run(self, tmp_path):
        with patch.dict(os.environ, {"SWEEP_DATABASE_URL": "postgresql://x"}), \
             patch("bench.sweep.fetch_health",
                   return_value={"pins": {"optimization_enabled": True}}), \
             patch("bench.sweep.psycopg.connect") as connect, \
             patch("bench.sweep.run_sweep") as run:
            rc = sw.main(self._argv(tmp_path, "--base-url", "http://127.0.0.1:5000"))
        assert rc != 0
        run.assert_not_called()
        connect.assert_not_called()

    def test_database_url_is_required(self, tmp_path):
        with patch.dict(os.environ), patch("bench.sweep.run_sweep") as run:
            os.environ.pop("SWEEP_DATABASE_URL", None)
            assert sw.main(self._argv(tmp_path)) != 0
        run.assert_not_called()

    def test_only_filters_by_id_in_file_order(self, tmp_path):
        env = {"SWEEP_DATABASE_URL": "postgresql://x", "SWEEP_TOKEN": "tok"}
        with patch.dict(os.environ, env), \
             patch("bench.sweep.fetch_health",
                   return_value={"pins": {"optimization_enabled": True}}), \
             patch("bench.sweep.psycopg.connect"), \
             patch("bench.sweep.run_sweep") as run:
            rc = sw.main(self._argv(tmp_path, "--only", "f2", "--repeats", "2"))
        assert rc == 0
        queries, repeats, base_url, token = run.call_args.args[:4]
        assert [q.id for q in queries] == ["f2"]
        assert (repeats, base_url, token) == (2, "http://127.0.0.1:5002", "tok")

    def test_unknown_only_id_exits_nonzero(self, tmp_path):
        with patch.dict(os.environ, {"SWEEP_DATABASE_URL": "postgresql://x"}), \
             patch("bench.sweep.run_sweep") as run:
            assert sw.main(self._argv(tmp_path, "--only", "nope")) != 0
        run.assert_not_called()


class TestResultsFileGuard:
    def test_refuses_a_non_empty_results_file_before_any_http_call(self, tmp_path):
        (tmp_path / "results.jsonl").write_text('{"query_id": "q"}\n', encoding="utf-8")
        with patch("bench.sweep.stream_generate_and_run") as gen, \
                pytest.raises(SystemExit, match="results.jsonl.*--out-dir"):
            sw.run_sweep([PASS_Q], 1, "http://x", None, None, tmp_path)
        gen.assert_not_called()
        assert (tmp_path / "results.jsonl").read_text(encoding="utf-8") == '{"query_id": "q"}\n'

    def test_empty_or_absent_results_file_starts(self, tmp_path):
        (tmp_path / "results.jsonl").write_text("", encoding="utf-8")
        (row,) = _Rig(tmp_path, _gen_events(), _judge_events()).run([PASS_Q])
        assert row["outcome"] == "handled"
        (row,) = _Rig(tmp_path / "fresh", _gen_events(), _judge_events()).run([PASS_Q])
        assert row["outcome"] == "handled"
