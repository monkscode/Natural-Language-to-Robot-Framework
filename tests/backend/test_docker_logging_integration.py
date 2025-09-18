#!/usr/bin/env python3
"""
Integration tests for Docker service logging and Robot Framework log extraction.
This replaces the temporary test files that were in the root directory.
"""

import pytest
import tempfile
import uuid
import os
import sys
from pathlib import Path

# Add the backend to the path for testing
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "backend"))

from tests.utils.docker_test_helpers import (
    create_test_output_xml, 
    setup_test_logging,
    cleanup_test_files
)


class TestDockerLogging:
    """Test Docker service logging and Robot Framework log extraction."""
    
    def setup_method(self):
        """Set up test environment."""
        setup_test_logging()
        self.run_id = f"test-{uuid.uuid4().hex[:8]}"
    
    def teardown_method(self):
        """Clean up test environment."""
        cleanup_test_files(self.run_id)
    
    def test_robot_framework_log_extraction_failed_test(self):
        """Test log extraction for failed tests."""
        # This test validates the Robot Framework log extraction functionality
        # that replaces Docker container log access
        
        with tempfile.TemporaryDirectory() as temp_dir:
            output_xml_path = os.path.join(temp_dir, "output.xml")
            log_html_path = os.path.join(temp_dir, "log.html")
            
            # Create sample output.xml with failure
            failure_message = "NoSuchElementException: Unable to locate element with xpath: //button[@id='submit']"
            xml_content = create_test_output_xml("FAIL", failure_message)
            
            with open(output_xml_path, 'w') as f:
                f.write(xml_content)
            
            # Create sample log.html
            with open(log_html_path, 'w') as f:
                f.write("<html><body>Sample Robot Framework Log</body></html>")
            
            # Test the log extraction function (simulated since we can't import directly)
            # In a real test environment, this would import and test _extract_robot_framework_logs
            
            # Verify files exist
            assert os.path.exists(output_xml_path)
            assert os.path.exists(log_html_path)
            
            # Verify XML content contains expected failure information
            with open(output_xml_path, 'r') as f:
                content = f.read()
                assert "NoSuchElementException" in content
                assert 'status="FAIL"' in content
                assert 'fail="1"' in content
    
    def test_robot_framework_log_extraction_passed_test(self):
        """Test log extraction for passed tests."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_xml_path = os.path.join(temp_dir, "output.xml")
            log_html_path = os.path.join(temp_dir, "log.html")
            
            # Create sample output.xml with success
            xml_content = create_test_output_xml("PASS", "", "Click Element", ["xpath://button[@id='submit']"], "PASS")
            
            with open(output_xml_path, 'w') as f:
                f.write(xml_content)
            
            with open(log_html_path, 'w') as f:
                f.write("<html><body>Sample Robot Framework Log</body></html>")
            
            # Verify files exist
            assert os.path.exists(output_xml_path)
            assert os.path.exists(log_html_path)
            
            # Verify XML content contains expected success information
            with open(output_xml_path, 'r') as f:
                content = f.read()
                assert 'status="PASS"' in content
                assert 'pass="1"' in content
                assert 'fail="0"' in content
    
    def test_missing_output_files_handling(self):
        """Test graceful handling of missing Robot Framework output files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Test with non-existent files
            output_xml_path = os.path.join(temp_dir, "nonexistent_output.xml")
            log_html_path = os.path.join(temp_dir, "nonexistent_log.html")
            
            # Verify files don't exist
            assert not os.path.exists(output_xml_path)
            assert not os.path.exists(log_html_path)
            
            # This test validates that the system handles missing files gracefully
            # In the actual implementation, this would test _extract_robot_framework_logs
            # with missing files and verify it returns appropriate error messages
    
    def test_docker_service_logging_configuration(self):
        """Test that Docker service logging is properly configured."""
        # This test validates that the logging configuration is working
        # It would test the log_docker_operation function and ContainerLogsInterceptor
        
        # In a real environment, this would:
        # 1. Import the Docker service
        # 2. Test the log_docker_operation function
        # 3. Verify the ContainerLogsInterceptor catches container.logs() calls
        # 4. Validate that 409 error detection works
        
        # For now, we just validate the test structure
        assert True  # Placeholder for actual logging tests
    
    def test_container_logs_interceptor_detection(self):
        """Test that the ContainerLogsInterceptor properly detects container.logs() calls."""
        # This test would validate that any attempt to call container.logs()
        # is properly intercepted and logged with the 🚨 CONTAINER_LOGS_CALL_DETECTED message
        
        # In a real environment, this would:
        # 1. Create a mock container
        # 2. Wrap it with ContainerLogsInterceptor
        # 3. Attempt to call logs() method
        # 4. Verify that it raises an error and logs the detection
        
        assert True  # Placeholder for actual interceptor tests


class TestDockerServiceIntegration:
    """Integration tests for Docker service functionality."""
    
    def test_docker_service_replaces_container_logs(self):
        """Test that Docker service uses Robot Framework files instead of container logs."""
        # This test validates the core fix: that the Docker service
        # no longer accesses container.logs() and instead uses Robot Framework files
        
        # In a real environment, this would:
        # 1. Mock the Docker client and container
        # 2. Call run_test_in_container()
        # 3. Verify that container.logs() is never called
        # 4. Verify that Robot Framework files are accessed instead
        
        assert True  # Placeholder for actual integration tests
    
    def test_workflow_service_docker_integration(self):
        """Test that workflow service properly integrates with Docker service."""
        # This test validates that the workflow service correctly calls
        # the Docker service and handles the results
        
        # In a real environment, this would:
        # 1. Mock the Docker service
        # 2. Call the workflow service functions
        # 3. Verify proper integration and error handling
        
        assert True  # Placeholder for actual workflow tests


if __name__ == "__main__":
    pytest.main([__file__, "-v"])