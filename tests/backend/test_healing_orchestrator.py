"""
Integration tests for the Healing Orchestrator service.

These tests verify the complete healing workflow coordination,
session state management, progress tracking, and report generation.
"""

import asyncio
import json
import pytest
import pytest_asyncio
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from src.backend.core.models import (
    FailureContext, HealingSession, HealingStatus, FailureType,
    HealingConfiguration, LocatorStrategy, ValidationResult
)
from src.backend.services.healing_orchestrator import HealingOrchestrator


@pytest.fixture
def healing_config():
    """Create a test healing configuration."""
    return HealingConfiguration(
        enabled=True,
        max_attempts_per_locator=3,
        chrome_session_timeout=30,
        healing_timeout=300,
        max_concurrent_sessions=2,
        backup_retention_days=7,
        enable_fingerprinting=True,
        confidence_threshold=0.7,
        strategies=[LocatorStrategy.ID, LocatorStrategy.CSS, LocatorStrategy.XPATH],
        max_alternatives=5,
        element_wait_timeout=10,
        interaction_test=True
    )


@pytest.fixture
def sample_failure_context():
    """Create a sample failure context for testing."""
    return FailureContext(
        test_file="tests/sample_test.robot",
        test_case="Login Test",
        failing_step="Click Element",
        original_locator="id=submit-button",
        target_url="http://localhost:8080/login",
        exception_type="NoSuchElementException",
        exception_message="Unable to locate element: id=submit-button",
        timestamp=datetime.now(),
        run_id="test-run-123",
        failure_type=FailureType.ELEMENT_NOT_FOUND
    )


@pytest_asyncio.fixture
async def orchestrator(healing_config):
    """Create a healing orchestrator instance for testing."""
    orchestrator = HealingOrchestrator(healing_config, "local", "test-model")
    
    # Mock the services to avoid external dependencies
    orchestrator.chrome_manager = AsyncMock()
    orchestrator.code_updater = Mock()
    orchestrator.fingerprinting = Mock()
    
    await orchestrator.start()
    yield orchestrator
    await orchestrator.stop()


class TestHealingOrchestratorInitialization:
    """Test orchestrator initialization and configuration."""
    
    def test_orchestrator_initialization(self, healing_config):
        """Test that orchestrator initializes correctly with configuration."""
        orchestrator = HealingOrchestrator(healing_config, "local", "test-model")
        
        assert orchestrator.config == healing_config
        assert orchestrator.model_provider == "local"
        assert orchestrator.model_name == "test-model"
        assert orchestrator.max_retries == 3
        assert orchestrator.retry_delay == 2.0
        assert len(orchestrator.active_sessions) == 0
        assert len(orchestrator.progress_callbacks) == 0
    
    @pytest.mark.asyncio
    async def test_orchestrator_start_stop(self, healing_config):
        """Test orchestrator start and stop lifecycle."""
        orchestrator = HealingOrchestrator(healing_config)
        
        # Mock chrome manager
        orchestrator.chrome_manager = AsyncMock()
        
        await orchestrator.start()
        orchestrator.chrome_manager.start.assert_called_once()
        
        await orchestrator.stop()
        orchestrator.chrome_manager.stop.assert_called_once()


class TestSessionManagement:
    """Test healing session management functionality."""
    
    @pytest.mark.asyncio
    async def test_initiate_healing_session(self, orchestrator, sample_failure_context):
        """Test initiating a new healing session."""
        session = await orchestrator.initiate_healing(sample_failure_context)
        
        assert session.session_id is not None
        assert session.failure_context == sample_failure_context
        assert session.status == HealingStatus.PENDING
        assert session.started_at is not None
        assert session.completed_at is None
        
        # Check session is tracked
        tracked_session = await orchestrator.get_session_status(session.session_id)
        assert tracked_session == session
    
    @pytest.mark.asyncio
    async def test_initiate_healing_disabled(self, sample_failure_context):
        """Test that healing fails when disabled in configuration."""
        config = HealingConfiguration(enabled=False)
        orchestrator = HealingOrchestrator(config)
        
        with pytest.raises(RuntimeError, match="Self-healing is disabled"):
            await orchestrator.initiate_healing(sample_failure_context)
    
    @pytest.mark.asyncio
    async def test_get_session_status(self, orchestrator, sample_failure_context):
        """Test retrieving session status."""
        session = await orchestrator.initiate_healing(sample_failure_context)
        
        retrieved_session = await orchestrator.get_session_status(session.session_id)
        assert retrieved_session == session
        
        # Test non-existent session
        non_existent = await orchestrator.get_session_status("non-existent-id")
        assert non_existent is None
    
    @pytest.mark.asyncio
    async def test_cancel_session(self, orchestrator, sample_failure_context):
        """Test cancelling an active healing session."""
        session = await orchestrator.initiate_healing(sample_failure_context)
        
        # Manually set to in progress for testing
        async with orchestrator.session_lock:
            session.status = HealingStatus.IN_PROGRESS
        
        # Cancel the session
        result = await orchestrator.cancel_session(session.session_id)
        assert result is True
        
        # Check session was cancelled
        cancelled_session = await orchestrator.get_session_status(session.session_id)
        assert cancelled_session.status == HealingStatus.FAILED
        assert cancelled_session.error_message == "Cancelled by user"
        assert cancelled_session.completed_at is not None
        
        # Test cancelling non-existent session
        result = await orchestrator.cancel_session("non-existent-id")
        assert result is False


class TestProgressTracking:
    """Test progress tracking and callback functionality."""
    
    @pytest.mark.asyncio
    async def test_progress_callback_registration(self, orchestrator):
        """Test registering progress callbacks."""
        session_id = "test-session-id"
        callback_called = []
        
        def test_callback(sid, progress_data):
            callback_called.append((sid, progress_data))
        
        orchestrator.register_progress_callback(session_id, test_callback)
        
        # Test notification
        test_progress = {"phase": "test", "progress": 0.5}
        orchestrator._notify_progress(session_id, test_progress)
        
        assert len(callback_called) == 1
        assert callback_called[0] == (session_id, test_progress)
    
    @pytest.mark.asyncio
    async def test_multiple_progress_callbacks(self, orchestrator):
        """Test multiple callbacks for the same session."""
        session_id = "test-session-id"
        callback1_calls = []
        callback2_calls = []
        
        def callback1(sid, data):
            callback1_calls.append((sid, data))
        
        def callback2(sid, data):
            callback2_calls.append((sid, data))
        
        orchestrator.register_progress_callback(session_id, callback1)
        orchestrator.register_progress_callback(session_id, callback2)
        
        test_progress = {"phase": "test", "progress": 0.5}
        orchestrator._notify_progress(session_id, test_progress)
        
        assert len(callback1_calls) == 1
        assert len(callback2_calls) == 1
        assert callback1_calls[0] == (session_id, test_progress)
        assert callback2_calls[0] == (session_id, test_progress)
    
    @pytest.mark.asyncio
    async def test_progress_callback_error_handling(self, orchestrator):
        """Test that callback errors don't break progress notification."""
        session_id = "test-session-id"
        good_callback_calls = []
        
        def failing_callback(sid, data):
            raise Exception("Callback error")
        
        def good_callback(sid, data):
            good_callback_calls.append((sid, data))
        
        orchestrator.register_progress_callback(session_id, failing_callback)
        orchestrator.register_progress_callback(session_id, good_callback)
        
        test_progress = {"phase": "test", "progress": 0.5}
        orchestrator._notify_progress(session_id, test_progress)
        
        # Good callback should still be called despite failing callback
        assert len(good_callback_calls) == 1
        assert good_callback_calls[0] == (session_id, test_progress)


class TestFailureAnalysis:
    """Test failure analysis functionality."""
    
    @pytest.mark.asyncio
    async def test_fallback_failure_analysis(self, orchestrator, sample_failure_context):
        """Test fallback failure analysis when AI analysis fails."""
        result = orchestrator._fallback_failure_analysis(sample_failure_context)
        
        assert isinstance(result, dict)
        assert "is_healable" in result
        assert "failure_type" in result
        assert "confidence" in result
        assert result["is_healable"] is True  # NoSuchElementException should be healable
        assert result["failure_type"] == "element_not_found"
        assert 0.0 <= result["confidence"] <= 1.0
    
    @pytest.mark.asyncio
    async def test_analyze_failure_with_retry(self, orchestrator, sample_failure_context):
        """Test failure analysis with retry logic."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.PENDING,
            started_at=datetime.now()
        )
        
        # Mock the agents and crew to simulate failures then success
        mock_crew = Mock()
        mock_result = Mock()
        mock_result.raw = json.dumps({
            "is_healable": True,
            "failure_type": "element_not_found",
            "confidence": 0.8,
            "element_type": "button",
            "action_intent": "click",
            "locator_strategy": "id",
            "failure_reason": "Element not found",
            "element_context": "Submit button",
            "healing_priority": "high",
            "recommendations": ["Try CSS selector", "Try XPath"]
        })
        mock_crew.kickoff.return_value = mock_result
        
        with patch('src.backend.services.healing_orchestrator.Crew', return_value=mock_crew):
            result = await orchestrator._analyze_failure(session)
        
        assert result["is_healable"] is True
        assert result["failure_type"] == "element_not_found"
        assert result["confidence"] == 0.8


class TestLocatorGeneration:
    """Test locator generation functionality."""
    
    @pytest.mark.asyncio
    async def test_fallback_locator_generation_id(self, orchestrator, sample_failure_context):
        """Test fallback locator generation for ID-based locators."""
        analysis_result = {"element_type": "button", "action_intent": "click"}
        
        alternatives = orchestrator._fallback_locator_generation(sample_failure_context, analysis_result)
        
        assert len(alternatives) > 0
        
        # Should generate CSS and XPath alternatives for ID locator
        locators = [alt["locator"] for alt in alternatives]
        strategies = [alt["strategy"] for alt in alternatives]
        
        assert any("css=" in loc for loc in locators)
        assert any("xpath=" in loc for loc in locators)
        assert "css" in strategies
        assert "xpath" in strategies
    
    @pytest.mark.asyncio
    async def test_fallback_locator_generation_css(self, orchestrator):
        """Test fallback locator generation for CSS-based locators."""
        failure_context = FailureContext(
            test_file="test.robot",
            test_case="Test",
            failing_step="Click",
            original_locator="css=#submit-button",
            target_url="http://test.com",
            exception_type="NoSuchElementException",
            exception_message="Element not found",
            timestamp=datetime.now(),
            run_id="test",
            failure_type=FailureType.ELEMENT_NOT_FOUND
        )
        
        analysis_result = {"element_type": "button"}
        alternatives = orchestrator._fallback_locator_generation(failure_context, analysis_result)
        
        assert len(alternatives) > 0
        
        # Should generate ID alternative for CSS selector
        locators = [alt["locator"] for alt in alternatives]
        assert any("id=" in loc for loc in locators)
    
    @pytest.mark.asyncio
    async def test_fallback_locator_generation_limits(self, orchestrator, sample_failure_context):
        """Test that fallback generation respects max alternatives limit."""
        analysis_result = {"element_type": "button"}
        
        alternatives = orchestrator._fallback_locator_generation(sample_failure_context, analysis_result)
        
        assert len(alternatives) <= orchestrator.config.max_alternatives


class TestLocatorValidation:
    """Test locator validation functionality."""
    
    @pytest.mark.asyncio
    async def test_validate_locators_success(self, orchestrator, sample_failure_context):
        """Test successful locator validation."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.IN_PROGRESS,
            started_at=datetime.now()
        )
        
        locator_candidates = [
            {
                "locator": "id=submit-btn",
                "strategy": "id",
                "confidence": 0.9,
                "reasoning": "Alternative ID",
                "stability_score": 0.9,
                "fallback_level": "primary"
            },
            {
                "locator": "css=#submit-btn",
                "strategy": "css",
                "confidence": 0.8,
                "reasoning": "CSS equivalent",
                "stability_score": 0.8,
                "fallback_level": "secondary"
            }
        ]
        
        # Mock Chrome session manager
        mock_session = Mock()
        mock_session.session_id = "chrome-session-123"
        
        orchestrator.chrome_manager.session_context = AsyncMock()
        orchestrator.chrome_manager.session_context.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        orchestrator.chrome_manager.session_context.return_value.__aexit__ = AsyncMock(return_value=None)
        
        # Mock validation results
        validation_result1 = ValidationResult(
            locator="id=submit-btn",
            strategy=LocatorStrategy.ID,
            is_valid=True,
            element_found=True,
            is_interactable=True,
            matches_expected_type=True,
            confidence_score=0.9,
            element_properties={"tag_name": "button", "text": "Submit"}
        )
        
        validation_result2 = ValidationResult(
            locator="css=#submit-btn",
            strategy=LocatorStrategy.CSS,
            is_valid=True,
            element_found=True,
            is_interactable=True,
            matches_expected_type=True,
            confidence_score=0.8,
            element_properties={"tag_name": "button", "text": "Submit"}
        )
        
        orchestrator.chrome_manager.validate_locator = AsyncMock(
            side_effect=[validation_result1, validation_result2]
        )
        
        results = await orchestrator._validate_locators(session, locator_candidates)
        
        assert len(results) == 2
        assert all(result["is_valid"] for result in results)
        assert len(session.attempts) == 2
        assert all(attempt.success for attempt in session.attempts)
    
    @pytest.mark.asyncio
    async def test_validate_locators_no_url(self, orchestrator):
        """Test validation when no target URL is available."""
        failure_context = FailureContext(
            test_file="test.robot",
            test_case="Test",
            failing_step="Click",
            original_locator="id=submit",
            target_url="",  # No URL
            exception_type="NoSuchElementException",
            exception_message="Element not found",
            timestamp=datetime.now(),
            run_id="test",
            failure_type=FailureType.ELEMENT_NOT_FOUND
        )
        
        session = HealingSession(
            session_id="test-session",
            failure_context=failure_context,
            status=HealingStatus.IN_PROGRESS,
            started_at=datetime.now()
        )
        
        results = await orchestrator._validate_locators(session, [])
        
        assert len(results) == 0


class TestLocatorSelection:
    """Test best locator selection functionality."""
    
    def test_select_best_locator_success(self, orchestrator):
        """Test selecting the best locator from validation results."""
        validation_results = [
            {
                "locator": "id=submit-btn",
                "strategy": "id",
                "is_valid": True,
                "confidence_score": 0.9,
                "original_candidate": {"stability_score": 0.9}
            },
            {
                "locator": "css=#submit-btn",
                "strategy": "css",
                "is_valid": True,
                "confidence_score": 0.8,
                "original_candidate": {"stability_score": 0.8}
            },
            {
                "locator": "xpath=//button[@id='submit-btn']",
                "strategy": "xpath",
                "is_valid": True,
                "confidence_score": 0.7,
                "original_candidate": {"stability_score": 0.6}
            }
        ]
        
        best_locator = orchestrator._select_best_locator(validation_results)
        
        assert best_locator is not None
        assert best_locator["locator"] == "id=submit-btn"
        assert best_locator["strategy"] == "id"
    
    def test_select_best_locator_no_valid(self, orchestrator):
        """Test selection when no valid locators are found."""
        validation_results = [
            {
                "locator": "id=submit-btn",
                "strategy": "id",
                "is_valid": False,
                "confidence_score": 0.3,
                "original_candidate": {"stability_score": 0.9}
            },
            {
                "locator": "css=#submit-btn",
                "strategy": "css",
                "is_valid": False,
                "confidence_score": 0.2,
                "original_candidate": {"stability_score": 0.8}
            }
        ]
        
        best_locator = orchestrator._select_best_locator(validation_results)
        
        assert best_locator is None
    
    def test_select_best_locator_confidence_threshold(self, orchestrator):
        """Test selection respects confidence threshold."""
        validation_results = [
            {
                "locator": "id=submit-btn",
                "strategy": "id",
                "is_valid": True,
                "confidence_score": 0.6,  # Below threshold (0.7)
                "original_candidate": {"stability_score": 0.9}
            }
        ]
        
        best_locator = orchestrator._select_best_locator(validation_results)
        
        assert best_locator is None


class TestCodeUpdate:
    """Test test code update functionality."""
    
    @pytest.mark.asyncio
    async def test_update_test_code_success(self, orchestrator, sample_failure_context):
        """Test successful test code update."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.IN_PROGRESS,
            started_at=datetime.now()
        )
        
        best_locator = {
            "locator": "id=submit-btn",
            "strategy": "id",
            "confidence_score": 0.9
        }
        
        # Mock successful update
        mock_update_result = Mock()
        mock_update_result.success = True
        mock_update_result.backup_path = "/tmp/backup.robot"
        
        orchestrator.code_updater.update_locator.return_value = mock_update_result
        
        result = await orchestrator._update_test_code(session, best_locator)
        
        assert result is True
        assert session.successful_locator == "id=submit-btn"
        assert session.backup_file_path == "/tmp/backup.robot"
        
        orchestrator.code_updater.update_locator.assert_called_once_with(
            sample_failure_context.test_file,
            sample_failure_context.original_locator,
            "id=submit-btn",
            create_backup=True
        )
    
    @pytest.mark.asyncio
    async def test_update_test_code_failure(self, orchestrator, sample_failure_context):
        """Test test code update failure."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.IN_PROGRESS,
            started_at=datetime.now()
        )
        
        best_locator = {
            "locator": "id=submit-btn",
            "strategy": "id",
            "confidence_score": 0.9
        }
        
        # Mock failed update
        mock_update_result = Mock()
        mock_update_result.success = False
        mock_update_result.error_message = "File not found"
        
        orchestrator.code_updater.update_locator.return_value = mock_update_result
        
        result = await orchestrator._update_test_code(session, best_locator)
        
        assert result is False
        assert session.successful_locator is None
        assert session.backup_file_path is None


class TestReportGeneration:
    """Test healing report generation functionality."""
    
    @pytest.mark.asyncio
    async def test_generate_healing_report_success(self, orchestrator, sample_failure_context):
        """Test generating a healing report for successful session."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.SUCCESS,
            started_at=datetime.now(),
            completed_at=datetime.now() + timedelta(seconds=30),
            successful_locator="id=submit-btn",
            backup_file_path="/tmp/backup.robot"
        )
        
        # Add some attempts
        session.attempts = [
            Mock(success=False, confidence_score=0.5),
            Mock(success=True, confidence_score=0.9)
        ]
        
        async with orchestrator.session_lock:
            orchestrator.active_sessions[session.session_id] = session
        
        report = await orchestrator.generate_healing_report(session.session_id)
        
        assert report is not None
        assert report.session == session
        assert report.original_failure == sample_failure_context
        assert report.healing_summary["healing_status"] == "success"
        assert report.healing_summary["healed_locator"] == "id=submit-btn"
        assert report.performance_metrics["attempts_count"] == 2
        assert report.performance_metrics["success_rate"] == 0.5
        assert len(report.recommendations) > 0
    
    @pytest.mark.asyncio
    async def test_generate_healing_report_not_found(self, orchestrator):
        """Test generating report for non-existent session."""
        report = await orchestrator.generate_healing_report("non-existent-session")
        
        assert report is None
    
    def test_generate_recommendations_success(self, orchestrator, sample_failure_context):
        """Test recommendation generation for successful healing."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.SUCCESS,
            started_at=datetime.now(),
            successful_locator="id=submit-btn"
        )
        
        recommendations = orchestrator._generate_recommendations(session)
        
        assert len(recommendations) > 0
        assert any("maintenance practices" in rec for rec in recommendations)
        assert any("ID-based locators" in rec for rec in recommendations)
    
    def test_generate_recommendations_failure(self, orchestrator, sample_failure_context):
        """Test recommendation generation for failed healing."""
        session = HealingSession(
            session_id="test-session",
            failure_context=sample_failure_context,
            status=HealingStatus.FAILED,
            started_at=datetime.now()
        )
        
        recommendations = orchestrator._generate_recommendations(session)
        
        assert len(recommendations) > 0
        assert any("Manual intervention" in rec for rec in recommendations)


class TestStatisticsAndCleanup:
    """Test statistics and cleanup functionality."""
    
    @pytest.mark.asyncio
    async def test_get_healing_statistics(self, orchestrator, sample_failure_context):
        """Test getting healing statistics."""
        # Add some test sessions
        session1 = HealingSession(
            session_id="session-1",
            failure_context=sample_failure_context,
            status=HealingStatus.SUCCESS,
            started_at=datetime.now()
        )
        
        session2 = HealingSession(
            session_id="session-2",
            failure_context=sample_failure_context,
            status=HealingStatus.FAILED,
            started_at=datetime.now()
        )
        
        async with orchestrator.session_lock:
            orchestrator.active_sessions["session-1"] = session1
            orchestrator.active_sessions["session-2"] = session2
        
        # Add some performance metrics
        orchestrator.performance_metrics["total_healing_time"] = [30.0, 45.0, 60.0]
        orchestrator.performance_metrics["success_rate"] = [1.0, 0.0, 1.0]
        
        stats = await orchestrator.get_healing_statistics()
        
        assert stats["session_counts"]["total"] == 2
        assert stats["session_counts"]["successful"] == 1
        assert stats["session_counts"]["failed"] == 1
        assert stats["success_rate"] == 0.5
        assert "avg_total_healing_time" in stats["performance_metrics"]
        assert "chrome_session_stats" in stats
        assert "configuration" in stats
    
    @pytest.mark.asyncio
    async def test_cleanup_completed_sessions(self, orchestrator, sample_failure_context):
        """Test cleanup of old completed sessions."""
        # Create old completed session
        old_session = HealingSession(
            session_id="old-session",
            failure_context=sample_failure_context,
            status=HealingStatus.SUCCESS,
            started_at=datetime.now() - timedelta(hours=48),
            completed_at=datetime.now() - timedelta(hours=48)
        )
        
        # Create recent completed session
        recent_session = HealingSession(
            session_id="recent-session",
            failure_context=sample_failure_context,
            status=HealingStatus.SUCCESS,
            started_at=datetime.now() - timedelta(hours=12),
            completed_at=datetime.now() - timedelta(hours=12)
        )
        
        # Create in-progress session
        active_session = HealingSession(
            session_id="active-session",
            failure_context=sample_failure_context,
            status=HealingStatus.IN_PROGRESS,
            started_at=datetime.now()
        )
        
        async with orchestrator.session_lock:
            orchestrator.active_sessions["old-session"] = old_session
            orchestrator.active_sessions["recent-session"] = recent_session
            orchestrator.active_sessions["active-session"] = active_session
        
        # Cleanup sessions older than 24 hours
        cleaned_count = await orchestrator.cleanup_completed_sessions(retention_hours=24)
        
        assert cleaned_count == 1
        
        async with orchestrator.session_lock:
            assert "old-session" not in orchestrator.active_sessions
            assert "recent-session" in orchestrator.active_sessions
            assert "active-session" in orchestrator.active_sessions


class TestHealthCheck:
    """Test health check functionality."""
    
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, orchestrator):
        """Test health check when system is healthy."""
        # Mock healthy Chrome manager
        orchestrator.chrome_manager.health_check = AsyncMock(return_value={
            "status": "healthy",
            "test_validation_success": True
        })
        
        health = await orchestrator.health_check()
        
        assert health["status"] == "healthy"
        assert health["orchestrator_enabled"] is True
        assert health["active_sessions"] == 0
        assert health["in_progress_sessions"] == 0
        assert "chrome_manager_health" in health
        assert "timestamp" in health
    
    @pytest.mark.asyncio
    async def test_health_check_degraded(self, orchestrator, sample_failure_context):
        """Test health check when system is degraded."""
        # Mock degraded Chrome manager
        orchestrator.chrome_manager.health_check = AsyncMock(return_value={
            "status": "degraded",
            "test_validation_success": False
        })
        
        # Add too many in-progress sessions
        for i in range(orchestrator.config.max_concurrent_sessions + 1):
            session = HealingSession(
                session_id=f"session-{i}",
                failure_context=sample_failure_context,
                status=HealingStatus.IN_PROGRESS,
                started_at=datetime.now()
            )
            async with orchestrator.session_lock:
                orchestrator.active_sessions[f"session-{i}"] = session
        
        health = await orchestrator.health_check()
        
        assert health["status"] == "degraded"
        assert health["in_progress_sessions"] > orchestrator.config.max_concurrent_sessions
    
    @pytest.mark.asyncio
    async def test_health_check_error(self, orchestrator):
        """Test health check when an error occurs."""
        # Mock Chrome manager to raise exception
        orchestrator.chrome_manager.health_check = AsyncMock(side_effect=Exception("Test error"))
        
        health = await orchestrator.health_check()
        
        assert health["status"] == "unhealthy"
        assert "error" in health
        assert "Test error" in health["error"]


class TestPerformanceMetrics:
    """Test performance metrics tracking."""
    
    def test_update_performance_metrics(self, orchestrator):
        """Test updating performance metrics."""
        metrics = {
            "total_healing_time": 45.0,
            "analysis_time": 10.0,
            "generation_time": 15.0,
            "validation_time": 20.0
        }
        
        orchestrator._update_performance_metrics(metrics, success=True)
        
        assert orchestrator.performance_metrics["total_healing_time"] == [45.0]
        assert orchestrator.performance_metrics["analysis_time"] == [10.0]
        assert orchestrator.performance_metrics["success_rate"] == [1.0]
        
        # Test failure case
        orchestrator._update_performance_metrics(metrics, success=False)
        
        assert len(orchestrator.performance_metrics["success_rate"]) == 2
        assert orchestrator.performance_metrics["success_rate"][-1] == 0.0
    
    def test_performance_metrics_limit(self, orchestrator):
        """Test that performance metrics are limited to 100 entries."""
        # Add more than 100 entries
        for i in range(150):
            metrics = {"total_healing_time": float(i)}
            orchestrator._update_performance_metrics(metrics, success=True)
        
        # Should be limited to 100
        assert len(orchestrator.performance_metrics["total_healing_time"]) == 100
        assert len(orchestrator.performance_metrics["success_rate"]) == 100
        
        # Should contain the last 100 entries
        assert orchestrator.performance_metrics["total_healing_time"][0] == 50.0
        assert orchestrator.performance_metrics["total_healing_time"][-1] == 149.0


if __name__ == "__main__":
    pytest.main([__file__])