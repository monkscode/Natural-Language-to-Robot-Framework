"""Tests for the two read rewrites in src.backend.crew_ai.robot_code_normalizer:
rewrite_select_text_reads (q08) and rewrite_typed_value_reads (N1), plus the
shared whole-file guard they reuse from the visibility rewrite (PR #113).

Every positive case is written in the assembler's REAL file shape — Settings +
Variables + Test Cases, as in bench run 0ee28976-9924-4385-90ae-6a7259235cb2 —
never only a minimal hand-made file: PR #111 shipped a guard that fired on every
Variables section and a minimal-file suite stayed green (memory
project_get_element_states_no_wait). Each "left alone" case that could pass
vacuously has a control that proves the same file WITHOUT the blocking detail
is rewritten.
"""

import pytest

from src.backend.crew_ai.robot_code_normalizer import (
    _scan_file_guard,
)

# ---------------------------------------------------------------------------
# The assembler's file shape
# ---------------------------------------------------------------------------

_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "Library    Collections\n"
    "\n"
    "*** Variables ***\n"
    "${browser}    chromium\n"
    "${headless}    True\n"
    "${url}    https://the-internet.herokuapp.com/dropdown\n"
    "${dropdown_locator}    id=dropdown\n"
    "${get_text_locator}    css=#dropdown\n"
    "\n"
    "*** Test Cases ***\n"
    "Generated Test\n"
    "    [Documentation]    Auto-generated test case\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    ${url}\n"
)


def _suite(*body: str, tail: str = "") -> str:
    """A generated q08-style file: the real header, then `body` lines, then Close Browser."""
    return _HEAD + "".join(f"    {line}\n" for line in body) + "    Close Browser" + tail


_SELECT = "Select Options By    ${dropdown_locator}    label    Option 2"
_READ = "${selected_option}=    Get Text    ${get_text_locator}"
_READ_NEW = "${selected_option}=    Get Selected Options    ${get_text_locator}    label"
_CHECK = "Should Contain    ${selected_option}    Option 2"
_OTHER_TEST_WITH_TRY = (
    "\n\nOther Test\n    TRY\n        Click    id=x\n    EXCEPT\n        Log    no\n    END\n"
)


# ---------------------------------------------------------------------------
# The shared whole-file guard (factored out of rewrite_visibility_checks_to_wait)
# ---------------------------------------------------------------------------

class TestScanFileGuard:
    def test_reports_which_of_the_asked_keywords_the_file_defines(self):
        code = _suite(_SELECT, _READ, _CHECK,
                      tail="\n\n*** Keywords ***\nGet Text\n    [Arguments]    ${l}\n    Log    own\n")
        guard = _scan_file_guard(code, ("gettext", "getselectedoptions"))
        assert guard.own_keywords == frozenset({"gettext"})
        assert guard.catches_errors is False
        assert guard.unsafe_section_state is False

    def test_a_variables_section_defines_no_keyword(self):
        # The #111 regression: `${name}    value` is not a keyword definition.
        guard = _scan_file_guard(_suite(_SELECT, _READ, _CHECK),
                                 ("gettext", "getselectedoptions", "selectoptionsby"))
        assert guard.own_keywords == frozenset()

    def test_try_means_the_file_catches_errors(self):
        code = _suite("TRY", "    Click    id=x", "EXCEPT", "    Log    no", "END")
        assert _scan_file_guard(code, ("gettext",)).catches_errors is True

    def test_a_rare_linebreak_makes_section_state_unsafe(self):
        code = _suite(_SELECT, "Log    a\x1cb")
        assert _scan_file_guard(code, ("gettext",)).unsafe_section_state is True
