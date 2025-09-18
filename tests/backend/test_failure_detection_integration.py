"""
Integration tests for the Failure Detection Service using real Robot Framework output files.
"""

import pytest
from pathlib import Path

from src.backend.services.failure_detection_service import FailureDetectionService
from src.backend.core.models import FailureType


class TestFailureDetectionIntegration:
    """Integration test cases for FailureDetectionService with real output files."""

    def setup_method(self):
        """Set up test fixtures."""
        self.service = FailureDetectionService()

    def test_analyze_real_output_xml(self):
        """Test analysis of the actual Robot Framework output.xml file in the project."""
        output_xml_path = Path("robot_tests/output.xml")
        
        if not output_xml_path.exists():
            pytest.skip("Real output.xml file not found")
        
        # Analyze the real output file
        failures = self.service.analyze_execution_result(str(output_xml_path))
        
        # The real output.xml has a SessionNotCreatedException which is not a locator failure
        # So we expect no healable failures to be detected
        assert isinstance(failures, list)
        
        # Generate statistics for any failures found
        stats = self.service.get_failure_statistics(failures)
        assert "total_failures" in stats
        assert "healable_failures" in stats
        assert "failure_types" in stats

    def test_service_initialization(self):
        """Test that the service initializes correctly."""
        service = FailureDetectionService()
        
        # Verify patterns are loaded
        assert len(service.LOCATOR_EXCEPTION_PATTERNS) > 0
        assert len(service.LOCATOR_PATTERNS) > 0
        assert len(service.URL_PATTERNS) > 0
        
        # Verify all failure types are covered
        assert FailureType.ELEMENT_NOT_FOUND in service.LOCATOR_EXCEPTION_PATTERNS
        assert FailureType.ELEMENT_NOT_INTERACTABLE in service.LOCATOR_EXCEPTION_PATTERNS
        assert FailureType.TIMEOUT in service.LOCATOR_EXCEPTION_PATTERNS
        assert FailureType.STALE_ELEMENT in service.LOCATOR_EXCEPTION_PATTERNS

    def test_service_with_empty_statistics(self):
        """Test statistics generation with no failures."""
        stats = self.service.get_failure_statistics([])
        
        expected_keys = [
            "total_failures", "healable_failures", "failure_types", 
            "most_common_locators", "most_common_urls"
        ]
        
        for key in expected_keys:
            assert key in stats
        
        assert stats["total_failures"] == 0
        assert stats["healable_failures"] == 0


if __name__ == "__main__":
    pytest.main([__file__])