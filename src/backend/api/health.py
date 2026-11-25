"""Health check handlers for /health and /api/health.

Extracted into a standalone module so the same callables can be mounted
by main.py and imported by the test suite — preventing the test fixture
from drifting out of sync with the production handlers.

Referenced by:
    src/backend/main.py          — registers routes on the production app
    tests/test_api/test_api_endpoints.py — mounts same handlers in test fixture
Depends on:
    src/backend/core/build_info  — the commit this image was built from
    src/backend/core/config      — settings singleton
    src/backend/services/workflow_service — get_active_workflow_count()
    litellm (get_model_cost_map_source_info) — which price list is in use (/health only)
"""

from src.backend.core.build_info import build_info
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


def _price_list() -> dict:
    """LiteLLM's price list in use: "live" (downloaded, src/backend/core/price_list.py) or "shipped" with litellm."""
    try:
        from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info
        info = get_model_cost_map_source_info()
        return {"source": {"remote": "live", "local": "shipped"}[info["source"]], "loaded_at": info["loaded_at"]}
    except Exception:  # noqa: BLE001 — never fail the health check over it
        return {"source": None, "loaded_at": None}


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
        "build": build_info(),
        "price_list": _price_list(),
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
        "build": build_info(),
    }
