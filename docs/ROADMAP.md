# Roadmap

Mark 1's development roadmap. For detailed design docs and implementation plans, see [FuturePlans/ENHANCEMENT_ROADMAP.md](../FuturePlans/ENHANCEMENT_ROADMAP.md).

**Current Version:** 1.0.0  
**Last Updated:** 2026-04-17

---

## Completed

### Multi-Agent AI System
Specialized CrewAI agents for test generation: Step Planner, Element Identifier, Code Assembler, Code Validator.

**Completed:** Q4 2024

### AI-Powered Element Detection
Vision-based element finding via SmartLocatorFinder — 18+ layered strategies with prioritized fallback chain.

**Completed:** Q4 2024

### Docker Isolation
Isolated test execution in containers. Each test generation runs in its own Docker container.

**Completed:** Q4 2024

### SeleniumLibrary Support
Full support for Robot Framework SeleniumLibrary.

**Completed:** Q4 2024

### Browser Library Support
Support for modern Robot Framework Browser Library (Playwright-based).

**Completed:** Q4 2024

---

## In Progress — Tier 1 (Before Next Customer)

Foundational items that block revenue or represent unacceptable production risk.

### Pin All Dependencies
Lock all 16 dependencies with pip-tools for reproducible builds.

**Status:** Not Started  
**Priority:** Critical  
**Effort:** 0.5 day  
**Design Doc:** [PIN_DEPENDENCIES.md](../FuturePlans/Tier1_Before_Next_Customer/PIN_DEPENDENCIES.md)

### Structured Error Tracking
JSON-structured logging with structlog, OTel trace correlation, and global exception handlers. Zero new infrastructure — replaces text logs with machine-parseable JSON.

**Status:** Not Started  
**Priority:** Critical  
**Effort:** 1 hour  
**Design Doc:** [SENTRY_ERROR_TRACKING.md](../FuturePlans/Tier1_Before_Next_Customer/SENTRY_ERROR_TRACKING.md)

### LLM Observability (OTel-First)
OpenTelemetry instrumentation with OpenLLMetry for per-LLM-call tracing. Zero-infra SQLite default, optional Grafana stack, forward-compatible with any OTel backend.

**Status:** Not Started  
**Priority:** Critical  
**Effort:** 2-3 days  
**Design Doc:** [LANGFUSE_OBSERVABILITY.md](../FuturePlans/Tier1_Before_Next_Customer/LANGFUSE_OBSERVABILITY.md)

---

## Planned — Tier 2 (Future Product Enhancements)

Build when customer demand exists. Not required for alpha/pilot.

### Test Artifact Store
SQLite-backed CRUD API for saving, retrieving, editing, and re-running generated tests. Re-run path bypasses CrewAI entirely. Deferred because users already download `.robot` files — framework-side persistence adds no value until teams need shared test management.

**Status:** Deferred  
**Priority:** High (when needed)  
**Effort:** 1-2 weeks  
**Design Doc:** [TEST_ARTIFACT_STORE.md](../FuturePlans/Tier2_Future_Product/TEST_ARTIFACT_STORE.md)

---

## Planned — Tier 3 (Eval & Quality)

Quality measurement and regression detection for prompts and locator generation.

### Prompt Evaluation Suite
promptfoo-based evaluation with 20 scenarios across 5 categories. Custom assertions for RF structure and locator quality.

**Status:** Not Started  
**Priority:** Medium  
**Effort:** 2-3 days  
**Design Doc:** [PROMPT_EVAL_SUITE.md](../FuturePlans/Tier3_Eval_And_Quality/PROMPT_EVAL_SUITE.md)

### Locator Quality Metrics
LocatorMetricsAccumulator tracking approach distribution, fallback depth, and health thresholds on browser-service.

**Status:** Not Started  
**Priority:** Medium  
**Effort:** 1 day  
**Design Doc:** [LOCATOR_QUALITY_METRICS.md](../FuturePlans/Tier3_Eval_And_Quality/LOCATOR_QUALITY_METRICS.md)

### Playwright MCP Evaluation
1-day spike: compare browser-service locator strategies vs Playwright MCP accessibility tree across 10 test pages.

**Status:** Not Started  
**Priority:** Medium  
**Effort:** 1 day  
**Design Doc:** [PLAYWRIGHT_MCP_EVALUATION.md](../FuturePlans/Tier4_Strategic_Migrations/PLAYWRIGHT_MCP_EVALUATION.md)

---

## Future — Tier 4 (Strategic Migrations)

Larger architectural decisions that require spikes before committing. Planned for post-pilot.

### CrewAI to LangGraph Migration
LangGraph spike with StateGraph and per-node LLM routing. Would eliminate custom output cleaning workarounds.

**Status:** Not Started  
**Priority:** Low  
**Effort:** 1 week (spike)  
**Design Doc:** [CREWAI_TO_LANGGRAPH.md](../FuturePlans/Tier4_Strategic_Migrations/CREWAI_TO_LANGGRAPH.md)

### Postgres + pgvector Migration
Replace SQLite + dual ChromaDB with Postgres + pgvector. Database abstraction layer with phased migration.

**Status:** Not Started  
**Priority:** Low  
**Effort:** 1 week  
**Design Doc:** [POSTGRES_PGVECTOR_MIGRATION.md](../FuturePlans/Tier4_Strategic_Migrations/POSTGRES_PGVECTOR_MIGRATION.md)

---

## Explicitly Out of Scope

See [ENHANCEMENT_ROADMAP.md](../FuturePlans/ENHANCEMENT_ROADMAP.md) for full rationale.

- CLI entry point + JUnit XML output (UI-first during alpha, no customer demand)
- Locator self-healing (requires full test replay, negligible savings over regeneration)
- Knowledge Pack GitHub auto-pull
- NL feedback + auto-triage engine
- Vertex AI migration
- Redis
- Kubernetes
- Frontend rewrite
- Billing system (until confirmed paying customers)

---

*For detailed implementation plans, see [FuturePlans/ENHANCEMENT_ROADMAP.md](../FuturePlans/ENHANCEMENT_ROADMAP.md). For strategic context, see [CTO.md](../CTO.md).*
