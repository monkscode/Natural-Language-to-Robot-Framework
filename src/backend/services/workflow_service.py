import os
import uuid
import logging
import json
import asyncio
from queue import Queue, Empty
from threading import Thread
from typing import Generator, Dict, Any

from src.backend.crew_ai.crew import run_crew
from src.backend.services.docker_service import get_docker_client, build_image, run_test_in_container

def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str) -> Generator[Dict[str, Any], None, None]:
    """
    Orchestrates the CrewAI workflow to generate Robot Framework code,
    yielding progress updates and the final code.
    """
    logging.info("--- Starting CrewAI Workflow ---")
    yield {"status": "running", "message": "Starting CrewAI workflow..."}

    if model_provider == "online":
        if not os.getenv("GEMINI_API_KEY"):
            logging.error("Orchestrator: GEMINI_API_KEY not found for online provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return

    try:
        validation_output, crew_with_results = run_crew(natural_language_query, model_provider, model_name)

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
            logging.info("CrewAI workflow complete. Code validation successful.")
            yield {"status": "complete", "robot_code": robot_code, "message": "Code generation successful."}
        else:
            logging.error(f"CrewAI workflow finished, but code validation failed. Reason: {validation_data.get('reason')}")
            yield {"status": "error", "message": f"Code validation failed: {validation_data.get('reason')}"}
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        logging.error(f"Failed to parse validation output from crew: {e}\nRaw output was:\n{raw_validation_output}")
        yield {"status": "error", "message": "Failed to parse validation output from the crew.", "robot_code": robot_code}
    except Exception as e:
        logging.error(f"An unexpected error occurred during the CrewAI workflow: {e}", exc_info=True)
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
    robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'robot_tests')
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

        result = run_test_in_container(client, run_id, test_filename)
        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"

    except (ConnectionError, RuntimeError, Exception) as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"
