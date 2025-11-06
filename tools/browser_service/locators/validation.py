"""
Locator Validation Module

This module validates locators using Playwright's built-in API methods.
It checks if a locator uniquely identifies an element on the page.

CRITICAL VALIDATION RULE:
- A locator is ONLY valid if count=1 (unique)
- If count>1, the locator matches multiple elements and is NOT usable
- If count=0, the locator doesn't match any elements

The validation process:
1. Count matches using Playwright's locator.count() method
2. Get element details for the first match
3. Check visibility using is_visible()
4. Verify coordinates match the expected element (if provided)
5. Return validation results with valid=True only if count==1

This is equivalent to testing a selector in the browser's F12 Console.
"""

from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


async def validate_locator_playwright(
    page,
    locator: str,
    expected_coords: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    """
    Validate a locator using Playwright's built-in methods.
    This is exactly like testing a selector in F12 Console.

    CRITICAL: Only locators with count=1 are marked as valid=True (unique locators only).

    Args:
        page: Playwright page object
        locator: The locator to validate (e.g., "id=search", "text=Login")
        expected_coords: Optional {x, y} to verify we found the right element

    Returns:
        Dictionary with validation results:
        - valid: True only if count==1 (unique locator)
        - unique: True if count==1
        - count: Number of elements matching the locator
        - validated: True if validation was performed
        - validation_method: Always "playwright"
        - is_visible: Whether the element is visible
        - correct_element: Whether coordinates match (if expected_coords provided)
        - element_info: Details about the element (tag, id, class, text, etc.)
        - bounding_box: Element's position and size
        - error: Error message if validation failed

    Example:
        >>> result = await validate_locator_playwright(page, "id=search-btn")
        >>> result['valid']  # True if exactly 1 match
        True
        >>> result['count']  # Number of matches
        1
    """
    try:
        # Step 1: Count matches (like F12: document.querySelectorAll().length)
        count = await page.locator(locator).count()

        if count == 0:
            return {
                'valid': False,
                'unique': False,
                'count': 0,
                'validated': True,
                'validation_method': 'playwright',
                'error': 'Locator does not match any elements'
            }

        # Step 2: Get first element details (like F12: inspect element)
        element_info = await page.locator(locator).first.evaluate("""
            (el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: el.className || null,
                text: el.textContent?.trim().slice(0, 100) || null,
                visible: el.offsetParent !== null,
                boundingBox: {
                    x: el.getBoundingClientRect().x,
                    y: el.getBoundingClientRect().y,
                    width: el.getBoundingClientRect().width,
                    height: el.getBoundingClientRect().height
                }
            })
        """)

        # Step 3: Check visibility (like F12: computed styles)
        is_visible = await page.locator(locator).first.is_visible()

        # Step 4: Get bounding box
        bounding_box = element_info['boundingBox']

        # Step 5: Verify it's the correct element (if coords provided)
        correct_element = True
        if expected_coords and bounding_box:
            # Check if expected coords are within the element's bounding box
            x_match = (bounding_box['x'] <= expected_coords['x'] <=
                       bounding_box['x'] + bounding_box['width'])
            y_match = (bounding_box['y'] <= expected_coords['y'] <=
                       bounding_box['y'] + bounding_box['height'])
            correct_element = x_match and y_match

        # CRITICAL: Only mark as valid if count == 1 (unique locator)
        return {
            'valid': count == 1,  # Only unique locators are valid
            'unique': count == 1,
            'count': count,
            'validated': True,
            'validation_method': 'playwright',
            'is_visible': is_visible,
            'correct_element': correct_element,
            'element_info': element_info,
            'bounding_box': bounding_box
        }

    except Exception as e:
        logger.error(f"Error validating locator '{locator}': {e}")
        return {
            'valid': False,
            'unique': False,
            'count': 0,
            'validated': False,
            'validation_method': 'playwright',
            'error': str(e)
        }
