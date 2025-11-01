from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks
from src.backend.crew_ai.llm_output_cleaner import LLMOutputCleaner, formatting_monitor
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
        logger.info(
            f"Inferred website name '{website_name}' and constructed URL: {url}")
        return url

    # If no URL found, return placeholder - let the popup analyzer handle it
    logger.warning("No URL found in query, returning placeholder")
    return "website mentioned in query"


def run_crew(query: str, model_provider: str, model_name: str, library_type: str = None):
    """
    Initializes and runs the CrewAI crew to generate Robot Framework test code.

    Args:
        query: User's natural language test description
        model_provider: "local" or "online"
        model_name: Model identifier
        library_type: "selenium" or "browser" (optional, defaults to config setting)

    Architecture Note:
    - Rate limiting was removed during Phase 2 of codebase cleanup. Direct LLM calls
      are now used without wrappers as Google Gemini API has sufficient rate limits.
    - Popup handling is done contextually by BrowserUse agents, not as a separate step.
    - Library context is loaded dynamically based on ROBOT_LIBRARY config setting.
    """
    # Load library context based on configuration
    from src.backend.core.config import settings
    from src.backend.crew_ai.library_context import get_library_context

    # Use provided library_type or fall back to config setting
    if library_type is None:
        library_type = settings.ROBOT_LIBRARY

    logger.info(f"🔧 Loading library context for: {library_type}")
    library_context = get_library_context(library_type)
    logger.info(
        f"✅ Loaded {library_context.library_name} context with dynamic keywords")

    # Initialize agents and tasks with library context
    agents = RobotAgents(model_provider, model_name, library_context)
    tasks = RobotTasks(library_context)

    # Define Agents (removed popup_strategy_agent - let BrowserUse handle popups contextually)
    step_planner_agent = agents.step_planner_agent()
    element_identifier_agent = agents.element_identifier_agent()
    code_assembler_agent = agents.code_assembler_agent()
    code_validator_agent = agents.code_validator_agent()

    # Define Tasks (removed popup analysis - focus only on user's explicit query)
    plan_steps = tasks.plan_steps_task(step_planner_agent, query)
    identify_elements = tasks.identify_elements_task(element_identifier_agent)
    assemble_code = tasks.assemble_code_task(code_assembler_agent)
    validate_code = tasks.validate_code_task(code_validator_agent, code_assembler_agent)

    # Create and run the crew
    crew = Crew(
        agents=[step_planner_agent, element_identifier_agent,
                code_assembler_agent, code_validator_agent],
        tasks=[plan_steps, identify_elements, assemble_code, validate_code],
        process=Process.sequential,
        verbose=True,
    )

    logger.info("🚀 Starting CrewAI workflow execution...")
    logger.info(
        f"🔄 Agent delegation enabled with max_iter={settings.MAX_AGENT_ITERATIONS}")
    logger.info(
        f"📊 LLM Output Cleaner Status: {formatting_monitor.get_stats()}")

    try:
        result = crew.kickoff()
        logger.info("✅ CrewAI workflow completed successfully")
        logger.info(f"🏁 Crew execution finished - delegation cycle complete")
        logger.info(f"📊 Final LLM Stats: {formatting_monitor.get_stats()}")
        return result, crew

    except Exception as e:
        error_msg = str(e)

        # Check if this is a formatting error that slipped through
        if LLMOutputCleaner.is_formatting_error(error_msg):
            logger.error("❌ LLM formatting error detected despite cleaning!")
            logger.error(f"   Error: {error_msg[:200]}...")
            logger.error(
                f"   This indicates the cleaning logic needs improvement")
            formatting_monitor.log_formatting_error(was_recovered=False)
        else:
            logger.error(f"❌ CrewAI workflow failed: {error_msg[:200]}...")

        logger.info(
            f"📊 LLM Stats at failure: {formatting_monitor.get_stats()}")
        raise
