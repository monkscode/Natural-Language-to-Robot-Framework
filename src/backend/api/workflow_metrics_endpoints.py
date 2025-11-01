"""
API endpoints for browser-use workflow metrics monitoring.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..core.workflow_metrics import get_workflow_metrics_collector, WorkflowMetrics


router = APIRouter(prefix="/workflow-metrics", tags=["workflow-metrics"])


class WorkflowMetricsResponse(BaseModel):
    """Response model for individual workflow metrics."""
    workflow_id: str
    timestamp: str
    total_elements: int
    successful_elements: int
    failed_elements: int
    success_rate: float
    total_llm_calls: int
    avg_llm_calls_per_element: float
    total_cost: float
    avg_cost_per_element: float
    custom_actions_enabled: bool
    custom_action_usage_count: int
    execution_time: float
    url: str
    session_id: Optional[str] = None


class AggregateMetricsResponse(BaseModel):
    """Response model for aggregated metrics."""
    total_workflows: int
    total_elements: int
    successful_elements: int
    failed_elements: int
    avg_success_rate: float
    total_llm_calls: int
    avg_llm_calls_per_element: float
    total_cost: float
    avg_cost_per_element: float
    custom_action_usage_rate: float
    avg_execution_time: float
    date_range: Dict[str, Optional[str]]


class RecordMetricsRequest(BaseModel):
    """Request model for recording workflow metrics."""
    workflow_id: str
    total_elements: int
    successful_elements: int
    failed_elements: int
    success_rate: float
    total_llm_calls: int
    avg_llm_calls_per_element: float
    total_cost: float
    avg_cost_per_element: float
    custom_actions_enabled: bool
    custom_action_usage_count: int
    execution_time: float
    url: str
    session_id: Optional[str] = None


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
        
        # Parse dates if provided
        start_dt = None
        end_dt = None
        
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid start_date format: {start_date}. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
        
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid end_date format: {end_date}. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
        
        # Get metrics
        if start_dt or end_dt:
            metrics = collector.get_metrics_by_date_range(start_dt, end_dt)
            # Apply limit after filtering
            metrics = metrics[:limit]
        else:
            metrics = collector.get_all_metrics(limit=limit)
        
        # Convert to response model
        return [
            WorkflowMetricsResponse(
                workflow_id=m.workflow_id,
                timestamp=m.timestamp.isoformat(),
                total_elements=m.total_elements,
                successful_elements=m.successful_elements,
                failed_elements=m.failed_elements,
                success_rate=m.success_rate,
                total_llm_calls=m.total_llm_calls,
                avg_llm_calls_per_element=m.avg_llm_calls_per_element,
                total_cost=m.total_cost,
                avg_cost_per_element=m.avg_cost_per_element,
                custom_actions_enabled=m.custom_actions_enabled,
                custom_action_usage_count=m.custom_action_usage_count,
                execution_time=m.execution_time,
                url=m.url,
                session_id=m.session_id
            )
            for m in metrics
        ]
        
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
        
        # Parse dates
        start_dt = None
        end_dt = None
        
        if last_days:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=last_days)
        else:
            if start_date:
                try:
                    start_dt = datetime.fromisoformat(start_date)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid start_date format: {start_date}. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
            
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid end_date format: {end_date}. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
        
        # Get aggregated metrics
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
        
        metrics = WorkflowMetrics(
            workflow_id=request.workflow_id,
            timestamp=datetime.now(),
            total_elements=request.total_elements,
            successful_elements=request.successful_elements,
            failed_elements=request.failed_elements,
            success_rate=request.success_rate,
            total_llm_calls=request.total_llm_calls,
            avg_llm_calls_per_element=request.avg_llm_calls_per_element,
            total_cost=request.total_cost,
            avg_cost_per_element=request.avg_cost_per_element,
            custom_actions_enabled=request.custom_actions_enabled,
            custom_action_usage_count=request.custom_action_usage_count,
            execution_time=request.execution_time,
            url=request.url,
            session_id=request.session_id
        )
        
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
        
        # Get metrics for different time periods
        last_24h = collector.get_aggregate_metrics(
            start_date=now - timedelta(hours=24),
            end_date=now
        )
        
        last_7d = collector.get_aggregate_metrics(
            start_date=now - timedelta(days=7),
            end_date=now
        )
        
        last_30d = collector.get_aggregate_metrics(
            start_date=now - timedelta(days=30),
            end_date=now
        )
        
        all_time = collector.get_aggregate_metrics()
        
        return {
            "last_24_hours": last_24h,
            "last_7_days": last_7d,
            "last_30_days": last_30d,
            "all_time": all_time,
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
