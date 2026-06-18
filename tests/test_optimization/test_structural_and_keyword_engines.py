"""
DAY_03 Pytest Tests

Validates all acceptance criteria for the Structural Rule Engine
and Keyword Correction Engine.

Uses in-memory SQLite with schema migrations via conftest fixtures --
does NOT modify real data.

Migrated from: scripts/verify_day03.py
"""

import json

import pytest

from src.backend.crew_ai.optimization.learning_config import (
    LearningEngine,
    EffectivenessScore,
)
from src.backend.crew_ai.optimization.structural_rule_engine import (
    IntentExtractor,
    StructuralRuleEngine,
)
from src.backend.crew_ai.optimization.keyword_correction_engine import (
    KeywordCorrectionEngine,
)


# ---------------------------------------------------------------------------
# Mock ExecutionRecord
# ---------------------------------------------------------------------------


class MockRecord:
    """Minimal mock matching ExecutionRecord fields used by engines."""

    def __init__(self, user_query="", robot_code=None, test_status="passed",
                 failure_category=None, failed_keyword=None,
                 error_message=None):
        self.user_query = user_query
        self.robot_code = robot_code
        self.test_status = test_status
        self.failure_category = failure_category
        self.failed_keyword = failed_keyword
        self.error_message = error_message


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Seed Pattern Matching
# ---------------------------------------------------------------------------


class TestIntentExtractorSeedMatching:
    """Tests for IntentExtractor seed pattern matching."""

    def test_single_intent_match(self, in_memory_db):
        """Verify seed patterns match correctly for iteration intent."""
        extractor = IntentExtractor(in_memory_db)
        intents = extractor.extract_intents("loop through all items in the table")
        assert any(i["intent"] == "iteration" for i in intents), (
            f"Seed: 'loop through all items' -> iteration intent: intents={[i['intent'] for i in intents]}"
        )

    def test_multi_trigger_higher_score(self, in_memory_db):
        """Verify multi-trigger match produces higher score."""
        extractor = IntentExtractor(in_memory_db)
        intents = extractor.extract_intents("for each row iterate through elements")
        iteration = [i for i in intents if i["intent"] == "iteration"]
        assert len(iteration) == 1 and iteration[0]["score"] > 0.4, (
            f"Seed: multi-trigger -> score increases: score={iteration[0]['score'] if iteration else 'N/A'}"
        )

    def test_counting_match(self, in_memory_db):
        """Verify counting intent matches."""
        extractor = IntentExtractor(in_memory_db)
        intents = extractor.extract_intents("count how many items are visible")
        assert any(i["intent"] == "counting" for i in intents), (
            f"Seed: 'count how many' -> counting intent: intents={[i['intent'] for i in intents]}"
        )

    def test_no_match_returns_empty(self, in_memory_db):
        """Verify no trigger match returns empty list."""
        extractor = IntentExtractor(in_memory_db)
        intents = extractor.extract_intents("click the submit button")
        assert len(intents) == 0, (
            "Seed: no trigger match -> empty list"
        )


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Multi-Intent Match
# ---------------------------------------------------------------------------


class TestIntentExtractorMultiMatch:
    """Tests for IntentExtractor multi-intent matching."""

    def test_multi_intent_query(self, in_memory_db):
        """Verify multiple intents can match the same query."""
        extractor = IntentExtractor(in_memory_db)
        intents = extractor.extract_intents("count all items in each row")
        intent_names = [i["intent"] for i in intents]
        assert len(intents) >= 2, (
            f"Seed: multi-intent query matches multiple intents: matched={intent_names}"
        )


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Seed Migration to SQLite
# ---------------------------------------------------------------------------


class TestIntentExtractorSeedMigration:
    """Tests for IntentExtractor seed migration to SQLite."""

    def test_intent_patterns_initially_empty(self, in_memory_db):
        """Verify intent_patterns table is initially empty."""
        IntentExtractor(in_memory_db)
        rows = in_memory_db.execute("SELECT COUNT(*) FROM intent_patterns").fetchone()[0]
        assert rows == 0, (
            "Migration: intent_patterns initially empty"
        )

    def test_seed_migrated_after_match(self, in_memory_db):
        """Verify seed patterns migrate to SQLite on first match."""
        extractor = IntentExtractor(in_memory_db)
        extractor.extract_intents("loop through all items")

        rows = in_memory_db.execute("SELECT COUNT(*) FROM intent_patterns").fetchone()[0]
        assert rows >= 1, (
            f"Migration: seed migrated to intent_patterns after match: rows={rows}"
        )

    def test_full_trigger_list_preserved(self, in_memory_db):
        """Verify full trigger list is preserved in migration."""
        extractor = IntentExtractor(in_memory_db)
        extractor.extract_intents("loop through all items")

        row = in_memory_db.execute(
            "SELECT * FROM intent_patterns WHERE intent_name = 'iteration'"
        ).fetchone()
        assert row is not None, "Migration: iteration row found"
        triggers = json.loads(row["triggers_json"])
        assert len(triggers) == 8, (
            f"Migration: full trigger list preserved (8 triggers): triggers_count={len(triggers)}"
        )

    def test_full_requires_preserved(self, in_memory_db):
        """Verify full requires list is preserved in migration."""
        extractor = IntentExtractor(in_memory_db)
        extractor.extract_intents("loop through all items")

        row = in_memory_db.execute(
            "SELECT * FROM intent_patterns WHERE intent_name = 'iteration'"
        ).fetchone()
        assert row is not None, "Migration: iteration row found"
        requires = json.loads(row["requires_json"])
        assert "keywords" in requires and len(requires["keywords"]) == 3, (
            f"Migration: full requires preserved: requires={requires}"
        )


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Duplicate Migration is No-Op
# ---------------------------------------------------------------------------


class TestIntentExtractorDuplicateMigration:
    """Tests for IntentExtractor duplicate migration handling."""

    def test_duplicate_insert_or_ignore(self, in_memory_db):
        """Verify duplicate migration uses INSERT OR IGNORE."""
        extractor = IntentExtractor(in_memory_db)

        # First match -> migration occurs
        extractor.extract_intents("iterate through all items")
        count_1 = in_memory_db.execute("SELECT COUNT(*) FROM intent_patterns").fetchone()[0]

        # Second match -> should NOT duplicate
        extractor.extract_intents("loop through each element")
        count_2 = in_memory_db.execute("SELECT COUNT(*) FROM intent_patterns").fetchone()[0]

        assert count_2 == count_1, (
            f"Migration: duplicate INSERT OR IGNORE is no-op: count_1={count_1}, count_2={count_2}"
        )


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Layer 2 > Layer 1 Priority
# ---------------------------------------------------------------------------


class TestIntentExtractorLayerPriority:
    """Tests for IntentExtractor layer priority."""

    def test_learned_pattern_takes_priority(self, in_memory_db):
        """Verify learned patterns take priority over seeds."""
        extractor = IntentExtractor(in_memory_db)

        # Insert a learned pattern with high score
        in_memory_db.execute("""
            INSERT INTO intent_patterns
            (intent_name, triggers_json, requires_json, source, score,
             evidence_count, last_updated, created_at)
            VALUES (?, ?, ?, 'learned', 0.9, 10, datetime('now'), datetime('now'))
        """, (
            "iteration",
            json.dumps(["all rows", "each row"]),
            json.dumps({"keywords": ["Get Elements", "FOR", "END"]}),
        ))
        in_memory_db.commit()

        # Should match learned pattern (Layer 2), not seed (Layer 1)
        intents = extractor.extract_intents("display all rows in table")
        assert len(intents) > 0 and intents[0]["source"] == "learned", (
            f"Priority: Layer 2 (learned) matched before Layer 1 (seed): "
            f"source={intents[0]['source'] if intents else 'N/A'}"
        )


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Score Threshold Filtering
# ---------------------------------------------------------------------------


class TestIntentExtractorThreshold:
    """Tests for IntentExtractor score threshold filtering."""

    def test_below_threshold_not_returned(self, in_memory_db):
        """Verify learned patterns below injection threshold are not returned."""
        extractor = IntentExtractor(in_memory_db)

        # Insert learned pattern below threshold (0.4)
        in_memory_db.execute("""
            INSERT INTO intent_patterns
            (intent_name, triggers_json, requires_json, source, score,
             evidence_count, last_updated, created_at)
            VALUES (?, ?, ?, 'learned', 0.2, 5, datetime('now'), datetime('now'))
        """, (
            "low_score_intent",
            json.dumps(["special trigger"]),
            json.dumps({"keywords": ["Some Keyword"]}),
        ))
        in_memory_db.commit()

        intents = extractor.extract_intents("special trigger query")
        low_matches = [i for i in intents if i["intent"] == "low_score_intent"]
        assert len(low_matches) == 0, (
            f"Threshold: learned pattern below 0.4 NOT returned by Layer 2: "
            f"matched={[i['intent'] for i in intents]}"
        )


# ---------------------------------------------------------------------------
# Test: IntentExtractor -- Layer 3 Placeholder
# ---------------------------------------------------------------------------


class TestIntentExtractorLayer3:
    """Tests for IntentExtractor Layer 3 placeholder."""

    def test_layer3_returns_empty(self, in_memory_db):
        """Verify Layer 3 returns empty list in Phase 1."""
        extractor = IntentExtractor(in_memory_db)
        intents = extractor.extract_intents("perform a completely novel action")
        assert intents == [], (
            "Layer 3: returns [] (Phase 3 placeholder)"
        )


# ---------------------------------------------------------------------------
# Test: StructuralRuleEngine -- ABC Implementation
# ---------------------------------------------------------------------------


class TestStructuralRuleEngineABC:
    """Tests for StructuralRuleEngine ABC compliance."""

    def test_implements_learning_engine(self, in_memory_db):
        """Verify StructuralRuleEngine implements LearningEngine ABC."""
        extractor = IntentExtractor(in_memory_db)
        engine = StructuralRuleEngine(in_memory_db, extractor)
        assert isinstance(engine, LearningEngine), (
            "SRE: isinstance(StructuralRuleEngine, LearningEngine)"
        )


# ---------------------------------------------------------------------------
# Test: _extract_code_elements -- Various Inputs
# ---------------------------------------------------------------------------


class TestExtractCodeElements:
    """Tests for StructuralRuleEngine._extract_code_elements."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_linear_code(self, in_memory_db):
        """Verify linear code keyword calls are extracted."""
        engine = self._make_engine(in_memory_db)
        linear_code = (
            "    Click    id=submit\n"
            "    Fill Text    id=name    John\n"
            "    Get Text    id=result"
        )
        elements = engine._extract_code_elements(linear_code)
        assert "Click" in elements and "Fill Text" in elements and "Get Text" in elements, (
            f"Extract: linear code -> keyword calls extracted: elements={sorted(elements)}"
        )

    def test_for_loop_code(self, in_memory_db):
        """Verify FOR loop tokens are extracted."""
        engine = self._make_engine(in_memory_db)
        for_code = (
            "    ${elements}=    Get Elements    css=tr\n"
            "    FOR    ${elem}    IN    @{elements}\n"
            "        Click    ${elem}\n"
            "    END"
        )
        elements = engine._extract_code_elements(for_code)
        assert "FOR" in elements and "END" in elements, (
            f"Extract: FOR loop -> 'FOR' and 'END' in elements: elements={sorted(elements)}"
        )

    def test_variable_assignment(self, in_memory_db):
        """Verify variable assignment keyword is extracted."""
        engine = self._make_engine(in_memory_db)
        var_code = "    ${count}=    Get Element Count    css=.item"
        elements = engine._extract_code_elements(var_code)
        assert "Get Element Count" in elements, (
            f"Extract: variable assignment -> keyword extracted: elements={sorted(elements)}"
        )

    def test_continuation_line(self, in_memory_db):
        """Verify continuation line token is extracted."""
        engine = self._make_engine(in_memory_db)
        continuation_code = (
            "    Click\n"
            "    ...    id=submit\n"
        )
        elements = engine._extract_code_elements(continuation_code)
        assert "id=submit" in elements, (
            f"Extract: continuation '...' -> next token extracted: elements={sorted(elements)}"
        )

    def test_comments_and_headers_skipped(self, in_memory_db):
        """Verify comments and section headers are skipped."""
        engine = self._make_engine(in_memory_db)
        mixed_code = (
            "*** Keywords ***\n"
            "# This is a comment\n"
            "    Click    id=btn\n"
        )
        elements = engine._extract_code_elements(mixed_code)
        assert (
            "Click" in elements
            and "# This is a comment" not in elements
            and "*** Keywords ***" not in elements
        ), f"Extract: comments/headers skipped, 'Click' extracted: elements={sorted(elements)}"

    def test_none_input(self, in_memory_db):
        """Verify None input returns empty frozenset."""
        engine = self._make_engine(in_memory_db)
        elements = engine._extract_code_elements(None)
        assert elements == frozenset(), (
            "Extract: None input -> empty frozenset"
        )

    def test_empty_string(self, in_memory_db):
        """Verify empty string returns empty frozenset."""
        engine = self._make_engine(in_memory_db)
        elements = engine._extract_code_elements("")
        assert elements == frozenset(), (
            "Extract: empty string -> empty frozenset"
        )

    def test_no_false_positive_form_data(self, in_memory_db):
        """Verify ${FORM_DATA} does NOT extract 'FOR'."""
        engine = self._make_engine(in_memory_db)
        no_fp_code = "    Log    ${FORM_DATA}"
        elements = engine._extract_code_elements(no_fp_code)
        assert "FOR" not in elements, (
            f"Extract: '${{FORM_DATA}}' does NOT extract 'FOR': elements={sorted(elements)}"
        )


# ---------------------------------------------------------------------------
# Test: _has_required_keywords
# ---------------------------------------------------------------------------


class TestHasRequiredKeywords:
    """Tests for StructuralRuleEngine._has_required_keywords."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_all_present(self, in_memory_db):
        """Verify all required keywords present returns True."""
        engine = self._make_engine(in_memory_db)
        code = (
            "    ${elems}=    Get Elements    css=tr\n"
            "    FOR    ${e}    IN    @{elems}\n"
            "        Click    ${e}\n"
            "    END"
        )
        assert engine._has_required_keywords(
            code, ["Get Elements", "FOR", "END"]
        ) is True, "HasKW: all required present -> True"

    def test_one_missing(self, in_memory_db):
        """Verify one missing keyword returns False."""
        engine = self._make_engine(in_memory_db)
        code = (
            "    ${elems}=    Get Elements    css=tr\n"
            "    FOR    ${e}    IN    @{elems}\n"
            "        Click    ${e}\n"
            "    END"
        )
        assert engine._has_required_keywords(
            code, ["Get Elements", "FOR", "END", "Should Contain"]
        ) is False, "HasKW: one missing -> False"

    def test_empty_required_list(self, in_memory_db):
        """Verify empty required list returns True."""
        engine = self._make_engine(in_memory_db)
        code = (
            "    ${elems}=    Get Elements    css=tr\n"
            "    FOR    ${e}    IN    @{elems}\n"
            "        Click    ${e}\n"
            "    END"
        )
        assert engine._has_required_keywords(code, []) is True, (
            "HasKW: empty required list -> True"
        )


# ---------------------------------------------------------------------------
# Test: StructuralRuleEngine.learn() -- Evidence Logic
# ---------------------------------------------------------------------------


class TestSRELearnEvidence:
    """Tests for StructuralRuleEngine.learn() evidence logic."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_passed_with_keywords_evidence(self, in_memory_db):
        """Verify passed + keywords present gives evidence +1."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items and click each one",
            robot_code=(
                "    ${elems}=    Get Elements    css=.item\n"
                "    FOR    ${e}    IN    @{elems}\n"
                "        Click    ${e}\n"
                "    END"
            ),
            test_status="passed",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule is not None and rule["evidence_count"] == 1, (
            f"Learn: passed + keywords present -> evidence=1: "
            f"evidence={rule['evidence_count'] if rule else 'N/A'}"
        )

    def test_passed_with_keywords_no_counter(self, in_memory_db):
        """Verify passed + keywords present gives counter_evidence=0."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items and click each one",
            robot_code=(
                "    ${elems}=    Get Elements    css=.item\n"
                "    FOR    ${e}    IN    @{elems}\n"
                "        Click    ${e}\n"
                "    END"
            ),
            test_status="passed",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule["counter_evidence"] == 0, (
            "Learn: passed + keywords present -> counter_evidence=0"
        )

    def test_passed_without_keywords_counter_evidence(self, in_memory_db):
        """Verify passed + keywords MISSING gives counter-evidence +1."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",  # No FOR/END/Get Elements
            test_status="passed",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule is not None and rule["counter_evidence"] == 1, (
            f"Learn: passed + keywords missing -> counter_evidence=1: "
            f"counter_evidence={rule['counter_evidence'] if rule else 'N/A'}"
        )

    def test_passed_without_keywords_no_evidence(self, in_memory_db):
        """Verify passed + keywords MISSING gives evidence=0."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",
            test_status="passed",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule["evidence_count"] == 0, (
            "Learn: passed + keywords missing -> evidence=0"
        )

    def test_failed_category_a_boosted_evidence(self, in_memory_db):
        """Verify failed + category A gives boosted evidence +3."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",
            test_status="failed",
            failure_category="A1",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule is not None and rule["evidence_count"] == 3, (
            f"Learn: failed + cat A -> evidence=3 (boosted): "
            f"evidence={rule['evidence_count'] if rule else 'N/A'}"
        )

    def test_failed_non_a_category_no_change(self, in_memory_db):
        """Verify failed + non-A category does not change evidence."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",
            test_status="failed",
            failure_category="B1",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule is not None and rule["evidence_count"] == 0, (
            f"Learn: failed + cat B -> evidence=0 (no change): "
            f"evidence={rule['evidence_count'] if rule else 'N/A'}"
        )

    def test_failed_non_a_category_no_counter_change(self, in_memory_db):
        """Verify failed + non-A category does not change counter_evidence."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",
            test_status="failed",
            failure_category="B1",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule["counter_evidence"] == 0, (
            "Learn: failed + cat B -> counter_evidence=0 (no change)"
        )


# ---------------------------------------------------------------------------
# Test: StructuralRuleEngine.learn() -- Mixed Structure
# ---------------------------------------------------------------------------


class TestSRELearnMixedStructure:
    """Tests for StructuralRuleEngine.learn() with mixed structure."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_iteration_evidence_in_mixed(self, in_memory_db):
        """Verify learn() gives evidence for iteration in mixed code."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="for each item only if it exists, verify the text",
            robot_code=(
                "    ${elems}=    Get Elements    css=.item\n"
                "    FOR    ${e}    IN    @{elems}\n"
                "        ${count}=    Get Element Count    ${e}\n"
                "        Run Keyword If    ${count} > 0    Click    ${e}\n"
                "    END"
            ),
            test_status="passed",
        )
        engine.learn(record)

        iter_rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert iter_rule is not None and iter_rule["evidence_count"] >= 1, (
            f"Mixed: iteration rule has evidence: "
            f"evidence={iter_rule['evidence_count'] if iter_rule else 'N/A'}"
        )

    def test_conditional_evidence_in_mixed(self, in_memory_db):
        """Verify learn() gives evidence for conditional_action in mixed code."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="for each item only if it exists, verify the text",
            robot_code=(
                "    ${elems}=    Get Elements    css=.item\n"
                "    FOR    ${e}    IN    @{elems}\n"
                "        ${count}=    Get Element Count    ${e}\n"
                "        Run Keyword If    ${count} > 0    Click    ${e}\n"
                "    END"
            ),
            test_status="passed",
        )
        engine.learn(record)

        cond_rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'conditional_action'"
        ).fetchone()
        assert cond_rule is not None and cond_rule["evidence_count"] >= 1, (
            f"Mixed: conditional_action rule has evidence: "
            f"evidence={cond_rule['evidence_count'] if cond_rule else 'N/A'}"
        )


# ---------------------------------------------------------------------------
# Test: _get_or_create_rule -- Initial Values
# ---------------------------------------------------------------------------


class TestSREGetOrCreateRule:
    """Tests for StructuralRuleEngine rule creation."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_rule_created(self, in_memory_db):
        """Verify new rules are created via learn."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",
            test_status="passed",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        assert rule is not None, "InitRule: rule created with initial score"

    def test_existing_rule_not_duplicated(self, in_memory_db):
        """Verify _get_or_create_rule returns existing rule, not duplicate."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items",
            robot_code="    Click    id=submit",
            test_status="passed",
        )

        # First call creates the rule
        engine.learn(record)

        # Second call should reuse the same rule
        engine.learn(record)

        count = in_memory_db.execute(
            "SELECT COUNT(*) FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()[0]
        assert count == 1, (
            f"InitRule: existing rule not duplicated: count={count}"
        )


# ---------------------------------------------------------------------------
# Test: Score Recalculation via EffectivenessScore
# ---------------------------------------------------------------------------


class TestSREScoreRecalculation:
    """Tests for StructuralRuleEngine score recalculation."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_score_via_effectiveness_score(self, in_memory_db):
        """Verify score is recalculated via EffectivenessScore.calculate()."""
        engine = self._make_engine(in_memory_db)
        record = MockRecord(
            user_query="loop through all items and click each one",
            robot_code=(
                "    ${elems}=    Get Elements    css=.item\n"
                "    FOR    ${e}    IN    @{elems}\n"
                "        Click    ${e}\n"
                "    END"
            ),
            test_status="passed",
        )
        engine.learn(record)

        rule = in_memory_db.execute(
            "SELECT * FROM structural_rules WHERE rule_name = 'iteration'"
        ).fetchone()
        expected_score = EffectivenessScore.calculate(1, 0)
        assert abs(rule["score"] - expected_score) < 0.001, (
            f"Score: recalculated via EffectivenessScore.calculate(1, 0): "
            f"got={rule['score']}, expected={expected_score}"
        )


# ---------------------------------------------------------------------------
# Test: StructuralRuleEngine.get_hints()
# ---------------------------------------------------------------------------


class TestSREGetHints:
    """Tests for StructuralRuleEngine.get_hints()."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_planner_gets_structural_hints(self, in_memory_db):
        """Verify planner role gets structural hints."""
        engine = self._make_engine(in_memory_db)

        # Insert a well-evidenced rule
        in_memory_db.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure, required_keywords_json,
             score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES ('iteration', 'all items|each row', 'for_loop', ?, 0.85, 10, 1,
                    datetime('now'), datetime('now'))
        """, (json.dumps(["Get Elements", "FOR", "END"]),))
        in_memory_db.commit()

        hints = engine.get_hints("loop through all items", "http://test.com", "planner")
        assert hints is not None and len(hints) > 0, (
            f"Hints: planner role -> structural hints returned: hints={hints}"
        )

    def test_hints_contain_structural(self, in_memory_db):
        """Verify hints contain 'STRUCTURAL'."""
        engine = self._make_engine(in_memory_db)

        in_memory_db.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure, required_keywords_json,
             score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES ('iteration', 'all items|each row', 'for_loop', ?, 0.85, 10, 1,
                    datetime('now'), datetime('now'))
        """, (json.dumps(["Get Elements", "FOR", "END"]),))
        in_memory_db.commit()

        hints = engine.get_hints("loop through all items", "http://test.com", "planner")
        assert hints is not None and any("STRUCTURAL" in h for h in hints), (
            "Hints: hint contains 'STRUCTURAL'"
        )

    def test_identifier_role_gets_none(self, in_memory_db):
        """Verify identifier role gets None."""
        engine = self._make_engine(in_memory_db)

        in_memory_db.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure, required_keywords_json,
             score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES ('iteration', 'all items|each row', 'for_loop', ?, 0.85, 10, 1,
                    datetime('now'), datetime('now'))
        """, (json.dumps(["Get Elements", "FOR", "END"]),))
        in_memory_db.commit()

        hints = engine.get_hints("loop through all items", "http://test.com", "identifier")
        assert hints is None, "Hints: identifier role -> None"

    def test_below_threshold_returns_none(self, in_memory_db):
        """Verify get_hints returns None when rule below threshold."""
        engine = self._make_engine(in_memory_db)

        # Insert a rule below threshold (only 1 observation, needs 3+)
        in_memory_db.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure, required_keywords_json,
             score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES ('iteration', 'all items', 'for_loop', ?, 0.5, 1, 0,
                    datetime('now'), datetime('now'))
        """, (json.dumps(["Get Elements", "FOR", "END"]),))
        in_memory_db.commit()

        hints = engine.get_hints("loop through all items", "http://test.com", "planner")
        assert hints is None, (
            f"Hints: rule with < 3 observations -> None: hints={hints}"
        )


# ---------------------------------------------------------------------------
# Test: StructuralRuleEngine.get_stats()
# ---------------------------------------------------------------------------


class TestSREGetStats:
    """Tests for StructuralRuleEngine.get_stats()."""

    def _make_engine(self, conn):
        extractor = IntentExtractor(conn)
        return StructuralRuleEngine(conn, extractor)

    def test_empty_stats(self, in_memory_db):
        """Verify empty database returns total=0, active=0."""
        engine = self._make_engine(in_memory_db)
        stats = engine.get_stats()
        assert stats["total_rules"] == 0 and stats["active_rules"] == 0, (
            "Stats: empty -> total=0, active=0"
        )

    def test_stats_with_rules(self, in_memory_db):
        """Verify stats counts total and active rules correctly."""
        engine = self._make_engine(in_memory_db)

        # Insert an active rule (high score, enough observations)
        in_memory_db.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure, required_keywords_json,
             score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES ('iteration', 'all items', 'for_loop', ?, 0.85, 10, 1,
                    datetime('now'), datetime('now'))
        """, (json.dumps(["Get Elements", "FOR", "END"]),))

        # Insert an inactive rule (low score, not enough observations)
        in_memory_db.execute("""
            INSERT INTO structural_rules
            (rule_name, query_pattern, required_structure, required_keywords_json,
             score, evidence_count, counter_evidence,
             last_updated, created_at)
            VALUES ('low_rule', 'test', 'linear', '[]', 0.1, 0, 5,
                    datetime('now'), datetime('now'))
        """)
        in_memory_db.commit()

        stats = engine.get_stats()
        assert stats["total_rules"] == 2 and stats["active_rules"] == 1, (
            f"Stats: total=2, active=1: total={stats['total_rules']}, active={stats['active_rules']}"
        )

    def test_engine_name(self, in_memory_db):
        """Verify engine name is 'structural_rule'."""
        engine = self._make_engine(in_memory_db)
        stats = engine.get_stats()
        assert stats["engine"] == "structural_rule", (
            "Stats: engine name is 'structural_rule'"
        )


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine -- ABC Implementation
# ---------------------------------------------------------------------------


class TestKCEABC:
    """Tests for KeywordCorrectionEngine ABC compliance."""

    def test_implements_learning_engine(self, in_memory_db):
        """Verify KeywordCorrectionEngine implements LearningEngine ABC."""
        engine = KeywordCorrectionEngine(in_memory_db)
        assert isinstance(engine, LearningEngine), (
            "KCE: isinstance(KeywordCorrectionEngine, LearningEngine)"
        )


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine.learn() -- B-Category
# ---------------------------------------------------------------------------


class TestKCELearn:
    """Tests for KeywordCorrectionEngine.learn()."""

    def test_b1_stores_wrong_keyword(self, in_memory_db):
        """Verify learn() extracts and stores keyword corrections for B1 failures."""
        engine = KeywordCorrectionEngine(in_memory_db)
        record = MockRecord(
            test_status="failed",
            failure_category="B1",
            error_message="No keyword with name 'FooBar' found",
        )
        engine.learn(record)

        row = in_memory_db.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = 'FooBar'"
        ).fetchone()
        assert row is not None, "KCE Learn: B1 -> wrong keyword 'FooBar' stored"

    def test_unknown_keyword_correct_is_none(self, in_memory_db):
        """Verify unknown keyword maps correct_keyword to None."""
        engine = KeywordCorrectionEngine(in_memory_db)
        record = MockRecord(
            test_status="failed",
            failure_category="B1",
            error_message="No keyword with name 'FooBar' found",
        )
        engine.learn(record)

        row = in_memory_db.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = 'FooBar'"
        ).fetchone()
        assert row is not None and row["correct_keyword"] is None, (
            "KCE Learn: unknown keyword -> correct_keyword=None"
        )

    def test_b6_known_correction(self, in_memory_db):
        """Verify learn() maps known Selenium keywords to Browser equivalents."""
        engine = KeywordCorrectionEngine(in_memory_db)
        record = MockRecord(
            test_status="failed",
            failure_category="B6",
            error_message="No keyword with name 'Input Text' found",
        )
        engine.learn(record)

        row = in_memory_db.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = 'Input Text'"
        ).fetchone()
        assert row is not None and row["correct_keyword"] == "Fill Text", (
            f"KCE Learn: B6 'Input Text' -> correct='Fill Text': "
            f"correct={row['correct_keyword'] if row else 'N/A'}"
        )

    def test_non_b_category_no_learning(self, in_memory_db):
        """Verify learn() does not process non-B category failures."""
        engine = KeywordCorrectionEngine(in_memory_db)
        record = MockRecord(
            test_status="failed",
            failure_category="A1",
            error_message="No keyword with name 'Input Text' found",
        )
        engine.learn(record)

        count = in_memory_db.execute(
            "SELECT COUNT(*) FROM keyword_corrections"
        ).fetchone()[0]
        assert count == 0, "KCE Learn: non-B category -> no learning"

    def test_duplicate_increments_evidence(self, in_memory_db):
        """Verify learn() increments evidence for duplicate wrong keywords."""
        engine = KeywordCorrectionEngine(in_memory_db)
        record = MockRecord(
            test_status="failed",
            failure_category="B1",
            error_message="No keyword with name 'Input Text' found",
        )

        # First occurrence
        engine.learn(record)
        row = in_memory_db.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = 'Input Text'"
        ).fetchone()
        initial_evidence = row["evidence_count"]

        # Second occurrence
        engine.learn(record)
        row = in_memory_db.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = 'Input Text'"
        ).fetchone()
        assert row["evidence_count"] == initial_evidence + 1, (
            f"KCE Learn: duplicate -> evidence incremented: "
            f"evidence={row['evidence_count']}, expected={initial_evidence + 1}"
        )


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine._extract_wrong_keyword()
# ---------------------------------------------------------------------------


class TestKCEExtractWrongKeyword:
    """Tests for KeywordCorrectionEngine._extract_wrong_keyword()."""

    def test_standard_pattern(self, in_memory_db):
        """Verify standard pattern extracts keyword name."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._extract_wrong_keyword(
            "No keyword with name 'Input Text' found"
        )
        assert result == "Input Text", (
            "Extract: standard pattern -> 'Input Text'"
        )

    def test_no_match(self, in_memory_db):
        """Verify no match returns None."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._extract_wrong_keyword("Some other error message")
        assert result is None, "Extract: no match -> None"

    def test_none_input(self, in_memory_db):
        """Verify None input returns None."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._extract_wrong_keyword(None)
        assert result is None, "Extract: None input -> None"

    def test_empty_input(self, in_memory_db):
        """Verify empty input returns None."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._extract_wrong_keyword("")
        assert result is None, "Extract: empty input -> None"


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine._infer_correct_keyword()
# ---------------------------------------------------------------------------


class TestKCEInferCorrectKeyword:
    """Tests for KeywordCorrectionEngine._infer_correct_keyword()."""

    def test_input_text_to_fill_text(self, in_memory_db):
        """Verify 'Input Text' maps to 'Fill Text'."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._infer_correct_keyword("Input Text")
        assert result == "Fill Text", "Infer: 'Input Text' -> 'Fill Text'"

    def test_click_element_to_click(self, in_memory_db):
        """Verify 'Click Element' maps to 'Click'."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._infer_correct_keyword("Click Element")
        assert result == "Click", "Infer: 'Click Element' -> 'Click'"

    def test_get_webelements_to_get_elements(self, in_memory_db):
        """Verify 'Get WebElements' maps to 'Get Elements'."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._infer_correct_keyword("Get WebElements")
        assert result == "Get Elements", "Infer: 'Get WebElements' -> 'Get Elements'"

    def test_unknown_keyword_returns_none(self, in_memory_db):
        """Verify unknown keyword returns None."""
        engine = KeywordCorrectionEngine(in_memory_db)
        result = engine._infer_correct_keyword("SomeUnknownKeyword")
        assert result is None, "Infer: unknown keyword -> None"


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine.get_hints()
# ---------------------------------------------------------------------------


class TestKCEGetHints:
    """Tests for KeywordCorrectionEngine.get_hints()."""

    def test_assembler_with_evidence_gets_hints(self, in_memory_db):
        """Verify get_hints returns hints for assembler with sufficient evidence."""
        engine = KeywordCorrectionEngine(in_memory_db)

        in_memory_db.execute("""
            INSERT INTO keyword_corrections
            (wrong_keyword, correct_keyword, library, error_pattern, score,
             evidence_count, last_seen)
            VALUES ('Input Text', 'Fill Text', 'browser', 'error', 0.5, 5,
                    datetime('now'))
        """)
        in_memory_db.commit()

        hints = engine.get_hints("fill in the form", "http://test.com", "assembler")
        assert hints is not None and len(hints) > 0, (
            f"KCE Hints: assembler + evidence>=3 -> hints returned: hints={hints}"
        )

    def test_hint_mentions_correct_keyword(self, in_memory_db):
        """Verify hint mentions the correct keyword."""
        engine = KeywordCorrectionEngine(in_memory_db)

        in_memory_db.execute("""
            INSERT INTO keyword_corrections
            (wrong_keyword, correct_keyword, library, error_pattern, score,
             evidence_count, last_seen)
            VALUES ('Input Text', 'Fill Text', 'browser', 'error', 0.5, 5,
                    datetime('now'))
        """)
        in_memory_db.commit()

        hints = engine.get_hints("fill in the form", "http://test.com", "assembler")
        assert hints is not None and any("Fill Text" in h for h in hints), (
            "KCE Hints: hint mentions correct keyword"
        )

    def test_planner_role_gets_none(self, in_memory_db):
        """Verify planner role gets None."""
        engine = KeywordCorrectionEngine(in_memory_db)

        in_memory_db.execute("""
            INSERT INTO keyword_corrections
            (wrong_keyword, correct_keyword, library, error_pattern, score,
             evidence_count, last_seen)
            VALUES ('Input Text', 'Fill Text', 'browser', 'error', 0.5, 5,
                    datetime('now'))
        """)
        in_memory_db.commit()

        hints = engine.get_hints("fill in the form", "http://test.com", "planner")
        assert hints is None, "KCE Hints: planner role -> None"

    def test_insufficient_evidence_returns_none(self, in_memory_db):
        """Verify get_hints returns None when evidence < 3."""
        engine = KeywordCorrectionEngine(in_memory_db)

        in_memory_db.execute("""
            INSERT INTO keyword_corrections
            (wrong_keyword, correct_keyword, library, error_pattern, score,
             evidence_count, last_seen)
            VALUES ('Input Text', 'Fill Text', 'browser', 'error', 0.5, 2,
                    datetime('now'))
        """)
        in_memory_db.commit()

        hints = engine.get_hints("fill in the form", "http://test.com", "assembler")
        assert hints is None, (
            f"KCE Hints: evidence<3 -> None (even score=0.5): hints={hints}"
        )


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine.get_stats()
# ---------------------------------------------------------------------------


class TestKCEGetStats:
    """Tests for KeywordCorrectionEngine.get_stats()."""

    def test_empty_stats(self, in_memory_db):
        """Verify empty database returns total=0, active=0."""
        engine = KeywordCorrectionEngine(in_memory_db)
        stats = engine.get_stats()
        assert stats["total_corrections"] == 0 and stats["active_corrections"] == 0, (
            "KCE Stats: empty -> total=0, active=0"
        )

    def test_stats_with_corrections(self, in_memory_db):
        """Verify stats counts total and active corrections correctly."""
        engine = KeywordCorrectionEngine(in_memory_db)

        # Insert active correction (score=0.5, evidence=5)
        in_memory_db.execute("""
            INSERT INTO keyword_corrections
            (wrong_keyword, correct_keyword, library, score, evidence_count,
             last_seen)
            VALUES ('Input Text', 'Fill Text', 'browser', 0.5, 5,
                    datetime('now'))
        """)
        # Insert inactive correction (score=0.5, evidence=1 -> below MIN_OBSERVATIONS)
        in_memory_db.execute("""
            INSERT INTO keyword_corrections
            (wrong_keyword, correct_keyword, library, score, evidence_count,
             last_seen)
            VALUES ('FooBar', NULL, 'browser', 0.5, 1, datetime('now'))
        """)
        in_memory_db.commit()

        stats = engine.get_stats()
        assert stats["total_corrections"] == 2 and stats["active_corrections"] == 1, (
            f"KCE Stats: total=2, active=1: "
            f"total={stats['total_corrections']}, active={stats['active_corrections']}"
        )

    def test_engine_name(self, in_memory_db):
        """Verify engine name is 'keyword_correction'."""
        engine = KeywordCorrectionEngine(in_memory_db)
        stats = engine.get_stats()
        assert stats["engine"] == "keyword_correction", (
            "KCE Stats: engine name is 'keyword_correction'"
        )


# ---------------------------------------------------------------------------
# Test: KeywordCorrectionEngine -- Score Increases with Evidence
# ---------------------------------------------------------------------------


class TestKCEDynamicScore:
    """Tests for KeywordCorrectionEngine dynamic score behavior (SER-4 fix)."""

    def test_score_increases_with_evidence(self, in_memory_db):
        """Verify KCE score increases with evidence via EffectivenessScore.calculate()."""
        engine = KeywordCorrectionEngine(in_memory_db)
        record = MockRecord(
            test_status="failed",
            failure_category="B1",
            error_message="No keyword with name 'Input Text' found",
        )

        for _ in range(5):
            engine.learn(record)

        row = in_memory_db.execute(
            "SELECT * FROM keyword_corrections WHERE wrong_keyword = 'Input Text'"
        ).fetchone()
        expected_score = EffectivenessScore.calculate(5, 0)  # 5/(5+0+1) = 0.8333
        assert abs(row["score"] - expected_score) < 0.001, (
            f"KCE Score: increases with evidence via EffectivenessScore: "
            f"score={row['score']}, expected={expected_score}"
        )
