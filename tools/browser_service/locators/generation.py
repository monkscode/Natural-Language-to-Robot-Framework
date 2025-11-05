"""
Locator Generation Module

This module generates locators from element attributes using a priority-based strategy.
Locators are generated in order of stability and reliability, with ID being the most
preferred and CSS class being the least preferred.

Priority Order:
1. id - Most stable, fastest (e.g., "id=search-button")
2. data-testid - Designed for testing (e.g., "data-testid=login-form")
3. name - Semantic, stable (e.g., "name=username")
4. aria-label - Accessibility, semantic (e.g., "[aria-label='Search']")
5. text - Content-based, can be fragile (e.g., "text=Login")
6. role - Playwright-specific, semantic (e.g., "role=button[name='Submit']")
7. css-class - Lower priority, can change (e.g., "button.primary")

The module supports both Browser Library (Playwright) and SeleniumLibrary syntax.
"""

from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)


def generate_locators_from_attributes(
    element_attrs: Dict[str, Any],
    library_type: str = "browser"
) -> List[Dict[str, Any]]:
    """
    Generate locators from element attributes in priority order.

    This function creates a list of potential locators based on the element's
    attributes, ordered by stability and reliability. Each locator includes
    its type, the locator string, and a priority value.

    Args:
        element_attrs: Dictionary of element attributes extracted from the DOM.
                      Expected keys: id, testId, name, ariaLabel, role, text,
                      className, tagName, etc.
        library_type: Target library type - "browser" for Browser Library (Playwright)
                     or "selenium" for SeleniumLibrary. Default is "browser".

    Returns:
        List of locator dictionaries, each containing:
        - type: Locator type (e.g., "id", "data-testid", "name")
        - locator: The locator string (e.g., "id=search-button")
        - priority: Integer priority (1 = highest, 7 = lowest)

    Example:
        >>> attrs = {'id': 'search-btn', 'name': 'search', 'text': 'Search'}
        >>> locators = generate_locators_from_attributes(attrs, "browser")
        >>> locators[0]
        {'type': 'id', 'locator': 'id=search-btn', 'priority': 1}
    """
    locators = []

    # Priority 1: ID (most stable, fastest)
    if element_attrs.get('id'):
        locators.append({
            'type': 'id',
            'locator': f"id={element_attrs['id']}",
            'priority': 1
        })

    # Priority 2: data-testid (designed for testing)
    if element_attrs.get('testId'):
        if library_type == "browser":
            locators.append({
                'type': 'data-testid',
                'locator': f"data-testid={element_attrs['testId']}",
                'priority': 2
            })
        else:  # selenium
            locators.append({
                'type': 'data-testid',
                'locator': f"css=[data-testid=\"{element_attrs['testId']}\"]",
                'priority': 2
            })

    # Priority 3: name (semantic, stable)
    if element_attrs.get('name'):
        if library_type == "browser":
            # Browser Library (Playwright) doesn't support name= prefix
            # Must use attribute selector
            locators.append({
                'type': 'name',
                'locator': f"[name=\"{element_attrs['name']}\"]",
                'priority': 3
            })
        else:  # selenium
            # SeleniumLibrary supports name= prefix
            locators.append({
                'type': 'name',
                'locator': f"name={element_attrs['name']}",
                'priority': 3
            })

    # Priority 4: aria-label (accessibility, semantic)
    if element_attrs.get('ariaLabel'):
        if library_type == "browser":
            locators.append({
                'type': 'aria-label',
                'locator': f"[aria-label=\"{element_attrs['ariaLabel']}\"]",
                'priority': 4
            })
        else:  # selenium
            locators.append({
                'type': 'aria-label',
                'locator': f"css=[aria-label=\"{element_attrs['ariaLabel']}\"]",
                'priority': 4
            })

    # Priority 5: text content (can be fragile if text changes)
    if element_attrs.get('text') and len(element_attrs['text']) > 0:
        text = element_attrs['text'][:50]  # First 50 chars
        if library_type == "browser":
            locators.append({
                'type': 'text',
                'locator': f"text={text}",
                'priority': 5
            })
        else:  # selenium
            # Selenium uses XPath for text
            locators.append({
                'type': 'text',
                'locator': f"xpath=//*[contains(text(), \"{text}\")]",
                'priority': 5
            })

    # Priority 6: role (Playwright-specific, semantic)
    if library_type == "browser" and element_attrs.get('role'):
        role = element_attrs['role']
        name = element_attrs.get(
            'ariaLabel') or element_attrs.get('text', '')[:30]
        if name:
            locators.append({
                'type': 'role',
                'locator': f"role={role}[name=\"{name}\"]",
                'priority': 6
            })

    # Priority 7: CSS class (lower priority, can change)
    if element_attrs.get('className'):
        first_class = element_attrs['className'].split(
        )[0] if element_attrs['className'] else None
        if first_class:
            tag = element_attrs.get('tagName', 'div')
            if library_type == "browser":
                locators.append({
                    'type': 'css-class',
                    'locator': f"{tag}.{first_class}",
                    'priority': 7
                })
            else:  # selenium
                locators.append({
                    'type': 'css-class',
                    'locator': f"css={tag}.{first_class}",
                    'priority': 7
                })

    return locators
