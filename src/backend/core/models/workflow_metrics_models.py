"""
Pydantic models for workflow metrics.

These models provide:
1. Shared base classes to eliminate duplication
2. Built-in validation via Pydantic
3. Backward compatibility via model_config (extra='ignore' removes obsolete fields automatically)
4. Clean serialization/deserialization
"""

from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator


# ============================================================================
# Token Breakdown Models (shared components)
# ============================================================================

class CrewAITokenBreakdown(BaseModel):
    """CrewAI token usage breakdown."""
    crewai_llm_calls: int = 0
    crewai_cost: float = 0.0
    crewai_tokens: int = 0
    crewai_prompt_tokens: int = 0
    crewai_completion_tokens: int = 0


class BrowserUseTokenBreakdown(BaseModel):
    """Browser-use token usage breakdown."""
    browser_use_llm_calls: int = 0
    browser_use_cost: float = 0.0
    browser_use_tokens: int = 0
    browser_use_prompt_tokens: int = 0
    browser_use_completion_tokens: int = 0
    browser_use_cached_tokens: int = 0


class BrowserUseElementMetrics(BaseModel):
    """Browser-use element processing metrics."""
    total_elements: int = 0
    successful_elements: int = 0
    failed_elements: int = 0
    success_rate: float = 0.0
    avg_llm_calls_per_element: float = 0.0
    avg_cost_per_element: float = 0.0
    custom_actions_enabled: bool = False
    custom_action_usage_count: int = 0


# ============================================================================
# Optimization Metrics Models
# ============================================================================

class TokenUsageStats(BaseModel):
    """Per-agent token usage tracking."""
    step_planner: int = 0
    element_identifier: int = 0
    code_assembler: int = 0
    code_validator: int = 0
    total: int = 0


class KeywordSearchStats(BaseModel):
    """Keyword search performance metrics."""
    calls: int = 0
    total_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    returned_keywords: List[str] = Field(default_factory=list)
    accuracy: float = 0.0


class PatternLearningStats(BaseModel):
    """Pattern learning metrics."""
    prediction_used: bool = False
    predicted_keywords_count: int = 0
    prediction_accuracy: float = 0.0


class ContextReductionStats(BaseModel):
    """Context reduction metrics."""
    baseline_tokens: int = 0
    optimized_tokens: int = 0
    reduction_percentage: float = 0.0


class OptimizationMetrics(BaseModel):
    """Combined optimization metrics."""
    token_usage: Optional[TokenUsageStats] = None
    keyword_search: Optional[KeywordSearchStats] = None
    pattern_learning: Optional[PatternLearningStats] = None
    context_reduction: Optional[ContextReductionStats] = None


# ============================================================================
# Main Workflow Metrics Model
# ============================================================================

class WorkflowMetricsBase(
    CrewAITokenBreakdown,
    BrowserUseTokenBreakdown,
    BrowserUseElementMetrics
):
    """
    Base model for workflow metrics combining all breakdown components.
    
    Inherits from:
    - CrewAITokenBreakdown: CrewAI-specific token metrics
    - BrowserUseTokenBreakdown: Browser-use token metrics
    - BrowserUseElementMetrics: Element processing metrics
    """
    # Core identifiers
    workflow_id: str
    url: str
    
    # Overall metrics (totals)
    total_llm_calls: int
    total_cost: float
    execution_time: float
    
    # Optional session tracking
    session_id: Optional[str] = None


class WorkflowMetrics(WorkflowMetricsBase):
    """
    Complete workflow metrics model with timestamp and optimization tracking.
    
    This is the main model used for storing and retrieving workflow metrics.
    Uses Pydantic's extra='ignore' to automatically handle obsolete fields
    during deserialization (no manual dict.pop needed).
    """
    model_config = ConfigDict(
        extra='ignore',  # Automatically ignores obsolete fields like 'browser_use_actual_cost'
        validate_assignment=True,
    )
    
    timestamp: datetime
    
    # Optimization metrics
    token_usage: Optional[Dict[str, int]] = None
    keyword_search_stats: Optional[Dict[str, Any]] = None
    pattern_learning_stats: Optional[Dict[str, Any]] = None
    context_reduction: Optional[Dict[str, Any]] = None
    
    @field_validator('timestamp', mode='before')
    @classmethod
    def parse_timestamp(cls, v) -> datetime:
        """Handle both datetime objects and ISO format strings."""
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v
    
    def model_post_init(self, __context):
        """Initialize default values for optimization metrics."""
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
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with ISO format timestamp."""
        data = self.model_dump()
        data['timestamp'] = self.timestamp.isoformat()
        
        # Add optimization metrics section for storage format
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
        Create from dictionary with automatic backward compatibility.
        
        Pydantic's extra='ignore' handles obsolete fields automatically.
        We only need to handle field renames and structural changes.
        """
        data = data.copy()
        
        # Handle field rename: browser_use_actual_cost -> browser_use_cost
        if 'browser_use_actual_cost' in data:
            if abs(data.get('browser_use_cost', 0.0)) < 1e-9:
                data['browser_use_cost'] = data['browser_use_actual_cost']
            # Note: extra='ignore' will automatically drop browser_use_actual_cost
        
        # Handle old format without CrewAI breakdown
        if 'crewai_llm_calls' not in data:
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
        
        # Handle optimization metrics from storage format
        if 'optimization' in data:
            opt = data.pop('optimization')
            data.setdefault('token_usage', opt.get('token_usage'))
            data.setdefault('keyword_search_stats', opt.get('keyword_search'))
            data.setdefault('pattern_learning_stats', opt.get('pattern_learning'))
            data.setdefault('context_reduction', opt.get('context_reduction'))
        
        return cls.model_validate(data)
    
    # Tracking methods
    def track_token_usage(self, agent_name: str, token_count: int) -> None:
        """Track token usage per agent."""
        if agent_name in self.token_usage:
            self.token_usage[agent_name] += token_count
            self.token_usage["total"] += token_count
    
    def track_keyword_search(self, latency_ms: float, returned_keywords: List[str]) -> None:
        """Track keyword search performance."""
        self.keyword_search_stats["calls"] += 1
        self.keyword_search_stats["total_latency_ms"] += latency_ms
        self.keyword_search_stats["avg_latency_ms"] = (
            self.keyword_search_stats["total_latency_ms"] / 
            self.keyword_search_stats["calls"]
        )
        self.keyword_search_stats["returned_keywords"].extend(returned_keywords)
    
    def track_pattern_learning(self, predicted: bool, keyword_count: int, accuracy: float = 0.0) -> None:
        """Track pattern learning usage."""
        self.pattern_learning_stats["prediction_used"] = predicted
        self.pattern_learning_stats["predicted_keywords_count"] = keyword_count
        self.pattern_learning_stats["prediction_accuracy"] = accuracy
    
    def track_context_reduction(self, baseline: int, optimized: int) -> None:
        """Track context reduction metrics."""
        self.context_reduction["baseline_tokens"] = baseline
        self.context_reduction["optimized_tokens"] = optimized
        if baseline > 0:
            reduction = ((baseline - optimized) / baseline) * 100
            self.context_reduction["reduction_percentage"] = round(reduction, 2)


# ============================================================================
# API Request/Response Models
# ============================================================================

class WorkflowMetricsResponse(WorkflowMetricsBase):
    """API response model for workflow metrics."""
    timestamp: str  # ISO format string for JSON serialization
    
    @classmethod
    def from_workflow_metrics(cls, m: WorkflowMetrics) -> 'WorkflowMetricsResponse':
        """Convert from WorkflowMetrics model."""
        return cls(
            workflow_id=m.workflow_id,
            timestamp=m.timestamp.isoformat(),
            url=m.url,
            total_llm_calls=m.total_llm_calls,
            total_cost=m.total_cost,
            execution_time=m.execution_time,
            crewai_llm_calls=m.crewai_llm_calls,
            crewai_cost=m.crewai_cost,
            crewai_tokens=m.crewai_tokens,
            crewai_prompt_tokens=m.crewai_prompt_tokens,
            crewai_completion_tokens=m.crewai_completion_tokens,
            browser_use_llm_calls=m.browser_use_llm_calls,
            browser_use_cost=m.browser_use_cost,
            browser_use_tokens=m.browser_use_tokens,
            browser_use_prompt_tokens=m.browser_use_prompt_tokens,
            browser_use_completion_tokens=m.browser_use_completion_tokens,
            browser_use_cached_tokens=m.browser_use_cached_tokens,
            total_elements=m.total_elements,
            successful_elements=m.successful_elements,
            failed_elements=m.failed_elements,
            success_rate=m.success_rate,
            avg_llm_calls_per_element=m.avg_llm_calls_per_element,
            avg_cost_per_element=m.avg_cost_per_element,
            custom_actions_enabled=m.custom_actions_enabled,
            custom_action_usage_count=m.custom_action_usage_count,
            session_id=m.session_id,
        )


class RecordMetricsRequest(WorkflowMetricsBase):
    """API request model for recording workflow metrics."""
    # All fields inherited from WorkflowMetricsBase with defaults


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


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    # Base components
    'CrewAITokenBreakdown',
    'BrowserUseTokenBreakdown', 
    'BrowserUseElementMetrics',
    'WorkflowMetricsBase',
    
    # Optimization metrics
    'TokenUsageStats',
    'KeywordSearchStats',
    'PatternLearningStats',
    'ContextReductionStats',
    'OptimizationMetrics',
    
    # Main models
    'WorkflowMetrics',
    'WorkflowMetricsResponse',
    'RecordMetricsRequest',
    'AggregateMetricsResponse',
]
