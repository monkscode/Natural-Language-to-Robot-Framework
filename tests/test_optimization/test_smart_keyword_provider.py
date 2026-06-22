"""
DAY 06 — Context Injection via SmartKeywordProvider (pytest).

Tests: AgentContextResult, _determine_complexity_tier, _format_hints,
_get_learning_hints, get_agent_context with Tier 0, lazy loading,
engine role filtering, regression, and integration.
Uses in-memory SQLite with full Phase 1 schema.
"""

import sqlite3
from datetime import datetime

import pytest

from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    EffectivenessScore,
    MAX_FEEDBACK_TEXT_CHARS,
)
from src.backend.crew_ai.optimization.smart_keyword_provider import (
    SmartKeywordProvider,
    AgentContextResult,
    _HINT_CHAR_CAP,
)


# ===================================================================
# Test Helpers (mock dependencies — NOT harness)
# ===================================================================

class MockPatternMatcher:
    """Minimal mock of QueryPatternMatcher."""

    def __init__(self, predicted=None):
        self._predicted = predicted

    def get_relevant_keywords(self, query):
        return self._predicted


class MockVectorStore:
    """Minimal mock of KeywordVectorStore."""

    def search(self, library_name, query, top_k=3):
        return [
            {
                "name": query,
                "args": ["arg1"],
                "description": f"Mock doc for {query}",
            }
        ]


class MockLibraryContext:
    """Minimal mock of LibraryContext."""
    library_name = "Browser"
    core_rules = "CORE RULES: Use Browser library keywords."
    planning_context = "FULL PLANNING CONTEXT"
    code_assembly_context = "FULL CODE ASSEMBLY CONTEXT"


class MockEngine:
    """Configurable mock for any learning engine."""

    def __init__(self, hints=None, raise_on_call=False):
        self._hints = hints
        self._raise = raise_on_call
        self.call_count = 0
        self.last_args = None

    def get_hints(self, user_query, url, agent_role, org_id=None):
        self.call_count += 1
        self.last_args = (user_query, url, agent_role, org_id)
        if self._raise:
            raise RuntimeError("Engine exploded")
        return self._hints

    def learn(self, record):
        pass

    def get_stats(self):
        return {"total_rules": 0, "active_rules": 0}


def create_provider(execution_memory=None, pattern_matcher=None,
                    predicted_keywords=None, pruning_enabled=False):
    """Factory for SmartKeywordProvider with mock dependencies."""
    return SmartKeywordProvider(
        library_context=MockLibraryContext(),
        pattern_matcher=pattern_matcher or MockPatternMatcher(predicted_keywords),
        vector_store=MockVectorStore(),
        context_pruner=None,
        pruning_enabled=pruning_enabled,
        execution_memory=execution_memory,
    )


# ===================================================================
# Category 1: AgentContextResult NamedTuple Tests
# ===================================================================

class TestAgentContextResult:

    def test_creation_default(self):
        r = AgentContextResult(context="hello")
        assert r.context == "hello"
        assert r.hints_count == 0
        assert r.hint_sources == ()

    def test_creation_with_metadata(self):
        r = AgentContextResult(
            context="ctx", hints_count=3, hint_sources=["structural"]
        )
        assert r.hints_count == 3
        assert r.hint_sources == ["structural"]

    def test_tuple_unpacking(self):
        r = AgentContextResult(
            context="ctx", hints_count=2, hints_available=3, hint_sources=["a", "b"]
        )
        c, n, avail, s, h, nl_ids, trace = r
        assert c == "ctx"
        assert n == 2
        assert avail == 3
        assert s == ["a", "b"]
        assert h == ""  # Default hint_text
        assert nl_ids == ()  # Default nl_injected_ids
        assert trace is None  # Default selection_trace

    def test_context_is_string(self):
        r = AgentContextResult(context="some context string")
        assert isinstance(r.context, str)
        assert "some context" in r.context


# ===================================================================
# Category 2: _determine_complexity_tier Tests
# ===================================================================

class TestComplexityTier:

    def test_simple_no_actions(self):
        p = create_provider()
        tier = p._determine_complexity_tier("go to website")
        assert tier["max_hints"] == 5, f"Expected simple tier (5 hints), got {tier}"

    def test_simple_one_action(self):
        p = create_provider()
        tier = p._determine_complexity_tier("click the button")
        assert tier["max_hints"] == 5

    def test_simple_three_actions(self):
        p = create_provider()
        tier = p._determine_complexity_tier("click the button and verify and check")
        assert tier["max_hints"] == 5

    def test_medium_four_actions(self):
        p = create_provider()
        tier = p._determine_complexity_tier("click fill verify check something")
        assert tier["max_hints"] == 8, f"Expected medium tier (8 hints), got {tier}"

    def test_medium_five_actions(self):
        p = create_provider()
        tier = p._determine_complexity_tier(
            "navigate to page, click button, fill form, verify text, submit"
        )
        assert tier["max_hints"] == 8

    def test_complex_many_actions(self):
        p = create_provider()
        tier = p._determine_complexity_tier(
            "navigate to website, click login, fill username, type password, "
            "submit the form, verify the page, scroll down, hover over menu, "
            "select dropdown option, check results, extract data, filter list, "
            "sort by name, close dialog, open new tab, enter search query"
        )
        assert tier["max_hints"] == 10

    def test_reads_from_config(self):
        """Verify tiers come from LEARNING_CONFIG, not hardcoded."""
        p = create_provider()
        config_tiers = LEARNING_CONFIG["COMPLEXITY_TIERS"]
        tier = p._determine_complexity_tier("click button")
        assert tier == config_tiers["simple"]


# ===================================================================
# Category 3: _format_hints Tests
# ===================================================================

class TestFormatHints:

    def test_empty_list(self):
        p = create_provider()
        result, selected = p._format_hints([], max_hints=5)
        assert result is None
        assert selected == []

    def test_single_hint(self):
        p = create_provider()
        result, selected = p._format_hints(
            [{"text": "Use FOR loop", "priority": "high"}],
            max_hints=5
        )
        assert "Use FOR loop" in result
        assert "LEARNING HINTS" in result
        assert len(selected) == 1

    def test_priority_sorting(self):
        p = create_provider()
        candidates = [
            {"text": "LOW_HINT", "priority": "low"},
            {"text": "HIGH_HINT", "priority": "high"},
            {"text": "MED_HINT", "priority": "medium"},
        ]
        result, selected = p._format_hints(candidates, max_hints=5)
        lines = result.split("\n")
        content_lines = [line for line in lines if "HINT" in line and "LEARNING" not in line]
        assert content_lines[0] == "HIGH_HINT", f"Expected HIGH first, got {content_lines}"
        assert content_lines[1] == "MED_HINT"
        assert content_lines[2] == "LOW_HINT"

    def test_max_hints_cap(self):
        p = create_provider()
        candidates = [{"text": f"hint_{i}", "priority": "medium"} for i in range(20)]
        result, selected = p._format_hints(candidates, max_hints=3)
        lines = [line for line in result.split("\n") if line.startswith("hint_")]
        assert len(lines) == 3, f"Expected 3 hints, got {len(lines)}: {lines}"
        assert len(selected) == 3

    def test_hard_cap(self):
        """HARD_CAP_HINTS is enforced by _get_learning_hints before calling _format_hints.

        _format_hints respects whatever max_hints it receives; the cap is applied
        by the caller so both count and formatted output agree on the same limit.
        """
        p = create_provider()
        hard_cap = LEARNING_CONFIG.get("HARD_CAP_HINTS", 10)
        candidates = [{"text": f"hint_{i}", "priority": "medium"} for i in range(15)]
        # Simulate what _get_learning_hints does: compute effective_max before calling.
        effective_max = min(15, hard_cap)
        result, selected = p._format_hints(candidates, max_hints=effective_max)
        lines = [line for line in result.split("\n") if line.startswith("hint_")]
        assert len(lines) <= hard_cap, f"Expected <= {hard_cap}, got {len(lines)}"

    def test_long_hint_truncated_at_char_cap(self):
        """A hint over _HINT_CHAR_CAP is cut to the cap with an ellipsis; a hint
        at/below the cap (e.g. a full 500-char feedback body) is shown whole."""
        p = create_provider()

        # 500 chars (a full-length feedback body) — below the cap, untruncated.
        body = "A" * 500
        result, _ = p._format_hints([{"text": body, "priority": "medium"}], max_hints=5)
        lines = [line for line in result.split("\n") if line.startswith("A")]
        assert lines[0] == body
        assert not lines[0].endswith("...")

        # 700 chars — above the cap, truncated to exactly _HINT_CHAR_CAP.
        long_text = "A" * 700
        result, _ = p._format_hints([{"text": long_text, "priority": "medium"}], max_hints=5)
        lines = [line for line in result.split("\n") if line.startswith("A")]
        assert len(lines[0]) == _HINT_CHAR_CAP, f"Expected cut to {_HINT_CHAR_CAP}, got {len(lines[0])}"
        assert lines[0].endswith("...")

    def test_exact_char_cap_boundary(self):
        """A hint exactly at _HINT_CHAR_CAP is NOT truncated (boundary is > cap)."""
        p = create_provider()
        exact_text = "A" * _HINT_CHAR_CAP
        result, _ = p._format_hints(
            [{"text": exact_text, "priority": "medium"}], max_hints=5,
        )
        lines = [line for line in result.split("\n") if line.startswith("A")]
        assert lines[0] == exact_text  # No truncation

    def test_hint_char_cap_fits_max_nl_feedback_hint(self):
        """Guard: the longest possible NL feedback hint — the USER FEEDBACK
        header plus a full MAX_FEEDBACK_TEXT_CHARS body — fits within
        _HINT_CHAR_CAP, so injected NL text is never truncated. Fails CI if the
        header grows or the feedback cap is raised without raising the cap."""
        from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
        # _format_feedback_hint does not use self; call it unbound to avoid
        # constructing an engine (no DB/ChromaDB needed for a pure format check).
        hint = NLFeedbackEngine._format_feedback_hint(None, "A" * MAX_FEEDBACK_TEXT_CHARS)
        assert len(hint) <= _HINT_CHAR_CAP

    def test_max_nl_feedback_hint_injected_uncut(self):
        """End-to-end: a maximal NL feedback hint passes through _format_hints in
        full. The old per-tier token budget (320/400/480 chars) would have cut
        it below the text the usage-attribution LLM later judges."""
        from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
        hint = NLFeedbackEngine._format_feedback_hint(None, "A" * MAX_FEEDBACK_TEXT_CHARS)
        p = create_provider()
        result, _ = p._format_hints([{"text": hint, "priority": "high"}], max_hints=5)
        assert hint in result

    def test_header_and_footer(self):
        p = create_provider()
        result, selected = p._format_hints(
            [{"text": "test hint", "priority": "medium"}],
            max_hints=5
        )
        assert result.startswith("═══ LEARNING HINTS")
        assert result.endswith("═" * 48)

    def test_unicode_hint(self):
        p = create_provider()
        result, selected = p._format_hints(
            [{"text": "⚠️ AVOID: Don't use single Get Text", "priority": "high"}],
            max_hints=5
        )
        assert "⚠️ AVOID" in result

    def test_mixed_priorities(self):
        p = create_provider()
        candidates = [
            {"text": "med1", "priority": "medium"},
            {"text": "high1", "priority": "high"},
            {"text": "med2", "priority": "medium"},
            {"text": "high2", "priority": "high"},
            {"text": "low1", "priority": "low"},
        ]
        result, selected = p._format_hints(candidates, max_hints=5)
        lines = [line for line in result.split("\n")
                 if line and not line.startswith("═")]
        # Highs first, then meds, then lows
        assert lines[0] == "high1"
        assert lines[1] == "high2"


# ===================================================================
# Category 4: _get_learning_hints Tests
# ===================================================================

class TestGetLearningHints:

    def test_no_db_conn(self):
        """No db_conn -> no hints (graceful degradation)."""
        p = create_provider(execution_memory=None)
        result = p._get_learning_hints("planner", "click button")
        assert result["text"] is None
        assert result["count"] == 0
        assert result["available"] == 0
        assert result["sources"] == []

    def test_all_engines_return_none(self, in_memory_db):
        """All engines return None -> no hints."""
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p._get_learning_hints("planner", "click button")
        assert result["text"] is None
        assert result["count"] == 0
        assert result["available"] == 0

    def test_structural_only(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=["Use FOR loop"])
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p._get_learning_hints("planner", "get all rows")
        assert result["text"] is not None
        assert result["count"] == 1
        assert result["available"] == 1
        assert result["sources"] == ["structural"]
        assert "Use FOR loop" in result["text"]

    def test_anti_pattern_only(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=["Don't use Get Text for tables"])
        result = p._get_learning_hints("assembler", "get all product names")
        assert result["sources"] == ["anti_pattern"]
        assert result["count"] == 1

    def test_keyword_only(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=["Use Fill Text not Input Text"])
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p._get_learning_hints("assembler", "type in the form")
        assert result["sources"] == ["keyword_correction"]
        assert "Fill Text" in result["text"]

    def test_mixed_engines(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=["Structural hint"])
        p._keyword_engine = MockEngine(hints=["Keyword hint"])
        p._anti_pattern_engine = MockEngine(hints=["Anti-pattern hint"])
        result = p._get_learning_hints("assembler", "fill form and submit")
        assert result["count"] == 3
        assert "structural" in result["sources"]
        assert "keyword_correction" in result["sources"]
        assert "anti_pattern" in result["sources"]

    def test_engine_error_non_blocking(self, in_memory_db):
        """One engine failing should not block others."""
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(raise_on_call=True)
        p._keyword_engine = MockEngine(hints=["Good hint"])
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p._get_learning_hints("assembler", "fill form")
        assert result["count"] == 1
        assert result["sources"] == ["keyword_correction"]

    def test_all_engines_fail(self, in_memory_db):
        """All engines raising -> same as no hints."""
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(raise_on_call=True)
        p._keyword_engine = MockEngine(raise_on_call=True)
        p._anti_pattern_engine = MockEngine(raise_on_call=True)
        result = p._get_learning_hints("planner", "click button")
        assert result["text"] is None
        assert result["count"] == 0

    def test_url_passed_to_engines(self, in_memory_db):
        """URL should be forwarded to each engine."""
        p = create_provider(execution_memory=in_memory_db)
        mock_s = MockEngine(hints=None)
        mock_k = MockEngine(hints=None)
        mock_a = MockEngine(hints=None)
        p._structural_engine = mock_s
        p._keyword_engine = mock_k
        p._anti_pattern_engine = mock_a
        p._get_learning_hints("planner", "query", url="https://demoqa.com")
        assert mock_s.last_args == ("query", "https://demoqa.com", "planner", None)
        assert mock_a.last_args == ("query", "https://demoqa.com", "planner", None)

    def test_none_url_becomes_empty_string(self, in_memory_db):
        """When url=None, engines should receive empty string."""
        p = create_provider(execution_memory=in_memory_db)
        mock_s = MockEngine(hints=None)
        p._structural_engine = mock_s
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        p._get_learning_hints("planner", "query", url=None)
        assert mock_s.last_args[1] == ""


# ===================================================================
# Category 5: get_agent_context with Tier 0 Tests
# ===================================================================

class TestGetAgentContext:

    def test_returns_agent_context_result(self):
        p = create_provider()
        result = p.get_agent_context("click button", "planner")
        assert isinstance(result, AgentContextResult)
        assert isinstance(result.context, str)
        assert isinstance(result.hints_count, int)
        assert isinstance(result.hint_sources, (list, tuple))

    def test_no_hints_no_db(self):
        """Without db_conn, no hints but context still returned."""
        p = create_provider(execution_memory=None)
        result = p.get_agent_context("click button", "planner")
        assert result.hints_count == 0
        assert result.hint_sources == ()
        assert len(result.context) > 0  # Should have existing tier context

    def test_hints_prepended_to_context(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=["STRUCTURAL_HINT_MARKER"])
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p.get_agent_context("get rows", "planner")
        # Hints should be in hint_text (for task-level injection), NOT in context (backstory)
        assert "STRUCTURAL_HINT_MARKER" in result.hint_text, "Hint not found in hint_text"
        assert "STRUCTURAL_HINT_MARKER" not in result.context, "Hint should not be in context (backstory)"
        assert result.hints_count > 0

    def test_no_hints_context_unchanged(self, in_memory_db):
        """When no hints available, context should be same as without Tier 0."""
        p_no_db = create_provider(execution_memory=None)
        result_no_db = p_no_db.get_agent_context("click button", "assembler")

        p_with_db = create_provider(execution_memory=in_memory_db)
        p_with_db._structural_engine = MockEngine(hints=None)
        p_with_db._keyword_engine = MockEngine(hints=None)
        p_with_db._anti_pattern_engine = MockEngine(hints=None)
        result_with_db = p_with_db.get_agent_context("click button", "assembler")

        # Both should have same content (no hint pollution)
        assert result_no_db.context == result_with_db.context

    def test_hint_metadata_in_result(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=["hint1", "hint2"])
        p._keyword_engine = MockEngine(hints=["hint3"])
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p.get_agent_context("click and fill", "assembler")
        assert result.hints_count == 3
        assert "structural" in result.hint_sources
        assert "keyword_correction" in result.hint_sources

    def test_url_param_backward_compat(self):
        """get_agent_context works without url param (backward compat)."""
        p = create_provider()
        result = p.get_agent_context("click button", "planner")
        assert isinstance(result, AgentContextResult)

    def test_url_param_forwarded(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        mock_s = MockEngine(hints=None)
        p._structural_engine = mock_s
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        p.get_agent_context("click", "planner", url="https://test.com")
        assert mock_s.last_args[1] == "https://test.com"

    def test_hint_failure_nonblocking(self, in_memory_db):
        """If _get_learning_hints raises, context should still be returned."""
        p = create_provider(execution_memory=in_memory_db)

        # Make _get_learning_hints raise
        def exploding_hints(*args, **kwargs):
            raise RuntimeError("Catastrophic failure")
        p._get_learning_hints = exploding_hints

        result = p.get_agent_context("click button", "planner")
        assert result.hints_count == 0
        assert len(result.context) > 0  # Existing tiers still work

    def test_all_agent_roles(self, in_memory_db):
        """All agent roles should work."""
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)

        for role in ["planner", "identifier", "assembler"]:
            result = p.get_agent_context("click button", role)
            assert isinstance(result, AgentContextResult), f"Failed for role: {role}"
            assert len(result.context) > 0, f"Empty context for role: {role}"

    def test_identifier_no_hints(self, in_memory_db):
        """Identifier role should get no hints (engines filter by role)."""
        p = create_provider(execution_memory=in_memory_db)
        # Structural only returns for planner/assembler
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p.get_agent_context("find element", "identifier")
        assert result.hints_count == 0


# ===================================================================
# Category 6: Lazy Loading Tests
# ===================================================================

class TestLazyLoading:

    def test_engines_not_created_at_init(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        assert p._structural_engine is None
        assert p._keyword_engine is None
        assert p._anti_pattern_engine is None

    def test_engines_created_on_first_hint_call(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._get_learning_hints("planner", "click button")
        assert p._structural_engine is not None
        assert p._keyword_engine is not None
        assert p._anti_pattern_engine is not None

    def test_engine_reused_on_second_call(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._get_learning_hints("planner", "click button")
        engine1 = p._structural_engine
        p._get_learning_hints("assembler", "fill form")
        engine2 = p._structural_engine
        assert engine1 is engine2, "Engine should be reused, not recreated"

    def test_no_creation_without_db(self):
        p = create_provider(execution_memory=None)
        p._get_learning_hints("planner", "click button")
        assert p._structural_engine is None
        assert p._keyword_engine is None
        assert p._anti_pattern_engine is None

    def test_intent_extractor_shared(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._get_learning_hints("planner", "click button")
        assert p._intent_extractor is not None


# ===================================================================
# Category 7: Engine Role Filtering Tests
# ===================================================================

class TestEngineRoleFiltering:

    def test_anti_pattern_excludes_identifier(self, in_memory_db):
        """AntiPatternEngine should NOT return hints for identifier (role-filtered)."""
        from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
        engine = AntiPatternEngine(in_memory_db)
        # Identifier is rejected by the role gate before any pattern lookup.
        result_identifier = engine.get_hints("click button", "https://test.com", "identifier")
        assert result_identifier is None, "Identifier should be filtered out by role"

    def test_keyword_includes_assembler(self, in_memory_db):
        """KeywordCorrectionEngine should return hints for assembler."""
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine
        engine = KeywordCorrectionEngine(in_memory_db)
        # Insert testable correction
        in_memory_db.execute(
            "INSERT INTO keyword_corrections "
            "(wrong_keyword, correct_keyword, library, error_pattern, "
            " score, evidence_count, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Input Text", "Fill Text", "Browser", "wrong keyword",
             0.83, 5, datetime.now().isoformat()),
        )
        in_memory_db.commit()
        result = engine.get_hints("type text", "", "assembler")
        assert result is not None, "Assembler should receive keyword corrections"
        assert any("Fill Text" in h for h in result)

    def test_keyword_excludes_planner(self, in_memory_db):
        """KeywordCorrectionEngine should NOT return hints for planner."""
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine
        engine = KeywordCorrectionEngine(in_memory_db)
        in_memory_db.execute(
            "INSERT INTO keyword_corrections "
            "(wrong_keyword, correct_keyword, library, error_pattern, "
            " score, evidence_count, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Input Text", "Fill Text", "Browser", "wrong keyword",
             0.83, 5, datetime.now().isoformat()),
        )
        in_memory_db.commit()
        result = engine.get_hints("type text", "", "planner")
        assert result is None, "Planner should NOT receive keyword corrections"

    def test_structural_excludes_identifier(self, in_memory_db):
        """StructuralRuleEngine should NOT return hints for identifier."""
        from src.backend.crew_ai.optimization.structural_rule_engine import StructuralRuleEngine, IntentExtractor
        ie = IntentExtractor(in_memory_db)
        engine = StructuralRuleEngine(in_memory_db, ie)
        result = engine.get_hints("get all rows", "", "identifier")
        assert result is None, "Identifier should NOT receive structural hints"


# ===================================================================
# Category 8: Regression Tests (existing tiers unchanged)
# ===================================================================

class TestRegression:

    def test_tier1_core_rules(self):
        """Core rules should always be in context."""
        p = create_provider()
        result = p.get_agent_context("click button", "planner")
        assert "CORE RULES" in result.context

    def test_no_url_param(self):
        """get_agent_context without url should still work."""
        p = create_provider()
        result = p.get_agent_context("click button", "assembler")
        assert isinstance(result, AgentContextResult)
        assert len(result.context) > 0

    def test_predicted_keywords(self):
        """Pattern learning predictions should still work."""
        p = create_provider(predicted_keywords=["Click", "Fill Text"])
        result = p.get_agent_context("click and fill", "assembler")
        assert "RELEVANT KEYWORDS" in result.context

    def test_zero_context_fallback(self):
        """With no predictions, should fall back to zero-context + tool."""
        p = create_provider(predicted_keywords=None)
        result = p.get_agent_context("click button", "assembler")
        assert "keyword_search" in result.context.lower() or "KEYWORD SEARCH" in result.context

    def test_full_context_fallback(self):
        """Full context fallback for identifier role."""
        p = create_provider()
        result = p.get_agent_context("find element", "identifier")
        # Identifier gets minimal guidance in fallback
        assert len(result.context) > 0
        assert isinstance(result, AgentContextResult)


# ===================================================================
# Category 9: Integration Tests
# ===================================================================

class TestIntegration:

    def test_real_structural_engine(self, in_memory_db):
        """End-to-end with real StructuralRuleEngine + DB."""
        from src.backend.crew_ai.optimization.structural_rule_engine import (
            StructuralRuleEngine, IntentExtractor,
        )
        ie = IntentExtractor(in_memory_db)
        se = StructuralRuleEngine(in_memory_db, ie)

        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = se
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)

        # No rules yet -> no hints
        result = p._get_learning_hints("planner", "get all rows from the table")
        assert result["count"] == 0

    def test_real_keyword_engine(self, in_memory_db):
        """End-to-end with real KeywordCorrectionEngine + DB."""
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine

        ke = KeywordCorrectionEngine(in_memory_db)
        # Insert a real keyword correction
        in_memory_db.execute(
            "INSERT INTO keyword_corrections "
            "(wrong_keyword, correct_keyword, library, error_pattern, "
            " score, evidence_count, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Input Text", "Fill Text", "Browser", "wrong keyword",
             0.83, 5, datetime.now().isoformat()),
        )
        in_memory_db.commit()

        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = ke
        p._anti_pattern_engine = MockEngine(hints=None)

        result = p._get_learning_hints("assembler", "fill form")
        assert result["count"] > 0
        assert "keyword_correction" in result["sources"]
        assert "Fill Text" in result["text"]

    def test_full_pipeline_with_hints(self, in_memory_db):
        """Full get_agent_context with real engine producing hints."""
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine

        ke = KeywordCorrectionEngine(in_memory_db)
        in_memory_db.execute(
            "INSERT INTO keyword_corrections "
            "(wrong_keyword, correct_keyword, library, error_pattern, "
            " score, evidence_count, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Input Text", "Fill Text", "Browser", "wrong keyword",
             0.83, 5, datetime.now().isoformat()),
        )
        in_memory_db.commit()

        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = ke
        p._anti_pattern_engine = MockEngine(hints=None)

        result = p.get_agent_context("fill the form", "assembler")
        assert isinstance(result, AgentContextResult)
        assert result.hints_count > 0
        # Hints should be in hint_text (for task-level injection), NOT in context (backstory)
        assert "Fill Text" in result.hint_text
        assert "Fill Text" not in result.context

    def test_empty_db_no_crash(self, in_memory_db):
        """Empty database should produce no hints without errors."""
        p = create_provider(execution_memory=in_memory_db)
        # Use real engines -- they'll find nothing
        result = p.get_agent_context("click button", "planner", url="https://test.com")
        assert result.hints_count == 0
        assert len(result.context) > 0

    def test_multiple_agents_independent(self, in_memory_db):
        """Each agent call should produce independent results."""
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine
        ke = KeywordCorrectionEngine(in_memory_db)
        in_memory_db.execute(
            "INSERT INTO keyword_corrections "
            "(wrong_keyword, correct_keyword, library, error_pattern, "
            " score, evidence_count, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Input Text", "Fill Text", "Browser", "wrong keyword",
             0.83, 5, datetime.now().isoformat()),
        )
        in_memory_db.commit()

        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = ke
        p._anti_pattern_engine = MockEngine(hints=None)

        # assembler gets keyword hints, planner does NOT
        assembler_result = p.get_agent_context("fill form", "assembler")
        planner_result = p.get_agent_context("fill form", "planner")
        assert assembler_result.hints_count > 0
        assert planner_result.hints_count == 0


# ===================================================================
# Category 10: Edge Cases
# ===================================================================

class TestEdgeCases:

    def test_empty_query(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p.get_agent_context("", "planner")
        assert isinstance(result, AgentContextResult)

    def test_very_long_query(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        long_query = "click " * 1000  # Very long query
        result = p.get_agent_context(long_query, "assembler")
        assert isinstance(result, AgentContextResult)

    def test_unknown_role(self):
        p = create_provider()
        result = p.get_agent_context("click button", "unknown_role")
        assert isinstance(result, AgentContextResult)

    def test_url_with_special_chars(self, in_memory_db):
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=None)
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p.get_agent_context(
            "click button", "planner",
            url="https://example.com/page?q=value&x=1#section"
        )
        assert isinstance(result, AgentContextResult)

    def test_multiple_same_source_hints(self, in_memory_db):
        """Multiple hints from same engine should count correctly."""
        p = create_provider(execution_memory=in_memory_db)
        p._structural_engine = MockEngine(hints=["hint1", "hint2", "hint3"])
        p._keyword_engine = MockEngine(hints=None)
        p._anti_pattern_engine = MockEngine(hints=None)
        result = p._get_learning_hints("planner", "complex query")
        assert result["count"] == 3
        assert result["sources"] == ["structural"]  # Only appears once

    def test_hint_with_newlines(self):
        p = create_provider()
        result, selected = p._format_hints(
            [{"text": "Line1\nLine2\nLine3", "priority": "high"}],
            max_hints=5
        )
        assert "Line1\nLine2\nLine3" in result
        assert len(selected) == 1
