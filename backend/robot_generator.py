import re
from typing import List, Optional
from pydantic import BaseModel

# --- Pydantic Models ---
class RobotStep(BaseModel):
    keyword: str
    locator: Optional[str] = None
    value: Optional[str] = None

class RobotTest(BaseModel):
    steps: List[RobotStep]
    teardown: Optional[str] = None

# --- Agent Functions ---
def analyze_query(natural_language_query: str) -> dict:
    """Agent 1: Analyzes the query to extract key information."""
    query_lower = natural_language_query.lower()
    search_term_match = re.search(r"search for '(.+?)'", query_lower)
    if "google" in query_lower and search_term_match:
        original_search_term_match = re.search(r"search for '(.+?)'", natural_language_query, re.IGNORECASE)
        if original_search_term_match:
            return {"test_type": "web_search", "search_term": original_search_term_match.group(1)}
    return {}

def find_best_locator(element_description: str) -> Optional[str]:
    """Agent 2: Finds the best locator for an element."""
    if "search input" in element_description.lower():
        return '[aria-label="Search"]'
    return None

def create_test_from_analysis(analysis: dict) -> RobotTest:
    """Agent 3: Creates a structured RobotTest object."""
    steps = []
    teardown = None
    if analysis.get("test_type") == "web_search":
        search_term = analysis["search_term"]
        input_locator = find_best_locator("the search input")
        steps.extend([
            RobotStep(keyword="Open Browser", value=f"https://www.google.com    chrome"),
            RobotStep(keyword="Input Text", locator=input_locator, value=search_term),
            RobotStep(keyword="Press Keys", locator=input_locator, value="ENTER"),
            RobotStep(keyword="Wait Until Page Contains", value=search_term),
            RobotStep(keyword="Title Should Be", value=f"{search_term} - Google Search")
        ])
        teardown = "Close Browser"
    return RobotTest(steps=steps, teardown=teardown)

def generate_robot_code(robot_test: RobotTest) -> str:
    """Agent 4: Generates Robot Framework code by manually building the string."""
    lines = ["*** Settings ***", "Library    SeleniumLibrary", "", "*** Test Cases ***", "User Defined Test"]
    for step in robot_test.steps:
        line = f"    {step.keyword}"
        if step.locator:
            line += f"    {step.locator}"
        if step.value:
            line += f"    {step.value}"
        lines.append(line)
    if robot_test.teardown:
        lines.append(f"    [Teardown]    {robot_test.teardown}")
    return "\n".join(lines)

def review_generated_code(robot_code: str) -> bool:
    """Agent 5: Validates the generated Robot Framework code."""
    if "*** Settings ***" not in robot_code or "*** Test Cases ***" not in robot_code:
        return False
    return True

# --- Main Workflow Function ---
def run_agentic_workflow(natural_language_query: str) -> Optional[str]:
    """
    Orchestrates the agentic workflow to generate Robot Framework code.
    Returns the generated code as a string, or None if any step fails.
    """
    analysis_result = analyze_query(natural_language_query)
    if not analysis_result:
        return None

    robot_test_object = create_test_from_analysis(analysis_result)
    robot_code = generate_robot_code(robot_test_object)

    if not review_generated_code(robot_code):
        return None

    return robot_code
