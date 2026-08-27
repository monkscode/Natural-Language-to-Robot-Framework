"""
T3 — every hint credit records the independent SOURCE that produced it.

The four counters on nl_feedback_corrections count OUTCOMES: one query re-run
five times is indistinguishable from five different queries agreeing. Those
counters cannot be redefined (they feed the conflict-detection prompt, the review
dashboard and the FR5 identity), so apply_hint_attribution additionally writes one
hint_evidence row per (hint, source, bucket). The source is the run's normalised
user_query; repeats collapse via ON CONFLICT DO NOTHING while the event counters
keep counting events.

Ordering is load-bearing and pinned here: the INSERT runs AFTER the shield
(_flag_hints_no_commit, which reads pre-increment counts) and BEFORE
_maybe_auto_disable_or_retire (which reads post-increment counts). Inverting
either silently moves a shipped threshold by one.

Real Postgres via the in_memory_db fixture (never mocked — optimization rule).
"""

import hashlib

from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine


_TS = "2026-01-01T00:00:00+00:00"


def _insert_exec(conn, workflow_id, user_query):
    """Insert an execution_records row the attribution claim can target."""
    conn.execute(
        "INSERT INTO execution_records "
        "(workflow_id, timestamp, user_query, test_status, hint_attribution_done) "
        "VALUES (?, ?, ?, 'passed', 0)",
        (workflow_id, _TS, user_query),
    )


def _insert_hint(conn, text, *, success=0, failure=0, unused=0):
    cur = conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, applied_count, success_count, "
        " failure_count, unused_count, is_active, conflict_flagged, "
        " created_at, last_seen) "
        "VALUES (?, 'C1', 'global', 0, ?, ?, ?, 1, 0, ?, ?) RETURNING id",
        (text, success, failure, unused, _TS, _TS),
    )
    return cur.fetchone()["id"]


def _evidence(conn, hint_id):
    return conn.execute(
        "SELECT source_kind, source_key, source_hash, bucket "
        "FROM hint_evidence WHERE hint_id = ? ORDER BY id",
        (hint_id,),
    ).fetchall()


def _counters(conn, hint_id):
    row = conn.execute(
        "SELECT applied_count, success_count, failure_count, unused_count "
        "FROM nl_feedback_corrections WHERE id = ?",
        (hint_id,),
    ).fetchone()
    return (row["applied_count"], row["success_count"],
            row["failure_count"], row["unused_count"])


class TestOneSourcePerQuery:

    def test_same_query_from_two_runs_is_one_source_but_two_events(self, in_memory_db):
        """(a) Repetition adds no evidence — but the event counters still count."""
        hid = _insert_hint(in_memory_db, "wait for the grid")
        _insert_exec(in_memory_db, "wf-a1", "  Search FOR shoes  ")
        _insert_exec(in_memory_db, "wf-a2", "search for shoes")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-a1", [hid], [], [])
        engine.apply_hint_attribution("wf-a2", [hid], [], [])

        rows = _evidence(in_memory_db, hid)
        assert len(rows) == 1, f"one source, got {len(rows)} evidence rows"
        assert rows[0]["source_kind"] == "query"
        assert rows[0]["bucket"] == "used"
        assert rows[0]["source_key"] == "search for shoes"
        assert rows[0]["source_hash"] == hashlib.sha256(
            b"search for shoes"
        ).hexdigest()
        # F6 — the event counters are additive and untouched by the collapse.
        assert _counters(in_memory_db, hid) == (2, 2, 0, 0)

    def test_two_different_queries_are_two_sources(self, in_memory_db):
        """(b) Independent queries agreeing is the signal the counters lost."""
        hid = _insert_hint(in_memory_db, "wait for the grid")
        _insert_exec(in_memory_db, "wf-b1", "search for shoes")
        _insert_exec(in_memory_db, "wf-b2", "open the cart")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-b1", [hid], [], [])
        engine.apply_hint_attribution("wf-b2", [hid], [], [])

        rows = _evidence(in_memory_db, hid)
        assert [r["source_key"] for r in rows] == [
            "search for shoes", "open the cart",
        ]
        assert _counters(in_memory_db, hid) == (2, 2, 0, 0)

    def test_same_query_in_a_different_bucket_adds_a_row(self, in_memory_db):
        """(c) An early failure must not freeze a later success from that query."""
        hid = _insert_hint(in_memory_db, "wait for the grid")
        _insert_exec(in_memory_db, "wf-c1", "search for shoes")
        _insert_exec(in_memory_db, "wf-c2", "search for shoes")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-c1", [], [hid], [],
                                      reasons={hid: "removed and judged harmful"})
        # The failure credit also flags the hint, and a flagged hint is excluded
        # from the next attribution's PRE-increment read. Clearing the flag is
        # the documented recovery path (user re-submits the feedback text, or an
        # admin unflags), and is what lets the same source reach a second bucket.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections SET conflict_flagged = 0 WHERE id = ?",
            (hid,),
        )
        in_memory_db.commit()
        engine.apply_hint_attribution("wf-c2", [hid], [], [])

        rows = _evidence(in_memory_db, hid)
        assert [r["bucket"] for r in rows] == ["failure", "used"]
        assert len({r["source_hash"] for r in rows}) == 1  # one source, two buckets


class TestInsertOrdering:

    def test_evidence_lands_after_the_shield_and_before_the_disable_check(
        self, in_memory_db,
    ):
        """T3 Step 2b — the shield reads PRE-insert counts, the disable/retire
        rules read POST-insert counts. Mirrors the pre/post-increment split the
        event counters already have; inverting it moves two shipped thresholds.
        """
        hid = _insert_hint(in_memory_db, "ordering")
        _insert_exec(in_memory_db, "wf-order", "ordering query")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        seen: dict[str, int] = {}

        def _rows_now():
            return in_memory_db.execute(
                "SELECT COUNT(*) AS n FROM hint_evidence WHERE hint_id = ?",
                (hid,),
            ).fetchone()["n"]

        orig_flag = engine._flag_hints_no_commit
        orig_rule = engine._maybe_auto_disable_or_retire

        def spy_flag(*args, **kwargs):
            seen["at_shield"] = _rows_now()
            return orig_flag(*args, **kwargs)

        def spy_rule(*args, **kwargs):
            seen["at_disable"] = _rows_now()
            return orig_rule(*args, **kwargs)

        engine._flag_hints_no_commit = spy_flag
        engine._maybe_auto_disable_or_retire = spy_rule

        engine.apply_hint_attribution("wf-order", [], [hid], [],
                                      reasons={hid: "removed"})

        assert seen["at_shield"] == 0, "shield must read PRE-insert source counts"
        assert seen["at_disable"] == 1, "disable/retire must read POST-insert counts"
