"""
Workflow metrics storage and retrieval for browser-use workflows.
Tracks metrics for cost optimization and monitoring.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
from dataclasses import dataclass, asdict
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class WorkflowMetrics:
    """Metrics for a single workflow execution."""
    workflow_id: str
    timestamp: datetime
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
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with ISO format timestamp."""
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkflowMetrics':
        """Create from dictionary with ISO format timestamp."""
        data = data.copy()
        if isinstance(data['timestamp'], str):
            data['timestamp'] = datetime.fromisoformat(data['timestamp'])
        return cls(**data)


class WorkflowMetricsCollector:
    """
    Collects and stores workflow metrics for monitoring and analysis.
    Uses file-based storage for simplicity (can be upgraded to database later).
    """
    
    def __init__(self, storage_path: str = "logs/workflow_metrics.jsonl"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        
        # Ensure file exists
        if not self.storage_path.exists():
            self.storage_path.touch()
            logger.info(f"Created workflow metrics storage at {self.storage_path}")
    
    def record_workflow(self, metrics: WorkflowMetrics) -> None:
        """Record a workflow execution's metrics."""
        with self._lock:
            try:
                with open(self.storage_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(metrics.to_dict()) + '\n')
                logger.info(f"Recorded metrics for workflow {metrics.workflow_id}")
            except Exception as e:
                logger.error(f"Failed to record workflow metrics: {e}")
    
    def get_all_metrics(self, limit: Optional[int] = None) -> List[WorkflowMetrics]:
        """Get all recorded metrics, optionally limited to most recent N."""
        metrics = []
        
        with self._lock:
            try:
                if not self.storage_path.exists():
                    return []
                
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                data = json.loads(line)
                                metrics.append(WorkflowMetrics.from_dict(data))
                            except json.JSONDecodeError as e:
                                logger.warning(f"Skipping invalid metrics line: {e}")
                
                # Sort by timestamp descending (most recent first)
                metrics.sort(key=lambda m: m.timestamp, reverse=True)
                
                if limit:
                    metrics = metrics[:limit]
                
                return metrics
            
            except Exception as e:
                logger.error(f"Failed to read workflow metrics: {e}")
                return []
    
    def get_metrics_by_date_range(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[WorkflowMetrics]:
        """Get metrics within a date range."""
        all_metrics = self.get_all_metrics()
        
        if not start_date and not end_date:
            return all_metrics
        
        filtered = []
        for metric in all_metrics:
            if start_date and metric.timestamp < start_date:
                continue
            if end_date and metric.timestamp > end_date:
                continue
            filtered.append(metric)
        
        return filtered
    
    def get_aggregate_metrics(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Get aggregated metrics for a date range."""
        metrics = self.get_metrics_by_date_range(start_date, end_date)
        
        if not metrics:
            return {
                'total_workflows': 0,
                'total_elements': 0,
                'successful_elements': 0,
                'failed_elements': 0,
                'avg_success_rate': 0.0,
                'total_llm_calls': 0,
                'avg_llm_calls_per_element': 0.0,
                'total_cost': 0.0,
                'avg_cost_per_element': 0.0,
                'custom_action_usage_rate': 0.0,
                'avg_execution_time': 0.0,
                'date_range': {
                    'start': start_date.isoformat() if start_date else None,
                    'end': end_date.isoformat() if end_date else None
                }
            }
        
        total_workflows = len(metrics)
        total_elements = sum(m.total_elements for m in metrics)
        successful_elements = sum(m.successful_elements for m in metrics)
        failed_elements = sum(m.failed_elements for m in metrics)
        total_llm_calls = sum(m.total_llm_calls for m in metrics)
        total_cost = sum(m.total_cost for m in metrics)
        custom_action_workflows = sum(1 for m in metrics if m.custom_actions_enabled)
        total_execution_time = sum(m.execution_time for m in metrics)
        
        return {
            'total_workflows': total_workflows,
            'total_elements': total_elements,
            'successful_elements': successful_elements,
            'failed_elements': failed_elements,
            'avg_success_rate': (successful_elements / total_elements * 100) if total_elements > 0 else 0.0,
            'total_llm_calls': total_llm_calls,
            'avg_llm_calls_per_element': total_llm_calls / total_elements if total_elements > 0 else 0.0,
            'total_cost': total_cost,
            'avg_cost_per_element': total_cost / total_elements if total_elements > 0 else 0.0,
            'custom_action_usage_rate': (custom_action_workflows / total_workflows * 100) if total_workflows > 0 else 0.0,
            'avg_execution_time': total_execution_time / total_workflows if total_workflows > 0 else 0.0,
            'date_range': {
                'start': start_date.isoformat() if start_date else None,
                'end': end_date.isoformat() if end_date else None
            }
        }


# Global instance
_metrics_collector: Optional[WorkflowMetricsCollector] = None


def get_workflow_metrics_collector() -> WorkflowMetricsCollector:
    """Get the global workflow metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = WorkflowMetricsCollector()
    return _metrics_collector
