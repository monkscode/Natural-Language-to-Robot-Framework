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

    logging.info(f"Received query: '{user_query}'. Starting agentic workflow...")

    # Call our new agentic workflow to generate the code
    robot_code = run_agentic_workflow(user_query)

    if not robot_code:
        logging.error("Agentic workflow failed to generate Robot Framework code.")
        raise HTTPException(status_code=500, detail="Failed to generate valid Robot Framework code from the query.")

    logging.info("Agentic workflow completed successfully. Running test in Docker...")

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
        image_tag = f"robot-test-runner:{uuid.uuid4()}"

        logging.info(f"Building Docker image: {image_tag}")
        client.images.build(path=robot_tests_dir, tag=image_tag, rm=True)

        logging.info(f"Running Docker container with test: {test_filename}")
        container_logs = client.containers.run(
            image=image_tag,
            command=["robot", test_filename],
            volumes={os.path.abspath(robot_tests_dir): {'bind': '/app', 'mode': 'ro'}},
            working_dir="/app",
            stderr=True,
            stdout=True,
            detach=False,
            auto_remove=True
        )
        logs = container_logs.decode('utf-8')
        logging.info("Docker container finished execution.")

        return {'model_used': 'Custom Agentic Workflow', 'robot_code': robot_code, 'logs': logs}

    except docker.errors.BuildError as e:
        logging.error(f"Docker build failed: {e}")
        return {'model_used': 'Custom Agentic Workflow', 'robot_code': robot_code, 'logs': f"Docker build failed: {e}"}
    except docker.errors.ContainerError as e:
        logging.error(f"Docker container failed: {e}")
        return {'model_used': 'Custom Agentic Workflow', 'robot_code': robot_code, 'logs': f"Docker container exited with error: {e.stderr.decode('utf-8')}"}
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))
