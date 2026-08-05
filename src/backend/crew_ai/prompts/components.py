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
    - ASSEMBLY: Specific to assemble_code_task
    (IDENTIFICATION components were removed in Task 16 — element
    identification is deterministic Python now, see element_identification.py)
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

    # ═══════════════════════════════════════════════════════════════════════════
    # ASSEMBLY COMPONENTS - Used in assemble_code_task
    # ═══════════════════════════════════════════════════════════════════════════

    VARIABLE_DECLARATION_RULES = """
--- CRITICAL: VARIABLE DECLARATION RULES ---
1. **ALWAYS include *** Variables *** section** (even if empty)
2. **Declare ALL variables before use:**
   - Browser init step → ${browser}    <value from step>, ${headless}    <value from step>
   - For each element with locator → ${elem_X_locator}    <locator value>
   - For Get Text results → ${variable_name}    (no initial value needed)

3. **Variable Naming Convention:**
   - Browser config: ${browser}, ${headless}
   - Element locators: ${search_box_locator}, ${product_name_locator}
   - Retrieved values: ${product_name}, ${product_price}, ${result}

**Example Variable Extraction:**
If you receive:
```json
{
  "keyword": "New Browser",
  "value": "https://www.flipkart.com",
  "browser": "chromium",
  "headless": "True"
}
```
You MUST declare:
```robot
*** Variables ***
${browser}    chromium
${headless}    True
```
"""

    LOCATOR_RULES = """
--- CRITICAL: LOCATOR RULES (USE PROVIDED LOCATORS EXACTLY) ---
⚠️ **MOST IMPORTANT RULE FOR LOCATORS** ⚠️

The locators provided have been found by AI vision on the actual webpage,
validated to work, and scored for stability. Your job is code assembly,
not locator optimization.

**For each step that needs a locator:**
1. Check if 'locator' key exists and 'found' is true
2. If found: declare the locator as a variable and copy the EXACT value
   from the 'locator' field
   - DO NOT modify, improve, or convert the locator
   - DO NOT change id=X to xpath=//*[@id='X'] or any other format
   - If you think a locator is wrong, USE IT ANYWAY and add a comment
3. If NOT found (found=false or error present):
   a. Add comment: # WARNING: Locator not found for <element_description>
   b. Use placeholder: xpath=//PLACEHOLDER_FOR_<element_id>
   c. Still generate syntactically valid code
   d. The action step itself stays a LIVE keyword call using the placeholder
      variable — NEVER comment out the step. Only the WARNING lines are
      comments; a commented-out step erases the repair surface

**Example for found locator:**
```robot
*** Variables ***
${search_box_locator}    id=search-input  # ← EXACT value from 'locator' field

*** Test Cases ***
Test
    Fill Text    ${search_box_locator}    shoes
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

❌ WRONG (DO NOT DO THIS):
Input: {"locator": "id=submit-btn"}
Output: ${submit_locator}    xpath=//*[@id='submit-btn']  ← WRONG! Copy it EXACTLY.
"""

    STABILITY_WARNING_RULES = """
--- LOCATOR STABILITY WARNINGS ---
Each step may carry a 'stability' field ('stable', 'volatile', or 'positional')
— the locator engine's verdict on whether the locator survives a fresh session.

**Rule:** When a step's 'stability' is present and NOT 'stable', add a comment
line directly ABOVE that step:
`# WARNING: locator for '<element_description>' is <stability> — may break in a fresh session`

**CRITICAL:** Still use the provided locator exactly as provided. The warning
is disclosure for the human reviewer, NOT permission to modify, substitute,
or skip the locator. Steps with stability 'stable' (or no stability field)
get NO warning comment.

**Example:**
*Input Step:*
`{"keyword": "Click", "element_description": "submit button", "locator": "xpath=//div[4]/center/input[1]", "stability": "positional"}`
*Output Code:*
```robot
    # WARNING: locator for 'submit button' is positional — may break in a fresh session
    Click    ${submit_button_locator}
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
`{"keyword": "Fill Text", "locator": "id=discount-code", "value": "SAVE10", "condition_type": "IF", "condition_value": "${total} > 100"}`
*Output Code:*
`    Run Keyword If    ${total} > 100    Fill Text    id=discount-code    SAVE10`
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
`{"keyword": "Click", "loop_type": "FOR", "loop_source": "@{links}"}`
*Output Code:*
`    FOR    ${link}    IN    @{links}`
`        Click    ${link}`
`    END`

**⚠️ CRITICAL: `element_type: "collection"` — the locator matches MANY**

A step tagged `"element_type": "collection"` has a locator that resolves to
several elements (e.g. `ol > li` matching 20 books). Browser Library resolves
STRICTLY: pointing a single-element keyword at it raises
`strict mode violation: locator resolved to N elements` at run time. `--dryrun`
does NOT catch this — it never evaluates the locator.

Resolve by CARDINALITY, not by the keyword the planner happened to write:

| Planner asked for | On a collection step, generate |
|---|---|
| `Get Text` (read them all) | `Get Elements` + FOR loop, `Get Text ${element}` inside |
| `Get Attribute` (read them all) | `Get Elements` + FOR loop, `Get Attribute ${element}` inside |
| `Click` (act on ONE of many) | narrow the locator to the intended row first, THEN `Click` |
| `Get Element Count` | use it AS-IS — it takes a many-match selector by design |

Use the `Get Elements` + FOR pattern shown above for the read cases. `Get
Elements` is the ONLY bulk-read keyword Browser Library has — never invent a
plural one to avoid the loop.

**IF you reach into a collection element, chain with `>>`, never `>>>`.**
Usually you do not need to: the loop variable itself is what you read. When the
value genuinely sits on a child, `${element} >> h3 > a` chains a child selector
onto an element reference — that is the correct operator.

`>>>` is Browser Library's IFRAME ENTRY operator. It stays correct in front of
an iframe SELECTOR (see the dropdown/iframe rules if they appear above), and is
wrong after `${element}` for a reason no loop variable can satisfy: Browser's
own documentation states that **frame piercing is not possible with an element
reference**. `${element}` is a reference handed back by `Get Elements`, not a
selector, so `>>>` after it can never enter a frame no matter what the element
is — it just sends Browser hunting for one and fails with
`resolved to <li>, <iframe>`.

**Key Rules:**
1. Use `@{variable}` (list notation) for Get Elements return value
2. Exit on empty/whitespace: `Exit For Loop If    len($text.strip()) == 0`
3. Use `Should Contain` for text validation (NOT `Should Be True 'X' in 'Y'`)
4. Use `${element}` as the loop variable inside FOR
5. On `element_type: "collection"`, pick the keyword by cardinality (table above)
6. Always close with `END`
"""

    DROPDOWN_HANDLING = """
--- HANDLING DROPDOWNS BASED ON element_type AND dropdown_framework ---
⚠️ **CRITICAL**: Check `dropdown_framework` FIRST. If present (non-empty), it
overrides `element_type` and selects a framework-specific interaction template.
Otherwise, fall back to the `element_type` rules below.

**FRAMEWORK ROUTING (when dropdown_framework is set):**

| dropdown_framework | Template |
|---|---|
| `tom-select` | TYPE 4 (Tom Select — see below) |
| `native`, `combobox-input`, `select2`, `kendo`, `react-select`, `vue-select`, `ant-design`, `material-ui`, `""` (empty) | Fall back to TYPE 1/2/3 by `element_type` |

Only `tom-select` has a specialized template today. Other frameworks reach the
Code Assembler with `dropdown_framework` set, but the assembler still routes
them by `element_type` until a dedicated template is added.

Dropdowns come in 4 templates, each requiring different Robot Framework keywords:

**TYPE 1: Native HTML Select (element_type='select')**
Use standard Select Options By keyword:
```robot
Select Options By    ${dropdown_locator}    label    Option Text
```

**⚠️ VERIFYING A SELECTION — `Get Selected Options` RETURN SHAPE**

`Get Selected Options    ${locator}    <option_attribute>` returns the values of
that ONE attribute (default `label`) — **a flat list of strings**, NOT a list of
objects. `${selected}[0]` is already the text, e.g. `"Option 2"`. There is no
`[label]` to index into it and no dictionary to read from it. `--dryrun` does NOT
catch this — it never evaluates variables.

```robot
# CORRECT — index the list, or let the keyword assert for you
${selected}=    Get Selected Options    ${dropdown_locator}
Should Be Equal    ${selected}[0]    Option 2
Get Selected Options    ${dropdown_locator}    label    ==    Option 2

# WRONG — every one of these treats a string as an object
${x}=    Set Variable    ${selected}[0][label]
${x}=    Get From Dictionary    ${selected}[0]    label
${x}=    Get From Dictionary    ${selected}[0]    text
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

**TYPE 4: Tom Select (dropdown_framework='tom-select')**
TomSelect wraps a native `<select>` with a custom UI. Interact via JavaScript:
find the option by display text at runtime and call TomSelect's `setValue()` API.
This is viewport-agnostic and works on any website regardless of internal option
values — no clicking, no waiting for dropdowns to open.

**Preferred — when `select_id` is set:**
`id=${select_id}` targets the hidden `<select>` directly.
```robot
Evaluate JavaScript    id=${select_id}    (el) => { const opt = Array.from(el.options).find(o => o.text.trim() === '${value}'); if (opt && el.tomselect) el.tomselect.setValue(opt.value); }
```

**Fallback — when `select_id` is null (locator targets the `.ts-control` div):**
Find the hidden `<select>` in the same container via `select.tomselected` (TomSelect always marks it) — position-independent.
```robot
Evaluate JavaScript    ${locator}    (el) => { const sel = el.closest('.ts-wrapper').parentElement.querySelector('select.tomselected'); if (sel && sel.tomselect) { const opt = Array.from(sel.options).find(o => o.text.trim() === '${value}'); if (opt) sel.tomselect.setValue(opt.value); } }
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

0. **IF dropdown_framework='tom-select'** → Use TYPE 4 (Tom Select template)
   - Use `Evaluate JavaScript    id=${select_id}    ...` when `select_id` is non-null
   - Use `Evaluate JavaScript    ${locator}    ...` (`select.tomselected` class lookup) when `select_id` is null

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

*Example 6 - Tom Select with select_id:*
Input: `{"locator": "css=#permission_id-ts-control", "element_type": "dropdown", "dropdown_framework": "tom-select", "select_id": "permission_id", "value": "label    Customer_permission"}`
Output:
```robot
    Evaluate JavaScript    id=permission_id    (el) => { const opt = Array.from(el.options).find(o => o.text.trim() === 'Customer_permission'); if (opt && el.tomselect) el.tomselect.setValue(opt.value); }
```

*Example 7 - Tom Select without select_id (auto-generated id, no positional assumptions):*
Input: `{"locator": "xpath=//label[normalize-space()='Timezone']/following::div[contains(@class,'ts-wrapper')][1]//div[contains(@class,'ts-control')]", "element_type": "dropdown", "dropdown_framework": "tom-select", "select_id": null, "value": "label    Asia/Kolkata"}`
Output:
```robot
    Evaluate JavaScript    xpath=//label[normalize-space()='Timezone']/following::div[contains(@class,'ts-wrapper')][1]//div[contains(@class,'ts-control')]    (el) => { const sel = el.closest('.ts-wrapper').parentElement.querySelector('select.tomselected'); if (sel && sel.tomselect) { const opt = Array.from(sel.options).find(o => o.text.trim() === 'Asia/Kolkata'); if (opt) sel.tomselect.setValue(opt.value); } }
```

**NOTE**: For 'label    X' values, extract just 'X' for Fill Text or Click text patterns.
"""

    CHECKBOX_RADIO_HANDLING = """
--- HANDLING HIDDEN RADIO BUTTONS AND CHECKBOXES ---
⚠️ **CRITICAL**: Check the 'element_type' field for radio/checkbox elements!

Modern CSS frameworks often HIDE the actual input element with CSS.
Standard 'Click' may FAIL because the input is not visible (Click has NO
force option).

**When element_type is 'radio' or 'checkbox':**
Use Check Checkbox with force=True — it checks a checkbox or SELECTS a radio
button even when the input itself is hidden:
```robot
    Check Checkbox    ${locator}    force=True
```
To untick a checkbox, same pattern with Uncheck Checkbox:
```robot
    Uncheck Checkbox    ${locator}    force=True
```

**When element_type is something else (button, link, div, etc.):**
Use standard Click keyword.
"""

    FILE_UPLOAD_HANDLING = """
--- HANDLING FILE UPLOADS (element_type='file-upload') ---
⚠️ **CRITICAL**: When a step's element has element_type='file-upload', the locator
points at the real `<input type="file">`. That input is usually HIDDEN behind a
styled browse button — this is EXPECTED and correct: hidden file inputs are legal
targets for the upload keyword. Do NOT try to make it visible, do NOT click any
styled browse button instead.

**NEVER use Click on a file-upload element.** Clicking opens the browser's NATIVE
file dialog, which Robot Framework cannot control — the test hangs until timeout.

**ALWAYS use Upload File By Selector:**
```robot
Upload File By Selector    ${locator}    <file path>
```

**File path rules:**
- If the step provides a file name or path value, use it.
- If the step names NO file, declare a placeholder variable so the user can fill
  in real test data before running:
```robot
*** Variables ***
# TODO: replace with the real file to upload
${UPLOAD_FILE}    ${CURDIR}${/}test_data.csv
```
  and use `${UPLOAD_FILE}` as the file path argument.

*Example 1 — hidden file input, file named in the step:*
Input: `{"locator": "id=customer_import_mapper", "element_type": "file-upload", "value": "customers.csv"}`
Output:
```robot
    Upload File By Selector    id=customer_import_mapper    ${CURDIR}${/}customers.csv
```

*Example 2 — no file named in the step:*
Input: `{"locator": "input[type=\\"file\\"][name=\\"ratedeck_csv\\"]", "element_type": "file-upload"}`
Output:
```robot
    Upload File By Selector    input[type="file"][name="ratedeck_csv"]    ${UPLOAD_FILE}
```
"""

    DATE_PICKER_HANDLING = """
--- HANDLING DATE PICKERS (element_type='date-picker') ---
⚠️ **CRITICAL**: Check `datepicker_framework` for date-picker elements!

Widget date pickers (flatpickr) render READONLY inputs — typing is disabled by
design and values are set via a calendar overlay. `Fill Text` waits for the
input to become editable and FAILS with a timeout, every run. The input being
readonly is EXPECTED and correct — do NOT try Fill Text on it, do NOT try to
remove the readonly attribute, do NOT click through the calendar overlay.

**When datepicker_framework='flatpickr':**
The flatpickr instance lives on the input element as `el._flatpickr`. Set the
date through the widget's own API — ONE Evaluate JavaScript line, no clicking,
no calendar navigation:
```robot
Evaluate JavaScript    ${locator}    (el) => { const fp = el._flatpickr; if (fp) fp.setDate('${value}', true); }
```
- `setDate(value, true)` updates the widget state AND fires the change event.
- flatpickr parses the value with the instance's own date format; a date-only
  value like '2026-07-01' is accepted even when the widget shows date+time.

**When datepicker_framework='native' or missing (plain input[type=date]):**
Native date inputs are editable — use Fill Text with an ISO date (YYYY-MM-DD):
```robot
Fill Text    ${locator}    2026-07-01
```

--- EXAMPLES ---

*Example 1 - flatpickr (readonly input, ASTPP CDR report filter):*
Input: `{"keyword": "Fill Text", "element_description": "From Date filter", "locator": "id=customer_cdr_from_date", "element_type": "date-picker", "datepicker_framework": "flatpickr", "value": "2026-07-01"}`
Output:
```robot
    Evaluate JavaScript    id=customer_cdr_from_date    (el) => { const fp = el._flatpickr; if (fp) fp.setDate('2026-07-01', true); }
```

*Example 2 - native date input:*
Input: `{"locator": "id=dob", "element_type": "date-picker", "datepicker_framework": "native", "value": "1990-05-15"}`
Output:
```robot
    Fill Text    id=dob    1990-05-15
```
"""

    STATE_VERIFICATION_HANDLING = """
--- HANDLING STATE VERIFICATION (keyword='Get Classes' / 'Get Attribute') ---
A Get Classes step verifies a field's state (shows an error / invalid /
highlighted) via its CSS classes. The step carries `element_classes`: the
class list the locator engine OBSERVED on the element at locate time —
captured after the preceding steps had already run, so for "click Save,
then verify the field shows an error" the observed list is the field IN its
error state. This is real evidence from the live page. Decide from it —
never from guesswork.

**Decision rule (strict order):**
1. Pick the STATE MARKER from `element_classes`: a class qualifies ONLY if
   it contains 'invalid', 'error', 'danger', or 'warning' (e.g. `invalid`,
   `is-invalid`, `has-error`, `ng-invalid`, `text-danger`).
   Base/layout/skin classes NEVER qualify — `form-control`, `text`, `field`,
   `medium`, `input`, `btn`, `row` are always on the element, error or not;
   asserting one produces a test that passes forever even when validation
   breaks.
2. If the step's 'value' names a state word, treat it as a cross-check
   only: use it if it appears in the observed `element_classes`; if it does
   not appear there, use the marker found by rule 1 instead and add a
   comment noting the user's word was not observed on the element.
3. If NO class qualifies but the observed `aria_invalid` is 'true', the
   site marks the state via ARIA instead of a CSS class — assert the
   attribute:
```robot
    Get Attribute    ${locator}    aria-invalid    ==    true
```
4. If still nothing, apply the SAME marker vocabulary to the observed
   `parent_classes` — Bootstrap-3-era sites mark the WRAPPER div, not
   the field (`form-group has-error` around a clean `form-control`
   input). Assert one level up by chaining the parent step onto the
   field's locator:
```robot
    Get Classes    ${locator} >> xpath=..    contains    <marker>
```
5. If none of the observed evidence (element classes, aria-invalid,
   parent classes) shows a state marker, do NOT pick anything else.
   Emit a loud placeholder that FAILS until a human fills it, with the
   observed classes listed so they can pick in seconds:
```robot
    # TODO: replace EXPECTED_STATE_CLASS with the class your app applies
    # to this state. Observed on this element: <element_classes>;
    # on its parent: <parent_classes>
    Get Classes    ${locator}    contains    EXPECTED_STATE_CLASS
```

**Generated line (rules 1 and 2):**
```robot
    Get Classes    ${locator}    contains    <marker>
```

For `Get Attribute` steps (the user named a specific attribute):
```robot
    Get Attribute    ${locator}    <attribute>    ==    <expected>
```

--- EXAMPLES ---

*Example 1 — auto-pick (ASTPP customer form, verified live 2026-07-09):*
Input: `{"keyword": "Get Classes", "element_description": "Email input field", "locator": "input[name=\\"email\\"]", "element_classes": "text field medium form-control invalid"}`
Output ('invalid' is the only error-family token; 'form-control' is base):
```robot
    Get Classes    input[name="email"]    contains    invalid
```

*Example 2 — marker on the parent (Bootstrap 3 convention):*
Input: `{"keyword": "Get Classes", "element_description": "Email input field", "locator": "id=email", "element_classes": "form-control", "aria_invalid": "", "parent_classes": "form-group has-error"}`
Output ('has-error' observed one level up; the field's own list is clean):
```robot
    Get Classes    id=email >> xpath=..    contains    has-error
```

*Example 3 — no marker observed anywhere (unrecognizable state class):*
Input: `{"keyword": "Get Classes", "element_description": "Email input field", "locator": "id=email", "element_classes": "form-control fld-x2", "aria_invalid": "", "parent_classes": "form-wrap"}`
Output:
```robot
    # TODO: replace EXPECTED_STATE_CLASS with the class your app applies
    # to this state. Observed on this element: form-control fld-x2;
    # on its parent: form-wrap
    Get Classes    id=email    contains    EXPECTED_STATE_CLASS
```
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
*   DEFAULT for search operations: after `Input Text` into the search box, use `Press Keys` with `Enter` instead of finding and clicking a search button. Most sites trigger search on Enter, and it removes one element to identify.
*   EXCEPTION: if the user explicitly asks to click a search/submit button, plan that click — the user's explicit instructions always win.
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

    PLANNING_STATE_VERIFICATION = """
--- VERIFYING A FIELD'S STATE (shows an error / disabled / highlighted) ---
When the user wants to verify a field's CONDITION — "the Email field shows an
error", "the field is marked invalid", "the button is disabled", "the row is
highlighted" — that is an ELEMENT-STATE check, not a text check.

Many sites (server-rendered apps especially) signal these states ONLY by
adding a CSS class to the field. There is often NO error message text
anywhere on the page — so do NOT plan a step to read an error message.

*   Target the field itself: element_description = the field the user named
    (e.g. "Email input field in the customer form"), NOT a message element.
*   Use keyword `Get Classes` for state checks (error/invalid/highlighted).
*   Use keyword `Get Attribute` only when the user names a specific
    attribute (e.g. "verify aria-invalid is true").
*   Put the user's own state word in 'value' if they used one (e.g. user
    says "marked invalid" → value = "invalid"); leave value empty for vague
    phrasings like "shows an error". Later stages fill it from what the
    browser actually observes on the field — never from a guess.

Example — "Click Save without filling anything and verify the Email field
shows an error":
1. Click Element → Save button in the customer form
2. Get Classes → Email input field in the customer form   (no value)
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
6.  **MOST CRITICAL**: DO NOT add popup dismissal, cookie consent, or any steps not explicitly mentioned in user query. The browser automation handles these automatically.
"""

    # NOTE: the IDENTIFICATION COMPONENTS (FORM_ELEMENT_HANDLING,
    # SPATIAL_CONTEXT_PRESERVATION, BATCH_TOOL_FORMAT) were removed in Task 16
    # together with identify_elements_task. The one real rule they carried —
    # the checkbox/radio/toggle description rewrite — is now code:
    # element_identification.rewrite_form_description(). Descriptions are
    # forwarded verbatim by build_elements(), and there is no LLM tool call
    # left to format.

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
4. For price/numeric validations: use Evaluate to convert strings to numbers

**FORBIDDEN in Final Answer:**
- Markdown code blocks (```json, ```)
- Thinking text ('Thought:', 'I will', etc.)
- Text before/after the JSON

**CORRECT Example:**
{"code": "*** Settings ***\\nLibrary    Browser\\n\\n*** Variables ***\\n${browser}    chromium\\n\\n*** Test Cases ***\\nGenerated Test\\n    New Browser    ${browser}    headless=True\\n    Close Browser"}
"""
