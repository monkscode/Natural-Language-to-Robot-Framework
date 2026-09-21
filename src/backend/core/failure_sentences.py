"""One plain-English sentence for a stored Robot Framework failure message.

Part A (E5b) made ``FailureClassifier`` label a failure message correctly
(``category``/``specific_type``). This module is the next step only: turning
a already-correct ``specific_type`` into one sentence a non-technical user can
read — what happened, then what to do, in the register ``provider_errors.py``
uses. Sentences are static strings; the raw failure text is never
interpolated into one, so a page-derived value (a selector, a URL, quoted
page text) can never leak into what gets shown.

Covers only the ``specific_type`` values Part A's corpus replay actually
produced, plus a handful of same-family types the replay saw zero of but the
classifier can still emit. Anything else — including the two deliberately
honest unknowns (``unknown``/``unclassified``) and the A1 composite structural
finding, whose real text is gone by the time a message reaches here — returns
None. A wrong diagnosis is worse than the raw message the caller already has.

Referenced by: (Task 3/4/7 of E5b Part B, not yet wired).
Depends on: crew_ai/optimization/failure_analyzer.py (FailureClassifier).
"""
import logging

from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

logger = logging.getLogger(__name__)

_classifier = FailureClassifier()


def _placeholder_never_resolved() -> str:
    return (
        "The test could not find a working locator for this element while it was "
        "being generated, so it left a placeholder that never matches anything on "
        "the page. Rephrase that step with a more specific description of the "
        "element and regenerate the test."
    )


def _page_load_timeout() -> str:
    return (
        "The page did not finish loading in time. Check that the site is "
        "reachable, or that step may need a longer wait."
    )


def _element_never_resolved() -> str:
    return (
        "The locator for this element never matched anything on the page. The "
        "element may not exist, or the page may have changed since the test was "
        "written."
    )


def _wrong_element_kind() -> str:
    return (
        "The element was found, but it does not support this action — for "
        "example, trying to select an option on something that is not a "
        "dropdown, or type into something that is not a text field."
    )


def _click_intercepted() -> str:
    return (
        "Something else on the page — a popup, banner, or overlay — was covering "
        "the element and blocked the click. Closing that overlay first may fix it."
    )


def _multiple_elements_found() -> str:
    return (
        "The locator matched more than one element on the page, so the step "
        "could not tell which one to act on. The step needs to identify a single, "
        "unique element."
    )


def _assertion_truth_failed() -> str:
    return "The test expected a condition to be true, but it was false."


def _invalid_selector_syntax() -> str:
    return (
        "The locator used in this step is not valid selector syntax, so it could "
        "not run. Fix the selector's syntax before rerunning."
    )


def _robot_data_error() -> str:
    return (
        "A value in this step could not be used as written — wrong type, an "
        "index that does not exist, or an expression that could not be "
        "evaluated. Check the step's arguments."
    )


def _navigation_network_error() -> str:
    return (
        "The browser could not reach the page — a network error happened while "
        "navigating, so nothing after that point in the test could run."
    )


def _attribute_missing() -> str:
    return "The attribute this step tried to read was not present on the element."


def _animation_or_disabled() -> str:
    return (
        "The element could not be interacted with when the action ran — it may "
        "have been disabled, or still animating into place."
    )


def _element_disabled() -> str:
    return (
        "The element was disabled when the action tried to run, so it could not "
        "be clicked or interacted with."
    )


def _element_not_editable() -> str:
    return (
        "The element was not editable — it may be read-only or disabled — so the "
        "test could not type into it."
    )


def _assertion_empty_actual() -> str:
    return (
        "The step meant to read a value from the page but got nothing back, so "
        "the check compared against an empty result."
    )


def _element_not_visible() -> str:
    return (
        "The element was found, but it was not visible — it may be hidden, "
        "off-screen, or covered by something else."
    )


def _element_still_present() -> str:
    return (
        "The test waited for an element to disappear or become hidden, but it "
        "was still there when the wait ran out."
    )


def _option_not_found() -> str:
    return "One or more of the options this step tried to select were not available."


def _wrong_keyword_name() -> str:
    return (
        "The test calls a keyword that Robot Framework does not recognize — "
        "check the keyword name for a typo or a missing library import."
    )


def _wrong_argument_count() -> str:
    return (
        "A keyword in the test was called with the wrong number of arguments. "
        "Check that step's arguments against the keyword's definition."
    )


# Ordered by the corpus counts in the Part A replay (highest first), then the
# same-family types the replay saw zero of. Order has no effect on the result
# — specific_type values are unique — but keeps the priority visible.
_SENTENCES = (
    ("placeholder_never_resolved", _placeholder_never_resolved),
    ("page_load_timeout", _page_load_timeout),
    ("element_never_resolved", _element_never_resolved),
    ("wrong_element_kind", _wrong_element_kind),
    ("click_intercepted", _click_intercepted),
    ("multiple_elements_found", _multiple_elements_found),
    ("assertion_truth_failed", _assertion_truth_failed),
    ("invalid_selector_syntax", _invalid_selector_syntax),
    ("robot_data_error", _robot_data_error),
    ("navigation_network_error", _navigation_network_error),
    ("attribute_missing", _attribute_missing),
    ("animation_or_disabled", _animation_or_disabled),
    ("element_disabled", _element_disabled),
    ("element_not_editable", _element_not_editable),
    ("assertion_empty_actual", _assertion_empty_actual),
    ("element_not_visible", _element_not_visible),
    ("element_still_present", _element_still_present),
    ("option_not_found", _option_not_found),
    ("wrong_keyword_name", _wrong_keyword_name),
    ("wrong_argument_count", _wrong_argument_count),
)

_SENTENCE_BY_TYPE = {specific_type: build for specific_type, build in _SENTENCES}


def failure_sentence(error_message: str | None) -> str | None:
    """One plain-English sentence for a stored failure message, or None.

    None means "no sentence for this" — the caller keeps whatever it already
    shows. A wrong diagnosis is worse than a raw message.
    """
    if not error_message:
        return None
    analysis = _classifier.classify(error_message)
    build = _SENTENCE_BY_TYPE.get(analysis.specific_type)
    if build is None:
        return None
    return build()
