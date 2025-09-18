import os
import uuid
import logging
import json
import asyncio
from queue import Queue, Empty
from threading import Thread
from typing import Generator, Dict, Any, Optional

from src.backend.crew_ai.crew import run_crew
from src.backend.services.docker_service import get_docker_client, build_image, run_test_in_container
from src.backend.services.healing_orchestrator import HealingOrchestrator
from src.backend.services.failure_detection_service import FailureDetectionService
from src.backend.core.config_loader import get_healing_config
from src.backend.core.models import FailureContext


def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str) -> Generator[Dict[str, Any], None, None]:
    """
    Orchestrates the CrewAI workflow to generate Robot Framework code,
    yielding progress updates and the final code.
    """
    logging.info("--- Starting CrewAI Workflow ---")
    yield {"status": "running", "message": "Starting CrewAI workflow..."}

    if model_provider == "online":
        if not os.getenv("GEMINI_API_KEY"):
            logging.error(
                "Orchestrator: GEMINI_API_KEY not found for online provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return

    try:
        validation_output, crew_with_results = run_crew(
            natural_language_query, model_provider, model_name)

        robot_code = crew_with_results.tasks[2].output.raw
        import re
        robot_code = re.sub(r'^```[a-zA-Z]*\n', '', robot_code)
        robot_code = re.sub(r'\n```$', '', robot_code)
        robot_code = robot_code.strip()

        raw_validation_output = crew_with_results.tasks[3].output.raw
        json_match = re.search(r'\{.*\}', raw_validation_output, re.DOTALL)

        if not json_match:
            raise ValueError("No JSON object found in the validation output.")

        json_string = json_match.group(0)
        validation_data = json.loads(json_string)

        if validation_data.get("valid"):
            logging.info(
                "CrewAI workflow complete. Code validation successful.")
            yield {"status": "complete", "robot_code": robot_code, "message": "Code generation successful."}
        else:
            logging.error(
                f"CrewAI workflow finished, but code validation failed. Reason: {validation_data.get('reason')}")
            yield {"status": "error", "message": f"Code validation failed: {validation_data.get('reason')}"}
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        logging.error(
            f"Failed to parse validation output from crew: {e}\nRaw output was:\n{raw_validation_output}")
        yield {"status": "error", "message": "Failed to parse validation output from the crew.", "robot_code": robot_code}
    except Exception as e:
        logging.error(
            f"An unexpected error occurred during the CrewAI workflow: {e}", exc_info=True)
        yield {"status": "error", "message": f"An unexpected error occurred: {e}"}


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
    with open(test_filepath, 'w') as f:
        f.write(robot_code)

    try:
        client = get_docker_client()
        for event in build_image(client):
            yield f"data: {json.dumps({'stage': 'execution', **event})}\n\n"

        # Execute test with healing integration
        async for event in execute_test_with_healing(client, run_id, test_filename, test_filepath, model_provider, model_name):
            yield event

    except (ConnectionError, RuntimeError, Exception) as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


async def execute_test_with_healing(client, run_id: str, test_filename: str, test_filepath: str,
                                    model_provider: str, model_name: str) -> Generator[str, None, None]:
    """
    Execute test with self-healing integration.

    Args:
        client: Docker client
        run_id: Unique run identifier
        test_filename: Name of the test file
        test_filepath: Full path to the test file
        model_provider: LLM provider for healing
        model_name: LLM model name for healing

    Yields:
        Event stream data for frontend
    """
    max_healing_attempts = 3
    healing_attempt = 0

    # Load healing configuration
    try:
        healing_config = get_healing_config()
        if not healing_config.enabled:
            # If healing is disabled, run test normally
            result = run_test_in_container(client, run_id, test_filename)
            yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"
            return
    except Exception as e:
        logging.warning(
            f"Failed to get healing config, running without healing: {e}")
        result = run_test_in_container(client, run_id, test_filename)
        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"
        return

    # Initialize healing services
    failure_detection = FailureDetectionService()
    healing_orchestrator = HealingOrchestrator(
        healing_config, model_provider, model_name)

    try:
        await healing_orchestrator.start()

        while healing_attempt <= max_healing_attempts:
            healing_attempt += 1

            # Execute the test
            yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': f'Running test (attempt {healing_attempt})...'})}\n\n"

            try:
                logging.info(f"🚀 WORKFLOW SERVICE: Calling run_test_in_container with run_id={run_id}, test_filename={test_filename}")
                result = run_test_in_container(client, run_id, test_filename)

                # Debug logging
                logging.info(f"✅ WORKFLOW SERVICE: Test execution completed, result status: {result.get('status', 'unknown')}")
                logging.debug(f"📊 WORKFLOW SERVICE: Full test execution result: {result}")

                # Check if test passed - need to check the actual test results, not just container status
                output_xml_path = os.path.join(
                    os.path.dirname(test_filepath), "output.xml")
                
                logging.info(f"🔍 WORKFLOW SERVICE: Checking test results at {output_xml_path}")

                # If output.xml exists, check the actual test results
                test_actually_passed = False
                if os.path.exists(output_xml_path):
                    logging.info(f"✅ WORKFLOW SERVICE: Found output.xml file, analyzing test results")
                    try:
                        # Quick check if all tests passed by looking for failures in output.xml
                        with open(output_xml_path, 'r') as f:
                            xml_content = f.read()
                            # If there are no failed tests, the test passed
                            test_actually_passed = 'fail="0"' in xml_content and 'status="FAIL"' not in xml_content
                            logging.info(f"📊 WORKFLOW SERVICE: Test analysis result: test_actually_passed={test_actually_passed}")
                    except Exception as e:
                        logging.error(f"❌ WORKFLOW SERVICE: Failed to check test results: {e}")
                else:
                    logging.warning(f"⚠️  WORKFLOW SERVICE: No output.xml file found at {output_xml_path}")

                if test_actually_passed:
                    # Test passed, no healing needed
                    logging.info(f"🎉 WORKFLOW SERVICE: Test passed, no healing needed")
                    yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"
                    return

                # Test failed, check if healing is possible
                logging.info(f"⚠️  WORKFLOW SERVICE: Test failed, checking for healing opportunities")
                logging.info(f"📁 WORKFLOW SERVICE: Looking for healing data at: {output_xml_path}")

                if not os.path.exists(output_xml_path):
                    # No output.xml means system failure, not healable
                    logging.error(f"❌ WORKFLOW SERVICE: No output.xml found at {output_xml_path}, cannot heal")
                    yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"
                    return

                # Analyze failures
                yield f"data: {json.dumps({'stage': 'healing', 'status': 'running', 'message': 'Analyzing test failures...'})}\n\n"
                logging.info("Starting failure analysis...")

                failures = failure_detection.analyze_execution_result(
                    output_xml_path,
                    result.get("result", {}).get("logs", "")
                )

                logging.info(f"Found {len(failures)} total failures")
                for i, failure in enumerate(failures):
                    logging.info(
                        f"Failure {i+1}: {failure.original_locator} - {failure.failure_type}")

                healable_failures = [
                    f for f in failures if f.failure_type.name != "OTHER"]
                logging.info(
                    f"Found {len(healable_failures)} healable failures")

                if not healable_failures:
                    # No healable failures found
                    logging.warning("No healable failures detected")
                    yield f"data: {json.dumps({'stage': 'execution', **result, 'healing_attempted': False, 'healing_reason': 'No healable failures detected'})}\n\n"
                    return

                # We have healable failures, proceed with healing
                logging.info(
                    f"Proceeding with healing for {len(healable_failures)} failures")

                if healing_attempt > max_healing_attempts:
                    # Max attempts reached
                    yield f"data: {json.dumps({'stage': 'execution', **result, 'healing_attempted': True, 'healing_reason': f'Maximum healing attempts ({max_healing_attempts}) reached'})}\n\n"
                    return

                # Attempt healing for each failure
                healing_successful = False

                for failure_context in healable_failures:
                    failure_context.run_id = run_id

                    yield f"data: {json.dumps({'stage': 'healing', 'status': 'running', 'message': f'Attempting to heal locator: {failure_context.original_locator}'})}\n\n"

                    try:
                        # Initiate healing session
                        healing_session = await healing_orchestrator.initiate_healing(failure_context)

                        # Register progress callback for real-time updates
                        def progress_callback(session_id: str, progress_data: Dict[str, Any]):
                            # This will be called by the healing orchestrator
                            pass

                        healing_orchestrator.register_progress_callback(
                            healing_session.session_id, progress_callback)

                        # Wait for healing to complete (with timeout)
                        timeout_seconds = 300  # 5 minutes
                        start_time = asyncio.get_event_loop().time()

                        while True:
                            session_status = await healing_orchestrator.get_session_status(healing_session.session_id)

                            if not session_status:
                                break

                            if session_status.status.name in ["SUCCESS", "FAILED", "TIMEOUT"]:
                                if session_status.status.name == "SUCCESS" and session_status.healed_locator:
                                    healing_successful = True
                                    yield f"data: {json.dumps({'stage': 'healing', 'status': 'complete', 'message': f'Successfully healed locator: {failure_context.original_locator} -> {session_status.healed_locator}'})}\n\n"
                                else:
                                    error_msg = session_status.error_message or "Unknown error"
                                    yield f"data: {json.dumps({'stage': 'healing', 'status': 'failed', 'message': f'Failed to heal locator: {failure_context.original_locator}. Reason: {error_msg}'})}\n\n"
                                break

                            # Check timeout
                            if asyncio.get_event_loop().time() - start_time > timeout_seconds:
                                await healing_orchestrator.cancel_session(healing_session.session_id)
                                yield f"data: {json.dumps({'stage': 'healing', 'status': 'timeout', 'message': f'Healing timeout for locator: {failure_context.original_locator}'})}\n\n"
                                break

                            await asyncio.sleep(2)  # Poll every 2 seconds

                    except Exception as e:
                        logging.error(
                            f"Healing failed for locator {failure_context.original_locator}: {e}")
                        yield f"data: {json.dumps({'stage': 'healing', 'status': 'error', 'message': f'Healing error for {failure_context.original_locator}: {str(e)}'})}\n\n"

                if not healing_successful:
                    # No healing was successful, return original failure
                    yield f"data: {json.dumps({'stage': 'execution', **result, 'healing_attempted': True, 'healing_successful': False})}\n\n"
                    return

                # Healing was successful, continue loop to re-run test
                yield f"data: {json.dumps({'stage': 'healing', 'status': 'complete', 'message': 'Healing completed successfully, re-running test...'})}\n\n"

            except Exception as e:
                logging.error(f"Test execution failed: {e}")
                yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"
                return

        # If we get here, max attempts were reached
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Test failed after {max_healing_attempts} healing attempts'})}\n\n"

    finally:
        await healing_orchestrator.stop()


def run_workflow_with_healing_support(natural_language_query: str, model_provider: str, model_name: str) -> Generator[Dict[str, Any], None, None]:
    """
    Enhanced workflow that supports healing integration.
    This is a wrapper around the existing workflow with healing hooks.
    """
    # Use existing workflow for code generation
    for event in run_agentic_workflow(natural_language_query, model_provider, model_name):
        yield event
