"""
Structural Rule Engine — Learns when queries need specific code structures.

This module contains two classes:
1. IntentExtractor — 3-layer self-evolving intent resolution from user queries
2. StructuralRuleEngine — LearningEngine that learns query→structure rules

Layer priority: Learned (SQLite) > Seed (in-memory) > LLM (Phase 3 placeholder)

The core design decision: learn() checks intent's requires.keywords against
extracted code elements via line-level parsing — NOT code_structure label
comparison. This is fully dynamic: new RF constructs, LLM-discovered intents,
and self-correcting patterns all work automatically without code changes.

Referenced by: FeedbackLoop (DAY_05), SmartKeywordProvider (DAY_06)
"""

import re
import json
import logging
import threading
from typing import Optional, List, Dict

from .learning_config import LearningEngine, EffectivenessScore, WRITER_THREAD_NAME
from .execution_memory import _assert_writer_thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IntentExtractor — 3-Layer Self-Evolving Intent Resolution
# ---------------------------------------------------------------------------

class IntentExtractor:
    """
    3-layer intent resolution for user queries.

    Layer 1: Seed patterns (in-memory, bootstrap, fast)
    Layer 2: Learned patterns (from SQLite intent_patterns table, most reliable)
    Layer 3: LLM classification (Phase 3 DAY_16, expensive, ~$0.001/call)

    Resolution order: Layer 2 → Layer 1 → Layer 3
    (Learned patterns checked first because they have execution evidence)
    """

    # Layer 1: Seed patterns — bootstrap only.
    # These get migrated to SQLite (Layer 2) on first match.
    SEED_PATTERNS = {
        "iteration": {
            "triggers": [
                "all rows", "each row", "every entry", "loop through",
                "for each", "all items", "iterate", "each element",
            ],
            "requires": {
                "code_structure": "for_loop",
                "keywords": ["Get Elements", "FOR", "END"],
            },
        },
        "counting": {
            "triggers": ["count", "how many", "number of", "total number"],
            "requires": {"keywords": ["Get Element Count"]},
        },
        "filtering_validation": {
            "triggers": [
                "filter", "filtered", "only show", "display only", "show only",
            ],
            "requires": {
                "code_structure": "for_loop",
                "keywords": ["Get Elements", "Should Contain"],
            },
        },
        "comparison": {
            "triggers": [
                "greater than", "less than", "more than", "at least",
                "compare", "higher", "lower",
            ],
            "requires": {"keywords": ["Should Be True", "Evaluate"]},
        },
        "conditional_action": {
            "triggers": ["if exists", "only if", "when present", "in case"],
            "requires": {
                "code_structure": "conditional",
                "keywords": ["Get Element Count", "Run Keyword If"],
            },
        },
        "data_extraction": {
            "triggers": ["extract", "get the value", "read the", "capture"],
            "requires": {"keywords": ["Get Text", "Set Variable"]},
        },
        "form_submission": {
            "triggers": [
                "fill in", "submit form", "fill form", "fill the form",
            ],
            "requires": {"keywords": ["Fill Text", "Click"]},
        },
        "sorting_validation": {
            "triggers": [
                "sorted", "ascending", "descending", "sort by", "in order",
            ],
            "requires": {
                "code_structure": "for_loop",
                "keywords": ["Get Elements", "Should Be True"],
            },
        },
    }

    def __init__(self, execution_memory=None):
        self._em = execution_memory

    def extract_intents(self, user_query: str) -> List[dict]:
        """
        3-layer intent resolution. Returns matched intents with scores.

        Layer priority: Learned (SQLite) > Seed (in-memory) > LLM (expensive)
        """
        query_lower = user_query.lower()

        # Layer 2: Check learned patterns first (most reliable)
        learned_matches = self._check_learned_patterns(query_lower)
        if learned_matches:
            return learned_matches

        # Layer 1: Fall back to seed patterns
        seed_matches = self._check_seed_patterns(query_lower)
        if seed_matches:
            # Migrate matched seed pattern to SQLite for future learning
            for match in seed_matches:
                self._migrate_seed_to_learned(match["intent"], match)
            return seed_matches

        # Layer 3: LLM classification (Phase 3 only — DAY_16)
        # Return empty for now — no error injection for unknown intents
        return []

    def _check_learned_patterns(self, query_lower: str) -> List[dict]:
        """Query intent_patterns table for matching triggers.

        Performance: Only loads patterns above injection threshold at SQL level.
        This prevents unbounded memory growth as patterns accumulate.
        """
        if not self._em:
            return []
        with self._em.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM intent_patterns WHERE score >= ? "
                "ORDER BY score DESC",
                (EffectivenessScore.INJECTION_THRESHOLD,),
            ).fetchall()

        matches = []
        for row in rows:
            triggers = json.loads(row["triggers_json"])
            triggered_by = [t for t in triggers if t in query_lower]
            if triggered_by:
                matches.append({
                    "intent": row["intent_name"],
                    "score": row["score"],
                    "triggered_by": triggered_by,
                    "requires": json.loads(row["requires_json"]),
                    "source": row["source"],
                    "evidence_count": row["evidence_count"],
                })
        return matches

    def _check_seed_patterns(self, query_lower: str) -> List[dict]:
        """Check in-memory SEED_PATTERNS (bootstrap only).

        Score uses EffectivenessScore.calculate() for consistency with learned
        patterns. Initial evidence = number of triggers matched (min 2) to
        ensure seeds start above INJECTION_THRESHOLD.
        """
        matches = []
        for intent_name, pattern in self.SEED_PATTERNS.items():
            triggered_by = [t for t in pattern["triggers"] if t in query_lower]
            if triggered_by:
                # Use EffectivenessScore-compatible scoring so seed → learned
                # migration doesn't cause a scoring discontinuity.
                initial_evidence = max(2, len(triggered_by))
                matches.append({
                    "intent": intent_name,
                    "score": EffectivenessScore.calculate(initial_evidence, 0),
                    "triggered_by": triggered_by,
                    "requires": pattern["requires"],
                    "source": "seed",
                    "initial_evidence": initial_evidence,
                })
        return sorted(matches, key=lambda x: x["score"], reverse=True)

    def _migrate_seed_to_learned(self, intent_name: str, match: dict):
        """Move seed pattern to SQLite so it can accumulate evidence.

        CRITICAL: Store ALL triggers from SEED_PATTERNS, not just the matched
        subset. Otherwise 'iteration' with 8 triggers becomes 'iteration' with
        1 trigger after migration, severely reducing future match probability.

        Uses EffectivenessScore.calculate() for initial score to maintain
        consistency with the evidence-based scoring used after migration.
        """
        # Get FULL trigger list from seed patterns
        seed_pattern = self.SEED_PATTERNS.get(intent_name)
        if not seed_pattern:
            return

        seed_triggers = seed_pattern["triggers"]
        full_requires = seed_pattern["requires"]

        # Use EffectivenessScore-compatible initial values
        initial_evidence = match.get("initial_evidence", 2)
        initial_score = EffectivenessScore.calculate(initial_evidence, 0)

        if not self._em:
            return
        # Migration is a write; skip on read threads (e.g. get_hints calls).
        # Will execute next time extract_intents is called from learn().
        if threading.current_thread().name != WRITER_THREAD_NAME:
            return

        try:
            self._em._writer_conn.execute("""
                INSERT OR IGNORE INTO intent_patterns
                (intent_name, triggers_json, requires_json, source, score,
                 evidence_count, last_updated, created_at)
                VALUES (?, ?, ?, 'seed', ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
            """, (
                intent_name,
                json.dumps(seed_triggers),   # Full list, NOT match["triggered_by"]
                json.dumps(full_requires),   # Full requires, NOT match["requires"]
                initial_score,
                initial_evidence,
            ))
            self._em._writer_conn.commit()
            logger.debug(
                f"[LEARNING:STRUCTURAL] Seed pattern '{intent_name}' "
                f"migrated to learned patterns (score={initial_score}, "
                f"evidence={initial_evidence})"
            )
        except Exception as e:
            logger.debug(
                f"[LEARNING:STRUCTURAL] Seed migration skipped for "
                f"'{intent_name}': {e}"
            )


# ---------------------------------------------------------------------------
# StructuralRuleEngine — LearningEngine ABC Implementation
# ---------------------------------------------------------------------------

class StructuralRuleEngine(LearningEngine):
    """
    Learns rules about query patterns → required code structures.
    Implements LearningEngine ABC from shared infrastructure.

    Rule format in SQLite structural_rules table:
    {
        "rule_name": "iteration_required",
        "query_pattern": "all * in table|each row|every entry",
        "required_structure": "for_loop",
        "required_keywords": ["Get Elements", "FOR", "END"],
        "score": 0.85,
        "evidence_count": 12,
        "counter_evidence": 1,
    }

    Core design: learn() checks intent's requires.keywords against extracted
    code elements via line-level parsing. No hardcoded structure detectors.
    Scoring uses EffectivenessScore.calculate() — ALWAYS absolute formula.
    """

    def __init__(self, execution_memory=None, intent_extractor: IntentExtractor = None):
        self._em = execution_memory
        self.intent_extractor = intent_extractor

    def learn(self, record) -> None:
        """
        Process execution record and update structural rules.

        Called after EVERY execution (pass or fail) by FeedbackLoop.

        Evidence logic uses dynamic keyword presence checking:
        - Extracts ALL keyword calls from the robot code via line-level parsing
        - Checks if the intent's required keywords are present
        - No hardcoded structure detectors or label comparison
        """
        if not self._em:
            return
        _assert_writer_thread("StructuralRuleEngine.learn")

        intents = self.intent_extractor.extract_intents(record.user_query)

        for intent in intents:
            required_keywords = intent["requires"].get("keywords", [])
            if not required_keywords:
                continue

            rule = self._get_or_create_rule(intent)

            if record.test_status == "passed":
                if self._has_required_keywords(
                    record.robot_code, required_keywords
                ):
                    # Confirmed: query + required keywords present = success
                    self._increment_evidence(rule)
                else:
                    # Passed WITHOUT the expected keywords → counter-evidence
                    self._increment_counter_evidence(rule)

            elif record.test_status == "failed":
                if (record.failure_category
                        and record.failure_category.startswith("A")):
                    # Structural failure — STRONG confirmation that
                    # the required keywords were indeed needed
                    self._increment_evidence(rule, boost=True)

    def get_hints(self, user_query: str, url: str,
                  agent_role: str) -> Optional[List[str]]:
        """Return structural hints for the planner or assembler."""
        if agent_role not in ("planner", "assembler"):
            return None

        intents = self.intent_extractor.extract_intents(user_query)
        hints = []

        for intent in intents:
            rule = self._find_rule(intent["intent"])
            if rule and EffectivenessScore.passes_threshold(
                rule["evidence_count"], rule["counter_evidence"]
            ):
                if agent_role == "planner":
                    keywords_list = json.loads(
                        rule["required_keywords_json"] or "[]"
                    )
                    hints.append(
                        f"⚠️ STRUCTURAL: This query requires "
                        f"{rule['required_structure']} structure. "
                        f"Use {', '.join(keywords_list)}"
                    )
                elif agent_role == "assembler" and rule.get("code_template"):
                    hints.append(
                        f"📋 TEMPLATE: {rule['code_template']}"
                    )

        return hints if hints else None

    def get_stats(self) -> Dict:
        """Return engine statistics."""
        if not self._em:
            return {"total_rules": 0, "active_rules": 0, "engine": "structural_rule"}
        with self._em.read_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM structural_rules"
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM structural_rules "
                "WHERE score >= ? AND (evidence_count + counter_evidence) >= ?",
                (
                    EffectivenessScore.INJECTION_THRESHOLD,
                    EffectivenessScore.MIN_OBSERVATIONS,
                ),
            ).fetchone()[0]
        return {
            "total_rules": total,
            "active_rules": active,
            "engine": "structural_rule",
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create_rule(self, intent: dict) -> dict:
        """Get existing rule or create new one with zero counts.

        Called from learn() which is already on the writer thread.
        Uses _writer_conn throughout so the SELECT and INSERT are on the
        same connection — avoids a TOCTOU race in the INSERT OR IGNORE path.
        """
        rule_name = intent["intent"]
        row = self._em._writer_conn.execute(
            "SELECT * FROM structural_rules WHERE rule_name = ?",
            (rule_name,),
        ).fetchone()

        if row:
            return dict(row)

        # Create with evidence_count=0, counter_evidence=0, score=0.0
        requires = intent.get("requires", {})
        required_structure = requires.get("code_structure", "unknown")
        required_keywords = requires.get("keywords", [])
        query_pattern = "|".join(intent.get("triggered_by", []))

        self._em._writer_conn.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure,
             required_keywords_json, score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES (?, ?, ?, ?, 0.0, 0, 0, datetime('now', 'localtime'), datetime('now', 'localtime'))
        """, (
            rule_name,
            query_pattern,
            required_structure,
            json.dumps(required_keywords),
        ))
        self._em._writer_conn.commit()

        logger.debug(
            f"[LEARNING:STRUCTURAL] Created rule '{rule_name}' "
            f"(structure={required_structure}, "
            f"keywords={required_keywords})"
        )

        # Re-fetch to get the row with id
        return dict(self._em._writer_conn.execute(
            "SELECT * FROM structural_rules WHERE rule_name = ?",
            (rule_name,),
        ).fetchone())

    def _increment_evidence(self, rule: dict, boost: bool = False):
        """Increment evidence_count and recalculate score.

        boost=True adds +3 instead of +1 (for Category A structural failures).
        """
        increment = 3 if boost else 1
        new_evidence = rule["evidence_count"] + increment
        new_score = EffectivenessScore.calculate(
            new_evidence, rule["counter_evidence"]
        )
        self._em._writer_conn.execute(
            "UPDATE structural_rules "
            "SET evidence_count = ?, score = ?, "
            "    last_triggered = datetime('now', 'localtime'), "
            "    last_updated = datetime('now', 'localtime') "
            "WHERE id = ?",
            (new_evidence, new_score, rule["id"]),
        )
        self._em._writer_conn.commit()

        logger.debug(
            f"[LEARNING:STRUCTURAL] Rule '{rule['rule_name']}' "
            f"evidence +{increment} → {new_evidence} "
            f"(score={new_score})"
        )

    def _increment_counter_evidence(self, rule: dict):
        """Increment counter_evidence and recalculate score."""
        new_ce = rule["counter_evidence"] + 1
        new_score = EffectivenessScore.calculate(
            rule["evidence_count"], new_ce
        )
        self._em._writer_conn.execute(
            "UPDATE structural_rules "
            "SET counter_evidence = ?, score = ?, "
            "    last_updated = datetime('now', 'localtime') "
            "WHERE id = ?",
            (new_ce, new_score, rule["id"]),
        )
        self._em._writer_conn.commit()

        logger.debug(
            f"[LEARNING:STRUCTURAL] Rule '{rule['rule_name']}' "
            f"counter-evidence +1 → {new_ce} "
            f"(score={new_score})"
        )

    def _find_rule(self, intent_name: str) -> Optional[dict]:
        """Find a rule by intent name."""
        if not self._em:
            return None
        with self._em.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM structural_rules WHERE rule_name = ?",
                (intent_name,),
            ).fetchone()
        return dict(row) if row else None

    def _extract_code_elements(self, robot_code: str) -> frozenset:
        """Extract all keyword calls and control constructs from robot code.

        Parses line by line using RF's 2+ space separator convention.
        Handles continuation lines (...) by skipping the marker.
        Fully dynamic: no hardcoded keyword or construct lists.

        Returns frozenset of element names found in the code.
        """
        if not robot_code:
            return frozenset()

        elements = set()
        for line in robot_code.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") \
                    or stripped.startswith("*"):
                continue

            parts = [p for p in re.split(r"\s{2,}", stripped) if p]
            if not parts:
                continue

            first = parts[0]

            # Continuation line: skip "..." marker, use next token
            if first == "..." and len(parts) > 1:
                first = parts[1]
                parts = parts[1:]

            # Variable assignment: "${var}=", "@{list}=", "&{dict}="
            # → keyword is the next part after the assignment target
            if ("=" in first
                    and first.startswith(("$", "@", "&"))
                    and len(parts) > 1):
                elements.add(parts[1])
            else:
                elements.add(first)

        return frozenset(elements)

    def _has_required_keywords(self, robot_code: str,
                               required_keywords: list) -> bool:
        """Check if ALL required keywords are actual keyword calls in the code.

        Uses line-level extraction — no substring false positives.
        Fully dynamic: keywords come from intent patterns (seed/learned/LLM).
        """
        code_elements = self._extract_code_elements(robot_code)
        return all(kw in code_elements for kw in required_keywords)
