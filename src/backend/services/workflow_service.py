import os
import uuid
import logging
import json
import re
import asyncio
from queue import Queue, Empty
from threading import Thread
from typing import Generator, Dict, Any

from src.backend.crew_ai.crew import run_crew
from src.backend.services.docker_service import get_docker_client, build_image, run_test_in_container



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)

def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str) -> Generator[Dict[str, Any], None, None]:
    """
    Orchestrates the CrewAI workflow to generate Robot Framework code,
    yielding progress updates and the final code.
    
    Architecture Note:
    - VisionLocatorService was removed during Phase 3 of codebase cleanup as it was
      explicitly disabled and replaced by CrewAI agents with BatchBrowserUseTool.
    - All locator finding is now handled by CrewAI agents in a unified session,
      providing better context awareness and intelligent popup handling.
    - This approach improved first-run success rate from 60% to 90%+.
    """
    logging.info("--- Starting CrewAI Workflow with Vision Integration ---")
    yield {"status": "running", "message": "Starting CrewAI workflow..."}

    if model_provider == "online":
        if not os.getenv("GEMINI_API_KEY"):
            logging.error(
                "Orchestrator: GEMINI_API_KEY not found for online provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return

    # STEP 1: CrewAI workflow mode handles all locator finding
    # VisionLocatorService was removed during cleanup - CrewAI agents with BatchBrowserUseTool
    # now handle all locator finding in one unified session. This provides better context
    # awareness and handles popups intelligently.
    vision_locators = {}

    # STEP 2: Pass vision locators to CrewAI agents via environment (always empty now)
    if vision_locators:
        os.environ['VISION_LOCATORS_JSON'] = json.dumps(vision_locators)
        logging.info(f"📦 Stored {len(vision_locators)} vision locators for CrewAI agents")
    else:
        os.environ.pop('VISION_LOCATORS_JSON', None)  # Clear any previous data

    # STEP 4: Run CrewAI workflow
    # Note: Rate limiting was removed during Phase 2 of codebase cleanup.
    # Direct LLM calls are now used without wrappers. Google Gemini API has
    # sufficient rate limits (1500 RPM) for our use case.
    try:
        yield {"status": "running", "message": "Generating Robot Framework code..."}
        
        validation_output, crew_with_results = run_crew(
            natural_language_query, model_provider, model_name)

        # Extract robot code from task[2] (code_assembler - no more popup task)
        robot_code = crew_with_results.tasks[2].output.raw
        robot_code = re.sub(r'^```[a-zA-Z]*\n', '', robot_code)
        robot_code = re.sub(r'\n```$', '', robot_code)
        robot_code = robot_code.strip()

        # Extract validation output from task[3] (code_validator)
        raw_validation_output = crew_with_results.tasks[3].output.raw
        
        # Try multiple strategies to extract JSON
        validation_data = None
        
        # Strategy 1: Remove markdown code blocks
        cleaned_output = re.sub(r'```json\s*', '', raw_validation_output)
        cleaned_output = re.sub(r'```\s*', '', cleaned_output)
        cleaned_output = cleaned_output.strip()
        
        # Strategy 2: Try to parse the cleaned output directly
        try:
            validation_data = json.loads(cleaned_output)
            logging.info("✅ Parsed validation output directly")
        except json.JSONDecodeError:
            # Strategy 3: Extract JSON object with regex
            json_match = re.search(r'\{[^{}]*"valid"[^{}]*"reason"[^{}]*\}', cleaned_output, re.DOTALL)
            if json_match:
                try:
                    validation_data = json.loads(json_match.group(0))
                    logging.info("✅ Parsed validation output with regex")
                except json.JSONDecodeError:
                    pass
        
        if not validation_data:
            # Strategy 4: Look for valid/reason separately
            valid_match = re.search(r'"valid"\s*:\s*(true|false)', cleaned_output, re.IGNORECASE)
            reason_match = re.search(r'"reason"\s*:\s*"([^"]*)"', cleaned_output)
            
            if valid_match:
                validation_data = {
                    "valid": valid_match.group(1).lower() == 'true',
                    "reason": reason_match.group(1) if reason_match else "Validation completed"
                }
                logging.info("✅ Parsed validation output with fallback extraction")
        
        if not validation_data:
            logging.error(f"❌ Could not parse validation output. Raw output:\n{raw_validation_output[:500]}")
            raise ValueError("No valid JSON object found in the validation output.")

        if validation_data.get("valid"):
            logging.info("Generated Robot Framework code is here:\n%s", robot_code)
            logging.info(
                "CrewAI workflow complete. Code validation successful.")
            
            # Log vision locator usage stats
            if vision_locators:
                logging.info(f"🎯 Test generated with {len(vision_locators)} vision-validated locators")
            else:
                logging.info("🤖 Test generated with AI-based locators only")
            
            yield {"status": "complete", "robot_code": robot_code, "message": "Code generation successful."}
        else:
            logging.error(
                f"CrewAI workflow finished, but code validation failed. Reason: {validation_data.get('reason')}")
            yield {"status": "error", "message": f"Code validation failed: {validation_data.get('reason')}"}
            
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        logging.error("Failed to generate valid Robot Framework code." + str(e))
        try:
            logging.error(
                f"Failed to parse validation output from crew: {e}\nRaw output was:\n{raw_validation_output}")
            yield {"status": "error", "message": "Failed to parse validation output from the crew.", "robot_code": robot_code}
        except:
            yield {"status": "error", "message": f"Failed to parse validation output: {e}"}
    except Exception as e:
        logging.error(
            f"An unexpected error occurred during the CrewAI workflow: {e}", exc_info=True)
        yield {"status": "error", "message": f"An error occurred: {str(e)}"}
    
    # Clean up environment variables
    os.environ.pop('VISION_LOCATORS_JSON', None)


def run_workflow_in_thread(queue: Queue, user_query: str, model_provider: str, model_name: str):
    """Runs the synchronous agentic workflow and puts results in a queue."""
    try:
        for event in run_agentic_workflow(user_query, model_provider, model_name):
            queue.put(event)
    except Exception as e:
        logging.error(f"Exception in workflow thread: {e}")
        queue.put({"status": "error", "message": f"Workflow thread failed: {e}"})


async def stream_generate_and_run(user_query: str, model_provider: str, model_name: str) -> Generator[str, None, None]:
    robot_code = None
    q = Queue()

    workflow_thread = Thread(
        target=run_workflow_in_thread,
        args=(q, user_query, model_provider, model_name)
    )
    workflow_thread.start()

    while workflow_thread.is_alive():
        try:
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"

            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
                workflow_thread.join()
                break
            elif event.get("status") == "error":
                workflow_thread.join()
                return
        except Empty:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    if not robot_code:
        while not q.empty():
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"
            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
            elif event.get("status") == "error":
                return
        if not robot_code:
            final_error_message = "Agentic workflow finished without generating code."
            logging.error(final_error_message)
            yield f"data: {json.dumps({'stage': 'generation', 'status': 'error', 'message': final_error_message})}\n\n"
            return

    run_id = str(uuid.uuid4())
    robot_tests_dir = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), '..', '..', '..', 'robot_tests')
    run_dir = os.path.join(robot_tests_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    test_filename = "test.robot"
    test_filepath = os.path.join(run_dir, test_filename)
    with open(test_filepath, 'w', encoding='utf-8') as f:
        f.write(robot_code)

    try:
        client = get_docker_client()
        for event in build_image(client):
            yield f"data: {json.dumps({'stage': 'execution', **event})}\n\n"

        # Execute test (healing system removed - locators are validated during generation)
        logging.info(f"🚀 Executing test: {test_filename}")
        result = run_test_in_container(client, run_id, test_filename)
        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"

    except (ConnectionError, RuntimeError, Exception) as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


# Healing system removed - locators are validated during generation by browser-use
# No need for post-failure healing since validation happens upfront with F12-style checks
