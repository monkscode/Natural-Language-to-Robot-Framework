"""Keep our configured Gemini model priced and schema-capable in LiteLLM's model list.

litellm 1.105.3 bundles gemini-3.5-flash and gemini-3.8-flash, so today
ensure_model_entry() does nothing for them, even after a failed list download at
import. It stays as the guard for a model litellm lacks. For an unmapped
gemini-named model litellm builds an entry with schema support and a price of 0
(no error), so "known" here means a real price, not merely "no error": the cost
would otherwise read $0 silently.

When LiteLLM has no priced entry for the model, ensure_model_entry() adds our
pinned copy of its entries (verbatim from LiteLLM's online list,
pinned_model_entries.json), adding only missing keys, and logs a WARNING naming
the pinned date. If the model is still unpriced or not schema-capable it logs
an ERROR (planner and assembler would run without schema enforcement, cost $0).
It never raises: a failure of its own is logged as an ERROR and the attempt is
retried on the next call.

To add a model: copy its entries from LiteLLM's online list into
pinned_model_entries.json, refresh the existing entries from the same download,
set pinned_on to that day, and update test_the_pin_file_holds_both_models_entries.
A model we run but never pinned is reported by the ERROR line, not silently.

Referenced by:
    - src/backend/crew_ai/cleaned_llm_wrapper.py (get_llm, before the schema gate)
    - src/backend/main.py (startup_event, once for the configured model)
    - src/backend/core/price_list.py (forget_checked_models, then the configured model, after the live list is adopted)

Depends on:
    - litellm (model_cost, get_model_info, utils.supports_response_schema)
    - src/backend/crew_ai/pinned_model_entries.json
"""

import json
import logging
import threading
from pathlib import Path

import litellm

logger = logging.getLogger(__name__)

PINNED_FILE = Path(__file__).with_name("pinned_model_entries.json")
_CLOUD_PROVIDERS = frozenset({"vertex", "gemini"})
_checked: set[str] = set()
_lock = threading.Lock()


def _known(routed_model: str) -> bool:
    """True when LiteLLM has a real price for the model (an unmapped gemini name gets price 0)."""
    try:
        return bool(litellm.get_model_info(model=routed_model).get("input_cost_per_token"))
    except Exception:
        return False


def forget_checked_models() -> None:
    """Check every model again on its next ensure_model_entry call: litellm.model_cost was replaced."""
    with _lock:
        _checked.clear()


def ensure_model_entry(model_provider: str, routed_model: str) -> None:
    """Make routed_model known to LiteLLM if its model list lacks it.

    Cloud providers only; Ollama models are neither priced nor schema-enforced.
    Runs once per model per process; an attempt that failed is retried on the next
    call. Safe to call from any thread.
    """
    if model_provider not in _CLOUD_PROVIDERS:
        return
    with _lock:
        if routed_model in _checked:
            return
        try:
            if not _known(routed_model):
                pinned = json.loads(PINNED_FILE.read_text(encoding="utf-8"))
                name = routed_model.split("/", 1)[-1]
                entries = {key: entry for key, entry in pinned["entries"].items()
                           if key.split("/", 1)[-1] == name}
                for key, entry in entries.items():
                    litellm.model_cost.setdefault(key, dict(entry))
                if entries:
                    # model_cost was edited in place: drop LiteLLM's cached lookups (they hold the price-0 answer)
                    litellm.utils._invalidate_model_cost_lowercase_map()
                    logger.warning(
                        "LiteLLM's model list has no entry for %s (most likely its download from GitHub "
                        "failed when litellm was imported). Using our copy of its entries, pinned on %s; "
                        "their prices may be out of date.",
                        routed_model, pinned["pinned_on"],
                    )
            problems = []
            if not litellm.utils.supports_response_schema(model=routed_model):
                problems.append("the planner and the assembler run WITHOUT schema enforcement")
            if not _known(routed_model):
                problems.append("its cost reads $0")
            if problems:
                logger.error(
                    "LiteLLM does not know %s as a priced, schema-capable model: %s.",
                    routed_model, ", and ".join(problems),
                )
        except Exception:
            logger.exception(
                "Model-list fallback failed for %s: schema enforcement and pricing for it may be off.",
                routed_model,
            )
        else:
            _checked.add(routed_model)
