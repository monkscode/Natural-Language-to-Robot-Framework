"""
Tests for logging and monitoring functionality in the test self-healing system.
"""

import pytest
import asyncio
import json
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import logging

from src.backend.core.logging_config import (
    StructuredFormatter, HealingLoggerAdapter, setup_healing_logging, get_healing_logger
)
from src.backend.core.metrics import (
    MetricsCollector, HealingMetrics, MetricType, get_metrics_collector, initialize_metrics
)
from src.backend.core.audit_trail import (
    AuditTrail, AuditEvent, AuditEventType, get_audit_trail, initialize_audit_trail
)
from src.backend.core.alerting import (
    AlertingSystem, AlertRule, Alert, AlertType, AlertSeverity,
    LogNotificationChannel, EmailNotificationChannel, WebhookNotificationChannel,
    get_alerting_system, initialize_alerting
)
from src.backend.core.models import (
    FailureContext, HealingSession, HealingStatus, FailureType, LocatorStrategy
)


class TestStructuredFormatter:
    """Test structured logging formatter."""
    
    def test_basic_formatting(self):
        """Test basic log record formatting."""
        formatter = StructuredFormatter()
        
        # Create a log record
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None
        )
        
        # Format the record
        formatted = formatter.format(record)
        data = json.loads(formatted)
        
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "Test message"
        assert data["module"] == "test"
        assert data["line"] == 42
        assert "timestamp" in data
    
    def test_extra_fields(self):
        """Test formatting with extra fields."""
        formatter = StructuredFormatter()
        
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None
        )
        
        # Add extra fields
        record.session_id = "test-session-123"
        record.operation = "test_operation"
        record.duration = 1.5
        record.success = True
        
        formatted = formatter.format(record)
        data = json.loads(formatted)
        
        assert data["session_id"] == "test-session-123"
        assert data["operation"] == "test_operation"
        assert data["duration"] == 1.5
        assert data["success"] is True
    
    def test_exception_formatting(self):
        """Test exception formatting."""
        formatter = StructuredFormatter()
        
        try:
            raise ValueError("Test exception")
        except ValueError:
            record = logging.LogRecord(
                name="test.logger",
                level=logging.ERROR,
                pathname="test.py",
                lineno=42,
                msg="Error occurred",
                args=(),
                exc_info=True
            )
        
        formatted = formatter.format(record)
        data = json.loads(formatted)
        
        assert "exception" in data
        assert data["exception"]["type"] == "ValueError"
        assert data["exception"]["message"] == "Test exception"
        assert "traceback" in data["exception"]


class TestHealingLoggerAdapter:
    """Test healing logger adapter."""
    
    def test_logger_adapter_context(self):
        """Test logger adapter with context."""
        logger = logging.getLogger("test.healing")
        adapter = HealingLoggerAdapter(logger, {
            "session_id": "test-session",
            "test_case": "test_case_1"
        })
        
        with patch.object(logger, 'info') as mock_info:
            adapter.info("Test message")
            
            # Check that extra context was added
            mock_info.assert_called_once()
            args, kwargs = mock_info.call_args
            assert kwargs['extra']['session_id'] == "test-session"
            assert kwargs['extra']['test_case'] == "test_case_1"
    
    def test_operation_logging(self):
        """Test operation logging methods."""
        logger = logging.getLogger("test.healing")
        adapter = HealingLoggerAdapter(logger, {"session_id": "test-session"})
        
        with patch.object(logger, 'info') as mock_info:
            adapter.log_operation_start("test_operation", param1="value1")
            
            mock_info.assert_called_once()
            args, kwargs = mock_info.call_args
            assert "Starting test_operation" in args[0]
            assert kwargs['extra']['operation'] == "test_operation"
            assert kwargs['extra']['phase'] == "start"
            assert kwargs['extra']['metadata']['param1'] == "value1"
        
        with patch.object(logger, 'info') as mock_info:
            adapter.log_operation_success("test_operation", 2.5, result="success")
            
            mock_info.assert_called_once()
            args, kwargs = mock_info.call_args
            assert "Completed test_operation successfully" in args[0]
            assert kwargs['extra']['success'] is True
            assert kwargs['extra']['duration'] == 2.5
        
        with patch.object(logger, 'error') as mock_error:
            adapter.log_operation_failure("test_operation", 1.0, "Test error", "ERR001")
            
            mock_error.assert_called_once()
            args, kwargs = mock_error.call_args
            assert "Failed test_operation: Test error" in args[0]
            assert kwargs['extra']['success'] is False
            assert kwargs['extra']['error_code'] == "ERR001"


class TestMetricsCollector:
    """Test metrics collection system."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.collector = MetricsCollector(retention_hours=1)
    
    def test_counter_metrics(self):
        """Test counter metrics."""
        self.collector.increment_counter("test_counter")
        self.collector.increment_counter("test_counter", 5)
        self.collector.increment_counter("test_counter_labeled", labels={"type": "test"})
        
        assert self.collector._counters["test_counter"] == 6
        assert self.collector._counters["test_counter_labeled_type:test"] == 1
    
    def test_gauge_metrics(self):
        """Test gauge metrics."""
        self.collector.set_gauge("test_gauge", 42.5)
        self.collector.set_gauge("test_gauge_labeled", 100.0, labels={"env": "test"})
        
        assert self.collector._gauges["test_gauge"] == 42.5
        assert self.collector._gauges["test_gauge_labeled_env:test"] == 100.0
    
    def test_histogram_metrics(self):
        """Test histogram metrics."""
        values = [1.0, 2.5, 3.2, 1.8, 4.1]
        for value in values:
            self.collector.record_histogram("test_histogram", value)
        
        histogram_data = self.collector._histograms["test_histogram"]
        assert len(histogram_data) == 5
        assert all(point.value in values for point in histogram_data)
    
    def test_timer_metrics(self):
        """Test timer metrics."""
        timer_id = self.collector.start_timer("test_operation")
        assert timer_id in self.collector._operation_timers
        
        # Simulate some work
        import time
        time.sleep(0.01)
        
        self.collector.stop_timer(timer_id, "test_operation")
        
        timer_data = self.collector._timers["test_operation"]
        assert len(timer_data) == 1
        assert timer_data[0].value > 0
    
    def test_healing_session_tracking(self):
        """Test healing session metrics tracking."""
        session_id = "test-session-123"
        test_case = "test_case_1"
        failure_type = FailureType.ELEMENT_NOT_FOUND
        
        # Start session
        self.collector.record_healing_session_start(session_id, test_case, failure_type)
        
        assert session_id in self.collector._active_sessions
        assert self.collector._counters["healing_attempts_total"] == 1
        assert self.collector._gauges["active_healing_sessions"] == 1
        
        # Record phases
        self.collector.record_healing_session_phase(session_id, "analysis", 1.5, True)
        self.collector.record_healing_session_phase(session_id, "generation", 2.0, True)
        
        session_data = self.collector._active_sessions[session_id]
        assert "analysis" in session_data["phases"]
        assert session_data["phases"]["analysis"]["duration"] == 1.5
        
        # Complete session
        self.collector.record_healing_session_complete(
            session_id, HealingStatus.SUCCESS, 5.0
        )
        
        assert session_id not in self.collector._active_sessions
        assert len(self.collector._completed_sessions) == 1
        assert self.collector._counters["healing_success_total"] == 1
        assert self.collector._gauges["active_healing_sessions"] == 0
    
    def test_locator_validation_metrics(self):
        """Test locator validation metrics."""
        self.collector.record_locator_validation(
            "id=test-button", LocatorStrategy.ID, True, 0.5
        )
        self.collector.record_locator_validation(
            "css=.test-class", LocatorStrategy.CSS, False, 1.0
        )
        
        assert self.collector._counters["locator_validations_total"] == 2
        assert self.collector._counters["locator_validation_success_strategy:id"] == 1
        assert self.collector._counters["locator_validation_failure_strategy:css"] == 1
    
    def test_metrics_aggregation(self):
        """Test metrics aggregation."""
        # Create some test data
        self.collector.increment_counter("healing_attempts_total", 10)
        self.collector.increment_counter("healing_success_total", 7)
        self.collector.increment_counter("healing_failure_total", 3)
        
        for i in range(5):
            self.collector.record_histogram("healing_total_duration", float(i + 1))
        
        # Get aggregated metrics
        metrics = self.collector.get_current_metrics()
        
        assert metrics.total_healing_attempts == 10
        assert metrics.successful_healings == 7
        assert metrics.failed_healings == 3
        assert metrics.avg_healing_time == 3.0  # Average of 1,2,3,4,5
    
    def test_metrics_export(self):
        """Test metrics export."""
        self.collector.increment_counter("test_counter", 5)
        self.collector.set_gauge("test_gauge", 42.0)
        
        # Export as JSON
        json_export = self.collector.export_metrics("json")
        data = json.loads(json_export)
        
        assert isinstance(data, dict)
        assert "total_healing_attempts" in data
        
        # Export as Prometheus
        prometheus_export = self.collector.export_metrics("prometheus")
        assert "healing_attempts_total" in prometheus_export
        assert "healing_success_rate" in prometheus_export


class TestAuditTrail:
    """Test audit trail system."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.audit_trail = AuditTrail(storage_path=self.temp_dir)
    
    def teardown_method(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir)
    
    def test_basic_event_logging(self):
        """Test basic event logging."""
        event_id = self.audit_trail.log_event(
            event_type=AuditEventType.HEALING_SESSION_STARTED,
            component="test_component",
            message="Test event",
            session_id="test-session",
            details={"key": "value"}
        )
        
        assert event_id is not None
        assert len(self.audit_trail._recent_events) == 1
        
        event = self.audit_trail._recent_events[0]
        assert event.event_id == event_id
        assert event.event_type == AuditEventType.HEALING_SESSION_STARTED
        assert event.component == "test_component"
        assert event.message == "Test event"
        assert event.session_id == "test-session"
        assert event.details["key"] == "value"
    
    def test_healing_session_logging(self):
        """Test healing session specific logging."""
        # Create test failure context
        failure_context = FailureContext(
            test_file="test.robot",
            test_case="Test Case 1",
            failing_step="Click Element",
            original_locator="id=test-button",
            target_url="http://example.com",
            exception_type="NoSuchElementException",
            exception_message="Element not found",
            timestamp=datetime.now(),
            run_id="test-run-123",
            failure_type=FailureType.ELEMENT_NOT_FOUND
        )
        
        # Create test session
        session = HealingSession(
            session_id="test-session-123",
            failure_context=failure_context,
            status=HealingStatus.PENDING,
            started_at=datetime.now()
        )
        
        # Log session start
        event_id = self.audit_trail.log_healing_session_started(session)
        assert event_id is not None
        
        event = self.audit_trail._recent_events[-1]
        assert event.event_type == AuditEventType.HEALING_SESSION_STARTED
        assert event.session_id == "test-session-123"
        assert event.test_case == "Test Case 1"
        assert "failure_context" in event.details
        
        # Log session completion
        session.status = HealingStatus.SUCCESS
        session.healed_locator = "css=#test-button"
        session.confidence_score = 0.85
        
        completion_event_id = self.audit_trail.log_healing_session_completed(session, 5.0)
        assert completion_event_id is not None
        
        completion_event = self.audit_trail._recent_events[-1]
        assert completion_event.event_type == AuditEventType.HEALING_SESSION_COMPLETED
        assert completion_event.success is True
        assert completion_event.duration == 5.0
        assert completion_event.details["healed_locator"] == "css=#test-button"
    
    def test_event_querying(self):
        """Test event querying functionality."""
        # Create multiple events
        for i in range(5):
            self.audit_trail.log_event(
                event_type=AuditEventType.HEALING_SESSION_STARTED,
                component="test_component",
                message=f"Test event {i}",
                session_id=f"session-{i}",
                test_case=f"test_case_{i % 2}"  # Alternate between two test cases
            )
        
        # Test getting recent events
        recent_events = self.audit_trail.get_recent_events(3)
        assert len(recent_events) == 3
        
        # Test getting events by session
        session_events = self.audit_trail.get_events_by_session("session-2")
        assert len(session_events) == 1
        assert session_events[0].session_id == "session-2"
        
        # Test getting events by test case
        test_case_events = self.audit_trail.get_events_by_test_case("test_case_0")
        assert len(test_case_events) == 3  # Events 0, 2, 4
        
        # Test getting events by type
        type_events = self.audit_trail.get_events_by_type(AuditEventType.HEALING_SESSION_STARTED)
        assert len(type_events) == 5
        
        # Test search
        search_results = self.audit_trail.search_events("event 3")
        assert len(search_results) == 1
        assert "event 3" in search_results[0].message
    
    def test_event_export(self):
        """Test event export functionality."""
        # Create test events
        for i in range(3):
            self.audit_trail.log_event(
                event_type=AuditEventType.HEALING_SESSION_STARTED,
                component="test_component",
                message=f"Test event {i}",
                details={"index": i}
            )
        
        # Export as JSON
        json_export = self.audit_trail.export_events(format="json")
        data = json.loads(json_export)
        
        assert len(data) == 3
        assert data[0]["message"] == "Test event 0"
        assert data[0]["details"]["index"] == 0
        
        # Export as CSV
        csv_export = self.audit_trail.export_events(format="csv")
        lines = csv_export.strip().split('\n')
        assert len(lines) == 4  # Header + 3 events
        assert "event_id,event_type,timestamp" in lines[0]
    
    def test_file_persistence(self):
        """Test event persistence to files."""
        event_id = self.audit_trail.log_event(
            event_type=AuditEventType.HEALING_SESSION_STARTED,
            component="test_component",
            message="Test event"
        )
        
        # Check that file was created
        date_str = datetime.now().strftime("%Y-%m-%d")
        audit_file = Path(self.temp_dir) / f"audit_{date_str}.jsonl"
        
        assert audit_file.exists()
        
        # Check file content
        with open(audit_file, 'r') as f:
            line = f.readline().strip()
            event_data = json.loads(line)
            assert event_data["event_id"] == event_id


class TestAlertingSystem:
    """Test alerting system."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.alerting = AlertingSystem()
    
    @pytest.mark.asyncio
    async def test_alert_rule_management(self):
        """Test alert rule management."""
        rule = AlertRule(
            rule_id="test_rule",
            alert_type=AlertType.REPEATED_HEALING_FAILURES,
            severity=AlertSeverity.HIGH,
            name="Test Rule",
            description="Test rule description",
            condition={"failure_count": 3}
        )
        
        self.alerting.add_rule(rule)
        assert "test_rule" in self.alerting.rules
        
        self.alerting.remove_rule("test_rule")
        assert "test_rule" not in self.alerting.rules
    
    @pytest.mark.asyncio
    async def test_notification_channels(self):
        """Test notification channel management."""
        # Test log channel (default)
        assert "log" in self.alerting.notification_channels
        
        # Test email channel configuration
        self.alerting.configure_email_notifications(
            smtp_server="smtp.example.com",
            smtp_port=587,
            username="test@example.com",
            password="password",
            from_email="alerts@example.com",
            to_emails=["admin@example.com"]
        )
        
        assert "email" in self.alerting.notification_channels
        assert isinstance(self.alerting.notification_channels["email"], EmailNotificationChannel)
        
        # Test webhook channel configuration
        self.alerting.configure_webhook_notifications(
            webhook_url="https://example.com/webhook",
            headers={"Authorization": "Bearer token"}
        )
        
        assert "webhook" in self.alerting.notification_channels
        assert isinstance(self.alerting.notification_channels["webhook"], WebhookNotificationChannel)
    
    @pytest.mark.asyncio
    async def test_manual_alert_triggering(self):
        """Test manual alert triggering."""
        rule = AlertRule(
            rule_id="test_rule",
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.CRITICAL,
            name="Test Rule",
            description="Test rule",
            condition={}
        )
        
        self.alerting.add_rule(rule)
        
        alert_id = await self.alerting.trigger_alert(
            rule_id="test_rule",
            title="Test Alert",
            message="This is a test alert",
            details={"key": "value"}
        )
        
        assert alert_id is not None
        assert alert_id in self.alerting.active_alerts
        
        alert = self.alerting.active_alerts[alert_id]
        assert alert.title == "Test Alert"
        assert alert.message == "This is a test alert"
        assert alert.severity == AlertSeverity.CRITICAL
        assert alert.details["key"] == "value"
    
    @pytest.mark.asyncio
    async def test_alert_resolution(self):
        """Test alert resolution."""
        # Create and trigger an alert
        rule = AlertRule(
            rule_id="test_rule",
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.HIGH,
            name="Test Rule",
            description="Test rule",
            condition={}
        )
        
        self.alerting.add_rule(rule)
        alert_id = await self.alerting.trigger_alert("test_rule", "Test Alert", "Test message")
        
        # Resolve the alert
        success = await self.alerting.resolve_alert(alert_id, "test_user")
        assert success is True
        assert alert_id not in self.alerting.active_alerts
        assert len(self.alerting.alert_history) == 1
        
        resolved_alert = self.alerting.alert_history[0]
        assert resolved_alert.resolved_at is not None
        assert resolved_alert.is_active is False
    
    @pytest.mark.asyncio
    async def test_alert_acknowledgment(self):
        """Test alert acknowledgment."""
        # Create and trigger an alert
        rule = AlertRule(
            rule_id="test_rule",
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.MEDIUM,
            name="Test Rule",
            description="Test rule",
            condition={}
        )
        
        self.alerting.add_rule(rule)
        alert_id = await self.alerting.trigger_alert("test_rule", "Test Alert", "Test message")
        
        # Acknowledge the alert
        success = await self.alerting.acknowledge_alert(alert_id, "test_user")
        assert success is True
        
        alert = self.alerting.active_alerts[alert_id]
        assert alert.acknowledged_at is not None
        assert alert.acknowledged_by == "test_user"
    
    @pytest.mark.asyncio
    async def test_repeated_failures_detection(self):
        """Test repeated failures detection."""
        # Check repeated failures
        test_case = "test_case_1"
        failure_type = FailureType.ELEMENT_NOT_FOUND
        
        # Trigger multiple failures
        for _ in range(4):  # Exceeds default threshold of 3
            await self.alerting.check_healing_failure(test_case, failure_type)
        
        # Should have triggered an alert
        active_alerts = self.alerting.get_active_alerts()
        repeated_failure_alerts = [
            alert for alert in active_alerts 
            if alert.alert_type == AlertType.REPEATED_HEALING_FAILURES
        ]
        
        assert len(repeated_failure_alerts) > 0
        alert = repeated_failure_alerts[0]
        assert test_case in alert.details["test_case"]
    
    @pytest.mark.asyncio
    async def test_performance_monitoring(self):
        """Test performance degradation detection."""
        # Record slow operations
        for _ in range(15):  # Exceeds sample size threshold
            await self.alerting.check_performance_metrics(350.0)  # Exceeds 300s threshold
        
        # Should have triggered a performance alert
        active_alerts = self.alerting.get_active_alerts()
        performance_alerts = [
            alert for alert in active_alerts 
            if alert.alert_type == AlertType.PERFORMANCE_DEGRADATION
        ]
        
        assert len(performance_alerts) > 0
        alert = performance_alerts[0]
        assert alert.details["avg_duration"] > 300.0


class TestNotificationChannels:
    """Test notification channels."""
    
    @pytest.mark.asyncio
    async def test_log_notification_channel(self):
        """Test log notification channel."""
        channel = LogNotificationChannel("test_log", {})
        
        alert = Alert(
            alert_id="test-alert",
            rule_id="test-rule",
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.HIGH,
            title="Test Alert",
            message="Test message",
            details={"key": "value"},
            triggered_at=datetime.now()
        )
        
        with patch.object(channel.logger, 'log') as mock_log:
            success = await channel.send_notification(alert)
            
            assert success is True
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == logging.ERROR  # HIGH severity maps to ERROR level
            assert "Test Alert" in args[1]
    
    @pytest.mark.asyncio
    async def test_email_notification_channel(self):
        """Test email notification channel."""
        config = {
            'smtp_server': 'smtp.example.com',
            'smtp_port': 587,
            'username': 'test@example.com',
            'password': 'password',
            'from_email': 'alerts@example.com',
            'to_emails': ['admin@example.com']
        }
        
        channel = EmailNotificationChannel("test_email", config)
        
        alert = Alert(
            alert_id="test-alert",
            rule_id="test-rule",
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.CRITICAL,
            title="Critical Alert",
            message="Critical system error",
            details={"error_code": "SYS001"},
            triggered_at=datetime.now()
        )
        
        with patch('smtplib.SMTP') as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server
            
            success = await channel.send_notification(alert)
            
            assert success is True
            mock_server.send_message.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_webhook_notification_channel(self):
        """Test webhook notification channel."""
        config = {
            'webhook_url': 'https://example.com/webhook',
            'headers': {'Authorization': 'Bearer token'},
            'timeout': 30
        }
        
        channel = WebhookNotificationChannel("test_webhook", config)
        
        alert = Alert(
            alert_id="test-alert",
            rule_id="test-rule",
            alert_type=AlertType.HIGH_FAILURE_RATE,
            severity=AlertSeverity.MEDIUM,
            title="High Failure Rate",
            message="Failure rate exceeded threshold",
            details={"failure_rate": 0.8},
            triggered_at=datetime.now()
        )
        
        with patch('aiohttp.ClientSession') as mock_session:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_session.return_value.__aenter__.return_value.post.return_value.__aenter__.return_value = mock_response
            
            success = await channel.send_notification(alert)
            
            assert success is True


class TestIntegration:
    """Integration tests for logging and monitoring components."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        
        # Initialize components
        initialize_metrics(retention_hours=1)
        initialize_audit_trail(storage_path=self.temp_dir)
        initialize_alerting()
        
        self.metrics = get_metrics_collector()
        self.audit = get_audit_trail()
        self.alerting = get_alerting_system()
    
    def teardown_method(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir)
    
    @pytest.mark.asyncio
    async def test_end_to_end_healing_monitoring(self):
        """Test end-to-end healing operation monitoring."""
        # Start alerting system
        await self.alerting.start()
        
        try:
            # Simulate a healing session
            session_id = "integration-test-session"
            test_case = "Integration Test Case"
            failure_type = FailureType.ELEMENT_NOT_FOUND
            
            # Record session start
            self.metrics.record_healing_session_start(session_id, test_case, failure_type)
            self.audit.log_event(
                event_type=AuditEventType.HEALING_SESSION_STARTED,
                component="orchestrator",
                message=f"Started healing session for {test_case}",
                session_id=session_id,
                test_case=test_case
            )
            
            # Record phases
            self.metrics.record_healing_session_phase(session_id, "analysis", 1.5, True)
            self.audit.log_event(
                event_type=AuditEventType.FAILURE_ANALYSIS_COMPLETED,
                component="failure_analysis_agent",
                message="Analysis completed",
                session_id=session_id,
                test_case=test_case,
                success=True,
                duration=1.5
            )
            
            self.metrics.record_healing_session_phase(session_id, "generation", 2.0, True)
            self.metrics.record_healing_session_phase(session_id, "validation", 1.0, True)
            
            # Record locator validation
            self.metrics.record_locator_validation(
                "css=#test-button", LocatorStrategy.CSS, True, 0.5
            )
            
            # Complete session
            self.metrics.record_healing_session_complete(
                session_id, HealingStatus.SUCCESS, 5.0
            )
            self.audit.log_event(
                event_type=AuditEventType.HEALING_SESSION_COMPLETED,
                component="orchestrator",
                message="Healing session completed successfully",
                session_id=session_id,
                test_case=test_case,
                success=True,
                duration=5.0
            )
            
            # Verify metrics
            metrics = self.metrics.get_current_metrics()
            assert metrics.total_healing_attempts == 1
            assert metrics.successful_healings == 1
            assert metrics.failed_healings == 0
            assert metrics.avg_healing_time == 5.0
            
            # Verify audit trail
            session_events = self.audit.get_events_by_session(session_id)
            assert len(session_events) >= 2  # Start and complete events
            
            start_events = [e for e in session_events if e.event_type == AuditEventType.HEALING_SESSION_STARTED]
            complete_events = [e for e in session_events if e.event_type == AuditEventType.HEALING_SESSION_COMPLETED]
            
            assert len(start_events) == 1
            assert len(complete_events) == 1
            assert complete_events[0].success is True
            
            # Verify no alerts were triggered for successful operation
            active_alerts = self.alerting.get_active_alerts()
            assert len(active_alerts) == 0
            
        finally:
            await self.alerting.stop()
    
    @pytest.mark.asyncio
    async def test_failure_alerting_integration(self):
        """Test integration between metrics, audit, and alerting for failures."""
        await self.alerting.start()
        
        try:
            test_case = "Failing Test Case"
            failure_type = FailureType.ELEMENT_NOT_FOUND
            
            # Simulate multiple failures to trigger alert
            for i in range(4):  # Exceeds threshold of 3
                session_id = f"failing-session-{i}"
                
                # Record failure
                self.metrics.record_healing_session_start(session_id, test_case, failure_type)
                self.metrics.record_healing_session_complete(
                    session_id, HealingStatus.FAILED, 2.0, "Locator not found"
                )
                
                # Audit failure
                self.audit.log_event(
                    event_type=AuditEventType.HEALING_SESSION_COMPLETED,
                    component="orchestrator",
                    message="Healing session failed",
                    session_id=session_id,
                    test_case=test_case,
                    success=False,
                    error_message="Locator not found"
                )
                
                # Check for repeated failures
                await self.alerting.check_healing_failure(test_case, failure_type, session_id)
            
            # Verify metrics show failures
            metrics = self.metrics.get_current_metrics()
            assert metrics.total_healing_attempts == 4
            assert metrics.failed_healings == 4
            assert metrics.successful_healings == 0
            
            # Verify alert was triggered
            active_alerts = self.alerting.get_active_alerts()
            repeated_failure_alerts = [
                alert for alert in active_alerts 
                if alert.alert_type == AlertType.REPEATED_HEALING_FAILURES
            ]
            
            assert len(repeated_failure_alerts) > 0
            alert = repeated_failure_alerts[0]
            assert test_case in alert.details["test_case"]
            assert alert.severity == AlertSeverity.HIGH
            
            # Verify audit trail contains alert
            alert_events = self.audit.get_events_by_type(AuditEventType.ALERT_TRIGGERED)
            assert len(alert_events) > 0
            
        finally:
            await self.alerting.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])