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

first_failure_message reads the text those sentences classify out of a run's
output.xml. It is Part B's own reader, deliberately NOT OutputXmlParser: that
parser is the learning path's input, and it reads the first <test> only,
stores a caught failure as the error, and returns parser error text as a
message — none of which may reach a stored reason.

Referenced by: services/workflow_service.py (_failure_reason),
api/tests_endpoints.py (test_detail, Task 7).
Depends on: crew_ai/optimization/failure_analyzer.py (FailureClassifier),
xml.etree.ElementTree.
"""
import logging
from xml.etree import ElementTree

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


def failure_category(error_message: str | None) -> str | None:
    """The classifier's category code (e.g. "C1"), verbatim -- "unknown"
    included. This is the same vocabulary execution_records.failure_category
    already stores (feedback_loop.py:1006-1008), so a Tests-page result and a
    learning row use one code for the same failure.

    None for None, "", or whitespace-only text: there is nothing to
    classify, and "unknown" would misrepresent an absent message as one the
    classifier actually looked at (Ruling R4, Task 7).
    """
    if not error_message or not error_message.strip():
        return None
    return _classifier.classify(error_message).category


# The header line Robot Framework 7.4.2 puts above a test's failure text when
# the failure did not come from its own body. Each spelling was read off a real
# run (tests/fixtures/rf742_output_xml/). With the header on, a plain locator
# timeout misses the classifier's Playwright prefix gate and reads as
# page_load_timeout — Part A's defect (task-7-stress-review.md S5).
_RF_HEADERS = frozenset({
    "Setup failed:",
    "Parent suite setup failed:",
    "Teardown failed:",
    "Parent suite teardown failed:",
    "Several failures occurred:",
})


def _strip_rf_header(text: str) -> str:
    """Drop ONE leading RF header line; any other text comes back stripped."""
    text = text.strip()
    first_line, _, rest = text.partition("\n")
    if first_line.strip() in _RF_HEADERS:
        return rest.strip()
    return text


# RF appends a failed test teardown's text to the test's own status text.
# Spelling read off a real run (var_then_teardown_failure.xml).
_TEARDOWN_TAIL = "\n\nAlso teardown failed:"


def _status(element: ElementTree.Element) -> str | None:
    status = element.find("status")
    return status.get("status") if status is not None else None


def _status_text(element: ElementTree.Element) -> str:
    status = element.find("status")
    return (status.text or "").strip() if status is not None else ""


def _own_fail_message(element: ElementTree.Element) -> str:
    """The element's own <msg level="FAIL"> text, or "". The msg is the full
    text: RF cuts the middle out of a long <status> text, never out of it."""
    fail_msg = element.find("msg[@level='FAIL']")
    if fail_msg is not None and fail_msg.text and fail_msg.text.strip():
        return fail_msg.text.strip()
    return ""


def _is_caught_try_branch(branch: ElementTree.Element, parent: ElementTree.Element) -> bool:
    """A failed TRY branch is CAUGHT when an EXCEPT beside it ran (Ruling R2a).

    When that handler — or a FINALLY — fails too, RF marks the <try> AND the
    TRY branch FAIL, so the branch's own status cannot tell. An EXCEPT that did
    not match is NOT RUN; one that ran is PASS or FAIL.
    """
    if branch.tag != "branch" or branch.get("type") != "TRY":
        return False
    return any(sibling.get("type") == "EXCEPT" and _status(sibling) != "NOT RUN"
               for sibling in parent.findall("branch"))


def _first_uncaught_failure(parent: ElementTree.Element) -> ElementTree.Element | None:
    """The first child of `parent`, in document order, that FAILED uncaught.

    Only FAIL children count, so anything under a PASS element — Run Keyword
    And Ignore Error / Return Status / Expect Error, a Wait Until Keyword
    Succeeds that got there in the end, a TRY an EXCEPT handled — is never
    reached (Ruling R2), and a caught TRY branch is skipped (Ruling R2a).
    """
    for child in parent:
        if _status(child) == "FAIL" and not _is_caught_try_branch(child, parent):
            return child
    return None


def _failure_text(element: ElementTree.Element) -> str:
    """The full failure text a failed element stands for, or "".

    Its own <msg level="FAIL">; else, for an element without one (a user
    keyword, Run Keyword, IF, FOR, a TRY branch), the same for its first
    uncaught failed child, all the way down — never a later sibling (Ruling
    R15). Library wrappers that write their own FAIL msg (Wait Until Keyword
    Succeeds after its last retry, a Run Keyword And Expect Error mismatch)
    keep it. A <kw> whose chain holds no msg falls back to its own <status>
    text (Ruling R16); any other element to "" — the caller then reads the
    test's status. Iterative: deep nesting cannot hit the recursion limit.
    """
    current: ElementTree.Element | None = element
    while current is not None:
        message = _own_fail_message(current)
        if message:
            return message
        current = _first_uncaught_failure(current)
    return _status_text(element) if element.tag == "kw" else ""


def _test_status_reason(test: ElementTree.Element) -> str:
    """The test's own status text: RF's header line stripped, and cut before
    the "Also teardown failed:" tail — a teardown that failed LATER is not the
    reason (Ruling R15)."""
    text = _strip_rf_header(_status_text(test))
    return text.partition(_TEARDOWN_TAIL)[0].strip()


def _suite_teardowns(suite: ElementTree.Element):
    """Every suite-level <kw type="TEARDOWN">, in document order (a child
    suite's teardown is written before its parent's)."""
    for child in suite:
        if child.tag == "suite":
            yield from _suite_teardowns(child)
        elif child.tag == "kw" and child.get("type") == "TEARDOWN":
            yield child


def first_failure_message(output_xml_path: str | None) -> str | None:
    """Why the run failed, read from output.xml, or None.

    The FIRST FAILING <test>, not the first <test>: multi-test files exist.
    In it, the first uncaught failed element in document order IS the
    failure; its full text comes from _failure_text (every line — the
    placeholder token lives in the call log, never on line 1). When that
    yields nothing — or the test holds no failed element, as under a failed
    suite setup — the test's own status text, header-stripped and cut before
    the teardown tail.

    When NO test failed but the run did: RF writes a suite-TEARDOWN failure
    onto no test in the raw file (every test stays PASS), so the first failed
    suite-level teardown keyword's text is the reason (Ruling R14).

    None when there is no path, the file cannot be read or parsed, or nothing
    failed. Never a parser error string: that would be stored as the reason.
    """
    if not output_xml_path:
        return None
    try:
        root = ElementTree.parse(output_xml_path).getroot()
    except (OSError, ElementTree.ParseError) as e:
        logger.warning("Could not read output.xml for a failure reason: %s", e)
        return None
    failed_test = next((t for t in root.iter("test") if _status(t) == "FAIL"), None)
    if failed_test is not None:
        first = _first_uncaught_failure(failed_test)
        message = _failure_text(first) if first is not None else ""
        return message or _test_status_reason(failed_test) or None
    for suite in root.findall("suite"):
        for teardown in _suite_teardowns(suite):
            if _status(teardown) == "FAIL":
                return _failure_text(teardown) or None
    return None
