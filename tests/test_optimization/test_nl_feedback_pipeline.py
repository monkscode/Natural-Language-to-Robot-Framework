"""
NL Feedback Pipeline Verification -- Storage, Retrieval, Attribution, Integration (pytest format).

Migrated from scripts/verify_nl_feedback_pipeline.py.

Tests: NLFeedbackEngine (learn_from_feedback, get_hints, update_hint_effectiveness,
       _deduplicate_hints), SmartKeywordProvider NL wiring, FeedbackLoop routing.

Uses in-memory SQLite with full Phase 1 schema via the ``in_memory_db`` fixture.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest

from tests.test_optimization import pg_introspect
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
    ExecutionRecord,
)


# ===================================================================
# Test Helpers (kept from original script)
# ===================================================================


class SynchronousWriteQueue:
    """Drop-in replacement for LearningWriteQueue -- executes immediately."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def submit_and_wait(self, fn, *args, timeout=None, **kwargs):
        fn(*args, **kwargs)
        return ("ok", None)


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


_ORG = "org-A"


@dataclass
class FakeRecord:
    """Minimal ExecutionRecord-like object for unit tests."""
    workflow_id: str = "wf-test"
    domain: Optional[str] = "example.com"
    url: Optional[str] = "https://example.com/login"
    failure_category: Optional[str] = None
    user_query: str = "click login button"
    test_status: str = "failed"
    # T9: hint writes and reads fail closed without an org, so the
    # default record names one. Tenancy itself is covered by
    # test_nl_correction_org.py and test_org_filter_fails_closed.py.
    org_id: Optional[str] = _ORG


def create_execution_memory(conn):
    """Return the execution store wrapped by the in_memory_db fixture.

    conn is the _EngineCompatConn from the in_memory_db fixture; the real
    PostgresExecutionMemory it wraps is returned so read_conn() works correctly.
    """
    return conn._em


def _insert_hint(conn, text, scope="global", domain=None, url=None,
                 category="keyword", is_active=1, success_count=0,
                 evidence_count=1, failure_count=0, applied_count=0,
                 original_failure_category=None, conflict_flagged=0,
                 org_id=_ORG):
    """Insert a hint row directly into nl_feedback_corrections."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, url, "
        " original_failure_category, evidence_count, applied_count, "
        " success_count, failure_count, is_active, conflict_flagged, "
        " created_at, last_seen, org_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (text, category, scope, domain, url,
         original_failure_category, evidence_count, applied_count,
         success_count, failure_count, is_active, conflict_flagged, now, now,
         org_id),
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
        assert "nl_feedback_corrections" in pg_introspect.table_names(in_memory_db), (
            "nl_feedback_corrections table not found"
        )

    def test_columns(self, in_memory_db):
        col_names = pg_introspect.column_names(in_memory_db, "nl_feedback_corrections")
        expected = {
            "id", "feedback_text", "category", "scope", "domain", "url",
            "original_failure_category", "source_workflow_id",
            "evidence_count", "applied_count", "success_count", "failure_count",
            "is_active", "created_at", "last_seen",
        }
        missing = expected - col_names
        assert not missing, f"Missing columns: {missing}"

    def test_index_exists(self, in_memory_db):
        idx_names = pg_introspect.index_names(in_memory_db, "nl_feedback_corrections")
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
        """Same feedback_text + domain + scope -> evidence_count incremented.

        One submission per run: T5 counts a correction once for the run that
        produced it, so the three submissions here come from three runs — which
        is what evidence_count was always meant to measure.
        """
        engine = NLFeedbackEngine(in_memory_db)
        triage = {"feedback_text": "Use id=login-btn", "category": "locator"}

        engine.learn_from_feedback(FakeRecord(workflow_id="wf-dedup-1"), triage)
        engine.learn_from_feedback(FakeRecord(workflow_id="wf-dedup-2"), triage)
        engine.learn_from_feedback(FakeRecord(workflow_id="wf-dedup-3"), triage)

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
        hints = engine.get_hints("click button", "https://example.com", "assembler", org_id=_ORG)
        assert hints is not None, "Expected hints, got None"
        assert any("Always wait before click" in h for h in hints)

    def test_hints_domain_match(self, in_memory_db):
        _insert_hint(in_memory_db, "Use id=login-btn", scope="domain", domain="example.com")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click login", "https://example.com/page", "assembler", org_id=_ORG)
        assert hints is not None
        assert any("login-btn" in h for h in hints)

    def test_hints_domain_mismatch(self, in_memory_db):
        _insert_hint(in_memory_db, "Use other selector", scope="domain", domain="other.com")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click login", "https://example.com/page", "assembler", org_id=_ORG)
        assert hints is None, "Should not match different domain"

    def test_hints_url_match(self, in_memory_db):
        _insert_hint(in_memory_db, "Click exact button",
                     scope="url", url="https://example.com/login")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints(
            "click login", "https://example.com/login", "assembler"
        , org_id=_ORG)
        assert hints is not None
        assert any("Click exact button" in h for h in hints)

    def test_hints_inactive_excluded(self, in_memory_db):
        _insert_hint(in_memory_db, "Disabled hint", scope="global", is_active=0)
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click button", "https://example.com", "assembler", org_id=_ORG)
        assert hints is None, "Inactive hints should not be returned"

    def test_hints_empty_db(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click button", "https://example.com", "assembler", org_id=_ORG)
        assert hints is None

    def test_hints_no_db(self):
        engine = NLFeedbackEngine(None)
        hints = engine.get_hints("click button", "https://example.com", "assembler", org_id=_ORG)
        assert hints is None

    def test_hints_max_5(self, in_memory_db):
        for i in range(10):
            _insert_hint(in_memory_db, f"Hint number {i}", scope="global")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click", "https://example.com", "assembler", org_id=_ORG)
        assert hints is not None
        assert len(hints) <= 5, f"Expected max 5 hints, got {len(hints)}"

    def test_hints_ordered_by_success(self, in_memory_db):
        _insert_hint(in_memory_db, "Low success", scope="global", success_count=1)
        _insert_hint(in_memory_db, "High success", scope="global", success_count=10)
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click", "https://x.com", "assembler", org_id=_ORG)
        assert hints is not None
        # High success should come first
        assert "High success" in hints[0]

    def test_hints_formatted_with_prefix(self, in_memory_db):
        _insert_hint(in_memory_db, "Test prefix", scope="global")
        engine = NLFeedbackEngine(in_memory_db)
        hints = engine.get_hints("click", "https://x.com", "assembler", org_id=_ORG)
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
# Category 5: usage attribution — see test_apply_hint_attribution.py
# ===================================================================
#
# The all-injected update_hint_effectiveness was replaced by
# apply_hint_attribution (used/failure/unused buckets, atomic once-guard,
# FR2/G1 disable-retire). Its tests live in test_apply_hint_attribution.py.


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
            test_status="failed", org_id=_ORG,
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
        fl.process_execution(
            workflow_id="wf-int3", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="passed",
        )
        # process_execution no longer credits NL hints — usage attribution moved
        # to workflow_service._process_learning_record / apply_hint_attribution. It must
        # still complete and store the execution record without crashing.
        rec = conn.execute(
            "SELECT workflow_id FROM execution_records WHERE workflow_id = 'wf-int3'"
        ).fetchone()
        assert rec is not None


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
        , org_id=_ORG)
        assert hints is not None, "Expected hints to be returned"
        assert any("data-testid" in h for h in hints), (
            f"Expected feedback text in hints, got: {hints}"
        )

    def test_e2e_success_tracking(self, in_memory_db):
        """Full flow: store hint -> attribute a used pass -> verify counts."""
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(domain="example.com")
        triage = {"feedback_text": "Use Sleep 2s", "category": "keyword"}
        engine.learn_from_feedback(record, triage)
        hint_id = in_memory_db.execute(
            "SELECT id FROM nl_feedback_corrections"
        ).fetchone()["id"]
        in_memory_db.execute(
            "INSERT INTO execution_records (workflow_id, timestamp, user_query, "
            "test_status, hint_attribution_done) "
            "VALUES ('wf-e2e-ok', '2026-01-01T00:00:00+00:00', 'q', 'passed', 0)"
        )
        in_memory_db.commit()

        engine.apply_hint_attribution("wf-e2e-ok", [hint_id], [], [])

        row = in_memory_db.execute(
            "SELECT success_count, applied_count FROM nl_feedback_corrections"
        ).fetchone()
        assert row["success_count"] == 1
        assert row["applied_count"] == 1

    def test_e2e_auto_disable_flow(self, in_memory_db):
        """Full flow: store -> an attributed harmful verdict crosses the
        never-succeeded floor -> auto-disable -> hint no longer returned."""
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(domain="example.com", failure_category="A1")
        triage = {"feedback_text": "Bad advice that keeps failing", "category": "keyword"}
        engine.learn_from_feedback(record, triage)
        hint_id = in_memory_db.execute(
            "SELECT id FROM nl_feedback_corrections"
        ).fetchone()["id"]
        # Pre-seed failures just below the floor, each from its own query; one
        # harmful verdict from a further query crosses it. The floor counts
        # distinct sources (T4), so the evidence rows are part of the state.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections SET failure_count = ? WHERE id = ?",
            (AUTO_DISABLE_MIN_APPLICATIONS - 1, hint_id),
        )
        for i in range(AUTO_DISABLE_MIN_APPLICATIONS - 1):
            in_memory_db.execute(
                "INSERT INTO hint_evidence (hint_id, source_kind, source_key, "
                " source_hash, bucket, created_at) "
                "VALUES (?, 'query', ?, ?, 'failure', '2026-01-01T00:00:00+00:00')",
                (hint_id, f"q{i}", hashlib.sha256(f"q{i}".encode()).hexdigest()),
            )
        in_memory_db.execute(
            "INSERT INTO execution_records (workflow_id, timestamp, user_query, "
            "test_status, hint_attribution_done) "
            "VALUES ('wf-e2e-bad', '2026-01-01T00:00:00+00:00', 'q', 'passed', 0)"
        )
        in_memory_db.commit()

        engine.apply_hint_attribution(
            "wf-e2e-bad", [], [hint_id], [], reasons={hint_id: "harmful"},
        )

        row = in_memory_db.execute(
            "SELECT is_active FROM nl_feedback_corrections"
        ).fetchone()
        assert row["is_active"] == 0, "Hint should be auto-disabled"

        # Disabled hint must NOT appear in get_hints.
        hints = engine.get_hints("some query", "https://example.com", "assembler", org_id=_ORG)
        assert hints is None, "Disabled hint should not appear in hints"


class TestReactivationResetsUnusedCount:
    """Step 4b: re-submitting identical feedback (the UPSERT branch) resets
    unused_count to 0 — a fresh chance — while preserving the earned
    success/failure track record (otherwise an unused-retired hint would be
    re-retired on the next unused verdict)."""

    def test_resubmission_resets_unused_preserving_track_record(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        record = FakeRecord(workflow_id="wf-orig", domain="x.com", url="https://x.com")
        triage = {"feedback_text": "always wait for the element to be visible",
                  "category": "keyword"}
        engine.learn_from_feedback(record, triage)  # create the hint
        hid = in_memory_db.execute(
            "SELECT id FROM nl_feedback_corrections"
        ).fetchone()["id"]
        # Accrue an earned record + over-surfaced dead weight.
        in_memory_db.execute(
            "UPDATE nl_feedback_corrections "
            "SET success_count=3, failure_count=1, unused_count=7 WHERE id=?",
            (hid,),
        )
        in_memory_db.commit()

        # Re-submit identical feedback from a LATER run -> UPSERT branch (the
        # fresh chance). A different workflow_id on purpose: T5 counts one
        # correction per run, and re-affirming a hint from a new run is exactly
        # the recovery path that gate leaves open.
        engine.learn_from_feedback(
            FakeRecord(workflow_id="wf-resubmit", domain="x.com", url="https://x.com"),
            triage,
        )

        row = in_memory_db.execute(
            "SELECT unused_count, success_count, failure_count, is_active "
            "FROM nl_feedback_corrections WHERE id=?",
            (hid,),
        ).fetchone()
        assert row["unused_count"] == 0    # reset
        assert row["success_count"] == 3   # preserved
        assert row["failure_count"] == 1   # preserved
        assert row["is_active"] == 1
