import json
import logging
import os
import requests
import time
from typing import Any, Type, Optional, Dict

from dotenv import load_dotenv
load_dotenv("src/backend/.env")

from src.backend.core.config import settings

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", encoding="utf-8"
)
logger = logging.getLogger(__name__)


class BrowserUseAPI:
    """Enhanced API client for Browser Use Service."""

    def __init__(self, url: str):
        self.url = url.rstrip('/')

    def health_check(self) -> bool:
        """Check if the Browser Use Service is healthy."""
        try:
            response = requests.get(f"{self.url}/health", timeout=10)
            return response.status_code == 200
        except requests.exceptions.RequestException as e:
            logger.error(f"Health check failed: {e}")
            return False

    def submit_task(self, browser_use_objective: str) -> Optional[str]:
        """Submit a task and get a task_id."""
        try:
            logger.info(f"Submitting enhanced task to {self.url}/submit")
            response = requests.post(
                f"{self.url}/submit",
                json={"browser_use_objective": browser_use_objective},
                timeout=15,
                headers={'Content-Type': 'application/json'}
            )

            if response.status_code == 202:
                result = response.json()
                task_id = result.get("task_id")
                logger.info(f"Task submitted successfully with ID: {task_id}")
                return task_id
            elif response.status_code == 429:
                logger.warning("Service is busy, please try again later")
                return None
            else:
                logger.error(
                    f"Task submission failed with status code: {response.status_code}")
                logger.error(f"Response: {response.text}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"An error occurred during task submission: {e}")
            return None

    def query_task_status(self, task_id: str) -> Dict[str, Any]:
        """Query the status of a task using task_id."""
        try:
            logger.debug(f"Querying task status: {self.url}/query/{task_id}")
            response = requests.get(f"{self.url}/query/{task_id}", timeout=10)

            if response.status_code == 200:
                data = response.json()
                return {
                    "status": "completed",
                    "message": "completed",
                    "data": data,
                    "success": data.get("results", {}).get("success", False)
                }
            elif response.status_code == 202:
                data = response.json()
                return {
                    "status": data.get("status", "processing"),
                    "message": data.get("status", "processing"),
                    "running_time": data.get("running_time")
                }
            elif response.status_code == 404:
                return {"status": "error", "message": "Task not found"}
            else:
                logger.error(
                    f"Status query failed with status code: {response.status_code}")
                return {
                    "status": "error",
                    "message": f"Unexpected status code: {response.status_code}"
                }

        except requests.exceptions.RequestException as e:
            logger.error(f"An error occurred during status query: {e}")
            return {"status": "error", "message": f"Network error: {str(e)}"}


class BrowserUseToolInput(BaseModel):
    """Enhanced input schema for BrowserUseTool."""

    browser_use_objective: str = Field(
        ...,
        description=(
            "Detailed objective for browser automation with vision-based locator generation. "
            "The tool will use vision AI to identify elements and return structured locator data. "
            "Examples:\n"
            "- 'Find the search input box on Amazon homepage and return robust locators'\n"
            "- 'Locate the Add to Cart button for the first product on Flipkart search results'\n"
            "- 'Identify the price element of the first product on eBay'\n"
            "\nThe tool will return:\n"
            "- Multiple locator strategies (ID, name, data-*, aria-*, CSS, XPath)\n"
            "- Stability score for each locator\n"
            "- Validation data (uniqueness, visibility)\n"
            "- Best locator recommendation"
        )
    )


class BrowserUseTool(BaseTool):
    """Vision-enhanced browser automation tool with structured locator generation."""

    name: str = "vision_browser_automation"
    description: str = (
        "Perform vision-based web element identification with structured locator generation. "
        "This tool uses AI vision to identify elements on web pages and generates multiple "
        "robust locator strategies (ID, name, data-*, aria-*, XPath, CSS). "
        "Returns JSON with: best_locator, all_locators (ranked by stability), validation data. "
        "Ideal for dynamic websites where CSS classes change frequently. "
        "Works exceptionally well on e-commerce sites (Flipkart, Amazon, eBay). "
        "Use this when you need reliable, maintainable locators for test automation."
    )
    args_schema: Type[BaseModel] = BrowserUseToolInput

    def _run(self, browser_use_objective: str) -> Dict[str, Any]:
        """Execute the enhanced browser automation task with optional popup handling."""
        logger.info(
            f"Starting enhanced browser automation: {browser_use_objective[:200]}...")

        # Configuration from environment variables
        api_url = os.environ.get(
            "BROWSER_USE_SERVICE_URL") or settings.BROWSER_USE_SERVICE_URL
        # 15 minutes for complex tasks
        timeout = int(os.environ.get("BROWSER_USE_TIMEOUT", "900"))
        # Check every 5 seconds
        check_interval = int(os.environ.get("BROWSER_USE_CHECK_INTERVAL", "5"))
        max_retries = int(os.environ.get("BROWSER_USE_MAX_RETRIES", "3"))

        # Initialize API client
        api_client = BrowserUseAPI(api_url)

        # NOTE: Popup handling is managed by browser-use's system prompt
        # Explicitly mentioning "close popup BEFORE task" makes browser-use think popup closure IS the main task
        # Browser-use is a single-goal agent - it focuses on ONE objective at a time
        # The system prompt already has "IGNORE NON-ESSENTIAL POPUPS" guidance which works better

        # Concise objective to minimize token usage
        enhanced_objective = f"""{browser_use_objective}

TASK: Use vision AI to locate element, extract DOM attributes, return JSON:
{{"best_locator": "name=q", "all_locators": [{{"locator": "name=q", "type": "name"}}, {{"locator": "css=[placeholder='...']", "type": "placeholder"}}]}}

Priority: id > data-* > aria-* > name > placeholder > text > CSS > XPath
Avoid: dynamic classes, product names, non-unique selectors"""

        # Health check with detailed feedback
        logger.info("Performing enhanced health check...")
        if not self._health_check_with_retry(api_client):
            return {
                "status": "error",
                "browser_use_objective": browser_use_objective,
                "result": "",
                "message": "Enhanced Browser Use Service is not available. Please ensure the service is running on " + api_url,
                "success": False,
                "suggestions": [
                    "Check if service is running: python browser_use_service.py",
                    "Verify API URL in environment: BROWSER_USE_SERVICE_URL=" + api_url,
                    "Test service health: curl " + api_url + "/health"
                ]
            }

        # Submit task with retries
        logger.info("Submitting task with enhanced processing...")
        task_id = None
        for attempt in range(max_retries):
            task_id = api_client.submit_task(enhanced_objective)
            if task_id:
                break
            elif attempt < max_retries - 1:
                logger.warning(
                    f"Task submission attempt {attempt + 1} failed, retrying in 10 seconds...")
                time.sleep(10)

        if not task_id:
            return {
                "status": "error",
                "browser_use_objective": browser_use_objective,
                "result": "",
                "message": "Failed to submit task after multiple attempts. Service may be busy or experiencing issues.",
                "success": False,
                "suggestions": [
                    "Try again in a few minutes",
                    "Check service logs for errors",
                    "Simplify the automation objective"
                ]
            }

        logger.info(
            f"Task submitted with ID: {task_id}, starting enhanced monitoring...")

        # Enhanced polling with progress tracking
        start_time = time.time()
        last_status = None
        status_changes = []

        while time.time() - start_time < timeout:
            status_response = api_client.query_task_status(task_id)
            current_status = status_response.get("status")

            # Track status changes for better debugging
            if current_status != last_status:
                status_changes.append({
                    "status": current_status,
                    "timestamp": time.time(),
                    "elapsed": time.time() - start_time
                })

                if current_status == "running":
                    logger.info(f"Task {task_id} is now running...")
                elif current_status == "processing":
                    logger.info(f"Task {task_id} is being processed...")
                last_status = current_status

            if current_status == "completed":
                data = status_response.get("data", {})
                results = data.get("results", {})

                # Extract locator data if available
                locator_data = results.get("locator_data")
                result_text = results.get("result", "")
                success = results.get('success', False)
                execution_time = results.get("execution_time", 0)
                steps_taken = results.get("steps_taken", 0)

                logger.info(
                    f"Task {task_id} completed! Success: {success}, Steps: {steps_taken}, Time: {execution_time:.1f}s")

                # Parse JSON from result text if locator_data not directly available
                if not locator_data and result_text:
                    try:
                        import re
                        json_match = re.search(
                            r'\{[\s\S]*"success"[\s\S]*\}', str(result_text))
                        if json_match:
                            locator_data = json.loads(json_match.group(0))
                            logger.info(
                                "Extracted locator JSON from result text")
                    except (json.JSONDecodeError, AttributeError) as e:
                        logger.warning(
                            f"Could not parse JSON from result: {e}")

                # Build response with structured locator data
                response = {
                    "status": "success" if success else "partial_success",
                    "browser_use_objective": browser_use_objective,
                    "success": success,
                    "execution_time": execution_time,
                    "steps_taken": steps_taken,
                    "task_id": task_id,
                    "total_time": time.time() - start_time
                }

                # Include structured locator data if available
                if locator_data:
                    response.update({
                        "best_locator": locator_data.get("best_locator"),
                        "locator_type": locator_data.get("locator_type"),
                        "all_locators": locator_data.get("all_locators", []),
                        "element_info": locator_data.get("element_info", {}),
                        "confidence": locator_data.get("confidence", "unknown"),
                        "validation": locator_data.get("validation", {}),
                        "message": f"Found element with {len(locator_data.get('all_locators', []))} locator strategies",
                        "result": result_text  # Include raw result as well
                    })
                    logger.info(
                        f"Returning structured response with best_locator: {response['best_locator']}")
                else:
                    # Fallback to text-based response
                    response.update({
                        "result": result_text,
                        "message": data.get("message", "Task completed but no structured locator data available"),
                        "note": "Vision-based locator generation may not have produced structured output"
                    })
                    logger.warning(
                        "No structured locator data found in response")

                return response

            elif current_status in ["processing", "running"]:
                # Enhanced progress reporting
                elapsed = time.time() - start_time
                running_time = status_response.get("running_time", 0)

                if elapsed > 30 and elapsed % 30 == 0:  # Log every 30 seconds after initial 30 seconds
                    logger.info(
                        f"Task {task_id} still {current_status}... Elapsed: {elapsed:.1f}s")

                time.sleep(check_interval)

            elif current_status == "error":
                error_message = status_response.get(
                    "message", "Unknown error occurred")
                logger.error(f"Task {task_id} failed: {error_message}")

                return {
                    "status": "error",
                    "browser_use_objective": browser_use_objective,
                    "result": "",
                    "message": f"Task execution failed: {error_message}",
                    "success": False,
                    "task_id": task_id
                }
            else:
                logger.warning(f"Unknown status received: {current_status}")
                time.sleep(check_interval)

        # Timeout reached
        logger.error(f"Task {task_id} timed out after {timeout} seconds")
        return {
            "status": "error",
            "browser_use_objective": browser_use_objective,
            "result": {},
            "message": f"Task timed out after {timeout} seconds. The task may still be running on the server.",
            "success": False,
            "task_id": task_id
        }

    def _health_check_with_retry(self, api_client: BrowserUseAPI) -> bool:
        """Perform health check with retries."""
        for attempt in range(3):
            if api_client.health_check():
                return True
            if attempt < 2:
                logger.warning(
                    f"Health check attempt {attempt + 1} failed, retrying...")
                time.sleep(2)

        logger.error(
            "Browser Use Service health check failed after multiple attempts")
        return False


# ============================================================================
# BATCH BROWSER USE TOOL - NEW FOR MULTI-ELEMENT PROCESSING
# ============================================================================

class BatchBrowserUseToolInput(BaseModel):
    """Input schema for BatchBrowserUseTool."""

    elements: list = Field(
        ...,
        description=(
            "List of element specifications to find in one browser session. "
            "Each element should be a dict with keys: 'id' (unique identifier), "
            "'description' (what to find), 'action' (optional: input/click/get_text). "
            "Example: [{'id': 'elem_1', 'description': 'search box in header', 'action': 'input'}, "
            "{'id': 'elem_2', 'description': 'first product card', 'action': 'click'}]"
        )
    )

    url: str = Field(
        ...,
        description=(
            "Target URL to navigate to. The browser will open this page once and "
            "find all elements in the same session. Example: 'https://www.flipkart.com'"
        )
    )
    
    @classmethod
    def validate_input(cls, values):
        """
        Validator to handle malformed input from LLM.
        Sometimes the LLM wraps the correct data in an array or nests it incorrectly.
        """
        # This is called by Pydantic during validation
        # If we receive malformed data, we can't fix it here because Pydantic
        # has already rejected it. We need to handle it in the tool's _run method.
        return values

    user_query: str = Field(
        default="",
        description=(
            "Full user query for context. Helps BrowserUse understand the workflow. "
            "Example: 'Search for shoes on Flipkart and get the first product price'"
        )
    )


class BatchBrowserUseTool(BaseTool):
    """
    Batch browser automation tool for finding multiple elements in one persistent browser session.

    This tool is optimized for multi-step workflows where you need to find several elements
    on the same page or across multiple pages in a single user flow. Benefits:
    - Opens browser once, keeps session alive for all elements
    - BrowserUse sees full context and can handle popups intelligently
    - F12 validation for each locator (uniqueness, correctness)
    - Returns partial results if some elements fail
    - Much faster than multiple single calls
    """

    name: str = "batch_browser_automation"
    description: str = (
        "Find multiple web elements in one browser session with full context. "
        "Use this when you have 3+ elements to find in a workflow (e.g., search box, "
        "product card, price, name). The browser stays open across all lookups, "
        "BrowserUse understands the full task context, and popups are handled naturally. "
        "Returns validated locators for all elements (or partial results if some fail). "
        "Ideal for: multi-page workflows, e-commerce flows, form filling. "
        "Input: list of elements with descriptions + target URL + user query for context."
    )
    args_schema: Type[BaseModel] = BatchBrowserUseToolInput

    def _run(self, elements: list, url: str, user_query: str = "") -> Dict[str, Any]:
        """Execute batch browser automation to find multiple elements in one session."""

        # CRITICAL FIX: Handle case where CrewAI/LLM passes malformed input
        # Sometimes the input is wrapped incorrectly or duplicated
        # Expected: elements = [{"id": "elem_1", ...}, {"id": "elem_2", ...}]
        # Sometimes get: elements = [{"elements": [...], "url": "...", "user_query": "..."}]

        if isinstance(elements, list) and len(elements) > 0:
            first_item = elements[0]

            # Check if first item is actually a full request dict (malformed input)
            if isinstance(first_item, dict) and 'elements' in first_item and 'url' in first_item:
                logger.warning(
                    "⚠️ Detected malformed input - extracting correct data from nested structure")
                logger.warning(
                    f"   Received: {type(elements)} with {len(elements)} items")

                # Extract the actual data from the first (and likely only valid) entry
                actual_data = first_item
                elements = actual_data.get('elements', [])
                url = actual_data.get('url', url)
                user_query = actual_data.get('user_query', user_query)

                logger.info(
                    f"✅ Extracted correct data: {len(elements)} elements, URL: {url}")

        logger.info(
            f"Starting batch browser automation for {len(elements)} elements")
        logger.info(f"Target URL: {url}")
        logger.info(f"User query context: {user_query[:100]}...")

        # Configuration
        api_url = os.environ.get(
            "BROWSER_USE_SERVICE_URL") or settings.BROWSER_USE_SERVICE_URL
        # 15 minutes for batch
        timeout = int(os.environ.get("BROWSER_USE_TIMEOUT", "900"))
        check_interval = int(os.environ.get("BROWSER_USE_CHECK_INTERVAL", "5"))
        max_retries = int(os.environ.get("BROWSER_USE_MAX_RETRIES", "3"))

        # Initialize API client
        api_client = BrowserUseAPI(api_url)

        # Health check
        logger.info("Performing health check for batch processing...")
        if not self._health_check_with_retry(api_client):
            return {
                "status": "error",
                "message": f"Browser Use Service not available at {api_url}",
                "success": False,
                "elements_processed": 0,
                "results": []
            }

        # Submit workflow task (renamed from /batch to /workflow)
        logger.info("Submitting workflow task...")
        try:
            response = requests.post(
                f"{api_url}/workflow",
                json={
                    "elements": elements,
                    "url": url,
                    "user_query": user_query,
                    "session_config": {
                        "headless": True,
                        "timeout": timeout
                    }
                },
                timeout=15,
                headers={'Content-Type': 'application/json'}
            )

            if response.status_code == 202:
                result = response.json()
                task_id = result.get("task_id")
                logger.info(
                    f"Workflow task submitted successfully with ID: {task_id}")
                logger.info(
                    f"Processing {result.get('elements_count', len(elements))} elements in unified session...")
            elif response.status_code == 429:
                logger.warning("Service is busy")
                return {
                    "status": "error",
                    "message": "Service is busy processing another task. Please try again later.",
                    "success": False,
                    "elements_processed": 0,
                    "results": []
                }
            else:
                logger.error(
                    f"Batch task submission failed: {response.status_code}")
                return {
                    "status": "error",
                    "message": f"Task submission failed with status {response.status_code}",
                    "success": False,
                    "elements_processed": 0,
                    "results": []
                }

        except requests.exceptions.RequestException as e:
            logger.error(f"Error submitting batch task: {e}")
            return {
                "status": "error",
                "message": f"Network error: {str(e)}",
                "success": False,
                "elements_processed": 0,
                "results": []
            }

        # Poll for results
        logger.info(f"Polling for batch task {task_id} results...")
        start_time = time.time()
        last_status = None

        while time.time() - start_time < timeout:
            status_response = api_client.query_task_status(task_id)
            current_status = status_response.get("status")

            # Log status changes
            if current_status != last_status:
                if current_status == "running":
                    logger.info(f"Batch task {task_id} is now running...")
                elif current_status == "processing":
                    logger.info(f"Batch task {task_id} is being processed...")
                last_status = current_status

            if current_status == "completed":
                data = status_response.get("data", {})
                results = data.get("results", {})

                # Extract batch results
                element_results = results.get("results", [])
                summary = results.get("summary", {})
                success = results.get("success", False)
                execution_time = results.get("execution_time", 0)

                logger.info(f"Batch task completed! Success: {success}")
                logger.info(f"Summary: {summary}")
                logger.info(f"Execution time: {execution_time:.1f}s")

                # Build element_id -> locator mapping
                locator_mapping = {}
                for elem_result in element_results:
                    element_id = elem_result.get("element_id")
                    if elem_result.get("found"):
                        locator_mapping[element_id] = {
                            "best_locator": elem_result.get("best_locator"),
                            "all_locators": elem_result.get("all_locators", []),
                            "validation": elem_result.get("validation", {}),
                            "element_info": elem_result.get("element_info", {}),
                            "found": True
                        }
                    else:
                        locator_mapping[element_id] = {
                            "found": False,
                            "error": elem_result.get("error", "Element not found")
                        }

                return {
                    "status": "success",
                    "success": success,
                    "locator_mapping": locator_mapping,
                    "results": element_results,
                    "summary": summary,
                    "execution_time": execution_time,
                    "total_time": time.time() - start_time,
                    "session_id": results.get("session_id"),
                    "pages_visited": results.get("pages_visited", []),
                    "popups_handled": results.get("popups_handled", []),
                    "message": f"Batch completed: {summary.get('successful', 0)}/{summary.get('total_elements', 0)} elements found"
                }

            elif current_status in ["processing", "running"]:
                elapsed = time.time() - start_time

                # Log progress every 30 seconds
                if elapsed > 30 and int(elapsed) % 30 == 0:
                    logger.info(
                        f"Batch task still {current_status}... Elapsed: {elapsed:.1f}s")

                time.sleep(check_interval)

            elif current_status == "error":
                error_message = status_response.get("message", "Unknown error")
                logger.error(f"Batch task failed: {error_message}")

                # Try to return partial results if available
                data = status_response.get("data", {})
                results = data.get("results", {})
                element_results = results.get("results", [])

                return {
                    "status": "error",
                    "success": False,
                    "message": f"Batch task failed: {error_message}",
                    "results": element_results,  # Partial results if any
                    "task_id": task_id
                }
            else:
                time.sleep(check_interval)

        # Timeout
        logger.error(f"Batch task {task_id} timed out after {timeout} seconds")
        return {
            "status": "error",
            "success": False,
            "message": f"Batch task timed out after {timeout} seconds",
            "task_id": task_id,
            "elements_processed": 0,
            "results": []
        }

    def _health_check_with_retry(self, api_client: BrowserUseAPI) -> bool:
        """Perform health check with retries."""
        for attempt in range(3):
            if api_client.health_check():
                return True
            if attempt < 2:
                logger.warning(
                    f"Health check attempt {attempt + 1} failed, retrying...")
                time.sleep(2)

        logger.error(
            "Browser Use Service health check failed after multiple attempts")
        return False
