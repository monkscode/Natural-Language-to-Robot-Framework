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
    """Schema for a step with its identified locator contract.

    Produced by the deterministic element stage (element_identification.py,
    Task 16) — the full locator_mapping entry is stapled onto the plan step.
    Inherits all fields from PlannedStep and adds locator-specific fields.
    """
    # Locator information from browser automation (new fields only)
    locator: Optional[str] = Field(default=None, description="Best locator for the element")
    found: Optional[bool] = Field(default=False, description="Whether locator was found")
    element_type: Optional[str] = Field(default=None, description="Element type (input, select, etc.)")
    dropdown_framework: Optional[str] = Field(default=None, description="Custom dropdown framework detected (e.g. 'tom-select'), empty string if none")
    select_id: Optional[str] = Field(default=None, description="Original <select> element ID for TomSelect — used to build the Evaluate JavaScript locator")
    datepicker_framework: Optional[str] = Field(default=None, description="Date-picker widget framework detected (e.g. 'flatpickr'), empty string if none — routes the Assembler to the setDate Evaluate JavaScript idiom")
    element_classes: Optional[str] = Field(default=None, description="Space-separated class list observed on the element at locate time (from element_info.className) — captured after the preceding steps ran, so it is the evidence for Get Classes state verifications; empty string if the element has no class attribute")
    aria_invalid: Optional[str] = Field(default=None, description="Observed aria-invalid attribute value (from element_info.ariaInvalid) — 'true' when the page marks the field invalid via ARIA; routes the Assembler to a Get Attribute assertion when no error-family class is observed")
    parent_classes: Optional[str] = Field(default=None, description="Space-separated class list observed on the element's immediate parent at locate time (from element_info.parentClassName) — Bootstrap-3-era sites mark invalid fields on the wrapper div; the Assembler asserts one level up via '${locator} >> xpath=..' when the marker lives there")
    stability: Optional[str] = Field(default=None, description="Locator stability verdict from browser-service (Task 10): 'stable', 'volatile', or 'positional' — anything but 'stable' makes the Assembler emit an in-code WARNING comment above the step")
    all_locators: Optional[List[Any]] = Field(default=None, description="Full validated-locator candidate list from browser-service — forwarded so self-healing (Task 21) has alternatives at the Assembler boundary")


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
        log_prefix: Prefix for log messages (e.g., "PlanOutput")
        
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
                New Context    viewport={'width': 1920, 'height': 1080}
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

**Why**: Headless Chromium's default window is 800x600 even when the
viewport is set to None (that only disables Playwright's viewport
*emulation* layer, not the underlying window size). At 800x600, many real sites switch
to a mobile/responsive layout — nav items collapse into closed hamburger
menus, and text-based locators can silently match the wrong (but visible)
element instead of erroring. This causes:
- Elements outside viewport are not detected
- Locators match decoy elements in the mobile/narrow layout
- Tests fail with "element not found" or timeout errors on later steps

**Correct Order**:
1. New Browser    ${{browser}}    headless=${{headless}}
2. New Context    viewport={{'width': 1920, 'height': 1080}}    ← REQUIRED
3. New Page    ${{url}}

**Example**:
```robot
*** Test Cases ***
Generated Test
    New Browser    chromium    headless=True
    New Context    viewport={{'width': 1920, 'height': 1080}}
    New Page    https://example.com
    # Test steps here
```

**CRITICAL**: an explicit desktop-sized viewport ensures the site renders
its normal desktop layout, matching what was seen during element
identification (which already runs at 1920x1080).
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

            {PromptComponents.PLANNING_STATE_VERIFICATION}

            {PromptComponents.PLANNING_LOOP_HANDLING}

            {PromptComponents.PLANNING_OUTPUT_RULES.replace("{browser_init_placeholder}", self._cached_browser_init)}

            """
        return Task(
            description=description,
            expected_output="A JSON object with 'steps' key containing an array of test step objects. Each step has: 'step_description', 'element_description', 'value', 'keyword', and optional keys for conditions and loops.",
            agent=agent,
            output_pydantic=PlanOutput,
        )

    def assemble_code_task(self, agent, identified_steps_json: str) -> Task:
        """Build the assembler task around the merged steps.

        Task 16: the steps (with the full locator contract stapled on by the
        deterministic element stage) are embedded directly in the description —
        the assembler runs in its own single-task crew, so there is no CrewAI
        context chain to carry them.
        """
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

            "--- TEST STEPS WITH LOCATORS (deterministic element identification) ---\n"
            "The following JSON object holds the test steps; steps that target page "
            "elements carry validated locators and their metadata.\n"
            f"{identified_steps_json}\n\n"
            "Extract the steps array from the 'steps' key to generate Robot Framework code.\n\n"
            
            f"{self._cached_code_structure}\n\n"
            
            f"{self._cached_viewport}\n\n"
            
            f"{PromptComponents.VARIABLE_DECLARATION_RULES}\n\n"
            
            f"{PromptComponents.USE_PROVIDED_LOCATORS_RULES}\n\n"
            
            f"{PromptComponents.LOCATOR_MAPPING_RULES}\n\n"

            f"{PromptComponents.STABILITY_WARNING_RULES}\n\n"
            
            f"{PromptComponents.VALIDATION_RULES}\n"
            
            f"{PromptComponents.CONDITIONAL_LOGIC_HANDLING}\n"
            
            f"{PromptComponents.LOOP_HANDLING}\n"
            
            f"{PromptComponents.DROPDOWN_HANDLING}\n"
            
            f"{PromptComponents.CHECKBOX_RADIO_HANDLING}\n"

            f"{PromptComponents.FILE_UPLOAD_HANDLING}\n"

            f"{PromptComponents.DATE_PICKER_HANDLING}\n"

            f"{PromptComponents.STATE_VERIFICATION_HANDLING}\n"

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
        (NOT part of the main pipeline's crews). The agent receives the current code and
        the exact `robot --dryrun` error text, and is instructed to change ONLY the
        flagged keyword/syntax while reproducing every locator and value VERBATIM
        (production-hardening §8.1 — prevents a one-keyword fix from silently
        rewriting a carefully-chosen locator from the locator engine).

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

