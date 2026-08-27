"""
T4 — the three hint lifecycle rules read DIVERSITY from hint_evidence and
QUALITY (plus every `== 0` guard) from the event counters.

  shield       used_src >= 5 AND success/(success+failure) >= 0.70
  auto-disable failure_src > 0 AND success_count == 0 AND used_src >= 3
  auto-retire  unused_src >= 5 AND unused_count >= 5 AND success_count == 0
               AND age >= 30d

Each rule is pinned twice: N outcomes from ONE query must not trip it, N outcomes
from N distinct queries must. Two counterexamples pin why the split is a hybrid
and not pure source counting:

  - a coin-flip hint (5 queries, each one help and one failure) has 5 used
    sources but an event rate of 0.50, and must NOT be shielded — a source rate
    would read 5/5 = 1.00 and shield a hint that fails half its events;
  - a hint whose successes predate hint_evidence has success_src = 0 while
    success_count > 0, and must NOT be auto-disabled or retired — the `== 0`
    guards read the event counters, which is what makes the never-kill-a-helper
    invariant hold with no backfill.

The auto-retire rule additionally keeps its event leg, because unused_count is
the one counter that is RESET (learn_from_feedback's UPSERT branch, "a
re-submission is a fresh chance"). hint_evidence is append-only, so a source-only
rule would silently void that reset — pinned by the last test here.

Real Postgres via the in_memory_db fixture (never mocked — optimization rule).
"""

import hashlib
from datetime import datetime, timedelta, timezone

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import (
    NLFeedbackEngine,
    AUTO_DISABLE_MIN_APPLICATIONS,
    TRIGGER_FLAG_PROTECTION_MIN_APPLIED,
    UNUSED_RETIRE_THRESHOLD,
    UNUSED_RETIRE_MIN_AGE_DAYS,
)


_NOW = datetime.now(timezone.utc)
_RECENT = _NOW.isoformat()
_OLD = (_NOW - timedelta(days=UNUSED_RETIRE_MIN_AGE_DAYS + 10)).isoformat()


def _insert_exec(conn, workflow_id, user_query):
    conn.execute(
        "INSERT INTO execution_records "
        "(workflow_id, timestamp, user_query, test_status, hint_attribution_done) "
        "VALUES (?, '2026-01-01T00:00:00+00:00', ?, 'passed', 0)",
        (workflow_id, user_query),
    )


def _insert_hint(conn, text, *, success=0, failure=0, unused=0,
                 created_at=_RECENT):
    cur = conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, applied_count, success_count, "
        " failure_count, unused_count, is_active, conflict_flagged, "
        " created_at, last_seen) "
        "VALUES (?, 'C1', 'global', 0, ?, ?, ?, 1, 0, ?, ?) RETURNING id",
        (text, success, failure, unused, created_at, created_at),
    )
    return cur.fetchone()["id"]


def _seed_evidence(conn, hint_id, bucket, queries):
    """Evidence rows for outcomes that already happened, one per query."""
    for q in queries:
        conn.execute(
            "INSERT INTO hint_evidence "
            "(hint_id, source_kind, source_key, source_hash, bucket, created_at) "
            "VALUES (?, 'query', ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (hint_id, q, hashlib.sha256(q.encode("utf-8")).hexdigest(),
             bucket, _RECENT),
        )


def _hint_row(conn, hint_id):
    return conn.execute(
        "SELECT applied_count, success_count, failure_count, unused_count, "
        "       is_active, conflict_flagged "
        "FROM nl_feedback_corrections WHERE id = ?",
        (hint_id,),
    ).fetchone()


def _disable_reason(conn, hint_id):
    row = conn.execute(
        "SELECT reason FROM hint_audit "
        "WHERE hint_id = ? AND action = 'auto_disable'",
        (hint_id,),
    ).fetchone()
    return row["reason"] if row else None


# ===================================================================
# Shield — flagging protection needs independent sources
# ===================================================================

class TestShieldNeedsDistinctSources:

    def test_five_successes_from_one_query_stays_flaggable(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "helped one query five times",
                           success=TRIGGER_FLAG_PROTECTION_MIN_APPLIED)
        _seed_evidence(in_memory_db, hid, "used", ["search for shoes"])
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        flagged = engine._flag_hints_no_commit({hid: "bad"}, "trigger_2", _RECENT)

        assert flagged == [hid], "repetition must not buy a shield"
        assert _hint_row(in_memory_db, hid)["conflict_flagged"] == 1

    def test_five_successes_from_five_queries_is_shielded(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "helped five different queries",
                           success=TRIGGER_FLAG_PROTECTION_MIN_APPLIED)
        _seed_evidence(in_memory_db, hid, "used",
                       [f"query {i}" for i in range(TRIGGER_FLAG_PROTECTION_MIN_APPLIED)])
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        flagged = engine._flag_hints_no_commit({hid: "bad"}, "trigger_2", _RECENT)

        assert flagged == []
        assert _hint_row(in_memory_db, hid)["conflict_flagged"] == 0

    def test_coin_flip_hint_is_not_shielded(self, in_memory_db):
        """S4 (a) — 5 distinct queries, each one help and one failure. Source
        diversity is satisfied; the event rate (5/10 = 0.50) is not."""
        queries = [f"query {i}" for i in range(TRIGGER_FLAG_PROTECTION_MIN_APPLIED)]
        hid = _insert_hint(in_memory_db, "helps half the time",
                           success=len(queries), failure=len(queries))
        _seed_evidence(in_memory_db, hid, "used", queries)
        _seed_evidence(in_memory_db, hid, "failure", queries)
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        flagged = engine._flag_hints_no_commit({hid: "bad"}, "trigger_2", _RECENT)

        assert flagged == [hid], (
            "a hint that fails half its events must stay flaggable — a "
            "success-wins source rate would read 1.00 and shield it"
        )


# ===================================================================
# Auto-disable — never-succeeded needs independent sources
# ===================================================================

class TestAutoDisableNeedsDistinctSources:

    def test_three_failures_from_one_query_stays_active(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "failed one query repeatedly",
                           failure=AUTO_DISABLE_MIN_APPLICATIONS - 1)
        _seed_evidence(in_memory_db, hid, "failure", ["same query"])
        _insert_exec(in_memory_db, "wf-1q", "same query")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-1q", [], [hid], [])

        row = _hint_row(in_memory_db, hid)
        assert row["failure_count"] == AUTO_DISABLE_MIN_APPLICATIONS
        assert row["is_active"] == 1, "one query repeating is one piece of evidence"

    def test_three_failures_from_three_queries_disables(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "failed three different queries",
                           failure=AUTO_DISABLE_MIN_APPLICATIONS - 1)
        _seed_evidence(in_memory_db, hid, "failure", ["query one", "query two"])
        _insert_exec(in_memory_db, "wf-3q", "query three")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-3q", [], [hid], [])

        row = _hint_row(in_memory_db, hid)
        assert row["failure_count"] == AUTO_DISABLE_MIN_APPLICATIONS
        assert row["is_active"] == 0
        assert _disable_reason(in_memory_db, hid) == "never_succeeded"

    def test_legacy_success_count_blocks_disable(self, in_memory_db):
        """S4 (b) — successes that predate hint_evidence still protect. Every
        source leg passes here; only the event `success_count == 0` guard holds."""
        hid = _insert_hint(in_memory_db, "helped before hint_evidence existed",
                           success=3, failure=AUTO_DISABLE_MIN_APPLICATIONS - 1)
        _seed_evidence(in_memory_db, hid, "failure", ["query one", "query two"])
        _insert_exec(in_memory_db, "wf-legacy", "query three")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-legacy", [], [hid], [])

        row = _hint_row(in_memory_db, hid)
        assert row["failure_count"] == AUTO_DISABLE_MIN_APPLICATIONS
        assert row["is_active"] == 1, (
            "success_src is 0 for a legacy helper — the guard must read "
            "success_count, or three failures would kill a hint that has helped"
        )


# ===================================================================
# Auto-retire — over-surfaced dead weight needs independent sources
# ===================================================================

class TestAutoRetireNeedsDistinctSources:

    def test_five_unused_from_one_query_stays_active(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "surfaced on one query repeatedly",
                           unused=UNUSED_RETIRE_THRESHOLD - 1, created_at=_OLD)
        _seed_evidence(in_memory_db, hid, "unused", ["same query"])
        _insert_exec(in_memory_db, "wf-u1", "same query")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-u1", [], [], [hid])

        row = _hint_row(in_memory_db, hid)
        assert row["unused_count"] == UNUSED_RETIRE_THRESHOLD
        assert row["is_active"] == 1

    def test_five_unused_from_five_queries_retires(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "surfaced everywhere, used nowhere",
                           unused=UNUSED_RETIRE_THRESHOLD - 1, created_at=_OLD)
        _seed_evidence(in_memory_db, hid, "unused",
                       [f"query {i}" for i in range(UNUSED_RETIRE_THRESHOLD - 1)])
        _insert_exec(in_memory_db, "wf-u5", "one more query")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-u5", [], [], [hid])

        row = _hint_row(in_memory_db, hid)
        assert row["unused_count"] == UNUSED_RETIRE_THRESHOLD
        assert row["is_active"] == 0
        assert _disable_reason(in_memory_db, hid) == "never_used"

    def test_age_floor_still_applies_with_diverse_sources(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "new but widely surfaced",
                           unused=UNUSED_RETIRE_THRESHOLD - 1, created_at=_RECENT)
        _seed_evidence(in_memory_db, hid, "unused",
                       [f"query {i}" for i in range(UNUSED_RETIRE_THRESHOLD - 1)])
        _insert_exec(in_memory_db, "wf-young", "one more query")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-young", [], [], [hid])

        assert _hint_row(in_memory_db, hid)["is_active"] == 1

    def test_legacy_success_count_blocks_retire(self, in_memory_db):
        """S4 (b), retire half — same hint shape, the other automatic rule."""
        hid = _insert_hint(in_memory_db, "helped once long ago", success=3,
                           unused=UNUSED_RETIRE_THRESHOLD - 1, created_at=_OLD)
        _seed_evidence(in_memory_db, hid, "unused",
                       [f"query {i}" for i in range(UNUSED_RETIRE_THRESHOLD - 1)])
        _insert_exec(in_memory_db, "wf-legacy-u", "one more query")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-legacy-u", [], [], [hid])

        assert _hint_row(in_memory_db, hid)["is_active"] == 1

    def test_resubmission_reset_still_delays_retirement(self, in_memory_db):
        """unused_count is the one counter that RESETS — re-submitting identical
        feedback is a fresh chance (learn_from_feedback's UPSERT branch, Step
        4b). hint_evidence is append-only and cannot express that, so the retire
        rule keeps its event leg; a source-only rule would retire this hint on
        the very next unused outcome and silently void the reset.
        """
        engine = NLFeedbackEngine(in_memory_db)
        record = ExecutionRecord(
            workflow_id="wf-orig", timestamp=_NOW,
            user_query="check the dashboard", url="https://shop.test/dash",
            domain="shop.test", test_status="failed",
            user_feedback="wait for the spinner to disappear",
        )
        triage = {"feedback_text": record.user_feedback, "category": "keyword"}
        engine.learn_from_feedback(record, triage)
        hid = in_memory_db.execute(
            "SELECT id FROM nl_feedback_corrections"
        ).fetchone()["id"]

        # Over-surfaced on five distinct queries, never used, past the age floor.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections "
            "SET unused_count = ?, created_at = ? WHERE id = ?",
            (UNUSED_RETIRE_THRESHOLD, _OLD, hid),
        )
        _seed_evidence(in_memory_db, hid, "unused",
                       [f"query {i}" for i in range(UNUSED_RETIRE_THRESHOLD)])
        in_memory_db.commit()

        # The user re-submits the same correction: unused_count -> 0.
        engine.learn_from_feedback(record, triage)
        assert _hint_row(in_memory_db, hid)["unused_count"] == 0

        _insert_exec(in_memory_db, "wf-after-reset", "query 0")
        in_memory_db.commit()
        engine.apply_hint_attribution("wf-after-reset", [], [], [hid])

        row = _hint_row(in_memory_db, hid)
        assert row["unused_count"] == 1
        assert row["is_active"] == 1, (
            "the re-submission's fresh chance must survive — five stale unused "
            "sources cannot retire the hint on the next unused outcome"
        )
