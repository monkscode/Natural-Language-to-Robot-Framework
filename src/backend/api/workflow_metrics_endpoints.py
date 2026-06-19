"""
API endpoints for browser-use workflow metrics monitoring.

Uses shared models from core.models to eliminate code duplication.

Referenced by: main.py (router registration with prefix="/api")
Depends on: core/workflow_metrics.py, auth/jwt_utils.py, auth/ownership.py
"""

from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth.jwt_utils import is_validated_admin, require_user
from ..auth.ownership import is_dashboard_viewer
from ..core.models import (
    WorkflowMetrics,
    WorkflowMetricsResponse,
    RecordMetricsRequest,
    AggregateMetricsResponse,
)
from ..core.workflow_metrics import get_workflow_metrics_collector


router = APIRouter(prefix="/workflow-metrics", tags=["workflow-metrics"])


def _parse_date(date_str: Optional[str], param_name: str) -> Optional[datetime]:
    """Parse ISO format date string with error handling."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name} format: {date_str}. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
        ) from None


@router.get("/", response_model=List[WorkflowMetricsResponse])
async def get_workflow_metrics(
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of workflows to return"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"),
    user: dict | None = Depends(require_user),
):
    """
    Get workflow metrics with optional date filtering.

    Returns metrics for browser-use workflows including:
    - Total elements processed
    - Success rate
    - LLM call counts and costs
    - Custom action usage
    - Execution times
    """
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        collector = get_workflow_metrics_collector()

        start_dt = _parse_date(start_date, "start_date")
        end_dt = _parse_date(end_date, "end_date")

        # Get metrics — always pass scope_org so the query is org-filtered
        if start_dt or end_dt:
            metrics = collector.get_metrics_by_date_range(start_dt, end_dt, org_id=scope_org)
            metrics = metrics[:limit]
        else:
            metrics = collector.get_all_metrics(limit=limit, org_id=scope_org)

        # Convert to response model using the helper method
        return [WorkflowMetricsResponse.from_workflow_metrics(m) for m in metrics]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get workflow metrics: {str(e)}")


@router.get("/aggregate", response_model=AggregateMetricsResponse)
async def get_aggregate_metrics(
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"),
    last_days: Optional[int] = Query(None, ge=1, le=365, description="Get metrics for last N days (alternative to date range)"),
    user: dict | None = Depends(require_user),
):
    """
    Get aggregated workflow metrics for monitoring and analysis.

    Returns:
    - Total workflows executed
    - Total elements processed
    - Average success rate
    - Average LLM calls per element
    - Average cost per element
    - Custom action usage rate
    - Average execution time

    Use either date range (start_date/end_date) or last_days parameter.
    """
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        collector = get_workflow_metrics_collector()

        if last_days:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=last_days)
        else:
            start_dt = _parse_date(start_date, "start_date")
            end_dt = _parse_date(end_date, "end_date")

        aggregate = collector.get_aggregate_metrics(start_dt, end_dt, org_id=scope_org)

        return AggregateMetricsResponse(**aggregate)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get aggregate metrics: {str(e)}")


@router.post("/record")
async def record_workflow_metrics(request: RecordMetricsRequest):
    """
    Record metrics for a completed workflow.
    
    This endpoint is typically called by the browser-use service after
    completing a workflow execution.
    """
    try:
        collector = get_workflow_metrics_collector()
        
        # Convert request to WorkflowMetrics model
        # Use model_dump() to get all fields, then add timestamp
        request_data = request.model_dump()
        request_data['timestamp'] = datetime.now()
        
        metrics = WorkflowMetrics(**request_data)
        collector.record_workflow(metrics)
        
        return {
            "message": "Workflow metrics recorded successfully",
            "workflow_id": request.workflow_id
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to record workflow metrics: {str(e)}")


@router.get("/summary")
async def get_metrics_summary(
    user: dict | None = Depends(require_user),
):
    """
    Get a quick summary of recent workflow metrics.

    Returns metrics for:
    - Last 24 hours
    - Last 7 days
    - Last 30 days
    - All time
    """
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        collector = get_workflow_metrics_collector()
        now = datetime.now()

        return {
            "last_24_hours": collector.get_aggregate_metrics(
                start_date=now - timedelta(hours=24),
                end_date=now,
                org_id=scope_org,
            ),
            "last_7_days": collector.get_aggregate_metrics(
                start_date=now - timedelta(days=7),
                end_date=now,
                org_id=scope_org,
            ),
            "last_30_days": collector.get_aggregate_metrics(
                start_date=now - timedelta(days=30),
                end_date=now,
                org_id=scope_org,
            ),
            "all_time": collector.get_aggregate_metrics(org_id=scope_org),
            "timestamp": now.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metrics summary: {str(e)}")


@router.get("/health")
async def get_metrics_health():
    """
    Get health status of the workflow metrics system.
    """
    try:
        collector = get_workflow_metrics_collector()
        recent_metrics = collector.get_all_metrics(limit=10)

        return {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "storage": "postgres:workflow_metrics",
            "recent_workflows_count": len(recent_metrics),
            "last_recorded": recent_metrics[0].timestamp.isoformat() if recent_metrics else None
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metrics health: {str(e)}")


@router.get("/learning-health")
async def get_learning_health():
    """
    Get learning system health and effectiveness metrics.

    Returns status, total rules learned, hint injection rate, and
    natural comparison lift (Cat B pass rate - Cat A pass rate).
    """
    try:
        from src.backend.core.config import settings
        if not settings.OPTIMIZATION_ENABLED:
            return {
                "status": "disabled",
                "reason": "OPTIMIZATION_ENABLED=false",
                "total_rules": 0,
                "total_executions": 0,
                "effectiveness": None,
                "circuit_breaker": None,
            }

        from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
        feedback_loop = get_feedback_loop()
        if feedback_loop is None:
            return {
                "status": "unavailable",
                "reason": "Learning system failed to initialize",
                "total_rules": 0,
                "total_executions": 0,
                "effectiveness": None,
                "circuit_breaker": None,
            }

        stats = feedback_loop.get_learning_stats()
        cb = stats.get("circuit_breaker", {})

        structural_count = stats.get("structural_rules", {}).get("total_rules", 0)
        anti_pattern_count = stats.get("anti_patterns", {}).get("total_anti_patterns", 0)
        keyword_count = stats.get("keyword_corrections", {}).get("total_corrections", 0)

        return {
            "status": "active" if not cb.get("is_open", False) else "error",
            "total_rules": structural_count + anti_pattern_count + keyword_count,
            "total_executions": stats.get("total_records", 0),
            "effectiveness": stats.get("learning_effectiveness", {}),
            "circuit_breaker": cb,
            "structural_rules": structural_count,
            "anti_patterns": anti_pattern_count,
            "keyword_corrections": keyword_count,
            "contradictions": stats.get("contradictions", {}).get("total_flagged", 0),
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get learning health: {str(e)}"
        )
