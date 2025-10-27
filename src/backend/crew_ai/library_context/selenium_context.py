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

    @property
    def library_name(self) -> str:
        return "SeleniumLibrary"

    @property
    def library_import(self) -> str:
        return "Library    SeleniumLibrary"

    @property
    def planning_context(self) -> str:
        """
        Context for Step Planner Agent.
        Combines dynamic keywords with static best practices.
        """
        # Get dynamic keywords from installed library (top 25 most common)
        dynamic_keywords = self._doc_extractor.get_keywords_summary(
            max_keywords=25)

        # Add complete keyword list (lightweight - just names)
        from .dynamic_context import get_all_keywords_list
        all_keywords = get_all_keywords_list("SeleniumLibrary")

        # Add static best practices
        best_practices = """

**BEST PRACTICES:**

1. **Search Optimization:**
   - For search operations: Use "Press Keys" with "RETURN" instead of clicking search button
   - Works on Google, Flipkart, Amazon, and most modern websites
   - Faster and more reliable than finding/clicking search buttons

2. **Locator Strategy:**
   - Priority: id > name > data-* > aria-* > css > xpath
   - Avoid dynamic classes (e.g., class names with random numbers)
   - Use explicit locators from the element identifier agent

3. **Wait Strategy:**
   - Use "Wait Until Element Is Visible" for dynamic content
   - Default timeout: 10 seconds
   - Add waits after navigation or AJAX operations

**LOCATOR FORMATS:**
- id=<value>          → Find by ID attribute
- name=<value>        → Find by name attribute  
- xpath=<expression>  → Find by XPath
- css=<selector>      → Find by CSS selector
"""

        return dynamic_keywords + all_keywords + best_practices

    @property
    def code_assembly_context(self) -> str:
        """Context for Code Assembler Agent - extracted from existing tasks.py"""
        return """
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
"""

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
    def validation_context(self) -> str:
        """Context for Code Validator Agent - extracted from existing tasks.py"""
        return """
--- SELENIUMLIBRARY VALIDATION RULES ---

**VALIDATION CHECKLIST:**
1. All required libraries are imported (SeleniumLibrary, BuiltIn, String if needed)
2. All keywords have the correct number of arguments
3. Variables are properly declared before use
4. Should Be True statements have valid expressions
5. Run Keyword If statements have proper syntax
6. Price/numeric comparisons use proper conversion (Evaluate)

**COMMON ERRORS TO CHECK:**

1. **Missing Variable Declaration**
   ❌ WRONG: Get Text    name=q
   ✅ CORRECT: 
      *** Variables ***
      ${search_locator}    name=q
      
      *** Test Cases ***
      Test
          Get Text    ${search_locator}

2. **Incorrect Assignment Syntax**
   ❌ WRONG: Get Text    ${locator}    ${result}
   ✅ CORRECT: ${result}=    Get Text    ${locator}

3. **Missing Library Import**
   ❌ WRONG: (no *** Settings *** section)
   ✅ CORRECT:
      *** Settings ***
      Library    SeleniumLibrary
      Library    BuiltIn

4. **Invalid Locator Format**
   ❌ WRONG: Get Text    search-box
   ✅ CORRECT: Get Text    id=search-box

5. **Missing Browser Config**
   ❌ WRONG: Open Browser    https://example.com
   ✅ CORRECT: Open Browser    https://example.com    chrome    options=${options}

6. **Incorrect Should Be True Syntax**
   ❌ WRONG: Should Be True    price < 1000
   ✅ CORRECT: Should Be True    ${price} < 1000

7. **Get Text without locator argument**
   ❌ WRONG: ${text}=    Get Text
   ✅ CORRECT: ${text}=    Get Text    ${locator}

8. **Invalid expressions in Should Be True**
   ❌ WRONG: Should Be True    product_price < 1000
   ✅ CORRECT: Should Be True    ${product_price} < 1000

9. **Missing variable assignments**
   ❌ WRONG: Get Text    ${locator}
   ✅ CORRECT: ${result}=    Get Text    ${locator}

10. **Incorrect conditional syntax**
    ❌ WRONG: If    ${total} > 100    Input Text    ${locator}    text
    ✅ CORRECT: Run Keyword If    ${total} > 100    Input Text    ${locator}    text
"""
