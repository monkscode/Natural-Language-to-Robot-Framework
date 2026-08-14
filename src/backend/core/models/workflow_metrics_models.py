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

class KeywordSearchStats(BaseModel):
    """Keyword search performance metrics."""
    calls: int = 0
    total_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    returned_keywords: List[str] = Field(default_factory=list)


class PatternLearningStats(BaseModel):
    """Pattern learning metrics."""
    prediction_used: bool = False
    predicted_keywords_count: int = 0


class ContextReductionStats(BaseModel):
    """Context reduction metrics."""
    baseline_tokens: int = 0
    optimized_tokens: int = 0
    reduction_percentage: float = 0.0


class OptimizationMetrics(BaseModel):
    """Combined optimization metrics."""
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
    # None when the user query named no URL (extract_url_from_query returns
    # None rather than guessing a domain — Task 14).
    url: Optional[str] = None
    
    # Overall metrics (totals)
    total_llm_calls: int
    total_cost: float
    execution_time: float
    
    # Optional session tracking
    session_id: Optional[str] = None
    
    # Per-element approach metrics for pattern analysis
    element_approach_metrics: Optional[List[Dict[str, Any]]] = None

    # identify_s phase breakdown (2026-07-26 efficiency check). Typically
    # submit_s, queue_s, session_setup_s, agent_setup_s, agent_run_s,
    # postprocess_s, poll_wait_s — but the browser service owns the span names
    # and is versioned separately, so the set is not fixed. None on failed runs
    # and pre-2026-07 rows.
    #
    # NOT a partition: poll_wait_s overlaps every service-side span, so summing
    # these does not reconstruct identify_s.
    #
    # Dict[str, Any], matching agent_diagnostics below and the sibling
    # element_approach_metrics. Dict[str, float] rejects a None the moment the
    # service adopts the "None means not measured" convention it already uses
    # for llm_coverage_gap — and because WorkflowMetrics is built inside a
    # try/except that swallows ValidationError, that would silently discard the
    # ENTIRE metrics row (cost, tokens, elements), not just the timings.
    phase_timings: Optional[Dict[str, Any]] = None

    # Agent-history diagnostics: dom_elements_max, dom_elements_median,
    # llm_429_count, retry_lost_s, llm_total_s, llm_max_s, llm_calls_actual,
    # steps_total_s, llm_coverage_gap. Historical rows also carry agent_steps.
    agent_diagnostics: Optional[Dict[str, Any]] = None

    # Which model produced the numbers on this row, and through which provider.
    # A cost or token figure is not comparable across a model change without
    # them, and neither is recoverable elsewhere: llm_traces.model gives the
    # NAME but LiteLLM strips the provider prefix before the success callback
    # sees it, so rows read 'gemini-3.5-flash', never 'vertex_ai/...'. The
    # provider also decides the endpoint, the quota pool and the pricing path.
    # None on pre-2026-08 rows.
    model_provider: Optional[str] = None
    model_name: Optional[str] = None

    # Wall time of the whole generation run, measured in
    # run_agentic_workflow. NOT execution_time above, which is
    # browser_metrics['execution_time'] — the browser-use figure that
    # workflow_service passes straight through. None on pre-2026-08 rows.
    workflow_duration_s: Optional[float] = None

    # Deterministic dryrun gate outcome (dryrun_service.validate_and_repair):
    # 'passed' | 'failed' | 'skipped' | 'unverified'. attempts counts every
    # dryrun invoked, repairs every repair invoked — both counted BEFORE the
    # call, so an attempt that times out or raises is still reported.
    dryrun_status: Optional[str] = None
    dryrun_attempts: Optional[int] = None
    dryrun_repairs: Optional[int] = None

    # Per-crew-stage duration and LLM usage, keyed 'planner' / 'assembler'
    # (declared by crew.py via StageMetricsCallback.mark_stage), plus 'repair'
    # when the dryrun gate re-prompted. Each value carries duration_s,
    # llm_calls, prompt_tokens, completion_tokens, tokens, cost.
    #
    # Dict[str, Any] values for the same reason as phase_timings: WorkflowMetrics
    # is built inside a try/except that swallows ValidationError, so a strict
    # value type would discard the ENTIRE row — cost, tokens, elements — over
    # one unmeasured stage timing.
    crew_stage_metrics: Optional[Dict[str, Any]] = None

    # Guardrail invocations per attachment site, keyed 'assembly_output' /
    # 'repair_output'. CrewAI retries a task internally when a guardrail returns
    # (False, ...), so >1 means the assembler needed re-prompting.
    guardrail_attempts: Optional[Dict[str, int]] = None


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
    keyword_search_stats: Optional[Dict[str, Any]] = None
    pattern_learning_stats: Optional[Dict[str, Any]] = None
    context_reduction: Optional[Dict[str, Any]] = None

    # LLM output cleaning metrics (per-workflow, never cross-contaminated)
    llm_cleaning_stats: Optional[Dict[str, Any]] = None

    # True when optimization init failed and this run proceeded without hints.
    optimization_fallback_used: bool = False

    @field_validator('timestamp', mode='before')
    @classmethod
    def parse_timestamp(cls, v) -> datetime:
        """Handle both datetime objects and ISO format strings."""
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v
    
    def model_post_init(self, __context):
        """Initialize default values for optimization metrics."""
        if self.keyword_search_stats is None:
            self.keyword_search_stats = {
                "calls": 0,
                "total_latency_ms": 0.0,
                "avg_latency_ms": 0.0,
                "returned_keywords": [],
            }
        
        if self.pattern_learning_stats is None:
            self.pattern_learning_stats = {
                "prediction_used": False,
                "predicted_keywords_count": 0,
            }
        
        if self.context_reduction is None:
            self.context_reduction = {
                "baseline_tokens": 0,
                "optimized_tokens": 0,
                "reduction_percentage": 0.0
            }

        if self.llm_cleaning_stats is None:
            self.llm_cleaning_stats = {
                "total_responses": 0,
                "cleaned_responses": 0,
                "clean_rate": 0.0,
                "formatting_errors_detected": 0,
                "formatting_errors_recovered": 0,
            }
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with ISO format timestamp."""
        data = self.model_dump()
        data['timestamp'] = self.timestamp.isoformat()
        
        # Add optimization metrics section for storage format
        data['optimization'] = {
            'keyword_search': self.keyword_search_stats,
            'pattern_learning': self.pattern_learning_stats,
            'context_reduction': self.context_reduction,
            'llm_cleaning': self.llm_cleaning_stats,
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
            data.setdefault('keyword_search_stats', opt.get('keyword_search'))
            data.setdefault('pattern_learning_stats', opt.get('pattern_learning'))
            data.setdefault('context_reduction', opt.get('context_reduction'))
            data.setdefault('llm_cleaning_stats', opt.get('llm_cleaning'))
        
        return cls.model_validate(data)
    
    # Tracking methods
    # NOTE: track_keyword_search was removed with the retired keyword-search
    # tool (its sole caller; 0 recorded calls ever). The keyword_search_stats
    # FIELD stays — historical rows carry it and the frontend column reads it.
    def track_pattern_learning(self, predicted: bool, keyword_count: int) -> None:
        """Track pattern learning usage."""
        self.pattern_learning_stats["prediction_used"] = predicted
        self.pattern_learning_stats["predicted_keywords_count"] = keyword_count
    
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

    # Optimization metrics — previously stored in JSONL but omitted from API response
    keyword_search_stats: Optional[Dict[str, Any]] = None
    pattern_learning_stats: Optional[Dict[str, Any]] = None
    context_reduction: Optional[Dict[str, Any]] = None

    # LLM output cleaning metrics — per-workflow, accurate under concurrent load
    llm_cleaning_stats: Optional[Dict[str, Any]] = None

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
            element_approach_metrics=m.element_approach_metrics,
            phase_timings=m.phase_timings,
            agent_diagnostics=m.agent_diagnostics,
            model_provider=m.model_provider,
            model_name=m.model_name,
            workflow_duration_s=m.workflow_duration_s,
            dryrun_status=m.dryrun_status,
            dryrun_attempts=m.dryrun_attempts,
            dryrun_repairs=m.dryrun_repairs,
            crew_stage_metrics=m.crew_stage_metrics,
            guardrail_attempts=m.guardrail_attempts,
            keyword_search_stats=m.keyword_search_stats,
            pattern_learning_stats=m.pattern_learning_stats,
            context_reduction=m.context_reduction,
            llm_cleaning_stats=m.llm_cleaning_stats,
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
