"""
Workflow task processing module.

This module handles the execution of workflow tasks, including:
- Browser session management
- Agent initialization and execution
- Locator extraction and validation
- Result processing and metrics tracking
"""

import asyncio
import json
import logging
import re
import time
from typing import Dict, Any, List, Optional

# Import browser-use components
from browser_use import Agent

# Import local modules
from browser_service.config import config
from browser_service.browser import cleanup_browser_resources
from browser_service.locators import extract_and_validate_locators
from browser_service.prompts import build_workflow_prompt, build_system_prompt
from browser_service.agent import register_custom_actions
from browser_service.utils.json_parser import extract_json_for_element
from browser_service.utils.metrics import record_workflow_metrics
from src.backend.core.config import settings

logger = logging.getLogger(__name__)


def process_workflow_task(
    task_id: str,
    elements: List[Dict[str, Any]],
    url: str,
    user_query: str,
    session_config: Dict[str, Any],
    enable_custom_actions: Optional[bool] = None,
    tasks_dict: Optional[Dict[str, Dict[str, Any]]] = None
) -> None:
    """
    Process elements as a UNIFIED WORKFLOW in a single browser session.

    This is the primary processing function for ALL tasks. Instead of creating separate
    Agent instances for each element, this creates ONE Agent that performs the entire
    workflow: navigate → act → extract all locators in sequence.

    Benefits:
    - Single Agent session (optimal cost)
    - Context preserved across all actions
    - No "empty page" or navigation issues
    - Agent understands the complete workflow
    - Matches user intent naturally

    Args:
        task_id: Unique task identifier
        elements: List of element specs [{"id": "elem_1", "description": "...", "action": "..."}]
        url: Target URL
        user_query: Full user query for context (e.g., "search for shoes and get product name")
        session_config: Browser configuration
        enable_custom_actions: Optional flag to enable/disable custom actions (defaults to config value)
    """
    if tasks_dict is None:
        raise ValueError("tasks_dict parameter is required for task tracking")

    tasks_dict[task_id].update({
        "status": "running",
        "started_at": time.time(),
        "message": f"Processing {len(elements)} elements as unified workflow"
    })

    logger.info(f"🚀 Starting WORKFLOW MODE for task {task_id}")
    logger.info(f"   Elements: {len(elements)}")
    logger.info(f"   URL: {url}")
    logger.info(f"   Query: {user_query[:100]}...")

    # Capture enable_custom_actions parameter for use in async function
    # Default to config value if not provided
    if enable_custom_actions is None:
        enable_custom_actions_flag = settings.ENABLE_CUSTOM_ACTIONS
        logger.info(f"🔧 Using ENABLE_CUSTOM_ACTIONS from config: {enable_custom_actions_flag}")
    else:
        enable_custom_actions_flag = enable_custom_actions
        logger.info(f"🔧 Using ENABLE_CUSTOM_ACTIONS from API parameter: {enable_custom_actions_flag}")

    async def run_unified_workflow():
        """Execute the entire workflow in ONE Agent session."""
        from browser_use.browser.session import BrowserSession
        from browser_use.llm.google import ChatGoogle

        session = None
        connected_browser = None
        playwright_instance = None

        try:
            # Initialize browser session ONCE
            logger.info("🌐 Initializing browser session...")
            session = BrowserSession(
                headless=session_config.get("headless", False),
                viewport=None
            )

            # Parse user query to extract action parameters
            # Try multiple patterns to extract search term
            search_patterns = [
                r'search for ["\'](.*?)["\']',  # "search for 'shoes'"
                r'type ["\'](.*?)["\']',  # "type 'shoes'"
                r'input ["\'](.*?)["\']',  # "input 'shoes'"
                r'enter ["\'](.*?)["\']',  # "enter 'shoes'"
            ]

            search_term = None
            for pattern in search_patterns:
                search_match = re.search(pattern, user_query, re.IGNORECASE)
                if search_match:
                    search_term = search_match.group(1)
                    logger.info(
                        f"📝 Extracted search term: '{search_term}' using pattern: {pattern}")
                    break

            if not search_term:
                logger.warning(
                    f"⚠️ Could not extract search term from query: {user_query}")

            # Build unified workflow objective
            # SIMPLE, EXPLICIT APPROACH: Always extract locators in correct order
            workflow_steps = []
            workflow_steps.append(f"1. Navigate to {url}")

            step_num = 2

            # Separate elements by action type
            interactive_elements = [e for e in elements if e.get('action') in [
                'input', 'click']]
            result_elements = [e for e in elements if e.get('action') not in [
                'input', 'click']]

            logger.info(
                f"📊 Element breakdown: {len(interactive_elements)} interactive, {len(result_elements)} result elements")

            # Calculate dynamic max_steps based on workflow complexity
            # Formula: navigate(1) + extract_locators(all_elements) + perform_actions(interactive*2) + done(1) + buffer(3)
            dynamic_max_steps = 1 + \
                len(elements) + (len(interactive_elements) * 2) + 1 + 3
            logger.info(
                "📊 Dynamic max_steps calculation:")
            logger.info(
                "   Navigate: 1")
            logger.info(
                f"   Extract locators: {len(elements)} (one per element)")
            logger.info(
                f"   Perform actions: {len(interactive_elements) * 2} (2 steps per interactive element)")
            logger.info(
                "   Done + buffer: 4")
            logger.info(
                f"   Total max_steps: {dynamic_max_steps}")

            # PHASE 1: Extract locators for interactive elements BEFORE using them
            if interactive_elements:
                workflow_steps.append(
                    f"{step_num}. BEFORE performing any actions, extract validated locators for these elements on the CURRENT page:")
                for elem in interactive_elements:
                    workflow_steps.append(
                        f"   - {elem.get('id')}: {elem.get('description')}")
                step_num += 1

            # PHASE 2: Perform user actions using the extracted locators
            if search_term:
                for elem in interactive_elements:
                    elem_action = elem.get('action', '')
                    elem_id = elem.get('id')
                    elem_desc = elem.get('description', '')

                    if elem_action == 'input':
                        workflow_steps.append(
                            f"{step_num}. Type '{search_term}' into the element you just found ({elem_id})")
                        workflow_steps.append(
                            f"{step_num + 1}. Press Enter to submit the search")
                        workflow_steps.append(
                            f"{step_num + 2}. Wait 5 seconds for search results to load completely")
                        step_num += 3
                    elif elem_action == 'click':
                        workflow_steps.append(
                            f"{step_num}. Click on the element you just found ({elem_id}: '{elem_desc}')")
                        workflow_steps.append(
                            f"{step_num + 1}. Wait 3 seconds for the page to update")
                        step_num += 2

            # PHASE 3: Extract locators for result elements AFTER actions complete
            if result_elements:
                workflow_steps.append(
                    f"{step_num}. AFTER all actions are complete, extract validated locators for these elements on the RESULTS page:")
                for elem in result_elements:
                    workflow_steps.append(
                        f"   - {elem.get('id')}: {elem.get('description')}")
                step_num += 1

            # If no interactive elements, just extract all locators
            if not interactive_elements and not result_elements:
                workflow_steps.append(
                    f"{step_num}. Extract validated locators for all elements:")
                for elem in elements:
                    workflow_steps.append(
                        f"   - {elem.get('id')}: {elem.get('description')}")
                step_num += 1

            # ========================================
            # NEW APPROACH: Use Playwright's Built-in Methods
            # ========================================
            # Instead of embedding 2000+ lines of JavaScript in the prompt (causing LLM timeout),
            # we use a simplified prompt where the agent only finds elements and returns coordinates.
            # Then Python uses Playwright's built-in methods for locator extraction and F12-style validation.

            library_type = config.robot_library
            logger.info(
                f"🔧 Using Playwright built-in methods for {library_type} library")

            # Build workflow prompt (integrated function)
            # Default to custom action mode (smart locator strategy)
            # Will fall back to legacy mode if custom action registration fails
            unified_objective = build_workflow_prompt(
                user_query=user_query,
                url=url,
                elements=elements,
                library_type=library_type,
                include_custom_action=True  # Default: use smart locator strategy
            )

            logger.info("📝 Built simplified workflow objective")
            logger.info(f"   Elements to find: {len(elements)}")

            # Skip all the old JavaScript code and validation
            # The new approach doesn't need it - Playwright handles everything
            logger.info("📝 Built unified workflow objective")
            logger.info(f"   Total workflow steps: {len(workflow_steps)}")

            # ========================================
            # FEATURE FLAG: ENABLE_CUSTOM_ACTIONS
            # ========================================
            # Use feature flag from outer scope (already resolved to config or parameter value)
            # If False, skip custom action registration and use legacy workflow
            # Phase: Integration and Deployment | Requirements: 10.1, 10.3

            if enable_custom_actions_flag:
                logger.info("� Custom actions ENABLED")
                logger.info("   Mode: Smart locator strategy (custom action mode)")
            else:
                logger.info("🔧 Custom actions DISABLED")
                logger.info("   Mode: Legacy workflow (JavaScript validation)")

            # Build prompts based on feature flag
            unified_objective = build_workflow_prompt(
                user_query=user_query,
                url=url,
                elements=elements,
                library_type=library_type,
                include_custom_action=enable_custom_actions_flag
            )

            # Create Agent with prompts based on feature flag
            agent = Agent(
                task=unified_objective,
                browser_context=session,
                llm=ChatGoogle(
                    model=config.llm.google_model,
                    api_key=config.llm.google_api_key,
                    temperature=0.1
                ),
                use_vision=True,
                max_steps=dynamic_max_steps,
                system_prompt=build_system_prompt(include_custom_action=enable_custom_actions_flag)
            )

            # ========================================
            # REGISTER CUSTOM ACTIONS (if enabled)
            # ========================================
            # Register custom actions with the agent after creation.
            # The custom action will get the page object from browser_session during execution,
            # ensuring we use the SAME browser that's already open (no new browser instance needed).
            # This is the key strategy: validate locators using the existing browser_use browser.
            custom_actions_enabled = False

            if enable_custom_actions_flag:
                logger.info("🔧 Attempting to register custom actions...")
                # Pass None for page since the custom action will get it from browser_session during execution
                custom_actions_enabled = register_custom_actions(agent, page=None)

                if custom_actions_enabled:
                    logger.info("✅ Custom actions registered successfully")
                    logger.info("   Agent can now call find_unique_locator action")
                    logger.info("   Custom action will use the existing browser_use browser for validation")
                    logger.info("   Using smart locator strategy (custom action mode)")
                else:
                    logger.warning("⚠️ Custom action registration failed")
                    logger.warning("   Falling back to legacy workflow (JavaScript validation)")

                    # Fall back to legacy mode
                    unified_objective = build_workflow_prompt(
                        user_query=user_query,
                        url=url,
                        elements=elements,
                        library_type=library_type,
                        include_custom_action=False  # Fallback to legacy mode
                    )
                    agent.task = unified_objective
                    agent.system_prompt = build_system_prompt(include_custom_action=False)
                    logger.info("✅ Agent prompts updated with legacy workflow instructions")
            else:
                logger.info("⏭️ Skipping custom action registration (disabled via config)")
                logger.info("   Using legacy workflow mode")

            # Run the unified workflow
            logger.info("🤖 Starting unified Agent...")
            logger.info(
                "🤖 Using default ChatGoogle LLM (no rate limiting needed)")

            # Log available actions for debugging
            if hasattr(agent, 'tools') and agent.tools:
                if hasattr(agent.tools, 'registry') and hasattr(agent.tools.registry, '_registry'):
                    available_actions = list(agent.tools.registry._registry.keys())
                    logger.info(f"📋 Available custom actions: {available_actions}")
                else:
                    logger.info("📋 Tools registry structure unknown")
            else:
                logger.info("⚠️ Agent has no tools registered")

            start_time = time.time()
            agent_result = await agent.run()
            execution_time = time.time() - start_time

            logger.info(f"✅ Agent completed in {execution_time:.1f}s")

            # Check if agent actually used the custom action
            if custom_actions_enabled and hasattr(agent_result, 'history'):
                custom_action_calls = 0
                execute_js_calls = 0
                for step in agent_result.history:
                    if hasattr(step, 'action') and step.action:
                        action_name = str(step.action).lower()
                        if 'find_unique_locator' in action_name:
                            custom_action_calls += 1
                        elif 'execute_js' in action_name:
                            execute_js_calls += 1

                logger.info(f"📊 Action usage: find_unique_locator={custom_action_calls}, execute_js={execute_js_calls}")

                if custom_action_calls == 0 and execute_js_calls > 0:
                    logger.warning("⚠️ Agent used execute_js instead of find_unique_locator custom action!")
                    logger.warning("   This may indicate the custom action wasn't properly registered or visible to the agent")
                elif custom_action_calls > 0:
                    logger.info(f"✅ Agent successfully used find_unique_locator custom action {custom_action_calls} times")

            # ========================================
            # METRICS LOGGING: LLM Call Count
            # ========================================
            # Track LLM calls from agent history for cost tracking
            # Phase: Error Handling and Logging | Requirements: 6.1, 6.2, 6.3, 9.5
            llm_call_count = 0
            if hasattr(agent_result, 'history') and agent_result.history:
                # Count steps that involved LLM calls (agent actions)
                llm_call_count = len(agent_result.history)
                logger.info(f"📊 METRIC: Total LLM calls in workflow: {llm_call_count}")

            # Log custom action usage
            logger.info(f"📊 METRIC: Custom actions enabled: {custom_actions_enabled}")
            logger.info(f"📊 METRIC: Workflow execution time: {execution_time:.2f}s")

            # ========================================
            # EXTRACT LOCATORS USING PLAYWRIGHT'S BUILT-IN METHODS
            # ========================================

            def extract_coordinates_from_js_history(history, elements_list):
                """
                Fallback: Extract coordinates from JavaScript execution results in agent history.
                This handles cases where the agent executes JS to get coordinates but doesn't
                return them in the expected JSON format.
                """
                extracted_elements = []

                try:
                    import json

                    # Iterate through history to find execute_js actions
                    for step_idx, step in enumerate(history):
                        # Check if this step has action results
                        if hasattr(step, 'result') and step.result:
                            result_str = str(step.result)

                            # Look for JavaScript execution results with coordinates
                            # Pattern: Result: {"x":..., "y":..., "element_type":..., "visible_text":...}
                            if 'Result:' in result_str and '"x":' in result_str and '"y":' in result_str:
                                # Extract JSON after "Result:"
                                result_match = re.search(
                                    r'Result:\s*(\{[^}]*"x"[^}]*\})', result_str)
                                if result_match:
                                    try:
                                        coord_data = json.loads(
                                            result_match.group(1))

                                        # Try to match this to an element from our list
                                        # Use visible_text or element_type to match
                                        visible_text = coord_data.get(
                                            'visible_text', '').lower()
                                        element_type = coord_data.get(
                                            'element_type', '').lower()

                                        # Try to find matching element from our list
                                        matched_element = None
                                        for elem in elements_list:
                                            elem_desc = elem.get(
                                                'description', '').lower()
                                            elem_id = elem.get('id')

                                            # Check if already extracted
                                            if any(e.get('element_id') == elem_id for e in extracted_elements):
                                                continue

                                            # Match by description keywords in visible_text
                                            # or by element type
                                            if (visible_text and any(word in visible_text for word in elem_desc.split() if len(word) > 3)) or \
                                               (element_type and element_type in elem_desc):
                                                matched_element = elem
                                                break

                                        if matched_element:
                                            extracted_elements.append({
                                                'element_id': matched_element['id'],
                                                'found': True,
                                                'coordinates': {
                                                    'x': coord_data.get('x'),
                                                    'y': coord_data.get('y')
                                                },
                                                'element_type': element_type,
                                                'visible_text': coord_data.get('visible_text', '')
                                            })
                                            logger.info(
                                                f"   ✅ Matched JS result to element: {matched_element['id']}")
                                        else:
                                            # If we can't match, add it anyway with a generated ID
                                            # Use the first unmatched element
                                            unmatched = [e for e in elements_list if not any(
                                                ex.get('element_id') == e.get('id') for ex in extracted_elements)]
                                            if unmatched:
                                                elem = unmatched[0]
                                                extracted_elements.append({
                                                    'element_id': elem['id'],
                                                    'found': True,
                                                    'coordinates': {
                                                        'x': coord_data.get('x'),
                                                        'y': coord_data.get('y')
                                                    },
                                                    'element_type': element_type,
                                                    'visible_text': coord_data.get('visible_text', '')
                                                })
                                                logger.info(
                                                    f"   ⚠️ Could not match JS result, assigned to: {elem['id']}")

                                    except json.JSONDecodeError as e:
                                        logger.debug(
                                            f"   Failed to parse JS result JSON: {e}")
                                        continue

                    return extracted_elements

                except Exception as e:
                    logger.error(f"Error extracting from JS history: {e}")
                    return []

            def parse_coordinates_from_result(agent_result):
                """Extract element coordinates from agent result."""
                try:
                    final_result = ""
                    if hasattr(agent_result, 'final_result'):
                        final_result = str(agent_result.final_result())
                    elif hasattr(agent_result, 'history') and agent_result.history:
                        if len(agent_result.history) > 0:
                            last_step = agent_result.history[-1]
                            if hasattr(last_step, 'result'):
                                final_result = str(last_step.result)

                    logger.info(
                        f"📝 Agent final result (first 500 chars): {result[:500]}")

                    # Look for JSON with elements_found
                    json_match = re.search(
                        r'\{[\s\S]*"elements_found"[\s\S]*\}', final_result)
                    if json_match:
                        data = json.loads(json_match.group(0))
                        return data.get('elements_found', [])

                    logger.warning(
                        "Could not find elements_found JSON, trying fallback...")
                    return []

                except Exception as e:
                    logger.error(f"Error parsing coordinates: {e}")
                    return []

            elements_found = parse_coordinates_from_result(agent_result)
            logger.info(
                f"📍 Agent found {len(elements_found)} elements with coordinates")

            # FALLBACK: If no structured results, try to extract from JavaScript execution results in history
            if not elements_found and hasattr(agent_result, 'history'):
                logger.info(
                    "🔍 Attempting fallback: extracting coordinates from JavaScript execution history...")
                elements_found = extract_coordinates_from_js_history(
                    agent_result.history, elements)
                if elements_found:
                    logger.info(
                        f"✅ Fallback successful: extracted {len(elements_found)} elements from JS history")
                    for elem in elements_found:
                        coords = elem.get('coordinates', {})
                        logger.info(
                            f"   - {elem.get('element_id')}: coords=({coords.get('x')}, {coords.get('y')}), text=\"{elem.get('visible_text', '')[:50]}\"")
                else:
                    logger.error(
                        "❌ Fallback failed: no coordinates extracted from JS history")

            # Get the Playwright page from browser_use session
            # STRATEGY: Connect Playwright to browser_use's browser via CDP
            # This allows us to use the SAME browser for vision AND validation
            page = None

            try:
                logger.info(
                    "🔍 Attempting to access browser_use's browser for validation...")

                # Method 1: Try to get CDP URL from session
                cdp_url = None

                # Try session.cdp_url
                if hasattr(session, 'cdp_url'):
                    try:
                        cdp_url = session.cdp_url
                        if cdp_url:
                            logger.info(
                                f"✅ Found CDP URL from session.cdp_url: {cdp_url}")
                        else:
                            logger.debug("session.cdp_url is None")
                    except Exception as e:
                        logger.debug(f"Error accessing session.cdp_url: {e}")

                # Try cdp_client.url
                if not cdp_url and hasattr(session, 'cdp_client'):
                    try:
                        cdp_client = session.cdp_client
                        if hasattr(cdp_client, 'url'):
                            cdp_url = cdp_client.url
                            if cdp_url:
                                logger.info(
                                    f"✅ Found CDP URL from cdp_client.url: {cdp_url}")
                    except Exception as e:
                        logger.debug(f"Error accessing cdp_client.url: {e}")

                # Search all attributes
                if not cdp_url:
                    logger.info(
                        "🔍 Searching for CDP URL in session attributes...")
                    for attr in dir(session):
                        if not attr.startswith('_'):
                            try:
                                value = getattr(session, attr, None)
                                if value and isinstance(value, str) and 'ws://' in value and 'devtools' in value:
                                    cdp_url = value
                                    logger.info(
                                        f"✅ Found CDP URL in {attr}: {cdp_url}")
                                    break
                            except Exception:
                                pass

                # Method 2: If we have CDP URL, connect Playwright to browser_use's browser
                if cdp_url:
                    try:
                        from playwright.async_api import async_playwright

                        logger.info(
                            "🔌 Connecting Playwright to browser_use's browser via CDP...")
                        playwright_instance = await async_playwright().start()
                        connected_browser = await playwright_instance.chromium.connect_over_cdp(cdp_url)

                        # Get the active page from browser_use's browser
                        if connected_browser.contexts:
                            context = connected_browser.contexts[0]
                            logger.info(
                                f"✅ Found {len(connected_browser.contexts)} context(s)")

                            if context.pages:
                                page = context.pages[0]
                                logger.info(f"✅ Connected to browser_use's page! URL: {await page.url()}")
                            else:
                                logger.warning("⚠️ Context has no pages")
                        else:
                            logger.warning("⚠️ Browser has no contexts")

                    except Exception as e:
                        logger.error(
                            f"❌ Failed to connect Playwright via CDP: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())

                # Method 3: Fallback - try direct context access (old method)
                if not page:
                    logger.info("🔍 Trying fallback: direct context access...")
                    if hasattr(session, 'context') and session.context is not None:
                        context = session.context
                        logger.info(f"✅ Found context: {type(context)}")

                        if hasattr(context, 'pages'):
                            pages = context.pages
                            logger.info(f"✅ Context has {len(pages)} pages")
                            if pages and len(pages) > 0:
                                page = pages[0]
                                logger.info(
                                    "✅ Got page from session.context.pages[0]")

                if not page:
                    logger.warning("⚠️ Could not access Playwright page")
                    logger.info(f"   Session type: {type(session)}")
                    logger.info(
                        f"   Session attributes (first 20): {[attr for attr in dir(session) if not attr.startswith('_')][:20]}")
                    logger.info(
                        "   Will proceed without validation (trusting browser_use)")

            except Exception as e:
                logger.error(f"❌ Error accessing page: {e}")
                import traceback
                logger.error(traceback.format_exc())

            results_list = []
            workflow_completed = False

            if not page:
                logger.error("❌ No Playwright page available")
            else:
                # Extract and validate locators for each element using Playwright
                for elem_data in elements_found:
                    elem_id = elem_data.get('element_id')

                    # Find element description from original elements list
                    elem_desc = next(
                        (e.get('description')
                         for e in elements if e.get('id') == elem_id),
                        'Unknown element'
                    )

                    if elem_data.get('found') and elem_data.get('coordinates'):
                        coords = elem_data.get('coordinates')
                        logger.info(
                            f"🔍 Extracting locators for {elem_id}: {elem_desc}")
                        logger.info(
                            f"   Coordinates: ({coords.get('x')}, {coords.get('y')})")
                        logger.info(f"   Page available: {page is not None}")

                        # ========================================
                        # METRICS LOGGING: Per-Element Timing
                        # ========================================
                        # Track execution time for each element
                        # Phase: Error Handling and Logging | Requirements: 6.1, 6.2, 6.3, 9.5
                        element_start_time = time.time()

                        try:
                            # Use Playwright's built-in methods for locator extraction
                            locator_result = await extract_and_validate_locators(
                                page=page,
                                element_description=elem_desc,
                                element_coords=elem_data.get('coordinates'),
                                library_type=library_type
                            )

                            element_execution_time = time.time() - element_start_time

                            logger.info(
                                f"   Locator extraction completed: found={locator_result.get('found')}")

                            # ========================================
                            # METRICS LOGGING: Per-Element Metrics
                            # ========================================
                            logger.info(f"📊 METRIC [{elem_id}]: Execution time: {element_execution_time:.2f}s")
                            logger.info(f"📊 METRIC [{elem_id}]: Custom action used: {custom_actions_enabled}")
                            logger.info(f"📊 METRIC [{elem_id}]: Locator found: {locator_result.get('found')}")

                            # ========================================
                            # METRICS LOGGING: Per-Element Cost Estimation
                            # ========================================
                            # Estimate LLM calls for this element (rough estimate based on workflow)
                            # In custom action mode: ~4-6 calls per element
                            # In legacy mode: ~20-50 calls per element
                            estimated_cost_per_call = 0.0001  # Approximate cost per LLM call
                            estimated_llm_calls_for_element = 5 if custom_actions_enabled else 30
                            estimated_cost_for_element = estimated_llm_calls_for_element * estimated_cost_per_call

                            if settings.TRACK_LLM_COSTS:
                                logger.info(f"📊 METRIC [{elem_id}]: Estimated LLM calls: {estimated_llm_calls_for_element}")
                                logger.info(f"📊 METRIC [{elem_id}]: Estimated cost: ${estimated_cost_for_element:.6f}")

                            results_list.append({
                                'element_id': elem_id,
                                'description': elem_desc,
                                'found': locator_result.get('found'),
                                'best_locator': locator_result.get('best_locator'),
                                'all_locators': locator_result.get('all_locators', []),
                                'unique_locators': locator_result.get('unique_locators', []),
                                'element_info': locator_result.get('element_info', {}),
                                'validation_summary': locator_result.get('validation_summary', {}),
                                # Add validation data at result level
                                'validated': locator_result.get('validated', False),
                                'count': locator_result.get('count', 0),
                                'unique': locator_result.get('unique', False),
                                'valid': locator_result.get('valid', False),
                                'validation_method': locator_result.get('validation_method', 'playwright'),
                                # Add metrics data at result level
                                'metrics': {
                                    'execution_time': element_execution_time,
                                    'estimated_llm_calls': estimated_llm_calls_for_element,
                                    'estimated_cost': estimated_cost_for_element,
                                    'custom_action_used': custom_actions_enabled
                                }
                            })

                            if locator_result.get('found'):
                                logger.info(
                                    f"   ✅ Best locator: {locator_result.get('best_locator')}")
                                logger.info(
                                    f"   All locators count: {len(locator_result.get('all_locators', []))}")
                            else:
                                error_msg = locator_result.get(
                                    'error', 'Unknown error')
                                logger.error(
                                    f"   ❌ Locator extraction failed: {error_msg}")

                        except Exception as e:
                            element_execution_time = time.time() - element_start_time
                            logger.error(
                                f"   ❌ Error extracting locators for {elem_id}: {e}")

                            # Log metrics even for failed elements
                            if settings.TRACK_LLM_COSTS:
                                logger.info(f"📊 METRIC [{elem_id}]: Execution time: {element_execution_time:.2f}s (FAILED)")
                                logger.info(f"📊 METRIC [{elem_id}]: Custom action used: {custom_actions_enabled}")

                            results_list.append({
                                'element_id': elem_id,
                                'description': elem_desc,
                                'found': False,
                                'error': f'Locator extraction failed: {str(e)}',
                                # Add validation data for errors
                                'validated': False,
                                'count': 0,
                                'unique': False,
                                'valid': False,
                                'validation_method': 'playwright',
                                # Add metrics data for failed elements
                                'metrics': {
                                    'execution_time': element_execution_time,
                                    'estimated_llm_calls': 0,
                                    'estimated_cost': 0,
                                    'custom_action_used': custom_actions_enabled
                                }
                            })
                    else:
                        logger.warning(
                            f"⚠️ Element {elem_id} not found by agent")

                        # Log metrics for not found elements
                        if settings.TRACK_LLM_COSTS:
                            logger.info(f"📊 METRIC [{elem_id}]: Element not found by agent")
                            logger.info(f"📊 METRIC [{elem_id}]: Custom action used: {custom_actions_enabled}")

                        results_list.append({
                            'element_id': elem_id,
                            'description': elem_desc,
                            'found': False,
                            'error': 'Element not found by agent vision',
                            # Add validation data for not found elements
                            'validated': False,
                            'count': 0,
                            'unique': False,
                            'valid': False,
                            'validation_method': 'playwright',
                            # Add metrics data for not found elements
                            'metrics': {
                                'execution_time': 0,
                                'estimated_llm_calls': 0,
                                'estimated_cost': 0,
                                'custom_action_used': custom_actions_enabled
                            }
                        })

                workflow_completed = len(results_list) > 0

                # Log summary
                successful = sum(1 for r in results_list if r.get('found'))
                logger.info(
                    f"📊 Locator extraction complete: {successful}/{len(results_list)} elements found")

            # Continue with existing result processing if needed
            logger.info(
                f"📝 Workflow completed: {workflow_completed}, Results: {len(results_list)}")

            # Skip old JavaScript-based result parsing - we've already extracted locators using Playwright
            # The old logic below is kept for backward compatibility but won't be executed
            # since results_list is already populated

            # OLD PARSING LOGIC (SKIPPED) - removed dead code that referenced undefined variables

            # If no structured results, try to extract individual element results from history
            if not results_list:
                logger.warning(
                    "No structured workflow results, attempting to extract from history...")

                # APPROACH 1: Extract actual result content from agent history
                logger.info(
                    "   Approach 1: Extracting from agent history steps...")

                # Build a list of all result strings from history
                # CRITICAL: Try multiple ways to access the content to avoid double-escaping
                result_strings = []
                direct_results = []  # Store parsed results directly from tool execution

                # DEBUG: Check what attributes agent_result has
                logger.info(f"   🔍 agent_result type: {type(agent_result)}")
                logger.info(f"   🔍 agent_result has all_results: {hasattr(agent_result, 'all_results')}")
                logger.info(f"   🔍 agent_result has history: {hasattr(agent_result, 'history')}")

                # Strategy 1: Try all_results attribute
                if hasattr(agent_result, 'all_results') and agent_result.all_results:
                    logger.info(
                        f"   ✅ Found all_results with {len(agent_result.all_results)} items")
                    for idx, action_result in enumerate(agent_result.all_results):
                        # DEBUG: Log available attributes
                        logger.info(
                            f"   📋 all_results[{idx}] type: {type(action_result)}")
                        logger.info(
                            f"   📋 all_results[{idx}] has metadata: {hasattr(action_result, 'metadata')}")

                        # PRIORITY 1: Check for metadata attribute (custom actions)
                        if hasattr(action_result, 'metadata') and action_result.metadata:
                            logger.info(
                                f"   🔍 all_results[{idx}].metadata exists, type: {type(action_result.metadata)}")
                            if isinstance(action_result.metadata, dict):
                                logger.info(
                                    f"   🎯 all_results[{idx}].metadata is a dict! Direct access possible")
                                logger.info(
                                    f"   🔍 metadata keys: {list(action_result.metadata.keys())}")
                                if 'element_id' in action_result.metadata:
                                    direct_results.append(action_result.metadata)
                                    logger.info(
                                        f"   ✅ Found element_id in all_results[{idx}].metadata dict!")
                                    continue  # Skip string conversion
                                else:
                                    logger.warning(
                                        "   ⚠️ metadata exists but no element_id key")
                            else:
                                logger.warning(
                                    f"   ⚠️ metadata is not a dict: {type(action_result.metadata)}")
                        else:
                            logger.debug(
                                f"   all_results[{idx}] has no metadata or metadata is None")

                        # PRIORITY 2: Try multiple attribute names
                        content = None
                        if hasattr(action_result, 'extracted_content') and action_result.extracted_content:
                            content = action_result.extracted_content
                            logger.debug(
                                f"   Using extracted_content from all_results[{idx}]")
                        elif hasattr(action_result, 'content') and action_result.content:
                            content = action_result.content
                            logger.debug(
                                f"   Using content from all_results[{idx}]")
                        elif hasattr(action_result, 'result') and action_result.result:
                            # Check if result is a dict/object with direct access
                            if isinstance(action_result.result, dict):
                                logger.debug(
                                    f"   all_results[{idx}].result is a dict!")
                                direct_results.append(action_result.result)
                                continue  # Skip string conversion
                            content = str(action_result.result)
                            logger.debug(
                                f"   Using str(result) from all_results[{idx}]")

                        if content:
                            result_strings.append(content)
                            logger.debug(
                                f"   Collected content from all_results[{idx}]: {len(content)} chars")

                # Strategy 2: Try history attribute (MOST IMPORTANT for execute_js results)
                if hasattr(agent_result, 'history') and agent_result.history:
                    logger.info(
                        f"   ✅ Found history with {len(agent_result.history)} items")
                    for idx, step in enumerate(agent_result.history):
                        logger.info(f"   📋 history[{idx}] type: {type(step)}")
                        logger.info(f"   📋 history[{idx}] has result: {hasattr(step, 'result')}")

                        # PRIORITY 0: Check if step.result contains ActionResult objects with metadata
                        if hasattr(step, 'result') and step.result:
                            logger.info(f"   🔍 history[{idx}].result type: {type(step.result)}")

                            # step.result is a list of ActionResult objects
                            if isinstance(step.result, list):
                                logger.info(f"   🔍 history[{idx}].result is a list with {len(step.result)} items")
                                for result_idx, action_result in enumerate(step.result):
                                    logger.info(f"   🔍 history[{idx}].result[{result_idx}] type: {type(action_result)}")
                                    logger.info(f"   🔍 history[{idx}].result[{result_idx}] has metadata: {hasattr(action_result, 'metadata')}")

                                    if hasattr(action_result, 'metadata') and action_result.metadata:
                                        if isinstance(action_result.metadata, dict):
                                            logger.info(
                                                f"   🎯 history[{idx}].result[{result_idx}].metadata is a dict! Direct access possible")
                                            logger.info(
                                                f"   🔍 metadata keys: {list(action_result.metadata.keys())}")
                                            if 'element_id' in action_result.metadata:
                                                direct_results.append(action_result.metadata)
                                                logger.info(
                                                    f"   ✅ Found element_id in history[{idx}].result[{result_idx}].metadata dict!")
                                                # Don't break - there might be more ActionResults with metadata

                            # If result is not a list, check if it has metadata directly
                            elif hasattr(step.result, 'metadata') and step.result.metadata:
                                if isinstance(step.result.metadata, dict):
                                    logger.info(
                                        f"   🎯 history[{idx}].result.metadata is a dict! Direct access possible")
                                    logger.info(
                                        f"   🔍 metadata keys: {list(step.result.metadata.keys())}")
                                    if 'element_id' in step.result.metadata:
                                        direct_results.append(step.result.metadata)
                                        logger.info(
                                            f"   ✅ Found element_id in history[{idx}].result.metadata dict!")
                        # Check for tool_results in state (this is where execute_js output is stored)
                        if hasattr(step, 'state') and hasattr(step.state, 'tool_results'):
                            logger.debug(
                                f"   history[{idx}] has tool_results: {len(step.state.tool_results)} items")
                            for tool_idx, tool_result in enumerate(step.state.tool_results):
                                # DEBUG: Check what type tool_result is
                                logger.debug(
                                    f"   tool_result[{tool_idx}] type: {type(tool_result)}")
                                # First 10 attrs
                                logger.debug(
                                    f"   tool_result[{tool_idx}] attributes: {dir(tool_result)[:10]}...")

                                # Try to access result directly without str()
                                # PRIORITY 1: Check for metadata attribute (custom actions)
                                if hasattr(tool_result, 'metadata') and tool_result.metadata:
                                    if isinstance(tool_result.metadata, dict):
                                        logger.info(
                                            f"   🎯 tool_result[{tool_idx}].metadata is a dict! Direct access possible")
                                        if 'element_id' in tool_result.metadata:
                                            direct_results.append(tool_result.metadata)
                                            logger.info(
                                                "   ✅ Found element_id in tool_result.metadata dict!")
                                            continue  # Skip string conversion

                                # PRIORITY 2: Check if tool_result itself is a dict
                                if isinstance(tool_result, dict):
                                    logger.info(
                                        f"   🎯 tool_result[{tool_idx}] is a dict! Direct access possible")
                                    if 'element_id' in tool_result:
                                        direct_results.append(tool_result)
                                        logger.info(
                                            "   ✅ Found element_id in tool_result dict!")
                                        continue  # Skip string conversion

                                # PRIORITY 3: Check result attribute
                                elif hasattr(tool_result, 'result'):
                                    if isinstance(tool_result.result, dict):
                                        logger.info(
                                            f"   🎯 tool_result[{tool_idx}].result is a dict!")
                                        if 'element_id' in tool_result.result:
                                            direct_results.append(
                                                tool_result.result)
                                            logger.info(
                                                "   ✅ Found element_id in tool_result.result dict!")
                                            continue  # Skip string conversion
                                    else:
                                        # Add to string collection
                                        content = str(tool_result.result)
                                        if content and content not in result_strings:
                                            result_strings.append(content)
                                else:
                                    # Last resort: convert to string
                                    content = str(tool_result)
                                    if content and content not in result_strings:
                                        result_strings.append(content)

                        # Also try direct content attributes
                        content = None
                        if hasattr(step, 'extracted_content') and step.extracted_content:
                            content = step.extracted_content
                        elif hasattr(step, 'content') and step.content:
                            content = step.content
                        elif hasattr(step, 'result'):
                            if hasattr(step.result, 'extracted_content') and step.result.extracted_content:
                                content = step.result.extracted_content
                            elif hasattr(step.result, 'content') and step.result.content:
                                content = step.result.content
                            elif step.result:
                                content = str(step.result)
                                logger.debug(
                                    f"   Using str() for history[{idx}].result")

                        if content and content not in result_strings:
                            result_strings.append(content)
                            logger.debug(
                                f"   Collected content from history[{idx}]: {len(content)} chars")

                # Strategy 3: If still nothing, try converting entire agent_result to string as last resort
                if not result_strings and not direct_results:
                    logger.warning(
                        "   No content found via direct access, falling back to str(agent_result)")
                    result_strings.append(str(agent_result))

                # PRIORITY: If we found direct dict results, use them immediately!
                if direct_results:
                    logger.info(
                        f"   🎉 Found {len(direct_results)} direct dict results (NO PARSING NEEDED)!")
                    for direct_result in direct_results:
                        elem_id = direct_result.get('element_id')
                        if elem_id and direct_result.get('found'):
                            existing_idx = next(
                                (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                            if existing_idx is not None:
                                # Replace existing result (agent retry/correction)
                                old_locator = results_list[existing_idx].get(
                                    'best_locator')
                                new_locator = direct_result.get('best_locator')
                                logger.info(
                                    f"   🔄 Replacing {elem_id}: '{old_locator}' → '{new_locator}' (agent retry/correction)")
                                results_list[existing_idx] = direct_result
                            else:
                                # First occurrence, add it
                                results_list.append(direct_result)
                                logger.info(
                                    f"   ✅ Direct access: {elem_id} (best_locator: {direct_result.get('best_locator')})")

                    # If we got all elements via direct access, we're completely done!
                    if len(results_list) == len(elements):
                        logger.info(
                            f"   🏆 All {len(elements)} elements extracted via DIRECT ACCESS (fastest path)!")
                        # Skip all parsing - we have everything!
                        # Jump to re-ranking section

                # Combine all result strings
                full_result_str = "\n".join(result_strings)
                logger.info(
                    f"   Collected {len(result_strings)} result strings, total length: {len(full_result_str)} characters")

                # DEBUG: Show sample of result string to understand format
                logger.info("   📋 Result string sample (first 2000 chars):")
                logger.info(f"   {full_result_str[:2000]}")
                logger.info(f"   {'='*80}")

                # ROBUST EXTRACTION: Leverage "Result:" pattern from browser_use library
                # The browser_use library ALWAYS prints "Result: {json}" after JavaScript execution
                # This is the most reliable source of locator data
                logger.info(
                    "   🎯 Strategy: Extract from 'Result:' lines (most reliable)")

                # STRATEGY 1: Extract from "Result:" lines (MOST RELIABLE)
                # Pattern: "Result: {complete JSON object}"
                # The browser_use library prints this after every JavaScript execution
                def extract_from_result_lines(text):
                    """
                    Extract JSON from 'Result:' lines printed by browser_use.
                    This is the MOST RELIABLE method because:
                    1. Always printed by browser_use after JS execution
                    2. Contains complete, valid JSON
                    3. Has best_locator already selected (first unique locator)
                    4. No double-escaping issues
                    """
                    results = []
                    # Look for "Result: {" followed by JSON
                    # Find all Result: lines
                    lines = text.split('\n')
                    for line in lines:
                        if 'Result:' in line and 'element_id' in line:
                            # Extract everything after "Result:"
                            result_start = line.find('Result:')
                            if result_start != -1:
                                json_part = line[result_start + 7:].strip()

                                # Find complete JSON using brace matching
                                if json_part.startswith('{'):
                                    brace_count = 0
                                    in_string = False
                                    escape_next = False

                                    for i, char in enumerate(json_part):
                                        if escape_next:
                                            escape_next = False
                                            continue
                                        if char == '\\':
                                            escape_next = True
                                            continue
                                        if char == '"':
                                            in_string = not in_string
                                            continue
                                        if in_string:
                                            continue

                                        if char == '{':
                                            brace_count += 1
                                        elif char == '}':
                                            brace_count -= 1
                                            if brace_count == 0:
                                                json_str = json_part[:i+1]
                                                results.append(json_str)
                                                break

                    return results

                # STRATEGY 2: Extract any JSON with element_id (FALLBACK)
                def extract_all_element_jsons(text):
                    """Extract all JSON objects containing element_id from text."""
                    found_jsons = []
                    # Look for {"element_id": patterns
                    for pattern in ['"element_id":', "'element_id':"]:
                        pos = 0
                        while True:
                            pos = text.find(pattern, pos)
                            if pos == -1:
                                break

                            # Find the opening brace
                            brace_pos = text.rfind(
                                '{', max(0, pos - 50), pos + 20)
                            if brace_pos == -1:
                                pos += 1
                                continue

                            # Match braces to find complete JSON
                            brace_count = 0
                            in_string = False
                            escape_next = False

                            for i in range(brace_pos, min(len(text), brace_pos + 10000)):
                                char = text[i]

                                if escape_next:
                                    escape_next = False
                                    continue
                                if char == '\\':
                                    escape_next = True
                                    continue
                                if char == '"':
                                    in_string = not in_string
                                    continue
                                if in_string:
                                    continue

                                if char == '{':
                                    brace_count += 1
                                elif char == '}':
                                    brace_count -= 1
                                    if brace_count == 0:
                                        json_str = text[brace_pos:i+1]
                                        if json_str not in found_jsons:
                                            found_jsons.append(json_str)
                                        break

                            pos += 1

                    return found_jsons

                # Try Strategy 1 first (Result: lines)
                result_line_jsons = extract_from_result_lines(full_result_str)
                if result_line_jsons:
                    logger.info(
                        f"   ✅ Extracted {len(result_line_jsons)} JSON blocks from 'Result:' lines")
                    extracted_jsons = result_line_jsons
                else:
                    logger.warning(
                        "   ⚠️  No 'Result:' lines found, trying fallback extraction...")
                    # Try Strategy 2 (any JSON with element_id)
                    extracted_jsons = extract_all_element_jsons(
                        full_result_str)
                    if extracted_jsons:
                        logger.info(
                            f"   ✅ Extracted {len(extracted_jsons)} JSON blocks (fallback method)")
                    else:
                        logger.warning(
                            "   ⚠️  No JSON blocks with element_id found in output")

                # Add extracted JSONs to the result string for pattern matching
                if extracted_jsons:
                    full_result_str += "\n" + "\n".join(extracted_jsons)
                    logger.debug(
                        f"   Added {len(extracted_jsons)} JSON blocks to search string")

                    # OPTIMIZATION: Try to parse extracted JSONs directly
                    # This is faster and more reliable than pattern matching + extraction
                    logger.info(
                        "   🚀 Attempting direct JSON parsing (optimized path)...")
                    for json_str in extracted_jsons:
                        try:
                            parsed = json.loads(json_str)
                            elem_id = parsed.get('element_id')
                            if elem_id and parsed.get('found'):
                                # CRITICAL: Use validated locators from agent if available
                                # The agent now validates locators during execution (while browser is open)
                                # This is more reliable than validating after browser closes

                                # Initialize variables at the top level to avoid UnboundLocalError
                                dom_attrs = parsed.get('dom_attributes', {})
                                dom_id = parsed.get(
                                    'dom_id') or dom_attrs.get('id')
                                generated_locators = []

                                # Check if agent already validated locators
                                if 'locators' in parsed and parsed['locators']:
                                    # Agent provided locators - verify they were actually validated
                                    generated_locators = parsed['locators']
                                    logger.info(
                                        f"   📋 Received {len(generated_locators)} locators from agent")

                                    # Add priority field if missing and verify validation status
                                    priority_map = {
                                        'id': 1, 'data-testid': 2, 'name': 3, 'css-class': 7}

                                    actually_validated_count = 0
                                    for loc in generated_locators:
                                        if 'priority' not in loc:
                                            loc['priority'] = priority_map.get(
                                                loc.get('type'), 10)

                                        # CRITICAL: Only mark as valid if it's unique (count=1)
                                        # For test automation, only unique locators are usable
                                        if loc.get('validated') and 'count' in loc:
                                            # Has validation data from JavaScript
                                            count = loc.get('count', 0)
                                            loc['unique'] = (count == 1)
                                            # ONLY unique locators are valid for testing
                                            loc['valid'] = (count == 1)
                                            loc['validated'] = True
                                            actually_validated_count += 1

                                            if count == 1:
                                                status = "✅ VALID & UNIQUE"
                                            elif count == 0:
                                                status = "❌ NOT FOUND"
                                            else:
                                                status = f"❌ INVALID - {count} matches (not unique)"

                                            logger.info(
                                                f"      {loc['type']}: {loc['locator']} → {status} (agent-validated)")
                                        else:
                                            # No validation data - mark as unvalidated
                                            loc['validated'] = False
                                            loc['unique'] = False
                                            loc['valid'] = False
                                            logger.warning(
                                                f"      {loc['type']}: {loc['locator']} → ⚠️ No validation data")

                                    logger.info(
                                        f"   ✅ {actually_validated_count}/{len(generated_locators)} locators have validation data")
                                else:
                                    # Fallback: Generate locators from DOM attributes
                                    logger.info(
                                        "   ⚠️ No pre-validated locators, generating from DOM attributes...")

                                    # Priority 1: ID
                                    if dom_id:
                                        generated_locators.append({
                                            'type': 'id',
                                            'locator': f'id={dom_id}',
                                            'priority': 1,
                                            # Assume unique/valid but not validated yet
                                            'unique': None,  # Unknown until validated
                                            'valid': None,   # Unknown until validated
                                            'validated': False  # Not yet validated
                                        })

                                    # Priority 2: data-testid
                                    if dom_attrs.get('data-testid'):
                                        generated_locators.append({
                                            'type': 'data-testid',
                                            'locator': f'data-testid={dom_attrs["data-testid"]}',
                                            'priority': 2,
                                            'unique': None,
                                            'valid': None,
                                            'validated': False
                                        })

                                    # Priority 3: name
                                    if dom_attrs.get('name'):
                                        generated_locators.append({
                                            'type': 'name',
                                            'locator': f'name={dom_attrs["name"]}',
                                            'priority': 3,
                                            'unique': None,
                                            'valid': None,
                                            'validated': False
                                        })

                                    # Priority 4: CSS class (if available)
                                    if dom_attrs.get('class'):
                                        first_class = dom_attrs['class'].split(
                                        )[0] if dom_attrs['class'] else None
                                        if first_class:
                                            element_type = parsed.get(
                                                'element_type', 'div')
                                            generated_locators.append({
                                                'type': 'css-class',
                                                'locator': f'{element_type}.{first_class}',
                                                'priority': 7,
                                                'unique': None,
                                                'valid': None,
                                                'validated': False
                                            })

                                # VALIDATION: If Playwright page is available, validate locators
                                # This confirms uniqueness and that the locator actually works
                                if page:
                                    logger.info(
                                        f"   🔍 Validating {len(generated_locators)} locators for {elem_id}...")
                                    for loc in generated_locators:
                                        # Skip if already validated by agent
                                        if loc.get('validated') and 'count' in loc:
                                            logger.info(
                                                f"      {loc['type']}: {loc['locator']} → Already validated by agent")
                                            continue

                                        try:
                                            # Use Playwright to count matches
                                            count = await page.locator(loc['locator']).count()
                                            loc['count'] = count
                                            loc['unique'] = (count == 1)
                                            # ONLY unique locators are valid for testing
                                            loc['valid'] = (count == 1)
                                            # Successfully validated
                                            loc['validated'] = True

                                            if count == 1:
                                                status = "✅ VALID & UNIQUE"
                                            elif count == 0:
                                                status = "❌ NOT FOUND"
                                            else:
                                                status = f"❌ INVALID - {count} matches (not unique)"

                                            logger.info(
                                                f"      {loc['type']}: {loc['locator']} → {status} (playwright-validated)")
                                        except Exception as e:
                                            # Validation attempt failed due to technical error (invalid syntax, etc.)
                                            logger.warning(
                                                f"      ❌ {loc['type']}: {loc['locator']} → Validation error: {e}")
                                            # Unknown count due to error
                                            loc['count'] = None
                                            loc['unique'] = False
                                            loc['valid'] = False
                                            # Could not validate due to error
                                            loc['validated'] = False
                                            # Store error for debugging
                                            loc['validation_error'] = str(e)
                                else:
                                    logger.info(
                                        f"   ⚠️ Page not available, skipping validation for {elem_id} (trusting browser_use)")

                                # Select best locator - ONLY use validated, unique, valid locators
                                # valid=True means count=1 (unique and usable for testing)
                                validated_unique = [loc for loc in generated_locators if loc.get(
                                    'validated') and loc.get('unique') and loc.get('valid')]

                                if validated_unique:
                                    # Found valid unique locators - select best by priority
                                    best_locator = sorted(validated_unique, key=lambda x: x['priority'])[
                                        0]['locator']
                                    logger.info(
                                        f"   ✅ Selected VALID unique locator: {best_locator}")
                                else:
                                    # No valid unique locators found - try smart locator finder
                                    best_locator = None

                                    # Log why we couldn't find a valid locator
                                    if generated_locators:
                                        non_unique = [loc for loc in generated_locators if loc.get(
                                            'validated') and loc.get('count', 0) > 1]
                                        not_found = [loc for loc in generated_locators if loc.get(
                                            'validated') and loc.get('count', 0) == 0]
                                        not_validated = [
                                            loc for loc in generated_locators if not loc.get('validated')]

                                        if non_unique:
                                            logger.error(
                                                f"   ❌ No valid locator: {len(non_unique)} locators are not unique")
                                        if not_found:
                                            logger.error(
                                                f"   ❌ No valid locator: {len(not_found)} locators not found on page")
                                        if not_validated:
                                            logger.warning(
                                                f"   ⚠️ {len(not_validated)} locators were not validated")
                                    else:
                                        logger.error(
                                            f"   ❌ No locators generated for {elem_id}")

                                    # SMART FALLBACK: If we have coordinates and page, try systematic locator finding
                                    if page and parsed.get('coordinates'):
                                        coords = parsed.get('coordinates', {})
                                        if coords.get('x') and coords.get('y'):
                                            logger.info(
                                                f"   🎯 Attempting smart locator finder at coordinates ({coords['x']}, {coords['y']})")
                                            try:
                                                from tools.smart_locator_finder import find_unique_locator_at_coordinates

                                                elem_desc = next(
                                                    (e.get('description') for e in elements if e.get('id') == elem_id),
                                                    'Unknown element'
                                                )

                                                smart_result = await find_unique_locator_at_coordinates(
                                                    page=page,
                                                    x=coords['x'],
                                                    y=coords['y'],
                                                    element_id=elem_id,
                                                    element_description=elem_desc,
                                                    library_type=library_type  # Pass library type from outer scope
                                                )

                                                if smart_result.get('found') and smart_result.get('best_locator'):
                                                    # Smart finder found a unique locator!
                                                    best_locator = smart_result['best_locator']
                                                    generated_locators = smart_result['all_locators']
                                                    logger.info(
                                                        f"   ✅ Smart finder found unique locator: {best_locator}")
                                                else:
                                                    logger.error(
                                                        "   ❌ Smart finder could not find unique locator")
                                            except Exception as e:
                                                logger.error(
                                                    f"   ❌ Smart locator finder error: {e}")
                                                import traceback
                                                logger.debug(traceback.format_exc())

                                # Find element description
                                elem_desc = next(
                                    (e.get('description')
                                     for e in elements if e.get('id') == elem_id),
                                    'Unknown element'
                                )

                                # Build result with locators
                                result = {
                                    'element_id': elem_id,
                                    'description': elem_desc,
                                    'found': True,
                                    'best_locator': best_locator,
                                    'all_locators': generated_locators,
                                    'element_info': {
                                        'id': dom_id,
                                        'tagName': parsed.get('element_type', ''),
                                        'text': parsed.get('visible_text', ''),
                                        'className': dom_attrs.get('class', ''),
                                        'name': dom_attrs.get('name', ''),
                                        'testId': dom_attrs.get('data-testid', '')
                                    },
                                    'coordinates': parsed.get('coordinates', {}),
                                    'validation_summary': {
                                        'total_generated': len(generated_locators),
                                        'valid': sum(1 for loc in generated_locators if loc.get('valid')),
                                        'unique': sum(1 for loc in generated_locators if loc.get('unique')),
                                        'validated': sum(1 for loc in generated_locators if loc.get('validated')),
                                        'best_type': generated_locators[0]['type'] if generated_locators else None
                                    }
                                }

                                # Check if we already have this element
                                existing_idx = next(
                                    (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                                if existing_idx is not None:
                                    # Replace existing result (agent retry/correction)
                                    old_locator = results_list[existing_idx].get(
                                        'best_locator')
                                    logger.info(
                                        f"   🔄 Replacing {elem_id}: '{old_locator}' → '{best_locator}' (agent retry/correction)")
                                    results_list[existing_idx] = result
                                else:
                                    # First occurrence, add it
                                    results_list.append(result)
                                    logger.info(
                                        f"   ✅ Directly parsed and added {elem_id} (best_locator: {best_locator})")
                        except json.JSONDecodeError as e:
                            logger.debug(
                                f"   Failed to parse JSON directly: {e}")
                            # Will fall back to pattern matching below

                    # If we got all elements via direct parsing, we're done!
                    if len(results_list) == len(elements):
                        logger.info(
                            f"   🎉 All {len(elements)} elements extracted via direct JSON parsing!")
                        # Skip pattern matching - we have everything we need

                for elem in elements:
                    elem_id = elem.get('id')
                    logger.info(f"   🔍 Looking for {elem_id}...")

                    # Check multiple patterns (with and without space after colon)
                    patterns_to_check = [
                        # No space (common in minified JSON)
                        f'"element_id":"{elem_id}"',
                        f'"element_id": "{elem_id}"',  # With space
                        f"'element_id':'{elem_id}'",  # Single quotes, no space
                        # Single quotes, with space
                        f"'element_id': '{elem_id}'"
                    ]

                    # DEBUG: Show which patterns we're checking
                    logger.info(
                        f"   📝 Checking {len(patterns_to_check)} patterns:")
                    for idx, pattern in enumerate(patterns_to_check, 1):
                        is_found = pattern in full_result_str
                        status = "✅ FOUND" if is_found else "❌ Not found"
                        logger.info(f"      {idx}. '{pattern}' -> {status}")

                    found = any(
                        pattern in full_result_str for pattern in patterns_to_check)

                    # DEBUG: Check if elem_id appears ANYWHERE in the string (any format)
                    if not found:
                        if elem_id in full_result_str:
                            logger.warning(
                                f"   ⚠️  '{elem_id}' exists in result but pattern didn't match!")
                            logger.warning(
                                f"   💡 Searching for context around '{elem_id}'...")
                            # Find where elem_id appears and show context
                            pos = full_result_str.find(elem_id)
                            if pos != -1:
                                start = max(0, pos - 100)
                                end = min(len(full_result_str), pos + 100)
                                context = full_result_str[start:end]
                                logger.warning(f"   Context: ...{context}...")
                        else:
                            logger.warning(
                                f"   ❌ '{elem_id}' does not appear ANYWHERE in result string")

                    if found:
                        logger.info(f"   Found '{elem_id}' in result string!")
                        try:
                            elem_data = extract_json_for_element(
                                full_result_str, elem_id)
                            if elem_data:
                                logger.info(
                                    f"   Extracted JSON for {elem_id}: found={elem_data.get('found')}")
                                if elem_data.get('found'):
                                    existing_idx = next(
                                        (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                                    if existing_idx is not None:
                                        # Check if we should replace
                                        old_result = results_list[existing_idx]
                                        old_locator = old_result.get(
                                            'best_locator')
                                        new_locator = elem_data.get(
                                            'best_locator')

                                        # CRITICAL: Don't replace if old has locators but new doesn't
                                        if old_locator and not new_locator:
                                            logger.info(
                                                f"   ⚠️ Skipping replacement for {elem_id}: old has locator '{old_locator}', new doesn't")
                                        else:
                                            # Replace existing result (agent retry/correction)
                                            logger.info(
                                                f"   🔄 Replacing {elem_id}: '{old_locator}' → '{new_locator}' (agent retry/correction)")
                                            results_list[existing_idx] = elem_data
                                    else:
                                        # First occurrence, add it
                                        results_list.append(elem_data)
                                        logger.info(
                                            f"   ✅ Extracted {elem_id} from full result string")
                                else:
                                    logger.warning(
                                        f"   {elem_id} found but 'found' is False")
                            else:
                                logger.warning(
                                    f"   extract_json_for_element returned None for {elem_id}")
                        except Exception as e:
                            logger.error(
                                f"   Exception extracting {elem_id}: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                    else:
                        logger.warning(
                            f"   '{elem_id}' not found in result string")

                # APPROACH 2: Check agent history structure
                if not results_list and hasattr(agent_result, 'history'):
                    logger.info(
                        f"   Approach 2: Checking agent history ({len(agent_result.history)} steps)...")

                    for step_idx, step in enumerate(agent_result.history):
                        # PRIORITY 1: Try to access metadata directly from ActionResult (for custom actions)
                        # This avoids the string conversion issue where Python dicts use single quotes
                        if hasattr(step, 'result'):
                            result_obj = step.result
                            # Check if result is an ActionResult with metadata
                            if hasattr(result_obj, 'metadata') and result_obj.metadata:
                                metadata = result_obj.metadata
                                # Check if this metadata is for one of our elements
                                if isinstance(metadata, dict) and 'element_id' in metadata:
                                    elem_id = metadata.get('element_id')
                                    if elem_id and any(e.get('id') == elem_id for e in elements):
                                        if metadata.get('found'):
                                            if not any(r.get('element_id') == elem_id for r in results_list):
                                                results_list.append(metadata)
                                                logger.info(
                                                    f"   ✅ Extracted {elem_id} from step {step_idx} metadata (direct access)")
                                                continue  # Skip string conversion for this step

                        # PRIORITY 2: Convert entire step to string (fallback)
                        step_str = str(step)

                        # Check for JavaScript execution results
                        if 'execute_js' in step_str or 'element_id' in step_str:
                            logger.debug(
                                f"   Step {step_idx} contains execute_js or element_id")

                            for elem in elements:
                                elem_id = elem.get('id')
                                if f'"element_id": "{elem_id}"' in step_str:
                                    try:
                                        elem_data = extract_json_for_element(
                                            step_str, elem_id)
                                        if elem_data and elem_data.get('found'):
                                            if not any(r.get('element_id') == elem_id for r in results_list):
                                                results_list.append(elem_data)
                                                logger.info(
                                                    f"   ✅ Extracted {elem_id} from step {step_idx}")
                                    except Exception as e:
                                        logger.debug(
                                            f"   Failed to extract {elem_id} from step {step_idx}: {e}")

                        # Also check specific attributes if they exist
                        if hasattr(step, 'state') and hasattr(step.state, 'tool_results'):
                            for tool_result in step.state.tool_results:
                                result_str = str(tool_result)
                                for elem in elements:
                                    elem_id = elem.get('id')
                                    if f'"element_id": "{elem_id}"' in result_str:
                                        try:
                                            elem_data = extract_json_for_element(
                                                result_str, elem_id)
                                            if elem_data and elem_data.get('found'):
                                                if not any(r.get('element_id') == elem_id for r in results_list):
                                                    results_list.append(
                                                        elem_data)
                                                    logger.info(
                                                        f"   ✅ Extracted {elem_id} from tool_results")
                                        except Exception as e:
                                            logger.debug(f"   Failed: {e}")

                        if hasattr(step, 'result'):
                            result_str = str(step.result)
                            for elem in elements:
                                elem_id = elem.get('id')
                                if f'"element_id": "{elem_id}"' in result_str:
                                    try:
                                        elem_data = extract_json_for_element(
                                            result_str, elem_id)
                                        if elem_data and elem_data.get('found'):
                                            if not any(r.get('element_id') == elem_id for r in results_list):
                                                results_list.append(elem_data)
                                                logger.info(
                                                    f"   ✅ Extracted {elem_id} from step.result")
                                    except Exception as e:
                                        logger.debug(f"   Failed: {e}")

            # If still no results, create default "not found" entries
            if not results_list:
                logger.error(
                    "Could not extract any element results from workflow")
                results_list = [
                    {
                        "element_id": elem.get('id'),
                        "description": elem.get('description'),
                        "found": False,
                        "error": "Could not extract from workflow result",
                        # Add validation data for not found elements
                        "validated": False,
                        "count": 0,
                        "unique": False,
                        "valid": False,
                        "validation_method": "playwright",
                        # Add metrics data for not found elements
                        "metrics": {
                            "execution_time": 0,
                            "estimated_llm_calls": 0,
                            "estimated_cost": 0,
                            "custom_action_used": custom_actions_enabled
                        }
                    }
                    for elem in elements
                ]

            # ========================================
            # PHASE 2: POST-PROCESS LOCATOR RE-RANKING
            # ========================================
            # Re-rank locators by quality to ensure best_locator is actually the best
            def score_locator(locator_obj):
                """
                Score locator based on robustness and stability.
                Higher score = better locator.

                STRICT SIX-TIER PRIORITY SYSTEM:
                ================================
                Tier 1 (90-100): Native Attributes - Most stable, browser-native lookups
                    - ID: 100 (best possible - unique, fast, stable)
                    - data-testid: 98 (designed specifically for testing)
                    - name: 96 (semantic, stable for forms)

                Tier 2 (70-89): Semantic Attributes - Accessibility-focused, stable
                    - aria-label: 88 (accessibility attribute, semantic)
                    - title: 85 (semantic attribute, descriptive)

                Tier 3 (50-69): Content-Based - Can change with content updates
                    - text: 65 (content-based, can change)
                    - role: 60 (Playwright-specific, semantic)

                Tier 4 (40-55): Fallback Strategies - Advanced strategies when basic attributes unavailable
                    - parent-id-xpath: 55 (anchored to parent ID, stable)
                    - nth-child: 50 (position-based, moderately stable)
                    - text-xpath: 48 (exact text match, more specific)
                    - attribute-combo: 45 (multiple attributes for uniqueness)

                Tier 5 (30-39): CSS Selectors - Styling-based, can change
                    - CSS with ID: 45 (should use id= instead)
                    - CSS with attribute: 40 (better than class)
                    - Regular CSS class: 35 (styling can change)
                    - Auto-generated class: 32 (very fragile)

                Tier 6 (0-29): XPath - LAST RESORT, fragile, breaks with DOM changes
                    - XPath with ID: 28 (should use id= instead!)
                    - XPath with data-testid: 26 (should use data-testid= instead!)
                    - XPath with semantic attrs: 24 (should use direct attribute)
                    - Text-based XPath: 20 (content can change)
                    - Structural XPath: 10-18 (very fragile, breaks easily)

                Clear score gaps between tiers prevent ties and ensure strict priority.
                """
                locator = locator_obj.get('locator', '')
                locator_type = locator_obj.get('type', '')

                # ========================================
                # TIER 1: NATIVE ATTRIBUTES (90-100)
                # ========================================
                # These are the most stable locators - browser-native lookups
                # that are fast, unique, and rarely change

                if locator_type == 'id' or locator.startswith('id='):
                    return 100  # Best possible - unique, fast, stable

                if locator_type == 'data-testid' or 'data-testid=' in locator:
                    return 98  # Designed specifically for testing

                if locator_type == 'name' or locator.startswith('name='):
                    return 96  # Semantic, stable for form elements

                # ========================================
                # TIER 2: SEMANTIC ATTRIBUTES (70-89)
                # ========================================
                # Accessibility-focused attributes that are semantic and stable

                if locator_type == 'aria-label' or 'aria-label=' in locator:
                    return 88  # Accessibility-focused, semantic

                if '@title=' in locator or locator_type == 'title':
                    return 85  # Semantic attribute, descriptive

                # ========================================
                # TIER 3: CONTENT-BASED (50-69)
                # ========================================
                # Locators based on visible content - can change with content updates

                if locator_type == 'text' or 'text=' in locator:
                    return 65  # Content-based, can change with text updates

                if locator_type == 'role' or 'role=' in locator:
                    return 60  # Playwright-specific, semantic but content-dependent

                # ========================================
                # TIER 4: CSS SELECTORS (30-49)
                # ========================================
                # Styling-based selectors - can change when CSS is refactored

                if locator_type == 'css' or locator.startswith('css='):
                    css_selector = locator.replace('css=', '')

                    if '#' in css_selector:
                        return 45  # CSS with ID (should use id= instead!)

                    if '[' in css_selector:
                        return 40  # CSS with attribute selector

                    # Check for auto-generated classes (very fragile)
                    if re.search(r'[_][0-9a-zA-Z]{5,}', css_selector):
                        return 32  # Auto-generated class (very fragile)

                    return 35  # Regular CSS class (styling can change)

                # ========================================
                # TIER 4.5: FALLBACK STRATEGIES (40-55)
                # ========================================
                # Advanced fallback strategies when basic attributes don't exist
                # These are better than generic CSS/XPath but not as good as native attributes

                # Parent ID + Relative XPath - anchored to stable ID
                if locator_type == 'parent-id-xpath':
                    return 55  # Anchored to parent ID (stable), but uses XPath

                # Nth-child selector - position-based, moderately stable
                if locator_type == 'nth-child':
                    return 50  # Position-based, can break if siblings change

                # Text-based XPath with exact match - better than generic XPath
                if locator_type == 'text-xpath':
                    return 48  # Text-based but exact match (more specific)

                # Attribute combination - multiple attributes for uniqueness
                if locator_type == 'attribute-combo':
                    # Multiple attributes (more stable than single class)
                    return 45

                # ========================================
                # TIER 5: XPATH - LAST RESORT (0-29)
                # ========================================
                # XPath locators are fragile and break when DOM structure changes
                # They should ONLY be used when no better option exists
                # Even "good" XPath gets low scores to enforce this priority

                if 'xpath' in locator_type or locator.startswith('//') or locator.startswith('xpath='):
                    # XPath with ID - should use id= instead!
                    if '@id=' in locator:
                        return 28  # Should use id= locator instead

                    # XPath with data-testid - should use data-testid= instead!
                    if '@data-testid=' in locator or '@data-test=' in locator:
                        return 26  # Should use data-testid= locator instead

                    # XPath with semantic attributes - should use direct attribute
                    if '@aria-label=' in locator or '@title=' in locator:
                        return 24  # Should use direct attribute locator

                    # Text-based XPath - content can change
                    if 'text()=' in locator or 'contains(text()' in locator:
                        return 20  # Content-based, can change

                    # Structural XPath (worst) - lots of [1], [2], etc.
                    # These break easily when DOM structure changes
                    index_count = locator.count(
                        '[1]') + locator.count('[2]') + locator.count('[3]')
                    if index_count >= 3:
                        return 10  # Very structural (extremely fragile)
                    elif index_count >= 2:
                        return 15  # Somewhat structural (fragile)

                    return 18  # Default XPath (still fragile)

                # Unknown/default - below Tier 4
                return 25

            logger.info("🔄 Re-ranking locators by quality score...")
            re_ranked_count = 0

            for result in results_list:
                if not result.get('found', False):
                    continue

                all_locators = result.get('all_locators', [])
                if not all_locators:
                    continue

                # Score each locator
                scored_locators = []
                for loc in all_locators:
                    score = score_locator(loc)
                    scored_locators.append({
                        **loc,
                        'quality_score': score
                    })

                # Sort by score (highest first)
                scored_locators.sort(
                    key=lambda x: x['quality_score'], reverse=True)

                # Log top 3 locators with their scores for debugging
                element_id = result.get('element_id', 'unknown')
                logger.info(f"📊 Locator Scores for {element_id}:")
                for i, loc in enumerate(scored_locators[:3]):  # Show top 3
                    locator_str = loc['locator'][:50]  # Truncate long locators
                    if i == 0:
                        # First locator is the selected best
                        logger.info(
                            f"   {loc['quality_score']:3d} - {loc['type']:15s} - {locator_str} ⭐ SELECTED AS BEST")
                        # Log warning if XPath is selected as best
                        if loc['type'] == 'xpath' or loc['locator'].startswith('xpath=') or loc['locator'].startswith('//'):
                            logger.warning(
                                "   ⚠️  XPath used as fallback - no ID, data-testid, name, or aria-label available")
                    else:
                        logger.info(
                            f"   {loc['quality_score']:3d} - {loc['type']:15s} - {locator_str}")

                # Update result with re-ranked locators
                old_best = result.get('best_locator', '')
                new_best = scored_locators[0]['locator'] if scored_locators else old_best

                if old_best != new_best:
                    logger.info(
                        f"   ✨ {result.get('element_id')}: Upgraded locator")
                    logger.info(
                        f"      OLD: {old_best} (score: {score_locator({'locator': old_best})})")
                    logger.info(
                        f"      NEW: {new_best} (score: {scored_locators[0]['quality_score']})")
                    re_ranked_count += 1

                result['best_locator'] = new_best
                result['all_locators'] = scored_locators

            logger.info(
                f"✅ Re-ranking complete: {re_ranked_count}/{len(results_list)} elements upgraded")

            # ========================================
            # RESULTS VALIDATION - Verify quality_score is present
            # ========================================
            logger.info("🔍 Validating results before return...")
            for result in results_list:
                elem_id = result.get('element_id')
                found = result.get('found')
                best_locator = result.get('best_locator', 'N/A')
                all_locators = result.get('all_locators', [])

                if found:
                    # Check if locators have quality_score
                    has_scores = all(loc.get('quality_score')
                                     is not None for loc in all_locators)
                    logger.info(
                        f"   ✅ {elem_id}: {best_locator} ({len(all_locators)} locators, scored={has_scores})")

                    if not has_scores and all_locators:
                        logger.warning(
                            f"   ⚠️ {elem_id}: Some locators missing quality_score!")
                else:
                    error = result.get('error', 'Unknown')
                    logger.error(f"   ❌ {elem_id}: {error}")
            # ========================================

            # ========================================
            # LOCATOR PRIORITY VALIDATION CHECK
            # ========================================
            # Verify that elements with ID attributes use ID locators
            # This catches cases where the scoring system may have failed
            # or where XPath/other locators were incorrectly prioritized
            logger.info("🔍 Running locator priority validation check...")
            validation_violations = 0

            for result in results_list:
                if not result.get('found', False):
                    continue

                element_info = result.get('element_info', {})
                element_id_attr = element_info.get('id', '').strip()
                best_locator = result.get('best_locator', '')
                all_locators = result.get('all_locators', [])
                elem_id = result.get('element_id', 'unknown')

                # Check if element has ID attribute but best_locator is not ID type
                if element_id_attr and element_id_attr != '':
                    # Determine if best_locator is an ID locator
                    is_id_locator = best_locator.startswith('id=')

                    if not is_id_locator:
                        # PRIORITY VIOLATION DETECTED
                        logger.error(f"❌ PRIORITY VIOLATION: {elem_id}")
                        logger.error(
                            f"   Element has ID attribute: '{element_id_attr}'")
                        logger.error(f"   But best_locator is: {best_locator}")
                        validation_violations += 1

                        # Search for ID locator in all_locators list
                        id_locator = None
                        id_locator_index = None
                        for idx, loc in enumerate(all_locators):
                            loc_str = loc.get('locator', '')
                            if loc_str.startswith('id='):
                                id_locator = loc
                                id_locator_index = idx
                                break

                        if id_locator:
                            # Automatically correct by forcing ID locator to be best_locator
                            logger.info(
                                f"   🔧 Forcing ID locator: {id_locator['locator']}")

                            # Move ID locator to first position
                            all_locators.pop(id_locator_index)
                            all_locators.insert(0, id_locator)

                            # Update best_locator
                            result['best_locator'] = id_locator['locator']
                            result['all_locators'] = all_locators

                            logger.info(
                                f"   ✅ Corrected: {elem_id} now uses ID locator")
                        else:
                            # ID locator not found in list - this is a critical issue
                            logger.error(
                                "   ⚠️  CRITICAL: ID locator not found in all_locators list!")
                            logger.error(
                                f"   Element ID attribute: '{element_id_attr}'")
                            logger.error(
                                f"   Available locators: {[loc.get('type') for loc in all_locators]}")
                            logger.error(
                                "   This indicates a problem with locator generation")

            if validation_violations > 0:
                logger.warning(
                    f"⚠️  Validation found {validation_violations} priority violations (corrected)")
            else:
                logger.info(
                    "✅ Validation passed: All elements with ID use ID locators")
            # ========================================

            # Calculate metrics
            successful = sum(1 for r in results_list if r.get('found', False))
            failed = len(results_list) - successful

            # ========================================
            # METRICS LOGGING: Cost Calculation
            # ========================================
            # Calculate estimated LLM costs based on call count
            # Phase: Error Handling and Logging | Requirements: 6.1, 6.2, 6.3, 9.5

            # Gemini 2.5 Flash pricing (as of design document):
            # Input: $0.00015 per 1K tokens
            # Output: $0.0006 per 1K tokens
            # Estimated average: ~1000 tokens per call (input + output)
            # Average cost per call: ~$0.0003

            avg_tokens_per_call = 1000  # Estimated average tokens per LLM call
            cost_per_1k_tokens = 0.00015  # Gemini 2.5 Flash input cost
            estimated_cost_per_call = (avg_tokens_per_call / 1000) * cost_per_1k_tokens

            total_estimated_cost = llm_call_count * estimated_cost_per_call
            avg_llm_calls_per_element = llm_call_count / len(elements) if len(elements) > 0 else 0
            avg_cost_per_element = total_estimated_cost / len(elements) if len(elements) > 0 else 0

            # Only log cost metrics if TRACK_LLM_COSTS is enabled
            if settings.TRACK_LLM_COSTS:
                logger.info("=" * 80)
                logger.info("📊 WORKFLOW COST METRICS")
                logger.info("=" * 80)
                logger.info(f"Total LLM calls: {llm_call_count}")
                logger.info(f"Average LLM calls per element: {avg_llm_calls_per_element:.1f}")
                logger.info(f"Estimated total cost: ${total_estimated_cost:.6f}")
                logger.info(f"Estimated cost per element: ${avg_cost_per_element:.6f}")
                logger.info(f"Custom actions enabled: {custom_actions_enabled}")
                logger.info(f"Total execution time: {execution_time:.2f}s")
                logger.info(f"Average time per element: {execution_time / len(elements):.2f}s" if len(elements) > 0 else "N/A")
                logger.info("=" * 80)

            # ========================================
            # VALIDATION VERIFICATION BEFORE WORKFLOW COMPLETION
            # ========================================
            # Verify all elements have proper validation data
            logger.info("🔍 Verifying validation data for all elements...")

            validation_issues = []
            elements_without_validation = []
            elements_not_unique = []
            elements_not_valid = []

            for result in results_list:
                elem_id = result.get('element_id', 'unknown')

                # Check if element has validated=True
                if not result.get('validated', False):
                    validation_issues.append(f"{elem_id}: missing validated=True")
                    elements_without_validation.append(elem_id)

                # Check if element has count=1 and unique=True (only for found elements)
                if result.get('found', False):
                    count = result.get('count', 0)
                    unique = result.get('unique', False)
                    valid = result.get('valid', False)

                    if count != 1 or not unique:
                        validation_issues.append(f"{elem_id}: count={count}, unique={unique} (expected count=1, unique=True)")
                        elements_not_unique.append(elem_id)

                    if not valid:
                        validation_issues.append(f"{elem_id}: valid={valid} (expected valid=True)")
                        elements_not_valid.append(elem_id)

            # Log validation summary
            if validation_issues:
                logger.warning(f"⚠️ Validation issues found for {len(validation_issues)} element(s):")
                for issue in validation_issues:
                    logger.warning(f"   - {issue}")
            else:
                logger.info("✅ All elements have complete validation data")

            # Create validation summary for results
            validation_summary = {
                'total_elements': len(results_list),
                'elements_with_validation': len(results_list) - len(elements_without_validation),
                'elements_without_validation': len(elements_without_validation),
                'elements_unique': len([r for r in results_list if r.get('unique', False) and r.get('found', False)]),
                'elements_not_unique': len(elements_not_unique),
                'elements_valid': len([r for r in results_list if r.get('valid', False) and r.get('found', False)]),
                'elements_not_valid': len(elements_not_valid),
                'validation_issues': validation_issues,
                'elements_without_validation_list': elements_without_validation,
                'elements_not_unique_list': elements_not_unique,
                'elements_not_valid_list': elements_not_valid
            }

            logger.info("📊 Validation Summary:")
            logger.info(f"   Total elements: {validation_summary['total_elements']}")
            logger.info(f"   Elements with validation: {validation_summary['elements_with_validation']}/{validation_summary['total_elements']}")
            logger.info(f"   Elements with unique locators: {validation_summary['elements_unique']}/{successful}")
            logger.info(f"   Elements with valid locators: {validation_summary['elements_valid']}/{successful}")

            if validation_issues:
                logger.warning(f"   ⚠️ {len(validation_issues)} validation issue(s) detected")
            # ========================================

            # CRITICAL: Only consider workflow successful if ALL elements have unique locators
            # This ensures we don't proceed with placeholder locators or non-unique locators
            all_found = successful == len(
                results_list) and len(results_list) > 0

            return {
                'success': all_found,  # Changed from 'successful > 0' to require ALL elements found
                'workflow_mode': True,
                'workflow_completed': workflow_completed,
                'results': results_list,
                'summary': {
                    'total_elements': len(elements),
                    'successful': successful,
                    'failed': failed,
                    'success_rate': successful / len(elements) if len(elements) > 0 else 0,
                    # Cost tracking metrics
                    'total_llm_calls': llm_call_count,
                    'avg_llm_calls_per_element': avg_llm_calls_per_element,
                    'estimated_total_cost': total_estimated_cost,
                    'estimated_cost_per_element': avg_cost_per_element,
                    'custom_actions_enabled': custom_actions_enabled
                },
                'validation_summary': validation_summary,  # Add validation summary to results
                'execution_time': execution_time,
                'session_id': str(id(session))
            }

        except Exception as e:
            logger.error(f"❌ Workflow task error: {e}", exc_info=True)
            return {
                'success': False,
                'workflow_mode': True,
                'error': str(e),
                'results': [],
                'summary': {
                    'total_elements': len(elements),
                    'successful': 0,
                    'failed': len(elements),
                    'success_rate': 0
                }
            }
        finally:
            # Use comprehensive cleanup utility
            await cleanup_browser_resources(
                session=session,
                connected_browser=connected_browser,
                playwright_instance=playwright_instance
            )

    # Run the async workflow
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(run_unified_workflow())
        loop.close()

        # Update task status
        tasks_dict[task_id].update({
            "status": "completed",
            "completed_at": time.time(),
            "message": f"Workflow completed: {results['summary']['successful']}/{results['summary']['total_elements']} elements found",
            "results": results
        })

        logger.info(f"🎉 Workflow task {task_id} completed successfully")
        if 'summary' in results and 'success_rate' in results['summary']:
            logger.info(
                f"   Success rate: {results['summary']['success_rate']*100:.1f}%")

        # ========================================
        # RECORD WORKFLOW METRICS
        # ========================================
        # Send metrics to the workflow metrics API endpoint for persistence
        if settings.TRACK_LLM_COSTS and 'summary' in results:
            try:
                record_workflow_metrics(
                    workflow_id=task_id,
                    url=url,
                    results=results,
                    session_id=results.get('session_id'),
                    backend_port=settings.APP_PORT
                )
            except Exception as metrics_error:
                # Don't fail the workflow if metrics recording fails
                logger.warning(f"⚠️ Failed to record workflow metrics: {metrics_error}")

    except Exception as e:
        logger.error(
            f"❌ Failed to execute workflow task {task_id}: {e}", exc_info=True)
        tasks_dict[task_id].update({
            "status": "completed",
            "completed_at": time.time(),
            "message": f"Workflow failed: {str(e)}",
            "results": {
                'success': False,
                'workflow_mode': True,
                'error': str(e),
                'results': [],
                'summary': {'total_elements': len(elements), 'successful': 0, 'failed': len(elements)}
            }
        })
