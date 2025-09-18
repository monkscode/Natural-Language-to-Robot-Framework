"""
Audit trail system for test self-healing operations.

This module provides comprehensive audit logging for all healing operations,
maintaining a detailed history of changes, decisions, and outcomes.
"""

import json
import uuid
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any, Union
from enum import Enum
from pathlib import Path
import logging

from .models import FailureContext, HealingSession, HealingStatus, LocatorStrategy


class AuditEventType(Enum):
    """Types of audit events."""
    HEALING_SESSION_STARTED = "healing_session_started"
    HEALING_SESSION_COMPLETED = "healing_session_completed"
    FAILURE_ANALYSIS_COMPLETED = "failure_analysis_completed"
    LOCATOR_GENERATION_COMPLETED = "locator_generation_completed"
    LOCATOR_VALIDATION_STARTED = "locator_validation_started"
    LOCATOR_VALIDATION_COMPLETED = "locator_validation_completed"
    TEST_CODE_BACKUP_CREATED = "test_code_backup_created"
    TEST_CODE_UPDATED = "test_code_updated"
    TEST_CODE_ROLLBACK = "test_code_rollback"
    CHROME_SESSION_CREATED = "chrome_session_created"
    CHROME_SESSION_REUSED = "chrome_session_reused"
    CHROME_SESSION_TIMEOUT = "chrome_session_timeout"
    AGENT_EXECUTION_STARTED = "agent_execution_started"
    AGENT_EXECUTION_COMPLETED = "agent_execution_completed"
    CONFIGURATION_CHANGED = "configuration_changed"
    ALERT_TRIGGERED = "alert_triggered"
    ERROR_OCCURRED = "error_occurred"


@dataclass
class AuditEvent:
    """A single audit event record."""
    event_id: str
    event_type: AuditEventType
    timestamp: datetime
    session_id: Optional[str]
    test_case: Optional[str]
    user_id: Optional[str]
    component: str
    message: str
    details: Dict[str, Any]
    success: Optional[bool] = None
    duration: Optional[float] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert audit event to dictionary."""
        data = asdict(self)
        data['event_type'] = self.event_type.value
        data['timestamp'] = self.timestamp.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AuditEvent':
        """Create audit event from dictionary."""
        data = data.copy()
        data['event_type'] = AuditEventType(data['event_type'])
        data['timestamp'] = datetime.fromisoformat(data['timestamp'])
        return cls(**data)


class AuditTrail:
    """Audit trail manager for healing operations."""
    
    def __init__(self, storage_path: str = "logs/audit"):
        """
        Initialize audit trail.
        
        Args:
            storage_path: Path to store audit files
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Logger for audit events
        self.logger = logging.getLogger("healing.audit")
        
        # In-memory cache for recent events
        self._recent_events: List[AuditEvent] = []
        self._max_recent_events = 1000
    
    def log_event(self, event_type: AuditEventType, component: str, message: str,
                  session_id: str = None, test_case: str = None, user_id: str = None,
                  details: Dict[str, Any] = None, success: bool = None,
                  duration: float = None, error_message: str = None) -> str:
        """
        Log an audit event.
        
        Args:
            event_type: Type of the audit event
            component: Component that generated the event
            message: Human-readable message
            session_id: Optional healing session ID
            test_case: Optional test case name
            user_id: Optional user ID
            details: Optional additional details
            success: Optional success indicator
            duration: Optional operation duration
            error_message: Optional error message
            
        Returns:
            Event ID
        """
        event_id = str(uuid.uuid4())
        
        event = AuditEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=datetime.now(),
            session_id=session_id,
            test_case=test_case,
            user_id=user_id,
            component=component,
            message=message,
            details=details or {},
            success=success,
            duration=duration,
            error_message=error_message
        )
        
        # Add to recent events cache
        self._recent_events.append(event)
        if len(self._recent_events) > self._max_recent_events:
            self._recent_events.pop(0)
        
        # Log to structured logger
        self.logger.info(message, extra={
            'event_id': event_id,
            'event_type': event_type.value,
            'session_id': session_id,
            'test_case': test_case,
            'component': component,
            'success': success,
            'duration': duration,
            'details': details
        })
        
        # Save to file
        self._save_event_to_file(event)
        
        return event_id
    
    def log_healing_session_started(self, session: HealingSession) -> str:
        """Log the start of a healing session."""
        return self.log_event(
            event_type=AuditEventType.HEALING_SESSION_STARTED,
            component="orchestrator",
            message=f"Started healing session for test case: {session.failure_context.test_case}",
            session_id=session.session_id,
            test_case=session.failure_context.test_case,
            details={
                "failure_context": asdict(session.failure_context),
                "original_locator": session.failure_context.original_locator,
                "target_url": session.failure_context.target_url,
                "failure_type": session.failure_context.failure_type.value
            }
        )
    
    def log_healing_session_completed(self, session: HealingSession, duration: float) -> str:
        """Log the completion of a healing session."""
        return self.log_event(
            event_type=AuditEventType.HEALING_SESSION_COMPLETED,
            component="orchestrator",
            message=f"Completed healing session with status: {session.status.value}",
            session_id=session.session_id,
            test_case=session.failure_context.test_case,
            success=session.status == HealingStatus.SUCCESS,
            duration=duration,
            error_message=session.error_message,
            details={
                "status": session.status.value,
                "attempts_count": len(session.attempts),
                "healed_locator": session.healed_locator,
                "confidence_score": session.confidence_score,
                "backup_file_path": session.backup_file_path
            }
        )
    
    def log_failure_analysis(self, session_id: str, test_case: str, analysis_result: Dict[str, Any],
                           duration: float, success: bool) -> str:
        """Log failure analysis completion."""
        return self.log_event(
            event_type=AuditEventType.FAILURE_ANALYSIS_COMPLETED,
            component="failure_analysis_agent",
            message=f"Failure analysis completed for {test_case}",
            session_id=session_id,
            test_case=test_case,
            success=success,
            duration=duration,
            details={
                "analysis_result": analysis_result,
                "is_healable": analysis_result.get("is_healable", False),
                "confidence": analysis_result.get("confidence", 0.0),
                "failure_type": analysis_result.get("failure_type", "unknown")
            }
        )
    
    def log_locator_generation(self, session_id: str, test_case: str, 
                             generated_locators: List[Dict[str, Any]], duration: float) -> str:
        """Log locator generation completion."""
        return self.log_event(
            event_type=AuditEventType.LOCATOR_GENERATION_COMPLETED,
            component="locator_generation_agent",
            message=f"Generated {len(generated_locators)} alternative locators",
            session_id=session_id,
            test_case=test_case,
            success=len(generated_locators) > 0,
            duration=duration,
            details={
                "locator_count": len(generated_locators),
                "locators": generated_locators,
                "strategies_used": list(set(loc.get("strategy", "unknown") for loc in generated_locators))
            }
        )
    
    def log_locator_validation(self, session_id: str, test_case: str, locator: str,
                             strategy: LocatorStrategy, success: bool, duration: float,
                             validation_details: Dict[str, Any]) -> str:
        """Log locator validation."""
        return self.log_event(
            event_type=AuditEventType.LOCATOR_VALIDATION_COMPLETED,
            component="locator_validation_agent",
            message=f"Validated locator: {locator} ({'success' if success else 'failed'})",
            session_id=session_id,
            test_case=test_case,
            success=success,
            duration=duration,
            details={
                "locator": locator,
                "strategy": strategy.value,
                "validation_result": validation_details
            }
        )
    
    def log_test_code_backup(self, session_id: str, test_case: str, original_file: str,
                           backup_file: str, success: bool) -> str:
        """Log test code backup creation."""
        return self.log_event(
            event_type=AuditEventType.TEST_CODE_BACKUP_CREATED,
            component="test_code_updater",
            message=f"Created backup of test file: {original_file}",
            session_id=session_id,
            test_case=test_case,
            success=success,
            details={
                "original_file": original_file,
                "backup_file": backup_file
            }
        )
    
    def log_test_code_update(self, session_id: str, test_case: str, file_path: str,
                           old_locator: str, new_locator: str, success: bool,
                           error_message: str = None) -> str:
        """Log test code update."""
        return self.log_event(
            event_type=AuditEventType.TEST_CODE_UPDATED,
            component="test_code_updater",
            message=f"Updated test code: replaced locator in {file_path}",
            session_id=session_id,
            test_case=test_case,
            success=success,
            error_message=error_message,
            details={
                "file_path": file_path,
                "old_locator": old_locator,
                "new_locator": new_locator
            }
        )
    
    def log_test_code_rollback(self, session_id: str, test_case: str, file_path: str,
                             backup_file: str, success: bool, error_message: str = None) -> str:
        """Log test code rollback."""
        return self.log_event(
            event_type=AuditEventType.TEST_CODE_ROLLBACK,
            component="test_code_updater",
            message=f"Rolled back test code from backup: {backup_file}",
            session_id=session_id,
            test_case=test_case,
            success=success,
            error_message=error_message,
            details={
                "file_path": file_path,
                "backup_file": backup_file
            }
        )
    
    def log_chrome_session_event(self, event_type: AuditEventType, session_id: str,
                                url: str, duration: float = None, success: bool = True,
                                error_message: str = None) -> str:
        """Log Chrome session events."""
        event_messages = {
            AuditEventType.CHROME_SESSION_CREATED: f"Created Chrome session for {url}",
            AuditEventType.CHROME_SESSION_REUSED: f"Reused Chrome session for {url}",
            AuditEventType.CHROME_SESSION_TIMEOUT: f"Chrome session timeout for {url}"
        }
        
        return self.log_event(
            event_type=event_type,
            component="chrome_session_manager",
            message=event_messages.get(event_type, f"Chrome session event: {event_type.value}"),
            session_id=session_id,
            success=success,
            duration=duration,
            error_message=error_message,
            details={
                "url": url,
                "chrome_session_id": session_id
            }
        )
    
    def log_agent_execution(self, agent_name: str, session_id: str, test_case: str,
                          task_description: str, success: bool, duration: float,
                          response_data: Dict[str, Any] = None, error_message: str = None) -> str:
        """Log AI agent execution."""
        return self.log_event(
            event_type=AuditEventType.AGENT_EXECUTION_COMPLETED,
            component=f"agent_{agent_name}",
            message=f"Agent {agent_name} executed task: {task_description}",
            session_id=session_id,
            test_case=test_case,
            success=success,
            duration=duration,
            error_message=error_message,
            details={
                "agent_name": agent_name,
                "task_description": task_description,
                "response_data": response_data or {}
            }
        )
    
    def log_configuration_change(self, component: str, setting_name: str,
                               old_value: Any, new_value: Any, user_id: str = None) -> str:
        """Log configuration changes."""
        return self.log_event(
            event_type=AuditEventType.CONFIGURATION_CHANGED,
            component=component,
            message=f"Configuration changed: {setting_name}",
            user_id=user_id,
            details={
                "setting_name": setting_name,
                "old_value": old_value,
                "new_value": new_value
            }
        )
    
    def log_alert(self, alert_type: str, message: str, severity: str,
                 details: Dict[str, Any] = None) -> str:
        """Log alert events."""
        return self.log_event(
            event_type=AuditEventType.ALERT_TRIGGERED,
            component="alerting_system",
            message=f"Alert triggered: {message}",
            details={
                "alert_type": alert_type,
                "severity": severity,
                "alert_details": details or {}
            }
        )
    
    def log_error(self, component: str, error_message: str, session_id: str = None,
                 test_case: str = None, details: Dict[str, Any] = None) -> str:
        """Log error events."""
        return self.log_event(
            event_type=AuditEventType.ERROR_OCCURRED,
            component=component,
            message=f"Error in {component}: {error_message}",
            session_id=session_id,
            test_case=test_case,
            success=False,
            error_message=error_message,
            details=details or {}
        )
    
    def get_recent_events(self, limit: int = 100) -> List[AuditEvent]:
        """Get recent audit events."""
        return self._recent_events[-limit:] if limit else self._recent_events.copy()
    
    def get_events_by_session(self, session_id: str) -> List[AuditEvent]:
        """Get all events for a specific healing session."""
        return [event for event in self._recent_events if event.session_id == session_id]
    
    def get_events_by_test_case(self, test_case: str, limit: int = 50) -> List[AuditEvent]:
        """Get events for a specific test case."""
        events = [event for event in self._recent_events if event.test_case == test_case]
        return events[-limit:] if limit else events
    
    def get_events_by_type(self, event_type: AuditEventType, limit: int = 100) -> List[AuditEvent]:
        """Get events by type."""
        events = [event for event in self._recent_events if event.event_type == event_type]
        return events[-limit:] if limit else events
    
    def search_events(self, query: str, limit: int = 100) -> List[AuditEvent]:
        """Search events by message content."""
        matching_events = []
        query_lower = query.lower()
        
        for event in self._recent_events:
            if (query_lower in event.message.lower() or 
                query_lower in str(event.details).lower()):
                matching_events.append(event)
        
        return matching_events[-limit:] if limit else matching_events
    
    def export_events(self, start_date: datetime = None, end_date: datetime = None,
                     format: str = "json") -> str:
        """Export events in specified format."""
        events = self._recent_events
        
        # Filter by date range if specified
        if start_date or end_date:
            filtered_events = []
            for event in events:
                if start_date and event.timestamp < start_date:
                    continue
                if end_date and event.timestamp > end_date:
                    continue
                filtered_events.append(event)
            events = filtered_events
        
        if format == "json":
            return json.dumps([event.to_dict() for event in events], indent=2)
        elif format == "csv":
            return self._export_csv_format(events)
        else:
            raise ValueError(f"Unsupported export format: {format}")
    
    def _save_event_to_file(self, event: AuditEvent):
        """Save audit event to daily log file."""
        date_str = event.timestamp.strftime("%Y-%m-%d")
        file_path = self.storage_path / f"audit_{date_str}.jsonl"
        
        try:
            with open(file_path, 'a') as f:
                f.write(json.dumps(event.to_dict()) + '\n')
        except Exception as e:
            self.logger.error(f"Failed to save audit event to file: {e}")
    
    def _export_csv_format(self, events: List[AuditEvent]) -> str:
        """Export events in CSV format."""
        import csv
        import io
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            'event_id', 'event_type', 'timestamp', 'session_id', 'test_case',
            'component', 'message', 'success', 'duration', 'error_message'
        ])
        
        # Write events
        for event in events:
            writer.writerow([
                event.event_id,
                event.event_type.value,
                event.timestamp.isoformat(),
                event.session_id or '',
                event.test_case or '',
                event.component,
                event.message,
                event.success if event.success is not None else '',
                event.duration if event.duration is not None else '',
                event.error_message or ''
            ])
        
        return output.getvalue()


# Global audit trail instance
_audit_trail: Optional[AuditTrail] = None


def get_audit_trail() -> AuditTrail:
    """Get the global audit trail instance."""
    global _audit_trail
    if _audit_trail is None:
        _audit_trail = AuditTrail()
    return _audit_trail


def initialize_audit_trail(storage_path: str = "logs/audit"):
    """Initialize the global audit trail."""
    global _audit_trail
    _audit_trail = AuditTrail(storage_path)