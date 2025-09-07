import os
import json
import logging
import time
import re
from typing import List, Optional
from pydantic import BaseModel, Field
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
import ollama


# --- Gemini API Wrapper with Dynamic Retry Logic ---
def call_gemini_with_retry(model, prompt: str, max_retries: int = 2):
    """
    Calls the Gemini API with a dynamic retry mechanism based on the API's feedback.
    """
    attempt = 0
    while attempt < max_retries:
        try:
            response = model.generate_content(prompt)
            return response
        except ResourceExhausted as e:
            attempt += 1
            error_message = str(e)

            # Use regex to find the retry delay in the error message
            match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_message)

            if match:
                wait_time = int(match.group(1)) + 1 # Add a 1-second buffer
                logging.warning(
                    f"Gemini API quota exceeded. Retrying after {wait_time} seconds (attempt {attempt}/{max_retries})."
                )
                time.sleep(wait_time)
            else:
                # If no specific delay is found, wait a default time or re-raise
                logging.warning(
                    f"Gemini API quota exceeded, but no retry_delay found. "
                    f"Waiting 60 seconds before attempt {attempt}/{max_retries}."
                )
                time.sleep(60) # Fallback wait time

        except Exception as e:
            logging.error(f"An unexpected error occurred calling Gemini API: {e}")
            raise e # Re-raise other exceptions immediately

    logging.error(f"Gemini API call failed after {max_retries} attempts.")
    raise Exception("Gemini API call failed after multiple retries.")


# --- Pydantic Models for Agent Communication ---
# These models define the "contracts" for data passed between agents.

class PlannedStep(BaseModel):
    """The output of the Step-Planning Agent."""
    step_description: str = Field(description="A high-level description of a single action to perform.")
    element_description: Optional[str] = Field(None, description="The description of the UI element to interact with, if any.")
    value: Optional[str] = Field(None, description="The value to be used in the action, e.g., text to input or a URL.")
    keyword: str = Field(description="The suggested Robot Framework keyword to use for this step (e.g., 'Input Text', 'Click Element').")

class LocatedStep(PlannedStep):
    """The output of the Element-Identification Agent."""
    locator: Optional[str] = Field(None, description="The Robot Framework locator for the element (e.g., 'id=search-bar').")

# --- Agent Implementations ---

def agent_step_planner(query: str, model_provider: str, model_name: str) -> List[PlannedStep]:
    """Agent 1 (AI): Breaks the query into a structured plan of high-level steps."""
    logging.info(f"Step Planner: Analyzing query with {model_provider} model '{model_name}'.")
    prompt = f"""
    You are an expert test automation planner for Robot Framework. Your task is to break down a natural language query into a structured series of high-level test steps. You must convert ALL actions mentioned in the query into a step. Do not drop or ignore any part of the query.

    The user query is: "{query}"

    --- RULES ---
    1.  Respond with a JSON array of objects. Each object represents a single test step.
    2.  Each object must have keys: "step_description", "element_description", "value", and "keyword".
    3.  If the query involves a web search (e.g., "search for X") but does not specify a URL, you MUST generate a first step to open a search engine. Use 'https://www.google.com' as the value for the URL.
    4.  When generating an "Open Browser" step, you MUST also include the `browser=chrome` argument and options to ensure a clean session. Use `options=add_argument("--headless");add_argument("--no-sandbox")`.

    --- EXAMPLES ---

    Example Query 1: "go to aqa.science and click the login button"
    Example JSON Response 1:
    [
        {{
            "step_description": "Navigate to a URL",
            "element_description": null,
            "value": "https://aqa.science  browser=chrome  options=add_argument(\"--headless\");add_argument(\"--no-sandbox\")",
            "keyword": "Open Browser"
        }},
        {{
            "step_description": "Click the login button",
            "element_description": "the login button",
            "value": null,
            "keyword": "Click Element"
        }}
    ]

    Example Query 2: "search for dhruvil vyas and then close the browser"
    Example JSON Response 2:
    [
        {{
            "step_description": "Navigate to a search engine",
            "element_description": null,
            "value": "https://www.google.com    browser=chrome",
            "keyword": "Open Browser"
        }},
        {{
            "step_description": "Input the search term into the search bar",
            "element_description": "the search input field",
            "value": "dhruvil vyas",
            "keyword": "Input Text"
        }},
        {{
            "step_description": "Press the ENTER key to start the search",
            "element_description": "the search input field",
            "value": "ENTER",
            "keyword": "Press Keys"
        }}
    ]

    Now, process the user query following all rules and generate the JSON response.
    """
    try:
        if model_provider == "local":
            response = ollama.chat(
                model=model_name,
                messages=[{'role': 'user', 'content': prompt}],
                options={'temperature': 0.0},
                format="json"
            )
            cleaned_response = response['message']['content']
        else: # Default to online
            model = genai.GenerativeModel(model_name)
            response = call_gemini_with_retry(model, prompt)
            cleaned_response = response.text.strip().lstrip("```json").rstrip("```").strip()

        planned_steps_data = json.loads(cleaned_response)

        # Handle both single object and list of objects
        if isinstance(planned_steps_data, dict):
            planned_steps_data = [planned_steps_data]

        return [PlannedStep(**step) for step in planned_steps_data]
    except Exception as e:
        raw_response = ""
        if model_provider == "local" and 'response' in locals():
            raw_response = response['message']['content']
        elif 'response' in locals():
            raw_response = response.text
        logging.error(f"Step Planner Agent failed: {e}\nRaw response was:\n{raw_response}")
        return []

def agent_element_identifier(steps: List[PlannedStep], model_provider: str, model_name: str) -> List[LocatedStep]:
    """Agent 2 (AI): For each step, finds the best locator for the described element."""
    logging.info(f"Element Identifier: Finding locators with {model_provider} model '{model_name}'.")
    located_steps = []
    delay_seconds = int(os.getenv("SECONDS_BETWEEN_API_CALLS", "0"))

    for i, step in enumerate(steps):
        if step.element_description:
            if i > 0 and delay_seconds > 0:
                logging.info(f"Element Identifier: Waiting for {delay_seconds} second(s).")
                time.sleep(delay_seconds)

            prompt = f"""You are an expert web element locator specialist for Robot Framework automation. Your task is to generate the most reliable and stable locator for web elements based on their description.

<task_description>
Your goal is to identify the best locator for a web element described as: "{step.element_description}"
The action to be performed is: "{step.step_description}"
The value/data to be used: "{step.value or 'N/A'}"
</task_description>

<locator_strategy>
When creating locators, follow this priority order:
1. **ID attributes** - Most stable (e.g., `id=submit-button`)
2. **Name attributes** - Very stable (e.g., `name=username`)
3. **Data attributes** - Stable (e.g., `css=[data-testid="login-btn"]`)
4. **ARIA labels** - Accessible and stable (e.g., `css=[aria-label="Submit form"]`)
5. **Class combinations** - Moderately stable (e.g., `css=.btn.btn-primary`)
6. **Text content** - Fallback for buttons/links (e.g., `xpath=//button[text()="Submit"]`)
7. **Partial text** - For dynamic content (e.g., `xpath=//button[contains(text(),"Submit")]`)
8. **Structural position** - Last resort (e.g., `xpath=(//button)[1]`)
</locator_strategy>

<locator_rules>
- Always prefer CSS selectors over XPath when possible (faster execution)
- Use XPath only when CSS cannot achieve the requirement
- Avoid fragile locators like absolute positions or multiple nested classes
- For input fields, prefer `name`, `id`, or `data-testid` attributes
- For buttons, prefer `id`, `aria-label`, or stable text content
- For links, prefer `id`, `href` patterns, or link text
- Make locators as specific as needed but not overly complex
- Consider element hierarchy only when necessary for uniqueness
</locator_rules>

<element_patterns>
Common element patterns and their best locator strategies:

**Search boxes/Input fields:**
- `id=search`, `name=q`, `css=[placeholder*="search"]`
- `css=input[type="search"]`, `css=[data-testid="search-input"]`

**Buttons:**
- `id=submit`, `css=button[type="submit"]`, `css=[aria-label="Submit"]`
- `xpath=//button[text()="Submit"]`, `css=.submit-btn`

**Links:**
- `id=login-link`, `css=a[href*="/login"]`, `link=Login`
- `xpath=//a[contains(text(),"Login")]`

**Form elements:**
- `name=username`, `id=password`, `css=[data-field="email"]`
- `css=input[type="email"]`, `css=select[name="country"]`

**List items/Results:**
- `css=.result-item:first-child`, `xpath=(//div[@class="result"])[1]`
- `css=[data-index="0"]`, `xpath=//li[position()=1]`

**Navigation elements:**
- `css=nav a[href="/home"]`, `css=.navbar .menu-item`
- `xpath=//nav//a[text()="Home"]`
</element_patterns>

<response_format>
Respond with a single JSON object containing only the "locator" key.
The locator value must be a valid Robot Framework locator string.

Examples:
{{"locator": "id=submit-button"}}
{{"locator": "css=button[aria-label='Search']"}}
{{"locator": "name=username"}}
{{"locator": "xpath=//button[contains(text(),'Submit')]"}}
{{"locator": "css=.search-input[placeholder*='Search']"}}
</response_format>

<reasoning_approach>
1. Analyze the element description to understand the element type and purpose
2. Consider the action being performed to ensure locator compatibility
3. Choose the most stable locator strategy from the priority list
4. Ensure the locator is specific enough to avoid ambiguity
5. Verify the locator follows Robot Framework syntax

For the element "{step.element_description}":
- Element type: [Analyze what type of element this likely is]
- Action context: {step.step_description}
- Recommended strategy: [Choose from the priority order above]
</reasoning_approach>

Now generate the JSON response with the optimal locator for the described element."""

            try:
                if model_provider == "local":
                    response = ollama.chat(
                        model=model_name,
                        messages=[{'role': 'user', 'content': prompt}],
                        options={'temperature': 0.0},
                        format="json"
                    )
                    cleaned_response = response['message']['content']
                else: # Default to online
                    model = genai.GenerativeModel(model_name)
                    response = call_gemini_with_retry(model, prompt)
                    cleaned_response = response.text.strip().lstrip("```json").rstrip("```").strip()

                locator_data = json.loads(cleaned_response)

                # Handle both single object and list of objects
                if isinstance(locator_data, dict):
                    locator_data = [locator_data]

                final_locator = locator_data[0].get("locator") if isinstance(locator_data, list) and locator_data and isinstance(locator_data[0], dict) else None

                located_steps.append(LocatedStep(**step.model_dump(), locator=final_locator))
            except Exception as e:
                raw_response = ""
                if model_provider == "local" and 'response' in locals():
                    raw_response = response['message']['content']
                elif 'response' in locals():
                    raw_response = response.text
                logging.error(f"Element Identifier Agent failed for step '{step.step_description}': {e}\nRaw response was:\n{raw_response}")
                located_steps.append(LocatedStep(**step.model_dump(), locator=None))
        else:
            located_steps.append(LocatedStep(**step.model_dump()))
    return located_steps

def agent_code_assembler(steps: List[LocatedStep], query: str) -> str:
    """Agent 3 (Logic): Assembles the final Robot Framework code from the structured steps."""
    logging.info("Code Assembler: Building the final .robot file.")
    lines = [
        "*** Settings ***",
        "Library    SeleniumLibrary",
        "",
        "*** Test Cases ***",
        f"Test Case From Query: {query}"
    ]
    for step in steps:
        line = f"    {step.keyword}"
        if step.locator:
            line += f"    {step.locator}"
        if step.value:
            line += f"    {step.value}"
        lines.append(line)

        # If the browser is opened, automatically maximize and handle consent pop-up
        if step.keyword == "Open Browser":
            lines.append("    Maximize Browser Window")

    lines.append("    [Teardown]    Close Browser")
    return "\n".join(lines)


class ValidationResult(BaseModel):
    """The output of the Code-Validation Agent."""
    valid: bool = Field(description="True if the code is valid, False otherwise.")
    reason: str = Field(description="A brief explanation of why the code is valid or invalid.")

def agent_code_validator(code: str, model_provider: str, model_name: str) -> ValidationResult:
    """Agent 4 (AI): Validates the generated Robot Framework code for correctness."""
    logging.info(f"Code Validator: Validating code with {model_provider} model '{model_name}'.")
    prompt = f"""
    You are an expert Robot Framework linter and quality assurance engineer. Your sole task is to validate the following Robot Framework code.

    The code to validate is:
    ```robotframework
    {code}
    ```

    --- VALIDATION RULES ---
    1. Check for common syntax errors like incorrect indentation or malformed tables.
    2. Ensure keywords are called with the correct number of mandatory arguments.
    3. **CRITICAL RULE:** The `Open Browser` keyword MUST be called with the `browser=chrome` argument.
    4. **CRITICAL RULE:** The `Open Browser` keyword MUST include the headless and no-sandbox options, like `options=add_argument("--headless");add_argument("--no-sandbox")`.

    Respond with a single JSON object with two keys:
    1. "valid": A boolean (`true` or `false`).
    2. "reason": A brief, one-sentence explanation for your decision.

    --- EXAMPLES ---

    Example 1 (Valid Code):
    - Input Code:
        *** Test Cases ***
        My Test
            Open Browser    https://google.com    browser=chrome    options=add_argument("--headless");add_argument("--no-sandbox")
    - JSON Response: {{"valid": true, "reason": "The code appears to be syntactically valid and follows all rules."}}

    Example 2 (Invalid - Missing URL):
    - Input Code:
        *** Test Cases ***
        My Test
            Open Browser    browser=chrome    options=add_argument("--headless");add_argument("--no-sandbox")
    - JSON Response: {{"valid": false, "reason": "The keyword 'Open Browser' is missing its mandatory 'url' argument."}}

    Example 3 (Invalid - Missing Browser):
    - Input Code:
        *** Test Cases ***
        My Test
            Open Browser    https://google.com    options=add_argument("--headless");add_argument("--no-sandbox")
    - JSON Response: {{"valid": false, "reason": "The 'Open Browser' keyword is missing the required 'browser=chrome' argument."}}

    Example 4 (Invalid - Missing Options):
    - Input Code:
        *** Test Cases ***
        My Test
            Open Browser    https://google.com    browser=chrome
    - JSON Response: {{"valid": false, "reason": "The 'Open Browser' keyword is missing the required 'options' argument for headless/no-sandbox."}}

    Now, provide the validation result for the code provided above.
    """
    try:
        if model_provider == "local":
            response = ollama.chat(
                model=model_name,
                messages=[{'role': 'user', 'content': prompt}],
                options={'temperature': 0.0},
                format="json"
            )
            cleaned_response = response['message']['content']
        else: # Default to online
            model = genai.GenerativeModel(model_name)
            response = call_gemini_with_retry(model, prompt)
            cleaned_response = response.text.strip().lstrip("```json").rstrip("```").strip()

        validation_data = json.loads(cleaned_response)
        return ValidationResult(**validation_data)
    except Exception as e:
        raw_response = ""
        if model_provider == "local" and 'response' in locals():
            raw_response = response['message']['content']
        elif 'response' in locals():
            raw_response = response.text
        logging.error(f"Code Validator Agent failed: {e}\nRaw response was:\n{raw_response}")
        # If validation fails due to an exception, assume the code is invalid.
        return ValidationResult(valid=False, reason=f"Validator agent threw an exception: {e}")

# --- Orchestrator ---

def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str):
    """
    Orchestrates the multi-agent workflow to generate Robot Framework code,
    yielding progress updates and the final code.
    """
    logging.info("--- Starting Multi-Agent Workflow ---")
    yield {"status": "running", "message": "Starting agentic workflow..."}
    MAX_ATTEMPTS = 3
    MAX_STEP_RETRIES = 2

    # Configure online provider if used
    if model_provider == "online":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logging.error("Orchestrator: GEMINI_API_KEY not found for online provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return
        genai.configure(api_key=api_key)

    current_query = natural_language_query
    planned_steps = None
    located_steps = None
    robot_code = None

    for attempt in range(MAX_ATTEMPTS):
        logging.info(f"--- Attempt {attempt + 1} of {MAX_ATTEMPTS} ---")
        yield {"status": "running", "message": f"Starting attempt {attempt + 1}/{MAX_ATTEMPTS}..."}

        # Agent 1: Plan (with retries, only if not already successful)
        if not planned_steps:
            for step_retry in range(MAX_STEP_RETRIES):
                yield {"status": "running", "message": f"Agent 1/4: Planning test steps... (retry {step_retry + 1}/{MAX_STEP_RETRIES})"}
                planned_steps = agent_step_planner(current_query, model_provider, model_name)
                if planned_steps:
                    yield {"status": "running", "message": "Agent 1/4: Test step planning complete."}
                    break
                else:
                    logging.warning(f"Step Planner failed on retry {step_retry + 1}")
                    if step_retry < MAX_STEP_RETRIES - 1:
                        yield {"status": "running", "message": f"Step planning failed, retrying... ({step_retry + 2}/{MAX_STEP_RETRIES})"}
            
            if not planned_steps:
                logging.error("Orchestrator: Step Planner failed after all retries. Aborting.")
                yield {"status": "error", "message": "Failed to generate a test plan after multiple retries."}
                return

        # Agent 2: Identify Locators (with retries, only if not already successful)
        if not located_steps:
            for step_retry in range(MAX_STEP_RETRIES):
                yield {"status": "running", "message": f"Agent 2/4: Identifying UI element locators... (retry {step_retry + 1}/{MAX_STEP_RETRIES})"}
                located_steps = agent_element_identifier(planned_steps, model_provider, model_name)
                if located_steps:
                    yield {"status": "running", "message": "Agent 2/4: UI element locator identification complete."}
                    break
                else:
                    logging.warning(f"Element Identifier failed on retry {step_retry + 1}")
                    if step_retry < MAX_STEP_RETRIES - 1:
                        yield {"status": "running", "message": f"Element identification failed, retrying... ({step_retry + 2}/{MAX_STEP_RETRIES})"}
            
            if not located_steps:
                logging.error("Orchestrator: Element Identifier failed after all retries. Aborting.")
                yield {"status": "error", "message": "Failed to identify UI element locators after multiple retries."}
                return

        # Agent 3: Assemble Code (deterministic, doesn't need retries)
        yield {"status": "running", "message": "Agent 3/4: Assembling Robot Framework code..."}
        robot_code = agent_code_assembler(located_steps, natural_language_query)
        yield {"status": "running", "message": "Agent 3/4: Code assembly complete."}

        # Agent 4: Validate (this is what might fail and trigger a retry)
        yield {"status": "running", "message": "Agent 4/4: Validating generated code..."}
        validation = agent_code_validator(robot_code, model_provider, model_name)
        yield {"status": "running", "message": "Agent 4/4: Code validation complete."}

        if validation.valid:
            logging.info("Code validation successful. Workflow complete.")
            yield {"status": "complete", "robot_code": robot_code, "message": "Code generation successful."}
            return
        else:
            logging.warning(f"Code validation failed on attempt {attempt + 1}. Reason: {validation.reason}")
            yield {"status": "running", "message": f"Validation failed: {validation.reason}. Attempting self-correction..."}
            
            # Reset only the specific steps that might need correction based on validation failure
            if "syntax" in validation.reason.lower() or "keyword" in validation.reason.lower():
                # Syntax/keyword issues might be due to step planning or element identification
                logging.info("Validation error suggests step planning issue. Resetting step planner.")
                planned_steps = None
                located_steps = None
                current_query = f"""
                The previous attempt to generate a test plan failed validation due to syntax/keyword issues.
                The user's original query was: "{natural_language_query}"
                The validation error was: "{validation.reason}"

                Please analyze the error and generate a new, corrected plan with proper Robot Framework syntax.
                """
            elif "locator" in validation.reason.lower() or "element" in validation.reason.lower():
                # Locator issues are specific to element identification
                logging.info("Validation error suggests element identification issue. Resetting element identifier only.")
                located_steps = None
                current_query = natural_language_query  # Keep original query for step planner
            else:
                # General validation failure - reset everything
                logging.info("General validation failure. Resetting all steps.")
                planned_steps = None
                located_steps = None
                current_query = f"""
                The previous attempt to generate a test plan failed validation.
                The user's original query was: "{natural_language_query}"
                The generated code was:
                ```robotframework
                {robot_code}
                ```
                The validation error was: "{validation.reason}"

                Please analyze the error and the original query, then generate a new, corrected plan.
                """
            
            logging.info("Attempting self-correction with targeted approach...")

    logging.error("Orchestrator: Failed to generate valid code after multiple attempts.")
    yield {"status": "error", "message": "Failed to generate valid code after multiple attempts."}
