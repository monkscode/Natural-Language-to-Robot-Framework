"""
Workflow Prompt Builder

This module builds workflow prompts for the browser automation agent.
The prompts guide the agent through the process of:
1. Navigating to target URLs
2. Finding elements using vision
3. Extracting and validating locators
4. Returning structured results

The module supports two workflow modes:
- Custom Action Mode: Uses find_unique_locator action with Playwright validation
- Legacy Mode: Uses JavaScript-based validation (backward compatibility)

Prompt Structure:
- User goal and context
- Step-by-step workflow instructions
- Element list with descriptions
- Custom action documentation (if enabled)
- Example workflows
- Critical rules and completion criteria
"""

from typing import List, Dict, Any


def build_workflow_prompt(
    user_query: str,
    url: str,
    elements: List[Dict[str, Any]],
    library_type: str = "browser",
    include_custom_action: bool = True
) -> str:
    """
    Build workflow prompt for browser-use agent.

    The agent will:
    1. Navigate to the URL
    2. Find each element using vision
    3. Get element coordinates
    4. Call find_unique_locator custom action (if enabled) OR use JavaScript validation (legacy)

    Args:
        user_query: User's goal for the workflow
        url: Target URL to navigate to
        elements: List of elements to find, each with 'id', 'description', and optional 'action'
        library_type: Robot Framework library type - "browser" (Browser Library/Playwright)
                     or "selenium" (SeleniumLibrary)
        include_custom_action: If True, include custom action instructions;
                              if False, use legacy JavaScript validation

    Returns:
        Formatted prompt string for the agent

    Example:
        >>> elements = [
        ...     {"id": "elem_1", "description": "Search input box", "action": "input"},
        ...     {"id": "elem_2", "description": "Search button", "action": "click"}
        ... ]
        >>> prompt = build_workflow_prompt(
        ...     user_query="Find search elements",
        ...     url="https://example.com",
        ...     elements=elements,
        ...     library_type="browser",
        ...     include_custom_action=True
        ... )
    """

    # Build element list
    element_list = []
    for elem in elements:
        elem_id = elem.get('id')
        elem_desc = elem.get('description')
        elem_action = elem.get('action', 'get_text')
        element_list.append(f"   - {elem_id}: {elem_desc} (action: {elem_action})")

    elements_str = "\n".join(element_list)

    if include_custom_action:
        # NEW WORKFLOW: Use custom action for locator finding
        prompt = f"""
You are completing a web automation workflow.

USER'S GOAL: {user_query}

WORKFLOW STEPS:
1. Navigate to {url}
2. Find each element listed below using your vision
3. For EACH element, call the find_unique_locator action to get a validated unique locator

ELEMENTS TO FIND:
{elements_str}

═══════════════════════════════════════════════════════════════════
CUSTOM ACTION: find_unique_locator
═══════════════════════════════════════════════════════════════════

This action finds and validates unique locators for web elements using 21 systematic strategies.
It uses Playwright validation to ensure every locator is unique (count=1).

PARAMETERS:
  • x (float, required): X coordinate of element center
  • y (float, required): Y coordinate of element center
  • element_id (str, required): Element identifier from the list above (e.g., "elem_1")
  • element_description (str, required): Human-readable description of the element
  • candidate_locator (str, optional): Your suggested locator if you can identify one
    Examples: "id=search-input", "data-testid=login-btn", "name=username"

⚠️ CRITICAL - YOU MUST CALL THIS ACTION:
  • You MUST call find_unique_locator for EVERY element in the list above
  • Call it IMMEDIATELY after you've identified the element using your vision
  • Call it IMMEDIATELY after you've obtained the element's center coordinates
  • The custom action handles ALL validation automatically

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

IMPORTANT - NO VALIDATION NEEDED FROM YOU:
  ✓ The action handles ALL validation using Playwright
  ✓ You do NOT need to check if the locator is unique
  ✓ You do NOT need to count elements
  ✓ You do NOT need to execute JavaScript
  ✓ Simply call the action and trust the validated result

═══════════════════════════════════════════════════════════════════
EXAMPLE WORKFLOW
═══════════════════════════════════════════════════════════════════

Step 1: Navigate to {url}

Step 2: Find first element using vision
  → Element: "Search input box"
  → Coordinates: x=450.5, y=320.8
  → Candidate locator identified: id=search-input

Step 3: Call the action
  find_unique_locator(
      x=450.5,
      y=320.8,
      element_id="elem_1",
      element_description="Search input box",
      candidate_locator="id=search-input"
  )

Step 4: Receive validated result
  {{
    "element_id": "elem_1",
    "found": true,
    "best_locator": "id=search-input",
    "validated": true,
    "count": 1,
    "unique": true,
    "valid": true,
    "validation_method": "playwright"
  }}

Step 5: Store result and move to next element

Step 6: Repeat for all elements in the list

Step 7: Call done() with all validated results
  {{
    "workflow_completed": true,
    "results": [
      {{
        "element_id": "elem_1",
        "found": true,
        "best_locator": "id=search-input",
        "validated": true,
        "count": 1,
        "unique": true
      }},
      {{
        "element_id": "elem_2",
        "found": true,
        "best_locator": "data-testid=product-card",
        "validated": true,
        "count": 1,
        "unique": true
      }}
    ]
  }}

═══════════════════════════════════════════════════════════════════
CRITICAL INSTRUCTIONS
═══════════════════════════════════════════════════════════════════

✓ MUST call find_unique_locator for EVERY element in the list
✓ MUST provide accurate coordinates (x, y) from your vision
✓ SHOULD provide candidate_locator if you can identify id, data-testid, or name
✓ MUST NOT validate locators yourself - the action does this
✓ MUST NOT execute JavaScript to check uniqueness - the action does this
✓ MUST NOT use querySelector, querySelectorAll, or execute_js for validation
✓ MUST NOT retry or check count - the action guarantees count=1
✓ ONLY call done() when ALL elements have validated results from the action

⛔ FORBIDDEN ACTIONS:
  • DO NOT call execute_js with querySelector to validate locators
  • DO NOT try to count elements yourself
  • DO NOT check if locators are unique yourself
  • DO NOT extract text content from elements - just find the locators
  • DO NOT use querySelector after getting the locator - just return it
  • The find_unique_locator action does ALL validation for you!

⚠️ IMPORTANT - YOUR ONLY JOB:
  • Find elements and get their validated locators
  • DO NOT extract text, click, or interact with elements
  • DO NOT verify the locator works by using it
  • Just call find_unique_locator and store the result
  • The locators will be used later in Robot Framework tests

⚠️ IMPORTANT - NUMERIC IDs:
  • If you find an element with ID starting with a number (e.g., id="892238219")
  • DO NOT try to use querySelector('#892238219') - this is INVALID CSS
  • INSTEAD: Call find_unique_locator with candidate_locator="id=892238219"
  • The custom action will handle numeric IDs correctly using [id="..."] syntax
  • DO NOT try to extract text using the locator - just return the locator itself

COMPLETION CRITERIA:
  • ALL elements must have validated results from find_unique_locator action
  • Each result must have: validated=true, count=1, unique=true, valid=true
  • Call done() with complete JSON structure containing all results
  • DO NOT extract text or interact with elements - just return the locators

⚠️ CRITICAL - DO NOT EXTRACT TEXT:
  • After getting the locator from find_unique_locator, DO NOT use it
  • DO NOT call execute_js to extract text using the locator
  • DO NOT verify the locator by using querySelector
  • Just store the locator and move to the next element
  • The locators will be used in Robot Framework tests, not by you

Your final done() call MUST include the complete JSON with all elements_found data!
DO NOT extract text content - just return the validated locators!
"""
    else:
        # LEGACY WORKFLOW: Use JavaScript validation (backward compatibility)
        prompt = f"""
You are completing a web automation workflow.

USER'S GOAL: {user_query}

WORKFLOW STEPS:
1. Navigate to {url}
2. Find each element listed below using your vision
3. For EACH element, return its center coordinates (x, y)

ELEMENTS TO FIND:
{elements_str}

CRITICAL INSTRUCTIONS:
1. Use your vision to identify each element on the page
2. For EACH element, use execute_js to get its DOM ID and coordinates
3. Execute this JavaScript for each element you find:
   ```javascript
   (function() {{
     const element = document.querySelector('YOUR_SELECTOR_HERE');
     if (element) {{
       const rect = element.getBoundingClientRect();
       const domId = element.id || '';
       const domName = element.name || '';
       const domClass = element.className || '';
       const domTestId = element.getAttribute('data-testid') || '';

       // VALIDATE LOCATORS: Check uniqueness
       const locators = [];

       // Check ID locator
       if (domId) {{
         // Always use attribute selector for IDs (handles numeric IDs correctly)
         const idCount = document.querySelectorAll(`[id="${{domId}}"]`).length;
         locators.push({{
           type: 'id',
           locator: `id=${{domId}}`,
           count: idCount,
           unique: idCount === 1,
           validated: true,
           note: 'Using [id="..."] selector (works with numeric IDs)'
         }});
       }}

       // Check name locator
       if (domName) {{
         const nameCount = document.querySelectorAll(`[name="${{domName}}"]`).length;
         locators.push({{
           type: 'name',
           locator: `name=${{domName}}`,
           count: nameCount,
           unique: nameCount === 1,
           validated: true
         }});
       }}

       // Check data-testid locator
       if (domTestId) {{
         const testIdCount = document.querySelectorAll(`[data-testid="${{domTestId}}"]`).length;
         locators.push({{
           type: 'data-testid',
           locator: `data-testid=${{domTestId}}`,
           count: testIdCount,
           unique: testIdCount === 1,
           validated: true
         }});
       }}

       // Check CSS class locator
       if (domClass) {{
         const firstClass = domClass.split(' ')[0];
         const tagName = element.tagName.toLowerCase();
         const cssCount = document.querySelectorAll(`${{tagName}}.${{firstClass}}`).length;
         locators.push({{
           type: 'css-class',
           locator: `${{tagName}}.${{firstClass}}`,
           count: cssCount,
           unique: cssCount === 1,
           validated: true
         }});
       }}

       return JSON.stringify({{
         element_id: "REPLACE_WITH_ELEM_ID_FROM_LIST",
         found: true,
         coordinates: {{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }},
         element_type: element.tagName.toLowerCase(),
         visible_text: element.textContent.trim().substring(0, 100),
         dom_id: domId,
         dom_attributes: {{
           id: domId,
           name: domName,
           class: domClass,
           'data-testid': domTestId
         }},
         locators: locators
       }});
     }}
     return JSON.stringify({{ element_id: "REPLACE_WITH_ELEM_ID", found: false }});
   }})()
   ```

4. **CRITICAL VALIDATION STEP:** After executing JavaScript for each element, CHECK the locators:
   - Look at the "locators" array in the JavaScript result
   - Find locators where "unique": true AND "count": 1
   - If NO unique locator found for an element, try a DIFFERENT selector and execute JavaScript again
   - Keep trying different selectors until you find a unique locator (count=1)

5. ONLY call done() when ALL elements have at least ONE unique locator (count=1)
   ```json
   {{
     "workflow_completed": true,
     "elements_found": [
       {{ "element_id": "elem_1", "found": true, "coordinates": {{"x": 450, "y": 320}}, "dom_id": "search-input", ... }},
       {{ "element_id": "elem_2", "found": true, "coordinates": {{"x": 650, "y": 520}}, "dom_id": "product-link", ... }}
     ]
   }}
   ```

CRITICAL RULES:
- You MUST execute JavaScript for EACH element to get its DOM attributes
- You MUST CHECK if locators are unique (count=1) in the JavaScript result
- If a locator is NOT unique (count>1), try a DIFFERENT selector (more specific)
- ONLY call done() when ALL elements have at least ONE unique locator
- You MUST include the element_id from the list above in each result
- You MUST call done() with the complete JSON structure
- DO NOT just say "I found it" - you MUST return the structured JSON
- The JSON MUST include all elements from the list above

UNIQUENESS REQUIREMENT:
- A locator is ONLY valid if count=1 (unique)
- If count>1, the locator matches multiple elements and is NOT usable
- You MUST find a unique locator for each element before calling done()
- Try more specific selectors: id > data-testid > name > specific CSS > XPath

Your final done() call MUST include the complete JSON with all elements_found data!
REMEMBER: ONLY call done() when ALL elements have at least ONE unique locator (count=1)!
"""

    return prompt.strip()
