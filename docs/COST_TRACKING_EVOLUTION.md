# Cost Tracking Evolution

This document tracks the evolution of cost calculation in the browser-use metrics system.

## Timeline

### Phase 1: Estimated Cost (Deprecated as of 2025-12-27)

Initially, browser-use costs were **estimated** based on LLM call count, not actual token usage.

```plantuml
@startuml Cost Estimation Flow (Legacy)
!theme plain

skinparam backgroundColor #FEFEFE
skinparam defaultFontName Arial

title Browser-Use Cost Estimation Flow (Legacy - Deprecated)

actor "Browser-use\nAgent" as Agent
participant "workflow.py" as Workflow
participant "browser_use_tool.py" as Tool
participant "workflow_service.py" as Service
database "workflow_metrics.jsonl" as Storage

== Workflow Execution ==
Agent -> Workflow: run()
activate Workflow

Workflow -> Workflow: Count LLM calls
note right
  llm_call_count = len(agent_result.history)
  Example: 4 calls
end note

Workflow -> Workflow: Estimate cost
note right
  **ESTIMATION FORMULA:**
  avg_tokens_per_call = 1000
  cost_per_1k_tokens = 0.00015
  estimated_cost_per_call = 
    (1000 / 1000) × 0.00015 = 0.00015
  
  total_estimated_cost = 
    llm_call_count × 0.00015
  = 4 × 0.00015 = 0.0006
  
  **Problem:** Ignores actual token usage!
  Real usage might be 35,956 tokens
  but we only estimate 4,000 tokens
end note

Workflow -> Tool: Return summary
activate Tool
note over Tool
  summary = {
    'total_llm_calls': 4,
    'estimated_total_cost': 0.0006
  }
end note

Tool -> Tool: Extract metrics
note right
  browser_metrics = {
    'llm_calls': 4,
    'cost': 0.0006  ← Estimated!
  }
end note

Tool -> Service: Store to temp file
deactivate Tool

Service -> Service: Merge metrics
note right
  total_cost = crewai_cost + browser_cost
  = 0.0063 + 0.0006 = 0.0069
  
  **Actual should be:**
  = 0.0063 + 0.0116 = 0.0179
  
  **Underestimated by 63%!**
end note

Service -> Storage: Record metrics
deactivate Workflow

@enduml
```

#### Problems with Estimation

1. **Inaccurate**: Assumed 1000 tokens per call, but actual was ~9000 tokens per call
2. **No vision token accounting**: Vision models use significantly more tokens for screenshots
3. **No cached token accounting**: Ignored prompt caching benefits
4. **Misleading metrics**: Made browser-use appear cheaper than it actually was

#### Example Comparison

| Metric | Estimated | Actual | Error |
|--------|-----------|--------|-------|
| Tokens per call | ~1,000 | ~8,989 | -89% |
| Total tokens | ~4,000 | 35,956 | -89% |
| Cost | $0.0006 | $0.0116 | -95% |

---

### Phase 2: Actual Cost (Current - 2025-12-27)

Now using browser-use 0.9.7's built-in `TokenCostService` with real token tracking.

```plantuml
@startuml Cost Tracking Flow (Current)
!theme plain

skinparam backgroundColor #FEFEFE
skinparam defaultFontName Arial

title Browser-Use Cost Tracking Flow (Current)

actor "Browser-use\nAgent" as Agent
participant "workflow.py" as Workflow
participant "TokenCostService" as TokenService
participant "browser_use_tool.py" as Tool
participant "workflow_service.py" as Service
database "workflow_metrics.jsonl" as Storage

== Workflow Execution ==
Agent -> Workflow: run(calculate_cost=True)
activate Workflow

Agent --> TokenService: Track token usage
activate TokenService
note right TokenService
  For each LLM call:
  - Count prompt tokens
  - Count completion tokens
  - Count cached tokens
  - Fetch pricing from LiteLLM
  - Calculate actual cost
end note

Workflow -> Agent: agent_result
note right
  agent_result.usage = UsageSummary {
    total_prompt_tokens: 35,230
    total_completion_tokens: 726
    total_tokens: 35,956
    total_prompt_cached_tokens: 3,109
    total_cost: 0.01163784
  }
end note

Workflow -> Workflow: Extract actual usage
note right
  **ACTUAL TOKEN EXTRACTION:**
  token_usage = {
    'input_tokens': 35,230,
    'output_tokens': 726,
    'total_tokens': 35,956,
    'cached_tokens': 3,109,
    'actual_cost': 0.01163784
  }
  
  **Pricing (Gemini 2.5 Flash):**
  - Input: $0.00030 / 1K tokens
  - Output: $0.00120 / 1K tokens
  - Cached: $0.00003 / 1K tokens
end note

Workflow -> Tool: Return summary
activate Tool
note over Tool
  summary = {
    'total_llm_calls': 4,
    'total_tokens': 35,956,
    'input_tokens': 35,230,
    'output_tokens': 726,
    'cached_tokens': 3,109,
    'estimated_total_cost': 0.0006,  ← Keep for reference
    'actual_cost': 0.01163784  ← Real cost!
  }
end note

Tool -> Tool: Extract metrics
note right
  browser_metrics = {
    'llm_calls': 4,
    'cost': 0.0006,  ← Deprecated
    'actual_cost': 0.01163784,  ← Use this!
    'tokens': 35,956,
    'input_tokens': 35,230,
    'output_tokens': 726,
    'cached_tokens': 3,109
  }
end note

Tool -> Service: Store to temp file
deactivate Tool
deactivate TokenService

Service -> Service: Merge metrics
note right
  **Use actual_cost for totals:**
  total_cost = crewai_cost + browser_actual_cost
  = 0.0063 + 0.0116 = 0.0179
  
  **Keep both for transparency:**
  - browser_use_cost: 0.0006 (estimated)
  - browser_use_actual_cost: 0.0116 (real)
end note

Service -> Storage: Record metrics
deactivate Workflow

@enduml
```

#### Benefits of Actual Tracking

1. **Accurate**: Real token counts from Google's API
2. **Transparent**: See prompt vs completion vs cached breakdown
3. **Optimizable**: Identify high-token operations
4. **Trustworthy**: Matches actual billing

#### Current Fields

```python
# WorkflowMetrics dataclass
browser_use_cost: float = 0.0              # Actual cost from browser-use (not estimated)
browser_use_tokens: int = 0                # Total tokens
browser_use_prompt_tokens: int = 0         # Input tokens
browser_use_completion_tokens: int = 0     # Output tokens
browser_use_cached_tokens: int = 0         # Cached tokens
```

---

## Migration Guide

### For API Consumers

**Old way (estimated - removed):**
```python
# This was underestimated and has been removed!
# cost = metrics['browser_use_cost']  # Was 0.0006
```

**Current way:**
```python
# Always uses actual cost from browser-use
cost = metrics['browser_use_cost']  # 0.0116 (actual)
```

### Total Cost Calculation

**Formula:**
```python
total_cost = crewai_cost + browser_use_cost
```

Note: `browser_use_cost` now always contains the **actual cost** from browser-use's TokenCostService, not an estimate.

---

## Configuration

To enable cost tracking in browser-use:

```python
from browser_use import Agent

agent = Agent(
    task="...",
    calculate_cost=True,  # Enable TokenCostService
    # ...
)
```

Or via environment variable:
```bash
BROWSER_USE_CALCULATE_COST=true
```

---

## References

- Browser-use TokenCostService: [tokens/service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/tokens/service.py)
- LiteLLM Pricing Data: [model_prices_and_context_window.json](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
- Gemini Pricing: [Google AI Pricing](https://ai.google.dev/pricing)
