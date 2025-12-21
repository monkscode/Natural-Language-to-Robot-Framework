import os
import uuid
import logging
import json
import re
import asyncio
from queue import Queue, Empty
from threading import Thread
from typing import Generator, Dict, Any
from datetime import datetime

from src.backend.crew_ai.crew import run_crew, extract_url_from_query
from src.backend.services.docker_service import get_docker_client, build_image, run_test_in_container
from src.backend.config.logging_config import EMOJI
from src.backend.core.temp_metrics_storage import get_temp_metrics_storage
from src.backend.core.workflow_metrics import (
    get_workflow_metrics_collector,
    WorkflowMetrics,
    calculate_crewai_cost
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)


def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str) -> Generator[Dict[str, Any], None, None]:
    """
    Orchestrates the CrewAI workflow to generate Robot Framework code,
    yielding progress updates and the final code.

    Architecture Note:
    - VisionLocatorService was removed during Phase 3 of codebase cleanup as it was
      explicitly disabled and replaced by CrewAI agents with BatchBrowserUseTool.
    - All locator finding is now handled by CrewAI agents in a unified session,
      providing better context awareness and intelligent popup handling.
    - This approach improved first-run success rate from 60% to 90%+.
    
    Args:
        natural_language_query: User's test description
        model_provider: "local" or "online"
        model_name: Model identifier
    """
    logging.info("--- Starting CrewAI Workflow with Vision Integration ---")
    
    # Generate unique workflow ID for metrics tracking
    workflow_id = str(uuid.uuid4())
    logging.info(f"🆔 Workflow ID: {workflow_id}")
    
    # Start with welcome message
    yield {"status": "running", "message": f"{EMOJI['start']} Starting your test generation journey...", "progress": 0}

    if model_provider == "online":
        if not os.getenv("GEMINI_API_KEY"):
            logging.error(
                "Orchestrator: GEMINI_API_KEY not found for online provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return

    # Run CrewAI workflow with simple progress updates
    # Note: Rate limiting was removed during Phase 2 of codebase cleanup.
    # Direct LLM calls are now used without wrappers. Google Gemini API has
    # sufficient rate limits (1500 RPM) for our use case.
    try:
        # Start AI workflow
        yield {"status": "running", "message": f"{EMOJI['ai']} Starting AI workflow...", "progress": 5}
        
        # Stage 1: Planning (10-25%)
        yield {"status": "running", "message": f"{EMOJI['ai']} Planning test steps...", "progress": 10}
        yield {"status": "info", "message": "💡 AI breaks complex tasks into atomic steps for better accuracy", "progress": 10}
        
        # Stage 2: Identifying (25-50%)
        yield {"status": "running", "message": f"{EMOJI['search']} Identifying page elements...", "progress": 30}
        yield {"status": "info", "message": "🎯 Using AI detection with 95%+ accuracy", "progress": 30}
        
        # Run CrewAI workflow (this takes most of the time - 10-15 seconds)
        # User sees progress messages above while this runs
        crew_result, crew_object, optimization_metrics = run_crew(
            natural_language_query, model_provider, model_name, library_type=None, workflow_id=workflow_id)
        
        # Stage 3: Generating (50-75%)
        yield {"status": "running", "message": f"{EMOJI['code']} Generating test code...", "progress": 60}
        yield {"status": "info", "message": "⚡ Browser Library is 2-3x faster than Selenium", "progress": 60}
        
        # Stage 4: Validating (75-95%)
        yield {"status": "running", "message": f"{EMOJI['validate']} Validating code...", "progress": 85}
        yield {"status": "info", "message": "🔬 Validating syntax, structure, and best practices", "progress": 85}

        # Extract robot code from task[2] (code_assembler - no more popup task)
        robot_code = crew_result.tasks_output[2].raw

        # Simplified cleaning logic - prompt now handles most cases
        # Keep only essential defensive measures
        
        # Step 1: Remove markdown code fences (defensive - LLMs sometimes add these)
        robot_code = re.sub(r'^```[a-zA-Z]*\n', '', robot_code)
        robot_code = re.sub(r'\n```$', '', robot_code)
        
        # Step 2: Handle multiple Settings blocks (LLM might output code multiple times)
        # Find ALL occurrences of *** Settings ***
        settings_matches = list(re.finditer(
            r'\*\*\*\s+Settings\s+\*\*\*', robot_code, re.IGNORECASE))
        
        if len(settings_matches) > 1:
            # Multiple Settings blocks found - take the LAST one (usually the cleanest)
            logging.info(
                f"✅ Found {len(settings_matches)} Settings blocks, using the last one")
            robot_code = robot_code[settings_matches[-1].start():]
        elif len(settings_matches) == 1:
            # Single Settings block - remove everything before it
            robot_code = robot_code[settings_matches[0].start():]
            logging.info("✅ Found Settings block, extracted code from there")
        else:
            # No Settings block found - try fallback to Variables or Test Cases
            logging.warning("⚠️ No *** Settings *** block found in code!")
            
            variables_match = re.search(
                r'\*\*\*\s+Variables\s+\*\*\*', robot_code, re.IGNORECASE)
            test_cases_match = re.search(
                r'\*\*\*\s+Test\s+Cases\s+\*\*\*', robot_code, re.IGNORECASE)
            
            if variables_match:
                robot_code = robot_code[variables_match.start():]
                logging.warning(
                    "⚠️ Starting from *** Variables *** instead (Settings missing!)")
            elif test_cases_match:
                robot_code = robot_code[test_cases_match.start():]
                logging.warning(
                    "⚠️ Starting from *** Test Cases *** instead (Settings and Variables missing!)")
            else:
                logging.error("❌ No Robot Framework sections found in output!")
        
        # Step 3: Final cleanup - remove any trailing non-Robot content
        # Split into lines and keep only content that's part of Robot Framework
        lines = robot_code.split('\n')
        cleaned_lines = []
        
        for line in lines:
            # Keep all lines - prompt should ensure clean output
            # Only skip completely empty trailing lines
            cleaned_lines.append(line)
        
        # Remove trailing empty lines
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()
        
        robot_code = '\n'.join(cleaned_lines).strip()

        # Extract validation output from task[3] (code_validator)
        raw_validation_output = crew_result.tasks_output[3].raw

        # Try multiple strategies to extract JSON
        validation_data = None

        # Strategy 1: Try to use output.pydantic or output.json_dict (CrewAI structured output)
        try:
            # First try pydantic attribute (when output_json is a Pydantic model)
            if hasattr(crew_result.tasks_output[3], 'pydantic') and crew_result.tasks_output[3].pydantic:
                validation_data = crew_result.tasks_output[3].pydantic.model_dump(
                )
                logging.info(
                    "✅ Parsed validation output from output.pydantic (Pydantic model)")
            # Fallback to json_dict
            elif hasattr(crew_result.tasks_output[3], 'json_dict') and crew_result.tasks_output[3].json_dict:
                validation_data = crew_result.tasks_output[3].json_dict
                logging.info(
                    "✅ Parsed validation output from output.json_dict")
        except (AttributeError, TypeError) as e:
            logging.debug(f"Could not access structured output: {e}")
            pass

        if not validation_data:
            # Strategy 2: Remove markdown code blocks and parse
            cleaned_output = re.sub(r'```json\s*', '', raw_validation_output)
            cleaned_output = re.sub(r'```\s*', '', cleaned_output)
            cleaned_output = cleaned_output.strip()

            # Strategy 3: Try to parse the cleaned output directly
            try:
                validation_data = json.loads(cleaned_output)
                logging.info("✅ Parsed validation output directly")
            except json.JSONDecodeError:
                # Strategy 4: Extract JSON object with regex (look for complete JSON)
                json_match = re.search(
                    r'\{[^{}]*"valid"[^{}]*"reason"[^{}]*\}', cleaned_output, re.DOTALL)
                if json_match:
                    try:
                        validation_data = json.loads(json_match.group(0))
                        logging.info("✅ Parsed validation output with regex")
                    except json.JSONDecodeError:
                        pass

        if not validation_data:
            # Strategy 5: Look for valid/reason separately in JSON format
            valid_match = re.search(
                r'"valid"\s*:\s*(true|false)', raw_validation_output, re.IGNORECASE)
            reason_match = re.search(
                r'"reason"\s*:\s*"([^"]*)"', raw_validation_output)

            if valid_match:
                validation_data = {
                    "valid": valid_match.group(1).lower() == 'true',
                    "reason": reason_match.group(1) if reason_match else "Validation completed"
                }
                logging.info(
                    "✅ Parsed validation output with fallback extraction")

        if not validation_data:
            # Strategy 6: Fallback to plain text "VALID" or "INVALID" format
            # This handles legacy format or cases where JSON output fails
            if 'VALID' in raw_validation_output.upper():
                # Check if it's explicitly INVALID
                if 'INVALID' in raw_validation_output.upper():
                    validation_data = {
                        "valid": False,
                        "reason": "Code validation found errors (parsed from text format)"
                    }
                    logging.info(
                        "✅ Parsed validation output from text format (INVALID)")
                else:
                    # It's VALID
                    validation_data = {
                        "valid": True,
                        "reason": "Code validation passed (parsed from text format)"
                    }
                    logging.info(
                        "✅ Parsed validation output from text format (VALID)")

        if not validation_data:
            logging.error(
                f"❌ Could not parse validation output. Raw output:\n{raw_validation_output[:500]}")
            raise ValueError(
                "No valid JSON object found in the validation output.")

        if validation_data.get("valid"):
            logging.info(
                "Generated Robot Framework code is here:\n%s", robot_code)
            logging.info(
                "CrewAI workflow complete. Code validation successful.")

            # ============================================
            # NEW: Collect and merge metrics with per-agent tracking
            # ============================================
            try:
                # 1. Extract CrewAI metrics with per-agent and per-task breakdown
                # Note: Using crewai-token-tracking package with enhanced metrics
                try:
                    # Get overall crew metrics from the result object
                    usage_metrics_obj = crew_result.token_usage
                    
                    # Convert UsageMetrics object to dict
                    usage_metrics_dict = {
                        'total_tokens': usage_metrics_obj.total_tokens,
                        'prompt_tokens': usage_metrics_obj.prompt_tokens,
                        'completion_tokens': usage_metrics_obj.completion_tokens,
                        'successful_requests': usage_metrics_obj.successful_requests
                    }
                    
                    logging.info(f"📊 Raw CrewAI usage metrics: {usage_metrics_dict}")
                    
                    # Extract per-agent token metrics from crew_result.token_metrics
                    per_agent_tokens = {}
                    per_task_tokens = {}
                    
                    if hasattr(crew_result, 'token_metrics') and crew_result.token_metrics:
                        token_metrics = crew_result.token_metrics
                        
                        # Extract per-agent metrics
                        if hasattr(token_metrics, 'per_agent') and token_metrics.per_agent:
                            for agent_name, agent_metrics in token_metrics.per_agent.items():
                                per_agent_tokens[agent_name] = {
                                    'total_tokens': agent_metrics.total_tokens,
                                    'prompt_tokens': agent_metrics.prompt_tokens,
                                    'completion_tokens': agent_metrics.completion_tokens,
                                    'successful_requests': agent_metrics.successful_requests
                                }
                                logging.info(f"📊 Agent '{agent_name}': {agent_metrics.total_tokens} tokens")
                        
                        # Extract per-task metrics
                        if hasattr(token_metrics, 'per_task') and token_metrics.per_task:
                            for task_name, task_metrics in token_metrics.per_task.items():
                                per_task_tokens[task_name] = {
                                    'total_tokens': task_metrics.total_tokens,
                                    'prompt_tokens': task_metrics.prompt_tokens,
                                    'completion_tokens': task_metrics.completion_tokens,
                                    'agent_name': task_metrics.agent_name
                                }
                                logging.info(f"📊 Task '{task_name}': {task_metrics.total_tokens} tokens (Agent: {task_metrics.agent_name})")
                    
                    # Also extract from individual task outputs
                    if not per_task_tokens:
                        for i, task_output in enumerate(crew_result.tasks_output):
                            if hasattr(task_output, 'usage_metrics') and task_output.usage_metrics:
                                metrics = task_output.usage_metrics
                                task_key = f"task_{i+1}_{metrics.task_name if hasattr(metrics, 'task_name') else 'unknown'}"
                                per_task_tokens[task_key] = {
                                    'total_tokens': metrics.total_tokens,
                                    'prompt_tokens': metrics.prompt_tokens,
                                    'completion_tokens': metrics.completion_tokens,
                                    'agent_name': metrics.agent_name if hasattr(metrics, 'agent_name') else 'unknown'
                                }
                    
                except Exception as e:
                    logging.warning(f"⚠️ Could not extract CrewAI usage metrics: {e}")
                    # Fallback to empty metrics
                    usage_metrics_dict = {
                        'total_tokens': 0,
                        'prompt_tokens': 0,
                        'completion_tokens': 0,
                        'successful_requests': 0
                    }
                    per_agent_tokens = {}
                    per_task_tokens = {}
                
                crewai_metrics = calculate_crewai_cost(
                    usage_metrics_dict,
                    model_name=model_name
                )
                logging.info(f"📊 CrewAI metrics: {crewai_metrics}")
                
                # 2. Read browser-use metrics from temp file
                temp_storage = get_temp_metrics_storage()
                browser_metrics = temp_storage.read_browser_metrics(workflow_id) or {}
                logging.info(f"📊 Browser-use metrics: {browser_metrics}")
                
                # 3. Create unified metrics with per-agent tracking
                # Calculate averages
                total_elements = browser_metrics.get('elements_processed', 0)
                browser_llm_calls = browser_metrics.get('llm_calls', 0)
                browser_cost = browser_metrics.get('cost', 0.0)
                
                avg_llm_calls = browser_llm_calls / total_elements if total_elements > 0 else 0
                avg_cost = browser_cost / total_elements if total_elements > 0 else 0
                
                unified_metrics = WorkflowMetrics(
                    workflow_id=workflow_id,
                    timestamp=datetime.now(),
                    url=extract_url_from_query(natural_language_query),
                    
                    # Totals
                    total_llm_calls=crewai_metrics['llm_calls'] + browser_llm_calls,
                    total_cost=crewai_metrics['cost'] + browser_cost,
                    execution_time=browser_metrics.get('execution_time', 0),
                    
                    # CrewAI breakdown
                    crewai_llm_calls=crewai_metrics['llm_calls'],
                    crewai_cost=crewai_metrics['cost'],
                    crewai_tokens=crewai_metrics['tokens'],
                    crewai_prompt_tokens=crewai_metrics['prompt_tokens'],
                    crewai_completion_tokens=crewai_metrics['completion_tokens'],
                    
                    # NEW: Per-agent and per-task token tracking
                    per_agent_tokens=per_agent_tokens,
                    per_task_tokens=per_task_tokens,
                    
                    # Browser-use breakdown
                    browser_use_llm_calls=browser_llm_calls,
                    browser_use_cost=browser_cost,
                    browser_use_tokens=browser_metrics.get('tokens', 0),
                    
                    # Browser-use specific
                    total_elements=total_elements,
                    successful_elements=browser_metrics.get('successful_elements', 0),
                    failed_elements=browser_metrics.get('failed_elements', 0),
                    success_rate=browser_metrics.get('success_rate', 0.0),
                    avg_llm_calls_per_element=avg_llm_calls,
                    avg_cost_per_element=avg_cost,
                    custom_actions_enabled=browser_metrics.get('custom_actions_enabled', False),
                    custom_action_usage_count=browser_metrics.get('custom_action_usage_count', 0),
                    session_id=browser_metrics.get('session_id'),
                )
                
                # 4. Record unified metrics
                collector = get_workflow_metrics_collector()
                collector.record_workflow(unified_metrics)
                
                # 5. Cleanup temp file
                temp_storage.delete_temp_file(workflow_id)
                
                logging.info(f"✅ Unified metrics recorded successfully")
                logging.info(f"   Total LLM calls: {unified_metrics.total_llm_calls} (CrewAI: {unified_metrics.crewai_llm_calls}, Browser-use: {unified_metrics.browser_use_llm_calls})")
                logging.info(f"   Total cost: ${unified_metrics.total_cost:.4f} (CrewAI: ${unified_metrics.crewai_cost:.4f}, Browser-use: ${unified_metrics.browser_use_cost:.4f})")
                
                # Log per-agent metrics
                if per_agent_tokens:
                    logging.info("📊 Per-Agent Token Usage:")
                    for agent_name, metrics in per_agent_tokens.items():
                        logging.info(f"   {agent_name}: {metrics['total_tokens']} tokens (Prompt: {metrics['prompt_tokens']}, Completion: {metrics['completion_tokens']})")
                
                # Log per-task metrics
                if per_task_tokens:
                    logging.info("📊 Per-Task Token Usage:")
                    for task_name, metrics in per_task_tokens.items():
                        logging.info(f"   {task_name}: {metrics['total_tokens']} tokens (Agent: {metrics['agent_name']})")
                
            except Exception as metrics_error:
                logging.error(f"❌ Failed to record unified metrics: {metrics_error}", exc_info=True)
                # Don't fail the workflow if metrics recording fails
                # Try to cleanup temp file anyway
                try:
                    temp_storage = get_temp_metrics_storage()
                    temp_storage.delete_temp_file(workflow_id)
                except:
                    pass

            # Calculate stats for success message
            lines = len(robot_code.split('\n'))
            
            # Show finalizing step before completion
            yield {"status": "running", "message": f"{EMOJI['success']} Finalizing test code...", "progress": 95}
            
            # Show 100% progress with running status (so UI displays it)
            yield {"status": "running", "message": f"{EMOJI['success']} Success! Generated {lines} lines of test code.", "progress": 100}
            
            # Final completion message (without progress, as it's already at 100%)
            yield {"status": "complete", "robot_code": robot_code, "message": f"{EMOJI['success']} Test generation complete."}
        else:
            logging.error(
                f"CrewAI workflow finished, but code validation failed. Reason: {validation_data.get('reason')}")
            yield {"status": "error", "message": f"Code validation failed: {validation_data.get('reason')}"}

    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        logging.error(
            "Failed to generate valid Robot Framework code." + str(e))
        try:
            logging.error(
                f"Failed to parse validation output from crew: {e}\nRaw output was:\n{raw_validation_output}")
            yield {"status": "error", "message": "Failed to parse validation output from the crew.", "robot_code": robot_code}
        except:
            yield {"status": "error", "message": f"Failed to parse validation output: {e}"}
    except Exception as e:
        logging.error(
            f"An unexpected error occurred during the CrewAI workflow: {e}", exc_info=True)
        
        # Cleanup temp metrics file on error
        try:
            temp_storage = get_temp_metrics_storage()
            temp_storage.delete_temp_file(workflow_id)
        except:
            pass
        
        yield {"status": "error", "message": f"An error occurred: {str(e)}"}
    


def run_workflow_in_thread(queue: Queue, user_query: str, model_provider: str, model_name: str):
    """Runs the synchronous agentic workflow and puts results in a queue."""
    try:
        # Run workflow and put all yielded events into queue
        for event in run_agentic_workflow(user_query, model_provider, model_name):
            queue.put(event)
    except Exception as e:
        logging.error(f"Exception in workflow thread: {e}")
        queue.put({"status": "error", "message": f"Workflow thread failed: {e}"})


def _learn_from_successful_test(user_query: str, robot_code: str, test_status: str) -> None:
    """
    Learn from a successful test execution for pattern optimization.
    
    Args:
        user_query: Original user query (None if not provided)
        robot_code: Generated robot code
        test_status: Test execution status
    """
    if test_status != 'passed':
        logging.info(f"⏭️  Skipping pattern learning - test status: {test_status}")
        return
    
    if not user_query:
        logging.info("⏭️  Test PASSED but skipping pattern learning - no user query provided")
        return
    
    try:
        from src.backend.core.config import settings
        if not settings.OPTIMIZATION_ENABLED:
            return
            
        from src.backend.crew_ai.optimization import SmartKeywordProvider, QueryPatternMatcher, KeywordVectorStore
        from src.backend.crew_ai.library_context import get_library_context
        
        logging.info("📚 Test PASSED - Learning from successful execution...")
        
        # Initialize components
        library_context = get_library_context(settings.ROBOT_LIBRARY)
        chroma_store = KeywordVectorStore(persist_directory=settings.OPTIMIZATION_CHROMA_DB_PATH)
        pattern_matcher = QueryPatternMatcher(db_path=settings.OPTIMIZATION_PATTERN_DB_PATH, chroma_store=chroma_store)
        smart_provider = SmartKeywordProvider(
            library_context=library_context,
            pattern_matcher=pattern_matcher,
            vector_store=chroma_store
        )
        
        # Learn from the successful execution
        smart_provider.learn_from_execution(user_query, robot_code)
        logging.info("✅ Pattern learning completed - learned from PASSED test")
    except Exception as e:
        logging.warning(f"⚠️ Failed to learn from execution: {e}")


async def stream_generate_only(user_query: str, model_provider: str, model_name: str) -> Generator[str, None, None]:
    """
    Generates Robot Framework test code without executing it.
    Allows user to review and edit before execution.
    """
    robot_code = None
    q = Queue()

    workflow_thread = Thread(
        target=run_workflow_in_thread,
        args=(q, user_query, model_provider, model_name)
    )
    workflow_thread.start()

    # Wait for workflow to complete and stream events
    while workflow_thread.is_alive():
        try:
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"

            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
                workflow_thread.join()
                break
            elif event.get("status") == "error":
                workflow_thread.join()
                return
        except Empty:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    # Process remaining events in queue
    if not robot_code:
        while not q.empty():
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"
            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
            elif event.get("status") == "error":
                return
        if not robot_code:
            final_error_message = "Agentic workflow finished without generating code."
            logging.error(final_error_message)
            yield f"data: {json.dumps({'stage': 'generation', 'status': 'error', 'message': final_error_message})}\n\n"
            return

    # Generation complete - return code without execution
    logging.info("✅ Test generation complete. Ready for user review.")


async def stream_execute_only(robot_code: str, user_query: str = None) -> Generator[str, None, None]:
    """
    Executes provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.
    
    Args:
        robot_code: Robot Framework test code to execute
        user_query: Optional original user query for pattern learning
    """
    if not robot_code or not robot_code.strip():
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': 'No test code provided'})}\n\n"
        return

    run_id = str(uuid.uuid4())
    robot_tests_dir = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), '..', '..', '..', 'robot_tests')
    run_dir = os.path.join(robot_tests_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    test_filename = "test.robot"
    test_filepath = os.path.join(run_dir, test_filename)
    
    try:
        with open(test_filepath, 'w', encoding='utf-8') as f:
            f.write(robot_code)
        logging.info(f"📝 Saved test code to {test_filepath}")
    except Exception as e:
        logging.error(f"Failed to save test code: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Failed to save test code: {str(e)}'})}\n\n"
        return

    try:
        client = get_docker_client()
        for event in build_image(client):
            yield f"data: {json.dumps({'stage': 'execution', **event})}\n\n"

        # Execute test
        logging.info(f"🚀 Executing test: {test_filename}")
        result = run_test_in_container(client, run_id, test_filename)
        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"
        
        # Pattern learning: ONLY learn from PASSED tests
        _learn_from_successful_test(user_query, robot_code, result.get('test_status', 'unknown'))

    except (ConnectionError, RuntimeError, Exception) as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


async def stream_generate_and_run(user_query: str, model_provider: str, model_name: str) -> Generator[str, None, None]:
    """
    Legacy endpoint: Generates and executes test in one flow.
    Kept for backward compatibility.
    """
    robot_code = None
    q = Queue()

    workflow_thread = Thread(
        target=run_workflow_in_thread,
        args=(q, user_query, model_provider, model_name)
    )
    workflow_thread.start()

    while workflow_thread.is_alive():
        try:
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"

            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
                workflow_thread.join()
                break
            elif event.get("status") == "error":
                workflow_thread.join()
                return
        except Empty:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    if not robot_code:
        while not q.empty():
            event = q.get_nowait()
            event_data = {'stage': 'generation', **event}
            yield f"data: {json.dumps(event_data)}\n\n"
            if event.get("status") == "complete" and "robot_code" in event:
                robot_code = event["robot_code"]
            elif event.get("status") == "error":
                return
        if not robot_code:
            final_error_message = "Agentic workflow finished without generating code."
            logging.error(final_error_message)
            yield f"data: {json.dumps({'stage': 'generation', 'status': 'error', 'message': final_error_message})}\n\n"
            return

    run_id = str(uuid.uuid4())
    robot_tests_dir = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), '..', '..', '..', 'robot_tests')
    run_dir = os.path.join(robot_tests_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    test_filename = "test.robot"
    test_filepath = os.path.join(run_dir, test_filename)
    with open(test_filepath, 'w', encoding='utf-8') as f:
        f.write(robot_code)

    try:
        client = get_docker_client()
        for event in build_image(client):
            yield f"data: {json.dumps({'stage': 'execution', **event})}\n\n"

        # Execute test (healing system removed - locators are validated during generation)
        logging.info(f"🚀 Executing test: {test_filename}")
        result = run_test_in_container(client, run_id, test_filename)
        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"
        
        # Pattern learning: ONLY learn from PASSED tests
        _learn_from_successful_test(user_query, robot_code, result.get('test_status', 'unknown'))

    except (ConnectionError, RuntimeError, Exception) as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


# Healing system removed - locators are validated during generation by browser-use
# No need for post-failure healing since validation happens upfront with F12-style checks
