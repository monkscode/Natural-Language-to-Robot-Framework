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
    model: str = "gemini-1.5-pro-latest"

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
        client = docker.from_env()
        image_tag = "robot-test-runner:latest"
        dockerfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')

        # Stage 2a: Building Docker Image
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': 'Building container image for test execution...'})}\n\n"
        client.images.build(path=dockerfile_path, tag=image_tag, rm=True)

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
        logs = container_logs.decode('utf-8')

        log_html_path = f"/reports/{run_id}/log.html"
        report_html_path = f"/reports/{run_id}/report.html"

        final_result = {
            'logs': logs,
            'log_html': log_html_path,
            'report_html': report_html_path
        }
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'complete', 'message': 'Test execution finished.', 'result': final_result})}\n\n"

    except docker.errors.BuildError as e:
        logging.error(f"Docker build failed: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Docker build failed: {e}'})}\n\n"
    except docker.errors.ContainerError as e:
        logging.error(f"Docker container failed: {e}")
        error_logs = f"Docker container exited with error code {e.exit_status}.\n"
        if hasattr(e, 'logs') and e.logs:
            error_logs += f"Container Logs:\n{e.logs.decode('utf-8', errors='ignore')}"
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': error_logs})}\n\n"
    except Exception as e:
        logging.error(f"An unexpected error occurred during execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


@app.post('/generate-and-run')
async def generate_and_run_streaming(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = os.getenv("MODEL_PROVIDER", "online").lower()
    if model_provider == "local":
        model_name = os.getenv("LOCAL_MODEL", "llama3")
        logging.info(f"Using local model provider: {model_name}")
    else:
        model_name = os.getenv("ONLINE_MODEL", query.model)
        logging.info(f"Using online model provider: {model_name}")

    return StreamingResponse(stream_generate_and_run(user_query, model_name), media_type="text/event-stream")

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "robot_tests")
app.mount("/reports", StaticFiles(directory=ROBOT_TESTS_DIR), name="reports")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
