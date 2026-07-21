"""Stage-duration mapping from client-timestamped SSE events.

The bench runner records (client_time, parsed_event) for every SSE `data:`
line of a /generate-and-run stream. Observed reality of the live stream (see
Task-2 Decision Log): the task-COMPLETION checkpoints (20/60/80) are
unreliable — CrewAI fires the next task's start event before the completion
push, and the forward-only progress guard then discards the completion. The
reliable anchors are the stage STARTS (5 plan, 22 identify, 62 assemble), the
dryrun-gate start message ("🔬 Preparing verification environment...", no
progress value), and the terminal progress 100. So:

    plan     = t(progress 22)  - t(progress 5)
    identify = t(progress 62)  - t(progress 22)
    assemble = t(dryrun-start message) - t(progress 62)
               fallback: t(progress 100) - t(progress 62)  [gate skipped]
    dryrun   = t(progress 100) - t(dryrun-start message); None when gate absent
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
    """Mirrors a real captured /generate-and-run stream (one dryrun repair).

    Note: NO 20/60/80 completion events — the live stream drops them.
    """
    return [
        (0.0, _gen(0, "🎬 Starting test generation...")),
        (0.5, _gen(3, "🧠 Initializing AI agents...")),
        (1.0, _gen(5, "🧠 Analyzing your test requirements...")),
        (2.0, _gen(8, "📋 Breaking down test into steps...")),
        (3.0, _gen(22, "🔍 Scanning webpage for interactive elements...")),
        (5.0, _gen(30, "🌐 Navigating to website and detecting elements...")),
        (35.0, _gen(55, "📍 Found 2 elements on the page")),
        (39.0, _gen(62, "⚡ Writing test automation code...")),
        (40.0, _gen(65, "💻 Generating test script...")),
        (41.0, _gen(None, "🔬 Preparing verification environment...")),
        (42.0, _gen(88, "🔬 Verifying generated test...")),
        (55.0, _gen(92, "🔧 Fixing test code...")),
        (58.0, _gen(None, "🔬 Verifying generated test...")),
        (60.0, _gen(100, "🎉 Test generation complete")),
        (61.0, _gen(None, "🎉 Test generation complete.", status="complete",
                    robot_code="*** Test Cases ***", workflow_id="wf-123")),
        (62.0, _exec("Preparing execution environment...")),
        (90.0, _exec("", status="success", test_status="passed")),
    ]


class TestStageDurations:
    def test_full_timeline_maps_all_stages(self):
        d = stage_durations(_full_timeline())
        assert d["plan_s"] == 2.0       # t(22)=3.0 - t(5)=1.0
        assert d["identify_s"] == 36.0  # t(62)=39.0 - t(22)=3.0
        assert d["assemble_s"] == 2.0   # t(dryrun-start)=41.0 - t(62)=39.0
        assert d["dryrun_s"] == 19.0    # t(100)=60.0 - t(dryrun-start)=41.0
        assert d["exec_s"] == 28.0      # 90.0 - 62.0

    def test_total_is_first_to_last_event(self):
        d = stage_durations(_full_timeline())
        assert d["total_s"] == 90.0

    def test_gate_skipped_assemble_falls_back_to_100(self):
        # DRYRUN_ENABLED=false: no dryrun messages at all; 100 right after 65.
        events = [
            (1.0, _gen(5, "🧠 Analyzing your test requirements...")),
            (3.0, _gen(22, "🔍 Scanning webpage for interactive elements...")),
            (39.0, _gen(62, "⚡ Writing test automation code...")),
            (45.0, _gen(100, "🎉 Test generation complete")),
        ]
        d = stage_durations(events)
        assert d["assemble_s"] == 6.0
        assert d["dryrun_s"] is None

    def test_missing_execution_stage_yields_none(self):
        gen_only = [(t, e) for t, e in _full_timeline() if e["stage"] == "generation"]
        d = stage_durations(gen_only)
        assert d["exec_s"] is None
        assert d["plan_s"] == 2.0

    def test_missing_checkpoint_yields_none_for_dependent_stages_only(self):
        # Drop the progress-62 event: identify end + assemble start unknown.
        events = [(t, e) for t, e in _full_timeline() if e.get("progress") != 62]
        d = stage_durations(events)
        assert d["identify_s"] is None
        assert d["assemble_s"] is None
        assert d["plan_s"] == 2.0
        assert d["dryrun_s"] == 19.0

    def test_first_occurrence_of_a_checkpoint_wins(self):
        # A duplicate progress-22 later must not move the plan boundary.
        events = _full_timeline() + [(95.0, _gen(22, "late duplicate"))]
        d = stage_durations(events)
        assert d["plan_s"] == 2.0

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

    def test_generation_error_carries_workflow_id_when_present(self):
        """Failed runs must still be detachable — their pre-failure LLM calls
        are already in llm_traces, so the id must survive the error event."""
        events = [
            (0.0, _gen(5, "🧠 Analyzing your test requirements...")),
            (3.0, _gen(None, "An error occurred: boom", status="error",
                       workflow_id="wf-err")),
        ]
        ident = extract_run_identity(events)
        assert ident["generation_status"] == "error"
        assert ident["workflow_id"] == "wf-err"
