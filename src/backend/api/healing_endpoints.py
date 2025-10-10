"""
Healing API endpoints for the Test Self-Healing system.

This module provides REST endpoints for healing status, reports, configuration,
and Server-Sent Events for real-time progress updates.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..core.config_loader import get_healing_config, save_healing_config
from ..core.models import HealingConfiguration, HealingStatus, FailureContext
from ..services.healing_orchestrator import HealingOrchestrator
from ..core.config import settings
from ..services.docker_service import get_docker_client, get_healing_container_status
from .auth import get_current_user, rate_limit_healing

logger = logging.getLogger(__name__)

# Global healing orchestrator instance
_healing_orchestrator: Optional[HealingOrchestrator] = None

# In-memory storage for healing reports and statistics
# In production, this would be replaced with persistent storage
healing_reports: Dict[str, Dict[str, Any]] = {}
healing_statistics: Dict[str, Any] = {
    "total_attempts": 0,
    "successful_healings": 0,
    "failed_healings": 0,
    "average_healing_time": 0.0,
    "success_rate": 0.0,
    "last_updated": datetime.now()
}

# Store healing sessions for dashboard access
healing_session_history: Dict[str, Dict[str, Any]] = {}

def store_healing_session(session):
    """Store healing session for dashboard access."""
    try:
        healing_session_history[session.session_id] = {
            "session_id": session.session_id,
            "status": session.status.value,
            "test_file": session.failure_context.test_file,
            "test_case": session.failure_context.test_case,
            "failing_step": session.failure_context.failing_step,
            "original_locator": session.failure_context.original_locator,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "progress": session.progress,
            "current_phase": session.current_phase,
            "attempts_count": len(session.attempts),
            "error_message": session.error_message
        }
        logger.info(f"📊 HEALING API: Stored session {session.session_id} in history")
    except Exception as e:
        logger.error(f"❌ HEALING API: Failed to store session: {e}")

# SSE connections for real-time updates
sse_connections: Dict[str, List[Any]] = {}

router = APIRouter(prefix="/healing", tags=["healing"])

# Pydantic models for API requests/responses
class HealingConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_attempts_per_locator: Optional[int] = Field(None, ge=1, le=10)
    chrome_session_timeout: Optional[int] = Field(None, ge=10, le=300)
    healing_timeout: Optional[int] = Field(None, ge=60, le=1800)
    max_concurrent_sessions: Optional[int] = Field(None, ge=1, le=10)
    confidence_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_alternatives: Optional[int] = Field(None, ge=1, le=20)

class HealingSessionResponse(BaseModel):
    session_id: str
    status: str
    test_file: str
    test_case: str
    failing_step: str
    original_locator: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    progress: float = 0.0
    current_phase: str = "pending"
    attempts_count: int = 0
    error_message: Optional[str] = None

class HealingReportResponse(BaseModel):
    run_id: str
    test_file: str
    healing_attempts: List[Dict[str, Any]]
    total_attempts: int
    successful_healings: int
    failed_healings: int
    total_time: float
    generated_at: datetime

class HealingStatisticsResponse(BaseModel):
    total_attempts: int
    successful_healings: int
    failed_healings: int
    success_rate: float
    average_healing_time: float
    last_24h_attempts: int
    last_24h_success_rate: float
    top_failure_types: List[Dict[str, Any]]
    healing_trends: List[Dict[str, Any]]

async def get_healing_orchestrator() -> HealingOrchestrator:
    """Get or create the global healing orchestrator instance."""
    global _healing_orchestrator
    
    if _healing_orchestrator is None:
        config = get_healing_config()
        # Get model from settings based on provider
        model_provider = settings.MODEL_PROVIDER
        model_name = settings.ONLINE_MODEL if model_provider == "online" else settings.LOCAL_MODEL
        
        _healing_orchestrator = HealingOrchestrator(
            config, 
            model_provider=model_provider,
            model_name=model_name
        )
        await _healing_orchestrator.start()
        logger.info(f"🚀 Healing orchestrator initialized with {model_provider}/{model_name}")
    
    return _healing_orchestrator

@router.get("/status")
async def get_healing_status(user: dict = Depends(get_current_user)):
    """Get current healing system status and configuration."""
    try:
        config = get_healing_config()
        orchestrator = await get_healing_orchestrator()
        
        # Get Docker container status
        try:
            client = get_docker_client()
            container_status = get_healing_container_status(client)
        except Exception as e:
            logger.warning(f"Failed to get container status: {e}")
            container_status = {"error": str(e)}
        
        # Get active sessions count
        active_sessions = len([
            session for session in orchestrator.active_sessions.values()
            if session.status == HealingStatus.IN_PROGRESS
        ])
        
        return {
            "status": "success",
            "healing_enabled": config.enabled,
            "active_sessions": active_sessions,
            "configuration": {
                "max_attempts_per_locator": config.max_attempts_per_locator,
                "chrome_session_timeout": config.chrome_session_timeout,
                "healing_timeout": config.healing_timeout,
                "max_concurrent_sessions": config.max_concurrent_sessions,
                "confidence_threshold": config.confidence_threshold,
                "max_alternatives": config.max_alternatives
            },
            "containers": container_status,
            "statistics": healing_statistics
        }
    except Exception as e:
        logger.error(f"Failed to get healing status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing status: {str(e)}")

@router.post("/config")
async def update_healing_config(
    config_update: HealingConfigUpdate,
    user: dict = Depends(get_current_user)
):
    """Update healing configuration settings."""
    try:
        current_config = get_healing_config()
        
        # Update configuration with provided values
        config_dict = current_config.__dict__.copy()
        
        for field, value in config_update.dict(exclude_unset=True).items():
            if hasattr(current_config, field) and value is not None:
                config_dict[field] = value
        
        # Create and save new configuration
        updated_config = HealingConfiguration(**config_dict)
        save_healing_config(updated_config)
        
        # Update global orchestrator if it exists
        global _healing_orchestrator
        if _healing_orchestrator:
            _healing_orchestrator.config = updated_config
        
        logger.info(f"Healing configuration updated by user {user.get('username', 'unknown')}")
        
        return {
            "status": "success",
            "message": "Healing configuration updated successfully",
            "configuration": config_dict
        }
        
    except Exception as e:
        logger.error(f"Failed to update healing config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update healing config: {str(e)}")

@router.get("/sessions")
async def get_healing_sessions(
    status: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(get_current_user)
):
    """Get healing sessions with optional status filtering."""
    try:
        logger.info(f"🔍 HEALING API: Getting healing sessions (status={status}, limit={limit})")
        
        orchestrator = await get_healing_orchestrator()
        
        # Get sessions from active sessions
        sessions = list(orchestrator.active_sessions.values())
        
        logger.info(f"📊 HEALING API: Found {len(sessions)} active sessions")
        
        # Filter by status if provided
        if status:
            try:
                status_enum = HealingStatus(status.upper())
                sessions = [s for s in sessions if s.status == status_enum]
                logger.info(f"📊 HEALING API: Filtered to {len(sessions)} sessions with status {status}")
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        
        # Sort by start time (newest first) and limit
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        sessions = sessions[:limit]
        
        # Convert to response format
        session_responses = []
        for session in sessions:
            logger.info(f"📋 HEALING API: Processing session {session.session_id} - {session.status.value}")
            session_responses.append(HealingSessionResponse(
                session_id=session.session_id,
                status=session.status.value,
                test_file=session.failure_context.test_file,
                test_case=session.failure_context.test_case,
                failing_step=session.failure_context.failing_step,
                original_locator=session.failure_context.original_locator,
                started_at=session.started_at,
                completed_at=session.completed_at,
                progress=session.progress,
                current_phase=session.current_phase,
                attempts_count=len(session.attempts),
                error_message=session.error_message
            ))
        
        logger.info(f"✅ HEALING API: Returning {len(session_responses)} session responses")
        
        return {
            "status": "success",
            "sessions": session_responses,
            "total_sessions": len(orchestrator.active_sessions)
        }
        
    except Exception as e:
        logger.error(f"Failed to get healing sessions: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing sessions: {str(e)}")

@router.get("/sessions/{session_id}")
async def get_healing_session(
    session_id: str,
    user: dict = Depends(get_current_user)
):
    """Get detailed information about a specific healing session."""
    try:
        orchestrator = await get_healing_orchestrator()
        session = await orchestrator.get_session_status(session_id)
        
        if not session:
            raise HTTPException(status_code=404, detail=f"Healing session {session_id} not found")
        
        return {
            "status": "success",
            "session": HealingSessionResponse(
                session_id=session.session_id,
                status=session.status.value,
                test_file=session.failure_context.test_file,
                test_case=session.failure_context.test_case,
                failing_step=session.failure_context.failing_step,
                original_locator=session.failure_context.original_locator,
                started_at=session.started_at,
                completed_at=session.completed_at,
                progress=session.progress,
                current_phase=session.current_phase,
                attempts_count=len(session.attempts),
                error_message=session.error_message
            ),
            "attempts": [
                {
                    "locator": attempt.locator,
                    "strategy": attempt.strategy.value,
                    "success": attempt.success,
                    "confidence_score": attempt.confidence_score,
                    "error_message": attempt.error_message,
                    "timestamp": attempt.timestamp
                }
                for attempt in session.attempts
            ]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get healing session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing session: {str(e)}")

@router.post("/sessions/{session_id}/cancel")
async def cancel_healing_session(
    session_id: str,
    user: dict = Depends(get_current_user)
):
    """Cancel an active healing session."""
    try:
        orchestrator = await get_healing_orchestrator()
        success = await orchestrator.cancel_session(session_id)
        
        if not success:
            raise HTTPException(
                status_code=404, 
                detail=f"Healing session {session_id} not found or already completed"
            )
        
        logger.info(f"Healing session {session_id} cancelled by user {user.get('username', 'unknown')}")
        
        return {
            "status": "success",
            "message": f"Healing session {session_id} cancelled successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel healing session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cancel healing session: {str(e)}")

@router.get("/reports/{run_id}")
async def get_healing_report(
    run_id: str,
    user: dict = Depends(get_current_user)
):
    """Get healing report for a specific test run."""
    try:
        report = healing_reports.get(run_id)
        
        if not report:
            # Try to generate report from active/completed sessions
            orchestrator = await get_healing_orchestrator()
            matching_sessions = [
                session for session in orchestrator.active_sessions.values()
                if session.failure_context.run_id == run_id
            ]
            
            if not matching_sessions:
                raise HTTPException(status_code=404, detail=f"Healing report for run {run_id} not found")
            
            # Generate report from sessions
            report = _generate_report_from_sessions(run_id, matching_sessions)
            healing_reports[run_id] = report
        
        return {
            "status": "success",
            "report": HealingReportResponse(**report)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get healing report for run {run_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing report: {str(e)}")

@router.get("/reports")
async def get_healing_reports(
    limit: int = 20,
    days: int = 7,
    user: dict = Depends(get_current_user)
):
    """Get list of healing reports within the specified time range."""
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        
        # Filter reports by date and limit
        recent_reports = []
        for run_id, report in healing_reports.items():
            if report["generated_at"] >= cutoff_date:
                recent_reports.append({
                    "run_id": run_id,
                    "test_file": report["test_file"],
                    "total_attempts": report["total_attempts"],
                    "successful_healings": report["successful_healings"],
                    "failed_healings": report["failed_healings"],
                    "generated_at": report["generated_at"]
                })
        
        # Sort by generation date (newest first) and limit
        recent_reports.sort(key=lambda r: r["generated_at"], reverse=True)
        recent_reports = recent_reports[:limit]
        
        return {
            "status": "success",
            "reports": recent_reports,
            "total_reports": len(healing_reports)
        }
        
    except Exception as e:
        logger.error(f"Failed to get healing reports: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing reports: {str(e)}")

@router.get("/statistics")
async def get_healing_statistics(
    days: int = 30,
    user: dict = Depends(get_current_user)
):
    """Get healing statistics and trends."""
    try:
        # Calculate statistics from reports and sessions
        cutoff_date = datetime.now() - timedelta(days=days)
        
        # Get recent sessions for trend analysis
        orchestrator = await get_healing_orchestrator()
        recent_sessions = [
            session for session in orchestrator.active_sessions.values()
            if session.started_at >= cutoff_date
        ]
        
        # Calculate 24h statistics
        last_24h_cutoff = datetime.now() - timedelta(hours=24)
        last_24h_sessions = [
            session for session in recent_sessions
            if session.started_at >= last_24h_cutoff
        ]
        
        last_24h_attempts = len(last_24h_sessions)
        last_24h_successful = len([
            s for s in last_24h_sessions 
            if s.status == HealingStatus.SUCCESS
        ])
        last_24h_success_rate = (
            last_24h_successful / last_24h_attempts * 100 
            if last_24h_attempts > 0 else 0.0
        )
        
        # Generate failure type statistics
        failure_types = {}
        for session in recent_sessions:
            failure_type = getattr(session.failure_context, 'failure_type', 'unknown')
            failure_types[failure_type] = failure_types.get(failure_type, 0) + 1
        
        top_failure_types = [
            {"type": ftype, "count": count}
            for ftype, count in sorted(failure_types.items(), key=lambda x: x[1], reverse=True)[:5]
        ]
        
        # Generate daily trends for the past week
        healing_trends = []
        for i in range(7):
            day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            
            day_sessions = [
                s for s in recent_sessions
                if day_start <= s.started_at < day_end
            ]
            
            day_successful = len([
                s for s in day_sessions
                if s.status == HealingStatus.SUCCESS
            ])
            
            healing_trends.append({
                "date": day_start.strftime("%Y-%m-%d"),
                "attempts": len(day_sessions),
                "successful": day_successful,
                "success_rate": day_successful / len(day_sessions) * 100 if day_sessions else 0.0
            })
        
        healing_trends.reverse()  # Show oldest to newest
        
        return {
            "status": "success",
            "statistics": HealingStatisticsResponse(
                total_attempts=healing_statistics["total_attempts"],
                successful_healings=healing_statistics["successful_healings"],
                failed_healings=healing_statistics["failed_healings"],
                success_rate=healing_statistics["success_rate"],
                average_healing_time=healing_statistics["average_healing_time"],
                last_24h_attempts=last_24h_attempts,
                last_24h_success_rate=last_24h_success_rate,
                top_failure_types=top_failure_types,
                healing_trends=healing_trends
            )
        }
        
    except Exception as e:
        logger.error(f"Failed to get healing statistics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get healing statistics: {str(e)}")

@router.get("/progress/{session_id}")
async def stream_healing_progress(
    session_id: str,
    request: Request,
    user: dict = Depends(get_current_user)
):
    """Stream real-time healing progress updates via Server-Sent Events."""
    
    async def event_generator():
        try:
            orchestrator = await get_healing_orchestrator()
            session = await orchestrator.get_session_status(session_id)
            
            if not session:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": f"Session {session_id} not found"})
                }
                return
            
            # Send initial status
            yield {
                "event": "status",
                "data": json.dumps({
                    "session_id": session_id,
                    "status": session.status.value,
                    "progress": session.progress,
                    "current_phase": session.current_phase,
                    "attempts_count": len(session.attempts)
                })
            }
            
            # Register progress callback
            progress_queue = asyncio.Queue()
            
            def progress_callback(sid: str, progress_data: Dict[str, Any]):
                if sid == session_id:
                    try:
                        progress_queue.put_nowait(progress_data)
                    except asyncio.QueueFull:
                        logger.warning(f"Progress queue full for session {session_id}")
            
            orchestrator.register_progress_callback(session_id, progress_callback)
            
            try:
                # Stream progress updates
                while True:
                    if await request.is_disconnected():
                        break
                    
                    try:
                        # Wait for progress update with timeout
                        progress_data = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                        
                        yield {
                            "event": "progress",
                            "data": json.dumps(progress_data)
                        }
                        
                        # Check if session is complete
                        updated_session = await orchestrator.get_session_status(session_id)
                        if updated_session and updated_session.status in [
                            HealingStatus.SUCCESS, HealingStatus.FAILED, HealingStatus.TIMEOUT
                        ]:
                            yield {
                                "event": "complete",
                                "data": json.dumps({
                                    "session_id": session_id,
                                    "status": updated_session.status.value,
                                    "completed_at": updated_session.completed_at.isoformat() if updated_session.completed_at else None,
                                    "error_message": updated_session.error_message
                                })
                            }
                            break
                            
                    except asyncio.TimeoutError:
                        # Send heartbeat
                        yield {
                            "event": "heartbeat",
                            "data": json.dumps({"timestamp": datetime.now().isoformat()})
                        }
                        continue
                        
            finally:
                # Cleanup: remove progress callback
                callbacks = orchestrator.progress_callbacks.get(session_id, [])
                if progress_callback in callbacks:
                    callbacks.remove(progress_callback)
                    
        except Exception as e:
            logger.error(f"Error in progress stream for session {session_id}: {e}")
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)})
            }
    
    return EventSourceResponse(event_generator())

@router.delete("/containers/cleanup")
async def cleanup_healing_containers(
    user: dict = Depends(get_current_user),
    _: None = Depends(rate_limit_healing)
):
    """Clean up all healing-related containers."""
    try:
        client = get_docker_client()
        
        # Get all healing containers
        containers = client.containers.list(all=True, filters={"name": "chrome-healing-"})
        
        cleaned_up = 0
        errors = []
        
        for container in containers:
            try:
                container.stop(timeout=10)
                container.remove()
                cleaned_up += 1
                logger.info(f"Cleaned up container {container.name}")
            except Exception as e:
                error_msg = f"Failed to cleanup container {container.name}: {e}"
                logger.warning(error_msg)
                errors.append(error_msg)
        
        logger.info(f"Container cleanup completed by user {user.get('username', 'unknown')}: {cleaned_up} containers cleaned")
        
        return {
            "status": "success",
            "message": f"Cleaned up {cleaned_up} healing containers",
            "containers_cleaned": cleaned_up,
            "errors": errors if errors else None
        }
        
    except Exception as e:
        logger.error(f"Failed to cleanup healing containers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cleanup healing containers: {str(e)}")

@router.post("/test-google-healing")
async def trigger_google_search_healing_test(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    _: None = Depends(rate_limit_healing)
):
    """Trigger a test healing session for the Google search failure."""
    try:
        logger.info("🧪 HEALING API: Creating test Google search healing session")
        
        # Create a sample failure context based on the actual Google search failure
        from ..core.models import FailureContext, FailureType
        from datetime import datetime
        
        failure_context = FailureContext(
            test_file="test.robot",
            test_case="Example Google Search",
            failing_step="Click Element    name=btnK",
            original_locator="name=btnK",
            target_url="https://www.google.com",
            exception_type="ElementNotInteractableException",
            exception_message="element not interactable",
            timestamp=datetime.now(),
            run_id="test-healing-session",
            failure_type=FailureType.ELEMENT_NOT_INTERACTABLE
        )
        
        # Get healing orchestrator and initiate healing
        orchestrator = await get_healing_orchestrator()
        session = await orchestrator.initiate_healing(failure_context)
        
        # Store session for dashboard
        store_healing_session(session)
        
        logger.info(f"✅ HEALING API: Created test healing session: {session.session_id}")
        
        return {
            "status": "success",
            "message": "Test healing session created successfully",
            "session_id": session.session_id,
            "test_case": failure_context.test_case,
            "original_locator": failure_context.original_locator
        }
        
    except Exception as e:
        logger.error(f"❌ HEALING API: Failed to create test healing session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create test healing session: {str(e)}")

@router.post("/test")
async def trigger_test_healing(
    test_file: str,
    test_case: str,
    failing_locator: str,
    target_url: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    _: None = Depends(rate_limit_healing)
):
    """Trigger a test healing session for development/testing purposes."""
    try:
        # Create mock failure context
        failure_context = FailureContext(
            test_file=test_file,
            test_case=test_case,
            failing_step=f"Click Element    {failing_locator}",
            original_locator=failing_locator,
            target_url=target_url,
            exception_type="NoSuchElementException",
            exception_message=f"Element with locator '{failing_locator}' not found",
            timestamp=datetime.now(),
            run_id=f"test-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        
        orchestrator = await get_healing_orchestrator()
        session = await orchestrator.initiate_healing(failure_context)
        
        logger.info(f"Test healing session {session.session_id} initiated by user {user.get('username', 'unknown')}")
        
        return {
            "status": "success",
            "message": "Test healing session initiated",
            "session_id": session.session_id,
            "progress_url": f"/healing/progress/{session.session_id}"
        }
        
    except Exception as e:
        logger.error(f"Failed to trigger test healing: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to trigger test healing: {str(e)}")

def _generate_report_from_sessions(run_id: str, sessions: List[Any]) -> Dict[str, Any]:
    """Generate a healing report from a list of sessions."""
    healing_attempts = []
    successful_healings = 0
    failed_healings = 0
    total_time = 0.0
    
    for session in sessions:
        if session.status == HealingStatus.SUCCESS:
            successful_healings += 1
        elif session.status in [HealingStatus.FAILED, HealingStatus.TIMEOUT]:
            failed_healings += 1
        
        if session.completed_at and session.started_at:
            session_time = (session.completed_at - session.started_at).total_seconds()
            total_time += session_time
        
        healing_attempts.append({
            "session_id": session.session_id,
            "test_case": session.failure_context.test_case,
            "original_locator": session.failure_context.original_locator,
            "status": session.status.value,
            "attempts": len(session.attempts),
            "started_at": session.started_at.isoformat(),
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
            "error_message": session.error_message
        })
    
    return {
        "run_id": run_id,
        "test_file": sessions[0].failure_context.test_file if sessions else "unknown",
        "healing_attempts": healing_attempts,
        "total_attempts": len(sessions),
        "successful_healings": successful_healings,
        "failed_healings": failed_healings,
        "total_time": total_time,
        "generated_at": datetime.now()
    }