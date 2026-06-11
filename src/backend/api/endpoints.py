import asyncio
import base64
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.backend.core.config import settings
from src.backend.services.workflow_service import stream_generate_and_run, stream_generate_only, stream_execute_only
from src.backend.services.docker_service import get_docker_client, rebuild_image, get_docker_status, cleanup_test_containers
from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
from src.backend.crew_ai.optimization.learning_config import MAX_FEEDBACK_TEXT_CHARS
from src.backend.crew_ai.llm_provider_routing import PROVIDER_PREFIXES
# require_user/require_admin enforce JWT (and the admin role) per route.
from src.backend.auth.jwt_utils import require_user, require_admin

router = APIRouter()

class Query(BaseModel):
    query: str

class ExecuteRequest(BaseModel):
    robot_code: str
    user_query: Optional[str] = None  # Optional: original user query for pattern learning
    workflow_id: Optional[str] = None  # Optional: workflow ID from generation for unified tracking

@router.post('/generate-test', dependencies=[Depends(require_user)])
async def generate_test_only(query: Query):
    """
    Generate Robot Framework test code without executing it.
    Allows user to review and edit before execution.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.LOCAL_MODEL if model_provider == "local" else settings.ONLINE_MODEL

    if model_provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE ONLY] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_only(user_query, model_provider, model_name), media_type="text/event-stream")

@router.post('/execute-test', dependencies=[Depends(require_user)])
async def execute_test_only(request: ExecuteRequest):
    """
    Execute provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.
    
    Optional: Pass user_query for pattern learning from successful executions.
    Optional: Pass workflow_id for unified ID tracking (same ID for metrics and files).
    """
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

    return StreamingResponse(stream_execute_only(robot_code, user_query, workflow_id), media_type="text/event-stream")

@router.post('/generate-and-run', dependencies=[Depends(require_user)])
async def generate_and_run_streaming(query: Query):
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

    return StreamingResponse(stream_generate_and_run(user_query, model_provider, model_name), media_type="text/event-stream")

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


@router.post('/api/feedback', dependencies=[Depends(require_user)])
async def submit_feedback(request: FeedbackRequest):
    """
    Submit user feedback on test execution results.

    Triages feedback via NL seed patterns and routes to
    learning engines. Returns triage result for frontend display.

    Returns 200 with status="disabled" when learning system is off.
    """
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
            request.workflow_id, text, request.feedback_type,
        )
        return {"status": "success", "triage": triage}
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
