"""Tests for src.backend.crew_ai.robot_code_normalizer.

Covers the false-positive regressions the allowlist was designed to fix, plus
the legitimate rewrite cases it must keep working.
"""

import pytest

from src.backend.crew_ai.robot_code_normalizer import (
    ensure_browser_timeout,
    normalize_robot_code,
    strip_redundant_css_prefix,
)


@pytest.mark.parametrize(
    "label, source, expected",
    [
        # --- False positives that the old global-rewrite version corrupted ---
        (
            "Fill Text: #release is the typed text, not a locator",
            "    Fill Text    ${message_locator}    #release",
            "    Fill Text    ${message_locator}    #release",
        ),
        (
            "[Tags] takes literal tag values",
            "    [Tags]    #smoke",
            "    [Tags]    #smoke",
        ),
        (
            "Should Be Equal: .NET is an assertion value",
            "    Should Be Equal    ${value}    .NET",
            "    Should Be Equal    ${value}    .NET",
        ),
        (
            "Log with a hashtag literal — unknown-to-allowlist keyword stays untouched",
            "    Log    #hashtag",
            "    Log    #hashtag",
        ),
        (
            "Custom user-defined keyword — conservative skip",
            "    My Custom Keyword    #foo",
            "    My Custom Keyword    #foo",
        ),
        # --- Legitimate rewrites (Browser Library) ---
        (
            "Click with bare id",
            "    Click    #searchBox",
            "    Click    css=#searchBox",
        ),
        (
            "Click with bare class",
            "    Click    .btn-primary",
            "    Click    css=.btn-primary",
        ),
        (
            "Fill Text rewrites position 0 only",
            "    Fill Text    #email    user@example.com",
            "    Fill Text    css=#email    user@example.com",
        ),
        (
            "Drag And Drop rewrites both locator args",
            "    Drag And Drop    #src    #dst",
            "    Drag And Drop    css=#src    css=#dst",
        ),
        (
            "Assignment prefix + known keyword",
            "    ${x}=    Click    #foo",
            "    ${x}=    Click    css=#foo",
        ),
        # --- Legitimate rewrites (SeleniumLibrary) ---
        (
            "Click Element rewrites bare id",
            "    Click Element    #submit",
            "    Click Element    css=#submit",
        ),
        (
            "Input Text locator + literal text",
            "    Input Text    #q    search term",
            "    Input Text    css=#q    search term",
        ),
        (
            "Element Should Be Visible with bare class",
            "    Element Should Be Visible    .result-row",
            "    Element Should Be Visible    css=.result-row",
        ),
        # --- Variable declarations ---
        (
            "*** Variables *** bare id value",
            "${SEARCH}    #searchBox",
            "${SEARCH}    css=#searchBox",
        ),
        (
            "*** Variables *** bare class value",
            "${BTN}    .btn-submit",
            "${BTN}    css=.btn-submit",
        ),
        (
            "${x}= Set Variable stores a bare locator value",
            "    ${x}=    Set Variable    #foo",
            "    ${x}=    Set Variable    css=#foo",
        ),
        # --- Canonicalization ---
        (
            "Case-insensitive keyword lookup",
            "    click    #foo",
            "    click    css=#foo",
        ),
        (
            "Underscore-separated keyword name",
            "    fill_text    #x    y",
            "    fill_text    css=#x    y",
        ),
        # --- No-op edges ---
        (
            "Already-prefixed locator — no double-prefix",
            "    Click    css=#searchBox",
            "    Click    css=#searchBox",
        ),
        (
            "XPath locator — untouched",
            "    Click    //button[@id='x']",
            "    Click    //button[@id='x']",
        ),
        (
            "Full-line comment preserved verbatim",
            "# This is a comment about #searchBox",
            "# This is a comment about #searchBox",
        ),
        (
            "Line continuation marker preserved",
            "    ...    #continuation-text",
            "    ...    #continuation-text",
        ),
        (
            "Section header untouched",
            "*** Test Cases ***",
            "*** Test Cases ***",
        ),
        (
            "Empty string",
            "",
            "",
        ),
        (
            "Tab-separated cells",
            "\tClick\t#foo",
            "\tClick\tcss=#foo",
        ),
        # --- Multi-line integrity ---
        (
            "Multi-line input preserves non-locator lines",
            "*** Test Cases ***\nMy Test\n    Click    #x\n    Log    done",
            "*** Test Cases ***\nMy Test\n    Click    css=#x\n    Log    done",
        ),
    ],
)
def test_normalize_robot_code(label, source, expected):
    assert normalize_robot_code(source) == expected, label


def test_fast_path_returns_input_when_no_selector_chars():
    """Strings without `#` or `.` must short-circuit and return the same object."""
    src = "    Click    ${BTN}\n    Log    hello"
    assert normalize_robot_code(src) is src


def test_none_and_empty():
    assert normalize_robot_code("") == ""
    assert normalize_robot_code(None) is None


# ---------------------------------------------------------------------------
# ensure_browser_timeout — the Browser Library import defaults to 10s, which is
# the single largest failure cause in the bench corpus (33 nav timeouts, every
# one exactly 10000ms). Verified against the runner image's libdoc
# (Browser 19.14.2): the init signature starts `*_`, so `timeout` is
# keyword-only, and `New Page` has no timeout parameter — only the global reaches it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, source, expected",
    [
        (
            "Bare import gains the timeout",
            "*** Settings ***\nLibrary    Browser\n",
            "*** Settings ***\nLibrary    Browser    timeout=30s\n",
        ),
        (
            "Import that already declares a timeout is left alone",
            "*** Settings ***\nLibrary    Browser    timeout=10s\n",
            "*** Settings ***\nLibrary    Browser    timeout=10s\n",
        ),
        (
            "Import carrying other arguments is left alone (conservative)",
            "*** Settings ***\nLibrary    Browser    strict=False\n",
            "*** Settings ***\nLibrary    Browser    strict=False\n",
        ),
        (
            "Aliased import is left alone",
            "*** Settings ***\nLibrary    Browser    AS    B\n",
            "*** Settings ***\nLibrary    Browser    AS    B\n",
        ),
        (
            "SeleniumLibrary import untouched",
            "*** Settings ***\nLibrary    SeleniumLibrary\n",
            "*** Settings ***\nLibrary    SeleniumLibrary\n",
        ),
        (
            "Submodule import is a different library — untouched",
            "*** Settings ***\nLibrary    Browser.Playwright\n",
            "*** Settings ***\nLibrary    Browser.Playwright\n",
        ),
        (
            "No Settings block at all — nothing to inject into",
            "*** Test Cases ***\nMy Test\n    Log    hi",
            "*** Test Cases ***\nMy Test\n    Log    hi",
        ),
        (
            "CRLF line endings survive intact",
            "*** Settings ***\r\nLibrary    Browser\r\nLibrary    Collections\r\n",
            "*** Settings ***\r\nLibrary    Browser    timeout=30s\r\nLibrary    Collections\r\n",
        ),
        (
            "Tab-separated import",
            "*** Settings ***\nLibrary\tBrowser\n",
            "*** Settings ***\nLibrary\tBrowser    timeout=30s\n",
        ),
        # Robot Framework's real grammar, measured in the runner image rather than
        # assumed: the SETTING name is case-insensitive (`library    Browser` imports
        # fine), the LIBRARY name is not (`Library    browser` dies with
        # ModuleNotFoundError: No module named 'browser'), and a cell separator is
        # 2+ spaces or a tab (`Library Browser` is read as a setting literally named
        # "Library Browser" and errors).
        (
            "Lowercase setting name is valid Robot Framework",
            "*** Settings ***\nlibrary    Browser\n",
            "*** Settings ***\nlibrary    Browser    timeout=30s\n",
        ),
        (
            "Uppercase setting name is valid Robot Framework",
            "*** Settings ***\nLIBRARY    Browser\n",
            "*** Settings ***\nLIBRARY    Browser    timeout=30s\n",
        ),
        (
            "Lowercase library name is a DIFFERENT (broken) import — untouched",
            "*** Settings ***\nLibrary    browser\n",
            "*** Settings ***\nLibrary    browser\n",
        ),
        (
            "Single-space separator is not a library import at all — untouched",
            "*** Settings ***\nLibrary Browser\n",
            "*** Settings ***\nLibrary Browser\n",
        ),
        (
            "Trailing whitespace on the import line",
            "*** Settings ***\nLibrary    Browser   \n",
            "*** Settings ***\nLibrary    Browser    timeout=30s\n",
        ),
        (
            "Empty string",
            "",
            "",
        ),
    ],
)
def test_ensure_browser_timeout(label, source, expected):
    assert ensure_browser_timeout(source) == expected, label


def test_ensure_browser_timeout_is_idempotent():
    """The dryrun repair path re-runs the whole pipeline, so a second pass must
    not stack a second `timeout=` onto an import the first pass already fixed."""
    once = ensure_browser_timeout("*** Settings ***\nLibrary    Browser\n")
    assert ensure_browser_timeout(once) == once
    assert once.count("timeout=") == 1


def test_ensure_browser_timeout_none():
    assert ensure_browser_timeout(None) is None


def test_ensure_browser_timeout_yields_to_a_continuation_argument():
    """`Library    Browser` followed by `...    timeout=5s` is one import with the
    timeout on a continuation line. Measured in the runner image: Robot Framework
    accepts the resulting duplicate and the LATER value wins (Set Browser Timeout
    reported `5 seconds`), so a pre-existing intent is never overridden."""
    src = "*** Settings ***\nLibrary    Browser\n...    timeout=5s\n"
    assert ensure_browser_timeout(src) == (
        "*** Settings ***\nLibrary    Browser    timeout=30s\n...    timeout=5s\n"
    )


# ---------------------------------------------------------------------------
# strip_redundant_css_prefix — the assembler prompt says "always prefix CSS
# selectors with css=", and the model occasionally applies that to a locator
# that already carries a strategy prefix, emitting `css=id=searchBox`. That is
# never valid CSS, so Playwright rejects it at runtime:
#   locator.fill: Unexpected token "=" while parsing css selector "id=searchBox"
# Measured on the 2026-07-29 bench: all 30/30 runs are handed at least one
# non-css strategy-prefixed locator (id= x59, xpath= x22, text= x4), and 1 of
# 30 was mangled this way (q06 rep2). The dryrun gate cannot catch it —
# `robot --dryrun` validates keyword names and arity without resolving
# selectors, so the broken locator passes the gate and fails only at runtime.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, source, expected",
    [
        (
            "The q06 rep2 failure verbatim: css= stacked on an id= locator",
            "${search_box_locator}    css=id=searchBox",
            "${search_box_locator}    id=searchBox",
        ),
        (
            "css= stacked on xpath=",
            "    Click    css=xpath=//button[@type='submit']",
            "    Click    xpath=//button[@type='submit']",
        ),
        (
            "css= stacked on text=",
            "    Click    css=text=Login",
            "    Click    text=Login",
        ),
        (
            "css= stacked on role=",
            "    Click    css=role=button",
            "    Click    role=button",
        ),
        (
            "css= stacked on data-testid=",
            "    Click    css=data-testid=submit",
            "    Click    data-testid=submit",
        ),
        (
            "css= stacked on itself",
            "    Click    css=css=#submit",
            "    Click    css=#submit",
        ),
        # --- Must NOT be rewritten ---
        (
            "A correct css= locator is left alone",
            "    Fill Text    css=#searchBox    Cierra",
            "    Fill Text    css=#searchBox    Cierra",
        ),
        (
            "A bare strategy locator is already correct",
            "${search_box_locator}    id=searchBox",
            "${search_box_locator}    id=searchBox",
        ),
        (
            "CSS attribute selector containing = is valid CSS",
            "    Click    css=[data-x=y]",
            "    Click    css=[data-x=y]",
        ),
        (
            "CSS attribute selector on a tag is valid CSS",
            "    Click    css=input[id=foo]",
            "    Click    css=input[id=foo]",
        ),
        (
            "A strategy-like word that is not a strategy is left alone",
            "    Click    css=idx=5",
            "    Click    css=idx=5",
        ),
        (
            "Code with no css= at all takes the fast path",
            "*** Settings ***\nLibrary    Browser\n",
            "*** Settings ***\nLibrary    Browser\n",
        ),
        (
            "Empty string",
            "",
            "",
        ),
    ],
)
def test_strip_redundant_css_prefix(label, source, expected):
    assert strip_redundant_css_prefix(source) == expected, label


def test_strip_redundant_css_prefix_is_idempotent():
    """The dryrun repair path re-runs the whole pipeline, so a second pass over
    already-stripped code must be a no-op."""
    once = strip_redundant_css_prefix("    Click    css=id=searchBox")
    assert strip_redundant_css_prefix(once) == once == "    Click    id=searchBox"


def test_strip_redundant_css_prefix_none():
    assert strip_redundant_css_prefix(None) is None


def test_strip_redundant_css_prefix_handles_the_whole_failing_suite():
    """End-to-end shape of the q06 rep2 file: only the mangled cell changes."""
    src = (
        "*** Variables ***\n"
        "${search_box_locator}    css=id=searchBox\n"
        "${table_rows_locator}    css=tbody tr\n"
        "*** Test Cases ***\n"
        "Verify Web Tables Filtering\n"
        "    Fill Text    ${search_box_locator}    Cierra\n"
    )
    assert strip_redundant_css_prefix(src) == (
        "*** Variables ***\n"
        "${search_box_locator}    id=searchBox\n"
        "${table_rows_locator}    css=tbody tr\n"
        "*** Test Cases ***\n"
        "Verify Web Tables Filtering\n"
        "    Fill Text    ${search_box_locator}    Cierra\n"
    )
