"""
Custom action implementations for browser-use agent.

This module provides custom actions that the browser-use agent can call during workflow execution.
Custom actions allow the agent to invoke deterministic Python code for locator finding and validation,
bypassing the need for LLM calls for these operations.

Key Functions:
- find_unique_locator_action: Find and validate unique locator for element at coordinates

The custom action uses Playwright's built-in validation methods to ensure locators are unique
and valid before returning them to the agent.

Usage:
    from browser_service.agent.actions import find_unique_locator_action

    # Called by agent during workflow execution
    result = await find_unique_locator_action(
        x=450.5,
        y=320.8,
        element_id="elem_1",
        element_description="Search input box",
        candidate_locator="id=search-input",
        page=playwright_page
    )
"""

import asyncio
import logging
from typing import Dict, Any, Optional

# Get logger
logger = logging.getLogger(__name__)


async def find_unique_locator_action(
    x: float,
    y: float,
    element_id: str,
    element_description: str,
    candidate_locator: Optional[str] = None,
    page=None
) -> Dict[str, Any]:
    """
    Custom action that agent can call to find and validate unique locator.
    ALL validation done with Playwright - no JavaScript needed.
    Runs deterministically (no LLM calls).

    This function is registered with browser-use and callable by the agent.

    Comprehensive error handling includes:
    - Input validation (page object, coordinates, element_id)
    - Specific exception handling (TimeoutError, CancelledError, RuntimeError, ValueError)
    - Structured error results with error_type and full context
    - Detailed logging with element_id, coordinates, and error messages

    Args:
        x: X coordinate of element center
        y: Y coordinate of element center
        element_id: Element identifier (elem_1, elem_2, etc.)
        element_description: Human-readable description
        candidate_locator: Optional locator suggested by agent (e.g., "id=search")
        page: Playwright page object

    Returns:
        Dict with validated locator or error:
        {
            'element_id': str,
            'description': str,
            'found': bool,
            'best_locator': str | None,
            'all_locators': List[Dict],
            'element_info': Dict,
            'coordinates': Dict,
            'validation_summary': Dict,
            'error': str | None,  # Only present if error occurred
            'error_type': str | None,  # Type of error (e.g., 'TimeoutError', 'PageObjectError')
            'validated': bool,
            'count': int,
            'unique': bool,
            'valid': bool,
            'validation_method': str
        }

    Phase: Error Handling and Logging
    Requirements: 8.2, 8.4, 9.1
    """
    # Import config and settings here to avoid circular imports
    from browser_service.config import config
    from src.backend.core.config import settings

    logger.info(f"🎯 Custom Action: find_unique_locator called for {element_id}")
    logger.info(f"   Description: {element_description}")
    logger.info(f"   Coordinates: ({x}, {y})")
    if candidate_locator:
        logger.info(f"   Candidate locator: {candidate_locator}")

    # Helper function to create structured error result
    def create_error_result(error_type: str, error_message: str, additional_context: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a structured error result with complete validation data."""
        result = {
            'element_id': element_id,
            'description': element_description,
            'found': False,
            'error': error_message,
            'error_type': error_type,
            'coordinates': {'x': x, 'y': y},
            'validated': False,
            'count': 0,
            'unique': False,
            'valid': False,
            'validation_method': 'playwright'
        }
        if additional_context:
            result.update(additional_context)
        return result

    try:
        # ========================================
        # VALIDATION: Input Parameters
        # ========================================

        # Validate page object
        if page is None:
            error_msg = "Page object is None - cannot validate locators"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Description: {element_description}")
            logger.error(f"   Coordinates: ({x}, {y})")
            return create_error_result('PageObjectError', error_msg)

        # Validate coordinates
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            error_msg = f"Invalid coordinates: x={x} (type={type(x).__name__}), y={y} (type={type(y).__name__})"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            return create_error_result('InvalidCoordinatesError', error_msg)

        if x < 0 or y < 0:
            error_msg = f"Negative coordinates not allowed: x={x}, y={y}"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            return create_error_result('InvalidCoordinatesError', error_msg)

        # Validate element_id
        if not element_id or not isinstance(element_id, str):
            error_msg = f"Invalid element_id: {element_id} (type={type(element_id).__name__})"
            logger.error(f"❌ {error_msg}")
            return create_error_result('InvalidElementIdError', error_msg)

        # ========================================
        # STEP 1: Validate Candidate Locator (if provided)
        # ========================================

        if candidate_locator:
            logger.info("")
            logger.info("🔍 VALIDATING CANDIDATE LOCATOR")
            logger.info(f"   Locator: {candidate_locator}")
            logger.info("   Method: Playwright page.locator().count()")

            try:
                # Validate candidate locator syntax
                if not isinstance(candidate_locator, str) or not candidate_locator.strip():
                    logger.warning(f"⚠️ Invalid candidate locator format: {candidate_locator}")
                    logger.info("🔄 Continuing with smart locator finder...")
                else:
                    # Convert candidate locator to Playwright format if needed
                    # Agent might provide name=q which Playwright doesn't understand
                    playwright_locator = candidate_locator

                    # Check if it's a format that needs conversion
                    if '=' in candidate_locator:
                        prefix, value = candidate_locator.split('=', 1)
                        prefix = prefix.lower().strip()

                        if prefix == 'name':
                            # name=q → [name="q"] for Playwright
                            playwright_locator = f"[name='{value.strip()}']"
                            logger.info(f"   Converted name= to attribute selector: {playwright_locator}")
                        elif prefix == 'link':
                            # link=text → text=text for Playwright
                            playwright_locator = f"text={value.strip()}"
                            logger.info(f"   Converted link= to text selector: {playwright_locator}")
                        elif prefix == 'tag':
                            # tag=button → button for Playwright
                            playwright_locator = value.strip()
                            logger.info(f"   Converted tag= to CSS selector: {playwright_locator}")
                        # id=, text=, css=, xpath=, data-testid= work as-is in Playwright

                    # Try to validate with Playwright
                    count = await page.locator(playwright_locator).count()

                    # Log detailed validation results
                    is_unique = (count == 1)
                    is_valid = (count == 1)

                    logger.info("   Validation Results:")
                    logger.info(f"      - count: {count}")
                    logger.info(f"      - unique: {is_unique}")
                    logger.info(f"      - valid: {is_valid}")
                    logger.info("      - validated: True")
                    logger.info("      - validation_method: playwright")

                    if count == 1:
                        # Candidate is valid and unique!
                        # Use the converted locator for Browser Library compatibility
                        final_locator = playwright_locator if playwright_locator != candidate_locator else candidate_locator

                        logger.info("")
                        logger.info(f"{'='*80}")
                        logger.info("✅ CANDIDATE LOCATOR IS UNIQUE - Using it directly!")
                        logger.info(f"{'='*80}")
                        logger.info("   Skipping 21 strategies (not needed)")
                        logger.info(f"   Original: {candidate_locator}")
                        if final_locator != candidate_locator:
                            logger.info(f"   Converted: {final_locator} (Browser Library compatible)")
                        logger.info("   Type: candidate")
                        logger.info("   Priority: 0 (agent-provided)")
                        logger.info(f"{'='*80}")
                        logger.info("")

                        return {
                            'element_id': element_id,
                            'description': element_description,
                            'found': True,
                            'best_locator': final_locator,  # Use converted locator
                            'all_locators': [{
                                'type': 'candidate',
                                'locator': final_locator,  # Use converted locator
                                'priority': 0,
                                'strategy': 'Agent-provided candidate (converted for Browser Library)',
                                'count': count,
                                'unique': True,
                                'valid': True,
                                'validated': True,
                                'validation_method': 'playwright'
                            }],
                            'element_info': {},
                            'coordinates': {'x': x, 'y': y},
                            'validation_summary': {
                                'total_generated': 1,
                                'valid': 1,
                                'unique': 1,
                                'validated': 1,
                                'not_found': 0,
                                'not_unique': 0,
                                'errors': 0,
                                'best_type': 'candidate',
                                'best_strategy': 'Agent-provided candidate',
                                'validation_method': 'playwright'
                            },
                            # Add validation data at result level
                            'validated': True,
                            'count': count,
                            'unique': True,
                            'valid': True,
                            'validation_method': 'playwright'
                        }
                    elif count > 1:
                        logger.info(f"   ⚠️ Candidate locator NOT UNIQUE (matches {count} elements)")
                        logger.info("   🔄 Continuing with smart locator finder to find unique locator...")
                    else:  # count == 0
                        logger.info("   ⚠️ Candidate locator NOT FOUND (matches 0 elements)")
                        logger.info("   🔄 Continuing with smart locator finder to find valid locator...")

            except ValueError as e:
                # Invalid locator syntax
                logger.warning(f"⚠️ Candidate locator has invalid syntax: {e}")
                logger.warning(f"   Locator: {candidate_locator}")
                logger.info("🔄 Continuing with smart locator finder...")

            except asyncio.TimeoutError as e:
                # Playwright timeout during validation
                logger.warning(f"⚠️ Candidate locator validation timed out: {e}")
                logger.warning(f"   Locator: {candidate_locator}")
                logger.info("🔄 Continuing with smart locator finder...")

            except RuntimeError as e:
                # Playwright runtime errors (including invalid CSS selectors)
                error_str = str(e).lower()
                if 'not a valid selector' in error_str or 'invalid selector' in error_str:
                    logger.warning(f"⚠️ Candidate locator has invalid CSS syntax: {e}")
                    logger.warning(f"   Locator: {candidate_locator}")
                    logger.warning("   Note: This often happens with numeric IDs (e.g., #123)")
                    logger.info("🔄 Continuing with smart locator finder (will use [id='...'] syntax)...")
                else:
                    logger.warning(f"⚠️ Candidate locator validation failed with RuntimeError: {e}")
                    logger.warning(f"   Locator: {candidate_locator}")
                    logger.info("🔄 Continuing with smart locator finder...")

            except Exception as e:
                # Generic error during candidate validation
                logger.warning(f"⚠️ Candidate locator validation failed: {type(e).__name__}: {e}")
                logger.warning(f"   Locator: {candidate_locator}")
                logger.info("🔄 Continuing with smart locator finder...")

        # ========================================
        # STEP 2: Call Smart Locator Finder
        # ========================================

        logger.info("🔍 Calling smart_locator_finder with 21 strategies...")

        # Import smart_locator_finder
        try:
            from tools.smart_locator_finder import find_unique_locator_at_coordinates
        except ImportError as e:
            error_msg = f"Failed to import smart_locator_finder: {e}"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error("   This is a critical error - smart_locator_finder module is required")
            return create_error_result('ImportError', error_msg)

        # Call smart locator finder with timeout protection
        try:
            result = await asyncio.wait_for(
                find_unique_locator_at_coordinates(
                    page=page,
                    x=x,
                    y=y,
                    element_id=element_id,
                    element_description=element_description,
                    candidate_locator=None,  # Already validated above, so pass None
                    library_type=config.robot_library  # Use configured library type
                ),
                timeout=settings.CUSTOM_ACTION_TIMEOUT
            )

            # Log the result with detailed information
            if result.get('found'):
                best_locator = result.get('best_locator')
                validation_summary = result.get('validation_summary', {})

                logger.info("")
                logger.info(f"{'='*80}")
                logger.info(f"✅ CUSTOM ACTION SUCCEEDED for {element_id}")
                logger.info(f"{'='*80}")
                logger.info(f"   Best Locator: {best_locator}")
                logger.info(f"   Locator Type: {validation_summary.get('best_type', 'unknown')}")
                logger.info(f"   Strategy: {validation_summary.get('best_strategy', 'unknown')}")
                logger.info("   Validation Results:")
                logger.info(f"      - validated: {result.get('validated', False)}")
                logger.info(f"      - count: {result.get('count', 0)}")
                logger.info(f"      - unique: {result.get('unique', False)}")
                logger.info(f"      - valid: {result.get('valid', False)}")
                logger.info(f"      - validation_method: {result.get('validation_method', 'unknown')}")
                logger.info("   Validation Summary:")
                logger.info(f"      - total_strategies: {validation_summary.get('total_generated', 0)}")
                logger.info(f"      - valid: {validation_summary.get('valid', 0)}")
                logger.info(f"      - unique: {validation_summary.get('unique', 0)}")
                logger.info(f"      - not_found: {validation_summary.get('not_found', 0)}")
                logger.info(f"      - not_unique: {validation_summary.get('not_unique', 0)}")
                logger.info(f"      - errors: {validation_summary.get('errors', 0)}")
                logger.info(f"{'='*80}")
                logger.info("")
            else:
                error = result.get('error', 'Unknown error')
                validation_summary = result.get('validation_summary', {})

                logger.error("")
                logger.error(f"{'='*80}")
                logger.error(f"❌ CUSTOM ACTION FAILED for {element_id}")
                logger.error(f"{'='*80}")
                logger.error(f"   Error: {error}")
                logger.error(f"   Element ID: {element_id}")
                logger.error(f"   Description: {element_description}")
                logger.error(f"   Coordinates: ({x}, {y})")
                if validation_summary:
                    logger.error("   Validation Summary:")
                    logger.error(f"      - total_strategies: {validation_summary.get('total_generated', 0)}")
                    logger.error(f"      - valid: {validation_summary.get('valid', 0)}")
                    logger.error(f"      - not_found: {validation_summary.get('not_found', 0)}")
                    logger.error(f"      - not_unique: {validation_summary.get('not_unique', 0)}")
                    logger.error(f"      - errors: {validation_summary.get('errors', 0)}")
                logger.error(f"{'='*80}")
                logger.error("")

            return result

        except asyncio.TimeoutError:
            # Handle timeout gracefully
            timeout_msg = f"Smart locator finder timed out after {settings.CUSTOM_ACTION_TIMEOUT} seconds"
            logger.error(f"⏱️ {timeout_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Description: {element_description}")
            logger.error(f"   Coordinates: ({x}, {y})")
            logger.error("   This may indicate a complex page or slow network")

            return create_error_result('TimeoutError', timeout_msg, {
                'timeout_seconds': settings.CUSTOM_ACTION_TIMEOUT
            })

        except asyncio.CancelledError:
            # Task was cancelled (e.g., browser closed)
            cancel_msg = "Smart locator finder was cancelled (browser may have closed)"
            logger.error(f"🚫 {cancel_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Coordinates: ({x}, {y})")

            return create_error_result('CancelledError', cancel_msg)

        except RuntimeError as e:
            # Runtime errors (e.g., event loop issues, browser closed)
            runtime_msg = f"Runtime error in smart locator finder: {str(e)}"
            logger.error(f"❌ {runtime_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Coordinates: ({x}, {y})")
            logger.error("   This may indicate the browser was closed or the page navigated away")
            logger.error("   Stack trace:", exc_info=True)

            return create_error_result('RuntimeError', runtime_msg)

        except Exception as e:
            # Catch any other errors from smart_locator_finder
            finder_error_msg = f"Smart locator finder raised {type(e).__name__}: {str(e)}"
            logger.error(f"❌ {finder_error_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Coordinates: ({x}, {y})")
            logger.error("   Stack trace:", exc_info=True)

            return create_error_result(type(e).__name__, finder_error_msg)

    except asyncio.TimeoutError:
        # Top-level timeout (shouldn't happen, but handle it)
        timeout_msg = "Custom action timed out at top level"
        logger.error(f"⏱️ {timeout_msg}")
        logger.error(f"   Element ID: {element_id}")
        logger.error(f"   Coordinates: ({x}, {y})")

        return create_error_result('TimeoutError', timeout_msg)

    except asyncio.CancelledError:
        # Top-level cancellation
        cancel_msg = "Custom action was cancelled"
        logger.error(f"🚫 {cancel_msg}")
        logger.error(f"   Element ID: {element_id}")
        logger.error(f"   Coordinates: ({x}, {y})")

        return create_error_result('CancelledError', cancel_msg)

    except KeyboardInterrupt:
        # User interrupted execution
        interrupt_msg = "Custom action interrupted by user"
        logger.error(f"⚠️ {interrupt_msg}")
        logger.error(f"   Element ID: {element_id}")

        return create_error_result('KeyboardInterrupt', interrupt_msg)

    except Exception as e:
        # Catch-all for any unexpected errors
        error_msg = f"Unexpected error in find_unique_locator_action: {type(e).__name__}: {str(e)}"
        logger.error(f"❌ {error_msg}")
        logger.error(f"   Element ID: {element_id}")
        logger.error(f"   Description: {element_description}")
        logger.error(f"   Coordinates: ({x}, {y})")
        if candidate_locator:
            logger.error(f"   Candidate locator: {candidate_locator}")
        logger.error("   Stack trace:", exc_info=True)

        return create_error_result(type(e).__name__, error_msg)
