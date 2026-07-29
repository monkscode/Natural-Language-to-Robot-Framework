import logging
import os
import requests
import structlog
import threading
import time
from typing import Any, Type, Optional, Dict

from dotenv import load_dotenv
load_dotenv("src/backend/.env")

from src.backend.core.config import settings  # noqa: E402
from src.backend.core.temp_metrics_storage import get_temp_metrics_storage  # noqa: E402

from crewai.tools import BaseTool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", encoding="utf-8"
)
logger = logging.getLogger(__name__)


def _identity_from_context() -> tuple[str | None, str | None, str | None]:
    """Read (workflow_id, org_id, user_id) bound by the generation thread.

    Identity is taken from structlog contextvars — bound by bind_workflow_context
    in the same thread — NEVER from LLM-supplied tool args, which are untrusted.
    """
    ctx = structlog.contextvars.get_contextvars()
    return ctx.get("workflow_id"), ctx.get("org_id"), ctx.get("user_id")


# ---------------------------------------------------------------------------
# Process-global circuit breaker for the FastAPI → browser-service hop.
# Thread-safe: multiple generation threads share one breaker instance.
# ---------------------------------------------------------------------------
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_S = 30
_breaker_lock = threading.Lock()
_breaker_state = {"failures": 0, "opened_at": 0.0}


def _breaker_reset() -> None:
    with _breaker_lock:
        _breaker_state["failures"] = 0
        _breaker_state["opened_at"] = 0.0


def _breaker_allow() -> bool:
    with _breaker_lock:
        if _breaker_state["opened_at"] == 0.0:
            return True
        if time.time() - _breaker_state["opened_at"] >= _BREAKER_COOLDOWN_S:
            return True  # half-open: allow one probe
        return False


def _breaker_record_failure() -> None:
    with _breaker_lock:
        _breaker_state["failures"] += 1
        if _breaker_state["failures"] >= _BREAKER_THRESHOLD:
            _breaker_state["opened_at"] = time.time()


def _breaker_record_success() -> None:
    with _breaker_lock:
        _breaker_state["failures"] = 0
        _breaker_state["opened_at"] = 0.0


def _resolve_check_interval() -> float:
    """Seconds between browser-service status polls.

    Was 5s, which put every run on a 5-second grid: 28 of 30 rows in the
    2026-07-23 bench baseline landed on an exact boundary, wasting ~2.5s per run
    sleeping after the service had already finished. float, not int — a
    fractional override must not raise.
    """
    return float(os.environ.get("BROWSER_USE_CHECK_INTERVAL", "1.0"))


def _merge_phase_timings(service_timings, *, submit_s: float, poll_wait_s: float) -> dict:
    """Service-side spans plus the two the backend owns.

    service_timings is None when the browser service predates this change.

    poll_wait_s OVERLAPS every service-side span rather than partitioning
    alongside them — the backend is asleep on the poll grid for the whole time
    the service is working. Do not sum the seven keys and expect identify_s;
    the grid tail is poll_wait_s minus the sum of the service spans.
    """
    merged = dict(service_timings or {})
    merged["submit_s"] = submit_s
    merged["poll_wait_s"] = poll_wait_s
    return merged


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
        """Execute batch browser automation to find multiple elements in one session.

        Since Task 16 the only production caller is the deterministic element
        stage (element_identification._default_run_tool) — no LLM builds this
        input anymore, so the old malformed-Action-Input repair path is gone.
        """

        # Identity is sourced from contextvars (bound by the generation thread),
        # NEVER from LLM-supplied tool args. Fails open to (None, None, None).
        ctx_workflow_id, org_id, user_id = _identity_from_context()
        effective_workflow_id = ctx_workflow_id

        logger.info(
            f"Starting batch browser automation for {len(elements)} elements")
        logger.info(f"Target URL: {url}")
        logger.info(f"User query context: {user_query[:100]}...")
        logger.info(f"Workflow ID: {effective_workflow_id}")

        # Configuration
        api_url = os.environ.get(
            "BROWSER_USE_SERVICE_URL") or settings.BROWSER_USE_SERVICE_URL
        # 15 minutes for batch
        timeout = int(os.environ.get("BROWSER_USE_TIMEOUT", "900"))
        check_interval = _resolve_check_interval()

        # Initialize API client
        api_client = BrowserUseAPI(api_url)

        if not _breaker_allow():
            logger.warning("Browser-use hop circuit breaker OPEN — failing fast")
            return {
                "status": "error",
                "message": "Browser Use Service is unavailable (circuit breaker open)",
                "success": False,
                "elements_processed": 0,
                "results": [],
            }

        # Health check
        logger.info("Performing health check for batch processing...")
        if not self._health_check_with_retry(api_client):
            _breaker_record_failure()
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
            # Prepare request payload
            payload = {
                "elements": elements,
                "url": url,
                "user_query": user_query,
                "session_config": {
                    "headless": settings.BROWSER_HEADLESS,
                    "timeout": timeout
                }
            }

            # Forward identity from contextvars (never LLM-supplied).
            # parent_workflow_id prevents duplicate metrics recording in browser-service.
            if effective_workflow_id:
                payload["parent_workflow_id"] = effective_workflow_id
                logger.info(f"📎 Including parent_workflow_id: {effective_workflow_id} (will skip duplicate metrics)")
            if org_id:
                payload["org_id"] = org_id
            if user_id:
                payload["user_id"] = user_id

            _submit_t0 = time.perf_counter()
            response = requests.post(
                f"{api_url}/workflow",
                json=payload,
                timeout=15,
                headers={'Content-Type': 'application/json'}
            )
            submit_s = time.perf_counter() - _submit_t0

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
                _breaker_record_failure()
                return {
                    "status": "error",
                    "message": f"Task submission failed with status {response.status_code}",
                    "success": False,
                    "elements_processed": 0,
                    "results": []
                }

        except requests.exceptions.RequestException as e:
            logger.error(f"Error submitting batch task: {e}")
            _breaker_record_failure()
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
        # Total time asleep on the poll grid. Accumulates the requested
        # interval at every poll, so it overlaps the service-side spans
        # rather than partitioning with them.
        poll_wait_s = 0.0
        last_status = None
        # Local counter for transient network errors (connection refused during cleanup).
        # Isolated per _run() call — zero shared state, multi-user safe.
        network_error_retries = 0
        MAX_NETWORK_RETRIES = 3  # waits: 2s + 4s + 8s = 14s max overhead

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
                _breaker_record_success()
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

                # ============================================
                # NEW: Store browser-use metrics to temp file
                # ============================================
                if effective_workflow_id:
                    # Debug: Log what we received from browser-use service
                    logger.info("📊 DEBUG: Received summary from browser-use:")
                    logger.info(f"   summary keys: {list(summary.keys())}")
                    logger.info(f"   total_tokens: {summary.get('total_tokens', 'NOT_FOUND')}")
                    logger.info(f"   input_tokens: {summary.get('input_tokens', 'NOT_FOUND')}")
                    logger.info(f"   output_tokens: {summary.get('output_tokens', 'NOT_FOUND')}")
                    logger.info(f"   cached_tokens: {summary.get('cached_tokens', 'NOT_FOUND')}")
                    logger.info(f"   actual_cost: {summary.get('actual_cost', 'NOT_FOUND')}")
                    
                    browser_metrics = {
                        'llm_calls': summary.get('total_llm_calls', 0),
                        'cost': summary.get('actual_cost', 0.0),
                        'actual_cost': summary.get('actual_cost', 0.0),
                        'tokens': summary.get('total_tokens', 0),
                        'input_tokens': summary.get('input_tokens', 0),
                        'output_tokens': summary.get('output_tokens', 0),
                        'cached_tokens': summary.get('cached_tokens', 0),
                        'execution_time': execution_time,
                        'elements_processed': summary.get('total_elements', 0),
                        'successful_elements': summary.get('successful', 0),
                        'failed_elements': summary.get('failed', 0),
                        'success_rate': summary.get('success_rate', 0.0),
                        'custom_actions_enabled': summary.get('custom_actions_enabled', False),
                        'custom_action_usage_count': 0,  # Will be calculated if needed
                        'session_id': results.get('session_id'),  # Browser session ID
                        'timestamp': time.time(),
                        # Per-element approach metrics for pattern analysis
                        'element_approach_metrics': summary.get('element_approach_metrics', []),
                        # identify_s phase breakdown (2026-07-26 efficiency check).
                        # Service-side spans plus the two the backend owns.
                        'phase_timings': _merge_phase_timings(
                            summary.get('phase_timings'),
                            submit_s=submit_s,
                            poll_wait_s=poll_wait_s,
                        ),
                        'agent_diagnostics': summary.get('agent_diagnostics'),
                    }
                    
                    logger.info("📊 DEBUG: browser_metrics being saved:")
                    logger.info(f"   tokens: {browser_metrics['tokens']}")
                    logger.info(f"   input_tokens: {browser_metrics['input_tokens']}")
                    logger.info(f"   output_tokens: {browser_metrics['output_tokens']}")
                    
                    # Count custom action usage from results
                    for elem_result in element_results:
                        if elem_result.get('metrics', {}).get('custom_action_used', False):
                            browser_metrics['custom_action_usage_count'] += 1
                    
                    temp_storage = get_temp_metrics_storage()
                    temp_storage.write_browser_metrics(effective_workflow_id, browser_metrics)

                    logger.info(f"📊 Browser-use metrics saved to temp file for workflow {effective_workflow_id}")
                    logger.info(f"   LLM calls: {browser_metrics['llm_calls']}, Cost: ${browser_metrics['cost']:.4f}")
                else:
                    logger.warning("⚠️ No workflow_id provided, browser-use metrics not saved to temp file")

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
                            "found": True,
                            "element_type": elem_result.get("element_type"),
                            "dropdown_framework": elem_result.get("dropdown_framework", ""),
                            "select_id": elem_result.get("select_id"),
                            "datepicker_framework": elem_result.get("datepicker_framework", ""),
                            # "stable" default mirrors browser-service's own
                            # re-ranker default — absent must never read as volatile.
                            "stability": elem_result.get("stability", "stable"),
                            # ASTPP flags are emitted top-level only when True.
                            "visibility_filtered": elem_result.get("visibility_filtered", False),
                            "row_anchored": elem_result.get("row_anchored", False),
                            "row_anchor_ambiguous": elem_result.get("row_anchor_ambiguous", False),
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
                poll_wait_s += check_interval

            elif current_status == "error":
                error_message = status_response.get("message", "Unknown error")

                # Distinguish transient network errors (service crashed/restarting during
                # browser cleanup) from real task failures.
                # WinError 10061 = connection refused — the browser_use_service process died
                # briefly during Chrome kill (proc.kill()) in the finally block.
                # Fix 1 (workflow.py) stores results before cleanup, so the service recovers
                # quickly. Retry here to tolerate that brief window.
                is_network_error = (
                    "Network error" in error_message
                    or "connection" in error_message.lower()
                    or "10061" in error_message
                    or "refused" in error_message.lower()
                )
                if is_network_error and network_error_retries < MAX_NETWORK_RETRIES:
                    network_error_retries += 1
                    wait_secs = 2 ** network_error_retries  # 2s, 4s, 8s
                    logger.warning(
                        f"⚠️ Transient network error polling task {task_id} "
                        f"(retry {network_error_retries}/{MAX_NETWORK_RETRIES}, "
                        f"waiting {wait_secs}s): {error_message}"
                    )
                    time.sleep(wait_secs)
                    continue  # retry the poll

                # Real task failure OR network retries exhausted
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
                poll_wait_s += check_interval

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
