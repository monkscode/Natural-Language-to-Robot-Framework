import os
import json
import asyncio
import uuid
import logging
import docker
from fastapi import FastAPI, Request, HTTPException
from langchain_community.chat_models import ChatOllama
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv
from browser_use import Agent, ChatGoogle, Controller, BrowserProfile
from browser_use.llm.messages import UserMessage
from backend.robot_generator import generate_robot_code

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# ---------------------------

# Load environment variables from .env file
load_dotenv()
logging.info("Environment variables loaded.")


BROWSER_NAME = "chrome"

# --- Model Configuration ---
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "online") # "online" or "local"
ONLINE_MODEL = os.getenv("MODEL_NAME", "gemini-2.5-pro")
LOCAL_MODEL = os.getenv("LOCAL_MODEL_NAME", "qwen2.5-coder:14b")
logging.info(f"Model provider set to: {MODEL_PROVIDER}")
if MODEL_PROVIDER == "online":
    logging.info(f"Using online model: {ONLINE_MODEL}")
else:
    logging.info(f"Using local model: {LOCAL_MODEL}")
# -------------------------

# --- Pydantic Models for Data Validation ---
# Defines the structure for a single step in a Robot Framework test case.
class RobotStep(BaseModel):
    keyword: str = Field(description="The Robot Framework keyword to use (e.g., 'Open Browser', 'Input Text', 'Click Button').")
    locator: str = Field(description="The locator for the element to interact with (e.g., 'id=username', 'xpath=//button'). Can be an empty string if not needed.")
    value: str = Field(description="The value to use with the keyword (e.g., the text to input, the URL to open). Can be an empty string if not needed.")

# Defines the structure for a complete Robot Framework test, which consists of a list of steps.
class RobotTest(BaseModel):
    steps: List[RobotStep]

# Defines the structure of the incoming query from the frontend.
class Query(BaseModel):
    query: str
# -----------------------------------------



# Create a controller to enforce the output format from the LLM.
controller = Controller(output_model=RobotTest)

app = FastAPI()

# --- Static Files and Root Endpoint ---
# Mount the 'frontend' directory to serve static files like index.html, css, and js.
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serves the main index.html file."""
    logging.info("Root endpoint '/' accessed. Serving index.html.")
    with open(os.path.join(FRONTEND_DIR, "index.html")) as f:
        return HTMLResponse(content=f.read(), status_code=200)
# ------------------------------------

async def enhance_query(query: str, model: str) -> str:
    """
    Enhances the user's query using an LLM to make it more detailed and explicit.
    """
    logging.info(f"Enhancing query using model: {model}")
    prompt = f"""
    You are an expert at breaking down user requests into precise, step-by-step instructions for a web automation agent.
    Your output will be used to generate Robot Framework test cases.
    The user's request is: "{query}"

    Please convert this request into a numbered list of simple, explicit actions. Each action should correspond to a single browser interaction.
    For each action, provide a clear and specific locator. Locators should be in a format compatible with Robot Framework's SeleniumLibrary (e.g., 'id=element_id', 'xpath=//div[@class="some_class"]', 'css=.some-class').

    **Good Example:**
    User request: "Log in to our site with username 'testuser' and password 'password123', then navigate to the dashboard and verify the welcome message."
    Your output:
    1. Go to https://my-app.com/login.
    2. Input 'testuser' into the username field with locator 'id=username'.
    3. Input 'password123' into the password field with locator 'id=password'.
    4. Click the login button with locator 'tag=button'.
    5. Wait until the page contains the text 'Welcome, testuser!'.
    6. Verify that the element with locator 'css=.welcome-message' contains the text 'Welcome, testuser!'.

    **Bad Example (what to avoid):**
    - "Login and check the dashboard." (Too vague)
    - "Fill out the form." (Doesn't specify what to fill or where)
    - "Click the button." (Doesn't specify which button or its locator)

    Now, generate the detailed steps for the user's query.
    Enhanced query:
    """

    if MODEL_PROVIDER == "local":
        logging.info("Using local model for query enhancement.")
        llm = ChatOllama(model=LOCAL_MODEL)
    else:
        logging.info("Using online model for query enhancement.")
        llm = ChatGoogle(model=model)

    response = await llm.ainvoke([UserMessage(content=prompt.format(query=query))])
    logging.info("Query enhancement complete.")
    return response.completion.strip()

@app.post('/generate-and-run')
async def generate_and_run(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    robot_code = ""
    model_used = ""
    model = ONLINE_MODEL
    enhanced_query = await enhance_query(user_query, model)

    if MODEL_PROVIDER == "local":
        model_used = f"local: {LOCAL_MODEL}"
        llm = ChatOllama(model=LOCAL_MODEL)
    else: # "online"
        model_used = f"online: {ONLINE_MODEL}"
        llm = ChatGoogle(model=model)

    # Use browser-use to convert the query into Robot Framework steps
    browser_profile = BrowserProfile(channel=BROWSER_NAME)
    agent = Agent(
        task=enhanced_query,
        llm=llm,
        controller=controller,
        browser_profile=browser_profile,
        vision_detail_level='low'
    )

    history = await agent.run()
    result = history.final_result()

    logging.info(f"Result from agent: {result}")
    if result:
        robot_test = RobotTest.model_validate_json(result)
        robot_code = generate_robot_code(robot_test)
    else:
        raise HTTPException(status_code=500, detail="Failed to generate Robot Framework steps with online model")

    if not robot_code:
        raise HTTPException(status_code=500, detail="Failed to generate Robot Framework code")

    # Save the robot code to a file
    robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')
    os.makedirs(robot_tests_dir, exist_ok=True)
    test_filename = get_next_test_filename(robot_tests_dir)
    test_filepath = os.path.join(robot_tests_dir, test_filename)
    with open(test_filepath, 'w') as f:
        f.write(robot_code)

    # Run the test in a Docker container
    try:
        client = docker.from_env()
        image_tag = f"robot-test-runner:{uuid.uuid4()}"

        # Build the Docker image
        logging.info(f"Building Docker image: {image_tag}")
        client.images.build(path=robot_tests_dir, tag=image_tag, rm=True)
        logging.info("Docker image built successfully.")

        # Run the Docker container
        logging.info(f"Running Docker container with test: {test_filename}")
        container = client.containers.run(
            image=image_tag,
            command=["robot", test_filename],
            volumes={os.path.abspath(test_filepath): {'bind': f'/app/{test_filename}', 'mode': 'ro'}},
            working_dir="/app",
            stderr=True,
            stdout=True,
            detach=False,
            auto_remove=True,
            add_hosts={"host.docker.internal": "host-gateway"} # For Ollama connection from container to host
        )

        logs = container.decode('utf-8')
        logging.info("Docker container finished execution.")

        return {'model_used': model_used, 'robot_code': robot_code, 'logs': logs}
    except docker.errors.BuildError as e:
        logging.error(f"Docker build failed: {e}")
        logs = "Docker build failed:\n"
        for line in e.build_log:
            if 'stream' in line:
                logs += line['stream']
        # Return the logs to the user, but don't raise an HTTPException
        return {'model_used': model_used, 'robot_code': robot_code, 'logs': logs}
    except docker.errors.ContainerError as e:
        logging.error(f"Docker container failed: {e}")
        # The container error message is often very useful for debugging
        logs = f"Docker container exited with error:\n{e.stderr.decode('utf-8')}"
        return {'model_used': model_used, 'robot_code': robot_code, 'logs': logs}
    except Exception as e:
        logging.error(f"An unexpected error occurred during test execution: {e}")
        raise HTTPException(status_code=500, detail={"error": "An unexpected error occurred during test execution", "details": str(e)})

def get_next_test_filename(directory: str) -> str:
    """
    Finds the next available test filename in the format testN.robot.
    """
    if not os.path.exists(directory):
        return "test1.robot"

    test_files = [f for f in os.listdir(directory) if f.startswith('test') and f.endswith('.robot')]
    if not test_files:
        return "test1.robot"

    max_num = 0
    for f in test_files:
        try:
            num = int(f.replace('test', '').replace('.robot', ''))
            if num > max_num:
                max_num = num
        except ValueError:
            # Ignore files that don't match the pattern, e.g., test_abc.robot
            continue

    return f"test{max_num + 1}.robot"
