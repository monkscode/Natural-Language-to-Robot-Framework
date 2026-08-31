"""
DAY 05 Tests -- Feedback Loop & Learning Metrics Tracker.

Pytest conversion of scripts/verify_day05.py.
Tests: LearningMetricsTracker, ContradictionDetector, FeedbackLoop.
Uses in-memory SQLite with full Phase 1 schema.
"""

import json
from datetime import datetime, timezone
from dataclasses import dataclass

import pytest

from src.backend.crew_ai.optimization.learning_config import (
    LearningCircuitBreaker,
    EffectivenessScore,
)
from src.backend.crew_ai.optimization.feedback_loop import (
    LearningMetricsTracker,
    ContradictionDetector,
    FeedbackLoop,
)


# ===================================================================
# Test Helpers
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
        self.hints = []
        self._stats = {"total_rules": 0, "active_rules": 0}

    def learn(self, record):
        self.learn_calls.append(record)

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


@dataclass
class MockFailureAnalysis:
    category: str = "A1"
    failure_type: str = "missing_keyword"
    failed_keyword: str = "FOR"
    error_message: str = "Missing FOR loop"
    confidence: float = 0.9
    source: str = "xml_parser"


@dataclass
class MockMetrics:
    total_llm_calls: int = 3
    total_cost: float = 0.05


def create_execution_memory(conn):
    """Return the execution store wrapped by the in_memory_db fixture.

    conn is the _EngineCompatConn from the in_memory_db fixture; the real
    PostgresExecutionMemory it wraps is returned so read_conn() works correctly.
    """
    return conn._em


def _insert_structural_rule(conn, name, evidence, counter_evidence):
    score = EffectivenessScore.calculate(evidence, counter_evidence)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO structural_rules "
        "(rule_name, query_pattern, required_structure, "
        " evidence_count, counter_evidence, score, "
        " last_updated, created_at) "
        "VALUES (?, 'pattern', 'for_loop', ?, ?, ?, ?, ?)",
        (name, evidence, counter_evidence, score, now, now),
    )
    conn.commit()


def _insert_anti_pattern(conn, category, evidence, score, last_seen=None):
    now = last_seen or datetime.now().isoformat()
    conn.execute(
        "INSERT INTO anti_patterns "
        "(failure_category, query_pattern, bad_code_snippet, "
        " correct_alternative, evidence_count, score, domain, last_seen) "
        "VALUES (?, 'pattern', 'code', 'fix it', ?, ?, '*', ?)",
        (category, evidence, score, now),
    )
    conn.commit()


def _build_feedback_loop(conn):
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
# Category 1: LearningMetricsTracker Tests
# ===================================================================

class TestLearningMetricsTracker:
    """Tests for LearningMetricsTracker."""

    def test_metrics_record_execution(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        tracker.record_execution(
            workflow_id="wf-001", user_query="click button",
            is_first_attempt=True, hints_available=0, hints_injected=0,
            hint_sources=[], llm_calls=3, llm_cost=0.05, test_passed=True,
        )
        row = in_memory_db.execute("SELECT * FROM learning_metrics").fetchone()
        assert row is not None, "No record inserted"
        assert row["workflow_id"] == "wf-001"
        assert row["test_passed"] == 1
        assert row["llm_cost"] == 0.05

    def test_metrics_record_with_hints(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        tracker.record_execution(
            workflow_id="wf-002", user_query="verify rows",
            is_first_attempt=True, hints_available=3, hints_injected=2,
            hint_sources=["structural", "keyword"], llm_calls=5,
            llm_cost=0.10, test_passed=False, hint_tokens=150,
        )
        row = in_memory_db.execute("SELECT * FROM learning_metrics").fetchone()
        assert row["hints_injected"] == 2
        assert row["hint_tokens"] == 150
        assert row["hint_sources"] == ["structural", "keyword"]  # jsonb -> list

    def test_metrics_record_retry(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        tracker.record_execution(
            workflow_id="wf-003", user_query="click button",
            is_first_attempt=False, hints_available=2, hints_injected=2,
            hint_sources=["anti_pattern"], llm_calls=4, llm_cost=0.08,
            test_passed=True, is_retry_after_feedback=True, attempt_number=2,
        )
        row = in_memory_db.execute("SELECT * FROM learning_metrics").fetchone()
        assert row["is_retry_after_feedback"] == 1
        assert row["attempt_number"] == 2
        assert row["is_first_attempt"] == 0

    def test_metrics_effectiveness_cat_a_only(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        for i in range(5):
            tracker.record_execution(
                workflow_id=f"wf-a-{i}", user_query=f"query {i}",
                is_first_attempt=True, hints_available=0, hints_injected=0,
                hint_sources=[], llm_calls=2, llm_cost=0.03,
                test_passed=(i % 2 == 0),  # 3 pass, 2 fail
            )
        report = tracker.get_effectiveness_report()
        assert report["natural_comparison"]["no_hints_available"]["total"] == 5
        assert report["natural_comparison"]["no_hints_available"]["passed"] == 3
        assert report["natural_comparison"]["no_hints_available"]["pass_rate"] == 0.6

    def test_metrics_effectiveness_cat_b(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        # 20 Cat A: 8 pass (40%)
        for i in range(20):
            tracker.record_execution(
                workflow_id=f"catb-a-{i}", user_query=f"qa {i}",
                is_first_attempt=True, hints_available=0, hints_injected=0,
                hint_sources=[], llm_calls=2, llm_cost=0.03,
                test_passed=(i < 8),
            )
        # 20 Cat B: 16 pass (80%)
        for i in range(20):
            tracker.record_execution(
                workflow_id=f"catb-b-{i}", user_query=f"qb {i}",
                is_first_attempt=True, hints_available=3, hints_injected=2,
                hint_sources=["structural"], llm_calls=3, llm_cost=0.05,
                test_passed=(i < 16),
            )
        report = tracker.get_effectiveness_report()
        comp = report["natural_comparison"]
        assert comp["no_hints_available"]["pass_rate"] == 0.4, \
            f"Expected 0.4, got {comp['no_hints_available']['pass_rate']}"
        assert comp["hints_injected"]["pass_rate"] == 0.8, \
            f"Expected 0.8, got {comp['hints_injected']['pass_rate']}"
        assert comp["lift"] == 0.4, \
            f"Expected lift 0.4, got {comp['lift']}"
        assert comp["sufficient_data"] is True, \
            f"Expected sufficient=True"

    def test_metrics_effectiveness_insufficient_data(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        tracker.record_execution(
            workflow_id="wf-1", user_query="q", is_first_attempt=True,
            hints_available=0, hints_injected=0, hint_sources=[],
            llm_calls=1, llm_cost=0.01, test_passed=True,
        )
        report = tracker.get_effectiveness_report()
        assert report["natural_comparison"]["sufficient_data"] is False

    def test_metrics_cpst(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        # 2 executions: $0.10 each, 1 passes
        tracker.record_execution(
            workflow_id="c1", user_query="q1", is_first_attempt=True,
            hints_available=0, hints_injected=0, hint_sources=[],
            llm_calls=2, llm_cost=0.10, test_passed=True,
        )
        tracker.record_execution(
            workflow_id="c2", user_query="q2", is_first_attempt=True,
            hints_available=0, hints_injected=0, hint_sources=[],
            llm_calls=2, llm_cost=0.10, test_passed=False,
        )
        report = tracker.get_effectiveness_report()
        assert report["cost_per_successful_test"] == 0.2  # $0.20 / 1 success

    def test_metrics_retry_rate(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        tracker.record_execution(
            workflow_id="r1", user_query="q", is_first_attempt=False,
            hints_available=2, hints_injected=2, hint_sources=["s"],
            llm_calls=3, llm_cost=0.05, test_passed=True,
            is_retry_after_feedback=True, attempt_number=2,
        )
        tracker.record_execution(
            workflow_id="r2", user_query="q2", is_first_attempt=False,
            hints_available=1, hints_injected=1, hint_sources=["k"],
            llm_calls=3, llm_cost=0.05, test_passed=False,
            is_retry_after_feedback=True, attempt_number=2,
        )
        report = tracker.get_effectiveness_report()
        assert report["retry_after_feedback"]["total"] == 2
        assert report["retry_after_feedback"]["pass_rate"] == 0.5

    def test_metrics_hint_accuracy(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        # 4 Cat B: 3 pass -> hint_accuracy = 0.75
        for i in range(4):
            tracker.record_execution(
                workflow_id=f"h-{i}", user_query=f"q{i}",
                is_first_attempt=True, hints_available=2, hints_injected=1,
                hint_sources=["structural"], llm_calls=3, llm_cost=0.04,
                test_passed=(i < 3),
            )
        report = tracker.get_effectiveness_report()
        assert report["hint_accuracy"] == 0.75

    def test_metrics_empty_db(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        report = tracker.get_effectiveness_report()
        assert report["total_executions"] == 0
        assert report["cost_per_successful_test"] == 0.0
        assert report["natural_comparison"]["lift"] == 0.0

    def test_metrics_all_fail(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        for i in range(3):
            tracker.record_execution(
                workflow_id=f"f-{i}", user_query=f"q{i}",
                is_first_attempt=True, hints_available=0, hints_injected=0,
                hint_sources=[], llm_calls=2, llm_cost=0.05, test_passed=False,
            )
        report = tracker.get_effectiveness_report()
        assert report["cost_per_successful_test"] == 0.0  # No division by zero
        assert report["natural_comparison"]["no_hints_available"]["pass_rate"] == 0.0

    def test_metrics_multiple_records(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        for i in range(10):
            tracker.record_execution(
                workflow_id=f"m-{i}", user_query=f"q{i}",
                is_first_attempt=True, hints_available=0, hints_injected=0,
                hint_sources=[], llm_calls=1, llm_cost=0.01, test_passed=True,
            )
        count = in_memory_db.execute(
            "SELECT COUNT(*) FROM learning_metrics"
        ).fetchone()[0]
        assert count == 10

    def test_metrics_cpst_zero_successes(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        tracker.record_execution(
            workflow_id="z1", user_query="q", is_first_attempt=True,
            hints_available=0, hints_injected=0, hint_sources=[],
            llm_calls=5, llm_cost=0.50, test_passed=False,
        )
        report = tracker.get_effectiveness_report()
        assert report["cost_per_successful_test"] == 0.0

    def test_metrics_total_executions_includes_retries(self, in_memory_db):
        tracker = LearningMetricsTracker(in_memory_db)
        # 2 first attempts + 1 retry
        tracker.record_execution(
            workflow_id="t1", user_query="q", is_first_attempt=True,
            hints_available=0, hints_injected=0, hint_sources=[],
            llm_calls=1, llm_cost=0.01, test_passed=True,
        )
        tracker.record_execution(
            workflow_id="t2", user_query="q2", is_first_attempt=True,
            hints_available=2, hints_injected=1, hint_sources=["s"],
            llm_calls=2, llm_cost=0.02, test_passed=False,
        )
        tracker.record_execution(
            workflow_id="t3", user_query="q2", is_first_attempt=False,
            hints_available=2, hints_injected=2, hint_sources=["s", "k"],
            llm_calls=3, llm_cost=0.03, test_passed=True,
            is_retry_after_feedback=True, attempt_number=2,
        )
        report = tracker.get_effectiveness_report()
        assert report["total_executions"] == 3

    def test_metrics_lift_negative(self, in_memory_db):
        """Lift can be negative if hints hurt performance."""
        tracker = LearningMetricsTracker(in_memory_db)
        # Cat A: 5 all pass (100%)
        for i in range(5):
            tracker.record_execution(
                workflow_id=f"neg-a-{i}", user_query=f"qa{i}",
                is_first_attempt=True, hints_available=0, hints_injected=0,
                hint_sources=[], llm_calls=2, llm_cost=0.03, test_passed=True,
            )
        # Cat B: 5 all fail (0%)
        for i in range(5):
            tracker.record_execution(
                workflow_id=f"neg-b-{i}", user_query=f"qb{i}",
                is_first_attempt=True, hints_available=3, hints_injected=2,
                hint_sources=["structural"], llm_calls=3, llm_cost=0.05,
                test_passed=False,
            )
        report = tracker.get_effectiveness_report()
        assert report["natural_comparison"]["lift"] == -1.0


# ===================================================================
# Category 2: ContradictionDetector Tests
# ===================================================================

class TestContradictionDetector:
    """Tests for ContradictionDetector."""

    def test_cd_no_contradictions(self, in_memory_db):
        _insert_structural_rule(in_memory_db, "good_rule", 10, 1)
        detector = ContradictionDetector(in_memory_db)
        assert len(detector.detect_all()) == 0

    def test_cd_structural_contradiction(self, in_memory_db):
        _insert_structural_rule(in_memory_db, "bad_rule", 5, 5)  # 50% contradiction
        detector = ContradictionDetector(in_memory_db)
        flagged = detector.detect_all()
        assert len(flagged) == 1
        assert flagged[0]["rule_type"] == "structural"
        assert flagged[0]["rule_name"] == "bad_rule"
        assert flagged[0]["contradiction_ratio"] == 0.5

    def test_cd_structural_below_threshold(self, in_memory_db):
        _insert_structural_rule(in_memory_db, "ok_rule", 8, 2)  # 20% -- below 40%
        detector = ContradictionDetector(in_memory_db)
        assert len(detector.detect_all()) == 0

    def test_cd_structural_min_observations_gate(self, in_memory_db):
        _insert_structural_rule(
            in_memory_db, "tiny_rule", 1, 2
        )  # 67% ratio but only 3 obs
        detector = ContradictionDetector(in_memory_db)
        assert len(detector.detect_all()) == 0  # Below min_observations=5

    def test_cd_anti_pattern_staleness(self, in_memory_db):
        """Anti-pattern with sufficient evidence but stale (>90 days) is flagged."""
        stale_date = "2025-01-01T00:00:00"  # Well over 90 days ago
        _insert_anti_pattern(
            in_memory_db, "A1", evidence=6, score=0.8, last_seen=stale_date
        )
        detector = ContradictionDetector(in_memory_db)
        flagged = detector.detect_all()
        assert len(flagged) == 1
        assert flagged[0]["rule_type"] == "anti_pattern"

    def test_cd_anti_pattern_healthy(self, in_memory_db):
        _insert_anti_pattern(in_memory_db, "B1", evidence=8, score=0.7)
        detector = ContradictionDetector(in_memory_db)
        assert len(detector.detect_all()) == 0

    def test_cd_anti_pattern_min_evidence_gate(self, in_memory_db):
        _insert_anti_pattern(
            in_memory_db, "C1", evidence=3, score=0.2
        )  # Fresh and below min_observations=5 — not flagged
        detector = ContradictionDetector(in_memory_db)
        assert len(detector.detect_all()) == 0

    def test_cd_mixed_results(self, in_memory_db):
        _insert_structural_rule(in_memory_db, "good", 10, 1)
        _insert_structural_rule(in_memory_db, "bad", 3, 5)  # 62.5%
        _insert_anti_pattern(in_memory_db, "A1", evidence=7, score=0.8)  # Fresh — not flagged
        stale_date = "2025-01-01T00:00:00"
        _insert_anti_pattern(
            in_memory_db, "B2", evidence=6, score=0.8, last_seen=stale_date
        )  # Stale — flagged
        detector = ContradictionDetector(in_memory_db)
        flagged = detector.detect_all()
        assert len(flagged) == 2
        types = {f["rule_type"] for f in flagged}
        assert types == {"structural", "anti_pattern"}

    def test_cd_custom_threshold(self, in_memory_db):
        _insert_structural_rule(
            in_memory_db, "edge", 6, 4
        )  # 40% -- exactly at default
        detector_strict = ContradictionDetector(
            in_memory_db, contradiction_threshold=0.3
        )
        detector_lenient = ContradictionDetector(
            in_memory_db, contradiction_threshold=0.5
        )
        assert len(detector_strict.detect_all()) == 1
        assert len(detector_lenient.detect_all()) == 0

    def test_cd_custom_min_observations(self, in_memory_db):
        _insert_structural_rule(
            in_memory_db, "small", 2, 3
        )  # 60% but only 5 total
        detector_low = ContradictionDetector(
            in_memory_db, min_observations=4
        )
        detector_high = ContradictionDetector(
            in_memory_db, min_observations=6
        )
        assert len(detector_low.detect_all()) == 1
        assert len(detector_high.detect_all()) == 0

    def test_cd_get_summary(self, in_memory_db):
        _insert_structural_rule(in_memory_db, "bad1", 3, 7)
        stale_date = "2025-01-01T00:00:00"
        _insert_anti_pattern(
            in_memory_db, "A1", evidence=5, score=0.8, last_seen=stale_date
        )
        detector = ContradictionDetector(in_memory_db)
        summary = detector.get_summary()
        assert summary["total_flagged"] == 2
        assert summary["by_type"]["structural"] == 1
        assert summary["by_type"]["anti_pattern"] == 1
        assert len(summary["flagged_rules"]) == 2

    def test_cd_empty_db(self, in_memory_db):
        detector = ContradictionDetector(in_memory_db)
        assert detector.detect_all() == []
        summary = detector.get_summary()
        assert summary["total_flagged"] == 0

    def test_cd_checker_error_handled(self, in_memory_db):
        """A failing checker should not crash detect_all; the other checker still runs."""
        from unittest.mock import patch
        detector = ContradictionDetector(in_memory_db)
        with patch.object(detector, "_check_structural_rules", side_effect=RuntimeError("DB error")):
            flagged = detector.detect_all()
        # _check_anti_patterns still ran — result is a list (not a raised exception)
        assert isinstance(flagged, list)

    def test_cd_build_flag_format(self):
        flag = ContradictionDetector._build_flag(
            rule_type="structural", rule_id=42, rule_name="test_rule",
            evidence=10, counter_evidence=5, contradiction_ratio=0.3333,
            current_score=0.6667,
        )
        assert flag["rule_type"] == "structural"
        assert flag["rule_id"] == 42
        assert flag["contradiction_ratio"] == 0.3333
        assert flag["current_score"] == 0.6667

    def test_cd_zero_threshold(self, in_memory_db):
        """With threshold=0, everything with any counter_evidence is flagged."""
        _insert_structural_rule(
            in_memory_db, "any_ce", 9, 1
        )  # 10% contradiction
        detector = ContradictionDetector(
            in_memory_db, contradiction_threshold=0.0
        )
        assert len(detector.detect_all()) == 1


# ===================================================================
# Category 3: FeedbackLoop Tests
# ===================================================================

class TestFeedbackLoop:
    """Tests for FeedbackLoop."""

    def test_fl_process_execution_passed(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-p1", user_query="click button",
            url="https://example.com", robot_code="*** Test Cases ***",
            test_status="passed",
        )
        # Should have stored in execution_memory
        record = em.get("wf-p1")
        assert record is not None
        assert record.test_status == "passed"
        # All 3 engines should have been called
        assert len(se.learn_calls) == 1
        assert len(ke.learn_calls) == 1
        assert len(ae.learn_calls) == 1
        # Metrics should be recorded
        row = conn.execute("SELECT * FROM learning_metrics").fetchone()
        assert row is not None
        assert row["test_passed"] == 1

    def test_fl_process_execution_failed_with_analysis(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fa._result = MockFailureAnalysis()
        fl.process_execution(
            workflow_id="wf-f1", user_query="verify rows",
            url="https://demoqa.com", robot_code="*** Test Cases ***",
            test_status="failed", output_xml_path="/fake/output.xml",
        )
        record = em.get("wf-f1")
        assert record is not None
        assert record.failure_category == "A1"
        assert record.failed_keyword == "FOR"
        assert len(fa.analyze_calls) == 1

    def test_fl_process_execution_failed_no_xml(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-f2", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="failed",
        )
        # No output.xml -> no analysis call
        assert len(fa.analyze_calls) == 0
        record = em.get("wf-f2")
        assert record.failure_category is None

    def test_fl_circuit_breaker_disabled(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        # Trip circuit breaker by exceeding error threshold
        # MIN_CALLS_BEFORE_CHECK defaults to 10, ERROR_RATE_THRESHOLD to 0.5
        for _ in range(20):
            fl.circuit_breaker.record_error(RuntimeError("test"))
        assert fl.circuit_breaker.is_enabled() is False
        fl.process_execution(
            workflow_id="wf-cb", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="passed",
        )
        # Nothing should have been stored
        assert em.get("wf-cb") is None
        assert len(se.learn_calls) == 0

    def test_fl_process_execution_error_nonblocking(self, in_memory_db):
        """process_execution errors must not raise."""
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)

        # Make store raise
        def boom(record):
            raise RuntimeError("DB exploded")

        em.store = boom
        # Should NOT raise
        fl.process_execution(
            workflow_id="wf-err", user_query="q", url="https://x.com",
            robot_code="code", test_status="passed",
        )

    def test_fl_process_execution_with_metrics(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-m1", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="passed", metrics=MockMetrics(),
        )
        record = em.get("wf-m1")
        assert record.total_llm_calls == 3
        assert record.total_cost == 0.05

    def test_fl_process_execution_with_hints(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-h1", user_query="verify rows",
            url="https://demoqa.com", robot_code="FOR  ${row}",
            test_status="passed", hints_available=3, hints_injected=2,
            hint_sources=["structural", "keyword"], hint_tokens=200,
        )
        row = conn.execute("SELECT * FROM learning_metrics").fetchone()
        assert row["hints_injected"] == 2
        assert row["hint_tokens"] == 200

    def test_fl_process_user_feedback(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        # First store a record
        fl.process_execution(
            workflow_id="wf-fb", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="failed",
        )
        fl.process_user_feedback(
            workflow_id="wf-fb", feedback_text="wrong button clicked",
            feedback_type="completely_wrong",
        )
        record = em.get("wf-fb")
        assert record.user_feedback == "wrong button clicked"
        assert record.user_feedback_type == "completely_wrong"

    def test_fl_process_user_feedback_no_record(self, in_memory_db):
        """Feedback for non-existent workflow should not crash."""
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_user_feedback(
            workflow_id="nonexistent", feedback_text="bad",
            feedback_type="completely_wrong",
        )

    def test_fl_process_user_feedback_circuit_breaker_disabled(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        # First store a record so we can verify feedback is NOT saved
        fl.process_execution(
            workflow_id="wf-x", user_query="click button",
            url="https://example.com", robot_code="code",
            test_status="failed",
        )
        # Trip the circuit breaker via error rate
        for _ in range(20):
            fl.circuit_breaker.record_error(RuntimeError("test"))
        assert fl.circuit_breaker.is_enabled() is False
        fl.process_user_feedback(
            workflow_id="wf-x", feedback_text="bad",
            feedback_type="completely_wrong",
        )
        # Feedback should NOT have been saved because circuit breaker is open
        record = em.get("wf-x")
        assert record.user_feedback is None, (
            f"Expected feedback to be blocked, but got: {record.user_feedback}"
        )

    def test_fl_get_learning_stats(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        stats = fl.get_learning_stats()
        assert "structural_rules" in stats
        assert "keyword_corrections" in stats
        assert "anti_patterns" in stats
        assert "learning_effectiveness" in stats
        assert "contradictions" in stats
        assert "total_records" in stats
        assert "circuit_breaker" in stats

    def test_fl_daily_stats_updated(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-ds1", user_query="click",
            url="https://example.com", robot_code="code",
            test_status="passed",
        )
        fl.process_execution(
            workflow_id="wf-ds2", user_query="click2",
            url="https://example.com", robot_code="code",
            test_status="failed",
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT * FROM learning_stats WHERE stat_date = ?", (today,)
        ).fetchone()
        assert row is not None
        assert row["total_executions"] == 2
        assert row["total_passed"] == 1
        assert row["total_failed"] == 1

    def test_fl_domain_extraction(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-dom", user_query="click",
            url="https://www.demoqa.com/buttons", robot_code="code",
            test_status="passed",
        )
        record = em.get("wf-dom")
        assert record.domain == "demoqa.com"

    def test_fl_code_structure_detection(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-cs", user_query="verify rows",
            url="https://example.com",
            robot_code="*** Test Cases ***\nFOR  ${row}  IN  @{rows}\n    Log  ${row}\nEND",
            test_status="passed",
        )
        record = em.get("wf-cs")
        assert record.code_structure == "for_loop"

    def test_fl_retry_execution(self, in_memory_db):
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        fl.process_execution(
            workflow_id="wf-r1", user_query="click",
            url="https://example.com", robot_code="code",
            test_status="passed", is_first_attempt=False,
            is_retry_after_feedback=True, attempt_number=2,
        )
        row = conn.execute("SELECT * FROM learning_metrics").fetchone()
        assert row["is_retry_after_feedback"] == 1
        assert row["attempt_number"] == 2


# ===================================================================
# Category 4: Integration Tests
# ===================================================================

class TestIntegration:
    """Integration tests for the feedback loop system."""

    def test_int_schema_has_learning_metrics_table(self, in_memory_db):
        tables = [r[0] for r in in_memory_db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()]
        assert "learning_metrics" in tables

    def test_int_schema_has_indexes(self, in_memory_db):
        indexes = [r[0] for r in in_memory_db.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        ).fetchall()]
        assert "idx_metrics_workflow" in indexes
        assert "idx_metrics_first_attempt" in indexes
        assert "idx_metrics_hints" in indexes

    def test_int_update_daily_stats_method(self, in_memory_db):
        em = create_execution_memory(in_memory_db)
        em.update_daily_stats("passed")
        em.update_daily_stats("passed")
        em.update_daily_stats("failed")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = in_memory_db.execute(
            "SELECT * FROM learning_stats WHERE stat_date = ?", (today,)
        ).fetchone()
        assert row["total_executions"] == 3
        assert row["total_passed"] == 2
        assert row["total_failed"] == 1

    def test_int_full_pipeline(self, in_memory_db):
        """Full pipeline: process_execution -> engines learn -> metrics recorded -> stats available."""
        fl, em, fa, se, ke, ae, mt, cd, conn = _build_feedback_loop(in_memory_db)
        for i in range(3):
            fl.process_execution(
                workflow_id=f"pipe-{i}", user_query=f"query {i}",
                url="https://example.com",
                robot_code="*** Test Cases ***\ncode",
                test_status="passed" if i < 2 else "failed",
                hints_available=i, hints_injected=min(i, 1),
                hint_sources=["structural"] if i > 0 else [],
            )
        stats = fl.get_learning_stats()
        assert stats["total_records"] == 3
        assert stats["learning_effectiveness"]["total_executions"] == 3
        assert stats["contradictions"]["total_flagged"] == 0

    def test_int_feedback_loop_with_contradictions(self, in_memory_db):
        """End-to-end: contradictions show up in stats."""
        _insert_structural_rule(in_memory_db, "contradicted_rule", 3, 7)
        fl, em, fa, se, ke, ae, mt, cd, _ = _build_feedback_loop(in_memory_db)
        stats = fl.get_learning_stats()
        assert stats["contradictions"]["total_flagged"] == 1
        assert stats["contradictions"]["by_type"]["structural"] == 1

    def test_int_multiple_days_stats(self, in_memory_db):
        em = create_execution_memory(in_memory_db)
        em.update_daily_stats("passed")
        em.update_daily_stats("failed")
        # Simulate a different day by direct insert
        in_memory_db.execute(
            "INSERT INTO learning_stats (stat_date, total_executions, "
            "total_passed, total_failed) VALUES ('2025-01-01', 5, 3, 2)"
        )
        in_memory_db.commit()
        rows = in_memory_db.execute("SELECT * FROM learning_stats").fetchall()
        assert len(rows) == 2

    def test_int_module_imports(self):
        """Verify internal modules are importable via direct path.

        These symbols were removed from __init__.py exports (Finding #2)
        because they are internal to the optimization package. They remain
        importable via their direct module paths.
        """
        from src.backend.crew_ai.optimization.feedback_loop import (
            FeedbackLoop,
            LearningMetricsTracker,
            ContradictionDetector,
        )
        assert FeedbackLoop is not None
        assert LearningMetricsTracker is not None
        assert ContradictionDetector is not None


# ===================================================================
# D3 — _get_with_retry unit tests
# ===================================================================

class TestGetWithRetry:
    """Unit tests for the bounded record poll.

    T7 replaced the 3-attempt / 300ms drain retry with a budgeted poll that
    returns (record, outcome).  ceiling_ms=300, base_ms=100 reproduces the old
    shape exactly — two sleeps, three reads — so the arithmetic below stays
    checkable by hand; the production defaults are pinned in
    test_feedback_ordering.py.
    """

    def test_returns_record_on_first_attempt(self):
        from unittest.mock import MagicMock
        from src.backend.crew_ai.optimization.feedback_loop import _get_with_retry

        record = MagicMock()
        em = MagicMock()
        em.get.return_value = record

        result, outcome = _get_with_retry(em, "wf-001", ceiling_ms=300, base_ms=100)

        assert result is record
        assert outcome == "processed"
        assert em.get.call_count == 1

    def test_retries_and_finds_record_on_second_attempt(self):
        from unittest.mock import MagicMock, patch
        from src.backend.crew_ai.optimization.feedback_loop import _get_with_retry

        record = MagicMock()
        em = MagicMock()
        em.get.side_effect = [None, record]

        with patch("src.backend.crew_ai.optimization.feedback_loop.time.sleep"):
            result, outcome = _get_with_retry(
                em, "wf-002", ceiling_ms=300, base_ms=100)

        assert result is record
        assert outcome == "processed"
        assert em.get.call_count == 2

    def test_retries_and_finds_record_on_third_attempt(self):
        from unittest.mock import MagicMock, patch
        from src.backend.crew_ai.optimization.feedback_loop import _get_with_retry

        record = MagicMock()
        em = MagicMock()
        em.get.side_effect = [None, None, record]

        with patch("src.backend.crew_ai.optimization.feedback_loop.time.sleep"):
            result, outcome = _get_with_retry(
                em, "wf-003", ceiling_ms=300, base_ms=100)

        assert result is record
        assert outcome == "processed"
        assert em.get.call_count == 3

    def test_returns_no_record_after_the_budget_is_spent(self):
        from unittest.mock import MagicMock, patch
        from src.backend.crew_ai.optimization.feedback_loop import _get_with_retry

        em = MagicMock()
        em.get.return_value = None

        with patch("src.backend.crew_ai.optimization.feedback_loop.time.sleep"):
            result, outcome = _get_with_retry(
                em, "wf-004", ceiling_ms=300, base_ms=100)

        assert result is None
        assert outcome == "no_record"
        assert em.get.call_count == 3

    def test_sleep_never_follows_the_last_read(self):
        """The budget is spent between reads, never after the final one — an
        extra sleep would add latency that can no longer change the answer."""
        from unittest.mock import MagicMock, patch, call
        from src.backend.crew_ai.optimization.feedback_loop import _get_with_retry

        em = MagicMock()
        em.get.return_value = None

        with patch("src.backend.crew_ai.optimization.feedback_loop.time.sleep") as mock_sleep:
            _get_with_retry(em, "wf-005", ceiling_ms=300, base_ms=100)

        assert mock_sleep.call_count == 2
        assert em.get.call_count == mock_sleep.call_count + 1
        # Exponential back-off: 100ms then 200ms
        mock_sleep.assert_has_calls([call(0.1), call(0.2)])


# ===================================================================
# C3 — ChromaDB status surfaced in get_learning_stats()
# ===================================================================

class TestChromaObservabilityInStats:
    """C3: get_learning_stats() must expose ChromaDB health so operators can
    detect a failed init without reading logs.

    Concrete scenario: ChromaDB directory is missing on startup. The sentinel
    is set. An operator calls GET /api/learning-stats to understand why semantic
    search is returning empty results. The "execution_memory" key tells them
    exactly what went wrong.
    """

    def test_learning_stats_includes_execution_memory_key(self, in_memory_db):
        fl, *_ = _build_feedback_loop(in_memory_db)
        stats = fl.get_learning_stats()
        assert "execution_memory" in stats, (
            "get_learning_stats() must include 'execution_memory' key for C3 observability"
        )

    def test_chromadb_available_false_when_sentinel_set(self, in_memory_db):
        """When ChromaDB failed to init (sentinel set), stats report unavailable."""
        fl, *_ = _build_feedback_loop(in_memory_db)
        # create_execution_memory sets _chroma_client = _CHROMADB_INIT_FAILED
        stats = fl.get_learning_stats()
        assert stats["execution_memory"]["chromadb_available"] is False

    def test_chromadb_last_error_none_when_no_failure_recorded(self, in_memory_db):
        """_chroma_last_error is None when _chroma_failed_at is not set (sentinel set
        without a real failure, as in tests)."""
        fl, *_ = _build_feedback_loop(in_memory_db)
        stats = fl.get_learning_stats()
        assert stats["execution_memory"]["chromadb_last_error"] is None


# ===================================================================
# C4 — optimization_init_failures counter in FeedbackLoop
# ===================================================================

class TestOptimizationInitFailuresCounter:
    """C4: FeedbackLoop._optimization_init_failures lets operators detect how often
    crew.py's outer optimization-init try/except caught an exception and fell back
    to baseline (no hints injected).

    Concrete scenario: On a multi-worker deployment, one worker has a stale ChromaDB
    lock. Every workflow on that worker silently runs without optimization. With C4,
    GET /api/learning-stats shows optimization_init_failures > 0, which is immediately
    actionable (restart that worker / free the lock).
    """

    def test_counter_starts_at_zero(self, in_memory_db):
        fl, *_ = _build_feedback_loop(in_memory_db)
        assert fl._optimization_init_failures == 0

    def test_learning_stats_includes_counter(self, in_memory_db):
        fl, *_ = _build_feedback_loop(in_memory_db)
        stats = fl.get_learning_stats()
        assert "optimization_init_failures" in stats

    def test_counter_value_exposed_in_stats(self, in_memory_db):
        """Stats reports the current counter value — crew.py increments it on failure."""
        fl, *_ = _build_feedback_loop(in_memory_db)
        fl._optimization_init_failures = 3
        stats = fl.get_learning_stats()
        assert stats["optimization_init_failures"] == 3
