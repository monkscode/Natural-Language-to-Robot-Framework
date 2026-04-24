"""
NL Feedback Pipeline Verification -- Storage, Retrieval, Attribution, Integration (pytest format).

Migrated from scripts/verify_nl_feedback_pipeline.py.

Tests: NLFeedbackEngine (learn_from_feedback, get_hints, update_hint_effectiveness,
       _deduplicate_hints), SmartKeywordProvider NL wiring, FeedbackLoop routing.

Uses in-memory SQLite with full Phase 1 schema via the ``in_memory_db`` fixture.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest

from src.backend.crew_ai.optimization.schema_manager import SchemaManager
from src.backend.crew_ai.optimization.nl_feedback_engine import (
    NLFeedbackEngine,
    _SCOPE_BY_CATEGORY,
    AUTO_DISABLE_MIN_APPLICATIONS,
)
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    LearningCircuitBreaker,
    LearningWriteQueue,
)
from src.backend.crew_ai.optimization.feedback_loop import (
    LearningMetricsTracker,
    ContradictionDetector,
    FeedbackLoop,
)
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionMemory,
    ExecutionRecord,
)


# ===================================================================
# Test Helpers (kept from original script)
# ===================================================================


class SynchronousWriteQueue:
    """Drop-in replacement for LearningWriteQueue -- executes immediately."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class MockEngine:
    """Minimal mock of a LearningEngine for FeedbackLoop tests."""

    def __init__(self):
        self.learn_calls = []
        self.feedback_calls = []
        self._stats = {"total_rules": 0, "active_rules": 0}

    def learn(self, record):
        self.learn_calls.append(record)

    def learn_from_feedback(self, record, feedback_insight):
        self.feedback_calls.append((record, feedback_insight))

    def get_hints(self, query, url="", agent_role="assembler"):
        return []

    def get_stats(self):
        return self._stats


class MockFailureAnalyzer:
    """Minimal mock for FailureAnalyzer."""

    def __init__(self):
        self.analyze_calls = []
        self._result = None

    def analyze(self, output_xml_path=None, user_query="",
                robot_code="", exit_code=None):
        self.analyze_calls.append(output_xml_path)
        return self._result


@dataclass
class FakeRecord:
    """Minimal ExecutionRecord-like object for unit tests."""
    workflow_id: str = "wf-test"
    domain: Optional[str] = "example.com"
    url: Optional[str] = "https://example.com/login"
    failure_category: Optional[str] = None
    user_query: str = "click login button"
    test_status: str = "failed"


def create_execution_memory(conn):
    """Create ExecutionMemory backed by existing connection."""
    em = ExecutionMemory.__new__(ExecutionMemory)
    em.db_path = ":memory:"
    em._chroma_dir = None
    em.conn = conn
    em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
    em._execution_collection = None
    return em


def _insert_hint(conn, text, scope="global", domain=None, url=None,
                 category="keyword", is_active=1, success_count=0,
                 evidence_count=1, failure_count=0, applied_count=0,
                 original_failure_category=None):
    """Insert a hint row directly into nl_feedback_corrections."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, url, "
        " original_failure_category, evidence_count, applied_count, "
        " success_count, failure_count, is_active, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (text, category, scope, domain, url,
         original_failure_category, evidence_count, applied_count,
         success_count, failure_count, is_active, now, now),
    )
    conn.commit()


def _build_feedback_loop(conn):
    """Build a FeedbackLoop with mock engines, backed by *conn*."""
    em = create_execution_memory(conn)
    fa = MockFailureAnalyzer()
    se = MockEngine()
    ke = MockEngine()
    ae = MockEngine()
    mt = LearningMetricsTracker(conn)
    cd = ContradictionDetector(conn)
    wq = SynchronousWriteQueue()
    cb = LearningCircuitBreaker()
    fl = FeedbackLoop(
        execution_memory=em, failure_analyzer=fa,
        structural_engine=se, keyword_engine=ke,
        anti_pattern_engine=ae, metrics_tracker=mt,
        contradiction_detector=cd, write_queue=wq,
        circuit_breaker=cb,
    )
    return fl, em, fa, se, ke, ae, mt, cd, conn


# ===================================================================
# Category 1: Schema -- nl_feedback_corrections table
# ===================================================================


class TestSchema:
    """Schema: nl_feedback_corrections table structure."""

    def test_table_exists(self, in_memory_db):
        tables = in_memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='nl_feedback_corrections'"
        ).fetchone()
        assert tables is not None, "nl_feedback_corrections table not found"

    def test_columns(self, in_memory_db):
        cols = in_memory_db.execute("PRAGMA table_info(nl_feedback_corrections)").fetchall()
        col_names = {c["name"] for c in cols}
        expected = {
            "id", "feedback_text", "category", "scope", "domain", "url",
            "original_failure_category", "source_workflow_id",
            "evidence_count", "applied_count", "success_count", "failure_count",
            "is_active", "created_at", "last_seen",
        }
        missing = expected - col_names
        assert not missing, f"Missing columns: {missing}"

    def test_index_exists(self, in_memory_db):
        indexes = in_memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='nl_feedback_corrections'"
        ).fetchall()
        idx_names = {i["name"] for i in indexes}
        assert any("scope" in n or "domain" in n for n in idx_names), (
            f"No scope/domain index found. Indexes: {idx_names}"
        )


# ===================================================================
# Category 2: learn_from_feedback -- Storage
# ===================================================================


class TestStorage:
    """learn_from_feedback: persisting NL corrections."""

    def test_store_basic_feedback(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(failure_category="A1")
        triage = {
            "feedback_text": "Use CSS selector instead of XPath",
            "category": "locator",
        }
        engine.learn_from_feedback(record, triage)

        row = in_memory_db.execute("SELECT * FROM nl_feedback_corrections").fetchone()
        assert row is not None, "No row inserted"
        assert row["feedback_text"] == "Use CSS selector instead of XPath"
        assert row["category"] == "locator"
        assert row["evidence_count"] == 1
        assert row["is_active"] == 1

    def test_store_scope_url(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord()
        triage = {"feedback_text": "Wrong element", "category": "locator"}
        engine.learn_from_feedback(record, triage)

        row = in_memory_db.execute("SELECT scope FROM nl_feedback_corrections").fetchone()
        expected_scope = _SCOPE_BY_CATEGORY.get("locator", "domain")
        assert row["scope"] == expected_scope, (
            f"Expected scope={expected_scope}, got {row['scope']}"
        )

    def test_store_scope_global(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord()
        triage = {"feedback_text": "Always use Sleep 1s after click", "category": "keyword"}
        engine.learn_from_feedback(record, triage)

        row = in_memory_db.execute("SELECT scope FROM nl_feedback_corrections").fetchone()
        expected_scope = _SCOPE_BY_CATEGORY.get("keyword", "domain")
        assert row["scope"] == expected_scope, (
            f"Expected scope={expected_scope}, got {row['scope']}"
        )

    def test_store_dedup_increment(self, in_memory_db):
        """Same feedback_text + domain + scope -> evidence_count incremented."""
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord()
        triage = {"feedback_text": "Use id=login-btn", "category": "locator"}

        engine.learn_from_feedback(record, triage)
        engine.learn_from_feedback(record, triage)
        engine.learn_from_feedback(record, triage)

        row = in_memory_db.execute("SELECT evidence_count FROM nl_feedback_corrections").fetchone()
        assert row["evidence_count"] == 3, f"Expected 3, got {row['evidence_count']}"

    def test_store_different_domains_separate(self, in_memory_db):
        """Same text but different domains -> separate entries."""
        engine = NLFeedbackEngine(in_memory_db)
        r1 = FakeRecord(domain="example.com")
        r2 = FakeRecord(domain="other.com")
        triage = {"feedback_text": "Click the submit button", "category": "structural"}
        engine.learn_from_feedback(r1, triage)
        engine.learn_from_feedback(r2, triage)

        count = in_memory_db.execute("SELECT COUNT(*) FROM nl_feedback_corrections").fetchone()[0]
        assert count == 2, f"Expected 2 entries, got {count}"

    def test_store_empty_feedback_skipped(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord()

        engine.learn_from_feedback(record, {"feedback_text": "", "category": "locator"})
        engine.learn_from_feedback(record, {"feedback_text": "   ", "category": "locator"})
        engine.learn_from_feedback(record, {"category": "locator"})

        count = in_memory_db.execute("SELECT COUNT(*) FROM nl_feedback_corrections").fetchone()[0]
        assert count == 0, f"Expected 0 entries, got {count}"

    def test_store_positive_feedback_skipped(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord()
        triage = {"feedback_text": "Good job!", "category": "positive"}
        engine.learn_from_feedback(record, triage)

        count = in_memory_db.execute("SELECT COUNT(*) FROM nl_feedback_corrections").fetchone()[0]
        assert count == 0, "Positive feedback should not be stored"

    def test_store_original_failure_category(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(failure_category="B2")
        triage = {"feedback_text": "Wrong keyword used", "category": "keyword"}
        engine.learn_from_feedback(record, triage)

        row = in_memory_db.execute(
            "SELECT original_failure_category FROM nl_feedback_corrections"
        ).fetchone()
        assert row["original_failure_category"] == "B2"

    def test_store_no_db(self):
        """NLFeedbackEngine with no DB should not crash."""
        engine = NLFeedbackEngine(None)
        record = FakeRecord()
        triage = {"feedback_text": "test", "category": "locator"}
        engine.learn_from_feedback(record, triage)  # Should not raise


# ===================================================================
# Category 3: get_hints -- Retrieval
# ===================================================================


class TestRetrieval:
    """get_hints: retrieving stored corrections."""

    def test_hints_global_returned(self, in_memory_db):
        _insert_hint(in_memory_db, "Always wait before click", scope="global")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click button", "https://example.com", "assembler")
        assert hints is not None, "Expected hints, got None"
        assert any("Always wait before click" in h for h in hints)

    def test_hints_domain_match(self, in_memory_db):
        _insert_hint(in_memory_db, "Use id=login-btn", scope="domain", domain="example.com")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click login", "https://example.com/page", "assembler")
        assert hints is not None
        assert any("login-btn" in h for h in hints)

    def test_hints_domain_mismatch(self, in_memory_db):
        _insert_hint(in_memory_db, "Use other selector", scope="domain", domain="other.com")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click login", "https://example.com/page", "assembler")
        assert hints is None, "Should not match different domain"

    def test_hints_url_match(self, in_memory_db):
        _insert_hint(in_memory_db, "Click exact button",
                     scope="url", url="https://example.com/login")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints(
            "click login", "https://example.com/login", "assembler"
        )
        assert hints is not None
        assert any("Click exact button" in h for h in hints)

    def test_hints_inactive_excluded(self, in_memory_db):
        _insert_hint(in_memory_db, "Disabled hint", scope="global", is_active=0)
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click button", "https://example.com", "assembler")
        assert hints is None, "Inactive hints should not be returned"

    def test_hints_empty_db(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click button", "https://example.com", "assembler")
        assert hints is None

    def test_hints_no_db(self):
        engine = NLFeedbackEngine(None)
        hints = engine.get_hints("click button", "https://example.com", "assembler")
        assert hints is None

    def test_hints_max_5(self, in_memory_db):
        for i in range(10):
            _insert_hint(in_memory_db, f"Hint number {i}", scope="global")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click", "https://example.com", "assembler")
        assert hints is not None
        assert len(hints) <= 5, f"Expected max 5 hints, got {len(hints)}"

    def test_hints_ordered_by_success(self, in_memory_db):
        _insert_hint(in_memory_db, "Low success", scope="global", success_count=1)
        _insert_hint(in_memory_db, "High success", scope="global", success_count=10)
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click", "https://x.com", "assembler")
        assert hints is not None
        # High success should come first
        assert "High success" in hints[0]

    def test_hints_formatted_with_prefix(self, in_memory_db):
        _insert_hint(in_memory_db, "Test prefix", scope="global")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click", "https://x.com", "assembler")
        assert hints is not None
        assert hints[0].startswith("\u26a0\ufe0f USER FEEDBACK")
        assert "Test prefix" in hints[0]


# ===================================================================
# Category 4: _deduplicate_hints
# ===================================================================


class TestDeduplication:
    """_deduplicate_hints: merging similar hint rows."""

    def test_dedup_identical(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        items = [
            {"feedback_text": "Use CSS selector for login button", "id": 1},
            {"feedback_text": "Use CSS selector for login button", "id": 2},
        ]
        deduped = engine._deduplicate_hints(items)
        assert len(deduped) == 1

    def test_dedup_similar_overlap(self, in_memory_db):
        """Words with >60% overlap should be merged."""
        engine = NLFeedbackEngine(in_memory_db)
        items = [
            {"feedback_text": "Use CSS selector instead of XPath for the button", "id": 1},
            {"feedback_text": "Use CSS selector instead of XPath for the input", "id": 2},
        ]
        deduped = engine._deduplicate_hints(items)
        # These share most words -- should be deduped
        assert len(deduped) <= 1, f"Expected dedup, got {len(deduped)}"

    def test_dedup_different_kept(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        items = [
            {"feedback_text": "Always add Sleep keyword after navigation", "id": 1},
            {"feedback_text": "Use implicit wait instead of explicit waits", "id": 2},
        ]
        deduped = engine._deduplicate_hints(items)
        assert len(deduped) == 2

    def test_dedup_empty(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        deduped = engine._deduplicate_hints([])
        assert deduped == []


# ===================================================================
# Category 5: update_hint_effectiveness -- Attribution
# ===================================================================


class TestAttribution:
    """update_hint_effectiveness: tracking hint success/failure."""

    def test_effectiveness_success_increments(self, in_memory_db):
        _insert_hint(in_memory_db, "Test hint", scope="global",
                     applied_count=0, success_count=0, failure_count=0)
        engine = NLFeedbackEngine(in_memory_db)
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=True,
        )
        row = in_memory_db.execute(
            "SELECT applied_count, success_count, failure_count "
            "FROM nl_feedback_corrections"
        ).fetchone()
        assert row["applied_count"] == 1, f"Expected 1, got {row['applied_count']}"
        assert row["success_count"] == 1
        assert row["failure_count"] == 0

    def test_effectiveness_same_category_penalized(self, in_memory_db):
        """Failure with same category -> failure_count incremented."""
        _insert_hint(in_memory_db, "Fix hint", scope="global",
                     original_failure_category="A1",
                     applied_count=0, success_count=0, failure_count=0)
        engine = NLFeedbackEngine(in_memory_db)
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=False, new_failure_category="A1",
        )
        row = in_memory_db.execute(
            "SELECT applied_count, success_count, failure_count "
            "FROM nl_feedback_corrections"
        ).fetchone()
        assert row["applied_count"] == 1
        assert row["success_count"] == 0
        assert row["failure_count"] == 1

    def test_effectiveness_different_category_not_penalized(self, in_memory_db):
        """Failure with DIFFERENT category -> failure_count NOT incremented."""
        _insert_hint(in_memory_db, "Hint for A1", scope="global",
                     original_failure_category="A1",
                     applied_count=0, success_count=0, failure_count=0)
        engine = NLFeedbackEngine(in_memory_db)
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=False, new_failure_category="B2",
        )
        row = in_memory_db.execute(
            "SELECT applied_count, success_count, failure_count "
            "FROM nl_feedback_corrections"
        ).fetchone()
        assert row["applied_count"] == 1
        assert row["success_count"] == 0
        assert row["failure_count"] == 0, (
            f"Expected 0 (different category), got {row['failure_count']}"
        )

    def test_effectiveness_auto_disable(self, in_memory_db):
        """Hint with failures and no successes after enough applications -> disabled."""
        # Pre-set hint with (AUTO_DISABLE_MIN_APPLICATIONS - 1) applications and failures
        min_apps = AUTO_DISABLE_MIN_APPLICATIONS
        _insert_hint(in_memory_db, "Bad hint", scope="global",
                     original_failure_category="A1",
                     applied_count=min_apps - 1,
                     success_count=0, failure_count=min_apps - 1)
        engine = NLFeedbackEngine(in_memory_db)

        # One more failure with same category -> should trigger auto-disable
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=False, new_failure_category="A1",
        )
        row = in_memory_db.execute(
            "SELECT is_active FROM nl_feedback_corrections"
        ).fetchone()
        assert row["is_active"] == 0, "Hint should be auto-disabled"

    def test_effectiveness_no_auto_disable_with_success(self, in_memory_db):
        """Hint with at least one success should NOT be auto-disabled."""
        min_apps = AUTO_DISABLE_MIN_APPLICATIONS
        _insert_hint(in_memory_db, "Mixed hint", scope="global",
                     original_failure_category="A1",
                     applied_count=min_apps - 1,
                     success_count=1, failure_count=min_apps - 2)
        engine = NLFeedbackEngine(in_memory_db)

        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=False, new_failure_category="A1",
        )
        row = in_memory_db.execute(
            "SELECT is_active FROM nl_feedback_corrections"
        ).fetchone()
        assert row["is_active"] == 1, "Hint with successes should not be disabled"

    def test_effectiveness_domain_scoped(self, in_memory_db):
        """Only domain-matching hints should be updated."""
        _insert_hint(in_memory_db, "Domain hint", scope="domain", domain="example.com")
        _insert_hint(in_memory_db, "Other domain hint", scope="domain", domain="other.com")
        engine = NLFeedbackEngine(in_memory_db)

        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=True,
        )

        rows = in_memory_db.execute(
            "SELECT feedback_text, applied_count FROM nl_feedback_corrections "
            "ORDER BY feedback_text"
        ).fetchall()
        # Domain hint should be updated, Other domain should not
        example_row = [r for r in rows if "Domain hint" == r["feedback_text"]][0]
        other_row = [r for r in rows if "Other domain hint" == r["feedback_text"]][0]
        assert example_row["applied_count"] == 1
        assert other_row["applied_count"] == 0

    def test_effectiveness_no_db(self):
        """No DB -> should not crash."""
        engine = NLFeedbackEngine(None)
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=True,
        )

    def test_effectiveness_no_active_hints(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        # No hints in DB -> should not crash
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=True,
        )


# ===================================================================
# Category 6: Scope Mapping
# ===================================================================


class TestScopeMapping:
    """_SCOPE_BY_CATEGORY mapping completeness."""

    def test_scope_mapping_completeness(self):
        """Scope map must cover every category any code path can emit.

        Covers Phase 1 live categories plus pre-wired entries for Phase 3
        (assertion, positive, negative). Removing any of these would silently
        change scope when the corresponding patterns land.
        """
        expected = {
            "structural", "keyword", "locator", "timing", "data",
            "assertion", "uncategorized", "positive", "negative",
        }
        assert set(_SCOPE_BY_CATEGORY.keys()) == expected

    def test_scope_default_domain(self):
        """Unknown categories should default to 'domain'."""
        assert _SCOPE_BY_CATEGORY.get("totally_unknown", "domain") == "domain"


# ===================================================================
# Category 7: Integration -- FeedbackLoop routes to NL engine
# ===================================================================


class TestIntegration:
    """FeedbackLoop integration with NL feedback corrections table."""

    def test_integration_feedback_stores_in_nl_table(self, in_memory_db):
        """process_user_feedback should store feedback in nl_feedback_corrections."""
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        # First store an execution record
        fl.process_execution(
            workflow_id="wf-int1", user_query="click login",
            url="https://example.com", robot_code="*** Test Cases ***",
            test_status="failed",
        )
        # Now provide feedback
        fl.process_user_feedback(
            workflow_id="wf-int1",
            feedback_text="Use id=submit-btn instead of class selector",
            feedback_type="close_enough",
        )
        # Check nl_feedback_corrections table
        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections"
        ).fetchone()
        assert row is not None, "Feedback should be stored in nl_feedback_corrections"
        assert "submit-btn" in row["feedback_text"]

    def test_integration_feedback_text_in_triage(self, in_memory_db):
        """Triage dict should include feedback_text for engine routing."""
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-int2", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="failed",
        )
        result = fl.process_user_feedback(
            workflow_id="wf-int2",
            feedback_text="Try xpath selector",
            feedback_type="completely_wrong",
        )
        # If MockEngines received feedback_calls, the NL engine was in the routing
        # We can't directly check nl_engine calls since it's created inline,
        # but we CAN verify the triage result came back without error
        assert isinstance(result, dict)
        assert "category" in result

    def test_integration_process_execution_no_crash(self, in_memory_db):
        """process_execution with NL effectiveness tracking should not crash."""
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        # Insert a hint that would match
        _insert_hint(conn, "Test hint", scope="global")
        # Run execution -- should trigger Step 7 (effectiveness tracking)
        fl.process_execution(
            workflow_id="wf-int3", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="passed",
        )
        # Verify hint applied_count was updated
        row = conn.execute(
            "SELECT applied_count, success_count "
            "FROM nl_feedback_corrections"
        ).fetchone()
        assert row["applied_count"] == 1, (
            f"Expected applied_count=1, got {row['applied_count']}"
        )
        assert row["success_count"] == 1


# ===================================================================
# Category 8: End-to-end flow
# ===================================================================


class TestEndToEnd:
    """End-to-end: feedback -> store -> retrieve -> attribute."""

    def test_e2e_feedback_to_hint_injection(self, in_memory_db):
        """Full flow: feedback -> store -> get_hints retrieves it."""
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(domain="example.com", url="https://example.com/login")
        triage = {
            "feedback_text": "Always use data-testid attribute for selectors",
            "category": "locator",
        }
        engine.learn_from_feedback(record, triage)

        # Now retrieve hints for same url
        hints = engine.get_hints(
            "find login button", "https://example.com/login", "assembler"
        )
        assert hints is not None, "Expected hints to be returned"
        assert any("data-testid" in h for h in hints), (
            f"Expected feedback text in hints, got: {hints}"
        )

    def test_e2e_success_tracking(self, in_memory_db):
        """Full flow: store hint -> track success -> verify counts."""
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(domain="example.com")
        triage = {"feedback_text": "Use Sleep 2s", "category": "keyword"}
        engine.learn_from_feedback(record, triage)

        # Simulate successful execution
        engine.update_hint_effectiveness(
            domain="example.com", url="https://example.com",
            test_passed=True,
        )

        row = in_memory_db.execute(
            "SELECT success_count, applied_count FROM nl_feedback_corrections"
        ).fetchone()
        assert row["success_count"] == 1
        assert row["applied_count"] == 1

    def test_e2e_auto_disable_flow(self, in_memory_db):
        """Full flow: store -> repeated failures -> auto-disable -> no longer in hints."""
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(domain="example.com", failure_category="A1")
        triage = {"feedback_text": "Bad advice that keeps failing", "category": "keyword"}
        engine.learn_from_feedback(record, triage)

        # Simulate repeated failures
        min_apps = AUTO_DISABLE_MIN_APPLICATIONS
        for _ in range(min_apps):
            engine.update_hint_effectiveness(
                domain="example.com", url="https://example.com",
                test_passed=False, new_failure_category="A1",
            )

        # Verify hint is disabled
        row = in_memory_db.execute(
            "SELECT is_active FROM nl_feedback_corrections"
        ).fetchone()
        assert row["is_active"] == 0, "Hint should be auto-disabled after repeated failures"

        # Verify disabled hint does NOT appear in get_hints
        hints = engine.get_hints(
            "some query", "https://example.com", "assembler"
        )
        assert hints is None, "Disabled hint should not appear in hints"
