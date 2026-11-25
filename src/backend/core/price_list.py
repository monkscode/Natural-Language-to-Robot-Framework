"""Which LiteLLM price list prices runs: the copy shipped with litellm at import, the live one right after start.

litellm 1.105.3 downloads its price list from GitHub during `import litellm`. When that first try
fails it starts a retry thread while the import is still running, and the two can crash the import
(ImportError on a partially initialized litellm.rust_bridge.catalog; 3 of 13 offline starts died).
So both server processes set LITELLM_LOCAL_MODEL_COST_MAP=True before anything imports litellm:
litellm then loads its shipped list and starts no thread. FastAPI then downloads the live list
with litellm's own refetch + adopt once the server is up (users must see live prices), retrying
every 10 minutes for an hour. A value the operator set is never changed, and then nothing is
downloaded. Imports only the standard library at module level: it runs before everything else.

Referenced by:
    - src/backend/main.py (use_shipped_price_list at import; start_live_price_list in startup_event)
    - src/backend/runner_exec/app.py (use_shipped_price_list at import; it never prices anything)

Depends on:
    - litellm (refetch_model_cost_map, adopt_model_cost_map; imported inside the job)
    - src/backend/crew_ai/model_map_fallback.py, llm_provider_routing.py, core/config.py (inside the job)
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

ENV_VAR = "LITELLM_LOCAL_MODEL_COST_MAP"
RETRY_WAIT_S = 600
MAX_RETRIES = 6  # owner: "every 10 min for 1 hr only"


def use_shipped_price_list() -> bool:
    """Set ENV_VAR to "True" when it is absent. True when this call set it; an operator's value is never changed."""
    if ENV_VAR in os.environ:
        return False
    os.environ[ENV_VAR] = "True"
    return True


def start_live_price_list(set_by_us: bool) -> asyncio.Task | None:
    """From FastAPI's startup: drop our ENV_VAR and download the live list in the background.

    Returns the task for the caller to keep (the loop holds tasks only weakly), or None when the
    operator set the variable.
    """
    if not set_by_us:
        return None
    os.environ.pop(ENV_VAR, None)  # litellm's refetch obeys it too
    return asyncio.get_running_loop().create_task(load_live_price_list())


async def load_live_price_list(sleep=asyncio.sleep) -> None:
    """Download LiteLLM's live price list and use it: one round now, then up to MAX_RETRIES more,
    RETRY_WAIT_S apart. Never raises; on failure the shipped list stays in use."""
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            await sleep(RETRY_WAIT_S)
        try:
            import litellm
            from litellm.litellm_core_utils import get_model_cost_map as cost_map

            result = await cost_map.refetch_model_cost_map(url=litellm.model_cost_map_url)
            if isinstance(result, cost_map.ModelCostMapReloaded):
                count = cost_map.adopt_model_cost_map(result.model_cost_map)
                break
            reason = result.reason
        except Exception as e:  # noqa: BLE001 — a failed round is retried, never fatal
            reason = repr(e)
        logger.info("LiteLLM's live price list: round %d of %d failed (%s).", attempt + 1, 1 + MAX_RETRIES, reason)
    else:
        logger.warning(
            "LiteLLM's live price list could not be downloaded (%d rounds, %d minutes apart); the price list "
            "shipped with litellm stays in use until the next restart.",
            1 + MAX_RETRIES, RETRY_WAIT_S // 60,
        )
        return
    logger.info("Using LiteLLM's live price list (%d models).", count)
    # adopt replaced litellm.model_cost wholesale: entries model_map_fallback pinned are gone
    try:
        from src.backend.core.config import settings
        from src.backend.crew_ai import model_map_fallback
        from src.backend.crew_ai.llm_provider_routing import resolve_model_string

        model_map_fallback.forget_checked_models()
        model_map_fallback.ensure_model_entry(
            settings.MODEL_PROVIDER, resolve_model_string(settings.MODEL_PROVIDER, settings.ONLINE_MODEL))
    except Exception as e:  # noqa: BLE001 — the same guard as startup's model-list check
        logger.warning("[LLM] model-list check after the live price list skipped: %s", e)
