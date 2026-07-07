from crewai import Task, TaskOutput
import logging
import json
import re
from pydantic import BaseModel, Field
from typing import List, Optional, Tuple, Any

# Import reusable prompt components
from .prompts import PromptComponents

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS FOR TASK OUTPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class PlannedStep(BaseModel):
    """Schema for a single planned test step from plan_steps_task."""
    step_description: str = Field(description="Human-readable description of the step")
    element_description: Optional[str] = Field(default=None, description="Description of the element to interact with")
    value: Optional[str] = Field(default=None, description="Value to use in the action (URL, text, key, etc.)")
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


class IdentifiedElement(PlannedStep):
    """Schema for an element with an identified locator from identify_elements_task.

    Inherits all fields from PlannedStep and adds locator-specific fields.
    """
    # Locator information from browser automation (new fields only)
    locator: Optional[str] = Field(default=None, description="Best locator for the element")
    found: Optional[bool] = Field(default=False, description="Whether locator was found")
    element_type: Optional[str] = Field(default=None, description="Element type (input, select, etc.)")
    dropdown_framework: Optional[str] = Field(default=None, description="Custom dropdown framework detected (e.g. 'tom-select'), empty string if none")
    select_id: Optional[str] = Field(default=None, description="Original <select> element ID for TomSelect — used to build the Evaluate JavaScript locator")


class IdentificationOutput(BaseModel):
    """Schema for identify_elements_task output - steps with locators."""
    steps: List[IdentifiedElement] = Field(description="Steps with identified locators")


class AssemblyOutput(BaseModel):
    """Pydantic model for assemble_code_task output - Robot Framework code."""
    code: str = Field(description="Complete Robot Framework code ready to save as .robot file")


# ═══════════════════════════════════════════════════════════════════════════
# GUARDRAIL FUNCTIONS FOR OUTPUT FORMAT FIXING
# ═══════════════════════════════════════════════════════════════════════════

def _extract_json_by_key(raw: str, required_key: str, log_prefix: str) -> Optional[str]:
    """
    Common JSON extraction logic for guardrails.
    
    Implements 3 strategies to extract valid JSON containing a specific key:
    1. Direct JSON parse
    2. Find key pattern and raw_decode  
    3. Iterate through all '{' positions - returns LAST valid JSON (LLMs self-correct)
    
    CRITICAL: For 'code' key, we prefer the LAST valid JSON because LLMs often:
    - First explain the format: {"code": "..."}  (placeholder)
    - Then output the actual: {"code": "*** Settings ***..."}  (real code)
    
    Args:
        raw: Raw LLM output string
        required_key: The JSON key that must be present (e.g., "steps", "valid")
        log_prefix: Prefix for log messages (e.g., "IdentificationOutput")
        
    Returns:
        JSON string if extraction successful, None if failed
    """
    # Strategy 1: Check if it's already valid JSON
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and required_key in data:
            logger.info(f"✅ Guardrail: {log_prefix} is already valid JSON")
            return raw
    except json.JSONDecodeError:
        pass
    
    # Strategy 2 & 3: Find ALL valid JSON objects with the required key, then pick the best one
    # For 'code' key: prefer the one with actual content (not "..." placeholder)
    # For other keys: prefer the last one (LLMs self-correct)
    candidates = []
    
    search_start = 0
    while True:
        brace_start = raw.find('{', search_start)
        if brace_start == -1:
            break
        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(raw[brace_start:])
            if isinstance(data, dict) and required_key in data:
                candidates.append({
                    'data': data,
                    'position': brace_start,
                    'value': data.get(required_key, '')
                })
                logger.debug(f"Found candidate {log_prefix} at position {brace_start}")
        except json.JSONDecodeError:
            pass
        search_start = brace_start + 1
    
    if not candidates:
        return None
    
    # For 'code' key: filter out placeholders like "..." and prefer substantive content
    if required_key == 'code':
        # Filter candidates: remove placeholders (empty, "...", very short values)
        substantive = [
            c for c in candidates 
            if isinstance(c['value'], str) and len(c['value']) > 20 and c['value'].strip() != '...'
        ]
        if substantive:
            # Return the LAST substantive one (LLMs often self-correct)
            best = substantive[-1]
            logger.info(f"✅ Guardrail: Selected {log_prefix} with substantive code (position {best['position']}, {len(best['value'])} chars)")
            return json.dumps(best['data'])
        else:
            # No substantive candidates, fall back to last one
            best = candidates[-1]
            logger.warning(f"⚠️ Guardrail: No substantive {log_prefix} found, using last candidate")
            return json.dumps(best['data'])
    else:
        # For other keys: return the last valid JSON (LLMs self-correct)
        best = candidates[-1]
        logger.info(f"✅ Guardrail: Selected last {log_prefix} at position {best['position']}")
        return json.dumps(best['data'])


def assembly_output_guardrail(result: TaskOutput) -> Tuple[bool, Any]:
    """
    Guardrail for assemble_code_task that fixes output format without retry.
    
    The LLM often outputs valid RF code but in wrong format (not JSON).
    This guardrail extracts the code and wraps it in proper JSON format.
    
    Returns:
        (True, fixed_output) - If we can fix the format, PASS immediately
        (False, feedback) - Only if truly no code found, triggers retry
    """
    raw = result.raw
    logger.debug(f"Guardrail received output (length: {len(raw)})")
    
    # Strategy 1-2: Try common JSON extraction (handles already-valid JSON and mixed content)
    extracted = _extract_json_by_key(raw, "code", "AssemblyOutput")
    if extracted:
        return (True, extracted)
    
    # Strategy 3: Extract RF code and wrap in JSON (unique to assembly)
    # Find LAST occurrence of *** Settings *** - LLMs often self-correct and output
    # code multiple times, with the last version being the corrected one
    rf_pattern = r'\*\*\*\s*(Settings|Variables|Test Cases|Keywords|Tasks)\s*\*\*\*'
    rf_matches = list(re.finditer(rf_pattern, raw, re.IGNORECASE))
    
    if rf_matches:
        # Find the last *** Settings *** section (or fall back to last match)
        last_settings = None
        for match in rf_matches:
            if match.group(1).lower() == 'settings':
                last_settings = match
        
        # Use last Settings, or last match if no Settings found
        rf_match = last_settings if last_settings else rf_matches[-1]
        code_start = rf_match.start()
        rf_code = raw[code_start:].strip()
        
        # Log if duplicates were found and handled
        if len(rf_matches) > 1:
            logger.info(f"🧹 Guardrail: Found {len(rf_matches)} RF sections, using last *** Settings ***")
        
        # Clean trailing non-code text (explanations after the code)
        for pattern in [r'\n\s*This (is|code|should)', r'\n\s*Note:', r'\n\s*The above']:
            end_match = re.search(pattern, rf_code, re.IGNORECASE)
            if end_match:
                rf_code = rf_code[:end_match.start()].strip()
        
        # Wrap in JSON
        result_json = json.dumps({"code": rf_code})
        logger.info(f"✅ Guardrail: Wrapped RF code in JSON (code length: {len(rf_code)})")
        return (True, result_json)
    
    # Strategy 4: If the entire output looks like RF code but doesn't match pattern
    # (e.g., starts with Library or has RF keywords)
    if 'Library' in raw and ('Browser' in raw or 'SeleniumLibrary' in raw):
        # Assume it's RF code, wrap it
        result_json = json.dumps({"code": raw.strip()})
        logger.info(f"✅ Guardrail: Assumed RF code and wrapped (length: {len(raw)})")
        return (True, result_json)
    
    # Only fail if truly no code found - this triggers a retry
    logger.warning("❌ Guardrail: No RF code found, requesting retry")
    return (False, "Output must contain Robot Framework code. Start with *** Settings *** section.")


def identification_output_guardrail(result: TaskOutput) -> Tuple[bool, Any]:
    """
    Guardrail for identify_elements_task that handles JSON extraction.
    
    The LLM sometimes outputs valid JSON followed by extra text (trailing characters).
    This guardrail extracts the valid JSON portion.
    
    Returns:
        (True, fixed_output) - If we can extract valid JSON
        (False, feedback) - If JSON extraction fails, triggers retry
    """
    raw = result.raw
    logger.debug(f"IdentificationOutput guardrail received (length: {len(raw)})")
    
    extracted = _extract_json_by_key(raw, "steps", "IdentificationOutput")
    if extracted:
        return (True, extracted)
    
    # Failed to extract - trigger retry
    logger.warning("❌ Guardrail: Could not extract valid IdentificationOutput JSON")
    return (False, "Output must be a valid JSON object with 'steps' array. Ensure proper JSON formatting.")


class RobotTasks:
    def __init__(self, library_context=None, hint_context: dict = None):
        """
        Initialize Robot Framework tasks.

        Args:
            library_context: LibraryContext instance (optional, for dynamic library knowledge)
            hint_context: Dict mapping agent role -> hint text for task-level injection
        """
        self.library_context = library_context
        self._hint_context = hint_context or {}

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
            # Fallback when constructed without a context (identification-only
            # usage in tests) — Browser Library is the only supported target.
            return """
            --- MANDATORY STRUCTURE ---
            ```robot
            *** Settings ***
            Library    Browser
            Library    BuiltIn

            *** Test Cases ***
            Generated Test
                New Browser    chromium    headless=True
                New Context    viewport=None
                New Page    <url>
                # Test steps here
                Close Browser
            ```
            """

    def _get_browser_init_instructions(self) -> str:
        """Get library-specific browser initialization instructions."""
        if self.library_context:
            params = self.library_context.browser_init_params
            # Browser Library is the only supported target (Task 11/E8)
            param_list = ', '.join([f'{k}={v}' for k, v in params.items()])
            return f"""
    - For Browser Library: Use "New Browser" keyword
    - Include these parameters: {param_list}
    - Example: {{"keyword": "New Browser", "browser": "{params.get('browser', 'chromium')}", "headless": "{params.get('headless', 'True')}"}}
    - DO NOT include 'options' parameter for Browser Library
            """
        else:
            # Fallback when constructed without a context (identification-only
            # usage in tests) — Browser Library defaults.
            logger.warning(
                "No library context available, using Browser Library defaults")
            return """
    - Use "New Browser" keyword with browser=chromium and headless parameters
    - Example: {"keyword": "New Browser", "browser": "chromium", "headless": "True"}
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

    def _get_task_hints(self, role: str) -> str:
        """Get mandatory hint block for a task description, or empty string.

        Mirrors the proven viewport instruction pattern: MANDATORY + CRITICAL framing
        with specific, actionable directives. Hints are injected into task descriptions
        (high salience) rather than agent backstories (low salience).
        """
        hint_text = self._hint_context.get(role, "")
        if not hint_text:
            return ""
        logger.info(f"[LEARNING] Injecting hints into {role} task description")
        return (
            "--- MANDATORY USER CORRECTIONS (CRITICAL — FROM PAST FAILURES) ---\n\n"
            "The following corrections are based on real execution failures. "
            "You MUST apply ALL of them to your output.\n\n"
            f"{hint_text}\n\n"
            "**CRITICAL**: Ignoring these corrections will cause the test to FAIL again.\n"
            "Apply every correction listed above.\n\n"
        )

    def plan_steps_task(self, agent, query) -> Task:
        # Build prompt using PromptComponents for maintainability
        description = f"""{self._get_task_hints("planner")}
            CRITICAL: Any code examples in the USER FEEDBACK blocks above are reference context only.
            Your output MUST be pure JSON — do NOT echo, copy, or reproduce any Robot Framework code from those blocks.

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

            {PromptComponents.PLANNING_OUTPUT_RULES.replace("{browser_init_placeholder}", self._cached_browser_init)}

            """
        return Task(
            description=description,
            expected_output="A JSON object with 'steps' key containing an array of test step objects. Each step has: 'step_description', 'element_description', 'value', 'keyword', and optional keys for conditions and loops.",
            agent=agent,
            output_pydantic=PlanOutput,
        )

    def identify_elements_task(self, agent) -> Task:
        return Task(
            description=(
                "⚠️ **BATCH LOCATOR IDENTIFICATION WORKFLOW**\n\n"
                "Your mission: Find locators for ALL elements in ONE batch operation.\n"
                "The context will be a JSON object from 'plan_steps_task' with: {\"steps\": [array of test steps]}.\n"
                "Extract the test steps from the 'steps' key.\n\n"
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
                "  * ⚠️ Value (from 'value' field) - CRITICAL for input actions! This is the text to type.\n"
                "\n"
                f"{PromptComponents.FORM_ELEMENT_HANDLING}\n"
                f"{PromptComponents.SPATIAL_CONTEXT_PRESERVATION}\n"
                "Example elements list:\n"
                "```json\n"
                "[\n"
                "    {\"id\": \"elem_1\", \"description\": \"username input field\", \"action\": \"input\", \"value\": \"bob@example.com\"},\n"
                "    {\"id\": \"elem_2\", \"description\": \"password input field\", \"action\": \"input\", \"value\": \"password123\"},\n"
                "    {\"id\": \"elem_3\", \"description\": \"login submit button\", \"action\": \"click\"},\n"
                "    {\"id\": \"elem_4\", \"description\": \"country dropdown\", \"action\": \"select\", \"value\": \"India\"},\n"
                "    {\"id\": \"elem_5\", \"description\": \"first item title in results list\", \"action\": \"get_text\"}\n"
                "]\n"
                "```\n"
                "\n"
                "⚠️ **CRITICAL**: The 'value' field is required for these actions:\n"
                "- 'input': text to type (credentials, search terms, etc.)\n"
                "- 'select': the exact option text to select from the dropdown (e.g. 'India', 'Male', 'Yes')\n"
                "Without the 'value' field, browser-use won't know what to type or select!\n"
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
                f"Action Input: {{\"elements\": [{{\"id\": \"elem_1\", \"description\": \"username field\", \"action\": \"input\", \"value\": \"bob@example.com\"}}, {{\"id\": \"elem_2\", \"description\": \"password field\", \"action\": \"input\", \"value\": \"password123\"}}, {{\"id\": \"elem_3\", \"description\": \"login button\", \"action\": \"click\"}}], \"url\": \"https://example.com/login\", \"user_query\": \"Login with username and password\"}}\n"
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
                "            \"element_type\": null,\n"
                "            \"element_info\": {\"tagName\": \"input\", \"id\": \"search\", ...},\n"
                "            \"all_locators\": [...]\n"
                "        },\n"
                "        \"elem_2\": {\n"
                "            \"best_locator\": \"css=#pricelist_id-ts-control\",\n"
                "            \"found\": true,\n"
                "            \"element_type\": \"dropdown\",\n"
                "            \"element_info\": {\"tagName\": \"input\", \"id\": \"pricelist_id-ts-control\", ...},\n"
                "            \"dropdown_framework\": \"tom-select\",\n"
                "            \"select_id\": \"pricelist_id\",\n"
                "            \"all_locators\": [...]\n"
                "        }\n"
                "    },\n"
                "    \"summary\": {\"total_elements\": 2, \"successful\": 2, \"failed\": 0}\n"
                "}\n"
                "```\n"
                "\n"
                "⚠️ **IMPORTANT**: Extract ALL three of these fields from each locator_mapping entry:\n"
                "- 'element_type': use the 'element_type' field directly if non-null (e.g., 'checkbox', 'radio', 'collection', 'dropdown'); otherwise fall back to 'element_info.tagName' (e.g., 'input', 'select', 'button')\n"
                "- 'dropdown_framework' → 'dropdown_framework' (e.g., 'tom-select', or empty string '')\n"
                "- 'select_id' → 'select_id' (the original <select> element ID for TomSelect, or null)\n"
                "You MUST copy all three to the step when mapping locators!\n"
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
                "  * ALSO add 'element_type', 'dropdown_framework', and 'select_id' from the response\n"
                "- If step didn't need a locator (Open Browser, Close Browser):\n"
                "  * Leave it as-is (no locator key needed)\n"
                "\n"
                "Example output:\n"
                "```json\n"
                "[\n"
                "    {\"keyword\": \"Open Browser\", \"value\": \"https://www.flipkart.com\"},\n"
                "    {\"keyword\": \"Input Text\", \"element_description\": \"search box\", \"value\": \"shoes\", \"locator\": \"name=q\", \"element_type\": \"input\"},\n"
                "    {\"keyword\": \"Press Keys\", \"element_description\": \"search box\", \"value\": \"Enter\", \"locator\": \"name=q\", \"element_type\": \"input\"},\n"
                "    {\"keyword\": \"Select Options By\", \"element_description\": \"Rate Group dropdown\", \"locator\": \"css=#pricelist_id-ts-control\", \"element_type\": \"dropdown\", \"dropdown_framework\": \"tom-select\", \"select_id\": \"pricelist_id\", \"value\": \"label    default\"}\n"
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
                "    {\"step_description\": \"Press Enter\", \"keyword\": \"Press Keys\", \"element_description\": \"search box\", \"value\": \"Enter\"},\n"
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
                "6. ✅ ALWAYS include 'value' field for input actions (username, password, search terms)\n"
                "7. ✅ Map same locator to multiple steps if they use the same element\n"
                "8. ✅ Handle partial failures gracefully (if elem_2 fails, still use elem_1 and elem_3)\n"
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
                "Action Input: {\"elements\": [{\"id\": \"elem_1\", \"description\": \"username field\", \"action\": \"input\", \"value\": \"user@example.com\"}], \"url\": \"https://example.com\", \"user_query\": \"login to site\"}\n"
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
            guardrail=identification_output_guardrail,  # Fixes JSON with trailing chars
        )

    def assemble_code_task(self, agent) -> Task:
        # Build libraries section dynamically
        library_name = self.library_context.library_name if self.library_context else 'Browser'
        libraries_section = (
            f"--- LIBRARIES TO INCLUDE ---\n"
            f"Always include these libraries in the Settings section:\n"
            f"- {library_name} (for web automation)\n"
            f"- BuiltIn (for basic Robot Framework keywords like Should Be True, Evaluate)\n"
            f"- Collections (for Get Length with lists)\n\n"
        )
        
        description = (
            f"{self._get_task_hints('assembler')}"
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
            
            f"{libraries_section}"
            
            f"{PromptComponents.ASSEMBLY_FORMAT_RULES}"
        )
        
        return Task(
            description=description,
            expected_output=(
                "A JSON object with 'code' key containing the complete Robot Framework code. "
                "Format: {\"code\": \"*** Settings ***\\nLibrary    Browser\\n...\"}. "
                "The code value must be a valid .robot file content with proper newlines (\\n)."
            ),
            agent=agent,
            output_pydantic=AssemblyOutput,
            guardrail=assembly_output_guardrail,  # Fixes format without retry
        )

    def repair_code_task(self, agent, robot_code: str, dryrun_errors: str) -> Task:
        """Conservative repair task for the dryrun gate's bounded repair loop.

        Built by the top-level repair mini-crew in dryrun_service.repair_robot_code
        (NOT part of the main 3-agent crew). The agent receives the current code and
        the exact `robot --dryrun` error text, and is instructed to change ONLY the
        flagged keyword/syntax while reproducing every locator and value VERBATIM
        (production-hardening §8.1 — prevents a one-keyword fix from silently
        rewriting a carefully-chosen locator from the Element Identifier).

        Reuses AssemblyOutput + assembly_output_guardrail so the repaired output is
        parsed by the SAME pipeline (dryrun_service.extract_and_normalize_robot_code)
        as the main assembler output — no special-casing.
        """
        description = (
            "⚠️ **CONSERVATIVE REPAIR TASK — FIX ONLY WHAT IS FLAGGED** ⚠️\n"
            "The Robot Framework code below FAILED `robot --dryrun` — a compile-time "
            "check of keyword names, argument counts, library imports, and syntax. "
            "Your ONLY job is to correct the specific problem(s) reported, then output "
            "the COMPLETE corrected file.\n\n"

            "🔒 **CRITICAL — PRESERVE EVERYTHING ELSE VERBATIM** 🔒\n"
            "1. Change ONLY the keyword(s)/syntax the error names. Touch nothing else.\n"
            "2. Reproduce EVERY locator (css=, xpath=, id=, name=, etc.) and EVERY "
            "value/argument EXACTLY as given. Do NOT re-style, rename, reorder, "
            "'improve', or substitute any locator or value.\n"
            "3. Do NOT add, remove, or reorder test steps, settings, or variables.\n"
            "4. If the error says a keyword does not exist and SUGGESTS an alternative "
            "(e.g. \"Did you mean: Browser.Click\"), use the suggested keyword.\n"
            "5. Fix to the keyword that belongs to the library already imported "
            "in the Settings section.\n\n"

            "--- ROBOT --DRYRUN ERRORS (fix exactly these) ---\n"
            f"{dryrun_errors}\n\n"

            "--- CURRENT CODE (correct in place, preserve all locators/values verbatim) ---\n"
            f"{robot_code}\n"
        )
        return Task(
            description=description,
            expected_output=(
                "A JSON object with 'code' key containing the COMPLETE corrected Robot "
                "Framework file. Format: {\"code\": \"*** Settings ***\\n...\"}. Only the "
                "flagged keyword/syntax is changed; all locators and values are reproduced "
                "verbatim. No explanations, no markdown — only the JSON object."
            ),
            agent=agent,
            output_pydantic=AssemblyOutput,
            guardrail=assembly_output_guardrail,  # same format-fixer as the main assembler
        )

    # NOTE: analyze_popup_strategy_task has been REMOVED
    # ===================================================
    # Popup handling is now user-driven:
    # 1. BrowserUse agent handles popups contextually during element location
    # 2. If users need specific popup handling, they mention it in their query
    # 3. The POPUP_STRATEGY_JSON env var and this task are deprecated

