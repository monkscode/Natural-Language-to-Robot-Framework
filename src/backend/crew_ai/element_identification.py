"""Deterministic element→step hand-off (Task 16).

Replaces the element-identifier LLM agent. The pipeline is now:

    Planner (LLM) → build_elements()/extract_plan_url() → ONE
    batch_browser_automation call → merge_locators() → Assembler (LLM)

Every job the old agent did is a mechanical rule, ported here as code:
  - which steps need locators: keyword whitelist (step_needs_locator)
  - the target URL: the Open Browser / New Page step's value (extract_plan_url)
  - element descriptions: element_description VERBATIM, with the single
    FORM_ELEMENT_HANDLING exception (rewrite_form_description)
  - keyword→action mapping (action_for_keyword)
  - dedup by identical element_description (build_elements)
  - user_query: the user's original query, verbatim (identify_elements)
  - merge: staple the FULL locator_mapping contract onto each step
    (merge_locators) — locator, element_type, dropdown/datepicker framework,
    select_id, observed class/aria evidence, stability, all_locators, and
    the ASTPP flags (visibility_filtered / row_anchored /
    row_anchor_ambiguous, present only when True)

Steps arriving from crew._extract_plan_steps are PlanOutput-validated
(fail-fast, pre-browser); merge_locators additionally filters each step to
PlannedStep fields so raw-dict callers with LLM key drift can never crash
the model construction or leak fabricated locator keys.

FAILURE CONTRACT (Task 16 §F, deliberate): tool error OR found:false → the
step gets NO locator → the Assembler's existing placeholder path
(USE_PROVIDED_LOCATORS_RULES / LOCATOR_MAPPING_RULES, Task 12). NO retry,
NO second tool call, NO reformulation. The old LLM once violated its own
call-ONCE rule and returned a fabricated locator for a non-existent element;
this module makes that impossible.

Referenced by: src/backend/crew_ai/crew.py (between the planner and assembler
kickoffs). Depends on: tasks.py models, tools.browser_use_tool (lazily).
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tasks import IdentifiedElement, PlannedStep

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword whitelist (port checklist #1)
# ---------------------------------------------------------------------------

# Keywords that never target a page element. Exact matches on the normalized
# (lowercased, whitespace-collapsed) keyword.
_NO_LOCATOR_EXACT = frozenset({
    "open browser", "close browser", "close page", "close context",
    "new browser", "new context", "new page", "go to", "go back",
    "keyboard key", "sleep", "log", "evaluate", "take screenshot",
})

# Keyword families that never target a page element (assertions on variables,
# window management).
_NO_LOCATOR_PREFIXES = ("should ", "set ", "maximize ", "log ", "run keyword")

# Exact keyword → batch-tool action. These are the keywords the planner
# actually emits (census over logs/crewai.log*); an exact hit means the step
# needs a locator even when element_description is missing (planner defect →
# found:false placeholder, never silently skipped).
_ACTION_EXACT: Dict[str, str] = {
    # typing
    "input text": "input", "fill text": "input", "type text": "input",
    "fill secret": "input", "input password": "input",
    # clicking
    "click": "click", "click element": "click", "click button": "click",
    "click link": "click", "check checkbox": "click", "uncheck checkbox": "click",
    "select checkbox": "click", "unselect checkbox": "click",
    # keyboard on an element — reuses the sibling element when one exists
    # (dedup); standalone it only needs the element focused, so click.
    "press keys": "click", "press key": "click",
    # reading / locate-only
    "get text": "get_text", "get texts": "get_text", "get element": "get_text",
    "get elements": "get_text", "get element count": "get_text",
    "get classes": "get_text", "get attribute": "get_text",
    "get selected options": "get_text", "get state": "get_text",
    "wait for elements state": "get_text",
    # dropdowns
    "select options by": "select", "select options": "select",
    "select options by value": "select", "select options by label": "select",
    "select from list": "select", "select from list by label": "select",
    "select from list by value": "select", "select from list by index": "select",
    # locate-only: never click/type a file input in the exploration session
    # (a click opens the browser's NATIVE dialog and hangs).
    "upload file by selector": "get_text",
}

# Family fallbacks for planner phrasing drift ("Wait Until Element Is
# Visible", "Get Text From Elements", ...). A prefix hit needs a locator only
# when the step actually names an element — legitimately element-free family
# members exist (Get Length, Get Time).
_ACTION_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("input", "input"), ("fill", "input"), ("type", "input"),
    ("click", "click"), ("check ", "click"), ("uncheck", "click"),
    ("press", "click"),
    ("select", "select"),
    ("get ", "get_text"), ("wait ", "get_text"),
    ("hover", "get_text"), ("upload", "get_text"),
)

# Dedup precedence: when several steps share one element, the element's action
# is the highest-precedence one and the value comes from that winning step
# (e.g. Input Text + Press Keys on the search box → action=input, value from
# the Input Text step).
_ACTION_PRECEDENCE = {"input": 3, "select": 3, "click": 2, "get_text": 1}

# Actions the batch tool needs a value for (text to type / option to select).
_VALUE_ACTIONS = frozenset({"input", "select"})

# Steps whose value is the page URL (port checklist #2).
_NAVIGATION_KEYWORDS = frozenset({"open browser", "new page", "go to"})


def _normalize_keyword(keyword: Optional[str]) -> str:
    return " ".join((keyword or "").split()).lower()


def _as_dict(step: Any) -> Dict[str, Any]:
    """Accept plain dicts or pydantic models (PlanOutput.steps)."""
    if hasattr(step, "model_dump"):
        return step.model_dump()
    return dict(step)


def _prefix_action(keyword: str) -> Optional[str]:
    for prefix, action in _ACTION_PREFIXES:
        if keyword.startswith(prefix):
            return action
    return None


def step_needs_locator(step: Dict[str, Any]) -> bool:
    """Whitelist decision: does this plan step target a page element?"""
    keyword = _normalize_keyword(step.get("keyword"))
    if keyword in _NO_LOCATOR_EXACT or keyword.startswith(_NO_LOCATOR_PREFIXES):
        return False
    if keyword in _ACTION_EXACT:
        return True
    # Prefix families and unknown keywords share the same rule: the step
    # targets an element iff the planner named one.
    return bool((step.get("element_description") or "").strip())


def action_for_keyword(keyword: str) -> str:
    """Map a Robot Framework keyword to a batch-tool action.

    get_text (locate-only) is the default for anything unclassified — never
    click or type into an unknown element during the exploration session.
    """
    normalized = _normalize_keyword(keyword)
    if normalized in _ACTION_EXACT:
        return _ACTION_EXACT[normalized]
    return _prefix_action(normalized) or "get_text"


def extract_plan_url(steps: List[Any]) -> Optional[str]:
    """The URL is the first navigation step's value — nothing is guessed.

    Fallback: the planner's keyword vocabulary is a free string, so the URL
    sometimes rides on a non-navigation step (bench 2026-07-11 q03: keyword
    "New Browser", value=<url> — the whitelist miss skipped the browser call
    and every element got a found:false placeholder). If no navigation step
    carries a value, the first *literal* URL among the step values is taken;
    non-URL values (browser names, input text) are never eligible.
    """
    dict_steps = [_as_dict(step) for step in steps]
    for step in dict_steps:
        if _normalize_keyword(step.get("keyword")) in _NAVIGATION_KEYWORDS:
            value = (step.get("value") or "").strip()
            if value:
                return value
    for step in dict_steps:
        value = (step.get("value") or "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value
    return None


def rewrite_form_description(description: str) -> str:
    """Port of FORM_ELEMENT_HANDLING — the ONE description transformation.

    checkbox/radio/toggle descriptions are rewritten to explicitly target the
    <input> control (labels can't be checked/unchecked). The full original
    description is embedded so no spatial context is lost. Idempotent: a
    description already targeting the INPUT element passes through.
    """
    lowered = description.lower()
    if "input element" in lowered:
        return description
    if "checkbox" in lowered:
        kind = "checkbox"
    elif "radio" in lowered:
        kind = "radio button"
    elif "toggle" in lowered:
        kind = "toggle"
    else:
        return description
    return f'the {kind} INPUT element for "{description}"'


# ---------------------------------------------------------------------------
# Element building + dedup (port checklist #3, #4, #5)
# ---------------------------------------------------------------------------

def build_elements(steps: List[Any]) -> Tuple[List[Dict[str, Any]], Dict[int, str]]:
    """Build the batch-tool element specs from the plan.

    Returns (elements, step_element_ids) where step_element_ids maps a step's
    index in `steps` to the element id it resolved to. Locator-needing steps
    with no element_description get NO element (and no map entry) — the merge
    marks them found:false (planner defect → placeholder, accommodation (a)).
    """
    elements: List[Dict[str, Any]] = []
    by_description: Dict[str, Dict[str, Any]] = {}
    step_element_ids: Dict[int, str] = {}

    for index, raw_step in enumerate(steps):
        step = _as_dict(raw_step)
        if not step_needs_locator(step):
            continue
        description = (step.get("element_description") or "").strip()
        if not description:
            continue
        description = rewrite_form_description(description)
        action = action_for_keyword(step.get("keyword"))
        value = step.get("value")

        element = by_description.get(description)
        if element is None:
            element = {
                "id": f"elem_{len(elements) + 1}",
                "description": description,
                "action": action,
            }
            if action in _VALUE_ACTIONS and value not in (None, ""):
                element["value"] = value
            elements.append(element)
            by_description[description] = element
        elif _ACTION_PRECEDENCE[action] > _ACTION_PRECEDENCE[element["action"]]:
            element["action"] = action
            element.pop("value", None)
            if action in _VALUE_ACTIONS and value not in (None, ""):
                element["value"] = value

        step_element_ids[index] = element["id"]

    return elements, step_element_ids


# ---------------------------------------------------------------------------
# Merge (port checklist #7, #8)
# ---------------------------------------------------------------------------

def _entry_found(entry: Optional[Dict[str, Any]]) -> bool:
    """The single definition of "this element was found": the service said so
    AND handed over a usable locator. merge_locators and the stage summary
    both use it, so the SSE counts can never disagree with the merged steps.
    """
    return bool(entry and entry.get("found") and entry.get("best_locator"))


def merge_locators(
    steps: List[Any],
    step_element_ids: Dict[int, str],
    locator_mapping: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Staple the FULL locator_mapping contract onto each locator step.

    found:false, a missing mapping entry, or a missing element id all land on
    the same place: found=False and NO locator key — the Assembler's
    placeholder path. Non-locator steps pass through untouched (no found key).
    """
    merged: List[Dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        step = _as_dict(raw_step)
        # Only PlannedStep fields feed the models below. The normal path is
        # already-validated PlannedStep dumps, but the raw-JSON fallback in
        # crew._extract_plan_steps can carry LLM key drift (a hallucinated
        # 'locator'/'element_type'), which would collide with the explicit
        # kwargs — filter, never trust.
        plan_fields = {
            key: step[key] for key in step.keys() & PlannedStep.model_fields.keys()
        }
        if not step_needs_locator(step):
            merged.append(PlannedStep(**plan_fields).model_dump(exclude_none=True))
            continue

        element_id = step_element_ids.get(index)
        entry = locator_mapping.get(element_id) if element_id else None
        if not _entry_found(entry):
            merged.append(
                IdentifiedElement(**plan_fields, found=False).model_dump(exclude_none=True)
            )
            continue

        info = entry.get("element_info") or {}
        identified = IdentifiedElement(
            **plan_fields,
            locator=entry.get("best_locator"),
            found=True,
            # The old prompt's extraction rule, now code: element_type when
            # the service classified it, otherwise the observed tagName.
            element_type=entry.get("element_type") or info.get("tagName") or None,
            dropdown_framework=entry.get("dropdown_framework") or "",
            select_id=entry.get("select_id"),
            datepicker_framework=entry.get("datepicker_framework") or "",
            element_classes=info.get("className") or "",
            aria_invalid=str(info.get("ariaInvalid") or ""),
            parent_classes=info.get("parentClassName") or "",
            stability=entry.get("stability") or "stable",
            all_locators=entry.get("all_locators") or [],
            # ASTPP flags: stapled only when True (None → dropped by
            # exclude_none), mirroring the service's emitted-only-when-True
            # payload shape. row_anchor_ambiguous pairs with
            # stability='positional', which already drives the WARNING comment.
            visibility_filtered=entry.get("visibility_filtered") or None,
            row_anchored=entry.get("row_anchored") or None,
            row_anchor_ambiguous=entry.get("row_anchor_ambiguous") or None,
        )
        merged.append(identified.model_dump(exclude_none=True))
    return merged


# ---------------------------------------------------------------------------
# Orchestration: ONE tool call, no retry (port checklist #6, #7)
# ---------------------------------------------------------------------------

_batch_tool = None


def _default_run_tool(elements: List[Dict[str, Any]], url: str, user_query: str) -> Dict[str, Any]:
    """Call the real batch tool (lazy import keeps unit tests network-free)."""
    global _batch_tool
    if _batch_tool is None:
        from tools.browser_use_tool import BatchBrowserUseTool
        _batch_tool = BatchBrowserUseTool()
    return _batch_tool._run(elements=elements, url=url, user_query=user_query)


def identify_elements(
    steps: List[Any],
    user_query: str,
    run_tool: Optional[Callable[[List[Dict[str, Any]], str, str], Dict[str, Any]]] = None,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """The deterministic replacement for identify_elements_task.

    Builds the element specs, calls batch_browser_automation EXACTLY ONCE
    (or not at all when there is nothing to locate / no URL), and merges the
    full contract back onto the steps. Never raises on tool failure — every
    failure mode degrades to the found:false placeholder contract.

    Args:
        steps: plan steps (dicts or PlannedStep models), in plan order.
        user_query: the user's original query, forwarded VERBATIM.
        run_tool: injectable tool runner (tests); defaults to the real tool.
        on_progress: optional callback(progress, message) for SSE routing;
            its own errors are swallowed — progress must never break the run.
    """
    def notify(progress: int, message: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(progress, message)
        except Exception:
            logger.exception("identify_elements progress callback failed (ignored)")

    dict_steps = [_as_dict(s) for s in steps]
    notify(22, "🔍 Scanning webpage for interactive elements...")

    elements, step_element_ids = build_elements(dict_steps)
    url = extract_plan_url(dict_steps)
    locator_mapping: Dict[str, Dict[str, Any]] = {}

    if elements and url:
        notify(30, "🌐 Navigating to website and detecting elements...")
        runner = run_tool or _default_run_tool
        try:
            response = runner(elements, url, user_query) or {}
        except Exception:
            # CONTRACT: no retry, no reformulation — degrade to placeholders.
            logger.exception(
                "batch_browser_automation raised — steps fall back to the "
                "found:false placeholder path (no retry by design)"
            )
            response = {}
        if response.get("status") != "success":
            logger.warning(
                "batch_browser_automation returned status=%r — steps fall back "
                "to the found:false placeholder path (no retry by design)",
                response.get("status"),
            )
        locator_mapping = response.get("locator_mapping") or {}
    elif elements:
        logger.warning(
            "Plan has %d locator-needing elements but no navigation URL — "
            "skipping browser call; all get the found:false placeholder",
            len(elements),
        )

    # Same criterion merge_locators applies (_entry_found) — the reported
    # counts always match what the Assembler will actually see.
    found = sum(1 for e in elements if _entry_found(locator_mapping.get(e["id"])))
    if elements and url:
        notify(55, f"📍 Found {found} of {len(elements)} elements on the page")

    merged = merge_locators(dict_steps, step_element_ids, locator_mapping)
    # Honest stage completion: a failed tool call must not read as success —
    # progress still reaches 60 (forward-only ladder), only the text differs.
    if not elements:
        notify(60, "✅ No page elements needed for this test")
    elif found == len(elements):
        notify(60, "✅ All page elements identified")
    else:
        notify(60, f"⚠️ Identified {found} of {len(elements)} elements — "
                   "placeholders will mark the rest")

    return {
        "steps": merged,
        "summary": {
            "total_elements": len(elements),
            "found": found,
            "not_found": len(elements) - found,
        },
    }
