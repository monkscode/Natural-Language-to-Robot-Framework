"""
API endpoints for monitoring and logging data access.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException, Query, Path
from pydantic import BaseModel

from ..core.metrics import get_metrics_collector, HealingMetrics
from ..core.audit_trail import get_audit_trail, AuditEventType
from ..core.alerting import get_alerting_system, AlertSeverity, AlertType


router = APIRouter(prefix="/monitoring", tags=["monitoring"])


class MetricsResponse(BaseModel):
    """Response model for metrics data."""
    timestamp: datetime
    metrics: Dict[str, Any]


class AuditEventResponse(BaseModel):
    """Response model for audit events."""
    event_id: str
    event_type: str
    timestamp: datetime
    session_id: Optional[str]
    test_case: Optional[str]
    component: str
    message: str
    success: Optional[bool]
    duration: Optional[float]
    details: Dict[str, Any]


class AlertResponse(BaseModel):
    """Response model for alerts."""
    alert_id: str
    rule_id: str
    alert_type: str
    severity: str
    title: str
    message: str
    triggered_at: datetime
    resolved_at: Optional[datetime]
    acknowledged_at: Optional[datetime]
    acknowledged_by: Optional[str]
    is_active: bool
    details: Dict[str, Any]


class AlertRuleRequest(BaseModel):
    """Request model for creating alert rules."""
    rule_id: str
    alert_type: str
    severity: str
    name: str
    description: str
    condition: Dict[str, Any]
    enabled: bool = True
    cooldown_minutes: int = 60
    notification_channels: List[str] = ["log"]


@router.get("/metrics", response_model=MetricsResponse)
async def get_current_metrics():
    """Get current healing metrics."""
    try:
        metrics_collector = get_metrics_collector()
        current_metrics = metrics_collector.get_current_metrics()
        
        return MetricsResponse(
            timestamp=datetime.now(),
            metrics=current_metrics.__dict__
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metrics: {str(e)}")


@router.get("/metrics/export")
async def export_metrics(format: str = Query("json", regex="^(json|prometheus)$")):
    """Export metrics in specified format."""
    try:
        metrics_collector = get_metrics_collector()
        exported_data = metrics_collector.export_metrics(format)
        
        if format == "json":
            return {"data": exported_data, "format": "json"}
        else:  # prometheus
            return {"data": exported_data, "format": "prometheus"}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export metrics: {str(e)}")


@router.get("/audit/events", response_model=List[AuditEventResponse])
async def get_audit_events(
    limit: int = Query(100, ge=1, le=1000),
    session_id: Optional[str] = Query(None),
    test_case: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None)
):
    """Get audit events with optional filtering."""
    try:
        audit_trail = get_audit_trail()
        
        if session_id:
            events = audit_trail.get_events_by_session(session_id)
        elif test_case:
            events = audit_trail.get_events_by_test_case(test_case, limit)
        elif event_type:
            try:
                event_type_enum = AuditEventType(event_type)
                events = audit_trail.get_events_by_type(event_type_enum, limit)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid event type: {event_type}")
        elif search:
            events = audit_trail.search_events(search, limit)
        else:
            events = audit_trail.get_recent_events(limit)
        
        return [
            AuditEventResponse(
                event_id=event.event_id,
                event_type=event.event_type.value,
                timestamp=event.timestamp,
                session_id=event.session_id,
                test_case=event.test_case,
                component=event.component,
                message=event.message,
                success=event.success,
                duration=event.duration,
                details=event.details
            )
            for event in events
        ]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get audit events: {str(e)}")


@router.get("/audit/export")
async def export_audit_events(
    format: str = Query("json", regex="^(json|csv)$"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None)
):
    """Export audit events in specified format."""
    try:
        audit_trail = get_audit_trail()
        exported_data = audit_trail.export_events(start_date, end_date, format)
        
        return {"data": exported_data, "format": format}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export audit events: {str(e)}")


@router.get("/alerts", response_model=List[AlertResponse])
async def get_alerts(
    active_only: bool = Query(True),
    severity: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000)
):
    """Get alerts with optional filtering."""
    try:
        alerting_system = get_alerting_system()
        
        if active_only:
            alerts = alerting_system.get_active_alerts()
        else:
            alerts = alerting_system.get_alert_history(limit)
        
        # Apply filters
        if severity:
            try:
                severity_enum = AlertSeverity(severity)
                alerts = [alert for alert in alerts if alert.severity == severity_enum]
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid severity: {severity}")
        
        if alert_type:
            try:
                alert_type_enum = AlertType(alert_type)
                alerts = [alert for alert in alerts if alert.alert_type == alert_type_enum]
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid alert type: {alert_type}")
        
        return [
            AlertResponse(
                alert_id=alert.alert_id,
                rule_id=alert.rule_id,
                alert_type=alert.alert_type.value,
                severity=alert.severity.value,
                title=alert.title,
                message=alert.message,
                triggered_at=alert.triggered_at,
                resolved_at=alert.resolved_at,
                acknowledged_at=alert.acknowledged_at,
                acknowledged_by=alert.acknowledged_by,
                is_active=alert.is_active,
                details=alert.details
            )
            for alert in alerts[:limit]
        ]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get alerts: {str(e)}")


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str = Path(...),
    resolved_by: Optional[str] = Query(None)
):
    """Resolve an active alert."""
    try:
        alerting_system = get_alerting_system()
        success = await alerting_system.resolve_alert(alert_id, resolved_by)
        
        if not success:
            raise HTTPException(status_code=404, detail="Alert not found or already resolved")
        
        return {"message": "Alert resolved successfully", "alert_id": alert_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve alert: {str(e)}")


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str = Path(...),
    acknowledged_by: str = Query(...)
):
    """Acknowledge an active alert."""
    try:
        alerting_system = get_alerting_system()
        success = await alerting_system.acknowledge_alert(alert_id, acknowledged_by)
        
        if not success:
            raise HTTPException(status_code=404, detail="Alert not found")
        
        return {"message": "Alert acknowledged successfully", "alert_id": alert_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to acknowledge alert: {str(e)}")


@router.get("/alerts/rules")
async def get_alert_rules():
    """Get all alert rules."""
    try:
        alerting_system = get_alerting_system()
        rules = alerting_system.rules
        
        return {
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "alert_type": rule.alert_type.value,
                    "severity": rule.severity.value,
                    "name": rule.name,
                    "description": rule.description,
                    "condition": rule.condition,
                    "enabled": rule.enabled,
                    "cooldown_minutes": rule.cooldown_minutes,
                    "notification_channels": rule.notification_channels
                }
                for rule in rules.values()
            ]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get alert rules: {str(e)}")


@router.post("/alerts/rules")
async def create_alert_rule(rule_request: AlertRuleRequest):
    """Create a new alert rule."""
    try:
        from ..core.alerting import AlertRule, AlertType, AlertSeverity
        
        # Validate alert type and severity
        try:
            alert_type_enum = AlertType(rule_request.alert_type)
            severity_enum = AlertSeverity(rule_request.severity)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid enum value: {str(e)}")
        
        rule = AlertRule(
            rule_id=rule_request.rule_id,
            alert_type=alert_type_enum,
            severity=severity_enum,
            name=rule_request.name,
            description=rule_request.description,
            condition=rule_request.condition,
            enabled=rule_request.enabled,
            cooldown_minutes=rule_request.cooldown_minutes,
            notification_channels=rule_request.notification_channels
        )
        
        alerting_system = get_alerting_system()
        alerting_system.add_rule(rule)
        
        return {"message": "Alert rule created successfully", "rule_id": rule_request.rule_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create alert rule: {str(e)}")


@router.delete("/alerts/rules/{rule_id}")
async def delete_alert_rule(rule_id: str = Path(...)):
    """Delete an alert rule."""
    try:
        alerting_system = get_alerting_system()
        alerting_system.remove_rule(rule_id)
        
        return {"message": "Alert rule deleted successfully", "rule_id": rule_id}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete alert rule: {str(e)}")


@router.get("/health")
async def get_monitoring_health():
    """Get health status of monitoring systems."""
    try:
        metrics_collector = get_metrics_collector()
        audit_trail = get_audit_trail()
        alerting_system = get_alerting_system()
        
        # Get basic health indicators
        current_metrics = metrics_collector.get_current_metrics()
        recent_events = audit_trail.get_recent_events(10)
        active_alerts = alerting_system.get_active_alerts()
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(),
            "metrics": {
                "total_healing_attempts": current_metrics.total_healing_attempts,
                "success_rate": (current_metrics.successful_healings / current_metrics.total_healing_attempts 
                               if current_metrics.total_healing_attempts > 0 else 0),
                "avg_healing_time": current_metrics.avg_healing_time
            },
            "audit": {
                "recent_events_count": len(recent_events),
                "last_event_time": recent_events[-1].timestamp if recent_events else None
            },
            "alerts": {
                "active_count": len(active_alerts),
                "critical_count": len([a for a in active_alerts if a.severity.value == "critical"]),
                "high_count": len([a for a in active_alerts if a.severity.value == "high"])
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get monitoring health: {str(e)}")


@router.post("/notifications/email")
async def configure_email_notifications(
    smtp_server: str,
    smtp_port: int,
    username: str,
    password: str,
    from_email: str,
    to_emails: List[str]
):
    """Configure email notifications."""
    try:
        alerting_system = get_alerting_system()
        alerting_system.configure_email_notifications(
            smtp_server, smtp_port, username, password, from_email, to_emails
        )
        
        return {"message": "Email notifications configured successfully"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to configure email notifications: {str(e)}")


@router.post("/notifications/webhook")
async def configure_webhook_notifications(
    webhook_url: str,
    headers: Optional[Dict[str, str]] = None
):
    """Configure webhook notifications."""
    try:
        alerting_system = get_alerting_system()
        alerting_system.configure_webhook_notifications(webhook_url, headers or {})
        
        return {"message": "Webhook notifications configured successfully"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to configure webhook notifications: {str(e)}")