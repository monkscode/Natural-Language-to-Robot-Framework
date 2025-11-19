"""
SeleniumLibrary context for dynamic code generation.

This module provides SeleniumLibrary-specific knowledge by combining:
- Dynamic keyword extraction from installed library (via libdoc)
- Static best practices and code structure templates
"""

from .base import LibraryContext
from .dynamic_context import DynamicLibraryDocumentation
import logging

logger = logging.getLogger(__name__)


class SeleniumLibraryContext(LibraryContext):
    """Context provider for SeleniumLibrary with dynamic keyword extraction."""

    def __init__(self):
        """Initialize with dynamic documentation extractor."""
        self._doc_extractor = DynamicLibraryDocumentation("SeleniumLibrary")
        # Lazy-loaded caches for contexts
        self._planning_context_cache = None
        self._code_assembly_context_cache = None

    @property
    def library_name(self) -> str:
        return "SeleniumLibrary"

    @property
    def library_import(self) -> str:
        return "Library    SeleniumLibrary"

    @property
    def planning_context(self) -> str:
        """
        Minimal context for Test Automation Planner Agent.
        Returns high-level action categories without detailed keyword information.
        Uses lazy loading with caching for performance.
        """
        if self._planning_context_cache is None:
            self._planning_context_cache = self._doc_extractor.get_minimal_planning_context()
        return self._planning_context_cache

    @property
    def code_assembly_context(self) -> str:
        """
        Detailed context for Code Assembler Agent.
        Focuses on code structure and syntax rules.
        Keyword details are available via keyword_search_tool.
        Uses lazy loading with caching for performance.
        """
        if self._code_assembly_context_cache is None:
            # Code structure template with critical syntax rules
            code_structure = """
--- SELENIUMLIBRARY CODE STRUCTURE ---

**MANDATORY STRUCTURE:**

```robot
*** Settings ***
Library    SeleniumLibrary
Library    BuiltIn

*** Variables ***
${browser}    chrome
${options}    add_argument("--headless");add_argument("--no-sandbox");add_argument("--incognito")
# Declare all locators and variables here

*** Test Cases ***
Generated Test
    [Documentation]    Auto-generated test case
    Open Browser    ${url}    ${browser}    options=${options}
    # Test steps here
    Close Browser
```

**CRITICAL: SeleniumLibrary MUST use options=${options} parameter in Open Browser keyword**

**VARIABLE DECLARATION RULES:**
1. ALL variables must be declared in *** Variables *** section
2. Browser config: ${browser}, ${options}
3. Locators: ${element_name_locator}
4. Retrieved values: ${variable_name}

**KEYWORD SYNTAX:**

Open Browser:
    Open Browser    <url>    ${browser}    options=${options}

Input Text:
    Input Text    ${locator}    <text>

Press Keys (for search):
    Press Keys    ${locator}    RETURN

Click Element:
    Click Element    ${locator}

Get Text (store in variable):
    ${result}=    Get Text    ${locator}
    Log    Retrieved: ${result}

Wait Until Element Is Visible:
    Wait Until Element Is Visible    ${locator}    timeout=10s

Should Be True (validation):
    Should Be True    ${variable} < 1000

Close Browser:
    Close Browser

**HANDLING VALIDATION:**
For price or numeric validations:
1. Use Get Text to retrieve the value
2. Use Evaluate to convert string to number
3. Use Should Be True for comparison

Example:
    ${product_price}=    Get Text    ${price_locator}
    ${price_numeric}=    Evaluate    float('${product_price}'.replace('₹', '').replace(',', ''))
    Should Be True    ${price_numeric} < 9999

**HANDLING CONDITIONAL LOGIC:**
Use Run Keyword If for conditional execution:
    Run Keyword If    ${total} > 100    Input Text    ${locator}    SAVE10

**HANDLING LOOPS:**
Use FOR loops for iteration:
    FOR    ${link}    IN    @{links}
        Click Element    ${link}
    END

**LIBRARIES TO INCLUDE:**
- SeleniumLibrary (for web automation)
- BuiltIn (for Should Be True, Evaluate, etc.)
- String (if string manipulation is needed)

**CRITICAL RULES:**
1. Always declare variables before use
2. Use ${variable}= syntax for assignments
3. Locators must be stored in variables
4. Include proper indentation (4 spaces)
5. Add documentation to test cases

**KEYWORD REFERENCE:**
Use the keyword_search_tool to look up specific keyword details when needed.
Common keywords: Open Browser, Input Text, Press Keys, Click Element, Get Text,
Wait Until Element Is Visible, Should Be True, Close Browser
"""
            
            self._code_assembly_context_cache = code_structure
        
        return self._code_assembly_context_cache

    @property
    def browser_init_params(self) -> dict:
        """
        Return browser initialization parameters for SeleniumLibrary.

        Returns:
            dict: Dictionary with 'browser' and 'options' parameters
        """
        return {
            'browser': 'chrome',
            'options': 'add_argument("--headless");add_argument("--no-sandbox");add_argument("--incognito")'
        }

    @property
    def requires_viewport_config(self) -> bool:
        """
        SeleniumLibrary does not require explicit viewport configuration.

        Returns:
            bool: False - SeleniumLibrary manages viewport automatically
        """
        return False

    def get_viewport_config_code(self) -> str:
        """
        Return viewport configuration code for SeleniumLibrary.

        Returns:
            str: Empty string - SeleniumLibrary doesn't need viewport config
        """
        return ""

    @property
    def core_rules(self) -> str:
        """
        Core SeleniumLibrary rules that must always be included (~300 tokens).
        
        These critical rules ensure correct code generation even in optimized mode.
        """
        return """
**SELENIUMLIBRARY CORE RULES:**

1. **BROWSER INITIALIZATION:**
   Open Browser    <url>    chrome    options=${options}
   
   Example:
   ```robot
   ${browser}    chrome
   ${options}    add_argument("--headless");add_argument("--no-sandbox");add_argument("--incognito")
   Open Browser    https://example.com    ${browser}    options=${options}
   ```

2. **PARAMETER RULES:**
   - SeleniumLibrary uses: browser=chrome, options=${options}
   - NOT Browser Library syntax (no separate headless parameter)
   - Options format: add_argument("--headless");add_argument("--no-sandbox")

3. **WAIT STRATEGY:**
   - SeleniumLibrary does NOT auto-wait (unlike Browser Library)
   - Use "Wait Until Element Is Visible" for dynamic content
   - Default timeout: 10 seconds
   - Add waits after navigation or AJAX operations

4. **LOCATOR PRIORITY:**
   id > name > data-* > aria-* > css > xpath
   - id=<value> → Most reliable
   - name=<value> → Good for forms
   - xpath=<expression> → Last resort

5. **VARIABLE ASSIGNMENT:**
   - Use ${variable}= syntax for assignments
   - Example: ${text}=    Get Text    ${locator}
   - NOT: Get Text    ${locator}    ${text}

6. **COMMON PITFALLS:**
   ❌ Missing options parameter → Browser fails to start
   ❌ No explicit waits → Elements not found
   ❌ Wrong assignment syntax → Variables not set
"""

    @property
    def validation_context(self) -> str:
        """
        Context for Code Validator Agent - SeleniumLibrary (OPTIMIZED)
        
        PURPOSE: Provide LIBRARY-SPECIFIC syntax rules that differ from Browser Library.
        This is the "what changes between libraries" context.
        
        SCOPE: Minimal, focused rules (~50 tokens)
        - Library imports (SeleniumLibrary, BuiltIn)
        - Variable assignment syntax
        - Locator prefix requirements (id=, name=, xpath=)
        - Conditional keyword syntax (Run Keyword If)
        
        NOT INCLUDED: Generic validation workflow, error reporting format, delegation logic
        (That's in tasks.py validate_code_task description - ~500 tokens)
        
        SEPARATION OF CONCERNS:
        - validation_context = Library-specific SYNTAX rules (here)
        - Task description = Generic validation WORKFLOW (tasks.py)
        - Optimized context = Query-specific KEYWORDS (smart_provider)
        """
        return """
**SELENIUMLIBRARY RULES:**
• Import SeleniumLibrary, BuiltIn
• Variable assignment: ${var}= Get Text ${loc}
• Locators need prefix: id=, name=, xpath=, css=
• Variables in expressions: Should Be True ${price} < 1000
• Conditionals: Run Keyword If ${cond} Keyword Args
"""
