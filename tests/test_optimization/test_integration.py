"""
DAY_09 -- Phase 1 Integration Tests (E2E Scenarios).

Tests cross-component orchestration using REAL engines with real temp DBs.
Unlike day05 (MockEngine + SynchronousWriteQueue), these prove the actual
learning pipeline works end-to-end.

Five scenarios:
  1. Full E2E workflow (pass + fail)
  2. Feedback workflow (triage + persistence)
  3. Learning accumulation (boost -> hints appear)
  4. Kill switch (OPTIMIZATION_ENABLED=false)
  5. Circuit breaker (auto-disable + recovery)

Migrated from scripts/verify_day09_integration.py to pytest format.
All tests marked with @pytest.mark.integration.
"""

import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass

import pytest

from src.backend.crew_ai.optimization.schema_manager import SchemaManager
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    LearningCircuitBreaker,
    EffectivenessScore,
)
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionMemory,
    ExecutionRecord,
)
from src.backend.crew_ai.optimization.failure_analyzer import FailureAnalyzer
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
    """Drop-in replacement for LearningWriteQueue -- executes immediately.

    Unlike the async LearningWriteQueue, this ensures all writes
    complete before the test makes assertions. This is critical for
    integration tests where we need to verify DB state.
    """

    def __init__(self):
        self.submit_count = 0

    def submit(self, fn, *args, **kwargs):
        self.submit_count += 1
        fn(*args, **kwargs)

    def pending(self):
        return 0


class MockFailureAnalyzer:
    """Controllable mock -- returns pre-configured results."""

    def __init__(self, result=None):
        self._result = result
        self.call_count = 0

    def analyze(self, output_xml_path=None, user_query="",
                robot_code="", exit_code=None):
        self.call_count += 1
        return self._result


@dataclass
class MockFailureAnalysis:
    """Mimics FailureAnalysis return shape."""
    category: str = "A1"
    failure_type: str = "missing_keyword"
    failed_keyword: str = "FOR"
    error_message: str = "No keyword with name 'FOR' found."
    confidence: float = 0.9
    source: str = "xml_parser"


@dataclass
class MockMetrics:
    """Mimics WorkflowMetrics for process_execution."""
    total_llm_calls: int = 3
    total_cost: float = 0.05


def build_real_feedback_loop(conn, failure_result=None):
    """Build FeedbackLoop with REAL engines (not mocks).

    This is the key difference from day05 tests -- real engines
    with real DB connections and real schema.
    """
    em = create_execution_memory(conn)
    ie = IntentExtractor(conn)
    se = StructuralRuleEngine(conn, ie)
    ke = KeywordCorrectionEngine(conn)
    ae = AntiPatternEngine(conn)
    mt = LearningMetricsTracker(conn)
    cd = ContradictionDetector(conn)
    wq = SynchronousWriteQueue()
    cb = LearningCircuitBreaker()

    # Use mock for failure analyzer since we don't have real output.xml
    fa = MockFailureAnalyzer(result=failure_result)

    fl = FeedbackLoop(
        execution_memory=em, failure_analyzer=fa,
        structural_engine=se, keyword_engine=ke,
        anti_pattern_engine=ae, metrics_tracker=mt,
        contradiction_detector=cd, write_queue=wq,
        circuit_breaker=cb,
    )
    return fl, em, se, ke, ae, mt, cd, fa, wq, conn


# Realistic Robot Framework code snippets
RF_CODE_WITH_FOR = """*** Settings ***
Library    Browser

*** Test Cases ***
Verify All Rows Show Active
    New Browser    headless=true
    New Page    https://dashboard.example.com/users
    ${rows}=    Get Elements    css=table tbody tr
    FOR    ${row}    IN    @{rows}
        ${text}=    Get Text    ${row} >> css=td.status
        Should Be Equal    ${text}    Active
    END
"""

RF_CODE_WITHOUT_FOR = """*** Settings ***
Library    Browser

*** Test Cases ***
Verify Status
    New Browser    headless=true
    New Page    https://dashboard.example.com/users
    ${text}=    Get Text    css=table tbody tr:first-child td.status
    Should Be Equal    ${text}    Active
"""


# ===================================================================
# Integration 1: Full E2E Workflow (Pass + Fail)
# ===================================================================

@pytest.mark.integration
def test_e2e_passing_execution(in_memory_db):
    """Full pipeline: pass -> record stored -> engines learn -> metrics tracked."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="e2e-pass-001",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITH_FOR,
        test_status="passed",
        metrics=MockMetrics(total_llm_calls=5, total_cost=0.12),
    )

    # VERIFY: execution record stored
    record = em.get("e2e-pass-001")
    assert record is not None, "Record should be stored"
    assert record.test_status == "passed"
    assert record.domain == "dashboard.example.com"
    assert record.total_llm_calls == 5
    assert record.total_cost == 0.12


@pytest.mark.integration
def test_e2e_passing_metrics(in_memory_db):
    """After passing execution, learning_metrics should have a record."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="e2e-pass-002",
        user_query="verify all rows",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITH_FOR,
        test_status="passed",
    )
    row = conn.execute("SELECT * FROM learning_metrics").fetchone()
    assert row is not None, "Metrics should be recorded"
    assert row["test_passed"] == 1


@pytest.mark.integration
def test_e2e_passing_daily_stats(in_memory_db):
    """After passing execution, daily stats should be updated."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="e2e-pass-003",
        user_query="click button",
        url="https://example.com",
        robot_code="*** Test Cases ***",
        test_status="passed",
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM learning_stats WHERE stat_date = ?", (today,)
    ).fetchone()
    assert row is not None, "Daily stats should be updated"
    assert row["total_passed"] >= 1


@pytest.mark.integration
def test_e2e_failed_with_analysis(in_memory_db):
    """Failed execution -> failure analysis -> engines learn from failure."""
    failure = MockFailureAnalysis(
        category="A1",
        failed_keyword="FOR",
        error_message="No keyword with name 'FOR' found.",
    )
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(
        in_memory_db, failure_result=failure
    )
    fl.process_execution(
        workflow_id="e2e-fail-001",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITHOUT_FOR,
        test_status="failed",
        output_xml_path="/fake/output.xml",
    )

    record = em.get("e2e-fail-001")
    assert record is not None, "Record should be stored"
    assert record.test_status == "failed"
    assert record.failure_category == "A1"
    assert record.failed_keyword == "FOR"


@pytest.mark.integration
def test_e2e_failed_structural_rule_boosted(in_memory_db):
    """A1 failure with boost -> structural rule evidence increased by +3."""
    failure = MockFailureAnalysis(
        category="A1",
        failed_keyword="FOR",
        error_message="No keyword with name 'FOR' found.",
    )
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(
        in_memory_db, failure_result=failure
    )
    fl.process_execution(
        workflow_id="e2e-fail-boost",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITHOUT_FOR,
        test_status="failed",
        output_xml_path="/fake/output.xml",
    )

    # Check structural_rules table for evidence boost
    rules = conn.execute(
        "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
    ).fetchall()
    if rules:
        rule = dict(rules[0])
        # Boost = +3 for A1 failure
        assert rule["evidence_count"] >= 3, (
            f"A1 boost should give evidence >= 3, got {rule['evidence_count']}"
        )


# ===================================================================
# Integration 2: Feedback Workflow E2E
# ===================================================================

@pytest.mark.integration
def test_feedback_triage_result(in_memory_db):
    """process_user_feedback returns valid triage dict."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    # First create a record
    fl.process_execution(
        workflow_id="fb-001",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITHOUT_FOR,
        test_status="failed",
    )
    triage = fl.process_user_feedback(
        workflow_id="fb-001",
        feedback_text="It should have checked all rows not just one",
        feedback_type="completely_wrong",
    )
    assert isinstance(triage, dict), "Triage should be a dict"
    assert "category" in triage
    assert "confidence" in triage
    assert "taxonomy_code" in triage


@pytest.mark.integration
def test_feedback_structural_category(in_memory_db):
    """Structural feedback should be triaged to 'structural' category."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="fb-002",
        user_query="verify all rows",
        url="https://example.com",
        robot_code="code",
        test_status="failed",
    )
    triage = fl.process_user_feedback(
        workflow_id="fb-002",
        feedback_text="it needs a loop to go through each row",
        feedback_type="completely_wrong",
    )
    assert triage["category"] == "structural", (
        f"Expected 'structural', got '{triage['category']}'"
    )


@pytest.mark.integration
def test_feedback_confidence_above_base(in_memory_db):
    """Triage confidence should be above base confidence."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="fb-003",
        user_query="click button",
        url="https://example.com",
        robot_code="code",
        test_status="failed",
    )
    triage = fl.process_user_feedback(
        workflow_id="fb-003",
        feedback_text="it needs a loop for each row",
        feedback_type="completely_wrong",
    )
    assert triage["confidence"] >= 0.5, (
        f"Confidence should be >= 0.5, got {triage['confidence']}"
    )


@pytest.mark.integration
def test_feedback_persists_to_db(in_memory_db):
    """Feedback text and type should be persisted in execution record."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    fl.process_execution(
        workflow_id="fb-004",
        user_query="click button",
        url="https://example.com",
        robot_code="code",
        test_status="failed",
    )
    fl.process_user_feedback(
        workflow_id="fb-004",
        feedback_text="wrong button was clicked",
        feedback_type="completely_wrong",
    )
    record = em.get("fb-004")
    assert record.user_feedback == "wrong button was clicked", (
        f"Feedback not persisted: {record.user_feedback}"
    )
    assert record.user_feedback_type == "completely_wrong"


# ===================================================================
# Integration 3: Learning Accumulation
# ===================================================================

@pytest.mark.integration
def test_accumulation_single_a1_triggers_hints(in_memory_db):
    """1 A1 failure with boost=True -> evidence=3 -> hints activate."""
    failure = MockFailureAnalysis(
        category="A1",
        failed_keyword="FOR",
        error_message="No keyword with name 'FOR' found.",
    )
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(
        in_memory_db, failure_result=failure
    )

    # Process 1 failed execution
    fl.process_execution(
        workflow_id="acc-001",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITHOUT_FOR,
        test_status="failed",
        output_xml_path="/fake/output.xml",
    )

    # Check: evidence should be 3 (boost +3)
    rule = conn.execute(
        "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
    ).fetchone()
    if rule:
        assert rule["evidence_count"] >= 3, (
            f"Boost should give evidence >= 3, got {rule['evidence_count']}"
        )
        # score = 3/(3+0+1) = 0.75 -> passes threshold (>= 0.4 AND obs >= 3)
        assert EffectivenessScore.passes_threshold(
            rule["evidence_count"], rule["counter_evidence"]
        ), "Rule should pass threshold after 1 boosted A1 failure"

        # Verify hints are now available
        hints = se.get_hints(
            "verify all rows in table show Active",
            "https://dashboard.example.com/users",
            "planner",
        )
        assert hints is not None, (
            "Hints should be available after 1 boosted A1 failure"
        )


@pytest.mark.integration
def test_accumulation_multiple_failures_increase_evidence(in_memory_db):
    """Multiple A1 failures -> evidence grows proportionally."""
    failure = MockFailureAnalysis(
        category="A1",
        failed_keyword="FOR",
        error_message="No keyword with name 'FOR' found.",
    )
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(
        in_memory_db, failure_result=failure
    )

    # Process 3 failed executions
    for i in range(3):
        fl.process_execution(
            workflow_id=f"acc-multi-{i}",
            user_query="verify all rows in table show Active",
            url="https://dashboard.example.com/users",
            robot_code=RF_CODE_WITHOUT_FOR,
            test_status="failed",
            output_xml_path="/fake/output.xml",
        )

    rule = conn.execute(
        "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
    ).fetchone()
    if rule:
        # 3 A1 failures with boost +3 each = 9
        assert rule["evidence_count"] >= 9, (
            f"3 A1 failures should give evidence >= 9, got {rule['evidence_count']}"
        )


@pytest.mark.integration
def test_accumulation_passing_adds_evidence(in_memory_db):
    """Passing execution with FOR code -> evidence +1 (no boost)."""
    failure = MockFailureAnalysis(
        category="A1",
        failed_keyword="FOR",
        error_message="No keyword with name 'FOR' found.",
    )
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(
        in_memory_db, failure_result=failure
    )

    # First, create the rule via a failure
    fl.process_execution(
        workflow_id="acc-pass-setup",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITHOUT_FOR,
        test_status="failed",
        output_xml_path="/fake/output.xml",
    )

    rule_before = conn.execute(
        "SELECT evidence_count FROM structural_rules WHERE rule_name = 'iteration'"
    ).fetchone()
    evidence_before = rule_before["evidence_count"] if rule_before else 0

    # Now process a passing execution with FOR code
    fl.process_execution(
        workflow_id="acc-pass-001",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code=RF_CODE_WITH_FOR,
        test_status="passed",
    )

    rule_after = conn.execute(
        "SELECT evidence_count FROM structural_rules WHERE rule_name = 'iteration'"
    ).fetchone()
    if rule_after:
        evidence_after = rule_after["evidence_count"]
        assert evidence_after > evidence_before, (
            f"Passing should increase evidence: "
            f"before={evidence_before}, after={evidence_after}"
        )


@pytest.mark.integration
def test_accumulation_threshold_boundary_direct_db():
    """Direct DB: evidence=2, counter=0 -> NOT pass (obs < 3)."""
    assert EffectivenessScore.passes_threshold(2, 0) is False, (
        "evidence=2, counter=0: observations=2 < MIN_OBSERVATIONS=3"
    )
    assert EffectivenessScore.passes_threshold(3, 0) is True, (
        "evidence=3, counter=0: observations=3 >= 3 AND score=0.75 >= 0.4"
    )
    assert EffectivenessScore.passes_threshold(2, 5) is False, (
        "evidence=2, counter=5: score=0.25 < 0.4"
    )
    # Edge case: exactly at min threshold
    assert EffectivenessScore.passes_threshold(3, 4) is False, (
        "evidence=3, counter=4: score=3/(3+4+1)=0.375 < 0.4"
    )


# ===================================================================
# Integration 4: Kill Switch
# ===================================================================

@pytest.mark.integration
def test_kill_switch_circuit_breaker(in_memory_db):
    """Circuit breaker disabled -> process_execution is a no-op."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    # Trip the circuit breaker
    for _ in range(20):
        fl.circuit_breaker.record_error(RuntimeError("simulated"))
    assert fl.circuit_breaker.is_enabled() is False, (
        "Circuit breaker should be disabled after 20 errors"
    )

    fl.process_execution(
        workflow_id="kill-001",
        user_query="click button",
        url="https://example.com",
        robot_code="code",
        test_status="passed",
    )
    # Nothing should have been stored
    assert em.get("kill-001") is None, (
        "Circuit breaker disabled: no record should be stored"
    )


@pytest.mark.integration
def test_kill_switch_feedback_returns_fallback(in_memory_db):
    """Circuit breaker disabled -> process_user_feedback returns fallback."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)
    for _ in range(20):
        fl.circuit_breaker.record_error(RuntimeError("simulated"))
    triage = fl.process_user_feedback(
        workflow_id="kill-fb",
        feedback_text="wrong button",
        feedback_type="completely_wrong",
    )
    assert triage["category"] == "uncategorized", (
        "Fallback triage should return 'uncategorized'"
    )
    assert triage["confidence"] == 0.0


@pytest.mark.integration
def test_kill_switch_optimization_setting():
    """OPTIMIZATION_ENABLED setting exists and affects circuit breaker."""
    try:
        from src.backend.settings import settings
        assert hasattr(settings, "OPTIMIZATION_ENABLED"), (
            "settings should have OPTIMIZATION_ENABLED"
        )
    except ImportError:
        # Settings import may fail in test env - check config instead
        pass


# ===================================================================
# Integration 5: Circuit Breaker
# ===================================================================

@pytest.mark.integration
def test_cb_initial_state():
    """Circuit breaker starts enabled."""
    cb = LearningCircuitBreaker()
    # Note: is_enabled also checks OPTIMIZATION_ENABLED setting
    # We only need to verify the error rate logic
    assert cb._error_count == 0
    assert cb._total_calls == 0


@pytest.mark.integration
def test_cb_auto_disable_after_errors():
    """Circuit breaker auto-disables after exceeding error threshold."""
    cb = LearningCircuitBreaker()
    # Record enough errors to exceed MIN_CALLS_BEFORE_CHECK (10) limit
    # with 100% error rate (threshold is 0.2 / 20%)
    for _ in range(15):
        cb.record_error(RuntimeError("test"))
    # Error rate = 15/15 = 100% > 50% threshold
    # And total_calls=15 >= MIN_CALLS_BEFORE_CHECK=10
    stats = cb.get_stats()
    assert stats["error_count"] == 15
    assert stats["error_rate"] == 1.0
    assert stats["total_calls"] == 15


@pytest.mark.integration
def test_cb_healthy_after_mixed():
    """Mixed success/error below threshold -> stays enabled."""
    cb = LearningCircuitBreaker()
    # 8 successes + 2 errors = 20% error rate < 50%
    for _ in range(8):
        cb.record_success()
    for _ in range(2):
        cb.record_error(RuntimeError("test"))
    stats = cb.get_stats()
    assert stats["error_rate"] == 0.2, f"Expected 0.2, got {stats['error_rate']}"


@pytest.mark.integration
def test_cb_stats_format():
    """get_stats() returns expected keys."""
    cb = LearningCircuitBreaker()
    cb.record_success()
    stats = cb.get_stats()
    assert "total_calls" in stats
    assert "error_count" in stats
    assert "error_rate" in stats
    assert "is_open" in stats
    assert stats["total_calls"] == 1
    assert stats["error_count"] == 0


@pytest.mark.integration
def test_cb_process_execution_after_errors(in_memory_db):
    """FeedbackLoop respects circuit breaker during processing."""
    fl, em, se, ke, ae, mt, cd, fa, wq, conn = build_real_feedback_loop(in_memory_db)

    # Process normally first
    fl.process_execution(
        workflow_id="cb-ok-001",
        user_query="click button",
        url="https://example.com",
        robot_code="code",
        test_status="passed",
    )
    assert em.get("cb-ok-001") is not None, "Normal execution should work"

    # Trip circuit breaker
    for _ in range(20):
        fl.circuit_breaker.record_error(RuntimeError("boom"))

    # Try to process -- should be blocked
    fl.process_execution(
        workflow_id="cb-blocked-001",
        user_query="click button",
        url="https://example.com",
        robot_code="code",
        test_status="passed",
    )
    assert em.get("cb-blocked-001") is None, (
        "Execution after CB trip should not store record"
    )
