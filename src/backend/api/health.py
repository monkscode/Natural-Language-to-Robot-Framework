"""Health check handlers for /health and /api/health.

Extracted into a standalone module so the same callables can be mounted
by main.py and imported by the test suite — preventing the test fixture
from drifting out of sync with the production handlers.

Referenced by:
    src/backend/main.py          — registers routes on the production app
    tests/test_api/test_api_endpoints.py — mounts same handlers in test fixture
Depends on:
    src/backend/core/config      — settings singleton
    src/backend/services/workflow_service — get_active_workflow_count()
"""

from src.backend.core.config import settings
from src.backend.services.workflow_service import get_active_workflow_count


def _pins() -> dict:
    """Comparability pins read by the bench preflight (bench/README.md)."""
    return {
        "optimization_enabled": settings.OPTIMIZATION_ENABLED,
        "model_provider": settings.MODEL_PROVIDER,
        "online_model": settings.ONLINE_MODEL,
        "dryrun_enabled": settings.DRYRUN_ENABLED,
    }


async def health_check():
    """Health check handler for Docker health monitoring (/health)."""
    active = get_active_workflow_count()
    max_wf = settings.MAX_CONCURRENT_WORKFLOWS
    return {
        "status": "healthy",
        "service": "nlrf-fastapi",
        "active_workflows": active,
        "max_workflows": max_wf,
        "available_slots": max(0, max_wf - active),
        "pins": _pins(),
    }


async def api_health_check():
    """Health check handler for the API gateway (/api/health)."""
    active = get_active_workflow_count()
    max_wf = settings.MAX_CONCURRENT_WORKFLOWS
    return {
        "status": "healthy",
        "service": "nlrf-api",
        "active_workflows": active,
        "max_workflows": max_wf,
        "available_slots": max(0, max_wf - active),
        "pins": _pins(),
    }
