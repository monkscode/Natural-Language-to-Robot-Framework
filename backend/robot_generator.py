from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel
from typing import List
import os

class RobotStep(BaseModel):
    keyword: str
    locator: str
    value: str

class RobotTest(BaseModel):
    steps: List[RobotStep]

# Path to the templates directory
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Jinja2 environment
env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(['html', 'xml']),
    trim_blocks=True,
    lstrip_blocks=True
)

def generate_robot_code(robot_test: RobotTest) -> str:
    """
    Generates Robot Framework code from a RobotTest object using a Jinja2 template.
    """
    template = env.get_template("robot_test.jinja2")
    return template.render(test=robot_test)
