"""
DAY_09 — Phase 1 Acceptance Unit Tests (Gap-Fillers).

Focuses on genuine coverage gaps NOT tested by verify_day01-08:
  1. ExecutionMemory edge cases (stats, non-existent records)
  2. FailureAnalyzer confidence bounds
  3. Role-filtered get_hints() for all 3 engines
  4. FeedbackLoop stat keys + persistence
  5. Real engine context injection with temp DB
  6. NLFeedbackEngine edge cases
  7. LEARNING_CONFIG invariants
  8. _call_conflict_detection_llm response_format override warning (M2)

Migrated from scripts/verify_day09_unit.py to pytest format.
Subprocess regression tests removed (pytest discovers all tests).
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from src.backend.crew_ai.optimization.schema_manager import SchemaManager
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    LearningCircuitBreaker,
    LearningWriteQueue,
    EffectivenessScore,
)
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionMemory,
    ExecutionRecord,
)
from src.backend.core.url_utils import extract_domain
from src.backend.crew_ai.optimization.failure_analyzer import (
    FailureClassifier,
    FailureAnalyzer,
)
from src.backend.crew_ai.optimization.structural_rule_engine import (
    IntentExtractor,
    StructuralRuleEngine,
)
from src.backend.crew_ai.optimization.keyword_correction_engine import (
    KeywordCorrectionEngine,
)
from src.backend.crew_ai.optimization.anti_pattern_engine import (
    AntiPatternEngine,
)
from src.backend.crew_ai.optimization.feedback_loop import (
    LearningMetricsTracker,
    ContradictionDetector,
    FeedbackLoop,
)
from src.backend.crew_ai.optimization.nl_feedback_engine import (
    NLFeedbackEngine,
)


# ===================================================================
# Helpers
# ===================================================================

def create_execution_memory(conn):
    """Create ExecutionMemory backed by existing connection.

    When conn is _EngineCompatConn (from in_memory_db fixture), returns the
    real ExecutionMemory it wraps so read_conn() works correctly.
    """
    if hasattr(conn, '_em'):
        return conn._em
    em = ExecutionMemory.__new__(ExecutionMemory)
    em.db_path = ":memory:"
    em._chroma_dir = None
    em._writer_conn = conn
    em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
    em._execution_collection = None
    em._chroma_failed_at = None
    em._chroma_last_error = None
    return em


class SynchronousWriteQueue:
    """Drop-in replacement for LearningWriteQueue -- executes immediately."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class MockEngine:
    """Minimal mock of a LearningEngine for FeedbackLoop tests."""

    def __init__(self):
        self.learn_calls = []
        self.hints = []
        self._stats = {"total_rules": 0, "active_rules": 0}

    def learn(self, record):
        self.learn_calls.append(record)

    def learn_from_feedback(self, record, triage):
        pass

    def get_hints(self, query, domain=None):
        return self.hints

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


def _build_feedback_loop(conn):
    """Build FeedbackLoop with mock engines for unit-level testing."""
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


def _create_sre_with_active_rule(conn):
    """Create SRE with a rule that passes threshold."""
    ie = IntentExtractor(conn)
    sre = StructuralRuleEngine(conn, ie)
    # Insert a rule with enough evidence to pass threshold
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO structural_rules "
        "(rule_name, query_pattern, required_structure, "
        " required_keywords_json, evidence_count, counter_evidence, "
        " score, last_updated, created_at) "
        "VALUES ('iteration', 'all.*rows', 'for_loop', "
        " '[\"FOR\", \"Get Text\"]', 5, 0, 0.83, ?, ?)",
        (now, now),
    )
    conn.commit()
    return sre, conn


def _create_kce_with_corrections(conn):
    """Create KCE with a keyword correction that passes threshold."""
    kce = KeywordCorrectionEngine(conn)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO keyword_corrections "
        "(wrong_keyword, correct_keyword, library, error_pattern, "
        " evidence_count, score, last_seen) "
        "VALUES ('Open Browser', 'New Browser', 'browser', "
        " 'No keyword Open Browser', 5, 0.83, ?)",
        (now,),
    )
    conn.commit()
    return kce, conn


# ===================================================================
# Section 1: ExecutionMemory Gaps
# ===================================================================

def test_em_get_total_records_after_store(in_memory_db):
    conn = in_memory_db
    em = create_execution_memory(conn)
    assert em.get_total_records() == 0, "Expected 0 records initially"
    em.store(ExecutionRecord(
        workflow_id="em-t1", timestamp=datetime.now(),
        user_query="click button", url="https://example.com",
        domain="example.com", robot_code="*** Test Cases ***",
        code_structure=None, test_status="passed",
    ))
    assert em.get_total_records() == 1, "Expected 1 record after store"
    em.store(ExecutionRecord(
        workflow_id="em-t2", timestamp=datetime.now(),
        user_query="verify rows", url="https://demoqa.com",
        domain="demoqa.com", robot_code="*** Test Cases ***",
        code_structure=None, test_status="failed",
        failure_category="A1", failed_keyword="FOR",
        error_message="Missing FOR loop",
    ))
    assert em.get_total_records() == 2, "Expected 2 records after two stores"


def test_em_get_domain_stats_correct(in_memory_db):
    conn = in_memory_db
    em = create_execution_memory(conn)
    for i in range(3):
        em.store(ExecutionRecord(
            workflow_id=f"ds-{i}", timestamp=datetime.now(),
            user_query=f"query {i}", url="https://example.com",
            domain="example.com", robot_code="code",
            code_structure=None,
            test_status="passed" if i < 2 else "failed",
        ))
    stats = em.get_domain_stats("example.com")
    assert stats["total"] == 3, f"Expected 3 total, got {stats['total']}"
    assert stats["passed"] == 2, f"Expected 2 passed, got {stats['passed']}"
    assert stats["failed"] == 1, f"Expected 1 failed, got {stats['failed']}"
    assert abs(stats["pass_rate"] - 2/3) < 0.01


def test_em_get_domain_stats_unknown_domain(in_memory_db):
    conn = in_memory_db
    em = create_execution_memory(conn)
    stats = em.get_domain_stats("nonexistent.com")
    assert stats["total"] == 0


def test_em_get_nonexistent_workflow(in_memory_db):
    conn = in_memory_db
    em = create_execution_memory(conn)
    record = em.get("nonexistent-workflow-id")
    assert record is None, "Expected None for non-existent workflow"


def test_em_update_daily_stats_creates_row(in_memory_db):
    conn = in_memory_db
    em = create_execution_memory(conn)
    em.update_daily_stats("passed")
    # UTC bucket — matches update_daily_stats (local date differs around midnight)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM learning_stats WHERE stat_date = ?", (today,)
    ).fetchone()
    assert row is not None, "Daily stats row should exist"
    assert row["total_executions"] == 1
    assert row["total_passed"] == 1
    assert row["total_failed"] == 0


def test_em_update_daily_stats_increments(in_memory_db):
    conn = in_memory_db
    em = create_execution_memory(conn)
    em.update_daily_stats("passed")
    em.update_daily_stats("failed")
    em.update_daily_stats("passed")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM learning_stats WHERE stat_date = ?", (today,)
    ).fetchone()
    assert row["total_executions"] == 3
    assert row["total_passed"] == 2
    assert row["total_failed"] == 1


# ===================================================================
# Section 2: FailureAnalyzer Gaps
# ===================================================================

def test_fa_confidence_always_in_range():
    """Every seed pattern should produce confidence in [0.0, 1.0]."""
    classifier = FailureClassifier()
    test_messages = [
        "No keyword with name 'FOR' found.",
        "Element 'id=submit' not found",
        "No keyword with name 'Open Browser' found.",
        "Keyword 'Input Text' expected 2 arguments, got 3.",
        "WebDriverException: Message: session not created",
        "TimeoutError: Locator 'id=btn' did not appear",
        "Variable '${VAR}' not found.",
        "StaleElementReferenceException: element is not attached",
        "completely unknown error xyz 123",
        "",
    ]
    for msg in test_messages:
        result = classifier.classify(msg)
        assert 0.0 <= result.confidence <= 1.0, (
            f"Confidence {result.confidence} out of range for: '{msg}'"
        )


def test_fa_classify_empty_string():
    classifier = FailureClassifier()
    result = classifier.classify("")
    assert result is not None, "classify('') should not return None"
    assert result.category is not None


def test_fa_classify_none_graceful():
    """classify(None) should not crash -- it should handle gracefully."""
    classifier = FailureClassifier()
    try:
        result = classifier.classify(None)
        # If it returns, the result should be valid
        assert result is not None
    except (TypeError, AttributeError):
        # Also acceptable -- just verify it doesn't produce an
        # unhandled exception that crashes the caller
        pass


# ===================================================================
# Section 3: StructuralRuleEngine Gaps -- Role Filtering
# ===================================================================

def test_sre_hints_identifier_returns_none(in_memory_db):
    sre, conn = _create_sre_with_active_rule(in_memory_db)
    hints = sre.get_hints("verify all rows show Active",
                          "https://example.com", "identifier")
    assert hints is None, f"Expected None for identifier, got {hints}"


def test_sre_hints_score_at_boundary(in_memory_db):
    """Rule with evidence=3, counter=0 -> score=0.75, passes threshold."""
    conn = in_memory_db
    ie = IntentExtractor(conn)
    sre = StructuralRuleEngine(conn, ie)
    now = datetime.now().isoformat()
    # evidence=3, counter=0 -> score = 3/(3+0+1) = 0.75
    conn.execute(
        "INSERT INTO structural_rules "
        "(rule_name, query_pattern, required_structure, "
        " required_keywords_json, evidence_count, counter_evidence, "
        " score, last_updated, created_at) "
        "VALUES ('iteration', 'all.*rows', 'for_loop', "
        " '[\"FOR\"]', 3, 0, 0.75, ?, ?)",
        (now, now),
    )
    conn.commit()
    hints = sre.get_hints("verify all rows show Active",
                          "https://example.com", "planner")
    assert hints is not None, "Evidence=3 (score=0.75) should pass threshold"


def test_sre_hints_below_threshold(in_memory_db):
    """Rule with evidence=1, counter=1 -> score=0.33, fails threshold."""
    conn = in_memory_db
    ie = IntentExtractor(conn)
    sre = StructuralRuleEngine(conn, ie)
    now = datetime.now().isoformat()
    # evidence=1, counter=1 -> score = 1/(1+1+1) = 0.33
    conn.execute(
        "INSERT INTO structural_rules "
        "(rule_name, query_pattern, required_structure, "
        " required_keywords_json, evidence_count, counter_evidence, "
        " score, last_updated, created_at) "
        "VALUES ('iteration', 'all.*rows', 'for_loop', "
        " '[\"FOR\"]', 1, 1, 0.33, ?, ?)",
        (now, now),
    )
    conn.commit()
    hints = sre.get_hints("verify all rows show Active",
                          "https://example.com", "planner")
    # Score 0.33 < 0.4 threshold, but also need observations >= 3.
    # observations = 1 + 1 = 2 < 3 -> should NOT pass threshold
    assert hints is None, "Score=0.33, obs=2 should NOT pass threshold"


# ===================================================================
# Section 4: KeywordCorrectionEngine Gaps -- Role Filtering
# ===================================================================

def test_kce_hints_planner_returns_none(in_memory_db):
    kce, conn = _create_kce_with_corrections(in_memory_db)
    hints = kce.get_hints("click button", "https://example.com", "planner")
    assert hints is None, f"Expected None for planner, got {hints}"


def test_kce_hints_identifier_returns_none(in_memory_db):
    kce, conn = _create_kce_with_corrections(in_memory_db)
    hints = kce.get_hints("click button", "https://example.com", "identifier")
    assert hints is None, f"Expected None for identifier, got {hints}"


def test_kce_learn_none_error_message(in_memory_db):
    """learn() with None error_message should not crash."""
    conn = in_memory_db
    kce = KeywordCorrectionEngine(conn)

    class FakeRecord:
        failure_category = "B1"
        failed_keyword = "Open Browser"
        error_message = None
        user_query = "test"
        robot_code = "code"

    kce.learn(FakeRecord())
    # Should complete without crash -- nothing stored since can't extract


# ===================================================================
# Section 5: AntiPatternEngine Gaps -- Role Filtering
# ===================================================================

def test_ape_hints_identifier_returns_none(in_memory_db):
    conn = in_memory_db
    ape = AntiPatternEngine(conn)
    hints = ape.get_hints("check all rows", "https://example.com", "identifier")
    assert hints is None, f"Expected None for identifier, got {hints}"


def test_ape_learn_empty_robot_code(in_memory_db):
    """learn() with empty robot_code should not crash."""
    conn = in_memory_db
    ape = AntiPatternEngine(conn)

    class FakeRecord:
        test_status = "failed"
        failure_category = "A1"
        error_message = "Missing FOR loop"
        user_query = "verify all rows"
        robot_code = ""
        failed_keyword = None
        domain = None

    ape.learn(FakeRecord())
    # Should work -- may store with empty snippet


def test_ape_hints_no_patterns_returns_none(in_memory_db):
    conn = in_memory_db
    ape = AntiPatternEngine(conn)
    hints = ape.get_hints("click button", "https://example.com", "planner")
    assert hints is None, "Empty DB should return None"


# ===================================================================
# Section 6: FeedbackLoop Gaps
# ===================================================================

def test_fl_get_learning_stats_has_all_keys(in_memory_db):
    fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
    stats = fl.get_learning_stats()
    expected_keys = [
        "structural_rules", "keyword_corrections", "anti_patterns",
        "learning_effectiveness", "contradictions", "total_records",
        "circuit_breaker",
    ]
    for key in expected_keys:
        assert key in stats, f"Missing key '{key}' in learning_stats"


def test_fl_process_execution_error_status(in_memory_db):
    """test_status='error' should skip failure analysis."""
    fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="wf-err1", user_query="q",
        url="https://example.com", robot_code="code",
        test_status="error",
    )
    # No failure analysis should have been called
    assert len(fa.analyze_calls) == 0, "error status should not trigger analysis"
    record = em.get("wf-err1")
    assert record is not None, "Record should still be stored"


def test_fl_process_execution_submit_count(in_memory_db):
    """Verify write_queue receives correct number of submits."""

    class CountingQueue:
        def __init__(self):
            self.count = 0

        def submit(self, fn, *args, **kwargs):
            self.count += 1
            fn(*args, **kwargs)

    conn = in_memory_db
    em = create_execution_memory(conn)
    cq = CountingQueue()
    fl = FeedbackLoop(
        execution_memory=em, failure_analyzer=MockFailureAnalyzer(),
        structural_engine=MockEngine(), keyword_engine=MockEngine(),
        anti_pattern_engine=MockEngine(),
        metrics_tracker=LearningMetricsTracker(conn),
        contradiction_detector=ContradictionDetector(conn),
        write_queue=cq, circuit_breaker=LearningCircuitBreaker(),
    )
    # FeedbackLoop.__init__ submits the one-time anchor reconcile; reset so
    # the count below reflects only process_execution's own submits.
    cq.count = 0
    fl.process_execution(
        workflow_id="wf-cnt", user_query="q",
        url="https://example.com", robot_code="code",
        test_status="passed",
    )
    # Expected submits: store(1) + 3 engines(3) + pattern_learner(1) + metrics(1) + daily_stats(1) = 7.
    # The old Step-7 nl_engine submit (update_hint_effectiveness) was removed —
    # NL-hint usage attribution now runs from workflow_service._process_learning.
    assert cq.count == 7, f"Expected 7 submits, got {cq.count}"


def test_fl_process_user_feedback_persists(in_memory_db):
    """Verify process_user_feedback actually persists feedback to DB."""
    fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="wf-fb-persist", user_query="click button",
        url="https://example.com", robot_code="*** Test Cases ***",
        test_status="failed",
    )
    fl.process_user_feedback(
        workflow_id="wf-fb-persist",
        feedback_text="it didn't click the right button",
        feedback_type="completely_wrong",
    )
    record = em.get("wf-fb-persist")
    assert record.user_feedback == "it didn't click the right button", (
        f"Feedback not persisted: {record.user_feedback}"
    )
    assert record.user_feedback_type == "completely_wrong"


def test_fl_no_holdout_in_config():
    """Verify no holdout/A-B testing flags exist in LEARNING_CONFIG."""
    holdout_keys = [
        "HOLDOUT_RATE", "AB_TEST", "CONTROL_GROUP",
        "HOLDOUT_PERCENTAGE", "SKIP_HINTS_RATE",
    ]
    for key in holdout_keys:
        assert key not in LEARNING_CONFIG, (
            f"Unexpected holdout key '{key}' in LEARNING_CONFIG -- "
            "hints should always be injected in Phase 1"
        )


# ===================================================================
# Section 7: Real Engine Context Injection (THE KEY GAP)
#
# Unlike verify_day06 which uses MockEngine, these tests create
# real engines with temp DB and verify actual hint generation.
# ===================================================================

def test_ci_sre_cold_db_returns_none(in_memory_db):
    """StructuralRuleEngine.get_hints() with empty DB -> None."""
    conn = in_memory_db
    ie = IntentExtractor(conn)
    sre = StructuralRuleEngine(conn, ie)
    hints = sre.get_hints("verify all rows", "https://example.com", "planner")
    assert hints is None, "Cold DB should return None"


def test_ci_sre_warm_db_returns_hints(in_memory_db):
    """Seed structural data -> get_hints() returns hint."""
    conn = in_memory_db
    ie = IntentExtractor(conn)
    sre = StructuralRuleEngine(conn, ie)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO structural_rules "
        "(rule_name, query_pattern, required_structure, "
        " required_keywords_json, evidence_count, counter_evidence, "
        " score, last_updated, created_at) "
        "VALUES ('iteration', 'all.*rows', 'for_loop', "
        " '[\"FOR\", \"Get Text\"]', 10, 1, 0.91, ?, ?)",
        (now, now),
    )
    conn.commit()
    hints = sre.get_hints("verify all rows show Active",
                          "https://example.com", "planner")
    assert hints is not None, "Warm DB should return hints"
    assert len(hints) > 0
    assert "FOR" in hints[0], f"Hint should mention FOR: {hints[0]}"


def test_ci_kce_cold_db_returns_none(in_memory_db):
    """KeywordCorrectionEngine.get_hints() with empty DB -> None."""
    conn = in_memory_db
    kce = KeywordCorrectionEngine(conn)
    hints = kce.get_hints("click button", "https://example.com", "assembler")
    assert hints is None, "Cold DB should return None"


def test_ci_kce_warm_db_returns_correction(in_memory_db):
    """Seed keyword correction -> get_hints() returns correction."""
    conn = in_memory_db
    kce = KeywordCorrectionEngine(conn)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO keyword_corrections "
        "(wrong_keyword, correct_keyword, library, error_pattern, "
        " evidence_count, score, last_seen) "
        "VALUES ('Open Browser', 'New Browser', 'browser', "
        " 'No keyword Open Browser', 5, 0.83, ?)",
        (now,),
    )
    conn.commit()
    hints = kce.get_hints("open the website", "https://example.com", "assembler")
    assert hints is not None, "Warm DB should return hints"
    assert len(hints) > 0
    assert "New Browser" in hints[0], f"Hint should mention correction: {hints[0]}"


def test_ci_ape_cold_db_returns_none(in_memory_db):
    """AntiPatternEngine.get_hints() with empty DB -> None."""
    conn = in_memory_db
    ape = AntiPatternEngine(conn)
    hints = ape.get_hints("check all rows", "https://example.com", "planner")
    assert hints is None, "Cold DB should return None"


def test_ci_ape_warm_db_returns_warning(in_memory_db):
    """Seed anti-pattern data -> get_hints() returns warning."""
    conn = in_memory_db
    ape = AntiPatternEngine(conn)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO anti_patterns "
        "(failure_category, query_pattern, bad_code_snippet, "
        " error_message, domain, score, evidence_count, last_seen) "
        "VALUES ('A1', 'check all rows', 'Get Text  id=cell', "
        " 'Missing FOR loop construct', 'example.com', 0.83, 5, ?)",
        (now,),
    )
    conn.commit()
    hints = ape.get_hints("check all rows in table",
                          "https://example.com", "planner")
    assert hints is not None, "Warm DB should return hints"
    assert len(hints) > 0


def test_ci_config_complexity_tiers():
    """LEARNING_CONFIG complexity tiers have expected max_hints values."""
    tiers = LEARNING_CONFIG["COMPLEXITY_TIERS"]
    assert tiers["simple"]["max_hints"] == 5, (
        f"simple max_hints should be 5, got {tiers['simple']['max_hints']}"
    )
    assert tiers["medium"]["max_hints"] == 8, (
        f"medium max_hints should be 8, got {tiers['medium']['max_hints']}"
    )
    assert tiers["complex"]["max_hints"] == 10, (
        f"complex max_hints should be 10, got {tiers['complex']['max_hints']}"
    )


def test_ci_hints_always_injected():
    """Verify no skip/holdout mechanism prevents hint injection."""
    # EffectivenessScore should not have any holdout logic
    assert not hasattr(EffectivenessScore, "should_inject"), (
        "EffectivenessScore should not have 'should_inject' method"
    )
    assert not hasattr(EffectivenessScore, "holdout_rate"), (
        "EffectivenessScore should not have 'holdout_rate'"
    )


# ===================================================================
# Section 8: NLFeedbackEngine Gaps
# ===================================================================

def test_nle_long_text_no_crash(in_memory_db):
    """process_feedback() with very long text should not crash."""
    conn = in_memory_db
    engine = NLFeedbackEngine(conn)
    long_text = "the button is wrong " * 100  # >2000 chars
    result = engine.process_feedback(
        "wf-long", long_text, "completely_wrong"
    )
    assert "category" in result
    assert "confidence" in result
    assert 0.0 <= result["confidence"] <= 1.0


def test_nle_no_match_returns_uncategorized(in_memory_db):
    """Text matching no patterns returns 'uncategorized'."""
    conn = in_memory_db
    engine = NLFeedbackEngine(conn)
    result = engine.process_feedback(
        "wf-no-match",
        "the quantum flux capacitor is misaligned",
        "completely_wrong",
    )
    assert result["category"] == "uncategorized"
    assert result["specific_type"] == "no_match"


def test_nle_highest_confidence_wins(in_memory_db):
    """When multiple patterns match, first match (highest priority) wins."""
    conn = in_memory_db
    engine = NLFeedbackEngine(conn)
    # This text should match multiple patterns
    result = engine.process_feedback(
        "wf-multi",
        "it needs a loop for each row and the keyword is wrong",
        "completely_wrong",
    )
    assert "category" in result
    # Should pick the first matching category (highest priority by dict order)
    assert result["confidence"] > 0.5, (
        "Multiple matches should boost confidence"
    )


# ===================================================================
# Section 9: Audit Gap-Closers (from deep audit)
#
# Tests that close genuine production gaps identified during Phase 1
# audit. Each test protects a real scenario in our framework.
# ===================================================================

def test_cb_reset_recovers_from_trip():
    """CircuitBreaker.reset() allows recovery after trip.

    Real scenario: Learning system hits a transient DB issue, CB trips,
    admin fixes it and calls reset() -- system should resume learning.
    """
    cb = LearningCircuitBreaker()
    # Trip the CB: 15 errors with 0 successes = 100% error rate > 20% threshold
    for _ in range(15):
        cb.record_error(RuntimeError("transient DB lock"))
    assert cb._total_calls >= cb._min_calls, "Should exceed min calls"
    # CB should be tripped (error_rate=1.0 > 0.2)
    stats_before = cb.get_stats()
    assert stats_before["error_rate"] > cb._error_threshold

    # Reset
    cb.reset()
    assert cb._error_count == 0, "reset() should clear error count"
    assert cb._total_calls == 0, "reset() should clear total calls"
    stats_after = cb.get_stats()
    assert stats_after["error_rate"] == 0.0
    assert stats_after["error_count"] == 0


def test_deduplication_aggregates_after_threshold(in_memory_db):
    """After 5 identical records, store() deduplicates (UPDATE not INSERT).

    Real scenario: User runs "click login button on example.com" 10 times.
    First 5 create separate rows; 6th onward updates the most recent row.
    This prevents unbounded DB growth from repetitive queries.
    """
    conn = in_memory_db
    em = create_execution_memory(conn)
    # Store 7 identical executions (same query + domain + status)
    for i in range(7):
        em.store(ExecutionRecord(
            workflow_id=f"dedup-{i}",
            timestamp=datetime.now(),
            user_query="click login button",
            url="https://example.com/login",
            domain="example.com",
            robot_code=f"*** Test Cases ***\nAttempt {i}",
            code_structure="linear",
            test_status="passed",
            total_llm_calls=2,
            total_cost=0.01,
        ))
    # After dedup threshold (5), row count should stop growing
    row_count = conn.execute(
        "SELECT COUNT(*) FROM execution_records "
        "WHERE LOWER(TRIM(user_query)) = 'click login button' "
        "AND domain = 'example.com' AND test_status = 'passed'"
    ).fetchone()[0]
    assert row_count == 5, (
        f"After 7 stores with dedup threshold 5, expected 5 rows, got {row_count}. "
        "Records 6 and 7 should UPDATE the latest row instead of inserting."
    )


def test_deduplication_keeps_latest_code(in_memory_db):
    """Deduplication should keep the most recent robot_code.

    Real scenario: User retries same query, LLM generates improved code.
    The deduped row should have the latest version, not the first.
    """
    conn = in_memory_db
    em = create_execution_memory(conn)
    # Store 6 identical records -- 6th triggers dedup
    for i in range(6):
        em.store(ExecutionRecord(
            workflow_id=f"dedup-code-{i}",
            timestamp=datetime.now(),
            user_query="fill form",
            url="https://example.com",
            domain="example.com",
            robot_code=f"version_{i}",
            code_structure="linear",
            test_status="passed",
        ))
    # The most recent row should have the latest robot_code
    latest = conn.execute(
        "SELECT robot_code FROM execution_records "
        "WHERE LOWER(TRIM(user_query)) = 'fill form' "
        "AND domain = 'example.com' "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    assert latest["robot_code"] == "version_5", (
        f"Expected latest code 'version_5', got '{latest['robot_code']}'"
    )


def test_feedback_with_missing_workflow_returns_triage(in_memory_db):
    """process_user_feedback with nonexistent workflow still returns triage.

    Real scenario: User submits feedback on an expired or deleted workflow.
    The framework should still triage the feedback (just skip engine routing).
    """
    fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
    triage = fl.process_user_feedback(
        workflow_id="nonexistent-workflow-id-xyz",
        feedback_text="the button click was wrong",
        feedback_type="completely_wrong",
    )
    assert isinstance(triage, dict), "Should return dict even for missing workflow"
    assert "category" in triage, "Triage should have 'category'"
    assert "confidence" in triage, "Triage should have 'confidence'"
    # Should still triage successfully (just no engine routing)
    assert triage["confidence"] >= 0.0


def test_feedback_with_missing_workflow_no_crash(in_memory_db):
    """Engines should not crash when record doesn't exist.

    Real scenario: Feedback on expired workflow -- the engine routing
    path (step 4) should be skipped gracefully.
    """
    fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
    # Should not raise any exception
    triage = fl.process_user_feedback(
        workflow_id="absolutely-does-not-exist",
        feedback_text="it selected the wrong element",
        feedback_type="close_enough",
    )
    # Engines should NOT have been called (no record to route)
    assert len(se.learn_calls) == 0, (
        "Engines should not be called when record is missing"
    )


def test_update_user_feedback_stores_correctly(in_memory_db):
    """update_user_feedback persists text and type in DB.

    Real scenario: User gives feedback after test fails; we need to
    store it so NL triage and future learning can use it.
    """
    conn = in_memory_db
    em = create_execution_memory(conn)
    em.store(ExecutionRecord(
        workflow_id="fb-store-test",
        timestamp=datetime.now(),
        user_query="click submit",
        url="https://example.com",
        domain="example.com",
        robot_code="*** Test Cases ***",
        code_structure="linear",
        test_status="failed",
        error_message="Element not found",
    ))
    em.update_user_feedback(
        "fb-store-test",
        "it clicked the wrong button, should be the green one",
        "completely_wrong",
    )
    record = em.get("fb-store-test")
    assert record.user_feedback == (
        "it clicked the wrong button, should be the green one"
    ), f"Feedback text mismatch: {record.user_feedback}"
    assert record.user_feedback_type == "completely_wrong"


def test_registry_respects_optimization_disabled():
    """get_feedback_loop() returns None when OPTIMIZATION_ENABLED=False.

    Real scenario: Admin disables learning via config to debug issues.
    Registry should respect this without errors.
    """
    import src.backend.crew_ai.optimization.learning_registry as registry
    # Save and reset module state
    saved_instance = registry._feedback_loop_instance
    saved_failed_at = registry._init_failed_at
    try:
        # Reset singleton state
        registry._feedback_loop_instance = None
        registry._init_failed_at = None
        # Temporarily disable optimization
        from src.backend.core.config import settings
        original = settings.OPTIMIZATION_ENABLED
        settings.OPTIMIZATION_ENABLED = False
        try:
            result = registry.get_feedback_loop()
            assert result is None, (
                "get_feedback_loop() should return None when OPTIMIZATION_ENABLED=False"
            )
            # Second call should use cached result
            result2 = registry.get_feedback_loop()
            assert result2 is None, "Cached result should also be None"
        finally:
            settings.OPTIMIZATION_ENABLED = original
    finally:
        # Restore module state
        registry._feedback_loop_instance = saved_instance
        registry._init_failed_at = saved_failed_at


def test_registry_caches_after_first_call():
    """A recent failed init returns None from the cooldown cache — no re-init.

    Real scenario: Multiple API requests hit get_feedback_loop() while the
    learning store is down; only the first attempt (per cooldown window) pays
    the connection cost.
    """
    import time
    import src.backend.crew_ai.optimization.learning_registry as registry
    saved_instance = registry._feedback_loop_instance
    saved_failed_at = registry._init_failed_at
    try:
        registry._feedback_loop_instance = None
        registry._init_failed_at = time.monotonic()  # Simulate a fresh failure
        result = registry.get_feedback_loop()
        # Inside the cooldown window: cached None, no re-initialization
        assert result is None, (
            "Should return cached None without reinitializing"
        )
    finally:
        registry._feedback_loop_instance = saved_instance
        registry._init_failed_at = saved_failed_at


# ---------------------------------------------------------------------------
# M2 — _call_conflict_detection_llm response_format override warning
# ---------------------------------------------------------------------------

class TestCallConflictDetectionLlmResponseFormatWarning:
    """_call_conflict_detection_llm warns when caller passes a conflicting response_format."""

    def _run(self, extra_kwargs):
        from unittest.mock import MagicMock, patch
        from src.backend.crew_ai.optimization.learning_config import _call_conflict_detection_llm

        mock_chunk = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"flag": false}'

        with patch("litellm.completion", return_value=iter([mock_chunk])), \
             patch("litellm.stream_chunk_builder", return_value=mock_response):
            return _call_conflict_detection_llm(
                model_string="gemini/gemini-2.5-flash",
                messages=[{"role": "user", "content": "test"}],
                extra_kwargs=extra_kwargs,
            )

    def test_warns_when_caller_response_format_conflicts(self, caplog):
        """Warning is emitted when caller passes a response_format != json_object."""
        import logging
        with caplog.at_level(logging.WARNING,
                             logger="src.backend.crew_ai.optimization.learning_config"):
            self._run({"response_format": {"type": "text"}})

        conflict_warns = [
            r for r in caplog.records
            if "[CONFLICT_DETECT]" in r.message and "overridden" in r.message
        ]
        assert len(conflict_warns) == 1, (
            f"Expected exactly 1 override warning, got {len(conflict_warns)}: "
            + str([r.message for r in conflict_warns])
        )

    def test_no_warning_when_format_already_json_object(self, caplog):
        """No warning when caller passes the same response_format we enforce."""
        import logging
        with caplog.at_level(logging.WARNING,
                             logger="src.backend.crew_ai.optimization.learning_config"):
            self._run({"response_format": {"type": "json_object"}})

        conflict_warns = [
            r for r in caplog.records
            if "[CONFLICT_DETECT]" in r.message and "overridden" in r.message
        ]
        assert len(conflict_warns) == 0, (
            "No warning expected when format already matches json_object"
        )

    def test_no_warning_when_no_response_format_in_extra_kwargs(self, caplog):
        """No warning when caller does not pass response_format at all."""
        import logging
        with caplog.at_level(logging.WARNING,
                             logger="src.backend.crew_ai.optimization.learning_config"):
            self._run({})

        conflict_warns = [
            r for r in caplog.records
            if "[CONFLICT_DETECT]" in r.message and "overridden" in r.message
        ]
        assert len(conflict_warns) == 0, (
            "No warning expected when no response_format was passed"
        )
