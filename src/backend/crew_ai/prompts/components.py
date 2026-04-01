"""
Prompt Components Module for CrewAI Tasks.

This module contains reusable, modular prompt building blocks that are used
across different CrewAI tasks. Each component is a self-contained prompt section
that can be composed together to build complete task prompts.

Benefits:
- Single source of truth for shared instructions
- Easy to maintain and update
- Enables future plug-and-play functionality (conditional prompt inclusion)
- Reduces token consumption by avoiding duplication

Usage:
    from .prompts import PromptComponents
    
    prompt = f"{PromptComponents.ELEMENT_DESCRIPTION_RULES}..."
"""


class PromptComponents:
    """
    Modular, reusable prompt building blocks for CrewAI tasks.
    
    Components are organized into categories:
    - SHARED: Rules used across multiple tasks
    - PLANNING: Specific to plan_steps_task
    - IDENTIFICATION: Specific to identify_elements_task
    - ASSEMBLY: Specific to assemble_code_task
    """
    
    # ═══════════════════════════════════════════════════════════════════════════
    # SHARED COMPONENTS - Used across multiple tasks
    # ═══════════════════════════════════════════════════════════════════════════
    
    ELEMENT_DESCRIPTION_RULES = """
--- ELEMENT DESCRIPTION BEST PRACTICES ---
⚠️ **CRITICAL FOR VISION-BASED ELEMENT DETECTION**:
Element descriptions must be SPECIFIC and include SPATIAL/CONTEXTUAL clues to help vision AI accurately locate elements.

**BAD (Too Generic - Ambiguous)**:
- "button" ❌ (Which button? Where?)
- "text field" ❌ (Multiple text fields exist)
- "first item" ❌ (First item where? In which container?)
- "link" ❌ (Too many links on a page)

**GOOD (Specific with Spatial Context)**:
- "submit button in the main form area" ✅
- "email text field in the login form" ✅
- "first item in the main content list (center area)" ✅
- "documentation link in the footer navigation" ✅

**Key Principles for ALL Scenarios**:
1. **Add location context**: Specify WHERE the element is located
   - Page regions: "in header", "in footer", "in sidebar", "in main content area"
   - Relative position: "below the title", "next to the image", "above the form"
   - Container context: "in the navigation menu", "in the product list", "in the dialog box"

2. **Exclude ambiguous areas**: Clarify what to avoid
   - "in main content (not in sidebar)"
   - "in the form (not in header)"
   - "in the results area (not in filters)"

3. **Be specific about element type and purpose**:
   - Instead of "button" → "login submit button"
   - Instead of "text" → "article title text"
   - Instead of "input" → "search query input field"

4. **For lists/grids, specify container and position**:
   - "first item in the search results list"
   - "third card in the features grid"
   - "last option in the dropdown menu"

5. **For forms, include field purpose**:
   - "username input field in login form"
   - "confirm password field"
   - "subscribe checkbox at form bottom"

6. **⚠️ CRITICAL: For checkboxes and radio buttons**:
   - ALWAYS describe the INPUT element, not just the label text!
   - Checkboxes/radios have two parts: the clickable INPUT and the text label
   - Clicking the label text may NOT toggle the checkbox if no <label> association exists
   - Use explicit INPUT element descriptions:
     * "checkbox 1" → "the checkbox INPUT element for 'checkbox 1'" ✅
     * "remember me" → "the checkbox INPUT element for 'remember me'" ✅
     * "male option" → "the radio button INPUT element for 'male'" ✅
     * "agree to terms" → "the checkbox INPUT element for 'agree to terms'" ✅
   - This ensures the actual clickable input control is targeted, not just text
"""

    LOCATOR_USAGE_RULES = """
⚠️ **CRITICAL LOCATOR USAGE RULE** ⚠️
When mapping locators to steps:
1. Use ONLY the 'best_locator' value from locator_mapping
2. DO NOT analyze or select from 'all_locators' array
3. DO NOT override with your own preference
4. DO NOT second-guess the locator selection
5. The 'best_locator' has already been:
   - AI-detected with vision on actual page
   - Validated with Playwright (unique & working)
   - Scored by quality (ID=100, text=65, XPath=18)
   - Re-ranked to select optimal option
6. Even if you see a 'better' locator in all_locators, IGNORE IT
7. Your ONLY job is to copy best_locator values to steps

Process:
- Go through each test step again
- If step needed a locator (e.g., elem_1, elem_2, elem_3):
  * Add 'locator' key to that step's JSON
  * Use the 'best_locator' value EXACTLY from locator_mapping
  * DO NOT modify, analyze, or substitute the locator
  * ALSO add 'element_type' from the response (e.g., 'input', 'select')
- If step didn't need a locator (Open Browser, Close Browser):
  * Leave it as-is (no locator key needed)
"""

    # ═══════════════════════════════════════════════════════════════════════════
    # ASSEMBLY COMPONENTS - Used in assemble_code_task
    # ═══════════════════════════════════════════════════════════════════════════

    VARIABLE_DECLARATION_RULES = """
--- CRITICAL: VARIABLE DECLARATION RULES ---
1. **ALWAYS include *** Variables *** section** (even if empty)
2. **Declare ALL variables before use:**
   - If Open Browser step has 'browser' key → ${browser}    <value from step>
   - If Open Browser step has 'options' key → ${options}    <value from step>
   - For each element with locator → ${elem_X_locator}    <locator value>
   - For Get Text results → ${variable_name}    (no initial value needed)

3. **Variable Naming Convention:**
   - Browser config: ${browser}, ${options}
   - Element locators: ${search_box_locator}, ${product_name_locator}
   - Retrieved values: ${product_name}, ${product_price}, ${result}

4. **Extracting Values from Steps:**
   - Look for 'browser' key in Open Browser step → use as ${browser} value
   - Look for 'options' key in Open Browser step → use as ${options} value
   - Look for 'locator' key in each step → declare as ${elem_X_locator}

**Example Variable Extraction:**
If you receive:
```json
{
  "keyword": "Open Browser",
  "value": "https://www.flipkart.com",
  "browser": "chrome",
  "options": "add_argument('--headless')"
}
```
You MUST declare:
```robot
*** Variables ***
${browser}    chrome
${options}    add_argument('--headless')
```
"""

    USE_PROVIDED_LOCATORS_RULES = """
--- CRITICAL: USE PROVIDED LOCATORS EXACTLY (NO EXCEPTIONS) ---
⚠️ **MOST IMPORTANT RULE FOR LOCATORS** ⚠️

The locators provided have been:
- Found by AI vision on the actual webpage
- Validated to work correctly
- Scored and prioritized (ID > data-testid > name > aria-label > text > XPath)
- Selected as the BEST option for stability

**YOU MUST:**
1. Copy the EXACT locator value from the 'locator' field
2. DO NOT modify, improve, or convert the locator
3. DO NOT change id=X to xpath=//*[@id='X']
4. DO NOT change any locator format
5. If you think a locator is wrong, USE IT ANYWAY and add a comment

**WHY THIS IS CRITICAL:**
- The locator was validated on the actual page
- Changing it will break the test
- The scoring system already selected the best option
- Your job is code assembly, not locator optimization

**EXAMPLES:**
✅ CORRECT:
Input: {"locator": "id=submit-btn"}
Output: ${submit_locator}    id=submit-btn

❌ WRONG (DO NOT DO THIS):
Input: {"locator": "id=submit-btn"}
Output: ${submit_locator}    xpath=//*[@id='submit-btn']  ← WRONG!

❌ WRONG (DO NOT DO THIS):
Input: {"locator": "xpath=//button[1]"}
Output: ${submit_locator}    id=submit-btn  ← WRONG! Use provided XPath!

❌ WRONG (DO NOT DO THIS):
Input: {"locator": "name=q"}
Output: ${search_locator}    id=search-box  ← WRONG! Use provided name locator!

⚠️ REMEMBER: Locators are pre-validated and pre-scored. DO NOT modify them! ⚠️
"""

    LOCATOR_MAPPING_RULES = """
--- LOCATOR MAPPING RULES ---
For each step that needs a locator:
1. Check if 'locator' key exists and 'found' is true
2. If found: Declare locator as variable and use it EXACTLY as provided
3. If NOT found (found=false or error present):
   a. Add comment: # WARNING: Locator not found for <element_description>
   b. Use placeholder: xpath=//PLACEHOLDER_FOR_<element_id>
   c. Still generate syntactically valid code

**Example for found locator:**
```robot
*** Variables ***
${search_box_locator}    id=search-input  # ← Use EXACT value from 'locator' field

*** Test Cases ***
Test
    Input Text    ${search_box_locator}    shoes
```

**Example for missing locator:**
```robot
*** Variables ***
${product_locator}    xpath=//PLACEHOLDER_FOR_elem_2

*** Test Cases ***
Test
    # WARNING: Locator not found for 'first product name'
    # Manual intervention required: Inspect page and update locator
    ${product_name}=    Get Text    ${product_locator}
```
"""

    VALIDATION_RULES = """
--- CRITICAL RULES FOR VALIDATION ---

**⚠️ TEXT VALIDATION (Web Element Text)**
Text from web elements often contains newlines and special characters.
NEVER use `Should Be True 'X' in "${text}"` - it breaks with newlines!

- ❌ WRONG: `Should Be True    'Cierra' in "${row_text}"`  (SyntaxError if text has \\n)
- ✅ CORRECT: `Should Contain    ${row_text}    Cierra`  (handles newlines safely)

**Rule:** For ANY text validation from Get Text or Get Elements, use `Should Contain`.

**NUMERIC VALIDATION (Prices, Counts, etc.)**
When you encounter a step with keyword 'Should Be True' and a 'condition_expression' key:
1. Generate a proper Should Be True statement with the expression
2. The expression should be a valid Python expression that Robot Framework can evaluate
3. Use proper Python string methods for text manipulation

**Example for price validation:**
*Input Step:*
`{"keyword": "Should Be True", "condition_expression": "${float(product_price.replace('₹', '').replace(',', '')) < 9999}"}`
*Output Code:*
`    ${price_numeric}=    Evaluate    float('${product_price}'.replace('₹', '').replace(',', ''))`
`    Should Be True    ${price_numeric} < 9999`
"""

    CONDITIONAL_LOGIC_HANDLING = """
--- HANDLING CONDITIONAL LOGIC ---
If a step in the context contains the keys `condition_type` and `condition_value`, you MUST use the `Run Keyword If` keyword from Robot Framework's BuiltIn library. The format should be: `Run Keyword If    ${condition_value}    Keyword    argument1    argument2`

**Example:**
*Input Step:*
`{"keyword": "Input Text", "locator": "id=discount-code", "value": "SAVE10", "condition_type": "IF", "condition_value": "${total} > 100"}`
*Output Code:*
`    Run Keyword If    ${total} > 100    Input Text    id=discount-code    SAVE10`
"""

    LOOP_HANDLING = """
--- HANDLING LOOPS AND COLLECTIONS ---
If a step in the context contains the keys `loop_type` and `loop_source`, you MUST use a `FOR` loop.

**CRITICAL: Collection Pattern (Get Elements → FOR loop)**
When you see a "Get Elements" step followed by a step with `loop_type`/`loop_source`, generate:

*Input Steps:*
```
Step 1: {"keyword": "Get Elements", "element_description": "all data rows", "locator": ".rt-tr-group"}
Step 2: {"keyword": "Get Text", "loop_type": "FOR", "loop_source": "table_rows"}
Step 3: {"keyword": "Should Contain", "value": "expected_text"}
```

*Output Code:*
```robot
    @{elements}=    Get Elements    ${rows_locator}
    FOR    ${element}    IN    @{elements}
        ${text}=    Get Text    ${element}
        Exit For Loop If    len($text.strip()) == 0
        Should Contain    ${text}    expected_text
    END
```

**⚠️ CRITICAL: Newline-Safe Patterns**
Text from web elements often contains newlines and whitespace. AVOID Python expressions with string literals:
- ❌ WRONG: `Continue For Loop If    '${text}' == ''`  (breaks with newlines)
- ❌ WRONG: `Should Be True    'X' in '${text}'`  (breaks with newlines)
- ✅ CORRECT: `Exit For Loop If    len($text.strip()) == 0`  (uses $var for Python object)
- ✅ CORRECT: `Should Contain    ${text}    expected_text`

**NOTE:** Use `$variable` (no curly braces) to pass variable as Python object, allowing `.strip()` method.

**Simple Loop Example:**
*Input Step:*
`{"keyword": "Click Element", "loop_type": "FOR", "loop_source": "@{links}"}`
*Output Code:*
`    FOR    ${link}    IN    @{links}`
`        Click Element    ${link}`
`    END`

**Key Rules:**
1. Use `@{variable}` (list notation) for Get Elements return value
2. Exit on empty/whitespace: `Exit For Loop If    len($text.strip()) == 0`
3. Use `Should Contain` for text validation (NOT `Should Be True 'X' in 'Y'`)
4. Use `${element}` as the loop variable inside FOR
5. Always close with `END`
"""

    DROPDOWN_HANDLING = """
--- HANDLING DROPDOWNS BASED ON element_type ---
⚠️ **CRITICAL**: Check the 'element_type' and 'role' fields to choose the correct interaction pattern!

Dropdowns come in 3 types, each requiring different Robot Framework keywords:

**TYPE 1: Native HTML Select (element_type='select')**
Use standard Select Options By keyword:
```robot
Select Options By    ${dropdown_locator}    label    Option Text
```

**TYPE 2: Combobox Input (element_type='input', typically role='combobox')**
These are searchable/filterable dropdowns. Use Fill Text + Enter pattern:
```robot
# Type the option text to filter, then press Enter to select
Fill Text    ${dropdown_locator}    Volvo
Keyboard Key    press    Enter
```

**TYPE 3: Click-based Trigger (element_type='span', 'button', or 'div' without combobox role)**
These require clicking the trigger first, then clicking the option text:
```robot
# Click the dropdown trigger to open options
Click    ${dropdown_locator}
# Click the option - Browser Library auto-waits for visibility
Click    <iframe_prefix> >>> text=<option_text>
```

⚠️ **CRITICAL: `>>` vs `>>>` Syntax**
- `>>>` = **Frame entry** (enters an iframe context) - USE THIS for iframe prefixes
- `>>` = **Selector chaining** (combines selectors, stays in same context) - NOT for iframe entry!

⚠️ **IMPORTANT**: If the dropdown locator contains `>>>` (frame entry syntax), you MUST extract and use the same iframe prefix for the text click!
- Pattern: `<iframe_prefix> >>> <element_selector>` → text click: `<iframe_prefix> >>> text=<value>`
- Valid iframe prefix examples: `iframe[id="iframeMain"]`, `iframe[id="contentFrame"]`, `iframe[name="main"]`
- **WRONG**: `iframe >> nth=0` (this uses `>>` which is selector chaining, NOT frame entry)

--- DECISION LOGIC ---
When you see a dropdown-related step (Select Options By, dropdown, select):

1. **IF element_type='select'** → Use Select Options By keyword
   
2. **IF element_type='input'** (usually role='combobox') → Use Fill Text + Enter
   - Extract option text from value (e.g., 'label    Volvo' → 'Volvo')
   
3. **IF element_type='span', 'button', or 'div'** (without role='combobox') → Use Click + Click text
   - First: Click the trigger to open the dropdown
   - Then: Click the option text
   - **CRITICAL IFRAME RULE**: If locator contains `>>>`, extract the prefix and use it:
     - Locator: `iframe[id="iframeMain"] >>> [role='button']` → prefix is `iframe[id="iframeMain"]`
     - Locator: `iframe[name="content"] >>> .dropdown` → prefix is `iframe[name="content"]`
     - Text click: `<prefix> >>> text=<value>`

--- EXAMPLES ---

*Example 1 - Native Select (element_type='select'):*
Input: `{"locator": "id=country", "element_type": "select", "value": "label    USA"}`
Output:
```robot
    Select Options By    id=country    label    USA
```

*Example 2 - Combobox Input (element_type='input'):*
Input: `{"locator": "id=react-select-input", "element_type": "input", "value": "label    Volvo"}`
Output:
```robot
    # Custom dropdown (element_type=input) - using Fill Text+Enter pattern
    Fill Text    id=react-select-input    Volvo
    Keyboard Key    press    Enter
```

*Example 3 - Click-based Trigger with IFRAME (element_type='span'):*
Input: `{"locator": "iframe[id=\"iframeMain\"] >>> [role='button']", "element_type": "span", "value": "ConfigAM"}`
Output:
```robot
    # Custom dropdown (element_type=span) - Click trigger then option
    Click    iframe[id="iframeMain"] >>> [role='button']
    Click    iframe[id="iframeMain"] >>> text=ConfigAM
```

*Example 4 - Click-based Trigger with DIFFERENT IFRAME:*
Input: `{"locator": "iframe[name=\"content\"] >>> .dropdown-trigger", "element_type": "button", "value": "Option2"}`
Output:
```robot
    Click    iframe[name="content"] >>> .dropdown-trigger
    Click    iframe[name="content"] >>> text=Option2
```

*Example 5 - Click-based Trigger WITHOUT iframe:*
Input: `{"locator": "[role='button']", "element_type": "span", "value": "Option1"}`
Output:
```robot
    Click    [role='button']
    Click    text=Option1
```

**NOTE**: For 'label    X' values, extract just 'X' for Fill Text or Click text patterns.
"""

    CHECKBOX_RADIO_HANDLING = """
--- HANDLING HIDDEN RADIO BUTTONS AND CHECKBOXES ---
⚠️ **CRITICAL**: Check the 'element_type' field for radio/checkbox elements!

Modern CSS frameworks often HIDE the actual input element with CSS.
Standard 'Click' may FAIL because the input is not visible.

**When element_type is 'radio' or 'checkbox':**
1. Use keyword_search tool to search for 'click hidden element force'
2. The tool will return the correct keyword with force=True option
3. Use that keyword syntax in your generated code

**When element_type is something else (button, link, div, etc.):**
Use standard Click keyword.
"""

    # ═══════════════════════════════════════════════════════════════════════════
    # PLANNING COMPONENTS - Used in plan_steps_task
    # ═══════════════════════════════════════════════════════════════════════════

    EXPLICIT_ELEMENTS_ONLY_RULES = """
--- CRITICAL: ONLY EXPLICIT ELEMENTS ---
⚠️ **MOST IMPORTANT RULE**: ONLY create steps for elements and actions EXPLICITLY mentioned in the user's query.

❌ DO NOT ADD:
- Popup dismissal steps (login popups, cookie consent, promotional popups)
- Cookie consent handling
- Newsletter dismissals
- Chat widget closures
- Any "smart" anticipatory steps
- Common website pattern handling

✅ ONLY ADD:
- Steps for elements the user explicitly mentions
- Actions the user explicitly requests
- Nothing else

**WHY**: The browser automation (BrowserUse Agent) handles popups contextually and intelligently. 
Adding popup handling steps wastes time and confuses the workflow.

**EXAMPLE**:
User query: "search for shoes on Flipkart and get first product name and price"

✅ CORRECT steps (with specific, spatially-aware element descriptions):
1. Open Browser → Flipkart
2. Input Text → search input field in the top header → "shoes"
3. Press Keys → Enter
4. Get Text → first item title in the main results list (center content area)
5. Get Text → price text below the title in the first result item

❌ WRONG (DO NOT DO THIS):
1. Open Browser → Flipkart
2. Click Element → login popup close button  ← USER NEVER MENTIONED THIS!
3. Click Element → cookie consent accept  ← USER NEVER MENTIONED THIS!
4. Input Text → search box → "shoes"
5. Get Text → product name ← TOO GENERIC! Need spatial context
6. ...
"""

    SEARCH_OPTIMIZATION_RULES = """
--- SEARCH OPTIMIZATION RULES ---
*   For search operations: After `Input Text` into search box, use `Press Keys` with `Enter` (Enter key) instead of finding/clicking a search button.
*   Modern websites (Flipkart, Amazon, Google, etc.) trigger search on Enter press.
*   This is faster, more reliable, and reduces element identification overhead.
"""

    PLANNING_CONDITIONAL_LOGIC = """
--- HANDLING CONDITIONAL LOGIC ---
For validation steps that require comparison (like price checks), structure the step as:
*   Use `Get Text` to retrieve the value
*   Use a separate validation step with `Should Be True` keyword
*   Include the `condition_expression` key with the actual comparison logic

Example for price validation:
1. Get Text from price element -> store in variable
2. Validate with Should Be True and condition_expression like "${float(product_price.replace('₹', '').replace(',', '')) < 9999}"
"""

    PLANNING_LOOP_HANDLING = """
--- HANDLING LOOPS ---
If the user's query implies a loop (e.g., "for every link", "for each item"), you must structure the output JSON for that step with two additional keys: `loop_type` and `loop_source`.
*   `loop_type`: Should be "FOR".
*   `loop_source`: Should be the element that contains the items to loop over (e.g., "the main menu").
"""

    PLANNING_OUTPUT_RULES = """
--- FINAL OUTPUT RULES ---
1.  You MUST respond with ONLY valid JSON in this exact format: {"steps": [...]}
2.  The "steps" array contains objects, each representing a single test step with keys: "step_description", "element_description", "value", and "keyword".
3.  For validation steps, use "Should Be True" keyword with a "condition_expression" key.
4.  The keys `condition_type`, `condition_value`, `loop_type`, and `loop_source` are OPTIONAL and should only be included for steps with conditional logic or loops.
5.  If the query involves a web search (e.g., "search for X") but does not specify a URL, you MUST generate a first step to open a search engine. Use 'https://www.google.com' as the value for the URL.
6.  When generating a browser initialization step, you MUST include library-specific parameters:
{browser_init_placeholder}
7.  **CRITICAL**: For ANY search operation (Google, Flipkart, Amazon, etc.), after "Input Text" step, use "Press Keys" with value "Enter" instead of generating a separate "Click Element" step for search button. This applies to ALL websites.
8.  **MOST CRITICAL**: DO NOT add popup dismissal, cookie consent, or any steps not explicitly mentioned in user query. The browser automation handles these automatically.
"""

    # ═══════════════════════════════════════════════════════════════════════════
    # IDENTIFICATION COMPONENTS - Used in identify_elements_task
    # ═══════════════════════════════════════════════════════════════════════════

    FORM_ELEMENT_HANDLING = """
⚠️ **CRITICAL FORM ELEMENT HANDLING** ⚠️
When the description mentions checkboxes, radio buttons, or toggle switches:
- ALWAYS request the actual INPUT element, NOT the label text!
- Modify description to explicitly target the input control:
  * 'checkbox 1' → 'the checkbox INPUT element next to text "checkbox 1"'
  * 'remember me checkbox' → 'the checkbox INPUT element for "remember me"'
  * 'male radio button' → 'the radio button INPUT element for "male"'
  * 'agree to terms' → 'the checkbox INPUT element for "agree to terms"'
- This ensures BrowserUse finds the clickable <input> element, not just the text label
- Text labels alone cannot be checked/unchecked - only input elements can!
"""

    SPATIAL_CONTEXT_PRESERVATION = """
⚠️ CRITICAL: Preserve the FULL element description from the plan, including ALL spatial hints like:
- Location context: 'in header', 'in main content', 'in sidebar', 'in footer'
- Relative position: 'below the image', 'next to the button', 'above the form'
- Exclusions: 'not in sidebar', 'not in filters', 'not in navigation'
- Container: 'in the results list', 'in the form', 'in the dialog'
These spatial clues help vision AI accurately locate the correct element!
"""

    BATCH_TOOL_FORMAT = """
--- CRITICAL OUTPUT RULE ---

⚠️ MOST IMPORTANT: You MUST output the tool call in EXACTLY this format:

Action: batch_browser_automation
Action Input: {"elements": [...], "url": "...", "user_query": "..."}

CRITICAL FORMATTING RULES:
1. The line 'Action: batch_browser_automation' must have NOTHING else on it
2. Do NOT add any text before, after, or on the same line as 'Action:'
3. Do NOT add backticks, quotes, or any other characters after 'batch_browser_automation'
4. The next line must be 'Action Input:' followed by a JSON dictionary
5. Action Input must be a DICTIONARY { } NOT an array [ ]

✅ CORRECT FORMAT:
Action: batch_browser_automation
Action Input: {"elements": [{"id": "elem_1", "description": "search box", "action": "input"}], "url": "https://example.com", "user_query": "search for items"}

❌ WRONG FORMATS (DO NOT DO THIS):
Action: batch_browser_automation` and `Action Input` using...  ← WRONG! Extra text on Action line
Action: batch_browser_automation`  ← WRONG! Backtick at end
First I need to... Action: batch_browser_automation  ← WRONG! Text before Action
Action Input: [{"elements": [...]}]  ← WRONG! Array instead of dictionary

REMEMBER:
- Action line = ONLY 'Action: batch_browser_automation'
- Action Input = ONE dictionary starting with { and ending with }
- The 'elements' key INSIDE the dictionary contains the array
- NO explanations, NO thinking, NO extra text

Structure of Action Input:
{
  "elements": [array of elements],  ← Array is INSIDE the dictionary
  "url": "...",
  "user_query": "..."
}
"""

    # ═══════════════════════════════════════════════════════════════════════════
    # ASSEMBLY OUTPUT RULES
    # ═══════════════════════════════════════════════════════════════════════════

    ASSEMBLY_OUTPUT_RULES = """
🚨 **CODE GENERATOR - OUTPUT JSON FORMAT** 🚨

Your task: Generate Robot Framework code and return as JSON.

**OUTPUT FORMAT:**
{"code": "*** Settings ***\\nLibrary    Browser\\n..."}

**RULES:**
1. Final Answer must be a JSON object with "code" key
2. "code" value: Complete Robot Framework code with \\n for newlines
3. No markdown, no explanatory text - just the JSON

--- KEYWORD SYNTAX LOOKUP (CRITICAL) ---
⚠️ Use 'keyword_search' tool for unfamiliar keywords.

**BEFORE generating code for unfamiliar keywords:**
1. Call keyword_search with the EXACT keyword name
2. Check returned syntax: argument count and order
3. If tool shows <arg1> <arg2> <arg3>, use SEPARATE arguments (4 spaces between)
4. If step value has 'x=y' format, check if tool expects 2 separate args

**Pattern Recognition:**
- 'attr=value' → likely: Keyword    ${loc}    attr    value (3 args)
- 'just_text' → likely: Keyword    ${loc}    just_text (2 args)
- When unsure → ALWAYS search first

**FORBIDDEN in Final Answer:**
- Markdown code blocks (```json, ```)
- Thinking text ('Thought:', 'I will', etc.)
- Text before/after the JSON

**CORRECT Example:**
{"code": "*** Settings ***\\nLibrary    Browser\\n\\n*** Variables ***\\n${browser}    chromium\\n\\n*** Test Cases ***\\nGenerated Test\\n    New Browser    ${browser}    headless=True\\n    Close Browser"}
"""

    ASSEMBLY_FORMAT_RULES = """
--- OUTPUT FORMAT ---
Final Answer must be: {"code": "<robot_code>"}

1. Code must start with *** Settings ***
2. Use \\n for newlines
3. End with last test keyword (e.g., Close Browser)
4. No explanatory text in code value
5. For price/numeric validations: use Evaluate to convert strings to numbers
"""
