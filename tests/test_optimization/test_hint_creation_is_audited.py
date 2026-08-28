"""T11 — a hint's audit trail starts when the hint does.

`hint_audit` is the timeline the admin drawer renders and the LLM-accuracy KPI
reads. Until now the engine wrote into it only when a re-submission cleared a
conflict flag, so an engine-created hint's story began mid-sentence: the row
appeared with no record of who asked for it, and every later reinforcement was
invisible unless it happened to unflag something. The admin API has written a
`create` row since it shipped (`learning_endpoints.py`), so this is one missing
call site mirroring code that already exists — no schema change; `hint_audit`
takes any `action` string and `actor` is already threaded from the verified
token (`feedback_loop.py` -> `feedback_insight['actor']`).

Placement is the whole design. Both rows go INSIDE their own branch:

  * `create`   — after `RETURNING id`, so it is atomic with the hint itself and
                 carries the id the INSERT just produced.
  * `reinforce`— inside `if existing:` and AFTER T5's claim gate, which returns
                 early on a duplicate submission from the same run. A row
                 written above that gate would record a reinforcement that never
                 happened, which is exactly the kind of counted-but-untrue event
                 T5 exists to stop.

The same `except` also gains the `rollback()` every sibling write handler
already has (Step 2b). Without it a mid-transaction failure leaves the shared
writer connection aborted, and the NEXT queued correction dies of
`InFailedSqlTransaction` before its own handler can run — one bad submission
taking an unrelated one down with it.

Real Postgres via the in_memory_db fixture (never mocked — optimization rule).

Referenced by: docs/superpowers/plans/2026-08-26-feedback-integrity-and-org-isolation.md (T11)
Depends on: src/backend/crew_ai/optimization/nl_feedback_engine.py
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine


_NOW = datetime.now(timezone.utc)
_TEXT = "prefer data-testid over nth-child selectors"
# T9: hint writes fail closed without an org, so every record names one.
_ORG = "org-A"


def _record(workflow_id, *, org_id=_ORG):
    return ExecutionRecord(
        workflow_id=workflow_id, timestamp=_NOW,
        user_query="open the dashboard and read the total",
        url="https://shop.test/dash", domain="shop.test",
        test_status="failed", org_id=org_id,
    )


def _triage(actor="alice@example.com", text=_TEXT):
    """category 'structural' -> scope 'domain' -> the general dedup branch."""
    return {"category": "structural", "feedback_text": text, "actor": actor}


def _audit(conn, hint_id=None):
    if hint_id is None:
        return conn.execute(
            "SELECT hint_id, action, actor, reason, before_value, after_value "
            "FROM hint_audit ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT hint_id, action, actor, reason, before_value, after_value "
        "FROM hint_audit WHERE hint_id = ? ORDER BY id",
        (hint_id,),
    ).fetchall()


def _only_hint(conn):
    rows = conn.execute(
        "SELECT id, evidence_count, conflict_flagged FROM nl_feedback_corrections "
        "ORDER BY id"
    ).fetchall()
    assert len(rows) == 1, f"expected exactly one hint, got {len(rows)}"
    return rows[0]


def _seed_hint(conn, *, conflict_flagged=0):
    """A hint that already exists, on the engine's own dedup key
    (feedback_text, domain, scope, org_id) — so a submission reinforces it
    instead of taking the create branch."""
    cur = conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, evidence_count, is_active, "
        " conflict_flagged, created_at, last_seen, org_id) "
        "VALUES (?, 'structural', 'domain', 'shop.test', 1, 1, ?, ?, ?, ?) "
        "RETURNING id",
        (_TEXT, conflict_flagged, _NOW.isoformat(), _NOW.isoformat(), _ORG),
    )
    hid = cur.fetchone()["id"]
    conn.commit()
    return hid


class TestTheCreateRow:

    def test_an_engine_created_hint_gets_a_create_audit_row(self, in_memory_db):
        """(a) The hint the user's feedback created says who asked for it."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())

        hint = _only_hint(in_memory_db)
        rows = _audit(in_memory_db, hint["id"])
        assert [r["action"] for r in rows] == ["create"], (
            "the engine created a hint with no 'create' audit row — its "
            "timeline starts mid-story"
        )
        assert rows[0]["actor"] == "alice@example.com"

    def test_the_create_row_records_what_was_created(self, in_memory_db):
        """after_value is the provenance: the text, its scope, and the run and
        org it came from. Mirrors the admin create's own after_value."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())

        after = json.loads(_audit(in_memory_db)[0]["after_value"])
        assert after["feedback_text"] == _TEXT
        assert after["scope"] == "domain"
        assert after["domain"] == "shop.test"
        assert after["org_id"] == _ORG
        assert after["created_via"] == "workflow"
        assert after["source_workflow_id"] == "wf-1"

    def test_the_actor_falls_back_to_unknown_never_to_a_name(self, in_memory_db):
        """Auth off / API caller: record the honest literal, not a guess.
        Mirrors the existing unflag-row behaviour."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(
            _record("wf-1"),
            {"category": "structural", "feedback_text": _TEXT},
        )

        rows = _audit(in_memory_db)
        assert [r["action"] for r in rows] == ["create"]
        assert rows[0]["actor"] == "unknown"


class TestTheReinforceRow:

    def test_reinforcing_an_existing_hint_writes_a_reinforce_row(self, in_memory_db):
        """(b) A later run re-submitting the same correction is a distinct
        event in the hint's life, and today it leaves no trace at all unless it
        happens to clear a flag."""
        hint_id = _seed_hint(in_memory_db)
        engine = NLFeedbackEngine(in_memory_db)

        engine.learn_from_feedback(_record("wf-2"), _triage(actor="bob@example.com"))

        rows = _audit(in_memory_db, hint_id)
        assert [r["action"] for r in rows] == ["reinforce"], (
            "a reinforcement left no audit row — the hint's counters moved "
            "with nothing on record saying why"
        )
        assert rows[0]["actor"] == "bob@example.com"
        after = json.loads(rows[0]["after_value"])
        assert after["evidence_count"] == 2, (
            "the reinforce row must record the counter it produced"
        )
        # Anti-false-green: the reinforcement itself must actually have happened.
        assert _only_hint(in_memory_db)["evidence_count"] == 2

    def test_a_flagged_hint_gets_both_the_unflag_and_the_reinforce_row(self, in_memory_db):
        """The two rows describe different facts and neither replaces the other:
        `unflag` is the implicit override the LLM-accuracy KPI counts, and
        `reinforce` is the evidence event."""
        hint_id = _seed_hint(in_memory_db, conflict_flagged=1)
        engine = NLFeedbackEngine(in_memory_db)

        engine.learn_from_feedback(_record("wf-2"), _triage())

        actions = {r["action"] for r in _audit(in_memory_db, hint_id)}
        assert actions == {"unflag", "reinforce"}


class TestTheGatedDuplicateWritesNothing:

    def test_a_second_submission_from_the_same_run_writes_no_audit_row(self, in_memory_db):
        """(c) T5 gates every effect of a repeat submission from one run. An
        audit row placed above that gate would report a reinforcement that
        never happened."""
        hint_id = _seed_hint(in_memory_db)
        engine = NLFeedbackEngine(in_memory_db)

        engine.learn_from_feedback(_record("wf-2"), _triage())
        engine.learn_from_feedback(_record("wf-2"), _triage())

        actions = [r["action"] for r in _audit(in_memory_db, hint_id)]
        assert actions == ["reinforce"], (
            f"the gated duplicate wrote an audit row too (got {actions})"
        )
        assert _only_hint(in_memory_db)["evidence_count"] == 2

    def test_a_second_submission_of_new_text_from_the_same_run_writes_no_row(
        self, in_memory_db,
    ):
        """The create branch's own repeat: T5's claim is written there too
        (R1), so the second submission is gated before it can reinforce."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        engine.learn_from_feedback(_record("wf-1"), _triage())

        actions = [r["action"] for r in _audit(in_memory_db)]
        assert actions == ["create"], (
            f"the gated repeat of a just-created hint was audited (got {actions})"
        )

    def test_an_org_less_record_writes_no_audit_row(self, in_memory_db):
        """T9's write guard returns above everything. The refusal must stay
        silent in the audit trail — there is no hint to have a story about."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1", org_id=None), _triage())

        assert _audit(in_memory_db) == []


class TestAFailedCorrectionDoesNotPoisonTheNextOne:
    """Step 2b — the missing `rollback()`.

    `learn_from_feedback` runs on the shared, long-lived writer connection, and
    every sibling write handler in this module rolls back when it fails. This
    one did not, so an aborted transaction outlived the submission that caused
    it: the next correction off the write queue hit `InFailedSqlTransaction` on
    its very first statement and was swallowed by its own except.
    """

    def test_the_next_correction_still_stores(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)

        def _abort_the_transaction(*_args, **_kwargs):
            # A real server-side error, mid-transaction, after the hint INSERT
            # has already run — the shape of the NOT NULL violation that was
            # observed. Injected at the connection, never by dropping a table:
            # the fixture DSN keeps `public` on the search_path, so catalog
            # damage in a test lands on the live database.
            in_memory_db._writer_conn.execute("SELECT 1 / 0")

        with patch.object(
            NLFeedbackEngine, "_claim_feedback_run", _abort_the_transaction,
        ):
            engine.learn_from_feedback(_record("wf-doomed"), _triage())

        engine.learn_from_feedback(
            _record("wf-next"), _triage(text="use an explicit wait"))

        # Read on a fresh connection: the writer connection is the thing under
        # test, and a SELECT issued on it would itself die of the abort and
        # fail this test one statement too early.
        with in_memory_db.read_conn() as conn:
            texts = [r["feedback_text"] for r in conn.execute(
                "SELECT feedback_text FROM nl_feedback_corrections ORDER BY id"
            ).fetchall()]
        assert texts == ["use an explicit wait"], (
            "the correction after a failed one was lost: the failed submission "
            f"left the writer connection in an aborted transaction (got {texts})"
        )

    def test_the_refusal_path_needs_no_transaction(self, in_memory_db):
        """The T9 org guard returns before any SQL runs, so the new rollback
        must not be reachable from it — and a correction after a refusal is
        unaffected."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-noorg", org_id=None), _triage())
        engine.learn_from_feedback(_record("wf-next"), _triage())

        assert _only_hint(in_memory_db)["evidence_count"] == 1


@pytest.mark.parametrize("actor", ["alice@example.com", None])
def test_create_and_reinforce_agree_on_the_actor(in_memory_db, actor):
    """One submitter, two runs, two rows — the same identity on both, so the
    'who' half of the story is answerable from either end."""
    engine = NLFeedbackEngine(in_memory_db)
    engine.learn_from_feedback(_record("wf-1"), _triage(actor=actor))
    engine.learn_from_feedback(_record("wf-2"), _triage(actor=actor))

    rows = _audit(in_memory_db)
    assert [r["action"] for r in rows] == ["create", "reinforce"]
    expected = actor or "unknown"
    assert [r["actor"] for r in rows] == [expected, expected]
