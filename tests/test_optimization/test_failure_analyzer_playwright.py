"""Playwright-message classification (E5b).

The messages come from three places:
- the bench corpus: 44 distinct normalised forms cover all 251 recorded failures;
- live execution_records rows (2026-09-18) the corpus does not contain;
- Playwright 1.62's own message templates and Robot Framework 7.4.2's wording,
  for forms no run has produced yet.

Referenced by: crew_ai/optimization/failure_analyzer.py
Depends on: pytest
"""
from unittest.mock import patch

import pytest

from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

# --- corpus forms -----------------------------------------------------------
PLACEHOLDER = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('//PLACEHOLDER_FOR_sign_in_button')"
)
NEVER_RESOLVED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('text=\"OEM Solution\"')"
)
GOTO_TIMEOUT = (
    "TimeoutError: page.goto: Timeout 30000ms exceeded.\n"
    "Call log:\n  - navigating to \"https://example.com/\", waiting until \"load\""
)
NET_ERR = (
    "Error: page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://example.com/\n"
    "Call log:\n  - navigating to \"https://example.com/\", waiting until \"load\""
)
STRICT = (
    "Error: locator.click: Error: strict mode violation: locator('tbody > tr') "
    "resolved to 9 elements"
)
WRONG_KIND = (
    "Error: locator.fill: Error: Element is not an <input>, <textarea>, <select> "
    "or [contenteditable] and does not have a role allowing [aria-readonly]"
)
FRAME_EXPECTED = (
    "Error: locator.elementHandle: Error: Selector \"ol > li >> nth=0\" resolved to "
    "<li class=\"col\">...</li>, <iframe> was expected"
)
BAD_SELECTOR = (
    "Error: locator.fill: Unexpected token \"=\" while parsing css selector "
    "\"id=searchBox\". Did you mean to CSS.escape it?"
)
INTERCEPTED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('[title=\"Filter\"]')\n"
    "  - locator resolved to <button title=\"Filter\">...</button>\n"
    "  - <div class=\"overlay\">...</div> intercepts pointer events"
)
DISABLED = (
    "TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
    "Call log:\n  - waiting for locator('id=react-select-3-option-1')\n"
    "  - locator resolved to <div aria-disabled=\"true\">...</div>\n"
    "  - attempting click action\n"
    "    - waiting for element to be visible, enabled and stable\n"
    "    - element is not enabled"
)
NOT_EDITABLE = (
    "TimeoutError: locator.fill: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('id=customer_cdr_from_date')\n"
    "  - locator resolved to <input readonly>...</input>\n"
    "  - element is not editable"
)
WAITFOR_TIMEOUT = (
    "TimeoutError: locator.waitFor: Timeout 30000ms exceeded.\n"
    "Call log:\n  - waiting for locator('h6') to be visible"
)
ATTR_MISSING = "AttributeError: Attribute 'title' not found!"
ASSERTION_EMPTY = "'' does not contain 'buy milk'"
ROBOT_DATA = (
    "ValueError: Argument 'dictionary' got value 'Option 2' that cannot be "
    "converted to Mapping: Invalid expression."
)
INVALID_INDEX = (
    "String '${selected_options}[0]' used with invalid index 'label'. To use "
    "'[label]' as a literal value, it needs to be escaped like '\\[label]'."
)

# --- live execution_records forms the corpus does not contain -----------------
WAITFOR_HIDDEN = (
    "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
    "Call log:\n  - waiting for locator('h6') to be visible\n"
    "    4 × locator resolved to hidden <h6>…</h6>"
)
OUTSIDE_VIEWPORT = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('text=\"Accounts\"')\n"
    "  - locator resolved to <a href=\"#\">Accounts</a>\n"
    "  - attempting click action\n"
    "    - element is visible, enabled and stable\n"
    "    - scrolling into view if needed\n"
    "    - done scrolling\n"
    "    - element is outside of the viewport"
)
QUERY_SELECTOR_SYNTAX = (
    "Error: locator.fill: SyntaxError: Failed to execute 'querySelectorAll' on "
    "'Document': 'name:email' is not a valid selector."
)
ARG_COUNT_RF7 = "Keyword 'Browser.Keyboard Key' expected 2 arguments, got 3."

# --- Playwright 1.62 / Robot Framework 7.4.2 templates ------------------------
STILL_VISIBLE = (
    "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#spinner') to be hidden\n"
    "    3 × locator resolved to visible <div id=\"spinner\">…</div>"
)
STILL_ATTACHED = (
    "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#toast') to be detached\n"
    "    3 × locator resolved to hidden <div id=\"toast\">…</div>"
)
NOT_A_SELECT = (
    "Error: locator.selectOption: Error: Element is not a <select> element\n"
    "Call log:\n  - waiting for locator('#country')"
)
NOT_A_CHECKBOX = (
    "Error: locator.check: Error: Not a checkbox or radio button\n"
    "Call log:\n  - waiting for locator('#terms')"
)
SELECTOR_PARSE = (
    "Error: locator.click: Error while parsing selector `text=` - selector cannot be empty"
)
OPTION_DISABLED = (
    "TimeoutError: locator.selectOption: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#size')\n"
    "  - locator resolved to <select id=\"size\">…</select>\n"
    "  - option being selected is not enabled"
)
NOT_STABLE = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#btn')\n"
    "  - locator resolved to <button id=\"btn\">…</button>\n"
    "  - attempting click action\n"
    "    - element is not stable"
)
WAIT_FOR_URL = (
    "TimeoutError: page.waitForURL: Timeout 10000ms exceeded.\n"
    "=========================== logs ===========================\n"
    "waiting for navigation to \"**/dashboard\" until \"load\""
)
RETRY_WRAPPED_PLACEHOLDER = (
    "Keyword 'Click' failed after retrying for 30 seconds. The last error was: "
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('//PLACEHOLDER_FOR_login')"
)

# --- page-derived text inside a Playwright message must not decide the label ---
ENABLED_OPTION_NOT_VISIBLE = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('id=react-select-3-option-1')\n"
    "  - locator resolved to <div aria-disabled=\"false\" role=\"option\">…</div>\n"
    "  - attempting click action\n"
    "    - waiting for element to be visible, enabled and stable\n"
    "    - element is not visible"
)
INTERCEPTED_THEN_HIDDEN = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#menu-item')\n"
    "  - locator resolved to <a id=\"menu-item\">…</a>\n"
    "  - <div class=\"overlay\">…</div> intercepts pointer events\n"
    "  - retrying click action\n"
    "    - element is not visible"
)
SELECTOR_TEXT_SAYS_WAS_EXPECTED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('text=\"Delivery was expected\"')"
)


@pytest.mark.parametrize("message,category,specific_type", [
    # corpus
    (PLACEHOLDER, "C1", "placeholder_never_resolved"),
    (NEVER_RESOLVED, "C1", "element_never_resolved"),
    (WAITFOR_TIMEOUT, "C1", "element_never_resolved"),
    (GOTO_TIMEOUT, "D1", "page_load_timeout"),
    (NET_ERR, "D1", "navigation_network_error"),
    (STRICT, "C2", "multiple_elements_found"),
    (WRONG_KIND, "C7", "wrong_element_kind"),
    (FRAME_EXPECTED, "C7", "wrong_element_kind"),
    (BAD_SELECTOR, "B3", "invalid_selector_syntax"),
    (INTERCEPTED, "D4", "click_intercepted"),
    (DISABLED, "D3", "element_disabled"),
    (NOT_EDITABLE, "D3", "element_not_editable"),
    (ATTR_MISSING, "B3", "attribute_missing"),
    (ASSERTION_EMPTY, "E1", "assertion_empty_actual"),
    (ROBOT_DATA, "B3", "robot_data_error"),
    (INVALID_INDEX, "B3", "robot_data_error"),
    # live rows
    (WAITFOR_HIDDEN, "C1", "element_not_visible"),
    (OUTSIDE_VIEWPORT, "C1", "element_not_visible"),
    (QUERY_SELECTOR_SYNTAX, "B3", "invalid_selector_syntax"),
    (ARG_COUNT_RF7, "B4", "wrong_argument_count"),
    # Playwright 1.62 / RF 7.4.2 templates
    (STILL_VISIBLE, "E1", "element_still_present"),
    (STILL_ATTACHED, "E1", "element_still_present"),
    (NOT_A_SELECT, "C7", "wrong_element_kind"),
    (NOT_A_CHECKBOX, "C7", "wrong_element_kind"),
    (SELECTOR_PARSE, "B3", "invalid_selector_syntax"),
    (OPTION_DISABLED, "D3", "element_disabled"),
    (NOT_STABLE, "D3", "element_resolved_but_not_actionable"),
    (WAIT_FOR_URL, "D1", "page_load_timeout"),
    (RETRY_WRAPPED_PLACEHOLDER, "C1", "placeholder_never_resolved"),
    # page-derived text
    (ENABLED_OPTION_NOT_VISIBLE, "C1", "element_not_visible"),
    (INTERCEPTED_THEN_HIDDEN, "C1", "element_not_visible"),
    (SELECTOR_TEXT_SAYS_WAS_EXPECTED, "C1", "element_never_resolved"),
])
def test_playwright_messages_are_classified(message, category, specific_type):
    result = FailureClassifier().classify(message)

    assert (result.category, result.specific_type) == (category, specific_type), (
        f"{message[:60]!r} -> {result.category}/{result.specific_type}, "
        f"expected {category}/{specific_type}"
    )


@pytest.mark.parametrize("message,category,specific_type", [
    ("No keyword with name 'Open Browser' found", "B6", "library_mismatch"),
    ("No keyword with name 'Do Something' found", "B1", "wrong_keyword_name"),
    ("Variable '${missing}' not found", "A2", "missing_variable_assignment"),
    ("'enabled' is not a valid state", "B3", "invalid_parameter_value"),
    ("Navigation timeout exceeded", "D1", "page_load_timeout"),
    ("Element is not attached to the DOM", "C3", "stale_element"),
    # Generated names and quoted page text that contain the new rules' words.
    ("Variable '${attribute}' not found.", "A2", "missing_variable_assignment"),
    ("Variable '${search_locator_timeout}' not found.", "A2", "missing_variable_assignment"),
    ("No keyword with name 'Wait For Locator Timeout' found.", "B1", "wrong_keyword_name"),
    ("'Session timeout while waiting for OTP' should contain 'Welcome'",
     "E1", "assertion_contain_failed"),
    ("'Payment was expected by Friday' should be equal to 'Paid'",
     "E1", "assertion_equality_failed"),
    ("'Attribute not found' should be equal to 'Saved'", "E1", "assertion_equality_failed"),
    ("'Error evaluating expression' should contain 'OK'", "E1", "assertion_contain_failed"),
])
def test_existing_robot_level_rules_are_preserved(message, category, specific_type):
    """The new passes are additive: every pre-E5b rule still wins where it used to."""
    result = FailureClassifier().classify(message)

    assert (result.category, result.specific_type) == (category, specific_type), (
        f"regression: {message[:50]!r} -> {result.category}/{result.specific_type}"
    )


def test_result_keeps_the_whole_message():
    """The label comes from the last attempt, the stored message stays complete."""
    result = FailureClassifier().classify(RETRY_WRAPPED_PLACEHOLDER)

    assert result.error_message == RETRY_WRAPPED_PLACEHOLDER
    assert (result.source, result.confidence) == ("regex", 0.95)


def test_unrecognised_message_still_unknown():
    result = FailureClassifier().classify("something nobody has ever seen")

    assert result.category == "unknown"
    assert result.specific_type == "unclassified"


def test_non_empty_assertion_wording_stays_unknown():
    """Deliberate (E5b Decision Log): only the empty-actual assertion is added.

    RF 7's 'X' does not contain 'Y' with a real actual value is left unknown, so
    it is neither relabelled nor newly stored as an anti-pattern; the raw
    message is already the clearest thing to show a user.
    """
    result = FailureClassifier().classify("'Welcome back' does not contain 'Dashboard'")

    assert result.category == "unknown"


# --- Task 3 review, fix round 1: states Playwright 1.62 also logs, and page text
# --- in selectors / URLs that must not decide the label ------------------------
NOT_VISIBLE_THEN_NOT_STABLE = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#menu')\n"
    "  - locator resolved to <a id=\"menu\">…</a>\n"
    "  - attempting click action\n"
    "    - element is not visible\n"
    "  - retrying click action\n"
    "    12 × element is not stable"
)
INTERCEPTED_THEN_DETACHED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#menu')\n"
    "  - locator resolved to <a id=\"menu\">…</a>\n"
    "  - <div class=\"overlay\">…</div> intercepts pointer events\n"
    "  - element was detached from the DOM, retrying"
)
OPTION_NOT_FOUND = (
    "TimeoutError: locator.selectOption: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#size')\n"
    "  - locator resolved to <select id=\"size\">…</select>\n"
    "  - did not find some options\n"
    "  - retrying select option action"
)
SELECTOR_TEXT_SAYS_NOT_ENABLED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('text=\"Two-factor login is not enabled\"')"
)
WAIT_FOR_URL_WITH_LOCATOR_IN_URL = (
    "TimeoutError: page.waitForURL: Timeout 10000ms exceeded.\n"
    "=========================== logs ===========================\n"
    "waiting for navigation to \"**/store-locator\" until \"load\""
)
RETRY_WRAPPED_EMPTY_ASSERTION = (
    "Keyword 'Should Contain' failed after retrying for 30 seconds. "
    "The last error was: '' does not contain 'buy milk'"
)


@pytest.mark.parametrize("message,category,specific_type", [
    (NOT_VISIBLE_THEN_NOT_STABLE, "D3", "element_resolved_but_not_actionable"),
    (INTERCEPTED_THEN_DETACHED, "C3", "stale_element"),
    (OPTION_NOT_FOUND, "B3", "option_not_found"),
    (SELECTOR_TEXT_SAYS_NOT_ENABLED, "C1", "element_never_resolved"),
    (WAIT_FOR_URL_WITH_LOCATOR_IN_URL, "D1", "page_load_timeout"),
    (RETRY_WRAPPED_EMPTY_ASSERTION, "E1", "assertion_empty_actual"),
])
def test_last_logged_state_and_page_text(message, category, specific_type):
    result = FailureClassifier().classify(message)

    assert (result.category, result.specific_type) == (category, specific_type), (
        f"{message[:60]!r} -> {result.category}/{result.specific_type}, "
        f"expected {category}/{specific_type}"
    )


@pytest.mark.parametrize("message", [
    # A closed browser is not a locator timeout, even when the selector says "timeout".
    "Error: locator.click: Target page, context or browser has been closed\n"
    "Call log:\n  - waiting for locator('id=session-timeout-ok')",
    # Only Browser's own "Attribute 'x' not found" is an attribute read.
    "Element with attribute data-row not found in cache",
])
def test_lookalikes_stay_unknown(message):
    assert FailureClassifier().classify(message).category == "unknown"


def test_a_tie_on_position_keeps_the_first_listed_marker():
    """A longer variant of a marker lands at the same index; list order decides."""
    message = (
        "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
        "Call log:\n  - locator resolved to <button>Save</button>\n"
        "  - element is not stable, retrying"
    )
    markers = (
        ("element is not stable", "D3", "element_resolved_but_not_actionable"),
        ("element is not stable, retrying", "Z9", "would_win_on_alphabet"),
    )

    with patch.object(FailureClassifier, "WAIT_TAIL_MARKERS", markers):
        result = FailureClassifier().classify(message)

    assert (result.category, result.specific_type) == (
        "D3", "element_resolved_but_not_actionable")
