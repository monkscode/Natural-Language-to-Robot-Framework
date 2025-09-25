import json
import logging
import os
import requests
import time
from typing import Any, Type, Optional, Dict

from dotenv import load_dotenv
load_dotenv("src/backend/.env")

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
                logger.error(f"Task submission failed with status code: {response.status_code}")
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
                logger.error(f"Status query failed with status code: {response.status_code}")
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
            "Detailed objective for browser automation. Be specific about what data to extract. "
            "For e-commerce sites, focus on extracting actual data rather than finding CSS selectors. "
            "Examples:\n"
            "- 'Open Amazon and search for laptops, get the first 3 product names and prices'\n"
            "- 'Navigate to eBay, search for shoes, extract the first product title and cost'\n"
            "- 'Go to Flipkart, search for phones, get product details from first item'\n"
            "\nTips for better results:\n"
            "- Be specific about the website URL\n"
            "- Mention what data you want extracted\n"
            "- For locators, ask for 'flexible selectors' or 'data extraction' instead of exact CSS\n"
            "- Include fallback instructions for dynamic sites"
        )
    )


class BrowserUseTool(BaseTool):
    """Enhanced Browser automation tool for CrewAI with better error handling."""
    
    name: str = "enhanced_browser_automation"
    description: str = (
        "Perform advanced web browser automation with enhanced error handling and data extraction. "
        "This tool can navigate websites, extract product information, fill forms, and handle "
        "dynamic content on modern e-commerce sites. It's particularly good at extracting "
        "product data from sites like Flipkart, Amazon, eBay even when CSS classes are dynamic. "
        "Use this when you need to automate complex web interactions or extract structured data "
        "from websites that change their layouts frequently."
    )
    args_schema: Type[BaseModel] = BrowserUseToolInput

    def _run(self, browser_use_objective: str) -> Dict[str, Any]:
        """Execute the enhanced browser automation task."""
        logger.info(f"Starting enhanced browser automation: {browser_use_objective[:200]}...")
        
        # Configuration from environment variables
        api_url = os.environ.get("BROWSER_USE_API_URL", "http://localhost:4999")
        timeout = int(os.environ.get("BROWSER_USE_TIMEOUT", "900"))  # 15 minutes for complex tasks
        check_interval = int(os.environ.get("BROWSER_USE_CHECK_INTERVAL", "5"))  # Check every 5 seconds
        max_retries = int(os.environ.get("BROWSER_USE_MAX_RETRIES", "3"))
        
        # Initialize API client
        api_client = BrowserUseAPI(api_url)
        
        # Enhanced objective with better guidance
        enhanced_objective = f"""
        {browser_use_objective}
        
        ENHANCED AUTOMATION GUIDANCE:
        - If you encounter dynamic CSS classes or changing selectors, focus on extracting the actual data instead
        - For product listings, look for patterns like: product names in titles/links, prices with currency symbols
        - Use flexible selectors: [title], [class*="product"], [class*="price"], etc.
        - If exact selectors fail, extract visible text content and identify products by patterns
        - Return the actual data found (names, prices, details) even if you can't provide exact selectors
        - Handle encoding issues gracefully (currency symbols, special characters)
        - Be persistent but efficient - try different approaches if initial methods fail
        - Don't add any name of the product or anything which is specific in the locators. Always provide the generic locators so that if the product is changed, the locators should still work.
        """
        
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
                    "Verify API URL in environment: BROWSER_USE_API_URL=" + api_url,
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
                logger.warning(f"Task submission attempt {attempt + 1} failed, retrying in 10 seconds...")
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

        logger.info(f"Task submitted with ID: {task_id}, starting enhanced monitoring...")
        
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
                
                # Enhanced result processing
                success = results.get('success', False)
                result_data = results.get("result", "")
                execution_time = results.get("execution_time", 0)
                steps_taken = results.get("steps_taken", 0)
                
                logger.info(f"Task {task_id} completed! Success: {success}, Steps: {steps_taken}, Time: {execution_time:.1f}s")
                
                # Provide detailed feedback
                feedback_message = data.get("message", "Task completed")
                if not success and result_data:
                    # Even if marked as unsuccessful, if we got data, consider it partially successful
                    if len(str(result_data).strip()) > 20:
                        success = True
                        feedback_message = "Task completed with data extracted (marked as successful despite automation challenges)"
                
                return {
                    "status": "success" if success else "partial_success",
                    "browser_use_objective": browser_use_objective,
                    "result": result_data,
                    "message": feedback_message,
                    "success": success,
                    "execution_time": execution_time,
                    "steps_taken": steps_taken,
                    "task_id": task_id,
                    "status_changes": status_changes,
                    "total_time": time.time() - start_time
                }
                
            elif current_status in ["processing", "running"]:
                # Enhanced progress reporting
                elapsed = time.time() - start_time
                running_time = status_response.get("running_time", 0)
                
                if elapsed > 30 and elapsed % 30 == 0:  # Log every 30 seconds after initial 30 seconds
                    logger.info(f"Task {task_id} still {current_status}... Elapsed: {elapsed:.1f}s")
                
                time.sleep(check_interval)
                
            elif current_status == "error":
                error_message = status_response.get("message", "Unknown error occurred")
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
                logger.warning(f"Health check attempt {attempt + 1} failed, retrying...")
                time.sleep(2)
        
        logger.error("Browser Use Service health check failed after multiple attempts")
        return False