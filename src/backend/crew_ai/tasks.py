from crewai import Task, TaskOutput
import functools
import logging
import json
import re
from pydantic import BaseModel, Field
from typing import List, Optional, Tuple, Any

# Import reusable prompt components
from .prompts import PromptComponents
from src.backend.core.grafana_events import record_guardrail_result

logger = logging.getLogger(__name__)


def _track_guardrail(name: str):
    """Emit a guardrail_passed/guardrail_retry Grafana event per invocation.

    CrewAI retries the task when a guardrail returns (False, ...), so each
    invocation counts as one attempt; the pass event carries the total.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(result: TaskOutput) -> Tuple[bool, Any]:
            outcome = fn(result)
            try:
                record_guardrail_result(name, bool(outcome[0]))
            except Exception:
                pass
            return outcome
        return wrapper
    return decorator


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
    # ASTPP flags (Task 16 boundary): stapled only when True — merge_locators
    # passes None otherwise, and model_dump(exclude_none=True) drops the key,
    # mirroring browser-service's emitted-only-when-True payload shape.
    visibility_filtered: Optional[bool] = Field(default=None, description="Locator engine narrowed the match set to visible elements (ASTPP flag B) — context for healing/debugging")
    row_anchored: Optional[bool] = Field(default=None, description="Locator was rescued by anchoring to a specific row's text (ASTPP flag A / G1)")
    row_anchor_ambiguous: Optional[bool] = Field(default=None, description="Row anchor matched more than one row and the locator fell back to a positional form — pairs with stability='positional', which already drives the Assembler's WARNING comment")


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


@_track_guardrail("assembly_output")
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


# ═══════════════════════════════════════════════════════════════════════════
# DETERMINISTIC PROMPT COMPOSER (Task 24R Stage 2)
# ═══════════════════════════════════════════════════════════════════════════
# The 8 element-type blocks are included only when a MERGED step's own
# routing fields need them (plan §B trigger table — each trigger reads the
# same fields its block routes on). Fail-open: ANY doubt or exception →
# include ALL blocks and ship the steps JSON verbatim = today's prompt.

def _norm(value: Any) -> str:
    """Trigger-matching view of a step field: lowercased/stripped. Tolerates
    the tagName fallback in merge_locators ('SELECT') and keyword case drift
    — a missed trigger would silently drop a NEEDED block."""
    return str(value).strip().lower() if value is not None else ""


def _needs_stability_warning(step: dict) -> bool:
    stability = step.get("stability")
    return stability is not None and _norm(stability) != "stable"


def _needs_conditional(step: dict) -> bool:
    return bool(step.get("condition_type") or step.get("condition_value"))


def _needs_loop(step: dict) -> bool:
    # Either loop key is enough, mirroring _needs_conditional: LOOP_HANDLING
    # routes on loop_type AND loop_source, so a step carrying only one of
    # them still needs the block.
    #
    # element_type == "collection" is the third signal, and the one the
    # planner cannot give us: the service classifies a multi-match element as
    # a collection, but the planner still emits a SINGULAR keyword for it
    # ("get the titles of all books" -> Get Text). Browser's Get Text /
    # Get Attribute / Click resolve strictly, so a 20-match locator raises
    # "strict mode violation" at run time — invisible to --dryrun. Routing on
    # the loop keys alone withheld this block from 6 of the 8 strict-mode
    # failures in bench history, every one of which already carried
    # element_type=collection in the prompt. Matches how _needs_dropdown,
    # _needs_checkbox_radio, _needs_file_upload and _needs_date_picker all
    # route on element_type.
    return (
        bool(step.get("loop_type") or step.get("loop_source"))
        or _norm(step.get("element_type")) == "collection"
        or _norm(step.get("keyword")) == "get elements"
    )


def _needs_dropdown(step: dict) -> bool:
    # Keyword match covers the whole select family ("Select Options By
    # Label", "Select From List By Value", ...) — the same vocabulary
    # element_identification._ACTION_EXACT accepts. Matters on found:false
    # steps, where the keyword is the only dropdown signal left (no
    # element_type from the service). "Select Checkbox"/"Unselect Checkbox"
    # are click-family — excluded.
    keyword = _norm(step.get("keyword"))
    return (
        _norm(step.get("element_type")) in ("select", "dropdown")
        or bool(step.get("dropdown_framework"))
        or (keyword.startswith("select") and "checkbox" not in keyword)
    )


def _needs_checkbox_radio(step: dict) -> bool:
    # Keyword fallback for the same reason as _needs_dropdown: on found:false
    # there is no element_type, and dropping the block there costs the
    # force=True idiom on exactly the steps a human has to repair. Covers
    # Check/Uncheck/Select/Unselect Checkbox (element_identification._ACTION_EXACT).
    return (
        _norm(step.get("element_type")) in ("radio", "checkbox")
        or "checkbox" in _norm(step.get("keyword"))
    )


def _needs_file_upload(step: dict) -> bool:
    # Keyword fallback (see _needs_checkbox_radio): without the block a
    # found:false upload step loses the never-Click rule, which is the whole
    # point of FILE_UPLOAD_HANDLING.
    return (
        _norm(step.get("element_type")) == "file-upload"
        or _norm(step.get("keyword")).startswith("upload")
    )


def _needs_date_picker(step: dict) -> bool:
    # No keyword fallback here on purpose: the planner emits a plain
    # "Fill Text" for date fields, so there is no date-specific keyword to
    # trigger on — element_type/datepicker_framework are the only signals.
    return (
        _norm(step.get("element_type")) == "date-picker"
        or bool(step.get("datepicker_framework"))
    )


def _needs_state_verification(step: dict) -> bool:
    return _norm(step.get("keyword")) in ("get classes", "get attribute")


# The assembler description's rule sections in TODAY's order — trigger=None
# marks the always-sent core; the fail-open path (all triggers pass) then
# reproduces today's prompt exactly.
_ASSEMBLER_RULE_SECTIONS = (
    ("VARIABLE_DECLARATION_RULES", None),
    ("LOCATOR_RULES", None),
    ("STABILITY_WARNING_RULES", _needs_stability_warning),
    ("VALIDATION_RULES", None),
    ("CONDITIONAL_LOGIC_HANDLING", _needs_conditional),
    ("LOOP_HANDLING", _needs_loop),
    ("DROPDOWN_HANDLING", _needs_dropdown),
    ("CHECKBOX_RADIO_HANDLING", _needs_checkbox_radio),
    ("FILE_UPLOAD_HANDLING", _needs_file_upload),
    ("DATE_PICKER_HANDLING", _needs_date_picker),
    ("STATE_VERIFICATION_HANDLING", _needs_state_verification),
)

_ALL_CONDITIONAL_BLOCKS = [
    name for name, trigger in _ASSEMBLER_RULE_SECTIONS if trigger is not None
]


def select_conditional_blocks(steps: List[dict]) -> List[str]:
    """Names of the conditional blocks these merged steps need, in the
    description's order. Fail-open: any exception → ALL conditional blocks."""
    try:
        return [
            name for name, trigger in _ASSEMBLER_RULE_SECTIONS
            if trigger is not None and any(trigger(step) for step in steps)
        ]
    except Exception:
        logger.exception(
            "PROMPT COMPOSER: trigger evaluation failed — failing open "
            "(all conditional blocks included)")
        return list(_ALL_CONDITIONAL_BLOCKS)


def _slim_keeps(key: str, value) -> bool:
    """Keep/drop rule for one slim_steps_view field. `found` ALWAYS survives
    (LOCATOR_RULES routes on it, False included)."""
    if key == "found":
        return True
    if key == "all_locators":
        return False
    if value is None or value == "" or value == []:
        return False
    return not (key == "stability" and value == "stable")


def slim_steps_view(steps: List[dict]) -> List[dict]:
    """PROMPT-ONLY slim view of the merged steps (F7, ~203 t/run dead
    weight). merge_locators output itself is untouched — this builds new
    dicts. Drops all_locators (never instructed, always 1 redundant entry),
    any ""/[]/None value, and stability=="stable"."""
    return [
        {key: value for key, value in step.items() if _slim_keeps(key, value)}
        for step in steps
    ]


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
        # NOTE (Task 24R Stage 1): code-structure and viewport instructions are
        # NOT cached/shipped here anymore — they live ONCE in the assembler
        # agent's system prompt (library_context.code_assembly_context via
        # RobotAgents._get_agent_context); the task description used to re-ship
        # both verbatim (~1,130 tokens/run, finding F1).
        self._cached_keyword_guidelines = self._get_keyword_guidelines()
        self._cached_browser_init = self._get_browser_init_instructions()

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
        # The preamble only makes sense when a hint block was actually
        # injected above it — with no hints it referenced blocks that do not
        # exist (finding F9; always the case in bench, learning-OFF).
        planner_hints = self._get_task_hints("planner")
        if planner_hints:
            planner_hints += (
                "CRITICAL: Any code examples in the USER FEEDBACK blocks above are reference context only.\n"
                "Your output MUST be pure JSON — do NOT echo, copy, or reproduce any Robot Framework code from those blocks.\n"
            )
        description = f"""{planner_hints}
            Your mission is to act as an expert Test Automation Planner. You must analyze a user's natural language query and decompose it into a comprehensive, step-by-step test plan that a junior test engineer could follow.

            The user query is: "{query}"

            {PromptComponents.EXPLICIT_ELEMENTS_ONLY_RULES}

            --- CORE PRINCIPLES ---
            1.  **Explicitness:** Your plan must be explicit. Do not assume any prior context. If a user says "log in", you must include steps for navigating to the login page, entering the username, entering the password, and clicking the submit button.
            2.  **Decomposition:** Break down complex actions into smaller, single-action steps. For example, "search for a product and add it to the cart" should be multiple steps: "Input text into search bar", "Press Keys with Enter to search", "Click product link", "Click add to cart button".
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

        Task 24R Stage 2: the description is composed deterministically —
        element-type rule blocks are included only when the merged steps'
        own routing fields need them, the steps ship as a slim prompt-only
        view, and the steps JSON sits LAST (dynamic tail). Fail-open: any
        doubt/exception → all blocks + the original JSON verbatim.
        """
        included_blocks = list(_ALL_CONDITIONAL_BLOCKS)
        steps_json_for_prompt = identified_steps_json
        try:
            payload = json.loads(identified_steps_json)
            steps = payload.get("steps") if isinstance(payload, dict) else None
            if isinstance(steps, list) and all(isinstance(s, dict) for s in steps):
                included_blocks = select_conditional_blocks(steps)
                steps_json_for_prompt = json.dumps({"steps": slim_steps_view(steps)})
            else:
                logger.warning(
                    "PROMPT COMPOSER: unexpected steps JSON shape — failing "
                    "open (all conditional blocks, steps JSON verbatim)")
        except Exception:
            logger.exception(
                "PROMPT COMPOSER: steps JSON processing failed — failing "
                "open (all conditional blocks, steps JSON verbatim)")
        logger.info(
            "PROMPT COMPOSER: conditional blocks included=[%s] (%d of %d)",
            ", ".join(included_blocks) or "none",
            len(included_blocks), len(_ALL_CONDITIONAL_BLOCKS),
        )

        # Code structure + viewport rules are NOT shipped here — they live
        # once, in the assembler's system prompt (F1 dedup).
        rule_sections = "\n".join(
            getattr(PromptComponents, name)
            for name, trigger in _ASSEMBLER_RULE_SECTIONS
            if trigger is None or name in included_blocks
        )

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
            f"{PromptComponents.ASSEMBLY_OUTPUT_RULES}\n"
            f"{rule_sections}\n"
            f"{libraries_section}"
            "--- TEST STEPS WITH LOCATORS (deterministic element identification) ---\n"
            "The following JSON object holds the test steps; steps that target page "
            "elements carry validated locators and their metadata. "
            "Extract the steps array from the 'steps' key to generate Robot Framework code.\n"
            f"{steps_json_for_prompt}"
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

