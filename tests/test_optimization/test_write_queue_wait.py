"""LearningWriteQueue.submit_and_wait — the writer thread's real verdict.

/api/feedback used to answer "processed" for four post-lookup failures in
which nothing was stored (org-less write refused, nl_engine is None,
write_queue.submit raises, learn_from_feedback raises on the writer thread) —
`submit` is fire-and-forget by design, so none of that ever reached the
caller. This is the mechanism that lets /api/feedback (a human-speed endpoint,
not the pipeline) await a real verdict without breaking the pipeline's own
non-blocking `submit`.

All tests run against the REAL LearningWriteQueue, not a test double, per the
task brief ("proven against the real drain loop").

Ambient thread-name note: tests/test_optimization/conftest.py renames the
pytest thread to WRITER_THREAD_NAME (autouse), so that engine write methods'
`_assert_writer_thread` guard passes when a synchronous test double calls
them inline. That would make submit_and_wait's own re-entrancy guard fire on
every call made directly from a test body here, since the guard cannot tell
"a test pretending to be the writer thread" from "the real writer thread
calling back into itself". This file overrides that fixture (by name) to a
no-op so its own thread keeps its real name — the one test that wants the
re-entrancy guard to fire drives it from inside a job actually running on the
real writer thread instead.
"""
import threading
import time

import pytest

from src.backend.crew_ai.optimization.learning_config import (
    LearningWriteQueue,
    WRITER_THREAD_NAME,
)


@pytest.fixture(autouse=True)
def _rename_test_thread_to_writer():
    """Override the package-wide autouse fixture of the same name (see the
    module docstring): this file's tests call submit_and_wait directly from
    the pytest thread, which must NOT be named WRITER_THREAD_NAME here."""
    yield


class TestHappyPath:
    def test_a_job_that_succeeds_returns_ok(self):
        queue = LearningWriteQueue()
        calls = []
        try:
            verdict = queue.submit_and_wait(calls.append, "did-run")
        finally:
            queue.shutdown()
        # The None is `list.append`'s OWN return value, not a constant this
        # branch supplies — see TestTheWriteFunctionsAnswer below, which is
        # what keeps this assertion from passing for the wrong reason.
        assert verdict == ("ok", None)
        assert calls == ["did-run"]


class TestTheWriteFunctionsAnswer:
    """The second element on the "ok" branch is the write function's return.

    `_wrapped` used to call `write_fn(...)` and drop the result on the floor,
    so a write that ran and deliberately changed nothing was indistinguishable
    from one that stored a correction. The one awaited caller
    (feedback_loop.process_user_feedback) turned that into "Thanks - your
    feedback helps the system learn" for a resubmission the engine's run-level
    gate had refused on a switched-off hint.

    Every assertion in this file that reads ("ok", None) does so because its
    write function returns None; these two pin that the None is carried, not
    manufactured.
    """

    def test_the_return_value_is_carried_back_to_the_caller(self):
        queue = LearningWriteQueue()
        try:
            verdict = queue.submit_and_wait(lambda: "gated_inactive_hint")
        finally:
            queue.shutdown()

        assert verdict == ("ok", "gated_inactive_hint")

    def test_a_write_that_returns_nothing_still_answers_none(self):
        """The other half, and the reason the sibling tests above stay valid:
        a write function with no return value must keep producing exactly the
        ("ok", None) they assert."""
        queue = LearningWriteQueue()
        try:
            verdict = queue.submit_and_wait(lambda: None)
        finally:
            queue.shutdown()

        assert verdict == ("ok", None)

    def test_a_falsy_return_is_carried_rather_than_normalised(self):
        """`if value:` in the wrong place would turn 0, "" or False into None
        and lose a real answer. The reader compares against a constant, so the
        value must arrive exactly as the write function produced it."""
        queue = LearningWriteQueue()
        try:
            verdicts = [
                queue.submit_and_wait(lambda v=v: v) for v in (0, "", False)
            ]
        finally:
            queue.shutdown()

        assert verdicts == [("ok", 0), ("ok", ""), ("ok", False)]


class TestFailure:
    def test_a_raising_job_returns_failed_with_the_exception(self):
        queue = LearningWriteQueue()
        boom = ValueError("writer thread blew up")

        def raiser():
            raise boom

        try:
            verdict = queue.submit_and_wait(raiser)
        finally:
            queue.shutdown()

        assert verdict[0] == "failed"
        assert verdict[1] is boom

    def test_the_failure_is_logged_with_the_learning_prefix(self, caplog):
        queue = LearningWriteQueue()
        try:
            with caplog.at_level("WARNING"):
                queue.submit_and_wait(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        finally:
            queue.shutdown()

        matching = [r for r in caplog.records if "[LEARNING]" in r.message]
        assert matching, "expected a [LEARNING]-prefixed log line for the failed write"
        assert matching[0].levelname == "WARNING"


class TestTimeout:
    def test_a_slow_job_times_out_but_still_completes(self):
        queue = LearningWriteQueue()
        finished = threading.Event()

        def slow():
            time.sleep(0.3)
            finished.set()

        try:
            verdict = queue.submit_and_wait(slow, timeout=0.05)
            assert verdict == ("timeout", None)
            # The job is NOT cancelled — it must still complete on its own.
            assert finished.wait(2), "the job must still run to completion"
        finally:
            queue.shutdown()

    def test_a_job_queued_behind_a_slow_one_still_gets_its_own_verdict(self):
        queue = LearningWriteQueue()
        release_slow = threading.Event()

        def slow_job():
            release_slow.wait(2)

        queue.submit(slow_job)  # occupies the writer thread first

        try:
            # This job sits behind slow_job in the queue; its own timeout is
            # too short to survive slow_job still holding the writer thread.
            verdict = queue.submit_and_wait(lambda: None, timeout=0.05)
            assert verdict == ("timeout", None)
        finally:
            release_slow.set()
            queue.shutdown()


class TestReentrancy:
    def test_a_call_from_the_writer_thread_raises_instead_of_hanging(self):
        queue = LearningWriteQueue()
        caught = []
        done = threading.Event()

        def reentrant_job():
            try:
                queue.submit_and_wait(lambda: None, timeout=1.0)
            except RuntimeError as e:
                caught.append(e)
            finally:
                done.set()

        queue.submit(reentrant_job)
        try:
            assert done.wait(2), "the reentrant job never finished — it hung"
            assert len(caught) == 1
        finally:
            queue.shutdown()


class TestShutdownInteraction:
    def test_shutdown_does_not_strand_a_waiter_queued_during_drain(self):
        """A submit_and_wait job queued AFTER the SENTINEL (because it was
        submitted while shutdown() was already blocked joining the writer
        thread) must still be drained and get a real verdict — not be left
        waiting on an event nothing will ever set."""
        queue = LearningWriteQueue()
        block = threading.Event()
        started = threading.Event()

        def blocking_job():
            started.set()
            block.wait(2)

        queue.submit(blocking_job)
        assert started.wait(2), "blocking job never started"

        shutdown_thread = threading.Thread(
            target=queue.shutdown, kwargs={"timeout": 5.0}
        )
        shutdown_thread.start()
        time.sleep(0.1)  # let shutdown() enqueue its SENTINEL behind blocking_job

        result_box = {}

        def waiter():
            result_box["verdict"] = queue.submit_and_wait(lambda: None, timeout=5.0)

        waiter_thread = threading.Thread(target=waiter)
        waiter_thread.start()
        time.sleep(0.1)  # let the wrapped job land BEHIND the SENTINEL

        block.set()  # release blocking_job so the writer reaches the drain branch
        waiter_thread.join(6)
        shutdown_thread.join(6)

        assert result_box.get("verdict") == ("ok", None), (
            "a waiter queued during shutdown's drain must not be stranded"
        )

    def test_a_submission_after_shutdown_times_out_rather_than_hanging(self):
        queue = LearningWriteQueue()
        queue.shutdown()  # worker thread has fully exited by the time this returns

        verdict = queue.submit_and_wait(lambda: None, timeout=0.2)
        assert verdict == ("timeout", None), (
            "nothing will ever process this job, so it must time out, not hang"
        )
