"""
Coverage tests for nl_feedback_engine.py — targeting 26 missing lines.

Tests:
- conflict_flag_hints: hint not found in DB → skip with warning
- conflict_flag_hints: exception mid-loop → rollback → return []
- get_hints_by_id: None input → warning + []
- learn_from_feedback: positive category → skips storage entirely
- update_hint_effectiveness: malformed injected_hint_ids JSON → fallback to scope-wide
- get_hints_with_ids: no execution_memory → ([], [])
- get_active_hints_raw: no execution_memory → []
"""

import json
import threading
import pytest

from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(in_memory_em):
    """NLFeedbackEngine backed by an in-memory (temp-file) SQLite DB."""
    return NLFeedbackEngine(execution_memory=in_memory_em)


@pytest.fixture
def no_em_engine():
    """NLFeedbackEngine with no execution_memory — guards disabled path."""
    return NLFeedbackEngine(execution_memory=None)


# ---------------------------------------------------------------------------
# get_hints_by_id — None input
# ---------------------------------------------------------------------------

class TestGetHintsByIdNoneInput:
    def test_none_input_returns_empty_list(self, engine):
        result = engine.get_hints_by_id(None)
        assert result == []

    def test_empty_list_returns_empty_list(self, engine):
        result = engine.get_hints_by_id([])
        assert result == []


# ---------------------------------------------------------------------------
# get_hints_with_ids — no execution_memory
# ---------------------------------------------------------------------------

class TestGetHintsWithIdsNoEm:
    def test_no_em_returns_empty_tuple(self, no_em_engine):
        hints, ids = no_em_engine.get_hints_with_ids("test query", "https://x.com", "assembler")
        assert hints == []
        assert ids == []


# ---------------------------------------------------------------------------
# get_active_hints_raw — no execution_memory
# ---------------------------------------------------------------------------

class TestGetActiveHintsRawNoEm:
    def test_no_em_returns_empty_list(self, no_em_engine):
        result = no_em_engine.get_active_hints_raw("example.com", "https://example.com")
        assert result == []


# ---------------------------------------------------------------------------
# learn_from_feedback — positive category skips storage
# ---------------------------------------------------------------------------

class TestLearnFromFeedbackPositiveCategory:
    def test_positive_category_does_not_insert_row(self, engine, in_memory_em):
        """Feedback triaged as 'positive' must not be stored."""
        from dataclasses import dataclass

        @dataclass
        class FakeRecord:
            workflow_id: str = "wf-pos"
            url: str = "https://example.com"
            domain: str = "example.com"
            failure_category: str = None
            user_query: str = "test query"

        insight = {
            "category": "positive",
            "feedback_text": "Looks good!",
        }

        engine.learn_from_feedback(FakeRecord(), insight)

        with in_memory_em.read_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM nl_feedback_corrections"
            ).fetchone()[0]

        assert count == 0  # nothing stored


# ---------------------------------------------------------------------------
# conflict_flag_hints — hint not found
# ---------------------------------------------------------------------------

class TestConflictFlagHintsHintNotFound:
    def test_hint_not_in_db_is_skipped_gracefully(self, engine, in_memory_em):
        """A hint_id that has no matching row is silently skipped."""
        per_hint_reasons = {9999: "this hint does not exist in the DB"}

        result = engine.conflict_flag_hints(per_hint_reasons, trigger_type="trigger_1")

        # 9999 was not in the DB, so nothing was actually flagged
        assert result == []
        # DB is unmodified (no row for 9999)
        with in_memory_em.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nl_feedback_corrections WHERE id = 9999"
            ).fetchall()
        assert rows == []


# ---------------------------------------------------------------------------
# conflict_flag_hints — rollback on exception
# ---------------------------------------------------------------------------

class TestConflictFlagHintsRollbackOnException:
    def test_db_error_causes_rollback_and_returns_empty_list(self, engine, in_memory_em):
        """If a DB write fails mid-loop, the transaction is rolled back and
        the method returns [] so callers always get a valid list."""
        import sqlite3

        # Insert a real hint so the SELECT inside conflict_flag_hints succeeds
        in_memory_em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, evidence_count, "
            " anchor_query, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, datetime('now'), datetime('now'))",
            ("use stable", "timing", "global", None, None, "test query"),
        )
        in_memory_em._writer_conn.commit()

        row = in_memory_em._writer_conn.execute(
            "SELECT id FROM nl_feedback_corrections LIMIT 1"
        ).fetchone()
        hint_id = row["id"]

        # Wrap the writer connection in a proxy that intercepts UPDATE calls.
        # We cannot monkey-patch sqlite3.Connection.execute (it's a C-level slot),
        # so we swap out _writer_conn with a proxy object that delegates everything
        # except the failing UPDATE.
        real_conn = in_memory_em._writer_conn
        call_count = [0]

        class _FailingProxy:
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def execute(self, sql, params=()):
                call_count[0] += 1
                if call_count[0] > 1 and "UPDATE" in sql.upper():
                    raise sqlite3.OperationalError("simulated write failure")
                return real_conn.execute(sql, params)

            def commit(self):
                return real_conn.commit()

            def rollback(self):
                return real_conn.rollback()

        in_memory_em._writer_conn = _FailingProxy()
        try:
            result = engine.conflict_flag_hints({hint_id: "some reason"}, trigger_type="trigger_1")
        finally:
            in_memory_em._writer_conn = real_conn

        assert result == []  # rollback → nothing flagged


# ---------------------------------------------------------------------------
# update_hint_effectiveness — malformed injected_hint_ids
# ---------------------------------------------------------------------------

class TestUpdateHintEffectivenessMalformedJson:
    def test_malformed_injected_hint_ids_falls_back_to_scope_wide(
        self, engine, in_memory_em
    ):
        """A malformed JSON string in injected_hint_ids falls back to the
        scope-wide legacy path instead of raising an exception."""
        # Insert an active hint in scope
        in_memory_em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, evidence_count, "
            " anchor_query, created_at, last_seen) "
            "VALUES ('use xpath', 'locator', 'global', NULL, NULL, 3, "
            " 'test', datetime('now'), datetime('now'))",
        )
        in_memory_em._writer_conn.commit()

        # Should not raise — malformed JSON is handled with a warning + fallback
        engine.update_hint_effectiveness(
            domain=None,
            url=None,
            test_passed=True,
            new_failure_category=None,
            injected_hint_ids="{bad json",  # malformed → falls back to scope-wide
        )

        # Verify the fallback ran (applied_count was incremented on the global hint)
        row = in_memory_em._writer_conn.execute(
            "SELECT applied_count FROM nl_feedback_corrections LIMIT 1"
        ).fetchone()
        assert row["applied_count"] == 1


# ---------------------------------------------------------------------------
# update_hint_effectiveness — no execution_memory
# ---------------------------------------------------------------------------

class TestUpdateHintEffectivenessNoEm:
    def test_no_em_returns_early_without_error(self, no_em_engine):
        """When execution_memory is None, the method should return silently."""
        # Should not raise
        no_em_engine.update_hint_effectiveness(
            domain="example.com",
            url="https://example.com",
            test_passed=True,
        )


# ---------------------------------------------------------------------------
# conflict_flag_hints — strong-history guard (applied < threshold)
# ---------------------------------------------------------------------------

class TestConflictFlagHintsStrongHistoryGuard:
    def test_hint_below_threshold_is_flagged_immediately(self, engine, in_memory_em):
        """A hint with applied_count < TRIGGER_FLAG_PROTECTION_MIN_APPLIED
        (5) is flagged without the strong-history guard triggering."""
        in_memory_em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, evidence_count, "
            " applied_count, success_count, anchor_query, created_at, last_seen) "
            "VALUES ('click button', 'locator', 'global', NULL, NULL, 1, "
            " 2, 2, 'test', datetime('now'), datetime('now'))",
        )
        in_memory_em._writer_conn.commit()

        row = in_memory_em._writer_conn.execute(
            "SELECT id FROM nl_feedback_corrections LIMIT 1"
        ).fetchone()
        hint_id = row["id"]

        result = engine.conflict_flag_hints(
            {hint_id: "bad advice"}, trigger_type="trigger_1"
        )

        assert hint_id in result

    def test_hint_above_threshold_with_high_success_rate_is_not_flagged(
        self, engine, in_memory_em
    ):
        """Strong-history guard: applied>=5 AND success_rate>=0.70 → skip flag."""
        in_memory_em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, evidence_count, "
            " applied_count, success_count, anchor_query, created_at, last_seen) "
            "VALUES ('use stable state', 'timing', 'global', NULL, NULL, 1, "
            " 10, 9, 'stable', datetime('now'), datetime('now'))",
            # 10 applied, 9 successes = 90% success rate → protected
        )
        in_memory_em._writer_conn.commit()

        row = in_memory_em._writer_conn.execute(
            "SELECT id FROM nl_feedback_corrections LIMIT 1"
        ).fetchone()
        hint_id = row["id"]

        result = engine.conflict_flag_hints(
            {hint_id: "supposedly bad"}, trigger_type="trigger_1"
        )

        assert result == []  # protected by strong-history guard
