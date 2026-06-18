from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks
from src.backend.crew_ai.llm_output_cleaner import LLMOutputCleaner
from src.backend.crew_ai.callbacks import get_crew_callbacks
from src.backend.core.workflow_metrics import WorkflowMetrics, count_tokens
from src.backend.crew_ai.llm_provider_routing import resolve_model_string
from datetime import datetime
import os
import re
import logging
import threading

logger = logging.getLogger(__name__)

# CrewAI log file path and rotation settings
CREWAI_LOG_FILE = "logs/crewai.log.txt"  # CrewAI appends .txt to paths not ending in .json/.txt
CREWAI_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50MB per file
CREWAI_LOG_BACKUP_COUNT = 9              # 9 backups = 450MB max

_log_rotation_lock = threading.Lock()


def _rotate_crewai_log():
    """Rotate crewai.log.txt if it exceeds the size limit.

    Thread-safe: guarded by _log_rotation_lock to prevent race conditions
    when multiple workflows trigger rotation simultaneously.

    CrewAI's output_log_file appends .txt to any path not ending in .json or .txt,
    so "logs/crewai.log" becomes "logs/crewai.log.txt". CREWAI_LOG_FILE reflects
    the actual filename CrewAI creates. We handle rotation manually before each
    crew.kickoff() call since CrewAI's FileHandler has no rotation support.

    Rotation scheme: crewai.log.txt -> crewai.log.txt.1 -> ... -> crewai.log.txt.9
    """
    try:
        with _log_rotation_lock:
            if not os.path.exists(CREWAI_LOG_FILE):
                return

            file_size = os.path.getsize(CREWAI_LOG_FILE)
            if file_size < CREWAI_LOG_MAX_BYTES:
                return

            logger.info(
                f"📂 Rotating {CREWAI_LOG_FILE} ({file_size / (1024*1024):.1f}MB exceeds "
                f"{CREWAI_LOG_MAX_BYTES / (1024*1024):.0f}MB limit)"
            )

            # Shift existing backups: .8 -> .9, .7 -> .8, ... , .1 -> .2
            for i in range(CREWAI_LOG_BACKUP_COUNT - 1, 0, -1):
                src = f"{CREWAI_LOG_FILE}.{i}"
                dst = f"{CREWAI_LOG_FILE}.{i + 1}"
                if os.path.exists(src):
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.rename(src, dst)

            # Current log -> .1
            backup_path = f"{CREWAI_LOG_FILE}.1"
            if os.path.exists(backup_path):
                os.remove(backup_path)
            os.rename(CREWAI_LOG_FILE, backup_path)
            logger.info(f"📂 Rotated {CREWAI_LOG_FILE} -> {backup_path}")
    except OSError as e:
        logger.warning(f"📂 Log rotation skipped due to OS error: {e}")


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


def run_crew(query: str, model_provider: str, model_name: str, library_type: str = None, workflow_id: str = "", progress_queue=None):
    """
    Initializes and runs the CrewAI crew to generate Robot Framework test code.

    Args:
        query: User's natural language test description
        model_provider: "local", "gemini", or "vertex"
        model_name: Model identifier
        library_type: "selenium" or "browser" (optional, defaults to config setting)
        workflow_id: Unique workflow identifier for metrics tracking

    Architecture Note:
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
    keyword_search_tool = None
    smart_provider = None
    baseline_context_tokens = 0
    optimized_context_tokens = 0
    
    # Build the LiteLLM-routable model string for token counting.
    # Must match what get_llm() produces so LiteLLM looks up the right
    # entry in its model cost database (gemini/ vs vertex_ai/ have separate pricing).
    # Delegates to resolve_model_string() so any new provider added to
    # PROVIDER_PREFIXES is picked up here automatically.
    token_model = resolve_model_string(model_provider, model_name)
    
    hint_metadata = {}
    feedback_loop = None

    if settings.OPTIMIZATION_ENABLED:
        try:
            logger.info("🚀 Optimization system enabled - initializing components")
            from src.backend.crew_ai.optimization import (
                get_keyword_vector_store,
                QueryPatternMatcher,
                SmartKeywordProvider,
                ContextPruner,
                reconcile_selection_traces,
            )

            # Get learning execution_memory from FeedbackLoop singleton
            # Must be initialized BEFORE QueryPatternMatcher and SmartKeywordProvider
            learning_em = None
            try:
                from src.backend.crew_ai.optimization.learning_registry import (
                    get_feedback_loop,
                )
                feedback_loop = get_feedback_loop()
                if feedback_loop is not None:
                    learning_em = feedback_loop.execution_memory
                    logger.info("✅ Learning execution_memory obtained from FeedbackLoop singleton")
                else:
                    logger.warning("⚠️ FeedbackLoop unavailable — learning hints will be disabled")
            except Exception as e:
                logger.warning(f"⚠️ Learning DB init failed: {e}")
                logger.warning("   Learning hints will be disabled")

            # Initialize the pgvector keyword store and pattern matcher.
            # FeedbackLoop.__init__() already created its own KeywordVectorStore
            # and QueryPatternMatcher internally. Reuse those instances when
            # available to avoid opening a second store and loading the fastembed
            # ONNX model a second time.
            _fl_pattern_learner = (
                getattr(feedback_loop, "pattern_learner", None)
                if feedback_loop is not None else None
            )
            _fl_chroma_store = (
                getattr(_fl_pattern_learner, "chroma_store", None)
                if _fl_pattern_learner is not None else None
            )

            if _fl_chroma_store is not None:
                vector_store = _fl_chroma_store
                pattern_matcher = _fl_pattern_learner
                logger.info(
                    "✅ Reusing FeedbackLoop's ChromaDB client and pattern matcher "
                    "(avoids double ONNX model load)"
                )
            else:
                # Shared process-wide store (NOT a per-workflow instance: each
                # KeywordVectorStore owns a connection pool, and per-run pools
                # were never closed — leaking connections until Postgres hit
                # max_connections and took auth down with it).
                vector_store = get_keyword_vector_store()
                pattern_matcher = QueryPatternMatcher(
                    chroma_store=vector_store
                )

            # Ensure keyword collection is ready (auto-rebuild if version mismatch).
            # Called regardless of whether we reused FeedbackLoop's store — the
            # FeedbackLoop never calls ensure_collection_ready itself.
            vector_store.ensure_collection_ready(library_context.library_name)
            
            # Initialize context pruner if enabled
            context_pruner = None
            if settings.OPTIMIZATION_CONTEXT_PRUNING_ENABLED:
                try:
                    logger.info("🔍 Initializing context pruner...")
                    context_pruner = ContextPruner()
                    logger.info("✅ Context pruner initialized")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to initialize context pruner: {e}")
                    logger.warning("   Context pruning will be disabled")

            # Initialize smart keyword provider with metrics + learning DB
            smart_provider = SmartKeywordProvider(
                library_context=library_context,
                pattern_matcher=pattern_matcher,
                vector_store=vector_store,
                context_pruner=context_pruner,
                pruning_enabled=settings.OPTIMIZATION_CONTEXT_PRUNING_ENABLED,
                pruning_threshold=settings.OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD,
                metrics=optimization_metrics,
                execution_memory=learning_em,
                nl_engine=feedback_loop.nl_engine if feedback_loop is not None else None,
            )
            
            # Calculate baseline context size (full context)
            baseline_context = library_context.code_assembly_context
            baseline_context_tokens = count_tokens(baseline_context, token_model)
            
            # Get optimized contexts for ALL agents
            # URL extracted once, passed to all agents for domain-scoped hints
            url = extract_url_from_query(query)
            logger.info("🎯 Generating optimized contexts for all agents...")
            planner_result = smart_provider.get_agent_context(query, "planner", url=url)
            assembler_result = smart_provider.get_agent_context(query, "assembler", url=url)

            planner_context = planner_result.context
            assembler_context = assembler_result.context

            # Capture hint metadata for FeedbackLoop integration
            # count = injected (budget-capped), available = found before cap
            # nl_injected_ids = union of NL hint IDs across all agents;
            # any hint that reached any agent could have shaped the output.
            all_nl_ids = sorted(
                set(planner_result.nl_injected_ids)
                | set(assembler_result.nl_injected_ids)
            )
            hint_metadata = {
                "agents": {
                    "planner":   {"count": planner_result.hints_count,   "available": planner_result.hints_available,   "sources": planner_result.hint_sources},
                    "assembler": {"count": assembler_result.hints_count, "available": assembler_result.hints_available, "sources": assembler_result.hint_sources},
                },
                "nl_injected_ids": all_nl_ids,
            }
            # R7 holdout flag for this workflow. smart_provider.was_holdout is
            # True only when the coin came up AND hints were actually
            # available to suppress; re-confirming total available > 0 keeps
            # a coin flip on a hint-less (Cat A) workflow out of Cat C.
            total_hints_available = sum(
                r["available"] for r in hint_metadata["agents"].values()
            )
            hint_metadata["was_holdout"] = (
                smart_provider.was_holdout and total_hints_available > 0
            )
            total_hints = sum(r["count"] for r in hint_metadata["agents"].values())
            if total_hints > 0:
                logger.info(f"📚 Learning hints injected: {hint_metadata}")

            # N3 (F2c): fold the per-agent NL selection traces into one record
            # keyed by hint_id, injected-wins against the SAME union attribution
            # credits (all_nl_ids). Observability only — never mutates all_nl_ids.
            # Attached AFTER the log above so the per-candidate trace can't bloat
            # it; the helper is fully defensive (None on any error).
            hint_metadata["selection_trace"] = reconcile_selection_traces(
                [planner_result.selection_trace,
                 assembler_result.selection_trace],
                all_nl_ids,
            )

            # Build hint_context for task-level injection
            hint_context = {}
            if planner_result.hint_text:
                hint_context["planner"] = planner_result.hint_text
            if assembler_result.hint_text:
                hint_context["assembler"] = assembler_result.hint_text

            # Calculate total optimized tokens
            planner_tokens = count_tokens(planner_context, token_model)
            assembler_tokens = count_tokens(assembler_context, token_model)
            optimized_context_tokens = assembler_tokens  # For backward compatibility metric

            logger.info(f"📊 Context sizes: Planner={planner_tokens}, Assembler={assembler_tokens}")
            
            # Track context reduction (using assembler as reference)
            if optimization_metrics:
                optimization_metrics.track_context_reduction(
                    baseline=baseline_context_tokens,
                    optimized=optimized_context_tokens
                )
                pct = optimization_metrics.context_reduction['reduction_percentage']
                direction = "reduction" if pct >= 0 else "increase"
                logger.info(
                    f"📊 Context change (assembler): {baseline_context_tokens} -> {optimized_context_tokens} tokens "
                    f"({abs(pct):.1f}% {direction})"
                )
            
            # Get keyword search tool
            keyword_search_tool = smart_provider.get_keyword_search_tool()
            
            logger.info("✅ Optimization system initialized successfully for ALL agents")
            if feedback_loop is not None:
                feedback_loop._optimization_init_ok = True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize optimization system: {e}")
            logger.warning("⚠️ Falling back to baseline behavior (full context)")
            planner_context = None
            assembler_context = None
            keyword_search_tool = None
            smart_provider = None
            optimization_metrics = None
            hint_context = {}
            hint_metadata = {}    # prevent corrupted-shape leakage downstream
            if feedback_loop is not None:
                feedback_loop._optimization_init_failures += 1
                feedback_loop._optimization_init_ok = False
    else:
        logger.info("ℹ️ Optimization system disabled (OPTIMIZATION_ENABLED=False)")
        planner_context = None
        assembler_context = None
        hint_context = {}

    # Initialize agents and tasks with library context and workflow_id
    agents = RobotAgents(
        model_provider,
        model_name,
        library_context,
        assembler_context=assembler_context,
        keyword_search_tool=keyword_search_tool,
        planner_context=planner_context,
    )
    tasks = RobotTasks(library_context, workflow_id=workflow_id, hint_context=hint_context)

    # Define Agents (removed popup_strategy_agent - let BrowserUse handle popups contextually)
    # The CrewAI LLM validator agent was removed in favour of a deterministic
    # `robot --dryrun` gate (see src/backend/services/dryrun_service.py). The crew
    # now ends at the Code Assembler — delivered code comes from tasks[2].
    step_planner_agent = agents.step_planner_agent()
    element_identifier_agent = agents.element_identifier_agent()
    code_assembler_agent = agents.code_assembler_agent()

    # Define Tasks (removed popup analysis - focus only on user's explicit query)
    plan_steps = tasks.plan_steps_task(step_planner_agent, query)
    identify_elements = tasks.identify_elements_task(element_identifier_agent)
    assemble_code = tasks.assemble_code_task(code_assembler_agent)

    # Register real-time progress event routing (no-op when progress_queue is None)
    if progress_queue is not None:
        from src.backend.crew_ai.progress_events import register_workflow, unregister_workflow
        register_workflow(workflow_id, progress_queue, {
            str(plan_steps.id): 0,
            str(identify_elements.id): 1,
            str(assemble_code.id): 2,
        })

    # Rotate crewai.log if it exceeds size limit (before creating the Crew)
    _rotate_crewai_log()

    step_callback, task_callback = get_crew_callbacks()

    # Create and run the crew
    crew = Crew(
        agents=[step_planner_agent, element_identifier_agent,
                code_assembler_agent],
        tasks=[plan_steps, identify_elements, assemble_code],
        process=Process.sequential,
        verbose=True,
        output_log_file=CREWAI_LOG_FILE,
        step_callback=step_callback,
        task_callback=task_callback,
        embedder=None,  # Disable automatic knowledge/embedding system
    )

    logger.info("🚀 Starting CrewAI workflow execution...")
    logger.info("🔄 Sequential 3-agent pipeline (planner → identifier → assembler)")
    logger.info(
        f"📊 LLM Output Cleaner Status: {agents.llm._monitor.get_stats()}")

    try:
        try:
            result = crew.kickoff()
            logger.info("✅ CrewAI workflow completed successfully")
            logger.info("🏁 Crew execution finished")
            # agents.llm._monitor is the authoritative call count: incremented once per
            # CleanedLLMWrapper.call() invocation, scoped to this workflow only.
            # Compare against "Raw CrewAI usage metrics" in workflow_service.py — that
            # figure is N_agents × real_calls due to CrewAI summing the shared LLM
            # instance once per agent in calculate_usage_metrics().
            logger.info(f"📊 Final LLM Stats: {agents.llm._monitor.get_stats()}")

            # NOTE: Pattern learning is NOT done here!
            # Learning should only happen AFTER test execution succeeds (test_status == "passed")
            # This ensures we only learn from validated, working code.
            # The learning is triggered in workflow_service.py after Docker execution completes successfully.

            # Return optimization metrics separately (Crew object doesn't allow dynamic attributes)
            if optimization_metrics:
                logger.info("📊 Optimization metrics collected")

            # Progress intentionally caps at 80 here (TaskCompleted for the Code
            # Assembler — the last crew task — maps to 80 in progress_events.py).
            # The terminal 100% is NO LONGER pushed here: it now belongs to the
            # deterministic dryrun gate in workflow_service.run_agentic_workflow,
            # which runs AFTER this returns. Pushing 100% here would hide the
            # progress bar (frontend hides at >=100%) while the gate is still
            # verifying/repairing. See dryrun_service.validate_and_repair (prog-2).

            return result, crew, optimization_metrics, hint_metadata, agents.llm._monitor

        except Exception as e:
            error_msg = str(e)

            # Check if this is a formatting error that slipped through
            if LLMOutputCleaner.is_formatting_error(error_msg):
                logger.error("❌ LLM formatting error detected despite cleaning!")
                logger.error(f"   Error: {error_msg[:200]}...")
                logger.error(
                    f"   This indicates the cleaning logic needs improvement")
                agents.llm._monitor.log_formatting_error(was_recovered=False)
            else:
                logger.error(f"❌ CrewAI workflow failed: {error_msg[:200]}...")

            logger.info(
                f"📊 LLM Stats at failure: {agents.llm._monitor.get_stats()}")
            raise

    finally:
        if progress_queue is not None:
            unregister_workflow(workflow_id)
