from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from pydantic import ValidationError
from src.backend.crew_ai.tasks import RobotTasks, PlanOutput, _extract_json_by_key
from src.backend.crew_ai.element_identification import identify_elements
from src.backend.crew_ai.llm_output_cleaner import LLMOutputCleaner
from src.backend.crew_ai.callbacks import get_crew_callbacks
from src.backend.core.workflow_metrics import WorkflowMetrics, count_tokens
from src.backend.crew_ai.llm_provider_routing import resolve_model_string
from datetime import datetime
import json
import os
import re
import time
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


# TLDs accepted as the final label of a bare hostname. Curated rather than
# exhaustive: accepting any TLD-shaped ending would mint domains out of
# filenames ("test.py" — .py is Paraguay's TLD).
_ALLOWED_TLDS = frozenset({
    "com", "in", "org", "net", "co", "io", "ai", "app", "dev", "tech",
    "uk", "us", "au", "ca", "de", "fr", "eu", "jp",
})

_FULL_URL_RE = re.compile(r'https?://[^\s]+', re.IGNORECASE)
_HOSTNAME_RE = re.compile(r'\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b')


def extract_url_from_query(query: str) -> str | None:
    """
    Extract a URL from the user query, if one is actually present.

    Full URLs with protocol are returned as written. Bare dotted hostnames
    (example.com, portal.mycompany.co.uk) are returned as https://<hostname>
    when their final label is a known TLD; the whole hostname is captured so
    multi-label domains are never truncated to an earlier label.

    Returns None when the query names no URL. Never guesses: the result
    feeds metrics and the learning store's domain keys, where a fabricated
    domain poisons persistent state while None only means generic (unscoped)
    hints for this one run.
    """
    match = _FULL_URL_RE.search(query)
    if match:
        url = match.group(0).rstrip('.,;!?')  # Remove trailing punctuation
        logger.info(f"Extracted full URL from query: {url}")
        return url

    for candidate in _HOSTNAME_RE.finditer(query):
        hostname = candidate.group(0).lower()
        if hostname.rsplit('.', 1)[-1] in _ALLOWED_TLDS:
            url = f"https://{hostname}"
            logger.info(
                f"Extracted domain from query and constructed URL: {url}")
            return url

    logger.info("No URL in query; proceeding without domain scoping")
    return None


def _validated_plan_steps(raw_steps: list) -> list:
    """Re-validate fallback-path steps through PlanOutput.

    The pydantic path is already validated by CrewAI's converter; the
    json_dict/raw fallbacks hand back dicts exactly as the LLM emitted them —
    key drift (a hallucinated 'locator', a missing 'keyword', a numeric
    'value') must fail HERE, before the browser call is paid for, not
    mid-merge after it. Same outcome as a planner task failure.
    """
    try:
        return list(PlanOutput(steps=raw_steps).steps)
    except ValidationError as e:
        raise ValueError(f"Planner output failed validation: {e}") from e


def _extract_plan_steps(task_output) -> list:
    """Extract the planned steps from the planner task's output.

    Prefers the already-validated pydantic model (the normal path — CrewAI's
    converter ran during kickoff), then json_dict, then the same raw-JSON
    extraction the guardrails use. Raises when nothing parses or validates:
    with no plan there is nothing to identify or assemble, so failing the
    workflow here is correct (same outcome as a planner task failure).
    """
    pydantic_output = getattr(task_output, "pydantic", None)
    steps = getattr(pydantic_output, "steps", None)
    if steps is not None:
        return list(steps)

    json_dict = getattr(task_output, "json_dict", None)
    if isinstance(json_dict, dict) and isinstance(json_dict.get("steps"), list):
        return _validated_plan_steps(json_dict["steps"])

    raw = getattr(task_output, "raw", "") or ""
    extracted = _extract_json_by_key(raw, "steps", "PlanOutput")
    if extracted:
        parsed_steps = json.loads(extracted).get("steps")
        if isinstance(parsed_steps, list):
            return _validated_plan_steps(parsed_steps)

    raise ValueError("Planner output contained no parsable steps")


def run_crew(query: str, model_provider: str, model_name: str, workflow_id: str = "", progress_queue=None, org_id: str | None = None):
    """
    Initializes and runs the CrewAI crew to generate Robot Framework test code.

    Args:
        query: User's natural language test description
        model_provider: "local", "gemini", or "vertex"
        model_name: Model identifier
        workflow_id: Unique workflow identifier for metrics tracking

    Architecture Note:
    - Popup handling is done contextually by BrowserUse agents, not as a separate step.
    - Library context comes from settings.ROBOT_LIBRARY (browser-only since Task 11/E8).
    - Optimization system (pattern learning, ChromaDB) can be enabled via OPTIMIZATION_ENABLED config.
    """
    # Load library context based on configuration
    from src.backend.core.config import settings
    from src.backend.crew_ai.library_context import get_library_context

    logger.info(f"🔧 Loading library context for: {settings.ROBOT_LIBRARY}")
    library_context = get_library_context(settings.ROBOT_LIBRARY)
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
                org_id=org_id,
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
    tasks = RobotTasks(library_context, hint_context=hint_context)

    # Task 16 pipeline: two single-task kickoffs around a deterministic Python
    # stage — Planner (LLM) → element_identification (plain Python + ONE
    # batch_browser_automation call) → Assembler (LLM). The element-identifier
    # LLM agent was deleted: its whole job (which steps need locators, the URL,
    # element specs, the batch call, copying the locator contract onto steps)
    # was mechanical rule-following, now code. The CrewAI LLM validator agent
    # was removed earlier in favour of the deterministic `robot --dryrun` gate
    # (see src/backend/services/dryrun_service.py). Delivered code comes from
    # the assembler crew's tasks[-1].
    step_planner_agent = agents.step_planner_agent()
    code_assembler_agent = agents.code_assembler_agent()

    plan_steps = tasks.plan_steps_task(step_planner_agent, query)

    # Register real-time progress event routing (no-op when progress_queue is
    # None). The assembler task is registered later (register_task) — it can
    # only be built after the element stage, since its description embeds the
    # merged steps. Stage indices stay 0/1/2: index 1 is the python stage,
    # which pushes its own events via push_stage_progress.
    if progress_queue is not None:
        from src.backend.crew_ai.progress_events import (
            register_workflow,
            register_task,
            unregister_workflow,
            push_stage_progress,
        )
        register_workflow(workflow_id, progress_queue, {str(plan_steps.id): 0})

    # Rotate crewai.log if it exceeds size limit (before creating the Crews)
    _rotate_crewai_log()

    step_callback, task_callback = get_crew_callbacks()

    def _make_crew(agent, task):
        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=True,
            output_log_file=CREWAI_LOG_FILE,
            step_callback=step_callback,
            task_callback=task_callback,
            embedder=None,  # Disable automatic knowledge/embedding system
        )

    planner_crew = _make_crew(step_planner_agent, plan_steps)

    logger.info("🚀 Starting CrewAI workflow execution...")
    logger.info("🔄 2-agent pipeline (planner → deterministic element stage → assembler)")
    logger.info(
        f"📊 LLM Output Cleaner Status: {agents.llm._monitor.get_stats()}")

    try:
        try:
            planner_crew.kickoff()
            plan_step_dicts = _extract_plan_steps(plan_steps.output)

            # ── Deterministic element stage (Task 16) ──
            # ONE batch tool call, full contract merged onto the steps; tool
            # error or found:false degrade to the Assembler's placeholder
            # path inside identify_elements — it never raises for those.
            on_progress = None
            if progress_queue is not None:
                on_progress = (lambda progress, message:
                               push_stage_progress(workflow_id, message, progress))
            stage_started = time.time()
            identification = identify_elements(
                plan_step_dicts, query, on_progress=on_progress)
            logger.info(
                "⏱️ Deterministic element stage finished in %.1fs — %s",
                time.time() - stage_started, identification["summary"],
            )

            assemble_code = tasks.assemble_code_task(
                code_assembler_agent,
                json.dumps({"steps": identification["steps"]}),
            )
            if progress_queue is not None:
                register_task(workflow_id, str(assemble_code.id), 2)

            assembler_crew = _make_crew(code_assembler_agent, assemble_code)
            result = assembler_crew.kickoff()

            logger.info("✅ CrewAI workflow completed successfully")
            logger.info("🏁 Crew execution finished")
            # agents.llm._monitor is the authoritative call count: incremented once per
            # CleanedLLMWrapper.call() invocation, scoped to this workflow only.
            # (Both crews share the same agents.llm instance, so the monitor —
            # and the assembler crew's calculate_usage_metrics(), which reads
            # the shared LLM's cumulative usage once for its single agent —
            # cover the planner and assembler stages together.)
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

            # The ASSEMBLER crew is returned: workflow_service reads delivered
            # code from its tasks[-1].output and usage metrics from it (the
            # shared LLM accumulates across both kickoffs).
            return result, assembler_crew, optimization_metrics, hint_metadata, agents.llm._monitor

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
