import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.backend.core.config import settings
from src.backend.services.workflow_service import stream_generate_and_run, stream_generate_only, stream_execute_only
from src.backend.services.docker_service import get_docker_client, rebuild_image, get_docker_status, cleanup_test_containers

router = APIRouter()

class Query(BaseModel):
    query: str

class ExecuteRequest(BaseModel):
    robot_code: str
    user_query: Optional[str] = None  # Optional: original user query for pattern learning

@router.post('/generate-test')
async def generate_test_only(query: Query):
    """
    Generate Robot Framework test code without executing it.
    Allows user to review and edit before execution.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.ONLINE_MODEL if model_provider == "online" else settings.LOCAL_MODEL

    if model_provider == "online" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE ONLY] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_only(user_query, model_provider, model_name), media_type="text/event-stream")

@router.post('/execute-test')
async def execute_test_only(request: ExecuteRequest):
    """
    Execute provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.
    
    Optional: Pass user_query for pattern learning from successful executions.
    """
    robot_code = request.robot_code
    user_query = request.user_query  # Optional: for pattern learning
    
    if not robot_code or not robot_code.strip():
        raise HTTPException(status_code=400, detail="Robot code not provided")

    logging.info(f"[EXECUTE ONLY] Executing user-provided test code ({len(robot_code)} characters)")
    if user_query:
        logging.info(f"[EXECUTE ONLY] ✅ User query provided for pattern learning: {user_query[:50]}...")
    else:
        logging.warning("[EXECUTE ONLY] ⚠️ No user query provided - pattern learning will be skipped")

    return StreamingResponse(stream_execute_only(robot_code, user_query), media_type="text/event-stream")

@router.post('/generate-and-run')
async def generate_and_run_streaming(query: Query):
    """
    Legacy endpoint: Generate and execute test in one flow.
    Kept for backward compatibility.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.ONLINE_MODEL if model_provider == "online" else settings.LOCAL_MODEL

    if model_provider == "online" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE AND RUN] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_and_run(user_query, model_provider, model_name), media_type="text/event-stream")

@router.post('/rebuild-docker-image')
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

@router.get('/docker-status')
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

@router.delete('/test/containers/cleanup')
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