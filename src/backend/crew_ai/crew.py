from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks
from src.backend.crew_ai.llm_output_cleaner import LLMOutputCleaner, formatting_monitor
from src.backend.core.workflow_metrics import WorkflowMetrics, count_tokens
from datetime import datetime
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


def run_crew(query: str, model_provider: str, model_name: str, library_type: str = None, workflow_id: str = ""):
    """
    Initializes and runs the CrewAI crew to generate Robot Framework test code.

    Args:
        query: User's natural language test description
        model_provider: "local" or "online"
        model_name: Model identifier
        library_type: "selenium" or "browser" (optional, defaults to config setting)
        workflow_id: Unique workflow identifier for metrics tracking

    Architecture Note:
    - Rate limiting was removed during Phase 2 of codebase cleanup. Direct LLM calls
      are now used without wrappers as Google Gemini API has sufficient rate limits.
    - Popup handling is done contextually by BrowserUse agents, not as a separate step.
    - Library context is loaded dynamically based on ROBOT_LIBRARY config setting.
    - Optimization system (pattern learning, ChromaDB) can be enabled via OPTIMIZATION_ENABLED config.
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

    # Initialize metrics for optimization tracking
    optimization_metrics = None
    if settings.OPTIMIZATION_ENABLED:
        # Create a temporary metrics object for tracking optimization metrics
        # This will be merged with the main workflow metrics later
        optimization_metrics = WorkflowMetrics(
            workflow_id=workflow_id or "temp",
            timestamp=datetime.now(),
            url=extract_url_from_query(query),
            total_llm_calls=0,
            total_cost=0.0,
            execution_time=0.0
        )
    
    # Initialize optimization system if enabled
    optimized_context = None
    keyword_search_tool = None
    smart_provider = None
    baseline_context_tokens = 0
    optimized_context_tokens = 0
    
    if settings.OPTIMIZATION_ENABLED:
        try:
            logger.info("🚀 Optimization system enabled - initializing components")
            from src.backend.crew_ai.optimization import (
                KeywordVectorStore,
                QueryPatternMatcher,
                SmartKeywordProvider,
                ContextPruner
            )
            
            # Initialize ChromaDB vector store
            vector_store = KeywordVectorStore(
                persist_directory=settings.OPTIMIZATION_CHROMA_DB_PATH
            )
            
            # Ensure collection is ready (auto-rebuild if version mismatch)
            vector_store.ensure_collection_ready(library_context.library_name)
            
            # Initialize pattern matcher (with ChromaDB for query embeddings)
            pattern_matcher = QueryPatternMatcher(
                db_path=settings.OPTIMIZATION_PATTERN_DB_PATH,
                chroma_store=vector_store  # Pass ChromaDB store for query embeddings
            )
            
            # Initialize context pruner if enabled
            context_pruner = None
            if settings.OPTIMIZATION_CONTEXT_PRUNING_ENABLED:
                try:
                    logger.info("🔍 Initializing context pruner...")
                    context_pruner = ContextPruner(
                        persist_directory=settings.OPTIMIZATION_CHROMA_DB_PATH
                    )
                    logger.info("✅ Context pruner initialized")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to initialize context pruner: {e}")
                    logger.warning("   Context pruning will be disabled")
            
            # Initialize smart keyword provider with metrics
            smart_provider = SmartKeywordProvider(
                library_context=library_context,
                pattern_matcher=pattern_matcher,
                vector_store=vector_store,
                context_pruner=context_pruner,
                pruning_enabled=settings.OPTIMIZATION_CONTEXT_PRUNING_ENABLED,
                pruning_threshold=settings.OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD,
                metrics=optimization_metrics
            )
            
            # Calculate baseline context size (full context)
            baseline_context = library_context.code_assembly_context
            baseline_context_tokens = count_tokens(baseline_context)
            
            # Get optimized contexts for ALL agents
            logger.info("🎯 Generating optimized contexts for all agents...")
            planner_context = smart_provider.get_agent_context(query, "planner")
            # Identifier context skipped - element_identifier_agent doesn't use context
            # It only needs batch_browser_automation tool, no keyword knowledge required
            identifier_context = None
            assembler_context = smart_provider.get_agent_context(query, "assembler")
            validator_context = smart_provider.get_agent_context(query, "validator")
            
            # Calculate total optimized tokens (skip None values)
            planner_tokens = count_tokens(planner_context)
            identifier_tokens = 0  # Not generated, saves ~50-100ms per workflow
            assembler_tokens = count_tokens(assembler_context)
            validator_tokens = count_tokens(validator_context)
            optimized_context_tokens = assembler_tokens  # For backward compatibility metric
            
            logger.info(f"📊 Context sizes: Planner={planner_tokens}, Identifier=N/A (skipped), Assembler={assembler_tokens}, Validator={validator_tokens}")
            
            # Track context reduction (using assembler as reference)
            if optimization_metrics:
                optimization_metrics.track_context_reduction(
                    baseline=baseline_context_tokens,
                    optimized=optimized_context_tokens
                )
                logger.info(
                    f"📊 Context reduction (assembler): {baseline_context_tokens} -> {optimized_context_tokens} tokens "
                    f"({optimization_metrics.context_reduction['reduction_percentage']:.1f}% reduction)"
                )
            
            # Get keyword search tool
            keyword_search_tool = smart_provider.get_keyword_search_tool()
            
            logger.info("✅ Optimization system initialized successfully for ALL agents")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize optimization system: {e}")
            logger.warning("⚠️ Falling back to baseline behavior (full context)")
            planner_context = None
            identifier_context = None
            assembler_context = None
            validator_context = None
            keyword_search_tool = None
            smart_provider = None
            optimization_metrics = None
    else:
        logger.info("ℹ️ Optimization system disabled (OPTIMIZATION_ENABLED=False)")
        planner_context = None
        identifier_context = None
        assembler_context = None
        validator_context = None

    # Initialize agents and tasks with library context and workflow_id
    agents = RobotAgents(
        model_provider, 
        model_name, 
        library_context,
        assembler_context=assembler_context,  # Use consistent naming with other contexts
        keyword_search_tool=keyword_search_tool,
        planner_context=planner_context,
        identifier_context=identifier_context,
        validator_context=validator_context
    )
    tasks = RobotTasks(library_context, workflow_id=workflow_id)

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
        embedder=None,  # Disable automatic knowledge/embedding system
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
        
        # Capture per-agent token metrics for cost attribution
        logger.info("📊 Capturing per-agent token metrics...")
        per_agent_metrics = {}
        agents_list = [
            ("step_planner", step_planner_agent),
            ("element_identifier", element_identifier_agent),
            ("code_assembler", code_assembler_agent),
            ("code_validator", code_validator_agent),
        ]
        
        for agent_name, agent in agents_list:
            try:
                # Get cumulative token usage from the agent's LLM
                usage = agent.llm.get_token_usage_summary()
                per_agent_metrics[agent_name] = {
                    'total_tokens': usage.total_tokens,
                    'prompt_tokens': usage.prompt_tokens,
                    'completion_tokens': usage.completion_tokens,
                    'successful_requests': usage.successful_requests,
                }
                logger.info(f"   • {agent_name}: {usage.total_tokens} tokens "
                           f"(prompt: {usage.prompt_tokens}, completion: {usage.completion_tokens})")
            except Exception as e:
                logger.warning(f"⚠️ Could not get token usage for {agent_name}: {e}")
                per_agent_metrics[agent_name] = {
                    'total_tokens': 0,
                    'prompt_tokens': 0,
                    'completion_tokens': 0,
                    'successful_requests': 0,
                }
        
        # NOTE: Pattern learning is NOT done here!
        # Learning should only happen AFTER test execution succeeds (test_status == "passed")
        # This ensures we only learn from validated, working code.
        # The learning is triggered in workflow_service.py after Docker execution completes successfully.
        
        # Return optimization metrics separately (Crew object doesn't allow dynamic attributes)
        if optimization_metrics:
            logger.info("📊 Optimization metrics collected")
        
        return result, crew, optimization_metrics, per_agent_metrics

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
