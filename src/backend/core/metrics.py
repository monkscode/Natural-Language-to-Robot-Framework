"""
Metrics collection system for test self-healing operations.

This module provides comprehensive metrics collection for healing success rates,
performance tracking, and operational insights.
"""

import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
import json
import logging
from pathlib import Path

from .models import HealingStatus, FailureType, LocatorStrategy


class MetricType(Enum):
    """Types of metrics collected."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"


@dataclass
class MetricPoint:
    """A single metric data point."""
    timestamp: datetime
    value: float
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class HealingMetrics:
    """Comprehensive healing metrics."""
    # Success/failure rates
    total_healing_attempts: int = 0
    successful_healings: int = 0
    failed_healings: int = 0
    
    # Performance metrics
    avg_healing_time: float = 0.0
    avg_analysis_time: float = 0.0
    avg_generation_time: float = 0.0
    avg_validation_time: float = 0.0
    avg_update_time: float = 0.0
    
    # Failure type distribution
    failure_type_counts: Dict[str, int] = field(default_factory=dict)
    
    # Locator strategy effectiveness
    strategy_success_rates: Dict[str, float] = field(default_factory=dict)
    
    # Chrome session metrics
    avg_session_creation_time: float = 0.0
    session_reuse_rate: float = 0.0
    session_timeout_count: int = 0
    
    # Agent performance
    agent_success_rates: Dict[str, float] = field(default_factory=dict)
    agent_avg_response_times: Dict[str, float] = field(default_factory=dict)
    
    # Test file update metrics
    backup_creation_count: int = 0
    syntax_validation_failures: int = 0
    rollback_count: int = 0
    
    # Time-based metrics
    healings_per_hour: float = 0.0
    peak_concurrent_sessions: int = 0
    
    # Error patterns
    most_common_errors: Dict[str, int] = field(default_factory=dict)
    repeated_failure_patterns: Dict[str, int] = field(default_factory=dict)


class MetricsCollector:
    """Thread-safe metrics collector for healing operations."""
    
    def __init__(self, retention_hours: int = 24):
        """
        Initialize metrics collector.
        
        Args:
            retention_hours: How long to retain detailed metrics in memory
        """
        self.retention_hours = retention_hours
        self.retention_delta = timedelta(hours=retention_hours)
        
        # Thread safety
        self._lock = threading.RLock()
        
        # Metric storage
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._timers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        
        # Time-series data
        self._time_series: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        
        # Healing session tracking
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._completed_sessions: deque = deque(maxlen=1000)
        
        # Error tracking
        self._error_patterns: Dict[str, int] = defaultdict(int)
        self._repeated_failures: Dict[str, List[datetime]] = defaultdict(list)
        
        # Performance tracking
        self._operation_timers: Dict[str, float] = {}
        
        # Logger
        self.logger = logging.getLogger("healing.metrics")
    
    def increment_counter(self, name: str, value: int = 1, labels: Dict[str, str] = None):
        """Increment a counter metric."""
        with self._lock:
            key = self._make_key(name, labels)
            self._counters[key] += value
            
            # Add to time series
            self._time_series[key].append(MetricPoint(
                timestamp=datetime.now(),
                value=self._counters[key],
                labels=labels or {}
            ))
    
    def set_gauge(self, name: str, value: float, labels: Dict[str, str] = None):
        """Set a gauge metric value."""
        with self._lock:
            key = self._make_key(name, labels)
            self._gauges[key] = value
            
            # Add to time series
            self._time_series[key].append(MetricPoint(
                timestamp=datetime.now(),
                value=value,
                labels=labels or {}
            ))
    
    def record_histogram(self, name: str, value: float, labels: Dict[str, str] = None):
        """Record a value in a histogram."""
        with self._lock:
            key = self._make_key(name, labels)
            self._histograms[key].append(MetricPoint(
                timestamp=datetime.now(),
                value=value,
                labels=labels or {}
            ))
    
    def start_timer(self, name: str, labels: Dict[str, str] = None) -> str:
        """Start a timer and return a timer ID."""
        timer_id = f"{name}_{id(threading.current_thread())}_{time.time()}"
        with self._lock:
            self._operation_timers[timer_id] = time.time()
        return timer_id
    
    def stop_timer(self, timer_id: str, name: str, labels: Dict[str, str] = None):
        """Stop a timer and record the duration."""
        with self._lock:
            if timer_id in self._operation_timers:
                start_time = self._operation_timers.pop(timer_id)
                duration = time.time() - start_time
                
                key = self._make_key(name, labels)
                self._timers[key].append(MetricPoint(
                    timestamp=datetime.now(),
                    value=duration,
                    labels=labels or {}
                ))
    
    def record_healing_session_start(self, session_id: str, test_case: str, failure_type: FailureType):
        """Record the start of a healing session."""
        with self._lock:
            self._active_sessions[session_id] = {
                "test_case": test_case,
                "failure_type": failure_type.value,
                "start_time": datetime.now(),
                "phases": {}
            }
            
            # Update counters
            self.increment_counter("healing_attempts_total")
            self.increment_counter("healing_attempts_by_type", labels={"failure_type": failure_type.value})
            
            # Update active sessions gauge
            self.set_gauge("active_healing_sessions", len(self._active_sessions))
    
    def record_healing_session_phase(self, session_id: str, phase: str, duration: float, success: bool):
        """Record completion of a healing session phase."""
        with self._lock:
            if session_id in self._active_sessions:
                self._active_sessions[session_id]["phases"][phase] = {
                    "duration": duration,
                    "success": success,
                    "timestamp": datetime.now()
                }
                
                # Record phase metrics
                self.record_histogram(f"healing_phase_duration", duration, {"phase": phase})
                self.increment_counter(f"healing_phase_{'success' if success else 'failure'}", 
                                     labels={"phase": phase})
    
    def record_healing_session_complete(self, session_id: str, status: HealingStatus, 
                                      total_duration: float, error_message: str = None):
        """Record completion of a healing session."""
        with self._lock:
            if session_id not in self._active_sessions:
                return
            
            session_data = self._active_sessions.pop(session_id)
            session_data.update({
                "end_time": datetime.now(),
                "status": status.value,
                "total_duration": total_duration,
                "error_message": error_message
            })
            
            self._completed_sessions.append(session_data)
            
            # Update counters
            if status == HealingStatus.SUCCESS:
                self.increment_counter("healing_success_total")
                self.increment_counter("healing_success_by_type", 
                                     labels={"failure_type": session_data["failure_type"]})
            else:
                self.increment_counter("healing_failure_total")
                self.increment_counter("healing_failure_by_type", 
                                     labels={"failure_type": session_data["failure_type"]})
                
                # Track error patterns
                if error_message:
                    self._error_patterns[error_message] += 1
                    
                    # Track repeated failures for the same test
                    test_case = session_data["test_case"]
                    self._repeated_failures[test_case].append(datetime.now())
            
            # Record duration
            self.record_histogram("healing_total_duration", total_duration)
            
            # Update active sessions gauge
            self.set_gauge("active_healing_sessions", len(self._active_sessions))
    
    def record_locator_validation(self, locator: str, strategy: LocatorStrategy, 
                                success: bool, duration: float):
        """Record locator validation metrics."""
        with self._lock:
            self.increment_counter("locator_validations_total")
            self.increment_counter(f"locator_validation_{'success' if success else 'failure'}", 
                                 labels={"strategy": strategy.value})
            self.record_histogram("locator_validation_duration", duration, 
                                {"strategy": strategy.value})
    
    def record_chrome_session_metrics(self, creation_time: float, reused: bool, timeout: bool):
        """Record Chrome session metrics."""
        with self._lock:
            self.record_histogram("chrome_session_creation_time", creation_time)
            self.increment_counter("chrome_sessions_created")
            
            if reused:
                self.increment_counter("chrome_sessions_reused")
            
            if timeout:
                self.increment_counter("chrome_session_timeouts")
    
    def record_agent_performance(self, agent_name: str, success: bool, response_time: float):
        """Record AI agent performance metrics."""
        with self._lock:
            self.increment_counter(f"agent_{'success' if success else 'failure'}", 
                                 labels={"agent": agent_name})
            self.record_histogram("agent_response_time", response_time, 
                                {"agent": agent_name})
    
    def record_test_update_metrics(self, backup_created: bool, syntax_valid: bool, rollback: bool):
        """Record test file update metrics."""
        with self._lock:
            if backup_created:
                self.increment_counter("test_backups_created")
            
            if not syntax_valid:
                self.increment_counter("syntax_validation_failures")
            
            if rollback:
                self.increment_counter("test_rollbacks")
    
    def get_current_metrics(self) -> HealingMetrics:
        """Get current aggregated metrics."""
        with self._lock:
            # Calculate success rate
            total_attempts = self._counters.get("healing_attempts_total", 0)
            successful = self._counters.get("healing_success_total", 0)
            failed = self._counters.get("healing_failure_total", 0)
            
            # Calculate average times
            total_durations = [p.value for p in self._histograms.get("healing_total_duration", [])]
            avg_healing_time = sum(total_durations) / len(total_durations) if total_durations else 0.0
            
            # Calculate phase averages
            phase_durations = {}
            for phase in ["analysis", "generation", "validation", "update"]:
                durations = [p.value for p in self._histograms.get("healing_phase_duration", []) 
                           if p.labels.get("phase") == phase]
                phase_durations[phase] = sum(durations) / len(durations) if durations else 0.0
            
            # Calculate failure type distribution
            failure_type_counts = {}
            for key, count in self._counters.items():
                if key.startswith("healing_attempts_by_type"):
                    failure_type = key.split("failure_type:")[-1].rstrip("}")
                    failure_type_counts[failure_type] = count
            
            # Calculate strategy success rates
            strategy_success_rates = {}
            for strategy in LocatorStrategy:
                success_key = f"locator_validation_success_strategy:{strategy.value}"
                failure_key = f"locator_validation_failure_strategy:{strategy.value}"
                
                success_count = self._counters.get(success_key, 0)
                failure_count = self._counters.get(failure_key, 0)
                total_count = success_count + failure_count
                
                if total_count > 0:
                    strategy_success_rates[strategy.value] = success_count / total_count
            
            # Calculate Chrome session metrics
            chrome_durations = [p.value for p in self._histograms.get("chrome_session_creation_time", [])]
            avg_session_creation = sum(chrome_durations) / len(chrome_durations) if chrome_durations else 0.0
            
            sessions_created = self._counters.get("chrome_sessions_created", 0)
            sessions_reused = self._counters.get("chrome_sessions_reused", 0)
            session_reuse_rate = sessions_reused / sessions_created if sessions_created > 0 else 0.0
            
            # Calculate agent success rates
            agent_success_rates = {}
            agent_response_times = {}
            
            for key, count in self._counters.items():
                if key.startswith("agent_success_agent:"):
                    agent_name = key.split("agent:")[-1].rstrip("}")
                    failure_count = self._counters.get(f"agent_failure_agent:{agent_name}", 0)
                    total_count = count + failure_count
                    
                    if total_count > 0:
                        agent_success_rates[agent_name] = count / total_count
                    
                    # Calculate average response time
                    response_times = [p.value for p in self._histograms.get("agent_response_time", [])
                                    if p.labels.get("agent") == agent_name]
                    if response_times:
                        agent_response_times[agent_name] = sum(response_times) / len(response_times)
            
            # Calculate healings per hour
            now = datetime.now()
            hour_ago = now - timedelta(hours=1)
            recent_completions = [s for s in self._completed_sessions 
                                if s.get("end_time", now) > hour_ago]
            healings_per_hour = len(recent_completions)
            
            # Get most common errors
            most_common_errors = dict(sorted(self._error_patterns.items(), 
                                           key=lambda x: x[1], reverse=True)[:10])
            
            # Calculate repeated failure patterns
            repeated_patterns = {}
            for test_case, failures in self._repeated_failures.items():
                recent_failures = [f for f in failures if f > now - timedelta(hours=24)]
                if len(recent_failures) >= 3:  # 3 or more failures in 24 hours
                    repeated_patterns[test_case] = len(recent_failures)
            
            return HealingMetrics(
                total_healing_attempts=total_attempts,
                successful_healings=successful,
                failed_healings=failed,
                avg_healing_time=avg_healing_time,
                avg_analysis_time=phase_durations.get("analysis", 0.0),
                avg_generation_time=phase_durations.get("generation", 0.0),
                avg_validation_time=phase_durations.get("validation", 0.0),
                avg_update_time=phase_durations.get("update", 0.0),
                failure_type_counts=failure_type_counts,
                strategy_success_rates=strategy_success_rates,
                avg_session_creation_time=avg_session_creation,
                session_reuse_rate=session_reuse_rate,
                session_timeout_count=self._counters.get("chrome_session_timeouts", 0),
                agent_success_rates=agent_success_rates,
                agent_avg_response_times=agent_response_times,
                backup_creation_count=self._counters.get("test_backups_created", 0),
                syntax_validation_failures=self._counters.get("syntax_validation_failures", 0),
                rollback_count=self._counters.get("test_rollbacks", 0),
                healings_per_hour=healings_per_hour,
                peak_concurrent_sessions=max([p.value for p in self._time_series.get("active_healing_sessions", [])], default=0),
                most_common_errors=most_common_errors,
                repeated_failure_patterns=repeated_patterns
            )
    
    def export_metrics(self, format: str = "json") -> str:
        """Export metrics in specified format."""
        metrics = self.get_current_metrics()
        
        if format == "json":
            return json.dumps(metrics, default=str, indent=2)
        elif format == "prometheus":
            return self._export_prometheus_format(metrics)
        else:
            raise ValueError(f"Unsupported export format: {format}")
    
    def save_metrics_to_file(self, file_path: str):
        """Save current metrics to a file."""
        metrics_data = {
            "timestamp": datetime.now().isoformat(),
            "metrics": self.get_current_metrics(),
            "raw_counters": dict(self._counters),
            "raw_gauges": dict(self._gauges)
        }
        
        with open(file_path, 'w') as f:
            json.dump(metrics_data, f, default=str, indent=2)
    
    def cleanup_old_data(self):
        """Clean up old metric data based on retention policy."""
        cutoff_time = datetime.now() - self.retention_delta
        
        with self._lock:
            # Clean up time series data
            for key, series in self._time_series.items():
                while series and series[0].timestamp < cutoff_time:
                    series.popleft()
            
            # Clean up histograms
            for key, hist in self._histograms.items():
                while hist and hist[0].timestamp < cutoff_time:
                    hist.popleft()
            
            # Clean up timers
            for key, timers in self._timers.items():
                while timers and timers[0].timestamp < cutoff_time:
                    timers.popleft()
            
            # Clean up repeated failures
            for test_case, failures in self._repeated_failures.items():
                self._repeated_failures[test_case] = [f for f in failures if f > cutoff_time]
    
    def _make_key(self, name: str, labels: Dict[str, str] = None) -> str:
        """Create a key for metric storage."""
        if not labels:
            return name
        
        label_str = "_".join(f"{k}:{v}" for k, v in sorted(labels.items()))
        return f"{name}_{label_str}"
    
    def _export_prometheus_format(self, metrics: HealingMetrics) -> str:
        """Export metrics in Prometheus format."""
        lines = []
        
        # Counters
        lines.append(f"# HELP healing_attempts_total Total number of healing attempts")
        lines.append(f"# TYPE healing_attempts_total counter")
        lines.append(f"healing_attempts_total {metrics.total_healing_attempts}")
        
        lines.append(f"# HELP healing_success_total Total number of successful healings")
        lines.append(f"# TYPE healing_success_total counter")
        lines.append(f"healing_success_total {metrics.successful_healings}")
        
        # Gauges
        lines.append(f"# HELP healing_avg_duration_seconds Average healing duration")
        lines.append(f"# TYPE healing_avg_duration_seconds gauge")
        lines.append(f"healing_avg_duration_seconds {metrics.avg_healing_time}")
        
        # Success rate
        success_rate = (metrics.successful_healings / metrics.total_healing_attempts 
                       if metrics.total_healing_attempts > 0 else 0)
        lines.append(f"# HELP healing_success_rate Healing success rate")
        lines.append(f"# TYPE healing_success_rate gauge")
        lines.append(f"healing_success_rate {success_rate}")
        
        return "\n".join(lines)


# Global metrics collector instance
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """Get the global metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


def initialize_metrics(retention_hours: int = 24):
    """Initialize the global metrics collector."""
    global _metrics_collector
    _metrics_collector = MetricsCollector(retention_hours)