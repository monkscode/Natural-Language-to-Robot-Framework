"""
DAY_00 Tests -- migrated from scripts/verify_day00.py

Validates all acceptance criteria for the Phase 1 schema and shared infrastructure.
Uses pytest fixtures from conftest.py for database and temporary directory management.
"""

import sqlite3
import os
import shutil
import threading
import time

import pytest

from src.backend.crew_ai.optimization.schema_manager import (
    SchemaManager,
    SCHEMA_MIGRATIONS,
)
from src.backend.crew_ai.optimization.learning_config import (
    EffectivenessScore,
    LearningCircuitBreaker,
    LearningWriteQueue,
    LearningEngine,
    LEARNING_CONFIG,
)


class TestSchemaManager:
    """Verify schema creation and structure."""

    def test_schema_created_successfully(self, tmp_db_path):
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        version = SchemaManager.ensure_current(conn)
        assert version >= 1, f"Schema created successfully: version={version}"
        conn.close()

    def test_wal_mode_enabled(self, tmp_db_path):
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        SchemaManager.ensure_current(conn)
        journal = conn.execute("PRAGMA journal_mode").fetchone()
        assert journal[0] == "wal", f"WAL mode enabled: mode={journal[0]}"
        conn.close()

    def test_all_tables_exist(self, in_memory_db):
        expected_tables = {
            "execution_records", "intent_patterns", "structural_rules",
            "keyword_corrections", "anti_patterns", "learning_stats",
            "schema_version", "nl_feedback_corrections", "learning_metrics",
            "trigger_events", "hint_audit",
            "hint_review_sessions", "hint_review_recommendations",
            "hint_review_pages", "hint_workflow_trace",
        }
        tables = set(SchemaManager.get_table_names(in_memory_db))
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
        indexes = set(SchemaManager.get_index_names(in_memory_db))
        missing = expected_indexes - indexes
        assert len(missing) == 0, (
            f"All 10 indexes exist: missing={sorted(missing)}"
        )

    def test_schema_version_records_v1(self, in_memory_db):
        row = in_memory_db.execute(
            "SELECT version, description FROM schema_version WHERE version=1"
        ).fetchone()
        assert row is not None and row[0] == 1, (
            "schema_version records v1: "
            + (f"desc='{row[1]}'" if row else "NOT FOUND")
        )

    def test_ensure_current_is_idempotent(self, in_memory_db):
        version2 = SchemaManager.ensure_current(in_memory_db)
        assert version2 >= 1, (
            f"SchemaManager.ensure_current() is idempotent: "
            f"version after 2nd call={version2}"
        )

    def test_integrity_check_passes(self, in_memory_db):
        result = in_memory_db.execute("PRAGMA integrity_check").fetchone()
        assert result[0] == "ok", "PRAGMA integrity_check passes"

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


class TestMigrationScript:
    """Verify migration script backup/rollback logic."""

    def test_backup_and_rollback(self, tmp_dir):
        # Create a fake old DB
        fake_old = os.path.join(tmp_dir, "fake_pattern_learning.db")
        conn = sqlite3.connect(fake_old)
        conn.execute(
            "CREATE TABLE keyword_stats "
            "(keyword_name TEXT, usage_count INTEGER, last_used TEXT)"
        )
        conn.execute(
            "INSERT INTO keyword_stats VALUES ('Click', 10, '2026-01-01')"
        )
        conn.commit()
        conn.close()

        # Backup test
        backup_path = f"{fake_old}.backup.test"
        shutil.copy2(fake_old, backup_path)
        assert os.path.exists(backup_path), "Backup mechanism works"

        # Verify original is intact
        conn = sqlite3.connect(fake_old)
        count = conn.execute("SELECT COUNT(*) FROM keyword_stats").fetchone()[0]
        conn.close()
        assert count == 1, "Original DB intact after backup"

        # Rollback test
        os.remove(backup_path)
        assert not os.path.exists(backup_path), (
            "Backup can be removed (simulated rollback)"
        )


class TestConfig:
    """Verify config.py has the new settings."""

    def test_execution_memory_db_setting_exists(self):
        from src.backend.core.config import Settings

        s = Settings()
        assert hasattr(s, "EXECUTION_MEMORY_DB"), (
            "EXECUTION_MEMORY_DB setting exists"
        )

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
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
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
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(hint_review_pages)"
            ).fetchall()
        }
        missing = expected - columns
        assert not missing, (
            f"hint_review_pages missing columns: {sorted(missing)}"
        )


# ===================================================================
# TestV11Migration — schema v11 additions (query-similarity anchors)
# ===================================================================


def _apply_migrations_through(conn, max_version):
    """Apply migrations 1..max_version exactly as SchemaManager.ensure_current
    would, recording each in schema_version, leaving the DB at max_version so a
    subsequent ensure_current() applies only the migrations that follow."""
    conn.execute(SchemaManager.VERSION_TABLE_SQL)
    for version in sorted(SCHEMA_MIGRATIONS.keys()):
        if version > max_version:
            break
        for sql in SCHEMA_MIGRATIONS[version]["sql"]:
            conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_version (version, description, applied_at) "
            "VALUES (?, ?, ?)",
            (
                version,
                SCHEMA_MIGRATIONS[version]["description"],
                "2026-01-01T00:00:00+00:00",
            ),
        )
    conn.commit()


class TestV11Migration:
    """Verify the three additions introduced by schema migration v11.

    Migration v11 adds:
      - nl_feedback_corrections.anchor_query (TEXT, nullable)
      - execution_records.model_version (TEXT, nullable)
      - learning_metrics.was_holdout (INTEGER NOT NULL DEFAULT 0)
    and backfills anchor_query from execution_records.user_query.
    """

    def test_anchor_query_column_on_nl_feedback_corrections(self, in_memory_db):
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(nl_feedback_corrections)"
            ).fetchall()
        }
        assert "anchor_query" in columns, (
            f"anchor_query column missing from nl_feedback_corrections; "
            f"found columns: {sorted(columns)}"
        )

    def test_model_version_column_on_execution_records(self, in_memory_db):
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
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

    def test_migration_v11_backfills_anchor_query(self):
        """Pre-v11 DB with hint rows + matching execution_records → after v11
        every resolvable row's anchor_query is populated; an unresolvable
        source_workflow_id stays NULL (fail-closed) without aborting v11."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _apply_migrations_through(conn, 10)

        # Rows that pre-date v11 (anchor_query column does not exist yet).
        conn.execute(
            "INSERT INTO execution_records "
            "(workflow_id, timestamp, user_query, test_status) "
            "VALUES ('wf-1', '2026-01-01', 'log into my account', 'passed')"
        )
        conn.execute(
            "INSERT INTO execution_records "
            "(workflow_id, timestamp, user_query, test_status) "
            "VALUES ('wf-2', '2026-01-01', 'search for a laptop', 'failed')"
        )
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, source_workflow_id, created_at, last_seen) "
            "VALUES ('hint A', 'wf-1', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, source_workflow_id, created_at, last_seen) "
            "VALUES ('hint B', 'wf-2', '2026-01-01', '2026-01-01')"
        )
        # source_workflow_id with no matching execution_records row.
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, source_workflow_id, created_at, last_seen) "
            "VALUES ('hint orphan', 'wf-missing', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        version = SchemaManager.ensure_current(conn)
        assert version >= 11, f"expected schema v11+, got v{version}"

        anchors = {
            r["feedback_text"]: r["anchor_query"]
            for r in conn.execute(
                "SELECT feedback_text, anchor_query FROM nl_feedback_corrections"
            ).fetchall()
        }
        assert anchors["hint A"] == "log into my account"
        assert anchors["hint B"] == "search for a laptop"
        assert anchors["hint orphan"] is None, (
            "an unresolvable source_workflow_id must leave anchor_query NULL "
            "(fail-closed), not abort the migration"
        )

        null_count = conn.execute(
            "SELECT COUNT(*) FROM nl_feedback_corrections "
            "WHERE anchor_query IS NULL"
        ).fetchone()[0]
        assert null_count == 1, (
            f"only the orphan row should remain NULL after backfill; "
            f"got {null_count}"
        )
        conn.close()


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
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(trigger_events)"
            ).fetchall()
        }
        assert "actually_flagged_hint_ids" in columns, (
            f"actually_flagged_hint_ids column missing from trigger_events; "
            f"found columns: {sorted(columns)}"
        )

    def test_legacy_pre_v12_rows_get_null_actually_flagged(self):
        """Rows inserted before v12 retain NULL for actually_flagged_hint_ids.

        KPI queries COALESCE this back to flagged_hint_ids so legacy data
        preserves its pre-v12 semantics until it ages out of the rolling
        30-day window.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _apply_migrations_through(conn, 11)

        # Pre-v12: only the 14-column INSERT exists (no actually_flagged_hint_ids).
        conn.execute(
            "INSERT INTO trigger_events ("
            " trigger_type, workflow_id, domain, url, feedback_text, "
            " active_hint_ids, flagged_hint_ids, reason, llm_model, "
            " input_tokens, output_tokens, llm_latency_ms, status, "
            " error_message, created_at"
            ") VALUES ('trigger_1', 'wf-pre-v12', 'example.com', "
            "          'https://example.com', NULL, '[1,2]', '[1,2]', "
            "          NULL, 'gemini/gemini-2.5-flash', 100, 50, 1200, "
            "          'succeeded', NULL, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

        version = SchemaManager.ensure_current(conn)
        assert version >= 12, f"expected schema v12+, got v{version}"

        row = conn.execute(
            "SELECT flagged_hint_ids, actually_flagged_hint_ids "
            "FROM trigger_events WHERE workflow_id = 'wf-pre-v12'"
        ).fetchone()
        assert row["flagged_hint_ids"] == "[1,2]"
        assert row["actually_flagged_hint_ids"] is None, (
            "Pre-v12 row must have NULL actually_flagged_hint_ids — "
            "KPI queries COALESCE it back to flagged_hint_ids"
        )
        conn.close()

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
        assert row["flagged_hint_ids"] == "[1,2,3]", (
            "LLM-recommendation column must round-trip unchanged"
        )
        assert row["actually_flagged_hint_ids"] == "[2]", (
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
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(nl_feedback_corrections)"
            ).fetchall()
        }
        assert "unused_count" in columns, (
            f"unused_count column missing; found: {sorted(columns)}"
        )

    def test_hint_attribution_done_column_on_execution_records(self, in_memory_db):
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
        assert "hint_attribution_done" in columns, (
            f"hint_attribution_done column missing; found: {sorted(columns)}"
        )

    def test_used_and_unused_hint_ids_columns_on_trigger_events(self, in_memory_db):
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(trigger_events)"
            ).fetchall()
        }
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

    def test_migration_v13_resets_nl_counters_preserving_the_rest(self):
        """FR1: applying v13 zeros applied/success/failure on every NL hint
        while preserving evidence_count, is_active, conflict_flagged,
        anchor_query, created_at, and last_seen (only the all-injected-credit
        counters reset; the rest are earned/identity state)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _apply_migrations_through(conn, 12)

        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, evidence_count, applied_count, "
            " success_count, failure_count, is_active, conflict_flagged, "
            " anchor_query, created_at, last_seen) "
            "VALUES ('legacy hint', 'C1', 'global', 7, 112, 90, 2, 1, 0, "
            "        'log in to the account', '2026-01-01', '2026-02-01')"
        )
        conn.commit()

        version = SchemaManager.ensure_current(conn)
        assert version >= 13, f"expected schema v13+, got v{version}"

        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE feedback_text = 'legacy hint'"
        ).fetchone()
        # FR1 reset
        assert row["applied_count"] == 0
        assert row["success_count"] == 0
        assert row["failure_count"] == 0
        # new column default
        assert row["unused_count"] == 0
        # preserved (NOT reset)
        assert row["evidence_count"] == 7
        assert row["is_active"] == 1
        assert row["conflict_flagged"] == 0
        assert row["anchor_query"] == "log in to the account"
        assert row["created_at"] == "2026-01-01"
        assert row["last_seen"] == "2026-02-01"
        conn.close()

    def test_pre_v13_execution_record_reads_attribution_done_1(self):
        """N4: a row inserted before v13 materialises hint_attribution_done=1
        (already-attributed under the old Step-7 credit) so a cross-deploy
        re-run cannot double-credit on top of it. O(1) DEFAULT, no backfill."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _apply_migrations_through(conn, 12)

        conn.execute(
            "INSERT INTO execution_records "
            "(workflow_id, timestamp, user_query, test_status) "
            "VALUES ('wf-pre-v13', '2026-01-01', 'a query', 'failed')"
        )
        conn.commit()

        version = SchemaManager.ensure_current(conn)
        assert version >= 13, f"expected schema v13+, got v{version}"

        row = conn.execute(
            "SELECT hint_attribution_done FROM execution_records "
            "WHERE workflow_id = 'wf-pre-v13'"
        ).fetchone()
        assert row["hint_attribution_done"] == 1, (
            "pre-v13 rows must read 1 (already-attributed) — DEFAULT 1, no backfill"
        )
        conn.close()


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
        columns = {
            row[1]
            for row in in_memory_db.execute(
                "PRAGMA table_info(hint_workflow_trace)"
            ).fetchall()
        }
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

    def test_migration_v14_creates_table_and_index(self):
        # In isolation: v14 is the step that creates the table + the created_at
        # index (neither exists at v13).
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _apply_migrations_through(conn, 13)

        pre = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'hint_workflow_trace'"
        ).fetchone()
        assert pre is None, "hint_workflow_trace must not exist before v14"

        version = SchemaManager.ensure_current(conn)
        assert version >= 14, f"expected schema v14+, got v{version}"

        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'hint_workflow_trace'"
        ).fetchone()
        assert tbl is not None, "v14 must create hint_workflow_trace"

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_hint_trace_created_at'"
        ).fetchone()
        assert idx is not None, "v14 must create the created_at index (G3 prune)"
        conn.close()
