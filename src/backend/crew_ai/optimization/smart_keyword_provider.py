"""
Smart Keyword Provider with Hybrid Architecture

This module orchestrates the 4-tier keyword retrieval system:
0. Surgical Learning Hints (from learning engines — NEW in DAY_06)
1. Core Rules (always included, ~300 tokens)
2. Predicted Keywords (from pattern learning) OR Zero-Context + Tool
3. Full Context Fallback (if both fail)

Returns AgentContextResult (NamedTuple) containing both the context string
and hint metadata for downstream metrics tracking.
"""

import logging
import random
from typing import Optional, List, Dict, NamedTuple
from .pattern_learning import QueryPatternMatcher
from .keyword_vector_store import KeywordVectorStore
from .keyword_search_tool import KeywordSearchTool
from .context_pruner import ContextPruner
from .learning_config import LEARNING_CONFIG

logger = logging.getLogger(__name__)


# Flat per-hint character cap applied in _format_hints. 650 covers the longest
# possible NL feedback hint — the ~112-char "USER FEEDBACK" header
# (NLFeedbackEngine._format_feedback_hint) plus a full MAX_FEEDBACK_TEXT_CHARS
# (500) body = 612 — with headroom, so injected NL text is shown in full and
# matches the stored feedback the usage-attribution LLM later judges. A guard
# test (test_smart_keyword_provider.py) locks this header/cap relationship.
_HINT_CHAR_CAP = 650


# ---------------------------------------------------------------------------
# Result Type
# ---------------------------------------------------------------------------

class AgentContextResult(NamedTuple):
    """Return type for get_agent_context().

    Behaves as a tuple for backward compatibility while providing
    named access to hint metadata for metrics tracking.

    Fields:
        context: The assembled context string for the agent.
        hints_count: Number of learning hints injected (capped by budget).
        hints_available: Total hint candidates found before budget cap.
        hint_sources: Engine names that contributed hints,
                      e.g. ["structural", "anti_pattern"].
        hint_text: Raw formatted hint block for task-level injection.
        nl_injected_ids: IDs of NL feedback hints that survived the budget
                         cut and were injected into this agent's prompt.
        selection_trace: opt-in N3 trace {hint_id: {scope, source, priority,
                         similarity_score, available, injected, drop_reason}}
                         for this agent's NL candidates, or None when capture
                         is off. Default None — NOT {} — to avoid a shared
                         mutable default across NamedTuple instances.
    """
    context: str
    hints_count: int = 0
    hints_available: int = 0
    hint_sources: tuple = ()
    hint_text: str = ""
    nl_injected_ids: tuple = ()
    selection_trace: dict | None = None


def reconcile_selection_traces(traces, injected_ids):
    """Fold per-agent NL selection traces (F2b) into ONE record keyed by
    hint_id for the N3 trace (F2c). Injected-wins reconciliation:

      - a hint is injected (drop_reason=None) IFF it is in injected_ids — the
        union nl_injected_ids that apply_hint_attribution credits — so the
        trace can never contradict the counters;
      - otherwise the highest-precedence drop reason across agents is kept
        (holdout > similarity_below/no_anchor > dedup > cap);
      - similarity_score is the first non-null (identical across agents);
      - scope/source/priority come from any agent (identical, cached fragment).

    Reads injected_ids; never mutates it (FR4 guard #2). Observability only and
    fully defensive — returns None when no trace was captured or on any error,
    so it can never affect the union or the pipeline.
    """
    present = [t for t in traces if t]
    if not present:
        return None
    try:
        precedence = {"holdout": 0, "similarity_below": 1, "no_anchor": 1,
                      "dedup": 2, "cap": 3}
        injected = set(injected_ids)
        all_ids = set()
        for t in present:
            all_ids.update(t.keys())
        out = {}
        for hid in all_ids:
            entries = [t[hid] for t in present if hid in t]
            base = entries[0]
            sim = next((e.get("similarity_score") for e in entries
                        if e.get("similarity_score") is not None), None)
            if hid in injected:
                inj, reason = 1, None
            else:
                reasons = [e.get("drop_reason") for e in entries
                           if e.get("drop_reason")]
                reason = (min(reasons, key=lambda r: precedence.get(r, 99))
                          if reasons else None)
                inj = 0
            out[hid] = {
                "scope": base.get("scope"),
                "source": base.get("source"),
                "priority": base.get("priority"),
                "similarity_score": sim,
                "available": 1 if any(e.get("available") for e in entries) else 0,
                "injected": inj,
                "drop_reason": reason,
            }
        return out
    except Exception as e:
        logger.warning(
            "[LEARNING] selection_trace reconciliation failed (non-blocking): %s",
            e,
        )
        return None


class SmartKeywordProvider:
    """
    Intelligent keyword provider with hybrid approach:
    - Tier 0: Surgical Learning Hints (from past executions)
    - Tier 1: Core Rules (always included)
    - Tier 2: Predicted Keywords OR Zero-Context + Tool
    - Tier 3: Full Context Fallback
    """

    # R7 random-holdout: fraction of workflows on which otherwise-available
    # hints are suppressed, so an unbiased control group (Cat C) accrues for
    # the honest-lift measurement. Rolled once per provider (per workflow).
    _HOLDOUT_RATE = 0.05

    def __init__(self,
                 library_context,
                 pattern_matcher: QueryPatternMatcher,
                 vector_store: KeywordVectorStore,
                 context_pruner: Optional['ContextPruner'] = None,
                 pruning_enabled: bool = False,
                 pruning_threshold: float = 0.8,
                 metrics: Optional[object] = None,
                 execution_memory=None,
                 nl_engine=None,
                 org_id: str | None = None):
        """
        Initialize with library context and optimization components.

        Args:
            library_context: LibraryContext instance (e.g., BrowserLibraryContext)
            pattern_matcher: QueryPatternMatcher for pattern learning
            vector_store: KeywordVectorStore for semantic search
            context_pruner: Optional ContextPruner for smart keyword filtering
            pruning_enabled: Whether to enable context pruning
            pruning_threshold: Confidence threshold for category classification (0.0-1.0)
            metrics: Optional WorkflowMetrics instance for tracking
            execution_memory: Optional ExecutionMemory instance for learning engines.
                              When None, learning hints are skipped (graceful degradation).
            nl_engine: Optional shared NLFeedbackEngine from FeedbackLoop. When provided,
                       stats counters are shared with the FeedbackLoop singleton so
                       /api/learning-stats reflects all activity.
        """
        self.library_context = library_context
        self.pattern_matcher = pattern_matcher
        self.vector_store = vector_store
        self.context_pruner = context_pruner
        self.pruning_enabled = pruning_enabled and context_pruner is not None
        self.pruning_threshold = pruning_threshold
        self.metrics = metrics
        self._em = execution_memory
        self._nl_engine_shared = nl_engine  # FeedbackLoop's instance, may be None
        self._org_id = org_id  # Caller's org — scopes NL + anti-pattern + keyword-pattern reads

        # R7 random-holdout: roll the coin once per workflow. When it comes up
        # AND hints are available, _get_learning_hints suppresses them and
        # sets was_holdout — the run becomes a Cat C control sample.
        self._holdout_decision = random.random() < self._HOLDOUT_RATE
        self.was_holdout = False

        # Per-workflow cache of the Bank 1 (NL feedback) retrieval result,
        # keyed (user_query, url). get_agent_context runs 2x per workflow
        # (planner/assembler) with identical query+url, and Bank 1
        # retrieval is role-independent — so the ChromaDB similarity filter
        # need run only once. A new provider is built per workflow, so the
        # cache lifetime is one workflow: no staleness risk.
        self._nl_hints_cache: dict = {}

        # Lazy-loaded learning engine references
        self._structural_engine = None
        self._keyword_engine = None
        self._anti_pattern_engine = None
        self._nl_feedback_engine = None
        self._intent_extractor = None

        logger.info(f"SmartKeywordProvider initialized for {library_context.library_name}")
        if self.pruning_enabled:
            logger.info(f"Context pruning enabled with threshold {pruning_threshold}")
        if self._em is not None:
            logger.info("[LEARNING] Learning hint injection enabled (execution_memory provided)")

    # ------------------------------------------------------------------
    # Tier 0: Learning Hints (NEW — DAY_06)
    # ------------------------------------------------------------------

    def _get_learning_hints(self, agent_role: str, user_query: str,
                            url: str = None) -> dict:
        """
        Tier 0: Collect and format hints from all learning engines.

        Returns dict with:
            text: Formatted hint string or None (most common case)
            count: Number of hints injected
            sources: Engine names that contributed hints

        Complexity-adaptive budget from LEARNING_CONFIG:
        - Simple (1-3 steps):  max 5 hints
        - Medium (4-5 steps):  max 8 hints
        - Complex (6+ steps):  max 10 hints
        Each selected hint is shown in full up to a flat per-hint character cap
        (_HINT_CHAR_CAP), so injected NL text matches the stored feedback the
        usage-attribution LLM later judges.
        """
        if self._em is None:
            return {"text": None, "count": 0, "available": 0, "sources": []}

        # N3 selection-trace capture (F2b) is opt-in via HINT_TRACE_ENABLED
        # (OPTIMIZATION_ENABLED is already True on this path). When off, no sink
        # is threaded into the NL engine → zero capture cost.
        from src.backend.core.config import settings
        trace_on = settings.HINT_TRACE_ENABLED

        # Determine complexity tier
        tier = self._determine_complexity_tier(user_query)
        max_hints = tier["max_hints"]

        # Collect candidates from all engines with source tracking
        candidates = []
        sources = []
        safe_url = url or ""

        # Structural hints (planner + assembler)
        try:
            structural_hints = self._get_structural_engine().get_hints(
                user_query, safe_url, agent_role
            )
            if structural_hints:
                for hint in structural_hints:
                    candidates.append({"text": hint, "priority": "high"})
                if "structural" not in sources:
                    sources.append("structural")
        except Exception as e:
            logger.warning(f"[LEARNING] Structural engine hint retrieval failed: {e}")

        # Anti-pattern warnings (planner + assembler)
        try:
            anti_pattern_hints = self._get_anti_pattern_engine().get_hints(
                user_query, safe_url, agent_role, org_id=self._org_id
            )
            if anti_pattern_hints:
                for hint in anti_pattern_hints:
                    candidates.append({"text": hint, "priority": "medium"})
                if "anti_pattern" not in sources:
                    sources.append("anti_pattern")
        except Exception as e:
            logger.warning(f"[LEARNING] Anti-pattern engine hint retrieval failed: {e}")

        # Keyword corrections (assembler only)
        try:
            keyword_hints = self._get_keyword_engine().get_hints(
                user_query, safe_url, agent_role
            )
            if keyword_hints:
                for hint in keyword_hints:
                    candidates.append({"text": hint, "priority": "medium"})
                if "keyword_correction" not in sources:
                    sources.append("keyword_correction")
        except Exception as e:
            logger.warning(f"[LEARNING] Keyword engine hint retrieval failed: {e}")

        # NL user feedback corrections (all roles). Bank 1 retrieval is
        # role-independent, so its ChromaDB-backed result is cached per
        # (user_query, url) and reused across the 2 per-workflow calls.
        # nl_trace is the cached STAGE 2-4 fragment (agent-independent); the
        # per-agent stage-5 overlay runs on a copy at return time. Defined
        # before the try so it is always bound for the returns below.
        nl_trace = None
        try:
            cache_key = (user_query, safe_url)
            if cache_key in self._nl_hints_cache:
                nl_texts, nl_ids, nl_trace = self._nl_hints_cache[cache_key]
            else:
                nl_trace = {} if trace_on else None
                nl_texts, nl_ids = self._get_nl_feedback_engine().get_hints_with_ids(
                    user_query, safe_url, agent_role, selection_trace=nl_trace,
                    org_id=self._org_id,
                )
                self._nl_hints_cache[cache_key] = (nl_texts, nl_ids, nl_trace)
            if nl_texts:
                for text, nid in zip(nl_texts, nl_ids):
                    candidates.append({"text": text, "priority": "high", "src": "nl", "id": nid})
                if "nl_feedback" not in sources:
                    sources.append("nl_feedback")
        except Exception as e:
            logger.warning(f"[LEARNING] NL feedback engine hint retrieval failed: {e}")
            nl_trace = None

        if not candidates:
            # NL may still have recorded similarity/dedup drops in nl_trace
            # (no survivors reached the pool) — surface them (no available=1
            # entries → the overlay is a no-op copy).
            return {"text": None, "count": 0, "available": 0, "sources": [],
                    "nl_injected_ids": (),
                    "selection_trace": self._overlay_selection_trace(
                        nl_trace, set(), holdout=False)}

        # Apply hard cap before formatting so count and formatted output agree
        hard_cap = LEARNING_CONFIG.get("HARD_CAP_HINTS", 10)
        effective_max = min(max_hints, hard_cap)

        available = len(candidates)

        # R7 random-holdout: on a coin-flip workflow, suppress hints that
        # would otherwise be injected so an unbiased control group accrues.
        # The returned `available` still reports the real pre-suppression
        # count — that is what distinguishes Cat C ("hints existed,
        # suppressed") from Cat A ("no hints existed") in the lift report.
        if self._holdout_decision and available > 0:
            self.was_holdout = True
            logger.info(
                "[LEARNING] R7 holdout — suppressing %d available hint(s) "
                "for %s agent", available, agent_role,
            )
            return {
                "text": None,
                "count": 0,
                "available": available,
                "sources": [],
                "nl_injected_ids": (),
                "selection_trace": self._overlay_selection_trace(
                    nl_trace, set(), holdout=True),
            }

        formatted, selected = self._format_hints(candidates, effective_max)
        count = min(available, effective_max)
        nl_injected_ids = tuple(c["id"] for c in selected if c.get("src") == "nl")

        return {
            "text": formatted,
            "count": count,
            "available": available,
            "sources": sources,
            "nl_injected_ids": nl_injected_ids,
            "selection_trace": self._overlay_selection_trace(
                nl_trace, set(nl_injected_ids), holdout=False),
        }

    @staticmethod
    def _overlay_selection_trace(nl_trace, injected_ids, holdout):
        """Stage-5 overlay (F2b): resolve each available NL trace entry to its
        final per-agent fate on a COPY — the cached stage 2-4 fragment is
        shared across agents and must never be mutated. Returns None on any
        failure so capture never blocks hint selection (FR4).

        - holdout → every available hint suppressed (drop_reason='holdout').
        - else injected (id in injected_ids → drop_reason=None) or pushed out
          by the per-agent budget cap (drop_reason='cap').
        Entries already dropped at stages 2-4 are copied through unchanged.
        """
        if nl_trace is None:
            return None
        try:
            overlaid = {}
            for rid, entry in nl_trace.items():
                e = dict(entry)                      # copy — never mutate cache
                if e.get("available"):
                    if holdout:
                        e["injected"] = 0
                        e["drop_reason"] = "holdout"
                    elif rid in injected_ids:
                        e["injected"] = 1
                        e["drop_reason"] = None
                    else:
                        e["injected"] = 0
                        e["drop_reason"] = "cap"
                overlaid[rid] = e
            return overlaid
        except Exception as e:
            logger.warning(
                "[LEARNING] selection_trace overlay failed (non-blocking): %s", e
            )
            return None

    def _determine_complexity_tier(self, user_query: str) -> dict:
        """
        Determine query complexity based on estimated step count.

        Reads tier definitions from LEARNING_CONFIG["COMPLEXITY_TIERS"].
        Heuristic: counts action verbs suggesting multiple test steps.
        """
        tiers = LEARNING_CONFIG["COMPLEXITY_TIERS"]

        query_lower = user_query.lower()
        action_words = [
            "click", "fill", "type", "select", "verify", "check",
            "navigate", "open", "close", "scroll", "hover",
            "submit", "enter", "filter", "sort", "extract",
        ]
        step_estimate = sum(1 for word in action_words if word in query_lower)

        if step_estimate <= tiers["simple"]["max_steps"]:
            return tiers["simple"]
        elif step_estimate <= tiers["medium"]["max_steps"]:
            return tiers["medium"]
        return tiers["complex"]

    def _format_hints(
        self, candidates: List[dict], max_hints: int,
    ) -> tuple[Optional[str], List[dict]]:
        """Format the selected hints into the injectable block.

        Sorts by priority (high → medium → low), takes top N, and shows each
        hint in full up to a flat per-hint character cap (_HINT_CHAR_CAP).
        Earlier this truncated each hint to a per-tier token budget, which cut
        long NL feedback below the text the usage-attribution LLM later judges;
        the flat cap (≥ the longest possible NL hint) removes that mismatch.

        Returns (formatted_str, selected_candidates). selected_candidates
        preserves src/id fields added by _get_learning_hints so the caller
        can identify which NL hint IDs survived the budget cut.
        """
        # Sort: high priority first
        priority_order = {"high": 0, "medium": 1, "low": 2}
        candidates.sort(key=lambda x: priority_order.get(x["priority"], 1))

        # Take top N (hard cap already applied by caller)
        selected = candidates[:max_hints]

        # Show each hint in full up to a flat per-hint character cap.
        formatted = []
        for hint in selected:
            text = hint["text"]
            if len(text) > _HINT_CHAR_CAP:
                text = text[:_HINT_CHAR_CAP - 3] + "..."
            formatted.append(text)

        if not formatted:
            return None, []

        header = "═══ LEARNING HINTS (from past executions) ═══"
        return f"{header}\n" + "\n".join(formatted) + "\n" + "═" * 48, selected

    # ------------------------------------------------------------------
    # Lazy-loaded engine accessors
    # ------------------------------------------------------------------

    def _get_structural_engine(self):
        """Lazy-load StructuralRuleEngine with shared execution_memory."""
        if self._structural_engine is None:
            from .structural_rule_engine import StructuralRuleEngine, IntentExtractor
            if self._intent_extractor is None:
                self._intent_extractor = IntentExtractor(self._em)
            self._structural_engine = StructuralRuleEngine(
                self._em, self._intent_extractor
            )
            logger.debug("[LEARNING] StructuralRuleEngine lazy-loaded")
        return self._structural_engine

    def _get_keyword_engine(self):
        """Lazy-load KeywordCorrectionEngine with shared execution_memory."""
        if self._keyword_engine is None:
            from .keyword_correction_engine import KeywordCorrectionEngine
            self._keyword_engine = KeywordCorrectionEngine(self._em)
            logger.debug("[LEARNING] KeywordCorrectionEngine lazy-loaded")
        return self._keyword_engine

    def _get_anti_pattern_engine(self):
        """Lazy-load AntiPatternEngine with shared execution_memory."""
        if self._anti_pattern_engine is None:
            from .anti_pattern_engine import AntiPatternEngine
            self._anti_pattern_engine = AntiPatternEngine(self._em)
            logger.debug("[LEARNING] AntiPatternEngine lazy-loaded")
        return self._anti_pattern_engine

    def _get_nl_feedback_engine(self):
        """Return NLFeedbackEngine: shared FeedbackLoop instance if injected, else lazy-load."""
        if self._nl_engine_shared is not None:
            return self._nl_engine_shared
        if self._nl_feedback_engine is None:
            from .nl_feedback_engine import NLFeedbackEngine
            self._nl_feedback_engine = NLFeedbackEngine(self._em)
            logger.debug("[LEARNING] NLFeedbackEngine lazy-loaded")
        return self._nl_feedback_engine

    # ------------------------------------------------------------------
    # Existing Tier Methods (UNCHANGED from before DAY_06)
    # ------------------------------------------------------------------

    def _get_core_rules(self) -> str:
        """
        Get core library rules that are always included.

        Returns:
            Core rules string (~300 tokens)
        """
        return self.library_context.core_rules

    def _format_zero_context_with_tool(self, agent_role: str) -> str:
        """
        Format minimal context with keyword search tool instructions.

        Used when no predictions are available from pattern learning.
        Target: core rules (300) + tool instructions (200) = 500 tokens

        Args:
            agent_role: "planner" or "assembler"

        Returns:
            Formatted context string with core rules + tool usage instructions
        """
        core_rules = self._get_core_rules()

        return f"""
You are an expert Robot Framework developer using {self.library_context.library_name}.

{core_rules}

**KEYWORD SEARCH TOOL AVAILABLE:**

You have access to a keyword_search tool to find relevant keywords on-demand.
When you need a keyword, search for it by describing what you want to do.

**How to use the tool:**
- Need to click? Search: "click button element"
- Need to input text? Search: "type text input field"
- Need to wait? Search: "wait element visible"
- Need to get text? Search: "get text from element"

The tool will return the top 3 matching keywords with documentation and examples.
Use the exact keyword names and syntax from the tool results.

**Examples:**
```
Action: keyword_search
Action Input: "click button"

Result: Click, Click Element, Click Button (with docs and examples)
```

Use this tool whenever you need to find the right keyword for an action.
"""

    def _format_predicted_context(self, predicted_keywords: List[str], agent_role: str, user_query: str = "") -> str:
        """
        Format context with predicted keywords from pattern learning.

        Gets full documentation for predicted keywords from ChromaDB.
        Optionally applies context pruning to filter keywords by category.
        Target: core rules (300) + predicted keywords (500) = 800 tokens

        Args:
            predicted_keywords: List of keyword names predicted by pattern learning
            agent_role: "planner" or "assembler"
            user_query: User's query (used for pruning if enabled)

        Returns:
            Formatted context string with core rules + predicted keyword docs
        """
        # Get core rules
        core_rules = self._get_core_rules()

        # Apply context pruning if enabled
        keywords_to_fetch = predicted_keywords[:5]  # Limit to top 5 for efficiency

        if self.pruning_enabled and user_query:
            try:
                # Derive relevant categories directly from predicted keywords via
                # reverse-lookup against KEYWORD_CATEGORIES. The predicted keywords
                # ARE the result of pattern learning, so their own categories are
                # the correct ones to use for pruning.
                relevant_categories = [
                    cat for cat, cat_kws in self.context_pruner.KEYWORD_CATEGORIES.items()
                    if any(kw in predicted_keywords for kw in cat_kws)
                ]
                if not relevant_categories:
                    relevant_categories = list(self.context_pruner.KEYWORD_CATEGORIES.keys())

                # Create keyword dicts for pruning
                keyword_dicts = [{'name': kw} for kw in keywords_to_fetch]

                # Prune keywords to relevant categories
                pruned_keyword_dicts = self.context_pruner.prune_keywords(
                    keyword_dicts,
                    relevant_categories
                )

                # Extract pruned keyword names
                keywords_to_fetch = [kw['name'] for kw in pruned_keyword_dicts]

                # Log pruning stats
                stats = self.context_pruner.get_pruning_stats(
                    len(keyword_dicts),
                    len(pruned_keyword_dicts)
                )
                logger.info(
                    f"Context pruning: {stats['original_count']} -> {stats['pruned_count']} keywords "
                    f"({stats['reduction_percentage']:.1f}% reduction)"
                )
            except Exception as e:
                logger.warning(f"Context pruning failed: {e}, using all predicted keywords")

        # Get full documentation for keywords from ChromaDB
        logger.info(f"Fetching documentation for {len(keywords_to_fetch)} keywords")
        keyword_docs = []
        for keyword_name in keywords_to_fetch:
            # Search for exact keyword in ChromaDB
            results = self.vector_store.search(
                library_name=self.library_context.library_name,
                query=keyword_name,
                top_k=1
            )

            if results and results[0]['name'] == keyword_name:
                kw = results[0]
                # Format keyword documentation - MINIMAL format to reduce tokens
                # Only include essential info: name and first 2 args
                args_list = kw['args'][:2] if kw['args'] else []
                args_str = ', '.join([str(arg) for arg in args_list])
                if len(kw['args']) > 2:
                    args_str += ', ...'

                # Very short description (50 chars max)
                doc_str = kw['description'][:50] if kw['description'] else ''

                # Compact format: one line per keyword
                keyword_docs.append(f"• {kw['name']}({args_str}): {doc_str}")

        logger.info(f"Formatted {len(keyword_docs)} keyword docs in compact format")

        # Combine core rules + predicted keywords
        predicted_docs = '\n'.join(keyword_docs) if keyword_docs else 'No predicted keywords available'

        return f"""
You are an expert Robot Framework developer using {self.library_context.library_name}.

{core_rules}

**RELEVANT KEYWORDS (from similar queries):**
{predicted_docs}

Use keyword_search tool if you need additional keywords.
"""

    # ------------------------------------------------------------------
    # Main Entry Point
    # ------------------------------------------------------------------

    def get_agent_context(self, user_query: str, agent_role: str,
                          url: str = None) -> AgentContextResult:
        """
        Get optimized context for an agent based on query and role.

        Implements 4-tier retrieval:
        0. Surgical Learning Hints (from past executions — NEW)
        1. Core Rules (always)
        2. Predicted Keywords OR Zero-Context + Tool
        3. Full Context Fallback

        Args:
            user_query: User's natural language query
            agent_role: "planner" or "assembler"
            url: Optional target URL for domain-scoped hints

        Returns:
            AgentContextResult with context string and hint metadata
        """
        context_parts = []
        hints_count = 0
        hints_available = 0
        hint_sources = []
        hint_text = ""
        nl_injected_ids: tuple = ()
        selection_trace = None

        # ═══ Tier 0: Surgical Learning Hints ═══
        # Hints are routed to task descriptions (high salience),
        # NOT agent backstory (low salience). See tasks.py._get_task_hints().
        try:
            hint_result = self._get_learning_hints(agent_role, user_query, url)
            if hint_result:
                # hints_available always reflects the real pre-suppression
                # candidate count — including on an R7 holdout run, where
                # text is None and count is 0. This keeps Cat C ("hints
                # existed, suppressed") distinct from Cat A in the metrics.
                hints_available = hint_result["available"]
                # Captured even on a holdout run (text is None there).
                selection_trace = hint_result.get("selection_trace")
            if hint_result and hint_result["text"]:
                hint_text = hint_result["text"]
                hints_count = hint_result["count"]
                hint_sources = hint_result["sources"]
                nl_injected_ids = hint_result.get("nl_injected_ids", ())
                logger.info(
                    f"[LEARNING] Injected {hints_count}/{hints_available} hints from "
                    f"{hint_sources} for {agent_role} agent"
                )
        except Exception as e:
            logger.warning(
                f"[LEARNING] Hint retrieval failed for {agent_role} "
                f"(non-blocking): {e}"
            )

        # ═══ Existing Tiers (1, 2a, 2b, 3) — UNCHANGED ═══
        core_rules = self._get_core_rules()

        logger.info(f"Building context for {agent_role} agent")
        logger.debug(f"Core rules: {len(core_rules)} chars")

        # Tier 2: Try pattern learning for keyword prediction
        existing_context = None
        try:
            predicted_keywords = self.pattern_matcher.get_relevant_keywords(user_query, org_id=self._org_id)

            if predicted_keywords:
                logger.info(f"Pattern learning predicted {len(predicted_keywords)} keywords")

                # Track pattern learning metrics
                if self.metrics:
                    self.metrics.track_pattern_learning(
                        predicted=True,
                        keyword_count=len(predicted_keywords),
                    )

                try:
                    existing_context = self._format_predicted_context(predicted_keywords, agent_role, user_query)
                except Exception as e:
                    logger.warning(f"Failed to format predicted context: {e}, falling back to zero-context")
            else:
                logger.info("No predictions from pattern learning, using zero-context + tool")

                # Track that no prediction was used
                if self.metrics:
                    self.metrics.track_pattern_learning(
                        predicted=False,
                        keyword_count=0,
                    )
        except Exception as e:
            logger.warning(f"Pattern learning failed: {e}, falling back to zero-context")

            # Track that prediction failed
            if self.metrics:
                self.metrics.track_pattern_learning(
                    predicted=False,
                    keyword_count=0,
                )

        # Tier 2 Fallback: Zero-context + tool instructions
        if existing_context is None:
            try:
                existing_context = self._format_zero_context_with_tool(agent_role)
            except Exception as e:
                logger.error(f"Zero-context formatting failed: {e}, falling back to full context")
                # Tier 3: Full context fallback (baseline behavior)
                logger.warning("Using full context as fallback - optimization failed")
                existing_context = self._get_full_context_fallback(agent_role)

        context_parts.append(existing_context)

        return AgentContextResult(
            context="\n\n".join(context_parts),
            hints_count=hints_count,
            hints_available=hints_available,
            hint_sources=tuple(hint_sources),
            hint_text=hint_text,
            nl_injected_ids=nl_injected_ids,
            selection_trace=selection_trace,
        )

    def _get_full_context_fallback(self, agent_role: str) -> str:
        """
        Get full context as fallback when optimization fails.

        This ensures graceful degradation to baseline behavior.

        Args:
            agent_role: "planner" or "assembler"

        Returns:
            Full context string from library_context
        """
        logger.info(f"Fallback to full context for {agent_role} agent")

        # NOTE: the "identifier" branch was removed in Task 16 — it was
        # pre-existing dead code (zero callers even before the element
        # identifier agent itself was replaced by deterministic Python).
        if agent_role == "planner":
            return self.library_context.planning_context
        elif agent_role == "assembler":
            return self.library_context.code_assembly_context
        else:
            # Default to code assembly context
            logger.warning(f"Unknown agent role '{agent_role}', using code_assembly_context")
            return self.library_context.code_assembly_context

    def get_keyword_search_tool(self) -> KeywordSearchTool:
        """
        Get keyword search tool for agents.

        Returns:
            KeywordSearchTool instance configured for this library
        """
        return KeywordSearchTool(
            library_name=self.library_context.library_name,
            vector_store=self.vector_store,
            metrics=self.metrics
        )

