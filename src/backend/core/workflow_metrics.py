"""
Workflow metrics storage and retrieval for browser-use workflows.
Tracks metrics for cost optimization and monitoring.

This module provides:
- WorkflowMetrics: The main model (imported from core.models)
- WorkflowMetricsCollector: Storage and retrieval of metrics
- Utility functions: count_tokens, calculate_crewai_cost
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path
from threading import Lock

# Import the Pydantic model from models
from .models import WorkflowMetrics

logger = logging.getLogger(__name__)


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
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if line:
                            try:
                                data = json.loads(line)
                                metrics.append(WorkflowMetrics.from_dict(data))
                            except json.JSONDecodeError as e:
                                logger.warning(f"Skipping invalid JSON on line {line_num}: {e}")
                            except Exception as e:
                                logger.warning(f"Skipping invalid metrics on line {line_num}: {e}")
                
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


def count_tokens(text: str) -> int:
    """
    Count tokens in text using simple word-based estimation.
    
    This is a simple approximation: 1 token ≈ 0.75 words (or 1 word ≈ 1.33 tokens).
    For more accurate counting, consider using tiktoken library.
    
    Args:
        text: Text to count tokens for
    
    Returns:
        Estimated token count
    
    Example:
        >>> count_tokens("Hello world, this is a test")
        8
    """
    if not text:
        return 0
    
    words = text.split()
    estimated_tokens = int(len(words) * 1.33)
    
    return estimated_tokens


def calculate_crewai_cost(usage_metrics: dict, model_name: str = "gemini-2.5-flash") -> dict:
    """
    Extract cost and token metrics from CrewAI's usage_metrics.
    
    CrewAI already calculates cost via LiteLLM internally, so we simply extract it.
    No need to recalculate what's already been calculated!
    
    Args:
        usage_metrics: Usage metrics dict from CrewAI with keys:
            - total_tokens: Total tokens used
            - prompt_tokens: Input tokens
            - completion_tokens: Output tokens
            - successful_requests: Number of successful LLM calls
            - total_cost: Cost already calculated by CrewAI/LiteLLM
        model_name: Model name (for logging/fallback only)
    
    Returns:
        Dict with llm_calls, cost, tokens breakdown
    
    Example:
        >>> crew = Crew(agents=[...], tasks=[...])
        >>> result = crew.kickoff()
        >>> usage_obj = crew.calculate_usage_metrics()  # CrewAI 1.3.0+
        >>> usage_dict = {
        ...     'total_tokens': usage_obj.total_tokens,
        ...     'prompt_tokens': usage_obj.prompt_tokens,
        ...     'completion_tokens': usage_obj.completion_tokens,
        ...     'successful_requests': usage_obj.successful_requests,
        ...     'total_cost': usage_obj.total_cost  # Already calculated!
        ... }
        >>> metrics = calculate_crewai_cost(usage_dict, "gemini-2.5-flash")
        >>> print(f"Cost: ${metrics['cost']:.4f}")
    
    Note:
        If total_cost is not available (old CrewAI versions), falls back to LiteLLM calculation.
    """
    prompt_tokens = usage_metrics.get('prompt_tokens', 0)
    completion_tokens = usage_metrics.get('completion_tokens', 0)
    total_tokens = usage_metrics.get('total_tokens', 0)
    
    # First, try to use the cost already calculated by CrewAI
    cost = usage_metrics.get('total_cost', None)
    
    if cost is not None:
        logger.info(f"💰 CrewAI cost extracted directly: ${cost:.6f} (model: {model_name})")
    else:
        # Calculate via LiteLLM's completion_cost() function
        # LiteLLM caches pricing data internally and is always available when Gemini API is
        logger.info(f"💰 Calculating CrewAI cost via LiteLLM for model: {model_name}")
        
        from litellm import completion_cost
        
        # completion_cost() expects a completion response object
        # Create a minimal response object with the token usage
        completion_response = {
            "model": model_name,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens
            }
        }
        
        cost = completion_cost(completion_response=completion_response)
        logger.info(f"💰 CrewAI cost calculated: ${cost:.6f} (model: {model_name})")
    
    return {
        'llm_calls': usage_metrics.get('successful_requests', 0),
        'cost': round(cost, 6),
        'tokens': total_tokens,
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens
    }


# Re-export WorkflowMetrics for backward compatibility
__all__ = [
    'WorkflowMetrics',
    'WorkflowMetricsCollector',
    'get_workflow_metrics_collector',
    'count_tokens',
    'calculate_crewai_cost',
]
