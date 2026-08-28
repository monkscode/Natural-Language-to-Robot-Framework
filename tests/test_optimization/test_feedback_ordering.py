"""T7 — feedback is ordered behind the run's own learning write.

/api/feedback used to look the execution record up with a 300ms retry budget
(`_get_with_retry`) sized against write-queue drain lag, not against the store
not having been SUBMITTED yet.  The record is written by the learning writer
thread, so a user who clicks Submit the moment the result appears could lose
their correction outright: triage ran, engines were never routed, and the
endpoint still answered a plain success.

The mechanism is a bounded poll of the only party that knows whether the row
exists — Postgres.  Not a latch: a process-local dict is wrong under TTL
eviction, restart, replicas, and the failed-re-run IntegrityError path.

Pinned here: the poll's shape, the four outcomes it feeds back to the caller,
and the rule that keeps the raw feedback text durable even when the poll gives
up.
"""
from unittest.mock import MagicMock, patch

from src.backend.crew_ai.optimization.feedback_loop import (
    FeedbackLoop,
    LearningMetricsTracker,
    ContradictionDetector,
    _get_with_retry,
)
from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker

_SLEEP = "src.backend.crew_ai.optimization.feedback_loop.time.sleep"
_FIRE_CONFLICT = (
    "src.backend.crew_ai.optimization.conflict_detection.fire_conflict_detection"
)


class _RecordingWriteQueue:
    """Runs each job immediately, but remembers what was submitted."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(getattr(fn, "__name__", repr(fn)))
        fn(*args, **kwargs)

    def submit_and_wait(self, fn, *args, timeout=None, **kwargs):
        self.submitted.append(getattr(fn, "__name__", repr(fn)))
        fn(*args, **kwargs)
        return ("ok", None)


class _DeferredWriteQueue:
    """FIFO, drained on demand — the real queue's ordering without its thread."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def submit_and_wait(self, fn, *args, timeout=None, **kwargs):
        # Not reached by any test in this file today (every test that uses
        # this double never gets past the poll to Step 4), so there is no
        # deferral behaviour to preserve here — run inline like the other
        # doubles' submit_and_wait, per the Step 1d default.
        fn(*args, **kwargs)
        return ("ok", None)

    def drain(self):
        jobs, self.jobs = self.jobs, []
        for fn, args, kwargs in jobs:
            fn(*args, **kwargs)


class _VerdictWriteQueue:
    """submit() runs inline; submit_and_wait() returns a scripted verdict
    without running the job at all.

    Exists to unit-test process_user_feedback's OUTCOME MAPPING (Step 1b) in
    isolation from the real timing/threading mechanics of submit_and_wait
    itself, which test_write_queue_wait.py already covers against the real
    LearningWriteQueue.
    """

    def __init__(self, verdict):
        self._verdict = verdict
        self.waited = []

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def submit_and_wait(self, fn, *args, timeout=None, **kwargs):
        self.waited.append((fn, args, kwargs))
        return self._verdict


class _MockEngine:
    def __init__(self):
        self.learn_calls = []
        self.feedback_calls = []

    def learn(self, record):
        self.learn_calls.append(record)

    def learn_from_feedback(self, record, triage):
        self.feedback_calls.append((record, triage))

    def get_hints(self, query, domain=None):
        return []

    def get_stats(self):
        return {"total_rules": 0, "active_rules": 0}


class _StubNLEngine(_MockEngine):
    def __init__(self):
        super().__init__()
        self.triaged = []

    def process_feedback(self, workflow_id, feedback_text, feedback_type,
                         error_message=None):
        self.triaged.append(workflow_id)
        return {
            "category": "locator", "specific_type": "wrong_selector",
            "confidence": 0.9, "taxonomy_code": "L1", "matched_patterns": [],
        }


class _MockFailureAnalyzer:
    def analyze(self, output_xml_path=None, user_query="", robot_code="",
                exit_code=None):
        return None


def _build_loop(conn, write_queue=None):
    em = conn._em
    fl = FeedbackLoop(
        execution_memory=em,
        failure_analyzer=_MockFailureAnalyzer(),
        structural_engine=_MockEngine(),
        keyword_engine=_MockEngine(),
        anti_pattern_engine=_MockEngine(),
        pattern_learner=None,
        metrics_tracker=LearningMetricsTracker(conn),
        contradiction_detector=ContradictionDetector(conn),
        write_queue=write_queue or _RecordingWriteQueue(),
        circuit_breaker=LearningCircuitBreaker(),
    )
    fl.nl_engine = _StubNLEngine()
    return fl, em


def _store(fl, workflow_id, *, status="failed", code="v1", org_id="org-ordering"):
    # org_id defaults non-None: these records feed the NL-engine routing step,
    # and an org-less record now reports "no_org" and skips that write (T9 /
    # Task 1 mode a) — a case this file's poll/ordering tests are not about.
    # test_org_less_record_reports_no_org... below is the one that sets None
    # on purpose.
    fl.process_execution(
        workflow_id=workflow_id, user_query="click the login button",
        url="https://example.com", robot_code=code, test_status=status,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# The poll itself
# ---------------------------------------------------------------------------

class TestBoundedPoll:
    def test_a_record_that_lands_during_the_poll_is_found(self, in_memory_db):
        """The window the poll exists for: the store was submitted, the writer
        thread has not drained it yet."""
        fl, em = _build_loop(in_memory_db)
        sleeps = []

        def sleep_then_store(seconds):
            if not sleeps:
                _store(fl, "wf-late-store")
            sleeps.append(seconds)

        with patch(_SLEEP, sleep_then_store), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-late-store", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "processed"
        assert len(sleeps) == 1, "found on the second attempt, so exactly one sleep"
        assert fl.nl_engine.feedback_calls, "engines must be routed once the record exists"

    def test_an_existing_record_costs_no_sleeps(self, in_memory_db):
        fl, em = _build_loop(in_memory_db)
        _store(fl, "wf-present")

        sleeps = []
        with patch(_SLEEP, sleeps.append), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-present", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "processed"
        assert sleeps == [], "a record already on disk must not delay the response"

    def test_the_poll_is_bounded_at_three_seconds(self, in_memory_db):
        """100ms doubling, capped at 500ms a step, 3s in total — and the last
        step is trimmed so the budget is exact rather than approximate."""
        fl, em = _build_loop(in_memory_db)

        sleeps = []
        with patch(_SLEEP, sleeps.append):
            record, outcome = _get_with_retry(em, "wf-never")

        assert record is None
        assert outcome == "no_record"
        assert sleeps == [0.1, 0.2, 0.4, 0.5, 0.5, 0.5, 0.5, 0.3]
        assert round(sum(sleeps), 6) == 3.0

    def test_a_zero_step_cannot_spin_forever(self):
        """The loop's only exit is the elapsed budget, so a zero-length step
        would read Postgres in a tight loop and never return.  The step is
        clamped to at least 1ms so progress is structural, not argument-
        dependent."""
        calls = []

        def get(_workflow_id):
            calls.append(1)
            if len(calls) > 50:
                raise AssertionError("the poll never terminated")
            return None

        em = MagicMock()
        em.get.side_effect = get

        with patch(_SLEEP, lambda _s: None):
            record, outcome = _get_with_retry(
                em, "wf-zero", ceiling_ms=10, base_ms=0)

        assert record is None
        assert outcome == "no_record"
        assert len(calls) == 5   # 1 + 2 + 4 + 3 ms of budget, then the last read


# ---------------------------------------------------------------------------
# The four outcomes
# ---------------------------------------------------------------------------

class TestOutcomes:
    def test_breaker_open_reports_learning_paused_and_writes_nothing(self, in_memory_db):
        """Today this answers a plain success with the text discarded.  The
        outcome is read from the EXISTING is_enabled() check — a second call
        would transition breaker state and consume the HALF_OPEN probe."""
        queue = _RecordingWriteQueue()
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        _store(fl, "wf-paused")
        queue.submitted.clear()
        for _ in range(20):
            fl.circuit_breaker.record_error(RuntimeError("boom"))
        assert fl.circuit_breaker.is_enabled() is False

        with patch.object(em, "get", side_effect=AssertionError("polled anyway")):
            triage = fl.process_user_feedback(
                "wf-paused", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "learning_paused"
        assert queue.submitted == [], "a paused breaker must write nothing"

    def test_the_breaker_is_asked_exactly_once(self, in_memory_db):
        """is_enabled() transitions CLOSED->OPEN and OPEN->HALF_OPEN, and
        HALF_OPEN admits exactly one probe. Deriving the outcome from a SECOND
        call would consume that probe and bounce the real check, so the outcome
        has to come from the one call the request already makes."""
        fl, em = _build_loop(in_memory_db)
        _store(fl, "wf-probe")

        with patch.object(fl.circuit_breaker, "is_enabled",
                          wraps=fl.circuit_breaker.is_enabled) as spy, \
                patch(_FIRE_CONFLICT):
            fl.process_user_feedback(
                "wf-probe", "wrong login locator", "completely_wrong")

        assert spy.call_count == 1

    def test_a_record_that_never_arrives_reports_no_record(self, in_memory_db):
        fl, em = _build_loop(in_memory_db)

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-absent", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "no_record"
        assert triage["category"] == "locator", "triage still runs without a record"
        assert fl.nl_engine.feedback_calls == [], "no record means no engine routing"

    def test_the_engines_triage_dict_is_not_stamped_after_they_receive_it(self, in_memory_db):
        """The triage dict is handed to the writer thread at the routing step
        and is that thread's to read from then on.  Adding the outcome to it in
        place is a cross-thread mutation whose visibility depends on when the
        queue drains — the caller gets a copy instead."""
        fl, em = _build_loop(in_memory_db)
        _store(fl, "wf-shared")

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            returned = fl.process_user_feedback(
                "wf-shared", "wrong login locator", "completely_wrong")

        engine_triage = fl.nl_engine.feedback_calls[0][1]
        assert returned["outcome"] == "processed"
        assert "outcome" not in engine_triage

    def test_an_internal_error_reports_error(self, in_memory_db):
        """The outer except wraps every internal failure as success today."""
        fl, em = _build_loop(in_memory_db)

        with patch.object(em, "get", side_effect=RuntimeError("db down")):
            triage = fl.process_user_feedback(
                "wf-boom", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "error"

    def test_the_failed_re_run_finds_its_v1_record_immediately(self, in_memory_db):
        """S1 corner 4.  Generate -> run -> fail -> feedback -> edit -> run ->
        fail: the second store raises IntegrityError by contract and the queue
        swallows it, so nothing ever signals 'stored' for the second run.  The
        v1 row is right there, and the feedback belongs to it."""
        fl, em = _build_loop(in_memory_db)
        _store(fl, "wf-rerun", status="failed", code="v1 code")
        _store(fl, "wf-rerun", status="failed", code="v2 code")  # IntegrityError
        assert em.get("wf-rerun").robot_code == "v1 code"

        sleeps = []
        with patch(_SLEEP, sleeps.append), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-rerun", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "processed"
        assert sleeps == []
        assert fl.nl_engine.feedback_calls[0][0].robot_code == "v1 code"


class TestNLEngineVerdictOutcomes:
    """Task 1 (feedback integrity remediation): `outcome` now reflects the
    NL engine write's real verdict, not just whether the execution record was
    found. Only the NL engine's learn_from_feedback is awaited — structural,
    keyword and anti-pattern engines define no override (LearningEngine's
    no-op `pass`), so their write can never speak for "the correction was
    stored"."""

    def test_org_less_record_reports_no_org_and_skips_the_nl_write(self, in_memory_db):
        queue = _RecordingWriteQueue()
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        _store(fl, "wf-no-org", org_id=None)

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-no-org", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "no_org"
        assert fl.nl_engine.feedback_calls == [], (
            "the NL job must never be submitted for an org-less record"
        )
        assert fl.structural_engine.feedback_calls, (
            "the other three engines still route normally — only the NL "
            "engine's write is gated on org_id"
        )

    def test_nl_engine_write_failure_reports_error(self, in_memory_db):
        exc = RuntimeError("writer thread blew up")
        queue = _VerdictWriteQueue(("failed", exc))
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        _store(fl, "wf-nl-fail")

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-nl-fail", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "error"
        assert queue.waited, "the NL job must have been submitted via submit_and_wait"

    def test_nl_engine_write_timeout_reports_queued(self, in_memory_db):
        queue = _VerdictWriteQueue(("timeout", None))
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        _store(fl, "wf-nl-timeout")

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-nl-timeout", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "queued"

    def test_nl_engine_write_ok_keeps_processed(self, in_memory_db):
        queue = _VerdictWriteQueue(("ok", None))
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        _store(fl, "wf-nl-ok")

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-nl-ok", "wrong login locator", "completely_wrong")

        assert triage["outcome"] == "processed"


# ---------------------------------------------------------------------------
# S2 — the raw text stays durable even when the poll gives up
# ---------------------------------------------------------------------------

class TestTheTextIsDurable:
    """The UPDATE that stores the raw text is a silent no-op on a missing row,
    so WHEN it is queued decides whether the text survives.  Submitted before
    the lookup, it could be queued ahead of the store that creates the row, and
    the column then stayed NULL forever.  Submitted after the poll, the store
    goes in first and FIFO delivers the text — even when the poll itself timed
    out and the caller was told `no_record`."""

    def test_a_store_submitted_during_the_poll_still_receives_the_text(self, in_memory_db):
        """The case that separates the two orderings.  Feedback arrives BEFORE
        the run's own store is submitted; the store is submitted while the poll
        is still running.  Only because the UPDATE waits for the poll does it
        land behind the store in the queue."""
        queue = _DeferredWriteQueue()
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        submitted = []

        def submit_store_mid_poll(_seconds):
            if not submitted:
                _store(fl, "wf-race")   # SUBMITTED, still not drained
                submitted.append(True)

        with patch(_SLEEP, submit_store_mid_poll), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-race", "wrong login locator", "completely_wrong")

        # Nothing has drained, so the poll never sees the row and gives up.
        assert triage["outcome"] == "no_record"
        assert submitted, "the store must have been submitted during the poll"

        queue.drain()

        record = em.get("wf-race")
        assert record is not None
        assert record.user_feedback == "wrong login locator"
        assert record.user_feedback_type == "completely_wrong"

    def test_the_text_lands_when_the_queue_drains_after_the_ceiling(self, in_memory_db):
        """The store was queued before the feedback arrived, but the writer had
        not reached it within the poll's budget.  `no_record` is reported and
        the text is still stored — never skipped on that outcome."""
        queue = _DeferredWriteQueue()
        fl, em = _build_loop(in_memory_db, write_queue=queue)
        _store(fl, "wf-slow-drain")          # store QUEUED, not yet run
        assert em.get("wf-slow-drain") is None

        with patch(_SLEEP, lambda _s: None), patch(_FIRE_CONFLICT):
            triage = fl.process_user_feedback(
                "wf-slow-drain", "wrong login locator", "completely_wrong")
        assert triage["outcome"] == "no_record"

        queue.drain()

        record = em.get("wf-slow-drain")
        assert record is not None
        assert record.user_feedback == "wrong login locator"
        assert record.user_feedback_type == "completely_wrong"
