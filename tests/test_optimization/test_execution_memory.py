"""
DAY_01 Tests -- migrated from scripts/verify_day01.py

Validates all acceptance criteria for the Execution Memory module.
Uses pytest fixtures from conftest.py for database and temporary directory management.
"""

import sqlite3
import os
import threading
import uuid
from datetime import datetime
from unittest.mock import patch, MagicMock

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


class TestHintAttributionDone:
    """C1: hint_attribution_done is mapped onto the dataclass, written 0 by
    _store_sqlite's INSERT, and resolves to an int (never AttributeError) on
    read — including Case-B / re-run records. The DB column DEFAULT is 1
    (pre-v13 rows read already-attributed, N4); the dataclass default and the
    INSERT are 0, and must NOT be unified with the DB default."""

    def test_dataclass_default_is_zero(self):
        record = ExecutionRecord(
            workflow_id="t", timestamp=datetime.now(), user_query="q"
        )
        assert record.hint_attribution_done == 0

    def test_store_inserts_zero_and_get_resolves_int(self, tmp_dir):
        em = ExecutionMemory(
            db_path=os.path.join(tmp_dir, "attr.db"),
            chroma_dir=os.path.join(tmp_dir, "chroma_attr"),
        )
        em._store_sqlite(make_record(workflow_id="wf-attr-1"))
        fetched = em.get("wf-attr-1")
        assert fetched.hint_attribution_done == 0  # INSERT writes 0, not the DB DEFAULT 1
        em.close()

    def test_case_b_rerun_leaves_attribution_done_resolvable(self, tmp_dir):
        """A v1 fail then v2 pass (Case B -> _update_to_passing_state) leaves
        hint_attribution_done untouched at 0, and the read resolves to an int
        with no AttributeError — the gate the Step-4 attribution block needs."""
        em = ExecutionMemory(
            db_path=os.path.join(tmp_dir, "attr_b.db"),
            chroma_dir=os.path.join(tmp_dir, "chroma_attr_b"),
        )
        wid = "wf-attr-caseb"
        em._store_sqlite(make_record(workflow_id=wid, test_status="failed"))
        # Re-run with the same workflow_id that now passes -> Case B recovery.
        em._store_sqlite(make_record(workflow_id=wid, test_status="passed"))
        fetched = em.get(wid)
        assert fetched.test_status == "passed"       # Case B applied
        assert fetched.hint_attribution_done == 0    # untouched -> v2-pass can claim
        em.close()


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
        assert em._writer_conn.row_factory == sqlite3.Row, (
            "conn.row_factory = sqlite3.Row"
        )
        em.close()

    def test_wal_mode_enabled(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_exec.db")
        em = ExecutionMemory(
            db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma")
        )
        journal = em._writer_conn.execute("PRAGMA journal_mode").fetchone()
        assert journal[0] == "wal", f"WAL mode enabled: mode={journal[0]}"
        em.close()

    def test_schema_applied_tables_exist(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_exec.db")
        em = ExecutionMemory(
            db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma")
        )
        tables = [
            row[0]
            for row in em._writer_conn.execute(
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

        row = em._writer_conn.execute(
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

    def test_duplicate_workflow_id_raises_error_on_non_passing_rerun(self, tmp_dir):
        """Schema v6 onwards: a duplicate workflow_id with test_status != 'passed'
        still raises IntegrityError. Only the test_status='passed' branch
        triggers the Case B recovery path (covered by the next test)."""
        db_path = os.path.join(tmp_dir, "test_resilience.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_res"),
        )

        # First store with passing status (matches make_record default).
        record = make_record(workflow_id="wf-res-001")
        em._store_sqlite(record)

        # Duplicate workflow_id with status='failed' must propagate IntegrityError.
        with pytest.raises(sqlite3.IntegrityError):
            dup = make_record(
                workflow_id="wf-res-001",
                user_query="different query",
                test_status="failed",
                failure_category="B1",
            )
            em._store_sqlite(dup)

        # _store_sqlite already rolled back inside its IntegrityError handler;
        # the explicit rollback here is defensive (no-op if state is clean).
        em._writer_conn.rollback()
        em.close()

    def test_duplicate_workflow_id_case_b_recovery(self, tmp_dir):
        """Schema v6 Case B path: a previously-failed workflow that re-runs
        and now passes must NOT raise — the row is recovered in place.

        Verifies: original failure context (robot_code, failure_category,
        error_message, failed_keyword) is preserved; test_status flips to
        'passed'; working_code stores the corrected code."""
        db_path = os.path.join(tmp_dir, "test_resilience.db")
        em = ExecutionMemory(
            db_path=db_path,
            chroma_dir=os.path.join(tmp_dir, "chroma_res"),
        )

        # Run 1 — failed test, captures v1 robot_code and failure context.
        run1 = make_record(
            workflow_id="wf-caseb-001",
            robot_code="*** Test Cases ***\nBroken\n    Click    id=does-not-exist",
            test_status="failed",
            failure_category="B1",
            failed_keyword="Click",
            error_message="No element with id=does-not-exist",
        )
        em._store_sqlite(run1)

        # Run 2 — same workflow_id, edited code now passes.
        # Must NOT raise; instead must call _update_to_passing_state.
        run2 = make_record(
            workflow_id="wf-caseb-001",
            robot_code="*** Test Cases ***\nFixed\n    Click    id=submit",
            test_status="passed",
        )
        em._store_sqlite(run2)  # no IntegrityError

        row = em._writer_conn.execute(
            "SELECT robot_code, working_code, test_status, "
            "       failure_category, failed_keyword, error_message "
            "FROM execution_records WHERE workflow_id = ?",
            ("wf-caseb-001",),
        ).fetchone()

        # Failure context preserved from Run 1.
        assert row["robot_code"] == run1.robot_code
        assert row["failure_category"] == "B1"
        assert row["failed_keyword"] == "Click"
        assert row["error_message"] == "No element with id=does-not-exist"
        # Recovery columns reflect Run 2's outcome.
        assert row["working_code"] == run2.robot_code
        assert row["test_status"] == "passed"
        em.close()


class TestInjectedHintIdsStorage:
    """Schema v10 contract: injected_hint_ids is written on INSERT, preserved
    across the dedup UPDATE, and preserved across the Case B passing-state
    update. _row_to_record round-trips the column.

    These invariants are load-bearing for Trigger 1 / Trigger 2: the trigger
    paths read injected_hint_ids off the execution_records row to decide which
    hints to judge. Overwriting it later would feed the LLM the wrong set."""

    def test_insert_writes_injected_hint_ids(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        record = make_record(workflow_id="wf-inj-001")
        record.injected_hint_ids = "[5, 12]"
        em._store_sqlite(record)

        row = em._writer_conn.execute(
            "SELECT injected_hint_ids FROM execution_records WHERE workflow_id = ?",
            ("wf-inj-001",),
        ).fetchone()
        assert row["injected_hint_ids"] == "[5, 12]"
        em.close()

    def test_insert_writes_known_empty_marker(self, tmp_dir):
        """'[]' must persist as the known-empty marker, distinct from NULL."""
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        record = make_record(workflow_id="wf-inj-002")
        record.injected_hint_ids = "[]"
        em._store_sqlite(record)

        row = em._writer_conn.execute(
            "SELECT injected_hint_ids FROM execution_records WHERE workflow_id = ?",
            ("wf-inj-002",),
        ).fetchone()
        assert row["injected_hint_ids"] == "[]"
        em.close()

    def test_insert_persists_null_when_unset(self, tmp_dir):
        """Legacy / unset rows must store NULL (the dataclass default)."""
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        record = make_record(workflow_id="wf-inj-003")
        # No injected_hint_ids set -> defaults to None.
        em._store_sqlite(record)

        row = em._writer_conn.execute(
            "SELECT injected_hint_ids FROM execution_records WHERE workflow_id = ?",
            ("wf-inj-003",),
        ).fetchone()
        assert row["injected_hint_ids"] is None
        em.close()

    def test_dedup_update_does_not_overwrite_injected_hint_ids(self, tmp_dir):
        """After DEDUPLICATION_THRESHOLD identical runs, the dedup UPDATE
        kicks in. It must preserve the original row's injected_hint_ids so
        Trigger 1 sees the original (correct) set on the surviving workflow_id.

        Note: setup uses (threshold - 1) fills, not (threshold), because the
        threshold check is `>=` and we need exactly DEDUPLICATION_THRESHOLD
        rows present before `triggering` to make `triggering` hit the dedup
        UPDATE branch."""
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        threshold = em.DEDUPLICATION_THRESHOLD  # 5

        # First store: original injected_hint_ids.
        first = make_record(workflow_id="wf-dedup-001", user_query="same q",
                             domain="example.com")
        first.injected_hint_ids = "[7, 14]"
        em._store_sqlite(first)

        # Reach threshold with identical query+domain+status records.
        # (threshold - 1) inserts so COUNT(existing) reaches DEDUPLICATION_THRESHOLD
        # exactly when `triggering` is stored.
        for i in range(threshold - 1):
            r = make_record(workflow_id=f"wf-dedup-fill-{i}", user_query="same q",
                             domain="example.com")
            r.injected_hint_ids = "[]"   # different injected set in later runs
            em._store_sqlite(r)

        # Now a further identical record triggers the dedup UPDATE branch on
        # the MOST RECENT matching row (fill-3).
        triggering = make_record(workflow_id="wf-dedup-trig", user_query="same q",
                                  domain="example.com")
        triggering.injected_hint_ids = "[99]"   # would be wrong if it overwrote
        em._store_sqlite(triggering)

        # The first row's injected_hint_ids must be unchanged.
        original_row = em._writer_conn.execute(
            "SELECT injected_hint_ids FROM execution_records WHERE workflow_id = ?",
            ("wf-dedup-001",),
        ).fetchone()
        assert original_row["injected_hint_ids"] == "[7, 14]"

        # Each fill-row also has its original injected_hint_ids preserved,
        # including the most-recent one (fill-3) that was the dedup-UPDATE target.
        for i in range(threshold - 1):
            r = em._writer_conn.execute(
                "SELECT injected_hint_ids FROM execution_records WHERE workflow_id = ?",
                (f"wf-dedup-fill-{i}",),
            ).fetchone()
            assert r is not None, f"wf-dedup-fill-{i} missing from DB"
            assert r["injected_hint_ids"] == "[]"

        # And the triggering row was NOT inserted (dedup UPDATEd fill-3 instead).
        trig_row = em._writer_conn.execute(
            "SELECT injected_hint_ids FROM execution_records WHERE workflow_id = ?",
            ("wf-dedup-trig",),
        ).fetchone()
        assert trig_row is None, (
            "triggering row should NOT exist as a separate record after dedup UPDATE"
        )
        em.close()

    def test_update_to_passing_state_preserves_injected_hint_ids(self, tmp_dir):
        """Case B recovery (failed -> re-run passes with edited code) must
        preserve the original injected_hint_ids. Trigger 1 fires next and must
        judge the hints that shaped the failing v1 code, not [] or any new
        value."""
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        run1 = make_record(
            workflow_id="wf-caseb-inj-001",
            robot_code="*** Test Cases ***\nBroken\n    Click    id=missing",
            test_status="failed",
            failure_category="C1",
        )
        run1.injected_hint_ids = "[3, 8, 21]"
        em._store_sqlite(run1)

        run2 = make_record(
            workflow_id="wf-caseb-inj-001",
            robot_code="*** Test Cases ***\nFixed\n    Click    id=submit",
            test_status="passed",
        )
        # Pretend the re-run had a different injected set (e.g., 0 hints
        # because OPTIMIZATION_ENABLED was toggled off, or different agent
        # state). The Case B branch must NOT overwrite.
        run2.injected_hint_ids = "[]"
        em._store_sqlite(run2)

        row = em._writer_conn.execute(
            "SELECT injected_hint_ids, test_status, working_code "
            "FROM execution_records WHERE workflow_id = ?",
            ("wf-caseb-inj-001",),
        ).fetchone()
        assert row["injected_hint_ids"] == "[3, 8, 21]"
        assert row["test_status"] == "passed"
        assert row["working_code"] == run2.robot_code
        em.close()

    def test_update_user_feedback_preserves_injected_hint_ids(self, tmp_dir):
        """The fourth write path against execution_records is update_user_feedback.
        Trigger 2 reads record.injected_hint_ids AFTER user feedback has been
        attached -- a silent overwrite here would feed Trigger 2 the wrong
        hint IDs to judge."""
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        record = make_record(workflow_id="wf-ufb-001")
        record.injected_hint_ids = "[3, 8, 21]"
        em._store_sqlite(record)

        em.update_user_feedback(
            workflow_id="wf-ufb-001",
            feedback_text="this is wrong, click the other button",
            feedback_type="correction",
        )

        row = em._writer_conn.execute(
            "SELECT injected_hint_ids, user_feedback, user_feedback_type "
            "FROM execution_records WHERE workflow_id = ?",
            ("wf-ufb-001",),
        ).fetchone()
        assert row["injected_hint_ids"] == "[3, 8, 21]"
        assert row["user_feedback"] == "this is wrong, click the other button"
        assert row["user_feedback_type"] == "correction"
        em.close()

    def test_row_to_record_round_trips_injected_hint_ids(self, tmp_dir):
        """The dataclass round-trip preserves the JSON string verbatim --
        no parsing, no normalisation. Trigger 1/2 parse on read; storage is
        opaque."""
        db_path = os.path.join(tmp_dir, "test_inj.db")
        em = ExecutionMemory(db_path=db_path, chroma_dir=os.path.join(tmp_dir, "chroma_inj"))

        for wid, ids in [
            ("wf-rt-001", "[1, 2, 3]"),
            ("wf-rt-002", "[]"),
            ("wf-rt-003", None),
        ]:
            r = make_record(workflow_id=wid)
            r.injected_hint_ids = ids
            em._store_sqlite(r)
            fetched = em.get(wid)
            assert fetched.injected_hint_ids == ids, (
                f"Round-trip failed for {wid}: expected {ids!r}, got {fetched.injected_hint_ids!r}"
            )
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


# ===================================================================
# C3 — ChromaDB retry cooldown and error state observability
# ===================================================================

class TestChromaRetryAndObservability:
    """C3: After ChromaDB init failure, error state is exposed and retried after cooldown.

    Concrete scenario: ChromaDB directory is on a network mount that becomes
    unavailable. Without C3, the sentinel is set forever and no health check
    surfaces the problem. With C3:
    - _chroma_failed_at and _chroma_last_error are set immediately
    - Further calls within 300s are suppressed (no log spam)
    - After 300s the next call retries automatically — operator can fix the
      mount without restarting the process
    - On successful retry both error fields are cleared
    """

    def test_failure_records_error_state(self, tmp_dir):
        """After init failure, sentinel is set and error fields are populated."""
        em = ExecutionMemory(
            db_path=os.path.join(tmp_dir, "test.db"),
            chroma_dir=os.path.join(tmp_dir, "chroma"),
        )
        with patch("chromadb.PersistentClient", side_effect=RuntimeError("disk full")):
            em._init_chromadb()

        assert em._chroma_client is em._CHROMADB_INIT_FAILED
        assert em._chroma_failed_at is not None
        assert em._chroma_last_error == "disk full"
        em.close()

    def test_within_cooldown_suppresses_retry(self, tmp_dir):
        """Second call within cooldown does not retry ChromaDB init."""
        em = ExecutionMemory(
            db_path=os.path.join(tmp_dir, "test.db"),
            chroma_dir=os.path.join(tmp_dir, "chroma"),
        )
        call_count = 0

        def failing_client(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("still down")

        with patch("chromadb.PersistentClient", side_effect=failing_client):
            em._init_chromadb()  # first call — fails, sets sentinel + timestamp
            em._init_chromadb()  # second call — within default 300s cooldown

        assert call_count == 1, (
            f"PersistentClient called once only (cooldown suppressed second call); "
            f"got {call_count}"
        )
        em.close()

    def test_expired_cooldown_triggers_retry(self, tmp_dir):
        """After cooldown expires, the next call retries ChromaDB init."""
        em = ExecutionMemory(
            db_path=os.path.join(tmp_dir, "test.db"),
            chroma_dir=os.path.join(tmp_dir, "chroma"),
        )
        call_count = 0

        def failing_client(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("still broken")

        with patch("chromadb.PersistentClient", side_effect=failing_client):
            em._init_chromadb()  # first call — fails, records _chroma_failed_at

        # Expire the cooldown by setting it to 0 on the instance
        em._CHROMA_RETRY_COOLDOWN_S = 0

        with patch("chromadb.PersistentClient", side_effect=failing_client):
            em._init_chromadb()  # second call — cooldown expired, should retry

        assert call_count == 2, (
            f"PersistentClient called twice (retry after cooldown); got {call_count}"
        )
        em.close()

    def test_successful_retry_clears_error_state(self, tmp_dir):
        """After a successful retry, _chroma_failed_at and _chroma_last_error are None."""
        em = ExecutionMemory(
            db_path=os.path.join(tmp_dir, "test.db"),
            chroma_dir=os.path.join(tmp_dir, "chroma"),
        )
        # First call fails — sets error state
        with patch("chromadb.PersistentClient", side_effect=RuntimeError("transient")):
            em._init_chromadb()

        assert em._chroma_failed_at is not None

        # Expire cooldown
        em._CHROMA_RETRY_COOLDOWN_S = 0

        # Second call succeeds — mock a working client. A real chromadb
        # collection exposes .metadata as a dict; learning_anchors init
        # asserts hnsw:space == cosine, so the mock must reflect that.
        mock_collection = MagicMock()
        mock_collection.metadata = {"hnsw:space": "cosine"}
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection
        with patch("chromadb.PersistentClient", return_value=mock_client):
            em._init_chromadb()

        assert em._chromadb_available
        assert em._chroma_failed_at is None
        assert em._chroma_last_error is None
        em.close()


class TestWriterThreadGuard:
    """_assert_writer_thread fires when a guarded write runs off the writer thread.

    The autouse `_rename_test_thread_to_writer` fixture in conftest.py renames
    the main pytest thread to WRITER_THREAD_NAME, so every other optimization
    test passes the guard implicitly and its failure branch is never exercised.
    A freshly spawned thread keeps its own name, so calling a guarded write
    method inside one reaches the AssertionError branch.
    """

    def test_guarded_write_from_non_writer_thread_raises(self, in_memory_em):
        """update_daily_stats called off the writer thread raises AssertionError.

        The guard is the method's first line, so it raises before any
        _writer_conn use — no row is written and there is no cross-thread
        sqlite access.
        """
        captured: dict = {}

        def worker():
            try:
                in_memory_em.update_daily_stats("passed")
            except Exception as e:  # record whatever was raised for assertion
                captured["err"] = e

        t = threading.Thread(target=worker, name="request-thread")
        t.start()
        t.join()

        err = captured.get("err")
        assert isinstance(err, AssertionError), (
            f"expected AssertionError from the writer-thread guard, got {err!r}"
        )
        assert "update_daily_stats" in str(err)
        assert "request-thread" in str(err)
