import os
import uuid
import logging
import docker
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
from queue import Queue, Empty
from threading import Thread

# Import the new agentic workflow orchestrator
from src.backend.robot_generator import run_agentic_workflow

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Main Endpoint ---
def run_workflow_in_thread(queue, user_query, model_provider, model_name):
    """Runs the synchronous agentic workflow and puts results in a queue."""
    try:
        for event in run_agentic_workflow(user_query, model_provider, model_name):
            queue.put(event)
    except Exception as e:
        logging.error(f"Exception in workflow thread: {e}")
        queue.put({"status": "error", "message": f"Workflow thread failed: {e}"})

def run_docker_in_thread(queue, robot_code, run_id):
    """
    Runs the entire Docker build and execution process in a separate thread
    to avoid blocking the main FastAPI event loop.
    Communicates progress and logs back via a queue.
    """
    try:
        robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'robot_tests')
        run_dir = os.path.join(robot_tests_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)
        test_filename = "test.robot"
        test_filepath = os.path.join(run_dir, test_filename)
        with open(test_filepath, 'w') as f:
            f.write(robot_code)

        try:
            client = docker.from_env()
            client.ping()
        except docker.errors.DockerException as docker_err:
            error_message = "Docker is not available. Please ensure Docker Desktop is installed and running."
            # ... (rest of the error message generation)
            logging.error(f"Docker connection failed: {docker_err}")
            queue.put({"stage": "execution", "status": "error", "message": error_message})
            return

        image_tag = "robot-test-runner:latest"
        dockerfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')

        try:
            existing_image = client.images.get(image_tag)
            logging.info(f"Docker image '{image_tag}' already exists. Skipping build.")
            queue.put({"stage": "execution", "status": "running", "message": "Using existing container image for test execution..."})
        except docker.errors.ImageNotFound:
            logging.info(f"Docker image '{image_tag}' not found. Building new image.")
            queue.put({"stage": "execution", "status": "running", "message": "Building container image for test execution (first time only)..."})
            try:
                build_logs = client.api.build(path=dockerfile_path, tag=image_tag, rm=True, decode=True)
                for log in build_logs:
                    if 'stream' in log:
                        log_message = log['stream'].strip()
                        if log_message:
                            queue.put({"stage": "execution", "status": "running", "log": log_message})
                    if 'error' in log:
                        logging.error(f"Docker build error: {log['error']}")
                        queue.put({"stage": "execution", "status": "error", "message": log['error']})
                        return
                logging.info(f"Successfully built Docker image '{image_tag}'.")
                queue.put({"stage": "execution", "status": "running", "message": "Container image built successfully!"})
            except docker.errors.BuildError as build_err:
                logging.error(f"Failed to build Docker image: {build_err}")
                queue.put({"stage": "execution", "status": "error", "message": f"Docker image build failed: {build_err}"})
                return

        queue.put({"stage": "execution", "status": "running", "message": "Executing test inside the container..."})
        robot_command = ["robot", "--outputdir", f"/app/robot_tests/{run_id}", f"/app/robot_tests/{run_id}/{test_filename}"]

        try:
            container = client.containers.run(
                image=image_tag,
                command=robot_command,
                volumes={os.path.abspath(robot_tests_dir): {'bind': '/app/robot_tests', 'mode': 'rw'}},
                working_dir="/app",
                stderr=True,
                stdout=True,
                detach=False, # Run in blocking mode
                auto_remove=True
            )
            # This code runs after the container has finished
            all_logs = container.decode('utf-8')
            exit_code = 0 # If run() succeeds without ContainerError, exit code is 0
            message = "Test execution finished: All tests passed."
            status = "complete"

        except docker.errors.ContainerError as e:
            # This block runs if the container exits with a non-zero status code
            all_logs = e.container.logs().decode('utf-8', errors='ignore')
            exit_code = e.exit_status
            message = f"Test execution finished: Some tests failed (exit code {exit_code})."
            status = "complete" # It's a test failure, but the process is complete

        log_html_path = f"/reports/{run_id}/log.html"
        report_html_path = f"/reports/{run_id}/report.html"
        final_result = { 'logs': all_logs, 'log_html': log_html_path, 'report_html': report_html_path }

        # Final event
        queue.put({"stage": "execution", "status": status, "message": message, "result": final_result})

    except Exception as e:
        logging.error(f"An unexpected error occurred during Docker execution: {e}", exc_info=True)
        queue.put({"stage": "execution", "status": "error", "message": f"An unexpected error occurred during Docker execution: {e}"})


async def stream_generate_and_run(user_query: str, model_name: str):
    """
    Generator function that streams logs and results, with a heartbeat
    to prevent timeouts and buffer flushing issues.
    """
    robot_code = None
    model_provider = os.getenv("MODEL_PROVIDER", "online").lower()
    q = Queue()

    # Run the synchronous workflow in a separate thread
    workflow_thread = Thread(
        target=run_workflow_in_thread,
        args=(q, user_query, model_provider, model_name)
    )
    workflow_thread.start()

    # --- Stage 1: Generating Code ---
    while workflow_thread.is_alive() or not q.empty():
        try:
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"

            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
                workflow_thread.join()
                break
            elif event.get("status") == "error":
                logging.error(f"Error during code generation: {event.get('message')}")
                workflow_thread.join()
                return
        except Empty:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    if not robot_code:
        logging.error("Agentic workflow finished without generating code.")
        # Any final error messages in the queue should have been processed already.
        return

    # --- Stage 2: Docker Execution ---
    # Use a new queue for the Docker thread to ensure clean separation
    docker_q = Queue()
    run_id = str(uuid.uuid4())
    docker_thread = Thread(
        target=run_docker_in_thread,
        args=(docker_q, robot_code, run_id)
    )
    docker_thread.start()

    while docker_thread.is_alive() or not docker_q.empty():
        try:
            event = docker_q.get_nowait()
            logging.info(f"Yielding event: {event.get('status', 'log')}")
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("status") in ["complete", "error"]:
                docker_thread.join()
                logging.info("Docker thread finished.")
                break
        except Empty:
            # logging.info("Queue empty, sending heartbeat.")
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)


@app.post('/generate-and-run')
async def generate_and_run_streaming(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = os.getenv("MODEL_PROVIDER", "online").lower()
    model_name = "" # initialize
    if model_provider == "local":
        model_name = os.getenv("LOCAL_MODEL", "qwen2.5-coder:14b")
        logging.info(f"Using local model provider: {model_name}")
    else:
        model_name = os.getenv("ONLINE_MODEL")
        if not model_name:
            raise HTTPException(status_code=500, detail="ONLINE_MODEL environment variable is not set.")
        logging.info(f"Using online model provider: {model_name}")

    return StreamingResponse(stream_generate_and_run(user_query, model_name), media_type="text/event-stream")

def rebuild_image_task():
    """Synchronous function to rebuild the Docker image."""
    try:
        client = docker.from_env()
        image_tag = "robot-test-runner:latest"
        dockerfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
        
        # Remove existing image if it exists
        try:
            existing_image = client.images.get(image_tag)
            client.images.remove(image=image_tag, force=True)
            logging.info(f"Removed existing Docker image '{image_tag}'.")
        except docker.errors.ImageNotFound:
            logging.info(f"No existing Docker image '{image_tag}' to remove.")
        
        # Build new image
        logging.info(f"Building new Docker image '{image_tag}'.")
        # Using the low-level API to stream logs, though we don't use them here.
        # Could be adapted to log to a file.
        for _ in client.api.build(path=dockerfile_path, tag=image_tag, rm=True, decode=True):
            pass
        logging.info(f"Successfully rebuilt Docker image '{image_tag}'.")
        
    except Exception as e:
        logging.error(f"Failed to rebuild Docker image in background task: {e}", exc_info=True)

@app.post('/rebuild-docker-image')
async def rebuild_docker_image(background_tasks: BackgroundTasks):
    """
    Endpoint to force rebuild the Docker image in the background.
    Returns immediately.
    """
    background_tasks.add_task(rebuild_image_task)
    return {"status": "success", "message": "Docker image rebuild process started in the background."}

@app.get('/docker-status')
async def docker_status():
    """Endpoint to check Docker status and image availability."""
    try:
        client = docker.from_env()
        client.ping()
        
        image_tag = "robot-test-runner:latest"
        try:
            image = client.images.get(image_tag)
            image_info = {
                "exists": True,
                "id": image.id,
                "created": image.attrs.get('Created', 'Unknown'),
                "size": f"{image.attrs.get('Size', 0) / (1024*1024):.1f} MB"
            }
        except docker.errors.ImageNotFound:
            image_info = {"exists": False}
        
        return {
            "status": "success",
            "docker_available": True,
            "image": image_info
        }
        
    except docker.errors.DockerException as e:
        return {
            "status": "error",
            "docker_available": False,
            "error": str(e)
        }
    except Exception as e:
        logging.error("Unexpected error in /docker-status endpoint", exc_info=True)
        return {
            "status": "error",
            "docker_available": False,
            "error": "An unexpected error occurred."
        }

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "robot_tests")
app.mount("/reports", StaticFiles(directory=ROBOT_TESTS_DIR), name="reports")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
