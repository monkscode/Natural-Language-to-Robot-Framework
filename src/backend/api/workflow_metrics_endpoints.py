"""
API endpoints for browser-use workflow metrics monitoring.

Uses shared models from core.models to eliminate code duplication.
"""

from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query

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
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
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
    try:
        collector = get_workflow_metrics_collector()
        
        start_dt = _parse_date(start_date, "start_date")
        end_dt = _parse_date(end_date, "end_date")
        
        # Get metrics
        if start_dt or end_dt:
            metrics = collector.get_metrics_by_date_range(start_dt, end_dt)
            metrics = metrics[:limit]
        else:
            metrics = collector.get_all_metrics(limit=limit)
        
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
    last_days: Optional[int] = Query(None, ge=1, le=365, description="Get metrics for last N days (alternative to date range)")
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
    try:
        collector = get_workflow_metrics_collector()
        
        if last_days:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=last_days)
        else:
            start_dt = _parse_date(start_date, "start_date")
            end_dt = _parse_date(end_date, "end_date")
        
        aggregate = collector.get_aggregate_metrics(start_dt, end_dt)
        
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
async def get_metrics_summary():
    """
    Get a quick summary of recent workflow metrics.
    
    Returns metrics for:
    - Last 24 hours
    - Last 7 days
    - Last 30 days
    - All time
    """
    try:
        collector = get_workflow_metrics_collector()
        now = datetime.now()
        
        return {
            "last_24_hours": collector.get_aggregate_metrics(
                start_date=now - timedelta(hours=24),
                end_date=now
            ),
            "last_7_days": collector.get_aggregate_metrics(
                start_date=now - timedelta(days=7),
                end_date=now
            ),
            "last_30_days": collector.get_aggregate_metrics(
                start_date=now - timedelta(days=30),
                end_date=now
            ),
            "all_time": collector.get_aggregate_metrics(),
            "timestamp": now.isoformat()
        }
        
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
            "storage_path": str(collector.storage_path),
            "recent_workflows_count": len(recent_metrics),
            "last_recorded": recent_metrics[0].timestamp.isoformat() if recent_metrics else None
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metrics health: {str(e)}")
