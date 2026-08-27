"""
NL Feedback Engine — Learns from natural language user feedback.

Implements LearningEngine ABC with a 3-layer auto-triage pipeline.
Phase 1 implements Layer 1 only (seed patterns via regex).

Feedback Pipeline:
    1. Triage (process_feedback) — classify feedback category via seed patterns
    2. Storage (learn_from_feedback) — store correction in nl_feedback_corrections
    3. Retrieval (get_hints) — query corrections by scope, return deduplicated hints
    4. Attribution (apply_hint_attribution) — on a passing run, credit only the
       hints actually used in the code (one LLM judgment per workflow) and retire
       over-surfaced dead weight; sole writer of the usage counters

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

import hashlib
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

# Trigger-flag protection. Strong-history hints are shielded from single-shot
# flagging. Evaluated on USED outcomes (success + failure), NOT applied
# (injections) — so over-surfacing / unused can't strip a good hint's protection
# (S1). The MIN_APPLIED name is kept for continuity but now means "minimum used
# outcomes" (success + failure).
TRIGGER_FLAG_PROTECTION_MIN_APPLIED = 5
TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE = 0.70

# Over-surfaced dead-weight retirement (Part 2 / G1). A hint injected repeatedly
# but never used (unused_count >= threshold) with zero successes is retired — but
# only past an age floor, so a brand-new mis-surfaced hint isn't killed before
# its right context appears. created_at is already on the row (no schema change).
UNUSED_RETIRE_THRESHOLD = 5
UNUSED_RETIRE_MIN_AGE_DAYS = 30

# Audit reason for the unused-retirement auto-disable. Shared constant so the F6
# retirement-reversal metric (learning_endpoints) joins on the same string — a
# literal mismatch would silently make that metric read 0.
AUTO_DISABLE_REASON_NEVER_USED = "never_used"

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

    # Scope WHERE clause shared by get_hints_with_ids and get_active_hints_raw.
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
            # Upsert: increment evidence if exists, else insert.
            # URL-scoped hints include url in the dedup key so identical feedback
            # on two different pages under the same domain creates separate rows.
            # org_id is part of the dedup key: hints are org-private, so another
            # org's identical text must create that org's own row — matching
            # cross-org would let one tenant's feedback strengthen, reactivate,
            # or unflag another tenant's hint (and silently drop its own).
            if scope == "url":
                existing = self._em._writer_conn.execute(
                    "SELECT id, evidence_count, conflict_flagged "
                    "FROM nl_feedback_corrections "
                    "WHERE feedback_text = ? AND domain IS NOT DISTINCT FROM ? "
                    "AND url IS NOT DISTINCT FROM ? AND scope = ? "
                    "AND org_id IS NOT DISTINCT FROM ?",
                    (feedback_text.strip(), domain, url, scope, record.org_id),
                ).fetchone()
            else:
                existing = self._em._writer_conn.execute(
                    "SELECT id, evidence_count, conflict_flagged "
                    "FROM nl_feedback_corrections "
                    "WHERE feedback_text = ? AND domain IS NOT DISTINCT FROM ? AND scope = ? "
                    "AND org_id IS NOT DISTINCT FROM ?",
                    (feedback_text.strip(), domain, scope, record.org_id),
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
                    "    conflict_flag_reason = NULL, "
                    # Step 4b: a re-submission is a fresh chance — clear the
                    # over-surfaced dead-weight counter so the hint is not
                    # immediately re-retired. success/failure are preserved.
                    "    unused_count = 0 "
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
                            feedback_insight.get("actor") or "unknown",
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
                    " source_workflow_id, org_id, created_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?) RETURNING id",
                    (
                        feedback_text.strip(), category, scope,
                        domain, url, failure_category,
                        anchor_query, workflow_id, record.org_id, now, now,
                    ),
                )
                new_hint_id = cursor.fetchone()["id"]
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
                self._em.add_anchor("nl", new_hint_id, anchor_query, org_id=record.org_id)

        except Exception as e:
            logger.warning(
                "[LEARNING:NL] Failed to store feedback correction: %s", e,
            )

    def get_hints(
        self, user_query: str, url: str, agent_role: str,
        org_id: str | None = None,
    ) -> Optional[List[str]]:
        """Return formatted feedback hints for the current context, or None if none apply."""
        hints, _ = self.get_hints_with_ids(user_query, url, agent_role, org_id=org_id)
        return hints if hints else None

    def get_hints_with_ids(
        self, user_query: str, url: str, agent_role: str,
        selection_trace: dict | None = None,
        org_id: str | None = None,
    ) -> tuple[list[str], list[int]]:
        """Like get_hints() but also returns the DB ids of the selected hints.

        Returns (hints, ids) — parallel lists, same order.
        Returns ([], []) when there are no applicable hints or DB is unavailable.

        selection_trace: opt-in N3 observability out-param (F2b). When a dict is
            passed, it is filled {hint_id: {scope, source, priority,
            similarity_score, available, injected, drop_reason}} recording each
            candidate's STAGE 2-4 fate (similarity → dedup → internal [:5] cap).
            `available`/`injected` start 0 and `drop_reason` None for survivors;
            stage 5 (holdout / outer cap / injected) is overlaid later by the
            SmartKeywordProvider. PURELY OBSERVABILITY — the returned (hints,
            ids) are byte-identical with or without it. Default None = zero cost.
        """
        if not self._em:
            return [], []
        # Decision 10: an empty user_query carries no relevance signal —
        # aligns with CLAUDE.md "skip learning when user_query is empty".
        if not user_query or not user_query.strip():
            return [], []

        domain = extract_domain(url) if url else None

        org_filter = " AND (org_id = ? OR is_shared = 1)" if org_id is not None else ""
        params = (domain, url) if org_id is None else (domain, url, org_id)
        try:
            with self._em.read_conn() as conn:
                rows = conn.execute(
                    "SELECT id, feedback_text, category, scope, "
                    "       success_count, evidence_count "
                    "FROM nl_feedback_corrections "
                    "WHERE is_active = 1 "
                    "AND conflict_flagged = 0 "
                    f"AND ({self._SCOPE_WHERE}) "
                    f"{org_filter}"
                    "ORDER BY last_seen DESC, "
                    "         evidence_count DESC, "
                    "         success_count DESC "
                    "LIMIT 100",
                    params,
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
        # score_sink (local, only when tracing) captures per-candidate sim +
        # outcome from the similarity stage for the N3 trace (F2b). It stays
        # encapsulated here; the filter's return is byte-identical with/without.
        score_sink = {} if selection_trace is not None else None
        survivors = self._em.filter_by_query_similarity(
            user_query, [r["id"] for r in rows], kind="nl", score_sink=score_sink,
            org_id=org_id,
        )
        survivor_rows = [r for r in rows if r["id"] in survivors]
        if not survivor_rows:
            # Every candidate was dropped at similarity — still record them so
            # the dashboard can show why (similarity_below / no_anchor).
            self._record_selection_trace(
                selection_trace, rows, score_sink, survivors,
                deduped_ids=set(), returned_ids=set(),
            )
            return [], []

        deduped = self._deduplicate_hints(
            [{"feedback_text": r["feedback_text"], "id": r["id"]}
             for r in survivor_rows]
        )
        selected = deduped[:5]
        hints = [self._format_feedback_hint(h["feedback_text"]) for h in selected]
        ids = [h["id"] for h in selected]
        self._record_selection_trace(
            selection_trace, rows, score_sink, survivors,
            deduped_ids={h["id"] for h in deduped},
            returned_ids=set(ids),
        )
        return hints, ids

    @staticmethod
    def _record_selection_trace(selection_trace, rows, score_sink, survivors,
                                deduped_ids, returned_ids):
        """Populate the opt-in N3 selection trace (F2b) with each NL
        candidate's STAGE 2-4 fate. Best-effort: any failure is swallowed so
        the hint path is never affected (FR4). Caps (internal [:5]) and the
        outer per-agent cap both read as `cap`; `dedup` stays distinct.
        """
        if selection_trace is None:
            return
        try:
            sink = score_sink or {}
            for r in rows:
                rid = r["id"]
                info = sink.get(rid, {})
                entry = {
                    "scope": r["scope"],
                    "source": "nl",
                    "priority": "high",
                    "similarity_score": info.get("sim"),
                    "available": 0,
                    "injected": 0,
                    "drop_reason": None,
                }
                if rid not in survivors:
                    # similarity-stage drop: similarity_below / no_anchor.
                    entry["drop_reason"] = info.get("outcome")
                elif rid not in deduped_ids:
                    entry["drop_reason"] = "dedup"
                elif rid not in returned_ids:
                    entry["drop_reason"] = "cap"          # internal [:5] cut
                else:
                    entry["available"] = 1                # reached the pool
                selection_trace[rid] = entry
        except Exception as e:
            logger.warning(
                "[LEARNING:NL] selection_trace capture failed (non-blocking): %s",
                e,
            )

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
        org_id: str | None = None,
    ) -> List[Dict]:
        """Return raw active, unflagged hints in scope with usage metadata.

        Each dict has id, feedback_text, applied_count, success_count,
        failure_count, created_at — all populated so _hint_line renders the
        full metadata block in the conflict-detection prompt.
        Distinct from get_hints() — the LLM conflict-detection triggers need the
        raw text plus hint id to flag specific rows by id.  Returns [] (never None).

        When org_id is set, only the caller's org hints plus is_shared=1 hints
        are returned (privacy gate).
        """
        if not self._em:
            return []
        try:
            org_filter = " AND (org_id = ? OR is_shared = 1)" if org_id is not None else ""
            params = (domain, url) if org_id is None else (domain, url, org_id)
            return self._select_hints(
                f"is_active = 1 AND conflict_flagged = 0 AND ({self._SCOPE_WHERE}){org_filter}",
                params,
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
        (success+failure >= TRIGGER_FLAG_PROTECTION_MIN_APPLIED AND
        success/(success+failure) >= TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE)
        are NOT flagged by a single trigger call. The trigger_events row is
        still written by the caller, so batch hint review can act on accumulated
        evidence if multiple triggers fire against the same hint.

        The guard reads USED outcomes (success+failure), not applied
        (injections) — over-surfacing/unused can't strip a good hint's
        protection (S1). Hints with few used outcomes still flag immediately —
        LLM judgment is the best evidence available. The flagging loop itself
        lives in _flag_hints_no_commit, shared with apply_hint_attribution so a
        Case-B credit + harm-flag commit atomically.

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
        if not self._em or not per_hint_reasons:
            return []
        _assert_writer_thread("NLFeedbackEngine.conflict_flag_hints")
        try:
            now = datetime.now(timezone.utc).isoformat()
            actually_flagged = self._flag_hints_no_commit(
                per_hint_reasons, trigger_type, now,
            )
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

    def _flag_hints_no_commit(
        self,
        per_hint_reasons: dict[int, str],
        trigger_type: str,
        now: str,
    ) -> list[int]:
        """Flagging core shared by conflict_flag_hints (Trigger 2) and
        apply_hint_attribution (Case-B harmful-removed set).

        Runs the SELECT -> strong-history-guard -> UPDATE conflict_flagged ->
        audit loop WITHOUT commit or rollback: the CALLER owns the transaction,
        so the flag commits atomically with whatever else the caller wrote (the
        usage-attribution counters, for apply_hint_attribution). The caller
        asserts the writer thread.

        Strong-history guard (S1): a hint with >= TRIGGER_FLAG_PROTECTION_MIN_APPLIED
        USED outcomes (success + failure) and success/(success+failure)
        >= TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE is shielded from single-shot
        flagging — evaluated on used outcomes, not applied, so over-surfacing /
        unused can't strip protection. Reads PRE-increment counts
        (apply_hint_attribution calls this BEFORE applying its increments).

        Returns the ids actually flagged (protected hints excluded).
        """
        actually_flagged: list[int] = []
        audit_action = f"{trigger_type}_flag"
        for hint_id, reason in per_hint_reasons.items():
            hint = self._em._writer_conn.execute(
                "SELECT success_count, failure_count "
                "FROM nl_feedback_corrections WHERE id = ?",
                (hint_id,),
            ).fetchone()

            if hint is None:
                logger.warning(
                    "[LEARNING:NL] flag core: hint id=%d not found, skipping",
                    hint_id,
                )
                continue

            success = hint["success_count"] or 0
            failure = hint["failure_count"] or 0
            used = success + failure

            if used >= TRIGGER_FLAG_PROTECTION_MIN_APPLIED:
                success_rate = success / used
                if success_rate >= TRIGGER_FLAG_PROTECTION_MIN_SUCCESS_RATE:
                    logger.info(
                        "[LEARNING:NL] Flag suppressed for hint %d — strong "
                        "history (used=%d, success_rate=%.0f%%). Evidence "
                        "recorded; batch review reconsiders if triggers accumulate.",
                        hint_id, used, success_rate * 100,
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
        return actually_flagged

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

    def apply_hint_attribution(
        self,
        workflow_id: str,
        used_ids: list[int],
        failure_ids: list[int],
        unused_ids: list[int],
        *,
        reasons: Optional[dict[int, str]] = None,
    ) -> Optional[list[int]]:
        """Sole writer of NL-hint usage counters (Part 2). Runs on the writer
        thread inside ONE atomic transaction (P2/P3).

        Once-guard (B2/P5): atomically CLAIM the workflow first —
            UPDATE execution_records SET hint_attribution_done = 1
            WHERE workflow_id = ? AND hint_attribution_done = 0
        If it affects 0 rows (already credited, or no row — dedup/holdout/
        missing), the method credits nothing. The single write queue serialises
        writes, so concurrent duplicate runs of one workflow_id credit once.

        Buckets (membership-guarded upstream against the resolved active set by
        fire_usage_attribution; this method touches ONLY the explicit ids passed
        in — P4, there is no scope-wide path):
            used    -> applied_count += 1, success_count += 1
            failure -> applied_count += 1, failure_count += 1, then conflict-flag
            unused  -> applied_count += 1, unused_count  += 1

        G4 — any id present in more than one bucket is dropped from all three
        (no-signal) and logged. fire_usage_attribution already guarantees
        disjoint sets, but apply must never SILENTLY double-count if that ever
        regresses.

        N2 ordering inside the transaction: claim -> flag (strong-history guard
        reads PRE-increment counts, so the triggering verdict isn't in its own
        denominator) -> apply increments -> record the run's source in
        hint_evidence (T3) -> auto-disable / retire on POST-increment counts
        -> commit.

        hint_evidence (T3) records the independent SOURCE behind each outcome —
        the run's normalised user_query — one row per (hint, source, bucket).
        The event counters above still count every event (F6: they feed the
        conflict-detection prompt, the review dashboard and the FR5 identity);
        the evidence rows are additive, and are what the lifecycle rules read
        when they need diversity rather than repetition.

        Disable/retire rules, evaluated for EVERY touched hint (S1-R3) on the
        (success+failure) used-outcome basis:
          - never-succeeded (FR2, AUTOMATIC): failure>0 AND success==0 AND
            (success+failure) >= AUTO_DISABLE_MIN_APPLICATIONS -> disable
            (reason 'never_succeeded').
          - over-surfaced dead weight (G1): unused >= UNUSED_RETIRE_THRESHOLD AND
            success==0 AND age >= UNUSED_RETIRE_MIN_AGE_DAYS -> disable
            (reason 'never_used').
        The high-harm-RATIO rule is INFORM-ONLY this phase (FR2) — surfaced on
        the review panel, not auto-disabled here.

        Replaces the former all-injected per-execution crediting (Part 1).
        Called only from fire_usage_attribution via the learning write queue.

        Returns the list of hint ids actually flagged as harmful (possibly empty)
        when the claim is won and the transaction commits; None when the claim is
        lost (already attributed / no row — dedup/holdout) or the no-em guard /
        rollback path fires. fire_usage_attribution writes that list to
        trigger_events.actually_flagged_hint_ids (distinct from the recommended
        failure set, so strong-history-suppressed harmful hints are not counted
        as enforced).
        """
        if not self._em:
            return None
        _assert_writer_thread("NLFeedbackEngine.apply_hint_attribution")

        reasons = reasons or {}
        used_ids = list(used_ids or [])
        failure_ids = list(failure_ids or [])
        unused_ids = list(unused_ids or [])

        # G4 — drop any id present in more than one bucket (no-signal).
        used_set = set(used_ids)
        failure_set = set(failure_ids)
        unused_set = set(unused_ids)
        overlap = (
            (used_set & failure_set)
            | (used_set & unused_set)
            | (failure_set & unused_set)
        )
        if overlap:
            logger.warning(
                "[LEARNING:NL] apply_hint_attribution: %d id(s) in >1 bucket "
                "(used/failure/unused) — dropping from all as no-signal: %s",
                len(overlap), sorted(overlap),
            )
            used_ids = [i for i in used_ids if i not in overlap]
            failure_ids = [i for i in failure_ids if i not in overlap]
            unused_ids = [i for i in unused_ids if i not in overlap]

        conn = self._em._writer_conn
        try:
            # Atomic once-guard claim (B2/P5).
            claimed = conn.execute(
                "UPDATE execution_records SET hint_attribution_done = 1 "
                "WHERE workflow_id = ? AND hint_attribution_done = 0",
                (workflow_id,),
            ).rowcount
            if claimed != 1:
                # Already attributed, or no row (dedup/holdout/missing): the
                # UPDATE changed nothing. Release the transaction and skip.
                conn.commit()
                return None

            bucket: dict[int, str] = {}
            for i in used_ids:
                bucket[i] = "used"
            for i in failure_ids:
                bucket[i] = "failure"
            for i in unused_ids:
                bucket[i] = "unused"

            if not bucket:
                # Claim won but nothing to apply (all dropped by G4, or empty
                # buckets). The done flag is set (no-op); don't retry.
                conn.commit()
                return []

            now = datetime.now(timezone.utc).isoformat()

            # T3: the independent SOURCE behind every credit this run makes —
            # the run's own normalised query. The claim above matched a row, so
            # this transaction holds it and the read cannot miss it. Normalised
            # in Python (not SQL LOWER/TRIM) so the value hashed is exactly the
            # value stored. _process_learning skips learning on an empty query,
            # so an empty source_key means a direct caller, not a product path;
            # all such runs then share one source, which no rule can be tripped by.
            src_row = conn.execute(
                "SELECT user_query FROM execution_records WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            source_key = ((src_row["user_query"] if src_row else "") or "").strip().lower()
            source_hash = hashlib.sha256(source_key.encode("utf-8")).hexdigest()

            # Read each touched hint's PRE-increment counts ONCE (N2). Only
            # active, unflagged hints — a hint disabled/flagged since injection
            # is excluded here and silently skipped.
            placeholders = ",".join("?" * len(bucket))
            rows = conn.execute(
                "SELECT id, applied_count, success_count, failure_count, "
                "       unused_count, created_at "
                "FROM nl_feedback_corrections "
                f"WHERE id IN ({placeholders}) "
                "AND is_active = 1 AND conflict_flagged = 0",
                list(bucket.keys()),
            ).fetchall()
            pre = {r["id"]: r for r in rows}

            # N2 step 1: flag the harmful set FIRST (guard reads pre-increment
            # counts). Only ids still active/unflagged are eligible. The
            # no-commit core commits with this transaction; the increments below
            # target hints by explicit id, so a hint flagged here still has its
            # failure_count incremented (the record must degrade — N2).
            actually_flagged: list[int] = []
            harmful = {
                i: reasons.get(i, "usage-attribution: removed and judged harmful")
                for i in failure_ids if i in pre
            }
            if harmful:
                # Case-B harmful-removed flag — audited as a Trigger-1 flag (the
                # merged attribution path extends Trigger 1). The returned set is
                # what was enforced (strong-history-protected hints excluded).
                actually_flagged = self._flag_hints_no_commit(harmful, "trigger_1", now)

            # N2 step 2: apply increments by explicit id, then evaluate the
            # disable/retire rules on POST-increment counts (S1-R3).
            for hint_id, row in pre.items():
                kind = bucket[hint_id]
                applied = (row["applied_count"] or 0) + 1
                success = row["success_count"] or 0
                failure = row["failure_count"] or 0
                unused = row["unused_count"] or 0
                if kind == "used":
                    success += 1
                elif kind == "failure":
                    failure += 1
                else:
                    unused += 1

                conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET applied_count = ?, success_count = ?, "
                    "    failure_count = ?, unused_count = ? "
                    "WHERE id = ?",
                    (applied, success, failure, unused, hint_id),
                )
                # T3: record WHICH source produced this outcome, in the SAME
                # transaction as the counters — evidence must be exactly as
                # durable as what it justifies. ON CONFLICT DO NOTHING: the same
                # query crediting the same hint into the same bucket again is
                # one source, not two, while the event counters above keep
                # counting events (F6). Written BEFORE the disable/retire rules
                # so they read POST-insert source counts, matching the
                # POST-increment event counts they already read; the shield ran
                # before this loop and so reads PRE-insert counts (R5).
                conn.execute(
                    "INSERT INTO hint_evidence "
                    "(hint_id, source_kind, source_key, source_hash, bucket, "
                    " created_at) "
                    "VALUES (?, 'query', ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (hint_id, source_key, source_hash, kind, now),
                )
                self._maybe_auto_disable_or_retire(
                    hint_id, row, applied, success, failure, unused, now,
                )

            conn.commit()
            # F2e: stamp the N3 trace as a SEPARATE commit AFTER the counter
            # transaction above. Fully isolated (FR4 guard #3) — a trace-UPDATE
            # failure can never roll back the now-durable counters, nor change
            # the return value. Uses the full bucket (incl. ids excluded from
            # `pre` since flagged/disabled) so the trace reflects the LLM verdict.
            self._update_trace_attribution(workflow_id, bucket, reasons)
            logger.info(
                "[LEARNING:NL] apply_hint_attribution %s: used=%d failure=%d "
                "unused=%d (touched %d active hint(s))",
                workflow_id, len(used_ids), len(failure_ids),
                len(unused_ids), len(pre),
            )
            return actually_flagged
        except Exception as e:
            try:
                conn.rollback()
            except Exception as rb_err:
                logger.warning(
                    "[LEARNING:NL] apply_hint_attribution rollback failed: %s",
                    rb_err,
                )
            logger.warning(
                "[LEARNING:NL] apply_hint_attribution failed for %s — all "
                "changes rolled back: %s", workflow_id, e,
            )
            return None

    def _update_trace_attribution(self, workflow_id, bucket, reasons):
        """F2e: stamp attribution_bucket / attribution_reason onto the N3 trace
        rows ((workflow_id, hint_id)) as a SEPARATE commit AFTER the counter
        transaction. Best-effort and fully isolated — if it fails, the already-
        committed counters are unaffected (FR4 guard #3) and the caller's return
        value is preserved. The internal 'failure' bucket maps to the trace's
        'harmful'. Rows for non-injected/ pruned hints simply match nothing.
        """
        if not bucket:
            return
        trace_bucket = {"used": "used", "failure": "harmful", "unused": "unused"}
        conn = self._em._writer_conn
        try:
            for hint_id, kind in bucket.items():
                conn.execute(
                    "UPDATE hint_workflow_trace "
                    "SET attribution_bucket = ?, attribution_reason = ? "
                    "WHERE workflow_id = ? AND hint_id = ?",
                    (trace_bucket.get(kind, kind), reasons.get(hint_id),
                     workflow_id, hint_id),
                )
            conn.commit()
        except Exception as e:
            logger.warning(
                "[LEARNING:NL] trace attribution UPDATE failed (non-blocking, "
                "workflow_id=%s): %s", workflow_id, e,
            )
            try:
                conn.rollback()
            except Exception:
                pass

    def _maybe_auto_disable_or_retire(
        self, hint_id, before_row, applied, success, failure, unused, now_iso,
    ) -> None:
        """Evaluate the two AUTOMATIC disable/retire rules on POST-increment
        counts (the high-harm-ratio rule is inform-only this phase — FR2). Both
        require success == 0, so a hint that has ever genuinely helped is never
        auto-killed here.
        """
        used = success + failure
        # never-succeeded (FR2 automatic), on the used-outcome basis.
        if failure > 0 and success == 0 and used >= AUTO_DISABLE_MIN_APPLICATIONS:
            self._auto_disable_hint(
                hint_id, before_row, applied, success, failure, unused,
                "never_succeeded", now_iso,
            )
            return
        # over-surfaced dead weight (G1): retire only past the age floor.
        if (
            unused >= UNUSED_RETIRE_THRESHOLD
            and success == 0
            and self._hint_age_days(before_row["created_at"], now_iso)
            >= UNUSED_RETIRE_MIN_AGE_DAYS
        ):
            self._auto_disable_hint(
                hint_id, before_row, applied, success, failure, unused,
                AUTO_DISABLE_REASON_NEVER_USED, now_iso,
            )

    def _auto_disable_hint(
        self, hint_id, before_row, applied, success, failure, unused,
        reason, now_iso,
    ) -> None:
        """Deactivate a hint and write a best-effort auto_disable audit row (its
        own try/except — an audit failure must not roll back the disable).
        """
        self._em._writer_conn.execute(
            "UPDATE nl_feedback_corrections "
            "SET is_active = 0, disabled_at = ? WHERE id = ?",
            (now_iso, hint_id),
        )
        try:
            self._em._writer_conn.execute(
                "INSERT INTO hint_audit "
                "(hint_id, action, actor, reason, before_value, after_value, created_at) "
                "VALUES (?, 'auto_disable', 'system', ?, ?, ?, ?)",
                (
                    hint_id, reason,
                    json.dumps({
                        "applied_count": before_row["applied_count"],
                        "success_count": before_row["success_count"],
                        "failure_count": before_row["failure_count"],
                        "unused_count": before_row["unused_count"],
                    }),
                    json.dumps({
                        "is_active": 0, "applied_count": applied,
                        "success_count": success, "failure_count": failure,
                        "unused_count": unused,
                    }),
                    now_iso,
                ),
            )
        except Exception as audit_err:
            logger.warning(
                "[LEARNING:NL] Auto-disable audit write failed (non-blocking): %s",
                audit_err,
            )
        logger.info(
            "[LEARNING:NL] Auto-disabled hint id=%d reason=%s "
            "(success=%d, failure=%d, unused=%d)",
            hint_id, reason, success, failure, unused,
        )

    @staticmethod
    def _hint_age_days(created_at: Optional[str], now_iso: str) -> float:
        """Age of a hint in days from created_at to now (both ISO strings).
        Returns 0.0 if created_at is missing or unparseable — conservative, a
        hint whose age can't be established is NOT retired.
        """
        if not created_at:
            return 0.0
        try:
            created = datetime.fromisoformat(created_at)
            now = datetime.fromisoformat(now_iso)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            return (now - created).total_seconds() / 86400.0
        except Exception:
            return 0.0

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
