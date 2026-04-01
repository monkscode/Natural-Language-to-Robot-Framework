"""
DAY_01 Tests -- migrated from scripts/verify_day01.py

Validates all acceptance criteria for the Execution Memory module.
Uses pytest fixtures from conftest.py for database and temporary directory management.
"""

import sqlite3
import os
import uuid
from datetime import datetime

import pytest

from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionRecord,
    ExecutionMemory,
    CodeStructureExtractor,
)
from src.backend.crew_ai.optimization.learning_config import (
    ExecutionStore,
    SemanticStore,
    LEARNING_CONFIG,
    extract_domain,
)


def make_record(**overrides) -> ExecutionRecord:
    """Create an ExecutionRecord with sensible defaults for testing."""
    defaults = {
        "workflow_id": str(uuid.uuid4()),
        "timestamp": datetime.now(),
        "user_query": "click the submit button",
        "url": "https://www.demoqa.com/buttons",
        "domain": "demoqa.com",
        "robot_code": "*** Test Cases ***\nClick Submit\n    Click    id=submit",
        "code_structure": "linear",
        "test_status": "passed",
        "execution_exit_code": 0,
        "execution_duration_ms": 1500,
        "total_llm_calls": 3,
        "total_cost": 0.05,
    }
    defaults.update(overrides)
    return ExecutionRecord(**defaults)


class TestExecutionRecord:
    """Verify ExecutionRecord dataclass has all Phase 1 fields."""

    def test_has_all_phase1_fields(self):
        record = make_record()
        expected_fields = {
            "workflow_id", "timestamp", "user_query", "url", "domain",
            "robot_code", "code_structure", "test_status", "execution_exit_code",
            "execution_duration_ms", "failure_category", "failed_keyword",
            "error_message", "total_llm_calls", "total_cost",
            "user_feedback", "user_feedback_type",
        }
        actual_fields = set(record.__dataclass_fields__.keys())
        missing = expected_fields - actual_fields
        assert len(missing) == 0, (
            f"ExecutionRecord has all Phase 1 fields: missing={sorted(missing)}"
        )

    def test_default_test_status_is_error(self):
        record = ExecutionRecord(
            workflow_id="test", timestamp=datetime.now(), user_query="test"
        )
        assert record.test_status == "error", (
            "ExecutionRecord default test_status is 'error'"
        )

    def test_default_total_llm_calls_is_zero(self):
        record = ExecutionRecord(
            workflow_id="test", timestamp=datetime.now(), user_query="test"
        )
        assert record.total_llm_calls == 0, (
            "ExecutionRecord default total_llm_calls is 0"
        )

    def test_user_feedback_defaults_to_none(self):
        record = ExecutionRecord(
            workflow_id="test", timestamp=datetime.now(), user_query="test"
        )
        assert record.user_feedback is None, (
            "ExecutionRecord user_feedback defaults to None"
        )


class TestExtractDomain:
    """Verify extract_domain() parses various URL formats."""

    def test_standard_url(self):
        result = extract_domain("https://www.demoqa.com/elements")
        assert result == "demoqa.com", (
            f"extract_domain: standard URL: got '{result}'"
        )

    def test_url_without_www(self):
        result = extract_domain("https://demoqa.com/test")
        assert result == "demoqa.com", (
            f"extract_domain: URL without www: got '{result}'"
        )

    def test_herokuapp(self):
        result = extract_domain("http://the-internet.herokuapp.com")
        assert result == "the-internet.herokuapp.com", (
            f"extract_domain: herokuapp: got '{result}'"
        )

    def test_invalid_url(self):
        result = extract_domain("not_a_url")
        assert result == "unknown", (
            f"extract_domain: invalid URL: got '{result}'"
        )

    def test_empty_string(self):
        result = extract_domain("")
        assert result == "unknown", (
            f"extract_domain: empty string: got '{result}'"
        )


class TestCodeStructureExtractor:
    """Verify CodeStructureExtractor.detect() identifies all 4 structure types."""

    def test_linear(self):
        result = CodeStructureExtractor.detect("Click    id=submit")
        assert result == "linear", f"CodeStructure: linear: got '{result}'"

    def test_for_loop(self):
        result = CodeStructureExtractor.detect(
            "FOR    ${item}    IN    @{items}\n    Click    ${item}\nEND"
        )
        assert result == "for_loop", f"CodeStructure: for_loop: got '{result}'"

    def test_conditional(self):
        result = CodeStructureExtractor.detect(
            "Run Keyword If    '${status}' == 'active'    Click    id=btn"
        )
        assert result == "conditional", (
            f"CodeStructure: conditional: got '{result}'"
        )

    def test_conditional_if_keyword(self):
        result = CodeStructureExtractor.detect(
            "IF    '${status}' == 'active'\n    Click    id=btn\nEND"
        )
        assert result == "conditional", (
            f"CodeStructure: conditional (IF keyword): got '{result}'"
        )

    def test_mixed(self):
        result = CodeStructureExtractor.detect(
            "FOR    ${item}    IN    @{items}\n"
            "    Run Keyword If    '${item}' != ''    Click    ${item}\nEND"
        )
        assert result == "mixed", f"CodeStructure: mixed: got '{result}'"

    def test_empty(self):
        result = CodeStructureExtractor.detect("")
        assert result == "linear", f"CodeStructure: empty/None: got '{result}'"

    def test_none_input(self):
        result = CodeStructureExtractor.detect(None)
        assert result == "linear", f"CodeStructure: None input: got '{result}'"


class TestExecutionMemoryInit:
    """Verify ExecutionMemory init sets WAL, Row factory, and delegates to SchemaManager."""

    def test_row_factory_is_sqlite_row(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_exec.db")
        em = ExecutionMemory(
            db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma")
        )
        assert em.conn.row_factory == sqlite3.Row, (
            "conn.row_factory = sqlite3.Row"
        )
        em.close()

    def test_wal_mode_enabled(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_exec.db")
        em = ExecutionMemory(
            db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma")
        )
        journal = em.conn.execute("PRAGMA journal_mode").fetchone()
        assert journal[0] == "wal", f"WAL mode enabled: mode={journal[0]}"
        em.close()

    def test_schema_applied_tables_exist(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_exec.db")
        em = ExecutionMemory(
            db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma")
        )
        tables = [
            row[0]
            for row in em.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        assert "execution_records" in tables, (
            f"_ensure_schema() created tables via SchemaManager: tables={tables}"
        )
        em.close()

    def test_chromadb_not_initialized_at_init(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_exec.db")
        em = ExecutionMemory(
            db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma")
        )
        assert em._chroma_client is None, (
            "ChromaDB not initialized at init (lazy loading): "
            "Should be None until first semantic operation"
        )
        em.close()


class TestStoreAndGet:
    """Verify store() writes to SQLite and get() retrieves correctly."""

    def test_store_and_get_roundtrip(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_store.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_store"),
        )

        record = make_record(
            workflow_id="wf-store-001",
            user_query="fill in the login form",
            url="https://www.example.com/login",
            domain="example.com",
            test_status="passed",
            failure_category=None,
            total_llm_calls=5,
            total_cost=0.12,
        )
        em.store(record)

        retrieved = em.get("wf-store-001")
        assert retrieved is not None, "store() + get() round-trip works"
        assert retrieved.workflow_id == "wf-store-001", (
            "Retrieved record has correct workflow_id"
        )
        assert retrieved.user_query == "fill in the login form", (
            "Retrieved record has correct user_query"
        )
        assert retrieved.domain == "example.com", (
            "Retrieved record has correct domain"
        )
        assert abs(retrieved.total_cost - 0.12) < 0.001, (
            f"Retrieved record has correct total_cost: got {retrieved.total_cost}"
        )

        em.close()

    def test_get_returns_none_for_missing_id(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_store.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_store"),
        )

        missing = em.get("nonexistent-id")
        assert missing is None, "get() returns None for missing workflow_id"

        em.close()


class TestDeduplication:
    """Verify _store_sqlite() deduplication at threshold=5."""

    def test_deduplication_threshold(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_dedup.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_dedup"),
        )

        # Store 5 identical runs (same query, domain, status)
        for i in range(5):
            record = make_record(
                workflow_id=f"wf-dedup-{i:03d}",
                user_query="click the submit button",
                domain="demoqa.com",
                test_status="passed",
                total_llm_calls=1,
                total_cost=0.01,
            )
            em._store_sqlite(record)

        count_after_5 = em.get_total_records()
        assert count_after_5 == 5, (
            f"5 identical runs stored as 5 rows (below threshold): "
            f"count={count_after_5}"
        )

        # Store 6th identical run -- should aggregate into existing
        record_6 = make_record(
            workflow_id="wf-dedup-005",
            user_query="click the submit button",
            domain="demoqa.com",
            test_status="passed",
            total_llm_calls=2,
            total_cost=0.03,
        )
        em._store_sqlite(record_6)

        count_after_6 = em.get_total_records()
        assert count_after_6 == 5, (
            f"6th identical run aggregated (no new row): "
            f"count={count_after_6} (should still be 5)"
        )

        # Different status = different bucket (should create new row)
        record_diff = make_record(
            workflow_id="wf-dedup-006",
            user_query="click the submit button",
            domain="demoqa.com",
            test_status="failed",
            total_llm_calls=1,
            total_cost=0.01,
        )
        em._store_sqlite(record_diff)

        count_after_diff = em.get_total_records()
        assert count_after_diff == 6, (
            f"Different status creates new row (not deduplicated): "
            f"count={count_after_diff}"
        )

        em.close()


class TestQueryMethods:
    """Verify query_by_domain(), query_failures(), get_domain_stats()."""

    def test_query_methods(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_query.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_query"),
        )

        # Seed test data
        records = [
            make_record(
                workflow_id="wf-q-001", domain="demoqa.com",
                test_status="passed",
            ),
            make_record(
                workflow_id="wf-q-002", domain="demoqa.com",
                test_status="failed", failure_category="B1",
            ),
            make_record(
                workflow_id="wf-q-003", domain="demoqa.com",
                test_status="failed", failure_category="A1",
            ),
            make_record(
                workflow_id="wf-q-004", domain="example.com",
                test_status="passed",
            ),
            make_record(
                workflow_id="wf-q-005", domain="example.com",
                test_status="failed", failure_category="C1",
            ),
        ]
        for r in records:
            em._store_sqlite(r)

        # query_by_domain
        demoqa_records = em.query_by_domain("demoqa.com")
        assert len(demoqa_records) == 3, (
            f"query_by_domain returns correct count: got {len(demoqa_records)}"
        )

        # query_failures -- no filter
        all_failures = em.query_failures()
        assert len(all_failures) == 3, (
            f"query_failures (no filter) returns all failures: "
            f"got {len(all_failures)}"
        )

        # query_failures -- category filter
        a1_failures = em.query_failures(category="A1")
        assert len(a1_failures) == 1, (
            f"query_failures (category=A1) returns 1: got {len(a1_failures)}"
        )

        # query_failures -- domain filter
        example_failures = em.query_failures(domain="example.com")
        assert len(example_failures) == 1, (
            f"query_failures (domain=example.com) returns 1: "
            f"got {len(example_failures)}"
        )

        # query_failures -- category + domain filter
        b1_demoqa = em.query_failures(category="B1", domain="demoqa.com")
        assert len(b1_demoqa) == 1, (
            f"query_failures (category=B1, domain=demoqa.com) returns 1: "
            f"got {len(b1_demoqa)}"
        )

        # get_domain_stats
        stats = em.get_domain_stats("demoqa.com")
        assert stats["total"] == 3, (
            f"get_domain_stats total correct: got {stats}"
        )
        assert abs(stats["pass_rate"] - 1 / 3) < 0.01, (
            f"get_domain_stats pass_rate correct: "
            f"pass_rate={stats['pass_rate']:.3f}"
        )

        # get_total_records
        total = em.get_total_records()
        assert total == 5, f"get_total_records returns 5: got {total}"

        em.close()


class TestUserFeedback:
    """Verify update_user_feedback() updates feedback fields."""

    def test_user_feedback_update(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_feedback.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_fb"),
        )

        record = make_record(workflow_id="wf-fb-001")
        em._store_sqlite(record)

        # Verify feedback is None initially
        before = em.get("wf-fb-001")
        assert before.user_feedback is None, "Feedback is None before update"

        # Update feedback
        em.update_user_feedback(
            "wf-fb-001",
            "The test missed the dropdown selection step",
            "close_enough",
        )

        after = em.get("wf-fb-001")
        assert after.user_feedback == "The test missed the dropdown selection step", (
            "update_user_feedback sets feedback text"
        )
        assert after.user_feedback_type == "close_enough", (
            "update_user_feedback sets feedback type"
        )

        em.close()


class TestRowToRecord:
    """Verify _row_to_record uses sqlite3.Row named-column access."""

    def test_row_to_record(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_row.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_row"),
        )

        record = make_record(
            workflow_id="wf-row-001",
            failure_category="B1",
            failed_keyword="Input Text",
            error_message="No keyword with name 'Input Text' found",
        )
        em._store_sqlite(record)

        row = em.conn.execute(
            "SELECT * FROM execution_records WHERE workflow_id = ?",
            ("wf-row-001",),
        ).fetchone()

        # Verify Row supports named access
        assert row["workflow_id"] == "wf-row-001", (
            "sqlite3.Row supports named-column access"
        )

        converted = em._row_to_record(row)
        assert (
            converted.failure_category == "B1"
            and converted.failed_keyword == "Input Text"
        ), (
            f"_row_to_record converts correctly: "
            f"category={converted.failure_category}, "
            f"keyword={converted.failed_keyword}"
        )

        em.close()


class TestChromaDBOperations:
    """Verify ChromaDB lazy init, store, and semantic search."""

    def test_chromadb_lazy_init_and_search(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_chroma.db")
        chroma_dir = os.path.join(tmp_dir, "chroma_ops")
        em = ExecutionMemory(db_path=db_path, chroma_dir=chroma_dir)

        # ChromaDB should not be initialized yet
        assert em._chroma_client is None, (
            "ChromaDB lazy: not initialized before first use"
        )

        # Store records (triggers ChromaDB init via _store_chromadb)
        records = [
            make_record(
                workflow_id="wf-chroma-001",
                user_query="fill in login form and submit",
            ),
            make_record(
                workflow_id="wf-chroma-002",
                user_query="click all checkboxes in the table",
            ),
            make_record(
                workflow_id="wf-chroma-003",
                user_query="enter username and password then login",
            ),
        ]
        for r in records:
            em.store(r)

        # ChromaDB should now be initialized
        assert em._chromadb_available, (
            "ChromaDB lazy: initialized after first store"
        )

        # Semantic search
        results = em.find_similar_executions("login with credentials", top_k=2)
        assert results is not None and len(results.get("ids", [[]])[0]) > 0, (
            f"find_similar_executions returns results: "
            f"result_count="
            f"{len(results.get('ids', [[]])[0]) if results else 0}"
        )

        em.close()


class TestErrorResilience:
    """Verify store operations handle errors gracefully."""

    def test_normal_store_works(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_resilience.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_res"),
        )

        record = make_record(workflow_id="wf-res-001")
        em.store(record)
        # If we reach here without exception, the store works
        em.close()

    def test_duplicate_workflow_id_raises_error(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_resilience.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_res"),
        )

        record = make_record(workflow_id="wf-res-001")
        em._store_sqlite(record)

        with pytest.raises(sqlite3.IntegrityError):
            dup = make_record(
                workflow_id="wf-res-001", user_query="different query"
            )
            em._store_sqlite(dup)

        em.conn.rollback()
        em.close()


class TestABCImplementation:
    """Verify ExecutionMemory implements ExecutionStore and SemanticStore ABCs."""

    def test_is_subclass_of_execution_store(self):
        assert issubclass(ExecutionMemory, ExecutionStore), (
            "ExecutionMemory is subclass of ExecutionStore"
        )

    def test_is_subclass_of_semantic_store(self):
        assert issubclass(ExecutionMemory, SemanticStore), (
            "ExecutionMemory is subclass of SemanticStore"
        )


class TestConfigPath:
    """Verify LEARNING_CONFIG has updated ChromaDB path."""

    def test_chromadb_dir_setting(self):
        assert LEARNING_CONFIG["CHROMADB_DIR"] == "data/learning_chromadb", (
            f"LEARNING_CONFIG CHROMADB_DIR is 'data/learning_chromadb': "
            f"got '{LEARNING_CONFIG['CHROMADB_DIR']}'"
        )
