"""
End-to-end integration tests for the Healing Orchestrator.

These tests verify the complete healing workflow from failure detection
through locator healing and test code updates.
"""

import asyncio
import json
import pytest
import pytest_asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from src.backend.core.models import (
    FailureContext, HealingConfiguration, FailureType, 
    LocatorStrategy, ValidationResult
)
from src.backend.services.healing_orchestrator import HealingOrchestrator


@pytest.fixture
def integration_config():
    """Create configuration for integration testing."""
    return HealingConfiguration(
        enabled=True,
        max_attempts_per_locator=2,
        chrome_session_timeout=30,
        healing_timeout=60,
        max_concurrent_sessions=1,
        confidence_threshold=0.6,
        max_alternatives=3
    )


@pytest.fixture
def sample_robot_test_file():
    """Create a temporary Robot Framework test file."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.robot', delete=False) as f:
        f.write("""*** Test Cases ***
Login Test
    Open Browser    http://localhost:8080/login    chrome
    Input Text    id=username    testuser
    Input Text    id=password    testpass
    Click Element    id=submit-button
    Page Should Contain    Welcome
""")
        return f.name


@pytest.fixture
def integration_failure_context(sample_robot_test_file):
    """Create a failure context for integration testing."""
    return FailureContext(
        test_file=sample_robot_test_file,
        test_case="Login Test",
        failing_step="Click Element    id=submit-button",
        original_locator="id=submit-button",
        target_url="http://localhost:8080/login",
        exception_type="NoSuchElementException",
        exception_message="Unable to locate element: id=submit-button",
        timestamp=datetime.now(),
        run_id="integration-test-123",
        failure_type=FailureType.ELEMENT_NOT_FOUND
    )


@pytest_asyncio.fixture
async def integration_orchestrator(integration_config):
    """Create an orchestrator with mocked external dependencies."""
    orchestrator = HealingOrchestrator(integration_config, "local", "test-model")
    
    # Mock Chrome session manager
    mock_chrome_manager = AsyncMock()
    mock_session = Mock()
    mock_session.session_id = "test-chrome-session"
    
    # Mock session context manager
    mock_context = AsyncMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_context.__aexit__ = AsyncMock(return_value=None)
    mock_chrome_manager.session_context.return_value = mock_context
    
    # Mock validation results - simulate finding a working alternative
    validation_results = [
        ValidationResult(
            locator="id=submit-btn",
            strategy=LocatorStrategy.ID,
            is_valid=True,
            element_found=True,
            is_interactable=True,
            matches_expected_type=True,
            confidence_score=0.9,
            element_properties={"tag_name": "button", "text": "Submit"}
        ),
        ValidationResult(
            locator="css=button[type='submit']",
            strategy=LocatorStrategy.CSS,
            is_valid=True,
            element_found=True,
            is_interactable=True,
            matches_expected_type=True,
            confidence_score=0.8,
            element_properties={"tag_name": "button", "type": "submit"}
        )
    ]
    
    mock_chrome_manager.validate_locator = AsyncMock(side_effect=validation_results)
    orchestrator.chrome_manager = mock_chrome_manager
    
    # Mock test code updater
    mock_code_updater = Mock()
    mock_update_result = Mock()
    mock_update_result.success = True
    mock_update_result.backup_path = "/tmp/backup_test.robot"
    mock_update_result.updated_locators = [("id=submit-button", "id=submit-btn")]
    mock_code_updater.update_locator.return_value = mock_update_result
    orchestrator.code_updater = mock_code_updater
    
    # Mock fingerprinting service
    orchestrator.fingerprinting = Mock()
    
    await orchestrator.start()
    yield orchestrator
    await orchestrator.stop()


class TestHealingWorkflowIntegration:
    """Integration tests for the complete healing workflow."""
    
    @pytest.mark.asyncio
    async def test_complete_healing_workflow_success(self, integration_orchestrator, integration_failure_context):
        """Test a complete successful healing workflow from start to finish."""
        
        # Mock AI agent responses
        mock_analysis_response = {
            "is_healable": True,
            "failure_type": "element_not_found",
            "confidence": 0.9,
            "element_type": "button",
            "action_intent": "click",
            "locator_strategy": "id",
            "failure_reason": "Element ID changed",
            "element_context": "Submit button for login form",
            "healing_priority": "high",
            "recommendations": ["Try alternative ID", "Use CSS selector"]
        }
        
        mock_generation_response = {
            "alternatives": [
                {
                    "locator": "id=submit-btn",
                    "strategy": "id",
                    "confidence": 0.9,
                    "reasoning": "Alternative ID pattern",
                    "stability_score": 0.9,
                    "fallback_level": "primary"
                },
                {
                    "locator": "css=button[type='submit']",
                    "strategy": "css",
                    "confidence": 0.8,
                    "reasoning": "Type-based CSS selector",
                    "stability_score": 0.7,
                    "fallback_level": "secondary"
                }
            ],
            "generation_strategy": "ID and CSS alternatives",
            "recommendations": ["Monitor for stability"]
        }
        
        # Mock the Crew AI responses
        with patch('src.backend.services.healing_orchestrator.Crew') as mock_crew_class:
            mock_crew = Mock()
            mock_crew_class.return_value = mock_crew
            
            # Mock analysis result
            mock_analysis_result = Mock()
            mock_analysis_result.raw = json.dumps(mock_analysis_response)
            
            # Mock generation result  
            mock_generation_result = Mock()
            mock_generation_result.raw = json.dumps(mock_generation_response)
            
            # Set up crew to return different results for different calls
            mock_crew.kickoff.side_effect = [mock_analysis_result, mock_generation_result]
            
            # Track progress updates
            progress_updates = []
            def track_progress(session_id, progress_data):
                progress_updates.append(progress_data)
            
            # Initiate healing
            session = await integration_orchestrator.initiate_healing(integration_failure_context)
            integration_orchestrator.register_progress_callback(session.session_id, track_progress)
            
            # Wait for healing to complete (with timeout)
            max_wait = 30  # seconds
            wait_time = 0
            while session.status.value in ["pending", "in_progress"] and wait_time < max_wait:
                await asyncio.sleep(0.5)
                wait_time += 0.5
                session = await integration_orchestrator.get_session_status(session.session_id)
            
            # Verify healing completed successfully
            assert session.status.value == "success"
            assert session.successful_locator == "id=submit-btn"
            assert session.backup_file_path is not None
            assert len(session.attempts) == 2  # Two locators were validated
            assert any(attempt.success for attempt in session.attempts)
            
            # Verify progress updates were sent
            assert len(progress_updates) > 0
            assert any(update.get("phase") == "completed" for update in progress_updates)
            
            # Verify code updater was called
            integration_orchestrator.code_updater.update_locator.assert_called_once_with(
                integration_failure_context.test_file,
                "id=submit-button",
                "id=submit-btn",
                create_backup=True
            )
            
            # Verify Chrome session manager was used
            integration_orchestrator.chrome_manager.validate_locator.assert_called()
    
    @pytest.mark.asyncio
    async def test_healing_workflow_failure_no_valid_locators(self, integration_orchestrator, integration_failure_context):
        """Test healing workflow when no valid alternative locators are found."""
        
        # Mock AI responses that generate alternatives but they all fail validation
        mock_analysis_response = {
            "is_healable": True,
            "failure_type": "element_not_found",
            "confidence": 0.8,
            "element_type": "button",
            "action_intent": "click",
            "locator_strategy": "id",
            "failure_reason": "Element removed from page",
            "element_context": "Submit button",
            "healing_priority": "high",
            "recommendations": ["Check if element still exists"]
        }
        
        mock_generation_response = {
            "alternatives": [
                {
                    "locator": "id=nonexistent-btn",
                    "strategy": "id",
                    "confidence": 0.7,
                    "reasoning": "Alternative ID",
                    "stability_score": 0.8,
                    "fallback_level": "primary"
                }
            ],
            "generation_strategy": "Alternative ID search",
            "recommendations": ["Element may have been removed"]
        }
        
        # Mock validation to return failed results
        failed_validation = ValidationResult(
            locator="id=nonexistent-btn",
            strategy=LocatorStrategy.ID,
            is_valid=False,
            element_found=False,
            is_interactable=False,
            matches_expected_type=False,
            confidence_score=0.0,
            error_message="Element not found"
        )
        
        integration_orchestrator.chrome_manager.validate_locator = AsyncMock(return_value=failed_validation)
        
        with patch('src.backend.services.healing_orchestrator.Crew') as mock_crew_class:
            mock_crew = Mock()
            mock_crew_class.return_value = mock_crew
            
            mock_analysis_result = Mock()
            mock_analysis_result.raw = json.dumps(mock_analysis_response)
            
            mock_generation_result = Mock()
            mock_generation_result.raw = json.dumps(mock_generation_response)
            
            mock_crew.kickoff.side_effect = [mock_analysis_result, mock_generation_result]
            
            # Initiate healing
            session = await integration_orchestrator.initiate_healing(integration_failure_context)
            
            # Wait for healing to complete
            max_wait = 30
            wait_time = 0
            while session.status.value in ["pending", "in_progress"] and wait_time < max_wait:
                await asyncio.sleep(0.5)
                wait_time += 0.5
                session = await integration_orchestrator.get_session_status(session.session_id)
            
            # Verify healing failed
            assert session.status.value == "failed"
            assert session.successful_locator is None
            assert "No valid alternative locators found" in session.error_message
            assert len(session.attempts) == 1  # One failed attempt
            assert not any(attempt.success for attempt in session.attempts)
            
            # Verify code updater was not called
            integration_orchestrator.code_updater.update_locator.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_healing_workflow_not_healable(self, integration_orchestrator, integration_failure_context):
        """Test healing workflow when failure is determined to be not healable."""
        
        # Mock analysis response indicating failure is not healable
        mock_analysis_response = {
            "is_healable": False,
            "failure_type": "other",
            "confidence": 0.9,
            "element_type": "unknown",
            "action_intent": "unknown",
            "locator_strategy": "unknown",
            "failure_reason": "Network timeout error",
            "element_context": "Not a locator issue",
            "healing_priority": "low",
            "recommendations": ["Check network connectivity", "Verify server is running"]
        }
        
        with patch('src.backend.services.healing_orchestrator.Crew') as mock_crew_class:
            mock_crew = Mock()
            mock_crew_class.return_value = mock_crew
            
            mock_analysis_result = Mock()
            mock_analysis_result.raw = json.dumps(mock_analysis_response)
            
            mock_crew.kickoff.return_value = mock_analysis_result
            
            # Initiate healing
            session = await integration_orchestrator.initiate_healing(integration_failure_context)
            
            # Wait for healing to complete
            max_wait = 30
            wait_time = 0
            while session.status.value in ["pending", "in_progress"] and wait_time < max_wait:
                await asyncio.sleep(0.5)
                wait_time += 0.5
                session = await integration_orchestrator.get_session_status(session.session_id)
            
            # Verify healing failed due to not being healable
            assert session.status.value == "failed"
            assert session.successful_locator is None
            assert "Failure is not healable" in session.error_message
            assert len(session.attempts) == 0  # No attempts made
            
            # Verify no further processing occurred
            integration_orchestrator.code_updater.update_locator.assert_not_called()
            integration_orchestrator.chrome_manager.validate_locator.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_healing_report_generation(self, integration_orchestrator, integration_failure_context):
        """Test healing report generation for completed sessions."""
        
        # Create a completed session manually for testing
        session = await integration_orchestrator.initiate_healing(integration_failure_context)
        
        # Simulate completed healing
        async with integration_orchestrator.session_lock:
            session.status = session.status.__class__.SUCCESS
            session.completed_at = datetime.now()
            session.successful_locator = "id=submit-btn"
            session.backup_file_path = "/tmp/backup.robot"
            
            # Add some mock attempts
            from src.backend.core.models import LocatorAttempt
            session.attempts = [
                LocatorAttempt(
                    locator="id=submit-btn",
                    strategy=LocatorStrategy.ID,
                    success=True,
                    confidence_score=0.9,
                    timestamp=datetime.now()
                ),
                LocatorAttempt(
                    locator="css=button[type='submit']",
                    strategy=LocatorStrategy.CSS,
                    success=True,
                    confidence_score=0.8,
                    timestamp=datetime.now()
                )
            ]
        
        # Generate report
        report = await integration_orchestrator.generate_healing_report(session.session_id)
        
        # Verify report contents
        assert report is not None
        assert report.session == session
        assert report.original_failure == integration_failure_context
        assert report.healing_summary["healing_status"] == "success"
        assert report.healing_summary["healed_locator"] == "id=submit-btn"
        assert report.healing_summary["attempts_made"] == 2
        assert report.healing_summary["successful_attempts"] == 2
        assert report.performance_metrics["attempts_count"] == 2
        assert report.performance_metrics["success_rate"] == 1.0
        assert len(report.recommendations) > 0
        
        # Verify report can be serialized
        report_dict = report.to_dict()
        assert isinstance(report_dict, dict)
        assert report_dict["session_id"] == session.session_id
        assert report_dict["status"] == "success"
        assert report_dict["healed_locator"] == "id=submit-btn"


class TestHealingOrchestratorPerformance:
    """Performance and stress tests for the healing orchestrator."""
    
    @pytest.mark.asyncio
    async def test_concurrent_healing_sessions(self, integration_orchestrator):
        """Test handling multiple concurrent healing sessions."""
        
        # Create multiple failure contexts
        failure_contexts = []
        for i in range(3):
            context = FailureContext(
                test_file=f"test_{i}.robot",
                test_case=f"Test Case {i}",
                failing_step=f"Click Element    id=button-{i}",
                original_locator=f"id=button-{i}",
                target_url=f"http://localhost:8080/page{i}",
                exception_type="NoSuchElementException",
                exception_message=f"Unable to locate element: id=button-{i}",
                timestamp=datetime.now(),
                run_id=f"test-run-{i}",
                failure_type=FailureType.ELEMENT_NOT_FOUND
            )
            failure_contexts.append(context)
        
        # Mock successful healing for all sessions
        with patch('src.backend.services.healing_orchestrator.Crew') as mock_crew_class:
            mock_crew = Mock()
            mock_crew_class.return_value = mock_crew
            
            # Mock responses
            mock_result = Mock()
            mock_result.raw = json.dumps({
                "is_healable": True,
                "failure_type": "element_not_found",
                "confidence": 0.8,
                "element_type": "button",
                "action_intent": "click",
                "locator_strategy": "id",
                "failure_reason": "Element changed",
                "element_context": "Button element",
                "healing_priority": "medium",
                "recommendations": ["Try alternatives"]
            })
            
            mock_crew.kickoff.return_value = mock_result
            
            # Initiate multiple healing sessions
            sessions = []
            for context in failure_contexts:
                session = await integration_orchestrator.initiate_healing(context)
                sessions.append(session)
            
            # Wait for all sessions to complete or timeout
            max_wait = 60
            wait_time = 0
            while wait_time < max_wait:
                all_completed = True
                for session in sessions:
                    current_session = await integration_orchestrator.get_session_status(session.session_id)
                    if current_session.status.value in ["pending", "in_progress"]:
                        all_completed = False
                        break
                
                if all_completed:
                    break
                
                await asyncio.sleep(1)
                wait_time += 1
            
            # Verify all sessions completed (either success or failure)
            for session in sessions:
                current_session = await integration_orchestrator.get_session_status(session.session_id)
                assert current_session.status.value not in ["pending", "in_progress"]
                assert current_session.completed_at is not None
    
    @pytest.mark.asyncio
    async def test_healing_statistics_tracking(self, integration_orchestrator, integration_failure_context):
        """Test that healing statistics are properly tracked."""
        
        # Perform some healing operations to generate statistics
        with patch('src.backend.services.healing_orchestrator.Crew') as mock_crew_class:
            mock_crew = Mock()
            mock_crew_class.return_value = mock_crew
            
            mock_result = Mock()
            mock_result.raw = json.dumps({
                "is_healable": False,
                "failure_type": "other",
                "confidence": 0.5
            })
            
            mock_crew.kickoff.return_value = mock_result
            
            # Create a few sessions
            sessions = []
            for i in range(2):
                session = await integration_orchestrator.initiate_healing(integration_failure_context)
                sessions.append(session)
            
            # Wait for completion
            await asyncio.sleep(2)
            
            # Get statistics
            stats = await integration_orchestrator.get_healing_statistics()
            
            # Verify statistics structure
            assert "session_counts" in stats
            assert "success_rate" in stats
            assert "performance_metrics" in stats
            assert "chrome_session_stats" in stats
            assert "configuration" in stats
            
            assert stats["session_counts"]["total"] >= 2
            assert isinstance(stats["success_rate"], float)
            assert 0.0 <= stats["success_rate"] <= 1.0


if __name__ == "__main__":
    pytest.main([__file__])