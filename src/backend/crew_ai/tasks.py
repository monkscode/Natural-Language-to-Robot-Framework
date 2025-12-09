from crewai import Task
import os
import json
import logging
from pydantic import BaseModel, Field
from typing import List, Optional

logger = logging.getLogger(__name__)


class ValidationOutput(BaseModel):
    """Pydantic model for validation task output."""
    valid: bool = Field(description="Whether the code is valid or not")
    reason: str = Field(description="Explanation of validation result")
    errors: Optional[List[str]] = Field(
        default=None, description="List of error messages if code is invalid")


class RobotTasks:
    def __init__(self, library_context=None, workflow_id: str = ""):
        """
        Initialize Robot Framework tasks.

        Args:
            library_context: LibraryContext instance (optional, for dynamic library knowledge)
            workflow_id: Unique workflow identifier for metrics tracking
        """
        self.library_context = library_context
        self.workflow_id = workflow_id

    def _get_keyword_guidelines(self) -> str:
        """Get MINIMAL keyword guidelines for planning phase."""
        if self.library_context:
            # Use minimal planning context instead of detailed keywords
            return self.library_context.planning_context
        else:
            # Fallback to minimal guidance
            return """
            Available action types:
            • Browser Management: Opening/closing browsers
            • Element Interaction: Clicking, inputting text
            • Data Extraction: Getting text from elements
            • Keyboard Actions: Pressing keys
            
            Focus on HIGH-LEVEL steps. Code Assembler handles details.
            """

    def _get_code_structure_template(self) -> str:
        """Get code structure template from library context or use defaults."""
        if self.library_context:
            # Use dynamic code structure from library context
            return self.library_context.code_assembly_context
        else:
            # Fallback to basic SeleniumLibrary structure (backward compatibility)
            return """
            --- MANDATORY STRUCTURE ---
            ```robot
            *** Settings ***
            Library    SeleniumLibrary
            Library    BuiltIn

            *** Variables ***
            ${browser}    chrome
            ${options}    add_argument("--headless")

            *** Test Cases ***
            Generated Test
                Open Browser    <url>    ${browser}    options=${options}
                # Test steps here
                Close Browser
            ```
            """

    def _get_browser_init_instructions(self) -> str:
        """Get library-specific browser initialization instructions."""
        if self.library_context:
            params = self.library_context.browser_init_params
            library_name = self.library_context.library_name

            if library_name == "Browser":
                # Browser Library uses New Browser
                param_list = ', '.join([f'{k}={v}' for k, v in params.items()])
                return f"""
    - For Browser Library: Use "New Browser" keyword
    - Include these parameters: {param_list}
    - Example: {{"keyword": "New Browser", "browser": "{params.get('browser', 'chromium')}", "headless": "{params.get('headless', 'True')}"}}
    - DO NOT include 'options' parameter for Browser Library
                """
            else:
                # SeleniumLibrary uses Open Browser
                param_list = ', '.join([f'{k}={v}' for k, v in params.items()])
                return f"""
    - For SeleniumLibrary: Use "Open Browser" keyword
    - Include these parameters: {param_list}
    - Example: {{"keyword": "Open Browser", "value": "<url>", "browser": "{params.get('browser', 'chrome')}", "options": "{params.get('options', '')}"}}
                """
        else:
            # Fallback for backward compatibility
            logger.warning(
                "No library context available, using SeleniumLibrary defaults")
            return """
    - Use "Open Browser" keyword with browser=chrome and options parameters
    - Example: {"keyword": "Open Browser", "value": "<url>", "browser": "chrome", "options": "add_argument(\\"--headless\\")"}
            """

    def _get_viewport_instructions(self) -> str:
        """Get viewport configuration instructions if needed."""
        if self.library_context and self.library_context.requires_viewport_config:
            return f"""
--- VIEWPORT CONFIGURATION (CRITICAL FOR {self.library_context.library_name.upper()}) ---

**MANDATORY**: After "New Browser" and before "New Page", you MUST add:
{self.library_context.get_viewport_config_code()}

**Why**: Browser Library uses a small default viewport (800x600) which causes:
- Elements outside viewport are not detected
- Locators fail to find elements
- Tests fail with "element not found" errors

**Correct Order**:
1. New Browser    ${{browser}}    headless=${{headless}}
2. New Context    viewport=None    ← REQUIRED
3. New Page    ${{url}}

**Example**:
```robot
*** Test Cases ***
Generated Test
    New Browser    chromium    headless=True
    New Context    viewport=None
    New Page    https://example.com
    # Test steps here
```

**CRITICAL**: viewport=None uses full browser window size, ensuring all elements are visible.
            """
        return ""

    def plan_steps_task(self, agent, query) -> Task:
        return Task(
            description=f"""
            Your mission is to act as an expert Test Automation Planner. You must analyze a user's natural language query and decompose it into a comprehensive, step-by-step test plan that a junior test engineer could follow.

            The user query is: "{query}"

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
            3. Press Keys → RETURN
            4. Get Text → first item title in the main results list (center content area)
            5. Get Text → price text below the title in the first result item
            
            ❌ WRONG (DO NOT DO THIS):
            1. Open Browser → Flipkart
            2. Click Element → login popup close button  ← USER NEVER MENTIONED THIS!
            3. Click Element → cookie consent accept  ← USER NEVER MENTIONED THIS!
            4. Input Text → search box → "shoes"
            5. Get Text → product name ← TOO GENERIC! Need spatial context
            6. ...
            
            --- CORE PRINCIPLES ---
            1.  **Explicitness:** Your plan must be explicit. Do not assume any prior context. If a user says "log in", you must include steps for navigating to the login page, entering the username, entering the password, and clicking the submit button.
            2.  **Decomposition:** Break down complex actions into smaller, single-action steps. For example, "search for a product and add it to the cart" should be multiple steps: "Input text into search bar", "Click search button", "Click product link", "Click add to cart button".
            3.  **Keyword Precision:** Use the most appropriate Robot Framework keyword for each action.
            4.  **User Intent Only:** ONLY create steps for what the user explicitly asked for.

            --- KEYWORD GUIDELINES ---
            {self._get_keyword_guidelines()}
            
            --- SEARCH OPTIMIZATION RULES ---
            *   For search operations: After `Input Text` into search box, use `Press Keys` with `RETURN` (Enter key) instead of finding/clicking a search button.
            *   Modern websites (Flipkart, Amazon, Google, etc.) trigger search on Enter press.
            *   This is faster, more reliable, and reduces element identification overhead.

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

            --- HANDLING CONDITIONAL LOGIC ---
            For validation steps that require comparison (like price checks), structure the step as:
            *   Use `Get Text` to retrieve the value
            *   Use a separate validation step with `Should Be True` keyword
            *   Include the `condition_expression` key with the actual comparison logic
            
            Example for price validation:
            1. Get Text from price element -> store in variable
            2. Validate with Should Be True and condition_expression like "${{float(product_price.replace('₹', '').replace(',', '')) < 9999}}"

            --- HANDLING LOOPS ---
            If the user's query implies a loop (e.g., "for every link", "for each item"), you must structure the output JSON for that step with two additional keys: `loop_type` and `loop_source`.
            *   `loop_type`: Should be "FOR".
            *   `loop_source`: Should be the element that contains the items to loop over (e.g., "the main menu").

            --- FINAL OUTPUT RULES ---
            1.  You MUST respond with ONLY a valid JSON array of objects.
            2.  Each object in the array represents a single test step and MUST have the following keys: "step_description", "element_description", "value", and "keyword".
            3.  For validation steps, use "Should Be True" keyword with a "condition_expression" key.
            4.  The keys `condition_type`, `condition_value`, `loop_type`, and `loop_source` are OPTIONAL and should only be included for steps with conditional logic or loops.
            5.  If the query involves a web search (e.g., "search for X") but does not specify a URL, you MUST generate a first step to open a search engine. Use 'https://www.google.com' as the value for the URL.
            6.  When generating a browser initialization step, you MUST include library-specific parameters:
            {self._get_browser_init_instructions()}
            7.  **CRITICAL**: For ANY search operation (Google, Flipkart, Amazon, etc.), after "Input Text" step, use "Press Keys" with value "RETURN" instead of generating a separate "Click Element" step for search button. This applies to ALL websites.
            8.  **MOST CRITICAL**: DO NOT add popup dismissal, cookie consent, or any steps not explicitly mentioned in user query. The browser automation handles these automatically.

            """,
            expected_output="A JSON array of objects, where each object represents a single test step with the keys: 'step_description', 'element_description', 'value', and 'keyword', and optional keys for conditions and loops. ONLY steps for elements explicitly mentioned in user query.",
            agent=agent,
        )

    def identify_elements_task(self, agent) -> Task:
        workflow_id_instruction = f"\n**WORKFLOW ID**: {self.workflow_id}\n⚠️ CRITICAL: You MUST include 'workflow_id': '{self.workflow_id}' in the Action Input dictionary for metrics tracking.\n\n" if self.workflow_id else ""
        
        return Task(
            description=(
                "⚠️ **BATCH LOCATOR IDENTIFICATION WORKFLOW**\n\n"
                "Your mission: Find locators for ALL elements in ONE batch operation.\n"
                "The context will be the output of the 'plan_steps_task' (array of test steps).\n\n"
                f"{workflow_id_instruction}"
                "ℹ️ All elements will be found using batch_browser_automation.\n\n"
                "--- MANDATORY BATCH WORKFLOW ---\n"
                "\n"
                "**STEP 1: ANALYZE THE PLAN**\n"
                "- Read ALL test steps from context\n"
                "- Identify which steps need element locators\n"
                "- Note: 'Open Browser', 'Close Browser', 'Should Be True' steps DON'T need locators\n"
                "- Note: 'Input Text', 'Click Element', 'Get Text', 'Select From List' steps NEED locators\n"
                "\n"
                "**STEP 2: EXTRACT URL**\n"
                "- Find the 'Open Browser' step in the plan\n"
                "- Extract the URL from its 'value' field\n"
                "- Example: If step says {\"keyword\": \"Open Browser\", \"value\": \"https://www.flipkart.com\"}\n"
                "  → URL is \"https://www.flipkart.com\"\n"
                "\n"
                "**STEP 3: COLLECT ELEMENTS**\n"
                "- For each step that needs a locator, extract:\n"
                "  * Unique ID (e.g., \"elem_1\", \"elem_2\")\n"
                "  * Element description (from 'element_description' field) - USE EXACT DESCRIPTION with all spatial context\n"
                "  * Action keyword (from 'keyword' field: input, click, get_text, etc.)\n"
                "\n"
                "⚠️ **CRITICAL FORM ELEMENT HANDLING** ⚠️\n"
                "When the description mentions checkboxes, radio buttons, or toggle switches:\n"
                "- ALWAYS request the actual INPUT element, NOT the label text!\n"
                "- Modify description to explicitly target the input control:\n"
                "  * 'checkbox 1' → 'the checkbox INPUT element next to text \"checkbox 1\"'\n"
                "  * 'remember me checkbox' → 'the checkbox INPUT element for \"remember me\"'\n"
                "  * 'male radio button' → 'the radio button INPUT element for \"male\"'\n"
                "  * 'agree to terms' → 'the checkbox INPUT element for \"agree to terms\"'\n"
                "- This ensures BrowserUse finds the clickable <input> element, not just the text label\n"
                "- Text labels alone cannot be checked/unchecked - only input elements can!\n"
                "\n"
                "⚠️ CRITICAL: Preserve the FULL element description from the plan, including ALL spatial hints like:\n"
                "- Location context: 'in header', 'in main content', 'in sidebar', 'in footer'\n"
                "- Relative position: 'below the image', 'next to the button', 'above the form'\n"
                "- Exclusions: 'not in sidebar', 'not in filters', 'not in navigation'\n"
                "- Container: 'in the results list', 'in the form', 'in the dialog'\n"
                "These spatial clues help vision AI accurately locate the correct element!\n"
                "\n"
                "Example elements list:\n"
                "```json\n"
                "[\n"
                "    {\"id\": \"elem_1\", \"description\": \"search input field in the top header\", \"action\": \"input\"},\n"
                "    {\"id\": \"elem_2\", \"description\": \"the checkbox INPUT element next to text 'checkbox 1'\", \"action\": \"click\"},\n"
                "    {\"id\": \"elem_3\", \"description\": \"the radio button INPUT element for 'male'\", \"action\": \"click\"},\n"
                "    {\"id\": \"elem_4\", \"description\": \"first item title in the main results list\", \"action\": \"get_text\"}\n"
                "]\n"
                "```\n"
                "\n"
                "**STEP 4: BUILD USER QUERY CONTEXT**\n"
                "- Summarize what the test is trying to accomplish\n"
                "- Example: \"Search for shoes on Flipkart and extract first product name and price\"\n"
                "- This helps BrowserUse understand the workflow and handle popups intelligently\n"
                "\n"
                "**STEP 5: CALL BATCH TOOL (ONCE!)**\n"
                "\n"
                "```\n"
                "Action: batch_browser_automation\n"
                f"Action Input: {{\"elements\": [{{\"id\": \"elem_1\", \"description\": \"search box in header\", \"action\": \"input\"}}, {{\"id\": \"elem_2\", \"description\": \"first product name in search results\", \"action\": \"get_text\"}}, {{\"id\": \"elem_3\", \"description\": \"first product price in search results\", \"action\": \"get_text\"}}], \"url\": \"https://www.flipkart.com\", \"user_query\": \"Search for shoes and get first product name and price\", \"workflow_id\": \"{self.workflow_id}\"}}\n"
                "```\n"
                "\n"
                "**STEP 6: RECEIVE BATCH RESPONSE**\n"
                "\n"
                "The tool will return:\n"
                "```json\n"
                "{\n"
                "    \"success\": true,\n"
                "    \"locator_mapping\": {\n"
                "        \"elem_1\": {\n"
                "            \"best_locator\": \"name=q\",\n"
                "            \"found\": true,\n"
                "            \"element_info\": {\"tagName\": \"input\", \"id\": \"search\", ...},\n"
                "            \"all_locators\": [...]\n"
                "        },\n"
                "        \"elem_2\": {\n"
                "            \"best_locator\": \"id=react-select-4-input\",\n"
                "            \"found\": true,\n"
                "            \"element_info\": {\"tagName\": \"input\", \"id\": \"react-select-4-input\", ...},\n"
                "            \"all_locators\": [...]\n"
                "        }\n"
                "    },\n"
                "    \"summary\": {\"total_elements\": 2, \"successful\": 2, \"failed\": 0}\n"
                "}\n"
                "```\n"
                "\n"
                "⚠️ **IMPORTANT**: The 'element_info.tagName' tells you the HTML element type:\n"
                "- 'select' = native HTML select dropdown\n"
                "- 'input', 'div' = custom dropdown (React-Select, Material-UI, etc.)\n"
                "You MUST include this as 'element_type' when mapping to steps!\n"
                "\n"
                "**STEP 7: MAP LOCATORS TO STEPS**\n"
                "\n"
                "⚠️ **CRITICAL LOCATOR USAGE RULE** ⚠️\n"
                "When mapping locators to steps:\n"
                "1. Use ONLY the 'best_locator' value from locator_mapping\n"
                "2. DO NOT analyze or select from 'all_locators' array\n"
                "3. DO NOT override with your own preference\n"
                "4. DO NOT second-guess the locator selection\n"
                "5. The 'best_locator' has already been:\n"
                "   - AI-detected with vision on actual page\n"
                "   - Validated with Playwright (unique & working)\n"
                "   - Scored by quality (ID=100, text=65, XPath=18)\n"
                "   - Re-ranked to select optimal option\n"
                "6. Even if you see a 'better' locator in all_locators, IGNORE IT\n"
                "7. Your ONLY job is to copy best_locator values to steps\n\n"
                "Process:\n"
                "- Go through each test step again\n"
                "- If step needed a locator (e.g., elem_1, elem_2, elem_3):\n"
                "  * Add 'locator' key to that step's JSON\n"
                "  * Use the 'best_locator' value EXACTLY from locator_mapping\n"
                "  * DO NOT modify, analyze, or substitute the locator\n"
                "  * ALSO add 'element_type' from element_info.tagName (e.g., 'input', 'select', 'div')\n"
                "- If step didn't need a locator (Open Browser, Close Browser):\n"
                "  * Leave it as-is (no locator key needed)\n"
                "\n"
                "Example output:\n"
                "```json\n"
                "[\n"
                "    {\"keyword\": \"Open Browser\", \"value\": \"https://www.flipkart.com\"},\n"
                "    {\"keyword\": \"Input Text\", \"element_description\": \"search box\", \"value\": \"shoes\", \"locator\": \"name=q\", \"element_type\": \"input\"},\n"
                "    {\"keyword\": \"Press Keys\", \"element_description\": \"search box\", \"value\": \"RETURN\", \"locator\": \"name=q\", \"element_type\": \"input\"},\n"
                "    {\"keyword\": \"Get Text\", \"element_description\": \"first product name\", \"locator\": \"xpath=...\", \"element_type\": \"span\"},\n"
                "    {\"keyword\": \"Select Options By\", \"element_description\": \"dropdown\", \"locator\": \"id=react-select-4-input\", \"element_type\": \"input\", \"value\": \"label    Volvo\"}\n"
                "]\n"
                "```\n"
                "\n"
                "--- COMPLETE EXAMPLE ---\n"
                "\n"
                "**Input Context (from plan_steps_task):**\n"
                "```json\n"
                "[\n"
                "    {\"step_description\": \"Open browser to Flipkart\", \"keyword\": \"Open Browser\", \"value\": \"https://www.flipkart.com\"},\n"
                "    {\"step_description\": \"Input shoes in search\", \"keyword\": \"Input Text\", \"element_description\": \"search box\", \"value\": \"shoes\"},\n"
                "    {\"step_description\": \"Press Enter\", \"keyword\": \"Press Keys\", \"element_description\": \"search box\", \"value\": \"RETURN\"},\n"
                "    {\"step_description\": \"Get first product name\", \"keyword\": \"Get Text\", \"element_description\": \"first product name\"},\n"
                "    {\"step_description\": \"Get first product price\", \"keyword\": \"Get Text\", \"element_description\": \"first product price\"}\n"
                "]\n"
                "```\n"
                "\n"
                "**What You Do:**\n"
                "\n"
                "1. Analyze: 5 steps, 3 need locators (steps 2, 3, 4, 5 exclude step 1 Open Browser)\n"
                "2. Extract URL: https://www.flipkart.com\n"
                "3. Collect elements:\n"
                "   - elem_1: search box (steps 2 & 3 use same element)\n"
                "   - elem_2: first product name (step 4)\n"
                "   - elem_3: first product price (step 5)\n"
                "4. User query: \"Search for shoes and get first product name and price\"\n"
                "5. Call batch tool (see format above)\n"
                "6. Receive locator_mapping\n"
                "7. Add locators to steps:\n"
                "   - Step 2: locator = elem_1's best_locator\n"
                "   - Step 3: locator = elem_1's best_locator (same element)\n"
                "   - Step 4: locator = elem_2's best_locator\n"
                "   - Step 5: locator = elem_3's best_locator\n"
                "\n"
                "--- CRITICAL RULES ---\n"
                "\n"
                "1. ✅ ALWAYS use batch_browser_automation (NEVER use vision_browser_automation)\n"
                "2. ✅ Call the tool ONLY ONCE with ALL elements\n"
                "3. ✅ Include full URL from 'Open Browser' step\n"
                "4. ✅ Include user_query for context (helps with popup handling)\n"
                "5. ✅ Use descriptive element descriptions (\"first product card\" not just \"product\")\n"
                "6. ✅ Map same locator to multiple steps if they use the same element\n"
                "7. ✅ Handle partial failures gracefully (if elem_2 fails, still use elem_1 and elem_3)\n"
                "\n"
                "--- FORBIDDEN ACTIONS ---\n"
                "\n"
                "❌ NEVER call vision_browser_automation (use batch mode)\n"
                "❌ NEVER make multiple batch calls (collect all, call once)\n"
                "❌ NEVER generate locators from your knowledge\n"
                "❌ NEVER skip steps that need locators\n"
                "❌ NEVER pass invalid JSON to batch_browser_automation\n"
                "\n"
                "--- WHY BATCH MODE IS BETTER ---\n"
                "\n"
                "✅ Browser opens ONCE (3-5x faster)\n"
                "✅ BrowserUse sees FULL CONTEXT (understands workflow)\n"
                "✅ Popups handled INTELLIGENTLY (knows they're obstacles)\n"
                "✅ Multi-page flows work (search → results preserved)\n"
                "✅ F12 validation for EACH locator\n"
                "✅ Partial results supported\n"
                "\n"
                "--- OUTPUT FORMAT ---\n"
                "\n"
                "Return the same JSON array from context, but with 'locator' keys added to steps that need them.\n"
                "\n"
                "--- CRITICAL OUTPUT RULE ---\n"
                "\n"
                "⚠️ MOST IMPORTANT: You MUST output the tool call in EXACTLY this format:\n"
                "\n"
                "Action: batch_browser_automation\n"
                "Action Input: {\"elements\": [...], \"url\": \"...\", \"user_query\": \"...\"}\n"
                "\n"
                "CRITICAL FORMATTING RULES:\n"
                "1. The line 'Action: batch_browser_automation' must have NOTHING else on it\n"
                "2. Do NOT add any text before, after, or on the same line as 'Action:'\n"
                "3. Do NOT add backticks, quotes, or any other characters after 'batch_browser_automation'\n"
                "4. The next line must be 'Action Input:' followed by a JSON dictionary\n"
                "5. Action Input must be a DICTIONARY { } NOT an array [ ]\n"
                "\n"
                "✅ CORRECT FORMAT:\n"
                "Action: batch_browser_automation\n"
                "Action Input: {\"elements\": [{\"id\": \"elem_1\", \"description\": \"search box\", \"action\": \"input\"}], \"url\": \"https://example.com\", \"user_query\": \"search for items\"}\n"
                "\n"
                "❌ WRONG FORMATS (DO NOT DO THIS):\n"
                "Action: batch_browser_automation` and `Action Input` using...  ← WRONG! Extra text on Action line\n"
                "Action: batch_browser_automation`  ← WRONG! Backtick at end\n"
                "First I need to... Action: batch_browser_automation  ← WRONG! Text before Action\n"
                "Action Input: [{\"elements\": [...]}]  ← WRONG! Array instead of dictionary\n"
                "\n"
                "REMEMBER:\n"
                "- Action line = ONLY 'Action: batch_browser_automation'\n"
                "- Action Input = ONE dictionary starting with { and ending with }\n"
                "- The 'elements' key INSIDE the dictionary contains the array\n"
                "- NO explanations, NO thinking, NO extra text\n"
                "\n"
                "Structure of Action Input:\n"
                "{\n"
                "  \"elements\": [array of elements],  ← Array is INSIDE the dictionary\n"
                "  \"url\": \"...\",\n"
                "  \"user_query\": \"...\"\n"
                "}\n"
            ),
            expected_output="A JSON array of objects, where each object represents a single test step with the added 'locator' key obtained from the batch_browser_automation tool.",
            agent=agent,
        )

    def assemble_code_task(self, agent) -> Task:

        return Task(
            description=(
                "🚨 **YOU ARE A CODE PRINTER - OUTPUT ONLY CODE** 🚨\n\n"

                "Your ONLY task: Generate raw Robot Framework code from the provided steps.\n\n"

                "--- KEYWORD SYNTAX LOOKUP (CRITICAL) ---\n"
                "⚠️ You have access to 'keyword_search' tool. USE IT when:\n"
                "- You encounter ANY keyword you're not 100% certain about\n"
                "- The step value contains '=' pattern (e.g., 'attr=value') - may need splitting\n"
                "- You need to verify argument count, order, or syntax\n"
                "- The keyword is NOT in the common list (New Browser, Click, Fill Text, Get Text)\n\n"
                "**BEFORE generating code for unfamiliar keywords:**\n"
                "1. Call keyword_search with the EXACT keyword name from the step\n"
                "2. Review the returned syntax: check argument count and whether they're separate\n"
                "3. If tool shows args like <arg1> <arg2> <arg3>, use SEPARATE arguments (4 spaces between)\n"
                "4. If step value has 'x=y' format, check if tool expects 2 args: <x> and <y> separately\n\n"
                "**Pattern Recognition:**\n"
                "- Step value 'attr=value' → likely needs: Keyword    ${loc}    attr    value (3 args)\n"
                "- Step value 'just_text' → likely needs: Keyword    ${loc}    just_text (2 args)\n"
                "- When unsure → ALWAYS search first, then follow the tool's argument structure\n\n"

                "⛔ **ABSOLUTELY FORBIDDEN** ⛔\n"
                "DO NOT include:\n"
                "- Thinking process ('Thought:', 'I will', 'Let me', 'First', 'Now', 'I need')\n"
                "- Explanations ('From the first step:', 'Also add', 'Variables:', 'Test Case Steps:')\n"
                "- Markdown ('**Variables:**', '```robot', '```', '**Test Cases:**')\n"
                "- Numbered lists ('1. New Browser', '2. New Context')\n"
                "- ANY text before *** Settings ***\n"
                "- ANY text after the last keyword\n\n"

                "✅ **YOUR OUTPUT MUST BE** ✅\n"
                "Raw Robot Framework code that:\n"
                "1. Starts with *** Settings *** (first line, first character)\n"
                "2. Contains ONLY valid Robot Framework syntax\n"
                "3. Can be saved directly as a .robot file\n\n"

                "Example of CORRECT output:\n"
                "*** Settings ***\n"
                "Library    Browser\n"
                "...\n\n"

                "Example of WRONG output:\n"
                "Now, I will assemble...*** Settings ***  ← WRONG!\n"
                "**Variables:**  ← WRONG!\n\n"

                "The context will be the output of the 'identify_elements_task'.\n\n"

                f"{self._get_code_structure_template()}\n\n"

                f"{self._get_viewport_instructions()}\n\n"

                "--- CRITICAL: VARIABLE DECLARATION RULES ---\n"
                "1. **ALWAYS include *** Variables *** section** (even if empty)\n"
                "2. **Declare ALL variables before use:**\n"
                "   - If Open Browser step has 'browser' key → ${browser}    <value from step>\n"
                "   - If Open Browser step has 'options' key → ${options}    <value from step>\n"
                "   - For each element with locator → ${elem_X_locator}    <locator value>\n"
                "   - For Get Text results → ${variable_name}    (no initial value needed)\n\n"

                "3. **Variable Naming Convention:**\n"
                "   - Browser config: ${browser}, ${options}\n"
                "   - Element locators: ${search_box_locator}, ${product_name_locator}\n"
                "   - Retrieved values: ${product_name}, ${product_price}, ${result}\n\n"

                "4. **Extracting Values from Steps:**\n"
                "   - Look for 'browser' key in Open Browser step → use as ${browser} value\n"
                "   - Look for 'options' key in Open Browser step → use as ${options} value\n"
                "   - Look for 'locator' key in each step → declare as ${elem_X_locator}\n\n"

                "**Example Variable Extraction:**\n"
                "If you receive:\n"
                "```json\n"
                "{\n"
                "  \"keyword\": \"Open Browser\",\n"
                "  \"value\": \"https://www.flipkart.com\",\n"
                "  \"browser\": \"chrome\",\n"
                "  \"options\": \"add_argument('--headless')\"\n"
                "}\n"
                "```\n"
                "You MUST declare:\n"
                "```robot\n"
                "*** Variables ***\n"
                "${browser}    chrome\n"
                "${options}    add_argument('--headless')\n"
                "```\n\n"

                "--- CRITICAL: USE PROVIDED LOCATORS EXACTLY (NO EXCEPTIONS) ---\n"
                "⚠️ **MOST IMPORTANT RULE FOR LOCATORS** ⚠️\n\n"

                "The locators provided have been:\n"
                "- Found by AI vision on the actual webpage\n"
                "- Validated to work correctly\n"
                "- Scored and prioritized (ID > data-testid > name > aria-label > text > XPath)\n"
                "- Selected as the BEST option for stability\n\n"

                "**YOU MUST:**\n"
                "1. Copy the EXACT locator value from the 'locator' field\n"
                "2. DO NOT modify, improve, or convert the locator\n"
                "3. DO NOT change id=X to xpath=//*[@id='X']\n"
                "4. DO NOT change any locator format\n"
                "5. If you think a locator is wrong, USE IT ANYWAY and add a comment\n\n"

                "**WHY THIS IS CRITICAL:**\n"
                "- The locator was validated on the actual page\n"
                "- Changing it will break the test\n"
                "- The scoring system already selected the best option\n"
                "- Your job is code assembly, not locator optimization\n\n"

                "**EXAMPLES:**\n"
                "✅ CORRECT:\n"
                "Input: {\"locator\": \"id=submit-btn\"}\n"
                "Output: ${submit_locator}    id=submit-btn\n\n"

                "❌ WRONG (DO NOT DO THIS):\n"
                "Input: {\"locator\": \"id=submit-btn\"}\n"
                "Output: ${submit_locator}    xpath=//*[@id='submit-btn']  ← WRONG!\n\n"

                "❌ WRONG (DO NOT DO THIS):\n"
                "Input: {\"locator\": \"xpath=//button[1]\"}\n"
                "Output: ${submit_locator}    id=submit-btn  ← WRONG! Use provided XPath!\n\n"

                "❌ WRONG (DO NOT DO THIS):\n"
                "Input: {\"locator\": \"name=q\"}\n"
                "Output: ${search_locator}    id=search-box  ← WRONG! Use provided name locator!\n\n"

                "⚠️ REMEMBER: Locators are pre-validated and pre-scored. DO NOT modify them! ⚠️\n\n"

                "--- LOCATOR MAPPING RULES ---\n"
                "For each step that needs a locator:\n"
                "1. Check if 'locator' key exists and 'found' is true\n"
                "2. If found: Declare locator as variable and use it EXACTLY as provided\n"
                "3. If NOT found (found=false or error present):\n"
                "   a. Add comment: # WARNING: Locator not found for <element_description>\n"
                "   b. Use placeholder: xpath=//PLACEHOLDER_FOR_<element_id>\n"
                "   c. Still generate syntactically valid code\n\n"

                "**Example for found locator:**\n"
                "```robot\n"
                "*** Variables ***\n"
                "${search_box_locator}    id=search-input  # ← Use EXACT value from 'locator' field\n\n"
                "*** Test Cases ***\n"
                "Test\n"
                "    Input Text    ${search_box_locator}    shoes\n"
                "```\n\n"

                "**Example for missing locator:**\n"
                "```robot\n"
                "*** Variables ***\n"
                "${product_locator}    xpath=//PLACEHOLDER_FOR_elem_2\n\n"
                "*** Test Cases ***\n"
                "Test\n"
                "    # WARNING: Locator not found for 'first product name'\n"
                "    # Manual intervention required: Inspect page and update locator\n"
                "    ${product_name}=    Get Text    ${product_locator}\n"
                "```\n\n"

                "--- CRITICAL RULES FOR VALIDATION ---\n"
                "When you encounter a step with keyword 'Should Be True' and a 'condition_expression' key:\n"
                "1. Generate a proper Should Be True statement with the expression\n"
                "2. The expression should be a valid Python expression that Robot Framework can evaluate\n"
                "3. Use proper Python string methods for text manipulation\n\n"
                "**Example for price validation:**\n"
                "*Input Step:*\n"
                "`{\"keyword\": \"Should Be True\", \"condition_expression\": \"${float(product_price.replace('₹', '').replace(',', '')) < 9999}\"}`\n"
                "*Output Code:*\n"
                "`    ${price_numeric}=    Evaluate    float('${product_price}'.replace('₹', '').replace(',', ''))`\n"
                "`    Should Be True    ${price_numeric} < 9999`\n\n"
                "--- HANDLING CONDITIONAL LOGIC ---\n"
                "If a step in the context contains the keys `condition_type` and `condition_value`, you MUST use the `Run Keyword If` keyword from Robot Framework's BuiltIn library. "
                "The format should be: `Run Keyword If    ${condition_value}    Keyword    argument1    argument2`\n\n"
                "**Example:**\n"
                "*Input Step:*\n"
                "`{\"keyword\": \"Input Text\", \"locator\": \"id=discount-code\", \"value\": \"SAVE10\", \"condition_type\": \"IF\", \"condition_value\": \"${total} > 100\"}`\n"
                "*Output Code:*\n"
                "`    Run Keyword If    ${total} > 100    Input Text    id=discount-code    SAVE10`\n\n"
                "--- HANDLING LOOPS ---\n"
                "If a step in the context contains the keys `loop_type` and `loop_source`, you MUST use a `FOR` loop. The `loop_source` will be a list of elements. You should iterate over this list and perform the specified action on each item.\n\n"
                "**Example:**\n"
                "*Input Step:*\n"
                "`{\"keyword\": \"Click Element\", \"loop_type\": \"FOR\", \"loop_source\": \"@{links}\"}`\n"
                "*Output Code:*\n"
                "`    FOR    ${link}    IN    @{links}`\n"
                "`        Click Element    ${link}`\n"
                "`    END`\n\n"
                
                "--- HANDLING CUSTOM DROPDOWNS (React-Select, Material-UI, etc.) ---\n"
                "⚠️ **CRITICAL**: Check the 'element_type' field for dropdown-related keywords!\n\n"
                "If keyword is 'Select Options By' (or similar) but element_type is NOT 'select':\n"
                "- The element is a CUSTOM dropdown (e.g., React-Select, Material-UI)\n"
                "- 'Select Options By' ONLY works with native <select> elements\n"
                "- For custom dropdowns, use FILL TEXT + ENTER pattern (simplest and most reliable)\n\n"
                "**When element_type IS 'select' (native dropdown):**\n"
                "```robot\n"
                "Select Options By    ${dropdown_locator}    label    Option Text\n"
                "```\n\n"
                "**When element_type is 'input', 'div', or anything other than 'select' (custom dropdown):**\n"
                "Use FILL TEXT + ENTER pattern (2 steps):\n"
                "```robot\n"
                "# Fill Text clears and types the option to filter the dropdown\n"
                "Fill Text    ${dropdown_locator}    Option Text\n"
                "# Press Enter to select the filtered/highlighted option\n"
                "Keyboard Key    press    Enter\n"
                "```\n"
                "NOTE: If value is 'label    Option Text', extract just 'Option Text' for Fill Text.\n\n"
                "**Example with element_type check:**\n"
                "*Input Step (custom dropdown):*\n"
                "`{\"keyword\": \"Select Options By\", \"locator\": \"id=react-select-4-input\", \"element_type\": \"input\", \"value\": \"label    Volvo\"}`\n"
                "*Output Code (Fill Text+Enter pattern because element_type is 'input', not 'select'):*\n"
                "```robot\n"
                "    # Custom dropdown (element_type=input) - using Fill Text+Enter pattern\n"
                "    Fill Text    id=react-select-4-input    Volvo\n"
                "    Keyboard Key    press    Enter\n"
                "```\n\n"
                "*Input Step (native select):*\n"
                "`{\"keyword\": \"Select Options By\", \"locator\": \"id=country-select\", \"element_type\": \"select\", \"value\": \"label    USA\"}`\n"
                "*Output Code (Select Options By because element_type is 'select'):*\n"
                "```robot\n"
                "    Select Options By    id=country-select    label    USA\n"
                "```\n\n"
                "--- HANDLING HIDDEN RADIO BUTTONS AND CHECKBOXES ---\n"
                "⚠️ **CRITICAL**: Check the 'element_type' field for radio/checkbox elements!\n\n"
                "Modern CSS frameworks often HIDE the actual input element with CSS.\n"
                "Standard 'Click' may FAIL because the input is not visible.\n\n"
                "**When element_type is 'radio' or 'checkbox':**\n"
                "1. Use keyword_search tool to search for 'click hidden element force'\n"
                "2. The tool will return the correct keyword with force=True option\n"
                "3. Use that keyword syntax in your generated code\n\n"
                "**When element_type is something else (button, link, div, etc.):**\n"
                "Use standard Click keyword.\n\n"
                f"--- LIBRARIES TO INCLUDE ---\n"
                f"Always include these libraries in the Settings section:\n"
                f"- {self.library_context.library_name if self.library_context else 'SeleniumLibrary'} (for web automation)\n"
                f"- BuiltIn (for basic Robot Framework keywords like Should Be True, Evaluate)\n"
                f"- String (if string manipulation is needed)\n\n"
                "--- CRITICAL OUTPUT FORMAT RULES ---\n"
                "1. ⚠️ Output ONLY raw Robot Framework code\n"
                "2. Start with *** Settings *** (first line)\n"
                "3. End with the last test keyword (e.g., Close Browser)\n"
                "4. No explanatory text before or after the code\n"
                "5. No markdown formatting (no ```)\n"
                "6. For price or numeric validations, use Evaluate to convert strings to numbers\n"
                "7. Include popup dismissal steps as specified above\n\n"
                "Example of correct output:\n"
                "*** Settings ***\n"
                "Library    Browser\n\n"
                "*** Variables ***\n"
                "${browser}    chromium\n\n"
                "*** Test Cases ***\n"
                "Generated Test\n"
                "    New Browser    ${browser}\n"
                "    Close Browser"
            ),
            expected_output=(
                "ONLY raw Robot Framework code. "
                "First line MUST be: *** Settings *** "
                "Last line MUST be: Close Browser (or similar keyword). "
                "NO thinking process. NO explanations. NO markdown. "
                "Just pure code that can be saved as .robot file."
            ),
            agent=agent,
        )

    def validate_code_task(self, agent, code_assembler_agent=None) -> Task:
        # Get library-specific validation rules
        validation_rules = ""
        if self.library_context:
            validation_rules = f"\n\n{self.library_context.validation_context}\n\n"
        else:
            # Fallback validation rules
            validation_rules = """
                --- VALIDATION CHECKLIST ---
                1. All required libraries are imported (SeleniumLibrary, BuiltIn, String if needed)
                2. All keywords have the correct number of arguments
                3. Variables are properly declared before use
                4. Should Be True statements have valid expressions
                5. Run Keyword If statements have proper syntax
                6. Price/numeric comparisons use proper conversion (Evaluate)

                --- COMMON ERRORS TO CHECK ---
                1. Get Text without locator argument
                2. Invalid expressions in Should Be True
                3. Missing variable assignments (${var}=)
                4. Incorrect conditional syntax
                """

        return Task(
            description=(
                "⚠️ **PRIMARY TASK: VALIDATE THE ROBOT FRAMEWORK CODE** ⚠️\n"
                "Your MAIN responsibility is to validate Robot Framework code for correctness.\n"
                "Delegation is ONLY for invalid code - DO NOT delegate if code is valid!\n\n"

                f"{validation_rules}"

                "--- KEYWORD VERIFICATION (CRITICAL - PREVENT FALSE POSITIVES) ---\n"
                "⚠️ BEFORE flagging ANY keyword as having wrong/missing arguments:\n"
                "1. **USE keyword_search tool** to look up the actual keyword signature\n"
                "2. **CHECK required vs optional arguments** - many parameters look required but are optional\n"
                "3. **ONLY flag an error** if the code truly violates the documented syntax\n\n"
                "Example false positives to avoid:\n"
                "- 'Keyboard Key    press    Enter' is VALID (selector is optional)\n"
                "- 'Click    ${locator}' is VALID (other args are optional)\n"
                "When in doubt, SEARCH FIRST before flagging!\n\n"

                "--- VALIDATION WORKFLOW ---\n"
                "1. **Analyze the code thoroughly** - Check syntax, keywords, variables, locators\n"
                "2. **If code is VALID:**\n"
                "   - Return {\"valid\": true, \"reason\": \"Code is syntactically correct...\"}\n"
                "   - ⚠️ DO NOT delegate to any agent\n"
                "   - Your job is DONE - stop here\n"
                "3. **If code is INVALID (has errors):**\n"
                "   - Return {\"valid\": false, \"reason\": \"...\", \"errors\": [...]}\n"
                "   - THEN delegate to Robot Framework Code Assembler to fix the errors\n\n"

                "⚠️ **CRITICAL:** Only delegate if you found ACTUAL errors! Valid code = NO delegation! ⚠️\n\n"

                "--- REQUIRED JSON OUTPUT FORMAT ---\n"
                "You MUST respond with ONLY a valid JSON object (no markdown, no extra text):\n\n"
                "**If code is VALID (no errors found):**\n"
                "{\n"
                "  \"valid\": true,\n"
                "  \"reason\": \"Code is syntactically correct and follows all validation rules. All keywords are correct, variables are properly declared, and locators are valid.\"\n"
                "}\n"
                "⚠️ DO NOT delegate when returning valid=true! ⚠️\n\n"

                "**If code is INVALID (errors found):**\n"
                "{\n"
                "  \"valid\": false,\n"
                "  \"reason\": \"Code has validation errors that need to be fixed\",\n"
                "  \"errors\": [\n"
                "    \"Missing Variable Assignment: 'Get Text' keyword on line 15 returns a value but no variable is assigned. Should be: ${result}=    Get Text    ${locator}\",\n"
                "    \"Syntax Error: Incorrect indentation on line 18. Robot Framework requires 4 spaces for test step indentation.\",\n"
                "    \"Incorrect Keyword: 'Click Button' is not a valid keyword. Use 'Click Element' instead.\"\n"
                "  ]\n"
                "}\n"
                "Then delegate to Robot Framework Code Assembler with these error details.\n\n"

                "--- ERROR REPORTING GUIDELINES (Only if code is invalid) ---\n"
                "For each error in the errors array, provide:\n"
                "- Error type (e.g., Missing Variable Assignment, Syntax Error, Incorrect Keyword)\n"
                "- Specific location (line number or keyword name)\n"
                "- What is wrong\n"
                "- How to fix it (with example if possible)\n\n"

                "--- CRITICAL RULES ---\n"
                "1. ⚠️ **MOST IMPORTANT:** If code is valid, return valid=true and DO NOT delegate!\n"
                "2. You MUST output ONLY valid JSON (no markdown code blocks, no extra text)\n"
                "3. The JSON must have 'valid' (boolean) and 'reason' (string) fields\n"
                "4. If valid=false, include 'errors' array with detailed error descriptions\n"
                "5. Do NOT include any text before or after the JSON object\n"
                "6. Only delegate to Robot Framework Code Assembler if you found actual errors\n"
                "7. Be specific and actionable in error descriptions to help fix issues"
            ),
            expected_output="A valid JSON object with 'valid' (boolean) and 'reason' (string) fields. If valid=true, DO NOT delegate. If valid=false, include 'errors' array and delegate to Robot Framework Code Assembler for correction.",
            agent=agent,
            output_json=ValidationOutput,  # Force JSON output format using Pydantic model
            # Only allow delegation to Code Assembler
            allowed_agents=[
                code_assembler_agent] if code_assembler_agent else None,
        )

    # NOTE: analyze_popup_strategy_task has been REMOVED
    # ===================================================
    # Popup handling is now user-driven:
    # 1. BrowserUse agent handles popups contextually during element location
    # 2. If users need specific popup handling, they mention it in their query
    # 3. The POPUP_STRATEGY_JSON env var and this task are deprecated

