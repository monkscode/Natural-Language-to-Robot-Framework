"""
Keyword Correction Engine — Learns wrong→correct keyword mappings from failures.

Implements LearningEngine ABC. Captures keyword name mistakes (typically
Selenium→Browser Library) from failure analysis and builds a correction table.

Key design decisions:
- Score uses EffectivenessScore.calculate(evidence, 0) — no counter-evidence
- evidence_count >= 3 is the real gate for hint injection
- startswith("B") filter is forward-compatible for Phase 2 parameter corrections
- KNOWN_CORRECTIONS dict provides deterministic Selenium→Browser mappings

Referenced by: FeedbackLoop (DAY_05), SmartKeywordProvider (DAY_06)
"""

import re
import logging
from typing import Optional, List, Dict

from .learning_config import LearningEngine, EffectivenessScore
from .execution_memory import _assert_writer_thread

logger = logging.getLogger(__name__)


class KeywordCorrectionEngine(LearningEngine):
    """
    Maintains correction tables for keyword name mistakes.
    Implements LearningEngine ABC.

    Tables:
    - keyword_corrections: wrong_keyword → correct_keyword

    Key insight: real Docker executions surface keyword errors (failure
    category B*). By extracting the wrong keyword from the execution error
    message and inferring the correct one, we get free correction data to
    steer the Assembler on future runs.
    """

    # Known Selenium → Browser Library keyword corrections.
    # These are deterministic and always correct.
    KNOWN_CORRECTIONS = {
        "Input Text": "Fill Text",
        "Input Password": "Fill Secret",
        "Open Browser": "New Browser",
        "Close All Browsers": "Close Browser",
        "Click Element": "Click",
        "Click Button": "Click",
        "Wait Until Element Is Visible": "Wait For Elements State",
        "Element Should Be Visible": "Wait For Elements State",
        "Get WebElement": "Get Element",
        "Get WebElements": "Get Elements",
        "Select From List By Label": "Select Options By",
        "Select From List By Value": "Select Options By",
        "Page Should Contain Element": "Get Element Count",
    }

    def __init__(self, execution_memory=None):
        self._em = execution_memory

    def learn(self, record) -> None:
        """
        Learn keyword corrections from failure analysis.

        Triggered when failure_category starts with "B" (keyword/API errors).
        In Phase 1, only B1 (wrong keyword) and B6 (library mismatch) produce
        extractable data. The startswith("B") filter is kept for forward
        compatibility with Phase 2.
        """
        if not record.failure_category \
                or not record.failure_category.startswith("B"):
            return

        if not self._em:
            return
        _assert_writer_thread("KeywordCorrectionEngine.learn")

        # Extract wrong keyword from error message
        wrong_keyword = self._extract_wrong_keyword(record.error_message)
        if not wrong_keyword:
            return

        # Check if we already know this wrong keyword
        existing = self._em._writer_conn.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = ?",
            (wrong_keyword,),
        ).fetchone()

        if existing:
            # Increment evidence — same wrong keyword seen again
            new_evidence = existing["evidence_count"] + 1
            new_score = EffectivenessScore.calculate(new_evidence, 0)
            self._em._writer_conn.execute(
                "UPDATE keyword_corrections "
                "SET evidence_count = ?, "
                "    score = ?, "
                "    last_seen = datetime('now', 'localtime') "
                "WHERE id = ?",
                (new_evidence, new_score, existing["id"]),
            )
            logger.debug(
                f"[LEARNING:KEYWORD] Reinforced correction for "
                f"'{wrong_keyword}' (evidence={new_evidence}, score={new_score:.4f})"
            )
        else:
            # Store new correction (correct_keyword may be None if unknown)
            correct = self._infer_correct_keyword(wrong_keyword)
            initial_score = EffectivenessScore.calculate(1, 0)
            self._em._writer_conn.execute(
                "INSERT INTO keyword_corrections "
                "(wrong_keyword, correct_keyword, library, error_pattern, "
                " score, last_seen) "
                "VALUES (?, ?, 'browser', ?, ?, datetime('now', 'localtime'))",
                (wrong_keyword, correct, record.error_message, initial_score),
            )
            logger.debug(
                f"[LEARNING:KEYWORD] New correction: "
                f"'{wrong_keyword}' → '{correct or 'unknown'}'"
            )

        self._em._writer_conn.commit()

    def get_hints(self, user_query: str, url: str,
                  agent_role: str) -> Optional[List[str]]:
        """Return keyword correction hints for the assembler only."""
        if agent_role != "assembler":
            return None
        if not self._em:
            return None

        # Only return corrections with sufficient evidence
        with self._em.read_conn() as conn:
            corrections = conn.execute(
                "SELECT * FROM keyword_corrections "
                "WHERE score >= ? AND evidence_count >= ? "
                "ORDER BY evidence_count DESC LIMIT 5",
                (
                    EffectivenessScore.INJECTION_THRESHOLD,
                    EffectivenessScore.MIN_OBSERVATIONS,
                ),
            ).fetchall()

        if not corrections:
            return None

        hints = []
        for corr in corrections:
            if corr["correct_keyword"]:
                hints.append(
                    f"📋 Use '{corr['correct_keyword']}' not "
                    f"'{corr['wrong_keyword']}' "
                    f"({corr['library']} library)"
                )

        return hints if hints else None

    def get_stats(self) -> Dict:
        """Return engine statistics."""
        if not self._em:
            return {"total_corrections": 0, "active_corrections": 0, "engine": "keyword_correction"}
        with self._em.read_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM keyword_corrections"
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM keyword_corrections "
                "WHERE score >= ? AND evidence_count >= ?",
                (
                    EffectivenessScore.INJECTION_THRESHOLD,
                    EffectivenessScore.MIN_OBSERVATIONS,
                ),
            ).fetchone()[0]
        return {
            "total_corrections": total,
            "active_corrections": active,
            "engine": "keyword_correction",
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_wrong_keyword(self, error_message: str) -> Optional[str]:
        """Extract wrong keyword name from error messages.

        Primary pattern: "No keyword with name 'X' found"
        This covers B1 (wrong keyword name) and B6 (Selenium keyword used
        instead of Browser Library keyword).
        """
        if not error_message:
            return None
        match = re.search(
            r"No keyword with name '(.+?)' found", error_message
        )
        return match.group(1) if match else None

    def _infer_correct_keyword(self, wrong_keyword: str) -> Optional[str]:
        """Infer the correct keyword from known corrections.

        Uses the KNOWN_CORRECTIONS dict for deterministic Selenium→Browser
        Library mappings. Returns None for unknown wrong keywords — these
        will be resolved when a subsequent execution with the correct
        keyword succeeds.
        """
        return self.KNOWN_CORRECTIONS.get(wrong_keyword)
