from crewai import Task
import logging
from pydantic import BaseModel, Field
from typing import List, Optional

# Import reusable prompt components
from .prompts import PromptComponents

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS FOR TASK OUTPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class PlannedStep(BaseModel):
    """Schema for a single planned test step from plan_steps_task."""
    step_description: str = Field(description="Human-readable description of the step")
    element_description: str = Field(description="Description of the element to interact with")
    value: str = Field(description="Value to use in the action (URL, text, key, etc.)")
    keyword: str = Field(description="Robot Framework keyword to use")
    # Optional fields for conditional logic and loops
    condition_type: Optional[str] = Field(default=None, description="IF for conditional steps")
    condition_value: Optional[str] = Field(default=None, description="Condition expression")
    loop_type: Optional[str] = Field(default=None, description="FOR for loop steps")
    loop_source: Optional[str] = Field(default=None, description="Element containing items to loop")
    condition_expression: Optional[str] = Field(default=None, description="Expression for Should Be True")


class PlanOutput(BaseModel):
    """Schema for plan_steps_task output - a list of planned steps."""
    steps: List[PlannedStep] = Field(description="List of planned test steps")


class IdentifiedElement(BaseModel):
    """Schema for an element with an identified locator from identify_elements_task."""
    step_description: str = Field(description="Original step description")
    element_description: str = Field(description="Description of the element")
    value: str = Field(description="Value to use in the action")
    keyword: str = Field(description="Robot Framework keyword")
    # Locator information from browser automation
    locator: Optional[str] = Field(default=None, description="Best locator for the element")
    found: bool = Field(default=False, description="Whether locator was found")
    element_type: Optional[str] = Field(default=None, description="Element type (input, select, etc.)")
    filter_text: Optional[str] = Field(default=None, description="Filter text for table verification")
    # Inherited optional fields
    condition_type: Optional[str] = Field(default=None)
    condition_value: Optional[str] = Field(default=None)
    loop_type: Optional[str] = Field(default=None)
    loop_source: Optional[str] = Field(default=None)
    condition_expression: Optional[str] = Field(default=None)


class IdentificationOutput(BaseModel):
    """Schema for identify_elements_task output - steps with locators."""
    steps: List[IdentifiedElement] = Field(description="Steps with identified locators")


class ValidationOutput(BaseModel):
    """Pydantic model for validation task output."""
    valid: bool = Field(description="Whether the code is valid or not")
    reason: str = Field(description="Explanation of validation result")
    errors: Optional[List[str]] = Field(
        default=None, description="List of error messages if code is invalid")


class AssemblyOutput(BaseModel):
    """Pydantic model for assemble_code_task output - Robot Framework code."""
    code: str = Field(description="Complete Robot Framework code ready to save as .robot file")
    # Future extensibility fields (optional)
    warnings: Optional[List[str]] = Field(
        default=None, description="Any warnings generated during code assembly")


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
        
        # Cache static context - computed once on initialization
        # These values depend on library_context which is set at init time
        self._cached_keyword_guidelines = self._get_keyword_guidelines()
        self._cached_code_structure = self._get_code_structure_template()
        self._cached_browser_init = self._get_browser_init_instructions()
        self._cached_viewport = self._get_viewport_instructions()

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
        # Build prompt using PromptComponents for maintainability
        description = f"""
            Your mission is to act as an expert Test Automation Planner. You must analyze a user's natural language query and decompose it into a comprehensive, step-by-step test plan that a junior test engineer could follow.

            The user query is: "{query}"

            {PromptComponents.EXPLICIT_ELEMENTS_ONLY_RULES}

            --- CORE PRINCIPLES ---
            1.  **Explicitness:** Your plan must be explicit. Do not assume any prior context. If a user says "log in", you must include steps for navigating to the login page, entering the username, entering the password, and clicking the submit button.
            2.  **Decomposition:** Break down complex actions into smaller, single-action steps. For example, "search for a product and add it to the cart" should be multiple steps: "Input text into search bar", "Click search button", "Click product link", "Click add to cart button".
            3.  **Keyword Precision:** Use the most appropriate Robot Framework keyword for each action.
            4.  **User Intent Only:** ONLY create steps for what the user explicitly asked for.

            --- KEYWORD GUIDELINES ---
            {self._cached_keyword_guidelines}

            {PromptComponents.SEARCH_OPTIMIZATION_RULES}

            {PromptComponents.ELEMENT_DESCRIPTION_RULES}

            {PromptComponents.PLANNING_CONDITIONAL_LOGIC}

            {PromptComponents.PLANNING_LOOP_HANDLING}

            --- FINAL OUTPUT RULES ---
            1.  You MUST respond with ONLY valid JSON in this exact format: {{"steps": [...]}}
            2.  The "steps" array contains objects, each representing a single test step with keys: "step_description", "element_description", "value", and "keyword".
            3.  For validation steps, use "Should Be True" keyword with a "condition_expression" key.
            4.  The keys `condition_type`, `condition_value`, `loop_type`, and `loop_source` are OPTIONAL and should only be included for steps with conditional logic or loops.
            5.  If the query involves a web search (e.g., "search for X") but does not specify a URL, you MUST generate a first step to open a search engine. Use 'https://www.google.com' as the value for the URL.
            6.  When generating a browser initialization step, you MUST include library-specific parameters:
            {self._cached_browser_init}
            7.  **CRITICAL**: For ANY search operation (Google, Flipkart, Amazon, etc.), after "Input Text" step, use "Press Keys" with value "RETURN" instead of generating a separate "Click Element" step for search button. This applies to ALL websites.
            8.  **MOST CRITICAL**: DO NOT add popup dismissal, cookie consent, or any steps not explicitly mentioned in user query. The browser automation handles these automatically.

            """
        return Task(
            description=description,
            expected_output="A JSON object with 'steps' key containing an array of test step objects. Each step has: 'step_description', 'element_description', 'value', 'keyword', and optional keys for conditions and loops.",
            agent=agent,
            output_pydantic=PlanOutput,
        )

    def identify_elements_task(self, agent) -> Task:
        workflow_id_instruction = f"\n**WORKFLOW ID**: {self.workflow_id}\n⚠️ CRITICAL: You MUST include 'workflow_id': '{self.workflow_id}' in the Action Input dictionary for metrics tracking.\n\n" if self.workflow_id else ""
        
        return Task(
            description=(
                "⚠️ **BATCH LOCATOR IDENTIFICATION WORKFLOW**\n\n"
                "Your mission: Find locators for ALL elements in ONE batch operation.\n"
                "The context will be a JSON object from 'plan_steps_task' with: {\"steps\": [array of test steps]}.\n"
                "Extract the test steps from the 'steps' key.\n\n"
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
                "  * ALSO add 'element_type' from the response (e.g., 'input', 'select', 'table-verification')\n"
                "  * If 'filter_text' is present in the response, add it to the step JSON\n"
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
                "    {\"keyword\": \"Get Text\", \"element_description\": \"table data\", \"locator\": \".rt-tbody .rt-tr-group:has-text(\\\"Cierra\\\")\", \"element_type\": \"table-verification\", \"filter_text\": \"Cierra\"},\n"
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
                "Return a JSON object with 'steps' key: {\"steps\": [...]} with 'locator' keys added to steps that need them.\n"
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
            expected_output="A JSON object with 'steps' key containing an array of test step objects with 'locator', 'found', and 'element_type' keys added from batch_browser_automation.",
            agent=agent,
            output_pydantic=IdentificationOutput,
        )

    def assemble_code_task(self, agent) -> Task:
        # Build libraries section dynamically
        library_name = self.library_context.library_name if self.library_context else 'SeleniumLibrary'
        libraries_section = (
            f"--- LIBRARIES TO INCLUDE ---\n"
            f"Always include these libraries in the Settings section:\n"
            f"- {library_name} (for web automation)\n"
            f"- BuiltIn (for basic Robot Framework keywords like Should Be True, Evaluate)\n"
            f"- String (for Strip String - REQUIRED for table verification)\n"
            f"- Collections (for Get Length with lists)\n\n"
        )
        
        description = (
            f"{PromptComponents.ASSEMBLY_OUTPUT_RULES}\n\n"
            
            "The context will be a JSON object from 'identify_elements_task' with: {\"steps\": [array of steps with locators]}.\n"
            "Extract the steps array from the 'steps' key to generate Robot Framework code.\n\n"
            
            f"{self._cached_code_structure}\n\n"
            
            f"{self._cached_viewport}\n\n"
            
            f"{PromptComponents.VARIABLE_DECLARATION_RULES}\n\n"
            
            f"{PromptComponents.USE_PROVIDED_LOCATORS_RULES}\n\n"
            
            f"{PromptComponents.LOCATOR_MAPPING_RULES}\n\n"
            
            f"{PromptComponents.VALIDATION_RULES}\n"
            
            f"{PromptComponents.CONDITIONAL_LOGIC_HANDLING}\n"
            
            f"{PromptComponents.LOOP_HANDLING}\n"
            
            f"{PromptComponents.DROPDOWN_HANDLING}\n"
            
            f"{PromptComponents.CHECKBOX_RADIO_HANDLING}\n"
            
            f"{PromptComponents.TABLE_VERIFICATION_HANDLING}\n\n"
            
            f"{libraries_section}"
            
            f"{PromptComponents.ASSEMBLY_FORMAT_RULES}"
        )
        
        return Task(
            description=description,
            expected_output=(
                "A JSON object with 'code' key containing the complete Robot Framework code. "
                "Format: {\"code\": \"*** Settings ***\\nLibrary    Browser\\n...\"}. "
                "The code value must be a valid .robot file content with proper newlines (\\n). "
                "Optionally include 'warnings' array for any issues encountered."
            ),
            agent=agent,
            output_pydantic=AssemblyOutput,
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
            output_pydantic=ValidationOutput,  # Force structured JSON output using Pydantic model
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

