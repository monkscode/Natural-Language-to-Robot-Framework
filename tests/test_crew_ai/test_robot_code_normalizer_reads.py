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

import logging

import pytest

from src.backend.crew_ai.robot_code_normalizer import (
    _read_variable,
    _scan_file_guard,
    rewrite_select_text_reads,
    rewrite_typed_value_reads,
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
# A step allowed after the read (only the check, a Log of the bare value or a close may follow it): the controls'
# neutral replacement for a later use of the value.
_ALLOWED = "Log    ${selected_option}"
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


# ---------------------------------------------------------------------------
# rewrite_select_text_reads — rewritten
# ---------------------------------------------------------------------------

class TestSelectReadRewritten:
    def test_the_corpus_shape(self):
        """Bench run 0ee28976-9924-4385-90ae-6a7259235cb2, byte for byte."""
        source = _suite(_SELECT, _READ, _CHECK)
        assert rewrite_select_text_reads(source) == _suite(_SELECT, _READ_NEW, _CHECK)

    @pytest.mark.parametrize("attribute, value", [("label", "Option 2"), ("value", "2"), ("text", "Option 2"),
                                                  ("LABEL", "Option 2")])
    def test_the_select_attribute_is_carried_over(self, attribute, value):
        select = f"Select Options By    ${{dropdown_locator}}    {attribute}    {value}"
        check = f"Should Contain    ${{selected_option}}    {value}"
        expected = f"${{selected_option}}=    Get Selected Options    ${{get_text_locator}}    {attribute}"
        assert rewrite_select_text_reads(_suite(select, _READ, check)) == _suite(select, expected, check)

    def test_a_multi_select_checks_membership_of_each_value(self):
        select = "Select Options By    id=multi    value    a    c"
        read = "${chosen}=    Get Text    css=#multi"
        checks = ("Should Contain    ${chosen}    a", "Should Contain    ${chosen}    c")
        expected = "${chosen}=    Get Selected Options    css=#multi    value"
        assert rewrite_select_text_reads(_suite(select, read, *checks)) == _suite(select, expected, *checks)

    @pytest.mark.parametrize("select_locator, read_locator", [
        ("id=dropdown", "css=#dropdown"),
        ("css=#dropdown", "id=dropdown"),
        ("id=dropdown", "id=dropdown"),
        ("${dropdown_locator}", "css=#dropdown"),
        ("xpath=//select[@id='dropdown']", "xpath=//select[@id='dropdown']"),
    ])
    def test_the_same_element_in_every_spelling(self, select_locator, read_locator):
        select = f"Select Options By    {select_locator}    label    Option 2"
        read = f"${{selected_option}}=    Get Text    {read_locator}"
        expected = f"${{selected_option}}=    Get Selected Options    {read_locator}    label"
        assert rewrite_select_text_reads(_suite(select, read, _CHECK)) == _suite(select, expected, _CHECK)

    def test_a_variable_assigned_earlier_in_the_test_resolves(self):
        body = ("${sel}=    Set Variable    id=dropdown", "Select Options By    ${sel}    label    Option 2",
                _READ, _CHECK)
        assert rewrite_select_text_reads(_suite(*body)) == _suite(*body[:2], _READ_NEW, _CHECK)

    def test_variable_names_compare_as_robot_compares_them(self):
        check = "Should Contain    ${Selected Option}    Option 2"
        assert rewrite_select_text_reads(_suite(_SELECT, _READ, check)) == _suite(_SELECT, _READ_NEW, check)

    def test_a_variables_entry_written_with_an_equals_sign_resolves(self):
        source = _suite(_SELECT, _READ, _CHECK).replace("${dropdown_locator}    id=", "${dropdown_locator}=    id=")
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW)

    def test_the_browser_prefix_is_kept(self):
        read = "${selected_option}=    Browser.Get Text    ${get_text_locator}"
        expected = "${selected_option}=    Browser.Get Selected Options    ${get_text_locator}    label"
        assert rewrite_select_text_reads(_suite(_SELECT, read, _CHECK)) == _suite(_SELECT, expected, _CHECK)

    def test_builtin_prefixed_uses_and_a_log_are_allowed(self):
        body = (_SELECT, _READ, "Log    Selected: ${selected_option}",
                "BuiltIn.Should Contain    ${selected_option}    Option 2")
        assert rewrite_select_text_reads(_suite(*body)) == _suite(_SELECT, _READ_NEW, *body[2:])

    @pytest.mark.parametrize("operator", ["contains", "*=", "Contains"])
    def test_the_inline_assertion_form(self, operator):
        read = f"Get Text    ${{get_text_locator}}    {operator}    Option 2"
        expected = f"Get Selected Options    ${{get_text_locator}}    label    {operator}    Option 2"
        assert rewrite_select_text_reads(_suite(_SELECT, read)) == _suite(_SELECT, expected)

    def test_a_second_read_leaves_the_first_read_alone(self):
        # Only the check, a Log of the value or a close may follow a rewritten read: a second read is none of them.
        body = (_SELECT, _READ, _CHECK, "${again}=    Get Text    id=dropdown", "Should Contain    ${again}    Option 2")
        expected = (_SELECT, _READ, _CHECK, "${again}=    Get Selected Options    id=dropdown    label",
                    "Should Contain    ${again}    Option 2")
        assert rewrite_select_text_reads(_suite(*body)) == _suite(*expected)

    def test_a_reused_variable_name_for_the_second_read(self):
        body = (_SELECT, _READ, _CHECK, _READ, _CHECK)
        assert rewrite_select_text_reads(_suite(*body)) == _suite(_SELECT, _READ, _CHECK, _READ_NEW, _CHECK)

    def test_two_selects_in_one_test_the_last_read_keeps_its_own_attribute(self):
        body = ("Select Options By    id=size    value    m", "Select Options By    id=colour    label    Red",
                "${s}=    Get Text    css=#size", "Should Contain    ${s}    m",
                "${c}=    Get Text    css=#colour", "Should Contain    ${c}    Red")
        expected = (*body[:4], "${c}=    Get Selected Options    css=#colour    label", body[5])
        assert rewrite_select_text_reads(_suite(*body)) == _suite(*expected)

    @pytest.mark.parametrize("label, body", [
        ("the check, then Close Browser", (_SELECT, _READ, _CHECK)),
        ("the check, a Log of the value, then Close Browser", (_SELECT, _READ, _CHECK, _ALLOWED)),
        ("comment and blank lines between", (_SELECT, _READ, "# verify the selection", "", _CHECK, "# done")),
        ("Close Page and Close Context with literal arguments, prefixed or not", (
            _SELECT, _READ, _CHECK, "Close Page    CURRENT", "Browser.Close Context    ALL", "Close Browser    ALL")),
    ])
    def test_only_allowed_steps_after_the_read(self, label, body):
        source = _suite(*body)
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW), label

    def test_the_latest_select_on_the_element_decides(self):
        body = ("Select Options By    id=dropdown    label    Option 1", _SELECT, _READ, _CHECK)
        assert rewrite_select_text_reads(_suite(*body)) == _suite(body[0], _SELECT, _READ_NEW, _CHECK)

    def test_a_page_change_between_select_and_read_is_still_rewritten(self):
        # Review Focus 2: the check now reads the real selection; a page that dropped
        # the choice fails, which is the honest verdict for "verify Option 2 is selected".
        body = (_SELECT, "Click    id=submit", "Reload", _READ, _CHECK)
        assert rewrite_select_text_reads(_suite(*body)) == _suite(*body[:3], _READ_NEW, _CHECK)

    def test_a_trailing_comment_stays_after_the_new_cell(self):
        read = "${selected_option}=    Get Text    ${get_text_locator}    # read it back"
        expected = "${selected_option}=    Get Selected Options    ${get_text_locator}    label    # read it back"
        assert rewrite_select_text_reads(_suite(_SELECT, read, _CHECK)) == _suite(_SELECT, expected, _CHECK)

    def test_crlf_line_endings_survive(self):
        source = _suite(_SELECT, _READ, _CHECK).replace("\n", "\r\n")
        assert rewrite_select_text_reads(source) == _suite(_SELECT, _READ_NEW, _CHECK).replace("\n", "\r\n")

    def test_tab_separated_cells_keep_their_separator(self):
        read = "${selected_option}=\tGet Text\t${get_text_locator}"
        expected = "${selected_option}=\tGet Selected Options\t${get_text_locator}\tlabel"
        assert rewrite_select_text_reads(_suite(_SELECT, read, _CHECK)) == _suite(_SELECT, expected, _CHECK)

    def test_a_test_named_like_the_keyword_does_not_block(self):
        # #113's rule: a test name is not a keyword definition.
        source = _suite(_SELECT, _READ, _CHECK, tail="\n\nGet Text\n    Log    a test, not a keyword\n")
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW)

    def test_an_unrelated_own_keyword_does_not_block(self):
        source = _suite(_SELECT, _READ, _CHECK, tail="\n\n*** Keywords ***\nMy Helper\n    Log    own\n")
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW)


# ---------------------------------------------------------------------------
# rewrite_select_text_reads — left alone
# ---------------------------------------------------------------------------

# Where a scoped setter or `VAR` re-points the read's locator variable out of a test-by-test reading's sight (each
# position re-points it in Robot 7.4.2 — planning evidence): (label, the file, the setter text). The control is the
# same file with the setter text replaced by a neutral `Log    x`, and IS rewritten.
_SET_TEST = "Set Test Variable    ${get_text_locator}    id=other"
_SET_SUITE = "Set Suite Variable    ${get_text_locator}    id=other"
_VAR_SUITE = "VAR    ${get_text_locator}    id=other    scope=SUITE"
_SQUASHED = "SetTestVariable    ${get_text_locator}    id=other"
_SETTER_ANYWHERE = [
    ("a wrapper argument", _suite(_SELECT, "Run Keyword If    True    " + _SET_TEST, _READ, _CHECK), _SET_TEST),
    ("the file's own keyword", _suite(_SELECT, "Pick", _READ, _CHECK,
                                      tail="\n\n*** Keywords ***\nPick\n    " + _SET_TEST + "\n"), _SET_TEST),
    ("an earlier test's Set Suite Variable", _suite(_SELECT, _READ, _CHECK).replace(
        "*** Test Cases ***\n", "*** Test Cases ***\nPoint Elsewhere\n    " + _SET_SUITE + "\n\n"), _SET_SUITE),
    ("an earlier test's VAR with scope=SUITE", _suite(_SELECT, _READ, _CHECK).replace(
        "*** Test Cases ***\n", "*** Test Cases ***\nPoint Elsewhere\n    " + _VAR_SUITE + "\n\n"), _VAR_SUITE),
    ("a Suite Setup in Settings", _suite(_SELECT, _READ, _CHECK).replace(
        "Library    Collections\n", "Library    Collections\nSuite Setup    " + _SET_SUITE + "\n"), _SET_SUITE),
    ("the test's own Setup setting", _suite("[Setup]    " + _SET_TEST, _SELECT, _READ, _CHECK), _SET_TEST),
    ("SetTestVariable without spaces as the step", _suite(_SELECT, _SQUASHED, _READ, _CHECK), _SQUASHED),
]
_SETTER_IDS = [case[0] for case in _SETTER_ANYWHERE]

# What the reader cannot trust (owner, 2026-09-30: "Close the class" and "Fix both in the same amendment"): a line
# it cannot follow, a keyword or Variables entry whose meaning it cannot assume, a selection change it cannot see,
# or a Log that uses the value another way. In each, Robot reads another element or another selection than the
# reader would (planning evidence, Robot 7.4.2). (label, the file, the blocking text, its neutral replacement): the
# control is the same file with the blocking text replaced, and IS rewritten.
_NAME_LINE = "Generated Test\n"
_REPOINT = "${get_text_locator}=    Set Variable    id=other"
_LOCATOR_ENTRY = "${get_text_locator}    css=#dropdown\n"
_OWN_SET_VARIABLE = "\n\n*** Keywords ***\nSet Variable\n    [Arguments]    ${x}\n    RETURN    id=other\n"
_IMPORT_VARIABLES = "Import Variables    ${CURDIR}/v.py"
_IMPORT_RESOURCE = "Import Resource    ${CURDIR}/res.resource"
_SELECTOR_PREFIX = "Set Selector Prefix    iframe#f >>>"
_CANNOT_TRUST = [
    ("a typed assignment", _suite(_SELECT, "${get_text_locator: str}=    Set Variable    id=other", _READ, _CHECK),
     "${get_text_locator: str}=    Set Variable    id=other", "Log    x"),
    ("a step on the test-name line", _suite(_SELECT, _READ, _CHECK).replace(
        _NAME_LINE, "Generated Test    " + _REPOINT + "\n"), "Generated Test    " + _REPOINT + "\n", _NAME_LINE),
    ("an assignment on the test-name line", _suite("...    Set Variable    id=other", _SELECT, _READ, _CHECK).replace(
        _NAME_LINE, "Generated Test    ${get_text_locator}=\n"),
     "Generated Test    ${get_text_locator}=\n", _NAME_LINE),
    ("an indented continuation of the test-name line", _suite(_SELECT, _READ, _CHECK).replace(
        _NAME_LINE, _NAME_LINE + "    ...    " + _REPOINT + "\n"), "    ...    " + _REPOINT + "\n", "    Log    x\n"),
    ("an unindented continuation of the test-name line", _suite(_SELECT, _READ, _CHECK).replace(
        _NAME_LINE, _NAME_LINE + "...    " + _REPOINT + "\n"), "...    " + _REPOINT + "\n", "    Log    x\n"),
    ("a bare continuation line at column 0", _suite("Log    x", _SELECT, _READ, _CHECK).replace(
        "    Log    x\n", "...\n"), "\n...\n", "\n    Log    x\n"),
    ("a line indented by one space starts a new test", _suite(_SELECT, "Log    x", _READ, _CHECK).replace(
        "    Log    x\n", " Second Test    " + _REPOINT + "\n"),
     " Second Test    " + _REPOINT + "\n", "    Log    x\n"),
    ("the file's own Set Variable keyword", _suite(
        _SELECT, "${get_text_locator}=    Set Variable    css=#dropdown", _READ, _CHECK, tail=_OWN_SET_VARIABLE),
     "\nSet Variable\n", "\nMy Helper\n"),
    ("the file's own Should Contain keyword", _suite(
        _SELECT, _READ, _CHECK,
        tail="\n\n*** Keywords ***\nShould Contain\n    [Arguments]    @{a}\n    No Operation\n"),
     "\nShould Contain\n", "\nMy Helper\n"),
    ("the file's own Log keyword", _suite(
        _SELECT, _READ, "Log    ${selected_option}", _CHECK,
        tail="\n\n*** Keywords ***\nLog\n    [Arguments]    @{a}\n    No Operation\n"), "\nLog\n", "\nMy Helper\n"),
    ("a list of the same name first in Variables", _suite(_SELECT, _READ, _CHECK).replace(
        _LOCATOR_ENTRY, "@{get_text_locator}    id=other\n" + _LOCATOR_ENTRY),
     "@{get_text_locator}    id=other\n", "@{other_locator}    id=other\n"),
    ("a typed duplicate in Variables", _suite(_SELECT, _READ, _CHECK).replace(
        _LOCATOR_ENTRY, _LOCATOR_ENTRY + "${get_text_locator: str}    id=other\n"),
     "${get_text_locator: str}    id=other\n", "${other_locator: str}    id=other\n"),
    ("a list instead of the scalar in Variables", _suite(_SELECT, _READ, _CHECK).replace(
        _LOCATOR_ENTRY, "@{get_text_locator}    css=#dropdown\n"),
     "@{get_text_locator}    css=#dropdown\n", _LOCATOR_ENTRY),
    ("a select spelled without spaces", _suite(
        _SELECT, "SelectOptionsBy    ${dropdown_locator}    label    Option 1", _READ, _CHECK),
     "SelectOptionsBy    ${dropdown_locator}    label    Option 1", "Log    x"),
    ("the same select chosen again through an xpath", _suite(
        _SELECT, "Select Options By    xpath=//select[@id='dropdown']    label    Option 1", _READ, _CHECK),
     "xpath=//select[@id='dropdown']", "id=colour"),
    ("Deselect Options on the select", _suite(_SELECT, "Deselect Options    ${dropdown_locator}", _READ, _CHECK),
     "Deselect Options    ${dropdown_locator}", "Deselect Options    id=colour"),
    ("a wrapper runs another select", _suite(
        _SELECT, "Run Keyword    Select Options By    ${dropdown_locator}    label    Option 1", _READ, _CHECK),
     "Select Options By    ${dropdown_locator}    label    Option 1", "Log    x"),
    ("a Log of a method call on the value", _suite(_SELECT, _READ, "Log    ${selected_option.upper()}", _CHECK),
     "${selected_option.upper()}", "${selected_option}"),
    ("a Log of an item of the value", _suite(_SELECT, _READ, _CHECK, "Log    ${selected_option}[0]"),
     "${selected_option}[0]", "${selected_option}"),
    ("a Log of an expression over the value", _suite(_SELECT, _READ, "Log    ${{ ${selected_option} }}", _CHECK),
     "${{ ${selected_option} }}", "${selected_option}"),
    # A run-time import, a selector prefix, or a variable name built from another variable (owner, 2026-09-30:
    # "Close all six"): Robot 7.4.2 sends the read elsewhere, or uses its value, where this reader cannot see.
    ("Import Variables between select and read", _suite(_SELECT, _IMPORT_VARIABLES, _READ, _CHECK),
     _IMPORT_VARIABLES, "Log    x"),
    ("Import Variables in a Suite Setup", _suite(_SELECT, _READ, _CHECK).replace(
        "Library    Collections\n", "Library    Collections\nSuite Setup    " + _IMPORT_VARIABLES + "\n"),
     _IMPORT_VARIABLES, "Log    x"),
    ("Import Variables in an earlier test", _suite(_SELECT, _READ, _CHECK).replace(
        "*** Test Cases ***\n", "*** Test Cases ***\nEarlier Test\n    " + _IMPORT_VARIABLES + "\n\n"),
     _IMPORT_VARIABLES, "Log    x"),
    ("Import Resource between select and read", _suite(_SELECT, _IMPORT_RESOURCE, _READ, _CHECK),
     _IMPORT_RESOURCE, "Log    x"),
    ("Set Selector Prefix between select and read", _suite(_SELECT, _SELECTOR_PREFIX, _READ, _CHECK),
     _SELECTOR_PREFIX, "Log    x"),
    ("Set Selector Prefix in the file's own keyword", _suite(
        _SELECT, "Into Frame", _READ, _CHECK, tail="\n\n*** Keywords ***\nInto Frame\n    " + _SELECTOR_PREFIX + "\n"),
     _SELECTOR_PREFIX, "Log    x"),
    ("a nested name as an assignment target", _suite(
        _SELECT, "${get_text${EMPTY}_locator}=    Set Variable    id=nested", _READ, _CHECK),
     "${get_text${EMPTY}_locator}=", "${other_locator}="),
    ("a nested name first in Variables", _suite(_SELECT, _READ, _CHECK).replace(
        _LOCATOR_ENTRY, "${get_text${EMPTY}_locator}    id=nested_first\n" + _LOCATOR_ENTRY),
     "${get_text${EMPTY}_locator}    id=nested_first\n", "${other_locator}    id=nested_first\n"),
    # A later use of the value is also not allowed after the read (see _AFTER_THE_READ): the control replaces the
    # whole later line(s) with an allowed step.
    ("a nested name as a later use of the value", _suite(
        _SELECT, _READ, _CHECK, "Should Be Equal    ${selected_${EMPTY}option}    Option 2"),
     "Should Be Equal    ${selected_${EMPTY}option}    Option 2", _ALLOWED),
    # The same later use through Robot's `$name` expression syntax (Robot 7.4.2: each passes before the rewrite and
    # fails after it). The file is left alone for its `$` that starts no variable (see _BARE_DOLLAR).
    ("a nested name in an expression", _suite(
        _SELECT, _READ, _CHECK, "Should Be True    len($selected_${EMPTY}option) > 5"),
     "Should Be True    len($selected_${EMPTY}option) > 5", _ALLOWED),
    ("a nested name read by Get Variable Value", _suite(
        _SELECT, _READ, _CHECK, "${t}=    Get Variable Value    $selected_${EMPTY}option",
        "Should Be True    len($t) > 5"),
     "${t}=    Get Variable Value    $selected_${EMPTY}option\n    Should Be True    len($t) > 5", _ALLOWED),
    ("a nested name in an inline expression", _suite(
        _SELECT, _READ, _CHECK, "Should Be True    ${{ len($selected_${EMPTY}option) > 5 }}"),
     "Should Be True    ${{ len($selected_${EMPTY}option) > 5 }}", _ALLOWED),
    ("an expression name that starts with another variable", _suite(
        _SELECT, _READ, _CHECK, "Should Be True    len($${EMPTY}selected_option) > 5"),
     "Should Be True    len($${EMPTY}selected_option) > 5", _ALLOWED),
]
_CANNOT_TRUST_IDS = [case[0] for case in _CANNOT_TRUST]

# A `$` that starts no variable, or an escaped `\${`, anywhere in the file (owner, 2026-09-30: "One broad rule"):
# Robot can build the read's variable name from it at run time, out of the reader's sight (Robot 7.4.2: V1-V3, P1 and
# P2 each pass before the rewrite and fail after it). (label, the file, the control): the control is the same file
# with every such `$` taken out and its later use of the value replaced by an allowed step, and IS rewritten.
_POINTER_USE = "${t}=    Get Variable Value    ${n}\n    Should Be True    len($t) > 5"
_P1 = _suite("${n}=    Set Variable    $selected_option", _SELECT, _READ, _CHECK,
             "${t}=    Get Variable Value    ${n}", "Should Be True    len($t) > 5")
_ESCAPED = _suite("${n}=    Set Variable    \\${selected_option}", _SELECT, _READ, _CHECK,
                  "${t}=    Get Variable Value    ${n}", "Should Be True    len($t) > 5")
_P2 = _suite(_SELECT, _READ, _CHECK, "Should Be True    len(${n_var}) > 5").replace(
    _LOCATOR_ENTRY, _LOCATOR_ENTRY + "${n_var}    $selected_option\n")
_BARE_DOLLAR = [
    ("V1 a name prefix set to $selected", _suite(
        "${p}=    Set Variable    $selected", _SELECT, _READ, _CHECK, "Should Be True    len(${p}_option) > 5"),
     _suite("${p}=    Set Variable    selected", _SELECT, _READ, _CHECK, _ALLOWED)),
    ("V2 a lone $ set as a variable", _suite(
        "${d}=    Set Variable    $", _SELECT, _READ, _CHECK, "Should Be True    len(${d}selected_option) > 5"),
     _suite("${d}=    Set Variable    x", _SELECT, _READ, _CHECK, _ALLOWED)),
    ("V3 a $ from an inline expression", _suite(
        _SELECT, _READ, _CHECK, "Should Be True    len(${{'$'}}selected_option) > 5"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("P1 a pointer set before the read", _P1, _P1.replace("    $selected_option", "    other_value").replace(
        _POINTER_USE, _ALLOWED)),
    ("P2 a pointer in Variables", _P2, _P2.replace("    $selected_option", "    other_value").replace(
        "Should Be True    len(${n_var}) > 5", _ALLOWED)),
    ("a pointer built by Catenate", _suite(
        "${n}=    Catenate    SEPARATOR=    $    selected_option", _SELECT, _READ, _CHECK,
        "Should Be True    len(${n}) > 5"),
     _suite("${n}=    Catenate    SEPARATOR=    x    selected_option", _SELECT, _READ, _CHECK, _ALLOWED)),
    ("an escaped pointer", _ESCAPED, _ESCAPED.replace("    \\${selected_option}", "    other_value").replace(
        _POINTER_USE, _ALLOWED)),
    # Only the escaped `\${` alternative catches this file: the escape sits BEFORE the read, and only allowed steps
    # follow the read.
    ("an escaped ${ before the read", _suite("Log    \\${literal}", _SELECT, _READ, _CHECK),
     _suite(_SELECT, _READ, _CHECK)),
    # The accepted cost: a q08 test in a file that also holds a bare `$` gets no fix.
    ("accepted cost: a price in another test", _suite(
        _SELECT, _READ, _CHECK, tail="\n\nOther Test\n    Log    Price: $5\n"),
     _suite(_SELECT, _READ, _CHECK, tail="\n\nOther Test\n    Log    Price: 5\n")),
]
_BARE_DOLLAR_IDS = [case[0] for case in _BARE_DOLLAR]


def _with_setting(line: str) -> str:
    """The q08 file with one more line in its Settings section."""
    return _suite(_SELECT, _READ, _CHECK).replace("Library    Collections\n", f"Library    Collections\n{line}\n")


# After the read only its check, a `Log` of the bare value with literal other cells, and Browser's Close Browser /
# Close Context / Close Page with literal arguments may run (owner, 2026-09-30: "Allowlist after read"): Robot can
# reach the read's variable by a name built at run time in ways no text scan follows. Each later use below passes
# before the rewrite and fails after it (RF 7.3.2: final-fix-wave-rereview3.md X1, X8, Y1-Y3 and X11, and
# preflight/fw4-probes for the Log level and the own Close Browser); a teardown runs after the read too. (label, the
# file, the control): the control is the same file with only allowed steps after the read, and IS rewritten.
_OWN_CLOSE_BROWSER = "\n\n*** Keywords ***\nClose Browser\n    Log    own\n"
_FI = "language: fi\n\n"
_RF_VAR_USE = "Should Be True    len(RF_VAR_selected_option) > 5"
_AFTER_THE_READ = [
    ("an escaped $ builds the name", _suite(
        "${p}=    Set Variable    \\x24selected", _SELECT, _READ, _CHECK, "Should Be True    len(${p}_option) > 5"),
     _suite("${p}=    Set Variable    \\x24selected", _SELECT, _READ, _CHECK, _ALLOWED)),
    ("Get Variable Value of @selected_option", _suite(
        _SELECT, _READ, _CHECK, "${t}=    Get Variable Value    @selected_option",
        "Should Be Equal    ${t}    ${None}"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("RF_VAR_ in an expression", _suite(
        _SELECT, _READ, _CHECK, "Should Be True    len(RF_VAR_selected_option) > 5"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("RF_VAR_ in an inline expression", _suite(
        _SELECT, _READ, _CHECK, "Should Be True    ${{ len(RF_VAR_selected_option) > 5 }}"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("Get Variables", _suite(
        _SELECT, _READ, _CHECK, "${all}=    Get Variables    no_decoration=True",
        "Should Contain    ${all}[selected_option]    Option 1"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("a name built by Convert To Bytes 36 int", _suite(
        _SELECT, _READ, _CHECK, "${b}=    Convert To Bytes    36    int", "${s}=    Convert To String    ${b}",
        "${n}=    Catenate    SEPARATOR=    ${s}    selected_option", "${t}=    Get Variable Value    ${n}",
        "Should Contain    ${t}    Option 1"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("accepted cost: a Click after the check", _suite(_SELECT, _READ, _CHECK, "Click    id=submit"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("an assignment after the check", _suite(
        _SELECT, _READ, _CHECK, "${selected_option}=    Set Variable    other",
        "Should Be Equal    ${selected_option}    other"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("a Log with an inline expression in another cell", _suite(
        _SELECT, _READ, _CHECK,
        "Log    ${selected_option}    level=${{ 'INFO' if len(RF_VAR_selected_option) > 5 else 'BAD' }}"),
     _suite(_SELECT, _READ, _CHECK, "Log    ${selected_option}    level=INFO")),
    ("Close Browser with a variable argument", _suite(_SELECT, _READ, _CHECK, "Close Browser    ${browser}"),
     _suite(_SELECT, _READ, _CHECK, "Close Browser    ALL")),
    ("the file's own Close Browser keyword", _suite(_SELECT, _READ, _CHECK, tail=_OWN_CLOSE_BROWSER),
     _suite(_SELECT, _READ, _CHECK, tail=_OWN_CLOSE_BROWSER.replace("\nClose Browser\n", "\nMy Helper\n"))),
    ("a [Teardown] at the top of the test", _suite("[Teardown]    Close Browser", _SELECT, _READ, _CHECK),
     _suite("[Setup]    Close Browser", _SELECT, _READ, _CHECK)),
    ("a [Teardown] at the end of the test", _suite(_SELECT, _READ, _CHECK, "[Teardown]    Close Browser"),
     _suite(_SELECT, _READ, _CHECK, _ALLOWED)),
    ("a Test Teardown in Settings", _with_setting("Test Teardown    Close Browser"),
     _with_setting("Test Setup    Close Browser")),
    ("a Task Teardown in Settings", _with_setting("Task Teardown    Close Browser"),
     _with_setting("Task Setup    Close Browser")),
    ("a Suite Teardown in Settings", _with_setting("Suite Teardown    Close Browser"),
     _with_setting("Suite Setup    Close Browser")),
    # A `language:` line makes Robot accept translated markers, a teardown among them (Finnish: `Testin Alasajo` is
    # Test Teardown, `[Alasajo]` is [Teardown], `*** Asetukset ***` is *** Settings ***; each passes before the rewrite
    # and fails after it, RF 7.4.2: preflight/fw4-rereview/ctl_rf742.out): the file is left alone. The control is the
    # same file without the `language:` line.
    ("L1 a translated Test Teardown", _FI + _with_setting(f"Testin Alasajo    {_RF_VAR_USE}"),
     _with_setting(f"Testin Alasajo    {_RF_VAR_USE}")),
    # Robot strips a leading byte-order mark before it reads the first line, so the `language:` line still counts.
    ("L1 with a byte-order mark before the language line",
     "\N{ZERO WIDTH NO-BREAK SPACE}" + _FI + _with_setting(f"Testin Alasajo    {_RF_VAR_USE}"),
     _with_setting(f"Testin Alasajo    {_RF_VAR_USE}")),
    # Robot also honours the setting on a `...` line as the file's first data line (it drops the `...`; RF 7.4.2:
    # preflight/fw6-rereview/ctl_rf742.out, G1).
    ("G1 a language setting on a continuation line",
     "...    " + _FI + _with_setting(f"Testin Alasajo    {_RF_VAR_USE}"),
     _with_setting(f"Testin Alasajo    {_RF_VAR_USE}")),
    ("L2 a translated [Teardown] at the top of the test",
     _FI + _suite(f"[Alasajo]    {_RF_VAR_USE}", _SELECT, _READ, _CHECK),
     _suite(f"[Alasajo]    {_RF_VAR_USE}", _SELECT, _READ, _CHECK)),
    ("L3 a translated Test Teardown under a translated Settings header",
     _FI + _with_setting(f"Testin Alasajo    {_RF_VAR_USE}").replace("*** Settings ***", "*** Asetukset ***"),
     _with_setting(f"Testin Alasajo    {_RF_VAR_USE}").replace("*** Settings ***", "*** Asetukset ***")),
]
_AFTER_THE_READ_IDS = [case[0] for case in _AFTER_THE_READ]


class TestSelectReadLeftAlone:
    @pytest.mark.parametrize("label, body", [
        ("select by index", ("Select Options By    ${dropdown_locator}    index    2", _READ,
                             "Should Contain    ${selected_option}    2")),
        ("custom dropdown: no Select Options By", ("Click    css=.react-select", "Click    text=Option 2",
                                                   "${selected_option}=    Get Text    css=.react-select", _CHECK)),
        ("Get Text on the selected <option> itself", (
            _SELECT, "${selected_option}=    Get Text    css=#dropdown option[selected]", _CHECK)),
        ("an xpath read of an id-selected select", (
            _SELECT, "${selected_option}=    Get Text    xpath=//select[@id='dropdown']", _CHECK)),
        ("Should Be Equal already fails loudly", (_SELECT, _READ, "Should Be Equal    ${selected_option}    Option 2")),
        ("any other use of the variable", (_SELECT, _READ, "Should Be True    '${selected_option}' != ''", _CHECK)),
        ("an expression use", (_SELECT, _READ, _CHECK, "Should Be True    $selected_option")),
        ("an item use", (_SELECT, _READ, _CHECK, "Log    ${selected_option}[0]",
                         "Should Be Empty    ${selected_option}[0]")),
        ("Should Contain with an extra argument", (
            _SELECT, _READ, "Should Contain    ${selected_option}    Option 2    ignore_case=True")),
        ("Log To Console is not Log", (_SELECT, _READ, _CHECK, "Log To Console    ${selected_option}")),
        ("presence query: V is not the selected value", (
            _SELECT, _READ, "Should Contain    ${selected_option}    Option 1")),
        ("one of two checks names another value", (
            _SELECT, _READ, _CHECK, "Should Contain    ${selected_option}    Option 1")),
        ("a different element", (_SELECT, "${selected_option}=    Get Text    id=result", _CHECK)),
        ("the read before the select", (_READ, _SELECT, _CHECK)),
        ("Log only: the 50dcf346 shape", (_SELECT, "${selected_text}=    Get Text    ${get_text_locator}",
                                          "Log    Retrieved: ${selected_text}")),
        ("no use at all", (_SELECT, _READ)),
        ("inline == fails loudly already", (_SELECT, "Get Text    ${get_text_locator}    ==    Option 2")),
        ("inline form with an assignment", (
            _SELECT, "${t}=    Get Text    ${get_text_locator}    contains    Option 2",
            "Should Contain    ${t}    Option 2")),
        ("inline form naming another value", (_SELECT, "Get Text    ${get_text_locator}    contains    Option 1")),
        ("inline form with a message argument", (
            _SELECT, "Get Text    ${get_text_locator}    contains    Option 2    msg")),
        ("a non-Browser library prefix", (
            _SELECT, "${selected_option}=    Other.Get Text    ${get_text_locator}", _CHECK)),
        ("a select value that is only known at run time", (
            "${wanted}=    Get Text    id=label", "Select Options By    ${dropdown_locator}    label    ${wanted}",
            _READ, "Should Contain    ${selected_option}    ${wanted}")),
        ("a select on an element that cannot be named", (
            _SELECT, "Select Options By    ${unknown}    label    Option 1", _READ, _CHECK)),
        ("the locator variable redefined between select and read", (
            "Select Options By    ${get_text_locator}    label    Option 2",
            "${get_text_locator}=    Set Variable    id=other", _READ, _CHECK)),
        ("the locator variable made unknown between select and read", (
            _SELECT, "Set Test Variable    ${get_text_locator}    id=other", _READ, _CHECK)),
        ("a [Teardown] at the top uses the variable", (
            "[Teardown]    Should Be Equal    ${selected_option}    Option 2", _SELECT, _READ, _CHECK)),
        ("an IF block in the test", (_SELECT, "IF    True", "    Log    x", "END", _READ, _CHECK)),
        ("a FOR loop in the test", (_SELECT, "FOR    ${i}    IN    a", "    Log    ${i}", "END", _READ, _CHECK)),
        ("a VAR statement in the test", ("VAR    ${x}    1", _SELECT, _READ, _CHECK)),
        ("a [Template] test", ("[Template]    Log", _SELECT, _READ, _CHECK)),
    ])
    def test_left_alone(self, label, body):
        source = _suite(*body)
        assert rewrite_select_text_reads(source) == source, label

    @pytest.mark.parametrize("label, body", [
        ("continuation on the select line", (_SELECT, "...    Option 1", _READ, _CHECK)),
        ("continuation on the read line", (_SELECT, _READ, "...    contains    Option 2", _CHECK)),
        ("continuation on the check line", (_SELECT, _READ, _CHECK, "...    ignore_case=True")),
        ("the variable only on a continuation line", (
            _SELECT, _READ, _CHECK, "Should Be Equal", "...    ${selected_option}    x")),
    ])
    def test_a_continuation_line_is_never_followed(self, label, body):
        # docs/TODO.md rows 18 and 22: the line model does not follow `...` lines — inherited, not fixed.
        # Each first line is complete on its own, so only the continuation rule can leave the file alone
        # (a first line missing its value, locator or expected text is skipped for that reason instead,
        # and would pass even if the rule followed continuations — proved by mutation in the planning dry run).
        source = _suite(*body)
        assert rewrite_select_text_reads(source) == source, label

    def test_a_read_in_another_test_is_left_alone(self):
        source = _suite(_SELECT, tail=f"\n\nSecond Test\n    {_READ}\n    {_CHECK}\n")
        assert rewrite_select_text_reads(source) == source

    @pytest.mark.parametrize("definition", [
        "Get Text", "get_text", "GetText", "Get ${what}", "Get Selected Options", "Select Options By",
    ])
    def test_a_file_defining_the_keyword_itself_is_left_alone(self, definition):
        source = _suite(_SELECT, _READ, _CHECK,
                        tail=f"\n\n*** Keywords ***\n{definition}\n    [Arguments]    @{{a}}\n    Log    own\n")
        assert rewrite_select_text_reads(source) == source

    def test_a_settings_section_teardown_using_the_variable(self):
        source = _suite(_SELECT, _READ, _CHECK).replace(
            "Library    Collections\n", "Library    Collections\nTest Teardown    Log    ${selected_option}\n")
        assert rewrite_select_text_reads(source) == source

    def test_a_test_template_in_settings(self):
        source = _suite(_SELECT, _READ, _CHECK).replace(
            "Library    Collections\n", "Library    Collections\nTest Template    Log\n")
        assert rewrite_select_text_reads(source) == source

    def test_try_in_another_test_of_the_file(self):
        source = _suite(_SELECT, _READ, _CHECK, tail=_OTHER_TEST_WITH_TRY)
        assert rewrite_select_text_reads(source) == source

    def test_control_the_same_file_without_try_is_rewritten(self):
        source = _suite(_SELECT, _READ, _CHECK, tail="\n\nOther Test\n    Click    id=x\n")
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW)

    def test_own_keyword_under_an_error_catching_wrapper(self):
        source = _suite("Run Keyword And Return Status    Pick", _SELECT, _READ, _CHECK,
                        tail="\n\n*** Keywords ***\nPick\n    Click    id=x\n")
        assert rewrite_select_text_reads(source) == source

    def test_control_the_same_file_without_the_wrapper_is_rewritten(self):
        source = _suite("Pick", _SELECT, _READ, _CHECK, tail="\n\n*** Keywords ***\nPick\n    Click    id=x\n")
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW)

    def test_an_assignment_only_line_leaves_the_test_alone(self):
        # Robot joins `${x}=` and the `...` line under it into ONE statement that re-points the read's locator
        # (Robot 7.4.2, planning evidence); the reader holds no step for an assignment-only line (Rule 1).
        source = _suite(_SELECT, "Click    id=go", "${get_text_locator}=", "...    Set Variable    id=other",
                        _READ, _CHECK)
        assert rewrite_select_text_reads(source) == source

    def test_control_the_same_test_without_the_split_assignment_is_rewritten(self):
        source = _suite(_SELECT, "Click    id=go", "Log    x", _READ, _CHECK)
        assert rewrite_select_text_reads(source) == source.replace(_READ, _READ_NEW)

    @pytest.mark.parametrize("label, source, setter", _SETTER_ANYWHERE, ids=_SETTER_IDS)
    def test_a_scoped_setter_or_var_anywhere_leaves_the_file_alone(self, label, source, setter):
        # Rule 2: the whole file is left alone, silently, as for a pipe-separated line.
        assert rewrite_select_text_reads(source) == source, label

    @pytest.mark.parametrize("label, source, setter", _SETTER_ANYWHERE, ids=_SETTER_IDS)
    def test_control_the_same_file_with_a_neutral_step_is_rewritten(self, label, source, setter):
        control = source.replace(setter, "Log    x")
        assert control != source, label
        assert rewrite_select_text_reads(control) == control.replace(_READ, _READ_NEW), label

    @pytest.mark.parametrize("label, source, blocking, neutral", _CANNOT_TRUST, ids=_CANNOT_TRUST_IDS)
    def test_what_the_reader_cannot_trust_is_left_alone(self, label, source, blocking, neutral):
        assert rewrite_select_text_reads(source) == source, label

    @pytest.mark.parametrize("label, source, blocking, neutral", _CANNOT_TRUST, ids=_CANNOT_TRUST_IDS)
    def test_control_the_same_file_without_the_blocking_text_is_rewritten(self, label, source, blocking, neutral):
        control = source.replace(blocking, neutral)
        assert control != source, label
        assert rewrite_select_text_reads(control) == control.replace(_READ, _READ_NEW), label

    @pytest.mark.parametrize("label, source, control", _BARE_DOLLAR, ids=_BARE_DOLLAR_IDS)
    def test_a_dollar_that_starts_no_variable_leaves_the_file_alone(self, label, source, control):
        assert rewrite_select_text_reads(source) == source, label

    @pytest.mark.parametrize("label, source, control", _BARE_DOLLAR, ids=_BARE_DOLLAR_IDS)
    def test_control_the_same_file_without_that_dollar_is_rewritten(self, label, source, control):
        assert control != source, label
        assert rewrite_select_text_reads(control) == control.replace(_READ, _READ_NEW), label

    @pytest.mark.parametrize("label, source, control", _AFTER_THE_READ, ids=_AFTER_THE_READ_IDS)
    def test_anything_not_allowed_after_the_read_leaves_it_alone(self, label, source, control):
        assert rewrite_select_text_reads(source) == source, label

    @pytest.mark.parametrize("label, source, control", _AFTER_THE_READ, ids=_AFTER_THE_READ_IDS)
    def test_control_only_allowed_steps_after_the_read_is_rewritten(self, label, source, control):
        assert control != source, label
        assert rewrite_select_text_reads(control) == control.replace(_READ, _READ_NEW), label

    def test_a_pipe_separated_line_anywhere(self):
        source = _suite(_SELECT, _READ, _CHECK).replace("${headless}    True", "| ${headless} | True |")
        assert rewrite_select_text_reads(source) == source

    def test_a_rare_linebreak_anywhere(self):
        source = _suite(_SELECT, _READ, _CHECK, "Log    a\x1cb")
        assert rewrite_select_text_reads(source) == source

    def test_empty_and_none(self):
        assert rewrite_select_text_reads("") == ""
        assert rewrite_select_text_reads(None) is None


class TestSelectReadIdempotentAndLogged:
    def test_a_second_pass_changes_nothing(self):
        once = rewrite_select_text_reads(_suite(_SELECT, _READ, _CHECK))
        assert once != _suite(_SELECT, _READ, _CHECK)
        assert rewrite_select_text_reads(once) == once

    def test_one_info_line_per_fired_rewrite(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.robot_code_normalizer"):
            rewrite_select_text_reads(_suite(_SELECT, _READ, _CHECK))
        assert [r.getMessage() for r in caplog.records] == [
            "Select read normalizer: rewrote 1 Get Text line(s) on a select to Get Selected Options"]

    def test_a_guarded_file_says_why_it_was_left_alone(self, caplog):
        source = _suite(_SELECT, _READ, _CHECK, tail="\n\n*** Keywords ***\nGet Text\n    Log    own\n")
        with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.robot_code_normalizer"):
            rewrite_select_text_reads(source)
        assert "left 1 Get Text line(s) on a select unchanged" in caplog.text
        assert "(Get Text)" in caplog.text

    def test_nothing_is_logged_when_nothing_fires(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.robot_code_normalizer"):
            rewrite_select_text_reads(_suite(_SELECT, "${t}=    Get Text    id=other", "Log    ${t}"))
        assert caplog.records == []


# ---------------------------------------------------------------------------
# rewrite_typed_value_reads (N1)
# ---------------------------------------------------------------------------

_N1_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "Library    Collections\n"
    "\n"
    "*** Variables ***\n"
    "${browser}    chromium\n"
    "${headless}    True\n"
    "${customer_first_name_locator}    id=customer.firstName\n"
    "${customer_last_name_locator}    id=customer.lastName\n"
    "${customer_city_locator}    id=customer.address.city\n"
    "\n"
    "*** Test Cases ***\n"
    "Parabank Registration Test\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    https://parabank.parasoft.com/parabank/register.htm\n"
)


def _n1(*body: str, tail: str = "") -> str:
    return _N1_HEAD + "".join(f"    {line}\n" for line in body) + "    Close Browser" + tail


_NAMES = ("Fill Text    ${customer_first_name_locator}    John",
          "Fill Text    ${customer_last_name_locator}    Smith")
_TYPE = "Fill Text    ${customer_city_locator}    NewYork"
_ATTR = "${city_value}=    Get Attribute    ${customer_city_locator}    value"
_PROP = "${city_value}=    Get Property    ${customer_city_locator}    value"
_USE = "Should Be True    '${city_value}' == 'NewYork'"

# Rule 2 for N1: the typed field's locator re-pointed between the typing and the read: (label, the file). The
# control is the same file with the setter text replaced by a neutral `Log    x`, and IS rewritten.
_N1_SETTER = "Set Test Variable    ${customer_city_locator}    id=other"
_N1_SETTER_ANYWHERE = [
    ("a wrapper argument", _n1(_TYPE, "Run Keyword If    True    " + _N1_SETTER, _ATTR, _USE)),
    ("the file's own keyword", _n1(_TYPE, "Pick", _ATTR, _USE,
                                   tail="\n\n*** Keywords ***\nPick\n    " + _N1_SETTER + "\n")),
]
_N1_SETTER_IDS = [case[0] for case in _N1_SETTER_ANYWHERE]

# The shared reader's rules and N1's own-keyword guard, as for q08 (owner, 2026-09-30: "Close the class"): (label,
# the file, the blocking text, its neutral replacement). The control is the same file with the blocking text
# replaced, and IS rewritten.
_N1_NAME_LINE = "Parabank Registration Test\n"
_N1_REPOINT = "${customer_city_locator}=    Set Variable    id=other"
_N1_ENTRY = "${customer_city_locator}    id=customer.address.city\n"
_N1_CANNOT_TRUST = [
    ("a typed assignment", _n1(_TYPE, "${customer_city_locator: str}=    Set Variable    id=other", _ATTR, _USE),
     "${customer_city_locator: str}=    Set Variable    id=other", "Log    x"),
    ("a step on the test-name line", _n1(_TYPE, _ATTR, _USE).replace(
        _N1_NAME_LINE, "Parabank Registration Test    " + _N1_REPOINT + "\n"),
     "Parabank Registration Test    " + _N1_REPOINT + "\n", _N1_NAME_LINE),
    ("an indented continuation of the test-name line", _n1(_TYPE, _ATTR, _USE).replace(
        _N1_NAME_LINE, _N1_NAME_LINE + "    ...    " + _N1_REPOINT + "\n"),
     "    ...    " + _N1_REPOINT + "\n", "    Log    x\n"),
    ("a bare continuation line at column 0", _n1("Log    x", _TYPE, _ATTR, _USE).replace(
        "    Log    x\n", "...\n"), "\n...\n", "\n    Log    x\n"),
    ("a line indented by one space starts a new test", _n1(_TYPE, "Log    x", _ATTR, _USE).replace(
        "    Log    x\n", " Second Test    " + _N1_REPOINT + "\n"),
     " Second Test    " + _N1_REPOINT + "\n", "    Log    x\n"),
    ("a list of the same name first in Variables", _n1(_TYPE, _ATTR, _USE).replace(
        _N1_ENTRY, "@{customer_city_locator}    id=other\n" + _N1_ENTRY),
     "@{customer_city_locator}    id=other\n", "@{other_locator}    id=other\n"),
    ("the file's own Set Variable keyword", _n1(
        _TYPE, "${customer_city_locator}=    Set Variable    id=customer.address.city", _ATTR, _USE,
        tail=_OWN_SET_VARIABLE), "\nSet Variable\n", "\nMy Helper\n"),
    # As for q08 (owner, 2026-09-30: "Close all six").
    ("Import Variables between the typing and the read", _n1(_TYPE, _IMPORT_VARIABLES, _ATTR, _USE),
     _IMPORT_VARIABLES, "Log    x"),
    ("Set Selector Prefix between the typing and the read", _n1(_TYPE, _SELECTOR_PREFIX, _ATTR, _USE),
     _SELECTOR_PREFIX, "Log    x"),
    ("a nested name as an assignment target", _n1(
        _TYPE, "${customer_city${EMPTY}_locator}=    Set Variable    id=other", _ATTR, _USE),
     "${customer_city${EMPTY}_locator}=", "${other_locator}="),
    ("a nested name first in Variables", _n1(_TYPE, _ATTR, _USE).replace(
        _N1_ENTRY, "${customer_city${EMPTY}_locator}    id=other\n" + _N1_ENTRY),
     "${customer_city${EMPTY}_locator}    id=other\n", "${other_locator}    id=other\n"),
]
_N1_CANNOT_TRUST_IDS = [case[0] for case in _N1_CANNOT_TRUST]

# A file that can hand Fill Secret / Type Secret a secret (owner, 2026-10-01, PR #120 review): Browser 19.14.2 takes one
# only as `$name`, an escaped `\${name}`, `%NAME`, or an RF `Secret` made from `%{NAME}`. Before the rewrite the read
# fails and logs nothing; after it the read passes and output.xml holds the secret (runner, RF 7.4.2). So a `$` that
# starts no variable, an escaped `\${` or any `%` anywhere leaves the file alone. (label, the file, the blocking text,
# its neutral replacement): the control is the same file with the blocking text replaced, and IS rewritten.
_SECRET_FILL = "Fill Secret    ${customer_city_locator}    "
_N1_SECRET = [
    ("Fill Secret with $name", _n1(_TYPE, _SECRET_FILL + "$pw", _ATTR, _USE), "$pw", "${pw}"),
    ("Type Secret with $name", _n1(_TYPE, "Type Secret    ${customer_city_locator}    $pw", _ATTR, _USE),
     "$pw", "${pw}"),
    ("Fill Secret with %NAME", _n1(_TYPE, _SECRET_FILL + "%PW_ENV", _ATTR, _USE), "%PW_ENV", "${pw}"),
    ("Fill Secret with an escaped ${name}", _n1(_TYPE, _SECRET_FILL + "\\${pw}", _ATTR, _USE), "\\${pw}", "${pw}"),
    ("a Secret made from the environment", _n1(_TYPE, _SECRET_FILL + "${pw}", _ATTR, _USE).replace(
        _N1_ENTRY, _N1_ENTRY + "${pw: Secret}    %{PW_ENV}\n"), "%{PW_ENV}", "plain"),
    ("a keyword name built at run time", _n1(
        _TYPE, "Run Keyword    Fill ${EMPTY}Secret    ${customer_city_locator}    $pw", _ATTR, _USE), "$pw", "${pw}"),
    ("the secret in another test of the file", _n1(
        _TYPE, _ATTR, _USE, tail="\n\nSecond Test\n    Fill Secret    id=pw    $pw\n"), "$pw", "${pw}"),
    # The accepted cost: a `%` that carries no secret also leaves the file alone.
    ("accepted cost: a % in the New Page URL", _n1(_TYPE, _ATTR, _USE).replace(
        "register.htm\n", "register.htm?ref=a%20b\n"), "a%20b", "a20b"),
    ("accepted cost: a % in a Log line", _n1(_TYPE, "Log    50% done", _ATTR, _USE), "50% done", "50 done"),
]
_N1_SECRET_IDS = [case[0] for case in _N1_SECRET]


class TestTypedValueRewritten:
    def test_the_corpus_shape(self):
        """Bench run 8adaa3c8-ab51-4f78-8f69-38f161ba8360, byte for byte."""
        assert rewrite_typed_value_reads(_n1(*_NAMES, _TYPE, _ATTR, _USE)) == _n1(*_NAMES, _TYPE, _PROP, _USE)

    @pytest.mark.parametrize("typing", [
        "Fill Text    ${customer_city_locator}    NewYork",
        # A plain `${secret}`: Browser refuses it at run time, so no secret ever reaches the field, and the step still
        # proves a field. `$secret` would leave the whole file alone (see _N1_SECRET).
        "Fill Secret    ${customer_city_locator}    ${secret}",
        "Clear Text    ${customer_city_locator}",
        "Type Text    ${customer_city_locator}    NewYork",
        "Type Text    ${customer_city_locator}    NewYork    delay=10 ms",
        "Type Text    ${customer_city_locator}    txt=NewYork",
        "Type Text    ${customer_city_locator}    txt=NewYork    delay=10 ms",
        "Type Secret    ${customer_city_locator}    ${secret}",
        "Browser.Fill Text    ${customer_city_locator}    NewYork",
    ])
    def test_each_typing_keyword_that_proves_a_field(self, typing):
        assert rewrite_typed_value_reads(_n1(typing, _ATTR, _USE)) == _n1(typing, _PROP, _USE)

    def test_the_named_form_renames_the_argument(self):
        attr = "${city_value}=    Get Attribute    ${customer_city_locator}    attribute=value"
        prop = "${city_value}=    Get Property    ${customer_city_locator}    property=value"
        assert rewrite_typed_value_reads(_n1(_TYPE, attr, _USE)) == _n1(_TYPE, prop, _USE)

    def test_inline_assertion_arguments_keep_their_positions(self):
        attr = "Get Attribute    ${customer_city_locator}    value    ==    NewYork"
        prop = "Get Property    ${customer_city_locator}    value    ==    NewYork"
        assert rewrite_typed_value_reads(_n1(_TYPE, attr)) == _n1(_TYPE, prop)

    def test_the_browser_prefix_is_kept(self):
        attr = "${city_value}=    Browser.Get Attribute    ${customer_city_locator}    value"
        prop = "${city_value}=    Browser.Get Property    ${customer_city_locator}    value"
        assert rewrite_typed_value_reads(_n1(_TYPE, attr, _USE)) == _n1(_TYPE, prop, _USE)

    @pytest.mark.parametrize("typed, read", [("id=city", "css=#city"), ("css=#city", "id=city"),
                                             ("id=city", "id=city")])
    def test_the_same_element_in_every_spelling(self, typed, read):
        typing = f"Fill Text    {typed}    NewYork"
        attr = f"${{v}}=    Get Attribute    {read}    value"
        prop = f"${{v}}=    Get Property    {read}    value"
        assert rewrite_typed_value_reads(_n1(typing, attr)) == _n1(typing, prop)

    def test_idempotent_and_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.robot_code_normalizer"):
            once = rewrite_typed_value_reads(_n1(_TYPE, _ATTR, _USE))
        assert [r.getMessage() for r in caplog.records] == [
            "Typed value normalizer: rewrote 1 Get Attribute … value line(s) to Get Property"]
        assert rewrite_typed_value_reads(once) == once


class TestTypedValueLeftAlone:
    @pytest.mark.parametrize("label, body", [
        ("a prefilled field the test never typed into", (_ATTR, _USE)),
        ("typed into a different element", (_NAMES[0], _ATTR, _USE)),
        ("the read before the typing", (_ATTR, _TYPE, _USE)),
        ("Press Keys types into anything", ("Press Keys    ${customer_city_locator}    a", _ATTR)),
        ("Type Text with clear switched off", ("Type Text    ${customer_city_locator}    x    clear=False", _ATTR)),
        ("Type Text with clear given by position", ("Type Text    ${customer_city_locator}    x    0    False", _ATTR)),
        ("Type Text with clear named before the text",
         ("Type Text    ${customer_city_locator}    clear=False    txt=NewYork", _ATTR)),
        ("Type Text with clear in an expanded list",
         ("@{rest}=    Create List    0 ms    False", "Type Text    ${customer_city_locator}    NewYork    @{rest}", _ATTR)),
        ("Type Text with clear in an expanded dict",
         ("&{kw}=    Create Dictionary    clear=False", "Type Text    ${customer_city_locator}    NewYork    &{kw}", _ATTR)),
        ("Type Text with every argument in an expanded list",
         ("@{all}=    Create List    NewYork    0 ms    False", "Type Text    ${customer_city_locator}    @{all}", _ATTR)),
        # `${pw}`, not `$pw`: a `$` that starts no variable would leave the file alone by itself (see _N1_SECRET), so
        # the `clear` rule must be the only thing that blocks these two.
        ("Type Secret with clear named before the secret",
         ("Type Secret    ${customer_city_locator}    clear=False    secret=${pw}", _ATTR)),
        # Robot resolves a named argument's name at run time: each of these names is `clear`.
        ("Type Text with clear named through a variable",
         ("${n}=    Set Variable    clear", "Type Text    ${customer_city_locator}    NewYork    ${n}=False", _ATTR)),
        ("Type Text with txt named and clear named through a variable",
         ("${n}=    Set Variable    clear", "Type Text    ${customer_city_locator}    txt=NewYork    ${n}=False", _ATTR)),
        ("Type Text with an escape inside the name clear",
         ("Type Text    ${customer_city_locator}    NewYork    cl\\ear=False", _ATTR)),
        ("Type Text with an escape before the name clear",
         ("Type Text    ${customer_city_locator}    NewYork    \\clear=False", _ATTR)),
        ("Type Text with an empty variable inside the name clear",
         ("Type Text    ${customer_city_locator}    NewYork    c${EMPTY}lear=False", _ATTR)),
        ("Type Text with clear named by an inline expression",
         ("Type Text    ${customer_city_locator}    NewYork    ${{'clear'}}=False", _ATTR)),
        ("Type Text with clear named by a character escape",
         ("Type Text    ${customer_city_locator}    NewYork    \\x63lear=False", _ATTR)),
        ("Type Secret with clear named through a variable",
         ("${n}=    Set Variable    clear", "Type Secret    ${customer_city_locator}    ${pw}    ${n}=False", _ATTR)),
        # Accepted cost: a positional text holding `=` no longer proves a field.
        ("Type Text with a text holding =", ("Type Text    ${customer_city_locator}    a=b", _ATTR)),
        ("Keyboard Input names no element", ("Click    ${customer_city_locator}", "Keyboard Input    type    x", _ATTR)),
        ("Input Text is SeleniumLibrary's, not Browser's", ("Input Text    ${customer_city_locator}    x", _ATTR)),
        ("another attribute", (_TYPE, "${t}=    Get Attribute    ${customer_city_locator}    title")),
        ("an upper-case attribute name", (_TYPE, "${t}=    Get Attribute    ${customer_city_locator}    VALUE")),
        ("a dotted id is not a CSS id", ("Fill Text    id=customer.address.city    x",
                                        "${v}=    Get Attribute    css=#customer.address.city    value")),
        ("a continuation on the read line", (_TYPE, _ATTR, "...    ==    NewYork")),
        ("a continuation on the typing line", ("Type Text    ${customer_city_locator}    x", "...    clear=False", _ATTR)),
        ("a non-Browser typing keyword", ("Other.Fill Text    ${customer_city_locator}    x", _ATTR)),
        ("an IF block in the test", (_TYPE, "IF    True", "    Log    x", "END", _ATTR)),
    ])
    def test_left_alone(self, label, body):
        source = _n1(*body)
        assert rewrite_typed_value_reads(source) == source, label

    def test_a_read_in_another_test_is_left_alone(self):
        source = _n1(_TYPE, tail=f"\n\nSecond Test\n    {_ATTR}\n    {_USE}\n")
        assert rewrite_typed_value_reads(source) == source

    @pytest.mark.parametrize("definition", ["Get Attribute", "Get Property", "Fill Text", "get_attribute"])
    def test_a_file_defining_the_keyword_itself_is_left_alone(self, definition):
        source = _n1(_TYPE, _ATTR, _USE,
                     tail=f"\n\n*** Keywords ***\n{definition}\n    [Arguments]    @{{a}}\n    Log    own\n")
        assert rewrite_typed_value_reads(source) == source

    def test_try_in_another_test_of_the_file(self):
        source = _n1(_TYPE, _ATTR, _USE, tail=_OTHER_TEST_WITH_TRY)
        assert rewrite_typed_value_reads(source) == source

    def test_control_the_same_file_without_try_is_rewritten(self):
        source = _n1(_TYPE, _ATTR, _USE, tail="\n\nOther Test\n    Click    id=x\n")
        assert rewrite_typed_value_reads(source) == source.replace(_ATTR, _PROP)

    def test_control_an_unrelated_own_keyword_does_not_block(self):
        source = _n1(_TYPE, _ATTR, _USE, tail="\n\n*** Keywords ***\nMy Helper\n    Log    own\n")
        assert rewrite_typed_value_reads(source) == source.replace(_ATTR, _PROP)

    def test_an_assignment_only_line_leaves_the_test_alone(self):
        # As for q08: Robot joins the two lines into one statement that re-points the read's locator (Rule 1).
        source = _n1(_TYPE, "Click    id=go", "${customer_city_locator}=", "...    Set Variable    id=other",
                     _ATTR, _USE)
        assert rewrite_typed_value_reads(source) == source

    def test_control_the_same_test_without_the_split_assignment_is_rewritten(self):
        source = _n1(_TYPE, "Click    id=go", "Log    x", _ATTR, _USE)
        assert rewrite_typed_value_reads(source) == source.replace(_ATTR, _PROP)

    @pytest.mark.parametrize("label, source", _N1_SETTER_ANYWHERE, ids=_N1_SETTER_IDS)
    def test_a_scoped_setter_or_var_anywhere_leaves_the_file_alone(self, label, source):
        assert rewrite_typed_value_reads(source) == source, label

    @pytest.mark.parametrize("label, source", _N1_SETTER_ANYWHERE, ids=_N1_SETTER_IDS)
    def test_control_the_same_file_with_a_neutral_step_is_rewritten(self, label, source):
        control = source.replace(_N1_SETTER, "Log    x")
        assert control != source, label
        assert rewrite_typed_value_reads(control) == control.replace(_ATTR, _PROP), label

    @pytest.mark.parametrize("label, source, blocking, neutral", _N1_CANNOT_TRUST, ids=_N1_CANNOT_TRUST_IDS)
    def test_what_the_reader_cannot_trust_is_left_alone(self, label, source, blocking, neutral):
        assert rewrite_typed_value_reads(source) == source, label

    @pytest.mark.parametrize("label, source, blocking, neutral", _N1_CANNOT_TRUST, ids=_N1_CANNOT_TRUST_IDS)
    def test_control_the_same_file_without_the_blocking_text_is_rewritten(self, label, source, blocking, neutral):
        control = source.replace(blocking, neutral)
        assert control != source, label
        assert rewrite_typed_value_reads(control) == control.replace(_ATTR, _PROP), label

    @pytest.mark.parametrize("label, source, blocking, neutral", _N1_SECRET, ids=_N1_SECRET_IDS)
    def test_a_file_that_can_hold_a_secret_is_left_alone(self, label, source, blocking, neutral):
        assert rewrite_typed_value_reads(source) == source, label

    @pytest.mark.parametrize("label, source, blocking, neutral", _N1_SECRET, ids=_N1_SECRET_IDS)
    def test_control_the_same_file_without_the_secret_form_is_rewritten(self, label, source, blocking, neutral):
        control = source.replace(blocking, neutral)
        assert control != source, label
        assert rewrite_typed_value_reads(control) == control.replace(_ATTR, _PROP), label

    def test_empty_and_none(self):
        assert rewrite_typed_value_reads("") == ""
        assert rewrite_typed_value_reads(None) is None


# ---------------------------------------------------------------------------
# Branches of the shared reader no other test reached (PR #120 review, Codecov)
# ---------------------------------------------------------------------------

# A *** Variables *** value the reader cannot know, or a [Setting] / Settings line before the select read that
# mentions its variable: (label, the rewrite, the file, the blocking text, its neutral replacement, the read, the read
# rewritten). The control is the same file with the blocking text replaced, and IS rewritten.
_N1_UNTOUCHED = _n1(_TYPE, _ATTR, _USE)
_DENY_BRANCHES = [
    ("a ... line continues the locator's value", rewrite_typed_value_reads,
     _N1_UNTOUCHED.replace(_N1_ENTRY, _N1_ENTRY + "...    id=other\n"), "...    id=other\n", "", _ATTR, _PROP),
    ("the locator's value in two cells", rewrite_typed_value_reads,
     _N1_UNTOUCHED.replace(_N1_ENTRY, _N1_ENTRY.replace("\n", "    id=other\n")), "    id=other\n", "\n",
     _ATTR, _PROP),
    ("the locator's value holds a variable", rewrite_typed_value_reads,
     _N1_UNTOUCHED.replace(_N1_ENTRY, "${base}    id=customer.address\n${customer_city_locator}    ${base}.city\n"),
     "${base}.city", "id=customer.address.city", _ATTR, _PROP),
    ("the locator's value holds a backslash", rewrite_typed_value_reads,
     _N1_UNTOUCHED.replace(_N1_ENTRY, _N1_ENTRY.replace("customer.address", "customer\\.address")),
     "customer\\.address", "customer.address", _ATTR, _PROP),
    ("the test's [Documentation] before the read names its variable", rewrite_select_text_reads,
     _suite(_SELECT, _READ, _CHECK).replace("Auto-generated test case", "Checks ${selected_option}"),
     "Checks ${selected_option}", "Checks the selection", _READ, _READ_NEW),
    ("a Settings line names the read's variable", rewrite_select_text_reads,
     _with_setting("Metadata    Read    ${selected_option}"), "Read    ${selected_option}", "Read    the selection",
     _READ, _READ_NEW),
]
_DENY_BRANCH_IDS = [case[0] for case in _DENY_BRANCHES]


class TestReaderBranches:
    @pytest.mark.parametrize("label, rewrite, source, blocking, neutral, read, rewritten", _DENY_BRANCHES,
                             ids=_DENY_BRANCH_IDS)
    def test_a_value_or_mention_the_reader_cannot_follow_is_left_alone(
            self, label, rewrite, source, blocking, neutral, read, rewritten):
        assert read in source, label
        assert rewrite(source) == source, label

    @pytest.mark.parametrize("label, rewrite, source, blocking, neutral, read, rewritten", _DENY_BRANCHES,
                             ids=_DENY_BRANCH_IDS)
    def test_control_the_same_file_without_the_blocking_text_is_rewritten(
            self, label, rewrite, source, blocking, neutral, read, rewritten):
        control = source.replace(blocking, neutral)
        assert control != source, label
        assert rewrite(control) == control.replace(read, rewritten), label

    @pytest.mark.parametrize("label, source", [
        ("a Variables line that is not a variable entry",
         _N1_UNTOUCHED.replace(_N1_ENTRY, _N1_ENTRY + "timeout    30s\n")),
        ("a Variables entry with no value", _N1_UNTOUCHED.replace(_N1_ENTRY, _N1_ENTRY + "${empty_value}\n")),
        ("a Test Cases line before any test name",
         _N1_UNTOUCHED.replace("*** Test Cases ***\n", "*** Test Cases ***\n    Log    before any test\n")),
    ])
    def test_a_line_the_reader_passes_over_still_rewrites(self, label, source):
        assert source != _N1_UNTOUCHED, label
        assert rewrite_typed_value_reads(source) == source.replace(_ATTR, _PROP), label

    def test_an_empty_variables_line_keeps_the_last_name(self):
        # Unreachable through the rewrites: _parse_suite skips blank and comment lines, and indexes the same cells
        # (`content[0]`) before it calls _read_variable, so only a direct call reaches this line.
        variables = {"customercitylocator": "id=customer.address.city"}
        assert _read_variable("", variables, "customercitylocator") == "customercitylocator"
        assert _read_variable("    # a comment", variables, "customercitylocator") == "customercitylocator"
        assert variables == {"customercitylocator": "id=customer.address.city"}
