"""Tests for the specific_type -> plain-English sentence map.

Every message here is copied from a message already committed in
tests/test_optimization/test_failure_analyzer.py or
tests/test_optimization/test_failure_analyzer_playwright.py (line noted per
constant below), so each is a real shape the classifier is already proven to
route to the specific_type the test claims — never invented, never a live
corpus row.

Referenced by: none yet (Task 2 of E5b Part B).
Depends on: src/backend/core/failure_sentences.py, pytest.
"""
import pytest

from src.backend.core.failure_sentences import failure_sentence

# --- messages, copied from tests/test_optimization/test_failure_analyzer_playwright.py ---

# lines 19-22
PLACEHOLDER = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('//PLACEHOLDER_FOR_sign_in_button')"
)
# lines 23-26
NEVER_RESOLVED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('text=\"OEM Solution\"')"
)
# lines 27-30
GOTO_TIMEOUT = (
    "TimeoutError: page.goto: Timeout 30000ms exceeded.\n"
    "Call log:\n  - navigating to \"https://example.com/\", waiting until \"load\""
)
# lines 31-34
NET_ERR = (
    "Error: page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://example.com/\n"
    "Call log:\n  - navigating to \"https://example.com/\", waiting until \"load\""
)
# lines 35-38
STRICT = (
    "Error: locator.click: Error: strict mode violation: locator('tbody > tr') "
    "resolved to 9 elements"
)
# lines 39-42
WRONG_KIND = (
    "Error: locator.fill: Error: Element is not an <input>, <textarea>, <select> "
    "or [contenteditable] and does not have a role allowing [aria-readonly]"
)
# lines 47-50
BAD_SELECTOR = (
    "Error: locator.fill: Unexpected token \"=\" while parsing css selector "
    "\"id=searchBox\". Did you mean to CSS.escape it?"
)
# lines 51-56
INTERCEPTED = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('[title=\"Filter\"]')\n"
    "  - locator resolved to <button title=\"Filter\">...</button>\n"
    "  - <div class=\"overlay\">...</div> intercepts pointer events"
)
# lines 57-64
DISABLED = (
    "TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
    "Call log:\n  - waiting for locator('id=react-select-3-option-1')\n"
    "  - locator resolved to <div aria-disabled=\"true\">...</div>\n"
    "  - attempting click action\n"
    "    - waiting for element to be visible, enabled and stable\n"
    "    - element is not enabled"
)
# lines 65-70
NOT_EDITABLE = (
    "TimeoutError: locator.fill: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('id=customer_cdr_from_date')\n"
    "  - locator resolved to <input readonly>...</input>\n"
    "  - element is not editable"
)
# line 75
ATTR_MISSING = "AttributeError: Attribute 'title' not found!"
# line 76
ASSERTION_EMPTY = "'' does not contain 'buy milk'"
# lines 77-80
ROBOT_DATA = (
    "ValueError: Argument 'dictionary' got value 'Option 2' that cannot be "
    "converted to Mapping: Invalid expression."
)
# lines 87-91
WAITFOR_HIDDEN = (
    "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
    "Call log:\n  - waiting for locator('h6') to be visible\n"
    "    4 × locator resolved to hidden <h6>…</h6>"
)
# line 106
ARG_COUNT_RF7 = "Keyword 'Browser.Keyboard Key' expected 2 arguments, got 3."
# lines 109-113
STILL_VISIBLE = (
    "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#spinner') to be hidden\n"
    "    3 × locator resolved to visible <div id=\"spinner\">…</div>"
)
# line 237
WRONG_KEYWORD_NAME = "No keyword with name 'Do Something' found"
# lines 307-313
OPTION_NOT_FOUND = (
    "TimeoutError: locator.selectOption: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#size')\n"
    "  - locator resolved to <select id=\"size\">…</select>\n"
    "  - did not find some options\n"
    "  - retrying select option action"
)

# --- messages, copied from tests/test_optimization/test_failure_analyzer.py ---

# line 393
ANIMATION_OR_DISABLED = "Element is not interactable at this point"
# line 396
ASSERTION_TRUTH_FAILED = "'False' should be true"

# --- A1 composite text, copied verbatim from the f-string in
# src/backend/crew_ai/optimization/failure_analyzer.py:656 ---
A1_COMPOSITE = "Query implies iteration ('verify all rows show status') but code is linear"


@pytest.mark.parametrize("message,expected_fragment,specific_type", [
    (PLACEHOLDER, "placeholder", "placeholder_never_resolved"),
    (GOTO_TIMEOUT, "load", "page_load_timeout"),
    (NEVER_RESOLVED, "never", "element_never_resolved"),
    (WRONG_KIND, "does not support", "wrong_element_kind"),
    (INTERCEPTED, "overlay", "click_intercepted"),
    (STRICT, "more than one", "multiple_elements_found"),
    (ASSERTION_TRUTH_FAILED, "true", "assertion_truth_failed"),
    (BAD_SELECTOR, "selector", "invalid_selector_syntax"),
    (ROBOT_DATA, "convert", "robot_data_error"),
    (NET_ERR, "network", "navigation_network_error"),
    (ATTR_MISSING, "attribute", "attribute_missing"),
    (ANIMATION_OR_DISABLED, "disabled", "animation_or_disabled"),
    (DISABLED, "disabled", "element_disabled"),
    (NOT_EDITABLE, "editable", "element_not_editable"),
    (ASSERTION_EMPTY, "empty", "assertion_empty_actual"),
    (WAITFOR_HIDDEN, "visible", "element_not_visible"),
    (STILL_VISIBLE, "disappear", "element_still_present"),
    (OPTION_NOT_FOUND, "option", "option_not_found"),
    (WRONG_KEYWORD_NAME, "keyword", "wrong_keyword_name"),
    (ARG_COUNT_RF7, "argument", "wrong_argument_count"),
])
def test_mapped_type_gets_a_matching_sentence(message, expected_fragment, specific_type):
    """Each test asserts the classifier actually reached the type it claims,
    so a sentence cannot pass while quietly attached to the wrong branch."""
    from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

    analysis = FailureClassifier().classify(message)
    assert analysis.specific_type == specific_type, (
        f"fixture routes to {analysis.specific_type!r}, test claims {specific_type!r}"
    )

    sentence = failure_sentence(message)

    assert sentence is not None
    assert expected_fragment in sentence.lower()


def test_unrecognised_message_returns_none():
    from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

    message = "something nobody has ever seen"
    assert FailureClassifier().classify(message).category == "unknown"

    assert failure_sentence(message) is None


def test_a1_composite_text_returns_none():
    """The A1 composite sentence's real text is gone by the time this runs
    (FailureClassifier.classify alone never produces category A1 — only
    CompositeFailureDetector.detect does) so there is nothing honest to say."""
    from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

    assert FailureClassifier().classify(A1_COMPOSITE).category == "unknown"

    assert failure_sentence(A1_COMPOSITE) is None


def test_none_input_returns_none():
    assert failure_sentence(None) is None


def test_empty_string_returns_none():
    assert failure_sentence("") is None
