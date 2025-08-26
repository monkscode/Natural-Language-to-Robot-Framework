import os
import json
import asyncio
import shutil
import uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv
from browser_use import Agent, ChatGoogle, Controller, BrowserProfile

# Load environment variables from .env file
load_dotenv()

# Define the output format as a Pydantic model
class RobotStep(BaseModel):
    keyword: str = Field(description="The Robot Framework keyword to use (e.g., 'Open Browser', 'Input Text', 'Click Button').")
    locator: str = Field(description="The locator for the element to interact with (e.g., 'id=username', 'xpath=//button'). Can be an empty string if not needed.")
    value: str = Field(description="The value to use with the keyword (e.g., the text to input, the URL to open). Can be an empty string if not needed.")

class RobotTest(BaseModel):
    steps: List[RobotStep]

class Query(BaseModel):
    query: str

# Create a controller to enforce the output format
controller = Controller(output_model=RobotTest)

app = FastAPI()

# Define the path to the frontend directory, relative to this file
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open(os.path.join(FRONTEND_DIR, "index.html")) as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.post('/generate-and-run')
async def generate_and_run(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    # Use browser-use to convert the query into Robot Framework steps
    llm = ChatGoogle(model='gemini-1.5-flash')
    browser_profile = BrowserProfile(headless=True)
    agent = Agent(
        task=user_query,
        llm=llm,
        controller=controller,
        browser_profile=browser_profile,
        vision_detail_level='low'
    )

    history = await agent.run()
    result = history.final_result()

    if result:
        robot_test = RobotTest.model_validate_json(result)
        robot_code = generate_robot_code(robot_test)

        # Save the robot code to a file
        robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')
        os.makedirs(robot_tests_dir, exist_ok=True)
        test_filename = get_next_test_filename(robot_tests_dir)
        test_filepath = os.path.join(robot_tests_dir, test_filename)
        with open(test_filepath, 'w') as f:
            f.write(robot_code)

        # Run the test in a Docker container
        try:
            if not shutil.which("docker"):
                raise HTTPException(status_code=500, detail="Docker is not installed or not in the system's PATH. Please install Docker to run the robot tests.")

            image_tag = "robot-framework-runner"

            # Build the image using asyncio.create_subprocess_exec
            build_command = ["docker", "build", "-t", image_tag, "."]
            process = await asyncio.create_subprocess_exec(
                *build_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="backend"
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                raise HTTPException(status_code=500, detail={"error": "Docker build failed", "logs": stderr.decode()})

            # Run the container using asyncio.create_subprocess_exec
            run_command = [
                "docker", "run", "--rm",
                "-v", f"{robot_tests_dir}:/home/robot/tests",
                "-w", "/home/robot/tests",
                image_tag,
                "robot", test_filename
            ]
            process = await asyncio.create_subprocess_exec(
                *run_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            logs = stdout.decode() + stderr.decode()

            return {'robot_code': robot_code, 'logs': logs}
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "An unexpected error occurred", "details": str(e)})
    else:
        raise HTTPException(status_code=500, detail="Failed to generate Robot Framework steps")

def generate_robot_code(robot_test: RobotTest) -> str:
    """Generates the content of a .robot file from a RobotTest object."""
    code = "*** Settings ***\n"
    code += "Library    SeleniumLibrary\n\n"
    code += "*** Test Cases ***\n"
    code += "User Defined Test\n"
    code += "    [Documentation]    Test case generated from user query\n"

    for step in robot_test.steps:
        # A simple way to format the line, might need improvement
        line = f"    {step.keyword}"
        if step.locator:
            line += f"    {step.locator}"
        if step.value:
            line += f"    {step.value}"
        code += line + "\n"

    return code

@app.post('/generate-and-run-test')
async def generate_and_run_test(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    # Mock result from agent.run()
    result = """
{
    "steps": [
        {
            "keyword": "Open Browser",
            "locator": "https://www.google.com/search?q=Robot+Framework",
            "value": "browser=chrome"
        },
        {
            "keyword": "Close Browser",
            "locator": "",
            "value": ""
        }
    ]
}
"""

    if result:
        robot_test = RobotTest.model_validate_json(result)
        robot_code = generate_robot_code(robot_test)

        # Save the robot code to a file
        robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')
        os.makedirs(robot_tests_dir, exist_ok=True)
        test_filename = get_next_test_filename(robot_tests_dir)
        test_filepath = os.path.join(robot_tests_dir, test_filename)
        with open(test_filepath, 'w') as f:
            f.write(robot_code)

        # Run the test in a Docker container
        try:
            if not shutil.which("docker"):
                raise HTTPException(status_code=500, detail="Docker is not installed or not in the system's PATH. Please install Docker to run the robot tests.")

            image_tag = "robot-framework-runner"

            # Build the image using asyncio.create_subprocess_exec
            build_command = ["docker", "build", "-t", image_tag, "."]
            process = await asyncio.create_subprocess_exec(
                *build_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="backend"
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                raise HTTPException(status_code=500, detail={"error": "Docker build failed", "logs": stderr.decode()})

            # Run the container using asyncio.create_subprocess_exec
            run_command = [
                "docker", "run", "--rm",
                "-v", f"{robot_tests_dir}:/home/robot/tests",
                "-w", "/home/robot/tests",
                image_tag,
                "robot", test_filename
            ]
            process = await asyncio.create_subprocess_exec(
                *run_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            logs = stdout.decode() + stderr.decode()

            return {'robot_code': robot_code, 'logs': logs}
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "An unexpected error occurred", "details": str(e)})
    else:
        raise HTTPException(status_code=500, detail="Failed to generate Robot Framework steps")

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
