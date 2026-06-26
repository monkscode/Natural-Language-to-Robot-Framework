"""
LLM Observability — OTel instrumentation with pluggable trace backends.

Initializes OpenLLMetry (Traceloop SDK) which auto-instruments:
- LiteLLM calls (NL-to-RF pipeline via CleanedLLMWrapper)
- LangChain providers (browser-service via langchain-google-genai)

Trace export backends:
- "postgres": Built-in Postgres trace store (the consolidated DB; default)
- "grafana": Grafana Tempo via OTLP
- "otlp": Any OTLP-compatible endpoint (Langfuse, Jaeger, Datadog)
- "none": Disabled

Must be called BEFORE importing CrewAI/LiteLLM — OpenLLMetry patches at import time.

Referenced by: main.py (startup), workflow_service.py (span creation)
Depends on: core/config.py, core/trace_store.py
"""
import contextlib
import logging
import os
import sys

logger = logging.getLogger(__name__)


def init_observability() -> bool:
    """
    Initialize LLM observability. Must be called before any LLM imports.

    Returns True if tracing was enabled, False otherwise.
    """
    from src.backend.core.config import settings

    if settings.OBSERVABILITY_BACKEND == "none":
        logger.info("[OBSERVABILITY] Tracing disabled (OBSERVABILITY_BACKEND=none)")
        return False

    if "litellm" in sys.modules:
        logger.warning(
            "[OBSERVABILITY] litellm was imported BEFORE init — OpenLLMetry patches may not work. "
            "Ensure init_observability() is called before importing endpoints."
        )

    try:
        from traceloop.sdk import Traceloop

        # Always capture full prompt/response — customers consent to full data collection.
        os.environ["TRACELOOP_TRACE_CONTENT"] = "true"

        exporter = _get_exporter(settings.OBSERVABILITY_BACKEND, settings.OTLP_ENDPOINT)

        Traceloop.init(
            app_name="mark1-nlrf",
            disable_batch=False,
            exporter=exporter,
            traceloop_sync_enabled=False,  # Don't phone home to Traceloop cloud
        )

        logger.info(
            "[OBSERVABILITY] OpenLLMetry initialized — backend=%s, prompts=captured",
            settings.OBSERVABILITY_BACKEND,
        )
        return True

    except ImportError:
        logger.warning(
            "[OBSERVABILITY] traceloop-sdk not installed — tracing disabled. "
            "Install with: pip install traceloop-sdk"
        )
        return False
    except Exception as e:
        logger.warning("[OBSERVABILITY] Init failed (non-fatal): %s", e)
        return False


def _get_exporter(backend: str, otlp_endpoint: str):
    """Create the appropriate OTel span exporter for the configured backend."""
    if backend == "postgres":
        # Reuse the module-level singleton so the OTel BatchSpanProcessor and
        # the LiteLLM success-callback path share ONE PostgresSpanExporter
        # instance (one pool) instead of two objects writing independently.
        from src.backend.core.trace_store import get_trace_store
        store = get_trace_store()
        if store is None:
            raise RuntimeError("Postgres trace store could not be initialized")
        return store

    if backend in ("grafana", "otlp"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")

    raise ValueError(f"Unknown OBSERVABILITY_BACKEND: {backend!r}. Must be postgres, grafana, otlp, or none.")


def create_workflow_span(
    workflow_id: str,
    user_query: str,
    model_provider: str,
    model_name: str,
    library_type: str = "browser",
    org_id: str | None = None,
    user_id: str | None = None,
):
    """
    Context manager that wraps the run_crew() call in a parent OTel span.

    Sets OTel Baggage so workflow_id propagates to all child spans (including
    auto-instrumented LiteLLM spans). Without Baggage, child spans lack
    workflow_id and cannot be queried by workflow in the trace store.

    Usage:
        with create_workflow_span(workflow_id, query, provider, model):
            result = run_crew(query, provider, model)

    If observability is not initialized (backend=none or import error), this
    returns a no-op context manager so the call site never needs an if-check.
    """
    try:
        from opentelemetry import baggage, context, trace

        tracer = trace.get_tracer("mark1.workflow")
        attrs = {
            "workflow.id": workflow_id,
            "workflow.query": user_query,
            "workflow.model_provider": model_provider,
            "workflow.model_name": model_name,
            "workflow.library_type": library_type,
        }
        if org_id:
            attrs["workflow.org_id"] = org_id
        if user_id:
            attrs["workflow.user_id"] = user_id
        span = tracer.start_span(name="test-generation-workflow", attributes=attrs)

        # Set workflow_id as OTel Baggage — propagates to ALL child spans and
        # across service boundaries via HTTP headers (traceparent).
        ctx = baggage.set_baggage("workflow.id", workflow_id)
        if org_id:
            ctx = baggage.set_baggage("workflow.org_id", org_id, context=ctx)
        # user_id stays a local span attribute (above) but is deliberately NOT
        # put in baggage: baggage propagates via HTTP headers to every downstream
        # service, and nothing consumes workflow.user_id there — so it would only
        # leak a raw user identifier across the service boundary for no benefit.
        ctx = trace.set_span_in_context(span, ctx)

        @contextlib.contextmanager
        def _managed_span():
            token = context.attach(ctx)
            try:
                yield span
            finally:
                span.end()
                context.detach(token)

        return _managed_span()

    except ImportError:
        return contextlib.nullcontext()
    except Exception as e:
        logger.warning("[OBSERVABILITY] Failed to create workflow span (non-fatal): %s", e)
        return contextlib.nullcontext()
