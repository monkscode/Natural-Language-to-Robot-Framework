"""
Feedback Loop — Post-execution learning orchestrator + metrics tracking.

Three classes:
1. LearningMetricsTracker — Records every execution with hint context and cost
   data for natural comparison (Cat A vs Cat B) and ROI measurement (CPST).
2. ContradictionDetector — Extensible scanner that flags learned rules whose
   counter-evidence ratio exceeds a configurable threshold. Read-only: never
   modifies rules, only reports.
3. FeedbackLoop — Orchestrator that connects FailureAnalyzer, ExecutionMemory,
   all learning engines, and the metrics tracker.  Called after every execution
   and after user feedback.  Non-blocking: all writes go through LearningWriteQueue.

Design decisions:
- Dependency injection for all collaborators (testable, defaults construct
  real instances for production).
- ContradictionDetector uses a checker registry — adding a new engine
  requires only one method + one registry entry.
- FeedbackLoop.process_execution() is fully wrapped in try/except —
  learning never blocks the main pipeline.

Referenced by: workflow_service.py (DAY_07), /api/feedback endpoint (DAY_08).
Depends on: execution_memory.py (DAY_01), failure_analyzer.py (DAY_02),
            structural_rule_engine.py (DAY_03), keyword_correction_engine.py (DAY_03),
            anti_pattern_engine.py (DAY_04), learning_config.py (DAY_00).
"""

import json
import logging
from datetime import datetime
from typing import Optional, List, Dict

from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    LearningCircuitBreaker,
    LearningWriteQueue,
    EffectivenessScore,
    extract_domain,
)
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionMemory,
    ExecutionRecord,
    CodeStructureExtractor,
)
from src.backend.crew_ai.optimization.failure_analyzer import FailureAnalyzer
from src.backend.crew_ai.optimization.pattern_learning import QueryPatternMatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Learning Metrics Tracker
# ---------------------------------------------------------------------------

class LearningMetricsTracker:
    """
    Tracks learning system effectiveness through natural comparison + cost.

    Strategy (Natural Comparison — no holdout):
    - Category A: Executions where NO hints were injected (novel queries)
      → Organic baseline (LLM working alone)
    - Category B: Executions where hints WERE injected
      → Treatment group (LLM + learning)
    - Lift = Cat B pass rate - Cat A pass rate

    Also tracks:
    - Cost Per Successful Test (CPST) — the ROI metric
    - Retry-after-feedback success rate — does the feedback loop work?
    - Hint accuracy — when hints are injected, do tests pass?

    Why this works without holdout:
    - Early executions have no learned patterns → natural Category A
    - As learning grows, more executions get hints → natural Category B
    - Novel domains/queries always start as Category A → perpetual baseline
    """

    def __init__(self, db_conn):
        """
        Initialize with a shared SQLite connection.

        Args:
            db_conn: sqlite3.Connection with row_factory=sqlite3.Row.
                     Typically execution_memory.conn.
        """
        self.db_conn = db_conn
        self._min_sample_size = LEARNING_CONFIG[
            "NATURAL_COMPARISON_MIN_SAMPLE_SIZE"
        ]

    def record_execution(
        self,
        workflow_id: str,
        user_query: str,
        is_first_attempt: bool,
        hints_available: int,
        hints_injected: int,
        hint_sources: list,
        llm_calls: int,
        llm_cost: float,
        test_passed: bool,
        is_retry_after_feedback: bool = False,
        attempt_number: int = 1,
        hint_tokens: int = 0,
    ) -> None:
        """Record execution with full hint context and cost data."""
        self.db_conn.execute(
            "INSERT INTO learning_metrics "
            "(workflow_id, user_query, is_first_attempt, "
            " is_retry_after_feedback, attempt_number, "
            " hints_available, hints_injected, hint_sources, "
            " llm_calls, llm_cost, hint_tokens, test_passed, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))",
            (
                workflow_id,
                user_query,
                1 if is_first_attempt else 0,
                1 if is_retry_after_feedback else 0,
                attempt_number,
                hints_available,
                hints_injected,
                json.dumps(hint_sources),
                llm_calls,
                llm_cost,
                hint_tokens,
                1 if test_passed else 0,
            ),
        )
        self.db_conn.commit()
        logger.debug(
            "[LEARNING:METRICS] Recorded execution %s: "
            "hints_injected=%d, test_passed=%s",
            workflow_id, hints_injected, test_passed,
        )

    def get_effectiveness_report(self) -> dict:
        """
        Natural comparison + cost metrics report.

        Returns dict with:
        - natural_comparison: Cat A vs Cat B pass rates + lift
        - retry_after_feedback: success rate for feedback-driven retries
        - hint_accuracy: pass rate when hints were injected
        - cost_per_successful_test: current CPST
        - total_executions: aggregate count
        - sufficient_data: bool — enough data per category?
        """
        # Category A: No hints injected, first attempt (organic baseline)
        cat_a = self.db_conn.execute(
            "SELECT COUNT(*) as total, "
            "       COALESCE(SUM(test_passed), 0) as passed "
            "FROM learning_metrics "
            "WHERE hints_injected = 0 AND is_first_attempt = 1"
        ).fetchone()

        # Category B: Hints were injected, first attempt
        cat_b = self.db_conn.execute(
            "SELECT COUNT(*) as total, "
            "       COALESCE(SUM(test_passed), 0) as passed "
            "FROM learning_metrics "
            "WHERE hints_injected > 0 AND is_first_attempt = 1"
        ).fetchone()

        cat_a_total = cat_a["total"] or 0
        cat_a_passed = cat_a["passed"] or 0
        cat_b_total = cat_b["total"] or 0
        cat_b_passed = cat_b["passed"] or 0

        cat_a_rate = cat_a_passed / cat_a_total if cat_a_total > 0 else 0.0
        cat_b_rate = cat_b_passed / cat_b_total if cat_b_total > 0 else 0.0
        lift = cat_b_rate - cat_a_rate

        # Retry-after-feedback success rate
        retry = self.db_conn.execute(
            "SELECT COUNT(*) as total, "
            "       COALESCE(SUM(test_passed), 0) as passed "
            "FROM learning_metrics "
            "WHERE is_retry_after_feedback = 1"
        ).fetchone()
        retry_total = retry["total"] or 0
        retry_passed = retry["passed"] or 0
        retry_rate = retry_passed / retry_total if retry_total > 0 else 0.0

        # Hint accuracy = Cat B pass rate (same metric, different name)
        hint_accuracy = cat_b_rate

        # Cost Per Successful Test (CPST) — across ALL executions
        cost_data = self.db_conn.execute(
            "SELECT COALESCE(SUM(llm_cost), 0) as total_cost, "
            "       COALESCE(SUM(CASE WHEN test_passed = 1 "
            "                    THEN 1 ELSE 0 END), 0) as successes "
            "FROM learning_metrics"
        ).fetchone()
        total_cost = cost_data["total_cost"] or 0.0
        total_successes = cost_data["successes"] or 0
        cpst = total_cost / total_successes if total_successes > 0 else 0.0

        # Enough data for meaningful comparison?
        sufficient_data = (
            cat_a_total >= self._min_sample_size
            and cat_b_total >= self._min_sample_size
        )

        return {
            "natural_comparison": {
                "no_hints_available": {
                    "total": cat_a_total,
                    "passed": cat_a_passed,
                    "pass_rate": round(cat_a_rate, 4),
                },
                "hints_injected": {
                    "total": cat_b_total,
                    "passed": cat_b_passed,
                    "pass_rate": round(cat_b_rate, 4),
                },
                "lift": round(lift, 4),
                "sufficient_data": sufficient_data,
            },
            "retry_after_feedback": {
                "total": retry_total,
                "passed": retry_passed,
                "pass_rate": round(retry_rate, 4),
            },
            "hint_accuracy": round(hint_accuracy, 4),
            "cost_per_successful_test": round(cpst, 4),
            "total_executions": cat_a_total + cat_b_total + retry_total,
        }


# ---------------------------------------------------------------------------
# 2. Contradiction Detector
# ---------------------------------------------------------------------------

class ContradictionDetector:
    """
    Scans learned rules for contradiction signals.

    A rule is "contradicted" when its counter-evidence ratio exceeds the
    configured threshold — meaning the rule is wrong more often than
    expected.

    Design principles:
    - Read-only: never modifies rules, only reports
    - Extensible: checker registry pattern — adding a new engine
      requires only adding one method and one registry entry
    - Configurable thresholds via constructor (no hardcoded magic numbers)
    - Efficient: single SQL query per rule type (batch scan)
    - Minimum observation gate: avoids flagging rules with too little data

    Future extensibility:
    - Phase 2 adds domain-specific rules → add _check_domain_rules()
    - Phase 3 adds locator strategies → add _check_locator_rules()
    - External callers can register custom checkers via register_checker()
    """

    DEFAULT_CONTRADICTION_RATIO = 0.4
    DEFAULT_MIN_OBSERVATIONS = 5
    STALENESS_WINDOW_DAYS = 90  # Anti-patterns not seen in this many days are flagged

    def __init__(
        self,
        db_conn,
        contradiction_threshold: float = None,
        min_observations: int = None,
    ):
        """
        Initialize ContradictionDetector.

        Args:
            db_conn: sqlite3.Connection with row_factory=sqlite3.Row
            contradiction_threshold: Ratio above which a rule is flagged
                                     (default 0.4 = 40% contradictions).
            min_observations: Minimum total observations before checking a
                              rule (default 5).
        """
        self.db_conn = db_conn
        self.contradiction_threshold = (
            contradiction_threshold
            if contradiction_threshold is not None
            else self.DEFAULT_CONTRADICTION_RATIO
        )
        self.min_observations = (
            min_observations
            if min_observations is not None
            else self.DEFAULT_MIN_OBSERVATIONS
        )
        # Registry of checker methods — extensible for future engines
        self._checkers: List[tuple] = [
            ("structural", self._check_structural_rules),
            ("anti_pattern", self._check_anti_patterns),
        ]

    def register_checker(self, rule_type: str, checker_fn) -> None:
        """
        Register a custom contradiction checker.

        Args:
            rule_type: Unique name for the rule type (e.g., "domain")
            checker_fn: Callable returning List[dict] of flagged rules
        """
        # Prevent duplicate registration
        existing_types = {rt for rt, _ in self._checkers}
        if rule_type in existing_types:
            logger.warning(
                "[LEARNING] Checker '%s' already registered — skipping",
                rule_type,
            )
            return
        self._checkers.append((rule_type, checker_fn))

    def detect_all(self) -> List[dict]:
        """
        Run all registered contradiction checks.

        Returns:
            List of flagged rule dicts, each containing:
            - rule_type: str (e.g., "structural", "anti_pattern")
            - rule_id: int
            - rule_name: str
            - evidence: int
            - counter_evidence: int
            - contradiction_ratio: float
            - current_score: float
        """
        flagged = []
        for rule_type, checker in self._checkers:
            try:
                flagged.extend(checker())
            except Exception as e:
                logger.warning(
                    "[LEARNING] Contradiction checker '%s' failed: %s",
                    rule_type, e,
                )
        return flagged

    def get_summary(self) -> dict:
        """
        Return contradiction statistics for monitoring.

        Returns dict with:
        - total_flagged: int
        - by_type: dict mapping rule_type → count
        - flagged_rules: List[dict] with details
        """
        flagged = self.detect_all()
        by_type: Dict[str, int] = {}
        for rule_type, _ in self._checkers:
            by_type[rule_type] = len(
                [f for f in flagged if f["rule_type"] == rule_type]
            )
        return {
            "total_flagged": len(flagged),
            "by_type": by_type,
            "flagged_rules": flagged,
        }

    # ------------------------------------------------------------------
    # Built-in Checkers
    # ------------------------------------------------------------------

    def _check_structural_rules(self) -> List[dict]:
        """
        Scan structural_rules for high contradiction ratios.

        A structural rule is flagged when:
        - It has at least min_observations total (evidence + counter_evidence)
        - counter_evidence / total > contradiction_threshold

        Returns list of flagged rule dicts.
        """
        rows = self.db_conn.execute(
            "SELECT id, rule_name, evidence_count, counter_evidence, score "
            "FROM structural_rules "
            "WHERE (evidence_count + counter_evidence) >= ?",
            (self.min_observations,),
        ).fetchall()

        flagged = []
        for row in rows:
            total = row["evidence_count"] + row["counter_evidence"]
            ratio = row["counter_evidence"] / total if total > 0 else 0.0
            if ratio > self.contradiction_threshold:
                flagged.append(self._build_flag(
                    rule_type="structural",
                    rule_id=row["id"],
                    rule_name=row["rule_name"],
                    evidence=row["evidence_count"],
                    counter_evidence=row["counter_evidence"],
                    contradiction_ratio=ratio,
                    current_score=row["score"],
                ))
                logger.info(
                    "[LEARNING] Contradiction detected: structural rule '%s' "
                    "has %.0f%% counter-evidence (%d/%d)",
                    row["rule_name"],
                    ratio * 100,
                    row["counter_evidence"],
                    total,
                )
        return flagged

    def _check_anti_patterns(self) -> List[dict]:
        """
        Scan anti_patterns for staleness.

        In Phase 1, anti-pattern scores only increase (no counter-evidence
        mechanism exists — learn() passes 0 for counter_evidence). Score-based
        contradiction detection requires Phase 2's hint effectiveness tracking.

        Until then, flag STALE patterns: high evidence but not seen for 90+
        days. A stale anti-pattern may be outdated (e.g., framework was
        updated and the failure no longer occurs).

        Returns list of flagged rule dicts.
        """
        rows = self.db_conn.execute(
            "SELECT id, failure_category, query_pattern, "
            "       evidence_count, score, last_seen "
            "FROM anti_patterns "
            "WHERE evidence_count >= ? "
            "AND last_seen < datetime('now', ? || ' days')",
            (self.min_observations, f"-{self.STALENESS_WINDOW_DAYS}"),
        ).fetchall()

        flagged = []
        for row in rows:
            flagged.append(self._build_flag(
                rule_type="anti_pattern",
                rule_id=row["id"],
                rule_name=(
                    f"anti_{row['failure_category']}_{row['id']}"
                ),
                evidence=row["evidence_count"],
                counter_evidence=0,
                contradiction_ratio=0.0,
                current_score=row["score"],
            ))
            logger.info(
                "[LEARNING] Stale anti-pattern detected: '%s' (id=%d) "
                "last seen %s, evidence=%d. May be outdated.",
                row["failure_category"],
                row["id"],
                row["last_seen"],
                row["evidence_count"],
            )
        return flagged

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_flag(
        rule_type: str,
        rule_id: int,
        rule_name: str,
        evidence: int,
        counter_evidence: int,
        contradiction_ratio: float,
        current_score: float,
    ) -> dict:
        """Build a standardized flagged-rule dict."""
        return {
            "rule_type": rule_type,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "evidence": evidence,
            "counter_evidence": counter_evidence,
            "contradiction_ratio": round(contradiction_ratio, 4),
            "current_score": round(current_score, 4),
        }


# ---------------------------------------------------------------------------
# 3. Feedback Loop Orchestrator
# ---------------------------------------------------------------------------

class FeedbackLoop:
    """
    Orchestrates the post-execution learning process.

    Called AFTER every test execution (pass or fail).
    Non-blocking — all writes go through LearningWriteQueue.

    Steps:
    1. Parse output.xml (if exists)
    2. Classify failure (if failed)
    3. Build ExecutionRecord
    4. Store in ExecutionMemory
    5. Route to relevant learning engines
    6. Record learning metrics
    7. Update daily stats
    8. Check for contradictions (Risk 1 mitigation)

    All entry points check circuit breaker first and wrap in try/except.
    """

    def __init__(
        self,
        execution_memory: ExecutionMemory = None,
        failure_analyzer: FailureAnalyzer = None,
        structural_engine=None,
        keyword_engine=None,
        anti_pattern_engine=None,
        pattern_learner: QueryPatternMatcher = None,
        metrics_tracker: LearningMetricsTracker = None,
        contradiction_detector: ContradictionDetector = None,
        write_queue: LearningWriteQueue = None,
        circuit_breaker: LearningCircuitBreaker = None,
    ):
        """
        Initialize FeedbackLoop with optional dependency injection.

        All parameters default to None; when None, real instances are
        constructed using shared database connections.  Pass mocks in
        tests for deterministic behaviour.
        """
        # Core infrastructure
        self.execution_memory = execution_memory or ExecutionMemory()
        self.failure_analyzer = failure_analyzer or FailureAnalyzer()
        self.write_queue = write_queue or LearningWriteQueue()
        self.circuit_breaker = circuit_breaker or LearningCircuitBreaker()

        # Shared db_conn for all engine + tracker construction
        db_conn = self.execution_memory.conn

        # Learning engines (lazy import to avoid circular dependencies)
        if structural_engine is not None:
            self.structural_engine = structural_engine
        else:
            from src.backend.crew_ai.optimization.structural_rule_engine import (
                StructuralRuleEngine,
                IntentExtractor,
            )
            intent_extractor = IntentExtractor(db_conn)
            self.structural_engine = StructuralRuleEngine(
                db_conn, intent_extractor
            )

        if keyword_engine is not None:
            self.keyword_engine = keyword_engine
        else:
            from src.backend.crew_ai.optimization.keyword_correction_engine import (
                KeywordCorrectionEngine,
            )
            self.keyword_engine = KeywordCorrectionEngine(db_conn)

        if anti_pattern_engine is not None:
            self.anti_pattern_engine = anti_pattern_engine
        else:
            from src.backend.crew_ai.optimization.anti_pattern_engine import (
                AntiPatternEngine,
            )
            self.anti_pattern_engine = AntiPatternEngine(db_conn)

        # Pattern learner — keyword-to-query association (ChromaDB only)
        if pattern_learner is not None:
            self.pattern_learner = pattern_learner
        else:
            try:
                from src.backend.crew_ai.optimization.chroma_store import (
                    KeywordVectorStore,
                )
                from src.backend.core.config import settings as app_settings
                chroma_store = KeywordVectorStore(
                    persist_directory=app_settings.OPTIMIZATION_CHROMA_DB_PATH,
                )
                self.pattern_learner = QueryPatternMatcher(
                    chroma_store=chroma_store,
                )
            except Exception as e:
                logger.warning(
                    "[LEARNING] Pattern learner unavailable (non-blocking): %s", e,
                )
                self.pattern_learner = None

        # NL feedback engine — initialized once, reused across calls
        try:
            from src.backend.crew_ai.optimization.nl_feedback_engine import (
                NLFeedbackEngine,
            )
            self.nl_engine = NLFeedbackEngine(db_conn)
        except Exception as e:
            logger.warning(
                "[LEARNING] NLFeedbackEngine unavailable (non-blocking): %s", e,
            )
            self.nl_engine = None

        # Metrics & contradiction detection
        self.metrics_tracker = (
            metrics_tracker or LearningMetricsTracker(db_conn)
        )
        self.contradiction_detector = (
            contradiction_detector or ContradictionDetector(db_conn)
        )

    # ------------------------------------------------------------------
    # Main Entry Point — process_execution
    # ------------------------------------------------------------------

    def process_execution(
        self,
        workflow_id: str,
        user_query: str,
        url: str,
        robot_code: str,
        test_status: str,
        output_xml_path: Optional[str] = None,
        metrics=None,
        is_first_attempt: bool = True,
        hints_available: int = 0,
        hints_injected: int = 0,
        hint_sources: Optional[list] = None,
        is_retry_after_feedback: bool = False,
        attempt_number: int = 1,
        hint_tokens: int = 0,
    ) -> None:
        """
        Main entry point — called from workflow_service.py after execution.

        CRITICAL: This entire method is non-blocking.
        Errors are caught → circuit breaker → logged → pipeline continues.

        Args:
            workflow_id: Unique identifier for this workflow run.
            user_query: Original natural language test query.
            url: Target website URL.
            robot_code: Generated .robot file content.
            test_status: "passed" | "failed" | "error"
            output_xml_path: Path to Robot Framework output.xml (optional).
            metrics: WorkflowMetrics object (optional).
            is_first_attempt: Whether this is the first attempt for this query.
            hints_available: Number of hints available from learning system.
            hints_injected: Number of hints actually injected.
            hint_sources: List of engine names that provided hints.
            is_retry_after_feedback: Whether this execution is a retry
                                     after user feedback.
            attempt_number: Which attempt number this is (1-based).
            hint_tokens: Total tokens used by injected hints.
        """
        if not self.circuit_breaker.is_enabled():
            return

        try:
            # Step 1: Analyze failure (if failed)
            failure_analysis = None
            if test_status == "failed" and output_xml_path:
                failure_analysis = self.failure_analyzer.analyze(
                    output_xml_path=output_xml_path,
                    user_query=user_query,
                    robot_code=robot_code,
                    exit_code=None,
                )

            # Step 2: Build execution record
            record = ExecutionRecord(
                workflow_id=workflow_id,
                timestamp=datetime.now(),
                user_query=user_query,
                url=url,
                domain=extract_domain(url) if url else None,
                robot_code=robot_code,
                code_structure=(
                    CodeStructureExtractor.detect(robot_code)
                    if robot_code else None
                ),
                test_status=test_status,
                failure_category=(
                    failure_analysis.category if failure_analysis else None
                ),
                failed_keyword=(
                    failure_analysis.failed_keyword
                    if failure_analysis else None
                ),
                error_message=(
                    failure_analysis.error_message
                    if failure_analysis else None
                ),
                total_llm_calls=(
                    getattr(metrics, "total_llm_calls", 0)
                    if metrics else 0
                ),
                total_cost=(
                    getattr(metrics, "total_cost", 0.0)
                    if metrics else 0.0
                ),
            )

            # Step 3: Store via write queue (non-blocking)
            self.write_queue.submit(self.execution_memory.store, record)

            # Step 4: Route to learning engines (non-blocking)
            self.write_queue.submit(self.structural_engine.learn, record)
            self.write_queue.submit(self.keyword_engine.learn, record)
            self.write_queue.submit(self.anti_pattern_engine.learn, record)

            # Step 4b: Pattern learning — keyword-to-query association
            # Only learn from PASSED tests to avoid learning wrong keyword mappings
            if test_status == "passed" and self.pattern_learner is not None:
                self.write_queue.submit(
                    self.pattern_learner.learn_from_execution,
                    user_query, robot_code,
                )

            # Step 5: Record learning metrics (non-blocking)
            self.write_queue.submit(
                self.metrics_tracker.record_execution,
                workflow_id=workflow_id,
                user_query=user_query,
                is_first_attempt=is_first_attempt,
                hints_available=hints_available,
                hints_injected=hints_injected,
                hint_sources=hint_sources or [],
                llm_calls=(
                    getattr(metrics, "total_llm_calls", 0)
                    if metrics else 0
                ),
                llm_cost=(
                    getattr(metrics, "total_cost", 0.0)
                    if metrics else 0.0
                ),
                test_passed=test_status == "passed",
                is_retry_after_feedback=is_retry_after_feedback,
                attempt_number=attempt_number,
                hint_tokens=hint_tokens,
            )

            # Step 6: Update daily stats (non-blocking)
            self.write_queue.submit(
                self.execution_memory.update_daily_stats, test_status
            )

            self.circuit_breaker.record_success()

            logger.info(
                "[LEARNING] Processed execution %s: status=%s, "
                "hints_injected=%d",
                workflow_id, test_status, hints_injected,
            )

            # Step 7: Update NL feedback hint effectiveness (non-blocking)
            if self.nl_engine is not None:
                try:
                    domain = extract_domain(url) if url else None
                    failure_cat = (
                        failure_analysis.category
                        if failure_analysis else None
                    )
                    self.write_queue.submit(
                        self.nl_engine.update_hint_effectiveness,
                        domain=domain,
                        url=url,
                        test_passed=(test_status == "passed"),
                        new_failure_category=failure_cat,
                    )
                except Exception as e:
                    logger.warning(
                        "[LEARNING] NL hint effectiveness update failed: %s", e,
                    )

        except Exception as e:
            self.circuit_breaker.record_error(e)
            logger.warning(
                "[LEARNING] process_execution error (non-blocking): %s", e
            )

    # ------------------------------------------------------------------
    # User Feedback
    # ------------------------------------------------------------------

    def process_user_feedback(
        self,
        workflow_id: str,
        feedback_text: str,
        feedback_type: str,
    ) -> dict:
        """
        Process user NL feedback from the feedback UI.

        Pipeline:
        1. Store raw feedback in execution record
        2. Run NL triage (seed patterns → category + confidence)
        3. Route triaged insight to all engines via learn_from_feedback
        4. Return triage result for frontend display

        Called from: /api/feedback endpoint (DAY_08).

        Args:
            workflow_id: The workflow this feedback applies to.
            feedback_text: User's natural language feedback.
            feedback_type: "close_enough" | "completely_wrong"

        Returns:
            Triage result dict with category, confidence, taxonomy_code.
        """
        _fallback_triage = {
            "category": "uncategorized",
            "specific_type": "triage_unavailable",
            "confidence": 0.0,
            "taxonomy_code": "X0",
            "matched_patterns": [],
        }

        if not self.circuit_breaker.is_enabled():
            return _fallback_triage

        try:
            # Step 1: Store raw feedback
            self.write_queue.submit(
                self.execution_memory.update_user_feedback,
                workflow_id, feedback_text, feedback_type,
            )

            # Step 2: Get execution record for context
            record = self.execution_memory.get(workflow_id)
            error_message = (
                record.error_message if record else None
            )

            # Step 3: NL triage
            if self.nl_engine is not None:
                try:
                    triage = self.nl_engine.process_feedback(
                        workflow_id, feedback_text, feedback_type,
                        error_message=error_message,
                    )
                except Exception as e:
                    logger.warning("[LEARNING] NL triage failed: %s", e)
                    triage = _fallback_triage
            else:
                triage = _fallback_triage

            # Step 4: Route to engines via learn_from_feedback
            if record:
                # Inject raw feedback text so engines can access it
                triage["feedback_text"] = feedback_text

                engines = [
                    self.structural_engine,
                    self.keyword_engine,
                    self.anti_pattern_engine,
                ]
                if self.nl_engine is not None:
                    engines.append(self.nl_engine)

                for engine in engines:
                    try:
                        self.write_queue.submit(
                            engine.learn_from_feedback, record, triage,
                        )
                    except Exception as e:
                        logger.warning(
                            "[LEARNING] Engine routing failed for %s: %s",
                            type(engine).__name__, e,
                        )
            else:
                logger.warning(
                    "[LEARNING] No execution record for feedback: %s "
                    "(triage completed without engine routing)",
                    workflow_id,
                )

            logger.info(
                "[LEARNING] Feedback processed for %s: type=%s "
                "category=%s confidence=%.2f",
                workflow_id, feedback_type,
                triage.get("category", "?"),
                triage.get("confidence", 0.0),
            )

            self.circuit_breaker.record_success()
            return triage

        except Exception as e:
            self.circuit_breaker.record_error(e)
            logger.warning(
                "[LEARNING] process_user_feedback error (non-blocking): %s",
                e,
            )
            return _fallback_triage

    # ------------------------------------------------------------------
    # Stats & Monitoring
    # ------------------------------------------------------------------

    def get_learning_stats(self) -> dict:
        """
        Return comprehensive learning system statistics.

        Aggregates stats from all engines, the metrics tracker, and
        the contradiction detector into a single snapshot.
        """
        stats = {
            "structural_rules": self.structural_engine.get_stats(),
            "keyword_corrections": self.keyword_engine.get_stats(),
            "anti_patterns": self.anti_pattern_engine.get_stats(),
            "learning_effectiveness": (
                self.metrics_tracker.get_effectiveness_report()
            ),
            "contradictions": self.contradiction_detector.get_summary(),
            "total_records": self.execution_memory.get_total_records(),
            "circuit_breaker": self.circuit_breaker.get_stats(),
        }
        if self.nl_engine is not None:
            stats["nl_feedback"] = self.nl_engine.get_stats()
        return stats
