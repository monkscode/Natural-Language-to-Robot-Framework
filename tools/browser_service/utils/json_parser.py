"""
JSON Parsing Utilities

This module provides robust JSON extraction functions for parsing element and workflow
data from browser automation results. It handles nested braces, escaped quotes, and
various JSON formatting patterns.

Functions:
    - extract_json_for_element: Extract JSON for a specific element ID
    - extract_workflow_json: Extract workflow completion JSON
"""

import json
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def extract_json_for_element(text: str, element_id: str) -> Optional[Dict[str, Any]]:
    """
    Extract JSON object for a specific element_id from text, handling nested braces properly.

    This function searches for JSON objects containing a specific element_id and extracts
    the complete JSON structure, handling nested braces and escaped quotes correctly.

    Args:
        text: The text containing JSON data
        element_id: The element ID to search for (e.g., "elem_1")

    Returns:
        Parsed JSON dict if found, None otherwise

    Example:
        >>> text = '{"element_id": "elem_1", "found": true, "locator": "id=search"}'
        >>> result = extract_json_for_element(text, "elem_1")
        >>> result['found']
        True
    """
    # Find the starting position of the element_id
    # Check multiple patterns (with and without space after colon)
    search_patterns = [
        f'"element_id":"{element_id}"',  # No space (common in minified JSON)
        f'"element_id": "{element_id}"',  # With space
        f"'element_id':'{element_id}'",  # Single quotes, no space
        f"'element_id': '{element_id}'"   # Single quotes, with space
    ]

    start_pos = -1
    pattern_used = None
    for pattern in search_patterns:
        pos = text.find(pattern)
        if pos != -1:
            start_pos = pos
            pattern_used = pattern
            break

    if start_pos == -1:
        logger.debug(
            f"extract_json_for_element: '{element_id}' not found in text (tried {len(search_patterns)} patterns)")
        return None

    logger.debug(
        f"extract_json_for_element: Found '{element_id}' at position {start_pos} using pattern '{pattern_used}'")

    # Find the opening brace before element_id
    brace_pos = text.rfind('{', 0, start_pos)
    if brace_pos == -1:
        logger.debug(
            f"extract_json_for_element: No opening brace found before '{element_id}'")
        return None

    logger.debug(
        f"extract_json_for_element: Opening brace at position {brace_pos}")

    # Now match braces to find the closing brace
    brace_count = 0
    in_string = False
    escape_next = False

    for i in range(brace_pos, len(text)):
        char = text[i]

        # Handle escape sequences
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        # Handle strings (ignore braces inside strings)
        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        # Count braces
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1

            # Found matching closing brace
            if brace_count == 0:
                json_str = text[brace_pos:i+1]
                logger.debug(
                    f"extract_json_for_element: Found complete JSON for '{element_id}' ({len(json_str)} chars)")

                # CRITICAL FIX: Unescape double-escaped quotes before parsing
                # The JavaScript returns valid JSON, but when embedded in Python strings,
                # quotes get double-escaped (\" becomes \\")
                # We need to fix this before json.loads()
                try:
                    # First attempt: Parse as-is
                    parsed = json.loads(json_str)
                    logger.debug(
                        f"extract_json_for_element: Successfully parsed JSON for '{element_id}'")
                    return parsed
                except json.JSONDecodeError:
                    # Second attempt: Fix escaped quotes and try again
                    logger.debug(
                        "extract_json_for_element: First parse failed, trying to fix escaped quotes...")
                    try:
                        # Replace double-escaped quotes with single-escaped quotes
                        # \\" -> \"
                        fixed_json_str = json_str.replace('\\\\"', '\\"')
                        # Also handle \\' -> \'
                        fixed_json_str = fixed_json_str.replace("\\\\'", "\\'")

                        parsed = json.loads(fixed_json_str)
                        logger.debug(
                            f"extract_json_for_element: Successfully parsed JSON after fixing escapes for '{element_id}'")
                        return parsed
                    except json.JSONDecodeError as e2:
                        logger.error(
                            f"extract_json_for_element: Failed to parse JSON for {element_id} even after fixing escapes: {e2}")
                        logger.error(
                            f"Original JSON (first 500 chars): {json_str[:500]}...")
                        logger.error(
                            f"Fixed JSON (first 500 chars): {fixed_json_str[:500]}...")
                        return None

    logger.debug(
        f"extract_json_for_element: No matching closing brace found for '{element_id}'")
    return None


def extract_workflow_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract workflow completion JSON from text, handling nested braces properly.

    This function searches for workflow completion JSON objects and extracts the complete
    structure, validating that it contains the expected fields.

    Args:
        text: The text containing workflow JSON data

    Returns:
        Parsed JSON dict if found and valid, None otherwise

    Example:
        >>> text = '{"workflow_completed": true, "results": [...]}'
        >>> result = extract_workflow_json(text)
        >>> result['workflow_completed']
        True
    """
    # Find the starting position of workflow_completed
    search_pattern = '"workflow_completed"'
    start_pos = text.find(search_pattern)

    if start_pos == -1:
        return None

    # Find the opening brace before workflow_completed
    brace_pos = text.rfind('{', 0, start_pos)
    if brace_pos == -1:
        return None

    # Now match braces to find the closing brace
    brace_count = 0
    in_string = False
    escape_next = False

    for i in range(brace_pos, len(text)):
        char = text[i]

        # Handle escape sequences
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        # Handle strings (ignore braces inside strings)
        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        # Count braces
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1

            # Found matching closing brace
            if brace_count == 0:
                json_str = text[brace_pos:i+1]
                try:
                    parsed = json.loads(json_str)
                    # Verify it has the expected structure
                    if 'workflow_completed' in parsed and 'results' in parsed:
                        return parsed
                except json.JSONDecodeError as e:
                    logger.debug(f"Failed to parse workflow JSON: {e}")
                    logger.debug(f"JSON string: {json_str[:200]}...")
                    return None

    return None
