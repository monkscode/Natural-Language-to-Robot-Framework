"""
Day 08 Verification -- NL Feedback UI & Engine (pytest format).

Migrated from scripts/verify_day08.py.

Tests:
  1. NLFeedbackEngine seed patterns       (~12 tests)
  2. Cross-referencing                     (~6  tests)
  3. Confidence calculation                (~5  tests)
  4. Learning registry                     (~6  tests)
  5. FeedbackLoop NL triage integration    (~6  tests)
  6. API endpoints                         (~6  tests)
  7. user_query guard                      (~3  tests)
  8. Regression (Day 01-07 imports)        (~8  tests)
  9. Engine Stats & ABC                    (~4  tests)
"""

import inspect

import pytest

from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine


# ===================================================================
# 1. NLFeedbackEngine -- Seed Pattern Tests
# ===================================================================


class TestSeedPatterns:
    """NLFeedbackEngine seed pattern matching."""

    def test_loop_missing_a1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf1", "It should have checked all rows", "completely_wrong")
        assert r["taxonomy_code"] == "A1", f"Expected A1, got {r['taxonomy_code']}"
        assert r["category"] == "structural"

    def test_wrong_flow_a1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf2", "Steps are wrong, it did things out of order", "completely_wrong")
        assert r["taxonomy_code"] == "A1", f"Expected A1, got {r['taxonomy_code']}"

    def test_wrong_keyword_b1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf3", "Used the wrong keyword, should use Click Link instead", "completely_wrong")
        assert r["taxonomy_code"] == "B1", f"Expected B1, got {r['taxonomy_code']}"
        assert r["category"] == "keyword"

    def test_missing_keyword_b1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf4", "It forgot to submit the form", "completely_wrong")
        assert r["taxonomy_code"] == "B1", f"Expected B1, got {r['taxonomy_code']}"

    def test_wrong_element_c1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf5", "Clicked the wrong button", "completely_wrong")
        assert r["taxonomy_code"] == "C1", f"Expected C1, got {r['taxonomy_code']}"
        assert r["category"] == "locator"

    def test_element_not_found_c1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf6", "It couldn't find the search input", "completely_wrong")
        assert r["taxonomy_code"] == "C1", f"Expected C1, got {r['taxonomy_code']}"

    def test_too_fast_d1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf7", "It was too fast, page wasn't loaded yet", "completely_wrong")
        assert r["taxonomy_code"] == "D1", f"Expected D1, got {r['taxonomy_code']}"
        assert r["category"] == "timing"

    def test_wrong_value_e1(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf8", "Typed the wrong value in the field", "completely_wrong")
        assert r["taxonomy_code"] == "E1", f"Expected E1, got {r['taxonomy_code']}"
        assert r["category"] == "data"

    def test_no_match_x0(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf9", "I just don't like the color scheme", "completely_wrong")
        assert r["taxonomy_code"] == "X0", f"Expected X0, got {r['taxonomy_code']}"
        assert r["category"] == "uncategorized"

    def test_empty_text_close_enough_p0(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf10", "", "close_enough")
        assert r["taxonomy_code"] == "P0", f"Expected P0, got {r['taxonomy_code']}"
        assert r["category"] == "positive"
        assert r["confidence"] == 1.0

    def test_empty_text_completely_wrong_n0(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf11", "", "completely_wrong")
        assert r["taxonomy_code"] == "N0", f"Expected N0, got {r['taxonomy_code']}"
        assert r["category"] == "negative"

    def test_whitespace_only_is_empty(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf12", "   \n\t  ", "close_enough")
        assert r["taxonomy_code"] == "P0", f"Expected P0, got {r['taxonomy_code']}"


# ===================================================================
# 2. Cross-Referencing Tests
# ===================================================================


class TestCrossReferencing:
    """Cross-referencing feedback text against error messages."""

    def test_a1_structural_error_confirms(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf1", "Should have checked all rows",
            "completely_wrong",
            error_message="Count does not match expected: 5 items found",
        )
        assert r["taxonomy_code"] == "A1"
        # Cross-ref boost should increase confidence
        assert r["confidence"] >= 0.7, f"Expected >= 0.7, got {r['confidence']}"

    def test_b1_keyword_error_confirms(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf2", "Used the wrong keyword",
            "completely_wrong",
            error_message="No keyword with name 'Click Element' found in library",
        )
        assert r["taxonomy_code"] == "B1"
        assert r["confidence"] >= 0.7

    def test_c1_locator_error_confirms(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf3", "Clicked the wrong element",
            "completely_wrong",
            error_message="Element not found: id=submit-btn",
        )
        assert r["taxonomy_code"] == "C1"
        assert r["confidence"] >= 0.7

    def test_d1_timing_error_confirms(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf4", "Page didn't wait to load",
            "completely_wrong",
            error_message="Timeout: waited 10s for element",
        )
        assert r["taxonomy_code"] == "D1"
        assert r["confidence"] >= 0.7

    def test_error_does_not_confirm_category(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf5", "Should have checked all rows",
            "completely_wrong",
            error_message="Variable '${username}' not found",
        )
        assert r["taxonomy_code"] == "A1"
        # Variable error should NOT confirm structural issue
        assert r["confidence"] < 0.9

    def test_no_error_message_no_boost(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf6", "Should have checked all rows",
            "completely_wrong",
            error_message=None,
        )
        assert r["taxonomy_code"] == "A1"
        base_conf = 0.50 + 0.15  # base + 1 pattern
        assert r["confidence"] <= base_conf + 0.01


# ===================================================================
# 3. Confidence Calculation Tests
# ===================================================================


class TestConfidence:
    """Confidence score calculation."""

    def test_base_with_single_pattern(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf1", "It was too fast", "completely_wrong")
        assert 0.6 <= r["confidence"] <= 0.7, f"Got {r['confidence']}"

    def test_multi_pattern_boost(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf2",
            "It should have checked all rows, every item needs to iterate",
            "completely_wrong",
        )
        assert r["confidence"] >= 0.65, f"Expected >= 0.65, got {r['confidence']}"

    def test_cross_ref_boost(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf3", "Timeout issue, didn't wait",
            "completely_wrong",
            error_message="Timeout: waited 10 seconds",
        )
        assert r["confidence"] >= 0.7

    def test_capped_at_1_0(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback(
            "wf4",
            "Should have checked all rows, every item, each product, needs loop, iterate, missing loop, only first item",
            "completely_wrong",
            error_message="Count does not match expected",
        )
        assert r["confidence"] <= 1.0, f"Got {r['confidence']}"

    def test_uncategorized_gets_base_confidence(self):
        engine = NLFeedbackEngine()
        r = engine.process_feedback("wf5", "The moon is beautiful tonight", "completely_wrong")
        assert r["confidence"] == 0.50, f"Expected 0.50, got {r['confidence']}"


# ===================================================================
# 4. Learning Registry Tests
# ===================================================================


class TestLearningRegistry:
    """learning_registry module structure and exports."""

    def test_import_successful(self):
        from src.backend.crew_ai.optimization.learning_registry import (
            get_feedback_loop,
        )
        assert callable(get_feedback_loop)

    def test_get_feedback_loop_importable_from_workflow_service(self):
        from src.backend.services.workflow_service import get_feedback_loop
        assert callable(get_feedback_loop)

    def test_module_globals_exist(self):
        import src.backend.crew_ai.optimization.learning_registry as reg
        assert hasattr(reg, '_feedback_loop_instance')
        assert hasattr(reg, '_feedback_loop_init_attempted')
        # _registry_lock was added to fix the singleton race condition
        assert hasattr(reg, '_registry_lock')

    def test_uses_settings_for_optimization_enabled(self):
        import src.backend.crew_ai.optimization.learning_registry as reg
        src_code = open(reg.__file__, 'r', encoding='utf-8').read()
        assert 'from src.backend.core.config import settings' in src_code

    def test_try_once_caching_pattern(self):
        import src.backend.crew_ai.optimization.learning_registry as reg
        src_code = open(reg.__file__, 'r', encoding='utf-8').read()
        assert '_feedback_loop_init_attempted' in src_code
        assert 'if _feedback_loop_init_attempted:' in src_code


# ===================================================================
# 5. FeedbackLoop NL Triage Integration Tests
# ===================================================================


class TestFeedbackLoopIntegration:
    """FeedbackLoop.process_user_feedback NL triage wiring."""

    def test_process_user_feedback_returns_dict(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        sig = inspect.signature(FeedbackLoop.process_user_feedback)
        # Return annotation should be dict
        assert sig.return_annotation == dict, f"Return annotation is {sig.return_annotation}"

    def test_process_user_feedback_contains_nl_triage(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        src = inspect.getsource(FeedbackLoop.process_user_feedback)
        assert 'nl_engine' in src, "Missing nl_engine reference in process_user_feedback"
        assert 'process_feedback' in src, "Missing process_feedback call"

    def test_routes_to_all_engines(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        src = inspect.getsource(FeedbackLoop.process_user_feedback)
        assert 'learn_from_feedback' in src, "Missing learn_from_feedback call"
        assert 'structural_engine' in src
        assert 'keyword_engine' in src
        assert 'anti_pattern_engine' in src

    def test_returns_fallback_when_circuit_breaker_off(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        src = inspect.getsource(FeedbackLoop.process_user_feedback)
        assert '_fallback_triage' in src, "Missing fallback triage dict"
        assert 'return _fallback_triage' in src

    def test_extracts_error_message_for_cross_ref(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        src = inspect.getsource(FeedbackLoop.process_user_feedback)
        assert 'error_message' in src, "Missing error_message extraction"

    def test_get_learning_stats_method_exists(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        assert hasattr(FeedbackLoop, 'get_learning_stats')


# ===================================================================
# 6. API Endpoint Tests
# ===================================================================


class TestAPIEndpoints:
    """API endpoint registration and models."""

    def test_feedback_request_model(self):
        from src.backend.api.endpoints import FeedbackRequest
        assert FeedbackRequest is not None
        # Verify fields
        m = FeedbackRequest(workflow_id="test", feedback_type="close_enough")
        assert m.workflow_id == "test"
        assert m.feedback_text == ""
        assert m.feedback_type == "close_enough"

    def test_submit_feedback_endpoint_exists(self):
        from src.backend.api.endpoints import submit_feedback
        assert callable(submit_feedback)

    def test_get_learning_stats_endpoint_exists(self):
        from src.backend.api.endpoints import get_learning_stats
        assert callable(get_learning_stats)

    def test_feedback_route_registered(self):
        from src.backend.api.endpoints import router
        routes = [r.path for r in router.routes]
        assert '/api/feedback' in routes, f"Routes: {routes}"

    def test_learning_stats_route_registered(self):
        from src.backend.api.endpoints import router
        routes = [r.path for r in router.routes]
        assert '/api/learning-stats' in routes, f"Routes: {routes}"

    def test_imports_get_feedback_loop_from_learning_registry(self):
        import src.backend.api.endpoints as ep
        src = inspect.getsource(ep)
        assert 'from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop' in src


# ===================================================================
# 7. user_query Guard Tests
# ===================================================================


class TestUserQueryGuard:
    """user_query guard in _process_learning."""

    def test_process_learning_has_user_query_guard(self):
        from src.backend.services.workflow_service import _process_learning
        src = inspect.getsource(_process_learning)
        assert 'not user_query' in src or "user_query.strip()" in src, \
            "Missing user_query guard"
        assert 'Skipping learning' in src or 'paste-and-execute' in src

    def test_guard_runs_before_process_execution(self):
        from src.backend.services.workflow_service import _process_learning
        src = inspect.getsource(_process_learning)
        guard_pos = src.find("not user_query")
        process_pos = src.find("process_execution")
        assert guard_pos < process_pos, "Guard must be before process_execution call"

    def test_guard_returns_early_when_user_query_empty(self):
        from src.backend.services.workflow_service import _process_learning
        src = inspect.getsource(_process_learning)
        # Find the guard block and confirm it has a return
        lines = src.split('\n')
        in_guard = False
        found_return = False
        for line in lines:
            if 'not user_query' in line or 'user_query.strip()' in line:
                in_guard = True
            if in_guard and 'return' in line:
                found_return = True
                break
        assert found_return, "Guard block must have early return"


# ===================================================================
# 8. Regression Tests (Day 01-07 Imports)
# ===================================================================


class TestRegression:
    """Regression: Day 01-08 core imports still work."""

    def test_day01_execution_memory_imports(self):
        from src.backend.crew_ai.optimization.execution_memory import ExecutionMemory
        assert ExecutionMemory is not None

    def test_day02_failure_analyzer_imports(self):
        from src.backend.crew_ai.optimization.failure_analyzer import FailureAnalyzer
        assert FailureAnalyzer is not None

    def test_day03_structural_rule_and_keyword_correction_imports(self):
        from src.backend.crew_ai.optimization.structural_rule_engine import StructuralRuleEngine
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine
        assert StructuralRuleEngine is not None
        assert KeywordCorrectionEngine is not None

    def test_day04_anti_pattern_engine_imports(self):
        from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
        assert AntiPatternEngine is not None

    def test_day05_feedback_loop_and_metrics_tracker_imports(self):
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        from src.backend.crew_ai.optimization.feedback_loop import LearningMetricsTracker
        assert FeedbackLoop is not None
        assert LearningMetricsTracker is not None

    def test_day06_contradiction_detector_imports(self):
        from src.backend.crew_ai.optimization.feedback_loop import ContradictionDetector
        assert ContradictionDetector is not None

    def test_day07_workflow_service_re_exports(self):
        from src.backend.services.workflow_service import (
            stream_generate_and_run,
            stream_generate_only,
            stream_execute_only,
        )
        assert callable(stream_generate_and_run)
        assert callable(stream_generate_only)
        assert callable(stream_execute_only)

    def test_day08_nl_feedback_engine_and_learning_registry_imports(self):
        from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
        from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
        assert NLFeedbackEngine is not None
        assert callable(get_feedback_loop)


# ===================================================================
# 9. Engine Stats & ABC Tests
# ===================================================================


class TestEngineStats:
    """NLFeedbackEngine stats tracking and ABC method stubs."""

    def test_empty_engine_stats(self):
        engine = NLFeedbackEngine()
        stats = engine.get_stats()
        assert stats["total_processed"] == 0
        assert stats["average_confidence"] == 0.0
        assert stats["seed_pattern_count"] > 0

    def test_engine_stats_after_processing(self):
        engine = NLFeedbackEngine()
        engine.process_feedback("wf1", "It should have checked all rows", "completely_wrong")
        engine.process_feedback("wf2", "", "close_enough")
        stats = engine.get_stats()
        assert stats["total_processed"] == 2
        assert "structural" in stats["category_counts"]
        assert "positive" in stats["category_counts"]
        assert stats["average_confidence"] > 0
        assert stats["last_updated"] is not None

    def test_learn_is_noop(self):
        engine = NLFeedbackEngine()
        engine.learn(None)  # Should not raise

    def test_get_hints_returns_none(self):
        engine = NLFeedbackEngine()
        hints = engine.get_hints("test query", "http://example.com", "tester")
        assert hints is None
