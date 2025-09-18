"""
Alerting system for test self-healing operations.

This module provides alerting capabilities for repeated healing failures,
performance issues, and other critical events in the healing system.
"""

import asyncio
import smtplib
import json
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable, Set
from enum import Enum
import logging
from collections import defaultdict, deque

from .models import HealingStatus, FailureType
from .metrics import get_metrics_collector
from .audit_trail import get_audit_trail, AuditEventType


class AlertSeverity(Enum):
    """Alert severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertType(Enum):
    """Types of alerts."""
    REPEATED_HEALING_FAILURES = "repeated_healing_failures"
    HIGH_FAILURE_RATE = "high_failure_rate"
    PERFORMANCE_DEGRADATION = "performance_degradation"
    CHROME_SESSION_ISSUES = "chrome_session_issues"
    AGENT_FAILURES = "agent_failures"
    SYSTEM_ERROR = "system_error"
    CONFIGURATION_ISSUE = "configuration_issue"
    RESOURCE_EXHAUSTION = "resource_exhaustion"


@dataclass
class AlertRule:
    """Configuration for an alert rule."""
    rule_id: str
    alert_type: AlertType
    severity: AlertSeverity
    name: str
    description: str
    condition: Dict[str, Any]
    enabled: bool = True
    cooldown_minutes: int = 60
    notification_channels: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.notification_channels:
            self.notification_channels = ["log"]


@dataclass
class Alert:
    """An active alert."""
    alert_id: str
    rule_id: str
    alert_type: AlertType
    severity: AlertSeverity
    title: str
    message: str
    details: Dict[str, Any]
    triggered_at: datetime
    resolved_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    notification_sent: bool = False
    
    @property
    def is_active(self) -> bool:
        """Check if alert is still active."""
        return self.resolved_at is None
    
    @property
    def duration(self) -> timedelta:
        """Get alert duration."""
        end_time = self.resolved_at or datetime.now()
        return end_time - self.triggered_at


class NotificationChannel:
    """Base class for notification channels."""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self.logger = logging.getLogger(f"healing.alerting.{name}")
    
    async def send_notification(self, alert: Alert) -> bool:
        """Send notification for an alert."""
        raise NotImplementedError


class LogNotificationChannel(NotificationChannel):
    """Log-based notification channel."""
    
    async def send_notification(self, alert: Alert) -> bool:
        """Log the alert."""
        log_level = {
            AlertSeverity.LOW: logging.INFO,
            AlertSeverity.MEDIUM: logging.WARNING,
            AlertSeverity.HIGH: logging.ERROR,
            AlertSeverity.CRITICAL: logging.CRITICAL
        }.get(alert.severity, logging.WARNING)
        
        self.logger.log(log_level, f"ALERT: {alert.title} - {alert.message}", extra={
            'alert_id': alert.alert_id,
            'alert_type': alert.alert_type.value,
            'severity': alert.severity.value,
            'details': alert.details
        })
        
        return True


class EmailNotificationChannel(NotificationChannel):
    """Email notification channel."""
    
    async def send_notification(self, alert: Alert) -> bool:
        """Send email notification."""
        try:
            smtp_server = self.config.get('smtp_server', 'localhost')
            smtp_port = self.config.get('smtp_port', 587)
            username = self.config.get('username')
            password = self.config.get('password')
            from_email = self.config.get('from_email', 'healing-system@example.com')
            to_emails = self.config.get('to_emails', [])
            
            if not to_emails:
                self.logger.warning("No recipient emails configured")
                return False
            
            # Import email modules here to avoid import issues
            from email.mime.text import MimeText
            from email.mime.multipart import MimeMultipart
            
            # Create message
            msg = MimeMultipart()
            msg['From'] = from_email
            msg['To'] = ', '.join(to_emails)
            msg['Subject'] = f"[{alert.severity.value.upper()}] {alert.title}"
            
            # Create email body
            body = self._create_email_body(alert)
            msg.attach(MimeText(body, 'html'))
            
            # Send email
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                if username and password:
                    server.starttls()
                    server.login(username, password)
                
                server.send_message(msg)
            
            self.logger.info(f"Email notification sent for alert {alert.alert_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to send email notification: {e}")
            return False
    
    def _create_email_body(self, alert: Alert) -> str:
        """Create HTML email body."""
        severity_colors = {
            AlertSeverity.LOW: "#28a745",
            AlertSeverity.MEDIUM: "#ffc107",
            AlertSeverity.HIGH: "#fd7e14",
            AlertSeverity.CRITICAL: "#dc3545"
        }
        
        color = severity_colors.get(alert.severity, "#6c757d")
        
        return f"""
        <html>
        <body>
            <div style="font-family: Arial, sans-serif; max-width: 600px;">
                <div style="background-color: {color}; color: white; padding: 20px; border-radius: 5px 5px 0 0;">
                    <h2 style="margin: 0;">{alert.title}</h2>
                    <p style="margin: 5px 0 0 0;">Severity: {alert.severity.value.upper()}</p>
                </div>
                
                <div style="background-color: #f8f9fa; padding: 20px; border: 1px solid #dee2e6; border-top: none; border-radius: 0 0 5px 5px;">
                    <h3>Alert Details</h3>
                    <p><strong>Alert ID:</strong> {alert.alert_id}</p>
                    <p><strong>Type:</strong> {alert.alert_type.value}</p>
                    <p><strong>Triggered:</strong> {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
                    
                    <h4>Message</h4>
                    <p>{alert.message}</p>
                    
                    <h4>Additional Details</h4>
                    <pre style="background-color: #e9ecef; padding: 10px; border-radius: 3px; overflow-x: auto;">
{json.dumps(alert.details, indent=2)}
                    </pre>
                </div>
            </div>
        </body>
        </html>
        """


class WebhookNotificationChannel(NotificationChannel):
    """Webhook notification channel."""
    
    async def send_notification(self, alert: Alert) -> bool:
        """Send webhook notification."""
        import aiohttp
        
        try:
            webhook_url = self.config.get('webhook_url')
            if not webhook_url:
                self.logger.warning("No webhook URL configured")
                return False
            
            payload = {
                'alert_id': alert.alert_id,
                'alert_type': alert.alert_type.value,
                'severity': alert.severity.value,
                'title': alert.title,
                'message': alert.message,
                'details': alert.details,
                'triggered_at': alert.triggered_at.isoformat()
            }
            
            headers = self.config.get('headers', {})
            timeout = self.config.get('timeout', 30)
            
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=payload, headers=headers, timeout=timeout) as response:
                    if response.status == 200:
                        self.logger.info(f"Webhook notification sent for alert {alert.alert_id}")
                        return True
                    else:
                        self.logger.error(f"Webhook returned status {response.status}")
                        return False
                        
        except Exception as e:
            self.logger.error(f"Failed to send webhook notification: {e}")
            return False


class AlertingSystem:
    """Main alerting system for healing operations."""
    
    def __init__(self):
        """Initialize the alerting system."""
        self.logger = logging.getLogger("healing.alerting")
        
        # Alert rules and active alerts
        self.rules: Dict[str, AlertRule] = {}
        self.active_alerts: Dict[str, Alert] = {}
        self.alert_history: deque = deque(maxlen=1000)
        
        # Notification channels
        self.notification_channels: Dict[str, NotificationChannel] = {}
        
        # Cooldown tracking
        self.rule_cooldowns: Dict[str, datetime] = {}
        
        # Monitoring data
        self.failure_counts: Dict[str, List[datetime]] = defaultdict(list)
        self.performance_metrics: deque = deque(maxlen=100)
        
        # Background task for monitoring
        self._monitoring_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Initialize default rules
        self._initialize_default_rules()
        
        # Initialize default notification channels
        self._initialize_default_channels()
    
    def _initialize_default_rules(self):
        """Initialize default alert rules."""
        # Repeated healing failures
        self.add_rule(AlertRule(
            rule_id="repeated_failures",
            alert_type=AlertType.REPEATED_HEALING_FAILURES,
            severity=AlertSeverity.HIGH,
            name="Repeated Healing Failures",
            description="Multiple healing failures for the same test case",
            condition={
                "failure_count": 3,
                "time_window_minutes": 60,
                "same_test_case": True
            }
        ))
        
        # High failure rate
        self.add_rule(AlertRule(
            rule_id="high_failure_rate",
            alert_type=AlertType.HIGH_FAILURE_RATE,
            severity=AlertSeverity.MEDIUM,
            name="High Healing Failure Rate",
            description="Overall healing failure rate is too high",
            condition={
                "failure_rate_threshold": 0.7,
                "minimum_attempts": 10,
                "time_window_minutes": 120
            }
        ))
        
        # Performance degradation
        self.add_rule(AlertRule(
            rule_id="performance_degradation",
            alert_type=AlertType.PERFORMANCE_DEGRADATION,
            severity=AlertSeverity.MEDIUM,
            name="Performance Degradation",
            description="Healing operations are taking longer than usual",
            condition={
                "avg_duration_threshold": 300.0,  # 5 minutes
                "sample_size": 10
            }
        ))
        
        # Chrome session issues
        self.add_rule(AlertRule(
            rule_id="chrome_session_issues",
            alert_type=AlertType.CHROME_SESSION_ISSUES,
            severity=AlertSeverity.MEDIUM,
            name="Chrome Session Issues",
            description="High rate of Chrome session timeouts or failures",
            condition={
                "timeout_rate_threshold": 0.3,
                "minimum_sessions": 5,
                "time_window_minutes": 60
            }
        ))
        
        # Agent failures
        self.add_rule(AlertRule(
            rule_id="agent_failures",
            alert_type=AlertType.AGENT_FAILURES,
            severity=AlertSeverity.HIGH,
            name="AI Agent Failures",
            description="High rate of AI agent execution failures",
            condition={
                "failure_rate_threshold": 0.5,
                "minimum_executions": 5,
                "time_window_minutes": 60
            }
        ))
    
    def _initialize_default_channels(self):
        """Initialize default notification channels."""
        # Log channel (always available)
        self.add_notification_channel("log", LogNotificationChannel("log", {}))
    
    def add_rule(self, rule: AlertRule):
        """Add an alert rule."""
        self.rules[rule.rule_id] = rule
        self.logger.info(f"Added alert rule: {rule.name}")
    
    def remove_rule(self, rule_id: str):
        """Remove an alert rule."""
        if rule_id in self.rules:
            del self.rules[rule_id]
            self.logger.info(f"Removed alert rule: {rule_id}")
    
    def add_notification_channel(self, name: str, channel: NotificationChannel):
        """Add a notification channel."""
        self.notification_channels[name] = channel
        self.logger.info(f"Added notification channel: {name}")
    
    def configure_email_notifications(self, smtp_server: str, smtp_port: int,
                                    username: str, password: str, from_email: str,
                                    to_emails: List[str]):
        """Configure email notifications."""
        config = {
            'smtp_server': smtp_server,
            'smtp_port': smtp_port,
            'username': username,
            'password': password,
            'from_email': from_email,
            'to_emails': to_emails
        }
        
        channel = EmailNotificationChannel("email", config)
        self.add_notification_channel("email", channel)
    
    def configure_webhook_notifications(self, webhook_url: str, headers: Dict[str, str] = None):
        """Configure webhook notifications."""
        config = {
            'webhook_url': webhook_url,
            'headers': headers or {},
            'timeout': 30
        }
        
        channel = WebhookNotificationChannel("webhook", config)
        self.add_notification_channel("webhook", channel)
    
    async def start(self):
        """Start the alerting system."""
        if self._running:
            return
        
        self._running = True
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self.logger.info("Alerting system started")
    
    async def stop(self):
        """Stop the alerting system."""
        self._running = False
        
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
        
        self.logger.info("Alerting system stopped")
    
    async def check_healing_failure(self, test_case: str, failure_type: FailureType,
                                  session_id: str = None):
        """Check for repeated healing failures."""
        now = datetime.now()
        self.failure_counts[test_case].append(now)
        
        # Check repeated failures rule
        rule = self.rules.get("repeated_failures")
        if rule and rule.enabled:
            await self._check_repeated_failures_rule(rule, test_case, now)
    
    async def check_performance_metrics(self, operation_duration: float):
        """Check performance degradation."""
        self.performance_metrics.append({
            'duration': operation_duration,
            'timestamp': datetime.now()
        })
        
        # Check performance degradation rule
        rule = self.rules.get("performance_degradation")
        if rule and rule.enabled:
            await self._check_performance_rule(rule)
    
    async def trigger_alert(self, rule_id: str, title: str, message: str,
                          details: Dict[str, Any] = None) -> Optional[str]:
        """Manually trigger an alert."""
        rule = self.rules.get(rule_id)
        if not rule:
            self.logger.warning(f"Unknown rule ID: {rule_id}")
            return None
        
        return await self._create_alert(rule, title, message, details or {})
    
    async def resolve_alert(self, alert_id: str, resolved_by: str = None) -> bool:
        """Resolve an active alert."""
        if alert_id not in self.active_alerts:
            return False
        
        alert = self.active_alerts[alert_id]
        alert.resolved_at = datetime.now()
        
        # Move to history
        self.alert_history.append(alert)
        del self.active_alerts[alert_id]
        
        self.logger.info(f"Alert resolved: {alert_id} by {resolved_by or 'system'}")
        
        # Log to audit trail
        get_audit_trail().log_event(
            event_type=AuditEventType.ALERT_TRIGGERED,
            component="alerting_system",
            message=f"Alert resolved: {alert.title}",
            details={
                "alert_id": alert_id,
                "resolved_by": resolved_by,
                "duration": alert.duration.total_seconds()
            }
        )
        
        return True
    
    async def acknowledge_alert(self, alert_id: str, acknowledged_by: str) -> bool:
        """Acknowledge an active alert."""
        if alert_id not in self.active_alerts:
            return False
        
        alert = self.active_alerts[alert_id]
        alert.acknowledged_at = datetime.now()
        alert.acknowledged_by = acknowledged_by
        
        self.logger.info(f"Alert acknowledged: {alert_id} by {acknowledged_by}")
        return True
    
    def get_active_alerts(self) -> List[Alert]:
        """Get all active alerts."""
        return list(self.active_alerts.values())
    
    def get_alert_history(self, limit: int = 100) -> List[Alert]:
        """Get alert history."""
        return list(self.alert_history)[-limit:]
    
    async def _monitoring_loop(self):
        """Background monitoring loop."""
        while self._running:
            try:
                await self._check_all_rules()
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(60)
    
    async def _check_all_rules(self):
        """Check all enabled alert rules."""
        metrics = get_metrics_collector().get_current_metrics()
        
        for rule in self.rules.values():
            if not rule.enabled:
                continue
            
            # Check cooldown
            if rule.rule_id in self.rule_cooldowns:
                cooldown_end = self.rule_cooldowns[rule.rule_id] + timedelta(minutes=rule.cooldown_minutes)
                if datetime.now() < cooldown_end:
                    continue
            
            try:
                if rule.alert_type == AlertType.HIGH_FAILURE_RATE:
                    await self._check_failure_rate_rule(rule, metrics)
                elif rule.alert_type == AlertType.CHROME_SESSION_ISSUES:
                    await self._check_chrome_session_rule(rule, metrics)
                elif rule.alert_type == AlertType.AGENT_FAILURES:
                    await self._check_agent_failures_rule(rule, metrics)
                    
            except Exception as e:
                self.logger.error(f"Error checking rule {rule.rule_id}: {e}")
    
    async def _check_repeated_failures_rule(self, rule: AlertRule, test_case: str, now: datetime):
        """Check repeated failures rule."""
        condition = rule.condition
        time_window = timedelta(minutes=condition.get("time_window_minutes", 60))
        failure_threshold = condition.get("failure_count", 3)
        
        # Count recent failures for this test case
        cutoff_time = now - time_window
        recent_failures = [f for f in self.failure_counts[test_case] if f > cutoff_time]
        
        if len(recent_failures) >= failure_threshold:
            await self._create_alert(
                rule,
                f"Repeated Healing Failures: {test_case}",
                f"Test case '{test_case}' has failed healing {len(recent_failures)} times in the last {condition.get('time_window_minutes', 60)} minutes",
                {
                    "test_case": test_case,
                    "failure_count": len(recent_failures),
                    "time_window_minutes": condition.get("time_window_minutes", 60),
                    "recent_failures": [f.isoformat() for f in recent_failures]
                }
            )
    
    async def _check_performance_rule(self, rule: AlertRule):
        """Check performance degradation rule."""
        condition = rule.condition
        threshold = condition.get("avg_duration_threshold", 300.0)
        sample_size = condition.get("sample_size", 10)
        
        if len(self.performance_metrics) >= sample_size:
            recent_metrics = list(self.performance_metrics)[-sample_size:]
            avg_duration = sum(m['duration'] for m in recent_metrics) / len(recent_metrics)
            
            if avg_duration > threshold:
                await self._create_alert(
                    rule,
                    "Performance Degradation Detected",
                    f"Average healing duration ({avg_duration:.1f}s) exceeds threshold ({threshold}s)",
                    {
                        "avg_duration": avg_duration,
                        "threshold": threshold,
                        "sample_size": sample_size
                    }
                )
    
    async def _check_failure_rate_rule(self, rule: AlertRule, metrics):
        """Check high failure rate rule."""
        condition = rule.condition
        threshold = condition.get("failure_rate_threshold", 0.7)
        min_attempts = condition.get("minimum_attempts", 10)
        
        total_attempts = metrics.total_healing_attempts
        failed_attempts = metrics.failed_healings
        
        if total_attempts >= min_attempts:
            failure_rate = failed_attempts / total_attempts
            
            if failure_rate > threshold:
                await self._create_alert(
                    rule,
                    "High Healing Failure Rate",
                    f"Healing failure rate ({failure_rate:.1%}) exceeds threshold ({threshold:.1%})",
                    {
                        "failure_rate": failure_rate,
                        "threshold": threshold,
                        "total_attempts": total_attempts,
                        "failed_attempts": failed_attempts
                    }
                )
    
    async def _check_chrome_session_rule(self, rule: AlertRule, metrics):
        """Check Chrome session issues rule."""
        condition = rule.condition
        threshold = condition.get("timeout_rate_threshold", 0.3)
        min_sessions = condition.get("minimum_sessions", 5)
        
        # This would need to be implemented based on actual Chrome session metrics
        # For now, we'll use placeholder logic
        pass
    
    async def _check_agent_failures_rule(self, rule: AlertRule, metrics):
        """Check AI agent failures rule."""
        condition = rule.condition
        threshold = condition.get("failure_rate_threshold", 0.5)
        min_executions = condition.get("minimum_executions", 5)
        
        for agent_name, success_rate in metrics.agent_success_rates.items():
            if success_rate < (1 - threshold):  # Convert success rate to failure rate
                await self._create_alert(
                    rule,
                    f"High Agent Failure Rate: {agent_name}",
                    f"Agent '{agent_name}' has a low success rate ({success_rate:.1%})",
                    {
                        "agent_name": agent_name,
                        "success_rate": success_rate,
                        "failure_rate": 1 - success_rate,
                        "threshold": threshold
                    }
                )
    
    async def _create_alert(self, rule: AlertRule, title: str, message: str,
                          details: Dict[str, Any]) -> str:
        """Create and process a new alert."""
        alert_id = f"alert_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{rule.rule_id}"
        
        alert = Alert(
            alert_id=alert_id,
            rule_id=rule.rule_id,
            alert_type=rule.alert_type,
            severity=rule.severity,
            title=title,
            message=message,
            details=details,
            triggered_at=datetime.now()
        )
        
        # Add to active alerts
        self.active_alerts[alert_id] = alert
        
        # Set cooldown
        self.rule_cooldowns[rule.rule_id] = datetime.now()
        
        # Send notifications
        await self._send_notifications(alert, rule.notification_channels)
        
        # Log to audit trail
        get_audit_trail().log_alert(
            alert_type=rule.alert_type.value,
            message=title,
            severity=rule.severity.value,
            details=details
        )
        
        self.logger.warning(f"Alert triggered: {title} (ID: {alert_id})")
        
        return alert_id
    
    async def _send_notifications(self, alert: Alert, channels: List[str]):
        """Send notifications through specified channels."""
        for channel_name in channels:
            channel = self.notification_channels.get(channel_name)
            if channel:
                try:
                    success = await channel.send_notification(alert)
                    if success:
                        alert.notification_sent = True
                except Exception as e:
                    self.logger.error(f"Failed to send notification via {channel_name}: {e}")


# Global alerting system instance
_alerting_system: Optional[AlertingSystem] = None


def get_alerting_system() -> AlertingSystem:
    """Get the global alerting system instance."""
    global _alerting_system
    if _alerting_system is None:
        _alerting_system = AlertingSystem()
    return _alerting_system


def initialize_alerting():
    """Initialize the global alerting system."""
    global _alerting_system
    _alerting_system = AlertingSystem()