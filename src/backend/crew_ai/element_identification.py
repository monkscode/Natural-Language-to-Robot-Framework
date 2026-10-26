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
  - F1 marks: bs's `changed_by_action` mark and `came_back` flag are
    deliberately NOT stapled onto the steps or IdentifiedElement (the
    Assembler must never see them); they travel BESIDE the steps as the
    `change_waits` list that identify_elements returns (change_waits_from)
  - secret fields (#166): each plan typing step on a found field, with the
    NAME of an exact `{{NAME}}` value, also travels BESIDE the steps as
    `secret_fields` (secret_fields) for robot_code_normalizer.link_secret_fields

Steps arriving from crew._extract_plan_steps are PlanOutput-validated
(fail-fast, pre-browser); merge_locators additionally filters each step to
PlannedStep fields so raw-dict callers with LLM key drift can never crash
the model construction or leak fabricated locator keys.

FAILURE CONTRACT (Task 16 §F, deliberate): tool error OR found:false → the
step gets NO locator → the Assembler's existing placeholder path
(LOCATOR_RULES, Task 12). NO retry,
NO second tool call, NO reformulation. The old LLM once violated its own
call-ONCE rule and returned a fabricated locator for a non-existent element;
this module makes that impossible.

Referenced by: src/backend/crew_ai/crew.py (between the planner and assembler
kickoffs). Depends on: tasks.py models, tools.browser_use_tool (lazily).
"""

import logging
import re
from typing import Any, Callable

import structlog

from src.backend.core.secret_scrub import mask_values

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
_ACTION_EXACT: dict[str, str] = {
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
_ACTION_PREFIXES: tuple[tuple[str, str], ...] = (
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

# The only schemes that name a page the browser can be told to open. This is an
# allowlist because the blacklist it replaced ("about:", "data:", "javascript:",
# "file:", "chrome:") leaked every scheme outside those five: mailto: qualified
# on the dot rule, tel: on the single-token rule, and blob:/chrome-extension:/
# ws: likewise. about:blank was the measured defect, but nothing about it was
# special — it was just the one the planner happened to emit.
_NAVIGABLE_SCHEMES = frozenset({"http", "https"})

# A leading `<scheme>:` per RFC 3986, and the bare port that has to be told
# apart from one. `urlsplit` reads "localhost:3000" as scheme='localhost' and
# "example.com:443/path" as scheme='example.com', so a scheme allowlist built
# on it would discard exactly the internal destinations this framework is
# pointed at.
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_PORT_RE = re.compile(r"^\d{1,5}(?:[/?#]|$)")

# The scheme sitting directly in front of an embedded URL. Anchored to the END
# of the text before the match, so it sees the wrapper in both "blob:https://x"
# and "url=blob:https://x" — the leading-scheme test only saw the first.
_PRECEDING_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):$")

# An absolute URL buried inside a longer value. PlannedStep has no browser
# field, so the planner packs browser params into `value` — 21 of the 30 plans
# in the 2026-07-31 baseline do. Usually they ride on their own step and the
# destination stays separate; when the destination is packed in beside them
# ("chromium, headless=True, url=https://...") the token-based passes cannot
# see it. Restricted to the navigable schemes so this cannot readmit
# about:blank or mailto:, which the passes above reject deliberately.
#
# The comma is NOT a terminator: it is legal in an HTTP path or query, and
# excluding it truncated "?tags=python,robot" to "?tags=python" — a valid URL
# for a DIFFERENT page, which navigates and tests the wrong thing instead of
# failing loudly. A genuinely trailing comma is still removed, by the rstrip
# below. The semicolon stays excluded: it separates packed fields
# ("url=https://x;browser=chromium") and has no such rescue.
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s;'\"<>]+", re.IGNORECASE)

# Punctuation the planner leaves glued to a URL when it packs one into a list
# or a sentence. A trailing dot is sentence punctuation here, never a root-zone
# label — the browser service would resolve the mangled host either way.
_VALUE_SEPARATORS = ",;."

# Schemes to reject even when a bare port is what follows the colon. This is
# the one shape the port test cannot resolve on structure: `tel` and
# `localhost` are both valid host labels, so "tel:12345" and "localhost:12345"
# are the same string shape and only the name separates them.
#
# The set is a judgement about which labels could plausibly be a HOST, not a
# complete list of non-navigable schemes — it cannot be one, there are some
# 380 registered schemes and any list would still leak the 381st. It does not
# need to be: the allowlist decides every value whose payload is not a bare
# port, so this only breaks the numeric tie.
#
# Deliberately absent: ftp, ws, wss, chrome-extension, view-source. Their
# real syntax carries an authority ("ftp://host/path"), which the allowlist
# already rejects. Only the authority-less "ftp:12345" reaches here, and that
# is not a valid URI of any of those schemes — while "ftp:21" and "ws:8080"
# are entirely ordinary intranet targets. Listing them would trade an input
# that cannot occur for one that can, and both mistakes cost the same thing:
# a skipped browser call and found:false on every element.
_NUMERIC_PAYLOAD_SCHEMES = frozenset({
    "tel", "sms", "fax", "callto", "mailto", "data", "blob",
})


def _explicit_scheme(candidate: str) -> str | None:
    """The candidate's leading URI scheme, lowercased, or None if it has none.

    `<label>:<port>` is a host, not a scheme, unless the label is a scheme that
    takes a numeric payload. Deciding that on payload LENGTH does not work: it
    rejected "tel:123456" and accepted "tel:12345" on nothing but digit count.
    """
    match = _SCHEME_RE.match(candidate)
    if not match:
        return None
    scheme = match.group(1).lower()
    if _PORT_RE.match(candidate[match.end():]) and scheme not in _NUMERIC_PAYLOAD_SCHEMES:
        return None
    return scheme


def _preceding_scheme(prefix: str) -> str | None:
    """The URI scheme the text ends on, lowercased, or None."""
    match = _PRECEDING_SCHEME_RE.search(prefix)
    return match.group(1).lower() if match else None


def _normalize_keyword(keyword: str | None) -> str:
    return " ".join((keyword or "").split()).lower()


def _as_dict(step: Any) -> dict[str, Any]:
    """Accept plain dicts or pydantic models (PlanOutput.steps)."""
    if hasattr(step, "model_dump"):
        return step.model_dump()
    return dict(step)


def _prefix_action(keyword: str) -> str | None:
    for prefix, action in _ACTION_PREFIXES:
        if keyword.startswith(prefix):
            return action
    return None


def step_needs_locator(step: dict[str, Any]) -> bool:
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


def _url_candidate(value: Any) -> str | None:
    """The URL hiding in a step's value, or None when there isn't one.

    Only the first whitespace-delimited token is considered: planner values
    sometimes carry trailing prose that strip() cannot reach (one captured
    run: "https://example.org/    commit"). A value carrying an
    explicit scheme qualifies only when that scheme is navigable — the browser
    service completes a *missing* scheme (prompts/workflow.py:106), so
    "about:blank" or "mailto:sales@x.com" would otherwise be handed to it as a
    real navigation target. A bare port is not a scheme (see _SCHEME_RE).

    A scheme-less value qualifies when it carries a dot OR is the whole value
    on its own. Token count is what separates a site name from prose:
    "Flipkart" is one token, "the login page" is three. A dot alone cannot
    tell them apart, and requiring one rejected the planner's own shipped
    exemplar ("Open Browser -> Flipkart", prompts/components.py:699) — which
    returns None and skips the browser call entirely, handing every element a
    found:false placeholder. No bench query is scheme-less, so the 96.7% gate
    cannot see that class of failure; it has to be held here.
    """
    tokens = (value or "").strip().split()
    if not tokens:
        return None
    # A packed value leaves its separator glued to the URL
    # ("https://books.toscrape.com, browser=chromium"), which otherwise rode
    # through into the navigation target as a silent mangle rather than a
    # loud skip.
    candidate = tokens[0].rstrip(_VALUE_SEPARATORS)
    if not candidate:
        return None
    scheme = _explicit_scheme(candidate)
    if scheme:
        return candidate if scheme in _NAVIGABLE_SCHEMES else None
    if "." in candidate:
        return candidate
    return candidate if len(tokens) == 1 else None


def extract_plan_url(steps: list[Any], user_query: str = "") -> str | None:
    """The URL is the first navigation step that carries one.

    Four passes, most trusted first; the plan always outranks the query. Only
    the last pass guesses: it takes the FIRST http(s) URL in the user's own
    words, which is a guess when the query names more than one.

    A navigation step whose value cannot name a page — a non-navigable
    scheme, or prose — is skipped rather than trusted (see _url_candidate for
    exactly what qualifies; a scheme-less single token still does). On 4 of
    897 captured runs the planner emitted
    "New Page -> about:blank" and put the real URL on the next step, so
    first-value-wins discarded it and the agent was told to open
    https://about:blank. It reached the right page anyway — the browser-use
    LLM overrode the instruction and read the URL out of the goal text on 3
    of 3 runs. That repair is undeclared model behaviour, not a contract, and
    it is the only reason the defect never showed. The guard belongs here,
    where it is deterministic.

    Fallback: the planner's keyword vocabulary is a free string, so the URL
    sometimes rides on a non-navigation step (bench 2026-07-11 q03: keyword
    "New Browser", value=<url> — the whitelist miss skipped the browser call
    and every element got a found:false placeholder). If no navigation step
    carries one, the first *literal* URL among the step values is taken;
    non-URL values (browser names, input text) are never eligible.

    Last resort: the user's own query. Every pass above reads the plan, so all
    of them fail together when the planner writes no destination anywhere —
    34 of 1,260 captured runs were one prompt quirk away from exactly that
    (see the user_query pass below).
    """
    dict_steps = [_as_dict(step) for step in steps]
    for find_url in (_url_on_a_navigation_step,
                     _url_led_by_a_step_value,
                     _url_embedded_in_a_step_value):
        url = find_url(dict_steps)
        if url:
            return url
    # Last resort — the user's own words. Every pass above reads the plan, so
    # they fail together when the planner names no destination anywhere, and
    # the run is then already lost: identify_elements skips the browser call
    # and hands every element a found:false placeholder.
    #
    # This is not a new capability, it is an existing one made deliberate.
    # Measured over 1,260 captured runs with parseable plans, 34 reached a URL
    # ONLY because the browser-launch step's `value` carried one; blanking it
    # makes all three passes above return None. Nothing put it there on
    # purpose — PLANNING_OUTPUT_RULES rule 6 demanded `browser`/`headless`
    # keys that PlannedStep does not define, and structured output constrains
    # the reply to the declared properties, so the planner improvised into
    # `value` and the destination sometimes rode along. All 34 of those user
    # queries state the URL in plain text.
    #
    # Runs last on purpose: the plan is the more specific signal, and output
    # rule 5's search-engine default must be able to send a URL-bearing query
    # somewhere other than the URL it names.
    return _recover_embedded_url(user_query)


def _url_on_a_navigation_step(dict_steps: list[dict[str, Any]]) -> str | None:
    """Pass 1 — the destination where the contract says it lives."""
    for step in dict_steps:
        if _normalize_keyword(step.get("keyword")) in _NAVIGATION_KEYWORDS:
            candidate = _url_candidate(step.get("value"))
            if candidate:
                return candidate
    return None


def _url_led_by_a_step_value(dict_steps: list[dict[str, Any]]) -> str | None:
    """Pass 2 — a value that *starts* with a literal URL, on any keyword.

    The planner's keyword vocabulary is a free string, so the URL sometimes
    rides on a non-navigation step (bench 2026-07-11 q03: keyword "New
    Browser", value=<url> — the whitelist miss skipped the browser call and
    every element got a found:false placeholder).
    """
    for step in dict_steps:
        candidate = _url_candidate(step.get("value"))
        if candidate and _explicit_scheme(candidate) in _NAVIGABLE_SCHEMES:
            return candidate
    return None


def _url_embedded_in_a_step_value(dict_steps: list[dict[str, Any]]) -> str | None:
    """Pass 3 — a URL buried mid-value, e.g. "chromium, url=https://...".

    Steps that TYPE a value are skipped: scanning every token would undo the
    first-token rule _url_candidate exists to enforce, and turn the URL a user
    wants entered in a search box into the page to open. Lossless — across
    1,313 captured runs this pass fires 5 times, all on New Browser steps.
    """
    for step in dict_steps:
        if action_for_keyword(step.get("keyword") or "") in _VALUE_ACTIONS:
            continue
        recovered = _recover_embedded_url(step.get("value"))
        if recovered:
            return recovered
    return None


def _recover_embedded_url(text: Any) -> str | None:
    """The first absolute http(s) URL inside free text, or None.

    Scanned per whitespace token so a wrapper scheme cannot smuggle its payload
    past the allowlist: "blob:https://x/9f8e" and "view-source:https://x" embed
    a real URL inside a scheme the passes above reject on purpose.

    The scheme tested is the one IMMEDIATELY BEFORE the match, not the token's
    leading one. Testing the token's start looked equivalent and was not:
    "url=blob:https://x" has no token-leading scheme at all — `=` is not a
    scheme character, so the match fails and the guard waved it through — and
    packed field labels are precisely the shape this pass exists to read.
    """
    for token in str(text or "").split():
        match = _EMBEDDED_URL_RE.search(token)
        if not match:
            continue
        scheme = _preceding_scheme(token[:match.start()])
        if scheme and scheme not in _NAVIGABLE_SCHEMES:
            continue
        recovered = match.group(0).rstrip(_VALUE_SEPARATORS)
        if recovered:
            return recovered
    return None


def adopt_recovered_url(steps: list[dict[str, Any]], url: str) -> bool:
    """Write a URL recovered from the user query back onto the plan.

    extract_plan_url feeds the browser call, but that is only half the run: the
    Assembler is built from the merged steps and nothing else
    (tasks.assemble_code_task takes identified_steps_json — no query, no URL
    argument). A URL that reaches the browser call but not the plan locates
    every element and then generates a test that never navigates.

    Measured on the 34 captured runs the query pass exists to serve: with the
    rule-6 improvisation gone, 0 of 34 carry a navigation step with a URL and 0
    carry any http string anywhere — yet 34 of 34 emitted `New Page <url>`
    today, because the Assembler read it out of the launch step's value. So the
    plan is the channel, and this puts the destination back on it.

    A navigation step is the right slot and wins; the launch step is the
    fallback (the shape the Assembler already handled 34 of 34 times). The
    value is overwritten unconditionally, which is safe only because the caller
    reaches here exclusively after every plan pass returned None — so whatever
    is there is prose, about:blank or nothing, never a usable destination.
    Returns False when the plan has no slot at all.
    """
    fallback: dict[str, Any] | None = None
    for step in steps:
        keyword = _normalize_keyword(step.get("keyword"))
        if keyword in _NAVIGATION_KEYWORDS:
            step["value"] = url
            return True
        if fallback is None and keyword == "new browser":
            fallback = step
    if fallback is not None:
        fallback["value"] = url
        return True
    return False


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

def _apply_action_value(element: dict[str, Any], action: str, value: Any) -> None:
    """Attach the step value only for actions that consume one (fill/select)."""
    if action in _VALUE_ACTIONS and value not in (None, ""):
        element["value"] = value


def build_elements(steps: list[Any]) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Build the batch-tool element specs from the plan.

    Returns (elements, step_element_ids) where step_element_ids maps a step's
    index in `steps` to the element id it resolved to. Locator-needing steps
    with no element_description get NO element (and no map entry) — the merge
    marks them found:false (planner defect → placeholder, accommodation (a)).

    ORPHAN NAVIGATION: a navigation step that no element's action triggers is
    stapled to the next element created, as the optional `navigate_before`
    key, so the browser service can render the instruction at its sequence
    position (prompts/workflow.py). Only navigations AFTER the first element
    qualify — one before it is already the workflow's `url` via
    extract_plan_url, and re-rendering it would tell the agent to navigate
    where it is already standing.

    Deliberately NOT per-element URL pinning. An element found on a new page
    because the PREVIOUS element's action changed pages is the documented
    model working (EXAMPLE_WORKFLOW_TEMPLATE: "elem_1's action caused a page
    change, so elem_2 is naturally found on the new page"); pinning it to the
    plan's most recent navigation would force the agent back and destroy the
    state that action created.
    """
    elements: list[dict[str, Any]] = []
    by_description: dict[str, dict[str, Any]] = {}
    step_element_ids: dict[int, str] = {}
    pending_navigation: str | None = None

    for index, raw_step in enumerate(steps):
        step = _as_dict(raw_step)

        if _normalize_keyword(step.get("keyword")) in _NAVIGATION_KEYWORDS:
            # No elements yet ⇒ nothing has been located ⇒ this is the
            # pre-element navigation extract_plan_url already returns.
            # Consecutive navigations: the LAST is where the agent ends up, so
            # it overwrites — rendering an earlier one would send it back to a
            # page the plan has already left. The URL bar is the same one
            # extract_plan_url applies, so prose and non-navigable schemes
            # ("about:blank") attach nothing rather than becoming a real
            # navigation target.
            if elements:
                candidate = _url_candidate(step.get("value"))
                if candidate:
                    pending_navigation = candidate
            continue

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
            if pending_navigation:
                element["navigate_before"] = pending_navigation
            _apply_action_value(element, action, value)
            elements.append(element)
            by_description[description] = element
        else:
            if pending_navigation:
                # The duplicate resolves to an element that already rendered
                # EARLIER in the list, so the navigation cannot be placed at
                # its own sequence position. Keep the first occurrence —
                # splitting the element would change element counts, the step
                # budget and cost on speculation. No bench query reuses a
                # description across pages; this logs it if one ever does.
                logger.warning(
                    "Plan step %d navigates to %s and then reuses element "
                    "description %r from an earlier page — keeping the first "
                    "occurrence and dropping the navigation",
                    index, pending_navigation, description,
                )
            if _ACTION_PRECEDENCE[action] > _ACTION_PRECEDENCE[element["action"]]:
                element["action"] = action
                element.pop("value", None)
                _apply_action_value(element, action, value)

        # Consumed or deliberately dropped — either way it belonged to this
        # element and must not leak onto the next one.
        pending_navigation = None

        step_element_ids[index] = element["id"]

    return elements, step_element_ids


# ---------------------------------------------------------------------------
# Merge (port checklist #7, #8)
# ---------------------------------------------------------------------------

def _entry_found(entry: dict[str, Any] | None) -> bool:
    """The single definition of "this element was found": the service said so
    AND handed over a usable locator. merge_locators and the stage summary
    both use it, so the SSE counts can never disagree with the merged steps.
    """
    return bool(entry and entry.get("found") and entry.get("best_locator"))


def merge_locators(
    steps: list[Any],
    step_element_ids: dict[int, str],
    locator_mapping: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Staple the FULL locator_mapping contract onto each locator step.

    found:false, a missing mapping entry, or a missing element id all land on
    the same place: found=False and NO locator key — the Assembler's
    placeholder path. Non-locator steps pass through untouched (no found key).
    """
    merged: list[dict[str, Any]] = []
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

        merged.append(_identified_from_entry(plan_fields, entry))
    return merged


def _identified_from_entry(
    plan_fields: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    """Full-contract IdentifiedElement dump for a found mapping entry."""
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
    return identified.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Orchestration: ONE tool call, no retry (port checklist #6, #7)
# ---------------------------------------------------------------------------

_batch_tool = None


def _default_run_tool(elements: list[dict[str, Any]], url: str, user_query: str) -> dict[str, Any]:
    """Call the real batch tool (lazy import keeps unit tests network-free)."""
    global _batch_tool
    if _batch_tool is None:
        from tools.browser_use_tool import BatchBrowserUseTool
        _batch_tool = BatchBrowserUseTool()
    return _batch_tool._run(elements=elements, url=url, user_query=user_query)


_SECRET_VALUE_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]{0,63})\s*\}\}")


def _hand_secret_values(elements: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Copy of `elements` where a value that is exactly `{{NAME}}` is the org's stored value,
    plus {stored value: "{{NAME}}"} for the swap back. Nothing to hand over (no such value, no
    user, no org, name no longer stored) returns the same list and {}. A lost or unset key
    raises (SecretsUnavailable; an unreadable stored value as a RuntimeError sentence) and fails the generation."""
    wanted = {}
    for index, element in enumerate(elements):
        value = element.get("value")
        match = _SECRET_VALUE_RE.fullmatch(value) if isinstance(value, str) else None
        if match:
            wanted[index] = match.group(1).upper()
    if not wanted:
        return elements, {}
    from cryptography.fernet import InvalidToken
    from src.backend.auth import secrets  # lazy: the auth stack stays out of plain element tests

    org_id = secrets.org_of(structlog.contextvars.get_contextvars().get("user_id"))
    if not org_id:
        return elements, {}
    try:
        stored = secrets.get_values(org_id, set(wanted.values()))
    except InvalidToken:  # carries no text: the user would see a blank "An error occurred: "
        raise RuntimeError(
            "This request's secrets cannot be read with this server's SECRETS_ENCRYPTION_KEY. "
            "Add them again on the Secrets page, then try again."
        ) from None
    if not stored:
        return elements, {}
    handed = [
        dict(element, value=stored[wanted[i]]) if wanted.get(i) in stored else element
        for i, element in enumerate(elements)
    ]
    return handed, {stored[name]: "{{" + name + "}}" for name in sorted(stored, reverse=True)}


def _swap_values_back(node: Any, name_of_value: dict[str, str]) -> Any:
    """`node` with every stored value, in every string at any depth (dict keys too), replaced by
    its `{{NAME}}`; longest value first, in ONE pass, so nothing is half-swapped or swapped twice."""
    if not name_of_value:
        return node
    pattern = re.compile("|".join(re.escape(v) for v in sorted(name_of_value, key=len, reverse=True)))

    def swap(item: Any) -> Any:
        if isinstance(item, str):
            return pattern.sub(lambda m: name_of_value[m.group(0)], item)
        if isinstance(item, dict):
            return {swap(k): swap(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return type(item)(swap(v) for v in item)
        return item

    return swap(node)


def secret_fields(merged: list[dict[str, Any]]) -> tuple[tuple[str, str | None], ...]:
    """The plan's typing steps on fields browser-service found, as `(locator, NAME or None)` in plan order:
    NAME (upper-cased) when the step's value is exactly `{{NAME}}`, None for any other value; () when no
    step types a secret. Kept beside the steps like not_found (#166: link_secret_fields reads it)."""
    fields = []
    for step in merged:
        if step.get("found") and step.get("locator") and action_for_keyword(step.get("keyword") or "") == "input":
            value = step.get("value")
            match = _SECRET_VALUE_RE.fullmatch(value) if isinstance(value, str) else None
            fields.append((step["locator"], match.group(1).upper() if match else None))
    return tuple(fields) if any(name for _, name in fields) else ()


def _fetch_locator_mapping(
    elements: list[dict[str, Any]],
    url: str,
    user_query: str,
    run_tool: Callable[[list[dict[str, Any]], str, str], dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """ONE batch tool call; every failure mode returns {} (placeholder path)."""
    runner = run_tool or _default_run_tool
    # shortcut: bs still types what it is handed; the bs pin PR sends the name + a "secrets" map instead
    elements, name_of_value = _hand_secret_values(elements)
    try:
        response = runner(elements, url, user_query) or {}
    except Exception as exc:
        # CONTRACT: no retry, no reformulation — degrade to placeholders.
        if name_of_value:
            # No traceback, and the text masked: an error that echoes the request could hold a value.
            logger.error(
                "batch_browser_automation raised %s: %s — steps fall back to the "
                "found:false placeholder path (no retry by design)",
                type(exc).__name__, mask_values(str(exc), name_of_value),
            )
        else:
            logger.exception(
                "batch_browser_automation raised — steps fall back to the "
                "found:false placeholder path (no retry by design)"
            )
        response = {}
    response = _swap_values_back(response, name_of_value)
    if response.get("status") != "success":
        logger.warning(
            "batch_browser_automation returned status=%r — steps fall back "
            "to the found:false placeholder path (no retry by design)",
            response.get("status"),
        )
    return response.get("locator_mapping") or {}


_ACTIONS_BS_PERFORMS = frozenset({"click", "input", "select"})


def _came_back_element(step_element_ids: dict[int, str], locator_mapping: dict[str, dict[str, Any]]) -> str | None:
    """The first element of this plan, in plan order, that bs found and flagged `came_back`, else None."""
    for element_id in dict.fromkeys(step_element_ids.values()):
        entry = locator_mapping.get(element_id)
        if _entry_found(entry) and entry.get("came_back"):
            return element_id
    return None


def _read_again_element(steps: list[Any], step_element_ids: dict[int, str]) -> str | None:
    """The first element that has a Get Text step after a step bs performs only once it has located
    that element, else None.

    `build_elements` folds the steps that share one description into one element, which bs locates
    ONCE, at the element's first step. A later `Get Text` of it, after a navigation or after an
    action on another element first met after that first step, reads a page bs never saw.
    """
    first_step: dict[str, int] = {}
    for index in sorted(step_element_ids):
        first_step.setdefault(step_element_ids[index], index)
    keywords = [_normalize_keyword(_as_dict(raw).get("keyword")) for raw in steps]
    for index in sorted(step_element_ids):
        element_id = step_element_ids[index]
        first = first_step[element_id]
        if index <= first or keywords[index] != "get text":
            continue
        for between in range(first + 1, index):
            if keywords[between] in _NAVIGATION_KEYWORDS:
                return element_id
            other = step_element_ids.get(between)
            if (
                other is not None and first_step[other] > first
                and action_for_keyword(keywords[between]) in _ACTIONS_BS_PERFORMS
            ):
                return element_id
    return None


def change_waits_from(
    steps: list[Any],
    step_element_ids: dict[int, str],
    locator_mapping: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """F1: (read locator, action locator) for each Get Text step bs marked as changed by an earlier action.

    The marks travel BESIDE the merged steps: the assembler never sees them, so it can neither copy nor drop
    them. A mark whose action element was not found, or that names the read itself, gives no wait.

    The WHOLE test gets no waits, with one WARNING, when a wait could stop on an in-between value of a
    test that undoes itself: bs flagged a found element of this plan `came_back` (its value came back to
    an earlier value), or one element is read again after an action bs performed after locating it
    (`_read_again_element`). The WARNING is logged only when this drops at least one wait: a test with
    nothing marked returns `[]` silently, as it always did.
    """
    waits = _marked_waits(steps, step_element_ids, locator_mapping)
    if not waits:
        return []
    came_back = _came_back_element(step_element_ids, locator_mapping)
    if came_back is not None:
        logger.warning(
            "Change-wait: no waits for this test — browser-service saw the value of %s come back to an "
            "earlier value (signal: change-wait-refused)", came_back)
        return []
    read_again = _read_again_element(steps, step_element_ids)
    if read_again is not None:
        logger.warning(
            "Change-wait: no waits for this test — %s is read again after an action browser-service "
            "performed after locating it (signal: change-wait-refused)", read_again)
        return []
    return waits


def _marked_waits(
    steps: list[Any],
    step_element_ids: dict[int, str],
    locator_mapping: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """The deduplicated (read locator, action locator) pairs of the marked Get Text steps, in plan order."""
    waits: list[dict[str, str]] = []
    for index, raw in enumerate(steps):
        step = _as_dict(raw)
        if _normalize_keyword(step.get("keyword")) != "get text":
            continue
        entry = locator_mapping.get(step_element_ids.get(index, ""))
        if not _entry_found(entry):
            continue
        action_id = entry.get("changed_by_action")
        action_entry = locator_mapping.get(action_id) if action_id else None
        if action_id == step_element_ids.get(index) or not _entry_found(action_entry):
            continue
        wait = {"read_locator": entry["best_locator"], "action_locator": action_entry["best_locator"]}
        if wait not in waits:
            waits.append(wait)
    return waits


def _stage_summary_message(found: int, total: int) -> str:
    """Honest stage completion: a failed tool call must not read as success."""
    if total == 0:
        return "✅ No page elements needed for this test"
    if found == total:
        return "✅ All page elements identified"
    return f"⚠️ Identified {found} of {total} elements"


def not_found_entries(
    steps: list[dict[str, Any]],
    step_element_ids: dict[int, str],
    locator_mapping: dict[str, dict[str, Any]],
    url: str | None,
) -> list[dict[str, Any]]:
    """The locator steps that end up without a working locator, and why, in plan order.

    One entry per element (folded steps share one) and one per step that names no
    element. The description is the planner's element_description as written (the
    first step's), never the form rewrite build_elements keys on. A step's value is never recorded: it may
    be a password.
    """
    entries: list[dict[str, Any]] = []
    listed: set[str] = set()
    for index, raw_step in enumerate(steps):
        step = _as_dict(raw_step)
        if not step_needs_locator(step):
            continue
        element_id = step_element_ids.get(index)
        if element_id is None:
            entries.append({"cause": "no_description", "keyword": step.get("keyword")})
            continue
        if element_id in listed:
            continue
        if not url:  # the same test identify_elements applies before calling the tool
            cause = "no_url"
        elif element_id not in locator_mapping:
            # A tool error, a raise and a crash-shaped "success" all land here; a
            # normal run backfills a record for every element it was given.
            cause = "not_checked"
        elif not _entry_found(locator_mapping[element_id]):
            cause = "not_found"
        else:
            continue
        listed.add(element_id)
        entries.append({"cause": cause, "description": (step.get("element_description") or "").strip()})
    return entries


def not_found_lines(entries: list[dict[str, Any]]) -> list[str]:
    """The Generation Logs copy: title, one line per cause present, footer. [] for no entries."""
    if not entries:
        return []
    n = len(entries)
    lines = [
        "Delivered — 1 element was not found while this test was generated" if n == 1
        else f"Delivered — {n} elements were not found while this test was generated"
    ]

    def of(cause: str) -> list[dict[str, Any]]:
        return [e for e in entries if e["cause"] == cause]

    def quoted(group: list[dict[str, Any]]) -> str:
        return ", ".join(f"“{e['description']}”" for e in group)

    if group := of("not_found"):
        if len(group) == 1:
            lines.append(
                f"{quoted(group)} — not found on the page, so the test has no working locator for it "
                "and will most likely fail at that step. If the web address is right and the element "
                "is on the page, describe it more specifically and generate again.")
        else:
            lines.append(
                f"{quoted(group)} — not found on the page, so the test has no working locator for them "
                "and will most likely fail at those steps. If the web address is right and the elements "
                "are on the page, describe them more specifically and generate again.")
    if group := of("not_checked"):
        pronoun = "it" if len(group) == 1 else "them"
        lines.append(
            f"{quoted(group)} — the page could not be checked just now, so no locator was found for "
            f"{pronoun}. Generate the test again in a minute.")
    if group := of("no_url"):
        lines.append(
            f"{quoted(group)} — not checked: your description has no web address, so no page was "
            "opened. Add the page's address (for example https://example.com) and generate again.")
    if group := of("no_description"):
        if len(group) == 1:
            lines.append(
                f"A “{group[0]['keyword']}” step does not name an element. Describe the element in "
                "your test description and generate again.")
        else:
            keywords = ", ".join(f"“{k}”" for k in dict.fromkeys(e["keyword"] for e in group))
            lines.append(
                f"{len(group)} steps do not name an element ({keywords}). Describe each element in "
                "your test description and generate again.")
    lines.append("You can still edit the code or run the test.")
    return lines


def not_found_notice(count: int) -> str:
    """The Generated Code card's subtitle."""
    noun = "1 element was" if count == 1 else f"{count} elements were"
    return f"{noun} not found while generating — details in Generation Logs below"


def identify_elements(
    steps: list[Any],
    user_query: str,
    run_tool: Callable[[list[dict[str, Any]], str, str], dict[str, Any]] | None = None,
    on_progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
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
    if url is None:
        # Last resort. The plan named no destination, so fall back to the user's
        # own words — and put the answer back on the plan, because the Assembler
        # reads the destination from there and from nowhere else.
        url = extract_plan_url(dict_steps, user_query)
        if url is not None and not adopt_recovered_url(dict_steps, url):
            logger.warning(
                "Recovered URL %s from the user query but the plan has no "
                "navigation or launch step to carry it — the browser call will "
                "run, the generated test will not navigate", url)
    locator_mapping: dict[str, dict[str, Any]] = {}

    if elements and url:
        notify(30, "🌐 Navigating to website and detecting elements...")
        locator_mapping = _fetch_locator_mapping(elements, url, user_query, run_tool)
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
    # Kept beside the steps like change_waits: the assembler never sees it. A locator
    # step that names no element has no element, so it is added to the count here.
    not_found = not_found_entries(dict_steps, step_element_ids, locator_mapping, url)
    no_description = sum(1 for e in not_found if e["cause"] == "no_description")
    # Progress still reaches 60 either way (forward-only ladder) — only the
    # completion text differs on partial/failed identification.
    notify(60, _stage_summary_message(found, len(elements) + no_description))

    # F1: browser-service's marks are external input — a malformed one costs
    # the wait, never the generation.
    try:
        change_waits = change_waits_from(dict_steps, step_element_ids, locator_mapping)
    except Exception:
        logger.warning("change-wait marks skipped — test generated without them", exc_info=True)
        change_waits = []

    return {
        "steps": merged,
        "summary": {
            "total_elements": len(elements),
            "found": found,
            "not_found": len(elements) - found,
        },
        "change_waits": change_waits,
        "not_found": not_found,
        "secret_fields": secret_fields(merged),
    }
