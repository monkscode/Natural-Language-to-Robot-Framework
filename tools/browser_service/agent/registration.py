"""
Custom action registration for browser-use agent.

This module handles the registration of custom actions with the browser-use agent.
Custom actions allow the agent to call deterministic Python code during workflow execution,
bypassing the need for LLM calls for specific operations like locator validation.

Key Functions:
- register_custom_actions: Register custom actions with browser-use agent

The registration process:
1. Creates or retrieves the Tools instance from the agent
2. Defines parameter models for custom actions using Pydantic
3. Registers action handlers that wrap the actual implementation
4. Handles page object retrieval from browser_session via CDP
5. Converts results to ActionResult format for the agent

Usage:
    from browser_service.agent.registration import register_custom_actions

    # Register custom actions with agent
    success = register_custom_actions(agent, page=None)
    if success:
        # Agent can now call find_unique_locator action
        pass
"""

import asyncio
import logging
from typing import Optional

# Get logger
logger = logging.getLogger(__name__)


def register_custom_actions(agent, page=None) -> bool:
    """
    Register custom actions with browser-use agent.

    This function registers the find_unique_locator custom action that allows
    the agent to call deterministic Python code for locator finding and validation.

    The custom action will get the page object from browser_session during execution,
    ensuring we use the SAME browser that's already open. This is the key strategy:
    validate locators using the existing browser_use browser (no new instance needed).

    Args:
        agent: Browser-use Agent instance
        page: Optional Playwright page object (used as fallback if browser_session doesn't provide one)

    Returns:
        bool: True if registration succeeded, False otherwise

    Phase: Custom Action Implementation
    Requirements: 3.1, 8.1, 9.1
    """
    try:
        logger.info("🔧 Registering custom actions with browser-use agent...")

        # Import required classes for custom action registration
        from browser_use.tools.service import Tools
        from browser_use.agent.views import ActionResult
        from pydantic import BaseModel, Field

        # Import the action implementation
        from browser_service.agent.actions import find_unique_locator_action

        # Import settings
        from src.backend.core.config import settings

        # Define parameter model for find_unique_locator action
        class FindUniqueLocatorParams(BaseModel):
            """Parameters for find_unique_locator custom action"""
            x: float = Field(description="X coordinate of element center")
            y: float = Field(description="Y coordinate of element center")
            element_id: str = Field(description="Element identifier (elem_1, elem_2, etc.)")
            element_description: str = Field(description="Human-readable description of element")
            candidate_locator: Optional[str] = Field(
                default=None,
                description="Optional candidate locator to validate first (e.g., 'id=search-input')"
            )

        # Get or create Tools instance from agent
        if not hasattr(agent, 'tools') or agent.tools is None:
            logger.info("   Creating new Tools instance for agent")
            tools = Tools()
            agent.tools = tools
        else:
            logger.info("   Using existing Tools instance from agent")
            tools = agent.tools

        # Register the find_unique_locator action
        @tools.registry.action(
            description="Find and validate unique locator for element at coordinates using 21 systematic strategies. "
                        "This action runs deterministically without LLM calls and validates all locators with Playwright. "
                        "Call this action after finding an element's coordinates to get a validated unique locator.",
            param_model=FindUniqueLocatorParams
        )
        async def find_unique_locator(
            params: FindUniqueLocatorParams,
            browser_session
        ) -> ActionResult:
            """
            Custom action wrapper that calls find_unique_locator_action.

            This function is called by the browser-use agent when it needs to find
            a unique locator for an element. It wraps the find_unique_locator_action
            function and returns results in ActionResult format.

            The browser_session parameter is provided by browser-use and contains
            the active browser context with the page that's currently open.
            """
            try:
                logger.info("🎯 Custom action 'find_unique_locator' called by agent")
                logger.info(f"   Element: {params.element_id} - {params.element_description}")
                logger.info(f"   Coordinates: ({params.x}, {params.y})")

                # Get the page from the browser_session provided by browser-use
                # IMPORTANT: browser-use now uses CDP (Chrome DevTools Protocol) instead of Playwright
                # We need to connect to browser-use's browser via CDP to get a Playwright page for validation
                active_page = None
                playwright_instance = None
                connected_browser = None

                try:
                    logger.info("🔍 Attempting to retrieve page from browser_session via CDP...")
                    logger.info(f"   browser_session type: {type(browser_session)}")

                    # Strategy 1: Connect via CDP (browser-use's current architecture)
                    # Get CDP URL from browser_session
                    cdp_url = None

                    # Try session.cdp_url
                    if hasattr(browser_session, 'cdp_url'):
                        try:
                            cdp_url = browser_session.cdp_url
                            if cdp_url:
                                logger.info(f"✅ Found CDP URL from browser_session.cdp_url: {cdp_url}")
                        except Exception as e:
                            logger.debug(f"Error accessing browser_session.cdp_url: {e}")

                    # Try cdp_client.url
                    if not cdp_url and hasattr(browser_session, 'cdp_client'):
                        try:
                            cdp_client = browser_session.cdp_client
                            if hasattr(cdp_client, 'url'):
                                cdp_url = cdp_client.url
                                if cdp_url:
                                    logger.info(f"✅ Found CDP URL from cdp_client.url: {cdp_url}")
                        except Exception as e:
                            logger.debug(f"Error accessing cdp_client.url: {e}")

                    # Search all attributes for CDP URL
                    if not cdp_url:
                        logger.info("🔍 Searching for CDP URL in browser_session attributes...")
                        for attr in dir(browser_session):
                            if not attr.startswith('_'):
                                try:
                                    value = getattr(browser_session, attr, None)
                                    if value and isinstance(value, str) and 'ws://' in value and 'devtools' in value:
                                        cdp_url = value
                                        logger.info(f"✅ Found CDP URL in {attr}: {cdp_url}")
                                        break
                                except Exception:
                                    pass

                    # If we have CDP URL, connect Playwright to browser-use's browser
                    if cdp_url:
                        try:
                            from playwright.async_api import async_playwright

                            logger.info("🔌 Connecting Playwright to browser-use's browser via CDP...")
                            playwright_instance = await async_playwright().start()
                            connected_browser = await playwright_instance.chromium.connect_over_cdp(cdp_url)

                            # Get the active page from browser-use's browser
                            if connected_browser.contexts:
                                context = connected_browser.contexts[0]
                                logger.info(f"✅ Found {len(connected_browser.contexts)} context(s)")

                                if context.pages:
                                    active_page = context.pages[0]
                                    page_url = await active_page.url()
                                    logger.info("✅ Connected to browser-use's page via CDP!")
                                    logger.info(f"   Page URL: {page_url}")
                                    logger.info(f"   Page type: {type(active_page)}")
                                else:
                                    logger.warning("⚠️ Context has no pages")
                            else:
                                logger.warning("⚠️ Browser has no contexts")

                        except Exception as e:
                            logger.error(f"❌ Failed to connect Playwright via CDP: {e}")
                            import traceback
                            logger.debug(traceback.format_exc())
                    else:
                        logger.warning("⚠️ Could not find CDP URL in browser_session")
                        logger.info(f"   browser_session attributes: {[attr for attr in dir(browser_session) if not attr.startswith('_')][:20]}")

                    # Strategy 2: Try get_pages() method (fallback)
                    if active_page is None and hasattr(browser_session, 'get_pages'):
                        try:
                            pages = await browser_session.get_pages()
                            if pages and len(pages) > 0:
                                active_page = pages[0]
                                logger.info("✅ Got Playwright page from browser_session.get_pages()[0]")
                                logger.info(f"   Page type: {type(active_page)}")
                                logger.info(f"   Total pages: {len(pages)}")
                            else:
                                logger.warning("⚠️ browser_session.get_pages() returned empty list")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to get pages: {e}")

                    # Strategy 2: Direct access to .page attribute
                    if active_page is None and hasattr(browser_session, 'page') and browser_session.page is not None:
                        active_page = browser_session.page
                        logger.info("✅ Got active page from browser_session.page")
                        logger.info(f"   Page type: {type(active_page)}")

                    # Strategy 3: Try get_current_page() method
                    if active_page is None and hasattr(browser_session, 'get_current_page'):
                        active_page = await browser_session.get_current_page()
                        logger.info("✅ Got active page from browser_session.get_current_page()")
                        logger.info(f"   Page type: {type(active_page)}")

                    # Strategy 3: Try context.pages
                    elif hasattr(browser_session, 'context') and browser_session.context is not None:
                        pages = browser_session.context.pages
                        if pages and len(pages) > 0:
                            active_page = pages[0]  # Get the first (usually only) page
                            logger.info("✅ Got active page from browser_session.context.pages[0]")
                            logger.info(f"   Page type: {type(active_page)}")
                            logger.info(f"   Total pages: {len(pages)}")
                        else:
                            logger.warning("⚠️ browser_session.context.pages is empty")

                    # Strategy 4: Try browser.contexts[0].pages
                    elif hasattr(browser_session, 'browser') and browser_session.browser is not None:
                        contexts = browser_session.browser.contexts
                        if contexts and len(contexts) > 0:
                            pages = contexts[0].pages
                            if pages and len(pages) > 0:
                                active_page = pages[0]
                                logger.info("✅ Got active page from browser_session.browser.contexts[0].pages[0]")
                                logger.info(f"   Page type: {type(active_page)}")
                            else:
                                logger.warning("⚠️ browser_session.browser.contexts[0].pages is empty")
                        else:
                            logger.warning("⚠️ browser_session.browser.contexts is empty")

                    # Fallback: Use page passed during registration
                    if active_page is None:
                        logger.warning("⚠️ All page retrieval strategies failed")
                        logger.warning("⚠️ Falling back to page passed during registration")
                        active_page = page
                        if active_page:
                            logger.info(f"   Fallback page type: {type(active_page)}")
                        else:
                            logger.error("❌ Fallback page is also None!")

                except Exception as e:
                    logger.error(f"❌ Error getting page from browser_session: {e}", exc_info=True)
                    active_page = page  # Use the page passed during registration as fallback
                    if active_page:
                        logger.info(f"   Fallback page type: {type(active_page)}")

                # Unwrap browser-use Page wrapper to get actual Playwright page
                if active_page and not hasattr(active_page, 'locator'):
                    logger.warning(f"⚠️ Page object is a browser-use wrapper: {type(active_page)}")
                    logger.info("   Attempting to unwrap to get Playwright page...")

                    # browser-use wraps the Playwright page in browser_use.actor.page.Page
                    # Try multiple strategies to get the underlying Playwright page
                    playwright_page = None

                    # Strategy 1: Check for .page attribute
                    if hasattr(active_page, 'page') and active_page.page is not None:
                        playwright_page = active_page.page
                        logger.info("✅ Unwrapped page from wrapper.page")

                    # Strategy 2: Check for ._page attribute
                    elif hasattr(active_page, '_page') and active_page._page is not None:
                        playwright_page = active_page._page
                        logger.info("✅ Unwrapped page from wrapper._page")

                    # Strategy 3: Check for ._client attribute (CDP client)
                    elif hasattr(active_page, '_client') and active_page._client is not None:
                        # _client might be the CDP client, try to get page from it
                        client = active_page._client
                        if hasattr(client, 'page') and client.page is not None:
                            playwright_page = client.page
                            logger.info("✅ Unwrapped page from wrapper._client.page")
                        else:
                            logger.warning("   _client exists but has no page attribute")

                    # Strategy 4: Check for ._browser_session attribute
                    elif hasattr(active_page, '_browser_session') and active_page._browser_session is not None:
                        # Try to get page from the browser session
                        session = active_page._browser_session
                        if hasattr(session, 'page') and session.page is not None:
                            playwright_page = session.page
                            logger.info("✅ Unwrapped page from wrapper._browser_session.page")
                        elif hasattr(session, 'get_current_page'):
                            try:
                                playwright_page = await session.get_current_page()
                                logger.info("✅ Unwrapped page from wrapper._browser_session.get_current_page()")
                            except Exception as e:
                                logger.warning(f"   Failed to get page from _browser_session: {e}")

                    # Strategy 5: Use the wrapper directly if it has evaluate method
                    # browser-use Page wrapper might proxy Playwright methods
                    elif hasattr(active_page, 'evaluate'):
                        logger.info("⚠️ Using browser-use Page wrapper directly (has evaluate method)")
                        logger.info("   This wrapper might proxy Playwright methods")
                        playwright_page = active_page  # Use wrapper as-is

                    if playwright_page:
                        logger.info(f"   Playwright page type: {type(playwright_page)}")
                        active_page = playwright_page
                    else:
                        logger.error("❌ Could not unwrap browser-use Page wrapper!")
                        logger.error(f"   Wrapper attributes: {[attr for attr in dir(active_page) if not attr.startswith('__')][:20]}")
                        active_page = None

                # Final verification: ensure we have a page with required methods
                if active_page:
                    required_methods = ['locator', 'evaluate', 'evaluate_handle']
                    missing_methods = [m for m in required_methods if not hasattr(active_page, m)]

                    if missing_methods:
                        logger.error(f"❌ Page object is missing required methods: {missing_methods}")
                        logger.error(f"   Type: {type(active_page)}")
                        logger.error(f"   Available methods: {[attr for attr in dir(active_page) if not attr.startswith('_')][:30]}")
                        active_page = None
                    else:
                        logger.info(f"✅ Page object has all required methods: {required_methods}")
                        logger.info(f"   Page type: {type(active_page)}")

                # Call the actual implementation with the active page
                # Wrap in timeout protection using CUSTOM_ACTION_TIMEOUT from config
                try:
                    result = await asyncio.wait_for(
                        find_unique_locator_action(
                            x=params.x,
                            y=params.y,
                            element_id=params.element_id,
                            element_description=params.element_description,
                            candidate_locator=params.candidate_locator,
                            page=active_page
                        ),
                        timeout=settings.CUSTOM_ACTION_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # Handle timeout gracefully
                    timeout_msg = (
                        f"Custom action timed out after {settings.CUSTOM_ACTION_TIMEOUT} seconds "
                        f"for element {params.element_id}"
                    )
                    logger.error(f"⏱️ {timeout_msg}")
                    logger.error(f"   Element: {params.element_id} - {params.element_description}")
                    logger.error(f"   Coordinates: ({params.x}, {params.y})")

                    # Return error result
                    result = {
                        'element_id': params.element_id,
                        'description': params.element_description,
                        'found': False,
                        'error': timeout_msg,
                        'coordinates': {'x': params.x, 'y': params.y},
                        'validated': False,
                        'count': 0,
                        'unique': False,
                        'valid': False,
                        'validation_method': 'playwright'
                    }

                # Convert result to ActionResult format
                action_result = None
                if result.get('found'):
                    best_locator = result.get('best_locator')

                    # Get validation data from result (not validation_summary)
                    validated = result.get('validated', False)
                    count = result.get('count', 0)
                    validation_method = result.get('validation_method', 'playwright')

                    # Success message for agent - CLEAR and UNAMBIGUOUS
                    # Include explicit confirmation that this is the CORRECT and FINAL locator
                    success_msg = (
                        "✅ SUCCESS - LOCATOR VALIDATED BY PLAYWRIGHT\n"
                        f"Element: {params.element_id}\n"
                        f"Locator: {best_locator}\n"
                        f"Validation Result: UNIQUE (count={count}, validated={validated})\n"
                        f"Method: {validation_method} (deterministic validation)\n"
                        "Status: COMPLETE AND CORRECT\n"
                        "This locator is guaranteed unique and valid.\n"
                        "Do NOT retry or attempt to find a different locator.\n"
                        "Move to the next element immediately."
                    )

                    logger.info(f"✅ Custom action succeeded: {best_locator}")

                    # CRITICAL FIX: Do NOT set success=True when is_done=False
                    # ActionResult validation rule: success can only be True when is_done=True
                    # For regular actions that succeed, leave success as None (default)
                    action_result = ActionResult(
                        extracted_content=success_msg,
                        long_term_memory=f"✅ VALIDATED: {params.element_id} = {best_locator} (Playwright confirmed count=1, unique=True). This is the CORRECT locator. Do NOT retry.",
                        metadata=result,
                        is_done=False  # Don't mark as done, let agent continue with other elements
                        # success is None by default for successful actions that aren't done
                    )

                else:
                    # Error message for agent - CLEAR about failure
                    error_msg = result.get('error', 'Could not find unique locator')
                    logger.error(f"❌ Custom action failed: {error_msg}")

                    action_result = ActionResult(
                        error=f"FAILED: Could not find unique locator for {params.element_id}. Error: {error_msg}. Try different coordinates or description.",
                        is_done=False  # Let agent try again with different approach
                    )

                # Cleanup: Close Playwright connection if we created one
                if connected_browser:
                    try:
                        logger.info("🧹 Cleaning up: Closing Playwright CDP connection...")
                        await connected_browser.close()
                        logger.info("✅ Playwright browser connection closed")
                    except Exception as e:
                        logger.warning(f"⚠️ Error closing Playwright browser: {e}")

                if playwright_instance:
                    try:
                        logger.info("🧹 Cleaning up: Stopping Playwright instance...")
                        await playwright_instance.stop()
                        logger.info("✅ Playwright instance stopped")
                    except Exception as e:
                        logger.warning(f"⚠️ Error stopping Playwright instance: {e}")

                return action_result

            except Exception as e:
                error_msg = f"Error in find_unique_locator custom action: {str(e)}"
                logger.error(f"❌ {error_msg}", exc_info=True)

                # Cleanup on error
                if connected_browser:
                    try:
                        await connected_browser.close()
                    except Exception:
                        pass
                if playwright_instance:
                    try:
                        await playwright_instance.stop()
                    except Exception:
                        pass

                return ActionResult(error=error_msg)

        logger.info("✅ Custom action 'find_unique_locator' registered successfully")
        logger.info("   Agent can now call: find_unique_locator(x, y, element_id, element_description, candidate_locator)")
        return True

    except Exception as e:
        # Log error but don't crash - allow fallback to legacy workflow
        logger.error(f"❌ Failed to register custom actions: {str(e)}")
        logger.error("   Stack trace:", exc_info=True)
        logger.warning("⚠️ Continuing with legacy workflow (custom actions disabled)")
        return False
