"""
Usage-aware hint attribution (Part 2) — apply_hint_attribution + the shared
no-commit flagging core.

apply_hint_attribution is the SOLE writer of NL-hint usage counters. It runs on
the writer thread inside one atomic transaction: an atomic once-guard claim
against execution_records.hint_attribution_done, then bucket increments
(used→success, failure→failure+flag, unused→unused), then the automatic
disable/retire rules (never-succeeded; G1 over-surfaced dead weight). The
high-harm-ratio rule is inform-only this phase (FR2).

Real SQLite via the in_memory_db fixture (never mocked — optimization rule). The
conftest renames the pytest thread to the writer thread, so direct calls satisfy
_assert_writer_thread.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.backend.crew_ai.optimization.nl_feedback_engine import (
    NLFeedbackEngine,
    AUTO_DISABLE_MIN_APPLICATIONS,
    UNUSED_RETIRE_THRESHOLD,
    UNUSED_RETIRE_MIN_AGE_DAYS,
)


_NOW = datetime.now(timezone.utc)
_RECENT = _NOW.isoformat()
_OLD = (_NOW - timedelta(days=UNUSED_RETIRE_MIN_AGE_DAYS + 10)).isoformat()


def _insert_exec(conn, workflow_id, *, done=0, status="passed"):
    """Insert an execution_records row the attribution claim can target."""
    conn.execute(
        "INSERT INTO execution_records "
        "(workflow_id, timestamp, user_query, test_status, hint_attribution_done) "
        "VALUES (?, '2026-01-01T00:00:00+00:00', 'q', ?, ?)",
        (workflow_id, status, done),
    )


def _insert_hint(conn, text, *, applied=0, success=0, failure=0, unused=0,
                 is_active=1, conflict_flagged=0, scope="global",
                 created_at=_RECENT):
    cur = conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, applied_count, success_count, "
        " failure_count, unused_count, is_active, conflict_flagged, "
        " created_at, last_seen) "
        "VALUES (?, 'C1', ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (text, scope, applied, success, failure, unused, is_active,
         conflict_flagged, created_at, created_at),
    )
    return cur.fetchone()["id"]


def _hint_row(conn, hint_id):
    return conn.execute(
        "SELECT applied_count, success_count, failure_count, unused_count, "
        "       is_active, conflict_flagged "
        "FROM nl_feedback_corrections WHERE id = ?",
        (hint_id,),
    ).fetchone()


def _done(conn, workflow_id):
    return conn.execute(
        "SELECT hint_attribution_done FROM execution_records WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()["hint_attribution_done"]


# ===================================================================
# Bucket increments
# ===================================================================

class TestBucketIncrements:

    def test_used_increments_success_and_applied(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "Used hint")
        _insert_exec(in_memory_db, "wf-1")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        result = engine.apply_hint_attribution("wf-1", [hid], [], [])

        assert result == []  # claim won; a used hint is not flagged
        row = _hint_row(in_memory_db, hid)
        assert (row["applied_count"], row["success_count"],
                row["failure_count"], row["unused_count"]) == (1, 1, 0, 0)
        assert _done(in_memory_db, "wf-1") == 1

    def test_unused_increments_unused_and_applied(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "Unused hint")
        _insert_exec(in_memory_db, "wf-2")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-2", [], [], [hid])

        row = _hint_row(in_memory_db, hid)
        assert (row["applied_count"], row["success_count"],
                row["failure_count"], row["unused_count"]) == (1, 0, 0, 1)

    def test_failure_increments_failure_and_flags(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "Harmful hint")
        _insert_exec(in_memory_db, "wf-3")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        result = engine.apply_hint_attribution(
            "wf-3", [], [hid], [], reasons={hid: "removed and caused the failure"},
        )

        assert result == [hid]  # the enforced flag set is returned
        row = _hint_row(in_memory_db, hid)
        assert (row["applied_count"], row["success_count"],
                row["failure_count"], row["unused_count"]) == (1, 0, 1, 0)
        assert row["conflict_flagged"] == 1  # harmful set is flagged in the same txn

    def test_mixed_buckets_in_one_call(self, in_memory_db):
        used = _insert_hint(in_memory_db, "used")
        unused = _insert_hint(in_memory_db, "unused")
        _insert_exec(in_memory_db, "wf-mix")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-mix", [used], [], [unused])

        assert _hint_row(in_memory_db, used)["success_count"] == 1
        assert _hint_row(in_memory_db, unused)["unused_count"] == 1


# ===================================================================
# Once-guard (atomic claim)
# ===================================================================

class TestOnceGuard:

    def test_second_apply_for_same_workflow_credits_nothing(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "hint")
        _insert_exec(in_memory_db, "wf-once")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        assert engine.apply_hint_attribution("wf-once", [hid], [], []) is not None
        # Re-run of the now-attributed workflow → claim finds 0 rows → no credit.
        assert engine.apply_hint_attribution("wf-once", [hid], [], []) is None

        row = _hint_row(in_memory_db, hid)
        assert row["success_count"] == 1  # credited exactly once

    def test_no_execution_record_credits_nothing(self, in_memory_db):
        """Dedup run: no execution_records row → claim finds 0 rows → skip."""
        hid = _insert_hint(in_memory_db, "hint")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        assert engine.apply_hint_attribution("wf-missing", [hid], [], []) is None
        assert _hint_row(in_memory_db, hid)["applied_count"] == 0

    def test_already_done_record_credits_nothing(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "hint")
        _insert_exec(in_memory_db, "wf-done", done=1)
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        assert engine.apply_hint_attribution("wf-done", [hid], [], []) is None
        assert _hint_row(in_memory_db, hid)["applied_count"] == 0

    def test_claim_won_but_no_buckets_sets_done_and_returns_true(self, in_memory_db):
        _insert_exec(in_memory_db, "wf-empty")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        assert engine.apply_hint_attribution("wf-empty", [], [], []) == []
        assert _done(in_memory_db, "wf-empty") == 1  # flag set (no-op), don't retry


# ===================================================================
# G4 — disjointness guard
# ===================================================================

class TestDisjointnessGuard:

    def test_id_in_multiple_buckets_dropped_from_all(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "ambiguous")
        _insert_exec(in_memory_db, "wf-g4")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        # Same id claimed as both used and failure → contradiction → no-signal.
        engine.apply_hint_attribution("wf-g4", [hid], [hid], [])

        row = _hint_row(in_memory_db, hid)
        assert (row["applied_count"], row["success_count"],
                row["failure_count"]) == (0, 0, 0)
        assert row["conflict_flagged"] == 0


# ===================================================================
# Membership: only active, unflagged hints are touched
# ===================================================================

class TestTouchedSet:

    def test_conflict_flagged_hint_not_touched(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "flagged", applied=5, success=2,
                           failure=3, conflict_flagged=1)
        _insert_exec(in_memory_db, "wf-cf")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-cf", [hid], [], [])

        row = _hint_row(in_memory_db, hid)
        assert row["applied_count"] == 5 and row["success_count"] == 2

    def test_inactive_hint_not_touched(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "inactive", applied=4, is_active=0)
        _insert_exec(in_memory_db, "wf-ia")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-ia", [hid], [], [])

        assert _hint_row(in_memory_db, hid)["applied_count"] == 4


# ===================================================================
# Auto-disable / retire (FR2 never-succeeded; G1 unused-retirement)
# ===================================================================

class TestAutoDisableAndRetire:

    def test_never_succeeded_auto_disables(self, in_memory_db):
        # Pre: failure == min-1, success 0. One more failure crosses the floor.
        hid = _insert_hint(in_memory_db, "bad",
                           failure=AUTO_DISABLE_MIN_APPLICATIONS - 1)
        _insert_exec(in_memory_db, "wf-ns")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-ns", [], [hid], [])

        row = _hint_row(in_memory_db, hid)
        assert row["failure_count"] == AUTO_DISABLE_MIN_APPLICATIONS
        assert row["is_active"] == 0
        audit = in_memory_db.execute(
            "SELECT reason FROM hint_audit WHERE hint_id = ? AND action = 'auto_disable'",
            (hid,),
        ).fetchone()
        assert audit["reason"] == "never_succeeded"

    def test_success_protects_from_never_succeeded(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "mixed", success=1,
                           failure=AUTO_DISABLE_MIN_APPLICATIONS + 5)
        _insert_exec(in_memory_db, "wf-prot")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-prot", [], [hid], [])

        assert _hint_row(in_memory_db, hid)["is_active"] == 1

    def test_unused_retirement_past_age_floor(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "dead weight",
                           unused=UNUSED_RETIRE_THRESHOLD - 1, created_at=_OLD)
        _insert_exec(in_memory_db, "wf-ur")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-ur", [], [], [hid])

        row = _hint_row(in_memory_db, hid)
        assert row["unused_count"] == UNUSED_RETIRE_THRESHOLD
        assert row["is_active"] == 0
        audit = in_memory_db.execute(
            "SELECT reason FROM hint_audit WHERE hint_id = ? AND action = 'auto_disable'",
            (hid,),
        ).fetchone()
        assert audit["reason"] == "never_used"

    def test_unused_retirement_blocked_by_age_floor(self, in_memory_db):
        """A brand-new hint at the unused threshold is NOT retired (G1 floor)."""
        hid = _insert_hint(in_memory_db, "fresh",
                           unused=UNUSED_RETIRE_THRESHOLD - 1, created_at=_RECENT)
        _insert_exec(in_memory_db, "wf-fresh")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-fresh", [], [], [hid])

        row = _hint_row(in_memory_db, hid)
        assert row["unused_count"] == UNUSED_RETIRE_THRESHOLD
        assert row["is_active"] == 1  # too new to retire

    def test_unused_retirement_blocked_by_prior_success(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "useful sometimes", success=1,
                           unused=UNUSED_RETIRE_THRESHOLD + 3, created_at=_OLD)
        _insert_exec(in_memory_db, "wf-us")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution("wf-us", [], [], [hid])

        assert _hint_row(in_memory_db, hid)["is_active"] == 1


# ===================================================================
# Strong-history flag suppression (S1: used-outcome basis)
# ===================================================================

class TestStrongHistoryGuard:

    def test_strong_history_suppresses_flag_but_still_increments_failure(self, in_memory_db):
        # used = success + failure = 8 + 0 = 8 (>=5); rate 1.0 (>=0.7) → protected.
        hid = _insert_hint(in_memory_db, "proven good", success=8, failure=0)
        _insert_exec(in_memory_db, "wf-sh")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution(
            "wf-sh", [], [hid], [], reasons={hid: "removed"},
        )

        row = _hint_row(in_memory_db, hid)
        assert row["conflict_flagged"] == 0       # flag suppressed (strong history)
        assert row["failure_count"] == 1          # but failure++ still applied (N2)

    def test_unused_does_not_grant_protection(self, in_memory_db):
        # applied is high via unused, but used outcomes (success+failure)=0 → not
        # protected → the harmful flag fires.
        hid = _insert_hint(in_memory_db, "all unused", success=0, failure=0,
                           unused=20)
        _insert_exec(in_memory_db, "wf-uo")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution(
            "wf-uo", [], [hid], [], reasons={hid: "removed"},
        )

        assert _hint_row(in_memory_db, hid)["conflict_flagged"] == 1


# ===================================================================
# Guards / atomicity
# ===================================================================

class TestGuardsAndAtomicity:

    def test_no_em_returns_none(self):
        engine = NLFeedbackEngine(None)
        assert engine.apply_hint_attribution("wf", [1], [], []) is None

    def test_rollback_on_mid_transaction_error_is_retryable(self, in_memory_db):
        from unittest.mock import patch

        hid = _insert_hint(in_memory_db, "hint")
        _insert_exec(in_memory_db, "wf-rb")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        with patch.object(
            engine, "_maybe_auto_disable_or_retire",
            side_effect=RuntimeError("simulated mid-transaction failure"),
        ):
            result = engine.apply_hint_attribution("wf-rb", [hid], [], [])

        assert result is None
        # Everything rolled back — counters unchanged AND the claim reverted, so
        # a later legitimate attribution can still run (hint_attribution_done=0).
        assert _hint_row(in_memory_db, hid)["applied_count"] == 0
        assert _done(in_memory_db, "wf-rb") == 0


# ===================================================================
# Shared no-commit flag core (_flag_hints_no_commit)
# ===================================================================

class TestFlagCore:

    def test_flags_low_history_hint_without_committing(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "weak", success=1, failure=4)
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        now = datetime.now(timezone.utc).isoformat()
        flagged = engine._flag_hints_no_commit({hid: "bad"}, "trigger_2", now)

        assert flagged == [hid]
        # Visible on the writer connection but NOT yet committed (caller commits).
        assert _hint_row(in_memory_db, hid)["conflict_flagged"] == 1

    def test_strong_history_hint_is_suppressed(self, in_memory_db):
        # used = 6 (>=5), success rate 5/6 ≈ 0.83 (>=0.7) → suppressed.
        hid = _insert_hint(in_memory_db, "strong", success=5, failure=1)
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        now = datetime.now(timezone.utc).isoformat()
        flagged = engine._flag_hints_no_commit({hid: "bad"}, "trigger_2", now)

        assert flagged == []
        assert _hint_row(in_memory_db, hid)["conflict_flagged"] == 0


# ===================================================================
# F2e — trace attribution UPDATE (separate commit AFTER the counters)
# ===================================================================


def _insert_trace_row(conn, workflow_id, hint_id):
    """A pre-existing N3 trace row (as F2d would have INSERTed at generation)."""
    conn.execute(
        "INSERT INTO hint_workflow_trace "
        "(workflow_id, hint_id, source, available, injected, created_at) "
        "VALUES (?, ?, 'nl', 1, 1, '2026-01-01T00:00:00+00:00')",
        (workflow_id, hint_id),
    )


class TestTraceAttributionUpdate:
    """F2e — apply_hint_attribution stamps attribution_bucket/reason onto the
    N3 trace as a SEPARATE commit AFTER the counter transaction (FR4 guard #3:
    a trace-UPDATE failure can never undo the committed counters)."""

    def test_used_and_unused_buckets_stamped(self, in_memory_db):
        h_used = _insert_hint(in_memory_db, "used hint")
        h_unused = _insert_hint(in_memory_db, "unused hint")
        _insert_exec(in_memory_db, "wf-tr1")
        _insert_trace_row(in_memory_db, "wf-tr1", h_used)
        _insert_trace_row(in_memory_db, "wf-tr1", h_unused)
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution(
            "wf-tr1", [h_used], [], [h_unused],
            reasons={h_used: "used in click", h_unused: "advice absent"},
        )

        rows = {
            r["hint_id"]: r for r in in_memory_db.execute(
                "SELECT hint_id, attribution_bucket, attribution_reason "
                "FROM hint_workflow_trace WHERE workflow_id = 'wf-tr1'"
            ).fetchall()
        }
        assert rows[h_used]["attribution_bucket"] == "used"
        assert rows[h_used]["attribution_reason"] == "used in click"
        assert rows[h_unused]["attribution_bucket"] == "unused"
        assert rows[h_unused]["attribution_reason"] == "advice absent"

    def test_failure_bucket_maps_to_harmful(self, in_memory_db):
        hid = _insert_hint(in_memory_db, "harmful hint")
        _insert_exec(in_memory_db, "wf-tr2")
        _insert_trace_row(in_memory_db, "wf-tr2", hid)
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        engine.apply_hint_attribution(
            "wf-tr2", [], [hid], [], reasons={hid: "removed, fixed it"})

        row = in_memory_db.execute(
            "SELECT attribution_bucket, attribution_reason "
            "FROM hint_workflow_trace WHERE workflow_id = 'wf-tr2' AND hint_id = ?",
            (hid,),
        ).fetchone()
        assert row["attribution_bucket"] == "harmful"   # internal 'failure'
        assert row["attribution_reason"] == "removed, fixed it"

    def test_missing_trace_row_is_noop(self, in_memory_db):
        # No trace row for this workflow (capture was off) → the UPDATE matches
        # nothing; counters still apply normally.
        hid = _insert_hint(in_memory_db, "used hint")
        _insert_exec(in_memory_db, "wf-tr-none")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        result = engine.apply_hint_attribution("wf-tr-none", [hid], [], [])

        assert result == []
        assert _hint_row(in_memory_db, hid)["success_count"] == 1

    def test_trace_update_failure_preserves_counters(self, in_memory_db):
        # FR4 guard #3: the trace UPDATE is a SEPARATE commit AFTER the counter
        # transaction. Drop the table so the UPDATE raises — the helper swallows
        # it, the committed counters survive, AND the won-claim return is intact
        # (proving the helper is bulletproof, not propagating into the except).
        hid = _insert_hint(in_memory_db, "used hint")
        _insert_exec(in_memory_db, "wf-tr3")
        in_memory_db.commit()
        in_memory_db.execute("DROP TABLE hint_workflow_trace")
        in_memory_db.commit()

        engine = NLFeedbackEngine(in_memory_db)
        result = engine.apply_hint_attribution("wf-tr3", [hid], [], [])

        row = _hint_row(in_memory_db, hid)
        assert (row["applied_count"], row["success_count"]) == (1, 1)  # committed
        assert result == []                                            # return intact
        assert _done(in_memory_db, "wf-tr3") == 1


# ===================================================================
# Motivating-bug reproduction (Phase 7) — the defect Part 2 exists to fix
# ===================================================================

class TestMotivatingBugReproduction:
    """The original defect: a PASSING run credited success_count to EVERY
    injected hint. The old update_hint_effectiveness had no notion of which
    hints the model actually USED, so it inflated success across the board and
    could re-credit on every re-run — poisoning the quality signal that every
    downstream disable/flag rule reads.

    Reproduce the canonical scenario over real SQLite (never mocked): 5 hints
    are injected into one workflow that PASSES; the attribution verdict says the
    model used 2 and ignored the other 3. The fix must credit
      - the 2 USED hints   -> success += 1   (applied 1, unused 0)
      - the 3 IGNORED hints -> unused += 1    (applied 1, success 0)  [NOT success]
    and credit the workflow EXACTLY ONCE — a re-run of the same passing test
    leaves every counter unchanged.
    """

    def test_only_used_hints_get_success_rest_unused_credited_once(self, in_memory_db):
        ids = [_insert_hint(in_memory_db, f"hint {i}") for i in range(5)]
        _insert_exec(in_memory_db, "wf-motivating", status="passed")
        in_memory_db.commit()

        used, ignored = ids[:2], ids[2:]
        engine = NLFeedbackEngine(in_memory_db)
        result = engine.apply_hint_attribution("wf-motivating", used, [], ignored)

        assert result == []  # a passing run flags nothing

        for hid in used:
            row = _hint_row(in_memory_db, hid)
            assert (row["applied_count"], row["success_count"],
                    row["failure_count"], row["unused_count"]) == (1, 1, 0, 0)
        for hid in ignored:
            row = _hint_row(in_memory_db, hid)
            assert (row["applied_count"], row["success_count"],
                    row["failure_count"], row["unused_count"]) == (1, 0, 0, 1), \
                "the 3 ignored hints must read unused, NOT success — that was the bug"

        # Exactly once: re-running the now-attributed passing test credits nothing.
        assert engine.apply_hint_attribution(
            "wf-motivating", used, [], ignored) is None
        for hid in ids:
            row = _hint_row(in_memory_db, hid)
            assert (row["success_count"] + row["failure_count"]
                    + row["unused_count"]) == 1  # untouched by the second pass

    def test_mixed_verdict_each_bucket_credited_once(self, in_memory_db):
        # Case-B shape (fail -> edit removing a hint -> pass): of 5 injected, the
        # verdict is 2 used, 1 harmful (removed, which fixed the failure), 2
        # ignored. Each bucket is credited on its own basis, exactly once.
        ids = [_insert_hint(in_memory_db, f"hint {i}") for i in range(5)]
        _insert_exec(in_memory_db, "wf-motivating-b", status="passed")
        in_memory_db.commit()

        used, harmful, ignored = ids[:2], [ids[2]], ids[3:]
        engine = NLFeedbackEngine(in_memory_db)
        result = engine.apply_hint_attribution(
            "wf-motivating-b", used, harmful, ignored,
            reasons={ids[2]: "removed and the test then passed"})

        assert result == harmful  # the harmful hint is flagged in the same txn
        for hid in used:
            assert _hint_row(in_memory_db, hid)["success_count"] == 1
        hrow = _hint_row(in_memory_db, harmful[0])
        assert (hrow["failure_count"], hrow["conflict_flagged"]) == (1, 1)
        for hid in ignored:
            assert _hint_row(in_memory_db, hid)["unused_count"] == 1

        # Exactly once: a re-run credits nothing and leaves every counter intact.
        assert engine.apply_hint_attribution(
            "wf-motivating-b", used, harmful, ignored) is None
        assert _hint_row(in_memory_db, harmful[0])["failure_count"] == 1
        for hid in used:
            assert _hint_row(in_memory_db, hid)["success_count"] == 1
