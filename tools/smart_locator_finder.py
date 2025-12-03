"""
Smart Locator Finder
====================

Deterministic locator extraction using multiple strategies.
Given coordinates, systematically tries different approaches to find unique locators.
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


async def find_unique_locator_at_coordinates(
    page,
    x: float,
    y: float,
    element_id: str,
    element_description: str,
    candidate_locator: Optional[str] = None,
    library_type: str = "browser"
) -> Dict:
    """
    Given coordinates, systematically try multiple strategies to find a unique locator.

    Strategy Priority:
    1. Native attributes (id, name, data-testid) - Most stable
    2. ARIA attributes (aria-label, role) - Semantic
    3. Text content - Content-based
    4. CSS with context - Structural
    5. XPath with context - Last resort

    Args:
        page: Playwright page object
        x: X coordinate of element center
        y: Y coordinate of element center
        element_id: Element identifier (elem_1, elem_2, etc.)
        element_description: Human-readable description
        candidate_locator: Optional locator to validate first (e.g., "id=search-input")
        library_type: "browser" or "selenium" - determines locator format

    Returns:
        Dict with best_locator, all_locators, validation_summary, validation_method
    """

    logger.info(f"🎯 Finding unique locator for {element_id} at ({x}, {y})")
    
    # Step 0: If candidate locator provided, validate it first
    if candidate_locator:
        logger.info(f"🔍 Validating candidate locator: {candidate_locator}")
        try:
            # Use shared conversion function from browser_service.locators
            from browser_service.locators import convert_to_playwright_locator
            
            playwright_locator, was_converted = convert_to_playwright_locator(candidate_locator)
            
            if was_converted:
                logger.info(f"   Converted to Playwright format: {playwright_locator}")
            
            count = await page.locator(playwright_locator).count()
            
            if count == 1:
                logger.info(f"✅ Candidate locator is unique: {playwright_locator}")
                return {
                    'element_id': element_id,
                    'description': element_description,
                    'found': True,
                    'best_locator': playwright_locator,  # Use converted locator
                    'all_locators': [{
                        'type': 'candidate',
                        'locator': playwright_locator,  # Use converted locator
                        'priority': 0,
                        'strategy': 'Agent-provided candidate' + (' (converted)' if was_converted else ''),
                        'count': count,
                        'unique': True,
                        'valid': True,
                        'validated': True,
                        'validation_method': 'playwright'
                    }],
                    'element_info': {},
                    'coordinates': {'x': x, 'y': y},
                    'validation_summary': {
                        'total_generated': 1,
                        'valid': 1,
                        'unique': 1,
                        'validated': 1,
                        'best_type': 'candidate',
                        'best_strategy': 'Agent-provided candidate',
                        'validation_method': 'playwright'
                    }
                }
            else:
                logger.info(f"⚠️ Candidate locator not unique (count={count}): {playwright_locator}")
                logger.info(f"🔄 Continuing with 21 strategies...")
        except Exception as e:
            logger.warning(f"⚠️ Candidate locator validation failed: {e}")
            logger.info(f"🔄 Continuing with 21 strategies...")

    # Step 1: Get the element at coordinates
    try:
        # Use Playwright to get element at coordinates
        element_handle = await page.evaluate_handle(
            """(coords) => {
                return document.elementFromPoint(coords.x, coords.y);
            }""",
            {"x": x, "y": y}
        )

        if not element_handle:
            logger.error(f"❌ No element found at coordinates ({x}, {y})")
            return {
                "element_id": element_id,
                "found": False,
                "error": "No element at coordinates"
            }

    except Exception as e:
        logger.error(f"❌ Error getting element at coordinates: {e}")
        return {
            "element_id": element_id,
            "found": False,
            "error": str(e)
        }

    # Step 2: Extract all possible attributes from the element
    try:
        element_data = await page.evaluate(
            """(coords) => {
                const element = document.elementFromPoint(coords.x, coords.y);
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
                "found": False,
                "error": "Could not extract element data"
            }

        logger.info(
            f"📋 Element data: tag={element_data['tagName']}, id={element_data['id']}, text=\"{element_data['textContent'][:30]}...\"")

    except Exception as e:
        logger.error(f"❌ Error extracting element data: {e}")
        return {
            "element_id": element_id,
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
            'priority': 1,
            'strategy': 'Native ID attribute'
        })

    # Strategy 2: data-testid (Priority 2)
    if element_data['dataTestId']:
        locator_strategies.append({
            'type': 'data-testid',
            'locator': f"data-testid={element_data['dataTestId']}",
            'priority': 2,
            'strategy': 'Test ID attribute'
        })

    # Strategy 3: data-test (Priority 2)
    if element_data['dataTest']:
        locator_strategies.append({
            'type': 'data-test',
            'locator': f"data-test={element_data['dataTest']}",
            'priority': 2,
            'strategy': 'Test attribute'
        })

    # Strategy 4: data-qa (Priority 2)
    if element_data['dataQa']:
        locator_strategies.append({
            'type': 'data-qa',
            'locator': f"data-qa={element_data['dataQa']}",
            'priority': 2,
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
                'priority': 3,
                'strategy': 'Name attribute'
            })
        else:
            # SeleniumLibrary: use name= prefix
            locator_strategies.append({
                'type': 'name',
                'locator': f"name={element_data['name']}",
                'priority': 3,
                'strategy': 'Name attribute'
            })

    # Strategy 6: aria-label (Priority 4)
    if element_data['ariaLabel']:
        aria_label_escaped = element_data['ariaLabel'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'aria-label',
            'locator': f'[aria-label="{aria_label_escaped}"]',
            'priority': 4,
            'strategy': 'ARIA label'
        })

    # Strategy 7: placeholder (Priority 5)
    if element_data['placeholder']:
        placeholder_escaped = element_data['placeholder'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'placeholder',
            'locator': f'[placeholder="{placeholder_escaped}"]',
            'priority': 5,
            'strategy': 'Placeholder attribute'
        })

    # Strategy 8: title (Priority 5)
    if element_data['title']:
        title_escaped = element_data['title'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'title',
            'locator': f'[title="{title_escaped}"]',
            'priority': 5,
            'strategy': 'Title attribute'
        })

    # Strategy 9: Text content (Priority 6)
    if element_data['innerText'] and len(element_data['innerText']) > 2:
        # Escape quotes in text
        text = element_data['innerText'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'text',
            'locator': f'text="{text}"',
            'priority': 6,
            'strategy': 'Visible text content'
        })

    # Strategy 10: Role + Name (Priority 7)
    if element_data['role'] and element_data['innerText']:
        text = element_data['innerText'].replace('"', '\\"')
        locator_strategies.append({
            'type': 'role',
            'locator': f'role={element_data["role"]}[name="{text}"]',
            'priority': 7,
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
                'priority': 8,
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
                'priority': 9,
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
                'priority': 10,
                'strategy': 'Simple CSS class'
            })

    # Strategy 14: XPath with parent ID (Priority 11)
    if element_data['parentId']:
        locator_strategies.append({
            'type': 'xpath-parent-id',
            'locator': f"xpath=//*[@id='{element_data['parentId']}']//{element_data['tagName']}",
            'priority': 11,
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
                'priority': 12,
                'strategy': 'XPath with parent class and position'
            })

    # Strategy 16: XPath with text (Priority 13)
    if element_data['innerText'] and len(element_data['innerText']) > 2:
        text = element_data['innerText'].replace("'", "\\'")
        locator_strategies.append({
            'type': 'xpath-text',
            'locator': f"xpath=//{element_data['tagName']}[contains(text(), '{text[:50]}')]",
            'priority': 13,
            'strategy': 'XPath with text content'
        })

    # Strategy 17: XPath with title attribute (Priority 14)
    if element_data['title']:
        title = element_data['title'].replace("'", "\\'")
        locator_strategies.append({
            'type': 'xpath-title',
            'locator': f"xpath=//{element_data['tagName']}[@title='{title}']",
            'priority': 14,
            'strategy': 'XPath with title attribute'
        })

    # Strategy 18: XPath with href (for links) (Priority 15)
    if element_data['href'] and element_data['tagName'] == 'a':
        # Use partial href match
        href_part = element_data['href'].split('?')[0].split('#')[0]
        if href_part:
            locator_strategies.append({
                'type': 'xpath-href',
                'locator': f"xpath=//a[contains(@href, '{href_part[-50:]}')]",
                'priority': 15,
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
                'priority': 16,
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
                'priority': 17,
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
                'priority': 18,
                'strategy': 'XPath - first element with class'
            })

    logger.info(
        f"🔍 Generated {len(locator_strategies)} locator strategies to test")

    # Step 4: Validate each strategy
    validated_locators = []

    for idx, strategy in enumerate(locator_strategies, 1):
        try:
            # Log strategy attempt
            logger.info(f"🔍 Strategy {idx}/{len(locator_strategies)}: {strategy['type']} (priority={strategy['priority']})")
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
    unique_locators = [loc for loc in validated_locators if loc.get(
        'valid') and loc.get('unique')]

    best_locator_obj = None  # Initialize to None
    if unique_locators:
        best_locator_obj = sorted(
            unique_locators, key=lambda x: x['priority'])[0]
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
        logger.info(f"      - validation_method: {best_locator_obj['validation_method']}")
        logger.info(f"   Total unique locators found: {len(unique_locators)}")
        logger.info(f"{'='*80}")
        logger.info(f"")
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
        'best_type': best_locator_obj['type'] if unique_locators else None,
        'best_strategy': best_locator_obj['strategy'] if unique_locators else None,
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
            'testId': element_data['dataTestId']
        },
        'coordinates': element_data['coordinates'],
        'validation_summary': validation_summary
    }
    
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
    if best_locator_obj:
        logger.info(f"   Best locator type: {validation_summary['best_type']}")
        logger.info(f"   Best strategy: {validation_summary['best_strategy']}")
    logger.info(f"")
    
    return result
