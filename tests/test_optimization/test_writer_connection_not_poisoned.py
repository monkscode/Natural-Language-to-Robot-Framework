"""A failed learning write must not poison the shared writer connection.

Every learning write in the system runs on ONE long-lived psycopg connection
(``PostgresExecutionMemory._writer_conn``, opened ``autocommit=False``), driven
by the single ``learning-writer`` thread owned by ``LearningWriteQueue``. When a
statement raises and nobody rolls back, Postgres leaves that connection in an
ABORTED transaction (SQLSTATE 25P02) and every later statement on it fails with
``InFailedSqlTransaction`` until something happens to roll back. The queue's
drain loop only logs the failure, and the ``_writer_conn`` reconnect check does
not help: an aborted transaction sets neither ``closed`` nor ``broken``.

Each test drives ONE write site to a REAL SQL failure -- a CHECK constraint added
through a separate admin connection, dropped again in a ``finally`` -- queues a
second, well-formed write behind it on the same connection, and asserts the
SECOND write landed. The first write failing is the setup; the survival of the
next one is the assertion.

Nothing here is mocked: real ``PostgresExecutionMemory``, real
``LearningWriteQueue``, real Postgres transaction state on the isolated
``learning_test`` schema. A double cannot exercise transaction state at all.

Referenced by: pytest (tests/test_optimization)
Depends on: tests/test_optimization/conftest.py (in_memory_em, _pg_test_em),
            psycopg, src.backend.crew_ai.optimization.*
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
import pytest

from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.feedback_loop import (
    FeedbackLoop,
    LearningMetricsTracker,
)
from src.backend.crew_ai.optimization.keyword_correction_engine import (
    KeywordCorrectionEngine,
)
from src.backend.crew_ai.optimization.learning_config import LearningWriteQueue
from src.backend.crew_ai.optimization.structural_rule_engine import (
    StructuralRuleEngine,
)

# Mirrors conftest._PG_TEST_SCHEMA -- the isolated schema in_memory_em is bound to.
_SCHEMA = "learning_test"
_ORG = "org-poison-probe"
_CONSTRAINT = "writer_poison_probe"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def write_queue():
    """A real LearningWriteQueue with its own ``learning-writer`` thread."""
    q = LearningWriteQueue()
    yield q
    q.shutdown()


@pytest.fixture
def pg_probe_admin(in_memory_em):
    """A SEPARATE admin connection used only to add/drop the CHECK constraint.

    Separate because the constraint has to exist on the server while the writer
    connection runs the doomed statement. ``lock_timeout`` keeps a DDL that
    cannot get its lock from hanging the suite.
    """
    from src.backend.core.config import settings
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    conn.execute("SET lock_timeout = '5s'")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def poison(admin, em, table: str, expr: str):
    """Add a CHECK constraint on ``table``, drop it again no matter what.

    ``learning_test`` is a session-scoped shared schema, so a leaked constraint
    would break every later test. The writer connection is rolled back before the
    DROP so an aborted transaction cannot hold the table lock the DDL needs.
    """
    admin.execute(
        f"ALTER TABLE {_SCHEMA}.{table} ADD CONSTRAINT {_CONSTRAINT} CHECK ({expr})"
    )
    try:
        yield
    finally:
        try:
            em._writer_conn.rollback()
        except Exception:
            pass
        admin.execute(
            f"ALTER TABLE {_SCHEMA}.{table} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}"
        )


def _record(**kw) -> ExecutionRecord:
    fields = {
        "workflow_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "user_query": "open the site and search for shoes",
        "url": "https://example.com/shop",
        "domain": "example.com",
        "robot_code": "*** Test Cases ***\n    Log    hello",
        "test_status": "failed",
        "org_id": _ORG,
    }
    fields.update(kw)
    return ExecutionRecord(**fields)


def _canary(em):
    """A well-formed follow-up write plus the assertion that it landed.

    Returns ``(record, job, verify)``. ``job`` is what gets queued behind the
    doomed write; it records its own exception so the failure message names the
    real symptom (``InFailedSqlTransaction: current transaction is aborted``)
    instead of only reporting a missing row.
    """
    record = _record(test_status="passed", user_query="canary write after failure")
    box: dict = {}

    def job():
        try:
            em.store(record)
        except BaseException as exc:
            box["exc"] = exc
            raise

    def verify():
        assert "exc" not in box, (
            "the write queued AFTER the failed one was itself rejected -- the "
            f"writer connection was left poisoned: {box['exc']!r}"
        )
        with em.read_conn() as conn:
            row = conn.execute(
                "SELECT 1 AS ok FROM execution_records WHERE workflow_id = ?",
                (record.workflow_id,),
            ).fetchone()
        assert row is not None, (
            "the follow-up write reported success but its row is missing"
        )

    return record, job, verify


@pytest.fixture
def feedback_loop(in_memory_em):
    """A real FeedbackLoop on the isolated store.

    ``__init__`` queues two startup jobs (anchor reconcile, stale-review
    recovery) on the queue it is handed. They are drained on a throwaway queue
    here so they cannot race the queue the test drives.
    """
    setup_q = LearningWriteQueue()
    fl = FeedbackLoop(execution_memory=in_memory_em, write_queue=setup_q)
    setup_q.shutdown()
    return fl


# ---------------------------------------------------------------------------
# postgres_execution_memory.py
# ---------------------------------------------------------------------------

def test_update_to_passing_state_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    seed = _record(test_status="failed")
    em.store(seed)

    _, job, verify = _canary(em)
    # Constrain working_code -- the column _update_to_passing_state writes.
    # Constraining a column the INSERT writes would fail the wrong statement.
    with poison(pg_probe_admin, em, "execution_records",
                "working_code IS DISTINCT FROM 'BOOM'"):
        rerun = _record(workflow_id=seed.workflow_id, test_status="passed",
                        robot_code="BOOM")
        write_queue.submit(em.store, rerun)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_update_user_feedback_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    seed = _record()
    em.store(seed)

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "execution_records",
                "user_feedback IS DISTINCT FROM 'BOOM'"):
        write_queue.submit(em.update_user_feedback, seed.workflow_id, "BOOM",
                           "completely_wrong")
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_update_daily_stats_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    _, job, verify = _canary(em)
    # The table is truncated per test, so the upsert takes the INSERT branch and
    # writes total_executions = 1.
    with poison(pg_probe_admin, em, "learning_stats", "total_executions <> 1"):
        write_queue.submit(em.update_daily_stats, "passed")
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


# ---------------------------------------------------------------------------
# anti_pattern_engine.py
# ---------------------------------------------------------------------------

def test_anti_pattern_learn_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = AntiPatternEngine(em)
    failed = _record(test_status="failed", failure_category="BOOM",
                     failed_keyword="Click", error_message="element not found")

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "anti_patterns",
                "failure_category IS DISTINCT FROM 'BOOM'"):
        write_queue.submit(engine.learn, failed)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_check_for_correct_alternative_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = AntiPatternEngine(em)
    now = datetime.now(timezone.utc).isoformat()
    em._writer_conn.execute(
        "INSERT INTO anti_patterns (failure_category, query_pattern, "
        " bad_code_snippet, error_message, domain, org_id, score, "
        " evidence_count, last_seen) "
        "VALUES ('A1', ?, 'Click    id=gone', 'element not found', "
        " 'example.com', ?, 0.9, 5, ?)",
        ("open the site and search for shoes", _ORG, now),
    )
    em._writer_conn.commit()

    # A passing run whose code does NOT contain the bad snippet resolves the
    # anti-pattern: UPDATE anti_patterns SET correct_alternative = robot_code[:500].
    passing = _record(test_status="passed", robot_code="BOOM")

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "anti_patterns",
                "correct_alternative IS DISTINCT FROM 'BOOM'"):
        write_queue.submit(engine.learn, passing)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


# ---------------------------------------------------------------------------
# keyword_correction_engine.py
# ---------------------------------------------------------------------------

def test_keyword_correction_learn_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = KeywordCorrectionEngine(em)
    failed = _record(
        failure_category="B1",
        error_message="No keyword with name 'Boom Keyword' found",
    )

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "keyword_corrections",
                "wrong_keyword IS DISTINCT FROM 'Boom Keyword'"):
        write_queue.submit(engine.learn, failed)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


# ---------------------------------------------------------------------------
# feedback_loop.py
# ---------------------------------------------------------------------------

def test_record_execution_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    tracker = LearningMetricsTracker(em)

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "learning_metrics",
                "user_query IS DISTINCT FROM 'BOOM'"):
        write_queue.submit(
            tracker.record_execution,
            str(uuid.uuid4()), "BOOM", True, 0, 0, [], 3, 0.01, True,
        )
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_recover_stale_review_sessions_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin, feedback_loop
):
    em = in_memory_em
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    em._writer_conn.execute(
        "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
        "VALUES ('pending_llm', 3, ?)",
        (now,),
    )
    em._writer_conn.commit()

    _, job, verify = _canary(em)
    # The recovery UPDATE sets status = 'failed'; the seeded row is 'pending_llm'
    # so the constraint is satisfiable when it is added.
    with poison(pg_probe_admin, em, "hint_review_sessions",
                "status IS DISTINCT FROM 'failed'"):
        write_queue.submit(feedback_loop._recover_stale_review_sessions)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_write_trigger_event_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin, feedback_loop
):
    em = in_memory_em

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "trigger_events",
                "trigger_type IS DISTINCT FROM 'BOOM'"):
        write_queue.submit(
            feedback_loop.write_trigger_event,
            trigger_type="BOOM",
            workflow_id=str(uuid.uuid4()),
            domain="example.com",
            url="https://example.com/shop",
            feedback_text="the locator was wrong",
            active_hint_ids=[],
            flagged_hint_ids=[],
            actually_flagged_hint_ids=[],
            reason=None,
            llm_model="gemini-2.5-flash",
            input_tokens=10,
            output_tokens=5,
            llm_latency_ms=42,
            status="succeeded",
            error_message=None,
        )
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


# ---------------------------------------------------------------------------
# structural_rule_engine.py -- StructuralRuleEngine.learn runs on EVERY
# execution (feedback_loop.py:1025), one line above the two engine learns
# above, and reaches the writer connection through four separate commits.
# ---------------------------------------------------------------------------

# "count the rows" matches exactly one seed pattern -- "counting" (trigger
# "count"), whose only required keyword is "Get Element Count". No other
# SEED_PATTERNS trigger is a substring of it, so each test below drives a
# single, predictable intent.
_SEED_QUERY = "count the rows"
_SEED_INTENT = "counting"
_SEED_KEYWORD_CODE = "*** Test Cases ***\n    ${n}=    Get Element Count    css=tr"


def _seed_structural_rule(em):
    """Pre-create the 'counting' rule so _get_or_create_rule takes its SELECT
    path and learn() proceeds straight to the increment helpers."""
    em._writer_conn.execute(
        "INSERT INTO structural_rules "
        "(rule_name, query_pattern, required_structure, required_keywords_json, "
        " score, evidence_count, counter_evidence, last_updated, created_at) "
        "VALUES (?, 'count', 'unknown', ?, 0.0, 0, 0, "
        " datetime('now', 'localtime'), datetime('now', 'localtime'))",
        (_SEED_INTENT, '["Get Element Count"]'),
    )
    em._writer_conn.commit()


def test_migrate_seed_to_learned_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = StructuralRuleEngine(em)
    # intent_patterns is truncated per test, so extract_intents finds no learned
    # pattern, falls through to the seed layer and migrates "counting".
    failed = _record(user_query=_SEED_QUERY, test_status="failed",
                     failure_category="B1")

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "intent_patterns",
                "intent_name IS DISTINCT FROM 'counting'"):
        write_queue.submit(engine.learn, failed)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_get_or_create_rule_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = StructuralRuleEngine(em)
    # No rule exists yet, so _get_or_create_rule takes its INSERT branch.
    # failure_category "B1" keeps learn() out of both increment helpers.
    failed = _record(user_query=_SEED_QUERY, test_status="failed",
                     failure_category="B1")

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "structural_rules",
                "rule_name IS DISTINCT FROM 'counting'"):
        write_queue.submit(engine.learn, failed)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_increment_evidence_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = StructuralRuleEngine(em)
    _seed_structural_rule(em)
    # Passed WITH the required keyword present -> _increment_evidence, which
    # updates the seeded evidence_count 0 -> 1.
    passing = _record(user_query=_SEED_QUERY, test_status="passed",
                      robot_code=_SEED_KEYWORD_CODE)

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "structural_rules", "evidence_count <> 1"):
        write_queue.submit(engine.learn, passing)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


def test_increment_counter_evidence_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = StructuralRuleEngine(em)
    _seed_structural_rule(em)
    # Passed WITHOUT the required keyword -> _increment_counter_evidence, which
    # updates the seeded counter_evidence 0 -> 1.
    passing = _record(user_query=_SEED_QUERY, test_status="passed",
                      robot_code="*** Test Cases ***\n    Log    hello")

    _, job, verify = _canary(em)
    with poison(pg_probe_admin, em, "structural_rules", "counter_evidence <> 1"):
        write_queue.submit(engine.learn, passing)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()


# ---------------------------------------------------------------------------
# anti_pattern_engine.py -- the READ that runs before the write
# ---------------------------------------------------------------------------

@contextmanager
def hide_column(admin, em, table: str, column: str):
    """Rename one column away and back, so a SELECT naming it fails 42703.

    A CHECK constraint cannot fail a SELECT, and this database's role is a
    superuser (so REVOKE SELECT is a no-op), which leaves renaming a single
    column as the narrowest genuine server-side read failure available. No row
    is touched; the finally puts the name back and then verifies it did.
    """
    hidden = f"{column}__hidden_by_test"
    admin.execute(
        f"ALTER TABLE {_SCHEMA}.{table} RENAME COLUMN {column} TO {hidden}"
    )
    try:
        yield
    finally:
        try:
            em._writer_conn.rollback()
        except Exception:
            pass
        admin.execute(
            f"ALTER TABLE {_SCHEMA}.{table} RENAME COLUMN {hidden} TO {column}"
        )
        restored = admin.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (_SCHEMA, table, column),
        ).fetchone()
        assert restored is not None, (
            f"{_SCHEMA}.{table}.{column} was not restored -- the shared test "
            "schema is now broken for every later test"
        )


def test_find_similar_anti_pattern_failure_does_not_poison(
    in_memory_em, write_queue, pg_probe_admin
):
    em = in_memory_em
    engine = AntiPatternEngine(em)
    failed = _record(test_status="failed", failure_category="A1",
                     failed_keyword="Click", error_message="element not found")

    _, job, verify = _canary(em)
    # learn() reads through _find_similar_anti_pattern BEFORE it writes, on the
    # same writer connection and inside the same transaction. A failing SELECT
    # aborts that transaction exactly like a failing UPDATE does, so the read
    # has to sit inside the guard too.
    with hide_column(pg_probe_admin, em, "anti_patterns", "org_id"):
        write_queue.submit(engine.learn, failed)
        write_queue.submit(job)
        write_queue.shutdown()

    verify()
