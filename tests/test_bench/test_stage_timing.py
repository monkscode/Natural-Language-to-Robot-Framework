"""Stage-duration mapping from client-timestamped SSE events.

The bench runner records (client_time, parsed_event) for every SSE `data:` line
of a /generate-and-run stream. bench_lib.stage_durations maps those onto the
fixed progress checkpoints pushed by progress_events.py / workflow_service.py:

    plan     = t(progress 20) - t(progress 5)
    identify = t(progress 60) - t(progress 22)
    assemble = t(progress 80) - t(progress 62)
    dryrun   = t(progress 100) - t(progress 80)
    exec     = t(last execution event) - t(first execution event)

Precision is +/-1s (the SSE drain loop sleeps 1s between polls) — accepted in
the approved Task-2 design.
"""

from bench.bench_lib import (
    count_dryrun_repairs,
    extract_run_identity,
    stage_durations,
)


def _gen(progress=None, message="", status="running", **extra):
    ev = {"stage": "generation", "status": status, "message": message}
    if progress is not None:
        ev["progress"] = progress
    ev.update(extra)
    return ev


def _exec(message="", status="running", **extra):
    ev = {"stage": "execution", "status": status, "message": message}
    ev.update(extra)
    return ev


def _full_timeline():
    """A realistic generate-and-run stream with one dryrun repair."""
    return [
        (0.0, _gen(5, "🧠 Analyzing your test requirements...")),
        (1.0, _gen(8, "📋 Breaking down test into steps...")),
        (4.0, _gen(20, "✅ Test steps planned successfully")),
        (5.0, _gen(22, "🔍 Scanning webpage for interactive elements...")),
        (12.0, _gen(30, "🌐 Navigating to website and detecting elements...")),
        (30.0, _gen(55, "📍 Found 12 elements on the page")),
        (35.0, _gen(60, "✅ All page elements identified")),
        (36.0, _gen(62, "⚡ Writing test automation code...")),
        (38.0, _gen(65, "💻 Generating test script...")),
        (50.0, _gen(80, "✅ Test code assembled")),
        (51.0, _gen(None, "🔬 Preparing verification environment...")),
        (52.0, _gen(88, "🔬 Verifying generated test...")),
        (55.0, _gen(92, "🔧 Fixing test code...")),
        (58.0, _gen(None, "🔬 Verifying generated test...")),
        (60.0, _gen(100, "✅ Test generation complete")),
        (61.0, _gen(None, "✅ Test generation complete.", status="complete",
                    robot_code="*** Test Cases ***", workflow_id="wf-123")),
        (62.0, _exec("Preparing execution environment...")),
        (90.0, _exec("", status="success", test_status="passed")),
    ]


class TestStageDurations:
    def test_full_timeline_maps_all_stages(self):
        d = stage_durations(_full_timeline())
        assert d["plan_s"] == 4.0
        assert d["identify_s"] == 30.0
        assert d["assemble_s"] == 14.0
        assert d["dryrun_s"] == 10.0
        assert d["exec_s"] == 28.0

    def test_total_is_first_to_last_event(self):
        d = stage_durations(_full_timeline())
        assert d["total_s"] == 90.0

    def test_missing_execution_stage_yields_none(self):
        gen_only = [(t, e) for t, e in _full_timeline() if e["stage"] == "generation"]
        d = stage_durations(gen_only)
        assert d["exec_s"] is None
        assert d["plan_s"] == 4.0

    def test_missing_checkpoint_yields_none_for_that_stage_only(self):
        # Drop the progress-62 event: assemble start unknown.
        events = [(t, e) for t, e in _full_timeline() if e.get("progress") != 62]
        d = stage_durations(events)
        assert d["assemble_s"] is None
        assert d["plan_s"] == 4.0
        assert d["dryrun_s"] == 10.0

    def test_first_occurrence_of_a_checkpoint_wins(self):
        # A duplicate progress-20 later must not move the plan boundary.
        events = _full_timeline() + [(95.0, _gen(20, "late duplicate"))]
        d = stage_durations(events)
        assert d["plan_s"] == 4.0

    def test_empty_stream(self):
        d = stage_durations([])
        assert d == {
            "plan_s": None, "identify_s": None, "assemble_s": None,
            "dryrun_s": None, "exec_s": None, "total_s": None,
        }


class TestDryrunRepairs:
    def test_counts_fixing_events(self):
        assert count_dryrun_repairs(_full_timeline()) == 1

    def test_two_repairs(self):
        events = _full_timeline() + [(59.0, _gen(None, "🔧 Fixing test code..."))]
        assert count_dryrun_repairs(events) == 2

    def test_zero_when_no_repairs(self):
        events = [(t, e) for t, e in _full_timeline()
                  if "🔧" not in e.get("message", "")]
        assert count_dryrun_repairs(events) == 0


class TestRunIdentity:
    def test_extracts_workflow_id_and_test_status(self):
        ident = extract_run_identity(_full_timeline())
        assert ident["workflow_id"] == "wf-123"
        assert ident["test_status"] == "passed"
        assert ident["generation_status"] == "complete"
        assert ident["dryrun_status"] is None

    def test_dryrun_status_attached_when_gate_did_not_pass(self):
        events = list(_full_timeline())
        t, complete = events[-3]
        complete = dict(complete)
        complete["dryrun_status"] = "unverified"
        events[-3] = (t, complete)
        ident = extract_run_identity(events)
        assert ident["dryrun_status"] == "unverified"

    def test_generation_error(self):
        events = [
            (0.0, _gen(5, "🧠 Analyzing your test requirements...")),
            (3.0, _gen(None, "An error occurred: boom", status="error")),
        ]
        ident = extract_run_identity(events)
        assert ident["generation_status"] == "error"
        assert ident["workflow_id"] is None
        assert ident["test_status"] is None
