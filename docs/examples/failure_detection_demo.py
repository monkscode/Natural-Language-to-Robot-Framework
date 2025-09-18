#!/usr/bin/env python3
"""
Demo script showing how the failure detection service works.

This script demonstrates the failure detection capabilities by analyzing
sample Robot Framework output files and identifying healable failures.
"""

import tempfile
import os
from pathlib import Path
import sys

# Add the backend to the path
sys.path.append(str(Path(__file__).parent.parent.parent / "src" / "backend"))

try:
    from services.failure_detection_service import FailureDetectionService
    from core.models import FailureType
except ImportError:
    print("⚠️  Could not import backend services. This demo requires the full application environment.")
    print("Run this demo from the project root with the virtual environment activated.")
    sys.exit(1)


def create_sample_output_xml():
    """Create a sample Robot Framework output.xml with various failure types."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<robot generator="Robot 7.3.2" generated="2025-01-01T12:00:00.000000">
    <suite id="s1" name="Login Test Suite" source="/app/robot_tests/login_test.robot">
        <test id="s1-t1" name="Valid Login Test" line="10">
            <kw name="Open Browser" library="SeleniumLibrary">
                <arg>https://example.com/login</arg>
                <arg>chrome</arg>
                <status status="PASS" starttime="2025-01-01T12:00:01.000000" endtime="2025-01-01T12:00:02.000000"/>
            </kw>
            <kw name="Click Element" library="SeleniumLibrary">
                <arg>id=login-button</arg>
                <status status="FAIL" starttime="2025-01-01T12:00:02.000000" endtime="2025-01-01T12:00:03.000000">
                    NoSuchElementException: Unable to locate element with locator 'id=login-button'
                </status>
            </kw>
            <status status="FAIL" starttime="2025-01-01T12:00:01.000000" endtime="2025-01-01T12:00:03.000000">
                NoSuchElementException: Unable to locate element with locator 'id=login-button'
            </status>
        </test>
        <test id="s1-t2" name="Invalid Login Test" line="20">
            <kw name="Input Text" library="SeleniumLibrary">
                <arg>css=input[name='username']</arg>
                <arg>testuser</arg>
                <status status="FAIL" starttime="2025-01-01T12:00:04.000000" endtime="2025-01-01T12:00:05.000000">
                    ElementNotInteractableException: Element is not interactable
                </status>
            </kw>
            <status status="FAIL" starttime="2025-01-01T12:00:04.000000" endtime="2025-01-01T12:00:05.000000">
                ElementNotInteractableException: Element is not interactable
            </status>
        </test>
        <test id="s1-t3" name="Timeout Test" line="30">
            <kw name="Wait Until Element Is Visible" library="SeleniumLibrary">
                <arg>xpath=//div[@class='loading']</arg>
                <arg>timeout=5s</arg>
                <status status="FAIL" starttime="2025-01-01T12:00:06.000000" endtime="2025-01-01T12:00:11.000000">
                    TimeoutException: Element did not become visible within 5 seconds
                </status>
            </kw>
            <status status="FAIL" starttime="2025-01-01T12:00:06.000000" endtime="2025-01-01T12:00:11.000000">
                TimeoutException: Element did not become visible within 5 seconds
            </status>
        </test>
        <status status="FAIL" starttime="2025-01-01T12:00:00.000000" endtime="2025-01-01T12:00:12.000000"/>
    </suite>
    <statistics>
        <total>
            <stat pass="0" fail="3" skip="0">All Tests</stat>
        </total>
    </statistics>
</robot>"""


def main():
    """Demonstrate the failure detection service."""
    print("🔍 Failure Detection Service Demo")
    print("=" * 50)
    
    # Initialize the failure detection service
    print("Initializing failure detection service...")
    failure_service = FailureDetectionService()
    print("✓ Service initialized")
    print()
    
    # Create a temporary output.xml file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp_file:
        temp_file.write(create_sample_output_xml())
        temp_xml_path = temp_file.name
    
    try:
        print(f"Created sample output.xml: {temp_xml_path}")
        print()
        
        # Analyze the execution result
        print("Analyzing test failures...")
        failures = failure_service.analyze_execution_result(temp_xml_path)
        
        print(f"Found {len(failures)} failure(s)")
        print()
        
        # Display each failure
        for i, failure in enumerate(failures, 1):
            print(f"Failure {i}:")
            print(f"  Test Case: {failure.test_case}")
            print(f"  Test File: {failure.test_file}")
            print(f"  Original Locator: {failure.original_locator}")
            print(f"  Failure Type: {failure.failure_type.name}")
            print(f"  Exception: {failure.exception_type}")
            print(f"  Target URL: {failure.target_url}")
            print(f"  Failing Step: {failure.failing_step}")
            print(f"  Timestamp: {failure.timestamp}")
            print()
        
        # Classify failures by type
        healable_failures = [f for f in failures if f.failure_type != FailureType.OTHER]
        non_healable_failures = [f for f in failures if f.failure_type == FailureType.OTHER]
        
        print("Failure Classification:")
        print(f"  Healable failures: {len(healable_failures)}")
        print(f"  Non-healable failures: {len(non_healable_failures)}")
        print()
        
        # Show failure types
        failure_types = {}
        for failure in failures:
            failure_type = failure.failure_type.name
            failure_types[failure_type] = failure_types.get(failure_type, 0) + 1
        
        print("Failure Types:")
        for failure_type, count in failure_types.items():
            print(f"  {failure_type}: {count}")
        print()
        
        # Show healing recommendations
        print("Healing Recommendations:")
        for i, failure in enumerate(healable_failures, 1):
            print(f"  {i}. {failure.original_locator} ({failure.failure_type.name})")
            if failure.failure_type == FailureType.ELEMENT_NOT_FOUND:
                print("     → Try alternative locator strategies (CSS, XPath, text-based)")
            elif failure.failure_type == FailureType.ELEMENT_NOT_INTERACTABLE:
                print("     → Wait for element to become interactable or scroll into view")
            elif failure.failure_type == FailureType.TIMEOUT:
                print("     → Increase timeout or use more specific wait conditions")
        print()
        
        print("🎉 Failure detection analysis completed!")
        print()
        print("Note: This demo shows how the failure detection service analyzes")
        print("Robot Framework output files and classifies failures for healing.")
        
    finally:
        # Clean up temporary file
        os.unlink(temp_xml_path)


if __name__ == "__main__":
    main()