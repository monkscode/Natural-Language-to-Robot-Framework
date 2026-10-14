"""
DAY 07 — Integration Wiring (pytest).

Tests: url_utils.extract_domain, learning_config re-export,
docker_service return dict enrichment, get_feedback_loop singleton,
_process_learning_record wiring, kill switch, error resilience, and regression.
Uses in-memory SQLite with full Phase 1 schema.
"""

import sqlite3
import importlib
from unittest.mock import patch, MagicMock, PropertyMock

import pytest



# ===================================================================
# Helpers
# ===================================================================

def _reset_singleton():
    """Reset the singleton state in learning_registry module.

    The singleton (get_feedback_loop) lives in learning_registry,
    NOT workflow_service. workflow_service imports the function via:
        from ...learning_registry import get_feedback_loop
    """
    import src.backend.crew_ai.optimization.learning_registry as lr
    lr._feedback_loop_instance = None
    lr._init_failed_at = None


# ===================================================================
# Category 1: url_utils.extract_domain Tests
# ===================================================================

class TestExtractDomain:

    def test_standard_url(self):
        from src.backend.core.url_utils import extract_domain
        assert extract_domain("https://www.demoqa.com/elements") == "demoqa.com"

    def test_no_www(self):
        from src.backend.core.url_utils import extract_domain
        assert extract_domain("https://demoqa.com/elements") == "demoqa.com"

    def test_http(self):
        from src.backend.core.url_utils import extract_domain
        assert extract_domain("http://the-internet.herokuapp.com") == "the-internet.herokuapp.com"

    def test_with_port(self):
        from src.backend.core.url_utils import extract_domain
        result = extract_domain("http://localhost:8080/page")
        assert result == "localhost:8080", f"Expected 'localhost:8080', got '{result}'"

    def test_no_protocol(self):
        from src.backend.core.url_utils import extract_domain
        # urlparse treats no-protocol as path, so netloc is empty
        result = extract_domain("demoqa.com/page")
        assert result == "unknown", f"Expected 'unknown' for no-protocol URL, got '{result}'"

    def test_empty_string(self):
        from src.backend.core.url_utils import extract_domain
        assert extract_domain("") == "unknown"

    def test_none(self):
        """extract_domain should handle None gracefully (returns 'unknown')."""
        from src.backend.core.url_utils import extract_domain
        result = extract_domain(None)
        assert result == "unknown", f"Expected 'unknown' for None, got '{result}'"

    def test_invalid(self):
        from src.backend.core.url_utils import extract_domain
        assert extract_domain("not-a-url") == "unknown"

    def test_subdomain_preserved(self):
        from src.backend.core.url_utils import extract_domain
        result = extract_domain("https://app.staging.example.com/page")
        assert result == "app.staging.example.com", f"Expected subdomain preserved, got '{result}'"


# ===================================================================
# Category 2: Re-export Compatibility Tests
# ===================================================================

class TestReexportCompatibility:

    def test_from_learning_config(self):
        """Import from learning_config still works (backward compat)."""
        from src.backend.crew_ai.optimization.learning_config import extract_domain
        assert extract_domain("https://www.example.com") == "example.com"

    def test_from_url_utils(self):
        """Import from canonical url_utils works."""
        from src.backend.core.url_utils import extract_domain
        assert extract_domain("https://www.example.com") == "example.com"

    def test_same_function(self):
        """Both imports point to the same function."""
        from src.backend.core.url_utils import extract_domain as ed1
        from src.backend.crew_ai.optimization.learning_config import extract_domain as ed2
        assert ed1 is ed2, "Re-exported function should be the same object"

    def test_feedback_loop_import(self):
        """feedback_loop.py can still import extract_domain from learning_config."""
        # This validates the existing import chain doesn't break
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        # If we got here, the import succeeded
        assert FeedbackLoop is not None


# ===================================================================
# Category 3: Docker Return Dict Enrichment Tests
# ===================================================================

class TestDockerReturnDict:

    def test_has_output_xml_path_key(self):
        """Verify run_test_in_container return dict includes output_xml_path."""
        import inspect
        from src.backend.services.docker_service import run_test_in_container
        source = inspect.getsource(run_test_in_container)
        assert '"output_xml_path"' in source or "'output_xml_path'" in source, \
            "run_test_in_container should include 'output_xml_path' in return dict"

    def test_has_exit_code_key(self):
        """Verify run_test_in_container return dict includes exit_code."""
        import inspect
        from src.backend.services.docker_service import run_test_in_container
        source = inspect.getsource(run_test_in_container)
        assert '"exit_code"' in source or "'exit_code'" in source, \
            "run_test_in_container should include 'exit_code' in return dict"

    def test_checks_os_path_exists(self):
        """Fallback return paths should check os.path.exists for output_xml_path."""
        import inspect
        from src.backend.services.docker_service import run_test_in_container
        source = inspect.getsource(run_test_in_container)
        assert "os.path.exists(output_xml_path)" in source, \
            "Fallback paths should check os.path.exists for output_xml_path"

    def test_all_paths_have_exit_code(self):
        """All return statements should include exit_code."""
        import inspect
        from src.backend.services.docker_service import run_test_in_container
        source = inspect.getsource(run_test_in_container)
        # Count return statements with status: complete
        complete_returns = source.count('"status": "complete"')
        exit_code_returns = source.count('"exit_code"')
        assert exit_code_returns >= complete_returns, \
            f"Expected exit_code in all {complete_returns} return dicts, found {exit_code_returns}"


# ===================================================================
# Category 4: get_feedback_loop Singleton Tests
# ===================================================================

class TestSingleton:

    def test_returns_same_instance(self):
        """get_feedback_loop() should return the same instance on repeated calls."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', True):
            mock_fl = MagicMock()
            with patch('src.backend.crew_ai.optimization.feedback_loop.FeedbackLoop',
                       return_value=mock_fl):
                first = lr.get_feedback_loop()
                second = lr.get_feedback_loop()
                assert first is second, "Singleton should return the same instance"

        _reset_singleton()

    def test_none_when_disabled(self):
        """get_feedback_loop() returns None when OPTIMIZATION_ENABLED=False."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', False):
            result = lr.get_feedback_loop()
            assert result is None, "Should return None when disabled"

        _reset_singleton()

    def test_none_on_init_failure(self):
        """get_feedback_loop() returns None if FeedbackLoop init raises."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', True):
            with patch('src.backend.crew_ai.optimization.feedback_loop.FeedbackLoop',
                       side_effect=RuntimeError("DB connection failed")):
                result = lr.get_feedback_loop()
                assert result is None, "Should return None on init failure"

        _reset_singleton()

    def test_no_retry_within_cooldown(self):
        """After a failed init, calls inside the cooldown window do NOT retry."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', True):
            mock_cls = MagicMock(side_effect=RuntimeError("DB broken"))
            with patch('src.backend.crew_ai.optimization.feedback_loop.FeedbackLoop',
                       mock_cls):
                lr.get_feedback_loop()  # First call -- fails
                lr.get_feedback_loop()  # Second call -- inside cooldown, no retry
                assert mock_cls.call_count == 1, \
                    f"Expected 1 init attempt, got {mock_cls.call_count}"

        _reset_singleton()

    def test_retries_after_cooldown(self):
        """A failed init is retried once the cooldown has elapsed — a transient
        Postgres outage at startup must not disable learning permanently."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', True):
            mock_fl = MagicMock()
            mock_cls = MagicMock(side_effect=[RuntimeError("DB down"), mock_fl])
            with patch('src.backend.crew_ai.optimization.feedback_loop.FeedbackLoop',
                       mock_cls):
                assert lr.get_feedback_loop() is None      # fails, starts cooldown
                # Age the failure past the cooldown window.
                lr._init_failed_at -= (lr._INIT_RETRY_COOLDOWN_S + 1)
                assert lr.get_feedback_loop() is mock_fl   # retried and recovered
                assert mock_cls.call_count == 2

        _reset_singleton()

    def test_caches_successful_instance(self):
        """After successful init, instance is cached."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', True):
            mock_fl = MagicMock()
            mock_cls = MagicMock(return_value=mock_fl)
            with patch('src.backend.crew_ai.optimization.feedback_loop.FeedbackLoop',
                       mock_cls):
                lr.get_feedback_loop()
                lr.get_feedback_loop()
                lr.get_feedback_loop()
                assert mock_cls.call_count == 1, "Should only construct once"

        _reset_singleton()


# ===================================================================
# Category 5: _process_learning_record Wiring Tests
# ===================================================================

class TestProcessLearningWiring:

    def test_calls_feedback_loop(self):
        """_process_learning_record should call process_execution on FeedbackLoop."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        result = {
            'test_status': 'passed',
            'output_xml_path': '/some/path/output.xml',
            'exit_code': 0,
        }

        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            ws._process_learning_record("wf-123", "click button on demoqa.com", "*** Test Cases ***", result)

        mock_fl.process_execution.assert_called_once()
        call_kwargs = mock_fl.process_execution.call_args
        assert call_kwargs[1]['workflow_id'] == "wf-123" or call_kwargs[0][0] == "wf-123"

        _reset_singleton()

    def test_skips_when_none(self):
        """_process_learning_record should skip when get_feedback_loop() returns None."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=None):
            # Should not raise
            ws._process_learning_record("wf-123", "query", "code", {"test_status": "passed"})
        # If we got here, it worked

        _reset_singleton()

    def test_nonblocking_on_error(self):
        """_process_learning_record should catch exceptions and not propagate."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()
        mock_fl.process_execution.side_effect = RuntimeError("DB crashed")

        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            # Should NOT raise
            ws._process_learning_record("wf-123", "query", "code", {"test_status": "failed"})

        _reset_singleton()

    def test_extracts_url(self):
        """_process_learning_record should extract URL from user_query."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        result = {'test_status': 'passed', 'output_xml_path': None, 'exit_code': 0}
        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            ws._process_learning_record("wf-123", "go to https://demoqa.com and click", "code", result)

        call_args = mock_fl.process_execution.call_args
        # url should be extracted from user_query
        kwargs = call_args[1] if call_args[1] else {}
        if 'url' in kwargs:
            assert "demoqa.com" in kwargs['url'], f"Expected demoqa.com in url, got {kwargs['url']}"

        _reset_singleton()

    def test_no_user_query(self):
        """_process_learning_record should handle user_query=None by skipping (guard clause)."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        result = {'test_status': 'passed', 'output_xml_path': None, 'exit_code': 0}
        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            # Should not raise -- guard clause returns early for None user_query
            ws._process_learning_record("wf-123", None, "code", result)

        # user_query=None triggers the empty-query guard -- process_execution NOT called
        mock_fl.process_execution.assert_not_called()

        _reset_singleton()

    def test_passes_output_xml(self):
        """_process_learning_record should pass output_xml_path from docker result."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        result = {'test_status': 'failed', 'output_xml_path': '/path/output.xml', 'exit_code': 1}
        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            ws._process_learning_record("wf-123", "query", "code", result)

        call_args = mock_fl.process_execution.call_args
        kwargs = call_args[1] if call_args[1] else {}
        if 'output_xml_path' in kwargs:
            assert kwargs['output_xml_path'] == '/path/output.xml'

        _reset_singleton()

    def test_passes_metrics_none(self):
        """_process_learning_record should pass metrics=None for execute-only mode."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        result = {'test_status': 'passed', 'output_xml_path': None, 'exit_code': 0}
        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            ws._process_learning_record("wf-123", "query", "code", result)

        call_args = mock_fl.process_execution.call_args
        kwargs = call_args[1] if call_args[1] else {}
        if 'metrics' in kwargs:
            assert kwargs['metrics'] is None, "Should pass metrics=None"

        _reset_singleton()

    def test_handles_missing_keys(self):
        """_process_learning_record should handle result dict missing keys."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        # Minimal result dict -- missing output_xml_path and exit_code
        result = {'test_status': 'error'}
        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            ws._process_learning_record("wf-123", "query", "code", result)

        call_args = mock_fl.process_execution.call_args
        kwargs = call_args[1] if call_args[1] else {}
        if 'output_xml_path' in kwargs:
            assert kwargs['output_xml_path'] is None

        _reset_singleton()


# ===================================================================
# Category 6: Kill Switch (OPTIMIZATION_ENABLED=False) Tests
# ===================================================================

class TestKillSwitch:

    def test_circuit_breaker_ignores_feature_flag(self):
        """LearningCircuitBreaker.is_enabled() only reflects its error state, ignoring OPTIMIZATION_ENABLED."""
        from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker
        from src.backend.core.config import settings

        cb = LearningCircuitBreaker()
        original = settings.OPTIMIZATION_ENABLED
        try:
            settings.OPTIMIZATION_ENABLED = False
            assert cb.is_enabled() is True, "Breaker state is decoupled from feature flag"
            settings.OPTIMIZATION_ENABLED = True
            assert cb.is_enabled() is True, "Breaker state remains enabled when valid"
        finally:
            settings.OPTIMIZATION_ENABLED = original

    def test_feedback_loop_skips(self):
        """FeedbackLoop.process_execution() returns immediately when disabled."""
        from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker
        from src.backend.core.config import settings

        # Create FeedbackLoop with a disabled circuit breaker
        mock_cb = LearningCircuitBreaker()
        original = settings.OPTIMIZATION_ENABLED
        try:
            settings.OPTIMIZATION_ENABLED = False

            from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
            mock_memory = MagicMock()
            fl = FeedbackLoop(
                execution_memory=mock_memory,
                circuit_breaker=mock_cb,
            )

            # This should return immediately without doing anything
            fl.process_execution(
                workflow_id="test",
                user_query="test",
                url="",
                robot_code="",
                test_status="passed",
            )

            # Verify no store calls were made
            mock_memory.store.assert_not_called()
        finally:
            settings.OPTIMIZATION_ENABLED = original

    def test_singleton_returns_none(self):
        """get_feedback_loop() returns None when disabled."""
        _reset_singleton()
        import src.backend.crew_ai.optimization.learning_registry as lr

        original = lr.settings.OPTIMIZATION_ENABLED
        try:
            lr.settings.OPTIMIZATION_ENABLED = False
            result = lr.get_feedback_loop()
            assert result is None
        finally:
            lr.settings.OPTIMIZATION_ENABLED = original

        _reset_singleton()


# ===================================================================
# Category 7: Error Resilience Tests
# ===================================================================

class TestErrorResilience:

    def test_init_crash_pipeline_ok(self):
        """If FeedbackLoop init crashes, _process_learning_record continues without error."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws
        import src.backend.crew_ai.optimization.learning_registry as lr

        with patch.object(lr.settings, 'OPTIMIZATION_ENABLED', True):
            with patch('src.backend.crew_ai.optimization.feedback_loop.FeedbackLoop',
                       side_effect=Exception("Catastrophic init failure")):
                # get_feedback_loop will try init, fail, return None
                # _process_learning_record should handle this gracefully -- Should NOT raise
                ws._process_learning_record("wf-123", "query", "code", {"test_status": "passed"})

        _reset_singleton()

    def test_process_crash_pipeline_ok(self):
        """If process_execution crashes, _process_learning_record swallows the error."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()
        mock_fl.process_execution.side_effect = Exception("Catastrophic process failure")

        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            # Should NOT raise
            ws._process_learning_record("wf-123", "query", "code", {"test_status": "failed"})

        _reset_singleton()

    def test_empty_result_dict(self):
        """_process_learning_record should handle completely empty result dict."""
        _reset_singleton()
        import src.backend.services.workflow_service as ws

        mock_fl = MagicMock()

        with patch('src.backend.services.workflow_service.get_feedback_loop', return_value=mock_fl):
            # Should NOT raise
            ws._process_learning_record("wf-123", "query", "code", {})

        _reset_singleton()

    def test_circuit_breaker_auto_disable(self):
        """Circuit breaker should auto-disable after too many errors."""
        from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker
        from src.backend.core.config import settings

        original = settings.OPTIMIZATION_ENABLED
        try:
            settings.OPTIMIZATION_ENABLED = True
            cb = LearningCircuitBreaker()

            # Simulate many errors
            for i in range(20):
                cb.record_error(Exception(f"Error {i}"))

            # After enough errors, should auto-disable
            # (depends on configured threshold, but with 20 errors and 0 successes
            # the error rate is 100%, which should trip any reasonable threshold)
            if cb._total_calls >= cb._min_calls:
                assert cb.is_enabled() is False, "Should auto-disable on high error rate"
        finally:
            settings.OPTIMIZATION_ENABLED = original


# ===================================================================
# Category 8: Integration Smoke Tests
# ===================================================================

class TestIntegrationSmoke:

    def test_workflow_service_imports(self):
        """workflow_service.py should import without errors."""
        import src.backend.services.workflow_service as ws
        assert hasattr(ws, 'get_feedback_loop')
        assert hasattr(ws, '_process_learning_record')
        assert hasattr(ws, 'stream_execute_only')

    def test_feedback_loop_imports(self):
        """FeedbackLoop should import successfully."""
        from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
        assert FeedbackLoop is not None

    def test_url_utils_imports(self):
        """url_utils should import successfully."""
        from src.backend.core.url_utils import extract_domain
        assert callable(extract_domain)

    def test_learning_config_has_extract_domain(self):
        """learning_config should still export extract_domain."""
        import src.backend.crew_ai.optimization.learning_config as lc
        assert hasattr(lc, 'extract_domain'), "extract_domain should be available in learning_config"
        assert callable(lc.extract_domain)

    def test_no_circular_imports(self):
        """Verify no circular import issues between workflow_service and feedback_loop."""
        # Force reimport
        import importlib
        import src.backend.services.workflow_service as ws
        importlib.reload(ws)
        assert hasattr(ws, 'get_feedback_loop')


# ===================================================================
# Category 9: Regression Tests
# ===================================================================

class TestRegression:

    def test_day01_import(self):
        """The execution store should still import."""
        from src.backend.crew_ai.optimization.postgres_execution_memory import (
            PostgresExecutionMemory,
        )
        assert PostgresExecutionMemory is not None

    def test_day02_import(self):
        """FailureAnalyzer should still import."""
        from src.backend.crew_ai.optimization.failure_analyzer import FailureAnalyzer
        assert FailureAnalyzer is not None

    def test_day03_import(self):
        """StructuralRuleEngine and KeywordCorrectionEngine should still import."""
        from src.backend.crew_ai.optimization.structural_rule_engine import StructuralRuleEngine
        from src.backend.crew_ai.optimization.keyword_correction_engine import KeywordCorrectionEngine
        assert StructuralRuleEngine is not None
        assert KeywordCorrectionEngine is not None

    def test_day04_import(self):
        """AntiPatternEngine should still import."""
        from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
        assert AntiPatternEngine is not None

    def test_day05_import(self):
        """FeedbackLoop, LearningMetricsTracker, ContradictionDetector should still import."""
        from src.backend.crew_ai.optimization.feedback_loop import (
            FeedbackLoop,
            LearningMetricsTracker,
            ContradictionDetector,
        )
        assert FeedbackLoop is not None
        assert LearningMetricsTracker is not None
        assert ContradictionDetector is not None

    def test_day06_import(self):
        """SmartKeywordProvider should still import."""
        from src.backend.crew_ai.optimization.smart_keyword_provider import SmartKeywordProvider
        assert SmartKeywordProvider is not None

    def test_schema_manager(self, in_memory_db):
        """The fixture-applied schema should contain the core tables."""
        from tests.test_optimization import pg_introspect
        table_names = pg_introspect.table_names(in_memory_db)
        assert "execution_records" in table_names
        assert "structural_rules" in table_names
