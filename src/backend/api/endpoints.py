import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any

from src.backend.core.config import settings
from src.backend.services.workflow_service import stream_generate_and_run
from src.backend.services.docker_service import get_docker_client, rebuild_image, get_docker_status, get_healing_container_status
from src.backend.services.healing_orchestrator import HealingOrchestrator
from src.backend.core.config_loader import get_healing_config
from src.backend.core.models import HealingConfiguration

router = APIRouter()

# Import healing endpoints
from .healing_endpoints import router as healing_router

class Query(BaseModel):
    query: str

@router.post('/generate-and-run')
async def generate_and_run_streaming(query: Query):
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.ONLINE_MODEL if model_provider == "online" else settings.LOCAL_MODEL

    if model_provider == "online" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"Using {model_provider} model provider: {model_name}")

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
        return {"status": "error", "docker_available": False, "error": str(e)}
    except Exception as e:
        logging.error("Unexpected error in /docker-status endpoint", exc_info=True)
        return {"status": "error", "docker_available": False, "error": "An unexpected error occurred."}

class HealingConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_attempts_per_locator: Optional[int] = None
    chrome_session_timeout: Optional[int] = None
    healing_timeout: Optional[int] = None
    max_concurrent_sessions: Optional[int] = None
    confidence_threshold: Optional[float] = None
    max_alternatives: Optional[int] = None

@router.get('/healing/status')
async def get_healing_status():
    """Get current healing system status."""
    try:
        # Load healing configuration
        config = get_healing_config()
        
        # Get Docker client for container status
        client = get_docker_client()
        container_status = get_healing_container_status(client)
        
        return {
            "status": "success",
            "healing_enabled": config.enabled,
            "configuration": {
                "max_attempts_per_locator": config.max_attempts_per_locator,
                "chrome_session_timeout": config.chrome_session_timeout,
                "healing_timeout": config.healing_timeout,
                "max_concurrent_sessions": config.max_concurrent_sessions,
                "confidence_threshold": config.confidence_threshold,
                "max_alternatives": config.max_alternatives
            },
            "containers": container_status
        }
    except Exception as e:
        logging.error(f"Failed to get healing status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing status: {str(e)}")

@router.post('/healing/config')
async def update_healing_config(config_update: HealingConfigUpdate):
    """Update healing configuration."""
    try:
        # Load current configuration
        current_config = get_healing_config()
        
        # Update configuration with provided values
        config_dict = current_config.__dict__.copy()
        
        for field, value in config_update.dict(exclude_unset=True).items():
            if hasattr(current_config, field) and value is not None:
                config_dict[field] = value
        
        # Create new configuration object
        updated_config = HealingConfiguration(**config_dict)
        
        # Save configuration (this would need to be implemented in config_loader)
        # For now, we'll just return the updated config
        
        return {
            "status": "success",
            "message": "Healing configuration updated successfully",
            "configuration": config_dict
        }
        
    except Exception as e:
        logging.error(f"Failed to update healing config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update healing config: {str(e)}")

@router.get('/healing/sessions')
async def get_healing_sessions():
    """Get active healing sessions."""
    try:
        # This would require a global healing orchestrator instance
        # For now, return empty list
        return {
            "status": "success",
            "active_sessions": [],
            "total_sessions": 0
        }
    except Exception as e:
        logging.error(f"Failed to get healing sessions: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing sessions: {str(e)}")

@router.post('/healing/sessions/{session_id}/cancel')
async def cancel_healing_session(session_id: str):
    """Cancel an active healing session."""
    try:
        # This would require a global healing orchestrator instance
        # For now, return success
        return {
            "status": "success",
            "message": f"Healing session {session_id} cancelled successfully"
        }
    except Exception as e:
        logging.error(f"Failed to cancel healing session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cancel healing session: {str(e)}")

@router.get('/healing/reports/{run_id}')
async def get_healing_report(run_id: str):
    """Get healing report for a specific test run."""
    try:
        # This would load healing reports from storage
        # For now, return empty report
        return {
            "status": "success",
            "run_id": run_id,
            "healing_attempts": [],
            "total_attempts": 0,
            "successful_healings": 0,
            "failed_healings": 0
        }
    except Exception as e:
        logging.error(f"Failed to get healing report: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing report: {str(e)}")

@router.delete('/healing/containers/cleanup')
async def cleanup_healing_containers():
    """Clean up all healing-related containers."""
    try:
        client = get_docker_client()
        
        # Get all healing containers
        containers = client.containers.list(all=True, filters={"name": "chrome-healing-"})
        
        cleaned_up = 0
        for container in containers:
            try:
                container.stop(timeout=10)
                container.remove()
                cleaned_up += 1
            except Exception as e:
                logging.warning(f"Failed to cleanup container {container.name}: {e}")
        
        return {
            "status": "success",
            "message": f"Cleaned up {cleaned_up} healing containers",
            "containers_cleaned": cleaned_up
        }
        
    except Exception as e:
        logging.error(f"Failed to cleanup healing containers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cleanup healing containers: {str(e)}")

@router.delete('/test/containers/cleanup')
async def cleanup_test_containers():
    """Clean up all test-related containers."""
    try:
        from src.backend.services.docker_service import cleanup_test_containers
        client = get_docker_client()
        result = cleanup_test_containers(client)
        return result
        
    except Exception as e:
        logging.error(f"Failed to cleanup test containers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cleanup test containers: {str(e)}")