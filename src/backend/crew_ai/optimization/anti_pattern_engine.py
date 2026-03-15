"""
Anti-Pattern Engine — Learns patterns to AVOID from failed executions.

Stores anti-patterns in the `anti_patterns` table (managed by SchemaManager):
- failure_category: From taxonomy (A1, B1, C1, etc.)
- query_pattern: What kind of query triggered the failure
- bad_code_snippet: The code that failed
- error_message: The error it caused
- correct_alternative: What should have been generated (filled on subsequent pass)
- domain: Optional domain specificity
- score: MDES effectiveness score (only increases, no counter-evidence)

Key design:
- Anti-patterns are generated from FAILURES, not passes
- "correct_alternative" backfilled when a subsequent similar query passes
- Score uses EffectivenessScore.calculate(evidence, 0) — never decrements
- Two similarity thresholds: conservative (>= 3) for merging, permissive (>= 2) for hints

Referenced by: Hint injection pipeline (DAY_06+).
Depends on: learning_config.py (DAY_00), schema_manager.py (DAY_00).
"""

import logging
from typing import Optional, List, Dict

from src.backend.crew_ai.optimization.learning_config import (
    LearningEngine,
    EffectivenessScore,
    extract_domain,
)

logger = logging.getLogger(__name__)


class AntiPatternEngine(LearningEngine):
    """
    Learns patterns to AVOID from failed executions.
    Implements LearningEngine ABC.

    Anti-patterns stored in SQLite anti_patterns table:
    - failure_category: From taxonomy (A1, B1, C1, etc.)
    - query_pattern: What kind of query triggered this
    - bad_code_snippet: The code that failed
    - error_message: The error it caused
    - correct_alternative: What should have been generated (if known)
    - domain: Optional domain specificity
    - score: MDES effectiveness score

    Key design:
    - Anti-patterns are generated from failures, NOT from passes
    - The "correct alternative" comes from subsequent successful executions
      of similar queries, or from validator fixes
    - Score increases when the same anti-pattern is seen again; never decreases
    """

    # Similarity thresholds for word overlap matching
    _MERGE_OVERLAP_THRESHOLD = 3   # Conservative: for learn() merging
    _HINTS_OVERLAP_THRESHOLD = 2   # Permissive: for get_hints() warnings

    # Score and evidence gates (consistent with all engines)
    _INJECTION_THRESHOLD = EffectivenessScore.INJECTION_THRESHOLD  # 0.4
    _MIN_OBSERVATIONS = EffectivenessScore.MIN_OBSERVATIONS        # 3

    # Code extraction context window
    _CONTEXT_LINES = 2   # ±2 lines around failed keyword
    _MAX_SNIPPET_LINES = 5

    def __init__(self, db_conn):
        """
        Initialize AntiPatternEngine with a SQLite connection.

        Args:
            db_conn: sqlite3.Connection with row_factory=sqlite3.Row
        """
        self.db_conn = db_conn

    def learn(self, record) -> None:
        """
        Learn anti-patterns from failed executions.

        Only creates anti-patterns when:
        1. Test failed
        2. Failure category is classified (not "unknown" or None)
        3. We have an error message for context

        When a test passes, checks if it resolves an existing anti-pattern
        by providing the correct alternative code.
        """
        if record.test_status != "failed":
            # Check if this passing execution resolves an existing anti-pattern
            self._check_for_correct_alternative(record)
            return

        if not record.failure_category or record.failure_category == "unknown":
            return

        if not record.error_message:
            return

        # Check for existing anti-pattern with same category + similar query
        existing = self._find_similar_anti_pattern(
            record.failure_category, record.user_query
        )

        if existing:
            # Reinforce existing anti-pattern
            new_evidence = existing["evidence_count"] + 1
            new_score = EffectivenessScore.calculate(new_evidence, 0)
            self.db_conn.execute(
                "UPDATE anti_patterns SET evidence_count = ?, score = ?, "
                "last_seen = datetime('now', 'localtime') WHERE id = ?",
                (new_evidence, new_score, existing["id"])
            )
        else:
            # Create new anti-pattern
            initial_score = EffectivenessScore.calculate(1, 0)
            self.db_conn.execute("""
                INSERT INTO anti_patterns
                (failure_category, query_pattern, bad_code_snippet, error_message,
                 domain, score, evidence_count, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now', 'localtime'))
            """, (
                record.failure_category,
                record.user_query,
                self._extract_relevant_code(record.robot_code,
                                            getattr(record, 'failed_keyword', None)),
                record.error_message,
                getattr(record, 'domain', None),
                initial_score,
            ))
        self.db_conn.commit()

    def get_hints(self, user_query: str, url: str,
                  agent_role: str) -> Optional[List[str]]:
        """
        Return anti-pattern warnings relevant to the query.

        Targets:
        - Planner: "Don't generate linear code for iteration queries"
        - Assembler: "Don't use single Get Text for 'all rows'"
        - Validator/Identifier: None (not relevant for these roles)

        Only returns anti-patterns with score >= 0.4 AND evidence_count >= 3.
        """
        if agent_role not in ("planner", "assembler", "validator"):
            return None

        domain = extract_domain(url) if url else None

        # Query anti-patterns relevant to this query
        warnings = self._find_matching_anti_patterns(user_query, domain)

        if not warnings:
            return None

        hints = []
        for ap in warnings:
            if agent_role == "planner":
                hints.append(
                    f"⚠️ AVOID: Previously failed with category {ap['failure_category']}: "
                    f"{ap['error_message'][:100]}"
                )
            elif agent_role == "assembler":
                if ap.get("correct_alternative"):
                    hints.append(
                        f"⚠️ AVOID: Don't use:\n{ap['bad_code_snippet'][:80]}\n"
                        f"Instead use:\n{ap['correct_alternative'][:80]}"
                    )
                else:
                    hints.append(
                        f"⚠️ AVOID: This pattern caused {ap['failure_category']}: "
                        f"{ap['error_message'][:100]}"
                    )

        return hints if hints else None

    def get_stats(self) -> Dict:
        """Return engine statistics."""
        total = self.db_conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]

        active = self.db_conn.execute(
            "SELECT COUNT(*) FROM anti_patterns WHERE score >= ? AND evidence_count >= ?",
            (self._INJECTION_THRESHOLD, self._MIN_OBSERVATIONS)
        ).fetchone()[0]

        # Category breakdown
        categories = self.db_conn.execute(
            "SELECT failure_category, COUNT(*) as cnt "
            "FROM anti_patterns GROUP BY failure_category"
        ).fetchall()

        return {
            "total_anti_patterns": total,
            "active_anti_patterns": active,
            "by_category": {row[0]: row[1] for row in categories},
            "engine": "anti_pattern",
        }

    # -------------------------------------------------------------------
    # Private: Similarity Matching
    # -------------------------------------------------------------------

    def _find_similar_anti_pattern(self, category: str,
                                   user_query: str) -> Optional[dict]:
        """
        Find existing anti-pattern matching category + similar query.

        Used during learn() to decide whether to reinforce an existing
        anti-pattern or create a new one. Conservative threshold (>= 3 words)
        to avoid accidentally merging different anti-patterns.

        Future: Replace word overlap with ChromaDB semantic similarity.
        """
        rows = self.db_conn.execute(
            "SELECT * FROM anti_patterns WHERE failure_category = ? "
            "ORDER BY score DESC",
            (category,)
        ).fetchall()

        query_words = set(user_query.lower().split())
        for row in rows:
            pattern_words = set((row["query_pattern"] or "").lower().split())
            overlap = len(query_words & pattern_words)
            if overlap >= self._MERGE_OVERLAP_THRESHOLD:
                return dict(row)
        return None

    def _find_matching_anti_patterns(self, user_query: str,
                                     domain: str = None) -> List[dict]:
        """
        Find anti-patterns that might apply to this query.

        Used by get_hints() and _check_for_correct_alternative().
        Permissive threshold (>= 2 words) to cast a wider net for warnings.
        Gated by score >= 0.4 AND evidence_count >= 3.
        """
        query_lower = user_query.lower()

        # Get high-scoring anti-patterns above injection threshold
        sql = (
            "SELECT * FROM anti_patterns "
            "WHERE score >= ? AND evidence_count >= ?"
        )
        params: list = [self._INJECTION_THRESHOLD, self._MIN_OBSERVATIONS]

        if domain:
            sql += " AND (domain = ? OR domain IS NULL)"
            params.append(domain)

        sql += " ORDER BY score DESC LIMIT 10"

        rows = self.db_conn.execute(sql, params).fetchall()

        # Filter by relevance to query using word overlap
        relevant = []
        query_words = set(query_lower.split())
        for row in rows:
            pattern = (row["query_pattern"] or "").lower()
            pattern_words = set(pattern.split())
            overlap = len(query_words & pattern_words)
            if overlap >= self._HINTS_OVERLAP_THRESHOLD:
                relevant.append(dict(row))

        return relevant

    # -------------------------------------------------------------------
    # Private: Correct Alternative Resolution
    # -------------------------------------------------------------------

    def _check_for_correct_alternative(self, record) -> None:
        """
        When a test PASSES, check if it resolves an existing anti-pattern
        by providing the correct alternative code.

        Uses _find_matching_anti_patterns (threshold-gated: score >= 0.4,
        evidence >= 3) -- new/fluke anti-patterns won't be resolved until
        they have sufficient evidence. This is by design.

        Only updates an anti-pattern's correct_alternative when the passing
        code does NOT contain the bad_code_snippet -- proving the code
        pattern actually changed. This prevents indiscriminate overwrites
        across different failure categories.
        """
        if not getattr(record, 'robot_code', None):
            return

        domain = getattr(record, 'domain', None)

        # Find unresolved anti-patterns for similar queries
        anti_patterns = self._find_matching_anti_patterns(
            record.user_query, domain
        )

        updated = False
        for ap in anti_patterns:
            if ap.get("correct_alternative"):
                continue  # Already resolved

            # Only set correct_alternative if the bad snippet is NOT present
            # in the passing code. This proves the failing pattern was actually
            # replaced, rather than being a coincidental pass.
            bad_snippet = ap.get("bad_code_snippet", "")
            if self._bad_snippet_present(bad_snippet, record.robot_code):
                logger.debug(
                    "[LEARNING:ANTI_PATTERN] Skipping alternative for ap_id=%d: "
                    "bad_code_snippet still present in passing code",
                    ap["id"],
                )
                continue

            self.db_conn.execute(
                "UPDATE anti_patterns SET correct_alternative = ? WHERE id = ?",
                (record.robot_code[:500], ap["id"])
            )
            updated = True

        if updated:
            self.db_conn.commit()

    # -------------------------------------------------------------------
    # Private: Bad Snippet Detection
    # -------------------------------------------------------------------

    def _bad_snippet_present(self, bad_snippet: str, robot_code: str) -> bool:
        """Check if the bad code pattern is still present in the passing code.

        Uses line-level comparison instead of raw substring matching to avoid
        false positives from short common strings like 'Click' or 'Get Text'.

        Returns True only when ALL meaningful lines from bad_snippet appear
        as complete (stripped) lines in robot_code.
        """
        if not bad_snippet or not robot_code:
            return False

        snippet_lines = [
            line.strip() for line in bad_snippet.split("\n")
            if line.strip()
        ]

        if not snippet_lines:
            return False

        code_lines = {
            line.strip() for line in robot_code.split("\n")
            if line.strip()
        }

        return all(sline in code_lines for sline in snippet_lines)

    # -------------------------------------------------------------------
    # Private: Code Extraction
    # -------------------------------------------------------------------

    def _extract_relevant_code(self, robot_code: str,
                               failed_keyword: str = None) -> str:
        """
        Extract the relevant portion of code around the failure.

        Strategy:
        - If failed_keyword provided: ±2 lines around the keyword (adaptive window)
        - Blank lines are skipped to maximize meaningful content
        - Capped at 5 meaningful lines
        - Fallback: first 5 non-header lines of test body
        """
        if not robot_code:
            return ""

        lines = robot_code.split("\n")

        if failed_keyword:
            for i, line in enumerate(lines):
                if failed_keyword in line:
                    start = max(0, i - self._CONTEXT_LINES)
                    end = min(len(lines), i + self._CONTEXT_LINES + 1)
                    snippet_lines = [
                        l for l in lines[start:end] if l.strip()
                    ]
                    return "\n".join(snippet_lines[:self._MAX_SNIPPET_LINES])

        # Fallback: first 5 non-header, non-empty lines
        test_lines = [
            l for l in lines
            if l.strip() and not l.startswith("***")
        ]
        return "\n".join(test_lines[:self._MAX_SNIPPET_LINES])
