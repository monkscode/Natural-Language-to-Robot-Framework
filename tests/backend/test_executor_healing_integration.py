"""
Integration tests for Robot Framework executor with self-healing capabilities.

These tests verify that the healing system properly integrates with the existing
test execution workflow and can automatically fix failing locators.
"""

import asyncio
import json
import os
import tempfile
import uuid
from unittest.mock import Mock, patch, AsyncMock
import pytest
from datetime import datetime

from src.backend.services.workflow_service import execute_test_with_healing
from src.backend.services.failure_detection_service import FailureDetectionService
from src.backend.services.healing_orchestrator import HealingOrchestrator
from src.backend.core.models import (
    FailureContext, HealingSession, HealingStatus, FailureType, HealingConfiguration
)


class TestExecutorHealingIntegration:
    """Test suite for executor-healing integration."""

    @pytest.fixture
    def mock_docker_client(self):
        """Mock Docker client for testing."""
        client = Mock()
        client.containers = Mock()
        client.containers.run = Mock()
        return client

    @pytest.fixture
    def sample_robot_test(self):
        """Sample Robot Framework test content."""
        return """*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Sample Test
    Open Browser    https://example.com    chrome
    Click Element    id=submit-button
    Close Browser
"""

    @pytest.fixture
    def sample_failure_output_xml(self):
        """Sample output.xml with locator failure."""
        return """<?xml version="1.0" encoding="UTF-8"?>
<robot generated="2024-01-01T12:00:00.000000" generator="Robot 6.0">
    <suite id="s1" name="Test Suite" source="/app/robot_tests/test.robot">
        <test id="s1-t1" name="Sample Test">
            <kw name="Open Browser" library="SeleniumLibrary">
                <arg>https://example.com</arg>
                <arg>chrome</arg>
                <status status="PASS" starttime="20240101 12:00:00.000" endtime="20240101 12:00:01.000"/>
            </kw>
            <kw name="Click Element" library="SeleniumLibrary">
                <arg>id=submit-button</arg>
                <status status="FAIL" starttime="20240101 12:00:01.000" endtime="20240101 12:00:02.000">
                    NoSuchElementException: Unable to locate element: id=submit-button
                </status>
            </kw>
            <status status="FAIL" starttime="20240101 12:00:00.000" endtime="20240101 12:00:02.000">
                NoSuchElementException: Unable to locate element: id=submit-button
            </status>
        </test>
    </suite>
</robot>
"""

    @pytest.fixture
    def healing_config(self):
        """Sample healing configuration."""
        return HealingConfiguration(
            enabled=True,
            max_attempts_per_locator=3,
            chrome_session_timeout=30,
            healing_timeout=300,
            max_concurrent_sessions=3,
            confidence_threshold=0.7,
            max_alternatives=5
        )

    @pytest.mark.asyncio
    async def test_execute_test_with_healing_disabled(self, mock_docker_client, sample_robot_test):
        """Test that healing is skipped when disabled in configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, "test.robot")
            with open(test_file, 'w') as f:
                f.write(sample_robot_test)
            
            run_id = str(uuid.uuid4())
            
            # Mock healing config as disabled
            with patch('src.backend.services.workflow_service.get_healing_config') as mock_config:
                mock_config.return_value = HealingConfiguration(enabled=False)
                
                # Mock successful test execution
                mock_docker_client.containers.run.return_value = b"Test passed"
                
                with patch('src.backend.services.workflow_service.run_test_in_container') as mock_run:
                    mock_run.return_value = {
                        "status": "complete",
                        "message": "Test execution finished: All tests passed.",
                        "result": {"logs": "Test passed"}
                    }
                    
                    events = []
                    async for event in execute_test_with_healing(
                        mock_docker_client, run_id, "test.robot", test_file, "local", "llama3.1"
                    ):
                        events.append(json.loads(event.replace("data: ", "").strip()))
                    
                    # Should have only one execution event (no healing)
                    assert len(events) == 1
                    assert events[0]["stage"] == "execution"
                    assert events[0]["status"] == "complete"

    @pytest.mark.asyncio
    async def test_execute_test_with_successful_healing(self, mock_docker_client, sample_robot_test, 
                                                       sample_failure_output_xml, healing_config):
        """Test successful healing workflow integration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, "test.robot")
            output_xml_file = os.path.join(temp_dir, "output.xml")
            
            with open(test_file, 'w') as f:
                f.write(sample_robot_test)
            
            with open(output_xml_file, 'w') as f:
                f.write(sample_failure_output_xml)
            
            run_id = str(uuid.uuid4())
            
            # Mock healing config as enabled
            with patch('src.backend.services.workflow_service.get_healing_config') as mock_config:
                mock_config.return_value = healing_config
                
                # Mock first test run failure, then success after healing
                test_results = [
                    {
                        "status": "complete",
                        "message": "Test execution finished: Some tests failed (exit code 1).",
                        "result": {"logs": "NoSuchElementException: Unable to locate element: id=submit-button"}
                    },
                    {
                        "status": "complete", 
                        "message": "Test execution finished: All tests passed.",
                        "result": {"logs": "Test passed"}
                    }
                ]
                
                with patch('src.backend.services.workflow_service.run_test_in_container') as mock_run:
                    mock_run.side_effect = test_results
                    
                    # Mock healing orchestrator
                    with patch('src.backend.services.workflow_service.HealingOrchestrator') as mock_orchestrator_class:
                        mock_orchestrator = AsyncMock()
                        mock_orchestrator_class.return_value = mock_orchestrator
                        
                        # Mock successful healing session
                        mock_session = HealingSession(
                            session_id="test-session",
                            failure_context=FailureContext(
                                test_file=test_file,
                                test_case="Sample Test",
                                failing_step="Click Element",
                                original_locator="id=submit-button",
                                target_url="https://example.com",
                                exception_type="NoSuchElementException",
                                exception_message="Unable to locate element: id=submit-button",
                                timestamp=datetime.now(),
                                run_id=run_id,
                                failure_type=FailureType.ELEMENT_NOT_FOUND
                            ),
                            status=HealingStatus.PENDING,
                            started_at=datetime.now()
                        )
                        
                        # Mock healing completion
                        completed_session = mock_session
                        completed_session.status = HealingStatus.SUCCESS
                        completed_session.healed_locator = "css=#submit-btn"
                        completed_session.completed_at = datetime.now()
                        
                        mock_orchestrator.initiate_healing.return_value = mock_session
                        mock_orchestrator.get_session_status.side_effect = [
                            mock_session,  # First check - in progress
                            completed_session  # Second check - completed
                        ]
                        
                        # Mock failure detection
                        with patch('src.backend.services.workflow_service.FailureDetectionService') as mock_detection_class:
                            mock_detection = Mock()
                            mock_detection_class.return_value = mock_detection
                            
                            mock_failure = FailureContext(
                                test_file=test_file,
                                test_case="Sample Test", 
                                failing_step="Click Element",
                                original_locator="id=submit-button",
                                target_url="https://example.com",
                                exception_type="NoSuchElementException",
                                exception_message="Unable to locate element: id=submit-button",
                                timestamp=datetime.now(),
                                run_id=run_id,
                                failure_type=FailureType.ELEMENT_NOT_FOUND
                            )
                            
                            mock_detection.analyze_execution_result.return_value = [mock_failure]
                            
                            events = []
                            async for event in execute_test_with_healing(
                                mock_docker_client, run_id, "test.robot", test_file, "local", "llama3.1"
                            ):
                                events.append(json.loads(event.replace("data: ", "").strip()))
                            
                            # Verify healing workflow events
                            event_stages = [event["stage"] for event in events]
                            assert "execution" in event_stages
                            assert "healing" in event_stages
                            
                            # Verify healing was attempted
                            healing_events = [e for e in events if e["stage"] == "healing"]
                            assert len(healing_events) > 0
                            
                            # Verify final success
                            final_event = events[-1]
                            assert final_event["stage"] == "execution"
                            assert final_event["status"] == "complete"
                            assert "All tests passed" in final_event["message"]

    @pytest.mark.asyncio
    async def test_execute_test_with_healing_failure(self, mock_docker_client, sample_robot_test,
                                                    sample_failure_output_xml, healing_config):
        """Test handling of healing failure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, "test.robot")
            output_xml_file = os.path.join(temp_dir, "output.xml")
            
            with open(test_file, 'w') as f:
                f.write(sample_robot_test)
            
            with open(output_xml_file, 'w') as f:
                f.write(sample_failure_output_xml)
            
            run_id = str(uuid.uuid4())
            
            # Mock healing config as enabled
            with patch('src.backend.services.workflow_service.get_healing_config') as mock_config:
                mock_config.return_value = healing_config
                
                # Mock test run failure
                test_result = {
                    "status": "complete",
                    "message": "Test execution finished: Some tests failed (exit code 1).",
                    "result": {"logs": "NoSuchElementException: Unable to locate element: id=submit-button"}
                }
                
                with patch('src.backend.services.workflow_service.run_test_in_container') as mock_run:
                    mock_run.return_value = test_result
                    
                    # Mock healing orchestrator with failed healing
                    with patch('src.backend.services.workflow_service.HealingOrchestrator') as mock_orchestrator_class:
                        mock_orchestrator = AsyncMock()
                        mock_orchestrator_class.return_value = mock_orchestrator
                        
                        # Mock failed healing session
                        mock_session = HealingSession(
                            session_id="test-session",
                            failure_context=FailureContext(
                                test_file=test_file,
                                test_case="Sample Test",
                                failing_step="Click Element",
                                original_locator="id=submit-button",
                                target_url="https://example.com",
                                exception_type="NoSuchElementException",
                                exception_message="Unable to locate element: id=submit-button",
                                timestamp=datetime.now(),
                                run_id=run_id,
                                failure_type=FailureType.ELEMENT_NOT_FOUND
                            ),
                            status=HealingStatus.PENDING,
                            started_at=datetime.now()
                        )
                        
                        # Mock healing failure
                        failed_session = mock_session
                        failed_session.status = HealingStatus.FAILED
                        failed_session.error_message = "No valid alternative locators found"
                        failed_session.completed_at = datetime.now()
                        
                        mock_orchestrator.initiate_healing.return_value = mock_session
                        mock_orchestrator.get_session_status.return_value = failed_session
                        
                        # Mock failure detection
                        with patch('src.backend.services.workflow_service.FailureDetectionService') as mock_detection_class:
                            mock_detection = Mock()
                            mock_detection_class.return_value = mock_detection
                            
                            mock_failure = FailureContext(
                                test_file=test_file,
                                test_case="Sample Test",
                                failing_step="Click Element", 
                                original_locator="id=submit-button",
                                target_url="https://example.com",
                                exception_type="NoSuchElementException",
                                exception_message="Unable to locate element: id=submit-button",
                                timestamp=datetime.now(),
                                run_id=run_id,
                                failure_type=FailureType.ELEMENT_NOT_FOUND
                            )
                            
                            mock_detection.analyze_execution_result.return_value = [mock_failure]
                            
                            events = []
                            async for event in execute_test_with_healing(
                                mock_docker_client, run_id, "test.robot", test_file, "local", "llama3.1"
                            ):
                                events.append(json.loads(event.replace("data: ", "").strip()))
                            
                            # Verify healing was attempted but failed
                            healing_events = [e for e in events if e["stage"] == "healing"]
                            assert len(healing_events) > 0
                            
                            # Verify final failure with healing info
                            final_event = events[-1]
                            assert final_event["stage"] == "execution"
                            assert final_event.get("healing_attempted") == True
                            assert final_event.get("healing_successful") == False

    @pytest.mark.asyncio
    async def test_execute_test_with_non_healable_failure(self, mock_docker_client, sample_robot_test, healing_config):
        """Test handling of non-healable failures."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, "test.robot")
            output_xml_file = os.path.join(temp_dir, "output.xml")
            
            with open(test_file, 'w') as f:
                f.write(sample_robot_test)
            
            # Create output.xml with non-healable failure
            non_healable_xml = """<?xml version="1.0" encoding="UTF-8"?>
<robot generated="2024-01-01T12:00:00.000000" generator="Robot 6.0">
    <suite id="s1" name="Test Suite" source="/app/robot_tests/test.robot">
        <test id="s1-t1" name="Sample Test">
            <kw name="Should Be Equal" library="BuiltIn">
                <arg>actual</arg>
                <arg>expected</arg>
                <status status="FAIL" starttime="20240101 12:00:01.000" endtime="20240101 12:00:02.000">
                    AssertionError: 'actual' != 'expected'
                </status>
            </kw>
            <status status="FAIL" starttime="20240101 12:00:00.000" endtime="20240101 12:00:02.000">
                AssertionError: 'actual' != 'expected'
            </status>
        </test>
    </suite>
</robot>
"""
            
            with open(output_xml_file, 'w') as f:
                f.write(non_healable_xml)
            
            run_id = str(uuid.uuid4())
            
            # Mock healing config as enabled
            with patch('src.backend.services.workflow_service.get_healing_config') as mock_config:
                mock_config.return_value = healing_config
                
                # Mock test run failure
                test_result = {
                    "status": "complete",
                    "message": "Test execution finished: Some tests failed (exit code 1).",
                    "result": {"logs": "AssertionError: 'actual' != 'expected'"}
                }
                
                with patch('src.backend.services.workflow_service.run_test_in_container') as mock_run:
                    mock_run.return_value = test_result
                    
                    # Mock failure detection with no healable failures
                    with patch('src.backend.services.workflow_service.FailureDetectionService') as mock_detection_class:
                        mock_detection = Mock()
                        mock_detection_class.return_value = mock_detection
                        
                        # Return empty list (no healable failures)
                        mock_detection.analyze_execution_result.return_value = []
                        
                        events = []
                        async for event in execute_test_with_healing(
                            mock_docker_client, run_id, "test.robot", test_file, "local", "llama3.1"
                        ):
                            events.append(json.loads(event.replace("data: ", "").strip()))
                        
                        # Should attempt healing analysis but find no healable failures
                        healing_events = [e for e in events if e["stage"] == "healing"]
                        # At least one healing event should occur (the analysis phase)
                        assert len(healing_events) >= 1
                        
                        # Final event should indicate no healing was attempted
                        final_event = events[-1]
                        assert final_event["stage"] == "execution"
                        assert final_event.get("healing_attempted") == False
                        assert "No healable failures detected" in final_event.get("healing_reason", "")

    def test_failure_detection_service_integration(self, sample_failure_output_xml):
        """Test failure detection service integration."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(sample_failure_output_xml)
            output_xml_path = f.name
        
        try:
            detection_service = FailureDetectionService()
            failures = detection_service.analyze_execution_result(output_xml_path)
            
            assert len(failures) == 1
            failure = failures[0]
            
            assert failure.test_case == "Sample Test"
            assert failure.original_locator == "id=submit-button"
            assert failure.failure_type == FailureType.ELEMENT_NOT_FOUND
            assert failure.exception_type == "NoSuchElementException"
            
        finally:
            os.unlink(output_xml_path)

    def test_healing_config_loading(self):
        """Test healing configuration loading."""
        from src.backend.core.config_loader import get_healing_config
        
        # This should load the default configuration
        config = get_healing_config()
        
        assert isinstance(config, HealingConfiguration)
        assert hasattr(config, 'enabled')
        assert hasattr(config, 'max_attempts_per_locator')
        assert hasattr(config, 'confidence_threshold')


if __name__ == "__main__":
    pytest.main([__file__])
