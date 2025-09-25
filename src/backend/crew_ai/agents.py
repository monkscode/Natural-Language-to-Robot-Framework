import os
from crewai import Agent
from crewai.llm import LLM
# from langchain_community.llms import Ollama
from crewai_tools import SeleniumScrapingTool, ScrapeElementFromWebsiteTool
# Import browser_use_tool to ensure it's included in the package
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from tools.browser_use_tool import BrowserUseTool
from langchain_ollama import OllamaLLM

# Initialize the LLMs
def get_llm(model_provider, model_name):
    if model_provider == "local":
        return OllamaLLM(model=model_name)
    else:
        return LLM(
            api_key=os.getenv("GEMINI_API_KEY"),
            model=f"{model_name}",
            num_retries=5,
        )

# Initialize the tools (FIXED: Create instances instead of passing classes)
selenium_tool = SeleniumScrapingTool()
scrape_tool = ScrapeElementFromWebsiteTool()
browser_use_tool = BrowserUseTool()  # Create instance here

class RobotAgents:
    def __init__(self, model_provider, model_name):
        self.llm = get_llm(model_provider, model_name)

    def step_planner_agent(self) -> Agent:
        return Agent(
            role="Test Automation Planner",
            goal="Break down a natural language query into a structured series of high-level test steps for Robot Framework.",
            backstory="You are an expert test automation planner. Your task is to analyze a user's query and convert it into a structured plan of test steps. You must be meticulous and ensure that all actions mentioned in the query are converted into a step.",
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def element_identifier_agent(self) -> Agent:
        return Agent(
            role="Web Element Locator Specialist",
            goal="Generate the most reliable and stable locator for web elements based on their description.",
            backstory="You are an expert in web element identification for Robot Framework automation. Your task is to find the best locator for a given web element description, following a strict priority order of locator strategies. You must be precise and provide only valid Robot Framework locators. You must use the provided tools to assist in locating elements on web pages.",
            tools=[browser_use_tool],  # FIXED: Use instance instead of class
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_assembler_agent(self) -> Agent:
        return Agent(
            role="Robot Framework Code Assembler",
            goal="Assemble the final Robot Framework code from structured steps.",
            backstory="You are a meticulous code assembler. Your task is to take a list of structured test steps and assemble them into a complete and syntactically correct Robot Framework test case. You must ensure the code is clean, readable, and follows the standard Robot Framework syntax.",
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def code_validator_agent(self) -> Agent:
        return Agent(
            role="Robot Framework Linter and Quality Assurance Engineer",
            goal="Validate the generated Robot Framework code for correctness and adherence to critical rules.",
            backstory="You are an expert Robot Framework linter. Your sole task is to validate the provided Robot Framework code for syntax errors, correct keyword usage, and adherence to critical rules. You must be thorough and provide a clear validation result.",
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )
