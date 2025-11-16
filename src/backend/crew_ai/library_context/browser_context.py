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
        # Lazy-loaded caches for contexts
        self._planning_context_cache = None
        self._code_assembly_context_cache = None

    @property
    def library_name(self) -> str:
        return "Browser"

    @property
    def library_import(self) -> str:
        return "Library    Browser"

    @property
    def browser_init_params(self) -> dict:
        """Return browser initialization parameters for Browser Library."""
        return {
            'browser': 'chromium',
            'headless': 'True'
        }

    @property
    def requires_viewport_config(self) -> bool:
        """Browser Library requires explicit viewport configuration."""
        return True

    def get_viewport_config_code(self) -> str:
        """Return viewport configuration code for Browser Library."""
        return "    New Context    viewport=None"

    @property
    def core_rules(self) -> str:
        """
        Core Browser Library rules that must always be included (~300 tokens).
        
        These critical rules ensure correct code generation even in optimized mode.
        """
        return """
**BROWSER LIBRARY CORE RULES:**

1. **CRITICAL SEQUENCE (MUST FOLLOW):**
   New Browser → New Context viewport=None → New Page
   
   Example:
   ```robot
   New Browser    chromium    headless=True
   New Context    viewport=None    ← REQUIRED!
   New Page    https://example.com
   ```

2. **VIEWPORT REQUIREMENT:**
   - ALWAYS include "New Context    viewport=None" after New Browser
   - Default viewport (800x600) causes element detection failures
   - This is the #1 cause of Browser Library test failures

3. **PARAMETER RULES:**
   - Browser Library uses: browser=chromium, headless=True
   - NOT SeleniumLibrary syntax (no 'options' parameter)
   - Valid browsers: chromium, firefox, webkit

4. **AUTO-WAITING:**
   - Browser Library auto-waits for elements (built-in)
   - Explicit waits rarely needed
   - Elements must be visible, enabled, stable

5. **LOCATOR PRIORITY:**
   text > role > data-testid > id > css > xpath
   - text=<value> → Most stable
   - role=<role>[name="<name>"] → Accessibility-first
   - CSS selectors need no prefix

6. **COMMON PITFALLS:**
   ❌ Missing viewport config → Elements not found
   ❌ Using SeleniumLibrary syntax → Keyword errors
   ❌ Wrong sequence → Browser not initialized
"""

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
    New Context    viewport=None
    New Page    ${url}
    # Test steps here
    Close Browser
```

**CRITICAL: VIEWPORT CONFIGURATION**
Browser Library uses a small default viewport (800x600) which causes element detection failures.
You MUST include "New Context    viewport=None" after "New Browser" and before "New Page".

**Correct Order:**
1. New Browser    ${browser}    headless=${headless}
2. New Context    viewport=None    ← REQUIRED
3. New Page    ${url}

**VARIABLE DECLARATION RULES:**
1. ALL variables must be declared in *** Variables *** section
2. Browser config: ${browser}, ${headless}
3. Locators: ${element_name_locator}
4. Retrieved values: ${variable_name}

**KEYWORD SYNTAX:**

New Browser (NO options parameter):
    New Browser    chromium    headless=True
    Note: Browser Library uses 'browser' and 'headless' parameters, NOT 'options'

New Context (viewport configuration):
    New Context    viewport=None

New Page:
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
1. Always use New Browser before New Context before New Page
2. MUST include "New Context    viewport=None" for proper element detection
3. Browser Library uses 'browser' and 'headless' parameters (NOT 'options')
4. Browser Library auto-waits, so explicit waits are rarely needed
5. Locators can be CSS selectors without prefix
6. Text and role selectors are preferred for stability

**KEYWORD REFERENCE:**
Use the keyword_search_tool to look up specific keyword details when needed.
Common keywords: New Browser, New Context, New Page, Fill Text, Click, Get Text, 
Keyboard Key, Wait For Elements State, Close Browser
"""
            
            self._code_assembly_context_cache = code_structure
        
        return self._code_assembly_context_cache

    @property
    def validation_context(self) -> str:
        """Context for Code Validator Agent - Browser Library specific (OPTIMIZED)"""
        return """
**BROWSER LIBRARY RULES:**
• Library Browser must be imported
• New Browser before New Page
• Variable assignment: ${var}= Get Text ${loc}
• No SeleniumLibrary keywords (use New Browser, not Open Browser)
"""
