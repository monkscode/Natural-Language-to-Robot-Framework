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
import sqlite3

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


# ===================================================================
# 10. UPSERT hint_audit unflag (schema v8 refinement)
# ===================================================================


class TestUpsertHintAuditUnflag:
    """Gap 7 deferred refinement: UPSERT writes hint_audit row iff it clears a flag."""

    def _store_hint(self, conn, conflict_flagged: int) -> int:
        """Insert a hint row directly and return its id."""
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, "
            " original_failure_category, evidence_count, "
            " source_workflow_id, created_at, last_seen, conflict_flagged) "
            "VALUES (?, 'structural', 'domain', 'example.com', NULL, NULL, 1, NULL, "
            "        datetime('now'), datetime('now'), ?)",
            ("Use data-testid for all selectors", conflict_flagged),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_upsert_flagged_hint_writes_audit_row(self, in_memory_db):
        """Re-submitting identical feedback on a flagged hint writes action='unflag'."""
        conn = in_memory_db
        hint_id = self._store_hint(conn, conflict_flagged=1)
        engine = NLFeedbackEngine(conn)

        from unittest.mock import MagicMock
        record = MagicMock()
        record.workflow_id = "wf-resubmit"
        record.domain = "example.com"
        record.url = None
        record.failure_category = None

        engine.learn_from_feedback(record, {
            "feedback_text": "Use data-testid for all selectors",
            "category": "structural",
        })

        audit_rows = conn.execute(
            "SELECT action, actor, reason FROM hint_audit WHERE hint_id = ?",
            (hint_id,),
        ).fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0]["action"] == "unflag"
        assert audit_rows[0]["actor"] == "user1"
        assert "implicit override" in audit_rows[0]["reason"]

    def test_upsert_unflagged_hint_writes_no_audit_row(self, in_memory_db):
        """Re-submitting identical feedback on a non-flagged hint does NOT write hint_audit."""
        conn = in_memory_db
        hint_id = self._store_hint(conn, conflict_flagged=0)
        engine = NLFeedbackEngine(conn)

        from unittest.mock import MagicMock
        record = MagicMock()
        record.workflow_id = "wf-resubmit-clean"
        record.domain = "example.com"
        record.url = None
        record.failure_category = None

        engine.learn_from_feedback(record, {
            "feedback_text": "Use data-testid for all selectors",
            "category": "structural",
        })

        audit_rows = conn.execute(
            "SELECT * FROM hint_audit WHERE hint_id = ?",
            (hint_id,),
        ).fetchall()
        assert len(audit_rows) == 0


# ===================================================================
# 11. get_hints_by_id
# ===================================================================


class TestGetHintsById:
    """get_hints_by_id returns only active, unflagged hints for the given IDs."""

    def _insert_hint(self, conn, text: str, is_active: int, conflict_flagged: int) -> int:
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, "
            " original_failure_category, evidence_count, "
            " source_workflow_id, created_at, last_seen, "
            " is_active, conflict_flagged) "
            "VALUES (?, 'structural', 'global', NULL, NULL, NULL, 1, NULL, "
            "        datetime('now'), datetime('now'), ?, ?)",
            (text, is_active, conflict_flagged),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_none_ids_returns_empty_logs_warning(self, in_memory_db, caplog):
        engine = NLFeedbackEngine(in_memory_db)
        import logging
        with caplog.at_level(logging.WARNING, logger="src.backend.crew_ai.optimization.nl_feedback_engine"):
            result = engine.get_hints_by_id(None)
        assert result == []
        assert any("None" in r.message for r in caplog.records)

    def test_empty_ids_returns_empty_no_query(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        result = engine.get_hints_by_id([])
        assert result == []

    def test_returns_only_active_unflagged(self, in_memory_db):
        conn = in_memory_db
        id_active = self._insert_hint(conn, "use data-testid", is_active=1, conflict_flagged=0)
        id_flagged = self._insert_hint(conn, "use xpath", is_active=1, conflict_flagged=1)
        id_disabled = self._insert_hint(conn, "use css", is_active=0, conflict_flagged=0)

        engine = NLFeedbackEngine(conn)
        result = engine.get_hints_by_id([id_active, id_flagged, id_disabled])

        returned_ids = {r["id"] for r in result}
        assert returned_ids == {id_active}, (
            f"Expected only the active+unflagged hint, got {returned_ids}"
        )

    def test_all_disabled_returns_empty(self, in_memory_db):
        conn = in_memory_db
        id1 = self._insert_hint(conn, "hint a", is_active=0, conflict_flagged=0)
        id2 = self._insert_hint(conn, "hint b", is_active=1, conflict_flagged=1)

        engine = NLFeedbackEngine(conn)
        result = engine.get_hints_by_id([id1, id2])
        assert result == []

    def test_no_db_conn_returns_empty(self):
        engine = NLFeedbackEngine(execution_memory=None)
        result = engine.get_hints_by_id([1, 2, 3])
        assert result == []

    def test_result_shape(self, in_memory_db):
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "use stable state", is_active=1, conflict_flagged=0)
        engine = NLFeedbackEngine(conn)
        result = engine.get_hints_by_id([hint_id])
        assert len(result) == 1
        assert result[0]["id"] == hint_id
        assert result[0]["feedback_text"] == "use stable state"


# ===================================================================
# 12. conflict_flag_hints (new signature — per-hint reasons + audit)
# ===================================================================


class TestConflictFlagHints:
    """conflict_flag_hints writes per-hint flags + hint_audit rows atomically."""

    def _insert_hint(
        self, conn, text: str = "some hint",
        applied: int = 0, success: int = 0,
    ) -> int:
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, "
            " original_failure_category, evidence_count, applied_count, success_count, "
            " source_workflow_id, created_at, last_seen) "
            "VALUES (?, 'structural', 'global', NULL, NULL, NULL, 1, ?, ?, NULL, "
            "        datetime('now'), datetime('now'))",
            (text, applied, success),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_empty_dict_returns_without_db_write(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        engine.conflict_flag_hints({}, trigger_type="trigger_1")
        rows = in_memory_db.execute(
            "SELECT * FROM nl_feedback_corrections WHERE conflict_flagged = 1"
        ).fetchall()
        assert rows == []

    def test_flags_hint_and_writes_audit_row(self, in_memory_db):
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "use data-testid")
        engine = NLFeedbackEngine(conn)

        engine.conflict_flag_hints(
            {hint_id: "v2 replaced data-testid approach"},
            trigger_type="trigger_1",
        )

        row = conn.execute(
            "SELECT conflict_flagged, conflict_flag_reason "
            "FROM nl_feedback_corrections WHERE id = ?",
            (hint_id,),
        ).fetchone()
        assert row["conflict_flagged"] == 1
        assert row["conflict_flag_reason"] == "v2 replaced data-testid approach"

        import json
        audit = conn.execute(
            "SELECT action, actor, reason, before_value, after_value "
            "FROM hint_audit WHERE hint_id = ?",
            (hint_id,),
        ).fetchone()
        assert audit["action"] == "trigger_1_flag"
        assert audit["actor"] == "trigger_1"
        assert audit["reason"] == "v2 replaced data-testid approach"
        assert json.loads(audit["before_value"]) == {"conflict_flagged": 0}
        after = json.loads(audit["after_value"])
        assert after["conflict_flagged"] == 1
        assert after["conflict_flag_reason"] == "v2 replaced data-testid approach"

    def test_per_hint_reasons_stored_individually(self, in_memory_db):
        conn = in_memory_db
        id_a = self._insert_hint(conn, "hint A")
        id_b = self._insert_hint(conn, "hint B")
        engine = NLFeedbackEngine(conn)

        engine.conflict_flag_hints(
            {id_a: "reason for A", id_b: "reason for B"},
            trigger_type="trigger_2",
        )

        row_a = conn.execute(
            "SELECT conflict_flag_reason FROM nl_feedback_corrections WHERE id = ?",
            (id_a,),
        ).fetchone()
        row_b = conn.execute(
            "SELECT conflict_flag_reason FROM nl_feedback_corrections WHERE id = ?",
            (id_b,),
        ).fetchone()
        assert row_a["conflict_flag_reason"] == "reason for A"
        assert row_b["conflict_flag_reason"] == "reason for B"

    def test_no_db_conn_returns_silently(self):
        engine = NLFeedbackEngine(execution_memory=None)
        engine.conflict_flag_hints({99: "some reason"}, trigger_type="trigger_1")

    def test_trigger_type_stored_in_audit_action(self, in_memory_db):
        conn = in_memory_db
        hint_id = self._insert_hint(conn)
        engine = NLFeedbackEngine(conn)

        engine.conflict_flag_hints({hint_id: "test reason"}, trigger_type="trigger_2")

        audit = conn.execute(
            "SELECT action FROM hint_audit WHERE hint_id = ?", (hint_id,)
        ).fetchone()
        assert audit["action"] == "trigger_2_flag"

    def test_conflict_flag_hints_rollback_on_partial_failure(self, in_memory_db):
        """Regression: if a mid-loop UPDATE raises, partial writes must be
        rolled back so they do not piggy-back the next caller's commit on
        the shared connection.

        sqlite3.Connection.execute is C-level read-only, so direct attribute
        monkey-patching raises AttributeError. A FlakyConn wrapper with
        __getattr__ delegation is used instead (same pattern as SpyConn in
        TestUpdateHintEffectivenessWithInjectedIds).
        """
        conn = in_memory_db
        id_a = self._insert_hint(conn, "hint A")
        id_b = self._insert_hint(conn, "hint B")

        update_count = [0]

        class FlakyWriterConn:
            """Wraps _writer_conn; raises on the 2nd UPDATE."""
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("UPDATE"):
                    update_count[0] += 1
                    if update_count[0] == 2:
                        raise sqlite3.OperationalError("simulated mid-loop failure")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        class FlakyEm:
            """execution_memory stub exposing a flaky _writer_conn."""
            def __init__(self, real_compat_conn):
                self._writer_conn = FlakyWriterConn(real_compat_conn._writer_conn)
                self._real = real_compat_conn

            def read_conn(self):
                return self._real.read_conn()

            def __getattr__(self, name):
                return getattr(self._real, name)

        engine = NLFeedbackEngine(FlakyEm(conn))
        engine.conflict_flag_hints(
            {id_a: "reason A", id_b: "reason B"},
            trigger_type="trigger_1",
        )

        # With rollback() in the except block, neither hint should be flagged:
        # the first iteration's UPDATE was rolled back before it could piggyback
        # any future commit on the shared connection.
        rows = conn.execute(
            "SELECT id, conflict_flagged FROM nl_feedback_corrections "
            "WHERE id IN (?, ?)", (id_a, id_b),
        ).fetchall()
        flagged_states = {r["id"]: r["conflict_flagged"] for r in rows}
        assert flagged_states[id_a] == 0, (
            f"Partial UPDATE on hint {id_a} leaked past rollback — "
            "this is the exact bug we are guarding against."
        )
        assert flagged_states[id_b] == 0

    def test_conflict_flag_protected_high_history_hint(self, in_memory_db):
        """Hint with applied=10, success=8 (80%) must NOT be flagged by a single trigger."""
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "proven hint", applied=10, success=8)
        engine = NLFeedbackEngine(conn)

        engine.conflict_flag_hints({hint_id: "contradicts new approach"}, trigger_type="trigger_1")

        row = conn.execute(
            "SELECT conflict_flagged FROM nl_feedback_corrections WHERE id = ?",
            (hint_id,),
        ).fetchone()
        assert row["conflict_flagged"] == 0, (
            "Hint with 80% success rate over 10 applications must be protected "
            "from single-trigger flagging (strong-history guard)"
        )

    def test_conflict_flag_low_history_hint_still_flagged(self, in_memory_db):
        """Hint with applied=3, success=3 (100% but N<5) must still be flagged."""
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "low history hint", applied=3, success=3)
        engine = NLFeedbackEngine(conn)

        engine.conflict_flag_hints({hint_id: "LLM says conflict"}, trigger_type="trigger_1")

        row = conn.execute(
            "SELECT conflict_flagged FROM nl_feedback_corrections WHERE id = ?",
            (hint_id,),
        ).fetchone()
        assert row["conflict_flagged"] == 1, (
            "Hint with applied=3 is below the protection threshold of 5 — "
            "must be flagged even at 100% success rate"
        )

    def test_conflict_flag_records_trigger_event_when_protected(self, in_memory_db):
        """Documents the contract: suppressed flagging leaves conflict_flagged=0 and
        the hint still queryable. The caller's trigger_events write is unaffected
        (caller path unchanged — not tested here)."""
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "high-history hint", applied=10, success=8)
        engine = NLFeedbackEngine(conn)

        engine.conflict_flag_hints({hint_id: "trigger fired"}, trigger_type="trigger_1")

        row = conn.execute(
            "SELECT conflict_flagged FROM nl_feedback_corrections WHERE id = ?",
            (hint_id,),
        ).fetchone()
        assert row["conflict_flagged"] == 0, "Protected hint must remain unflagged"

        result = engine.get_hints_by_id([hint_id])
        assert len(result) == 1, "Protected hint must still be returned by get_hints_by_id"
        assert result[0]["id"] == hint_id


# ===================================================================
# 12b. conflict_flag_hints return-value contract (schema v12)
# ===================================================================
# Schema v12 split trigger_events.flagged_hint_ids (LLM recommendation) from
# actually_flagged_hint_ids (enforcement, post-strong-history-guard).
# conflict_flag_hints is now the source of truth for the enforcement list —
# fire_conflict_detection forwards the return value into the telemetry row.


class TestConflictFlagHintsReturnValue:

    def _insert_hint(
        self, conn, text: str = "some hint",
        applied: int = 0, success: int = 0,
    ) -> int:
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, "
            " original_failure_category, evidence_count, applied_count, success_count, "
            " source_workflow_id, created_at, last_seen) "
            "VALUES (?, 'structural', 'global', NULL, NULL, NULL, 1, ?, ?, NULL, "
            "        datetime('now'), datetime('now'))",
            (text, applied, success),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_returns_actually_flagged_ids_on_success(self, in_memory_db):
        """A non-protected hint that gets flagged must appear in the return list."""
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "flagme")
        engine = NLFeedbackEngine(conn)

        result = engine.conflict_flag_hints(
            {hint_id: "v2 disproves this"}, trigger_type="trigger_1",
        )

        assert result == [hint_id], (
            f"Expected [{hint_id}] returned, got {result!r}"
        )

    def test_returns_empty_list_when_all_suppressed_by_strong_history(self, in_memory_db):
        """Strong-history-protected hints must NOT appear in the return list."""
        conn = in_memory_db
        hint_id = self._insert_hint(conn, "proven hint", applied=10, success=8)
        engine = NLFeedbackEngine(conn)

        result = engine.conflict_flag_hints(
            {hint_id: "contradicts new approach"}, trigger_type="trigger_1",
        )

        assert result == [], (
            f"Protected hint must be excluded from the actually-flagged list; "
            f"got {result!r}"
        )

    def test_returns_only_unprotected_ids_in_mixed_set(self, in_memory_db):
        """A mix of protected + unprotected hints must yield only the unprotected
        ones — matches the trigger_events.actually_flagged_hint_ids semantic."""
        conn = in_memory_db
        protected_id   = self._insert_hint(conn, "old proven", applied=10, success=8)
        unprotected_id = self._insert_hint(conn, "new untested", applied=2, success=1)
        engine = NLFeedbackEngine(conn)

        result = engine.conflict_flag_hints(
            {protected_id: "guard kicks in",
             unprotected_id: "low-history flags through"},
            trigger_type="trigger_1",
        )

        assert result == [unprotected_id], (
            f"Expected only the unprotected hint {unprotected_id} returned; "
            f"got {result!r}"
        )

    def test_returns_empty_list_for_empty_input(self, in_memory_db):
        engine = NLFeedbackEngine(in_memory_db)
        assert engine.conflict_flag_hints({}, trigger_type="trigger_1") == []

    def test_returns_empty_list_on_rollback(self, in_memory_db):
        """Mid-loop failure → rollback → empty list (no false positives).

        Mirrors the existing rollback test but additionally asserts the
        return-value contract: nothing is committed, nothing is returned.
        """
        conn = in_memory_db
        id_a = self._insert_hint(conn, "hint A")
        id_b = self._insert_hint(conn, "hint B")

        update_count = [0]

        class FlakyWriterConn:
            def __init__(self, real):
                self._real = real
            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("UPDATE"):
                    update_count[0] += 1
                    if update_count[0] == 2:
                        raise sqlite3.OperationalError("simulated mid-loop failure")
                return self._real.execute(sql, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self._real, name)

        class FlakyEm:
            def __init__(self, real_compat_conn):
                self._writer_conn = FlakyWriterConn(real_compat_conn._writer_conn)
                self._real = real_compat_conn
            def read_conn(self):
                return self._real.read_conn()
            def __getattr__(self, name):
                return getattr(self._real, name)

        engine = NLFeedbackEngine(FlakyEm(conn))
        result = engine.conflict_flag_hints(
            {id_a: "reason A", id_b: "reason B"},
            trigger_type="trigger_1",
        )

        assert result == [], (
            f"On rollback, the actually-flagged list must be empty (DB is "
            f"unchanged); got {result!r}"
        )


# ===================================================================
# 13. update_hint_effectiveness -- injected_hint_ids semantic branches
# ===================================================================


class TestUpdateHintEffectivenessWithInjectedIds:
    """Three semantic branches of injected_hint_ids credit accounting.

    Verifies the Step 2a contract:
      - '[]'    -> known-empty, no counter writes
      - None    -> legacy row, fall back to scope-wide credit
      - '[1,3]' -> filtered, credit only those IDs (and only if still active+unflagged)
    """

    def _insert_hint(self, conn, text: str, *, scope: str = "global",
                     domain: str | None = None,
                     is_active: int = 1, conflict_flagged: int = 0,
                     applied: int = 0, success: int = 0, failure: int = 0) -> int:
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, "
            " original_failure_category, evidence_count, applied_count, "
            " success_count, failure_count, source_workflow_id, created_at, "
            " last_seen, is_active, conflict_flagged) "
            "VALUES (?, 'structural', ?, ?, NULL, NULL, 1, ?, ?, ?, NULL, "
            "        datetime('now'), datetime('now'), ?, ?)",
            (text, scope, domain, applied, success, failure, is_active, conflict_flagged),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _counters(self, conn, hint_id: int) -> dict:
        row = conn.execute(
            "SELECT applied_count, success_count, failure_count "
            "FROM nl_feedback_corrections WHERE id = ?",
            (hint_id,),
        ).fetchone()
        return dict(row)

    # ---------- Branch 1: '[]' known-empty -> no-op ----------

    def test_known_empty_skips_all_counters(self, in_memory_db):
        conn = in_memory_db
        id_a = self._insert_hint(conn, "hint A", domain="example.com")
        id_b = self._insert_hint(conn, "hint B", domain="example.com")

        # Wrap conn in a spy so we can verify the early-return path is taken,
        # not the downstream SELECT-with-empty-IN-list path. SQLite tolerates
        # "IN ()" by returning zero rows, so the contract (no counter writes)
        # is the same on both paths -- but the spec calls out the early return
        # as the intended branch, and a regression that removes it would cost
        # a wasted DB round-trip per known-empty workflow.
        # (sqlite3.Connection.execute is read-only at the C level, so a
        # straight attribute monkey-patch raises AttributeError -- hence the
        # wrapper.)
        sql_seen: list[str] = []

        class SpyConn:
            def __init__(self, real):
                self._real = real
            def execute(self, sql, *args, **kwargs):
                sql_seen.append(sql)
                return self._real.execute(sql, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self._real, name)

        spy = SpyConn(conn)
        engine = NLFeedbackEngine(spy)

        engine.update_hint_effectiveness(
            domain="example.com", url=None, test_passed=True,
            injected_hint_ids="[]",
        )

        # No SELECT/UPDATE against nl_feedback_corrections during the call --
        # the early return short-circuited before any query.
        offending = [s for s in sql_seen if "nl_feedback_corrections" in s]
        assert offending == [], (
            "Expected early return for injected_hint_ids='[]'; got SQL: "
            f"{offending}"
        )

        assert self._counters(conn, id_a) == {"applied_count": 0, "success_count": 0, "failure_count": 0}
        assert self._counters(conn, id_b) == {"applied_count": 0, "success_count": 0, "failure_count": 0}

    # ---------- Branch 2: None legacy fallback -> scope-wide credit ----------

    def test_none_falls_back_to_scope_wide_credit(self, in_memory_db):
        conn = in_memory_db
        id_dom = self._insert_hint(conn, "domain hint", scope="domain", domain="example.com")
        id_other = self._insert_hint(conn, "other domain hint", scope="domain", domain="other.com")
        id_global = self._insert_hint(conn, "global hint", scope="global")
        engine = NLFeedbackEngine(conn)

        engine.update_hint_effectiveness(
            domain="example.com", url=None, test_passed=True,
            injected_hint_ids=None,
        )

        # In-scope hints credited (domain match + global) -- same as pre-v10 behaviour.
        assert self._counters(conn, id_dom)["applied_count"] == 1
        assert self._counters(conn, id_dom)["success_count"] == 1
        assert self._counters(conn, id_global)["applied_count"] == 1
        assert self._counters(conn, id_global)["success_count"] == 1
        # Out-of-scope hint untouched.
        assert self._counters(conn, id_other) == {"applied_count": 0, "success_count": 0, "failure_count": 0}

    # ---------- Branch 3: '[id1,id2]' -> filtered credit ----------

    def test_filtered_credits_only_listed_ids(self, in_memory_db):
        conn = in_memory_db
        id_a = self._insert_hint(conn, "A", scope="domain", domain="example.com")
        id_b = self._insert_hint(conn, "B", scope="domain", domain="example.com")
        id_c = self._insert_hint(conn, "C", scope="domain", domain="example.com")
        engine = NLFeedbackEngine(conn)

        engine.update_hint_effectiveness(
            domain="example.com", url=None, test_passed=True,
            injected_hint_ids=f"[{id_a}, {id_c}]",
        )

        assert self._counters(conn, id_a)["success_count"] == 1
        assert self._counters(conn, id_b) == {"applied_count": 0, "success_count": 0, "failure_count": 0}
        assert self._counters(conn, id_c)["success_count"] == 1

    def test_filtered_excludes_disabled_and_flagged_ids(self, in_memory_db):
        """A hint injected at gen time but later disabled / flagged must NOT be
        credited by a downstream update_hint_effectiveness call (counters would
        re-activate stale state). is_active=1 AND conflict_flagged=0 is the gate."""
        conn = in_memory_db
        id_active = self._insert_hint(conn, "active", domain="example.com")
        id_disabled = self._insert_hint(conn, "disabled", domain="example.com", is_active=0)
        id_flagged = self._insert_hint(conn, "flagged", domain="example.com", conflict_flagged=1)
        engine = NLFeedbackEngine(conn)

        engine.update_hint_effectiveness(
            domain="example.com", url=None, test_passed=True,
            injected_hint_ids=f"[{id_active}, {id_disabled}, {id_flagged}]",
        )

        assert self._counters(conn, id_active)["success_count"] == 1
        assert self._counters(conn, id_disabled)["success_count"] == 0
        assert self._counters(conn, id_flagged)["success_count"] == 0

    # ---------- Cross-cutting: failure attribution honoured ----------

    def test_filtered_with_nonexistent_ids_is_noop(self, in_memory_db):
        """Hints can be deleted between injection (gen time) and the
        downstream counter update. IDs that no longer exist in the DB must
        simply not be credited -- not raise, not partially commit, not fall
        back to the legacy scope-wide path."""
        conn = in_memory_db
        id_real = self._insert_hint(conn, "real hint", domain="example.com")
        engine = NLFeedbackEngine(conn)

        # All three IDs in the payload are ints, but 999/1000 do not exist
        # in the DB. id_real exists; it should still be credited.
        engine.update_hint_effectiveness(
            domain="example.com", url=None, test_passed=True,
            injected_hint_ids=f"[999, {id_real}, 1000]",
        )

        assert self._counters(conn, id_real)["success_count"] == 1
        # Sanity: no leaked rows; just the one we created.
        leaked = conn.execute(
            "SELECT id FROM nl_feedback_corrections WHERE id IN (999, 1000)"
        ).fetchall()
        assert leaked == []

    def test_filtered_failure_increments_only_when_category_matches(self, in_memory_db):
        """Smart attribution (P5A widening) still applies under the filtered
        path: a failure increments failure_count only when new_failure_category
        is in RELATED_CATEGORIES.get(orig_cat, {orig_cat})."""
        conn = in_memory_db
        # Hint with original category B1 (keyword-only related set)
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, domain, url, "
            " original_failure_category, evidence_count, applied_count, "
            " success_count, failure_count, source_workflow_id, created_at, "
            " last_seen, is_active, conflict_flagged) "
            "VALUES ('keyword hint', 'keyword', 'global', NULL, NULL, 'B1', "
            "        1, 0, 0, 0, NULL, datetime('now'), datetime('now'), 1, 0)",
        )
        conn.commit()
        hid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        engine = NLFeedbackEngine(conn)

        # Failure with unrelated category C1 -> no failure_count increment.
        engine.update_hint_effectiveness(
            domain=None, url=None, test_passed=False,
            new_failure_category="C1",
            injected_hint_ids=f"[{hid}]",
        )
        assert self._counters(conn, hid)["failure_count"] == 0
        assert self._counters(conn, hid)["applied_count"] == 1

    # ---------- Malformed JSON -> scope-wide fallback ----------

    def test_malformed_json_falls_back_to_scope_wide(self, in_memory_db, caplog):
        """If injected_hint_ids is syntactically invalid JSON, the engine must
        fall back to the legacy scope-wide credit path (same as None) and log a
        WARNING. Counters for in-scope hints must still be updated."""
        import logging

        conn = in_memory_db
        id_in_scope = self._insert_hint(conn, "in-scope hint", scope="domain", domain="example.com")
        id_out_of_scope = self._insert_hint(conn, "other domain", scope="domain", domain="other.com")
        engine = NLFeedbackEngine(conn)

        with caplog.at_level(logging.WARNING, logger="src.backend.crew_ai.optimization.nl_feedback_engine"):
            engine.update_hint_effectiveness(
                domain="example.com", url=None, test_passed=True,
                injected_hint_ids="[1, 2,",  # truncated — malformed JSON
            )

        # In-scope hint credited via the legacy scope-wide path.
        assert self._counters(conn, id_in_scope)["applied_count"] == 1
        assert self._counters(conn, id_in_scope)["success_count"] == 1
        # Out-of-scope hint untouched.
        assert self._counters(conn, id_out_of_scope) == {
            "applied_count": 0, "success_count": 0, "failure_count": 0
        }
        # A WARNING mentioning malformed injected_hint_ids must have been emitted.
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("malformed injected_hint_ids" in str(m) for m in warning_msgs), (
            f"Expected WARNING about malformed injected_hint_ids; got: {warning_msgs}"
        )

    # ---------- Rollback on mid-loop failure ----------

    def test_update_hint_effectiveness_rollback_on_mid_loop_failure(
        self, in_memory_db, caplog
    ):
        """Regression: if a mid-loop UPDATE raises, all partial counter writes
        must be rolled back so they cannot piggy-back any future commit on the
        shared _writer_conn.

        FlakyWriterConn raises on the 2nd UPDATE, after hint A's counter UPDATE
        has been issued but before hint B's. With rollback() in the except block,
        both A and B counters must remain at 0. The WARNING log must include
        the processed/total hint counts and the context (domain, url, passed)
        so failures can be analysed from log files.
        """
        import logging

        conn = in_memory_db
        id_a = self._insert_hint(conn, "hint A")
        id_b = self._insert_hint(conn, "hint B")
        id_c = self._insert_hint(conn, "hint C")

        update_count = [0]

        class FlakyWriterConn:
            """Wraps _writer_conn; raises on the 2nd UPDATE."""
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("UPDATE"):
                    update_count[0] += 1
                    if update_count[0] == 2:
                        raise sqlite3.OperationalError("simulated mid-loop failure")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        class FlakyEm:
            """execution_memory stub exposing a flaky _writer_conn."""
            def __init__(self, real_compat_conn):
                self._writer_conn = FlakyWriterConn(real_compat_conn._writer_conn)
                self._real = real_compat_conn

            def read_conn(self):
                return self._real.read_conn()

            def __getattr__(self, name):
                return getattr(self._real, name)

        engine = NLFeedbackEngine(FlakyEm(conn))

        with caplog.at_level(
            logging.WARNING,
            logger="src.backend.crew_ai.optimization.nl_feedback_engine",
        ):
            engine.update_hint_effectiveness(
                domain="example.com", url="https://example.com/page",
                test_passed=True,
                injected_hint_ids=f"[{id_a}, {id_b}, {id_c}]",
            )

        # All counters must be 0 — the partial update for hint A was rolled back.
        for hint_id in (id_a, id_b, id_c):
            assert self._counters(conn, hint_id) == {
                "applied_count": 0, "success_count": 0, "failure_count": 0
            }, (
                f"Partial counter update for hint {hint_id} leaked past rollback — "
                "this is the exact bug Finding 3 guards against."
            )

        # The WARNING must carry enough context to analyse failures from logs.
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        rollback_warnings = [m for m in warning_msgs if "update_hint_effectiveness failed" in str(m)]
        assert rollback_warnings, f"Expected WARNING about failed effectiveness update; got: {warning_msgs}"
        msg = str(rollback_warnings[0])
        assert "1/3" in msg, f"Expected '1/3 hint(s)' in log (hint A was processed before failure); got: {msg}"
        assert "example.com" in msg, f"Expected domain in log for traceability; got: {msg}"
        assert "passed=True" in msg, f"Expected test_passed in log for traceability; got: {msg}"

    def test_update_hint_effectiveness_does_not_pollute_next_writer(
        self, in_memory_db
    ):
        """After a mid-loop failure + rollback, the next write task on the same
        connection must NOT accidentally commit hint A's partial counter update.

        This is Outcome B from the Finding 3 description: without rollback(),
        the next task's commit() would flush the zombie partial state. With
        rollback(), the connection is clean and only the next task's own rows
        are committed.
        """
        conn = in_memory_db
        id_a = self._insert_hint(conn, "hint A")
        id_b = self._insert_hint(conn, "hint B")

        update_count = [0]

        class FlakyWriterConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("UPDATE"):
                    update_count[0] += 1
                    if update_count[0] == 2:
                        raise sqlite3.OperationalError("simulated mid-loop failure")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        class FlakyEm:
            def __init__(self, real_compat_conn):
                self._writer_conn = FlakyWriterConn(real_compat_conn._writer_conn)
                self._real = real_compat_conn

            def read_conn(self):
                return self._real.read_conn()

            def __getattr__(self, name):
                return getattr(self._real, name)

        engine = NLFeedbackEngine(FlakyEm(conn))
        engine.update_hint_effectiveness(
            domain=None, url=None, test_passed=True,
            injected_hint_ids=f"[{id_a}, {id_b}]",
        )

        # Simulate the next writer task (e.g. write_trigger_event) running
        # a plain execute() + commit() on the same underlying connection.
        real_conn = conn._writer_conn
        real_conn.execute(
            "INSERT INTO trigger_events "
            "(trigger_type, status, created_at) "
            "VALUES ('trigger_1', 'completed', datetime('now'))"
        )
        real_conn.commit()

        # The next task's row must be committed cleanly.
        te_row = real_conn.execute(
            "SELECT id FROM trigger_events WHERE trigger_type = 'trigger_1'"
        ).fetchone()
        assert te_row is not None, "Next writer task's trigger_events row must commit"

        # hint A's counter must NOT have been committed by the next task's commit —
        # the partial UPDATE was rolled back, not left open to piggy-back.
        assert self._counters(conn, id_a) == {
            "applied_count": 0, "success_count": 0, "failure_count": 0
        }, (
            "hint A's partial counter update must not persist after rollback — "
            "the next task's commit() must not flush it (Outcome B bug)."
        )
        assert self._counters(conn, id_b) == {
            "applied_count": 0, "success_count": 0, "failure_count": 0
        }


# ===================================================================
# 13. C1 regression — admin triage failure logs a warning
# ===================================================================


class TestAdminTriageWarning:
    """Regression: create_hint logs WARNING when run_triage=True and process_feedback raises."""

    def test_triage_failure_logs_warning(self, caplog):
        import logging
        from unittest.mock import MagicMock, patch

        from src.backend.api.learning_endpoints import HintCreateRequest, create_hint

        request = HintCreateRequest(
            feedback_text="Click the submit button",
            anchor_query="submit the form on the page",
            scope="global",
            run_triage=True,
            actor="test-admin",
        )
        fb = MagicMock()
        fb.nl_engine.process_feedback.side_effect = RuntimeError("regex crash")

        # _admin_conn is called after the triage block; raising here keeps the
        # test isolated (no real DB) while still exercising the warning path.
        with patch("src.backend.api.learning_endpoints._admin_conn",
                   side_effect=RuntimeError("db-not-needed")), \
             caplog.at_level(logging.WARNING, logger="src.backend.api.learning_endpoints"):
            with pytest.raises(RuntimeError, match="db-not-needed"):
                create_hint(request, fb=fb)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Admin triage failed" in m for m in warning_msgs), (
            f"Expected WARNING about triage failure; got: {warning_msgs}"
        )
