"""
T5 — one correction per run, counted once.

Re-submitting the same correction from the SAME run must not count twice. The
gate is a hint_evidence row keyed (hint_id, 'workflow', sha256(workflow_id),
'evidence'), written in the same transaction as the create/reinforce — so the
token is exactly as durable as the effects it authorises, and a rolled-back
submission leaves no phantom.

The row is written in BOTH branches (R1). The create branch writes none of the
UPSERT effects, so if it wrote no evidence row the second submission would find
none, create one, and reinforce — only the third would be blocked. That is the
ordinary user flow: Generate -> Run -> Feedback -> edit -> Run again (same
workflow_id) -> Feedback again.

What the gate deliberately leaves open: the SAME text from a DIFFERENT run. That
is Gap 7's recovery path — a user re-affirming a hint the LLM wrongly flagged —
and it still unflags, reactivates and resets unused_count.

Real Postgres via the in_memory_db fixture (never mocked — optimization rule).
"""

import hashlib
from datetime import datetime, timezone

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import (
    GATED_INACTIVE_HINT,
    NLFeedbackEngine,
)


_NOW = datetime.now(timezone.utc)
_TEXT = "wait for the spinner to disappear before asserting"


# T9: hint writes fail closed without an org, so records here name one.
_ORG = "org-A"


def _record(workflow_id, *, url="https://shop.test/dash", org_id=_ORG):
    return ExecutionRecord(
        workflow_id=workflow_id, timestamp=_NOW,
        user_query="verify the dashboard loads", url=url, domain="shop.test",
        test_status="failed", org_id=org_id,
    )


def _triage(text=_TEXT, actor="tester"):
    return {"category": "keyword", "feedback_text": text, "actor": actor}


def _triage_locator(text=_TEXT):
    """category 'locator' -> scope 'url' -> the OTHER dedup SELECT branch."""
    return {"category": "locator", "feedback_text": text, "actor": "tester"}


def _hints(conn):
    return conn.execute(
        "SELECT id, feedback_text, evidence_count, unused_count, is_active, "
        "       conflict_flagged, last_seen "
        "FROM nl_feedback_corrections ORDER BY id"
    ).fetchall()


def _evidence(conn, hint_id):
    return conn.execute(
        "SELECT source_kind, source_key, source_hash, bucket "
        "FROM hint_evidence WHERE hint_id = ? ORDER BY id",
        (hint_id,),
    ).fetchall()


def _seed_hint(conn, text=_TEXT, *, conflict_flagged=0):
    """A hint that already exists — the engine's dedup key is
    (feedback_text, domain, scope, org_id), so the domain must match the
    record's or the submission takes the create branch instead."""
    cur = conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, evidence_count, is_active, "
        " conflict_flagged, created_at, last_seen, org_id) "
        "VALUES (?, 'keyword', 'global', 'shop.test', 1, 1, ?, ?, ?, ?) "
        "RETURNING id",
        (text, conflict_flagged, _NOW.isoformat(), _NOW.isoformat(), _ORG),
    )
    hid = cur.fetchone()["id"]
    conn.commit()
    return hid


class TestSameRunCountsOnce:
    """The four cases of T5 Step 1."""

    def test_new_text_twice_from_one_run_counts_once(self, in_memory_db):
        """(a) The create branch must leave the token behind, or the FIRST
        repeat slips through and only the third is blocked (R1)."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-1"), _triage())

        rows = _hints(in_memory_db)
        assert len(rows) == 1
        hid = rows[0]["id"]
        # State the second submission would otherwise change.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections "
            "SET unused_count = 7, last_seen = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?", (hid,),
        )
        in_memory_db.commit()

        engine.learn_from_feedback(_record("wf-1"), _triage())

        row = _hints(in_memory_db)[0]
        assert row["evidence_count"] == 1, (
            "the same run's second submission reinforced the hint it had just "
            "created — the create branch wrote no gate token"
        )
        assert row["unused_count"] == 7, "gated submission reset unused_count"
        assert row["last_seen"] == "2020-01-01T00:00:00+00:00", (
            "gated submission refreshed last_seen"
        )

    def test_existing_text_twice_from_one_run_counts_once(self, in_memory_db):
        """(b) The UPSERT branch: one reinforcement, and a flag raised between
        the two submissions survives the second."""
        hid = _seed_hint(in_memory_db)
        engine = NLFeedbackEngine(in_memory_db)

        engine.learn_from_feedback(_record("wf-2"), _triage())
        assert _hints(in_memory_db)[0]["evidence_count"] == 2

        # Trigger 2 flags the hint between the two submissions.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections "
            "SET conflict_flagged = 1, conflict_flag_reason = 'llm' WHERE id = ?",
            (hid,),
        )
        in_memory_db.commit()

        engine.learn_from_feedback(_record("wf-2"), _triage())

        row = _hints(in_memory_db)[0]
        assert row["evidence_count"] == 2, "the same run counted twice"
        assert row["conflict_flagged"] == 1, (
            "the same run's duplicate submission unflagged the hint — the gate "
            "must cover every UPSERT effect, the audit row included"
        )
        audit = in_memory_db.execute(
            "SELECT id FROM hint_audit WHERE hint_id = ? AND action = 'unflag'",
            (hid,),
        ).fetchall()
        assert audit == [], "gated submission wrote an unflag audit row"

    def test_same_text_from_another_run_still_counts_and_unflags(self, in_memory_db):
        """(c) Gap 7's recovery path — the gate is per run, not per hint."""
        hid = _seed_hint(in_memory_db, conflict_flagged=1)
        engine = NLFeedbackEngine(in_memory_db)

        engine.learn_from_feedback(_record("wf-3a"), _triage())
        engine.learn_from_feedback(_record("wf-3b"), _triage())

        row = _hints(in_memory_db)[0]
        assert row["evidence_count"] == 3, (
            "a different run's identical feedback was gated — that is the "
            "cross-run recovery path and must stay open"
        )
        assert row["conflict_flagged"] == 0
        assert len(_evidence(in_memory_db, hid)) == 2, "one token per run"

    def test_url_scoped_hints_are_gated_on_the_same_run(self, in_memory_db):
        """The dedup SELECT has two branches — url-scoped hints key on url as
        well. The gate sits after they converge, so both are covered; this
        fails if it is ever moved inside one branch.
        """
        engine = NLFeedbackEngine(in_memory_db)
        # category 'locator' -> scope 'url'
        engine.learn_from_feedback(_record("wf-url"), _triage_locator())
        engine.learn_from_feedback(_record("wf-url"), _triage_locator())

        rows = _hints(in_memory_db)
        assert len(rows) == 1
        assert rows[0]["evidence_count"] == 1, "the url-scoped branch is ungated"

        # A different page of the same domain is a different hint, and a later
        # run reinforces — neither is collateral of the gate.
        engine.learn_from_feedback(
            _record("wf-url", url="https://shop.test/cart"), _triage_locator())
        engine.learn_from_feedback(_record("wf-url-2"), _triage_locator())

        rows = _hints(in_memory_db)
        assert len(rows) == 2
        assert rows[0]["evidence_count"] == 2
        assert rows[1]["evidence_count"] == 1

    def test_different_text_on_one_run_counts_separately(self, in_memory_db):
        """(d) The gate is per (hint, run) — a second correction on the same run
        is a different hint and must be stored."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-4"), _triage())
        engine.learn_from_feedback(_record("wf-4"), _triage("use the stable id"))

        rows = _hints(in_memory_db)
        assert len(rows) == 2, "the run's second, different correction was lost"
        assert {r["evidence_count"] for r in rows} == {1}


class TestGateToken:
    """The token's shape — it must be invisible to the lifecycle rules."""

    def test_token_is_workflow_kind_and_does_not_inflate_source_counts(
        self, in_memory_db,
    ):
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-5"), _triage())
        hid = _hints(in_memory_db)[0]["id"]

        rows = _evidence(in_memory_db, hid)
        assert len(rows) == 1
        assert rows[0]["source_kind"] == "workflow"
        assert rows[0]["bucket"] == "evidence"
        assert rows[0]["source_key"] == "wf-5"
        assert rows[0]["source_hash"] == hashlib.sha256(b"wf-5").hexdigest()

        # T4's three lifecycle rules count source_kind='query' rows only. A
        # 'query' row here would silently inflate used_src and shield the hint.
        assert engine._source_counts(hid) == {
            "used_src": 0, "failure_src": 0, "unused_src": 0,
        }

    def test_feedback_without_a_run_is_not_gated(self, in_memory_db):
        """A record with no workflow_id has no run to be idempotent about.

        The honest default is to proceed ungated: sha256(None) raises, and
        learn_from_feedback rolls back and re-raises, so gating on a missing id
        would discard the correction entirely — reported now rather than
        silent, but discarded either way. The product path always has an id
        (the record is read back by workflow_id), so this covers direct/API
        callers only.
        """
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record(None), _triage())
        engine.learn_from_feedback(_record(None), _triage())

        rows = _hints(in_memory_db)
        assert len(rows) == 1
        assert rows[0]["evidence_count"] == 2, (
            "a run-less submission was gated — the correction is discarded, "
            "not deduplicated"
        )
        assert _evidence(in_memory_db, rows[0]["id"]) == [], (
            "no run, no token: an empty source_hash would gate every "
            "run-less caller against every other one"
        )


class TestClaimConflictTarget:
    """M12: the claim's ON CONFLICT DO NOTHING had no explicit target, so
    Postgres applied it to a violation of ANY unique index on hint_evidence,
    not just uq_hint_evidence's own (hint_id, source_kind, source_hash,
    bucket) key. Simulate an unrelated unique index (a plausible future
    index, or a bug) and confirm a genuinely NEW claim is never silently
    read as 'already claimed' because of it.

    Real Postgres via in_memory_db — the collision has to be a real
    constraint violation, not something a mock can fake.
    """

    def test_unrelated_index_collision_does_not_silently_gate_a_new_claim(
        self, in_memory_db,
    ):
        engine = NLFeedbackEngine(in_memory_db)
        hid1 = _seed_hint(in_memory_db, text="hint one")
        hid2 = _seed_hint(in_memory_db, text="hint two")

        in_memory_db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS test_m12_uq_source_key "
            "ON hint_evidence (source_key)"
        )
        in_memory_db.commit()
        try:
            assert engine._claim_feedback_run(
                hid1, "wf-shared", "hashA", _NOW.isoformat()) is True

            # hid2's (hint_id, source_kind, source_hash, bucket) tuple has
            # NEVER been claimed — it must not be gated. It shares
            # source_key ('wf-shared') with hid1's row, which collides with
            # the unrelated index above but not with uq_hint_evidence.
            try:
                claimed2 = engine._claim_feedback_run(
                    hid2, "wf-shared", "hashB", _NOW.isoformat())
            except Exception:
                # An explicit conflict target makes Postgres raise on a
                # collision it was never told to swallow — loud, not silent.
                in_memory_db.rollback()
            else:
                assert claimed2 is True, (
                    "hid2's first-ever claim was silently gated by an "
                    "unrelated unique-index collision — ON CONFLICT had no "
                    "explicit target"
                )
        finally:
            in_memory_db.rollback()
            in_memory_db.execute("DROP INDEX IF EXISTS test_m12_uq_source_key")
            in_memory_db.commit()


class TestRetractThenResubmit:
    """What the run-level gate does to a RETRACTED hint, pinned in both rows.

    The gate returns False before the UPDATE that carries `SET is_active = 1`,
    so which run the resubmission comes from decides whether a retracted hint
    comes back. Neither row was asserted anywhere: grep for `assert.*is_active`
    across this suite returned exactly one hit, on an INITIAL value.

    This is deliberate behaviour, not a defect being enshrined: every effect in
    that UPDATE is a per-run fact and T5 gates them together, and carving
    `is_active` out would let one effect fire without the evidence token that
    authorises it. What was wrong is what the USER was told — POST /api/feedback
    answered outcome "processed" and the SPA rendered "Thanks - your feedback
    helps the system learn" for the same-run case below, in which nothing at
    all happened. Fix wave D closed that half: the gate now RETURNS
    GATED_INACTIVE_HINT when the hint it declined to reinstate was inactive, and
    the return value is what /api/feedback reports. Both rows are asserted here
    — the row that must signal, and the row that must not.
    """

    def test_the_same_run_cannot_reinstate_a_hint_it_retracted(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-retract"), _triage())
        hid = _hints(in_memory_db)[0]["id"]

        # What POST /hints/{id}/retract does to the row.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections SET is_active = 0 WHERE id = ?",
            (hid,),
        )
        in_memory_db.commit()

        signal = engine.learn_from_feedback(_record("wf-retract"), _triage())

        # The user typed their correction and got "Thanks - your feedback helps
        # the system learn" for a submission that did nothing, and could not
        # undo it: /learning is closed to a plain org member and reactivate is
        # org-admin-and-above. The gate is the only place that knows, so the
        # gate is what has to say so.
        assert signal == GATED_INACTIVE_HINT, (
            "the gate declined to reinstate an INACTIVE hint and returned "
            "nothing, so every layer above it reports this no-op as a stored "
            "correction"
        )

        row = _hints(in_memory_db)[0]
        assert row["is_active"] == 0, (
            "the same run's resubmission reinstated a retracted hint, which "
            "means it ran the UPSERT the run-level gate exists to stop"
        )
        assert row["evidence_count"] == 1, "the same run counted twice"
        assert len(_evidence(in_memory_db, hid)) == 1, "one token per run"

    def test_a_different_run_does_reinstate_it(self, in_memory_db):
        """The other row, and the reason the first is not simply "retracted
        hints stay retracted": the gate is per RUN. Fresh evidence from a
        different run is Gap 7's recovery path and reactivates the hint."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-retract-a"), _triage())
        hid = _hints(in_memory_db)[0]["id"]

        in_memory_db.execute(
            "UPDATE nl_feedback_corrections SET is_active = 0 WHERE id = ?",
            (hid,),
        )
        in_memory_db.commit()

        signal = engine.learn_from_feedback(_record("wf-retract-b"), _triage())

        # Nothing to report: the hint IS reinstated, so "processed" is the true
        # answer and the endpoint must keep giving it.
        assert signal is None, (
            "a resubmission that DID reinstate the hint must not be reported "
            "as a no-op"
        )

        row = _hints(in_memory_db)[0]
        assert row["is_active"] == 1, (
            "a different run's identical feedback must reinstate the hint - "
            "that is the cross-run recovery path"
        )
        assert row["evidence_count"] == 2
        assert row["evidence_count"] == 2


class TestTheGateOnlySignalsTheInactiveCase:
    """The sentinel is not "the gate fired" — it is "the gate fired AND the
    hint it declined to reinstate is switched off".

    An ordinary same-run duplicate hits the identical `return`, and there
    "Thanks - your feedback helps the system learn" is TRUE: the guidance is on
    file, active, and injecting into every matching prompt. Reporting that as a
    no-op would trade one false statement for another.
    """

    def test_a_duplicate_on_an_ACTIVE_hint_still_reports_nothing(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-dup"), _triage())
        assert _hints(in_memory_db)[0]["is_active"] == 1

        signal = engine.learn_from_feedback(_record("wf-dup"), _triage())

        assert signal is None, (
            "the ordinary same-run duplicate must stay 'processed' - the hint "
            "is on file and injecting, which is what the message claims"
        )

    def test_a_first_submission_reports_nothing(self, in_memory_db):
        """The create branch has its own claim site and never reaches the
        gate's `return`."""
        engine = NLFeedbackEngine(in_memory_db)

        signal = engine.learn_from_feedback(_record("wf-new"), _triage())

        assert signal is None
        assert len(_hints(in_memory_db)) == 1

    def test_empty_text_cannot_reach_the_gate_at_all(self, in_memory_db):
        """Why `no_text` still wins over the new outcome, structurally rather
        than by ordering: learn_from_feedback returns on empty/blank text
        BEFORE the dedup SELECT and before the claim, so the gate can never
        fire for a submission Step 4b would refine. Asserted on a run that has
        already claimed a retracted hint — the exact state in which the gate
        WOULD fire if the text carried words."""
        engine = NLFeedbackEngine(in_memory_db)
        engine.learn_from_feedback(_record("wf-blank"), _triage())
        hid = _hints(in_memory_db)[0]["id"]
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections SET is_active = 0 WHERE id = ?",
            (hid,),
        )
        in_memory_db.commit()

        assert engine.learn_from_feedback(_record("wf-blank"), _triage("")) is None
        assert engine.learn_from_feedback(_record("wf-blank"), _triage("   ")) is None

