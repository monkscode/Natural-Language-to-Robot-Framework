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
            Generate multiple alternative locators for the failed element using comprehensive element fingerprinting.
            
            Failure Analysis:
            {failure_analysis}
            
            DOM Context (if available):
            {dom_context or "No DOM context provided - generate locators based on analysis"}
            
            --- ENHANCED LOCATOR GENERATION STRATEGY ---
            
            **Use the Enhanced Locator Tool** with these operations:
            
            1. **extract_properties**: Extract 17+ properties from the target element
               - Input: {{"dom_content": "<html>...</html>", "locator": "id=original-locator"}}
               - Returns: Comprehensive ElementProperties with tag, id, name, type, aria_label, visible_text, 
                 location, dimensions, neighbor_texts, parent_tag, sibling_tags, is_button, etc.
            
            2. **generate_locators**: Generate 8 alternative locators ranked by stability
               - Input: {{"dom_content": "<html>...</html>", "target_element_description": "Submit button"}}
               - Returns: 8 locators following BrowserStack hierarchy:
                 * Priority 1: ID locator (95% stability)
                 * Priority 2: Name locator (90% stability)
                 * Priority 3: Aria-label CSS (92% stability)
                 * Priority 4: Data-attribute CSS (88% stability)
                 * Priority 5: Class CSS (70% stability)
                 * Priority 6: Text-based XPath (85% stability)
                 * Priority 7: Relative XPath (65% stability)
                 * Priority 8: Absolute XPath (50% stability)
            
            3. **find_by_similarity**: Use Similo algorithm when direct locators fail
               - Input: {{"target_properties": {{...}}, "current_dom": "<html>...</html>", "threshold": 0.7}}
               - Returns: Best matching element with similarity score and recommended locator
               - Threshold guide: 0.85+ (strict), 0.7 (balanced), 0.55 (lenient)
            
            **Property Stability Weights (from research):**
            - Highly stable (2.70-2.95): id, name, aria_label, visible_text, is_button
            - Moderately stable (1.30-2.20): attributes, location, alt, area
            - Less stable (0.50-1.00): class_name, xpath, neighbor_texts
            
            **Generation Rules:**
            1. Start with extract_properties if original locator available
            2. Generate all 8 alternative locators using generate_locators
            3. If all alternatives fail, use find_by_similarity with threshold 0.7
            4. Return locators in priority order (1-8) for intelligent fallback
            5. Include stability scores and confidence levels for each
            
            --- OUTPUT FORMAT ---
            
            You MUST respond with ONLY a valid JSON object containing:
            {{
                "alternatives": [
                    {{
                        "locator": "robot framework locator string (e.g., 'id=submit-btn')",
                        "strategy": "id|name|css|xpath|link_text",
                        "priority": int (1-8, lower = try first),
                        "stability": float (0.0-1.0, higher = more stable),
                        "confidence": float (0.0 to 1.0),
                        "reasoning": "why this locator should work",
                        "fallback_level": "primary|secondary|tertiary"
                    }}
                ],
                "properties_extracted": {{
                    "tag": "element tag name",
                    "id": "element id if available",
                    "name": "element name if available",
                    "aria_label": "aria-label if available",
                    "visible_text": "element text content",
                    "total_properties": int
                }},
                "generation_strategy": "comprehensive_fingerprinting_with_similo",
                "similarity_matching_used": boolean,
                "best_match_score": float (if similarity matching used),
                "recommendations": [
                    "Try locators in priority order (1-8)",
                    "Primary locator has 95% stability",
                    "If all fail, adjust similarity threshold to 0.6"
                ]
            }}
            """,
            expected_output="A JSON object with 8 alternative locators ranked by priority and stability.",
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