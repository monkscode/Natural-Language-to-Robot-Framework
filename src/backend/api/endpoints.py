import asyncio
import base64
import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.backend.core.config import settings
from src.backend.services.workflow_service import stream_generate_and_run, stream_generate_only, stream_execute_only
from src.backend.services.docker_service import get_docker_client, rebuild_image, get_docker_status, cleanup_test_containers
from src.backend.api.history_endpoints import resolve_robot_code
from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
from src.backend.crew_ai.optimization.learning_config import MAX_FEEDBACK_TEXT_CHARS
from src.backend.crew_ai.llm_provider_routing import PROVIDER_PREFIXES
# require_user/require_admin enforce JWT (and the admin role) per route.
from src.backend.auth.jwt_utils import require_user, require_admin, is_validated_admin
from src.backend.auth.ownership import caller_can_read
from src.backend.core.run_registry import get_run_registry

router = APIRouter()

SSE_MEDIA_TYPE = "text/event-stream"

class Query(BaseModel):
    query: str

class ExecuteRequest(BaseModel):
    robot_code: Optional[str] = None  # The code to execute (omit when rerun_of is set)
    user_query: Optional[str] = None  # Optional: original user query for pattern learning
    workflow_id: Optional[str] = None  # Optional: workflow ID from generation for unified tracking
    rerun_of: Optional[str] = None  # Optional: run id whose STORED code to re-execute (learning skipped)

@router.post('/generate-test')
async def generate_test_only(query: Query, user: dict | None = Depends(require_user)):
    """
    Generate Robot Framework test code without executing it.
    Allows user to review and edit before execution.

    require_user enforces the JWT exactly as before; it is a parameter (not a
    route dependency) so the requester can be recorded on the run's history row.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.LOCAL_MODEL if model_provider == "local" else settings.ONLINE_MODEL

    if model_provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE ONLY] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_only(user_query, model_provider, model_name, user=user), media_type=SSE_MEDIA_TYPE)

def _rerun_from_history(source_run_id: str, user: dict | None) -> StreamingResponse:
    """Re-execute a history run's STORED code as a fresh run (History's
    "Run again"): no LLM, no regeneration cost, new run id owned by the
    requester.

    Learning is deliberately skipped (user_query=None): the original execution
    already recorded the (query -> code) evidence, so a passing rerun would
    double-count it and a failing rerun usually means site drift, not bad
    generation. The source's query still lands on the new history row via
    history_query so the run is recognizable in the list.

    Access mirrors the detail endpoint: owner or validated admin; unknown ids
    and other users' runs both 404.
    """
    try:
        source_run_id = str(uuid.UUID(source_run_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid rerun_of: must be a UUID")

    source = get_run_registry().get_run(source_run_id)
    admin = is_validated_admin(user)
    allowed = source is not None and caller_can_read(
        user, source.get("user_id"), source.get("org_id"), is_platform_admin=admin
    )
    if source is None or not allowed:
        # 404, not 403 — don't leak run existence across orgs.
        raise HTTPException(status_code=404, detail="Run not found")

    robot_code = resolve_robot_code(source)
    if not robot_code:
        raise HTTPException(
            status_code=409,
            detail="No stored code for this run — it predates code persistence. Use Regenerate instead.",
        )

    # Lineage anchor, root-flattened: re-running a re-run still points at the
    # ORIGINAL run, so /api/feedback always resolves to the row that owns the
    # learning record in one lookup (no chain walking).
    learning_anchor = source.get("rerun_of") or source_run_id

    logging.info(f"[RERUN] Re-executing stored code of run {source_run_id} as a new run")
    return StreamingResponse(
        stream_execute_only(robot_code, user=user,
                            history_query=source.get("user_query"),
                            rerun_of=learning_anchor),
        media_type=SSE_MEDIA_TYPE,
    )


@router.post('/execute-test')
async def execute_test_only(request: ExecuteRequest, user: dict | None = Depends(require_user)):
    """
    Execute provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.

    Optional: Pass user_query for pattern learning from successful executions.
    Optional: Pass workflow_id for unified ID tracking (same ID for metrics and files).
    Optional: Pass rerun_of (instead of robot_code) to re-execute a history
    run's stored code as a new run — learning is skipped by design.
    """
    if request.rerun_of:
        # Threaded: source-run lookup, admin re-validation and the stored-code
        # disk fallback all block; the StreamingResponse it returns only wraps
        # the (not yet started) async generator, so building it off-loop is safe.
        return await asyncio.to_thread(_rerun_from_history, request.rerun_of, user)

    robot_code = request.robot_code
    user_query = request.user_query  # Optional: for pattern learning
    workflow_id = request.workflow_id  # Optional: for unified ID tracking

    if not robot_code or not robot_code.strip():
        raise HTTPException(status_code=400, detail="Robot code not provided")

    logging.info(f"[EXECUTE ONLY] Executing user-provided test code ({len(robot_code)} characters)")
    if workflow_id:
        safe_workflow_id = (
            workflow_id if re.match(r'^[a-zA-Z0-9_-]+$', workflow_id)
            else "[b64]" + base64.b64encode(workflow_id.encode('UTF-8')).decode()
        )
        logging.info("[EXECUTE ONLY] 🆔 Using unified workflow_id: %s", safe_workflow_id)
    if user_query:
        safe_user_query = user_query.replace("\n", " ").replace("\r", "")
        logging.info(f"[EXECUTE ONLY] ✅ User query provided for pattern learning: {safe_user_query[:50]}...")
    else:
        logging.warning("[EXECUTE ONLY] ⚠️ No user query provided - pattern learning will be skipped")

    return StreamingResponse(stream_execute_only(robot_code, user_query, workflow_id, user=user), media_type=SSE_MEDIA_TYPE)

@router.post('/generate-and-run')
async def generate_and_run_streaming(query: Query, user: dict | None = Depends(require_user)):
    """
    Legacy endpoint: Generate and execute test in one flow.
    Kept for backward compatibility.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.LOCAL_MODEL if model_provider == "local" else settings.ONLINE_MODEL

    if model_provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE AND RUN] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_and_run(user_query, model_provider, model_name, user=user), media_type=SSE_MEDIA_TYPE)

@router.post('/rebuild-docker-image', dependencies=[Depends(require_admin)])
async def rebuild_docker_image_endpoint():
    try:
        client = get_docker_client()
        result = rebuild_image(client)
        return result
    except ConnectionError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logging.error(f"Unexpected error during Docker image rebuild: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

@router.get('/docker-status', dependencies=[Depends(require_user)])
async def docker_status_endpoint():
    try:
        client = get_docker_client()
        status = get_docker_status(client)
        return status
    except ConnectionError as e:
        logging.error(f"Docker connection error: {e}")
        return {"status": "error", "docker_available": False, "error": "Docker is unavailable."}
    except Exception as e:
        logging.error("Unexpected error in /docker-status endpoint", exc_info=True)
        return {"status": "error", "docker_available": False, "error": "An unexpected error occurred."}

@router.delete('/test/containers/cleanup', dependencies=[Depends(require_admin)])
async def cleanup_test_containers_endpoint():
    """
    Clean up all test-related containers.
    
    Note: This endpoint uses the docker_service.cleanup_test_containers() function
    which specifically targets "robot-test-*" containers. There is also a standalone
    CLI tool (tools/cleanup_docker_containers.py) that provides more comprehensive
    cleanup including test-runner-* containers. Both are kept as they serve 
    different purposes: API endpoint for programmatic cleanup vs manual CLI tool 
    for comprehensive maintenance.
    """
    try:
        client = get_docker_client()
        result = cleanup_test_containers(client)
        return result
        
    except Exception as e:
        logging.error(f"Failed to cleanup test containers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cleanup test containers: {str(e)}")


# ---------------------------------------------------------------------------
# Learning System Endpoints (DAY_08)
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    workflow_id: str
    feedback_text: str = ""
    feedback_type: str  # "close_enough" | "completely_wrong"


@router.post('/api/feedback')
async def submit_feedback(request: FeedbackRequest, user: dict | None = Depends(require_user)):
    """
    Submit user feedback on test execution results.

    Triages feedback via NL seed patterns and routes to
    learning engines. Returns triage result for frontend display.

    Returns 200 with status="disabled" when learning system is off.
    """
    # One PK lookup serves both the ownership gate and the re-run redirect.
    # Threaded: the registry query (and its first-use pool init) blocks.
    try:
        run_row = await asyncio.to_thread(
            lambda: get_run_registry().get_run(request.workflow_id))
    except Exception:
        run_row = None  # registry unavailable — fail closed for non-admins

    # Authorization: feedback mutates the learning store (hints, Case B
    # credits), so only the run's owner — or a validated admin — may submit
    # it. `user` is None only when AUTH_ENFORCED is off (local debugging).
    # Unattributed/unknown runs are admin-only (fail closed).
    admin = await asyncio.to_thread(is_validated_admin, user)
    owner_id = run_row.get("user_id") if run_row else None
    org_id = run_row.get("org_id") if run_row else None
    if not caller_can_read(user, owner_id, org_id, is_platform_admin=admin):
        raise HTTPException(
            status_code=403,
            detail="You cannot submit feedback for this run",
        )

    # Re-run rows never own a learning record (their execution deliberately
    # skipped learning), so feedback applies to the ORIGINAL run the code was
    # cloned from. rerun_of is root-flattened at creation and was written
    # server-side at rerun time, when the requester was already authorized
    # against that original — no second ownership check needed.
    feedback_target_id = request.workflow_id
    if run_row and run_row.get("rerun_of"):
        feedback_target_id = run_row["rerun_of"]
        logging.info(
            f"[FEEDBACK] {request.workflow_id} is a re-run — applying feedback "
            f"to its original run {feedback_target_id}"
        )

    feedback_loop = get_feedback_loop()
    if not feedback_loop:
        return {
            "status": "disabled",
            "message": "Learning system is not enabled",
        }

    # Validate feedback_type
    if request.feedback_type not in ("close_enough", "completely_wrong"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid feedback_type: {request.feedback_type}. "
                   f"Must be 'close_enough' or 'completely_wrong'.",
        )

    if request.feedback_text and len(request.feedback_text) > MAX_FEEDBACK_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Feedback must be {MAX_FEEDBACK_TEXT_CHARS} characters or fewer",
        )
    text = request.feedback_text or ""

    try:
        # process_user_feedback runs a blocking conflict-detection LLM call
        # (up to 30s). Offload it so this async handler does not freeze the
        # event loop for every other request while it waits.
        triage = await asyncio.to_thread(
            feedback_loop.process_user_feedback,
            feedback_target_id, text, request.feedback_type,
        )
        return {"status": "success", "triage": triage, "applied_to": feedback_target_id}
    except Exception as e:
        logging.error(f"[FEEDBACK] Error processing feedback: {e}")
        return {
            "status": "error",
            "message": "Feedback received but triage failed",
        }


@router.get('/api/learning-stats', dependencies=[Depends(require_admin)])
async def get_learning_stats():
    """
    Return comprehensive learning system statistics.

    Aggregates stats from all engines, metrics tracker,
    and circuit breaker into a single snapshot.

    Returns 200 with status="disabled" when learning system is off.
    """
    feedback_loop = get_feedback_loop()
    if not feedback_loop:
        return {
            "status": "disabled",
            "message": "Learning system is not enabled",
        }

    try:
        stats = feedback_loop.get_learning_stats()
        return {"status": "success", "stats": stats}
    except Exception as e:
        logging.error(f"[LEARNING-STATS] Error fetching stats: {e}")
        return {
            "status": "error",
            "message": "Failed to retrieve learning stats",
        }
