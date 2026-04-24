"""Tests for src.backend.crew_ai.robot_code_normalizer.

Covers the false-positive regressions the allowlist was designed to fix, plus
the legitimate rewrite cases it must keep working.
"""

import pytest

from src.backend.crew_ai.robot_code_normalizer import normalize_robot_code


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
