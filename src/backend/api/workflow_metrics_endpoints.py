"""
API endpoints for browser-use workflow metrics monitoring.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..core.workflow_metrics import get_workflow_metrics_collector, WorkflowMetrics


router = APIRouter(prefix="/workflow-metrics", tags=["workflow-metrics"])


class BrowserTokenBreakdown(BaseModel):
    """Shared model for browser-use token breakdown to avoid duplication."""
    browser_use_prompt_tokens: int = 0
    browser_use_completion_tokens: int = 0
    browser_use_cached_tokens: int = 0


class WorkflowMetricsResponse(BrowserTokenBreakdown):
    """Response model for individual workflow metrics with CrewAI and Browser-use breakdown."""
    workflow_id: str
    timestamp: str
    url: str
    
    # Overall metrics (totals)
    total_llm_calls: int
    total_cost: float
    execution_time: float
    
    # CrewAI breakdown
    crewai_llm_calls: int
    crewai_cost: float
    crewai_tokens: int
    crewai_prompt_tokens: int
    crewai_completion_tokens: int
    
    # Browser-use breakdown
    browser_use_llm_calls: int
    browser_use_cost: float  # Actual cost from browser-use (calculated from real token usage)
    browser_use_tokens: int
    # Token breakdown inherited from BrowserTokenBreakdown
    
    # Browser-use specific metrics
    total_elements: int
    successful_elements: int
    failed_elements: int
    success_rate: float
    avg_llm_calls_per_element: float
    avg_cost_per_element: float
    custom_actions_enabled: bool
    custom_action_usage_count: int
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


class RecordMetricsRequest(BrowserTokenBreakdown):
    """Request model for recording workflow metrics (backward compatible)."""
    workflow_id: str
    url: str
    
    # Overall metrics
    total_llm_calls: int
    total_cost: float
    execution_time: float
    
    # CrewAI breakdown (optional for backward compatibility)
    crewai_llm_calls: int = 0
    crewai_cost: float = 0.0
    crewai_tokens: int = 0
    crewai_prompt_tokens: int = 0
    crewai_completion_tokens: int = 0
    
    # Browser-use breakdown (optional for backward compatibility)
    browser_use_llm_calls: int = 0
    browser_use_cost: float = 0.0
    browser_use_tokens: int = 0
    # Token breakdown inherited from BrowserTokenBreakdown
    
    # Browser-use specific
    total_elements: int = 0
    successful_elements: int = 0
    failed_elements: int = 0
    success_rate: float = 0.0
    avg_llm_calls_per_element: float = 0.0
    avg_cost_per_element: float = 0.0
    custom_actions_enabled: bool = False
    custom_action_usage_count: int = 0
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
                url=m.url,
                
                # Totals
                total_llm_calls=m.total_llm_calls,
                total_cost=m.total_cost,
                execution_time=m.execution_time,
                
                # CrewAI breakdown
                crewai_llm_calls=m.crewai_llm_calls,
                crewai_cost=m.crewai_cost,
                crewai_tokens=m.crewai_tokens,
                crewai_prompt_tokens=m.crewai_prompt_tokens,
                crewai_completion_tokens=m.crewai_completion_tokens,
                
                # Browser-use breakdown
                browser_use_llm_calls=m.browser_use_llm_calls,
                browser_use_cost=m.browser_use_cost,
                browser_use_tokens=m.browser_use_tokens,
                browser_use_prompt_tokens=m.browser_use_prompt_tokens,
                browser_use_completion_tokens=m.browser_use_completion_tokens,
                browser_use_cached_tokens=m.browser_use_cached_tokens,
                
                # Browser-use specific
                total_elements=m.total_elements,
                successful_elements=m.successful_elements,
                failed_elements=m.failed_elements,
                success_rate=m.success_rate,
                avg_llm_calls_per_element=m.avg_llm_calls_per_element,
                avg_cost_per_element=m.avg_cost_per_element,
                custom_actions_enabled=m.custom_actions_enabled,
                custom_action_usage_count=m.custom_action_usage_count,
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
            url=request.url,
            
            # Totals
            total_llm_calls=request.total_llm_calls,
            total_cost=request.total_cost,
            execution_time=request.execution_time,
            
            # CrewAI breakdown
            crewai_llm_calls=request.crewai_llm_calls,
            crewai_cost=request.crewai_cost,
            crewai_tokens=request.crewai_tokens,
            crewai_prompt_tokens=request.crewai_prompt_tokens,
            crewai_completion_tokens=request.crewai_completion_tokens,
            
            # Browser-use breakdown
            browser_use_llm_calls=request.browser_use_llm_calls,
            browser_use_cost=request.browser_use_cost,
            browser_use_tokens=request.browser_use_tokens,
            
            # Browser-use specific
            total_elements=request.total_elements,
            successful_elements=request.successful_elements,
            failed_elements=request.failed_elements,
            success_rate=request.success_rate,
            avg_llm_calls_per_element=request.avg_llm_calls_per_element,
            avg_cost_per_element=request.avg_cost_per_element,
            custom_actions_enabled=request.custom_actions_enabled,
            custom_action_usage_count=request.custom_action_usage_count,
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
