import os
import uuid
import logging
import json
import re
import asyncio
from queue import Queue, Empty
from threading import Thread
import threading
from typing import AsyncGenerator, Generator, Dict, Any
from datetime import datetime, timezone

from src.backend.crew_ai.crew import run_crew, extract_url_from_query
from src.backend.crew_ai.robot_code_normalizer import normalize_robot_code
from src.backend.services.docker_service import get_docker_client, build_image, run_test_in_container
from src.backend.config.logging_config import EMOJI, bind_workflow_context
from src.backend.core.observability import create_workflow_span
from src.backend.core.temp_metrics_storage import get_temp_metrics_storage
from src.backend.core.workflow_metrics import (
    get_workflow_metrics_collector,
    WorkflowMetrics,
    calculate_crewai_cost
)
from src.backend.core.config import settings


# ---------------------------------------------------------------------------
# Learning System — import singleton from registry
# ---------------------------------------------------------------------------

from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop

# Hint metadata cache — bridges generation phase (crew.py) and execution phase
# (_process_learning). Keyed by workflow_id, consumed via .pop() in _process_learning().
#
# Leak mitigation: entries are timestamped on write. _store_hint_metadata() evicts
# entries older than _HINT_CACHE_TTL_SECONDS and caps total size at _HINT_CACHE_MAX_SIZE
# before inserting, so generate-only flows (no subsequent execution) cannot grow the
# cache indefinitely.
_hint_metadata_cache: Dict[str, dict] = {}
_HINT_CACHE_TTL_SECONDS = 3600   # 1 hour — enough to cover any realistic generate→execute gap
_HINT_CACHE_MAX_SIZE = 500       # hard ceiling independent of TTL

# Active workflow tracking — prevents accepting more workflows than the server can handle.
# Counter + lock pattern. Incremented when a workflow SSE stream starts, decremented
# in the finally block when it ends (regardless of success/failure/exception).
_active_workflow_count = 0
_active_workflow_lock = threading.Lock()

# Belt-and-suspenders lock for _hint_metadata_cache.
# Individual dict operations are atomic under CPython's GIL, but explicit locking
# makes the thread-safety guarantee portable across Python implementations.
_hint_metadata_lock = threading.Lock()


def _acquire_workflow_slot() -> bool:
    """Try to acquire a workflow slot. Returns False if at capacity."""
    global _active_workflow_count
    with _active_workflow_lock:
        if _active_workflow_count >= settings.MAX_CONCURRENT_WORKFLOWS:
            return False
        _active_workflow_count += 1
        return True


def _release_workflow_slot():
    """Release a workflow slot. Always called from a finally block after a successful acquire."""
    global _active_workflow_count
    with _active_workflow_lock:
        _active_workflow_count = max(0, _active_workflow_count - 1)


class _SlotReleaser:
    """Release a workflow slot exactly once, when all participants finish.

    A countdown latch: each participant calls done() once, and the slot is
    released when the count reaches zero. participant_count is the number of
    distinct finishers — two (generator + background workflow thread) for the
    generate flows, one (generator only) for the execute-only flow. If
    Thread.start() raises before the thread runs, call done() once for the
    thread's share so the generator's own done() still reaches zero.

    done() is idempotent once the slot is released: a caller may release the
    slot early (the moment user-facing work is done) and still call done()
    again from a finally block as a guaranteed fallback.
    """

    __slots__ = ("_count", "_lock")

    def __init__(self, participant_count: int = 2) -> None:
        self._count = participant_count
        self._lock = threading.Lock()

    def done(self) -> None:
        """Signal one participant finished; release the slot when all are done.

        No-op once the slot has been released, so over-calling is safe.
        Thread-safe.
        """
        with self._lock:
            if self._count <= 0:
                return
            self._count -= 1
            if self._count == 0:
                _release_workflow_slot()


def get_active_workflow_count() -> int:
    """Get the current number of active workflows. Used by health endpoints."""
    with _active_workflow_lock:
        return _active_workflow_count


def _safe_delete_temp_metrics(workflow_id: str) -> None:
    """Delete the temp metrics file for a workflow, swallowing all errors.

    Called from every exit path in run_agentic_workflow() — success, validation
    failure, parse error, and unexpected exception — so the file is never orphaned.
    """
    try:
        get_temp_metrics_storage().delete_temp_file(workflow_id)
    except Exception as e:
        logging.debug("Temp metrics cleanup failed for %s (non-blocking): %s", workflow_id, e)


def _safe_evict_hint_metadata(workflow_id: str) -> None:
    """Remove the hint metadata cache entry for a workflow, swallowing all errors.

    Called from every error exit path in run_agentic_workflow() where
    _process_learning() will NOT be called (validation failure, parse error,
    unexpected exception). Without this call the entry leaks indefinitely because
    _process_learning() is the only other consumer that pops it.

    On the success path _process_learning() pops the entry itself — do NOT
    call this function there to avoid a redundant double-pop.
    """
    try:
        with _hint_metadata_lock:
            _hint_metadata_cache.pop(workflow_id, None)
    except Exception as e:
        logging.debug("Hint metadata eviction failed for %s (non-blocking): %s", workflow_id, e)


def _store_hint_metadata(workflow_id: str, hint_metadata: dict) -> None:
    """Write hint metadata to the cache, evicting stale and excess entries first.

    Entries are timestamped on write. Before inserting, entries older than
    _HINT_CACHE_TTL_SECONDS are purged, and if the cache still exceeds
    _HINT_CACHE_MAX_SIZE the oldest entries are removed until it fits.
    This prevents unbounded growth from generate-only flows that never
    reach _process_learning() or _safe_evict_hint_metadata().
    """
    try:
        now = datetime.now(tz=timezone.utc).timestamp()
        with _hint_metadata_lock:
            # Evict TTL-expired entries
            expired = [
                wid for wid, entry in _hint_metadata_cache.items()
                if now - entry.get("_stored_at", now) > _HINT_CACHE_TTL_SECONDS
            ]
            for wid in expired:
                del _hint_metadata_cache[wid]
                logging.debug("hint_metadata_cache: evicted expired entry %s", wid)

            # Evict oldest entries if still over size cap
            overflow = len(_hint_metadata_cache) - _HINT_CACHE_MAX_SIZE + 1
            if overflow > 0:
                oldest = sorted(
                    _hint_metadata_cache.keys(),
                    key=lambda wid: _hint_metadata_cache[wid].get("_stored_at", 0),
                )[:overflow]
                for wid in oldest:
                    del _hint_metadata_cache[wid]
                    logging.info("hint_metadata_cache: evicted oldest entry %s (size cap)", wid)

            _hint_metadata_cache[workflow_id] = {**hint_metadata, "_stored_at": now}
    except Exception as e:
        logging.debug("Hint metadata store failed for %s (non-blocking): %s", workflow_id, e)


def _build_conflict_prompt(
    failed_code: str,
    working_code: str,
    active_hints: list,
    domain: str | None = None,
    url: str | None = None,
    user_query: str | None = None,
) -> str:
    """Build the LLM prompt for Trigger 1 conflict detection.

    Trigger 1 fires when a previously-failed workflow re-runs with a
    user-edited robot code and passes. The two code blobs are presented
    to the LLM side by side, plus the list of NL feedback hints that were
    injected at v1 generation time. The LLM identifies which of those hints
    (if any) gave advice that the corrected code disproved.

    Design principle — context-rich, non-prescriptive: state the system's
    purpose and the evidence; let the LLM derive its analysis method.
    Prescriptive step-by-step instructions age poorly as models improve.

    Args:
        failed_code: robot_code v1 from the DB (the auto-generated failing code).
        working_code: robot_code v2 — the developer's manual fix that passed.
        active_hints: [{"id": int, "feedback_text": str, ...}, ...]. Hint ids
            are the only handle the LLM has on individual hints — they MUST be
            preserved into the response. Metadata fields applied_count,
            success_count, failure_count, created_at are always present for
            live hint rows (populated by _select_hints).
        domain: domain string for the workflow URL (e.g. "example.com").
        url: full URL of the page under test.
        user_query: original NL query, truncated to 200 chars in the prompt.

    Returns:
        Prompt string ready for litellm.completion().
    """
    from src.backend.crew_ai.optimization.conflict_detection import (
        _hint_line,
        _build_context_prefix,
    )

    hint_lines = "\n".join(_hint_line(h) for h in active_hints)
    context_prefix = _build_context_prefix(domain, url, user_query)

    return (
        "You are the conflict detection component of an adaptive test automation "
        "learning system.\n\n"
        "This system auto-generates Robot Framework test code guided by learned hints "
        "from past human corrections. The hints listed below were injected into the "
        "agents when v1 was generated — the failed code reflects their influence. "
        "When a developer manually corrects the generated output and it passes, the "
        "system identifies which hints are now proven harmful by that correction.\n\n"
        f"{context_prefix}\n"
        "EXAMPLES — for reference only, do not respond to these:\n\n"
        "Example 1 — DO flag (hint advice was the failing approach, v2 explicitly replaced it):\n"
        "  Active hint:\n"
        "    [17] On this domain, use `Wait For Elements State` with state=visible\n"
        "         before clicking dynamic elements.\n"
        "  v1 (failed):\n"
        "    Wait For Elements State    css=#submit-btn    visible\n"
        "    Click    css=#submit-btn\n"
        "  v2 (passed):\n"
        "    Wait For Elements State    css=#submit-btn    stable\n"
        "    Click    css=#submit-btn\n"
        '  Verdict: {"flag":[{"id":17,"reason":"v1 followed the visible-state approach;'
        " v2 explicitly replaced it with state=stable. The visible-state wait was the"
        ' failing approach."}]}\n\n'
        "Example 2 — DO NOT flag (hint advice present in BOTH versions; fix was elsewhere):\n"
        "  Active hint:\n"
        '    [22] Use data-testid attributes for locators on this domain.\n'
        "  v1 (failed):\n"
        '    Click       css=[data-testid="login"]\n'
        '    Fill Text   css=[data-testid="username"]    alice\n'
        "  v2 (passed):\n"
        '    Click       css=[data-testid="login"]\n'
        "    Sleep       1s\n"
        '    Fill Text   css=[data-testid="username"]    alice\n'
        '  Verdict: {"flag":[]}\n\n'
        "Example 3 — DO NOT flag (hint advice not present in v1 at all):\n"
        "  Active hint:\n"
        "    [44] Use `Select Options By` for native <select> dropdowns.\n"
        "  v1 (failed):\n"
        "    Click    css=button.menu-trigger\n"
        "    Click    css=li.menu-item-3\n"
        "  v2 (passed):\n"
        "    Hover    css=button.menu-trigger\n"
        "    Click    css=li.menu-item-3\n"
        '  Verdict: {"flag":[]}\n\n'
        "FAILED CODE (auto-generated using injected hints — did not pass):\n"
        "```\n"
        f"{failed_code.strip()}\n"
        "```\n\n"
        "CORRECTED CODE (developer's manual fix — passed against the real system):\n"
        "```\n"
        f"{working_code.strip()}\n"
        "```\n\n"
        "INJECTED HINTS (used at v1 generation time — numbers in brackets are unique hint IDs):\n"
        f"{hint_lines}\n\n"
        "Based on this evidence, identify which hints are now proven harmful — "
        "meaning their advice, if applied in future test generation for this domain, "
        "would likely reproduce this class of failure.\n\n"
        "Only a hint whose advice is actually reflected in the failing code and "
        "absent from the corrected code is a candidate for flagging. "
        "When multiple things changed between the two versions, use your knowledge "
        "of Robot Framework and test automation to determine which hint's advice was "
        "causally responsible for the failure — not merely present in the failing code.\n\n"
        "Only flag a hint when you are confident its specific advice is causally "
        "responsible for the failure shown. If the evidence is ambiguous, the connection "
        "is indirect, or the hint's advice is not clearly reflected in the failing code, "
        "do not flag. Preservation is always the safer choice: a hint that survives "
        "incorrectly will be gradually downscored by future executions and auto-disabled; "
        "a hint wrongly flagged loses accumulated learning with no automatic recovery.\n\n"
        "Each reason must specifically address THIS hint's advice and how v2 disproved it. "
        "Do NOT write generic reasons that could apply to multiple hints. "
        "If you cannot articulate a hint-specific reason, do not flag that hint.\n\n"
        "Respond with ONLY valid JSON:\n"
        '{"flag": [{"id": <int>, "reason": "<explanation specific to this hint>"}, ...]}\n'
        'If no hints should be flagged: {"flag": []}'
    )


def _fire_llm_conflict_detection(
    feedback_loop,
    workflow_id: str,
    failed_code: str,
    working_code: str,
    url: str | None,
    injected_hint_ids: str | None = None,
    user_query: str | None = None,
) -> None:
    """Trigger 1 — synchronous LLM conflict detection on a re-run pass.

    Called inline from _process_learning when Case B is detected
    (previously-failed workflow now passes with edited code). Already
    runs inside an asyncio.to_thread worker — must NOT be wrapped in
    another to_thread (no event loop in this thread → RuntimeError,
    silently swallowed by the outer try/except).

    Failure modes are all non-blocking:
    - No active hints in scope → telemetry row written (status='no_active_hints'),
      no LLM call, return.
    - litellm.completion timeout/error → logged warning + telemetry row.
    - JSON parse failure → logged warning + telemetry row.
    - LLM returns non-list `flag` value → guarded by isinstance check.

    Telemetry: one row written to trigger_events per call (including the
    early-return no_active_hints case). Submitted via the write queue
    fire-and-forget; failure to write telemetry never propagates.
    """
    from src.backend.crew_ai.optimization.learning_config import extract_domain
    from src.backend.crew_ai.optimization.conflict_detection import fire_conflict_detection

    domain = extract_domain(url) if url else None
    fire_conflict_detection(
        feedback_loop=feedback_loop,
        trigger_type="trigger_1",
        workflow_id=workflow_id,
        domain=domain,
        url=url,
        feedback_text=None,
        injected_hint_ids=injected_hint_ids,
        prompt_builder=lambda active_hints: _build_conflict_prompt(
            failed_code, working_code, active_hints,
            domain=domain, url=url, user_query=user_query,
        ),
    )


def _process_learning(run_id: str, user_query: str, robot_code: str, result: dict):
    """Feed execution results into the adaptive learning system.

    Non-blocking: failures are logged and swallowed so the main
    pipeline is never affected.

    Guard: skips learning when user_query is empty (paste-and-execute).
    Empty queries pollute ChromaDB embeddings and break SQLite
    deduplication — garbage in, garbage out.
    """
    try:
        feedback_loop = get_feedback_loop()
        if feedback_loop is None:
            return

        # Guard: skip learning when no user query (paste-and-execute)
        if not user_query or not user_query.strip():
            logging.info(
                "⏭️ Skipping learning for %s — no user query "
                "(paste-and-execute mode)", run_id,
            )
            return

        test_status = result.get('test_status', 'unknown')
        output_xml_path = result.get('output_xml_path')
        _exit_code = result.get('exit_code')
        url = extract_url_from_query(user_query) if user_query else None

        # Retrieve and consume hint metadata stored during generation phase.
        # Strip _stored_at (internal timestamp added by _store_hint_metadata)
        # then iterate hint_meta["agents"] — homogeneous dict of
        # {count, available, sources}, no isinstance guard needed.
        with _hint_metadata_lock:
            hint_meta = _hint_metadata_cache.pop(run_id, {})
        hint_meta.pop("_stored_at", None)
        _agents = hint_meta.get("agents", {})
        hints_injected = sum(d.get("count", 0) for d in _agents.values())
        hints_available = sum(d.get("available", d.get("count", 0)) for d in _agents.values())
        all_sources = []
        for d in _agents.values():
            all_sources.extend(d.get("sources", []))
        nl_injected_ids = hint_meta.get("nl_injected_ids", [])
        injected_hint_ids_json = json.dumps(nl_injected_ids)
        # R7 holdout flag — crew.py sets this on hint_metadata when the
        # holdout coin suppressed otherwise-available hints for this run.
        was_holdout = hint_meta.get("was_holdout", False)

        # Always fetch the pre-run record — DB is authoritative and restart-safe
        # for is_first_attempt. pre_run_record is None when this is the first
        # execution of this workflow_id.
        # DEPENDENCY: failed re-runs do not overwrite the DB record (IntegrityError
        # re-raise in _store_sqlite). Do not remove that behaviour without revisiting this.
        pre_run_record = None
        try:
            pre_run_record = feedback_loop.execution_memory.get(run_id)
        except Exception as e:
            logging.warning(
                "[LEARNING:TRIGGER1] pre-process lookup failed (non-blocking): %s",
                e,
            )

        is_first_attempt = (pre_run_record is None)

        # Trigger 1 gate: expose the pre-run record ONLY when the current run
        # passed. Preserves exact Trigger 1 behaviour — original_record is not
        # None only on passed outcomes, same as the previous guard.
        original_record = pre_run_record if test_status == "passed" else None

        feedback_loop.process_execution(
            workflow_id=run_id,
            user_query=user_query or "",
            url=url or "",
            robot_code=robot_code,
            test_status=test_status,
            output_xml_path=output_xml_path,
            metrics=None,  # execution-only mode — no LLM metrics
            is_first_attempt=is_first_attempt,
            hints_available=hints_available,
            hints_injected=hints_injected,
            hint_sources=all_sources,
            injected_hint_ids=injected_hint_ids_json,
            was_holdout=was_holdout,
        )

        # Trigger 1 Step C: Case B detection — previously failed workflow
        # now passes with edited code. Synchronous LLM check on the same
        # worker thread. Each condition matters:
        #   - original_record exists → this is a re-run (else first run, no DB row yet)
        #   - test_status was 'failed' → this is the Case B branch (passed/error excluded)
        #   - both code blobs present → diff is well-defined
        #   - .strip() comparison → whitespace-only edits do not trigger
        #   - nl_engine available → guards the .get_hints_by_id / .get_active_hints_raw + .conflict_flag_hints calls
        #   - circuit_breaker enabled → mirrors process_execution's own gate;
        #     prevents Trigger 1 leaking LLM calls / DB writes when learning
        #     has been disabled by repeated errors. Note: is_enabled() has a
        #     legitimate OPEN→HALF_OPEN side-effect on the breaker; that's
        #     consistent with how process_execution polls it earlier in this
        #     function.
        if (
            original_record is not None
            and original_record.test_status == "failed"
            and original_record.robot_code
            and robot_code
            and original_record.robot_code.strip() != robot_code.strip()
            and feedback_loop.nl_engine is not None
            and feedback_loop.circuit_breaker.is_enabled()
        ):
            _fire_llm_conflict_detection(
                feedback_loop=feedback_loop,
                workflow_id=run_id,
                failed_code=original_record.robot_code,
                working_code=robot_code,
                url=url,
                injected_hint_ids=original_record.injected_hint_ids,
                user_query=user_query,
            )

        logging.info(f"✅ Learning system processed execution {run_id}")
    except Exception as e:
        logging.warning(f"⚠️ Learning system error (non-blocking): {e}")


def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str, progress_queue: Queue = None) -> Generator[Dict[str, Any], None, None]:
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
        model_provider: "local", "gemini", or "vertex"
        model_name: Model identifier
    """
    logging.info("--- Starting CrewAI Workflow with Vision Integration ---")

    # Generate unique workflow ID for metrics tracking
    workflow_id = str(uuid.uuid4())
    logging.info(f"🆔 Workflow ID: {workflow_id}")

    # Bind workflow context so all subsequent log entries include workflow_id
    # without modifying any individual log call sites.
    bind_workflow_context(
        workflow_id=workflow_id,
        model_provider=model_provider,
        model_name=model_name,
        library_type=settings.ROBOT_LIBRARY,
    )

    # Start with welcome message
    yield {"status": "running", "message": f"{EMOJI['start']} Starting test generation...", "progress": 0}

    if model_provider == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            logging.error("Orchestrator: GEMINI_API_KEY not found for gemini provider.")
            yield {"status": "error", "message": "GEMINI_API_KEY not found."}
            return

    elif model_provider == "vertex":
        creds_path = os.getenv("VERTEXAI_CREDENTIALS")
        if not creds_path:
            logging.error("Orchestrator: VERTEXAI_CREDENTIALS not set for vertex provider.")
            yield {"status": "error", "message": "VERTEXAI_CREDENTIALS not set. Point it to your service account JSON file."}
            return
        if not os.path.exists(creds_path):
            logging.error(f"Orchestrator: Credentials file not found at: {creds_path}")
            yield {"status": "error", "message": "Vertex AI credentials file not found. Check that VERTEXAI_CREDENTIALS in your .env points to a valid service account JSON file."}
            return
        if not settings.VERTEXAI_PROJECT:
            logging.error("Orchestrator: VERTEXAI_PROJECT not set for vertex provider.")
            yield {"status": "error", "message": "VERTEXAI_PROJECT not set in .env for Vertex AI."}
            return
        if not settings.VERTEXAI_LOCATION:
            logging.error("Orchestrator: VERTEXAI_LOCATION not set for vertex provider.")
            yield {"status": "error", "message": "VERTEXAI_LOCATION not set in .env for Vertex AI."}
            return

    # Run CrewAI workflow with real-time progress events.
    # Note: Rate limiting was removed during Phase 2 of codebase cleanup.
    # Direct LLM calls are now used without wrappers. Google Gemini API has
    # sufficient rate limits (1500 RPM) for our use case.
    try:
        yield {"status": "running", "message": f"{EMOJI['ai']} Initializing AI agents...", "progress": 3}

        # Real-time progress events are pushed directly to progress_queue by the
        # CrewAI event bus handlers in progress_events.py during crew.kickoff().
        # The OTel span wraps only run_crew() — all 4 LiteLLM calls inside are
        # auto-captured as child spans by OpenLLMetry.
        with create_workflow_span(workflow_id, natural_language_query, model_provider, model_name, settings.ROBOT_LIBRARY):
            _validation_output, crew_with_results, optimization_metrics, hint_metadata, llm_monitor = run_crew(
                natural_language_query, model_provider, model_name, library_type=None, workflow_id=workflow_id,
                progress_queue=progress_queue)

        # Store hint metadata for the execution phase to consume
        if hint_metadata:
            _store_hint_metadata(workflow_id, hint_metadata)

        # Extract robot code from task[2] (code_assembler)
        # With output_pydantic=AssemblyOutput, code is in output.pydantic.code
        # Fall back to output.raw for backward compatibility
        task_output = crew_with_results.tasks[2].output
        
        # Strategy 1: Try Pydantic output (new format with output_pydantic)
        if hasattr(task_output, 'pydantic') and task_output.pydantic:
            robot_code = task_output.pydantic.code
            logging.info("✅ Extracted robot code from output.pydantic.code (AssemblyOutput)")
        # Strategy 2: Try json_dict output
        elif hasattr(task_output, 'json_dict') and task_output.json_dict and 'code' in task_output.json_dict:
            robot_code = task_output.json_dict['code']
            logging.info("✅ Extracted robot code from output.json_dict['code']")
        # Strategy 3: Try parsing raw output as JSON ({"code": "..."})
        else:
            raw_output = getattr(task_output, "raw", "") or ""
            # Guard: Normalize non-string raw outputs (e.g., dict/list) to JSON string
            if not isinstance(raw_output, str):
                raw_output = json.dumps(raw_output)
            try:
                parsed_json = json.loads(raw_output)
                if isinstance(parsed_json, dict) and 'code' in parsed_json:
                    robot_code = parsed_json['code']
                    logging.info("✅ Extracted robot code from parsed JSON in raw output")
                else:
                    # Fallback: use raw output directly (legacy format)
                    robot_code = raw_output
                    logging.info("✅ Using raw output as robot code (legacy format)")
            except (json.JSONDecodeError, TypeError):
                # Raw output is not JSON, use as-is (legacy format)
                robot_code = raw_output
                logging.info("✅ Using raw output as robot code (not JSON)")

        # CRITICAL: Normalize escaped newlines/tabs to actual characters
        # LLM often outputs literal \n instead of actual newlines in JSON
        if '\\n' in robot_code or '\\t' in robot_code or '\\r' in robot_code:
            robot_code = robot_code.replace('\\r\\n', '\n')  # Windows line endings
            robot_code = robot_code.replace('\\n', '\n')
            robot_code = robot_code.replace('\\t', '\t')
            robot_code = robot_code.replace('\\r', '\r')
            logging.info("✅ Normalized escaped newlines/tabs to actual characters")

        # Prefix bare CSS selectors (#id, .class) with `css=` so Robot Framework does
        # not parse them as comments. See robot_code_normalizer for full rationale.
        robot_code = normalize_robot_code(robot_code)

        # Simplified cleaning logic - prompt now handles most cases
        # Keep only essential defensive measures
        
        # Step 1: Handle multiple Settings blocks (LLM might output code multiple times)
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
        
        # Step 2: Final cleanup - remove any trailing non-Robot content
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
        
        # Step 3: Strip trailing JSON artifacts that may leak from LLM output
        # LLM sometimes outputs {"code": "...robot code..."} and the closing "} leaks through
        json_trailing_patterns = [
            '"}',  # JSON closing brace with quote
        ]
        for pattern in json_trailing_patterns:
            if robot_code.endswith(pattern):
                robot_code = robot_code[:-len(pattern)].strip()
                logging.info(f"✅ Stripped trailing JSON artifact: {pattern}")

        # Extract validation output from task[3] (code_validator)
        raw_validation_output = crew_with_results.tasks[3].output.raw

        # Try multiple strategies to extract JSON
        validation_data = None

        # Strategy 1: Try to use output.pydantic or output.json_dict (CrewAI structured output)
        try:
            # First try pydantic attribute (when output_json is a Pydantic model)
            if hasattr(crew_with_results.tasks[3].output, 'pydantic') and crew_with_results.tasks[3].output.pydantic:
                validation_data = crew_with_results.tasks[3].output.pydantic.model_dump(
                )
                logging.info(
                    "✅ Parsed validation output from output.pydantic (Pydantic model)")
            # Fallback to json_dict
            elif hasattr(crew_with_results.tasks[3].output, 'json_dict') and crew_with_results.tasks[3].output.json_dict:
                validation_data = crew_with_results.tasks[3].output.json_dict
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
            # NEW: Collect and merge metrics
            # ============================================
            try:
                # 1. Extract CrewAI metrics
                # Note: In CrewAI 1.3.0, we need to call calculate_usage_metrics() method
                try:
                    usage_metrics_obj = crew_with_results.calculate_usage_metrics()
                    
                    # Convert UsageMetrics object to dict
                    usage_metrics_dict = {
                        'total_tokens': usage_metrics_obj.total_tokens,
                        'prompt_tokens': usage_metrics_obj.prompt_tokens,
                        'completion_tokens': usage_metrics_obj.completion_tokens,
                        'successful_requests': usage_metrics_obj.successful_requests
                    }
                    
                    logging.info(f"📊 Raw CrewAI usage metrics: {usage_metrics_dict}")
                    # NOTE: successful_requests above is inflated — CrewAI's
                    # calculate_usage_metrics() adds the shared LLM's _token_usage once per
                    # agent. The authoritative call count is in "📊 Final LLM Stats" (crew.py),
                    # which reads llm_monitor (agents.llm._monitor) — incremented exactly once
                    # per CleanedLLMWrapper.call() invocation, scoped to this workflow only.

                except Exception as e:
                    logging.warning(f"⚠️ Could not extract CrewAI usage metrics: {e}")
                    # Fallback to empty metrics
                    usage_metrics_dict = {
                        'total_tokens': 0,
                        'prompt_tokens': 0,
                        'completion_tokens': 0,
                        'successful_requests': 0
                    }
                
                crewai_metrics = calculate_crewai_cost(
                    usage_metrics_dict,
                    model_name=model_name
                )
                logging.info(f"📊 CrewAI metrics: {crewai_metrics}")
                
                # 2. Read browser-use metrics from temp file
                temp_storage = get_temp_metrics_storage()
                browser_metrics = temp_storage.read_browser_metrics(workflow_id) or {}
                logging.info(f"📊 Browser-use metrics: {browser_metrics}")
                logging.info(f"📊 DEBUG: browser_metrics tokens = {browser_metrics.get('tokens', 'NOT_FOUND')}")
                logging.info(f"📊 DEBUG: browser_metrics input_tokens = {browser_metrics.get('input_tokens', 'NOT_FOUND')}")
                logging.info(f"📊 DEBUG: browser_metrics output_tokens = {browser_metrics.get('output_tokens', 'NOT_FOUND')}")
                
                # 3. Create unified metrics
                # Calculate averages
                total_elements = browser_metrics.get('elements_processed', 0)
                browser_llm_calls = browser_metrics.get('llm_calls', 0)
                browser_actual_cost = browser_metrics.get('actual_cost', 0.0)
                
                avg_llm_calls = browser_llm_calls / total_elements if total_elements > 0 else 0
                avg_cost = browser_actual_cost / total_elements if total_elements > 0 else 0
                
                unified_metrics = WorkflowMetrics(
                    workflow_id=workflow_id,
                    timestamp=datetime.now(),
                    url=extract_url_from_query(natural_language_query),
                    
                    # Totals
                    total_llm_calls=crewai_metrics['llm_calls'] + browser_llm_calls,
                    total_cost=crewai_metrics['cost'] + browser_actual_cost,
                    execution_time=browser_metrics.get('execution_time', 0),
                    
                    # CrewAI breakdown
                    crewai_llm_calls=crewai_metrics['llm_calls'],
                    crewai_cost=crewai_metrics['cost'],
                    crewai_tokens=crewai_metrics['tokens'],
                    crewai_prompt_tokens=crewai_metrics['prompt_tokens'],
                    crewai_completion_tokens=crewai_metrics['completion_tokens'],
                    
                    # Browser-use breakdown (with granular token tracking)
                    browser_use_llm_calls=browser_llm_calls,
                    browser_use_cost=browser_actual_cost,
                    browser_use_tokens=browser_metrics.get('tokens', 0),
                    browser_use_prompt_tokens=browser_metrics.get('input_tokens', 0),
                    browser_use_completion_tokens=browser_metrics.get('output_tokens', 0),
                    browser_use_cached_tokens=browser_metrics.get('cached_tokens', 0),
                    
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
                    
                    # Per-element approach metrics for pattern analysis
                    element_approach_metrics=browser_metrics.get('element_approach_metrics', []),
                )
                
                # 4. Merge optimization metrics from CrewAI run (context reduction, keyword
                # search stats, pattern predictions) — these are tracked inside crew.py
                # but stored in a separate object that was previously dropped here.
                if optimization_metrics is not None:
                    if optimization_metrics.context_reduction:
                        unified_metrics.context_reduction = optimization_metrics.context_reduction
                    if optimization_metrics.keyword_search_stats:
                        unified_metrics.keyword_search_stats = optimization_metrics.keyword_search_stats
                    if optimization_metrics.pattern_learning_stats:
                        unified_metrics.pattern_learning_stats = optimization_metrics.pattern_learning_stats
                elif settings.OPTIMIZATION_ENABLED:
                    unified_metrics.optimization_fallback_used = True

                # Populate LLM cleaning stats — each workflow has its own monitor instance
                # so concurrent workflows never share counts.
                if llm_monitor is not None:
                    unified_metrics.llm_cleaning_stats = llm_monitor.get_numeric_stats()

                collector = get_workflow_metrics_collector()
                collector.record_workflow(unified_metrics)

                _safe_delete_temp_metrics(workflow_id)
                
                logging.info(f"✅ Unified metrics recorded successfully")
                logging.info(f"   Total LLM calls: {unified_metrics.total_llm_calls} (CrewAI: {unified_metrics.crewai_llm_calls}, Browser-use: {unified_metrics.browser_use_llm_calls})")
                logging.info(f"   Total cost: ${unified_metrics.total_cost:.4f} (CrewAI: ${unified_metrics.crewai_cost:.4f}, Browser-use: ${unified_metrics.browser_use_cost:.4f})")
                
            except Exception as metrics_error:
                logging.error(f"❌ Failed to record unified metrics: {metrics_error}", exc_info=True)
                _safe_delete_temp_metrics(workflow_id)

            # Calculate stats for success message
            lines = len(robot_code.split('\n'))
            
            # Show finalizing step before completion
            yield {"status": "running", "message": f"{EMOJI['success']} Finalizing test code..."}

            # Confirm success with line count (progress already at 100% from event bus)
            yield {"status": "running", "message": f"{EMOJI['success']} Success! Generated {lines} lines of test code."}
            
            # Final completion message (without progress, as it's already at 100%)
            yield {"status": "complete", "robot_code": robot_code, "workflow_id": workflow_id, "message": f"{EMOJI['success']} Test generation complete."}
        else:
            logging.error(
                f"CrewAI workflow finished, but code validation failed. Reason: {validation_data.get('reason')}")
            _safe_delete_temp_metrics(workflow_id)
            _safe_evict_hint_metadata(workflow_id)
            yield {"status": "error", "message": f"Code validation failed: {validation_data.get('reason')}"}

    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        logging.error(
            "Failed to generate valid Robot Framework code." + str(e))
        _safe_delete_temp_metrics(workflow_id)
        _safe_evict_hint_metadata(workflow_id)
        try:
            logging.error(
                f"Failed to parse validation output from crew: {e}\nRaw output was:\n{raw_validation_output}")
            yield {"status": "error", "message": "Failed to parse validation output from the crew.", "robot_code": robot_code}
        except:
            yield {"status": "error", "message": f"Failed to parse validation output: {e}"}
    except Exception as e:
        logging.error(
            f"An unexpected error occurred during the CrewAI workflow: {e}", exc_info=True)

        _safe_delete_temp_metrics(workflow_id)
        _safe_evict_hint_metadata(workflow_id)

        yield {"status": "error", "message": f"An error occurred: {str(e)}"}
    


def run_workflow_in_thread(
    queue: Queue,
    user_query: str,
    model_provider: str,
    model_name: str,
    releaser: "_SlotReleaser | None" = None,
):
    """Runs the synchronous agentic workflow and puts results in a queue.

    Passes the queue to run_agentic_workflow() so it can be forwarded to run_crew(),
    where the CrewAI event bus handlers push real-time progress events directly.

    If a _SlotReleaser is provided, calls releaser.done() unconditionally in the
    finally block so the countdown latch can release the slot even when the SSE
    client has already disconnected and the generator's finally fired first.
    """
    try:
        for event in run_agentic_workflow(user_query, model_provider, model_name, progress_queue=queue):
            queue.put(event)
    except Exception as e:
        logging.error(f"Exception in workflow thread: {e}")
        queue.put({"status": "error", "message": f"Workflow thread failed: {e}"})
    finally:
        if releaser is not None:
            releaser.done()


# ---------------------------------------------------------------------------
# Private helpers — eliminate duplication across the three stream functions
# ---------------------------------------------------------------------------

class _GenerationError(Exception):
    """Sentinel raised inside _drain_generation_queue to signal the caller should return early.

    Not a real error — caught immediately by the caller's try/except block.
    Using an exception avoids polluting the generator's yield type and keeps
    the caller's control flow explicit.
    """


def _capacity_error_sse(stage: str) -> str:
    """Return a formatted SSE capacity-exceeded error string for the given pipeline stage."""
    max_wf = settings.MAX_CONCURRENT_WORKFLOWS
    return (
        f"data: {json.dumps({'stage': stage, 'status': 'error', 'message': f'Service is at capacity ({max_wf}/{max_wf} active workflows). Please try again later.'})}\n\n"
    )


def _start_workflow_thread(
    q: Queue, user_query: str, model_provider: str, model_name: str, releaser: "_SlotReleaser"
) -> Thread:
    """Start the workflow thread, pre-decrementing the releaser if start() raises.

    If Thread.start() fails before the thread ever runs, the thread's share of the
    latch will never be decremented by run_workflow_in_thread's finally block.
    Pre-decrementing here ensures the generator's own done() still reaches zero.
    """
    thread = Thread(
        target=run_workflow_in_thread,
        args=(q, user_query, model_provider, model_name, releaser),
    )
    try:
        thread.start()
    except Exception:
        releaser.done()
        raise
    return thread


async def _drain_generation_queue(workflow_thread: Thread, q: Queue, result_store: dict):
    """Drain the workflow queue, yielding SSE generation events.

    Populates result_store with 'robot_code' and 'workflow_id' on completion.
    Raises _GenerationError when an error event is seen or no code is produced,
    so the caller can return early.  The error SSE is always yielded before
    raising, so the client receives it.
    """
    while workflow_thread.is_alive():
        try:
            event = q.get_nowait()
            yield f"data: {json.dumps({'stage': 'generation', **event})}\n\n"
            if event.get("status") == "complete" and "robot_code" in event:
                result_store["robot_code"] = event["robot_code"]
                result_store["workflow_id"] = event.get("workflow_id")
                workflow_thread.join()
                return
            elif event.get("status") == "error":
                workflow_thread.join()
                raise _GenerationError()
        except Empty:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    # Thread finished — drain any remaining buffered events
    while not q.empty():
        event = q.get_nowait()
        yield f"data: {json.dumps({'stage': 'generation', **event})}\n\n"
        if event.get("status") == "complete" and "robot_code" in event:
            result_store["robot_code"] = event["robot_code"]
            result_store["workflow_id"] = event.get("workflow_id")
        elif event.get("status") == "error":
            raise _GenerationError()

    if not result_store.get("robot_code"):
        msg = "Agentic workflow finished without generating code."
        logging.error(msg)
        yield f"data: {json.dumps({'stage': 'generation', 'status': 'error', 'message': msg})}\n\n"
        raise _GenerationError()


async def _stream_docker_execution(run_id: str, robot_code: str, user_query: str | None, release_slot):
    """Save robot_code to disk, run in Docker, and trigger learning.

    Yields SSE data strings for all execution events (build progress, result, errors).
    Handles all error paths internally — the caller's finally block remains
    responsible for slot and hint-cache cleanup.

    release_slot is a 0-arg callable (the caller's _SlotReleaser.done). It is
    invoked the moment the result SSE has been sent, so the concurrency slot is
    freed before the background learning step — which may make a 5-30s Trigger 1
    LLM call — runs. Idempotent: the caller's finally releases the slot too.
    """
    robot_tests_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "robot_tests"
    )
    run_dir = os.path.join(robot_tests_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    test_filename = "test.robot"
    test_filepath = os.path.join(run_dir, test_filename)

    try:
        with open(test_filepath, "w", encoding="utf-8") as f:
            f.write(robot_code)
        logging.info(f"📝 Saved test code to {test_filepath}")
    except Exception as e:
        logging.error(f"Failed to save test code: {e}")
        _safe_evict_hint_metadata(run_id)
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Failed to save test code: {str(e)}'})}\n\n"
        return

    try:
        # Offload all blocking Docker I/O to a thread so the event loop
        # remains free to serve heartbeats and other concurrent requests.
        # Lambda keeps build_image() generator creation and consumption in
        # the same worker thread, avoiding cross-thread generator handoff.
        client = await asyncio.to_thread(get_docker_client)
        build_events = await asyncio.to_thread(lambda: list(build_image(client)))
        for event in build_events:
            yield f"data: {json.dumps({'stage': 'execution', **event})}\n\n"

        logging.info(f"🚀 Executing test: {test_filename}")
        result = await asyncio.to_thread(run_test_in_container, client, run_id, test_filename)
        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"

        # The user now has their result. Release the workflow slot before the
        # background learning step so a Case B re-run's Trigger 1 LLM call
        # (~5-30s) does not hold concurrency capacity it has no need for.
        release_slot()

        await asyncio.to_thread(_process_learning, run_id, user_query, robot_code, result)

    except Exception as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        _safe_evict_hint_metadata(run_id)
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


async def stream_generate_only(user_query: str, model_provider: str, model_name: str) -> AsyncGenerator[str, None]:
    """
    Generates Robot Framework test code without executing it.
    Allows user to review and edit before execution.
    """
    if not _acquire_workflow_slot():
        yield _capacity_error_sse("generation")
        return

    # Two-party latch: thread + generator both call done().
    # Slot is released only when the last of the two finishes.
    # This prevents premature release when the client disconnects mid-stream
    # while the LLM thread is still running.
    releaser = _SlotReleaser()
    try:
        q = Queue()
        workflow_thread = _start_workflow_thread(q, user_query, model_provider, model_name, releaser)
        result_store: dict = {}
        try:
            async for sse in _drain_generation_queue(workflow_thread, q, result_store):
                yield sse
        except _GenerationError:
            return
        logging.info("✅ Test generation complete. Ready for user review.")
    finally:
        releaser.done()  # Generator's share of the latch


async def stream_execute_only(robot_code: str, user_query: str = None, workflow_id: str = None) -> AsyncGenerator[str, None]:
    """
    Executes provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.

    Args:
        robot_code: Robot Framework test code to execute
        user_query: Optional original user query for pattern learning
        workflow_id: Optional workflow ID from generation phase (for unified ID tracking)
    """
    if not robot_code or not robot_code.strip():
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': 'No test code provided'})}\n\n"
        return

    if not _acquire_workflow_slot():
        yield _capacity_error_sse("execution")
        return

    # Single-participant latch (no separate generation thread). The slot is
    # released early by _stream_docker_execution the moment the result SSE is
    # sent; this releaser's finally call is the guaranteed fallback for paths
    # that never reach that point. done() is idempotent — calling it twice
    # releases the slot exactly once.
    releaser = _SlotReleaser(participant_count=1)

    # Guard: finally must not raise NameError if we return before run_id is assigned
    # (happens when workflow_id is present but fails UUID validation).
    run_id = None
    try:
        # Validate or generate the run ID.
        # workflow_id comes from an untrusted request body; if present it must be a
        # canonical UUID4 string so it is safe to use as a directory name.
        # Path traversal (e.g. "../../tmp/x") is blocked because uuid.UUID() rejects
        # anything that is not a 32-hex-digit UUID representation.
        if workflow_id:
            try:
                run_id = str(uuid.UUID(workflow_id))  # normalises and validates format
            except ValueError:
                yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': 'Invalid workflow_id: must be a UUID'})}\n\n"
                return
        else:
            run_id = str(uuid.uuid4())
        logging.info(f"🆔 Execution ID (unified): {run_id}")

        async for sse in _stream_docker_execution(run_id, robot_code, user_query, releaser.done):
            yield sse
    finally:
        # Guard against CancelledError (BaseException, not caught by except above):
        # if the task was cancelled between await points, _process_learning never
        # ran and its cache pop never fired. No-op if already consumed.
        if run_id is not None:
            _safe_evict_hint_metadata(run_id)
        releaser.done()


async def stream_generate_and_run(user_query: str, model_provider: str, model_name: str) -> AsyncGenerator[str, None]:
    """
    Legacy endpoint: Generates and executes test in one flow.
    Kept for backward compatibility.
    """
    if not _acquire_workflow_slot():
        yield _capacity_error_sse("generation")
        return

    # Two-party latch: thread (phase 1 — LLM generation) + generator (phase 2 — Docker).
    # Slot is released only when both have finished, preventing premature release on
    # client disconnect during phase 1 while the LLM thread is still consuming quota.
    releaser = _SlotReleaser()
    # Guard: finally must not raise NameError if we return before run_id is assigned
    # (happens when UUID validation fails or generation errors before reaching execution).
    run_id = None
    try:
        q = Queue()
        workflow_thread = _start_workflow_thread(q, user_query, model_provider, model_name, releaser)

        result_store: dict = {}
        try:
            async for sse in _drain_generation_queue(workflow_thread, q, result_store):
                yield sse
        except _GenerationError:
            return

        robot_code = result_store["robot_code"]
        workflow_id = result_store.get("workflow_id")

        # Validate workflow_id before using as a directory name: uuid.UUID() rejects
        # anything that is not a canonical UUID, blocking path traversal like "../../tmp/x".
        if workflow_id:
            try:
                run_id = str(uuid.UUID(workflow_id))
            except ValueError:
                yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': 'Invalid workflow_id'})}\n\n"
                return
        else:
            run_id = str(uuid.uuid4())
        logging.info(f"🆔 Execution ID (unified with generation): {run_id}")

        async for sse in _stream_docker_execution(run_id, robot_code, user_query, releaser.done):
            yield sse
    finally:
        # Guard against CancelledError (BaseException, not caught by except above):
        # if the task was cancelled between await points, _process_learning never
        # ran and its cache pop never fired. No-op if already consumed.
        if run_id is not None:
            _safe_evict_hint_metadata(run_id)
        releaser.done()  # Generator's share of the latch
