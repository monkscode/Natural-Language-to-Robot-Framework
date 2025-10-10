from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks
import re
import logging

logger = logging.getLogger(__name__)

def extract_url_from_query(query: str) -> str:
    """
    Dynamically extract URL from user query using regex patterns.
    Returns the URL if found, otherwise returns a generic placeholder.
    """
    # Pattern 1: Full URLs with protocol (http:// or https://)
    url_pattern = r'https?://[^\s]+'
    match = re.search(url_pattern, query, re.IGNORECASE)
    if match:
        url = match.group(0).rstrip('.,;!?')  # Remove trailing punctuation
        logger.info(f"Extracted full URL from query: {url}")
        return url
    
    # Pattern 2: Domain names with common TLDs (www.example.com, example.in, etc.)
    domain_pattern = r'\b(?:www\.)?([a-zA-Z0-9-]+\.(?:com|in|org|net|co|io|ai|app|dev|tech))\b'
    match = re.search(domain_pattern, query, re.IGNORECASE)
    if match:
        domain = match.group(0)
        # Add https:// if not present
        url = f"https://{domain}" if not domain.startswith('http') else domain
        logger.info(f"Extracted domain from query and constructed URL: {url}")
        return url
    
    # Pattern 3: Website names without TLD (e.g., "on flipkart", "amazon", "google")
    # Try to extract potential website name and construct URL
    website_pattern = r'\b(?:on|from|at|in|visit|go to|open)\s+([a-zA-Z0-9]+)\b'
    match = re.search(website_pattern, query, re.IGNORECASE)
    if match:
        website_name = match.group(1).lower()
        # Common TLD is .com, user can be more specific if needed
        url = f"https://www.{website_name}.com"
        logger.info(f"Inferred website name '{website_name}' and constructed URL: {url}")
        return url
    
    # If no URL found, return placeholder - let the popup analyzer handle it
    logger.warning("No URL found in query, returning placeholder")
    return "website mentioned in query"

def run_crew(query: str, model_provider: str, model_name: str):
    """
    Initializes and runs the CrewAI crew to generate Robot Framework test code.
    
    Architecture Note:
    - Rate limiting was removed during Phase 2 of codebase cleanup. Direct LLM calls
      are now used without wrappers as Google Gemini API has sufficient rate limits.
    - Popup handling is done contextually by BrowserUse agents, not as a separate step.
    """
    agents = RobotAgents(model_provider, model_name)
    tasks = RobotTasks()

    # Define Agents (removed popup_strategy_agent - let BrowserUse handle popups contextually)
    step_planner_agent = agents.step_planner_agent()
    element_identifier_agent = agents.element_identifier_agent()
    code_assembler_agent = agents.code_assembler_agent()
    code_validator_agent = agents.code_validator_agent()

    # Define Tasks (removed popup analysis - focus only on user's explicit query)
    plan_steps = tasks.plan_steps_task(step_planner_agent, query)
    identify_elements = tasks.identify_elements_task(element_identifier_agent)
    assemble_code = tasks.assemble_code_task(code_assembler_agent)
    validate_code = tasks.validate_code_task(code_validator_agent)

    # Create and run the crew
    crew = Crew(
        agents=[step_planner_agent, element_identifier_agent, code_assembler_agent, code_validator_agent],
        tasks=[plan_steps, identify_elements, assemble_code, validate_code],
        process=Process.sequential,
        verbose=True,
    )

    logger.info("🚀 Starting CrewAI workflow execution...")
    result = crew.kickoff()
    logger.info("✅ CrewAI workflow completed successfully")
    
    return result, crew
