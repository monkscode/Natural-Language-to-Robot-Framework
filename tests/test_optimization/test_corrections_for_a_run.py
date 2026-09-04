"""T8 Step 6 — the corrections a run has already contributed, read back.

T5 made the run claim a hint once, and that claim IS a `hint_evidence` row
keyed (hint_id, 'workflow', sha256(workflow_id), 'evidence'). This file pins
the READ against the WRITE that produces it: nothing else in the codebase
would notice a predicate that stopped matching, because both the endpoint
tests and the panel tests stub this method out.

The set it returns is exactly the run's own contribution — the hints it
CREATED and the ones it REINFORCED. Neither `execution_records.user_feedback`
(one column, last write wins) nor `nl_feedback_corrections.source_workflow_id`
(the creating run only) can express that.

Deliberately NOT filtered on is_active / conflict_flagged: these are the
user's own words, and hiding a disabled hint would make the panel report
"nothing on file" for a correction that was recorded. Which of them the LLM
later flagged is a separate product decision, and is not exposed here.

Real Postgres via the in_memory_db fixture (never mocked — optimization rule).

Referenced by: src/backend/crew_ai/optimization/nl_feedback_engine.py.
Depends on: tests/test_optimization/conftest.py (in_memory_db).
"""

from datetime import datetime, timezone
from unittest.mock import patch

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import (
    NLFeedbackEngine, run_source_hash,
)


_NOW = datetime.now(timezone.utc)
_TEXT = "wait for the spinner to disappear before asserting"
_OTHER = "the search box locator was off"


# T9: hint writes fail closed without an org, so records here name one.
_ORG = "org-A"


def _record(workflow_id, *, url="https://shop.test/dash", org_id=_ORG):
    return ExecutionRecord(
        workflow_id=workflow_id, timestamp=_NOW,
        user_query="verify the dashboard loads", url=url, domain="shop.test",
        test_status="failed", org_id=org_id,
    )


def _triage(text=_TEXT):
    return {"category": "keyword", "feedback_text": text, "actor": "tester"}


def _texts(rows):
    return [r["feedback_text"] for r in rows]


class TestWhatOneRunContributed:
    def test_a_correction_is_readable_from_the_run_that_made_it(self, in_memory_db):
        """The round trip. A predicate that drifts from _claim_feedback_run's
        INSERT returns [] here and nowhere else — every other test of this
        feature stubs the engine out."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())

        rows = engine.get_corrections_for_run("wf-1")

        assert _texts(rows) == [_TEXT]
        assert rows[0]["recorded_at"], "the claim carries when it was recorded"
        assert rows[0]["hint_id"] > 0

    def test_another_runs_correction_is_not_returned(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())

        assert engine.get_corrections_for_run("wf-2") == []

    def test_two_different_corrections_from_one_run_both_appear(self, in_memory_db):
        """T5's gate is per (hint, run), not per run — one run may legitimately
        file two different corrections, and both are its contribution."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        engine.learn_from_feedback(_record("wf-1"), _triage(_OTHER))

        # Oldest first: the panel renders this as a chronology of what the
        # user told the system, so submission order is part of the contract.
        assert _texts(engine.get_corrections_for_run("wf-1")) == [_TEXT, _OTHER]

    def test_a_reinforcement_counts_as_the_later_runs_contribution(self, in_memory_db):
        """wf-1 creates the hint, wf-2 re-affirms it (Gap 7's recovery path).
        The hint's source_workflow_id names wf-1 forever, so only the claim row
        can tell wf-2 that it contributed anything."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        engine.learn_from_feedback(_record("wf-2"), _triage())

        assert _texts(engine.get_corrections_for_run("wf-2")) == [_TEXT]
        assert _texts(engine.get_corrections_for_run("wf-1")) == [_TEXT]

    def test_a_disabled_hint_is_still_the_users_own_words(self, in_memory_db):
        """Filtering on is_active would report "nothing on file" for a
        correction that was recorded — the lie this whole task removes."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections SET is_active = 0, conflict_flagged = 1")
        in_memory_db.commit()

        assert _texts(engine.get_corrections_for_run("wf-1")) == [_TEXT]

    def test_a_credit_source_row_is_not_a_correction(self, in_memory_db):
        """T3 writes source_kind='query' rows for hint CREDITS, in the same
        table. Reading on source_hash alone would report a credit as something
        the user typed, so both halves of the claim's identity are pinned."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        hint_id = engine.get_corrections_for_run("wf-1")[0]["hint_id"]
        # Same hash, wrong kind and bucket — a credit, not a submission.
        in_memory_db.execute(
            "INSERT INTO hint_evidence "
            "(hint_id, source_kind, source_key, source_hash, bucket, created_at) "
            "VALUES (?, 'query', 'verify the dashboard loads', ?, 'used', ?)",
            (hint_id, run_source_hash("wf-1"), _NOW.isoformat()),
        )
        in_memory_db.commit()

        assert len(engine.get_corrections_for_run("wf-1")) == 1

    def test_a_run_that_never_submitted_anything_is_empty(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)

        assert engine.get_corrections_for_run("wf-never") == []

    def test_a_correction_carries_every_column_can_retract_is_built_from(
            self, in_memory_db):
        """T7: GET /api/feedback/{run_id} (endpoints.py) computes can_retract
        from THREE columns — org_id and created_by_user_id feed
        hint_mutation_verdict, and is_active decides whether the action would
        do anything. None of the three reaches the client, but the engine must
        hand all three to that caller: this is the one place the predicate
        would silently break if the SELECT stopped carrying one, and asserting
        only two of three would have let the third go missing."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(
            _record("wf-1"),
            {"category": "keyword", "feedback_text": _TEXT,
             "actor": "author@e.com", "actor_user_id": "u-author"},
        )

        row = engine.get_corrections_for_run("wf-1")[0]
        assert row["org_id"] == _ORG
        assert row["created_by_user_id"] == "u-author"
        # is_active is selected WITHOUT being filtered on (see the class
        # above): can_retract must be false for a hint already retracted, so
        # the value has to travel even though the row is returned either way.
        assert row["is_active"] == 1


class TestItNeverRaisesIntoTheRequest:
    """The panel calls this on mount for every finished run. An exception is a
    500 on a page whose run just succeeded.

    No test here for `NLFeedbackEngine(None)`: the `not self._em` guard is
    house style (every sibling read has it) but it cannot be pinned — without
    it the call raises AttributeError, the same `except` catches it, and the
    method still answers []. A test that passes either way is noise.
    """

    def test_a_broken_read_returns_empty(self, in_memory_db):
        """The failure is injected at the connection, NOT by dropping the
        table: this fixture's search_path is `{test_schema},public`, so a
        DROP takes the test copy out of the way and the SELECT quietly reads
        the LIVE public one instead — [] for entirely the wrong reason."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        assert engine.get_corrections_for_run("wf-1"), "seeded row must be there"

        with patch.object(in_memory_db, "read_conn",
                          side_effect=RuntimeError("connection pool exhausted")):
            assert engine.get_corrections_for_run("wf-1") == []

    def test_an_empty_run_id_matches_no_claim(self, in_memory_db):
        """`sha256("")` is a perfectly good hash of nothing, so without the
        falsy guard an empty id would match a claim row carrying that hash.
        Seeded directly — T5 never writes one, which is exactly why the read
        must not be the thing that starts trusting it."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())
        hint_id = engine.get_corrections_for_run("wf-1")[0]["hint_id"]
        in_memory_db.execute(
            "INSERT INTO hint_evidence "
            "(hint_id, source_kind, source_key, source_hash, bucket, created_at) "
            "VALUES (?, 'workflow', '', ?, 'evidence', ?)",
            (hint_id, run_source_hash(""), _NOW.isoformat()),
        )
        in_memory_db.commit()

        assert engine.get_corrections_for_run("") == []
        assert engine.get_corrections_for_run(None) == []
