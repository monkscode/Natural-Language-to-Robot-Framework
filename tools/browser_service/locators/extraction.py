"""
Element Attribute Extraction Module

This module provides the complete locator extraction and validation pipeline.
It combines element attribute extraction, locator generation, and Playwright
validation to find the best unique locator for a given element.

Pipeline:
1. Extract element attributes from coordinates using minimal JavaScript
2. Generate candidate locators from attributes (priority-ordered)
3. Validate each locator using Playwright API
4. Select the best unique locator (count=1, correct element)

This approach uses minimal JavaScript (< 50 lines) for attribute extraction,
then relies on Playwright's Python API for validation. This is much cleaner
and more maintainable than generating large JavaScript validation code.
"""

from typing import Dict, Any, Optional
import logging

from .generation import generate_locators_from_attributes
from .validation import validate_locator_playwright

logger = logging.getLogger(__name__)


async def extract_element_attributes(page, coords: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """
    Extract element attributes using minimal JavaScript.
    This is like inspecting an element in F12 DevTools.

    Args:
        page: Playwright page object
        coords: Dictionary with 'x' and 'y' coordinates

    Returns:
        Dictionary with element attributes or None if not found.
        Attributes include:
        - id: Element ID
        - name: Element name attribute
        - testId: data-testid attribute
        - ariaLabel: aria-label attribute
        - role: ARIA role
        - title: Title attribute
        - placeholder: Placeholder text
        - type: Input type
        - tagName: HTML tag name
        - className: CSS classes
        - text: Text content (first 100 chars)
        - href: Link href
        - visible: Whether element is visible
        - boundingBox: Element position and size

    Example:
        >>> attrs = await extract_element_attributes(page, {'x': 100, 'y': 200})
        >>> attrs['tagName']
        'button'
        >>> attrs['id']
        'submit-btn'
    """
    try:
        # Minimal JavaScript to get element attributes (< 50 lines)
        element_info = await page.evaluate("""
            (coords) => {
                const el = document.elementFromPoint(coords.x, coords.y);
                if (!el || el.tagName === 'HTML' || el.tagName === 'BODY') {
                    return null;
                }

                // Extract all useful attributes
                return {
                    // Primary identifiers (highest priority)
                    id: el.id || null,
                    name: el.name || null,
                    testId: el.dataset?.testid || null,

                    // Semantic attributes
                    ariaLabel: el.getAttribute('aria-label') || null,
                    role: el.getAttribute('role') || null,
                    title: el.title || null,
                    placeholder: el.placeholder || null,
                    type: el.type || null,

                    // Structure
                    tagName: el.tagName.toLowerCase(),
                    className: el.className || null,

                    // Content
                    text: el.textContent?.trim().slice(0, 100) || null,
                    href: el.href || null,

                    // Visibility
                    visible: el.offsetParent !== null,

                    // Position (for verification)
                    boundingBox: {
                        x: el.getBoundingClientRect().x,
                        y: el.getBoundingClientRect().y,
                        width: el.getBoundingClientRect().width,
                        height: el.getBoundingClientRect().height
                    }
                };
            }
        """, coords)

        return element_info

    except Exception as e:
        logger.error(f"Error extracting element attributes: {e}")
        return None


async def extract_and_validate_locators(
    page,
    element_description: str,
    element_coords: Dict[str, float],
    library_type: str = "browser"
) -> Dict[str, Any]:
    """
    Complete locator extraction and validation pipeline.
    Uses Playwright's built-in methods - no massive JavaScript!

    CRITICAL: Only returns locators with count=1 as valid (unique locators only).

    Args:
        page: Playwright page object
        element_description: Description of the element (for logging)
        element_coords: {x, y} coordinates from browser-use vision
        library_type: "browser" or "selenium"

    Returns:
        Dictionary with extraction results:
        - found: Whether a valid locator was found
        - best_locator: The best unique locator string
        - all_locators: List of all validated locators
        - unique_locators: List of unique locators (count=1)
        - element_info: Element attributes
        - validation_summary: Summary of validation results
        - validated: Whether validation was performed
        - count: Count for best locator (1 if unique)
        - unique: Whether best locator is unique
        - valid: Whether best locator is valid
        - validation_method: Always "playwright"
        - error: Error message if extraction failed

    Example:
        >>> result = await extract_and_validate_locators(
        ...     page, "Search button", {'x': 100, 'y': 200}, "browser"
        ... )
        >>> result['found']
        True
        >>> result['best_locator']
        'id=search-btn'
        >>> result['unique']
        True
    """
    logger.info(f"🔍 Extracting locators for: {element_description}")
    logger.info(
        f"   Coordinates: ({element_coords['x']}, {element_coords['y']})")

    # Step 1: Extract element attributes using minimal JavaScript
    element_attrs = await extract_element_attributes(page, element_coords)

    if not element_attrs:
        logger.error("❌ Could not find element at coordinates")
        return {
            'found': False,
            'error': 'Element not found at coordinates'
        }

    logger.info(
        f"   Found element: <{element_attrs['tagName']}> \"{element_attrs.get('text', '')[:50]}\"")

    # Step 2: Generate locators from attributes (in Python, not JavaScript!)
    locators = generate_locators_from_attributes(element_attrs, library_type)

    if not locators:
        logger.warning(
            "⚠️ No locators could be generated from element attributes")
        return {
            'found': False,
            'error': 'No locators could be generated',
            'element_info': element_attrs
        }

    logger.info(f"   Generated {len(locators)} candidate locators")

    # Step 3: Validate each locator using Playwright validation
    validated_locators = []
    for loc in locators:
        validation = await validate_locator_playwright(
            page,
            loc['locator'],
            element_coords
        )

        # Merge validation results into locator dict
        loc.update(validation)

        if loc.get('validated'):
            validated_locators.append(loc)
            if loc.get('valid'):
                # valid=True means count=1 (unique)
                status = "✅ UNIQUE"
            else:
                # valid=False means count>1 or count=0
                count = loc.get('count', 0)
                if count > 1:
                    status = f"⚠️ NOT UNIQUE ({count} matches)"
                else:
                    status = "❌ NOT FOUND"
            correct = "✅" if loc.get(
                'correct_element') else "⚠️ Different element"
            logger.info(
                f"   {loc['type']}: {loc['locator']} → {status}, {correct}")
        else:
            logger.warning(f"   ❌ {loc['type']}: {loc['locator']} → VALIDATION FAILED")

    # Step 4: Filter and sort - ONLY unique locators (count=1) are considered valid
    unique_locators = [loc for loc in validated_locators if loc.get(
        'unique') and loc.get('correct_element')]
    valid_locators = [loc for loc in validated_locators if loc.get('valid')]

    # Step 5: Select best locator
    best_locator = None
    if unique_locators:
        # Prefer unique locators that match the correct element, sorted by priority
        best_locator = sorted(unique_locators, key=lambda x: x['priority'])[0]
        logger.info(
            f"✅ Best locator: {best_locator['locator']} (unique, correct element)")
    elif valid_locators:
        # Fallback to any valid locator
        best_locator = sorted(valid_locators, key=lambda x: x['priority'])[0]
        logger.warning(
            f"⚠️ Best locator: {best_locator['locator']} (valid but not unique or wrong element)")

    result = {
        'found': best_locator is not None,
        'best_locator': best_locator['locator'] if best_locator else None,
        'all_locators': validated_locators,
        'unique_locators': unique_locators,
        'element_info': element_attrs,
        'validation_summary': {
            'total_generated': len(locators),
            'valid': len(valid_locators),
            'unique': len(unique_locators),
            'best_type': best_locator['type'] if best_locator else None,
            'validation_method': 'playwright'
        }
    }

    # Add validation data to the result itself for easy access
    if best_locator:
        result['validated'] = True
        result['count'] = best_locator.get('count', 1)
        result['unique'] = best_locator.get('unique', True)
        result['valid'] = best_locator.get('valid', True)
        result['validation_method'] = 'playwright'
    else:
        result['validated'] = True  # Validation was attempted
        result['count'] = 0  # No unique locator found
        result['unique'] = False
        result['valid'] = False
        result['validation_method'] = 'playwright'

    return result
