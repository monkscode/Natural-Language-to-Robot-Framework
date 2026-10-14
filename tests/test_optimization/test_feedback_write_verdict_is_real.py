"""C1 — "processed" must mean the correction was stored, not "nothing raised".

`LearningWriteQueue.submit_and_wait` can only report what ESCAPES the job.
`NLFeedbackEngine.learn_from_feedback` used to wrap its whole storage body in
`except Exception`, roll back, log a WARNING and return normally — so a write
that stored nothing came back as ("ok", None), `/api/feedback` answered
"Thanks — your feedback helps the system learn", and the correction was gone.

These tests run the REAL engine against the REAL queue and a real Postgres
schema. A double cannot catch this defect: the swallow lives inside the engine,
so any stand-in for either party passes whether or not the bug is present —
which is exactly why `test_feedback_ordering.py`'s `_VerdictWriteQueue` mapping
test stayed green while the mode was open.

The failure injected here is the cascade the engine's own except block
documents: the shared, long-lived writer connection arrives carrying an aborted
transaction, so the correction's first statement dies of InFailedSqlTransaction.
That is a real Postgres failure on a real connection, not a patched method.

Ambient thread-name note: tests/test_optimization/conftest.py renames the
pytest thread to WRITER_THREAD_NAME (autouse). submit_and_wait refuses to run
on a thread by that name — it would be waiting for itself — so this file
overrides that fixture to a no-op, exactly as test_write_queue_wait.py does.
"""

from datetime import datetime, timezone

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.learning_config import LearningWriteQueue
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine

_NOW = datetime.now(timezone.utc)
_ORG = "org-A"
_TEXT = "the login locator was wrong"


@pytest.fixture(autouse=True)
def _rename_test_thread_to_writer():
    """Override the package-wide autouse fixture (see the module docstring):
    these tests call submit_and_wait from the pytest thread, which must NOT
    carry the writer thread's name."""
    yield


def _record(workflow_id="wf-c1", *, org_id=_ORG):
    return ExecutionRecord(
        workflow_id=workflow_id, timestamp=_NOW,
        user_query="log in and open the dashboard",
        url="https://shop.test/login", domain="shop.test",
        test_status="failed", org_id=org_id,
    )


def _triage(text=_TEXT):
    return {"category": "keyword", "feedback_text": text, "actor": "tester"}


def _leave_the_writer_transaction_aborted(conn):
    """Put the shared writer connection into the state a previous failed job
    leaves it in: an open transaction Postgres has already aborted.

    Every later statement on it raises InFailedSqlTransaction until someone
    rolls back — which is precisely the cascade the engine's except block
    describes, and the shape a dropped connection or a missing index takes.
    """
    try:
        conn.execute("SELECT 1 FROM a_relation_that_does_not_exist")
    except Exception:
        pass  # deliberately NOT rolled back — that is the state under test
    else:  # pragma: no cover - defensive
        raise AssertionError("the poisoning statement was expected to fail")


def _hint_count(conn):
    with conn.read_conn() as read:
        return read.execute(
            "SELECT COUNT(*) AS n FROM nl_feedback_corrections"
        ).fetchone()["n"]


class TestAFailedWriteIsReportedAsFailed:
    def test_a_correction_that_stores_nothing_comes_back_as_failed(
        self, in_memory_db,
    ):
        """The whole of C1: real engine, real queue, real failure.

        Before the fix the engine swallowed the exception and returned, so the
        verdict was ("ok", None) — `/api/feedback` thanked the user for a
        correction that was never written.
        """
        engine = NLFeedbackEngine(in_memory_db)
        queue = LearningWriteQueue()
        _leave_the_writer_transaction_aborted(in_memory_db)

        try:
            verdict, detail = queue.submit_and_wait(
                engine.learn_from_feedback, _record(), _triage(),
            )
        finally:
            queue.shutdown()

        assert verdict == "failed", (
            "the write stored nothing but reported success — /api/feedback "
            "would answer 'Thanks, your feedback helps the system learn'"
        )
        assert isinstance(detail, Exception)
        assert _hint_count(in_memory_db) == 0, "nothing may have been stored"

    def test_the_connection_still_works_for_the_next_correction(
        self, in_memory_db,
    ):
        """The rollback must still happen before the raise.

        The writer connection is shared and long-lived: if the failed job left
        its transaction aborted, the NEXT correction off the queue would die of
        InFailedSqlTransaction too — trading one silent loss for a cascade.
        """
        engine = NLFeedbackEngine(in_memory_db)
        queue = LearningWriteQueue()
        _leave_the_writer_transaction_aborted(in_memory_db)

        try:
            first, _ = queue.submit_and_wait(
                engine.learn_from_feedback, _record("wf-c1-a"), _triage(),
            )
            second, _ = queue.submit_and_wait(
                engine.learn_from_feedback, _record("wf-c1-b"),
                _triage("the search box needs a longer wait"),
            )
        finally:
            queue.shutdown()

        assert first == "failed"
        assert second == "ok", "the next job inherited the aborted transaction"
        assert _hint_count(in_memory_db) == 1


class TestACommittedWriteIsNeverReportedAsLost:
    def test_an_anchor_failure_after_the_commit_still_reports_ok(
        self, in_memory_db,
    ):
        """`add_anchor` runs AFTER the SQL commit and is best-effort by design
        (a missed doc is healed by the next reconcile). Its embedder call sits
        outside its own try, so a broken embedder raises out of it — and that
        must not turn a stored correction into a reported failure.
        """
        engine = NLFeedbackEngine(in_memory_db)
        queue = LearningWriteQueue()

        def _broken_embedder(_text):
            raise RuntimeError("embedder unavailable")

        in_memory_db._em._embed = _broken_embedder

        try:
            verdict, detail = queue.submit_and_wait(
                engine.learn_from_feedback, _record("wf-c1-anchor"), _triage(),
            )
        finally:
            queue.shutdown()

        assert verdict == "ok", (
            "a best-effort anchor failure was reported as a lost correction"
        )
        assert detail is None
        assert _hint_count(in_memory_db) == 1, "the correction was committed"
