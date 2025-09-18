"""Healing tasks for the test self-healing system."""

from crewai import Task
from typing import Dict, Any, List


class HealingTasks:
    """Collection of specialized tasks for test healing workflows."""

    def analyze_failure_task(self, agent, failure_context: Dict[str, Any]) -> Task:
        """Task for analyzing test failures to determine healing feasibility."""
        return Task(
            description=f"""
            Analyze the provided test failure to determine if it's a healable locator issue and extract relevant context.
            
            Failure Context:
            - Test File: {failure_context.get('test_file', 'N/A')}
            - Test Case: {failure_context.get('test_case', 'N/A')}
            - Failing Step: {failure_context.get('failing_step', 'N/A')}
            - Original Locator: {failure_context.get('original_locator', 'N/A')}
            - Target URL: {failure_context.get('target_url', 'N/A')}
            - Exception Type: {failure_context.get('exception_type', 'N/A')}
            - Exception Message: {failure_context.get('exception_message', 'N/A')}
            
            --- ANALYSIS CRITERIA ---
            
            **Healable Failure Types:**
            - NoSuchElementException: Element not found with current locator
            - ElementNotInteractableException: Element exists but cannot be interacted with
            - TimeoutException: Element took too long to appear (may indicate locator issue)
            - StaleElementReferenceException: Element reference is no longer valid
            
            **Non-Healable Failure Types:**
            - AssertionError: Test logic failures
            - Network-related exceptions: Connection issues
            - Application errors: 500 errors, crashes
            - Data-related failures: Missing test data
            
            --- CONTEXT EXTRACTION ---
            
            If the failure is healable, extract:
            1. **Element Type**: What type of element was being targeted (button, input, link, etc.)
            2. **Action Intent**: What action was being performed (click, input text, select, etc.)
            3. **Element Context**: Any surrounding context that might help identify the element
            4. **Locator Strategy**: What strategy was used (id, css, xpath, etc.)
            5. **Failure Reason**: Specific reason why the locator failed
            
            --- OUTPUT FORMAT ---
            
            You MUST respond with ONLY a valid JSON object containing:
            {{
                "is_healable": boolean,
                "failure_type": "element_not_found|element_not_interactable|timeout|stale_element|other",
                "confidence": float (0.0 to 1.0),
                "element_type": "button|input|link|select|div|span|other",
                "action_intent": "click|input_text|select|get_text|wait|other",
                "locator_strategy": "id|name|css|xpath|link_text|other",
                "failure_reason": "brief description of why locator failed",
                "element_context": "description of element's purpose and surrounding context",
                "healing_priority": "high|medium|low",
                "recommendations": ["list", "of", "healing", "strategies"]
            }}
            """,
            expected_output="A JSON object with failure analysis results including healability assessment and extracted context.",
            agent=agent,
        )

    def generate_alternative_locators_task(self, agent, failure_analysis: Dict[str, Any], dom_context: str = "") -> Task:
        """Task for generating alternative locators based on failure analysis."""
        return Task(
            description=f"""
            Generate multiple alternative locators for the failed element based on the failure analysis and DOM context.
            
            Failure Analysis:
            {failure_analysis}
            
            DOM Context (if available):
            {dom_context or "No DOM context provided - generate locators based on analysis"}
            
            --- LOCATOR GENERATION STRATEGY ---
            
            **Priority Order:**
            1. ID-based locators (most stable)
            2. Name-based locators
            3. CSS selectors (class, attribute combinations)
            4. XPath expressions (relative and absolute)
            5. Link text (for links)
            6. Partial link text
            7. Tag name with attributes
            
            **Generation Rules:**
            1. Generate at least 3-5 alternative locators
            2. Use different strategies for each alternative
            3. Consider element context and surrounding elements
            4. Prioritize stability over specificity
            5. Include both strict and flexible matching approaches
            
            **Locator Patterns:**
            - For buttons: Look for text content, aria-labels, data attributes
            - For inputs: Look for name, placeholder, labels, surrounding text
            - For links: Look for href patterns, text content, title attributes
            - For containers: Look for class combinations, data attributes, structure
            
            **Robot Framework Syntax:**
            - ID: `id=element_id`
            - Name: `name=element_name`
            - CSS: `css=.class-name` or `css=input[type='submit']`
            - XPath: `xpath=//button[text()='Submit']` or `xpath=//input[@placeholder='Search']`
            - Link: `link=Link Text`
            - Partial Link: `partial link=Partial Text`
            
            --- OUTPUT FORMAT ---
            
            You MUST respond with ONLY a valid JSON object containing:
            {{
                "alternatives": [
                    {{
                        "locator": "robot framework locator string",
                        "strategy": "id|name|css|xpath|link_text|partial_link_text|tag_name",
                        "confidence": float (0.0 to 1.0),
                        "reasoning": "why this locator should work",
                        "stability_score": float (0.0 to 1.0),
                        "fallback_level": "primary|secondary|tertiary"
                    }}
                ],
                "generation_strategy": "description of overall approach used",
                "dom_analysis": "key insights from DOM analysis if available",
                "recommendations": ["additional", "healing", "suggestions"]
            }}
            """,
            expected_output="A JSON object with multiple alternative locators and their metadata.",
            agent=agent,
        )

    def validate_locators_task(self, agent, locator_candidates: List[Dict[str, Any]], validation_context: Dict[str, Any]) -> Task:
        """Task for validating alternative locators against live browser sessions."""
        return Task(
            description=f"""
            Validate the provided alternative locators against a live browser session to determine which ones work correctly.
            
            Locator Candidates:
            {locator_candidates}
            
            Validation Context:
            - Target URL: {validation_context.get('target_url', 'N/A')}
            - Expected Element Type: {validation_context.get('element_type', 'N/A')}
            - Expected Action: {validation_context.get('action_intent', 'N/A')}
            - Original Locator: {validation_context.get('original_locator', 'N/A')}
            
            --- VALIDATION CRITERIA ---
            
            **Element Existence:**
            - Does the locator find exactly one element?
            - Is the element visible on the page?
            - Is the element in the expected location?
            
            **Element Properties:**
            - Does the element match the expected type (tag name)?
            - Does the element have expected attributes?
            - Does the element contain expected text content?
            
            **Interactability:**
            - Is the element clickable (if click action expected)?
            - Is the element editable (if input action expected)?
            - Is the element enabled and not disabled?
            
            **Stability Assessment:**
            - How specific is the locator (more specific = potentially less stable)?
            - Does the locator depend on dynamic content?
            - How likely is the locator to break with UI changes?
            
            --- VALIDATION PROCESS ---
            
            For each locator candidate:
            1. Attempt to find the element using the locator
            2. Check element visibility and interactability
            3. Verify element properties match expectations
            4. Assess locator stability and reliability
            5. Test the intended action if safe to do so
            
            --- OUTPUT FORMAT ---
            
            You MUST respond with ONLY a valid JSON object containing:
            {{
                "validation_results": [
                    {{
                        "locator": "the tested locator string",
                        "strategy": "locator strategy used",
                        "is_valid": boolean,
                        "element_found": boolean,
                        "is_visible": boolean,
                        "is_interactable": boolean,
                        "matches_expected_type": boolean,
                        "confidence_score": float (0.0 to 1.0),
                        "stability_score": float (0.0 to 1.0),
                        "error_message": "error description if validation failed",
                        "element_properties": {{
                            "tag_name": "element tag",
                            "text_content": "element text",
                            "attributes": {{"key": "value"}}
                        }},
                        "validation_notes": "additional observations"
                    }}
                ],
                "best_candidate": {{
                    "locator": "best working locator",
                    "strategy": "strategy used",
                    "confidence_score": float,
                    "selection_reason": "why this was selected as best"
                }},
                "validation_summary": {{
                    "total_tested": int,
                    "successful_validations": int,
                    "success_rate": float,
                    "validation_time": float
                }},
                "recommendations": ["suggestions", "for", "improvement"]
            }}
            """,
            expected_output="A JSON object with detailed validation results for all tested locators.",
            agent=agent,
        )

    def healing_orchestration_task(self, agent, healing_session_data: Dict[str, Any]) -> Task:
        """Task for orchestrating the complete healing workflow."""
        return Task(
            description=f"""
            Orchestrate the complete healing workflow by coordinating failure analysis, locator generation, and validation.
            
            Healing Session Data:
            {healing_session_data}
            
            --- ORCHESTRATION WORKFLOW ---
            
            **Phase 1: Failure Analysis**
            - Analyze the failure context to determine healability
            - Extract element context and failure details
            - Assess healing priority and feasibility
            
            **Phase 2: Locator Generation**
            - Generate multiple alternative locators based on analysis
            - Use different strategies and approaches
            - Prioritize stability and reliability
            
            **Phase 3: Validation**
            - Test each alternative locator against live session
            - Validate element properties and interactability
            - Select the best working alternative
            
            **Phase 4: Decision Making**
            - Determine if healing was successful
            - Select the optimal replacement locator
            - Generate healing recommendations
            
            --- DECISION CRITERIA ---
            
            **Success Criteria:**
            - At least one alternative locator works correctly
            - Selected locator has high confidence score (>0.7)
            - Element properties match expectations
            - Locator is stable and reliable
            
            **Failure Criteria:**
            - No alternative locators work
            - All locators have low confidence scores
            - Element properties don't match expectations
            - Healing attempts exceed timeout limits
            
            --- OUTPUT FORMAT ---
            
            You MUST respond with ONLY a valid JSON object containing:
            {{
                "healing_successful": boolean,
                "selected_locator": "best replacement locator or null",
                "selected_strategy": "strategy of selected locator",
                "confidence_score": float (0.0 to 1.0),
                "healing_summary": {{
                    "original_locator": "original failing locator",
                    "failure_reason": "why original failed",
                    "alternatives_tested": int,
                    "successful_alternatives": int,
                    "healing_approach": "description of approach used"
                }},
                "performance_metrics": {{
                    "total_time": float,
                    "analysis_time": float,
                    "generation_time": float,
                    "validation_time": float
                }},
                "recommendations": [
                    "specific recommendations for test improvement",
                    "suggestions for preventing similar failures",
                    "locator strategy recommendations"
                ],
                "next_actions": [
                    "what should be done next",
                    "follow-up actions needed"
                ]
            }}
            """,
            expected_output="A JSON object with complete healing workflow results and recommendations.",
            agent=agent,
        )