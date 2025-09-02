import os
import json
import asyncio
import uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv
from browser_use import Agent, ChatGoogle, Controller, BrowserProfile
from browser_use.llm.messages import UserMessage

# Load environment variables from .env file
load_dotenv()

BROWSER_NAME = "chrome"

# Define the output format as a Pydantic model
class RobotStep(BaseModel):
    keyword: str = Field(description="The Robot Framework keyword to use (e.g., 'Open Browser', 'Input Text', 'Click Button').")
    locator: str = Field(description="The locator for the element to interact with (e.g., 'id=username', 'xpath=//button'). Can be an empty string if not needed.")
    value: str = Field(description="The value to use with the keyword (e.g., the text to input, the URL to open). Can be an empty string if not needed.")

class RobotTest(BaseModel):
    steps: List[RobotStep]

class Query(BaseModel):
    query: str
    model: str = "models/gemini-1.5-pro-latest"


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

async def enhance_query(query: str, model: str) -> str:
    """
    Enhances the user's query using an LLM to make it more detailed and explicit.
    """
    llm = ChatGoogle(model=model)
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
    response = await llm.ainvoke([UserMessage(content=prompt.format(query=query))])
    return response.completion.strip()

@app.post('/generate-and-run')
async def generate_and_run(query: Query):
    user_query = query.query
    model = query.model
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    enhanced_query = await enhance_query(user_query, model)

    # Use browser-use to convert the query into Robot Framework steps
    llm = ChatGoogle(model=model)
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

    if result:
        robot_test = RobotTest.model_validate_json(result)
        robot_code = await generate_robot_code_with_llm(robot_test, model)

        # Save the robot code to a file
        robot_tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'robot_tests')
        os.makedirs(robot_tests_dir, exist_ok=True)
        test_filename = get_next_test_filename(robot_tests_dir)
        test_filepath = os.path.join(robot_tests_dir, test_filename)
        with open(test_filepath, 'w') as f:
            f.write(robot_code)

        # Run the test directly
        try:
            run_command = ["robot", test_filename]
            process = await asyncio.create_subprocess_exec(
                *run_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=robot_tests_dir
            )
            stdout, stderr = await process.communicate()

            logs = stdout.decode() + stderr.decode()

            return {'robot_code': robot_code, 'logs': logs}
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "An unexpected error occurred", "details": str(e)})
    else:
        raise HTTPException(status_code=500, detail="Failed to generate Robot Framework steps")

async def generate_robot_code_with_llm(robot_test: RobotTest, model: str) -> str:
    """
    Generates the full content of a .robot file using an LLM.
    """
    llm = ChatGoogle(model=model)

    # Convert the robot_test object to a JSON string for the prompt
    steps_json = robot_test.model_dump_json(indent=2)

    prompt = f"""
    You are an expert in Robot Framework. Your task is to generate a complete and valid .robot file based on a list of steps provided in JSON format.

    The generated file must include all necessary sections: `*** Settings ***`, `*** Test Cases ***`.
    Under `*** Settings ***`, you must include `Library    SeleniumLibrary`.
    The test case should be named "User Defined Test".

    Here is the JSON object containing the test steps:
    {steps_json}

    Please generate the full .robot file content. Do not include any explanations or markdown formatting in your response, only the raw Robot Framework code.
    """

    response = await llm.ainvoke([UserMessage(content=prompt)])
    return response.completion.strip()



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
