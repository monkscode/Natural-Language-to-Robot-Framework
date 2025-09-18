#!/usr/bin/env python3
"""
Docker test utilities and helpers for debugging.
"""

import logging
import tempfile
import uuid
import os
import xml.etree.ElementTree as ET


def create_sample_robot_test(run_id: str):
    """Create a sample Robot Framework test file for testing."""
    robot_content = """*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Sample Test
    [Documentation]    A simple test that will fail to demonstrate log extraction
    Open Browser    https://www.google.com    chrome    options=add_argument("--headless")
    Click Element    xpath://button[@id='nonexistent']    # This will fail
    Close Browser
"""
    
    # Create test directory structure
    test_dir = os.path.join("robot_tests", run_id)
    os.makedirs(test_dir, exist_ok=True)
    
    # Write test file
    test_file = os.path.join(test_dir, "test.robot")
    with open(test_file, 'w') as f:
        f.write(robot_content)
    
    return test_file


def create_sample_output_files(run_id: str):
    """Create sample Robot Framework output files for testing."""
    test_dir = os.path.join("robot_tests", run_id)
    
    # Create output.xml
    output_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<robot generator="Robot 7.3.2" generated="2025-01-01T12:00:00.000000">
    <suite id="s1" name="Sample Test Suite" source="{test_dir}/test.robot">
        <test id="s1-t1" name="Sample Test" line="5">
            <kw name="Open Browser" library="SeleniumLibrary">
                <arg>https://www.google.com</arg>
                <arg>chrome</arg>
                <status status="PASS" starttime="2025-01-01T12:00:01.000000" endtime="2025-01-01T12:00:02.000000"/>
            </kw>
            <kw name="Click Element" library="SeleniumLibrary">
                <arg>xpath://button[@id='nonexistent']</arg>
                <status status="FAIL" starttime="2025-01-01T12:00:02.000000" endtime="2025-01-01T12:00:03.000000">
                    NoSuchElementException: Unable to locate element with xpath: //button[@id='nonexistent']
                </status>
            </kw>
            <status status="FAIL" starttime="2025-01-01T12:00:01.000000" endtime="2025-01-01T12:00:03.000000">
                NoSuchElementException: Unable to locate element with xpath: //button[@id='nonexistent']
            </status>
        </test>
        <status status="FAIL" starttime="2025-01-01T12:00:00.000000" endtime="2025-01-01T12:00:04.000000"/>
    </suite>
    <statistics>
        <total>
            <stat pass="0" fail="1" skip="0">All Tests</stat>
        </total>
    </statistics>
</robot>"""
    
    # Create log.html
    log_html = """<!DOCTYPE html>
<html>
<head><title>Robot Framework Log</title></head>
<body>
<h1>Test Execution Log</h1>
<p>Sample Test: FAIL</p>
<p>Error: NoSuchElementException: Unable to locate element with xpath: //button[@id='nonexistent']</p>
</body>
</html>"""
    
    # Write files
    with open(os.path.join(test_dir, "output.xml"), 'w') as f:
        f.write(output_xml)
    
    with open(os.path.join(test_dir, "log.html"), 'w') as f:
        f.write(log_html)
    
    return os.path.join(test_dir, "output.xml"), os.path.join(test_dir, "log.html")


def create_test_output_xml(test_status="FAIL", failure_message="", keyword_name="Click Element", 
                          keyword_args=None, keyword_status="FAIL"):
    """Helper method to create test output.xml content for testing."""
    if keyword_args is None:
        keyword_args = ["xpath://button[@id='submit']"]
    
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<robot generator="Robot 7.3.2" generated="2025-01-01T12:00:00.000000">
    <suite id="s1" name="Test Suite" source="/app/robot_tests/test.robot">
        <test id="s1-t1" name="Test Case" line="5">
            <kw name="{keyword_name}" library="SeleniumLibrary">
                {' '.join([f'<arg>{arg}</arg>' for arg in keyword_args])}
                <status status="{keyword_status}" starttime="2025-01-01T12:00:01.000000" endtime="2025-01-01T12:00:02.000000">
                    {failure_message if keyword_status == "FAIL" else ""}
                </status>
            </kw>
            <status status="{test_status}" starttime="2025-01-01T12:00:01.000000" endtime="2025-01-01T12:00:02.000000">
                {failure_message if test_status == "FAIL" else ""}
            </status>
        </test>
        <status status="{test_status}" starttime="2025-01-01T12:00:00.000000" endtime="2025-01-01T12:00:03.000000"/>
    </suite>
    <statistics>
        <total>
            <stat pass="{'0' if test_status == 'FAIL' else '1'}" fail="{'1' if test_status == 'FAIL' else '0'}" skip="0">All Tests</stat>
        </total>
    </statistics>
</robot>"""


def setup_test_logging():
    """Set up detailed logging for tests."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )


def cleanup_test_files(run_id: str):
    """Clean up test files after testing."""
    import shutil
    test_dir = os.path.join("robot_tests", run_id)
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
        logging.info(f"Cleaned up test directory: {test_dir}")