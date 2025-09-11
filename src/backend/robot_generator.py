import os
import json
import logging
from src.backend.crew_ai.crew import RobotCrew

def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str):
    """
    Orchestrates the CrewAI workflow to generate Robot Framework code,
    yielding progress updates and the final code.
    """
    logging.info("--- Starting CrewAI Workflow ---")
    yield {"status": "running", "message": "Starting CrewAI workflow..."}

    if model_provider == "online":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logging.error("Orchestrator: GEMINI_API_KEY not found for online provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return

    try:
        crew_instance = RobotCrew(natural_language_query, model_provider, model_name)
        validation_output, crew_with_results = crew_instance.run()

        # The assembled code is the output of the third task
        robot_code = crew_with_results.tasks[2].output.raw

        # Clean the generated code to remove markdown and other artifacts
        import re
        # Remove markdown fences like ```robotframework ... ``` or ``` ... ```
        robot_code = re.sub(r'^```[a-zA-Z]*\n', '', robot_code)
        robot_code = re.sub(r'\n```$', '', robot_code)
        robot_code = robot_code.strip()

        try:
            raw_validation_output = crew_with_results.tasks[3].output.raw

            # Use regex to find JSON, which is more robust
            import re
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
            # If validation fails, we can still return the generated code for debugging purposes
            yield {"status": "error", "message": "Failed to parse validation output from the crew.", "robot_code": robot_code}

    except Exception as e:
        logging.error(f"An unexpected error occurred during the CrewAI workflow: {e}", exc_info=True)
        yield {"status": "error", "message": f"An unexpected error occurred: {e}"}
