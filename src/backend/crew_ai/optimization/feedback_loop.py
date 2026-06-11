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

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict

from src.backend.crew_ai.llm_provider_routing import resolve_model_string
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
from src.backend.crew_ai.optimization.postgres_execution_memory import (
    PostgresExecutionMemory,
)
from src.backend.crew_ai.optimization.failure_analyzer import FailureAnalyzer
from src.backend.crew_ai.optimization.pattern_learning import QueryPatternMatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# D3 — Write-queue drain helper
# ---------------------------------------------------------------------------

def _get_with_retry(
    em,
    workflow_id: str,
    max_attempts: int = 3,
    base_ms: int = 100,
):
    """Retry em.get() to handle write-queue drain lag.

    process_user_feedback is offloaded to an asyncio.to_thread worker by the
    async /api/feedback handler, so this runs on a worker thread (never the
    event loop) — the sleep below is safe.  The execution record is written by
    the learning-writer thread via write_queue.submit() inside
    _process_learning(), which starts only AFTER the execution result SSE is
    already sent to the client.  A user (or automated caller) can therefore
    submit feedback before the INSERT is committed.

    Sleeping base_ms * 2^attempt between attempts gives the writer thread time
    to drain.  Max wait: 100ms + 200ms = 300ms across 3 attempts — well within
    the MAX_CONCURRENT_WORKFLOWS=10 worst-case queue depth of ~160ms.
    Returns None after all attempts (same as the no-retry path), so the caller
    degrades gracefully with an existing warning log.
    """
    for attempt in range(max_attempts):
        record = em.get(workflow_id)
        if record is not None:
            return record
        if attempt < max_attempts - 1:
            time.sleep(base_ms / 1000 * (2 ** attempt))
    return None


# ---------------------------------------------------------------------------
# Trigger 2 — LLM conflict-detection prompt builder
# ---------------------------------------------------------------------------

def _build_conflict_prompt_with_feedback(
    robot_code: str,
    feedback_text: str,
    active_hints: list,
    working_code: Optional[str] = None,
    domain: str | None = None,
    url: str | None = None,
    user_query: str | None = None,
) -> str:
    """LLM prompt for Trigger 2 — conflict detection at feedback submission.

    State A (working_code provided): shows both failed code and corrected
    code. Code evidence (v1 vs v2 diff) is the primary signal; feedback
    text is interpretation of that diff. Only flag when code corroborates.

    State B (working_code is None): shows only the code that was in use
    when the test failed. The corrected-code section is omitted entirely
    — no empty block, no LLM confusion.

    Design principles:
    - State A: code diff is the strongest signal; feedback interprets it.
    - State B: feedback is primary; code is corroborating context.
    - Conservative: when uncertain, preserve. A hint wrongly flagged loses
      accumulated learning with no automatic recovery.
    - Non-prescriptive: state the system, the evidence, the stakes —
      let the LLM derive its analysis method.
    """
    from src.backend.crew_ai.optimization.conflict_detection import (
        _hint_line,
        _build_context_prefix,
    )

    hint_lines = "\n".join(_hint_line(h) for h in active_hints)
    context_prefix = _build_context_prefix(domain, url, user_query)

    code_section = (
        "ROBOT FRAMEWORK CODE (v1 — the auto-generated code that was in use when "
        "the test failed):\n"
        "```\n"
        f"{robot_code.strip()}\n"
        "```\n"
    )
    if working_code:
        code_section += (
            "\nCORRECTED CODE (v2 — developer's manual fix that passed against "
            "the real system):\n"
            "```\n"
            f"{working_code.strip()}\n"
            "```\n"
        )

    if working_code:
        signal_instruction = (
            "Signal priority for this judgment:\n"
            "1. The v1 vs v2 code diff is the strongest evidence — v2 actually passed "
            "against the real system.\n"
            "2. The user's feedback interprets the diff and the user's intent. Use it to "
            "disambiguate when multiple things changed. Do NOT flag a hint based on "
            "feedback alone if the v1 vs v2 diff does not bear it out.\n"
            "3. A hint whose advice agrees with the user's feedback is REINFORCEMENT, not "
            "a conflict. If the feedback and an existing hint say the same thing in "
            "different words, do NOT flag that hint — the feedback is confirming it, "
            "not contradicting it.\n"
        )
    else:
        signal_instruction = (
            "The user's feedback text is the primary statement of what was wrong. "
            "Use the code as corroborating context to verify the described problem — "
            "not as an independent source of additional changes to act on.\n"
            "A hint whose advice agrees with the user's feedback is REINFORCEMENT, not "
            "a conflict. If the feedback and an existing hint say the same thing in "
            "different words, do NOT flag that hint — the feedback is confirming it, "
            "not contradicting it.\n"
        )

    return (
        "You are the conflict detection component of an adaptive test automation "
        "learning system.\n\n"
        "This system auto-generates Robot Framework test code guided by learned hints "
        "from past human corrections. The hints listed below were injected into the "
        "agents when this test was generated. When a developer reports that something "
        "in the generated output was wrong, the system identifies which injected hints "
        "directly conflict with that feedback — those hints, if applied in future test "
        "generation, would reproduce the same mistake.\n\n"
        f"{context_prefix}\n"
        "EXAMPLES — for reference only, do not respond to these:\n\n"
        "Example 1 — DO flag (feedback explicitly contradicts hint; code corroborates):\n"
        "  Active hint:\n"
        "    [7] Use SeleniumLibrary's `Open Browser` keyword on this domain.\n"
        "  v1 (failed):\n"
        "    Open Browser    https://example.com    chrome\n"
        "    Click           id=cta\n"
        "  v2 (passed):\n"
        "    New Browser    chromium\n"
        "    New Page       https://example.com\n"
        "    Click          id=cta\n"
        '  User feedback: "This site needs Browser Library — SeleniumLibrary doesn\'t'
        ' handle the SPA navigation reliably."\n'
        '  Verdict: {"flag":[{"id":7,"reason":"Feedback explicitly states the switch'
        " from SeleniumLibrary to Browser Library; v1 to v2 diff corroborates."
        ' The Open Browser approach was the failing approach."}]}\n\n'
        "Example 2 — DO NOT flag (hint agrees with feedback — reinforcement, not conflict):\n"
        "  Active hint:\n"
        "    [12] Wait for elements with state=stable before clicking dynamic content.\n"
        "  v1 (failed):\n"
        "    Click    css=#load-more\n"
        "  v2 (passed):\n"
        "    Wait For Elements State    css=#load-more    stable\n"
        "    Click                      css=#load-more\n"
        '  User feedback: "Need to wait for the element to settle before clicking."\n'
        '  Verdict: {"flag":[]}\n\n'
        "Example 3 — DO NOT flag (feedback too vague to link to any specific hint):\n"
        "  Active hints:\n"
        "    [3] Use Browser Library for this domain.\n"
        "    [8] Use data-testid locators on this domain.\n"
        '  User feedback: "Still doesn\'t work."\n'
        '  Verdict: {"flag":[]}\n\n'
        f"{code_section}\n"
        "USER FEEDBACK (developer's statement about what was wrong or what they changed):\n"
        f'"{feedback_text.strip()}"\n\n'
        "INJECTED HINTS (used at generation time — numbers in brackets are unique hint IDs):\n"
        f"{hint_lines}\n\n"
        f"{signal_instruction}\n"
        "Only flag a hint when you are confident its specific advice directly contradicts "
        "the evidence. If the feedback is too vague to link confidently to a specific "
        "hint, if the connection is indirect or ambiguous, or if the hint's advice is not "
        "clearly reflected in the code shown, do not flag. Preservation is always the "
        "safer choice: a hint that survives incorrectly will be gradually downscored by "
        "future executions; a hint wrongly flagged loses accumulated learning with no "
        "automatic recovery.\n\n"
        "Each reason must specifically address THIS hint's advice and how the evidence "
        "disproves it. Do NOT write generic reasons that could apply to multiple hints. "
        "If you cannot articulate a hint-specific reason, do not flag that hint.\n\n"
        "Respond with ONLY valid JSON:\n"
        '{"flag": [{"id": <int>, "reason": "<explanation specific to this hint>"}, ...]}\n'
        'If no hints should be flagged: {"flag": []}'
    )


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

    def __init__(self, execution_memory=None):
        """
        Initialize LearningMetricsTracker.

        Args:
            execution_memory: ExecutionMemory instance. Write methods
                              use _writer_conn; reads use read_conn().
        """
        self._em = execution_memory
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
        was_holdout: bool = False,
    ) -> None:
        """Record execution with full hint context and cost data.

        was_holdout: True when the R7 random-holdout suppressed
        otherwise-available hints for this run (Category C). hints_injected
        is 0 for such a run, but hints_available still reflects the real
        pre-suppression count — that is what distinguishes Cat C from Cat A.
        """
        if not self._em:
            return
        from src.backend.crew_ai.optimization.execution_memory import _assert_writer_thread
        _assert_writer_thread("LearningMetricsTracker.record_execution")
        self._em._writer_conn.execute(
            "INSERT INTO learning_metrics "
            "(workflow_id, user_query, is_first_attempt, "
            " is_retry_after_feedback, attempt_number, "
            " hints_available, hints_injected, hint_sources, "
            " llm_calls, llm_cost, hint_tokens, test_passed, was_holdout, "
            " timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))",
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
                1 if was_holdout else 0,
            ),
        )
        self._em._writer_conn.commit()
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
        if not self._em:
            return {
                "natural_comparison": {
                    "no_hints_available": {"total": 0, "passed": 0, "pass_rate": 0.0},
                    "hints_injected": {"total": 0, "passed": 0, "pass_rate": 0.0},
                    "holdout_suppressed": {"total": 0, "passed": 0, "pass_rate": 0.0},
                    "lift": 0.0, "lift_is_biased": True, "honest_lift": None,
                    "sufficient_data": False,
                },
                "retry_after_feedback": {"total": 0, "passed": 0, "pass_rate": 0.0},
                "hint_accuracy": 0.0, "cost_per_successful_test": 0.0,
                "total_executions": 0,
            }

        with self._em.read_conn() as conn:
            # Category A: No hints injected, first attempt (organic baseline)
            # Category A: organic baseline — no hints injected, first attempt,
            # and NOT an R7 holdout run. A holdout run also has
            # hints_injected = 0, but it is "hints existed, suppressed" — the
            # opposite of "no hints existed" — so it must be excluded here or
            # it corrupts the baseline.
            cat_a = conn.execute(
                "SELECT COUNT(*) as total, "
                "       COALESCE(SUM(test_passed), 0) as passed "
                "FROM learning_metrics "
                "WHERE hints_injected = 0 AND is_first_attempt = 1 "
                "AND was_holdout = 0"
            ).fetchone()

            # Category B: Hints were injected, first attempt
            cat_b = conn.execute(
                "SELECT COUNT(*) as total, "
                "       COALESCE(SUM(test_passed), 0) as passed "
                "FROM learning_metrics "
                "WHERE hints_injected > 0 AND is_first_attempt = 1"
            ).fetchone()

            # Category C: R7 holdout — hints were available but suppressed.
            # The unbiased control group for honest_lift (Cat B - Cat C).
            cat_c = conn.execute(
                "SELECT COUNT(*) as total, "
                "       COALESCE(SUM(test_passed), 0) as passed "
                "FROM learning_metrics "
                "WHERE was_holdout = 1 AND is_first_attempt = 1"
            ).fetchone()

            # Retry-after-feedback success rate
            retry = conn.execute(
                "SELECT COUNT(*) as total, "
                "       COALESCE(SUM(test_passed), 0) as passed "
                "FROM learning_metrics "
                "WHERE is_retry_after_feedback = 1"
            ).fetchone()

            # Cost Per Successful Test (CPST) — across ALL executions
            cost_data = conn.execute(
                "SELECT COALESCE(SUM(llm_cost), 0) as total_cost, "
                "       COALESCE(SUM(CASE WHEN test_passed = 1 "
                "                    THEN 1 ELSE 0 END), 0) as successes "
                "FROM learning_metrics"
            ).fetchone()

        cat_a_total = cat_a["total"] or 0
        cat_a_passed = cat_a["passed"] or 0
        cat_b_total = cat_b["total"] or 0
        cat_b_passed = cat_b["passed"] or 0
        cat_c_total = cat_c["total"] or 0
        cat_c_passed = cat_c["passed"] or 0

        cat_a_rate = cat_a_passed / cat_a_total if cat_a_total > 0 else 0.0
        cat_b_rate = cat_b_passed / cat_b_total if cat_b_total > 0 else 0.0
        cat_c_rate = cat_c_passed / cat_c_total if cat_c_total > 0 else 0.0
        # Biased lift (B - A): Cat B queries resemble past successes, so this
        # flatters the learning system. Kept for continuity, labelled biased.
        lift = cat_b_rate - cat_a_rate
        # Honest lift (B - C): Cat C is the unbiased control. Emitted as None
        # until Cat C has enough samples — it accrues at only ~5% of
        # hint-available runs, so cat_c_rate is noise until hundreds have run.
        # Without the None guard, cat_b_rate - 0.0 would read as a large fake
        # positive lift before any holdout data exists (C15).
        honest_lift = (
            round(cat_b_rate - cat_c_rate, 4)
            if cat_c_total >= self._min_sample_size
            else None
        )

        retry_total = retry["total"] or 0
        retry_passed = retry["passed"] or 0
        retry_rate = retry_passed / retry_total if retry_total > 0 else 0.0

        # Hint accuracy = Cat B pass rate (same metric, different name)
        hint_accuracy = cat_b_rate

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
                "holdout_suppressed": {
                    "total": cat_c_total,
                    "passed": cat_c_passed,
                    "pass_rate": round(cat_c_rate, 4),
                },
                "lift": round(lift, 4),
                "lift_is_biased": True,
                "honest_lift": honest_lift,
                "sufficient_data": sufficient_data,
            },
            "retry_after_feedback": {
                "total": retry_total,
                "passed": retry_passed,
                "pass_rate": round(retry_rate, 4),
            },
            "hint_accuracy": round(hint_accuracy, 4),
            "cost_per_successful_test": round(cpst, 4),
            "total_executions": (
                cat_a_total + cat_b_total + cat_c_total + retry_total
            ),
        }


# ---------------------------------------------------------------------------
# 2. Contradiction Detector
# ---------------------------------------------------------------------------

class ContradictionDetector:
    """Scans learned rules for contradiction signals.

    A rule is "contradicted" when its counter-evidence ratio exceeds the configured
    threshold.  Read-only — never modifies rules, only reports.
    """

    DEFAULT_CONTRADICTION_RATIO = 0.4
    DEFAULT_MIN_OBSERVATIONS = 5
    STALENESS_WINDOW_DAYS = 90  # Anti-patterns not seen in this many days are flagged

    def __init__(
        self,
        execution_memory=None,
        contradiction_threshold: float = None,
        min_observations: int = None,
    ):
        self._em = execution_memory
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

    def detect_all(self) -> List[dict]:
        """Run both contradiction checks, returning flagged rule dicts."""
        flagged = []
        for checker in (self._check_structural_rules, self._check_anti_patterns):
            try:
                flagged.extend(checker())
            except Exception as e:
                logger.warning(
                    "[LEARNING] Contradiction checker '%s' failed: %s",
                    getattr(checker, "__name__", repr(checker)), e,
                )
        return flagged

    def get_summary(self) -> dict:
        """Return contradiction statistics for monitoring."""
        flagged = self.detect_all()
        return {
            "total_flagged": len(flagged),
            "by_type": {
                "structural": sum(1 for f in flagged if f["rule_type"] == "structural"),
                "anti_pattern": sum(1 for f in flagged if f["rule_type"] == "anti_pattern"),
            },
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
        if not self._em:
            return []
        with self._em.read_conn() as conn:
            rows = conn.execute(
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
        if not self._em:
            return []
        with self._em.read_conn() as conn:
            rows = conn.execute(
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
        # Core infrastructure. Phase 4 cutover: the learning store is now
        # PostgreSQL + pgvector (PostgresExecutionMemory); the legacy SQLite
        # ExecutionMemory is injected only by tests that still target it.
        self.execution_memory = execution_memory or PostgresExecutionMemory()
        self.failure_analyzer = failure_analyzer or FailureAnalyzer()
        self.write_queue = write_queue or LearningWriteQueue()
        self.circuit_breaker = circuit_breaker or LearningCircuitBreaker()

        # Shared ExecutionMemory for all engine + tracker construction
        em = self.execution_memory

        # Learning engines (lazy import to avoid circular dependencies)
        if structural_engine is not None:
            self.structural_engine = structural_engine
        else:
            from src.backend.crew_ai.optimization.structural_rule_engine import (
                StructuralRuleEngine,
                IntentExtractor,
            )
            intent_extractor = IntentExtractor(em)
            self.structural_engine = StructuralRuleEngine(
                em, intent_extractor
            )

        if keyword_engine is not None:
            self.keyword_engine = keyword_engine
        else:
            from src.backend.crew_ai.optimization.keyword_correction_engine import (
                KeywordCorrectionEngine,
            )
            self.keyword_engine = KeywordCorrectionEngine(em)

        if anti_pattern_engine is not None:
            self.anti_pattern_engine = anti_pattern_engine
        else:
            from src.backend.crew_ai.optimization.anti_pattern_engine import (
                AntiPatternEngine,
            )
            self.anti_pattern_engine = AntiPatternEngine(em)

        # Pattern learner — keyword-to-query association (ChromaDB only)
        if pattern_learner is not None:
            self.pattern_learner = pattern_learner
        else:
            try:
                from src.backend.crew_ai.optimization.keyword_vector_store import (
                    get_keyword_vector_store,
                )
                # Shared process-wide store — run_crew reuses this same instance
                # instead of opening a second pool per workflow.
                chroma_store = get_keyword_vector_store()
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
            self.nl_engine = NLFeedbackEngine(em)
        except Exception as e:
            logger.warning(
                "[LEARNING] NLFeedbackEngine unavailable (non-blocking): %s", e,
            )
            self.nl_engine = None

        # Metrics & contradiction detection
        self.metrics_tracker = (
            metrics_tracker or LearningMetricsTracker(em)
        )
        self.contradiction_detector = (
            contradiction_detector or ContradictionDetector(em)
        )

        # C4: count times crew.py caught an optimization-init failure and fell back.
        # Exposed via get_learning_stats() so operators can detect silent degradation.
        self._optimization_init_failures: int = 0

        # Current-state flag (NOT a counter): True when crew.py's most recent
        # optimization-init succeeded, False when it fell back. Drives the
        # health roll-up; crew.py sets it. Default True — no init seen yet.
        self._optimization_init_ok: bool = True

        # Outcome of the last learning_anchors reconcile — {ran_at, checked,
        # missing_count} — or None until it has run. Read by get_health_status().
        self.last_reconcile: Optional[dict] = None

        # Submit the one-time anchor reconcile to the writer thread. Lazy by
        # construction: FeedbackLoop is built lazily and the reconcile runs on
        # the writer thread, so ExecutionMemory's deferred ONNX load is
        # respected (no eager 80 MB model load at process startup).
        self.write_queue.submit(self._run_anchor_reconcile)

        # Recover hint_review_sessions that were left in 'pending_llm' by a
        # previous process that died mid-_run_hint_review (SIGKILL / OOM /
        # container restart). Without this, start_hint_review's BEGIN IMMEDIATE
        # COUNT would observe the stale row and reject all future reviews
        # forever. Safe because deployment is single-worker uvicorn — a new
        # process means no in-process daemon thread is still working on the
        # row. Multi-worker would need a heartbeat instead.
        self.write_queue.submit(self._recover_stale_review_sessions)

    def _run_anchor_reconcile(self) -> None:
        """Run the learning_anchors reconcile and record its outcome.

        Submitted to LearningWriteQueue once on FeedbackLoop init, so it runs
        on the writer thread. Best-effort — a failure is logged and swallowed
        (learning must never block); the next process restart re-runs it.
        """
        try:
            self.last_reconcile = self.execution_memory.reconcile_anchors()
        except Exception as e:
            logger.warning(
                "[LEARNING] anchor reconcile failed (non-blocking): %s", e
            )

    def _recover_stale_review_sessions(self) -> None:
        """Mark any pending_llm hint_review_sessions as failed at startup.

        Runs on the writer thread via LearningWriteQueue. If the previous
        process died mid-_run_hint_review, its session row stays at
        'pending_llm' forever — and start_hint_review's COUNT gate would
        permanently reject all new reviews. This one-shot UPDATE clears
        such rows so the system self-heals on the next start.
        """
        from src.backend.crew_ai.optimization.execution_memory import _assert_writer_thread
        _assert_writer_thread("FeedbackLoop._recover_stale_review_sessions")
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cursor = self.execution_memory._writer_conn.execute(
                "UPDATE hint_review_sessions "
                "SET status = 'failed', "
                "    error_message = 'process died before completion "
                "(auto-recovered at startup)', "
                "    completed_at = ? "
                "WHERE status = 'pending_llm'",
                (now,),
            )
            self.execution_memory._writer_conn.commit()
            if cursor.rowcount:
                logger.warning(
                    "[LEARNING] Recovered %d stale pending_llm review "
                    "session(s) from a previous crashed process",
                    cursor.rowcount,
                )
        except Exception as e:
            logger.warning(
                "[LEARNING] stale-review-session recovery failed "
                "(non-blocking): %s", e
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
        injected_hint_ids: Optional[str] = None,
        was_holdout: bool = False,
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
        from src.backend.core.config import settings
        if not settings.OPTIMIZATION_ENABLED or not self.circuit_breaker.is_enabled():
            return

        # Engine-boundary enforcement of the CLAUDE.md invariant: learning
        # must be skipped when user_query is empty (paste-and-execute mode)
        # to avoid polluting embeddings. Callers (workflow_service._process_learning
        # today) are expected to guard this first; reaching here means a
        # caller violated the invariant — surface it loudly so the violation
        # is fixed at its source rather than silently swallowed.
        if not user_query or not user_query.strip():
            logger.warning(
                "[LEARNING] process_execution called with empty user_query for "
                "workflow_id=%s — skipping to protect embedding integrity. "
                "Callers must guard empty queries before invoking this method.",
                workflow_id,
            )
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

            # Step 2: Build execution record. model_version is a reporting
            # dimension only — use resolve_model_string so the prefix matches
            # what LiteLLM actually routes on (vertex_ai/..., ollama/...) and
            # duplicate provider prefixes in the env var are stripped.
            selected_model = (
                settings.LOCAL_MODEL
                if settings.MODEL_PROVIDER == "local"
                else settings.ONLINE_MODEL
            )
            model_version = resolve_model_string(
                settings.MODEL_PROVIDER,
                selected_model,
            )
            record = ExecutionRecord(
                workflow_id=workflow_id,
                # UTC, tz-aware: stored as ISO text and compared against the
                # UTC cutoffs the dashboard queries use — local time would skew
                # every window by the host's UTC offset.
                timestamp=datetime.now(timezone.utc),
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
                injected_hint_ids=injected_hint_ids,
                model_version=model_version,
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
                was_holdout=was_holdout,
            )

            # Step 6: Update daily stats (non-blocking)
            self.write_queue.submit(
                self.execution_memory.update_daily_stats, test_status
            )

            self.circuit_breaker.record_success()

            safe_wid = (
                workflow_id if re.match(r'^[a-zA-Z0-9_-]+$', workflow_id)
                else "[b64]" + base64.b64encode(workflow_id.encode('UTF-8')).decode()
            )
            logger.info(
                "[LEARNING] Processed execution %s: status=%s, "
                "hints_injected=%d",
                safe_wid, test_status, hints_injected,
            )

            # NL-hint usage attribution (Part 2) runs from
            # workflow_service._process_learning AFTER process_execution, not
            # here: it credits only the hints actually used in passing code via
            # one LLM judgment per workflow. The old Step-7 all-injected
            # per-execution crediting was removed with that change.

            # Health check — its own try/except so a monitoring failure can
            # never reach the outer except and trip the circuit breaker
            # (disabling all learning because a status read threw).
            try:
                status = self.get_health_status()
                if status in ("DEGRADED", "FAILED"):
                    logger.error(
                        "[LEARNING] Health degraded after execution: %s",
                        status,
                    )
            except Exception as hc_err:
                logger.warning(
                    "[LEARNING] Health check failed (non-blocking): %s", hc_err
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

            # Step 2: Get execution record for context (retry handles write-queue lag)
            record = _get_with_retry(self.execution_memory, workflow_id)
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

            # Step 3b: Trigger 2 — LLM conflict detection at feedback submission.
            #
            # Fires BEFORE the engines routing loop (Step 4) so that if the
            # user's feedback contradicts an active hint, that hint is
            # conflict_flagged FIRST. If the user re-submitted identical text,
            # the subsequent learn_from_feedback UPSERT path (Gap 7 / Step 6)
            # will clear the flag for the exact hint matching the user's text —
            # user wins over LLM judgment on direct overrides.
            #
            # `self.nl_engine is not None` is the first condition: if absent,
            # zero work is performed, no LLM call, no cost, no crash. Same
            # nl_engine guard the usage-attribution path applies.
            #
            # No regex negation gate — natural language is unbounded; the
            # `if active_hints:` DB check is the gate. Cost per call is small
            # (~$0.0001 at Gemini 2.5 Flash); the maintenance tax of regex
            # heuristics is not worth the saving.
            #
            # Empty text IS gated: the one-click verdicts (P0/N0 fast paths,
            # e.g. the failure-path Skip) carry no words to judge hints
            # against, so the LLM call would be pure waste. Code-evidence
            # conflict detection for those workflows still happens at
            # execution time via Trigger 1.
            if (
                self.nl_engine is not None
                and record is not None
                and record.robot_code
                and feedback_text.strip()
            ):
                from src.backend.crew_ai.optimization.conflict_detection import (
                    fire_conflict_detection,
                )

                domain = extract_domain(record.url) if record.url else None
                fire_conflict_detection(
                    feedback_loop=self,
                    trigger_type="trigger_2",
                    workflow_id=record.workflow_id,
                    domain=domain,
                    url=record.url,
                    feedback_text=feedback_text,
                    injected_hint_ids=record.injected_hint_ids,
                    prompt_builder=lambda active_hints: _build_conflict_prompt_with_feedback(
                        robot_code=record.robot_code,
                        feedback_text=feedback_text,
                        active_hints=active_hints,
                        working_code=record.working_code,
                        domain=domain,
                        url=record.url,
                        user_query=record.user_query,
                    ),
                )

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
            safe_wid = (
                workflow_id if re.match(r'^[a-zA-Z0-9_-]+$', workflow_id)
                else "[b64]" + base64.b64encode(workflow_id.encode('UTF-8')).decode()
            )

            if not record:
                logger.warning(
                    "[LEARNING] No execution record for feedback: %s "
                    "(triage completed without engine routing)",
                    safe_wid,
                )
            logger.info(
                "[LEARNING] Feedback processed for %s: type=%s "
                "category=%s confidence=%.2f",
                safe_wid, feedback_type,
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
    # Trigger Telemetry — write path used by Trigger 1 and Trigger 2
    # ------------------------------------------------------------------

    def write_trigger_event(
        self,
        *,
        trigger_type: str,
        workflow_id: Optional[str],
        domain: Optional[str],
        url: Optional[str],
        feedback_text: Optional[str],
        active_hint_ids: list,
        flagged_hint_ids: list,
        actually_flagged_hint_ids: list,
        reason: Optional[str],
        llm_model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        llm_latency_ms: int,
        status: str,
        error_message: Optional[str],
        used_hint_ids: Optional[list] = None,
        unused_hint_ids: Optional[list] = None,
    ) -> None:
        """Insert one row into the trigger_events audit log.

        Runs inside the LearningWriteQueue worker thread — callers MUST
        submit via `self.write_queue.submit(self.write_trigger_event, ...)`,
        never call directly from the request path. The queue's per-item
        try/except absorbs any failure here so telemetry never blocks the
        pipeline (a missing row is acceptable; a stalled LLM trigger is not).
        Keyword-only signature guards against positional-arg drift across
        the two call sites.

        Status taxonomy (matches Phase 2 stats SQL discriminators):
            succeeded         — LLM responded with valid JSON
            no_active_hints   — early return, no LLM call, zero tokens
            llm_timeout       — litellm.completion raised on timeout
            llm_error         — litellm.completion raised for any other reason
            json_parse_failed — LLM responded but JSON decode failed

        flagged_hint_ids vs actually_flagged_hint_ids (schema v12 split):
            flagged_hint_ids         — what the LLM recommended flagging
                (read by _compute_exonerations for weekly-review LLM-judgment
                scoring; carries the full recommendation including strong-
                history-suppressed hints)
            actually_flagged_hint_ids — what was actually committed to
                nl_feedback_corrections.conflict_flagged=1 by
                conflict_flag_hints (read by engagement_rate / reversal_rate
                KPIs so suppressed-only triggers do not inflate denominators)
        Pass [] (not None) for both on early-return / failure paths so the
        column is queryable as a JSON array on every row.

        used_hint_ids / unused_hint_ids (usage-attribution telemetry, Part 2):
            the hints the attribution LLM judged used / unused. Keyword-only with
            a None default so the trigger_1 / trigger_2 callers stay unchanged.
            None is written as SQL NULL (NOT the string "null" — json.dumps(None)
            == "null"), so a row from a non-attribution trigger is NULL here.
        """
        from datetime import datetime, timezone
        from src.backend.crew_ai.optimization.execution_memory import _assert_writer_thread
        _assert_writer_thread("FeedbackLoop.write_trigger_event")
        self.execution_memory._writer_conn.execute(
            """
            INSERT INTO trigger_events (
                trigger_type, workflow_id, domain, url, feedback_text,
                active_hint_ids, flagged_hint_ids, actually_flagged_hint_ids,
                reason, llm_model,
                input_tokens, output_tokens, llm_latency_ms,
                status, error_message, used_hint_ids, unused_hint_ids, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger_type, workflow_id, domain, url, feedback_text,
                json.dumps(active_hint_ids),
                json.dumps(flagged_hint_ids),
                json.dumps(actually_flagged_hint_ids),
                reason, llm_model,
                input_tokens, output_tokens, llm_latency_ms,
                status, error_message,
                json.dumps(used_hint_ids) if used_hint_ids is not None else None,
                json.dumps(unused_hint_ids) if unused_hint_ids is not None else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.execution_memory._writer_conn.commit()

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
            "execution_memory": {
                "chromadb_available": self.execution_memory._chromadb_available,
                "chromadb_last_error": self.execution_memory._chroma_last_error,
                "chromadb_failed_at": self.execution_memory._chroma_failed_at,
            },
            "optimization_init_failures": self._optimization_init_failures,
        }
        if self.nl_engine is not None:
            stats["nl_feedback"] = self.nl_engine.get_stats()
        return stats

    def get_health_status(self) -> str:
        """Roll the learning system's reliability signals into one status:
        OK / DEGRADED / FAILED / DISABLED.

        Recomputed on every call — never a stored flag — so it self-clears
        once a developer fixes the root cause. Reads everything passively:
        it never calls _init_chromadb (a health check must not trigger an
        80 MB ONNX load).

        FAILED   — ChromaDB init failed, or the circuit breaker has tripped.
        DEGRADED — optimization-init fell back, an active hint has a NULL
                   anchor_query, or the last reconcile left missing docs.
        DISABLED — OPTIMIZATION_ENABLED is false (a config state, not a fault).
        OK       — every check passes.
        """
        from src.backend.core.config import settings
        if not settings.OPTIMIZATION_ENABLED:
            return "DISABLED"

        # FAILED — hard failures. The ChromaDB check is tri-state: ONLY the
        # _CHROMADB_INIT_FAILED sentinel means "failed". `None` means "not yet
        # lazily initialized" — transient, not a fault — so it must not raise
        # a false FAILED in the seconds after a restart.
        if (self.execution_memory._chroma_client
                is ExecutionMemory._CHROMADB_INIT_FAILED):
            return "FAILED"
        if not self.circuit_breaker.is_enabled():
            return "FAILED"

        # DEGRADED — partial failures, each a current-state read.
        if not self._optimization_init_ok:
            return "DEGRADED"
        if (self.last_reconcile
                and self.last_reconcile.get("missing_count", 0) > 0):
            return "DEGRADED"
        try:
            with self.execution_memory.read_conn() as conn:
                null_anchors = conn.execute(
                    "SELECT COUNT(*) FROM nl_feedback_corrections "
                    "WHERE is_active = 1 AND anchor_query IS NULL"
                ).fetchone()[0]
            if null_anchors > 0:
                return "DEGRADED"
        except Exception as e:
            logger.warning(
                "[LEARNING] health anchor_query check failed: %s", e
            )

        return "OK"
