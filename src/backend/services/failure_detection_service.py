"""
Failure Detection Service for Test Self-Healing System.

This service analyzes Robot Framework test execution results to identify
failures that can be automatically healed through locator replacement.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse

from ..core.models import FailureContext, FailureType


class FailureDetectionService:
    """Service for detecting and analyzing test failures that can be healed."""
    
    # Selenium exception patterns that indicate locator-related failures
    LOCATOR_EXCEPTION_PATTERNS = {
        FailureType.ELEMENT_NOT_FOUND: [
            r"NoSuchElementException",
            r"Unable to locate element",
            r"Element not found",
            r"Could not find element",
            r"Element .* not found",
            r"No element found"
        ],
        FailureType.ELEMENT_NOT_INTERACTABLE: [
            r"ElementNotInteractableException",
            r"Element is not interactable",
            r"Element .* is not clickable",
            r"Element not clickable",
            r"Element not visible"
        ],
        FailureType.TIMEOUT: [
            r"TimeoutException",
            r"Timed out waiting for element",
            r"Element .* timed out",
            r"Wait timeout exceeded"
        ],
        FailureType.STALE_ELEMENT: [
            r"StaleElementReferenceException",
            r"Element is no longer attached",
            r"Stale element reference"
        ]
    }
    
    # Locator extraction patterns for different Robot Framework keywords
    LOCATOR_PATTERNS = [
        r"locator[=:\s]+['\"]([^'\"]+)['\"]",
        r"Click Element\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|$)",
        r"Input Text\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|\s+[^\s]+\s*$)",
        r"Wait Until Element Is Visible\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|$)",
        r"Element Should Be Visible\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|$)",
        r"Get Text\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|$)",
        r"Element Should Contain\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|\s+[^\s]+\s*$)",
        r"Click Button\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|$)",
        r"Select From List By Label\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|\s+[^\s]+\s*$)",
        r"Clear Element Text\s+([^\s]+(?:\s[^\s]+)*?)(?:\s{2,}|$)"
    ]
    
    # URL extraction patterns
    URL_PATTERNS = [
        r"Opening browser .* to base url ['\"]([^'\"]+)['\"]",
        r"Go To\s+([^\s]+)",
        r"Navigate to ['\"]([^'\"]+)['\"]",
        r"Current URL is ['\"]([^'\"]+)['\"]"
    ]

    def __init__(self):
        """Initialize the failure detection service."""
        pass

    def analyze_execution_result(self, output_xml_path: str, log_content: Optional[str] = None) -> List[FailureContext]:
        """
        Analyze Robot Framework execution results and extract healable failures.
        
        Args:
            output_xml_path: Path to Robot Framework output.xml file
            log_content: Optional log content for additional context
            
        Returns:
            List of FailureContext objects for healable failures
        """
        failures = []
        
        logging.info(f"🔍 FAILURE DETECTION: Starting analysis of {output_xml_path}")
        
        try:
            tree = ET.parse(output_xml_path)
            root = tree.getroot()
            
            # Extract run ID from the robot element
            run_id = root.get('generated', datetime.now().isoformat())
            logging.info(f"📊 FAILURE DETECTION: Analyzing run ID: {run_id}")
            
            # Find all failed test cases
            for suite in root.findall('.//suite'):
                suite_name = suite.get('name', 'Unknown Suite')
                suite_source = suite.get('source', '')
                logging.info(f"📁 FAILURE DETECTION: Analyzing suite: {suite_name}")
                
                for test in suite.findall('.//test'):
                    test_name = test.get('name', 'Unknown Test')
                    
                    # Check if test failed
                    status = test.find('status')
                    if status is not None and status.get('status') == 'FAIL':
                        logging.info(f"❌ FAILURE DETECTION: Found failed test: {test_name}")
                        failure_context = self._extract_failure_context(
                            test, suite_name, test_name, suite_source, run_id, log_content
                        )
                        if failure_context:
                            logging.info(f"✅ FAILURE DETECTION: Created failure context for {failure_context.original_locator} - {failure_context.failure_type}")
                            failures.append(failure_context)
                        else:
                            logging.warning(f"⚠️  FAILURE DETECTION: Could not create failure context for {test_name}")
            
        except ET.ParseError as e:
            logging.error(f"❌ FAILURE DETECTION: Failed to parse output.xml: {e}")
            raise ValueError(f"Failed to parse output.xml: {e}")
        except FileNotFoundError:
            logging.error(f"❌ FAILURE DETECTION: Output file not found: {output_xml_path}")
            raise FileNotFoundError(f"Output file not found: {output_xml_path}")
        
        logging.info(f"🎯 FAILURE DETECTION: Analysis complete. Found {len(failures)} failures")
        for i, failure in enumerate(failures):
            logging.info(f"   {i+1}. {failure.original_locator} ({failure.failure_type.name}) - {failure.exception_type}")
        
        return failures

    def is_locator_failure(self, exception_details: str) -> Tuple[bool, FailureType]:
        """
        Determine if a failure is related to element locators.
        
        Args:
            exception_details: Exception message and stack trace
            
        Returns:
            Tuple of (is_locator_failure, failure_type)
        """
        logging.info(f"🔍 FAILURE DETECTION: Analyzing exception: {exception_details[:200]}...")
        
        for failure_type, patterns in self.LOCATOR_EXCEPTION_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, exception_details, re.IGNORECASE):
                    logging.info(f"✅ FAILURE DETECTION: Matched pattern '{pattern}' for failure type: {failure_type}")
                    return True, failure_type
        
        logging.warning(f"⚠️  FAILURE DETECTION: No locator failure pattern matched, classifying as OTHER")
        return False, FailureType.OTHER

    def extract_failure_context(self, logs: str, test_file: str = "", test_case: str = "") -> Optional[FailureContext]:
        """
        Extract failure context from test logs.
        
        Args:
            logs: Test execution logs
            test_file: Path to the test file
            test_case: Name of the test case
            
        Returns:
            FailureContext if a healable failure is found, None otherwise
        """
        # Extract failing locator
        locator = self._extract_locator_from_logs(logs)
        if not locator:
            return None
        
        # Extract target URL
        url = self._extract_url_from_logs(logs)
        
        # Extract exception details
        exception_type, exception_message = self._extract_exception_details(logs)
        
        # Check if this is a locator-related failure
        is_healable, failure_type = self.is_locator_failure(exception_message)
        if not is_healable:
            return None
        
        # Extract failing step
        failing_step = self._extract_failing_step(logs)
        
        return FailureContext(
            test_file=test_file,
            test_case=test_case,
            failing_step=failing_step,
            original_locator=locator,
            target_url=url or "",
            exception_type=exception_type,
            exception_message=exception_message,
            timestamp=datetime.now(),
            run_id="",  # Will be set by caller
            failure_type=failure_type
        )

    def _extract_failure_context(self, test_element: ET.Element, suite_name: str, 
                                test_name: str, suite_source: str, run_id: str,
                                log_content: Optional[str] = None) -> Optional[FailureContext]:
        """Extract failure context from a failed test element."""
        # Get failure message from status
        status = test_element.find('status')
        if status is None:
            return None
        
        failure_message = status.text or ""
        
        # Check if this is a locator-related failure
        is_healable, failure_type = self.is_locator_failure(failure_message)
        if not is_healable:
            return None
        
        # Extract additional context from keywords
        failing_step = ""
        locator = ""
        url = ""
        
        # Look through all keywords to find the failing one and extract context
        for kw in test_element.findall('.//kw'):
            kw_status = kw.find('status')
            if kw_status is not None and kw_status.get('status') == 'FAIL':
                failing_step = kw.get('name', '')
                
                # Extract locator from keyword arguments - first arg is usually the locator
                args = kw.findall('arg')
                if args and args[0].text:
                    # For most Selenium keywords, the first argument is the locator
                    locator = args[0].text.strip()
                
                # Extract URL from messages
                for msg in kw.findall('msg'):
                    if msg.text:
                        potential_url = self._extract_url_from_text(msg.text)
                        if potential_url:
                            url = potential_url
                
                break
        
        # If no locator found in keywords, try to extract from failure message
        if not locator:
            locator = self._extract_locator_from_text(failure_message) or "unknown"
        
        # Extract exception type and message
        exception_type, exception_message = self._extract_exception_details(failure_message)
        
        return FailureContext(
            test_file=suite_source,
            test_case=test_name,
            failing_step=failing_step,
            original_locator=locator,
            target_url=url,
            exception_type=exception_type,
            exception_message=exception_message,
            timestamp=datetime.now(),
            run_id=run_id,
            failure_type=failure_type
        )

    def _extract_locator_from_logs(self, logs: str) -> Optional[str]:
        """Extract locator from log content."""
        return self._extract_locator_from_text(logs)

    def _extract_locator_from_text(self, text: str) -> Optional[str]:
        """Extract locator from text using various patterns."""
        for pattern in self.LOCATOR_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _extract_url_from_logs(self, logs: str) -> Optional[str]:
        """Extract URL from log content."""
        return self._extract_url_from_text(logs)

    def _extract_url_from_text(self, text: str) -> Optional[str]:
        """Extract URL from text using various patterns."""
        for pattern in self.URL_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                url = match.group(1).strip()
                # Validate URL format
                try:
                    parsed = urlparse(url)
                    if parsed.scheme and parsed.netloc:
                        return url
                except:
                    continue
        return None

    def _extract_exception_details(self, message: str) -> Tuple[str, str]:
        """Extract exception type and message from failure text."""
        # Look for exception class names
        exception_match = re.search(r'(\w*Exception):\s*(.*?)(?:\n|$)', message, re.IGNORECASE)
        if exception_match:
            return exception_match.group(1), exception_match.group(2).strip()
        
        # Look for other error patterns
        error_match = re.search(r'(Error|Failed):\s*(.*?)(?:\n|$)', message, re.IGNORECASE)
        if error_match:
            return error_match.group(1), error_match.group(2).strip()
        
        # Default to generic error
        return "UnknownException", message.strip()

    def _extract_failing_step(self, logs: str) -> str:
        """Extract the failing test step from logs."""
        # Look for Robot Framework keyword patterns
        step_patterns = [
            r'FAIL\s*:\s*(.*?)(?:\n|$)',
            r'Keyword\s+[\'"]([^\'"]+)[\'"].*failed',
            r'Step\s+[\'"]([^\'"]+)[\'"].*failed'
        ]
        
        for pattern in step_patterns:
            match = re.search(pattern, logs, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        
        return "Unknown step"

    def get_failure_statistics(self, failures: List[FailureContext]) -> Dict[str, Any]:
        """
        Generate statistics about detected failures.
        
        Args:
            failures: List of detected failures
            
        Returns:
            Dictionary containing failure statistics
        """
        if not failures:
            return {
                "total_failures": 0,
                "healable_failures": 0,
                "failure_types": {},
                "most_common_locators": {},
                "most_common_urls": {}
            }
        
        # Count failure types
        failure_type_counts = {}
        for failure in failures:
            failure_type = failure.failure_type.value
            failure_type_counts[failure_type] = failure_type_counts.get(failure_type, 0) + 1
        
        # Count most common locators
        locator_counts = {}
        for failure in failures:
            locator = failure.original_locator
            locator_counts[locator] = locator_counts.get(locator, 0) + 1
        
        # Count most common URLs
        url_counts = {}
        for failure in failures:
            if failure.target_url:
                url_counts[failure.target_url] = url_counts.get(failure.target_url, 0) + 1
        
        return {
            "total_failures": len(failures),
            "healable_failures": len(failures),
            "failure_types": failure_type_counts,
            "most_common_locators": dict(sorted(locator_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
            "most_common_urls": dict(sorted(url_counts.items(), key=lambda x: x[1], reverse=True)[:10])
        }