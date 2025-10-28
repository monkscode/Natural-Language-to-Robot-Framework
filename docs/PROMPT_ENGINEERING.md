# Prompt Engineering Documentation

## Overview

This document provides a comprehensive analysis of all prompts used in the Mark 1 Natural Language to Robot Framework system. The system uses sophisticated prompt engineering techniques across 4 specialized AI agents to convert natural language queries into executable Robot Framework test code.

**Last Updated:** October 29, 2025  
**System Version:** Mark 1  
**Architecture:** Multi-Agent CrewAI System

---

## Table of Contents

1. [Prompt Architecture Overview](#prompt-architecture-overview)
2. [Main Workflow Agents](#main-workflow-agents)
   - [Step Planner Agent](#step-planner-agent)
   - [Element Identifier Agent](#element-identifier-agent)
   - [Code Assembler Agent](#code-assembler-agent)
   - [Code Validator Agent](#code-validator-agent)
3. [Prompt Engineering Techniques](#prompt-engineering-techniques)
4. [Dynamic Context Injection](#dynamic-context-injection)
5. [Output Format Enforcement](#output-format-enforcement)
6. [Common Pitfalls and Solutions](#common-pitfalls-and-solutions)

---

## Prompt Architecture Overview

### System Design Philosophy

The prompts are designed with these core principles:

1. **Role-Based Specialization**: Each agent has a distinct role with specific expertise
2. **Context Chaining**: Output from one agent becomes input for the next
3. **Strict Output Formatting**: JSON-based outputs for reliable parsing
4. **Dynamic Context Injection**: Library-specific knowledge injected at runtime
5. **Error Prevention**: Explicit rules to prevent common LLM mistakes
6. **Batch Processing**: Optimized for efficiency (3-5x speedup via batch operations)

### Prompt Flow Diagram

```
User Query
    ↓
[Step Planner Agent] → Structured test steps (JSON)
    ↓
[Element Identifier Agent] → Steps with locators (JSON)
    ↓
[Code Assembler Agent] → Complete Robot Framework code (raw text)
    ↓
[Code Validator Agent] → Validation result (JSON)
    ↓
Final Test Code
```

---

## Main Workflow Agents

### Step Planner Agent

**File**: `src/backend/crew_ai/agents.py`  
**Task**: `src/backend/crew_ai/tasks.py` → `plan_steps_task()`

#### Role Definition

```python
role="Test Automation Planner"
goal="Break down a natural language query into a structured series of high-level test steps"
```

#### Key Prompt Components

**1. Critical Rules Section** (Anti-Hallucination)
```
⚠️ **MOST IMPORTANT RULE**: ONLY create steps for elements and actions EXPLICITLY mentioned in the user's query.

❌ DO NOT ADD:
- Popup dismissal steps (login popups, cookie consent, promotional popups)
- Cookie consent handling
- Newsletter dismissals
- Chat widget closures
- Any "smart" anticipatory steps
```

**Why This Matters**: LLMs tend to add "helpful" extra steps based on common web patterns. This rule prevents test bloat and ensures the agent stays focused on user intent.

**2. Search Optimization Rules**
```
**CRITICAL**: For ANY search operation (Google, Flipkart, Amazon, etc.), 
after "Input Text" step, use "Press Keys" with value "RETURN" instead of 
generating a separate "Click Element" step for search button.
```

**Reasoning**: Modern websites trigger search on Enter keypress. Using `Press Keys RETURN` is:
- Faster (no need to locate search button)
- More reliable (always works)
- Reduces element identification overhead

**3. Dynamic Library Context Injection**

```python
library_knowledge = ""
if self.library_context:
    library_knowledge = f"\n\n{self.library_context.planning_context}"
```

The agent receives library-specific keywords and best practices dynamically:

**For SeleniumLibrary**:
```robot
Open Browser    ${url}    chrome    options=${options}
Input Text      ${locator}    ${text}
Click Element   ${locator}
```

**For Browser Library** (Playwright):
```robot
New Browser     chromium    headless=True
New Context     viewport=None
New Page        ${url}
Type Text       ${locator}    ${text}
Click           ${locator}
```

#### Output Format

**Strict JSON Schema**:
```json
[
  {
    "step_description": "Human-readable description",
    "element_description": "Element to interact with",
    "value": "Value/URL/text to use",
    "keyword": "Robot Framework keyword name"
  }
]
```

**Example Output**:
```json
[
  {
    "step_description": "Open browser to Flipkart",
    "keyword": "Open Browser",
    "value": "https://www.flipkart.com",
    "browser": "chrome",
    "options": "add_argument('--headless')"
  },
  {
    "step_description": "Input 'shoes' in search box",
    "keyword": "Input Text",
    "element_description": "search box in header",
    "value": "shoes"
  },
  {
    "step_description": "Press Enter to search",
    "keyword": "Press Keys",
    "element_description": "search box",
    "value": "RETURN"
  },
  {
    "step_description": "Get first product name",
    "keyword": "Get Text",
    "element_description": "first product name in results"
  }
]
```

#### Advanced Features

**Conditional Logic Support**:
```json
{
  "keyword": "Input Text",
  "locator": "id=discount-code",
  "value": "SAVE10",
  "condition_type": "IF",
  "condition_value": "${total} > 100"
}
```

**Loop Support**:
```json
{
  "keyword": "Click Element",
  "loop_type": "FOR",
  "loop_source": "@{links}"
}
```

---

### Element Identifier Agent

**File**: `src/backend/crew_ai/agents.py`  
**Task**: `src/backend/crew_ai/tasks.py` → `identify_elements_task()`

#### Role Definition

```python
role="Advanced Web Element Locator Specialist with Batch Vision AI"
goal="Use batch_browser_automation tool to find ALL web element locators in ONE browser session"
```

#### Critical Innovation: Batch Processing

**Traditional Approach** (Slow):
```
Open browser → Find element 1 → Close browser
Open browser → Find element 2 → Close browser
Open browser → Find element 3 → Close browser
Total time: ~15-20 seconds
```

**Batch Approach** (3-5x Faster):
```
Open browser → Find elements 1, 2, 3, 4, 5 → Close browser
Total time: ~3-5 seconds
```

#### Prompt Structure

**STEP 1: Analysis Phase**
```
**STEP 1: ANALYZE THE PLAN**
- Read ALL test steps from context
- Identify which steps need element locators
- Note: 'Open Browser', 'Close Browser', 'Should Be True' steps DON'T need locators
- Note: 'Input Text', 'Click Element', 'Get Text' steps NEED locators
```

**STEP 2: URL Extraction**
```
**STEP 2: EXTRACT URL**
- Find the 'Open Browser' step in the plan
- Extract the URL from its 'value' field
- Example: {"keyword": "Open Browser", "value": "https://www.flipkart.com"}
  → URL is "https://www.flipkart.com"
```

**STEP 3: Element Collection**
```
**STEP 3: COLLECT ELEMENTS**
- For each step that needs a locator, extract:
  * Unique ID (e.g., "elem_1", "elem_2")
  * Element description (from 'element_description' field)
  * Action keyword (from 'keyword' field: input, click, get_text, etc.)
```

**STEP 4: Batch Tool Call**
```
Action: batch_browser_automation
Action Input: {
    "elements": [
        {"id": "elem_1", "description": "search box in header", "action": "input"},
        {"id": "elem_2", "description": "first product name", "action": "get_text"},
        {"id": "elem_3", "description": "first product price", "action": "get_text"}
    ],
    "url": "https://www.flipkart.com",
    "user_query": "Search for shoes and get first product name and price"
}
```

#### Critical Formatting Rules

**The Action Line Problem**: LLMs often add extra text on the `Action:` line, breaking CrewAI parsing.

❌ **WRONG Formats**:
```
Action: batch_browser_automation and Action Input using...  // Extra text!
Action: batch_browser_automation`  // Backtick at end!
First I need to... Action: batch_browser_automation  // Text before!
```

✅ **CORRECT Format**:
```
Action: batch_browser_automation
Action Input: {"elements": [...], "url": "...", "user_query": "..."}
```

**Enforcement in Prompt**:
```
⚠️ MOST IMPORTANT: You MUST output the tool call in EXACTLY this format:

Action: batch_browser_automation
Action Input: {"elements": [...], "url": "...", "user_query": "..."}

CRITICAL FORMATTING RULES:
1. The line 'Action: batch_browser_automation' must have NOTHING else on it
2. Do NOT add any text before, after, or on the same line as 'Action:'
3. Do NOT add backticks, quotes, or any other characters after 'batch_browser_automation'
4. The next line must be 'Action Input:' followed by a JSON dictionary
5. Action Input must be a DICTIONARY { } NOT an array [ ]
```

#### Why User Query Context Matters

The `user_query` parameter helps BrowserUse AI understand:
1. **Overall Intent**: What is the user trying to accomplish?
2. **Popup Handling**: Dismisses login/cookie popups intelligently
3. **Multi-Step Workflows**: Maintains context across navigation

**Example**:
```json
{
  "user_query": "Search for shoes on Flipkart and get first product price"
}
```

BrowserUse understands:
- This is a search workflow → dismiss login popup (not needed for search)
- Result extraction follows search → wait for results to load
- Focus on first product → ignore promotional banners

---

### Code Assembler Agent

**File**: `src/backend/crew_ai/agents.py`  
**Task**: `src/backend/crew_ai/tasks.py` → `assemble_code_task()`

#### Role Definition

```python
role="Robot Framework Code Assembler"
goal="Assemble the final Robot Framework code from structured steps"
```

#### Critical Component: Variable Declaration

**The Problem**: Robot Framework requires ALL variables to be declared before use, but LLMs often forget this.

**The Solution**: Explicit extraction instructions in prompt.

**Variable Extraction Rules**:
```
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
```

#### Dynamic Library Context

**For SeleniumLibrary**:
```robot
*** Settings ***
Library    SeleniumLibrary
Library    BuiltIn

*** Variables ***
${browser}    chrome
${options}    add_argument("--headless")

*** Test Cases ***
Generated Test
    Open Browser    https://example.com    ${browser}    options=${options}
    # Test steps
    Close Browser
```

**For Browser Library** (Playwright):
```robot
*** Settings ***
Library    Browser

*** Variables ***
${browser}    chromium
${headless}    True

*** Test Cases ***
Generated Test
    New Browser    ${browser}    headless=${headless}
    New Context    viewport=None
    New Page    https://example.com
    # Test steps
```

#### Viewport Configuration (Browser Library Critical Fix)

**The Problem**: Browser Library uses a small default viewport (800x600), causing elements outside the viewport to be undetectable.

**The Solution**: Always add `New Context viewport=None` after `New Browser`.

**Prompt Injection**:
```python
def _get_viewport_instructions(self) -> str:
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
```

#### Handling Missing Locators

**Scenario**: What if BrowserUse couldn't find a locator?

**Strategy**: Generate syntactically valid code with placeholder + warning.

```python
"""
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
```

This ensures:
1. Code is syntactically valid (passes validation)
2. Clear warning for manual intervention
3. Easy to fix (replace placeholder with correct locator)

#### Output Format

**CRITICAL**: NO markdown fences, NO explanatory text, ONLY raw Robot Framework code.

❌ **WRONG**:
```
Here is the generated Robot Framework code:

```robot
*** Settings ***
Library    SeleniumLibrary
```

✅ **CORRECT**:
```
*** Settings ***
Library    SeleniumLibrary

*** Variables ***
${browser}    chrome
```

---

### Code Validator Agent

**File**: `src/backend/crew_ai/agents.py`  
**Task**: `src/backend/crew_ai/tasks.py` → `validate_code_task()`

#### Role Definition

```python
role="Robot Framework Linter and Quality Assurance Engineer"
goal="Validate the generated Robot Framework code for correctness and adherence to rules"
```

#### Validation Checklist

**Dynamic from Library Context**:
```python
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
    """
```

#### Common Errors to Check

```
--- COMMON ERRORS TO CHECK ---
1. Get Text without locator argument
2. Invalid expressions in Should Be True
3. Missing variable assignments (${var}=)
4. Incorrect conditional syntax
5. Undeclared variables used in test
6. Missing library imports
7. Invalid keyword arguments
```

#### Output Format

**Strict JSON Schema**:
```json
{
  "valid": boolean,
  "reason": "Explanation of validation result"
}
```

**Example Outputs**:

✅ **Valid Code**:
```json
{
  "valid": true,
  "reason": "The code is valid."
}
```

❌ **Invalid Code**:
```json
{
  "valid": false,
  "reason": "Missing variable declaration for ${product_price}. Variables must be declared in *** Variables *** section before use."
}
```

---

## Prompt Engineering Techniques

### 1. Role-Based Specialization

---

## Prompt Engineering Techniques

### 1. Role-Based Specialization

**Pattern**:
```python
Agent(
    role="Specific Expert Role",
    goal="Single, focused objective",
    backstory="Detailed expertise and experience"
)
```

**Why It Works**: LLMs perform better when given a specific persona and expertise domain. The backstory provides context that influences decision-making.

**Example**:
```python
role="Test Automation Planner"
backstory="You are an expert test automation planner with a strict focus on user requirements..."
```

### 2. Explicit Rule Enforcement

**Pattern**:
```
--- CRITICAL RULES ---
1. Rule statement
2. Rule statement
3. Rule statement

❌ WRONG: Anti-pattern example
✅ CORRECT: Correct pattern example
```

**Why It Works**: LLMs respond well to explicit "do this, don't do that" instructions with visual markers.

**Example**:
```
❌ WRONG FORMATS (DO NOT DO THIS):
Action: batch_browser_automation` and Action Input using...  // Extra text!

✅ CORRECT FORMAT:
Action: batch_browser_automation
Action Input: {"elements": [...]}
```

### 3. Step-by-Step Workflows

**Pattern**:
```
**STEP 1: ACTION NAME**
- Sub-action 1
- Sub-action 2

**STEP 2: NEXT ACTION**
- Sub-action 1
- Sub-action 2
```

**Why It Works**: Breaking complex tasks into numbered steps improves LLM task completion and reduces hallucination.

**Example** (Element Identifier Agent):
```
**STEP 1: ANALYZE THE PLAN**
- Read ALL test steps from context
- Identify which steps need element locators

**STEP 2: EXTRACT URL**
- Find the 'Open Browser' step in the plan
- Extract the URL from its 'value' field
```

### 4. Concrete Examples

**Pattern**:
```
**Example:**
*Input:* <example input>
*Output:* <example output>
```

**Why It Works**: Few-shot learning. LLMs learn output format and style from examples.

**Example**:
```
**Example for price validation:**
*Input Step:*
`{"keyword": "Should Be True", "condition_expression": "${float(product_price.replace('₹', '').replace(',', '')) < 9999}"}`

*Output Code:*
`    ${price_numeric}=    Evaluate    float('${product_price}'.replace('₹', '').replace(',', ''))`
`    Should Be True    ${price_numeric} < 9999`
```

### 5. Output Format Enforcement

**Pattern**:
```
--- OUTPUT FORMAT ---
You MUST respond with ONLY a valid JSON object containing:
{
  "key1": "type (description)",
  "key2": "type (description)"
}
```

**Why It Works**: Explicit schema + "MUST" + "ONLY" reduces format violations.

**Example**:
```
--- OUTPUT FORMAT ---
You MUST respond with ONLY a valid JSON object containing:
{
  "valid": boolean,
  "reason": "Explanation of validation result"
}
```

### 6. Context Chaining

**Pattern**:
```python
Task(
    description="Process the output from the previous task. The context will be: {previous_task_output}"
)
```

**Why It Works**: Each agent builds on the previous agent's work, creating a reliable pipeline.

**Example Flow**:
```
Task 1: "Break down query into steps" → JSON array
Task 2: "Add locators to steps from context (context = Task 1 output)" → JSON array with locators
Task 3: "Generate code from steps in context (context = Task 2 output)" → Robot Framework code
```

### 7. Dynamic Context Injection

**Pattern**:
```python
library_knowledge = ""
if self.library_context:
    library_knowledge = f"\n\n{self.library_context.planning_context}"

prompt = f"Base prompt{library_knowledge}"
```

**Why It Works**: Allows same prompt to adapt to different libraries (SeleniumLibrary vs Browser Library) without code duplication.

**Example**:
```python
# SeleniumLibrary context injected
"Use 'Open Browser' keyword with chrome browser and options parameter"

# Browser Library context injected
"Use 'New Browser' keyword with chromium and headless parameter"
```

### 8. Error Prevention Through Repetition

**Pattern**: Repeat critical rules multiple times in different sections.

**Why It Works**: Reinforcement increases compliance, especially for common LLM mistakes.

**Example** (Action line formatting repeated 3 times):
1. In backstory: "The Action line must contain ONLY 'Action: batch_browser_automation'"
2. In task description: "CRITICAL: Do NOT add extra text on Action line"
3. In output format: "⚠️ MOST IMPORTANT: Action line = ONLY 'Action: batch_browser_automation'"

### 9. Visual Markers and Emojis

**Pattern**:
```
⚠️ **CRITICAL REQUIREMENT**
✅ CORRECT
❌ WRONG
🎯 KEY INSIGHT
```

**Why It Works**: Visual markers help LLMs identify importance and structure.

**Example**:
```
⚠️ **MOST IMPORTANT RULE**: ONLY create steps for elements explicitly mentioned

✅ CORRECT steps:
1. Open Browser → Flipkart
2. Input Text → search box → "shoes"

❌ WRONG (DO NOT DO THIS):
1. Open Browser → Flipkart
2. Click Element → login popup close button  ← USER NEVER MENTIONED THIS!
```

### 10. Research-Backed Techniques

**Pattern**: Include academic research findings and industry best practices.

**Why It Works**: Grounds the agent's decisions in proven methodologies.

**Example** (Locator Generation Agent):
```
**Property Stability Weights (from research):**
- Highly stable (2.70-2.95): id, name, aria_label, visible_text, is_button
- Moderately stable (1.30-2.20): attributes, location, alt, area
- Less stable (0.50-1.00): class_name, xpath, neighbor_texts

**BrowserStack Locator Hierarchy:**
Priority 1: ID locator (95% stability)
Priority 2: Name locator (90% stability)
Priority 3: Aria-label CSS (92% stability)
```

---

## Dynamic Context Injection

### Library Context System

**File**: `src/backend/crew_ai/library_context/base.py`

The system supports multiple Robot Framework libraries through a factory pattern:

```python
class LibraryContext:
    @property
    def planning_context(self) -> str:
        """Keywords and best practices for planning phase"""
    
    @property
    def code_assembly_context(self) -> str:
        """Code structure template for assembly phase"""
    
    @property
    def validation_context(self) -> str:
        """Validation rules for validation phase"""
```

### Supported Libraries

**1. SeleniumLibrary** (Selenium WebDriver)
```python
library_name = "SeleniumLibrary"
browser_init_params = {
    "browser": "chrome",
    "options": "add_argument('--headless')"
}
requires_viewport_config = False
```

**2. Browser Library** (Playwright)
```python
library_name = "Browser"
browser_init_params = {
    "browser": "chromium",
    "headless": "True"
}
requires_viewport_config = True  # Critical!
```

### Context Injection Points

**1. Agent Initialization** (`agents.py`):
```python
class RobotAgents:
    def __init__(self, model_provider, model_name, library_context=None):
        self.library_context = library_context
    
    def step_planner_agent(self):
        library_knowledge = ""
        if self.library_context:
            library_knowledge = f"\n\n{self.library_context.planning_context}"
        
        return Agent(
            backstory=f"Base backstory{library_knowledge}"
        )
```

**2. Task Creation** (`tasks.py`):
```python
def _get_keyword_guidelines(self) -> str:
    if self.library_context:
        return self.library_context.planning_context
    else:
        return "Fallback guidelines..."
```

### Dynamic Keyword Mapping

**SeleniumLibrary Keywords**:
```
Open Browser → Opens new browser session
Input Text → Types text into element
Click Element → Clicks element
Get Text → Retrieves element text
Close Browser → Closes browser
```

**Browser Library Keywords**:
```
New Browser → Opens new browser instance
Type Text → Types text into element
Click → Clicks element
Get Text → Retrieves element text
(Auto-closes on test end)
```

### Critical: Viewport Configuration

**Browser Library Only**: Must inject viewport configuration instructions.

**Why**: Browser Library defaults to 800x600 viewport. Elements outside this viewport are undetectable, causing 90% of test failures.

**Injection**:
```python
def _get_viewport_instructions(self) -> str:
    if self.library_context and self.library_context.requires_viewport_config:
        return """
**MANDATORY**: After "New Browser" and before "New Page", you MUST add:
New Context    viewport=None

**Why**: Browser Library uses 800x600 default viewport. viewport=None uses full window.
        """
```

**Result**:
```robot
*** Test Cases ***
Test
    New Browser    chromium    headless=True
    New Context    viewport=None    ← CRITICAL LINE
    New Page    https://example.com
```

---

## Output Format Enforcement

### Why Strict Formatting Matters

**Problem**: LLMs are creative and often add:
- Markdown fences (```json, ```robot)
- Explanatory text ("Here is the code:", "Let me explain...")
- Extra whitespace or formatting

**Impact**: Parsing breaks, workflow fails.

**Solution**: Multi-layered format enforcement.

### Layer 1: Explicit Instructions

```
--- OUTPUT FORMAT ---
1. You MUST respond with ONLY a valid JSON object.
2. Do NOT include any introductory text, natural language explanations, or markdown formatting like ```json.
3. The JSON object must have exactly two keys: 'valid' (a boolean) and 'reason' (a string).
```

### Layer 2: Expected Output Declaration

```python
Task(
    expected_output="A single, raw JSON object with two keys: 'valid' (boolean) and 'reason' (string)."
)
```

### Layer 3: Example Format

```
**Example Output:**
{
  "valid": true,
  "reason": "The code is valid."
}

**NOT THIS:**
```json
{
  "valid": true
}
```
```

### Layer 4: LLM Output Cleaning (Fallback)

**File**: `src/backend/crew_ai/llm_output_cleaner.py`

Automatic cleaning layer that fixes common formatting issues:

```python
def clean_action_line(text: str) -> str:
    """Fix common Action line formatting issues"""
    # Pattern 1: Extra text after tool name
    text = re.sub(
        r'(Action:\s+\w+)(.+?)(\n)',
        r'\1\3',
        text
    )
    
    # Pattern 2: Backticks or quotes
    text = re.sub(
        r'(Action:\s+\w+)[`"\']',
        r'\1',
        text
    )
    
    return text
```

**Common Fixes**:
```
Input:  "Action: batch_browser_automation` and then..."
Output: "Action: batch_browser_automation\n"

Input:  "First I'll use Action: tool_name"
Output: "Action: tool_name\n"
```

### Layer 5: Validation and Retry

```python
try:
    result = json.loads(agent_output)
except json.JSONDecodeError:
    # Strip markdown fences
    cleaned = agent_output.strip('```json').strip('```').strip()
    result = json.loads(cleaned)
```

---

## Common Pitfalls and Solutions

### Pitfall 1: LLM Adds "Helpful" Extra Steps

**Problem**:
```json
// User: "Search for shoes on Flipkart"
// LLM generates:
[
  {"keyword": "Open Browser", "value": "https://flipkart.com"},
  {"keyword": "Click Element", "element": "login popup close"},  // NOT REQUESTED!
  {"keyword": "Click Element", "element": "cookie consent accept"},  // NOT REQUESTED!
  {"keyword": "Input Text", "element": "search box", "value": "shoes"}
]
```

**Solution**: Explicit anti-hallucination rules repeated multiple times.

```
⚠️ **MOST IMPORTANT RULE**: ONLY create steps for elements and actions EXPLICITLY mentioned in the user's query.

❌ DO NOT ADD:
- Popup dismissal steps (login popups, cookie consent, promotional popups)
- Cookie consent handling
- Newsletter dismissals
- Any "smart" anticipatory steps
```

**Result**:
```json
[
  {"keyword": "Open Browser", "value": "https://flipkart.com"},
  {"keyword": "Input Text", "element": "search box", "value": "shoes"}
]
```

### Pitfall 2: Action Line Formatting Breaks CrewAI

**Problem**: LLMs add extra text on the Action line.

```
// LLM Output:
Action: batch_browser_automation and then I'll process the results...
Action Input: {...}

// CrewAI Parser:
ERROR: Tool name is "batch_browser_automation and then I'll process the results..."
FAIL: No tool with that name
```

**Solution**: Repeated, explicit formatting rules + visual examples.

```
⚠️ MOST IMPORTANT: The line 'Action: batch_browser_automation' must have NOTHING else on it

❌ WRONG:
Action: batch_browser_automation and Action Input using...
Action: batch_browser_automation`
Action: batch_browser_automation. Now I'll...

✅ CORRECT:
Action: batch_browser_automation
Action Input: {...}
```

**Enforcement**: Repeat this rule 3-4 times in different sections (backstory, task description, output format).

### Pitfall 3: Forgetting Variable Declarations

**Problem**: LLM generates code that uses variables before declaring them.

```robot
*** Test Cases ***
Test
    Open Browser    https://example.com    ${browser}  # ${browser} UNDEFINED!
```

**Solution**: Explicit variable extraction instructions with examples.

```
--- CRITICAL: VARIABLE DECLARATION RULES ---
1. **ALWAYS include *** Variables *** section** (even if empty)
2. **Declare ALL variables before use:**
   - If Open Browser step has 'browser' key → ${browser}    <value from step>

**Example Variable Extraction:**
If you receive:
{
  "keyword": "Open Browser",
  "value": "https://www.flipkart.com",
  "browser": "chrome"
}

You MUST declare:
*** Variables ***
${browser}    chrome
```

### Pitfall 4: Browser Library Viewport Issue

**Problem**: Browser Library uses 800x600 viewport by default. Elements outside this viewport are invisible, causing locator failures.

```robot
*** Test Cases ***
Test
    New Browser    chromium    headless=True
    New Page    https://example.com
    Click    id=footer-link  # FAILS! Footer is below 600px viewport
```

**Solution**: Dynamic context injection for Browser Library.

```python
if self.library_context.requires_viewport_config:
    inject_instructions("""
**MANDATORY**: After "New Browser" and before "New Page":
New Context    viewport=None
    """)
```

**Result**:
```robot
*** Test Cases ***
Test
    New Browser    chromium    headless=True
    New Context    viewport=None    ← FIXES THE ISSUE
    New Page    https://example.com
    Click    id=footer-link  # SUCCESS!
```

### Pitfall 5: JSON Output with Markdown Fences

**Problem**: LLMs often wrap JSON in markdown code fences.

```
Here is the validation result:

```json
{
  "valid": true,
  "reason": "Code is correct"
}
```
```

**Solution**: Multi-layer enforcement + automatic cleaning.

**Prompt**:
```
--- OUTPUT FORMAT ---
You MUST respond with ONLY a valid JSON object.
Do NOT include any introductory text or markdown formatting like ```json.

**Example:**
{
  "valid": true
}

**NOT THIS:**
```json
{
  "valid": true
}
```
```

**Fallback Cleaner**:
```python
def clean_output(text: str) -> str:
    # Remove markdown fences
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()
```

### Pitfall 6: Single Element Calls Instead of Batch

**Problem**: LLM calls vision tool multiple times instead of using batch mode.

```
Action: vision_browser_automation
Action Input: {"element": "search box", "url": "..."}

Action: vision_browser_automation
Action Input: {"element": "product name", "url": "..."}

# Result: Browser opens/closes 5 times → 15-20 seconds
```

**Solution**: Explicit batch workflow + forbid single-element tool.

```
⚠️ **CRITICAL REQUIREMENT - BATCH PROCESSING MODE**

You have ONE PRIMARY TOOL: batch_browser_automation
You MUST collect ALL elements from the test steps and process them in ONE batch call.

**FORBIDDEN ACTIONS:**
❌ NEVER call vision_browser_automation (use batch mode instead)
❌ NEVER make multiple batch calls (collect all elements, call once)

**YOUR WORKFLOW:**
1. **Collect Elements:** Build a list of ALL elements that need locators
2. **Call Batch Tool ONCE:** Use batch_browser_automation with ALL elements
```

**Result**:
```
Action: batch_browser_automation
Action Input: {
  "elements": [
    {"id": "elem_1", "description": "search box"},
    {"id": "elem_2", "description": "product name"},
    {"id": "elem_3", "description": "product price"}
  ],
  "url": "https://flipkart.com"
}

# Result: Browser opens ONCE → 3-5 seconds (3-5x speedup!)
```

### Pitfall 7: Missing Locator Handling

**Problem**: BrowserUse couldn't find a locator. What should Code Assembler do?

**Wrong Approach**: Generate invalid code or skip the step.

```robot
*** Test Cases ***
Test
    ${product_name}=    Get Text    # SYNTAX ERROR: Missing locator argument!
```

**Correct Approach**: Generate valid code with placeholder + warning.

```robot
*** Variables ***
${product_locator}    xpath=//PLACEHOLDER_FOR_elem_2

*** Test Cases ***
Test
    # WARNING: Locator not found for 'first product name'
    # Manual intervention required: Inspect page and update locator
    ${product_name}=    Get Text    ${product_locator}
```

**Benefit**: Code is syntactically valid (passes validation), clear path for manual fix.

---

## Best Practices Summary

### 1. Prompt Design

✅ **DO**:
- Use explicit role definitions
- Provide step-by-step workflows
- Include concrete examples
- Repeat critical rules 2-3 times
- Use visual markers (⚠️ ✅ ❌)
- Inject dynamic context (library-specific knowledge)

❌ **DON'T**:
- Assume LLM understands implicit rules
- Use vague instructions
- Rely on single-mention rules
- Mix multiple concerns in one prompt

### 2. Output Format

✅ **DO**:
- Define strict JSON schemas
- Use `expected_output` parameter
- Provide example outputs
- Implement fallback cleaning layers

❌ **DON'T**:
- Allow free-form text outputs
- Mix JSON and natural language
- Rely on LLM to infer format

### 3. Error Prevention

✅ **DO**:
- Anticipate common LLM mistakes
- Add explicit "DON'T DO THIS" sections
- Provide anti-pattern examples
- Implement automatic error recovery

❌ **DON'T**:
- Assume LLMs will follow implicit conventions
- Leave edge cases unhandled
- Skip validation steps

### 4. Context Management

✅ **DO**:
- Chain tasks with clear context passing
- Inject dynamic library-specific knowledge
- Preserve context across agent transitions
- Validate context at each step

❌ **DON'T**:
- Assume agents share global context
- Hardcode library-specific logic
- Skip context validation

### 5. Tool Integration

✅ **DO**:
- Prefer batch processing over individual calls
- Provide clear tool usage examples
- Enforce tool call formatting strictly
- Handle tool failures gracefully

❌ **DON'T**:
- Allow inefficient tool usage patterns
- Assume LLMs format tool calls correctly
- Ignore tool error scenarios

---

## Maintenance and Evolution

### When to Update Prompts

**Trigger 1: New LLM Model**
- Test all prompts with new model
- Update formatting rules if needed
- Adjust example complexity

**Trigger 2: New Robot Framework Library**
- Create new LibraryContext subclass
- Define library-specific contexts
- Test with representative queries

**Trigger 3: Recurring Failures**
- Identify failure pattern
- Add explicit rule to prevent pattern
- Provide corrective example

**Trigger 4: New Feature**
- Add feature-specific instructions
- Update relevant task descriptions
- Test interaction with existing features

### Prompt Testing Checklist

```
□ Test with typical user queries
□ Test with edge cases (complex queries)
□ Validate JSON output format
□ Check tool call formatting
□ Verify variable declarations
□ Test library context injection
□ Validate error handling
□ Measure performance (time)
□ Check locator quality
□ Verify code correctness
```

### Version Control

```
# docs/PROMPT_ENGINEERING.md
- Version: 1.0.0
- Last Updated: October 29, 2025
- Change Log:
  * 1.0.0: Initial documentation
  * Future: Track prompt changes with version numbers
```

---

## Conclusion

The Mark 1 system's prompt engineering is a sophisticated multi-layered approach that:

1. **Specializes** agents for specific tasks
2. **Chains** context across sequential operations
3. **Enforces** strict output formatting
4. **Injects** dynamic library-specific knowledge
5. **Prevents** common LLM mistakes through repetition
6. **Optimizes** performance via batch processing

**Key Insight**: The prompts are not just instructions—they are a carefully designed system that guides LLMs through complex workflows while preventing common failure modes and ensuring reliable, production-ready outputs.

**Maintenance Philosophy**: Prompts evolve based on real-world usage. Monitor failures, identify patterns, and update prompts iteratively to improve system reliability and capability.

---

## Appendix: Prompt Locations

### Main Workflow
- **Step Planner**: `src/backend/crew_ai/agents.py::step_planner_agent()` + `tasks.py::plan_steps_task()`
- **Element Identifier**: `src/backend/crew_ai/agents.py::element_identifier_agent()` + `tasks.py::identify_elements_task()`
- **Code Assembler**: `src/backend/crew_ai/agents.py::code_assembler_agent()` + `tasks.py::assemble_code_task()`
- **Code Validator**: `src/backend/crew_ai/agents.py::code_validator_agent()` + `tasks.py::validate_code_task()`

### Supporting Components
- **LLM Wrapper**: `src/backend/crew_ai/cleaned_llm_wrapper.py`
- **Output Cleaner**: `src/backend/crew_ai/llm_output_cleaner.py`
- **Library Contexts**: `src/backend/crew_ai/library_context/`

---

**Document Version**: 1.0.0  
**Maintained By**: Natural Language to Robot Framework Team  
**Last Review**: October 29, 2025
