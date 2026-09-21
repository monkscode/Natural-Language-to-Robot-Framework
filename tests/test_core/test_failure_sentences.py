"""Tests for the specific_type -> plain-English sentence map.

Every message here is copied from a message already committed in
tests/test_optimization/test_failure_analyzer.py or
tests/test_optimization/test_failure_analyzer_playwright.py (line noted per
constant below), so each is a real shape the classifier is already proven to
route to the specific_type the test claims — never invented, never a live
corpus row.

The first_failure_message tests read tests/fixtures/rf742_output_xml/: each
.xml there is what Robot Framework 7.4.2 really wrote for the .robot file of
the same name, produced in the runner image with
``robot --output <name>.xml --log NONE --report NONE <name>.robot``
(suite_teardown_failure_rebot.xml is ``rebot --output`` over
suite_teardown_failure.xml). No XML here is hand-written.

Referenced by: none (test module).
Depends on: src/backend/core/failure_sentences.py, pytest.
"""
from pathlib import Path
from xml.etree import ElementTree

import pytest

from src.backend.core.failure_sentences import (
    _strip_rf_header,
    failure_sentence,
    first_failure_message,
)

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
# lines 81-84 — the "used with invalid index" shape of robot_data_error
INVALID_INDEX = (
    "String '${selected_options}[0]' used with invalid index 'label'. To use "
    "'[label]' as a literal value, it needs to be escaped like '\\[label]'."
)
# the "evaluating expression" shape of robot_data_error — no committed test
# shape exists for it (Ruling R5 source (1) checked first, then (2)); copied
# from bench/private/e5b/e5b_replay_rows.json, host books.toscrape.com (a
# public demo book-catalogue site, on the R5 allow-list) — a book title, not
# a URL or host, so nothing needed replacing with example.com.
EVAL_EXPRESSION = (
    "Evaluating expression 'len(A Light in the Attic) == 20' failed: "
    "SyntaxError: invalid syntax. Perhaps you forgot a comma? (<string>, line 1)"
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
    (ROBOT_DATA, "arguments", "robot_data_error"),
    (INVALID_INDEX, "arguments", "robot_data_error"),
    (EVAL_EXPRESSION, "arguments", "robot_data_error"),
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


# --- first_failure_message: real RF 7.4.2 output.xml files -----------------

_RF_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rf742_output_xml"


def _xml(name: str) -> str:
    return str(_RF_FIXTURES / f"{name}.xml")


def _timeout(action: str, selector: str) -> str:
    """The Playwright locator-timeout text the fixtures' Fail calls produce."""
    return (
        f"TimeoutError: locator.{action}: Timeout 10000ms exceeded.\n"
        f"Call log:\n  - waiting for locator(\"{selector}\")"
    )


def test_a_single_failing_test_returns_its_full_multi_line_message():
    """All lines, not the first: the placeholder token lives in the call log."""
    assert first_failure_message(_xml("single_failure")) == _timeout("click", "#submit")


def test_the_first_failing_test_is_read_not_the_first_test():
    """pass_then_fail: test 1 PASSES, test 2 fails (#second), test 3 fails."""
    assert first_failure_message(_xml("pass_then_fail")) == _timeout("fill", "#second")


def test_a_caught_failure_before_the_real_one_is_skipped():
    """Ruling R2's exact case: Run Keyword And Ignore Error + Fail(#caught),
    then Fail(#real). RF writes the caught Fail as a FAIL keyword under a PASS
    parent, BEFORE the real one."""
    assert first_failure_message(_xml("caught_then_real")) == _timeout("fill", "#real")


@pytest.mark.parametrize("fixture,expected", [
    # Run Keyword And Return Status + Run Keyword And Expect Error, then a real Fail
    ("caught_by_status_and_expect_then_real", _timeout("fill", "#real2")),
    # a failing TRY branch under a PASS <try>, then a real Fail
    ("try_except_then_real", _timeout("fill", "#after-try")),
    # Wait Until Keyword Succeeds whose first attempt FAILED, then a real Fail
    ("retry_succeeds_then_real", _timeout("fill", "#real3")),
])
def test_every_catching_idiom_is_skipped(fixture, expected):
    assert first_failure_message(_xml(fixture)) == expected


def test_a_user_keyword_wrapper_yields_the_failure_message():
    assert first_failure_message(_xml("user_keyword_wrapper")) == _timeout("click", "#inner")


def test_a_test_setup_failure_is_stored_without_its_header():
    assert first_failure_message(_xml("test_setup_failure")) == _timeout("click", "#test-setup")


def test_a_test_teardown_failure_is_stored_without_its_header():
    assert first_failure_message(_xml("test_teardown_failure")) == _timeout(
        "click", "#test-teardown")


def test_several_failures_stores_the_first_one():
    assert first_failure_message(_xml("several_failures")) == _timeout("click", "#first")


def test_a_skipped_test_is_not_a_failure():
    assert first_failure_message(_xml("skip_then_fail")) == "Error: the real one"


def test_a_failure_outside_any_keyword_falls_back_to_the_test_status():
    """A VAR statement is not a <kw>: the test's own status text is the reason."""
    assert first_failure_message(_xml("var_failure")) == (
        "Setting variable '${x}' failed: Variable '${undefined_variable}' not found.")


def test_a_suite_setup_failure_is_stored_without_its_header():
    """The test holds no keywords at all; its status carries the header."""
    assert first_failure_message(_xml("suite_setup_failure")) == _timeout(
        "click", "#suite-setup")


def test_a_suite_setup_locator_timeout_does_not_classify_as_page_load_timeout():
    """Part A's defect (S5): with the header on, the Playwright prefix gate
    misses and a locator timeout reads as a page that never loaded."""
    message = first_failure_message(_xml("suite_setup_failure"))

    assert failure_sentence(message) == failure_sentence(NEVER_RESOLVED)
    assert failure_sentence(message) != failure_sentence(GOTO_TIMEOUT)


def test_a_parent_suite_teardown_header_is_stripped():
    """rebot writes this header onto the test; raw robot output never does."""
    assert first_failure_message(_xml("suite_teardown_failure_rebot")) == _timeout(
        "click", "#suite-teardown")


def test_raw_output_of_a_suite_teardown_failure_has_no_failing_test():
    """RF 7.4.2 leaves every test PASS in the raw output.xml when only the
    suite teardown fails (statistics still count it failed). Pinned so the
    gap stays visible: there is no failing test to read a reason from."""
    assert first_failure_message(_xml("suite_teardown_failure")) is None


@pytest.mark.parametrize("fixture,header", [
    ("test_setup_failure", "Setup failed:"),
    ("suite_setup_failure", "Parent suite setup failed:"),
    ("test_teardown_failure", "Teardown failed:"),
    ("suite_teardown_failure_rebot", "Parent suite teardown failed:"),
    ("several_failures", "Several failures occurred:"),
])
def test_each_rf_header_is_real_and_is_stripped(fixture, header):
    """Every header is read off a real failing test's status, not typed in."""
    root = ElementTree.parse(_xml(fixture)).getroot()
    test = next(t for t in root.iter("test") if t.find("status").get("status") == "FAIL")
    status_text = test.find("status").text

    assert status_text.startswith(header + "\n")
    assert _strip_rf_header(status_text) == status_text[len(header) + 1:].strip()


def test_only_one_leading_header_line_is_stripped():
    assert _strip_rf_header("Setup failed:\nTeardown failed:\nboom") == "Teardown failed:\nboom"


def test_a_header_that_is_not_the_first_line_is_kept():
    text = "boom\n\nAlso teardown failed:\nbang"
    assert _strip_rf_header(text) == text


def test_the_full_message_is_returned_uncapped():
    """Capping and redaction are the caller's job; this returns the whole text."""
    message = first_failure_message(_xml("long_failure_with_token"))

    assert message.startswith(
        "Error: page.goto: net::ERR_ABORTED at https://example.com/app?token=SECRETVALUE123\n")
    assert len(message) > 3000


def test_none_path_returns_none():
    assert first_failure_message(None) is None


def test_a_missing_file_returns_none(tmp_path):
    assert first_failure_message(str(tmp_path / "output.xml")) is None


def test_an_unparseable_file_returns_none_not_a_parser_error(tmp_path):
    broken = tmp_path / "output.xml"
    broken.write_text("<robot><suite><test><status status='FAIL'>", encoding="utf-8")

    assert first_failure_message(str(broken)) is None


def test_a_file_with_no_test_returns_none(tmp_path):
    empty = tmp_path / "output.xml"
    empty.write_text("<robot><suite/></robot>", encoding="utf-8")

    assert first_failure_message(str(empty)) is None
