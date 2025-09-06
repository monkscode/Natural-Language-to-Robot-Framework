import os
import uuid
import logging
import docker
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

# Import the new agentic workflow orchestrator
from backend.robot_generator import run_agentic_workflow

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Environment and Model Configuration ---
load_dotenv()
logging.info("Environment variables loaded.")

# --- Pydantic Models ---
class Query(BaseModel):
    query: str
    model: str = "gemini-1.5-pro-latest"

# --- FastAPI App ---
app = FastAPI()

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serves the main index.html file."""
    with open(os.path.join(FRONTEND_DIR, "index.html")) as f:
        return HTMLResponse(content=f.read(), status_code=200)

# --- Main Endpoint ---
@app.post('/generate-and-run')
async def generate_and_run(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    # Determine the model provider and model name
    model_provider = os.getenv("MODEL_PROVIDER", "online").lower()
    if model_provider == "local":
        model_name = os.getenv("LOCAL_MODEL", "llama3")
        logging.info(f"Using local model provider: {model_name}")
    else:
        # Fallback to online model, using the request body if env var is not set
        model_name = os.getenv("ONLINE_MODEL", query.model)
        logging.info(f"Using online model provider: {model_name}")


    logging.info(f"Received query: '{user_query}' for model '{model_name}'. Starting agentic workflow...")

    # Call our new agentic workflow to generate the code
    robot_code = run_agentic_workflow(
        natural_language_query=user_query,
        model_provider=model_provider,
        model_name=model_name
    )

    if not robot_code:
        logging.error("Agentic workflow failed to generate Robot Framework code.")
        raise HTTPException(status_code=500, detail="Failed to generate valid Robot Framework code from the query.")

    logging.info(f"Generated Robot Code:\n{robot_code}")
    logging.info("Attempting to run test in Docker...")

    # --- Docker Execution ---
    robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')
    os.makedirs(robot_tests_dir, exist_ok=True)

    # Use a unique filename for this test run
    test_filename = f"test_{uuid.uuid4()}.robot"
    test_filepath = os.path.join(robot_tests_dir, test_filename)

    with open(test_filepath, 'w') as f:
        f.write(robot_code)

    try:
        client = docker.from_env()
        image_tag = "robot-test-runner:latest"

        logging.info(f"Building Docker image '{image_tag}' (if not already cached)...")
        client.images.build(path=robot_tests_dir, tag=image_tag, rm=True)
        logging.info("Docker image build process completed.")

        logging.info(f"Running Docker container with test: {test_filename}")

        # Command to execute inside the container.
        # We specify an output directory so that logs are written to the mounted volume.
        robot_command = [
            "robot",
            "--outputdir", "/app/robot_tests",
            f"robot_tests/{test_filename}"
        ]

        # Run the container with the volume mounted as read-write.
        container_logs = client.containers.run(
            image=image_tag,
            command=robot_command,
            volumes={os.path.abspath(robot_tests_dir): {'bind': '/app/robot_tests', 'mode': 'rw'}},
            working_dir="/app",
            stderr=True,
            stdout=True,
            detach=False,
            auto_remove=True
        )
        logs = container_logs.decode('utf-8')
        logging.info("Docker container finished execution successfully.")

        # On success, also include a hint about where to find the detailed logs.
        logs += "\n\n--- Robot Framework HTML logs (log.html, report.html) are available in the 'robot_tests' directory. ---"

        return {'model_used': model_name, 'robot_code': robot_code, 'logs': logs}

    except docker.errors.BuildError as e:
        logging.error(f"Docker build failed: {e}")
        return {'model_used': model_name, 'robot_code': robot_code, 'logs': f"Docker build failed: {e}"}
    except docker.errors.ContainerError as e:
        logging.error(f"Docker container failed: {e}")
        error_logs = f"Docker container exited with error code {e.exit_status}.\n"

        # The output from the container should be in the 'logs' attribute of the exception
        if hasattr(e, 'logs') and e.logs:
            error_logs += f"Container Logs:\n{e.logs.decode('utf-8', errors='ignore')}"
        else:
            error_logs += "No logs were captured from the container."

        error_logs += "\n\n--- Robot Framework HTML logs (log.html, report.html) may be available in the 'robot_tests' directory for inspection. ---"

        return {'model_used': model_name, 'robot_code': robot_code, 'logs': error_logs}
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))
