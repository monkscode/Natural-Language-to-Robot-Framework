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

import json
import re
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.backend.crew_ai.optimization.learning_config import (
    LearningEngine,
    extract_domain,
)
from src.backend.crew_ai.optimization.execution_memory import _assert_writer_thread


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
AUTO_DISABLE_RATIO_MIN_APPLICATIONS = 10
AUTO_DISABLE_FAILURE_RATIO = 0.6

# Trigger-flag protection (mirror of auto-disable thresholds, applied to the "good" side)
TRIGGER_FLAG_PROTECTION_MIN_APPLIED = 5
TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE = 0.70

# Failure-attribution widening (P5A). Maps a hint's original_failure_category to
# the set of new failure categories that should still count as "this hint failed
# to fix its issue". Strict same-category matching is preserved for taxonomy
# codes not listed here via the .get(orig_cat, {orig_cat}) fallback.
#
# C1↔D1 widening rationale: locator failures (C1) and timing failures (D1) are
# frequently co-symptomatic — a missing element is often just an element that
# hasn't appeared yet (timing), and a slow page makes locators appear absent.
# Widening prevents false-positive auto-disable of hints when a C1 hint is scored
# against a D1 failure or vice versa.  Trade-off: wider attribution reduces
# precision (a C1 hint gets partial credit for D1 failures it didn't cause) but
# increases recall (avoids premature disable of genuinely useful hints).
# Basis: heuristic based on failure-taxonomy reasoning, not empirical data;
# revisit if auto-disable rates look wrong once production data accumulates.
RELATED_CATEGORIES: dict[str, set[str]] = {
    "B1": {"B1"},            # keyword hint — only keyword failures
    "C1": {"C1", "D1"},      # locator → also timeout
    "D1": {"D1", "C1"},      # timing → also element-not-found
    "A1": {"A1"},             # structural — strict
}


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

    def __init__(self, execution_memory=None):
        """
        Initialize NLFeedbackEngine.

        Args:
            execution_memory: ExecutionMemory instance. Optional — engine
                              works without it (hints/learning disabled).
        """
        self._em = execution_memory
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
        if not self._em:
            return
        _assert_writer_thread("NLFeedbackEngine.learn_from_feedback")

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
        # The originating user query becomes the hint's anchor — the text the
        # similarity filter embeds and matches future queries against.
        anchor_query = getattr(record, "user_query", None)
        now = datetime.now(timezone.utc).isoformat()

        try:
            # Upsert: increment evidence if exists, else insert
            existing = self._em._writer_conn.execute(
                "SELECT id, evidence_count, conflict_flagged FROM nl_feedback_corrections "
                "WHERE feedback_text = ? AND domain IS ? AND scope = ?",
                (feedback_text.strip(), domain, scope),
            ).fetchone()

            new_hint_id = None
            if existing:
                # Gap 7: user re-submitting identical feedback text is the
                # strongest possible authoritative signal that the hint is
                # correct — strictly more reliable than any LLM judgment.
                # Reset conflict_flagged + its metadata so a previously-flagged
                # hint resumes injection on the next workflow. If the LLM was
                # actually right and the hint is genuinely harmful, Trigger 1
                # will re-flag it on the next Case B with fresh evidence; the
                # system re-learns from real outcomes rather than a single
                # past LLM call the user has now contested.
                #
                # Step 8 will additionally write a hint_audit row here (action
                # 'unflag') when the pre-update row had conflict_flagged=1, so
                # the Phase 2 LLM-accuracy KPI counts implicit overrides the
                # same way as explicit Unflag button clicks.
                self._em._writer_conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET evidence_count = evidence_count + 1, "
                    "    last_seen = ?, "
                    "    is_active = 1, "
                    "    conflict_flagged = 0, "
                    "    conflict_flagged_at = NULL, "
                    "    conflict_flag_reason = NULL "
                    "WHERE id = ?",
                    (now, existing["id"]),
                )
                if existing["conflict_flagged"] == 1:
                    self._em._writer_conn.execute(
                        "INSERT INTO hint_audit "
                        "(hint_id, action, actor, reason, before_value, after_value, created_at) "
                        "VALUES (?, 'unflag', ?, ?, ?, ?, ?)",
                        (
                            existing["id"],
                            "user1",
                            "User re-submitted identical feedback — implicit override of LLM flag",
                            json.dumps({"conflict_flagged": 1}),
                            json.dumps({"conflict_flagged": 0}),
                            now,
                        ),
                    )
                logger.info(
                    "[LEARNING:NL] Reinforced feedback '%s' for %s "
                    "(evidence=%d)",
                    feedback_text[:50], domain,
                    existing["evidence_count"] + 1,
                )
            else:
                cursor = self._em._writer_conn.execute(
                    "INSERT INTO nl_feedback_corrections "
                    "(feedback_text, category, scope, domain, url, "
                    " original_failure_category, evidence_count, anchor_query, "
                    " source_workflow_id, created_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        feedback_text.strip(), category, scope,
                        domain, url, failure_category,
                        anchor_query, workflow_id, now, now,
                    ),
                )
                new_hint_id = cursor.lastrowid
                logger.info(
                    "[LEARNING:NL] Stored new feedback correction: "
                    "'%s' scope=%s domain=%s",
                    feedback_text[:50], scope, domain,
                )

            self._em._writer_conn.commit()

            # After the SQL commit, embed the new hint's anchor query into
            # learning_anchors so the similarity filter can match it. Inline
            # — learn_from_feedback already runs on the writer thread.
            # add_anchor is best-effort (it swallows ChromaDB errors); a
            # missed doc is healed by the next reconcile. Reinforced hints
            # (the UPSERT branch) keep their original anchor unchanged —
            # single-anchor design.
            if new_hint_id is not None:
                self._em.add_anchor("nl", new_hint_id, anchor_query)

        except Exception as e:
            logger.warning(
                "[LEARNING:NL] Failed to store feedback correction: %s", e,
            )

    def get_hints(
        self, user_query: str, url: str, agent_role: str,
    ) -> Optional[List[str]]:
        """Return formatted feedback hints for the current context, or None if none apply."""
        hints, _ = self.get_hints_with_ids(user_query, url, agent_role)
        return hints if hints else None

    def get_hints_with_ids(
        self, user_query: str, url: str, agent_role: str,
    ) -> tuple[list[str], list[int]]:
        """Like get_hints() but also returns the DB ids of the selected hints.

        Returns (hints, ids) — parallel lists, same order.
        Returns ([], []) when there are no applicable hints or DB is unavailable.
        """
        if not self._em:
            return [], []
        # Decision 10: an empty user_query carries no relevance signal —
        # aligns with CLAUDE.md "skip learning when user_query is empty".
        if not user_query or not user_query.strip():
            return [], []

        domain = extract_domain(url) if url else None

        try:
            with self._em.read_conn() as conn:
                rows = conn.execute(
                    "SELECT id, feedback_text, category, scope, "
                    "       success_count, evidence_count "
                    "FROM nl_feedback_corrections "
                    "WHERE is_active = 1 "
                    "AND conflict_flagged = 0 "
                    f"AND ({self._SCOPE_WHERE}) "
                    "ORDER BY last_seen DESC, "
                    "         evidence_count DESC, "
                    "         success_count DESC "
                    "LIMIT 100",
                    (domain, url),
                ).fetchall()
        except Exception as e:
            logger.warning("[LEARNING:NL] get_hints_with_ids query failed: %s", e)
            return [], []

        if not rows:
            return [], []

        # Query-similarity filter: keep only the hints whose anchor query is
        # semantically close to user_query. Drops scope-matched-but-unrelated
        # hints (e.g. a login hint surfacing on a checkout test). The pool is
        # LIMIT 100 (recency-ordered) so the filter has room to work; the cap
        # of 5 is applied after. filter_by_query_similarity never raises — on
        # any ChromaDB problem it fails open (returns all candidates),
        # degrading gracefully to the old scope-only behaviour.
        survivors = self._em.filter_by_query_similarity(
            user_query, [r["id"] for r in rows], kind="nl",
        )
        rows = [r for r in rows if r["id"] in survivors]
        if not rows:
            return [], []

        deduped = self._deduplicate_hints(
            [{"feedback_text": r["feedback_text"], "id": r["id"]} for r in rows]
        )
        selected = deduped[:5]
        hints = [self._format_feedback_hint(h["feedback_text"]) for h in selected]
        ids = [h["id"] for h in selected]
        return hints, ids

    def _select_hints(self, where_clause: str, params) -> list[dict]:
        """SELECT id, feedback_text, applied_count, success_count, failure_count, created_at
        FROM nl_feedback_corrections WHERE <where_clause>."""
        with self._em.read_conn() as conn:
            rows = conn.execute(
                "SELECT id, feedback_text, applied_count, success_count, "
                "       failure_count, created_at FROM nl_feedback_corrections "
                f"WHERE {where_clause}",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_hints_raw(
        self, domain: Optional[str], url: Optional[str],
    ) -> List[Dict]:
        """Return raw active, unflagged hints in scope with usage metadata.

        Each dict has id, feedback_text, applied_count, success_count,
        failure_count, created_at — all populated so _hint_line renders the
        full metadata block in the conflict-detection prompt.
        Distinct from get_hints() — the LLM conflict-detection triggers need the
        raw text plus hint id to flag specific rows by id.  Returns [] (never None).
        """
        if not self._em:
            return []
        try:
            return self._select_hints(
                f"is_active = 1 AND conflict_flagged = 0 AND ({self._SCOPE_WHERE})",
                (domain, url),
            )
        except Exception as e:
            logger.warning("[LEARNING:NL] get_active_hints_raw failed: %s", e)
            return []

    def get_hints_by_id(self, ids: list[int]) -> List[Dict]:
        """Return raw active, unflagged hints for the given IDs.

        Same filter as get_active_hints_raw (is_active=1, conflict_flagged=0).
        A hint disabled or flagged between injection and trigger-fire is excluded.
        ids=None → warning + [].  ids=[] → [] immediately.
        """
        if ids is None:
            logger.warning("[LEARNING:NL] get_hints_by_id called with None — returning []")
            return []
        if not ids:
            return []
        if not self._em:
            return []
        try:
            placeholders = ",".join("?" * len(ids))
            return self._select_hints(
                f"id IN ({placeholders}) AND is_active = 1 AND conflict_flagged = 0",
                ids,
            )
        except Exception as e:
            logger.warning("[LEARNING:NL] get_hints_by_id failed: %s", e)
            return []

    def conflict_flag_hints(
        self,
        per_hint_reasons: dict[int, str],
        trigger_type: str,
    ) -> list[int]:
        """Suspend hints from injection without permanently deactivating them.

        Strong-history guard: hints with a proven track record
        (applied >= TRIGGER_FLAG_PROTECTION_MIN_APPLIED AND
        success_rate >= TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE) are NOT
        flagged by a single trigger call. The trigger_events row is still
        written by the caller, so batch hint review can act on accumulated
        evidence if multiple triggers fire against the same hint.

        Symmetric with auto-disable: auto-disable requires applied>=10 AND
        failure_rate>0.6 to deactivate. This protective floor (applied>=5
        AND success_rate>=0.7) is the inverse — a hint proven good is
        protected from single-shot flagging. Low-history hints (applied<5)
        still flag immediately — LLM judgment is the best evidence available.

        Args:
            per_hint_reasons: {hint_id: reason_string} — per-hint LLM reason.
            trigger_type: "trigger_1" or "trigger_2" — stored in the audit row.

        Returns:
            List of hint IDs that were actually flagged (committed). Hints
            suppressed by the strong-history guard are excluded. Empty list
            when nothing was flagged or the transaction rolled back. Caller
            (fire_conflict_detection) writes this to
            trigger_events.actually_flagged_hint_ids so engagement-rate KPIs
            measure enforcement, not LLM recommendation.

        Recovery paths (clear conflict_flagged back to 0):
        1. User re-submits identical feedback_text — the UPSERT branch of
           learn_from_feedback resets the flag. Treated as an explicit override.
        2. Admin clicks "Unflag" in the /learning dashboard.
        """
        _assert_writer_thread("NLFeedbackEngine.conflict_flag_hints")
        if not self._em or not per_hint_reasons:
            return []
        actually_flagged: list[int] = []
        try:
            now = datetime.now(timezone.utc).isoformat()
            audit_action = f"{trigger_type}_flag"
            for hint_id, reason in per_hint_reasons.items():
                hint = self._em._writer_conn.execute(
                    "SELECT applied_count, success_count "
                    "FROM nl_feedback_corrections WHERE id = ?",
                    (hint_id,),
                ).fetchone()

                if hint is None:
                    logger.warning(
                        "[LEARNING:NL] conflict_flag_hints: hint id=%d not found, skipping",
                        hint_id,
                    )
                    continue

                applied = hint["applied_count"] or 0
                success = hint["success_count"] or 0

                if applied >= TRIGGER_FLAG_PROTECTION_MIN_APPLIED:
                    success_rate = success / applied
                    if success_rate >= TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE:
                        logger.info(
                            "[LEARNING:NL] Trigger flag suppressed for hint %d — "
                            "strong history (applied=%d, success_rate=%.0f%%). "
                            "Evidence recorded in trigger_events; batch hint review "
                            "will reconsider if multiple triggers accumulate.",
                            hint_id, applied, success_rate * 100,
                        )
                        continue

                self._em._writer_conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET conflict_flagged = 1, "
                    "    conflict_flagged_at = ?, "
                    "    conflict_flag_reason = ? "
                    "WHERE id = ?",
                    (now, reason, hint_id),
                )
                try:
                    self._em._writer_conn.execute(
                        "INSERT INTO hint_audit "
                        "(hint_id, action, actor, reason, "
                        " before_value, after_value, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            hint_id, audit_action, trigger_type, reason,
                            json.dumps({"conflict_flagged": 0}),
                            json.dumps({
                                "conflict_flagged": 1,
                                "conflict_flag_reason": reason,
                            }),
                            now,
                        ),
                    )
                except Exception as audit_err:
                    logger.warning(
                        "[LEARNING:NL] hint_audit write failed for id=%d "
                        "(non-blocking): %s", hint_id, audit_err,
                    )
                actually_flagged.append(hint_id)
            self._em._writer_conn.commit()
            logger.info(
                "[LEARNING:NL] Trigger %s flagged %d/%d hint(s); rest "
                "suppressed by strong-history guard. flagged=%s",
                trigger_type, len(actually_flagged),
                len(per_hint_reasons), actually_flagged,
            )
            return actually_flagged
        except Exception as e:
            try:
                self._em._writer_conn.rollback()
            except Exception as rb_err:
                logger.warning(
                    "[LEARNING:NL] conflict_flag_hints rollback failed: %s",
                    rb_err,
                )
            logger.warning(
                "[LEARNING:NL] conflict_flag_hints failed: %s", e,
            )
            # Rollback discarded every UPDATE/INSERT, so nothing is committed
            # in the DB. Return [] so the caller writes an accurate empty
            # actually_flagged_hint_ids to trigger_events.
            return []

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
        injected_hint_ids: Optional[str] = None,
    ) -> None:
        """
        Track effectiveness of active hints after test execution.

        Uses smart failure attribution via category cross-referencing:
        - When test passed: increment applied_count + success_count for
          injected hints only (or all active in-scope for legacy NULL rows).
        - When test failed: compare original_failure_category with
          new failure. Same category → hint didn't fix its issue →
          increment failure_count. Different category → unrelated → skip.
        - Auto-disable (never-succeeded): if failure_count > 0 AND
          success_count == 0 AND applied_count >= 3 → is_active = 0.
        - Auto-disable (ratio): if applied_count >= 10 AND
          failure_count / applied_count > 0.6 → is_active = 0. Catches
          hints that used to work but are now consistently failing.

        Args:
            injected_hint_ids: JSON string of hint IDs that were actually
                injected (e.g. '[5, 12]'). '[]' = known-empty (no NL hints
                injected, skip counters). None = legacy row (all-in-scope
                fallback for backward compatibility).

        Called by FeedbackLoop.process_execution() after every test run.
        """
        if not self._em:
            return
        _assert_writer_thread("NLFeedbackEngine.update_hint_effectiveness")

        _processed_count = 0
        _total_hints = 0
        try:
            ids: list | None = None
            if injected_hint_ids is not None:
                try:
                    parsed = json.loads(injected_hint_ids)
                except json.JSONDecodeError:
                    logger.warning(
                        "[LEARNING:NL] malformed injected_hint_ids, falling back "
                        "to scope-wide path: %r",
                        injected_hint_ids,
                    )
                    parsed = None
                ids = parsed if isinstance(parsed, list) else None

            if ids is not None:
                # New path: credit only the hints that were actually injected.
                if not ids:
                    # '[]' = known-empty — no NL hints shaped this test, no-op.
                    return
                placeholders = ",".join("?" * len(ids))
                active_hints = self._em._writer_conn.execute(
                    "SELECT id, original_failure_category, "
                    "       applied_count, success_count, failure_count "
                    "FROM nl_feedback_corrections "
                    f"WHERE id IN ({placeholders}) "
                    "AND is_active = 1 AND conflict_flagged = 0",
                    ids,
                ).fetchall()
            else:
                # Legacy path (NULL injected_hint_ids or malformed JSON): fall
                # back to all active in-scope hints. Preserves behaviour for
                # rows written before schema v10. Excludes conflict_flagged=1
                # hints — already suspended; crediting them is noise.
                active_hints = self._em._writer_conn.execute(
                    "SELECT id, original_failure_category, "
                    "       applied_count, success_count, failure_count "
                    "FROM nl_feedback_corrections "
                    "WHERE is_active = 1 AND conflict_flagged = 0 "
                    f"AND ({self._SCOPE_WHERE})",
                    (domain, url),
                ).fetchall()

            if not active_hints:
                return
            _total_hints = len(active_hints)

            for hint in active_hints:
                hint_id = hint["id"]
                new_applied = hint["applied_count"] + 1
                new_success = hint["success_count"]
                new_failure = hint["failure_count"]

                if test_passed:
                    new_success += 1
                else:
                    # Smart attribution: penalize if new failure category is
                    # in the hint's related set (P5A widening). Defaults to
                    # strict same-category match for codes not in
                    # RELATED_CATEGORIES.
                    orig_cat = hint["original_failure_category"]
                    if orig_cat and new_failure_category:
                        related = RELATED_CATEGORIES.get(orig_cat, {orig_cat})
                        if new_failure_category in related:
                            new_failure += 1
                    # No category on either side → no penalty

                # Update counts
                self._em._writer_conn.execute(
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
                    now_iso = datetime.now(timezone.utc).isoformat()
                    self._em._writer_conn.execute(
                        "UPDATE nl_feedback_corrections "
                        "SET is_active = 0, disabled_at = ? WHERE id = ?",
                        (now_iso, hint_id),
                    )
                    try:
                        self._em._writer_conn.execute(
                            "INSERT INTO hint_audit "
                            "(hint_id, action, actor, reason, before_value, after_value, created_at) "
                            "VALUES (?, 'auto_disable', 'system', 'never_succeeded', ?, ?, ?)",
                            (
                                hint_id,
                                json.dumps({"applied_count": hint["applied_count"],
                                            "success_count": hint["success_count"],
                                            "failure_count": hint["failure_count"]}),
                                json.dumps({"is_active": 0, "applied_count": new_applied,
                                            "success_count": new_success,
                                            "failure_count": new_failure}),
                                now_iso,
                            ),
                        )
                    except Exception as audit_err:
                        logger.warning(
                            "[LEARNING:NL] Auto-disable audit write failed (non-blocking): %s",
                            audit_err,
                        )
                    logger.info(
                        "[LEARNING:NL] Auto-disabled hint id=%d "
                        "(applied=%d, success=0, failure=%d)",
                        hint_id, new_applied, new_failure,
                    )
                elif (
                    new_applied >= AUTO_DISABLE_RATIO_MIN_APPLICATIONS
                    and new_failure / new_applied > AUTO_DISABLE_FAILURE_RATIO
                ):
                    now_iso = datetime.now(timezone.utc).isoformat()
                    self._em._writer_conn.execute(
                        "UPDATE nl_feedback_corrections "
                        "SET is_active = 0, disabled_at = ? WHERE id = ?",
                        (now_iso, hint_id),
                    )
                    try:
                        self._em._writer_conn.execute(
                            "INSERT INTO hint_audit "
                            "(hint_id, action, actor, reason, before_value, after_value, created_at) "
                            "VALUES (?, 'auto_disable', 'system', 'failure_rate_exceeded', ?, ?, ?)",
                            (
                                hint_id,
                                json.dumps({"applied_count": hint["applied_count"],
                                            "success_count": hint["success_count"],
                                            "failure_count": hint["failure_count"]}),
                                json.dumps({"is_active": 0, "applied_count": new_applied,
                                            "success_count": new_success,
                                            "failure_count": new_failure}),
                                now_iso,
                            ),
                        )
                    except Exception as audit_err:
                        logger.warning(
                            "[LEARNING:NL] Auto-disable audit write failed (non-blocking): %s",
                            audit_err,
                        )
                    logger.info(
                        "[LEARNING:NL] Auto-disabled hint id=%d "
                        "(applied=%d, success=%d, failure=%d, ratio=%.2f)",
                        hint_id, new_applied, new_success, new_failure,
                        new_failure / new_applied,
                    )

                _processed_count += 1

            self._em._writer_conn.commit()

        except Exception as e:
            try:
                self._em._writer_conn.rollback()
            except Exception as rb_err:
                logger.warning(
                    "[LEARNING:NL] update_hint_effectiveness rollback failed: %s",
                    rb_err,
                )
            logger.warning(
                "[LEARNING:NL] update_hint_effectiveness failed after %d/%d "
                "hint(s) — all changes rolled back "
                "(domain=%s, url=%s, passed=%s): %s",
                _processed_count, _total_hints, domain, url, test_passed, e,
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
