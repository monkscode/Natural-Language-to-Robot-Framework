"""
NL Feedback Engine — Learns from natural language user feedback.

Implements LearningEngine ABC with a 3-layer auto-triage pipeline.
Phase 1 implements Layer 1 only (seed patterns via regex).

Feedback Pipeline:
    1. Triage (process_feedback) — classify feedback category via seed patterns
    2. Storage (learn_from_feedback) — store correction in nl_feedback_corrections
    3. Retrieval (get_hints) — query corrections by scope, return deduplicated hints
    4. Tracking (update_hint_effectiveness) — track success/failure with smart
       category cross-referencing to avoid false-positive penalization

Taxonomy codes:
    A1 — Structural issue (missing loop, wrong flow)
    B1 — Keyword issue (wrong RF keyword used)
    C1 — Locator issue (wrong element targeted)
    D1 — Timing issue (too fast, didn't wait)
    E1 — Data issue (wrong value, missing variable)
    P0 — Positive (close_enough with no detail)
    N0 — Negative (completely_wrong with no detail)
    X0 — Uncategorized (no pattern matched)
"""

import re
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.backend.crew_ai.optimization.learning_config import (
    LearningEngine,
    extract_domain,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scope auto-determination — maps triage category to hint scope
# ---------------------------------------------------------------------------

# Scope map covers the full planned triage taxonomy, not just Phase 1.
# "assertion" is pre-wired for DAY_08's wrong_assertion pattern (not yet implemented).
# "positive"/"negative" are reserved for future LLM-triage paths that may bypass
# the empty-text short-circuit. Do not remove — additions happen in Phase 3.
_SCOPE_BY_CATEGORY = {
    "structural": "domain",
    "keyword": "global",
    "locator": "url",
    "timing": "domain",
    "data": "url",
    "assertion": "domain",
    "uncategorized": "domain",
    "positive": "domain",
    "negative": "domain",
}

# Auto-disable: hint tried N times with 0 successes + same-category failures
AUTO_DISABLE_MIN_APPLICATIONS = 3


# ---------------------------------------------------------------------------
# Seed Patterns (Layer 1) — fast regex matching
# ---------------------------------------------------------------------------

SEED_PATTERNS: Dict[str, Dict] = {
    # A1 — Structural issues
    "loop_missing": {
        "patterns": [
            r"\ball\s+rows?\b",
            r"\bevery\b",
            r"\beach\s+(row|item|element|product)",
            r"\bloop\b",
            r"\biterate\b",
            r"\bonly\s+(the\s+)?first\b",
            r"\bmissing\s+loop\b",
            r"\bshould\s+have\s+checked\s+all\b",
        ],
        "taxonomy": "A1",
        "category": "structural",
        "specific_type": "loop_missing",
    },
    "wrong_flow": {
        "patterns": [
            r"\bwrong\s+order\b",
            r"\bsteps?\s+(are|were)\s+wrong\b",
            r"\bshould\s+have\s+done\s+.+\s+first\b",
            r"\bincorrect\s+sequence\b",
            r"\bout\s+of\s+order\b",
        ],
        "taxonomy": "A1",
        "category": "structural",
        "specific_type": "wrong_flow",
    },
    # B1 — Keyword issues
    "wrong_keyword": {
        "patterns": [
            r"\bwrong\s+keyword\b",
            r"\bshould\s+(use|have\s+used)\b",
            r"\binstead\s+of\b",
            r"\bwrong\s+command\b",
            r"\bincorrect\s+keyword\b",
            r"\buse\s+\w+\s+instead\b",
        ],
        "taxonomy": "B1",
        "category": "keyword",
        "specific_type": "wrong_keyword",
    },
    "missing_keyword": {
        "patterns": [
            r"\bmissing\s+keyword\b",
            r"\bforgot\s+to\b",
            r"\bdidn.t\s+(do|perform|execute)\b",
            r"\bskipped?\b.*\bstep\b",
        ],
        "taxonomy": "B1",
        "category": "keyword",
        "specific_type": "missing_keyword",
    },
    # C1 — Locator issues
    "wrong_element": {
        "patterns": [
            r"\bwrong\s+(element|button|field|link|input)\b",
            r"\bclicked?\s+(the\s+)?wrong\b",
            r"\bwrong\s+locator\b",
            r"\bwrong\s+(id|class|xpath|selector)\b",
            r"\bnot\s+the\s+right\s+(element|button)\b",
        ],
        "taxonomy": "C1",
        "category": "locator",
        "specific_type": "wrong_element",
    },
    "element_not_found": {
        "patterns": [
            r"\bcouldn.t\s+find\b",
            r"\belement\s+(not|wasn.t)\s+found\b",
            r"\bno\s+such\s+element\b",
            r"\bmissing\s+element\b",
        ],
        "taxonomy": "C1",
        "category": "locator",
        "specific_type": "element_not_found",
    },
    # D1 — Timing issues
    "too_fast": {
        "patterns": [
            r"\btoo\s+fast\b",
            r"\bdidn.t\s+wait\b",
            r"\bneed(s|ed)?\s+(to\s+)?wait\b",
            r"\btimeout\b",
            r"\bpage\s+(was|wasn.t)\s+(loaded|ready)\b",
            r"\bslow\b.*\b(load|page|site)\b",
        ],
        "taxonomy": "D1",
        "category": "timing",
        "specific_type": "too_fast",
    },
    # E1 — Data issues
    "wrong_value": {
        "patterns": [
            r"\bwrong\s+(value|data|text|number|input)\b",
            r"\bincorrect\s+(value|data)\b",
            r"\bshould\s+(be|have\s+been)\s+\".+\"",
            r"\btypo\b",
            r"\bwrong\s+variable\b",
        ],
        "taxonomy": "E1",
        "category": "data",
        "specific_type": "wrong_value",
    },
}


# Pre-compile all patterns for performance
_COMPILED_PATTERNS: Dict[str, Dict] = {}
for _name, _info in SEED_PATTERNS.items():
    _COMPILED_PATTERNS[_name] = {
        "compiled": [re.compile(p, re.IGNORECASE) for p in _info["patterns"]],
        "taxonomy": _info["taxonomy"],
        "category": _info["category"],
        "specific_type": _info["specific_type"],
    }


# ---------------------------------------------------------------------------
# NLFeedbackEngine
# ---------------------------------------------------------------------------

class NLFeedbackEngine(LearningEngine):
    """
    Learns from natural language user feedback via 3-layer auto-triage.
    Implements LearningEngine ABC.

    3-layer triage pipeline:
    Layer 1: Seed patterns (regex, fast, bootstrap) — Phase 1
    Layer 2: Learned patterns (from past LLM triage, SQLite — Phase 3)
    Layer 3: LLM triage (expensive, ~$0.001/call — Phase 3)

    Phase 1 implementation: Layer 1 only (seed patterns).
    """

    # Confidence tuning
    _BASE_CONFIDENCE = 0.50
    _PATTERN_BOOST = 0.15
    _CROSS_REF_BOOST = 0.20
    _MAX_CONFIDENCE = 1.0

    # Scope WHERE clause shared by get_hints and update_hint_effectiveness.
    # Parameter order is (domain, url).
    _SCOPE_WHERE = (
        "scope = 'global' "
        "OR (scope = 'domain' AND domain = ?) "
        "OR (scope = 'url' AND url = ?)"
    )

    def __init__(self, db_conn: sqlite3.Connection = None):
        """
        Initialize NLFeedbackEngine.

        Args:
            db_conn: Shared SQLite connection (for stats tracking).
                     Optional — engine works without it.
        """
        self._db_conn = db_conn
        self._total_processed = 0
        self._category_counts: Dict[str, int] = {}
        self._confidence_sum = 0.0
        self._last_updated: Optional[str] = None
        self._stats_lock = threading.Lock()

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def process_feedback(
        self,
        workflow_id: str,
        feedback_text: str,
        feedback_type: str,
        error_message: Optional[str] = None,
    ) -> Dict:
        """
        Triage user feedback through the seed pattern pipeline.

        Args:
            workflow_id: The workflow this feedback applies to.
            feedback_text: User's natural language feedback.
            feedback_type: "close_enough" | "completely_wrong"
            error_message: Optional error from test execution output.

        Returns:
            Triage result dict with keys:
            - category: "structural" | "keyword" | "locator" | "timing" |
                        "data" | "positive" | "negative" | "uncategorized"
            - specific_type: e.g. "loop_missing", "wrong_keyword"
            - confidence: 0.0–1.0
            - taxonomy_code: e.g. "A1", "B1", "P0"
            - matched_patterns: list of pattern names that matched
        """
        # Fast path: empty feedback text
        if not feedback_text or not feedback_text.strip():
            result = self._empty_feedback_result(feedback_type)
            self._update_stats(result)
            return result

        # Layer 1: Seed pattern matching
        matches = self._match_seed_patterns(feedback_text)

        if not matches:
            result = {
                "category": "uncategorized",
                "specific_type": "no_match",
                "confidence": self._BASE_CONFIDENCE,
                "taxonomy_code": "X0",
                "matched_patterns": [],
            }
            self._update_stats(result)
            return result

        # Use the first match category (highest priority by dict order)
        best = matches[0]
        pattern_count = len(matches)

        # Base + per-pattern boost, add cross-ref boost when error confirms, cap once.
        confidence = self._BASE_CONFIDENCE + (pattern_count * self._PATTERN_BOOST)
        if error_message and self._error_confirms_feedback(
            best["taxonomy"], error_message
        ):
            confidence += self._CROSS_REF_BOOST
        confidence = min(confidence, self._MAX_CONFIDENCE)

        result = {
            "category": best["category"],
            "specific_type": best["specific_type"],
            "confidence": round(confidence, 2),
            "taxonomy_code": best["taxonomy"],
            "matched_patterns": [m["name"] for m in matches],
        }
        self._update_stats(result)
        return result

    # -------------------------------------------------------------------
    # LearningEngine ABC
    # -------------------------------------------------------------------

    def learn(self, record) -> None:
        """No-op. NL engine learns from user feedback, not executions."""
        pass

    def learn_from_feedback(self, record, feedback_insight) -> None:
        """
        Store user feedback as a queryable correction in nl_feedback_corrections.

        This is the STORAGE side of the feedback pipeline. All feedback is
        stored regardless of triage category — seed patterns are NOT a gate.

        Scope is auto-determined from the triage category:
            structural → domain, keyword → global, locator → url, etc.

        Deduplication: same feedback_text + domain + scope → increment
        evidence_count and update last_seen.

        Args:
            record: ExecutionRecord for the workflow being given feedback.
            feedback_insight: Triage result dict (must include 'feedback_text').
        """
        if not self._db_conn:
            return

        feedback_text = feedback_insight.get("feedback_text")
        if not feedback_text or not feedback_text.strip():
            return

        category = feedback_insight.get("category", "uncategorized")
        # Skip purely informational triage results (no text correction)
        if category == "positive":
            return

        scope = _SCOPE_BY_CATEGORY.get(category, "domain")
        domain = getattr(record, "domain", None) or (
            extract_domain(getattr(record, "url", "")) if getattr(record, "url", None) else None
        )
        url = getattr(record, "url", None)
        failure_category = getattr(record, "failure_category", None)
        workflow_id = getattr(record, "workflow_id", None)
        now = datetime.now(timezone.utc).isoformat()

        try:
            # Upsert: increment evidence if exists, else insert
            existing = self._db_conn.execute(
                "SELECT id, evidence_count FROM nl_feedback_corrections "
                "WHERE feedback_text = ? AND domain IS ? AND scope = ?",
                (feedback_text.strip(), domain, scope),
            ).fetchone()

            if existing:
                self._db_conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET evidence_count = evidence_count + 1, "
                    "    last_seen = ?, "
                    "    is_active = 1 "
                    "WHERE id = ?",
                    (now, existing["id"]),
                )
                logger.info(
                    "[LEARNING:NL] Reinforced feedback '%s' for %s "
                    "(evidence=%d)",
                    feedback_text[:50], domain,
                    existing["evidence_count"] + 1,
                )
            else:
                self._db_conn.execute(
                    "INSERT INTO nl_feedback_corrections "
                    "(feedback_text, category, scope, domain, url, "
                    " original_failure_category, evidence_count, "
                    " source_workflow_id, created_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        feedback_text.strip(), category, scope,
                        domain, url, failure_category,
                        workflow_id, now, now,
                    ),
                )
                logger.info(
                    "[LEARNING:NL] Stored new feedback correction: "
                    "'%s' scope=%s domain=%s",
                    feedback_text[:50], scope, domain,
                )

            self._db_conn.commit()

        except Exception as e:
            logger.warning(
                "[LEARNING:NL] Failed to store feedback correction: %s", e,
            )

    def get_hints(
        self, user_query: str, url: str, agent_role: str,
    ) -> Optional[List[str]]:
        """
        Return user feedback corrections relevant to the current context.

        This is the RETRIEVAL side of the feedback pipeline. Queries
        nl_feedback_corrections by scope:
            - global corrections (always included)
            - domain-scoped corrections (matching domain)
            - url-scoped corrections (matching url)

        Results are deduplicated via word-overlap Jaccard similarity
        (>60% overlap = duplicate) to save tokens.

        Returns hints formatted as:
            "⚠️ USER FEEDBACK: <feedback_text>"
        """
        if not self._db_conn:
            return None

        domain = extract_domain(url) if url else None

        try:
            # Query: global + domain-match + url-match, active only
            rows = self._db_conn.execute(
                "SELECT id, feedback_text, category, scope, "
                "       success_count, evidence_count "
                "FROM nl_feedback_corrections "
                "WHERE is_active = 1 "
                f"AND ({self._SCOPE_WHERE}) "
                "ORDER BY success_count DESC, "
                "         evidence_count DESC, "
                "         last_seen DESC "
                "LIMIT 10",
                (domain, url),
            ).fetchall()
        except Exception as e:
            logger.warning(
                "[LEARNING:NL] get_hints query failed: %s", e,
            )
            return None

        if not rows:
            return None

        # Deduplicate via word overlap
        deduped = self._deduplicate_hints(
            [{"feedback_text": r["feedback_text"],
              "id": r["id"]} for r in rows]
        )

        # Format as hint strings (cap at 5)
        hints = [
            self._format_feedback_hint(h['feedback_text'])
            for h in deduped[:5]
        ]

        return hints if hints else None

    def _format_feedback_hint(self, feedback_text: str) -> str:
        # Pass feedback through verbatim with a universal warning header.
        # The header travels with every hint to every agent (Planner, Assembler,
        # Validator), so the "do not copy literally" guardrail applies even for
        # agents whose task prompts lack an explicit disclaimer. Preserving the
        # original line order retains the user's reasoning structure.
        return (
            "\u26a0\ufe0f USER FEEDBACK (reference context from a past test — "
            "do NOT copy any code literally, treat as guidance only):\n"
            f"{feedback_text.strip()}"
        )

    def update_hint_effectiveness(
        self,
        domain: Optional[str],
        url: Optional[str],
        test_passed: bool,
        new_failure_category: Optional[str] = None,
    ) -> None:
        """
        Track effectiveness of active hints after test execution.

        Uses smart failure attribution via category cross-referencing:
        - When test passed: increment applied_count + success_count for
          all active hints matching the domain/url.
        - When test failed: compare original_failure_category with
          new failure. Same category → hint didn't fix its issue →
          increment failure_count. Different category → unrelated → skip.
        - Auto-disable: if failure_count > 0 AND success_count == 0
          AND applied_count >= 3 → set is_active = 0.

        Called by FeedbackLoop.process_execution() after every test run.
        """
        if not self._db_conn:
            return

        try:
            # Find all active hints that would have been injected
            active_hints = self._db_conn.execute(
                "SELECT id, original_failure_category, "
                "       applied_count, success_count, failure_count "
                "FROM nl_feedback_corrections "
                "WHERE is_active = 1 "
                f"AND ({self._SCOPE_WHERE})",
                (domain, url),
            ).fetchall()

            if not active_hints:
                return

            for hint in active_hints:
                hint_id = hint["id"]
                new_applied = hint["applied_count"] + 1
                new_success = hint["success_count"]
                new_failure = hint["failure_count"]

                if test_passed:
                    new_success += 1
                else:
                    # Smart attribution: only penalize if SAME failure
                    # category persists (hint didn't fix its issue)
                    orig_cat = hint["original_failure_category"]
                    if (
                        orig_cat
                        and new_failure_category
                        and orig_cat == new_failure_category
                    ):
                        new_failure += 1
                    # Different category or no category → no penalty

                # Update counts
                self._db_conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET applied_count = ?, "
                    "    success_count = ?, "
                    "    failure_count = ? "
                    "WHERE id = ?",
                    (new_applied, new_success, new_failure, hint_id),
                )

                # Auto-disable check
                if (
                    new_failure > 0
                    and new_success == 0
                    and new_applied >= AUTO_DISABLE_MIN_APPLICATIONS
                ):
                    self._db_conn.execute(
                        "UPDATE nl_feedback_corrections "
                        "SET is_active = 0 WHERE id = ?",
                        (hint_id,),
                    )
                    logger.info(
                        "[LEARNING:NL] Auto-disabled hint id=%d "
                        "(applied=%d, success=0, failure=%d)",
                        hint_id, new_applied, new_failure,
                    )

            self._db_conn.commit()

        except Exception as e:
            logger.warning(
                "[LEARNING:NL] update_hint_effectiveness failed: %s", e,
            )

    def get_stats(self) -> Dict:
        """Return engine statistics for monitoring/debugging."""
        with self._stats_lock:
            avg_confidence = (
                round(self._confidence_sum / self._total_processed, 2)
                if self._total_processed > 0
                else 0.0
            )
            return {
                "total_processed": self._total_processed,
                "category_counts": dict(self._category_counts),
                "average_confidence": avg_confidence,
                "last_updated": self._last_updated,
                "seed_pattern_count": len(SEED_PATTERNS),
            }

    # -------------------------------------------------------------------
    # Internal methods — triage
    # -------------------------------------------------------------------

    def _match_seed_patterns(self, text: str) -> List[Dict]:
        """
        Scan feedback text against compiled seed patterns.

        Returns list of dicts with keys: name, taxonomy, category,
        specific_type. Ordered by appearance in SEED_PATTERNS (priority).
        De-duplicated by specific_type to avoid double-counting variants
        of the same issue category.
        """
        matches = []
        seen_types = set()

        for name, info in _COMPILED_PATTERNS.items():
            for compiled_re in info["compiled"]:
                if compiled_re.search(text):
                    if info["specific_type"] not in seen_types:
                        matches.append({
                            "name": name,
                            "taxonomy": info["taxonomy"],
                            "category": info["category"],
                            "specific_type": info["specific_type"],
                        })
                        seen_types.add(info["specific_type"])
                    break  # One match per pattern group is enough

        return matches

    def _error_confirms_feedback(
        self, taxonomy_code: str, error_message: str,
    ) -> bool:
        """
        Check if error message agrees with triaged feedback category.

        Cross-reference heuristics per taxonomy code:
        A1 (structural) — error mentions count mismatch, not a variable error
        B1 (keyword) — error mentions "No keyword" or "not found"
        C1 (locator) — error mentions element/locator not found
        D1 (timing) — error mentions timeout
        E1 (data) — error mentions value mismatch

        Returns True if error confirms the feedback category.
        """
        error_lower = error_message.lower()

        if taxonomy_code == "A1":
            # Structural: count mismatch, not a variable issue
            structural_signals = [
                "does not match expected",
                "count",
                "not equal",
                "items found",
            ]
            return (
                any(s in error_lower for s in structural_signals)
                and "variable" not in error_lower
            )

        if taxonomy_code == "B1":
            # Keyword: RF keyword not found
            keyword_signals = [
                "no keyword",
                "keyword not found",
                "not found in",
            ]
            return any(s in error_lower for s in keyword_signals)

        if taxonomy_code == "C1":
            # Locator: element not found
            locator_signals = [
                "element not found",
                "not found",
                "no element",
                "locator",
                "elementnotinteractableexception",
            ]
            return any(s in error_lower for s in locator_signals)

        if taxonomy_code == "D1":
            # Timing: timeout
            timing_signals = [
                "timeout",
                "timed out",
                "exceeded",
            ]
            return any(s in error_lower for s in timing_signals)

        if taxonomy_code == "E1":
            # Data: value mismatch
            data_signals = [
                "should be",
                "expected",
                "does not match",
                "mismatch",
            ]
            return any(s in error_lower for s in data_signals)

        return False

    def _empty_feedback_result(self, feedback_type: str) -> Dict:
        """Fast path result for empty feedback text."""
        if feedback_type == "close_enough":
            return {
                "category": "positive",
                "specific_type": "no_detail",
                "confidence": 1.0,
                "taxonomy_code": "P0",
                "matched_patterns": [],
            }
        return {
            "category": "negative",
            "specific_type": "no_detail",
            "confidence": 1.0,
            "taxonomy_code": "N0",
            "matched_patterns": [],
        }

    def _update_stats(self, result: Dict) -> None:
        """Update internal counters after processing feedback."""
        with self._stats_lock:
            self._total_processed += 1
            category = result.get("category", "unknown")
            self._category_counts[category] = (
                self._category_counts.get(category, 0) + 1
            )
            self._confidence_sum += result.get("confidence", 0.0)
            self._last_updated = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------
    # Internal methods — deduplication
    # -------------------------------------------------------------------

    @staticmethod
    def _deduplicate_hints(hints: List[Dict]) -> List[Dict]:
        """
        Remove semantically similar hints using word-overlap (Jaccard).

        Two hints with >60% word overlap are considered duplicates.
        The first occurrence (already sorted by success/evidence) wins.

        Args:
            hints: list of dicts with 'feedback_text' key.

        Returns:
            Deduplicated list preserving original order.
        """
        selected = []
        for hint in hints:
            words = set(hint["feedback_text"].lower().split())
            is_dup = False
            for existing in selected:
                existing_words = set(
                    existing["feedback_text"].lower().split()
                )
                union = words | existing_words
                if not union:
                    continue
                overlap = len(words & existing_words) / len(union)
                if overlap > 0.6:
                    is_dup = True
                    break
            if not is_dup:
                selected.append(hint)
        return selected
