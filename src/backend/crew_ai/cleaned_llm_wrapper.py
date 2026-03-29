"""
LLM Wrapper - LLM instantiation with output cleaning for CrewAI agents.

This module provides a single CleanedLLMWrapper that works for all providers:
- Online models (Gemini): model="gemini/gemini-2.5-flash"
- Local  models (Ollama): model="ollama/<model_name>"

The wrapper intercepts every LLM call via call() to apply Action/ActionInput
cleaning and rate-limit retry logic, then delegates to LiteLLM for the actual
API call.

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
from typing import Optional
from crewai.llm import LLM, CONTEXT_WINDOW_USAGE_RATIO

from .llm_output_cleaner import LLMOutputCleaner, formatting_monitor

logger = logging.getLogger(__name__)


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
            logger.debug(
                f"LiteLLM DB has no entry for '{self.model}' "
                f"(type={type(e).__name__}, detail={e}) — trying Ollama API next"
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
            formatting_monitor.log_response(was_cleaned=False)
            return result

        cleaned = LLMOutputCleaner.clean_output(result)
        was_cleaned = cleaned != result
        if was_cleaned:
            logger.debug(f"🧹 Cleaned LLM response (length: {len(result)} → {len(cleaned)})")
        formatting_monitor.log_response(was_cleaned=was_cleaned)
        return cleaned


def get_llm(model_provider: str, model_name: str, api_key: Optional[str] = None):
    """
    Get a CleanedLLMWrapper instance for the given provider and model.

    Works for all LiteLLM-supported providers via a single wrapper class:
    - Online (Gemini):  model_provider="online", model_name="gemini/gemini-2.5-flash"
    - Local  (Ollama):  model_provider="local",  model_name="qwen2.5-coder:14b"

    The returned instance:
    - Calls LiteLLM under the hood (__new__ override bypasses CrewAI's native
      provider factory entirely — no provider SDK needs to be installed)
    - Cleans 'Action: tool_name` extra text' → 'Action: tool_name'
    - Cleans 'Action Input: prefix {...}' → 'Action Input: {...}'
    - Retries on rate-limit errors (online models) using API-provided retryDelay
    - Tracks all responses via formatting_monitor

    Args:
        model_provider: "local" for Ollama, "online" for Gemini/other API models
        model_name: Model identifier.
                    Online: include provider prefix (e.g. "gemini/gemini-2.5-flash")
                    Local:  bare model name (e.g. "qwen2.5-coder:14b") — "ollama/"
                            is prepended here so callers stay provider-agnostic.
        api_key: API key for online models (optional, falls back to GEMINI_API_KEY
                 env var). Not used for local Ollama models.

    Returns:
        CleanedLLMWrapper instance ready for use with CrewAI agents
    """
    if model_provider == "local":
        # LiteLLM routes "ollama/<model>" to the Ollama HTTP API.
        # OLLAMA_API_BASE env var controls the server URL:
        #   Local dev (no Docker): http://localhost:11434  (default)
        #   Docker Desktop Mac/Win: http://host.docker.internal:11434
        #   Docker on Linux:        http://172.17.0.1:11434
        ollama_base_url = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
        logger.info(
            f"🧹 Creating CleanedLLMWrapper for local model: ollama/{model_name} "
            f"at {ollama_base_url}"
        )
        return CleanedLLMWrapper(
            model=f"ollama/{model_name}",
            base_url=ollama_base_url,
            is_litellm=True,  # No routing effect — __new__ override bypasses LLM.__new__
                              # entirely. Kept for documentation clarity only.
            num_retries=0,    # CleanedLLMWrapper.call() manages retries; Ollama has
                              # no rate limits so the handler stays dormant.
        )

    # Online provider (Gemini and future API-based models).
    # is_litellm=True has no routing effect — CleanedLLMWrapper.__new__ bypasses
    # LLM.__new__ entirely. Kept for documentation clarity only.
    logger.info(f"🧹 Creating CleanedLLMWrapper for model: {model_name}")
    return CleanedLLMWrapper(
        api_key=api_key or os.getenv("GEMINI_API_KEY"),
        model=model_name,
        num_retries=0,    # Disable LiteLLM's internal retry — CleanedLLMWrapper.call()
                          # handles 429s using the API-provided retryDelay value.
        is_litellm=True,
    )
