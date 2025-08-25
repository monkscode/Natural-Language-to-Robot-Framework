import os
import json
import asyncio
import uuid
from quart import Quart, request, jsonify, render_template
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv
from browser_use import Agent, ChatGoogle, Controller

# Load environment variables from .env file
load_dotenv()

# Define the output format as a Pydantic model
class RobotStep(BaseModel):
    keyword: str = Field(description="The Robot Framework keyword to use (e.g., 'Open Browser', 'Input Text', 'Click Button').")
    locator: str = Field(description="The locator for the element to interact with (e.g., 'id=username', 'xpath=//button'). Can be an empty string if not needed.")
    value: str = Field(description="The value to use with the keyword (e.g., the text to input, the URL to open). Can be an empty string if not needed.")

class RobotTest(BaseModel):
    steps: List[RobotStep]

# Create a controller to enforce the output format
controller = Controller(output_model=RobotTest)

app = Quart(__name__, static_folder='../frontend', template_folder='../frontend')

@app.route('/')
async def index():
    return await render_template('index.html')

@app.route('/generate-and-run', methods=['POST'])
async def generate_and_run():
    data = await request.get_json()
    if not data or 'query' not in data:
        return jsonify({'error': 'Query not provided'}), 400

    user_query = data['query']

    # Use browser-use to convert the query into Robot Framework steps
    llm = ChatGoogle(model='gemini-1.5-flash')
    agent = Agent(task=user_query, llm=llm, controller=controller)

    history = await agent.run()
    result = history.final_result()

    if result:
        robot_test = RobotTest.model_validate_json(result)
        robot_code = generate_robot_code(robot_test)

        # Save the robot code to a file
        robot_tests_dir = os.path.abspath('../robot_tests')
        os.makedirs(robot_tests_dir, exist_ok=True)
        test_filename = f"{uuid.uuid4()}.robot"
        test_filepath = os.path.join(robot_tests_dir, test_filename)
        with open(test_filepath, 'w') as f:
            f.write(robot_code)

        # Run the test in a Docker container
        try:
            image_tag = "robot-framework-runner"

            # WARNING: Running Docker with sudo from a web application is a security risk.
            # This is a workaround for the environment's permission issues.
            # In a production environment, you should configure Docker to run as a non-root user.
            # Build the image using asyncio.create_subprocess_exec
            build_command = ["sudo", "docker", "build", "-t", image_tag, "."]
            process = await asyncio.create_subprocess_exec(
                *build_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="backend"
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return jsonify({'error': 'Docker build failed', 'logs': stderr.decode()}), 500

            # Run the container using asyncio.create_subprocess_exec
            run_command = [
                "sudo", "docker", "run", "--rm",
                "-v", f"{os.path.abspath('../robot_tests')}:/home/robot/tests",
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

            return jsonify({'robot_code': robot_code, 'logs': logs})
        except Exception as e:
            return jsonify({'error': 'An unexpected error occurred', 'details': str(e)}), 500
    else:
        return jsonify({'error': 'Failed to generate Robot Framework steps'}), 500

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

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
