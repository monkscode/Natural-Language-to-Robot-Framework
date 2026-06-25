import os
import uuid
import logging
import json
import asyncio
from queue import Queue, Empty
from threading import Thread
import threading
from typing import AsyncGenerator, Generator, Dict, Any
from datetime import datetime, timezone

from src.backend.crew_ai.crew import run_crew, extract_url_from_query
from src.backend.runner_exec import client as runner_exec_client
from src.backend.services.dryrun_service import extract_and_normalize_robot_code, validate_and_repair
from src.backend.config.logging_config import EMOJI, bind_workflow_context
from src.backend.core.observability import create_workflow_span
from src.backend.core.temp_metrics_storage import get_temp_metrics_storage
from src.backend.core.workflow_metrics import (
    get_workflow_metrics_collector,
    WorkflowMetrics,
    calculate_crewai_cost
)
from src.backend.core.run_registry import get_run_registry
from src.backend.core.artifact_store import get_artifact_store
from src.backend.services.report_inliner import inline_report_screenshots
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


def _build_standalone_attribution_prompt(
    working_code: str,
    active_hints: list,
    domain: str | None = None,
    url: str | None = None,
    user_query: str | None = None,
) -> str:
    """Standalone (first-pass) usage-attribution prompt — Part 2.

    Used when a test PASSES on its first attempt (no prior failing version). The
    LLM decides, per injected hint, whether its advice is reflected in the
    passing code (`used`) or not (`unused`). No `harmful` bucket — that needs a
    v1->v2 diff (see _build_merged_attribution_prompt).

    Hints are rendered id+text only (no historical counters): usage is a
    code-presence judgment, and showing a hint's own success history would bias
    the model toward crediting it — a self-reinforcing loop this system exists to
    avoid. Strong-history protection lives in code (the flag-core guard), not here.

    Response shape consumed by fire_usage_attribution:
        {"used": [<id>, ...], "unused": [<id>, ...]}
    """
    from src.backend.crew_ai.optimization.conflict_detection import _build_context_prefix

    hint_lines = "\n".join(
        f"  [{h['id']}] {h['feedback_text']}" for h in active_hints
    )
    context_prefix = _build_context_prefix(domain, url, user_query)

    return (
        "You are the usage-attribution component of an adaptive test automation "
        "learning system.\n\n"
        "This system auto-generates Robot Framework test code. To guide it, we inject "
        "HINTS — short pieces of free-form English feedback that past users left "
        "(e.g. \"on this site, wait for elements to be visible before clicking\" or "
        "\"prefer data-testid locators\"). They are guidance, not code to copy. The "
        "hints below were given to the agents that produced the PASSING code shown.\n\n"
        "Your job: for EACH hint, decide whether that hint's GUIDANCE actually shaped "
        "the passing code.\n\n"
        f"{context_prefix}\n"
        "PASSING CODE (auto-generated, passed against the real system):\n"
        "```\n"
        f"{working_code.strip()}\n"
        "```\n\n"
        "INJECTED HINTS — the number in brackets is the hint's unique ID; echo it exactly:\n"
        f"{hint_lines}\n\n"
        "Classify each hint as exactly one of:\n"
        "  - \"used\"   — the hint's specific advice is clearly reflected in the passing "
        "code: a locator strategy, keyword, wait, value, assertion, or structure it "
        "recommended is present because of it.\n"
        "  - \"unused\" — the hint was injected but its advice is NOT reflected (the code "
        "passed without needing it, e.g. the advice did not apply to this test).\n\n"
        "Rules:\n"
        "  - Judge whether the ADVICE is present in spirit — not whether the hint's exact "
        "words appear. \"wait for visibility\" is used if the code waits for visibility, "
        "in any wording.\n"
        "  - Do not credit a hint for something the code would have done anyway, "
        "unconnected to the advice.\n"
        "  - If you genuinely cannot tell for a hint, OMIT it from both lists. Omitting is "
        "the safe choice — it records no signal for that hint.\n"
        "  - Put each hint id in at most one list.\n\n"
        "EXAMPLES (format only — do not answer these):\n"
        "  Hint [7] \"page loads slowly — wait for the element to be visible before clicking.\"\n"
        "    Code: Wait For Elements State  css=#go  visible  /  Click  css=#go\n"
        "    -> used.\n"
        "  Hint [12] \"prefer data-testid attributes for locators.\"\n"
        "    Code: Click  id=submit   (an id locator, no data-testid anywhere)\n"
        "    -> unused.\n\n"
        "Respond with ONLY valid JSON, nothing else:\n"
        '{"used": [<int>, ...], "unused": [<int>, ...]}'
    )


def _build_merged_attribution_prompt(
    failed_code: str,
    working_code: str,
    active_hints: list,
    domain: str | None = None,
    url: str | None = None,
    user_query: str | None = None,
) -> str:
    """Merged Case-B usage-attribution prompt — Part 2 (replaces the former
    Trigger-1 conflict prompt; one LLM call now yields credit AND harm-flag).

    Used when a previously-FAILED workflow re-runs with developer-edited code and
    PASSES. The LLM sees v1 (failed, hint-influenced) and v2 (passed) and sorts
    each injected hint into used / harmful / unused. Because credit and flagging
    come from the SAME judgment over the SAME v1/v2 pair, they cannot contradict.

    Hints are rendered id+text only (no counters) — same anti-bias rationale as
    the standalone prompt; strong-history protection is enforced in code.

    Response shape consumed by fire_usage_attribution:
        {"used": [<id>, ...],
         "harmful": [{"id": <int>, "reason": "<hint-specific>"}, ...],
         "unused": [<id>, ...]}
    """
    from src.backend.crew_ai.optimization.conflict_detection import _build_context_prefix

    hint_lines = "\n".join(
        f"  [{h['id']}] {h['feedback_text']}" for h in active_hints
    )
    context_prefix = _build_context_prefix(domain, url, user_query)

    return (
        "You are the usage-attribution component of an adaptive test automation "
        "learning system.\n\n"
        "This system auto-generates Robot Framework test code, guided by HINTS — short "
        "pieces of free-form English feedback from past users (guidance, not code to "
        "copy). The hints below were injected when v1 (the FAILED code) was generated. A "
        "developer then manually corrected it into v2, which PASSED. By comparing v1 and "
        "v2 you can see which hints helped, which were irrelevant, and which actively "
        "caused the failure.\n\n"
        "Your job: sort EACH hint into exactly one of used / harmful / unused.\n\n"
        f"{context_prefix}\n"
        "FAILED CODE (v1 — auto-generated using the injected hints, did not pass):\n"
        "```\n"
        f"{failed_code.strip()}\n"
        "```\n\n"
        "CORRECTED CODE (v2 — developer's manual fix, passed against the real system):\n"
        "```\n"
        f"{working_code.strip()}\n"
        "```\n\n"
        "INJECTED HINTS — the number in brackets is the hint's unique ID; echo it exactly:\n"
        f"{hint_lines}\n\n"
        "Classify each hint as exactly one of:\n"
        "  - \"used\"    — the hint's advice is reflected in the PASSING v2 code (it helped).\n"
        "  - \"harmful\" — the hint's advice is reflected in v1, the developer REMOVED or "
        "REPLACED it in v2, AND you are confident that advice was causally responsible for "
        "the failure. Give a short reason specific to THIS hint. (Marking a hint harmful "
        "suspends it from future tests.)\n"
        "  - \"unused\"  — injected but its advice is not reflected in v2 and it is not "
        "harmful (e.g. the advice did not apply to this test).\n\n"
        "Rules:\n"
        "  - Judge the ADVICE in spirit, not literal wording.\n"
        "  - \"harmful\" requires ALL of: the advice was actually followed in v1; v2 "
        "deliberately did it differently; and that change is what fixed the failure. Advice "
        "present in BOTH v1 and v2 is NOT harmful (the fix was elsewhere). Advice not "
        "present in v1 is NOT harmful.\n"
        "  - Be conservative with \"harmful\": preservation is safer. A hint wrongly kept is "
        "gradually downscored by future runs; a hint wrongly marked harmful loses its "
        "learning with no automatic recovery. Only mark harmful when confident and able to "
        "give a hint-specific reason. Generic reasons that could apply to many hints are "
        "not acceptable — omit instead.\n"
        "  - If you genuinely cannot tell for a hint, OMIT it entirely (no signal).\n"
        "  - Put each hint id in at most one list.\n\n"
        "EXAMPLES (format only — do not answer these):\n"
        "  Hint [17] \"wait for elements to be visible before clicking dynamic elements.\"\n"
        "    v1 (failed): Wait For Elements State  css=#submit  visible  /  Click  css=#submit\n"
        "    v2 (passed): Wait For Elements State  css=#submit  stable   /  Click  css=#submit\n"
        "    -> harmful. reason: \"advised waiting for 'visible', but the element was visible "
        "yet not ready; v2 fixed it by waiting for 'stable'.\"\n"
        "  Hint [22] \"use data-testid attributes for locators.\"\n"
        "    v1: Click  css=[data-testid=\"login\"]   (failed for an unrelated reason)\n"
        "    v2: Click  css=[data-testid=\"login\"]    (same locator; fix was a wait added elsewhere)\n"
        "    -> used (advice is in the passing v2 code; the failure was unrelated).\n"
        "  Hint [44] \"use Select Options By for native <select> dropdowns.\"\n"
        "    v1 and v2: neither touches a dropdown (this test has none).\n"
        "    -> unused.\n\n"
        "Respond with ONLY valid JSON, nothing else:\n"
        '{"used": [<int>, ...], '
        '"harmful": [{"id": <int>, "reason": "<specific to this hint>"}, ...], '
        '"unused": [<int>, ...]}'
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

        # Re-run after edit: _hint_metadata_cache was consumed by the v1
        # _process_learning call (.pop is destructive), so nl_injected_ids
        # defaulted to []. Recover the original generation-time hint IDs from
        # the DB — Schema v10 preserves execution_records.injected_hint_ids
        # across _update_to_passing_state. Without this, the re-run pass would
        # see injected_hint_ids='[]', the attribution gate below would skip, and
        # the hints actually injected at v1 would never be credited on the pass.
        if (
            not nl_injected_ids
            and pre_run_record is not None
            and pre_run_record.injected_hint_ids
        ):
            injected_hint_ids_json = pre_run_record.injected_hint_ids
            logging.info(
                "[LEARNING] %s: recovered injected_hint_ids=%s from DB "
                "(hint metadata cache consumed by prior run)",
                run_id, injected_hint_ids_json,
            )

        try:
            from src.backend.core.run_registry import get_run_registry
            _, _run_org_id = get_run_registry().get_run_owner(run_id)
        except Exception:
            _run_org_id = None

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
            org_id=_run_org_id,
        )

        # N3 (F2d): persist the reconciled selection trace on the writer thread.
        # Queued AFTER store(record) and BEFORE attribution so the later
        # apply_hint_attribution UPDATE (F2e) finds the rows (one FIFO queue).
        # Best-effort observability, gated by HINT_TRACE_ENABLED; runs on every
        # outcome (selection happens regardless of pass/fail).
        selection_trace = hint_meta.get("selection_trace")
        if selection_trace and settings.HINT_TRACE_ENABLED:
            feedback_loop.write_queue.submit(
                feedback_loop.execution_memory.store_hint_workflow_trace,
                run_id, selection_trace,
            )

        # NL-hint usage attribution (Part 2): on a passing run with NL hints
        # injected, credit only the hints actually used in the code via one LLM
        # judgment per workflow. Replaces the old all-injected crediting AND
        # merges the former Trigger-1 conflict check in as the Case-B branch.
        #
        # F3 gate: injected_hint_ids_json is a JSON STRING — parse it. Evaluate
        # `pre_run_record is None` FIRST in the once-guard disjunct (on a first
        # pass pre_run_record is None, so pre_run_record.hint_attribution_done
        # would AttributeError). The atomic claim inside apply_hint_attribution
        # is the authoritative once-guard; this gate is only a cost early-out
        # (skip the LLM on an already-credited passing re-run). NO circuit-breaker
        # check — attribution must never touch the shared breaker (R-M1).
        try:
            _ids = json.loads(injected_hint_ids_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            logging.warning(
                "[LEARNING] %s: malformed injected_hint_ids %r — skipping attribution",
                run_id, injected_hint_ids_json,
            )
            _ids = None
        if (
            test_status == "passed"
            and isinstance(_ids, list) and len(_ids) > 0
            and (pre_run_record is None or not pre_run_record.hint_attribution_done)
            and feedback_loop.nl_engine is not None
        ):
            from src.backend.crew_ai.optimization.learning_config import extract_domain
            from src.backend.crew_ai.optimization.conflict_detection import (
                fire_usage_attribution,
            )

            # Case B (merged): a previously-FAILED workflow now passes with
            # developer-edited code → one judgment sorts each hint into used /
            # harmful / unused over the v1->v2 diff. Otherwise standalone
            # (used / unused; no v1 to diff against).
            is_case_b = (
                pre_run_record is not None
                and pre_run_record.test_status == "failed"
                and pre_run_record.robot_code
                and robot_code
                and pre_run_record.robot_code.strip() != robot_code.strip()
            )
            domain = extract_domain(url) if url else None
            if is_case_b:
                _failed_code = pre_run_record.robot_code
                prompt_builder = lambda active_hints: _build_merged_attribution_prompt(
                    _failed_code, robot_code, active_hints,
                    domain=domain, url=url, user_query=user_query,
                )
            else:
                prompt_builder = lambda active_hints: _build_standalone_attribution_prompt(
                    robot_code, active_hints,
                    domain=domain, url=url, user_query=user_query,
                )
            fire_usage_attribution(
                feedback_loop=feedback_loop,
                workflow_id=run_id,
                domain=domain,
                url=url,
                feedback_text=None,
                injected_hint_ids=injected_hint_ids_json,
                case_b=is_case_b,
                prompt_builder=prompt_builder,
            )

        logging.info(f"✅ Learning system processed execution {run_id}")
    except Exception as e:
        logging.warning(f"⚠️ Learning system error (non-blocking): {e}")


def run_agentic_workflow(natural_language_query: str, model_provider: str, model_name: str, progress_queue: Queue = None, org_id: str | None = None, user_id: str | None = None) -> Generator[Dict[str, Any], None, None]:
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
        org_id=org_id,
        user_id=user_id,
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
        # The OTel span wraps only run_crew() — the LiteLLM calls inside are
        # auto-captured as child spans by OpenLLMetry. The dryrun gate + repair run
        # AFTER this span closes (R5); their calls are still captured by the
        # authoritative LiteLLM trace callback, just outside this workflow span.
        with create_workflow_span(workflow_id, natural_language_query, model_provider, model_name, settings.ROBOT_LIBRARY,
                                  org_id=org_id, user_id=user_id):
            # run_crew's first element is crew.kickoff()'s CrewOutput (the terminal
            # task is now the Assembler — there is no validator verdict). Unused here;
            # delivered code is read from crew_with_results.tasks[2] below.
            # org_id comes from the authenticated user (threaded down from the SSE
            # entry point); legacy/unauthenticated callers pass None → unscoped.
            _crew_output, crew_with_results, optimization_metrics, hint_metadata, llm_monitor = run_crew(
                natural_language_query, model_provider, model_name, library_type=None, workflow_id=workflow_id,
                progress_queue=progress_queue, org_id=org_id)

        # Store hint metadata for the execution phase to consume
        if hint_metadata:
            _store_hint_metadata(workflow_id, hint_metadata)

        # Extract robot code from task[2] (Code Assembler — the terminal crew task)
        # and apply the shared normalization pipeline (also used by the dryrun repair
        # path) so both normalize identically.
        robot_code = extract_and_normalize_robot_code(crew_with_results.tasks[2].output)

        # Deterministic robot --dryrun gate + bounded Assembler repair loop.
        # SOFT gate: Docker down / any error degrades to dryrun_status:'unverified'
        # and still delivers; it never raises here. Repair LLM cost is folded into
        # crewai_* below. Runs in this worker thread (blocking Docker I/O is fine —
        # not the asyncio loop) and AFTER create_workflow_span has closed (R5).
        gate = validate_and_repair(
            workflow_id, robot_code, model_provider, model_name,
            progress_queue=progress_queue,
        )
        robot_code = gate["code"]

        logging.info(
            "Generated Robot Framework code is here:\n%s", robot_code)
        logging.info(
            "CrewAI workflow complete. Dryrun gate status: %s", gate["dryrun_status"])

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
            # Fold the dryrun repair crew's LLM cost into the CrewAI bucket. The repair
            # mini-crew replaced the removed validator agent (whose cost used to live
            # here), and it is a FRESH RobotAgents — the main crew's
            # calculate_usage_metrics() never saw these calls, so there is no double
            # count. total_llm_calls/total_cost derive from crewai_metrics below, so the
            # totals update automatically (decision 5 / §5).
            _repair_usage = gate.get("repair_usage") or {}
            if _repair_usage:
                crewai_metrics['llm_calls'] += _repair_usage.get('llm_calls', 0)
                crewai_metrics['cost'] = round(crewai_metrics['cost'] + _repair_usage.get('cost', 0.0), 6)
                crewai_metrics['tokens'] += _repair_usage.get('tokens', 0)
                crewai_metrics['prompt_tokens'] += _repair_usage.get('prompt_tokens', 0)
                crewai_metrics['completion_tokens'] += _repair_usage.get('completion_tokens', 0)
                logging.info(f"📊 Folded dryrun repair usage into CrewAI metrics: {_repair_usage}")

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
            # so concurrent workflows never share counts. MAIN-crew counters only:
            # the dryrun repair mini-crew (dryrun_service.repair_robot_code) builds a
            # fresh monitor whose cleaning/empty-response counts are deliberately NOT
            # folded here. Its COST is folded into crewai_metrics above; repair is rare
            # and few-call on the SAME model, so the main crew's counts already surface
            # any flakiness. Only cost needs to be exact — these counters do not.
            if llm_monitor is not None:
                unified_metrics.llm_cleaning_stats = llm_monitor.get_numeric_stats()

            collector = get_workflow_metrics_collector()
            _run_org_id: str | None = None
            try:
                _, _run_org_id = get_run_registry().get_run_owner(workflow_id)
            except Exception:
                pass
            collector.record_workflow(unified_metrics, org_id=_run_org_id)

            _safe_delete_temp_metrics(workflow_id)

            logging.info("✅ Unified metrics recorded successfully")
            logging.info(f"   Total LLM calls: {unified_metrics.total_llm_calls} (CrewAI: {unified_metrics.crewai_llm_calls}, Browser-use: {unified_metrics.browser_use_llm_calls})")
            logging.info(f"   Total cost: ${unified_metrics.total_cost:.4f} (CrewAI: ${unified_metrics.crewai_cost:.4f}, Browser-use: ${unified_metrics.browser_use_cost:.4f})")

        except Exception as metrics_error:
            logging.error(f"❌ Failed to record unified metrics: {metrics_error}", exc_info=True)
            _safe_delete_temp_metrics(workflow_id)

        # Calculate stats for success message
        lines = len(robot_code.split('\n'))

        # Show finalizing step before completion
        yield {"status": "running", "message": f"{EMOJI['success']} Finalizing test code..."}

        # Confirm success with line count
        yield {"status": "running", "message": f"{EMOJI['success']} Success! Generated {lines} lines of test code."}

        # Terminal 100% — pushed here (the event bus now caps at 80 and crew.py no
        # longer pushes 100) so the bar reaches 100 only AFTER the dryrun gate has
        # finished verifying/repairing (prog-2/prog-6). Guarded for progress_queue=None.
        if progress_queue is not None:
            progress_queue.put({"status": "running", "progress": 100, "message": f"{EMOJI['success']} Test generation complete"})

        # Final completion. Reuse status:'complete' (NO new top-level status — a new
        # value would fall through the frontend switch and hang the button). When the
        # gate did not pass, attach dryrun_status + dryrun_errors so the frontend can
        # surface a warning without a new status.
        complete_event = {"status": "complete", "robot_code": robot_code, "workflow_id": workflow_id, "message": f"{EMOJI['success']} Test generation complete."}
        if gate["dryrun_status"] != "passed":
            complete_event["dryrun_status"] = gate["dryrun_status"]
            complete_event["dryrun_errors"] = gate.get("dryrun_errors", "")
        yield complete_event

    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        logging.error("Failed to generate valid Robot Framework code: %s", e)
        _safe_delete_temp_metrics(workflow_id)
        _safe_evict_hint_metadata(workflow_id)
        yield {"status": "error", "message": f"Failed to generate valid Robot Framework code: {e}"}
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
    org_id: str | None = None,
    user_id: str | None = None,
):
    """Runs the synchronous agentic workflow and puts results in a queue.

    Passes the queue to run_agentic_workflow() so it can be forwarded to run_crew(),
    where the CrewAI event bus handlers push real-time progress events directly.

    If a _SlotReleaser is provided, calls releaser.done() unconditionally in the
    finally block so the countdown latch can release the slot even when the SSE
    client has already disconnected and the generator's finally fired first.
    """
    try:
        for event in run_agentic_workflow(user_query, model_provider, model_name, progress_queue=queue, org_id=org_id, user_id=user_id):
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


def _record_run(run_id: str, user: dict | None, user_query: str | None, status: str,
                robot_code: str | None = None, rerun_of: str | None = None) -> None:
    """History bookkeeping (test_runs row) — must never break the run pipeline.

    get_run_registry() itself can raise on first use when Postgres is down, so
    the guard sits here rather than relying on the registry's internal
    swallowing alone.
    """
    try:
        get_run_registry().record_start(
            run_id, user, user_query, status,
            robot_code=robot_code, rerun_of=rerun_of,
        )
    except Exception as e:
        logging.error(f"[RUN_REGISTRY] unavailable — run {run_id} not recorded: {e}")


def _set_run_status(run_id: str, status: str) -> None:
    """Advance a test_runs row's status — never breaks the run pipeline."""
    try:
        get_run_registry().set_status(run_id, status)
    except Exception as e:
        logging.error(f"[RUN_REGISTRY] unavailable — status for {run_id} not recorded: {e}")


def _run_owner(run_id: str) -> str | None:
    """test_runs owner lookup that never breaks the run pipeline (None on error)."""
    try:
        return get_run_registry().get_owner(run_id)
    except Exception as e:
        logging.error(f"[RUN_REGISTRY] owner lookup failed for {run_id}: {e}")
        return None


def _capacity_error_sse(stage: str) -> str:
    """Return a formatted SSE capacity-exceeded error string for the given pipeline stage."""
    max_wf = settings.MAX_CONCURRENT_WORKFLOWS
    return (
        f"data: {json.dumps({'stage': stage, 'status': 'error', 'message': f'Service is at capacity ({max_wf}/{max_wf} active workflows). Please try again later.'})}\n\n"
    )


def _start_workflow_thread(
    q: Queue, user_query: str, model_provider: str, model_name: str, releaser: "_SlotReleaser",
    org_id: str | None = None,
    user_id: str | None = None,
) -> Thread:
    """Start the workflow thread, pre-decrementing the releaser if start() raises.

    If Thread.start() fails before the thread ever runs, the thread's share of the
    latch will never be decremented by run_workflow_in_thread's finally block.
    Pre-decrementing here ensures the generator's own done() still reaches zero.
    """
    thread = Thread(
        target=run_workflow_in_thread,
        args=(q, user_query, model_provider, model_name, releaser),
        kwargs={"org_id": org_id, "user_id": user_id},
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
    test_filename = "test.robot"
    test_filepath = str(get_artifact_store().run_dir(run_id, create=True) / test_filename)

    def _write_test_file():
        with open(test_filepath, "w", encoding="utf-8") as f:
            f.write(robot_code)

    try:
        await asyncio.to_thread(_write_test_file)
        logging.info(f"📝 Saved test code to {test_filepath}")
    except Exception as e:
        logging.error(f"Failed to save test code: {e}")
        _safe_evict_hint_metadata(run_id)
        await asyncio.to_thread(_set_run_status, run_id, "error")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': f'Failed to save test code: {str(e)}'})}\n\n"
        return

    try:
        # Phase 4: execution goes through the socket-holding executor; FastAPI
        # no longer touches Docker. ensure_image is required — if it raises
        # (executor unreachable / image build failed) the except below converts
        # it to an execution-error event, since there is no image to run on.
        await asyncio.to_thread(runner_exec_client.ensure_image)
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'running', 'message': 'Preparing execution environment...'})}\n\n"

        logging.info(f"🚀 Executing test: {test_filename}")
        result = await asyncio.to_thread(runner_exec_client.execute, run_id, test_filename)

        # History row: only passed/failed are real verdicts (from output.xml);
        # anything else means the run errored before producing one.
        _status = result.get("test_status")
        await asyncio.to_thread(
            _set_run_status, run_id, _status if _status in ("passed", "failed") else "error")

        yield f"data: {json.dumps({'stage': 'execution', **result})}\n\n"

        # The user now has their result. Release the workflow slot before the
        # background learning step so a Case B re-run's Trigger 1 LLM call
        # (~5-30s) does not hold concurrency capacity it has no need for.
        release_slot()

        # Inline screenshots BEFORE persist_run so the durable copy (local serve
        # or S3 upload) is self-contained for the CSP-sandboxed report route.
        # Wrapped: an inlining failure must never fail a successful run.
        try:
            n = await asyncio.to_thread(
                inline_report_screenshots, get_artifact_store().run_dir(run_id))
            logging.info("Inlined %d report screenshot reference(s) for %s", n, run_id)
        except Exception as e:
            logging.warning("Report screenshot inlining failed for %s: %s", run_id, e)

        # Artifacts are final — make them durable (no-op locally; S3 upload
        # otherwise). persist_run never raises, so a failed upload cannot turn
        # a successful run into an error.
        await asyncio.to_thread(get_artifact_store().persist_run, run_id)

        await asyncio.to_thread(_process_learning, run_id, user_query, robot_code, result)

    except Exception as e:
        logging.error(f"An error occurred during Docker execution: {e}")
        _safe_evict_hint_metadata(run_id)
        await asyncio.to_thread(_set_run_status, run_id, "error")
        yield f"data: {json.dumps({'stage': 'execution', 'status': 'error', 'message': str(e)})}\n\n"


async def stream_generate_only(
    user_query: str, model_provider: str, model_name: str, user: dict | None = None
) -> AsyncGenerator[str, None]:
    """
    Generates Robot Framework test code without executing it.
    Allows user to review and edit before execution.

    user: the authenticated requester (from require_user) — recorded on the
    test_runs history row so regular users can see their own runs.
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
        org_id = user.get("org_id") if user else None
        user_id = user.get("user_id") if user else None
        workflow_thread = _start_workflow_thread(q, user_query, model_provider, model_name, releaser, org_id=org_id, user_id=user_id)
        result_store: dict = {}
        try:
            async for sse in _drain_generation_queue(workflow_thread, q, result_store):
                yield sse
        except _GenerationError:
            return

        # History row: a generate-only run is terminal at 'generated' until the
        # user executes it (execute-test upserts the same id to 'running').
        wf_id = result_store.get("workflow_id")
        if wf_id:
            try:
                await asyncio.to_thread(
                    _record_run, str(uuid.UUID(wf_id)), user, user_query, status="generated",
                    robot_code=result_store.get("robot_code"))
            except ValueError:
                logging.warning("[RUN_REGISTRY] non-UUID workflow_id from generation; run not recorded")

        logging.info("✅ Test generation complete. Ready for user review.")
    finally:
        releaser.done()  # Generator's share of the latch


async def stream_execute_only(
    robot_code: str, user_query: str = None, workflow_id: str = None, user: dict | None = None,
    history_query: str | None = None, rerun_of: str | None = None
) -> AsyncGenerator[str, None]:
    """
    Executes provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.

    Args:
        robot_code: Robot Framework test code to execute
        user_query: Optional original user query for pattern learning
        workflow_id: Optional workflow ID from generation phase (for unified ID tracking)
        user: Authenticated requester (require_user) for the test_runs history row
        history_query: Description for the test_runs history row ONLY — used by
            history reruns, which re-execute stored code verbatim. Deliberately
            NOT passed to learning: a rerun has no generation step, so a pass
            would double-count the original (query -> code) evidence and a fail
            usually means site drift, not bad generation.
        rerun_of: The ORIGINAL run this re-run was cloned from (root-flattened
            by the endpoint). Stored on the history row so /api/feedback can
            route feedback to the run that owns the learning record.
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
            # A client-supplied id can reference another user's run. Never let
            # one user overwrite another's artifacts (robot_tests/{id}) or
            # history row: reuse is allowed only when the row is unowned/
            # unknown or owned by this requester — otherwise fork to a fresh id.
            owner = await asyncio.to_thread(_run_owner, run_id)
            if owner is not None and user is not None and owner != user.get("user_id"):
                logging.warning(
                    f"[RUN_REGISTRY] run {run_id} is owned by another user — forking to a fresh run id"
                )
                run_id = str(uuid.uuid4())
        else:
            run_id = str(uuid.uuid4())
        logging.info(f"🆔 Execution ID (unified): {run_id}")

        # History row. When this run_id came from a generation by another (or
        # the same) session, the upsert only advances status — the original
        # owner/query attribution is write-once.
        await asyncio.to_thread(
            _record_run, run_id, user, history_query or user_query, status="running",
            robot_code=robot_code, rerun_of=rerun_of)

        async for sse in _stream_docker_execution(run_id, robot_code, user_query, releaser.done):
            yield sse
    finally:
        # Guard against CancelledError (BaseException, not caught by except above):
        # if the task was cancelled between await points, _process_learning never
        # ran and its cache pop never fired. No-op if already consumed.
        if run_id is not None:
            _safe_evict_hint_metadata(run_id)
        releaser.done()


async def stream_generate_and_run(
    user_query: str, model_provider: str, model_name: str, user: dict | None = None
) -> AsyncGenerator[str, None]:
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
        org_id = user.get("org_id") if user else None
        user_id = user.get("user_id") if user else None
        workflow_thread = _start_workflow_thread(q, user_query, model_provider, model_name, releaser, org_id=org_id, user_id=user_id)

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

        # History row: generation is done and Docker execution starts now.
        await asyncio.to_thread(
            _record_run, run_id, user, user_query, status="running", robot_code=robot_code)

        async for sse in _stream_docker_execution(run_id, robot_code, user_query, releaser.done):
            yield sse
    finally:
        # Guard against CancelledError (BaseException, not caught by except above):
        # if the task was cancelled between await points, _process_learning never
        # ran and its cache pop never fired. No-op if already consumed.
        if run_id is not None:
            _safe_evict_hint_metadata(run_id)
        releaser.done()  # Generator's share of the latch
