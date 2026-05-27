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
    for candidate in (provider, prefix):
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
