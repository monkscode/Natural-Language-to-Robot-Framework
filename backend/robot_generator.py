import os
import json
import logging
import time
from typing import List, Optional
from pydantic import BaseModel, Field
import google.generativeai as genai
import ollama

# --- Pydantic Models for Agent Communication ---
# These models define the "contracts" for data passed between agents.

class PlannedStep(BaseModel):
    """The output of the Step-Planning Agent."""
    step_description: str = Field(description="A high-level description of a single action to perform.")
    element_description: Optional[str] = Field(None, description="The description of the UI element to interact with, if any.")
    value: Optional[str] = Field(None, description="The value to be used in the action, e.g., text to input or a URL.")
    keyword: str = Field(description="The suggested Robot Framework keyword to use for this step (e.g., 'Input Text', 'Click Element').")

class LocatedStep(PlannedStep):
    """The output of the Element-Identification Agent."""
    locator: Optional[str] = Field(None, description="The Robot Framework locator for the element (e.g., 'id=search-bar').")

# --- Agent Implementations ---

def agent_step_planner(query: str, model_provider: str, model_name: str) -> List[PlannedStep]:
    """Agent 1 (AI): Breaks the query into a structured plan of high-level steps."""
    logging.info(f"Step Planner: Analyzing query with {model_provider} model '{model_name}'.")
    prompt = f"""
    You are an expert test automation planner for Robot Framework. Your task is to break down a natural language query into a structured series of high-level test steps.
    The user query is: "{query}"

    Respond with a JSON array of objects. Each object must have the following keys:
    - "step_description": A clear, high-level description of the action (e.g., "Navigate to a URL", "Input text into a field", "Click an element").
    - "element_description": A description of the UI element involved (e.g., "the search input field", "the search button"). This can be null if the action doesn't involve an element.
    - "value": The value to use, such as a URL or the text to type. This can be null.
    - "keyword": The most appropriate Robot Framework keyword from SeleniumLibrary for this action. Examples: 'Open Browser', 'Input Text', 'Click Element', 'Press Keys', 'Title Should Be'.

    Example Query: "go to google.com and search for 'robot framework'"
    Example JSON Response:
    [
        {{
            "step_description": "Navigate to a URL",
            "element_description": null,
            "value": "https://www.google.com",
            "keyword": "Open Browser"
        }},
        {{
            "step_description": "Input text into the search field",
            "element_description": "the search input field",
            "value": "robot framework",
            "keyword": "Input Text"
        }},
        {{
            "step_description": "Press the ENTER key on the search field",
            "element_description": "the search input field",
            "value": "ENTER",
            "keyword": "Press Keys"
        }}
    ]
    """
    try:
        if model_provider == "local":
            response = ollama.chat(
                model=model_name,
                messages=[{'role': 'user', 'content': prompt}],
                options={'temperature': 0.0},
                format="json"
            )
            cleaned_response = response['message']['content']
        else: # Default to online
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            cleaned_response = response.text.strip().lstrip("```json").rstrip("```").strip()

        planned_steps_data = json.loads(cleaned_response)

        # Handle both single object and list of objects
        if isinstance(planned_steps_data, dict):
            planned_steps_data = [planned_steps_data]

        return [PlannedStep(**step) for step in planned_steps_data]
    except Exception as e:
        raw_response = ""
        if model_provider == "local" and 'response' in locals():
            raw_response = response['message']['content']
        elif 'response' in locals():
            raw_response = response.text
        logging.error(f"Step Planner Agent failed: {e}\nRaw response was:\n{raw_response}")
        return []

def agent_element_identifier(steps: List[PlannedStep], model_provider: str, model_name: str) -> List[LocatedStep]:
    """Agent 2 (AI): For each step, finds the best locator for the described element."""
    logging.info(f"Element Identifier: Finding locators with {model_provider} model '{model_name}'.")
    located_steps = []
    delay_seconds = int(os.getenv("SECONDS_BETWEEN_API_CALLS", "0"))

    for i, step in enumerate(steps):
        if step.element_description:
            if i > 0 and delay_seconds > 0:
                logging.info(f"Element Identifier: Waiting for {delay_seconds} second(s).")
                time.sleep(delay_seconds)

            prompt = f"""
            You are an expert in Selenium and Robot Framework locators. Your task is to find the best, most stable locator for a given web element based on its description.
            The element is described as: "{step.element_description}"
            The action to be performed is: "{step.step_description}"
            The value associated with the action is: "{step.value or 'N/A'}"

            Respond with a single JSON object with one key: "locator".
            The value must be a valid Robot Framework locator string.

            Example 1:
            - Element Description: "the search button"
            - JSON Response: {{"locator": "css=button[aria-label='Google Search']"}}

            Example 2:
            - Element Description: "the first video in the search results"
            - JSON Response: {{"locator": "xpath=(//ytd-video-renderer)[1]"}}

            Example 3:
            - Element Description: "the search input field"
            - JSON Response: {{"locator": "name=search_query"}}

            Now, provide the JSON response for the element described above.
            """
            try:
                if model_provider == "local":
                    response = ollama.chat(
                        model=model_name,
                        messages=[{'role': 'user', 'content': prompt}],
                        options={'temperature': 0.0},
                        format="json"
                    )
                    cleaned_response = response['message']['content']
                else: # Default to online
                    model = genai.GenerativeModel(model_name)
                    response = model.generate_content(prompt)
                    cleaned_response = response.text.strip().lstrip("```json").rstrip("```").strip()

                locator_data = json.loads(cleaned_response)

                # Handle both single object and list of objects
                if isinstance(locator_data, dict):
                    locator_data = [locator_data]

                final_locator = locator_data[0].get("locator") if isinstance(locator_data, list) and locator_data and isinstance(locator_data[0], dict) else None

                located_steps.append(LocatedStep(**step.model_dump(), locator=final_locator))
            except Exception as e:
                raw_response = ""
                if model_provider == "local" and 'response' in locals():
                    raw_response = response['message']['content']
                elif 'response' in locals():
                    raw_response = response.text
                logging.error(f"Element Identifier Agent failed for step '{step.step_description}': {e}\nRaw response was:\n{raw_response}")
                located_steps.append(LocatedStep(**step.model_dump(), locator=None))
        else:
            located_steps.append(LocatedStep(**step.model_dump()))
    return located_steps

def agent_code_assembler(steps: List[LocatedStep], query: str) -> str:
    """Agent 3 (Logic): Assembles the final Robot Framework code from the structured steps."""
    logging.info("Code Assembler: Building the final .robot file.")
    lines = [
        "*** Settings ***",
        "Library    SeleniumLibrary",
        "",
        "*** Test Cases ***",
        f"Test Case From Query: {query}"
    ]
    for step in steps:
        line = f"    {step.keyword}"
        if step.locator:
            line += f"    {step.locator}"
        if step.value:
            line += f"    {step.value}"
        lines.append(line)
    lines.append("    [Teardown]    Close Browser")
    return "\n".join(lines)

# --- Orchestrator ---

def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str) -> Optional[str]:
    """
    Orchestrates the multi-agent workflow to generate Robot Framework code.
    """
    logging.info("--- Starting Multi-Agent Workflow ---")

    # Configure online provider if used
    if model_provider == "online":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logging.error("Orchestrator: GEMINI_API_KEY not found for online provider.")
            raise ValueError("GEMINI_API_KEY not found in environment variables.")
        genai.configure(api_key=api_key)

    # Agent 1: Plan
    planned_steps = agent_step_planner(natural_language_query, model_provider, model_name)
    if not planned_steps:
        logging.error("Orchestrator: Step Planner failed. Aborting.")
        return None

    # Agent 2: Identify Locators
    located_steps = agent_element_identifier(planned_steps, model_provider, model_name)
    if not located_steps:
        logging.error("Orchestrator: Element Identifier failed. Aborting.")
        return None

    # Agent 3: Assemble Code
    robot_code = agent_code_assembler(located_steps, natural_language_query)

    logging.info("--- Multi-Agent Workflow Complete ---")
    return robot_code
