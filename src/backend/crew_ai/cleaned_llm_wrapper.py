"""
LLM Wrapper - LLM instantiation with output cleaning for CrewAI agents.

This module provides a single CleanedLLMWrapper that works for all providers:
- Gemini  models (Google AI Studio): model="gemini/gemini-2.5-flash"   (prefix added by get_llm())
- Vertex  models (Google Cloud):     model="vertex_ai/gemini-2.5-flash" (prefix added by get_llm())
- Local   models (Ollama):           model="ollama/<model_name>"         (prefix added by get_llm())

Callers always pass bare model names (e.g. "gemini-2.5-flash") via ONLINE_MODEL.
get_llm() derives the LiteLLM-routable string based on MODEL_PROVIDER.

The wrapper intercepts every LLM call via call() to apply Action/ActionInput
cleaning and rate-limit retry logic, then delegates to LiteLLM for the actual
API call.

LiteLLM Callback for per-call traces:
    _litellm_trace_callback() is registered once in litellm.success_callback.
    It fires after every successful LiteLLM completion, capturing per-call
    tokens and cost (computed by LiteLLM's pricing tables) and writing a row
    to data/llm_traces.db via get_trace_store(). This bypasses the broken
    opentelemetry-instrumentation-vertexai which creates empty-attribute spans.
    The callback is thread-safe: get_trace_store() uses WAL + threading.Lock.
    OTel context (trace_id, span_id, workflow_id) is read from the calling
    thread's OTel context, so each concurrent workflow's callbacks are isolated.

WHY CleanedLLMWrapper OVERRIDES __new__:
    LLM.__new__ is a factory method that, for known providers (gemini, openai,
    anthropic, azure, bedrock), unconditionally calls _get_native_provider()
    BEFORE checking is_litellm. _get_native_provider() does a live import of
    the provider SDK (e.g. google-genai for Gemini). If the SDK is not
    installed, this raises ImportError immediately — is_litellm is never
    checked. By overriding __new__ in CleanedLLMWrapper, LLM.__new__ is never
    called at all: we create the instance via object.__new__ and initialize via
    BaseLLM.__init__ directly, identical to LLM.__new__'s own LiteLLM fallback
    path (llm.py lines 404-406). No provider SDK is imported; LiteLLM handles
    all API communication.

NOTE ON DOUBLE BaseLLM.__init__:
    BaseLLM.__init__ is called twice per instantiation — once explicitly in
    __new__ and once via the normal __init__ chain. This is NOT a bug: it
    exactly mirrors how plain LLM() behaves on its own LiteLLM fallback path.
    Verified empirically (both LLM() and CleanedLLMWrapper() produce 2 calls).
    Both calls are idempotent — they set the same fields to the same values.
    Do not remove the __new__ call to "fix" this.

RATE LIMITING:
    Handled automatically by LiteLLM (used internally by CrewAI).
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from crewai.llm import LLM, CONTEXT_WINDOW_USAGE_RATIO

from .llm_output_cleaner import LLMOutputCleaner, LLMFormattingMonitor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LiteLLM per-call trace callback
# ---------------------------------------------------------------------------

def _litellm_trace_callback(kwargs: dict, completion_response, start_time: datetime, end_time: datetime) -> None:
    """
    LiteLLM success_callback — writes one row to the trace store per LLM call.

    Fires synchronously after every successful litellm.completion() in the
    calling thread. OTel context is thread-local (Python contextvars), so
    concurrent workflows don't bleed into each other.

    Errors are fully swallowed: a tracing failure must never abort an LLM call.
    """
    try:
        # --- duration ---
        duration_ms = (end_time - start_time).total_seconds() * 1000.0

        # --- model ---
        model: str = kwargs.get("model", "unknown")

        # --- prompt text — serialize messages list as JSON for queryability ---
        prompt_text: str | None = None
        messages = kwargs.get("messages")
        if messages:
            try:
                import json as _json
                prompt_text = _json.dumps(messages, ensure_ascii=False)
            except Exception as _exc:
                logger.warning("[LLM_TRACE] Failed to serialize prompt messages: %s", _exc)

        # --- response text — first choice content ---
        response_text: str | None = None
        try:
            choices = getattr(completion_response, "choices", None)
            if choices:
                msg = getattr(choices[0], "message", None)
                if msg:
                    response_text = getattr(msg, "content", None)
        except Exception as _exc:
            logger.warning("[LLM_TRACE] Failed to extract response text: %s", _exc)

        # --- tokens ---
        usage = getattr(completion_response, "usage", None)
        prompt_tokens: int = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens: int = getattr(usage, "completion_tokens", 0) or 0
        total_tokens: int = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)

        # --- cost — LiteLLM calculates this from its own pricing tables ---
        hidden = getattr(completion_response, "_hidden_params", {}) or {}
        cost_usd: float = float(hidden.get("response_cost") or 0.0)

        # --- OTel context — read from the calling thread ---
        # When no parent workflow span exists, synthesize a per-call trace_id so
        # orphan calls (health checks, misc LLM use) don't all collapse into a
        # single "0000..." bucket in the trace DB.
        trace_id_hex = uuid.uuid4().hex
        parent_span_id_hex: str | None = None
        workflow_id: str | None = None
        try:
            from opentelemetry import baggage, trace as otel_trace
            span_ctx = otel_trace.get_current_span().get_span_context()
            if span_ctx and span_ctx.trace_id:
                trace_id_hex = format(span_ctx.trace_id, "032x")
                parent_span_id_hex = format(span_ctx.span_id, "016x")
            workflow_id = baggage.get_baggage("workflow.id")
        except Exception as _exc:
            logger.warning("[LLM_TRACE] OTel context unavailable: %s", _exc)

        # Each LiteLLM call gets its own span_id so it appears as a distinct row.
        span_id_hex = uuid.uuid4().hex[:16]

        from src.backend.core.trace_store import get_trace_store
        store = get_trace_store()
        if store is None:
            return

        store.insert_litellm_call(
            span_id=span_id_hex,
            trace_id=trace_id_hex,
            parent_span_id=parent_span_id_hex,
            name=f"{model}.litellm",
            model=model,
            prompt_text=prompt_text,
            response_text=response_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            workflow_id=workflow_id,
        )
    except Exception as e:
        logger.warning("[LLM_TRACE] callback error (non-fatal): %s", e)


def _register_litellm_callback() -> None:
    """Register _litellm_trace_callback once in litellm.success_callback.

    Skipped entirely when OBSERVABILITY_BACKEND=none so the trace DB file is
    never created for users who opted out of tracing.

    litellm.success_callback is a process-global list. Importing this module
    multiple times (or creating multiple CleanedLLMWrapper instances) must not
    register duplicates — checked by identity before appending.
    """
    try:
        from src.backend.core.config import settings
        if settings.OBSERVABILITY_BACKEND == "none":
            logger.info("[LLM_TRACE] Tracing disabled — LiteLLM callback not registered")
            return

        import litellm
        if _litellm_trace_callback not in litellm.success_callback:
            litellm.success_callback.append(_litellm_trace_callback)
            logger.info("[LLM_TRACE] LiteLLM trace callback registered")
    except Exception as e:
        logger.warning("[LLM_TRACE] Could not register LiteLLM callback (non-fatal): %s", e)


_register_litellm_callback()


class CleanedLLMWrapper(LLM):
    """
    Wrapper around CrewAI's LLM that cleans Action/ActionInput lines.

    Uses LiteLLM as the sole transport layer for ALL providers (Gemini, Ollama,
    OpenAI, Anthropic, etc.). This gives provider independence: switching models
    requires only an env var change, and no provider-specific SDK is imported
    into this codebase.

    WHY __new__ IS OVERRIDDEN:
        LLM.__new__ is a factory method that, for known providers (gemini, openai,
        anthropic, etc.), attempts to import their native SDK class BEFORE checking
        is_litellm. For example, for "gemini/..." models it always runs:
            from crewai.llms.providers.gemini.completion import GeminiCompletion
        This import raises ImportError if crewai[google-genai] is not installed,
        crashing before is_litellm is ever checked. Our __new__ override bypasses
        this entirely — _get_native_provider is never called — so no provider SDK
        needs to be installed. LiteLLM handles all API communication.

    Specifically fixes:
    - 'Action: tool_name` extra text' → 'Action: tool_name'
    - 'Action Input: prefix {...}' → 'Action Input: {...}'

    Rate limiting is handled automatically by LiteLLM (used internally by CrewAI).
    """

    def __new__(cls, model: str, **kwargs):
        """Bypass LLM.__new__ factory to always use the LiteLLM path.

        Replicates the LiteLLM fallback path from LLM.__new__ directly, without
        calling _get_native_provider. This prevents any native provider SDK import
        from being attempted regardless of which model/provider is configured.

        Any is_litellm kwarg from the caller is discarded — we always force True.
        """
        kwargs.pop("is_litellm", None)  # caller's value is irrelevant; we always force True
        instance = object.__new__(cls)
        # This replicates LLM.__new__'s LiteLLM fallback path exactly (llm.py line 404-406):
        #   instance = object.__new__(cls)
        #   super(LLM, instance).__init__(...)   ← BaseLLM.__init__ call #1
        #   instance.is_litellm = True
        # BaseLLM.__init__ is then called a second time when Python's normal instantiation
        # runs CleanedLLMWrapper.__init__ → LLM.__init__ → BaseLLM.__init__ (call #2).
        # This double-call is intentional and matches CrewAI's own LLM behaviour —
        # plain LLM() also triggers BaseLLM.__init__ twice via the same mechanism.
        # Verified empirically: both LLM() and CleanedLLMWrapper() produce exactly 2 calls.
        # DO NOT remove this call to "fix" the double-init — doing so would make our wrapper
        # diverge from CrewAI's own instantiation pattern.
        super(LLM, instance).__init__(model=model, is_litellm=True, **kwargs)
        instance.is_litellm = True
        return instance

    def __init__(self, *args, **kwargs):
        """Initialize the wrapper with the same arguments as LLM."""
        super().__init__(*args, **kwargs)
        self._monitor = LLMFormattingMonitor()
        logger.info("🧹 Initialized CleanedLLMWrapper - will clean Action/ActionInput lines")

    def get_context_window_size(self) -> int:
        """Return the context window size for the configured model.

        LLM.get_context_window_size() matches self.model against keys in
        LLM_CONTEXT_WINDOW_SIZES using startswith, but our model string is
        'provider/model' (e.g. 'gemini/gemini-2.5-flash'). The provider prefix
        causes all startswith checks to fail, returning the tiny 6963-token
        default. This override fixes that by querying authoritative sources:

        1. LiteLLM's local model database  — covers all cloud providers
        2. Ollama's REST API               — covers local models not in LiteLLM DB
        3. CrewAI's built-in lookup        — last resort fallback
        """
        if self.context_window_size != 0:
            return self.context_window_size

        # Step 1: LiteLLM's local model database (no API call — ships with litellm)
        # Note: only use max_input_tokens (context window), NOT max_tokens (output limit)
        try:
            import litellm
            info = litellm.get_model_info(self.model)
            max_input_tokens = info.get("max_input_tokens")
            if max_input_tokens:
                self.context_window_size = int(max_input_tokens * CONTEXT_WINDOW_USAGE_RATIO)
                logger.debug(
                    f"🪟 Context window for '{self.model}': "
                    f"{self.context_window_size} tokens (source: LiteLLM DB)"
                )
                return self.context_window_size
        except Exception as e:
            # Intermediate step miss — not warning-worthy on its own. Ollama is
            # only tried for ollama/ models; the final unresolved case is
            # surfaced once, at WARNING, in Step 3 below.
            logger.debug(
                f"LiteLLM DB has no context-window entry for '{self.model}' "
                f"(type={type(e).__name__}, detail={e})"
            )

        # Step 2: Ollama API — local models are not in LiteLLM's central database
        if self.model.startswith("ollama/"):
            try:
                import requests
                model_name = self.model.split("/", 1)[1]
                # rstrip to handle trailing slash in OLLAMA_API_BASE (e.g. "http://host/")
                base_url = (self.base_url or "http://localhost:11434").rstrip("/")
                response = requests.post(
                    f"{base_url}/api/show",
                    json={"model": model_name},
                    timeout=5,
                )
                if response.status_code == 200:
                    model_info = response.json().get("model_info", {})
                    # Keys follow the pattern "{architecture}.context_length"
                    # e.g. "llama.context_length", "qwen2.context_length"
                    ctx_length = next(
                        (v for k, v in model_info.items()
                         if k.endswith(".context_length")),
                        None,
                    )
                    if ctx_length:
                        self.context_window_size = int(
                            ctx_length * CONTEXT_WINDOW_USAGE_RATIO
                        )
                        logger.debug(
                            f"🪟 Context window for '{self.model}': "
                            f"{self.context_window_size} tokens (source: Ollama API)"
                        )
                        return self.context_window_size
                    else:
                        logger.warning(
                            f"⚠️ Ollama /api/show returned no context_length key "
                            f"for '{model_name}' — falling back to CrewAI defaults"
                        )
                else:
                    logger.warning(
                        f"⚠️ Ollama /api/show returned HTTP {response.status_code} "
                        f"for '{model_name}' — falling back to CrewAI defaults"
                    )
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to query Ollama API for context window of "
                    f"'{self.model}' (type={type(e).__name__}, detail={e}) "
                    f"— falling back to CrewAI defaults"
                )

        # Step 3: CrewAI's built-in lookup (last resort — likely returns tiny default)
        result = super().get_context_window_size()
        # Cache the fallback like Steps 1 and 2 do: the context window cannot
        # change during the process, and without caching this whole method —
        # including a litellm lookup and a 5s Ollama HTTP call — re-runs on
        # every get_context_window_size() call, spamming this WARNING each time.
        self.context_window_size = result
        logger.warning(
            f"⚠️ Context window for '{self.model}' not resolved from LiteLLM DB "
            f"or Ollama API — using CrewAI fallback: {result} tokens. "
            f"Context summarization may be suboptimal."
        )
        return result

    def call(self, messages, *args, **kwargs) -> str:
        """
        Override call() for output cleaning.

        CrewAI uses call() -> _handle_non_streaming_response() -> litellm.completion().
        _generate() is a LangChain concept and is never called by CrewAI, so all
        cleaning must happen here.
        """
        result = super().call(messages, *args, **kwargs)
        if not isinstance(result, str):
            self._monitor.log_response(was_cleaned=False)
            return result

        cleaned = LLMOutputCleaner.clean_output(result)
        was_cleaned = cleaned != result
        if was_cleaned:
            logger.debug(f"🧹 Cleaned LLM response (length: {len(result)} → {len(cleaned)})")
        self._monitor.log_response(was_cleaned=was_cleaned)
        return cleaned


def get_llm(model_provider: str, model_name: str, api_key: Optional[str] = None):
    """
    Get a CleanedLLMWrapper instance for the given provider and model.

    Works for all LiteLLM-supported providers via a single wrapper class:
    - Gemini  (Google AI Studio):  model_provider="gemini", model_name="gemini-2.5-flash"
    - Vertex  (Google Cloud):      model_provider="vertex", model_name="gemini-2.5-flash"
    - Local   (Ollama):            model_provider="local",  model_name="qwen2.5-coder:14b"

    model_name is always the bare model name (no provider prefix). This function
    derives the correct LiteLLM-routable string automatically:
        gemini  → "gemini/gemini-2.5-flash"
        vertex  → "vertex_ai/gemini-2.5-flash"
        local   → "ollama/qwen2.5-coder:14b"

    The returned instance:
    - Calls LiteLLM under the hood (__new__ override bypasses CrewAI's native
      provider factory entirely — no provider SDK needs to be installed)
    - Cleans 'Action: tool_name` extra text' → 'Action: tool_name'
    - Cleans 'Action Input: prefix {...}' → 'Action Input: {...}'
    - Retries on transient API errors via LiteLLM (num_retries=3)
    - Tracks all responses via self._monitor (per-instance, never shared across workflows)

    Args:
        model_provider: "gemini" for Google AI Studio, "vertex" for Vertex AI,
                        "local" for Ollama. Any other value raises ValueError.
        model_name: Bare model name without provider prefix (e.g. "gemini-2.5-flash",
                    "qwen2.5-coder:14b"). The provider prefix is prepended here.
        api_key: API key for Gemini models (optional, falls back to GEMINI_API_KEY
                 env var). Not used for Vertex AI or local Ollama models.

    Returns:
        CleanedLLMWrapper instance ready for use with CrewAI agents

    Raises:
        ValueError: If model_provider is not one of: "gemini", "vertex", "local".
    """
    # Provider→prefix routing and Ollama api_base resolution live in
    # llm_provider_routing.py so the same logic is reused by the learning
    # system's conflict-detection triggers (which call litellm.completion()
    # directly and cannot use CleanedLLMWrapper — it would apply CrewAI's
    # Action/ActionInput cleaner to a JSON-only response).
    from .llm_provider_routing import (
        PROVIDER_PREFIXES,
        resolve_model_string,
        resolve_completion_kwargs,
    )

    if model_provider not in PROVIDER_PREFIXES:
        raise ValueError(
            f"Unsupported model_provider: '{model_provider}'. "
            f"Must be one of: {sorted(PROVIDER_PREFIXES)}. "
            f"Check your MODEL_PROVIDER environment variable."
        )

    routed_model = resolve_model_string(model_provider, model_name)

    if model_provider == "local":
        # LiteLLM routes "ollama/<model>" to the Ollama HTTP API.
        # OLLAMA_API_BASE env var controls the server URL:
        #   Local dev (no Docker): http://localhost:11434  (default)
        #   Docker Desktop Mac/Win: http://host.docker.internal:11434
        #   Docker on Linux:        http://172.17.0.1:11434
        ollama_base_url = resolve_completion_kwargs("local")["api_base"]
        logger.info(
            f"🧹 Creating CleanedLLMWrapper for local model: {routed_model} "
            f"at {ollama_base_url}"
        )
        return CleanedLLMWrapper(
            model=routed_model,
            base_url=ollama_base_url,
            is_litellm=True,  # No routing effect — __new__ override bypasses LLM.__new__
                              # entirely. Kept for documentation clarity only.
            num_retries=3,    # LiteLLM internal retry for transient API errors.
        )

    if model_provider == "vertex":
        # Auth is handled automatically: VERTEXAI_CREDENTIALS, VERTEXAI_PROJECT,
        # and VERTEXAI_LOCATION are read from os.environ by LiteLLM (loaded via python-dotenv).
        logger.info(f"🧹 Creating CleanedLLMWrapper for Vertex AI model: {routed_model}")
        return CleanedLLMWrapper(
            model=routed_model,
            num_retries=3,
            is_litellm=True,
        )

    # model_provider == "gemini" — Google AI Studio.
    # is_litellm=True has no routing effect — CleanedLLMWrapper.__new__ bypasses
    # LLM.__new__ entirely. Kept for documentation clarity only.
    logger.info(f"🧹 Creating CleanedLLMWrapper for Gemini model: {routed_model}")
    return CleanedLLMWrapper(
        api_key=api_key or os.getenv("GEMINI_API_KEY"),
        model=routed_model,
        num_retries=3,    # LiteLLM internal retry for transient API errors (429, 503, etc.)
        is_litellm=True,
    )
