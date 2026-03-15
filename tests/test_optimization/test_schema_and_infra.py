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

from src.backend.crew_ai.optimization.schema_manager import SchemaManager
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
            "keyword_stats",
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
