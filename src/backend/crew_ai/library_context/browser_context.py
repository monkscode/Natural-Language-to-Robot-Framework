"""
Browser Library context for dynamic code generation.

This module provides Browser Library-specific knowledge by combining:
- Dynamic keyword extraction from installed library (via libdoc)
- Static best practices and code structure templates
"""

from .base import LibraryContext
from .dynamic_context import DynamicLibraryDocumentation
import logging

logger = logging.getLogger(__name__)


class BrowserLibraryContext(LibraryContext):
    """Context provider for Browser Library with dynamic keyword extraction."""

    def __init__(self):
        """Initialize with dynamic documentation extractor."""
        self._doc_extractor = DynamicLibraryDocumentation("Browser")

    @property
    def library_name(self) -> str:
        return "Browser"

    @property
    def library_import(self) -> str:
        return "Library    Browser"

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
        all_keywords = get_all_keywords_list("Browser")

        # Add static best practices
        best_practices = """

**BEST PRACTICES:**

1. **Browser Initialization:**
   - Always use "New Browser" before "New Page"
   - Browser types: chromium (recommended), firefox, webkit
   - Set headless=True for CI/CD environments

2. **Auto-Waiting:**
   - Browser Library automatically waits for elements to be actionable
   - Explicit waits rarely needed (unlike SeleniumLibrary)
   - Elements must be visible, enabled, and stable before interaction

3. **Locator Strategy (Browser Library Advantages):**
   - **text=<value>** → Find by visible text (most stable!)
   - **role=<role>[name="<name>"]** → Find by ARIA role (accessibility-first)
   - **data-testid=<value>** → Find by test ID
   - **id=<value>** → Find by ID
   - **<css_selector>** → CSS selector (no prefix needed)
   - **xpath=<expression>** → XPath (no prefix needed)

4. **Priority Order:**
   - text > role > data-testid > id > css > xpath
   - Text and role selectors are more stable than CSS/XPath

**KEY DIFFERENCES FROM SELENIUM:**
- ✅ Auto-waiting built-in (no explicit waits needed)
- ✅ Strict mode ensures locators are unique
- ✅ Better locator strategies (text, role)
- ✅ Faster execution (Playwright engine)
"""

        return dynamic_keywords + all_keywords + best_practices

    @property
    def code_assembly_context(self) -> str:
        """Context for Code Assembler Agent - Browser Library specific"""
        return """
--- BROWSER LIBRARY CODE STRUCTURE ---

**MANDATORY STRUCTURE:**

```robot
*** Settings ***
Library    Browser

*** Variables ***
${browser}    chromium
${headless}    True
# Declare all locators and variables here

*** Test Cases ***
Generated Test
    [Documentation]    Auto-generated test case
    New Browser    ${browser}    headless=${headless}
    New Page    ${url}
    # Test steps here
    Close Browser
```

**VARIABLE DECLARATION RULES:**
1. ALL variables must be declared in *** Variables *** section
2. Browser config: ${browser}, ${headless}
3. Locators: ${element_name_locator}
4. Retrieved values: ${variable_name}

**KEYWORD SYNTAX:**

New Browser + New Page:
    New Browser    chromium    headless=True
    New Page    <url>

Fill Text:
    Fill Text    ${locator}    <text>

Keyboard Key (for search):
    Keyboard Key    press    Enter

Click:
    Click    ${locator}

Get Text (store in variable):
    ${result}=    Get Text    ${locator}
    Log    Retrieved: ${result}

Wait For Elements State:
    Wait For Elements State    ${locator}    visible    timeout=10s

Close Browser:
    Close Browser

**CRITICAL RULES:**
1. Always use New Browser before New Page
2. Browser Library auto-waits, so explicit waits are rarely needed
3. Locators can be CSS selectors without prefix
4. Text and role selectors are preferred for stability
"""

    @property
    def validation_context(self) -> str:
        """Context for Code Validator Agent - Browser Library specific"""
        return """
--- BROWSER LIBRARY VALIDATION RULES ---

**VALIDATION CHECKLIST:**
1. Library Browser is imported
2. New Browser is called before New Page
3. All keywords have correct arguments
4. Variables are properly declared
5. Locators use valid Browser Library format

**COMMON ERRORS TO CHECK:**

1. **Missing New Browser**
   ❌ WRONG: New Page    https://example.com
   ✅ CORRECT:
      New Browser    chromium    headless=True
      New Page    https://example.com

2. **Incorrect Assignment Syntax**
   ❌ WRONG: Get Text    ${locator}    ${result}
   ✅ CORRECT: ${result}=    Get Text    ${locator}

3. **Missing Library Import**
   ❌ WRONG: (no *** Settings *** section)
   ✅ CORRECT:
      *** Settings ***
      Library    Browser

4. **Using SeleniumLibrary keywords**
   ❌ WRONG: Open Browser    https://example.com
   ✅ CORRECT: 
      New Browser    chromium    headless=True
      New Page    https://example.com
"""
