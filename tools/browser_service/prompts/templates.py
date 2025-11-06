"""
Prompt Templates

This module contains reusable prompt fragments and templates used across
workflow and system prompts. These constants help maintain consistency
and make it easier to update common instructions.

Template Variables:
- {url}: Target URL to navigate to
- {user_query}: User's goal or objective
- {elements_str}: Formatted list of elements to find
- {elem_id}: Element identifier (e.g., "elem_1")
- {elem_desc}: Element description
- {elem_action}: Action to perform on element

Usage:
    Import these constants in workflow.py or system.py to build prompts
    with consistent messaging and formatting.
"""

# Custom Action Documentation
CUSTOM_ACTION_HEADER = """
═══════════════════════════════════════════════════════════════════
CUSTOM ACTION: find_unique_locator
═══════════════════════════════════════════════════════════════════

This action finds and validates unique locators for web elements using 21 systematic strategies.
It uses Playwright validation to ensure every locator is unique (count=1).
"""

CUSTOM_ACTION_PARAMETERS = """
PARAMETERS:
  • x (float, required): X coordinate of element center
  • y (float, required): Y coordinate of element center
  • element_id (str, required): Element identifier from the list above (e.g., "elem_1")
  • element_description (str, required): Human-readable description of the element
  • candidate_locator (str, optional): Your suggested locator if you can identify one
    Examples: "id=search-input", "data-testid=login-btn", "name=username"
"""

CUSTOM_ACTION_HOW_IT_WORKS = """
HOW IT WORKS:
  1. If you provide a candidate_locator, the action validates it first with Playwright
  2. If the candidate is unique (count=1), it returns immediately - FAST!
  3. If the candidate is not unique or not provided, it tries 21 strategies:
     - Priority 1: id, data-testid, name (most stable)
     - Priority 2: aria-label, placeholder, title (semantic)
     - Priority 3: text content, role (content-based)
     - Priority 4-21: CSS and XPath strategies (fallbacks)
  4. Each strategy is validated with Playwright to ensure count=1
  5. Returns the first unique locator found
"""

CUSTOM_ACTION_RETURN_VALUE = """
WHAT YOU RECEIVE:
The action returns a validated result with these fields:
  • validated: true (always - validation was performed)
  • count: 1 (guaranteed - only unique locators are returned)
  • unique: true (guaranteed - count equals 1)
  • valid: true (guaranteed - locator is usable)
  • best_locator: "id=search-input" (the validated locator string)
  • validation_method: "playwright" (how it was validated)
  • element_id: "elem_1" (matches your input)
  • found: true (element was found and locator extracted)
"""

CUSTOM_ACTION_NO_VALIDATION_NEEDED = """
IMPORTANT - NO VALIDATION NEEDED FROM YOU:
  ✓ The action handles ALL validation using Playwright
  ✓ You do NOT need to check if the locator is unique
  ✓ You do NOT need to count elements
  ✓ You do NOT need to execute JavaScript
  ✓ Simply call the action and trust the validated result
"""

# Critical Instructions
CRITICAL_MUST_CALL_ACTION = """
⚠️ CRITICAL - YOU MUST CALL THIS ACTION:
  • You MUST call find_unique_locator for EVERY element in the list above
  • Call it IMMEDIATELY after you've identified the element using your vision
  • Call it IMMEDIATELY after you've obtained the element's center coordinates
  • The custom action handles ALL validation automatically
"""

FORBIDDEN_ACTIONS = """
⛔ FORBIDDEN ACTIONS:
  • DO NOT call execute_js with querySelector to validate locators
  • DO NOT try to count elements yourself
  • DO NOT check if locators are unique yourself
  • DO NOT extract text content from elements - just find the locators
  • DO NOT use querySelector after getting the locator - just return it
  • The find_unique_locator action does ALL validation for you!
"""

YOUR_ONLY_JOB = """
⚠️ IMPORTANT - YOUR ONLY JOB:
  • Find elements and get their validated locators
  • DO NOT extract text, click, or interact with elements
  • DO NOT verify the locator works by using it
  • Just call find_unique_locator and store the result
  • The locators will be used later in Robot Framework tests
"""

NUMERIC_IDS_WARNING = """
⚠️ IMPORTANT - NUMERIC IDs:
  • If you find an element with ID starting with a number (e.g., id="892238219")
  • DO NOT try to use querySelector('#892238219') - this is INVALID CSS
  • INSTEAD: Call find_unique_locator with candidate_locator="id=892238219"
  • The custom action will handle numeric IDs correctly using [id="..."] syntax
  • DO NOT try to extract text using the locator - just return the locator itself
"""

DO_NOT_EXTRACT_TEXT = """
⚠️ CRITICAL - DO NOT EXTRACT TEXT:
  • After getting the locator from find_unique_locator, DO NOT use it
  • DO NOT call execute_js to extract text using the locator
  • DO NOT verify the locator by using querySelector
  • Just store the locator and move to the next element
  • The locators will be used in Robot Framework tests, not by you
"""

# Completion Criteria
COMPLETION_CRITERIA_CUSTOM_ACTION = """
COMPLETION CRITERIA:
  • ALL elements must have validated results from find_unique_locator action
  • Each result must have: validated=true, count=1, unique=true, valid=true
  • Call done() with complete JSON structure containing all results
  • DO NOT extract text or interact with elements - just return the locators
"""

COMPLETION_CRITERIA_LEGACY = """
COMPLETION CRITERIA:
- ONLY call done() when ALL elements have unique locators (count=1)
- Include success=True if all elements have unique locators
- Include success=False if you cannot find unique locators for some elements
"""

# Workflow Steps
WORKFLOW_STEPS_CUSTOM_ACTION = """
WORKFLOW STEPS:
1. Navigate to {url}
2. Find each element listed below using your vision
3. For EACH element, call the find_unique_locator action to get a validated unique locator
"""

WORKFLOW_STEPS_LEGACY = """
WORKFLOW STEPS:
1. Navigate to {url}
2. Find each element listed below using your vision
3. For EACH element, return its center coordinates (x, y)
"""

# Example JSON Structures
EXAMPLE_RESULT_JSON = """
{
  "element_id": "elem_1",
  "found": true,
  "best_locator": "id=search-input",
  "validated": true,
  "count": 1,
  "unique": true,
  "valid": true,
  "validation_method": "playwright"
}
"""

EXAMPLE_WORKFLOW_COMPLETION_JSON = """
{
  "workflow_completed": true,
  "results": [
    {
      "element_id": "elem_1",
      "found": true,
      "best_locator": "id=search-input",
      "validated": true,
      "count": 1,
      "unique": true
    },
    {
      "element_id": "elem_2",
      "found": true,
      "best_locator": "data-testid=product-card",
      "validated": true,
      "count": 1,
      "unique": true
    }
  ]
}
"""

# Validation Rules
UNIQUENESS_REQUIREMENT = """
UNIQUENESS REQUIREMENT:
- A locator is ONLY valid if count=1 (unique)
- If count>1, the locator matches multiple elements and is NOT usable
- You MUST find a unique locator for each element before calling done()
- Try more specific selectors: id > data-testid > name > specific CSS > XPath
"""

# Locator Priority Order
LOCATOR_PRIORITY_ORDER = """
LOCATOR PRIORITY ORDER:
1. id (most stable, unique identifier)
2. data-testid (designed for testing)
3. name (semantic attribute)
4. aria-label (accessibility attribute)
5. placeholder (form field hint)
6. title (tooltip text)
7. text content (visible text)
8. role (Playwright-specific)
9. CSS class (lower priority, may not be unique)
10. XPath (fallback for complex cases)
"""

# System Prompt Fragments
VERIFICATION_RULES = """
⚠️ CRITICAL VERIFICATION RULES:
   1. When find_unique_locator returns validated=true, the locator is UNIQUE (count=1)
   2. You MUST verify the locator points to the CORRECT element (matches description)
   3. If locator is unique BUT wrong element → Try again with different coordinates
   4. If locator is unique AND correct element → Mark SUCCESS, move to next element
   5. Maximum 2 retries per element - if still wrong, mark as failed and move on
"""

RETRY_LOGIC = """
RETRY LOGIC:
- Retry 1: If locator is unique but wrong element, try different coordinates
- Retry 2: If still wrong, try one more time with more accurate coordinates
- After 2 retries: Mark element as failed, move to next element
- This prevents infinite loops while allowing correction of coordinate errors
"""

VALIDATION_GUARANTEE = """
VALIDATION GUARANTEE:
- The find_unique_locator action validates UNIQUENESS (count=1) using Playwright
- It does NOT validate CORRECTNESS (whether it's the right element)
- You MUST verify the locator points to the element matching the description
- If unique but wrong element: Your coordinates were off, retry with better coordinates
- If unique and correct element: Success, move to next element
- Maximum 2 retries per element to avoid infinite loops
"""
