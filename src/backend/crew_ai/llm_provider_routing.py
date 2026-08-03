"""LLM provider routing — shared by cleaned_llm_wrapper (agent calls) and
the learning-system conflict-detection triggers (workflow_service / feedback_loop).

Intentionally dependency-free: no CrewAI, LiteLLM, or LangChain imports at the
module level. This lets lightweight infrastructure modules (e.g.
learning_config.py) consume the helper without dragging heavy frameworks
through their import graph.

Scope (deliberately narrow):
- Map MODEL_PROVIDER → LiteLLM model-string prefix.
- Resolve per-provider extra kwargs that litellm.completion() needs but
  does not derive from env vars on its own (currently only Ollama's
  api_base; Gemini and Vertex auth comes from env vars LiteLLM reads
  natively).
- Resolve the Vertex thinking guard, which is a fact about the provider and
  model family rather than about any one caller, so every LiteLLM call site
  gets it from the same place.

What this module does NOT own — those are call-site policy, not routing:
- Timeout / retry behavior (agents are long-running; triggers are snappy)
- Auth / API-key reading (different call sites use different env vars)
- Output post-processing (CleanedLLMWrapper applies an Action/ActionInput
  cleaner that is correct for CrewAI ReAct agents but would mangle JSON
  responses expected by trigger detection).

Referenced by: cleaned_llm_wrapper.get_llm(), learning_config conflict
detection helpers.
"""

import os


# LiteLLM model-string prefixes per supported MODEL_PROVIDER value.
# Add new providers here (and update get_llm() if they need extra constructor
# kwargs beyond the model string and the entries in resolve_completion_kwargs()).
PROVIDER_PREFIXES: dict[str, str] = {
    "gemini": "gemini",
    "vertex": "vertex_ai",
    "local":  "ollama",
}

_DEFAULT_PROVIDER = "gemini"


def resolve_model_string(provider: str, model_name: str) -> str:
    """Return the LiteLLM-routable model string.

    Strips an accidentally-prefixed `<provider>/` from `model_name` so a
    legacy caller passing e.g. `"gemini/gemini-2.5-flash"` does not produce
    `"gemini/gemini/gemini-2.5-flash"`. Same defensive behavior as the
    historical get_llm() vertex/gemini branches; uniformly applied to
    `local` too (was previously not stripped — a latent bug for callers
    that pass `"ollama/qwen..."`).

    Unknown providers fall back to the gemini prefix to match the
    historical default in get_llm()'s order-of-branches.
    """
    prefix = PROVIDER_PREFIXES.get(provider, _DEFAULT_PROVIDER)
    bare = model_name
    # Check current provider first, then all known keys/values so cross-provider
    # prefixes (e.g. "gemini/..." when using vertex) are also stripped.
    for candidate in (provider, prefix, *PROVIDER_PREFIXES.keys(), *PROVIDER_PREFIXES.values()):
        if candidate and model_name.startswith(f"{candidate}/"):
            bare = model_name[len(candidate) + 1:]
            break
    return f"{prefix}/{bare}"


def resolve_completion_kwargs(provider: str) -> dict:
    """Per-provider extra kwargs for litellm.completion().

    Only Ollama needs api_base — litellm.completion() does NOT read
    OLLAMA_API_BASE from the environment on its own. Inside Docker the
    container's localhost is not the host; the user must set
    OLLAMA_API_BASE (e.g. http://host.docker.internal:11434) and we
    must thread it through explicitly. cleaned_llm_wrapper.get_llm()
    consumes the same `api_base` value and passes it to CleanedLLMWrapper
    as `base_url` (constructor naming, not LiteLLM naming).
    """
    if provider == "local":
        return {
            "api_base": os.getenv("OLLAMA_API_BASE", "http://localhost:11434"),
        }
    return {}


def resolve_thinking_kwargs(provider: str, routed_model: str) -> dict:
    """Vertex thinking guard — one rule, every LiteLLM call site.

    Vertex flipped gemini-3.5-flash to server-side thinking-ON (2026-07-18),
    inflating completion tokens 4-6x and burning TPM/RPD quota. Every call we
    make to a Gemini model on Vertex must carry a zero budget.

    Lives here rather than at a call site because it is a fact about the
    provider, not about the caller: it was originally encoded inside
    get_llm() alone, and the conflict-detection path in learning_config —
    which calls litellm.completion() directly — silently went unguarded and
    paid for thinking on every call.

    Carries `thinkingConfig` (Vertex's own generationConfig field) rather than
    LiteLLM's `thinking` shorthand: from litellm 1.94.1 _map_thinking_param
    routes every "Gemini 3 or newer" model down a thinkingLevel branch that
    drops the budget and emits only includeThoughts=False, hiding thoughts
    without stopping them. thinkingConfig reaches generationConfig untouched
    and reproduces what 1.75.3 put on the wire.

    Gated on the model family because that same switch removed the loud
    failure that used to cover this. Vertex also serves Anthropic, Llama and
    Mistral, and resolve_model_string does not check the family — ONLINE_MODEL
    is a free string, so one config edit reaches here with a non-Gemini model.
    Measured on the pinned litellm 1.75.3: `thinking` raised
    UnsupportedParamsError on llama/mistral and mapped to a real Anthropic
    param on claude, while thinkingConfig is accepted silently by all three
    and would ship a Gemini-only generationConfig field to a non-Gemini
    endpoint.

    Takes the ROUTED model string, never the bare name. resolve_model_string
    strips a stale cross-provider prefix, so the bare argument
    'gemini/claude-sonnet-4@20250514' routes to
    'vertex_ai/claude-sonnet-4@20250514' — a bare-name family check sees
    'gemini' from the stripped prefix and would attach the guard to a Claude
    endpoint. Measured 2026-08-03; pinned by
    test_family_check_reads_the_routed_string_not_the_bare_name.
    """
    if provider == "vertex" and "gemini" in routed_model.lower():
        return {"thinkingConfig": {"thinkingBudget": 0}}
    return {}
