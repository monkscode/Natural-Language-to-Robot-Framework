"""A hint carries its author, and keeps carrying it after the author is gone.

Until now a hint recorded WHERE it was created (org_id, source_workflow_id)
but not WHO created it. The Author permission tier needs that: an author may
retract their own hint, and nothing in the row said whose it was.

Two columns, following the audit_log precedent, which already carries both
actor_user_id and actor_email for exactly this reason:

  created_by_user_id  the stable key while the user exists (it survives an
                      email change), and the key the Author tier compares
                      against the caller's token.
  created_by_email    a denormalised snapshot that survives user deletion, so
                      the hint keeps naming its creator even when there is no
                      users row left to join to.

No foreign key to users, deliberately: deleting a user must neither cascade
into the org's learning store nor be blocked by it. That is the same
non-cascade shape the org_id column has — the difference is that org_id is
now guarded by a BEFORE DELETE trigger on organizations, and this one is not,
because a hint outliving its author is correct while a hint outliving its org
is not.

Set on INSERT only. The reinforce path is a SELECT-then-UPDATE whose UPDATE
names its columns explicitly, so it already cannot overwrite the author —
that is satisfied by construction, and pinned here so a future refactor to
ON CONFLICT DO UPDATE cannot silently break it.

Real Postgres via the in_memory_db fixture (never mocked — optimization rule).

Depends on: src/backend/crew_ai/optimization/nl_feedback_engine.py,
            src/backend/crew_ai/optimization/pg_schema.py (v23)
"""

from datetime import datetime, timezone

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine


_NOW = datetime.now(timezone.utc)
_TEXT = "wait for the results grid before reading the first row"
_ORG = "org-A"
_EMAIL = "author@example.com"
_UID = "11111111-2222-3333-4444-555555555555"


def _record(workflow_id, *, org_id=_ORG):
    return ExecutionRecord(
        workflow_id=workflow_id, timestamp=_NOW,
        user_query="open the dashboard and read the total",
        url="https://shop.test/dash", domain="shop.test",
        test_status="failed", org_id=org_id,
    )


def _triage(*, actor=_EMAIL, actor_user_id=_UID, text=_TEXT):
    """category 'structural' -> scope 'domain' -> the general dedup branch."""
    return {
        "category": "structural", "feedback_text": text,
        "actor": actor, "actor_user_id": actor_user_id,
    }


def _hints(conn):
    return conn.execute(
        "SELECT id, feedback_text, evidence_count, org_id, "
        "       created_by_user_id, created_by_email "
        "FROM nl_feedback_corrections ORDER BY id"
    ).fetchall()


class TestTheAuthorIsRecorded:

    def test_a_created_hint_names_its_author(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())

        rows = _hints(in_memory_db)
        assert len(rows) == 1
        assert rows[0]["created_by_user_id"] == _UID
        assert rows[0]["created_by_email"] == _EMAIL
        assert rows[0]["org_id"] == _ORG

    def test_an_unidentified_submitter_leaves_the_author_null(self, in_memory_db):
        """AUTH_ENFORCED off is the only way to reach this. A hint with no
        knowable author records NULL rather than a made-up actor string —
        the Author tier must never match on a placeholder."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(
            _record("wf-1"), _triage(actor=None, actor_user_id=None))

        rows = _hints(in_memory_db)
        assert rows[0]["created_by_user_id"] is None
        assert rows[0]["created_by_email"] is None

    def test_there_is_no_foreign_key_to_users(self, in_memory_db):
        """The mechanism that makes deleting a user safe. With an FK, the
        delete would either cascade (destroying the org's hint) or be blocked
        (making the user undeletable). Neither is wanted: the hint stays and
        keeps naming them."""
        with in_memory_db.read_conn() as conn:
            rows = conn.execute(
                "SELECT c.conname, a.attname "
                "FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                "                   AND a.attnum = ANY(c.conkey) "
                "WHERE c.conrelid = 'nl_feedback_corrections'::regclass "
                "  AND c.contype = 'f'"
            ).fetchall()
        offenders = [r["attname"] for r in rows
                     if r["attname"] in ("created_by_user_id", "created_by_email")]
        assert offenders == [], f"author columns must carry no FK, found {offenders}"


class TestReinforcementDoesNotStealAuthorship:

    def test_a_second_identical_submission_keeps_the_first_author(self, in_memory_db):
        """Two users, identical text, same org. The dedup key
        (feedback_text, domain, scope, org_id) carries NO author, so this is
        ONE row and the FIRST author's name sticks. Owner-ruled acceptable;
        pinned here so the behaviour is deliberate rather than incidental."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        engine.learn_from_feedback(
            _record("wf-2"),
            _triage(actor="second@example.com",
                    actor_user_id="99999999-9999-9999-9999-999999999999"),
        )

        rows = _hints(in_memory_db)
        assert len(rows) == 1, "identical text in one org must dedup to one row"
        assert rows[0]["evidence_count"] == 2, "the second submission reinforced it"
        assert rows[0]["created_by_user_id"] == _UID, "the first author must stick"
        assert rows[0]["created_by_email"] == _EMAIL
