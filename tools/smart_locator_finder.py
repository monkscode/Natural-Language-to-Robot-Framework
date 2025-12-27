"""
Smart Locator Finder
====================

Deterministic locator extraction using multiple strategies.
Given coordinates, systematically tries different approaches to find unique locators.
"""

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Configuration Constants
# These values control the behavior of locator finding strategies

# Text validation thresholds
MIN_TEXT_LENGTH = 2  # Minimum text length to use for text-based locators
MAX_TEXT_DISPLAY_LENGTH = 50  # Maximum text length to display in logs (for actual text)
MAX_TEXT_CONTENT_LENGTH = 100  # Maximum text content to extract from elements

# Text comparison thresholds  
INNER_TEXT_PREFERENCE_THRESHOLD = 1.0  # Use inner_text if shorter (more relevant); 1.0 = always prefer if shorter

# Checkbox label matching
MAX_CHECKBOX_LABEL_LENGTH = 30  # Maximum label length for checkbox/radio detection heuristic

# Locator priorities (lower = better)
PRIORITY_CANDIDATE = 0  # Agent-provided candidate locators
PRIORITY_ID = 1  # Native ID attribute
PRIORITY_TEST_ID = 2  # data-testid, data-test, data-qa
PRIORITY_NAME = 3  # name attribute
PRIORITY_ARIA_LABEL = 4  # aria-label
PRIORITY_PLACEHOLDER = 5  # placeholder, title
PRIORITY_TEXT = 6  # Visible text content
PRIORITY_ROLE = 7  # ARIA role with name
PRIORITY_CSS_PARENT_ID = 8  # CSS with parent ID context
PRIORITY_CSS_NTH_CHILD = 9  # CSS with nth-child
PRIORITY_CSS_CLASS = 10  # Simple CSS class
PRIORITY_XPATH_PARENT_ID = 11  # XPath with parent ID
PRIORITY_XPATH_PARENT_CLASS = 12  # XPath with parent class and position
PRIORITY_XPATH_TEXT = 13  # XPath with text content
PRIORITY_XPATH_TITLE = 14  # XPath with title
PRIORITY_XPATH_HREF = 15  # XPath with href (for links)
PRIORITY_XPATH_CLASS_POSITION = 16  # XPath with class and position
PRIORITY_XPATH_MULTI_ATTR = 17  # XPath with multiple attributes
PRIORITY_XPATH_FIRST_OF_CLASS = 18  # XPath - first element with class


# ========================================
# SHADOW DOM SUPPORT
# ========================================
# These JavaScript snippets handle Shadow DOM traversal for element detection.
# The helper function is defined once and embedded into each snippet to avoid
# duplication while maintaining self-contained JavaScript execution.

# Shared Shadow DOM traversal helper - embedded into each JS snippet
# This function recursively pierces through shadow roots to find the actual
# element at coordinates. Supports Material UI, Salesforce Lightning, etc.
_SHADOW_DOM_HELPER_JS = """
    function getElementFromPointWithShadow(root, x, y) {
        let element = root.elementFromPoint(x, y);
        if (!element) return null;
        
        // Recursively traverse through shadow roots
        while (element && element.shadowRoot) {
            const shadowElement = element.shadowRoot.elementFromPoint(x, y);
            if (shadowElement && shadowElement !== element) {
                element = shadowElement;
            } else {
                break;
            }
        }
        return element;
    }
"""

# Check if element exists at coordinates (returns boolean)
SHADOW_DOM_ELEMENT_FROM_POINT_JS = f"""
(args) => {{
    const {{x, y}} = args;
    {_SHADOW_DOM_HELPER_JS}
    return getElementFromPointWithShadow(document, x, y) ? true : false;
}}
"""

# Get tag name of element at coordinates (returns string or null)
SHADOW_DOM_TAG_NAME_JS = f"""
(args) => {{
    const {{x, y}} = args;
    {_SHADOW_DOM_HELPER_JS}
    const el = getElementFromPointWithShadow(document, x, y);
    return el ? el.tagName.toLowerCase() : null;
}}
"""

# Get full element data at coordinates (returns object or null)
SHADOW_DOM_ELEMENT_DATA_JS = f"""
(args) => {{
    const {{x, y}} = args;
    {_SHADOW_DOM_HELPER_JS}
    const el = getElementFromPointWithShadow(document, x, y);
    if (!el) return null;
    
    // Get text content, preferring innerText (visible text) over textContent
    let textContent = '';
    try {{
        textContent = (el.innerText || el.textContent || '').trim().substring(0, 100);
    }} catch (e) {{
        textContent = '';
    }}
    
    // Get element's bounding rect for coordinates
    const rect = el.getBoundingClientRect();
    
    return {{
        tagName: el.tagName.toLowerCase(),
        id: el.id || '',
        className: el.className || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        ariaLabel: el.getAttribute('aria-label') || '',
        dataTestId: el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || '',
        href: el.getAttribute('href') || '',
        type: el.getAttribute('type') || '',
        role: el.getAttribute('role') || '',
        title: el.getAttribute('title') || '',
        textContent: textContent,
        isInShadowDom: el.getRootNode() !== document,
        parentTagName: el.parentElement ? el.parentElement.tagName.toLowerCase() : '',
        parentId: el.parentElement ? (el.parentElement.id || '') : '',
        parentClassName: el.parentElement ? (el.parentElement.className || '') : '',
        coordinates: {{
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2,
            width: rect.width,
            height: rect.height,
            top: rect.top,
            left: rect.left,
            right: rect.right,
            bottom: rect.bottom
        }}
    }};
}}
"""


async def _validate_semantic_match(page, locator: str, expected_text: str) -> tuple[bool, str]:
    """
    Validate that the element found by the locator contains the expected text.
    
    This is the KEY validation that prevents "unique but wrong element" bugs.
    We check if the actual element text contains the expected text (case-insensitive).
    
    Args:
        page: Playwright page object
        locator: The locator string to validate
        expected_text: The text AI expects to see on the element
        
    Returns:
        Tuple of (is_match: bool, actual_text: str)
        - is_match: True if expected_text is found in actual text (case-insensitive)
        - actual_text: The actual text content of the element
    """
    if not expected_text:
        return True, ""  # No expected text means no validation needed
    
    try:
        element = page.locator(locator)
        count = await element.count()
        
        if count != 1:
            return False, f"[Element count={count}, expected 1]"
        
        # Get the actual text content
        actual_text = await element.text_content() or ""
        actual_text = actual_text.strip()
        
        # Also try inner_text which may be more accurate for visible text
        try:
            inner_text = await element.inner_text() or ""
            inner_text = inner_text.strip()
            # Use inner_text if it's shorter (usually more relevant)
            if inner_text and len(inner_text) < len(actual_text) * INNER_TEXT_PREFERENCE_THRESHOLD:
                actual_text = inner_text
        except Exception:
            pass
        
        # Check for placeholder/value for inputs
        try:
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            if tag == 'input':
                placeholder = await element.get_attribute('placeholder') or ""
                value = await element.get_attribute('value') or ""
                # For inputs, check placeholder or value as well
                if placeholder and expected_text.lower() in placeholder.lower():
                    return True, placeholder
                if value and expected_text.lower() in value.lower():
                    return True, value
        except Exception:
            pass
        
        # Case-insensitive substring match
        expected_lower = expected_text.lower().strip()
        actual_lower = actual_text.lower()
        
        is_match = expected_lower in actual_lower
        
        if is_match:
            logger.info(f"   ✅ Semantic match: expected '{expected_text}' found in '{actual_text[:MAX_TEXT_DISPLAY_LENGTH]}...'")
        else:
            logger.warning(f"   ❌ Semantic MISMATCH: expected '{expected_text}', got '{actual_text[:MAX_TEXT_CONTENT_LENGTH]}'")
        
        return is_match, actual_text
        
    except Exception as e:
        logger.warning(f"   ⚠️ Semantic validation error: {e}")
        return False, f"[Error: {e}]"


# ========================================
# MULTI-ELEMENT COLLECTION DETECTION
# ========================================
# These functions detect when an element is part of a repeatable collection
# (table rows, list items, cards, etc.) and generate multi-element locators.

def _is_collection_element(element_data: dict, element_description: str) -> bool:
    """
    Detect if element is part of a repeatable collection.
    Works generically without hardcoding specific library classes.
    
    Detection methods:
    1. Semantic description keywords (rows, items, all, each)
    2. Standard HTML collection tags (tr, li, option)
    3. Common class patterns (row, item, card, entry)
    
    Args:
        element_data: Dict with element attributes from browser-use DOM
        element_description: Human-readable description from planner
        
    Returns:
        True if element appears to be part of a collection
    """
    tag = element_data.get('tagName', '').lower()
    class_name = element_data.get('className', '').lower()
    desc = element_description.lower()
    
    # Method 1: Semantic description keywords
    # Note: LLM may modify descriptions, so we check for various patterns
    desc_keywords = [
        'rows', 'items', 'all ', 'each', 'every', 'list of', 
        'visible rows', 'table rows', 'filtered',
        'cells', 'column cell', 'column cells',  # Table column patterns
        'results table', 'data table',  # Table context patterns
    ]
    if any(kw in desc for kw in desc_keywords):
        logger.info(f"   Collection detected via description keywords in: '{element_description}'")
        return True
    
    # Method 2: Standard HTML collection tags
    if tag in ['tr', 'li', 'option', 'dt', 'dd']:
        logger.info(f"   Collection detected via HTML tag: <{tag}>")
        return True
    
    # Method 3: Common class patterns (generic, not library-specific)
    collection_patterns = ['row', 'item', 'card', 'entry', 'record', 'tr-group', 'list-item', 'grid-item']
    if any(pattern in class_name for pattern in collection_patterns):
        logger.info(f"   Collection detected via class pattern in: '{class_name}'")
        return True
    
    return False


def _extract_collection_class(element_data: dict) -> Optional[str]:
    """
    Find the most specific class that identifies collection items.
    
    SMART APPROACH: 
    1. Prioritize classes containing semantic patterns (row, item, tr, card, etc.)
    2. Skip short/cryptic utility classes using pattern detection (not hardcoded lists)
    3. Return None if no suitable class found (better to fail than use wrong class)
    
    Args:
        element_data: Dict with element attributes
        
    Returns:
        Most appropriate class for collection matching, or None if not suitable
    """
    
    class_name = element_data.get('className', '')
    if not class_name:
        return None
    
    classes = class_name.split()
    
    # PRIORITY 1: Classes containing semantic collection patterns
    # These ARE the actual collection classes we want
    collection_patterns = ['row', 'item', 'tr', 'card', 'entry', 'record', 'group', 'cell', 'list']
    for cls in classes:
        cls_lower = cls.lower()
        for pattern in collection_patterns:
            if pattern in cls_lower:
                logger.info(f"   Extracted collection class: '{cls}' (matched pattern: {pattern})")
                return cls
    
    # PRIORITY 2: Skip utility-like classes using pattern detection
    # Utility classes typically: short (<=5 chars) OR follow letter-number pattern (mt-4, px-12)
    for cls in classes:
        # Skip if too short (likely utility: mt-4, p-2, d-flex are ~5 chars or less)
        if len(cls) <= 5:
            continue
        # Skip if matches pattern: 1-4 letters + hyphen + number (e.g., mt-4, px-12, col-6)
        if re.match(r'^[a-z]{1,4}-\d+$', cls.lower()):
            continue
        # Skip if matches pattern: single letter + hyphen (e.g., d-flex, m-auto)
        if re.match(r'^[a-z]-', cls.lower()):
            continue
        # This looks like a meaningful class name
        logger.info(f"   Using component-like class: '{cls}'")
        return cls
    
    # No suitable class found - better to return None than use wrong class
    logger.info(f"   No suitable collection class found in: '{class_name}'")
    return None


async def _find_collection_locator(page, element_data: dict, collection_class: str) -> Optional[str]:
    """
    Build a locator that matches all items in the collection.
    Tries multiple container strategies to find the most reliable locator.
    
    Args:
        page: Playwright page object
        element_data: Dict with element attributes
        collection_class: The class that identifies collection items
        
    Returns:
        Multi-element locator string, or None if not found
    """
    tag = element_data.get('tagName', '').lower()
    
    # Strategy 1: Standard HTML table - use tbody tr
    if tag == 'tr':
        candidates = [
            'tbody tr',
            'table tr:not(:first-child)',  # Skip header row
            'tbody > tr'
        ]
        for locator in candidates:
            try:
                count = await page.locator(locator).count()
                if count > 1:
                    logger.info(f"   Found table row locator: '{locator}' (count={count})")
                    return locator
            except Exception:
                continue
    
    # Strategy 2: Standard HTML list - use ul/ol li
    if tag == 'li':
        candidates = ['ul li', 'ol li', 'ul > li', 'ol > li']
        for locator in candidates:
            try:
                count = await page.locator(locator).count()
                if count > 1:
                    logger.info(f"   Found list item locator: '{locator}' (count={count})")
                    return locator
            except Exception:
                continue
    
    # Strategy 3: Class-based locator with common container patterns
    container_patterns = [
        f'.{collection_class}',  # Direct class selector
        f'[class*="{collection_class}"]',  # Contains class
    ]
    
    # Try adding common parent containers
    parent_containers = ['tbody', '.table-body', '.list', '.grid', '.container', 
                        '[class*="body"]', '[class*="content"]', '[class*="list"]']
    
    for parent in parent_containers:
        container_patterns.append(f'{parent} .{collection_class}')
    
    for locator in container_patterns:
        try:
            count = await page.locator(locator).count()
            if count > 1:
                logger.info(f"   Found collection locator: '{locator}' (count={count})")
                return locator
        except Exception:
            continue
    
    # Fallback: Just the class (might include non-data rows)
    fallback = f'.{collection_class}'
    try:
        count = await page.locator(fallback).count()
        if count > 1:
            logger.info(f"   Using fallback collection locator: '{fallback}' (count={count})")
            return fallback
    except Exception:
        pass
    
    return None


async def _find_collection_by_text_traversal(page, expected_text: str) -> Optional[dict]:
    """
    Find collection (table rows, list items) by using expected_text as a beacon.
    
    This is the PRIMARY method for finding collections. It works by:
    1. Finding an element containing the expected_text
    2. Traversing UP to find the row/item container
    3. Looking for siblings with similar structure
    4. Generating a collection locator for all matching elements
    
    This approach works even when element_index is invalid (common for non-interactive elements).
    
    Args:
        page: Playwright page object
        expected_text: Text that should be in one of the collection items (e.g., "Cierra")
        
    Returns:
        Dict with 'locator', 'count', 'row_class' if found, None otherwise
    """
    if not expected_text or len(expected_text.strip()) < 2:
        return None
    
    text = expected_text.strip()
    logger.info(f"🔍 TEXT-TRAVERSAL: Finding collection containing '{text}'")
    
    try:
        # Step 1: Find element containing the expected text
        text_locator = page.locator(f"text={text}").first
        count = await text_locator.count()
        
        if count == 0:
            logger.info(f"   No element found containing text: '{text}'")
            return None
        
        # Step 2: Traverse UP to find the row container using JavaScript
        row_info = await text_locator.evaluate("""
            (el) => {
                let current = el;
                
                // Traverse up to find a row-like parent
                while (current && current.parentElement) {
                    current = current.parentElement;
                    const tag = current.tagName.toLowerCase();
                    const className = current.className || '';
                    const role = current.getAttribute('role') || '';
                    
                    // Check if this looks like a row container
                    const isRowLike = (
                        tag === 'tr' || 
                        tag === 'li' ||
                        role === 'row' ||
                        role === 'listitem' ||
                        /row|tr-group|item|record|entry/i.test(className)
                    );
                    
                    if (isRowLike) {
                        // Found the row! Now verify it's part of a collection
                        const parent = current.parentElement;
                        if (parent) {
                            const siblings = Array.from(parent.children);
                            const sameTagSiblings = siblings.filter(s => 
                                s.tagName === current.tagName
                            );
                            
                            if (sameTagSiblings.length > 1) {
                                // This IS a collection row!
                                // Find the best class to use as a locator
                                const classes = className.split(' ').filter(c => c.length > 0);
                                
                                // Prefer classes with semantic meaning
                                const semanticPatterns = ['row', 'tr', 'item', 'record', 'entry', 'group'];
                                let bestClass = null;
                                
                                for (const cls of classes) {
                                    const clsLower = cls.toLowerCase();
                                    for (const pattern of semanticPatterns) {
                                        if (clsLower.includes(pattern)) {
                                            bestClass = cls;
                                            break;
                                        }
                                    }
                                    if (bestClass) break;
                                }
                                
                                // Fallback: use first class that's longer than 5 chars
                                if (!bestClass) {
                                    bestClass = classes.find(c => c.length > 5) || classes[0];
                                }
                                
                                return {
                                    tag: tag,
                                    className: bestClass,
                                    allClasses: className,
                                    siblingCount: sameTagSiblings.length,
                                    role: role,
                                    parentTag: parent.tagName.toLowerCase()
                                };
                            }
                        }
                    }
                }
                return null;
            }
        """)
        
        if row_info:
            logger.info(f"   ✅ Found row container: <{row_info['tag']}> class='{row_info.get('className', '')}'")
            logger.info(f"   📊 Collection has {row_info['siblingCount']} siblings")
            
            # Generate collection locator
            if row_info.get('className'):
                locator = f".{row_info['className']}"
            elif row_info.get('tag') == 'tr':
                locator = 'tbody tr'
            elif row_info.get('tag') == 'li':
                locator = 'ul li, ol li'
            elif row_info.get('role') == 'row':
                locator = '[role="row"]'
            else:
                logger.info(f"   Could not determine locator from row_info: {row_info}")
                return None
            
            # Validate the locator
            try:
                count = await page.locator(locator).count()
                if count > 1:
                    logger.info(f"   ✅ Collection locator: '{locator}' matches {count} elements")
                    return {
                        'locator': locator,
                        'count': count,
                        'row_class': row_info.get('className'),
                        'tag': row_info.get('tag'),
                        'source': 'text_traversal'
                    }
                else:
                    logger.info(f"   Locator '{locator}' only matched {count} element(s)")
            except Exception as e:
                logger.info(f"   Locator validation failed: {e}")
        else:
            logger.info("   Could not find row container by traversing from text element")
        
        return None
        
    except Exception as e:
        logger.warning(f"   ⚠️ Text traversal failed: {e}")
        return None


async def _find_checkbox_or_radio_by_label(page, label_text: str) -> Optional[dict]:
    """
    Find a checkbox or radio input element associated with the given label text.
    
    This handles multiple scenarios:
    1. <label for="id">text</label> <input id="id" type="checkbox">
    2. <label><input type="checkbox"> text</label>
    3. <input type="checkbox"> text (no label, adjacent text)
    4. Text is inside a container with a nearby checkbox
    
    Args:
        page: Playwright page object
        label_text: The visible text near the checkbox/radio
        
    Returns:
        Dict with 'locator' and 'element_type' if found, None otherwise
    """
    if not label_text:
        return None
    
    text = label_text.strip()
    logger.info(f"🔍 CHECKBOX-FINDER: Looking for checkbox/radio with label '{text}'")
    
    # Strategy 1: Find <label> with matching text, get its 'for' attribute
    try:
        label_locator = f'label:has-text("{text}")'
        label_count = await page.locator(label_locator).count()
        
        if label_count >= 1:
            # Get the 'for' attribute of the label
            for_attr = await page.locator(label_locator).first.get_attribute('for')
            
            if for_attr:
                # Label has 'for' attribute - find the associated input
                input_locator = f'input[id="{for_attr}"]'
                input_count = await page.locator(input_locator).count()
                
                if input_count == 1:
                    # Verify it's a checkbox or radio
                    input_type = await page.locator(input_locator).first.get_attribute('type')
                    if input_type in ['checkbox', 'radio']:
                        # Use id-based locator for stability
                        final_locator = f'id={for_attr}'
                        logger.info(f"   ✅ Found {input_type} via label[for]: {final_locator}")
                        return {'locator': final_locator, 'element_type': input_type}
            else:
                # No 'for' attribute - check for nested input inside label
                nested_input_locator = f'{label_locator} >> input[type="checkbox"], {label_locator} >> input[type="radio"]'
                try:
                    # Try checkbox first
                    nested_checkbox = f'{label_locator} >> input[type="checkbox"]'
                    if await page.locator(nested_checkbox).count() == 1:
                        # Get a stable locator for this nested checkbox
                        checkbox_id = await page.locator(nested_checkbox).first.get_attribute('id')
                        checkbox_name = await page.locator(nested_checkbox).first.get_attribute('name')
                        
                        if checkbox_id:
                            final_locator = f'id={checkbox_id}'
                        elif checkbox_name:
                            final_locator = f'[name="{checkbox_name}"]'
                        else:
                            # Use the label-relative locator
                            final_locator = nested_checkbox
                        
                        logger.info(f"   ✅ Found nested checkbox inside label: {final_locator}")
                        return {'locator': final_locator, 'element_type': 'checkbox'}
                    
                    # Try radio button
                    nested_radio = f'{label_locator} >> input[type="radio"]'
                    if await page.locator(nested_radio).count() == 1:
                        radio_id = await page.locator(nested_radio).first.get_attribute('id')
                        radio_name = await page.locator(nested_radio).first.get_attribute('name')
                        radio_value = await page.locator(nested_radio).first.get_attribute('value')
                        
                        if radio_id:
                            final_locator = f'id={radio_id}'
                        elif radio_name and radio_value:
                            final_locator = f'[name="{radio_name}"][value="{radio_value}"]'
                        elif radio_name:
                            final_locator = f'[name="{radio_name}"]'
                        else:
                            final_locator = nested_radio
                        
                        logger.info(f"   ✅ Found nested radio inside label: {final_locator}")
                        return {'locator': final_locator, 'element_type': 'radio'}
                except Exception as e:
                    logger.info(f"   ⚠️ Error checking nested input: {e}")
    except Exception as e:
        logger.info(f"   ⚠️ Error in label-based search: {e}")
    
    # Strategy 2: Find text element and look for adjacent checkbox/radio
    # This handles: <input type="checkbox"> checkbox 1
    try:
        # Look for checkboxes/radios that are siblings or near the text
        adjacent_patterns = [
            # Pattern: checkbox followed by text
            f'input[type="checkbox"]:left-of(:text("{text}"):visible)',
            f'input[type="radio"]:left-of(:text("{text}"):visible)',
            # Pattern: text node in same parent as checkbox
            f':text("{text}") >> xpath=preceding-sibling::input[@type="checkbox"]',
            f':text("{text}") >> xpath=preceding-sibling::input[@type="radio"]',
        ]
        
        for pattern in adjacent_patterns:
            try:
                count = await page.locator(pattern).count()
                if count == 1:
                    element = page.locator(pattern).first
                    input_type = await element.get_attribute('type')
                    input_id = await element.get_attribute('id')
                    input_name = await element.get_attribute('name')
                    input_value = await element.get_attribute('value')
                    
                    if input_id:
                        final_locator = f'id={input_id}'
                    elif input_name and input_value:
                        final_locator = f'[name="{input_name}"][value="{input_value}"]'
                    elif input_name:
                        final_locator = f'[name="{input_name}"]'
                    else:
                        # Use index-based locator as last resort
                        continue
                    
                    logger.info(f"   ✅ Found adjacent {input_type}: {final_locator}")
                    return {'locator': final_locator, 'element_type': input_type}
            except Exception:
                pass
    except Exception as e:
        logger.info(f"   ⚠️ Error in adjacent search: {e}")
    
    # Strategy 3: Use nth-of-type pattern for checkbox lists
    # Common pattern: the-internet.herokuapp.com/checkboxes has checkbox 1, checkbox 2
    try:
        # Extract number if text ends with a number (e.g., "checkbox 1" -> 1)
        number_match = re.search(r'(\d+)\s*$', text)
        if number_match:
            index = int(number_match.group(1))
            # Try to find all checkboxes on page and pick the nth one
            all_checkboxes = 'input[type="checkbox"]'
            checkbox_count = await page.locator(all_checkboxes).count()
            
            if checkbox_count >= index:
                # Use nth-of-type or nth() for Playwright
                nth_locator = f'input[type="checkbox"] >> nth={index - 1}'  # 0-indexed
                if await page.locator(nth_locator).count() == 1:
                    # Try to get a more stable locator
                    element = page.locator(nth_locator).first
                    input_id = await element.get_attribute('id')
                    input_name = await element.get_attribute('name')
                    
                    if input_id:
                        final_locator = f'id={input_id}'
                    elif input_name:
                        final_locator = f'[name="{input_name}"]'
                    else:
                        final_locator = f'input[type="checkbox"]:nth-of-type({index})'
                    
                    logger.info(f"   ✅ Found checkbox by index ({index}): {final_locator}")
                    return {'locator': final_locator, 'element_type': 'checkbox'}
            
            # Same for radio buttons
            all_radios = 'input[type="radio"]'
            radio_count = await page.locator(all_radios).count()
            
            if radio_count >= index:
                nth_locator = f'input[type="radio"] >> nth={index - 1}'
                if await page.locator(nth_locator).count() == 1:
                    element = page.locator(nth_locator).first
                    input_id = await element.get_attribute('id')
                    input_name = await element.get_attribute('name')
                    input_value = await element.get_attribute('value')
                    
                    if input_id:
                        final_locator = f'id={input_id}'
                    elif input_name and input_value:
                        final_locator = f'[name="{input_name}"][value="{input_value}"]'
                    else:
                        final_locator = f'input[type="radio"]:nth-of-type({index})'
                    
                    logger.info(f"   ✅ Found radio by index ({index}): {final_locator}")
                    return {'locator': final_locator, 'element_type': 'radio'}
    except Exception as e:
        logger.info(f"   ⚠️ Error in index-based search: {e}")
    
    logger.info(f"   ⚠️ CHECKBOX-FINDER: No checkbox/radio found for '{text}'")
    return None


async def _disambiguate_by_coordinates(page, selector: str, x: float, y: float) -> Optional[dict]:
    """
    When multiple elements match a selector, find which one is at or closest to (x, y).
    
    Uses 3-layer approach:
    1. Visible filter - try to reduce to 1 visible element
    2. Bounding box match - find element containing coordinates
    3. Closest distance - fallback if coordinates slightly off
    
    Args:
        page: Playwright page object
        selector: The selector that matched multiple elements
        x, y: Target coordinates
        
    Returns:
        Dict with 'locator' and 'disambiguated': True if found, None otherwise
    """
    import math
    
    try:
        locator = page.locator(selector)
        count = await locator.count()
        
        if count <= 1:
            return None  # Nothing to disambiguate
        
        logger.info(f"   🔍 DISAMBIGUATE: '{selector}' has {count} matches, using coordinates ({x}, {y})")
        
        # Track which selector to use for nth indexing
        base_selector_for_nth = selector
        
        # ========================================
        # Layer 1: Visible Filter
        # ========================================
        try:
            visible_selector = f"{selector} >> visible=true"
            visible_locator = page.locator(visible_selector)
            visible_count = await visible_locator.count()
            
            if visible_count == 1:
                logger.info(f"   ✅ DISAMBIGUATED (visible filter): Only 1 visible element")
                return {'locator': visible_selector, 'disambiguated': True, 'strategy': 'visible_filter'}
            elif visible_count < count:
                # Reduced count, use visible locator for next checks
                # IMPORTANT: Update base_selector_for_nth to use visible filter
                locator = visible_locator
                count = visible_count
                base_selector_for_nth = visible_selector  # FIX: Use visible selector for nth
                logger.info(f"   📉 Visible filter reduced to {visible_count} elements")
        except Exception as e:
            logger.info(f"   Visible filter failed: {e}")
        
        # ========================================
        # Layer 2: Bounding Box Match (exact hit)
        # ========================================
        best_exact_idx = -1
        for i in range(count):
            try:
                element = locator.nth(i)
                box = await element.bounding_box()
                
                if box:
                    # Check if (x, y) is inside this element's bounding box
                    if (box['x'] <= x <= box['x'] + box['width'] and
                        box['y'] <= y <= box['y'] + box['height']):
                        best_exact_idx = i
                        logger.info(f"   ✅ DISAMBIGUATED (bounding box): Coordinates inside element {i}")
                        break
            except Exception as e:
                logger.info(f"   Bounding box check failed for element {i}: {e}")
        
        if best_exact_idx >= 0:
            indexed_selector = f"{base_selector_for_nth} >> nth={best_exact_idx}"
            return {'locator': indexed_selector, 'disambiguated': True, 'strategy': 'bounding_box'}
        
        # ========================================
        # Layer 3: Closest Distance (fallback)
        # ========================================
        min_distance = float('inf')
        closest_idx = -1
        DISTANCE_THRESHOLD = 50  # 50px tolerance
        
        for i in range(count):
            try:
                element = locator.nth(i)
                box = await element.bounding_box()
                
                if box:
                    # Calculate distance to box center
                    center_x = box['x'] + box['width'] / 2
                    center_y = box['y'] + box['height'] / 2
                    distance = math.sqrt((x - center_x) ** 2 + (y - center_y) ** 2)
                    
                    if distance < min_distance:
                        min_distance = distance
                        closest_idx = i
            except Exception as e:
                logger.info(f"   Distance check failed for element {i}: {e}")
        
        if closest_idx >= 0 and min_distance < DISTANCE_THRESHOLD:
            indexed_selector = f"{base_selector_for_nth} >> nth={closest_idx}"
            logger.info(f"   ✅ DISAMBIGUATED (closest distance): Element {closest_idx} is {min_distance:.1f}px away")
            return {'locator': indexed_selector, 'disambiguated': True, 'strategy': 'closest_distance', 'distance': min_distance}
        
        logger.info(f"   ⚠️ DISAMBIGUATION FAILED: No element within {DISTANCE_THRESHOLD}px (closest: {min_distance:.1f}px)")
        return None
        
    except Exception as e:
        logger.info(f"   Disambiguation error: {e}")
        return None


async def _find_element_by_expected_text(page, expected_text: str, element_description: str, x: float = None, y: float = None) -> Optional[dict]:
    """
    Try to find element directly by the expected visible text.
    This is the TEXT-FIRST approach - more reliable than coordinates.
    
    ENHANCED: Now detects checkbox/radio context and returns the actual input element
    instead of just the text label. This fixes issues where clicking text labels
    doesn't toggle checkboxes without proper <label> association.
    
    Args:
        page: Playwright page object
        expected_text: The actual text AI sees on the element
        element_description: Human-readable description (for context)
        
    Returns:
        Dict with 'locator' and optionally 'element_type' if found, None otherwise.
        For backward compatibility, returns string locator for non-checkbox elements.
    """
    if not expected_text or len(expected_text.strip()) < MIN_TEXT_LENGTH:
        return None
    
    text = expected_text.strip()
    desc_lower = element_description.lower() if element_description else ""
    
    logger.info(f"🔍 TEXT-FIRST: Searching for element with text '{text}'")
    
    # ========================================
    # SPECIAL HANDLING: Checkbox/Radio Elements
    # ========================================
    # Detect if we're looking for a checkbox or radio button based on:
    # 1. Description mentions checkbox/radio/toggle/check/select
    # 2. Expected text looks like a checkbox label (short text, often with numbers)
    
    # OPTIMIZATION: Early exit for obvious non-form elements
    # Skip checkbox detection entirely if description indicates non-input elements
    skip_checkbox_check = False
    if element_description:
        # Keywords that clearly indicate non-form elements
        non_form_keywords = ['button', 'link', 'heading', 'title', 'paragraph', 'span', 'div text', 'label text', 'banner', 'menu item']
        if any(keyword in desc_lower for keyword in non_form_keywords):
            skip_checkbox_check = True
            logger.info(f"   ⏩ Skipping checkbox detection - element is clearly not a form input")
    
    if not skip_checkbox_check:
        is_checkbox_context = any(keyword in desc_lower for keyword in [
            'checkbox', 'check box', 'radio', 'toggle', 'check the', 'select the',
            'tick', 'untick', 'check mark', 'input element for'
        ])
        
        # Also detect common checkbox label patterns
        is_checkbox_like_text = (
            text.lower().startswith('checkbox') or
            text.lower().startswith('option') or
            text.lower().startswith('select') or
            text.lower() in ['yes', 'no', 'agree', 'accept', 'remember me', 'terms', 'newsletter'] or
            len(text) < MAX_CHECKBOX_LABEL_LENGTH  # Short text near form elements often indicates checkbox labels
        )
        
        if is_checkbox_context or is_checkbox_like_text:
            logger.info(f"   🎯 Checkbox/Radio context detected - checking for input element")
            checkbox_result = await _find_checkbox_or_radio_by_label(page, text)
            
            if checkbox_result:
                # Return the checkbox/radio input locator instead of text
                logger.info(f"   ✅ Returning checkbox/radio locator: {checkbox_result['locator']}")
                return checkbox_result
            else:
                logger.info(f"   ⚠️ No checkbox/radio found, falling back to text-based search")
    
    # ========================================
    # Standard Text-Based Search
    # ========================================
    # Build list of selectors to try based on expected_text
    selectors_to_try = []
    
    # Exact text match (highest priority)
    selectors_to_try.append(f'text="{text}"')
    
    # Role-based with exact name (very reliable for buttons/links)
    if "button" in desc_lower or any(word in text.lower() for word in ['submit', 'add', 'delete', 'save', 'cancel', 'ok', 'yes', 'no']):
        selectors_to_try.extend([
            f'role=button[name="{text}"]',
            f'button:has-text("{text}")',
        ])
    
    if "link" in desc_lower:
        selectors_to_try.extend([
            f'role=link[name="{text}"]',
            f'a:has-text("{text}")',
        ])
    
    # Generic text-based selectors
    # NOTE: Removed *:has-text() - it's too broad (matches every ancestor container)
    selectors_to_try.extend([
        f'[aria-label="{text}"]',
        f'[title="{text}"]',
        f'[placeholder="{text}"]',
    ])
    
    # Try partial matches if text is long
    if len(text) > 20:
        short_text = text[:20]
        selectors_to_try.extend([
            f'text="{short_text}"',
            # NOTE: Removed *:has-text() for partial text - same issue as above
        ])
    
    # Try each selector
    for selector in selectors_to_try:
        try:
            count = await page.locator(selector).count()
            if count == 1:
                logger.info(f"   ✅ TEXT-FIRST SUCCESS: Found unique element with '{selector}'")
                # Return as dict for consistency, but no special element_type
                return {'locator': selector}
            elif count > 1:
                # NEW: Try to disambiguate using coordinates if available
                if x is not None and y is not None:
                    result = await _disambiguate_by_coordinates(page, selector, x, y)
                    if result:
                        logger.info(f"   ✅ TEXT-FIRST SUCCESS (disambiguated): {result['locator']}")
                        return result
                else:
                    logger.info(f"   ⚠️ Multiple matches ({count}) for: {selector} (no coords for disambiguation)")
            # count == 0: no matches, try next
        except Exception as e:
            logger.info(f"   ⚠️ Selector failed: {selector} - {e}")
            pass
    
    logger.info(f"   ⚠️ TEXT-FIRST: No unique element found for text '{text}'")
    return None


async def _find_element_by_description(page, description: str) -> Optional[str]:
    """
    Fallback: Try to find element by its description when coordinates fail.
    Returns the unique locator string if found, None otherwise.
    
    This is used when document.elementFromPoint() returns BODY/HTML,
    which happens when coordinates land in empty space (common with centered layouts).
    
    Strategy: Use Playwright's semantic locators based on the element description.
    This is more reliable than coordinate-based approach since it matches what
    the AI "sees" (text, role, label) rather than pixel positions.
    """
    if not description:
        return None
    
    # Extract key words from description (e.g., "Add Element button" -> ["Add", "Element"])
    # Also handle common variations
    desc_lower = description.lower()
    keywords = description.replace("button", "").replace("link", "").replace("input", "").replace("field", "").strip().split()
    search_text = " ".join(keywords[:3])  # Use first 3 words max
    
    # Also try the full description as-is (without role words)
    full_text = " ".join(keywords)
    
    try:
        # Priority-ordered selectors based on Playwright best practices
        # Using semantic locators that match what the AI "sees"
        selectors_to_try = []
        
        # If description mentions "button", prioritize button locators
        if "button" in desc_lower:
            selectors_to_try.extend([
                f'role=button[name="{search_text}"]',
                f'role=button[name="{full_text}"]',
                f'button:has-text("{search_text}")',
                f'button >> text="{search_text}"',
            ])
        
        # If description mentions "link", prioritize link locators
        if "link" in desc_lower:
            selectors_to_try.extend([
                f'role=link[name="{search_text}"]',
                f'role=link[name="{full_text}"]',
                f'a:has-text("{search_text}")',
            ])
        
        # If description mentions input/field, prioritize input locators
        if "input" in desc_lower or "field" in desc_lower:
            selectors_to_try.extend([
                f'role=textbox[name="{search_text}"]',
                f'input[placeholder*="{search_text}"]',
                f'input[name*="{search_text}"]',
            ])
        
        # Generic selectors that work for any element type
        selectors_to_try.extend([
            f'text="{search_text}"',
            f'text="{full_text}"',
            f'[aria-label*="{search_text}"]',
            f'[title*="{search_text}"]',
            f'[role="button"]:has-text("{search_text}")',
            f'button:has-text("{search_text}")',
            f'a:has-text("{search_text}")',
        ])
        
        # Try each selector
        for selector in selectors_to_try:
            try:
                count = await page.locator(selector).count()
                if count == 1:
                    logger.info(f"   ✅ Found unique element with semantic locator: {selector}")
                    return selector
                elif count > 1:
                    logger.info(f"   ⚠️ Multiple matches ({count}) for: {selector}")
                # count == 0: no matches, try next
            except Exception as e:
                logger.info(f"   ⚠️ Selector failed: {selector} - {e}")
                pass
        
        logger.warning(f"   ❌ No unique element found for description: {description}")
        return None
    except Exception as e:
        logger.info(f"   Error in fallback search: {e}")
        return None


async def _find_table_rows_by_description(
    page,
    description: str,
    expected_text: Optional[str] = None
) -> Optional[dict]:
    """
    Find table rows when the description indicates we're looking for table rows.
    
    This handles scenarios like:
    - "all visible data rows"
    - "table rows after filtering"
    - "filtered results in table"
    
    Common table row patterns for different frameworks:
    - React-Table: .rt-tbody .rt-tr-group
    - Standard HTML: table tbody tr
    - ARIA grids: [role="grid"] [role="row"]
    
    Args:
        page: Playwright page object
        description: Element description from CrewAI
        expected_text: Optional text that should appear in the rows
        
    Returns:
        Dict with 'locator', 'count', and 'element_type' if found, None otherwise
    """
    if not description:
        return None
    
    desc_lower = description.lower()
    
    # Keywords that indicate we're looking for table rows (not individual cells)
    table_row_keywords = [
        # Explicit row keywords
        'table row', 'data row', 'table body', 'visible row', 'filtered row',
        'all rows', 'row result', 'matching row', 'search result', 'result row',
        'rows in table', 'rows within', 'data rows',
        # Table-related keywords (when user wants to verify table data)
        'data table', 'main table', 'content table', 'result table',
        'table on', 'table after', 'filtered table', 'search table',
        # Content area patterns (table displaying results)
        'table displaying', 'displaying results', 'content area of the table',
        'table content', 'table results', 'results in table'
    ]
    
    # Check if description mentions table rows
    is_table_row_request = any(keyword in desc_lower for keyword in table_row_keywords)
    
    if not is_table_row_request:
        return None
    
    logger.info(f"🔍 TABLE-ROW-FINDER: Description mentions table rows")
    
    # Common table row locators for different frameworks (ordered by specificity)
    table_row_locators = [
        # React-Table (demoqa, etc.)
        ('.rt-tbody .rt-tr-group', 'react-table-rows'),
        ('.rt-tbody > .rt-tr-group', 'react-table-rows-direct'),
        # Standard HTML tables
        ('table tbody tr', 'html-table-rows'),
        ('table > tbody > tr', 'html-table-rows-direct'),
        # ARIA grids
        ('[role="grid"] [role="row"]:not([role="columnheader"])', 'aria-grid-rows'),
        ('[role="rowgroup"] [role="row"]', 'aria-rowgroup-rows'),
        # Common data table classes
        ('.table-body tr', 'table-body-rows'),
        ('.data-table tbody tr', 'data-table-rows'),
        # AG Grid
        ('.ag-body-viewport .ag-row', 'ag-grid-rows'),
        # Material UI Table
        ('.MuiTableBody-root .MuiTableRow-root', 'mui-table-rows'),
    ]
    
    for locator, locator_type in table_row_locators:
        try:
            count = await page.locator(locator).count()
            
            if count >= 1:
                logger.info(f"   📋 Found {count} rows with: {locator}")
                
                # If expected_text provided, this is a TABLE VERIFICATION scenario
                if expected_text:
                    # Get first word of expected text for partial matching
                    first_word = expected_text.split()[0] if expected_text.split() else expected_text
                    
                    # Build a filtered locator that matches only rows with the text
                    filtered_locator = f'{locator}:has-text("{first_word}")'
                    
                    # Check if any row contains the expected text
                    matching_rows = page.locator(filtered_locator)
                    matching_count = await matching_rows.count()
                    
                    if matching_count >= 1:
                        logger.info(f"   ✅ {matching_count} rows contain '{first_word}'")
                        logger.info(f"   🔍 This is a TABLE-VERIFICATION scenario")
                        
                        # Return enriched metadata for table verification
                        return {
                            'locator': locator,  # Base row locator (matches all rows)
                            'filtered_locator': filtered_locator,  # Locator for rows with text
                            'count': count,  # Total row count
                            'matching_count': matching_count,  # Rows matching filter
                            'filter_text': first_word,  # The text to verify
                            'element_type': 'table-verification',  # Special type for verification
                            'locator_type': locator_type
                        }
                    else:
                        logger.info(f"   ⚠️ Rows found but none contain '{first_word}'")
                        continue
                else:
                    # No expected_text, return basic table-rows type
                    return {
                        'locator': locator,
                        'count': count,
                        'element_type': 'table-rows',
                        'locator_type': locator_type
                    }
                    
        except Exception as e:
            logger.info(f"   ⚠️ Locator failed: {locator} - {e}")
            continue
    
    logger.info(f"   ⚠️ TABLE-ROW-FINDER: No table rows found on page")
    return None


async def _refine_cell_to_clickable_element(
    page,
    cell_locator: str,
    expected_text: str
) -> Optional[str]:
    """
    Refine a table cell locator to find a specific clickable element inside.
    
    When a td contains multiple elements (e.g., "edit" and "delete" links),
    this function attempts to find the exact element matching expected_text.
    
    Refinement Priority (for QA automation best practices):
    1. Links (<a>) - Most common for table actions
    2. Buttons (<button>) - Standard clickable elements
    3. ARIA buttons ([role="button"]) - Custom button implementations
    4. Elements with aria-label (icon buttons)
    5. Elements with title attribute (tooltip elements)
    6. Any element with matching text (last resort)
    
    Args:
        page: Playwright page object
        cell_locator: The td cell locator
        expected_text: The text to find inside the cell
        
    Returns:
        Refined locator string if found, None otherwise
    """
    if not expected_text or not expected_text.strip():
        return None
    
    text = expected_text.strip()
    
    # Refinement strategies in priority order
    # Using >> for Playwright's chained locator syntax
    refinement_strategies = [
        # 1. Links - most common for table actions like "edit", "delete", "view"
        (f'{cell_locator} >> a:has-text("{text}")', 'link'),
        (f'{cell_locator} >> a:text("{text}")', 'link-exact'),
        
        # 2. Buttons - standard clickable elements
        (f'{cell_locator} >> button:has-text("{text}")', 'button'),
        (f'{cell_locator} >> button:text("{text}")', 'button-exact'),
        
        # 3. ARIA buttons - custom button implementations
        (f'{cell_locator} >> [role="button"]:has-text("{text}")', 'aria-button'),
        
        # 4. Icon buttons with aria-label
        (f'{cell_locator} >> [aria-label="{text}" i]', 'aria-label'),
        (f'{cell_locator} >> [aria-label*="{text}" i]', 'aria-label-partial'),
        
        # 5. Elements with title attribute (tooltips)
        (f'{cell_locator} >> [title="{text}" i]', 'title'),
        (f'{cell_locator} >> [title*="{text}" i]', 'title-partial'),
        
        # 6. Input elements with matching value
        (f'{cell_locator} >> input[value="{text}" i]', 'input-value'),
        
        # 7. Any clickable element with text (span, div with onclick, etc.)
        (f'{cell_locator} >> :text("{text}")', 'any-text'),
    ]
    
    logger.info(f"   🔍 Refining cell locator to find clickable element with text '{text}'")
    
    for refined_locator, strategy_name in refinement_strategies:
        try:
            count = await page.locator(refined_locator).count()
            
            if count == 1:
                logger.info(f"   ✅ Refined to {strategy_name}: {refined_locator}")
                return refined_locator
            elif count > 1:
                logger.info(f"   ⚠️ Multiple matches ({count}) for {strategy_name}")
            # count == 0: no matches, try next strategy
            
        except Exception as e:
            logger.info(f"   ⚠️ Refinement failed for {strategy_name}: {e}")
            continue
    
    logger.info(f"   ⚠️ Could not refine cell to specific element, using cell locator")
    return None


async def _find_table_cell_by_structured_info(
    page, 
    table_cell_info: Optional[dict] = None,
    description: str = "",
    expected_text: Optional[str] = None
) -> Optional[dict]:
    """
    Find a table cell element using structured table_cell_info from BrowserUse agent.
    
    This function uses STRUCTURED INPUT from BrowserUse (preferred) rather than parsing
    natural language descriptions with regex (brittle).
    
    ENHANCED: When expected_text is provided and matches content inside the cell,
    attempts to refine the locator to target the specific clickable element (link, button)
    rather than the entire cell. This is critical for cells with multiple actions.
    
    Structured Format (from BrowserUse agent):
    {
        "table_heading": "Example 1",   # Text near/above the table (primary identifier)
        "table_index": 1,               # Fallback: nth table on page (1-indexed)
        "row": 1,                        # Row number (1-indexed)
        "column": 2,                     # Column number (1-indexed)
    }
    
    Args:
        page: Playwright page object
        table_cell_info: Structured dict with table/row/column info (from BrowserUse)
        description: Human-readable description (for logging only)
        expected_text: Optional expected text content for validation AND refinement
        
    Returns:
        Dict with 'locator' and 'element_type' keys if found, None otherwise
    """
    if not table_cell_info:
        logger.info(f"   ⚠️ No structured table_cell_info provided for: {description}")
        return None
    
    # Extract structured info
    table_heading = table_cell_info.get('table_heading')
    table_index = table_cell_info.get('table_index', 1)
    row = table_cell_info.get('row')
    column = table_cell_info.get('column')
    
    # Validate required fields
    if row is None or column is None:
        logger.warning(f"   ⚠️ Missing row ({row}) or column ({column}) in table_cell_info")
        return None
    
    logger.info(f"🔍 TABLE-CELL-FINDER: Using structured info")
    logger.info(f"   📋 Table heading: {table_heading or 'N/A'}, Index: {table_index}")
    logger.info(f"   📋 Row: {row}, Column: {column}")
    if expected_text:
        logger.info(f"   📋 Expected text: '{expected_text}'")
    
    # ========================================
    # Build Locator Strategies for the Cell
    # ========================================
    locators_to_try = []
    
    # Strategy 1: If table_heading provided, find table near that heading
    if table_heading:
        # XPath to find table following a heading with specific text
        locators_to_try.extend([
            # Table following h3 with text
            f'xpath=//h3[contains(text(), "{table_heading}")]/following-sibling::table[1]//tbody/tr[{row}]/td[{column}]',
            # Table following any heading with text
            f'xpath=//*[self::h1 or self::h2 or self::h3 or self::h4][contains(text(), "{table_heading}")]/following-sibling::table[1]//tbody/tr[{row}]/td[{column}]',
            # Table with caption containing text
            f'xpath=//table[.//caption[contains(text(), "{table_heading}")]]//tbody/tr[{row}]/td[{column}]',
        ])
    
    # Strategy 2: Use table_index (nth table on page)
    table_num = table_index if table_index else 1
    locators_to_try.extend([
        # CSS selector with nth-of-type (works with tables having tbody)
        f'table:nth-of-type({table_num}) tbody tr:nth-child({row}) td:nth-child({column})',
        # XPath selector (very reliable for tables)
        f'xpath=(//table)[{table_num}]//tbody/tr[{row}]/td[{column}]',
        # CSS without tbody (some tables don't use tbody)
        f'table:nth-of-type({table_num}) tr:nth-child({row}) td:nth-child({column})',
        # Direct XPath without tbody
        f'xpath=(//table)[{table_num}]//tr[{row}]/td[{column}]',
    ])
    
    # Strategy 3: Using role=table with nth-of-type
    locators_to_try.append(
        f'[role="table"]:nth-of-type({table_num}) [role="row"]:nth-child({row}) [role="cell"]:nth-child({column})'
    )
    
    # Try each locator to find the cell
    for cell_locator in locators_to_try:
        try:
            count = await page.locator(cell_locator).count()
            
            if count == 1:
                # Cell found! Now determine what to return
                
                if expected_text:
                    # Validate that expected_text is somewhere in this cell
                    is_match, actual_text = await _validate_semantic_match(page, cell_locator, expected_text)
                    
                    if not is_match:
                        logger.info(f"   ⚠️ Locator found but text mismatch: {cell_locator}")
                        continue  # Try next locator
                    
                    logger.info(f"   ✅ TABLE-CELL found with text match: {cell_locator}")
                    
                    # ========================================
                    # REFINEMENT: Try to find specific clickable element inside
                    # ========================================
                    # This handles cases like <td><a>edit</a> <a>delete</a></td>
                    # where we want to target the specific "edit" link, not the whole cell
                    
                    refined_locator = await _refine_cell_to_clickable_element(
                        page, cell_locator, expected_text
                    )
                    
                    if refined_locator:
                        # Successfully refined to a specific element inside the cell
                        return {
                            'locator': refined_locator, 
                            'element_type': 'table-cell-element',
                            'cell_locator': cell_locator  # Keep original cell for reference
                        }
                    else:
                        # Refinement failed, return the cell locator
                        # This is correct for cells where the text IS the content (e.g., <td>$45.00</td>)
                        logger.info(f"   📝 Using cell locator (no refinable inner element)")
                        return {'locator': cell_locator, 'element_type': 'table-cell'}
                else:
                    # No expected_text, just return the cell locator
                    logger.info(f"   ✅ TABLE-CELL locator found: {cell_locator}")
                    return {'locator': cell_locator, 'element_type': 'table-cell'}
            
            elif count > 1:
                logger.info(f"   ⚠️ Multiple matches ({count}) for: {cell_locator}")
            # count == 0: no matches, try next
            
        except Exception as e:
            logger.info(f"   ⚠️ Locator failed: {cell_locator} - {e}")
            continue
    
    logger.info(f"   ⚠️ TABLE-CELL-FINDER: No unique locator found for Row {row}, Col {column}")
    return None


async def _generate_locators_from_element_data(
    page,
    element_data: dict[str, Any],
    element_id: str,
    element_description: str,
    expected_text: Optional[str] = None
) -> Optional[dict]:
    """
    Generate and validate locators from element_data extracted from browser-use DOM.
    
    This is the FASTEST approach - we already have element attributes from browser-use DOM,
    so we can immediately try to generate locators without coordinate-based JavaScript.
    
    Priority order (most stable first):
    1. id attribute → id=xxx
    2. data-testid attribute → [data-testid="xxx"]
    3. name attribute → [name="xxx"]
    4. aria-label + role → [role="xxx"][aria-label="yyy"]
    5. placeholder → [placeholder="xxx"]
    6. xpath (from browser-use) → direct xpath
    
    For TABLE elements (td/th with xpath):
    - The xpath already contains precise table cell location
    - e.g., /html/body/.../table/tbody/tr[1]/td[2]
    
    Args:
        page: Playwright page object
        element_data: Dict with element attributes from browser-use DOM:
                     {tagName, id, name, className, ariaLabel, placeholder, 
                      title, role, dataTestId, xpath, textContent}
        element_id: Element identifier
        element_description: Human-readable description
        expected_text: Optional expected text for semantic validation
        
    Returns:
        Complete result dict if locator found, None otherwise
    """
    if not element_data:
        return None
    
    logger.info(f"🔍 STEP 0: Trying ELEMENT-DATA locators from browser-use DOM")
    logger.info(f"   Tag: <{element_data.get('tagName', '?')}>")
    
    # Track what we have for debugging
    attrs_found = []
    if element_data.get('id'):
        attrs_found.append(f"id='{element_data['id']}'")
    if element_data.get('dataTestId'):
        attrs_found.append(f"data-testid='{element_data['dataTestId']}'")
    if element_data.get('name'):
        attrs_found.append(f"name='{element_data['name']}'")
    if element_data.get('ariaLabel'):
        attrs_found.append(f"aria-label='{element_data['ariaLabel']}'")
    if element_data.get('xpath'):
        attrs_found.append(f"xpath available")
    
    if attrs_found:
        logger.info(f"   Available attributes: {', '.join(attrs_found)}")
    else:
        logger.info(f"   ⚠️ No usable attributes found in element_data")
        return None
    
    # ========================================
    # MULTI-ELEMENT COLLECTION DETECTION
    # ========================================
    # Check if this element is part of a collection (table rows, list items, etc.)
    # If so, return a multi-element locator for Robot Framework iteration
    
    if _is_collection_element(element_data, element_description):
        logger.info(f"   🔄 COLLECTION DETECTED: Element appears to be part of a repeatable collection")
        
        # PRIMARY METHOD: Use expected_text as a beacon to find the actual row container
        # This works even when element_data is from a wrong parent container
        if expected_text:
            text_result = await _find_collection_by_text_traversal(page, expected_text)
            
            if text_result and text_result.get('locator'):
                collection_locator = text_result['locator']
                count = text_result['count']
                
                logger.info(f"   ✅ MULTI-ELEMENT locator found via TEXT-TRAVERSAL: {collection_locator}")
                logger.info(f"   📊 Matches {count} elements")
                logger.info(f"   ⏭️ Skipping semantic validation (handled in Robot Framework iteration)")
                
                return {
                    'element_id': element_id,
                    'description': element_description,
                    'found': True,
                    'best_locator': collection_locator,
                    'element_type': 'collection',
                    'count': count,
                    'quality_score': 90,
                    'unique': False,
                    'valid': True,
                    'validated': True,
                    'all_locators': [{
                        'type': 'collection',
                        'locator': collection_locator,
                        'priority': 0,
                        'quality_score': 90,
                        'strategy': 'Text-traversal collection locator',
                        'count': count,
                        'unique': False,
                        'valid': True,
                        'validation_method': 'playwright'
                    }],
                    'element_info': {
                        'tagName': text_result.get('tag', ''),
                        'className': text_result.get('row_class', ''),
                        'collection_class': text_result.get('row_class', ''),
                        'source': 'text_traversal'
                    },
                    'validation_summary': {
                        'total_generated': 1,
                        'valid': 1,
                        'unique': 0,
                        'multi_element': True,
                        'collection_count': count,
                        'best_type': 'collection',
                        'best_strategy': 'Text-traversal collection locator',
                        'validation_method': 'playwright'
                    },
                    'validation_method': 'playwright',
                    'semantic_match': True
                }
            else:
                logger.info(f"   Text-traversal did not find collection, trying element_data approach...")
        
        # FALLBACK METHOD: Try element_data approach (may not work if element_data is from wrong container)
        collection_class = _extract_collection_class(element_data)
        if collection_class:
            logger.info(f"   📦 Collection class from element_data: '{collection_class}'")
            
            collection_locator = await _find_collection_locator(page, element_data, collection_class)
            
            if collection_locator:
                try:
                    count = await page.locator(collection_locator).count()
                    
                    if count > 1:
                        # VALIDATION: Check if matched elements contain expected_text
                        if expected_text:
                            text_found = False
                            try:
                                for i in range(min(count, 3)):
                                    el_text = await page.locator(collection_locator).nth(i).text_content() or ""
                                    if expected_text.lower() in el_text.lower():
                                        text_found = True
                                        break
                            except Exception:
                                pass
                            
                            if not text_found:
                                logger.warning(f"   ⚠️ Collection locator '{collection_locator}' does NOT contain expected text '{expected_text}'")
                                logger.warning(f"   ⚠️ Skipping this locator as it matches wrong elements")
                            else:
                                logger.info(f"   ✅ MULTI-ELEMENT locator found: {collection_locator}")
                                logger.info(f"   📊 Matches {count} elements (validated: contains expected text)")
                                
                                return {
                                    'element_id': element_id,
                                    'description': element_description,
                                    'found': True,
                                    'best_locator': collection_locator,
                                    'element_type': 'collection',
                                    'count': count,
                                    'quality_score': 85,
                                    'unique': False,
                                    'valid': True,
                                    'validated': True,
                                    'all_locators': [{
                                        'type': 'collection',
                                        'locator': collection_locator,
                                        'priority': 0,
                                        'quality_score': 85,
                                        'strategy': 'Element-data collection locator',
                                        'count': count,
                                        'unique': False,
                                        'valid': True,
                                        'validation_method': 'playwright'
                                    }],
                                    'element_info': {
                                        'tagName': element_data.get('tagName', ''),
                                        'className': element_data.get('className', ''),
                                        'collection_class': collection_class,
                                        'source': 'element_data_collection'
                                    },
                                    'validation_summary': {
                                        'total_generated': 1,
                                        'valid': 1,
                                        'unique': 0,
                                        'multi_element': True,
                                        'collection_count': count,
                                        'best_type': 'collection',
                                        'best_strategy': 'Element-data collection locator',
                                        'validation_method': 'playwright'
                                    },
                                    'validation_method': 'playwright',
                                    'semantic_match': True
                                }
                except Exception as e:
                    logger.warning(f"   ⚠️ Collection locator validation failed: {e}")
        else:
            logger.info(f"   Could not extract collection class from element_data")
    
    # ========================================
    # SINGLE ELEMENT LOCATOR GENERATION
    # ========================================
    # Generate candidate locators in priority order
    locator_candidates = []
    
    # Priority 1: ID (most stable)
    if element_data.get('id'):
        element_id_val = element_data['id']
        # Handle numeric IDs with attribute selector
        if element_id_val.isdigit():
            locator_candidates.append({
                'locator': f'[id="{element_id_val}"]',
                'type': 'id-attr',
                'priority': PRIORITY_ID,
                'strategy': 'ID attribute selector (numeric ID)'
            })
        else:
            locator_candidates.append({
                'locator': f'#{element_id_val}',  # Use CSS ID selector - Playwright native format
                'type': 'id',
                'priority': PRIORITY_ID,
                'strategy': 'ID selector from element_data'
            })
    
    # Priority 2: data-testid (very stable for testing)
    if element_data.get('dataTestId'):
        locator_candidates.append({
            'locator': f'[data-testid="{element_data["dataTestId"]}"]',
            'type': 'data-testid',
            'priority': PRIORITY_TEST_ID,
            'strategy': 'data-testid from element_data'
        })
    
    # Priority 3: name attribute
    if element_data.get('name'):
        locator_candidates.append({
            'locator': f'[name="{element_data["name"]}"]',
            'type': 'name',
            'priority': PRIORITY_NAME,
            'strategy': 'Name attribute from element_data'
        })
    
    # Priority 4: aria-label (with role if available)
    if element_data.get('ariaLabel'):
        aria_label = element_data['ariaLabel']
        role = element_data.get('role')
        if role:
            locator_candidates.append({
                'locator': f'[role="{role}"][aria-label="{aria_label}"]',
                'type': 'aria-role',
                'priority': PRIORITY_ARIA_LABEL,
                'strategy': 'ARIA label + role from element_data'
            })
        else:
            locator_candidates.append({
                'locator': f'[aria-label="{aria_label}"]',
                'type': 'aria-label',
                'priority': PRIORITY_ARIA_LABEL,
                'strategy': 'ARIA label from element_data'
            })
    
    # Priority 5: placeholder (for inputs)
    if element_data.get('placeholder'):
        locator_candidates.append({
            'locator': f'[placeholder="{element_data["placeholder"]}"]',
            'type': 'placeholder',
            'priority': PRIORITY_PLACEHOLDER,
            'strategy': 'Placeholder attribute from element_data'
        })
    
    # Priority 6: xpath (from browser-use DOM) - ONLY AS LAST RESORT
    # XPath is powerful but fragile (breaks when page structure changes)
    # 
    # IMPORTANT: If expected_text is available and no better locators were found,
    # we should NOT use xpath. Instead, return None and let TEXT-FIRST (STEP 1)
    # handle it with disambiguation logic. TEXT-FIRST can create stable locators
    # like text="Solutions" >> nth=0 which are much more maintainable.
    #
    has_semantic_locators = len(locator_candidates) > 0  # id, name, aria-label, etc.
    
    if element_data.get('xpath'):
        # Only use xpath if:
        # 1. No expected_text provided (can't use TEXT-FIRST), OR
        # 2. We have better locators to try first (id, name, etc.)
        if not expected_text or has_semantic_locators:
            xpath = element_data['xpath']
            locator_candidates.append({
                'locator': f'xpath={xpath}',
                'type': 'xpath',
                'priority': PRIORITY_XPATH_PARENT_ID,
                'strategy': 'XPath from browser-use DOM (fallback)'
            })
        else:
            # Skip xpath - let TEXT-FIRST (STEP 1) handle with disambiguation
            logger.info(f"   ⏭️ Skipping xpath - expected_text available, will use TEXT-FIRST strategy")
    
    # Try each candidate locator
    for candidate in locator_candidates:
        locator = candidate['locator']
        try:
            count = await page.locator(locator).count()
            
            if count == 1:
                # SEMANTIC VALIDATION: Verify we found the RIGHT element
                semantic_match = True
                actual_text = ""
                if expected_text:
                    semantic_match, actual_text = await _validate_semantic_match(page, locator, expected_text)
                    if not semantic_match:
                        logger.info(f"   ⚠️ {candidate['type']}: unique but text mismatch (trying next)")
                        logger.info(f"      Expected: '{expected_text}', Actual: '{actual_text}'")
                        continue  # Try next locator
                
                logger.info(f"   ✅ ELEMENT-DATA locator found: {locator}")
                logger.info(f"      Strategy: {candidate['strategy']}")
                
                # Check if this is a table element
                tag_name = element_data.get('tagName', '').lower()
                element_type = None
                if tag_name in ['td', 'th']:
                    element_type = 'table-cell'
                    logger.info(f"      Element type: table-cell (from <{tag_name}>)")
                elif tag_name == 'tr':
                    element_type = 'table-row'
                    logger.info(f"      Element type: table-row")
                
                return {
                    'element_id': element_id,
                    'description': element_description,
                    'found': True,
                    'best_locator': locator,
                    'element_type': element_type,
                    'all_locators': [{
                        'type': candidate['type'],
                        'locator': locator,
                        'priority': candidate['priority'],
                        'strategy': candidate['strategy'],
                        'count': count,
                        'unique': True,
                        'valid': True,
                        'validated': True,
                        'semantic_match': semantic_match,
                        'validation_method': 'playwright'
                    }],
                    'element_info': {
                        'tagName': element_data.get('tagName', ''),
                        'id': element_data.get('id', ''),
                        'textContent': element_data.get('textContent', ''),
                        'actual_text': actual_text,
                        'source': 'element_data'
                    },
                    'coordinates': element_data.get('coordinates', {}),
                    'validation_summary': {
                        'total_generated': len(locator_candidates),
                        'valid': 1,
                        'unique': 1,
                        'validated': 1,
                        'best_type': candidate['type'],
                        'best_strategy': candidate['strategy'],
                        'validation_method': 'playwright'
                    },
                    'validated': True,
                    'count': count,
                    'unique': True,
                    'valid': True,
                    'semantic_match': semantic_match,
                    'validation_method': 'playwright'
                }
            
            elif count > 1:
                logger.info(f"   ⚠️ {candidate['type']}: not unique (count={count})")
            else:  # count == 0
                logger.info(f"   ⚠️ {candidate['type']}: not found (count=0)")
                
        except Exception as e:
            logger.info(f"   ⚠️ {candidate['type']}: validation failed - {e}")
            continue
    
    logger.info(f"   ⚠️ ELEMENT-DATA: No unique locator found, falling back to other strategies")
    return None


async def _validate_candidate_locator(
    page,
    candidate_locator: str,
    element_id: str,
    element_description: str,
    expected_text: Optional[str],
    x: float,
    y: float
) -> Optional[dict]:
    """
    Validate an agent-provided candidate locator.
    
    Args:
        page: Playwright page object
        candidate_locator: The locator suggested by the agent
        element_id: Element identifier
        element_description: Element description
        expected_text: Expected text for semantic validation
        x, y: Coordinates (for result dict)
        
    Returns:
        Complete result dict if valid, None if invalid/failed
    """
    logger.info(f"🔍 Step 0: Validating candidate locator: {candidate_locator}")
    try:
        # Use shared conversion function from browser_service.locators
        from browser_service.locators import convert_to_playwright_locator
        
        playwright_locator, was_converted = convert_to_playwright_locator(candidate_locator)
        
        if was_converted:
            logger.info(f"   Converted to Playwright format: {playwright_locator}")
        
        count = await page.locator(playwright_locator).count()
        
        if count == 1:
            # SEMANTIC VALIDATION: Verify we found the RIGHT element
            semantic_match = True
            actual_text = ""
            if expected_text:
                semantic_match, actual_text = await _validate_semantic_match(page, playwright_locator, expected_text)
                if not semantic_match:
                    logger.warning(f"⚠️ Candidate locator is unique BUT text doesn't match!")
                    logger.warning(f"   Expected: '{expected_text}'")
                    logger.warning(f"   Actual: '{actual_text}'")
                    logger.info("   Continuing to find correct element...")
                    return None  # Continue to try other approaches
                else:
                    logger.info(f"✅ Candidate locator is unique AND semantically correct")
            
            if semantic_match:
                logger.info(f"✅ Candidate locator is unique: {playwright_locator}")
                return {
                    'element_id': element_id,
                    'description': element_description,
                    'found': True,
                    'best_locator': playwright_locator,
                    'all_locators': [{
                        'type': 'candidate',
                        'locator': playwright_locator,
                        'priority': PRIORITY_CANDIDATE,
                        'strategy': 'Agent-provided candidate' + (' (converted)' if was_converted else ''),
                        'count': count,
                        'unique': True,
                        'valid': True,
                        'validated': True,
                        'semantic_match': semantic_match,
                        'validation_method': 'playwright'
                    }],
                    'element_info': {'actual_text': actual_text} if actual_text else {},
                    'coordinates': {'x': x, 'y': y},
                    'validation_summary': {
                        'total_generated': 1,
                        'valid': 1,
                        'unique': 1,
                        'validated': 1,
                        'best_type': 'candidate',
                        'best_strategy': 'Agent-provided candidate',
                        'validation_method': 'playwright'
                    },
                    'semantic_match': semantic_match
                }
        else:
            logger.info(f"⚠️ Candidate locator not unique (count={count}): {playwright_locator}")
    except Exception as e:
        logger.warning(f"⚠️ Candidate locator validation failed: {e}")
    
    return None


async def find_unique_locator_at_coordinates(
    page,
    x: float,
    y: float,
    element_id: str,
    element_description: str,
    expected_text: Optional[str] = None,
    candidate_locator: Optional[str] = None,
    library_type: str = "browser",
    element_data: Optional[dict] = None  # Element attributes from browser-use DOM (id, class, text, etc.)
) -> dict:
    """
    Find a unique locator for an element using a semantic-first approach.

    Strategy Priority (Semantic-First):
    0. ELEMENT DATA: If element_data is provided (from browser-use DOM), generate locators from those attributes
    1. Candidate locator (if provided) - Agent's suggestion
    2. TEXT-FIRST: Semantic locators from expected_text - Most reliable, uses actual visible text
    3. SEMANTIC: Locators from description - Fallback when expected_text not available
    4. COORDINATE: Coordinate-based extraction + 21 strategies - Last resort when semantic fails

    The semantic-first approach is more reliable because:
    - Doesn't depend on viewport size or layout (centered layouts won't break it)
    - Matches what the AI "sees" (text, role, label)
    - Produces more stable locators (text=, role=, aria-label)

    SEMANTIC VALIDATION:
    - If expected_text is provided, we validate that the found element's actual text
      matches the expected text (case-insensitive, substring match)
    - This prevents "unique but wrong element" bugs where coordinates land on wrong element

    Args:
        page: Playwright page object
        x: X coordinate of element center (used as fallback)
        y: Y coordinate of element center (used as fallback)
        element_id: Element identifier (elem_1, elem_2, etc.)
        element_description: Human-readable description (primary source for semantic locators)
        expected_text: The actual visible text AI sees on the element (e.g., "Submit", "Nike Air Max 270").
                      Used for semantic validation AND for text-first locator search.
        candidate_locator: Optional locator to validate first (e.g., "id=search-input")
        library_type: "browser" or "selenium" - determines locator format
        element_data: Optional dict with element attributes from browser-use DOM:
                     {"tagName": "a", "id": "", "textContent": "Services", "href": "/services", ...}

    Returns:
        Dict with best_locator, all_locators, validation_summary, validation_method, semantic_match
    """

    logger.info(f"🎯 Finding unique locator for {element_id}")
    logger.info(f"   Description: '{element_description}'")
    if expected_text:
        logger.info(f"   Expected text: '{expected_text}'")
    if element_data:
        logger.info(f"   Element data from index: <{element_data.get('tagName', '?')}> id='{element_data.get('id', '')}' text='{element_data.get('textContent', '')[:30]}...'")
    logger.info(f"   Coordinates: ({x}, {y}) [fallback]")
    
    # ========================================
    # STEP 0: ELEMENT-DATA approach (highest priority - FASTEST)
    # ========================================
    # When element_index is provided, we already have element attributes from browser-use DOM.
    # This is the FASTEST approach - no coordinate-based JavaScript needed.
    if element_data:
        result = await _generate_locators_from_element_data(
            page, element_data, element_id, element_description, expected_text
        )
        if result:
            return result
    
    # ========================================
    # STEP 0.1: Validate candidate locator (if provided)
    # ========================================
    if candidate_locator:
        result = await _validate_candidate_locator(
            page, candidate_locator, element_id, element_description, 
            expected_text, x, y
        )
        if result:
            return result
    
    # ========================================
    # STEP 1: Try TEXT-FIRST approach (using expected_text)
    # ========================================
    # This is the MOST RELIABLE approach - uses the actual text AI sees
    # (only runs if not a table-row scenario, or if table-row detection failed)
    if expected_text and expected_text.strip():
        logger.info(f"🔍 Step 1: Trying TEXT-FIRST locators from expected_text: '{expected_text}'")
        
        text_result = await _find_element_by_expected_text(page, expected_text, element_description, x, y)
        
        if text_result:
            # text_result is now a dict with 'locator' and optionally 'element_type'
            text_locator = text_result.get('locator')
            element_type = text_result.get('element_type')  # 'checkbox', 'radio', or None
            
            logger.info(f"✅ TEXT-FIRST locator found: {text_locator}" + (f" (element_type={element_type})" if element_type else ""))
            
            # Determine strategy name based on whether it's a checkbox/radio
            if element_type:
                strategy_name = f'Checkbox/Radio INPUT locator (type={element_type})'
                locator_type = f'{element_type}-input'
            else:
                strategy_name = 'Text-first locator from expected_text'
                locator_type = 'text-first'
            
            return {
                'element_id': element_id,
                'description': element_description,
                'found': True,
                'best_locator': text_locator,
                'element_type': element_type,  # NEW: Pass element_type to caller
                'all_locators': [{
                    'type': locator_type,
                    'locator': text_locator,
                    'priority': 0,
                    'strategy': strategy_name,
                    'count': 1,
                    'unique': True,
                    'valid': True,
                    'validated': True,
                    'semantic_match': True,  # By definition, text-first is semantically correct
                    'validation_method': 'playwright'
                }],
                'element_info': {'expected_text': expected_text, 'element_type': element_type} if element_type else {'expected_text': expected_text},
                'coordinates': {'x': x, 'y': y, 'note': 'Not used - text-first approach succeeded'},
                'validation_summary': {
                    'total_generated': 1,
                    'valid': 1,
                    'unique': 1,
                    'validated': 1,
                    'best_type': locator_type,
                    'best_strategy': strategy_name,
                    'validation_method': 'playwright'
                },
                # Top-level validation fields (required by workflow validation)
                'validated': True,
                'count': 1,
                'unique': True,
                'valid': True,
                'semantic_match': True,
                'validation_method': 'playwright'
            }
        else:
            logger.info(f"⚠️ TEXT-FIRST approach failed - trying table cell locators")
    else:
        logger.info(f"⚠️ No expected_text provided - skipping TEXT-FIRST approach")
    
    # ========================================
    # STEP 2: Try SEMANTIC LOCATORS from description (fallback)
    # ========================================
    # This is a fallback when expected_text is not available or didn't work
    if element_description and element_description.strip():
        logger.info(f"🔍 Step 2: Trying SEMANTIC locators from description: '{element_description}'")
        
        semantic_locator = await _find_element_by_description(page, element_description)
        
        if semantic_locator:
            # If expected_text provided, validate that we found the right element
            semantic_match = True
            actual_text = ""
            if expected_text:
                semantic_match, actual_text = await _validate_semantic_match(page, semantic_locator, expected_text)
                if not semantic_match:
                    logger.warning(f"⚠️ Description-based locator found BUT text doesn't match!")
                    logger.warning(f"   Expected: '{expected_text}'")
                    logger.warning(f"   Actual: '{actual_text}'")
                    logger.info("   Continuing to coordinate-based approach...")
                    # Don't return - continue to try coordinates
                else:
                    logger.info(f"✅ Semantic locator is correct (text matches)")
            
            if semantic_match:
                logger.info(f"✅ Semantic locator found: {semantic_locator}")
                return {
                    'element_id': element_id,
                    'description': element_description,
                    'found': True,
                    'best_locator': semantic_locator,
                    'all_locators': [{
                        'type': 'semantic',
                        'locator': semantic_locator,
                        'priority': 0,
                        'strategy': 'Semantic locator from description',
                        'count': 1,
                        'unique': True,
                        'valid': True,
                        'validated': True,
                        'semantic_match': semantic_match,
                        'validation_method': 'playwright'
                    }],
                    'element_info': {'description': element_description, 'actual_text': actual_text} if actual_text else {'description': element_description},
                    'coordinates': {'x': x, 'y': y, 'note': 'Not used - semantic approach succeeded'},
                    'validation_summary': {
                        'total_generated': 1,
                        'valid': 1,
                        'unique': 1,
                        'validated': 1,
                        'best_type': 'semantic',
                        'best_strategy': 'Semantic locator from description',
                        'validation_method': 'playwright'
                    },
                    # Top-level validation fields (required by workflow validation)
                    'validated': True,
                    'count': 1,
                    'unique': True,
                    'valid': True,
                    'semantic_match': semantic_match,
                    'validation_method': 'playwright'
                }
        else:
            logger.info(f"⚠️ Semantic approach failed - falling back to coordinate-based approach")
    else:
        logger.info(f"⚠️ No description provided - skipping semantic approach, using coordinates")
    
    # ========================================
    # STEP 3: FALLBACK - Coordinate-based approach (21 strategies)
    # ========================================
    logger.info(f"🔍 Step 3: Using COORDINATE-based approach at ({x}, {y})")
    
    # Get the element at coordinates
    try:
        # Use Playwright to get element at coordinates (Shadow DOM aware)
        element_exists = await page.evaluate(
            SHADOW_DOM_ELEMENT_FROM_POINT_JS,
            {"x": x, "y": y}
        )

        if not element_exists:
            logger.error(f"❌ No element found at coordinates ({x}, {y})")
            return {
                "element_id": element_id,
                "description": element_description,
                "found": False,
                "error": f"No element at coordinates ({x}, {y}) and semantic approach also failed"
            }
        
        # Check if we got BODY or HTML (coordinates landed in empty space) - Shadow DOM aware
        tag_check = await page.evaluate(
            SHADOW_DOM_TAG_NAME_JS,
            {"x": x, "y": y}
        )
        
        if tag_check in ['body', 'html']:
            # Both semantic AND coordinate approaches failed
            logger.error(f"❌ Coordinates ({x}, {y}) landed on {tag_check.upper()} (empty space)")
            logger.error(f"   Both semantic and coordinate approaches failed for: {element_description}")
            return {
                'element_id': element_id,
                'description': element_description,
                'found': False,
                'error': f"Semantic approach failed and coordinates ({x}, {y}) landed on {tag_check.upper()} (empty space)",
                'coordinates': {'x': x, 'y': y},
                'validation_summary': {
                    'total_generated': 0,
                    'valid': 0,
                    'unique': 0,
                    'validated': 0,
                    'best_type': None,
                    'best_strategy': None,
                    'validation_method': 'playwright'
                }
            }

    except Exception as e:
        logger.error(f"❌ Error getting element at coordinates: {e}")
        return {
            "element_id": element_id,
            "found": False,
            "error": str(e)
        }

    # Step 2: Extract all possible attributes from the element (Shadow DOM aware)
    try:
        element_data = await page.evaluate(
            """(coords) => {
                // Shadow DOM aware element detection
                function getElementFromPointWithShadow(root, x, y) {
                    let element = root.elementFromPoint(x, y);
                    if (!element) return null;
                    while (element && element.shadowRoot) {
                        const shadowElement = element.shadowRoot.elementFromPoint(x, y);
                        if (shadowElement && shadowElement !== element) {
                            element = shadowElement;
                        } else {
                            break;
                        }
                    }
                    return element;
                }
                
                const element = getElementFromPointWithShadow(document, coords.x, coords.y);
                if (!element) return null;
                
                const rect = element.getBoundingClientRect();
                
                // Get all attributes
                const attrs = {};
                for (let attr of element.attributes) {
                    attrs[attr.name] = attr.value;
                }
                
                // Get computed role
                let computedRole = element.getAttribute('role');
                if (!computedRole) {
                    // Try to infer role from tag
                    const tagRoleMap = {
                        'button': 'button',
                        'a': 'link',
                        'input': element.type || 'textbox',
                        'textarea': 'textbox',
                        'select': 'combobox',
                        'img': 'img',
                        'h1': 'heading', 'h2': 'heading', 'h3': 'heading',
                        'nav': 'navigation',
                        'main': 'main',
                        'header': 'banner',
                        'footer': 'contentinfo'
                    };
                    computedRole = tagRoleMap[element.tagName.toLowerCase()];
                }
                
                return {
                    tagName: element.tagName.toLowerCase(),
                    id: element.id || '',
                    name: element.name || '',
                    className: element.className || '',
                    textContent: element.textContent?.trim().substring(0, 100) || '',
                    innerText: element.innerText?.trim().substring(0, 100) || '',
                    value: element.value || '',
                    placeholder: element.placeholder || '',
                    title: element.title || '',
                    alt: element.alt || '',
                    href: element.href || '',
                    src: element.src || '',
                    type: element.type || '',
                    ariaLabel: element.getAttribute('aria-label') || '',
                    ariaDescribedby: element.getAttribute('aria-describedby') || '',
                    dataTestId: element.getAttribute('data-testid') || '',
                    dataTest: element.getAttribute('data-test') || '',
                    dataQa: element.getAttribute('data-qa') || '',
                    role: computedRole || '',
                    attributes: attrs,
                    coordinates: {
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2
                    },
                    // Get parent context
                    parentId: element.parentElement?.id || '',
                    parentClass: element.parentElement?.className || '',
                    // Get position among siblings
                    siblingIndex: Array.from(element.parentElement?.children || []).indexOf(element) + 1,
                    totalSiblings: element.parentElement?.children.length || 0
                };
            }""",
            {"x": x, "y": y}
        )

        if not element_data:
            logger.error(f"❌ Could not extract element data")
            return {
                "element_id": element_id,
                "description": element_description,
                "found": False,
                "error": "Could not extract element data"
            }

        logger.info(
            f"📋 Element data: tag={element_data['tagName']}, id={element_data['id']}, text=\"{element_data['textContent'][:30]}...\"")

    except Exception as e:
        logger.error(f"❌ Error extracting element data: {e}")
        return {
            "element_id": element_id,
            "description": element_description,
            "found": False,
            "error": str(e)
        }

    # Step 3: Try multiple locator strategies in priority order
    locator_strategies = []

    # Strategy 1: ID (Priority 1 - Best)
    if element_data['id']:
        locator_strategies.append({
            'type': 'id',
            'locator': f"id={element_data['id']}",
            'priority': PRIORITY_ID,
            'strategy': 'Native ID attribute'
        })

    # Strategy 2: data-testid (Priority 2)
    if element_data['dataTestId']:
        locator_strategies.append({
            'type': 'data-testid',
            'locator': f"data-testid={element_data['dataTestId']}",
            'priority': PRIORITY_TEST_ID,
            'strategy': 'Test ID attribute'
        })

    # Strategy 3: data-test (Priority 2)
    if element_data['dataTest']:
        locator_strategies.append({
            'type': 'data-test',
            'locator': f"data-test={element_data['dataTest']}",
            'priority': PRIORITY_TEST_ID,
            'strategy': 'Test attribute'
        })

    # Strategy 4: data-qa (Priority 2)
    if element_data['dataQa']:
        locator_strategies.append({
            'type': 'data-qa',
            'locator': f"data-qa={element_data['dataQa']}",
            'priority': PRIORITY_TEST_ID,
            'strategy': 'QA attribute'
        })

    # Strategy 5: name (Priority 3)
    # Note: Browser Library (Playwright) doesn't support name= prefix
    # SeleniumLibrary supports name= prefix
    if element_data['name']:
        if library_type == "browser":
            # Browser Library: use attribute selector
            name_escaped = element_data['name'].replace('"', '\\"')
            locator_strategies.append({
                'type': 'name',
                'locator': f'[name="{name_escaped}"]',
                'priority': PRIORITY_NAME,
                'strategy': 'Name attribute'
            })
        else:
            # SeleniumLibrary: use name= prefix
            locator_strategies.append({
                'type': 'name',
                'locator': f"name={element_data['name']}",
                'priority': PRIORITY_NAME,
                'strategy': 'Name attribute'
            })

    # Strategy 6: aria-label (Priority 4)
    if element_data['ariaLabel']:
        aria_label_escaped = element_data['ariaLabel'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'aria-label',
            'locator': f'[aria-label="{aria_label_escaped}"]',
            'priority': PRIORITY_ARIA_LABEL,
            'strategy': 'ARIA label'
        })

    # Strategy 7: placeholder (Priority 5)
    if element_data['placeholder']:
        placeholder_escaped = element_data['placeholder'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'placeholder',
            'locator': f'[placeholder="{placeholder_escaped}"]',
            'priority': PRIORITY_PLACEHOLDER,
            'strategy': 'Placeholder attribute'
        })

    # Strategy 8: title (Priority 5)
    if element_data['title']:
        title_escaped = element_data['title'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'title',
            'locator': f'[title="{title_escaped}"]',
            'priority': PRIORITY_PLACEHOLDER,
            'strategy': 'Title attribute'
        })

    # Strategy 9: Text content (Priority 6)
    if element_data['innerText'] and len(element_data['innerText']) > MIN_TEXT_LENGTH:
        # Escape quotes in text
        text = element_data['innerText'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'text',
            'locator': f'text="{text}"',
            'priority': PRIORITY_TEXT,
            'strategy': 'Visible text content'
        })

    # Strategy 10: Role + Name (Priority 7)
    if element_data['role'] and element_data['innerText']:
        text = element_data['innerText'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'role',
            'locator': f'role={element_data["role"]}[name="{text}"]',
            'priority': PRIORITY_ROLE,
            'strategy': 'ARIA role with name'
        })

    # Strategy 11: CSS with parent ID context (Priority 8)
    if element_data['parentId'] and element_data['className']:
        first_class = element_data['className'].split(
        )[0] if element_data['className'] else ''
        if first_class:
            locator_strategies.append({
                'type': 'css-parent-id',
                'locator': f"#{element_data['parentId']} {element_data['tagName']}.{first_class}",
                'priority': PRIORITY_CSS_PARENT_ID,
                'strategy': 'CSS with parent ID context'
            })

    # Strategy 12: CSS with nth-child (Priority 9)
    if element_data['siblingIndex'] and element_data['parentClass']:
        first_parent_class = element_data['parentClass'].split(
        )[0] if element_data['parentClass'] else ''
        if first_parent_class:
            locator_strategies.append({
                'type': 'css-nth-child',
                'locator': f".{first_parent_class} > {element_data['tagName']}:nth-child({element_data['siblingIndex']})",
                'priority': PRIORITY_CSS_NTH_CHILD,
                'strategy': 'CSS with nth-child'
            })

    # Strategy 13: Simple CSS class (Priority 10)
    if element_data['className']:
        first_class = element_data['className'].split(
        )[0] if element_data['className'] else ''
        if first_class:
            locator_strategies.append({
                'type': 'css-class',
                'locator': f"{element_data['tagName']}.{first_class}",
                'priority': PRIORITY_CSS_CLASS,
                'strategy': 'Simple CSS class'
            })

    # Strategy 14: XPath with parent ID (Priority 11)
    if element_data['parentId']:
        locator_strategies.append({
            'type': 'xpath-parent-id',
            'locator': f"xpath=//*[@id='{element_data['parentId']}']//{element_data['tagName']}",
            'priority': PRIORITY_XPATH_PARENT_ID,
            'strategy': 'XPath with parent ID'
        })

    # Strategy 15: XPath with parent class and position (Priority 12)
    if element_data['parentClass'] and element_data['siblingIndex']:
        first_parent_class = element_data['parentClass'].split(
        )[0] if element_data['parentClass'] else ''
        if first_parent_class:
            locator_strategies.append({
                'type': 'xpath-parent-class-position',
                'locator': f"xpath=//*[contains(@class, '{first_parent_class}')]//{element_data['tagName']}[{element_data['siblingIndex']}]",
                'priority': PRIORITY_XPATH_PARENT_CLASS,
                'strategy': 'XPath with parent class and position'
            })

    # Strategy 16: XPath with text (Priority 13)
    if element_data['innerText'] and len(element_data['innerText']) > MIN_TEXT_LENGTH:
        text = element_data['innerText'].replace("'", "\\'")
        locator_strategies.append({
            'type': 'xpath-text',
            'locator': f"xpath=//{element_data['tagName']}[contains(text(), '{text[:MAX_TEXT_DISPLAY_LENGTH]}')]",
            'priority': PRIORITY_XPATH_TEXT,
            'strategy': 'XPath with text content'
        })

    # Strategy 17: XPath with title attribute (Priority 14)
    if element_data['title']:
        title = element_data['title'].replace("'", "\\'")
        locator_strategies.append({
            'type': 'xpath-title',
            'locator': f"xpath=//{element_data['tagName']}[@title='{title}']",
            'priority': PRIORITY_XPATH_TITLE,
            'strategy': 'XPath with title attribute'
        })

    # Strategy 18: XPath with href (for links) (Priority 15)
    if element_data['href'] and element_data['tagName'] == 'a':
        # Use partial href match
        href_part = element_data['href'].split('?')[0].split('#')[0]
        # Safe slicing to prevent IndexError when href_part is empty or too short
        if href_part and len(href_part) > 0:
            href_slice = href_part[-MAX_TEXT_DISPLAY_LENGTH:] if len(href_part) >= MAX_TEXT_DISPLAY_LENGTH else href_part
            locator_strategies.append({
                'type': 'xpath-href',
                'locator': f"xpath=//a[contains(@href, '{href_slice}')]",
                'priority': PRIORITY_XPATH_HREF,
                'strategy': 'XPath with href'
            })

    # Strategy 19: XPath with class and position (Priority 16)
    if element_data['className'] and element_data['siblingIndex']:
        first_class = element_data['className'].split(
        )[0] if element_data['className'] else ''
        if first_class:
            locator_strategies.append({
                'type': 'xpath-class-position',
                'locator': f"xpath=(//{element_data['tagName']}[contains(@class, '{first_class}')])[{element_data['siblingIndex']}]",
                'priority': PRIORITY_XPATH_CLASS_POSITION,
                'strategy': 'XPath with class and position'
            })

    # Strategy 20: XPath with multiple attributes (Priority 17)
    if element_data['className'] and element_data['innerText']:
        first_class = element_data['className'].split(
        )[0] if element_data['className'] else ''
        text = element_data['innerText'].replace("'", "\\'")[:30]
        if first_class and text:
            locator_strategies.append({
                'type': 'xpath-multi-attr',
                'locator': f"xpath=//{element_data['tagName']}[contains(@class, '{first_class}') and contains(text(), '{text}')]",
                'priority': PRIORITY_XPATH_MULTI_ATTR,
                'strategy': 'XPath with class and text'
            })

    # Strategy 21: XPath - first of type with class (Priority 18)
    if element_data['className']:
        first_class = element_data['className'].split(
        )[0] if element_data['className'] else ''
        if first_class:
            locator_strategies.append({
                'type': 'xpath-first-of-class',
                'locator': f"xpath=(//{element_data['tagName']}[contains(@class, '{first_class}')])[1]",
                'priority': PRIORITY_XPATH_FIRST_OF_CLASS,
                'strategy': 'XPath - first element with class'
            })

    logger.info(
        f"🔍 Generated {len(locator_strategies)} locator strategies to test")

    # Step 4: Validate each strategy
    validated_locators = []
    
    # Sort strategies by priority for optimal early exit
    # Lower priority number = better locator (1=ID is best, 18=XPath-first-of-class is worst)
    sorted_strategies = sorted(locator_strategies, key=lambda x: x['priority'])

    for idx, strategy in enumerate(sorted_strategies, 1):
        try:
            # Log strategy attempt (DEBUG level - verbose details)
            logger.info(f"🔍 Strategy {idx}/{len(sorted_strategies)}: {strategy['type']} (priority={strategy['priority']})")
            logger.info(f"   Locator: {strategy['locator']}")
            logger.info(f"   Strategy: {strategy['strategy']}")
            
            # Validate with Playwright
            count = await page.locator(strategy['locator']).count()
            
            # Determine validation status
            is_unique = (count == 1)
            is_valid = (count == 1)  # Only unique locators are valid
            
            validated_locators.append({
                **strategy,
                'count': count,
                'unique': is_unique,
                'valid': is_valid,
                'validated': True,
                'validation_method': 'playwright'
            })

            # Log validation result with detailed status
            if is_unique:
                logger.info(f"   ✅ VALID & UNIQUE: count={count}, unique={is_unique}, valid={is_valid}")
                
                # OPTIMIZATION: Early exit for high-priority unique locators
                # If we found a high-priority unique locator (ID, test-id, name), stop searching
                # Priority 1-3 are considered "high-priority" (ID, test attributes, name)
                if strategy['priority'] <= PRIORITY_NAME:  # PRIORITY_NAME = 3
                    logger.info(f"   ⚡ EARLY EXIT: High-priority unique locator found (priority={strategy['priority']})")
                    logger.info(f"   Skipping validation of {len(sorted_strategies) - idx} remaining strategies")
                    break  # Exit the loop early
                    
            elif count > 1:
                logger.info(f"   ❌ NOT UNIQUE: count={count}, unique={is_unique}, valid={is_valid}")
            elif count == 0:
                logger.info(f"   ❌ NOT FOUND: count={count}, unique={is_unique}, valid={is_valid}")
            else:
                logger.info(f"   ⚠️ UNEXPECTED: count={count}, unique={is_unique}, valid={is_valid}")

        except Exception as e:
            logger.warning(f"   ❌ VALIDATION ERROR: {type(e).__name__}: {e}")
            logger.warning(f"   Locator: {strategy['locator']}")
            validated_locators.append({
                **strategy,
                'count': 0,  # Set to 0 instead of None for consistency
                'unique': False,
                'valid': False,
                'validated': False,
                'validation_error': str(e),
                'validation_method': 'playwright'
            })

    # Step 5: Select best locator (unique, lowest priority number)
    # WITH SEMANTIC VALIDATION if expected_text is provided
    unique_locators = [loc for loc in validated_locators if loc.get(
        'valid') and loc.get('unique')]

    best_locator_obj = None  # Initialize to None
    semantic_match = True  # Assume match unless expected_text is provided
    actual_text = ""
    
    if unique_locators:
        # Sort by priority (lowest = best)
        sorted_locators = sorted(unique_locators, key=lambda x: x['priority'])
        
        # If expected_text is provided, find a locator that ALSO matches semantically
        if expected_text:
            logger.info(f"🔍 Checking semantic match for {len(sorted_locators)} unique locators...")
            
            for loc in sorted_locators:
                is_match, text = await _validate_semantic_match(page, loc['locator'], expected_text)
                loc['semantic_match'] = is_match
                loc['actual_text'] = text
                
                if is_match and best_locator_obj is None:
                    best_locator_obj = loc
                    semantic_match = True
                    actual_text = text
                    logger.info(f"   ✅ Found semantically matching locator: {loc['locator']}")
            
            # If no semantic match found, DO NOT return wrong locator - return failure instead
            if best_locator_obj is None:
                logger.error(f"   ❌ SEMANTIC MISMATCH: No locator matched expected text '{expected_text}'")
                logger.error(f"   All {len(sorted_locators)} unique locators have wrong text content")
                
                # Return failure - do not give back a wrong locator
                return {
                    'element_id': element_id,
                    'description': element_description,
                    'found': False,
                    'error': f"Semantic mismatch: Expected '{expected_text}' but none of the {len(sorted_locators)} unique locators matched",
                    'semantic_match': False,
                    'expected_text': expected_text,
                    'candidates_found': len(sorted_locators),
                    'candidate_locators': [loc['locator'] for loc in sorted_locators[:3]]  # Top 3 for debugging
                }
        else:
            # No expected_text, just use first unique locator
            best_locator_obj = sorted_locators[0]
        
        if best_locator_obj:
            best_locator = best_locator_obj['locator']
            
            # Log final selected locator with complete details
            logger.info(f"")
            logger.info(f"{'='*80}")
            logger.info(f"✅ FINAL SELECTED LOCATOR for {element_id}")
            logger.info(f"{'='*80}")
            logger.info(f"   Locator: {best_locator}")
            logger.info(f"   Type: {best_locator_obj['type']}")
            logger.info(f"   Priority: {best_locator_obj['priority']} (1=best, 18=worst)")
            logger.info(f"   Strategy: {best_locator_obj['strategy']}")
            logger.info(f"   Validation Results:")
            logger.info(f"      - count: {best_locator_obj['count']}")
            logger.info(f"      - unique: {best_locator_obj['unique']}")
            logger.info(f"      - valid: {best_locator_obj['valid']}")
            logger.info(f"      - validated: {best_locator_obj['validated']}")
            logger.info(f"      - semantic_match: {semantic_match}")
            if expected_text:
                logger.info(f"      - expected_text: '{expected_text}'")
                logger.info(f"      - actual_text: '{actual_text[:50]}...' " if len(actual_text) > 50 else f"      - actual_text: '{actual_text}'")
            logger.info(f"      - validation_method: {best_locator_obj['validation_method']}")
            logger.info(f"   Total unique locators found: {len(unique_locators)}")
            logger.info(f"{'='*80}")
            logger.info(f"")
        else:
            best_locator = None
    else:
        best_locator = None
        
        # Log failure with detailed breakdown
        logger.error(f"")
        logger.error(f"{'='*80}")
        logger.error(f"❌ NO UNIQUE LOCATOR FOUND for {element_id}")
        logger.error(f"{'='*80}")

        # Log why - categorize failures
        non_unique = [loc for loc in validated_locators if loc.get(
            'validated') and loc.get('count', 0) > 1]
        not_found = [loc for loc in validated_locators if loc.get(
            'validated') and loc.get('count', 0) == 0]
        errors = [
            loc for loc in validated_locators if not loc.get('validated')]

        logger.error(f"   Failure Breakdown:")
        if non_unique:
            logger.error(f"      - {len(non_unique)} locators matched multiple elements (not unique)")
            for loc in non_unique[:3]:  # Show first 3
                logger.error(f"         • {loc['type']}: count={loc['count']}")
        if not_found:
            logger.error(f"      - {len(not_found)} locators found no elements")
            for loc in not_found[:3]:  # Show first 3
                logger.error(f"         • {loc['type']}: {loc['locator']}")
        if errors:
            logger.error(f"      - {len(errors)} locators had validation errors")
            for loc in errors[:3]:  # Show first 3
                logger.error(f"         • {loc['type']}: {loc.get('validation_error', 'Unknown error')}")
        
        logger.error(f"   Total strategies attempted: {len(validated_locators)}")
        logger.error(f"{'='*80}")
        logger.error(f"")

    # Step 6: Build result with complete validation data
    validation_summary = {
        'total_generated': len(validated_locators),
        'valid': sum(1 for loc in validated_locators if loc.get('valid')),
        'unique': sum(1 for loc in validated_locators if loc.get('unique')),
        'validated': sum(1 for loc in validated_locators if loc.get('validated')),
        'not_found': sum(1 for loc in validated_locators if loc.get('validated') and loc.get('count', 0) == 0),
        'not_unique': sum(1 for loc in validated_locators if loc.get('validated') and loc.get('count', 0) > 1),
        'errors': sum(1 for loc in validated_locators if not loc.get('validated')),
        'best_type': best_locator_obj['type'] if best_locator_obj else None,
        'best_strategy': best_locator_obj['strategy'] if best_locator_obj else None,
        'semantic_match': semantic_match,
        'validation_method': 'playwright'
    }
    
    result = {
        'element_id': element_id,
        'description': element_description,
        'found': best_locator is not None,
        'best_locator': best_locator,
        'all_locators': validated_locators,
        'element_info': {
            'id': element_data['id'],
            'tagName': element_data['tagName'],
            'text': element_data['textContent'],
            'className': element_data['className'],
            'name': element_data['name'],
            'testId': element_data['dataTestId'],
            'actual_text': actual_text,  # Add actual text for debugging
        },
        'coordinates': element_data['coordinates'],
        'validation_summary': validation_summary,
        'semantic_match': semantic_match  # NEW: Flag indicating if actual text matches expected
    }
    
    # If semantic mismatch, add warning
    if expected_text and not semantic_match:
        result['semantic_warning'] = f"Expected '{expected_text}' but element contains '{actual_text}'"
    
    # Add validation data to the result itself for easy access
    if best_locator_obj:
        result['validated'] = True
        result['count'] = best_locator_obj.get('count', 1)
        result['unique'] = True
        result['valid'] = True
        result['validation_method'] = 'playwright'
    else:
        result['validated'] = True  # Validation was attempted
        result['count'] = 0  # No unique locator found
        result['unique'] = False
        result['valid'] = False
        result['validation_method'] = 'playwright'
    
    # Log validation summary
    logger.info(f"")
    logger.info(f"📊 VALIDATION SUMMARY for {element_id}")
    logger.info(f"   Total strategies attempted: {validation_summary['total_generated']}")
    logger.info(f"   Valid (count=1): {validation_summary['valid']}")
    logger.info(f"   Unique (count=1): {validation_summary['unique']}")
    logger.info(f"   Not found (count=0): {validation_summary['not_found']}")
    logger.info(f"   Not unique (count>1): {validation_summary['not_unique']}")
    logger.info(f"   Validation errors: {validation_summary['errors']}")
    logger.info(f"   Successfully validated: {validation_summary['validated']}")
    logger.info(f"   Semantic match: {semantic_match}")
    if expected_text and not semantic_match:
        logger.warning(f"   ⚠️ SEMANTIC MISMATCH: Expected '{expected_text}', got '{actual_text[:50]}...'")
    if best_locator_obj:
        logger.info(f"   Best locator type: {validation_summary['best_type']}")
        logger.info(f"   Best strategy: {validation_summary['best_strategy']}")
    logger.info(f"")
    
    return result
