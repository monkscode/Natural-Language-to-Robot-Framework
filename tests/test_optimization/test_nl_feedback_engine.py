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
