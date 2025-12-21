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
    """Metrics for a single workflow execution with CrewAI and Browser-use breakdown."""
    workflow_id: str
    timestamp: datetime
    url: str
    
    # Overall metrics (totals)
    total_llm_calls: int
    total_cost: float
    execution_time: float
    
    # CrewAI breakdown
    crewai_llm_calls: int = 0
    crewai_cost: float = 0.0
    crewai_tokens: int = 0
    crewai_prompt_tokens: int = 0
    crewai_completion_tokens: int = 0
    
    # NEW: Per-agent token tracking
    per_agent_tokens: Dict[str, Dict[str, int]] = None
    per_task_tokens: Dict[str, Dict[str, int]] = None
    
    # Browser-use breakdown
    browser_use_llm_calls: int = 0
    browser_use_cost: float = 0.0
    browser_use_tokens: int = 0
    
    # Browser-use specific metrics
    total_elements: int = 0
    successful_elements: int = 0
    failed_elements: int = 0
    success_rate: float = 0.0
    avg_llm_calls_per_element: float = 0.0
    avg_cost_per_element: float = 0.0
    custom_actions_enabled: bool = False
    custom_action_usage_count: int = 0
    session_id: Optional[str] = None
    
    # Optimization metrics (NEW)
    token_usage: Dict[str, int] = None
    keyword_search_stats: Dict[str, Any] = None
    pattern_learning_stats: Dict[str, Any] = None
    context_reduction: Dict[str, Any] = None
    
    def __post_init__(self):
        """Initialize optimization metrics with default values if not provided."""
        if self.per_agent_tokens is None:
            self.per_agent_tokens = {}
        
        if self.per_task_tokens is None:
            self.per_task_tokens = {}
        
        if self.token_usage is None:
            self.token_usage = {
                "step_planner": 0,
                "element_identifier": 0,
                "code_assembler": 0,
                "code_validator": 0,
                "total": 0
            }
        
        if self.keyword_search_stats is None:
            self.keyword_search_stats = {
                "calls": 0,
                "total_latency_ms": 0.0,
                "avg_latency_ms": 0.0,
                "returned_keywords": [],
                "accuracy": 0.0
            }
        
        if self.pattern_learning_stats is None:
            self.pattern_learning_stats = {
                "prediction_used": False,
                "predicted_keywords_count": 0,
                "prediction_accuracy": 0.0
            }
        
        if self.context_reduction is None:
            self.context_reduction = {
                "baseline_tokens": 0,
                "optimized_tokens": 0,
                "reduction_percentage": 0.0
            }
    
    def track_token_usage(self, agent_name: str, token_count: int) -> None:
        """
        Track token usage per agent.
        
        Args:
            agent_name: Name of the agent (step_planner, element_identifier, code_assembler, code_validator)
            token_count: Number of tokens used by the agent
        """
        if agent_name in self.token_usage:
            self.token_usage[agent_name] = token_count
            self.token_usage["total"] = sum(
                v for k, v in self.token_usage.items() if k != "total"
            )
    
    def track_keyword_search(self, latency_ms: float, returned_keywords: List[str]) -> None:
        """
        Track keyword search tool usage.
        
        Args:
            latency_ms: Search latency in milliseconds
            returned_keywords: List of keyword names returned by search
        """
        self.keyword_search_stats["calls"] += 1
        self.keyword_search_stats["total_latency_ms"] += latency_ms
        self.keyword_search_stats["avg_latency_ms"] = (
            self.keyword_search_stats["total_latency_ms"] / 
            self.keyword_search_stats["calls"]
        )
        # Store returned keywords for accuracy calculation later
        self.keyword_search_stats["returned_keywords"].extend(returned_keywords)
    
    def track_pattern_learning(self, predicted: bool, keyword_count: int, accuracy: float = 0.0) -> None:
        """
        Track pattern learning usage.
        
        Args:
            predicted: Whether prediction was used
            keyword_count: Number of predicted keywords
            accuracy: Prediction accuracy (0.0-1.0)
        """
        self.pattern_learning_stats["prediction_used"] = predicted
        self.pattern_learning_stats["predicted_keywords_count"] = keyword_count
        self.pattern_learning_stats["prediction_accuracy"] = accuracy
    
    def track_context_reduction(self, baseline: int, optimized: int) -> None:
        """
        Track context size reduction.
        
        Args:
            baseline: Baseline token count (without optimization)
            optimized: Optimized token count (with optimization)
        """
        self.context_reduction["baseline_tokens"] = baseline
        self.context_reduction["optimized_tokens"] = optimized
        self.context_reduction["reduction_percentage"] = (
            ((baseline - optimized) / baseline * 100) if baseline > 0 else 0.0
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with ISO format timestamp."""
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        
        # Add optimization metrics section
        data['optimization'] = {
            'token_usage': self.token_usage,
            'keyword_search': self.keyword_search_stats,
            'pattern_learning': self.pattern_learning_stats,
            'context_reduction': self.context_reduction
        }
        
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkflowMetrics':
        """
        Create from dictionary with ISO format timestamp.
        Handles both old and new format for backward compatibility.
        """
        data = data.copy()
        
        # Convert timestamp string to datetime
        if isinstance(data.get('timestamp'), str):
            data['timestamp'] = datetime.fromisoformat(data['timestamp'])
        
        # Remove obsolete fields from very old format
        data.pop('execution_id', None)  # Old field, no longer used
        
        # Backward compatibility: Handle old format without breakdown
        if 'crewai_llm_calls' not in data:
            # Old format - all metrics were from browser-use
            old_total_llm_calls = data.get('total_llm_calls', 0)
            old_total_cost = data.get('total_cost', 0.0)
            
            # Set CrewAI metrics to 0 (didn't exist in old format)
            data['crewai_llm_calls'] = 0
            data['crewai_cost'] = 0.0
            data['crewai_tokens'] = 0
            data['crewai_prompt_tokens'] = 0
            data['crewai_completion_tokens'] = 0
            
            # Set browser-use metrics to old totals
            data['browser_use_llm_calls'] = old_total_llm_calls
            data['browser_use_cost'] = old_total_cost
            data['browser_use_tokens'] = 0  # Not tracked in old format
            
            # Keep totals as-is
            data['total_llm_calls'] = old_total_llm_calls
            data['total_cost'] = old_total_cost
        
        # Ensure all required fields have defaults
        data.setdefault('crewai_llm_calls', 0)
        data.setdefault('crewai_cost', 0.0)
        data.setdefault('crewai_tokens', 0)
        data.setdefault('crewai_prompt_tokens', 0)
        data.setdefault('crewai_completion_tokens', 0)
        data.setdefault('browser_use_llm_calls', 0)
        data.setdefault('browser_use_cost', 0.0)
        data.setdefault('browser_use_tokens', 0)
        data.setdefault('total_elements', 0)
        data.setdefault('successful_elements', 0)
        data.setdefault('failed_elements', 0)
        data.setdefault('success_rate', 0.0)
        data.setdefault('avg_llm_calls_per_element', 0.0)
        data.setdefault('avg_cost_per_element', 0.0)
        data.setdefault('custom_actions_enabled', False)
        data.setdefault('custom_action_usage_count', 0)
        data.setdefault('session_id', None)
        
        # Handle optimization metrics (backward compatibility)
        # Check if optimization section exists in data
        if 'optimization' in data:
            opt = data.pop('optimization')
            data.setdefault('token_usage', opt.get('token_usage'))
            data.setdefault('keyword_search_stats', opt.get('keyword_search'))
            data.setdefault('pattern_learning_stats', opt.get('pattern_learning'))
            data.setdefault('context_reduction', opt.get('context_reduction'))
        else:
            # Set defaults for optimization metrics if not present
            data.setdefault('token_usage', None)
            data.setdefault('keyword_search_stats', None)
            data.setdefault('pattern_learning_stats', None)
            data.setdefault('context_reduction', None)
        
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
    
    # Simple word-based estimation
    # Split on whitespace and count words
    words = text.split()
    
    # Approximate: 1 word ≈ 1.33 tokens (or 0.75 words per token)
    # This is a rough estimate based on typical English text
    estimated_tokens = int(len(words) * 1.33)
    
    return estimated_tokens


def calculate_crewai_cost(usage_metrics: dict, model_name: str = "gemini-2.0-flash-exp") -> dict:
    """
    Calculate cost from CrewAI usage_metrics.
    
    Args:
        usage_metrics: Usage metrics dict from CrewAI with keys:
            - total_tokens: Total tokens used
            - prompt_tokens: Input tokens
            - completion_tokens: Output tokens
            - successful_requests: Number of successful LLM calls
        model_name: Model used for pricing
    
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
        ...     'successful_requests': usage_obj.successful_requests
        ... }
        >>> metrics = calculate_crewai_cost(usage_dict, "gemini-2.0-flash-exp")
        >>> print(f"Cost: ${metrics['cost']:.4f}")
    """
    # Pricing per 1K tokens (update based on your model)
    PRICING = {
        "gemini-2.0-flash-exp": {"input": 0.00015, "output": 0.0006},
        "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
        "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
        "gemini-1.5-flash-8b": {"input": 0.0000375, "output": 0.00015},
    }
    
    pricing = PRICING.get(model_name, PRICING["gemini-2.0-flash-exp"])
    
    prompt_tokens = usage_metrics.get('prompt_tokens', 0)
    completion_tokens = usage_metrics.get('completion_tokens', 0)
    total_tokens = usage_metrics.get('total_tokens', 0)
    
    cost = (
        (prompt_tokens / 1000) * pricing["input"] +
        (completion_tokens / 1000) * pricing["output"]
    )
    
    return {
        'llm_calls': usage_metrics.get('successful_requests', 0),
        'cost': round(cost, 6),
        'tokens': total_tokens,
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens
    }
