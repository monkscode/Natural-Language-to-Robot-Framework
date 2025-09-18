"""
Unit tests for the Failure Detection Service.

Tests various Robot Framework output.xml formats and failure scenarios
to ensure accurate detection and classification of healable failures.
"""

import pytest
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, mock_open

from src.backend.services.failure_detection_service import FailureDetectionService
from src.backend.core.models import FailureType


class TestFailureDetectionService:
    """Test cases for FailureDetectionService."""

    def setup_method(self):
        """Set up test fixtures."""
        self.service = FailureDetectionService()

    def test_is_locator_failure_element_not_found(self):
        """Test detection of NoSuchElementException failures."""
        exception_details = "NoSuchElementException: Unable to locate element with xpath: //button[@id='submit']"
        is_healable, failure_type = self.service.is_locator_failure(exception_details)
        
        assert is_healable is True
        assert failure_type == FailureType.ELEMENT_NOT_FOUND

    def test_is_locator_failure_element_not_interactable(self):
        """Test detection of ElementNotInteractableException failures."""
        exception_details = "ElementNotInteractableException: Element is not interactable"
        is_healable, failure_type = self.service.is_locator_failure(exception_details)
        
        assert is_healable is True
        assert failure_type == FailureType.ELEMENT_NOT_INTERACTABLE

    def test_is_locator_failure_timeout(self):
        """Test detection of TimeoutException failures."""
        exception_details = "TimeoutException: Timed out waiting for element to be visible"
        is_healable, failure_type = self.service.is_locator_failure(exception_details)
        
        assert is_healable is True
        assert failure_type == FailureType.TIMEOUT

    def test_is_locator_failure_stale_element(self):
        """Test detection of StaleElementReferenceException failures."""
        exception_details = "StaleElementReferenceException: Element is no longer attached to DOM"
        is_healable, failure_type = self.service.is_locator_failure(exception_details)
        
        assert is_healable is True
        assert failure_type == FailureType.STALE_ELEMENT

    def test_is_locator_failure_non_healable(self):
        """Test detection of non-healable failures."""
        exception_details = "AssertionError: Expected text 'Hello' but got 'Hi'"
        is_healable, failure_type = self.service.is_locator_failure(exception_details)
        
        assert is_healable is False
        assert failure_type == FailureType.OTHER

    def test_extract_locator_from_text_click_element(self):
        """Test locator extraction from Click Element keyword."""
        text = "Click Element    xpath://button[@id='submit']"
        locator = self.service._extract_locator_from_text(text)
        
        assert locator == "xpath://button[@id='submit']"

    def test_extract_locator_from_text_input_text(self):
        """Test locator extraction from Input Text keyword."""
        text = "Input Text    id:username    testuser"
        locator = self.service._extract_locator_from_text(text)
        
        assert locator == "id:username"

    def test_extract_locator_from_text_wait_until_visible(self):
        """Test locator extraction from Wait Until Element Is Visible keyword."""
        text = "Wait Until Element Is Visible    css:.submit-button"
        locator = self.service._extract_locator_from_text(text)
        
        assert locator == "css:.submit-button"

    def test_extract_url_from_text_open_browser(self):
        """Test URL extraction from Open Browser message."""
        text = "Opening browser 'chrome' to base url 'https://example.com/login'."
        url = self.service._extract_url_from_text(text)
        
        assert url == "https://example.com/login"

    def test_extract_url_from_text_go_to(self):
        """Test URL extraction from Go To keyword."""
        text = "Go To    https://example.com/dashboard"
        url = self.service._extract_url_from_text(text)
        
        assert url == "https://example.com/dashboard"

    def test_extract_exception_details_selenium_exception(self):
        """Test extraction of Selenium exception details."""
        message = "NoSuchElementException: Unable to locate element with xpath: //button[@id='submit']"
        exception_type, exception_message = self.service._extract_exception_details(message)
        
        assert exception_type == "NoSuchElementException"
        assert exception_message == "Unable to locate element with xpath: //button[@id='submit']"

    def test_extract_exception_details_generic_error(self):
        """Test extraction of generic error details."""
        message = "Error: Something went wrong during test execution"
        exception_type, exception_message = self.service._extract_exception_details(message)
        
        assert exception_type == "Error"
        assert exception_message == "Something went wrong during test execution"

    def test_extract_failing_step(self):
        """Test extraction of failing step from logs."""
        logs = """
        FAIL : Click Element failed
        NoSuchElementException: Unable to locate element
        """
        step = self.service._extract_failing_step(logs)
        
        assert step == "Click Element failed"

    def create_test_output_xml(self, test_status="FAIL", failure_message="", keyword_name="Click Element", 
                              keyword_args=None, keyword_status="FAIL"):
        """Helper method to create test output.xml content."""
        if keyword_args is None:
            keyword_args = ["xpath://button[@id='submit']"]
        
        root = ET.Element("robot", generator="Robot 7.3.2", generated="2025-01-01T00:00:00.000000")
        suite = ET.SubElement(root, "suite", id="s1", name="TestSuite", source="/app/test.robot")
        test = ET.SubElement(suite, "test", id="s1-t1", name="Test Case", line="5")
        
        # Add keyword
        kw = ET.SubElement(test, "kw", name=keyword_name, owner="SeleniumLibrary")
        for arg in keyword_args:
            arg_elem = ET.SubElement(kw, "arg")
            arg_elem.text = arg
        
        # Add keyword status
        kw_status = ET.SubElement(kw, "status", status=keyword_status, 
                                 start="2025-01-01T00:00:00.000000", elapsed="1.0")
        if keyword_status == "FAIL":
            kw_status.text = failure_message
        
        # Add test status
        test_status_elem = ET.SubElement(test, "status", status=test_status,
                                        start="2025-01-01T00:00:00.000000", elapsed="2.0")
        if test_status == "FAIL":
            test_status_elem.text = failure_message
        
        return ET.tostring(root, encoding='unicode')

    def test_analyze_execution_result_element_not_found(self):
        """Test analysis of output.xml with element not found failure."""
        failure_message = "NoSuchElementException: Unable to locate element with xpath: //button[@id='submit']"
        xml_content = self.create_test_output_xml(failure_message=failure_message)
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(xml_content)
            f.flush()
            
            failures = self.service.analyze_execution_result(f.name)
            
        assert len(failures) == 1
        failure = failures[0]
        assert failure.failure_type == FailureType.ELEMENT_NOT_FOUND
        assert failure.original_locator == "xpath://button[@id='submit']"
        assert failure.exception_type == "NoSuchElementException"
        assert "Unable to locate element" in failure.exception_message

    def test_analyze_execution_result_timeout_failure(self):
        """Test analysis of output.xml with timeout failure."""
        failure_message = "TimeoutException: Timed out waiting for element to be visible"
        xml_content = self.create_test_output_xml(
            failure_message=failure_message,
            keyword_name="Wait Until Element Is Visible",
            keyword_args=["css:.loading-spinner"]
        )
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(xml_content)
            f.flush()
            
            failures = self.service.analyze_execution_result(f.name)
            
        assert len(failures) == 1
        failure = failures[0]
        assert failure.failure_type == FailureType.TIMEOUT
        assert failure.original_locator == "css:.loading-spinner"
        assert failure.failing_step == "Wait Until Element Is Visible"

    def test_analyze_execution_result_non_healable_failure(self):
        """Test analysis of output.xml with non-healable failure."""
        failure_message = "AssertionError: Expected text 'Hello' but got 'Hi'"
        xml_content = self.create_test_output_xml(
            failure_message=failure_message,
            keyword_name="Element Should Contain",
            keyword_args=["id:message", "Hello"]
        )
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(xml_content)
            f.flush()
            
            failures = self.service.analyze_execution_result(f.name)
            
        # Should not detect any healable failures
        assert len(failures) == 0

    def test_analyze_execution_result_passing_test(self):
        """Test analysis of output.xml with passing test."""
        xml_content = self.create_test_output_xml(
            test_status="PASS",
            keyword_status="PASS",
            failure_message=""
        )
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(xml_content)
            f.flush()
            
            failures = self.service.analyze_execution_result(f.name)
            
        # Should not detect any failures
        assert len(failures) == 0

    def test_analyze_execution_result_invalid_xml(self):
        """Test analysis with invalid XML file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write("Invalid XML content <unclosed>")
            f.flush()
            
            with pytest.raises(ValueError, match="Failed to parse output.xml"):
                self.service.analyze_execution_result(f.name)

    def test_analyze_execution_result_missing_file(self):
        """Test analysis with missing output.xml file."""
        with pytest.raises(FileNotFoundError, match="Output file not found"):
            self.service.analyze_execution_result("/nonexistent/file.xml")

    def test_extract_failure_context_from_logs(self):
        """Test extraction of failure context from log content."""
        logs = """
        INFO: Opening browser 'chrome' to base url 'https://example.com/login'.
        INFO: Click Element    xpath://button[@id='submit']
        FAIL: NoSuchElementException: Unable to locate element with xpath: //button[@id='submit']
        """
        
        context = self.service.extract_failure_context(
            logs, 
            test_file="/app/test.robot", 
            test_case="Login Test"
        )
        
        assert context is not None
        assert context.test_file == "/app/test.robot"
        assert context.test_case == "Login Test"
        assert context.original_locator == "xpath://button[@id='submit']"
        assert context.target_url == "https://example.com/login"
        assert context.failure_type == FailureType.ELEMENT_NOT_FOUND

    def test_extract_failure_context_non_healable_logs(self):
        """Test extraction with non-healable failure logs."""
        logs = """
        INFO: Element Should Contain    id:message    Hello
        FAIL: AssertionError: Expected text 'Hello' but got 'Hi'
        """
        
        context = self.service.extract_failure_context(logs)
        
        # Should return None for non-healable failures
        assert context is None

    def test_get_failure_statistics_empty_list(self):
        """Test statistics generation with empty failure list."""
        stats = self.service.get_failure_statistics([])
        
        assert stats["total_failures"] == 0
        assert stats["healable_failures"] == 0
        assert stats["failure_types"] == {}
        assert stats["most_common_locators"] == {}
        assert stats["most_common_urls"] == {}

    def test_get_failure_statistics_with_failures(self):
        """Test statistics generation with multiple failures."""
        from src.backend.core.models import FailureContext
        
        failures = [
            FailureContext(
                test_file="test1.robot",
                test_case="Test 1",
                failing_step="Click Element",
                original_locator="xpath://button[@id='submit']",
                target_url="https://example.com/page1",
                exception_type="NoSuchElementException",
                exception_message="Element not found",
                timestamp=datetime.now(),
                run_id="run1",
                failure_type=FailureType.ELEMENT_NOT_FOUND
            ),
            FailureContext(
                test_file="test2.robot",
                test_case="Test 2",
                failing_step="Input Text",
                original_locator="id:username",
                target_url="https://example.com/page1",
                exception_type="TimeoutException",
                exception_message="Timeout waiting",
                timestamp=datetime.now(),
                run_id="run1",
                failure_type=FailureType.TIMEOUT
            ),
            FailureContext(
                test_file="test3.robot",
                test_case="Test 3",
                failing_step="Click Element",
                original_locator="xpath://button[@id='submit']",
                target_url="https://example.com/page2",
                exception_type="NoSuchElementException",
                exception_message="Element not found",
                timestamp=datetime.now(),
                run_id="run1",
                failure_type=FailureType.ELEMENT_NOT_FOUND
            )
        ]
        
        stats = self.service.get_failure_statistics(failures)
        
        assert stats["total_failures"] == 3
        assert stats["healable_failures"] == 3
        assert stats["failure_types"]["element_not_found"] == 2
        assert stats["failure_types"]["timeout"] == 1
        assert stats["most_common_locators"]["xpath://button[@id='submit']"] == 2
        assert stats["most_common_locators"]["id:username"] == 1
        assert stats["most_common_urls"]["https://example.com/page1"] == 2
        assert stats["most_common_urls"]["https://example.com/page2"] == 1

    def test_multiple_locator_patterns(self):
        """Test extraction of various locator patterns."""
        test_cases = [
            ("Click Button    id:submit-btn", "id:submit-btn"),
            ("Select From List By Label    name:country    USA", "name:country"),
            ("Clear Element Text    css:.input-field", "css:.input-field"),
            ("Get Text    xpath://div[@class='result']", "xpath://div[@class='result']"),
            ("Element Should Be Visible    link:Click Here", "link:Click Here"),
        ]
        
        for text, expected_locator in test_cases:
            locator = self.service._extract_locator_from_text(text)
            assert locator == expected_locator, f"Failed for text: {text}"

    def test_complex_output_xml_structure(self):
        """Test analysis of complex output.xml with multiple suites and tests."""
        # Create a more complex XML structure
        root = ET.Element("robot", generator="Robot 7.3.2", generated="2025-01-01T00:00:00.000000")
        
        # Suite 1 with passing test
        suite1 = ET.SubElement(root, "suite", id="s1", name="Suite1", source="/app/suite1.robot")
        test1 = ET.SubElement(suite1, "test", id="s1-t1", name="Passing Test", line="5")
        kw1 = ET.SubElement(test1, "kw", name="Click Element", owner="SeleniumLibrary")
        arg1 = ET.SubElement(kw1, "arg")
        arg1.text = "id:button1"
        kw1_status = ET.SubElement(kw1, "status", status="PASS", start="2025-01-01T00:00:00.000000", elapsed="1.0")
        test1_status = ET.SubElement(test1, "status", status="PASS", start="2025-01-01T00:00:00.000000", elapsed="2.0")
        
        # Suite 2 with failing test
        suite2 = ET.SubElement(root, "suite", id="s2", name="Suite2", source="/app/suite2.robot")
        test2 = ET.SubElement(suite2, "test", id="s2-t1", name="Failing Test", line="10")
        kw2 = ET.SubElement(test2, "kw", name="Input Text", owner="SeleniumLibrary")
        arg2a = ET.SubElement(kw2, "arg")
        arg2a.text = "xpath://input[@name='email']"
        arg2b = ET.SubElement(kw2, "arg")
        arg2b.text = "test@example.com"
        failure_msg = "ElementNotInteractableException: Element is not interactable"
        kw2_status = ET.SubElement(kw2, "status", status="FAIL", start="2025-01-01T00:00:00.000000", elapsed="1.0")
        kw2_status.text = failure_msg
        test2_status = ET.SubElement(test2, "status", status="FAIL", start="2025-01-01T00:00:00.000000", elapsed="2.0")
        test2_status.text = failure_msg
        
        xml_content = ET.tostring(root, encoding='unicode')
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(xml_content)
            f.flush()
            
            failures = self.service.analyze_execution_result(f.name)
            
        assert len(failures) == 1
        failure = failures[0]
        assert failure.test_case == "Failing Test"
        assert failure.original_locator == "xpath://input[@name='email']"
        assert failure.failure_type == FailureType.ELEMENT_NOT_INTERACTABLE
        assert failure.failing_step == "Input Text"


if __name__ == "__main__":
    pytest.main([__file__])