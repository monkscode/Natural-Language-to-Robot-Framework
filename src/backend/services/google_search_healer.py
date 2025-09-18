#!/usr/bin/env python3
"""
Google Search Specific Healing Service

This service provides specialized healing strategies for Google search automation failures.
It addresses common issues like ElementNotInteractableException on search buttons.
"""

import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from ..core.models import FailureContext, FailureType, LocatorStrategy


@dataclass
class GoogleSearchHealingStrategy:
    """Represents a healing strategy for Google search failures."""
    name: str
    description: str
    locator_alternatives: List[str]
    action_alternatives: List[str]
    confidence: float
    priority: int


class GoogleSearchHealer:
    """Specialized healer for Google search automation issues."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.healing_strategies = self._initialize_strategies()
    
    def _initialize_strategies(self) -> Dict[str, GoogleSearchHealingStrategy]:
        """Initialize Google search specific healing strategies."""
        return {
            "search_button_not_interactable": GoogleSearchHealingStrategy(
                name="Search Button Not Interactable",
                description="Handle cases where Google search button cannot be clicked",
                locator_alternatives=[
                    "name=btnK",
                    "css=input[name='btnK']",
                    "css=button[name='btnK']",
                    "css=input[value*='Google Search']",
                    "css=button[aria-label*='Search']",
                    "css=form input[type='submit']",
                    "css=button[type='submit']",
                    "xpath=//input[@name='btnK']",
                    "xpath=//button[@name='btnK']",
                    "xpath=//input[@value='Google Search']"
                ],
                action_alternatives=[
                    "Click Element",
                    "Press Keys    name=q    RETURN",
                    "Execute JavaScript    document.querySelector('form').submit();",
                    "Execute JavaScript    document.querySelector('input[name=\"q\"]').form.submit();",
                    "Wait Until Element Is Enabled    {locator}    timeout=10s\nClick Element    {locator}"
                ],
                confidence=0.9,
                priority=1
            ),
            
            "search_input_not_found": GoogleSearchHealingStrategy(
                name="Search Input Not Found",
                description="Handle cases where Google search input field is not found",
                locator_alternatives=[
                    "name=q",
                    "css=input[name='q']",
                    "css=textarea[name='q']",
                    "css=input[title*='Search']",
                    "css=input[aria-label*='Search']",
                    "css=input[placeholder*='Search']",
                    "xpath=//input[@name='q']",
                    "xpath=//textarea[@name='q']",
                    "xpath=//input[contains(@title, 'Search')]"
                ],
                action_alternatives=[
                    "Input Text",
                    "Wait Until Element Is Visible    {locator}    timeout=10s\nInput Text    {locator}    {text}",
                    "Clear Element Text    {locator}\nInput Text    {locator}    {text}"
                ],
                confidence=0.85,
                priority=2
            ),
            
            "page_load_timeout": GoogleSearchHealingStrategy(
                name="Page Load Timeout",
                description="Handle cases where Google page takes too long to load",
                locator_alternatives=[
                    "css=input[name='q']",
                    "css=form[role='search']",
                    "css=body",
                    "xpath=//input[@name='q']"
                ],
                action_alternatives=[
                    "Wait Until Page Contains Element    {locator}    timeout=30s",
                    "Wait Until Element Is Visible    {locator}    timeout=30s",
                    "Reload Page\nWait Until Element Is Visible    {locator}    timeout=30s"
                ],
                confidence=0.7,
                priority=3
            ),
            
            "cookie_consent_blocking": GoogleSearchHealingStrategy(
                name="Cookie Consent Blocking",
                description="Handle cookie consent dialogs that block interaction",
                locator_alternatives=[
                    "css=button[id*='accept']",
                    "css=button[id*='Accept']",
                    "css=button:contains('Accept')",
                    "css=button:contains('I agree')",
                    "xpath=//button[contains(text(), 'Accept')]",
                    "xpath=//button[contains(text(), 'I agree')]",
                    "css=button[data-ved*='accept']"
                ],
                action_alternatives=[
                    "Click Element    {locator}",
                    "Wait Until Element Is Visible    {locator}    timeout=5s\nClick Element    {locator}",
                    "Execute JavaScript    document.querySelector('{css_locator}').click();"
                ],
                confidence=0.8,
                priority=0  # Highest priority - handle first
            )
        }
    
    def analyze_google_search_failure(self, failure_context: FailureContext) -> Optional[GoogleSearchHealingStrategy]:
        """
        Analyze a failure context to determine if it's a Google search issue and suggest healing.
        
        Args:
            failure_context: The failure context to analyze
            
        Returns:
            GoogleSearchHealingStrategy if applicable, None otherwise
        """
        if not self._is_google_search_failure(failure_context):
            return None
        
        # Determine the specific type of Google search failure
        if "ElementNotInteractableException" in failure_context.exception_message:
            if "btnK" in failure_context.original_locator:
                return self.healing_strategies["search_button_not_interactable"]
        
        if "NoSuchElementException" in failure_context.exception_message:
            if "name=q" in failure_context.original_locator:
                return self.healing_strategies["search_input_not_found"]
        
        if "TimeoutException" in failure_context.exception_message:
            if "google.com" in failure_context.target_url:
                return self.healing_strategies["page_load_timeout"]
        
        # Check for cookie consent issues
        if self._might_be_cookie_consent_issue(failure_context):
            return self.healing_strategies["cookie_consent_blocking"]
        
        return None
    
    def _is_google_search_failure(self, failure_context: FailureContext) -> bool:
        """Check if the failure is related to Google search."""
        google_indicators = [
            "google.com" in failure_context.target_url.lower(),
            "name=q" in failure_context.original_locator,
            "name=btnK" in failure_context.original_locator,
            "google search" in failure_context.test_case.lower()
        ]
        return any(google_indicators)
    
    def _might_be_cookie_consent_issue(self, failure_context: FailureContext) -> bool:
        """Check if the failure might be due to cookie consent dialog."""
        consent_indicators = [
            "element not interactable" in failure_context.exception_message.lower(),
            "google.com" in failure_context.target_url.lower(),
            # If the first interaction fails, it might be consent blocking
            failure_context.failing_step.strip().startswith("Click Element") or 
            failure_context.failing_step.strip().startswith("Input Text")
        ]
        return all(consent_indicators[:2]) and any(consent_indicators[2:])
    
    def generate_healed_robot_code(self, failure_context: FailureContext, strategy: GoogleSearchHealingStrategy) -> str:
        """
        Generate healed Robot Framework code based on the healing strategy.
        
        Args:
            failure_context: The original failure context
            strategy: The healing strategy to apply
            
        Returns:
            Healed Robot Framework code
        """
        if strategy.name == "Search Button Not Interactable":
            return self._generate_search_button_healing_code(failure_context)
        elif strategy.name == "Search Input Not Found":
            return self._generate_search_input_healing_code(failure_context)
        elif strategy.name == "Cookie Consent Blocking":
            return self._generate_cookie_consent_healing_code(failure_context)
        elif strategy.name == "Page Load Timeout":
            return self._generate_page_load_healing_code(failure_context)
        
        return self._generate_generic_healing_code(failure_context, strategy)
    
    def _generate_search_button_healing_code(self, failure_context: FailureContext) -> str:
        """Generate healing code for search button interaction issues."""
        search_term = self._extract_search_term_from_context(failure_context)
        
        return f"""*** Keywords ***
Robust Google Search Button Click
    [Arguments]    ${{search_term}}={search_term}
    [Documentation]    Robust Google search with multiple fallback strategies
    
    # Strategy 1: Wait and click button
    TRY
        Wait Until Element Is Enabled    name=btnK    timeout=10s
        Click Element    name=btnK
        RETURN
    EXCEPT
        Log    Button click failed, trying Enter key
    END
    
    # Strategy 2: Press Enter instead
    TRY
        Press Keys    name=q    RETURN
        RETURN
    EXCEPT
        Log    Enter key failed, trying JavaScript
    END
    
    # Strategy 3: JavaScript form submission
    TRY
        Execute JavaScript    document.querySelector('form').submit();
        RETURN
    EXCEPT
        Log    JavaScript submission failed, trying alternative selectors
    END
    
    # Strategy 4: Alternative button selectors
    ${{button_selectors}}=    Create List
    ...    css=input[name="btnK"]
    ...    css=button[name="btnK"]
    ...    css=input[value*="Google Search"]
    
    FOR    ${{selector}}    IN    @{{button_selectors}}
        TRY
            Wait Until Element Is Enabled    ${{selector}}    timeout=5s
            Click Element    ${{selector}}
            RETURN
        EXCEPT
            Continue For Loop
        END
    END
    
    Fail    All search button strategies failed

# Replace the original failing step with:
Robust Google Search Button Click"""
    
    def _generate_search_input_healing_code(self, failure_context: FailureContext) -> str:
        """Generate healing code for search input field issues."""
        search_term = self._extract_search_term_from_context(failure_context)
        
        return f"""*** Keywords ***
Robust Google Search Input
    [Arguments]    ${{search_term}}={search_term}
    [Documentation]    Robust Google search input with multiple locator strategies
    
    ${{input_selectors}}=    Create List
    ...    name=q
    ...    css=input[name="q"]
    ...    css=textarea[name="q"]
    ...    css=input[title*="Search"]
    ...    css=input[aria-label*="Search"]
    
    FOR    ${{selector}}    IN    @{{input_selectors}}
        TRY
            Wait Until Element Is Visible    ${{selector}}    timeout=10s
            Clear Element Text    ${{selector}}
            Input Text    ${{selector}}    ${{search_term}}
            RETURN
        EXCEPT
            Continue For Loop
        END
    END
    
    Fail    All search input strategies failed

# Replace the original failing step with:
Robust Google Search Input    {search_term}"""
    
    def _generate_cookie_consent_healing_code(self, failure_context: FailureContext) -> str:
        """Generate healing code for cookie consent handling."""
        return """*** Keywords ***
Handle Google Cookie Consent
    [Documentation]    Handle Google's cookie consent dialog if present
    
    ${consent_selectors}=    Create List
    ...    css=button[id*="accept"]
    ...    css=button[id*="Accept"]
    ...    css=button:contains("Accept")
    ...    css=button:contains("I agree")
    ...    xpath=//button[contains(text(), "Accept")]
    
    FOR    ${selector}    IN    @{consent_selectors}
        ${consent_present}=    Run Keyword And Return Status
        ...    Wait Until Element Is Visible    ${selector}    timeout=3s
        IF    ${consent_present}
            Click Element    ${selector}
            Sleep    1s
            RETURN
        END
    END
    
    Log    No cookie consent dialog found or already handled

# Add this keyword call before any Google interaction:
Handle Google Cookie Consent"""
    
    def _generate_page_load_healing_code(self, failure_context: FailureContext) -> str:
        """Generate healing code for page load timeout issues."""
        return """*** Keywords ***
Robust Google Page Load
    [Documentation]    Robust Google page loading with retries
    
    FOR    ${i}    IN RANGE    3
        TRY
            Go To    https://www.google.com
            Wait Until Element Is Visible    name=q    timeout=30s
            RETURN
        EXCEPT
            Run Keyword If    ${i} < 2    Sleep    5s
            Run Keyword If    ${i} == 2    Fail    Failed to load Google after 3 attempts
        END
    END

# Replace the original page navigation with:
Robust Google Page Load"""
    
    def _generate_generic_healing_code(self, failure_context: FailureContext, strategy: GoogleSearchHealingStrategy) -> str:
        """Generate generic healing code based on strategy."""
        alternatives = "\n".join([f"    ...    {alt}" for alt in strategy.locator_alternatives[:5]])
        
        return f"""*** Keywords ***
Healed Google Interaction
    [Documentation]    Healed interaction with multiple locator strategies
    
    ${{locator_alternatives}}=    Create List
{alternatives}
    
    FOR    ${{locator}}    IN    @{{locator_alternatives}}
        TRY
            Wait Until Element Is Visible    ${{locator}}    timeout=10s
            # Apply the original action with the alternative locator
            {failure_context.failing_step.replace(failure_context.original_locator, "${locator}")}
            RETURN
        EXCEPT
            Continue For Loop
        END
    END
    
    Fail    All locator alternatives failed

# Replace the original failing step with:
Healed Google Interaction"""
    
    def _extract_search_term_from_context(self, failure_context: FailureContext) -> str:
        """Extract search term from the failure context."""
        # Try to extract from the failing step or test case
        if "Input Text" in failure_context.failing_step:
            parts = failure_context.failing_step.split()
            if len(parts) >= 4:
                return " ".join(parts[3:])  # Everything after "Input Text name=q"
        
        # Fallback to a default search term
        return "test search"
    
    def get_healing_confidence(self, failure_context: FailureContext) -> float:
        """Get confidence score for healing this specific failure."""
        strategy = self.analyze_google_search_failure(failure_context)
        return strategy.confidence if strategy else 0.0
    
    def get_healing_recommendations(self, failure_context: FailureContext) -> List[Dict[str, Any]]:
        """Get detailed healing recommendations for the failure."""
        strategy = self.analyze_google_search_failure(failure_context)
        if not strategy:
            return []
        
        return [
            {
                "strategy_name": strategy.name,
                "description": strategy.description,
                "confidence": strategy.confidence,
                "priority": strategy.priority,
                "locator_alternatives": strategy.locator_alternatives[:3],  # Top 3
                "action_alternatives": strategy.action_alternatives[:2],   # Top 2
                "estimated_success_rate": f"{strategy.confidence * 100:.0f}%"
            }
        ]