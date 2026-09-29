"""Keep our configured Gemini model known to LiteLLM when its model-list download fails.

At ``import litellm``, LiteLLM 1.75.3 downloads its model list from GitHub
(5 s timeout) and, on ANY failure, silently loads the copy bundled in the
package (litellm/litellm_core_utils/get_model_cost_map.py). That copy has no
gemini-3.5-flash, so one failed download at process start turns off schema
enforcement for the planner and the assembler (get_llm drops response_format),
moves the system prompt into the user turn of every request, and makes the
model's cost read $0 for the life of the process. LiteLLM logs nothing about
the failed download; the only traces are get_llm's INFO line and a cost-lookup
WARNING, and neither names the cause.

ensure_model_entry() adds our pinned copy of the model's entries (verbatim
from LiteLLM's online list, pinned_model_entries.json) to litellm.model_cost
ONLY when LiteLLM does not know the model, adding only missing keys, and logs a
WARNING naming the pinned date. If the model is still not schema-capable it
logs an ERROR. After a good download, or with a litellm that bundles the model,
it does nothing. It never raises: a failure of its own is logged as an ERROR and
the attempt is retried on the next call.

To add a model (a one-time step per model): copy its entries from LiteLLM's
online list into pinned_model_entries.json, refresh the existing entries from
the same download, set pinned_on to that day, and update
test_the_pin_file_holds_the_three_entries. A model we run but never pinned is
reported by the ERROR line, not silently.

Referenced by:
    - src/backend/crew_ai/cleaned_llm_wrapper.py (get_llm, before the schema gate)
    - src/backend/main.py (startup_event, once for the configured model)

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
    try:
        litellm.get_model_info(model=routed_model)
    except Exception:
        return False
    return True


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
                    logger.warning(
                        "LiteLLM's model list has no entry for %s (most likely its download from GitHub "
                        "failed when litellm was imported). Using our copy of its entries, pinned on %s; "
                        "their prices may be out of date.",
                        routed_model, pinned["pinned_on"],
                    )
            if not litellm.utils.supports_response_schema(model=routed_model):
                logger.error(
                    "LiteLLM does not know %s as a schema-capable model: the planner and the "
                    "assembler run WITHOUT schema enforcement%s.",
                    routed_model, "" if _known(routed_model) else ", and its cost reads $0",
                )
        except Exception:
            logger.error(
                "Model-list fallback failed for %s: schema enforcement and pricing for it may be off.",
                routed_model, exc_info=True,
            )
        else:
            _checked.add(routed_model)
