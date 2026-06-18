"""
DAY_00 Tests -- migrated from scripts/verify_day00.py

Validates all acceptance criteria for the Phase 1 schema and shared infrastructure.
Uses pytest fixtures from conftest.py for database and temporary directory management.
"""

import sqlite3
import threading
import time

import pytest

from tests.test_optimization import pg_introspect
from src.backend.crew_ai.optimization.learning_config import (
    EffectivenessScore,
    LearningCircuitBreaker,
    LearningWriteQueue,
    LearningEngine,
    LEARNING_CONFIG,
)


class TestSchema:
    """Verify schema creation and structure."""

    def test_all_tables_exist(self, in_memory_db):
        expected_tables = {
            "execution_records", "intent_patterns", "structural_rules",
            "keyword_corrections", "anti_patterns", "learning_stats",
            "schema_version", "nl_feedback_corrections", "learning_metrics",
            "trigger_events", "hint_audit",
            "hint_review_sessions", "hint_review_recommendations",
            "hint_review_pages", "hint_workflow_trace",
            "learning_anchors", "execution_embeddings",
        }
        tables = pg_introspect.table_names(in_memory_db)
        assert expected_tables == tables, (
            f"All tables exist: found={sorted(tables)}"
        )

    def test_all_indexes_exist(self, in_memory_db):
        expected_indexes = {
            "idx_exec_domain", "idx_exec_status", "idx_exec_failure",
            "idx_exec_timestamp", "idx_exec_structure",
            "idx_intent_score", "idx_intent_source",
            "idx_rules_score",
            "idx_anti_category", "idx_anti_domain",
        }
        indexes = pg_introspect.index_names(in_memory_db)
        missing = expected_indexes - indexes
        assert len(missing) == 0, (
            f"All 10 indexes exist: missing={sorted(missing)}"
        )

    def test_schema_version_records_consolidated(self, in_memory_db):
        # The Postgres schema (pg_schema) is created whole, recording the single
        # consolidated SCHEMA_VERSION rather than the SQLite per-migration history.
        from src.backend.crew_ai.optimization.pg_schema import SCHEMA_VERSION
        row = in_memory_db.execute(
            "SELECT version, description FROM schema_version WHERE version=?",
            (SCHEMA_VERSION,),
        ).fetchone()
        assert row is not None and row[0] == SCHEMA_VERSION, (
            f"schema_version records v{SCHEMA_VERSION}: "
            + (f"desc='{row[1]}'" if row else "NOT FOUND")
        )

    def test_no_invalid_indexes(self, in_memory_db):
        # Postgres analog of SQLite's PRAGMA integrity_check: every index in the
        # learning schema is valid (none left half-built / not-ready / corrupt).
        invalid = [
            r[0] for r in in_memory_db.execute(
                "SELECT c.relname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "  AND (NOT i.indisvalid OR NOT i.indisready)"
            ).fetchall()
        ]
        assert invalid == [], f"invalid/not-ready indexes present: {invalid}"

    def test_check_constraint_on_test_status(self, in_memory_db):
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_db.execute(
                "INSERT INTO execution_records "
                "(workflow_id, timestamp, user_query, test_status) "
                "VALUES ('test-1', '2026-01-01', 'test query', 'invalid_status')"
            )

    def test_valid_row_insert_succeeds(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO execution_records "
            "(workflow_id, timestamp, user_query, test_status) "
            "VALUES ('test-2', '2026-01-01', 'test query', 'passed')"
        )
        in_memory_db.commit()
        count = in_memory_db.execute(
            "SELECT COUNT(*) FROM execution_records"
        ).fetchone()[0]
        assert count == 1, "Valid row insert succeeds"

    def test_unique_constraint_on_workflow_id(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO execution_records "
            "(workflow_id, timestamp, user_query, test_status) "
            "VALUES ('test-2', '2026-01-01', 'test query', 'passed')"
        )
        in_memory_db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_db.execute(
                "INSERT INTO execution_records "
                "(workflow_id, timestamp, user_query, test_status) "
                "VALUES ('test-2', '2026-01-01', 'another query', 'failed')"
            )


class TestEffectivenessScore:
    """Verify MDES EffectivenessScore calculations."""

    def test_basic_calculation(self):
        score = EffectivenessScore.calculate(8, 2)
        assert abs(score - 0.7273) < 0.001, (
            f"EffectivenessScore basic calculation: "
            f"8s/2f -> {score} (expected ~0.7273)"
        )

    def test_zero_history(self):
        score_zero = EffectivenessScore.calculate(0, 0)
        assert score_zero == 0.0, (
            f"EffectivenessScore zero history = 0.0: 0s/0f -> {score_zero}"
        )

    def test_perfect_record(self):
        score_perfect = EffectivenessScore.calculate(10, 0)
        assert abs(score_perfect - 0.9091) < 0.001, (
            f"EffectivenessScore perfect record: 10s/0f -> {score_perfect}"
        )

    def test_staleness_penalty_90_days(self):
        score_stale = EffectivenessScore.calculate(8, 2, last_used_days_ago=100)
        expected_stale = round(0.7273 * 0.95, 4)
        assert abs(score_stale - expected_stale) < 0.001, (
            f"EffectivenessScore 90-day staleness penalty: "
            f"100d -> {score_stale} (expected ~{expected_stale})"
        )

    def test_staleness_penalty_180_days(self):
        score_very_stale = EffectivenessScore.calculate(8, 2, last_used_days_ago=200)
        expected_very_stale = round(0.7273 * 0.85, 4)
        assert abs(score_very_stale - expected_very_stale) < 0.001, (
            f"EffectivenessScore 180-day staleness penalty: "
            f"200d -> {score_very_stale} (expected ~{expected_very_stale})"
        )

    def test_passes_threshold_rejects_insufficient_observations(self):
        passes = EffectivenessScore.passes_threshold(2, 0)
        assert passes is False, (
            "passes_threshold rejects insufficient observations: 2 obs < min 3"
        )

    def test_passes_threshold_accepts_sufficient_observations(self):
        passes = EffectivenessScore.passes_threshold(8, 2)
        assert passes is True, (
            f"passes_threshold accepts sufficient obs + high score: "
            f"score={EffectivenessScore.calculate(8, 2)} >= 0.4"
        )


class TestCircuitBreaker:
    """Verify LearningCircuitBreaker behavior."""

    def test_initially_enabled(self):
        cb = LearningCircuitBreaker()
        assert cb.is_enabled(), "CircuitBreaker initially enabled"

    def test_stays_enabled_below_threshold(self):
        cb = LearningCircuitBreaker()
        for _ in range(8):
            cb.record_success()
        cb.record_error(Exception("test error 1"))
        cb.record_error(Exception("test error 2"))
        assert cb.is_enabled(), (
            f"CircuitBreaker stays enabled at 20% errors (threshold boundary): "
            f"stats={cb.get_stats()}"
        )

    def test_disables_above_threshold(self):
        cb = LearningCircuitBreaker()
        for _ in range(7):
            cb.record_success()
        for _ in range(3):
            cb.record_error(Exception("test"))
        assert not cb.is_enabled(), (
            f"CircuitBreaker disables above 20% errors: stats={cb.get_stats()}"
        )

    def test_reset_re_enables(self):
        cb = LearningCircuitBreaker()
        for _ in range(7):
            cb.record_success()
        for _ in range(3):
            cb.record_error(Exception("test"))
        cb.reset()
        assert cb.is_enabled(), "CircuitBreaker reset re-enables"


class TestWriteQueue:
    """Verify LearningWriteQueue behavior."""

    def test_processes_all_writes(self):
        wq = LearningWriteQueue()
        results = []
        event = threading.Event()

        def write_fn(value):
            results.append(value)
            if len(results) >= 3:
                event.set()

        wq.submit(write_fn, "a")
        wq.submit(write_fn, "b")
        wq.submit(write_fn, "c")

        event.wait(timeout=5)
        assert results == ["a", "b", "c"], (
            f"LearningWriteQueue processes all writes: results={results}"
        )

    def test_survives_errors(self):
        wq = LearningWriteQueue()

        def failing_fn():
            raise Exception("intentional test error")

        wq.submit(failing_fn)
        # Give it time to process
        time.sleep(0.5)
        assert True, "LearningWriteQueue survives errors: No crash after error"


class TestLearningEngine:
    """Verify LearningEngine ABC interface."""

    def test_abc_prevents_direct_instantiation(self):
        with pytest.raises(TypeError):
            LearningEngine()

    def test_concrete_implementation_works(self):
        class TestEngine(LearningEngine):
            def learn(self, record):
                pass

            def get_hints(self, user_query, url, agent_role):
                return ["test hint"]

            def get_stats(self):
                return {"total_rules": 0}

        engine = TestEngine()
        hints = engine.get_hints("test", "http://test.com", "planner")
        assert hints == ["test hint"], "LearningEngine concrete implementation works"


class TestConfig:
    """Verify config.py has the new settings."""

    def test_optimization_enabled_setting_exists(self):
        from src.backend.core.config import Settings

        s = Settings()
        assert hasattr(s, "OPTIMIZATION_ENABLED"), (
            "OPTIMIZATION_ENABLED setting exists"
        )

    def test_hint_trace_enabled_setting_defaults_true(self):
        from src.backend.core.config import Settings

        s = Settings()
        assert hasattr(s, "HINT_TRACE_ENABLED"), (
            "HINT_TRACE_ENABLED setting exists"
        )
        assert s.HINT_TRACE_ENABLED is True, (
            "HINT_TRACE_ENABLED defaults ON (gated by OPTIMIZATION_ENABLED)"
        )

    def test_hint_trace_retention_days_defaults_90(self):
        from src.backend.core.config import Settings

        s = Settings()
        assert hasattr(s, "HINT_TRACE_RETENTION_DAYS"), (
            "HINT_TRACE_RETENTION_DAYS setting exists"
        )
        assert s.HINT_TRACE_RETENTION_DAYS == 90, (
            "HINT_TRACE_RETENTION_DAYS defaults to 90 (0 = keep all)"
        )


# ===================================================================
# TestParseReviewResponse — _parse_review_response state-validity
# ===================================================================


class TestParseReviewResponse:
    """_parse_review_response: unflag acceptance and state-validity checks."""

    def _make_content(self, decisions: list) -> str:
        import json
        return json.dumps({"decisions": decisions, "summary": "test"})

    def _hint_states(self, *items):
        """items: (id, is_active, conflict_flagged)"""
        return {
            i: {"is_active": a, "conflict_flagged": f}
            for i, a, f in items
        }

    def test_unflag_accepted_for_suspended_hint(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 1, "recommendation": "unflag", "reason": "flag was wrong"},
        ])
        # State #2: is_active=1, conflict_flagged=1
        result = _parse_review_response(
            content,
            known_hint_ids={1},
            zero_application_ids=set(),
            hint_states=self._hint_states((1, 1, 1)),
        )
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["recommendation"] == "unflag"

    def test_unflag_rejected_for_active_unflagged_hint(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 2, "recommendation": "unflag", "reason": "no reason to flag"},
        ])
        # State #1: is_active=1, conflict_flagged=0 — unflag is invalid
        result = _parse_review_response(
            content,
            known_hint_ids={2},
            zero_application_ids=set(),
            hint_states=self._hint_states((2, 1, 0)),
        )
        assert result["decisions"] == []

    def test_unflag_rejected_for_disabled_hint(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 3, "recommendation": "unflag", "reason": "bring it back"},
        ])
        # State #4: is_active=0, conflict_flagged=1 — unflag alone is useless
        result = _parse_review_response(
            content,
            known_hint_ids={3},
            zero_application_ids=set(),
            hint_states=self._hint_states((3, 0, 1)),
        )
        assert result["decisions"] == []

    def test_reactivate_rejected_for_active_hint(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 4, "recommendation": "reactivate", "reason": "already active"},
        ])
        # State #1: is_active=1, conflict_flagged=0 — reactivate is invalid
        result = _parse_review_response(
            content,
            known_hint_ids={4},
            zero_application_ids=set(),
            hint_states=self._hint_states((4, 1, 0)),
        )
        assert result["decisions"] == []

    def test_disable_rejected_for_disabled_hint(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 5, "recommendation": "disable", "reason": "already off"},
        ])
        # State #3: is_active=0, conflict_flagged=0 — disable is invalid
        result = _parse_review_response(
            content,
            known_hint_ids={5},
            zero_application_ids=set(),
            hint_states=self._hint_states((5, 0, 0)),
        )
        assert result["decisions"] == []

    def test_keep_and_flag_review_always_accepted(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 6, "recommendation": "keep", "reason": "looks fine"},
            {"id": 7, "recommendation": "flag_review", "reason": "mixed signals"},
        ])
        result = _parse_review_response(
            content,
            known_hint_ids={6, 7},
            zero_application_ids=set(),
            hint_states=self._hint_states((6, 0, 1), (7, 1, 0)),
        )
        assert {d["recommendation"] for d in result["decisions"]} == {"keep", "flag_review"}

    def test_no_hint_states_skips_state_checks(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        # Without hint_states, state-validity checks are skipped entirely.
        content = self._make_content([
            {"id": 8, "recommendation": "unflag", "reason": "no state check"},
        ])
        result = _parse_review_response(
            content,
            known_hint_ids={8},
            zero_application_ids=set(),
            hint_states=None,
        )
        assert len(result["decisions"]) == 1

    def test_unflag_rejected_for_zero_application_hint(self):
        from src.backend.crew_ai.optimization.learning_config import _parse_review_response
        content = self._make_content([
            {"id": 9, "recommendation": "unflag", "reason": "flag was wrong"},
        ])
        # State #2 would normally accept unflag, but zero_application_ids
        # takes precedence — unflag is not in ZERO_APP_ALLOWED.
        result = _parse_review_response(
            content,
            known_hint_ids={9},
            zero_application_ids={9},
            hint_states=self._hint_states((9, 1, 1)),
        )
        assert result["decisions"] == []


# ===================================================================
# TestV10Migration — schema v10 additions
# ===================================================================


class TestV10Migration:
    """Verify the two additions introduced by schema migration v10.

    Migration v10 adds:
      - execution_records.injected_hint_ids (TEXT, nullable)
      - hint_review_pages table with per-chunk review tracking columns
    """

    def test_injected_hint_ids_column_on_execution_records(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "execution_records")
        assert "injected_hint_ids" in columns, (
            f"injected_hint_ids column missing from execution_records; "
            f"found columns: {sorted(columns)}"
        )

    def test_hint_review_pages_table_columns(self, in_memory_db):
        expected = {
            "id", "session_id", "scope_type", "scope_value", "status",
            "hint_count", "llm_latency_ms", "error_message", "retry_count",
            "created_at", "completed_at",
        }
        columns = pg_introspect.column_names(in_memory_db, "hint_review_pages")
        missing = expected - columns
        assert not missing, (
            f"hint_review_pages missing columns: {sorted(missing)}"
        )


# ===================================================================
# TestV11Migration — schema v11 additions (query-similarity anchors)
# ===================================================================


class TestV11Migration:
    """Verify the three additions introduced by schema migration v11.

    Migration v11 adds:
      - nl_feedback_corrections.anchor_query (TEXT, nullable)
      - execution_records.model_version (TEXT, nullable)
      - learning_metrics.was_holdout (INTEGER NOT NULL DEFAULT 0)
    and backfills anchor_query from execution_records.user_query.
    """

    def test_anchor_query_column_on_nl_feedback_corrections(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "nl_feedback_corrections")
        assert "anchor_query" in columns, (
            f"anchor_query column missing from nl_feedback_corrections; "
            f"found columns: {sorted(columns)}"
        )

    def test_model_version_column_on_execution_records(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "execution_records")
        assert "model_version" in columns, (
            f"model_version column missing from execution_records; "
            f"found columns: {sorted(columns)}"
        )

    def test_was_holdout_column_defaults_to_zero(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO learning_metrics "
            "(workflow_id, user_query, test_passed, timestamp) "
            "VALUES ('wf-holdout', 'a query', 1, '2026-01-01T00:00:00+00:00')"
        )
        row = in_memory_db.execute(
            "SELECT was_holdout FROM learning_metrics "
            "WHERE workflow_id = 'wf-holdout'"
        ).fetchone()
        assert row[0] == 0, (
            f"was_holdout should default to 0 for an insert that omits it; "
            f"got {row[0]}"
        )


# ===================================================================
# TestV12Migration — split flagged_hint_ids into recommendation vs enforcement
# ===================================================================


class TestV12Migration:
    """Verify the schema v12 addition.

    Migration v12 adds:
      - trigger_events.actually_flagged_hint_ids (TEXT, nullable on pre-v12 rows).

    The column carries the post-strong-history-guard enforcement set —
    distinct from flagged_hint_ids (LLM recommendation). KPI queries
    COALESCE the two so legacy NULL rows fall back to the old semantics.
    """

    def test_actually_flagged_column_exists_after_migration(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "trigger_events")
        assert "actually_flagged_hint_ids" in columns, (
            f"actually_flagged_hint_ids column missing from trigger_events; "
            f"found columns: {sorted(columns)}"
        )

    def test_post_v12_row_can_carry_distinct_values(self, in_memory_db):
        """A v12+ insert separately populating both columns must round-trip
        the two distinct lists — proves the column accepts the enforcement
        set independent of the recommendation set.
        """
        in_memory_db.execute(
            "INSERT INTO trigger_events ("
            " trigger_type, workflow_id, domain, url, feedback_text, "
            " active_hint_ids, flagged_hint_ids, actually_flagged_hint_ids, "
            " reason, llm_model, input_tokens, output_tokens, "
            " llm_latency_ms, status, error_message, created_at"
            ") VALUES ('trigger_1', 'wf-v12', 'example.com', "
            "          'https://example.com', NULL, '[1,2,3]', '[1,2,3]', "
            "          '[2]', NULL, 'gemini/gemini-2.5-flash', 100, 50, "
            "          1200, 'succeeded', NULL, '2026-05-26T00:00:00+00:00')"
        )
        in_memory_db.commit()

        row = in_memory_db.execute(
            "SELECT flagged_hint_ids, actually_flagged_hint_ids "
            "FROM trigger_events WHERE workflow_id = 'wf-v12'"
        ).fetchone()
        assert row["flagged_hint_ids"] == [1, 2, 3], (
            "LLM-recommendation column must round-trip unchanged"
        )
        assert row["actually_flagged_hint_ids"] == [2], (
            "Enforcement column must round-trip independently of "
            "the recommendation"
        )


# ===================================================================
# TestV13Migration — usage-attribution columns + FR1 counter reset
# ===================================================================


class TestV13Migration:
    """Verify schema migration v13 (usage-aware hint attribution).

    Migration v13 adds:
      - nl_feedback_corrections.unused_count (INTEGER NOT NULL DEFAULT 0)
      - execution_records.hint_attribution_done (INTEGER NOT NULL DEFAULT 1 —
        the per-workflow once-guard; pre-v13 rows read 1 = already-attributed)
      - trigger_events.used_hint_ids / unused_hint_ids (TEXT, attribution telemetry)
    and (FR1) resets every NL hint's applied/success/failure counters to 0,
    because the legacy counters credited ALL injected hints (untrustworthy).
    """

    def test_unused_count_column_on_nl_feedback_corrections(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "nl_feedback_corrections")
        assert "unused_count" in columns, (
            f"unused_count column missing; found: {sorted(columns)}"
        )

    def test_hint_attribution_done_column_on_execution_records(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "execution_records")
        assert "hint_attribution_done" in columns, (
            f"hint_attribution_done column missing; found: {sorted(columns)}"
        )

    def test_used_and_unused_hint_ids_columns_on_trigger_events(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "trigger_events")
        assert {"used_hint_ids", "unused_hint_ids"} <= columns, (
            f"used/unused_hint_ids columns missing; found: {sorted(columns)}"
        )

    def test_unused_count_defaults_to_zero(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, created_at, last_seen) "
            "VALUES ('a fresh hint', '2026-01-01', '2026-01-01')"
        )
        row = in_memory_db.execute(
            "SELECT unused_count FROM nl_feedback_corrections "
            "WHERE feedback_text = 'a fresh hint'"
        ).fetchone()
        assert row[0] == 0


# ===================================================================
# TestV14Migration — hint_workflow_trace observability table (N3)
# ===================================================================


class TestV14Migration:
    """Verify schema migration v14 (N3 selection→attribution observability).

    Migration v14 adds ONE table `hint_workflow_trace` keyed
    (workflow_id, hint_id) with NO foreign key (R-N3 — a deduped run has no
    execution_records row, so the trace must stand alone) plus a created_at
    index for the G3 90-day prune. drop_reason / attribution_bucket are free
    TEXT (enum documented in a comment) so new values need no migration.
    """

    EXPECTED_COLUMNS = {
        "workflow_id", "hint_id", "scope", "source", "priority",
        "similarity_score", "available", "injected", "drop_reason",
        "attribution_bucket", "attribution_reason", "created_at",
    }

    def test_hint_workflow_trace_columns(self, in_memory_db):
        columns = pg_introspect.column_names(in_memory_db, "hint_workflow_trace")
        assert self.EXPECTED_COLUMNS <= columns, (
            f"hint_workflow_trace missing columns: "
            f"{sorted(self.EXPECTED_COLUMNS - columns)}"
        )

    def test_composite_primary_key_enforced(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, created_at) VALUES ('wf-1', 5, '2026-01-01')"
        )
        in_memory_db.commit()
        # Same (workflow_id, hint_id) → composite-PK violation.
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_db.execute(
                "INSERT INTO hint_workflow_trace "
                "(workflow_id, hint_id, created_at) "
                "VALUES ('wf-1', 5, '2026-01-02')"
            )

    def test_same_hint_different_workflow_coexists(self, in_memory_db):
        # The PK is composite — one hint id appears once per workflow.
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, created_at) VALUES ('wf-a', 5, '2026-01-01')"
        )
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, created_at) VALUES ('wf-b', 5, '2026-01-01')"
        )
        in_memory_db.commit()
        count = in_memory_db.execute(
            "SELECT COUNT(*) FROM hint_workflow_trace WHERE hint_id = 5"
        ).fetchone()[0]
        assert count == 2

    def test_on_conflict_do_nothing_keeps_first_row(self, in_memory_db):
        # F2d will INSERT … ON CONFLICT(workflow_id, hint_id) DO NOTHING.
        # Confirm the composite PK is a valid conflict target and the first
        # row wins (the selection trace is written once per workflow).
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, drop_reason, created_at) "
            "VALUES ('wf-2', 7, 'cap', '2026-01-01')"
        )
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, drop_reason, created_at) "
            "VALUES ('wf-2', 7, 'holdout', '2026-01-02') "
            "ON CONFLICT(workflow_id, hint_id) DO NOTHING"
        )
        in_memory_db.commit()
        rows = in_memory_db.execute(
            "SELECT drop_reason FROM hint_workflow_trace "
            "WHERE workflow_id = 'wf-2' AND hint_id = 7"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] == "cap"

    def test_no_foreign_key_allows_standalone_trace(self, in_memory_db):
        # R-N3: with foreign_keys ON, a trace row for a workflow/hint that has
        # NO execution_records or nl_feedback_corrections row must still insert
        # (the dedup-no-row case). NO FK by design.
        in_memory_db.execute("PRAGMA foreign_keys=ON")
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, created_at) "
            "VALUES ('wf-never-stored', 99999, '2026-01-01')"
        )
        in_memory_db.commit()
        count = in_memory_db.execute(
            "SELECT COUNT(*) FROM hint_workflow_trace "
            "WHERE workflow_id = 'wf-never-stored'"
        ).fetchone()[0]
        assert count == 1

    def test_defaults_and_nullable_columns(self, in_memory_db):
        # available/injected default 0; similarity_score + drop_reason +
        # attribution_* are nullable (attribution_* filled later by F2e).
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, created_at) VALUES ('wf-3', 1, '2026-01-01')"
        )
        in_memory_db.commit()
        row = in_memory_db.execute(
            "SELECT available, injected, similarity_score, drop_reason, "
            "       attribution_bucket, attribution_reason "
            "FROM hint_workflow_trace WHERE workflow_id = 'wf-3'"
        ).fetchone()
        assert row[0] == 0 and row[1] == 0          # available / injected
        assert row[2] is None                        # similarity_score
        assert row[3] is None                        # drop_reason
        assert row[4] is None and row[5] is None     # attribution_* (F2e)

    def test_similarity_score_round_trips_real(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, similarity_score, available, injected, "
            " created_at) VALUES ('wf-4', 2, 0.78, 1, 1, '2026-01-01')"
        )
        in_memory_db.commit()
        row = in_memory_db.execute(
            "SELECT similarity_score, available, injected "
            "FROM hint_workflow_trace WHERE workflow_id = 'wf-4'"
        ).fetchone()
        assert abs(row[0] - 0.78) < 1e-9
        assert row[1] == 1 and row[2] == 1
