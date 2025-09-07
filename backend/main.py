import os
import uuid
import logging
import docker
from fastapi import FastAPI, HTTPException
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
    while workflow_thread.is_alive():
        try:
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"

            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
                # Generation is done, we can break this loop and move to execution
                workflow_thread.join() # Ensure thread is cleaned up
                break
            elif event.get("status") == "error":
                logging.error(f"Error during code generation: {event.get('message')}")
                workflow_thread.join()
                return
        except Empty:
            # Send a heartbeat comment to keep the connection open
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    # In case the thread finished but we didn't get the code
    if not robot_code:
        # Check the queue one last time
        while not q.empty():
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"
            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
            elif event.get("status") == "error":
                return # Error was already sent

        if not robot_code:
            final_error_message = "Agentic workflow finished without generating code."
            logging.error(final_error_message)
            yield f"data: {json.dumps({'stage': 'generation', 'status': 'error', 'message': final_error_message})}\n\n"
            return


    # --- Stage 2: Docker Execution ---
    run_id = str(uuid.uuid4())
    robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests', run_id)
    os.makedirs(robot_tests_dir, exist_ok=True)
    test_filename = "test.robot"
    test_filepath = os.path.join(robot_tests_dir, test_filename)
    with open(test_filepath, 'w') as f:
        f.write(robot_code)

    try:
        # Check if Docker is available before proceeding
        try:
            client = docker.from_env()
            # Test if Docker daemon is responsive
            client.ping()
        except docker.errors.DockerException as docker_err:
            error_message = "Docker is not available. Please ensure Docker Desktop is installed and running."
            if "CreateFile" in str(docker_err) or "file specified" in str(docker_err):
                error_message += "\n\nWindows Error: Docker daemon is not accessible. This usually means:\n"
                error_message += "1. Docker Desktop is not installed\n"
                error_message += "2. Docker Desktop is not running\n"
                error_message += "3. Docker service is not started\n\n"
                error_message += "Please start Docker Desktop and try again."
            logging.error(f"Docker connection failed: {docker_err}")
            yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': error_message})}\n\n"
            return
        
        image_tag = "robot-test-runner:latest"
        dockerfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')

        # Stage 2a: Check and Build Docker Image (only if needed)
        try:
            # Check if the image already exists
            existing_image = client.images.get(image_tag)
            logging.info(f"Docker image '{image_tag}' already exists. Skipping build.")
            yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': 'Using existing container image for test execution...'})}\n\n"
        except docker.errors.ImageNotFound:
            # Image doesn't exist, need to build it
            logging.info(f"Docker image '{image_tag}' not found. Building new image.")
            yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': 'Building container image for test execution (first time only)...'})}\n\n"
            try:
                client.images.build(path=dockerfile_path, tag=image_tag, rm=True)
                logging.info(f"Successfully built Docker image '{image_tag}'.")
                yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': 'Container image built successfully!'})}\n\n"
            except docker.errors.BuildError as build_err:
                logging.error(f"Failed to build Docker image: {build_err}")
                yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Docker image build failed: {build_err}'})}\n\n"
                return

        # Stage 2b: Running Docker Container
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': 'Executing test inside the container...'})}\n\n"
        robot_command = ["robot", "--outputdir", f"/app/robot_tests/{run_id}", f"robot_tests/{run_id}/{test_filename}"]

        container_logs = client.containers.run(
            image=image_tag,
            command=robot_command,
            volumes={os.path.abspath(os.path.join(robot_tests_dir, '..')): {'bind': '/app/robot_tests', 'mode': 'rw'}},
            working_dir="/app",
            stderr=True,
            stdout=True,
            detach=False,
            auto_remove=True
        )

        # This block handles the case where all tests pass (exit code 0)
        logs = container_logs.decode('utf-8')
        message = "Test execution finished: All tests passed."
        log_html_path = f"/reports/{run_id}/log.html"
        report_html_path = f"/reports/{run_id}/report.html"
        final_result = { 'logs': logs, 'log_html': log_html_path, 'report_html': report_html_path }
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'complete', 'message': message, 'result': final_result})}\n\n"

    except docker.errors.ContainerError as e:
        # A non-zero exit code from the container. We must check if it was a test failure or a system error.
        log_file_path = os.path.join(robot_tests_dir, "log.html")

        # If log.html exists, Robot Framework ran and produced a report. This is a TEST failure.
        if os.path.exists(log_file_path):
            logging.warning(f"Robot test execution finished with failures (exit code {e.exit_status}). This is a test failure, not a system error.")
            logs = e.container.logs().decode('utf-8', errors='ignore')
            report_html_url = f"/reports/{run_id}/report.html"
            log_html_url = f"/reports/{run_id}/log.html"
            final_result = { 'logs': logs, 'log_html': log_html_url, 'report_html': report_html_url }
            message = f"Test execution finished: Some tests failed (exit code {e.exit_status})."
            yield f"data: {json.dumps({'stage': 'execution', 'status': 'complete', 'message': message, 'result': final_result})}\n\n"

        # If log.html does NOT exist, the test runner itself failed. This is a SYSTEM error.
        else:
            logging.error(f"Docker container failed before Robot Framework could generate a report (exit code {e.exit_status}).")
            error_logs = f"Docker container exited with a system error (exit code {e.exit_status}).\n"
            error_logs += "Robot Framework reports were not generated, indicating a problem with the test runner itself.\n\n"
            error_logs += f"Container Logs:\n{e.container.logs().decode('utf-8', errors='ignore')}"
            yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': error_logs})}\n\n"

    except docker.errors.BuildError as e:
        logging.error(f"Docker build failed: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Docker build failed: {e}'})}\n\n"
    except docker.errors.DockerException as e:
        error_message = f"Docker error: {e}"
        if "CreateFile" in str(e) or "file specified" in str(e):
            error_message = "Docker connection lost during execution. Please ensure Docker Desktop is running and try again."
        logging.error(f"Docker error during execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': error_message})}\n\n"
    except Exception as e:
        error_message = str(e)
        if "CreateFile" in str(e) or "file specified" in str(e):
            error_message = "System error: Docker is not accessible. Please ensure Docker Desktop is installed and running."
        logging.error(f"An unexpected error occurred during execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': error_message})}\n\n"


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

@app.post('/rebuild-docker-image')
async def rebuild_docker_image():
    """Endpoint to force rebuild the Docker image when needed."""
    try:
        client = docker.from_env()
        image_tag = "robot-test-runner:latest"
        dockerfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')
        
        # Remove existing image if it exists
        try:
            existing_image = client.images.get(image_tag)
            client.images.remove(image=image_tag, force=True)
            logging.info(f"Removed existing Docker image '{image_tag}'.")
        except docker.errors.ImageNotFound:
            logging.info(f"No existing Docker image '{image_tag}' to remove.")
        
        # Build new image
        logging.info(f"Building new Docker image '{image_tag}'.")
        client.images.build(path=dockerfile_path, tag=image_tag, rm=True)
        logging.info(f"Successfully rebuilt Docker image '{image_tag}'.")
        
        return {"status": "success", "message": f"Docker image '{image_tag}' rebuilt successfully."}
        
    except docker.errors.DockerException as e:
        error_message = f"Docker error: {e}"
        logging.error(f"Failed to rebuild Docker image: {e}")
        raise HTTPException(status_code=500, detail=error_message)
    except Exception as e:
        error_message = f"Unexpected error: {e}"
        logging.error(f"Unexpected error during Docker image rebuild: {e}")
        raise HTTPException(status_code=500, detail=error_message)

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
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "robot_tests")
app.mount("/reports", StaticFiles(directory=ROBOT_TESTS_DIR), name="reports")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
