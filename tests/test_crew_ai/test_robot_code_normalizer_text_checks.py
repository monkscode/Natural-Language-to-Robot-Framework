"""Tests for rewrite_text_checks_whole_word in src.backend.crew_ai.robot_code_normalizer.

`Should Contain    ${v}    Active    ignore_case=True` is a substring test: it passes "Active" on
"Inactive". Without the flag it fails CSS-uppercased text ("CONTAINED"). The rewrite turns a text
check on a `Get Text` read into `Should Match Regexp` with a whole-word, any-letter-case pattern
that holds no backslash.

Every positive case is written in the assembler's REAL file shape (Settings + Variables + Test
Cases), as in test_robot_code_normalizer_reads.py. Each "left alone" case has a control: the base
file (`_suite(_READ, _CHECK)`) IS rewritten, and the guard cases carry their own.
"""

import logging
import re
import warnings
from unittest.mock import patch

import pytest

import src.backend.core.pass_quality as pass_quality
import src.backend.crew_ai.robot_code_normalizer as normalizer
from src.backend.core.pass_quality import SELECT_CHECK_CANNOT_FAIL, check_pass_quality, hollow_shapes
from src.backend.crew_ai.robot_code_normalizer import (
    rewrite_select_text_reads,
    rewrite_text_checks_whole_word,
    rewrite_typed_value_reads,
)

LOGGER = "src.backend.crew_ai.robot_code_normalizer"

# ---------------------------------------------------------------------------
# The assembler's file shape
# ---------------------------------------------------------------------------

_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "\n"
    "*** Variables ***\n"
    "${browser}    chromium\n"
    "${headless}    True\n"
    "${url}    https://example.com/account\n"
    "${status_locator}    css=#status\n"
    "\n"
    "*** Test Cases ***\n"
    "Generated Test\n"
    "    [Documentation]    Auto-generated test case\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    ${url}\n"
)


def _suite(*body: str, tail: str = "") -> str:
    """A generated file: the real header, then `body` lines, then Close Browser."""
    return _HEAD + "".join(f"    {line}\n" for line in body) + "    Close Browser" + tail


_READ = "${status}=    Get Text    ${status_locator}"
_OTHER_READ = "${title}=    Get Text    css=h1"
_CHECK = "Should Contain    ${status}    Active"
_FLAG = "    ignore_case=True"


def _new(literal: str, pattern: str, variable: str = "${status}", keyword: str = "Should Match Regexp") -> str:
    """The rewritten check line (without its indentation)."""
    return (f"{keyword}    {variable}    {pattern}    msg='{variable}' does not contain '{literal}' "
            "(whole words, any letter case)    values=False")


_ACTIVE = "(?i)(?<![0-9a-z_])Active(?![0-9a-z_])"
_NEW = _new("Active", _ACTIVE)

# The brief's five pattern examples, exact.
_PATTERNS = [
    ("Active", "(?i)(?<![0-9a-z_])Active(?![0-9a-z_])"),
    ("$29.99", "(?i)[$]29[.]99(?![0-9a-z_])"),
    ("Hello World!", "(?i)(?<![0-9a-z_])Hello World!"),
    ("Products (6)", "(?i)(?<![0-9a-z_])Products [(]6[)]"),
    ("a|b?", "(?i)(?<![0-9a-z_])a[|]b[?]"),
]


def _pattern_of(literal: str) -> str:
    """The pattern cell the rewrite writes for `literal`, read from its output."""
    out = rewrite_text_checks_whole_word(_suite(_READ, f"Should Contain    ${{status}}    {literal}"))
    line = next(ln for ln in out.split("\n") if "Should Match Regexp" in ln)
    return line.strip().split("    ")[2]


# ---------------------------------------------------------------------------
# Rewritten
# ---------------------------------------------------------------------------

class TestTextCheckRewritten:
    def test_two_cells(self):
        assert rewrite_text_checks_whole_word(_suite(_READ, _CHECK)) == _suite(_READ, _NEW)

    def test_three_cells_with_the_flag_drop_the_flag(self):
        assert rewrite_text_checks_whole_word(_suite(_READ, _CHECK + _FLAG)) == _suite(_READ, _NEW)

    def test_the_full_line_example_exactly(self):
        source = _suite(_READ, "Should Contain    ${status}    Active    ignore_case=True")
        out = rewrite_text_checks_whole_word(source)
        assert ("    Should Match Regexp    ${status}    (?i)(?<![0-9a-z_])Active(?![0-9a-z_])    "
                "msg='${status}' does not contain 'Active' (whole words, any letter case)    values=False\n") in out

    def test_the_builtin_prefix_is_kept(self):
        source = _suite(_READ, "BuiltIn.Should Contain    ${status}    Active" + _FLAG)
        expected = _suite(_READ, _new("Active", _ACTIVE, keyword="BuiltIn.Should Match Regexp"))
        assert rewrite_text_checks_whole_word(source) == expected

    def test_a_browser_prefixed_read_is_followed(self):
        read = "${status}=    Browser.Get Text    ${status_locator}"
        assert rewrite_text_checks_whole_word(_suite(read, _CHECK)) == _suite(read, _NEW)

    def test_a_crlf_file_keeps_its_line_ends(self):
        source = _suite(_READ, _CHECK + _FLAG).replace("\n", "\r\n")
        out = rewrite_text_checks_whole_word(source)
        assert out == _suite(_READ, _NEW).replace("\n", "\r\n")
        assert out.count("\r\n") == out.count("\n")

    def test_two_checks_of_one_variable(self):
        second = "Should Contain    ${status}    Verified" + _FLAG
        expected = _suite(_READ, _NEW, _new("Verified", "(?i)(?<![0-9a-z_])Verified(?![0-9a-z_])"))
        assert rewrite_text_checks_whole_word(_suite(_READ, _CHECK, second)) == expected

    @pytest.mark.parametrize(("literal", "pattern"), _PATTERNS, ids=[lit for lit, _ in _PATTERNS])
    def test_each_pattern_example(self, literal, pattern):
        source = _suite(_READ, f"Should Contain    ${{status}}    {literal}" + _FLAG)
        assert rewrite_text_checks_whole_word(source) == _suite(_READ, _new(literal, pattern))

    def test_a_trailing_comment_stays_after_the_new_cells(self):
        source = _suite(_READ, _CHECK + _FLAG + "    # the account status")
        assert rewrite_text_checks_whole_word(source) == _suite(_READ, _NEW + "    # the account status")

    def test_no_backslash_is_written(self):
        source = _suite(_READ, *(f"Should Contain    ${{status}}    {lit}" for lit, _ in _PATTERNS))
        out = rewrite_text_checks_whole_word(source)
        assert out.count("Should Match Regexp") == len(_PATTERNS)
        assert "\\" not in out


# ---------------------------------------------------------------------------
# What the written pattern does (the cell IS the regex: it holds no backslash,
# so Robot passes it to `re` unchanged)
# ---------------------------------------------------------------------------

_SEMANTICS = [
    ("Inactive", "Active", False),
    ("ACTIVE", "Active", True),
    ("CONTAINED", "Contained", True),
    ("10 items", "1", False),
    ("Products", "Products", True),
    ("Out of stock - Unavailable", "Available", False),
    ("Welcome, John!", "Welcome", True),
    ("Products", "Prod", False),
    ("$129.99", "$29.99", False),
    ("Total: $29.99", "$29.99", True),
]


class TestThePatternMatchesWholeWordsInAnyCase:
    @pytest.mark.parametrize(("text", "literal", "matches"), _SEMANTICS,
                             ids=[f"{lit} in {text}" for text, lit, _ in _SEMANTICS])
    def test_search(self, text, literal, matches):
        assert (re.search(_pattern_of(literal), text) is not None) is matches

    def test_every_pattern_compiles_without_a_warning(self):
        patterns = [p for _, p in _PATTERNS] + [_pattern_of(lit) for _, lit, _ in _SEMANTICS]
        re.purge()  # a cached pattern would not warn again
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for pattern in patterns:
                re.compile(pattern)


# ---------------------------------------------------------------------------
# Left alone
# ---------------------------------------------------------------------------

class TestTextCheckLeftAlone:
    @pytest.mark.parametrize("label, body", [
        ("a variable as the literal", (_READ, "Should Contain    ${status}    ${expected}")),
        ("a literal holding {", (_READ, "Should Contain    ${status}    a{b")),
        ("a literal holding }", (_READ, "Should Contain    ${status}    a}b")),
        ("a literal holding a backslash", (_READ, "Should Contain    ${status}    a\\b")),
        ("a literal holding ^", (_READ, "Should Contain    ${status}    ^Active")),
        ("a literal holding [", (_READ, "Should Contain    ${status}    [Active")),
        ("a literal holding ]", (_READ, "Should Contain    ${status}    Active]")),
        ("a named msg cell as the second argument", (_READ, "Should Contain    ${status}    msg=x")),
        ("a named item cell as the second argument", (_READ, "Should Contain    ${status}    item=Active")),
        ("a fourth cell", (_READ, _CHECK + _FLAG + "    strip_spaces=True")),
        ("ignore_case=False", (_READ, _CHECK + "    ignore_case=False")),
        ("another third cell", (_READ, _CHECK + "    collapse_spaces=True")),
        # A Get Text of another element first, so the file passes the quick "no Get Text at all" exit.
        ("Get Texts", (_OTHER_READ, "${status}=    Get Texts    ${status_locator}", _CHECK)),
        ("Get Selected Options", (_OTHER_READ, "${status}=    Get Selected Options    ${status_locator}", _CHECK)),
        ("Get Elements", (_OTHER_READ, "${status}=    Get Elements    ${status_locator}", _CHECK)),
        ("Get Attribute", (_OTHER_READ, "${status}=    Get Attribute    ${status_locator}    title", _CHECK)),
        ("Get Text with an inline assertion", ("${status}=    Get Text    ${status_locator}    contains    Active", _CHECK)),
        ("a Get Text of a select the test set", (
            "Select Options By    ${status_locator}    label    Active", _READ, _CHECK)),
        ("a select of the same element in another spelling", (
            "Select Options By    id=status    label    Active", _READ, _CHECK)),
        ("the variable re-assigned between", (_READ, "${status}=    Set Variable    Active", _CHECK)),
        ("a check in a FOR loop", (_READ, "FOR    ${i}    IN RANGE    2", "    " + _CHECK, "END")),
        ("a variable never assigned by Get Text", (_READ, "Should Contain    ${url}    example")),
        ("a two-variable assignment", ("${status}    ${other}=    Get Text    ${status_locator}", _CHECK)),
        ("an assignment on the check", (_READ, "${r}=    " + _CHECK)),
        ("a continued check", (_READ, _CHECK, "...    ignore_case=True")),
        ("a continued read", (_READ, "...    contains    Active", _CHECK)),
        ("another library's Should Contain", (_READ, "Other.Should Contain    ${status}    Active")),
        ("a check in a [Teardown]", (_READ, "[Teardown]    " + _CHECK)),
        ("a list item as the container", (_READ, "Should Contain    ${status}[0]    Active")),
    ])
    def test_left_alone(self, label, body):
        source = _suite(*body)
        assert rewrite_text_checks_whole_word(source) == source, label

    def test_a_select_of_another_element_does_not_block(self):
        # The control for the two select cases above: a select of a different element leaves the read a text read.
        body = ("Select Options By    id=country    label    India", _READ, _CHECK)
        expected = _suite(*body[:2], _NEW)
        assert rewrite_text_checks_whole_word(_suite(*body)) == expected

    @pytest.mark.parametrize("definition", ["Should Contain", "should_contain", "Get Text", "Should Match Regexp"])
    def test_a_file_defining_the_keyword_itself_is_left_alone(self, definition):
        tail = f"\n\n*** Keywords ***\n{definition}\n    [Arguments]    ${{a}}    ${{b}}\n    Log    own\n"
        source = _suite(_READ, _CHECK, tail=tail)
        assert rewrite_text_checks_whole_word(source) == source
        # control: a keyword of another name is no reason to leave the file alone
        control = _suite(_READ, _CHECK, tail="\n\n*** Keywords ***\nMy Own Check\n    Log    own\n")
        assert rewrite_text_checks_whole_word(control) == control.replace("    " + _CHECK, "    " + _NEW)

    def test_a_file_with_try_is_left_alone(self):
        other = "\n\nOther Test\n    TRY\n        Click    id=x\n    EXCEPT\n        Log    no\n    END\n"
        source = _suite(_READ, _CHECK, tail=other)
        assert rewrite_text_checks_whole_word(source) == source
        # control: the same second test without TRY
        control = _suite(_READ, _CHECK, tail="\n\nOther Test\n    Click    id=x\n")
        assert rewrite_text_checks_whole_word(control) == control.replace("    " + _CHECK, "    " + _NEW)

    def test_a_language_setting_leaves_the_file_alone(self):
        source = _suite(_READ, _CHECK).replace("*** Settings ***\n", "language: fi\n\n*** Settings ***\n")
        assert rewrite_text_checks_whole_word(source) == source

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_input_is_returned_as_is(self, empty):
        assert rewrite_text_checks_whole_word(empty) is empty

    def test_a_file_without_get_text_returns_the_same_object(self):
        source = _suite("${status}=    Get Attribute    ${status_locator}    title", _CHECK)
        assert rewrite_text_checks_whole_word(source) is source


# ---------------------------------------------------------------------------
# Idempotent and logged
# ---------------------------------------------------------------------------

class TestTextCheckIdempotentAndLogged:
    def test_a_second_pass_is_byte_identical(self):
        source = _suite(_READ, _CHECK + _FLAG, "Should Contain    ${status}    $29.99",
                        "Should Contain    ${status}    ${expected}")
        once = rewrite_text_checks_whole_word(source)
        assert once != source  # control: the first pass rewrote something
        assert rewrite_text_checks_whole_word(once) == once

    def test_one_info_line_names_the_count(self, caplog):
        source = _suite(_READ, _CHECK, "Should Contain    ${status}    Verified")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            rewrite_text_checks_whole_word(source)
        messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
        assert messages == [
            "Text check normalizer: rewrote 2 Should Contain line(s) to Should Match Regexp "
            "(whole words, any letter case)"
        ]


# ---------------------------------------------------------------------------
# The pass checker keeps sight of a cannot-fail select check: one locator rule
# for "the same element", the selects of the whole file, and a rewrite that
# never changes what the checker reports
# ---------------------------------------------------------------------------

QUERY = ("Go to https://the-internet.herokuapp.com/dropdown, select Option 2 from the dropdown, "
         "and verify Option 2 is selected")

_FLAGGED_CHECK = "Should Contain    ${selected_option}    Option 2    ignore_case=True"
_PLAIN_CHECK = "Should Contain    ${selected_option}    Option 2"
_OTHER_CASE_CHECK = "Should Contain    ${selected_option}    option 2"
_OPTION_2_NEW = ("Should Match Regexp    ${selected_option}    (?i)(?<![0-9a-z_])Option 2(?![0-9a-z_])    "
                 "msg='${selected_option}' does not contain 'Option 2' (whole words, any letter case)    values=False")

# (the select's locator, the read's locator): one element in two spellings the pass checker reads as one id.
_SPELLED_DIFFERENTLY = [
    ("css=select#dropdown", "id=dropdown"),
    ("id=dropdown", "css=select#dropdown"),
    ('xpath=//select[@id="dropdown"]', "id=dropdown"),
    ('css=[id="dropdown"]', "id=dropdown"),
    ("xpath=//select[@id='dropdown']", "css=#dropdown"),
]
_CHECKS = [
    pytest.param(_FLAGGED_CHECK, id="with the flag"),
    pytest.param(_PLAIN_CHECK, id="without the flag"),
    pytest.param(_OTHER_CASE_CHECK, id="another letter case, no flag"),
]


def _dropdown(select: str, read: str, check: str) -> str:
    """The q08 file as the assembler writes it: a select of `select`, a Get Text of `read`, then `check`."""
    return (
        "*** Settings ***\n"
        "Library    Browser    timeout=30s\n"
        "\n"
        "*** Variables ***\n"
        f"${{dropdown_trigger}}    {select}\n"
        f"${{dropdown_verification}}    {read}\n"
        "\n"
        "*** Test Cases ***\n"
        "Generated Test\n"
        "    New Browser    chromium    headless=True\n"
        "    New Page    https://the-internet.herokuapp.com/dropdown\n"
        "    Select Options By    ${dropdown_trigger}    label    Option 2\n"
        "    ${selected_option}=    Get Text    ${dropdown_verification}\n"
        f"    {check}\n"
        "    Close Browser\n"
    )


def _two_tests(check: str, select: str = "id=dropdown", read: str = "id=dropdown") -> str:
    """The select sits in another test: test `Pick` selects on `select`, test `Check` reads `read`, then `check`."""
    return (
        "*** Settings ***\n"
        "Library    Browser    timeout=30s\n"
        "\n"
        "*** Test Cases ***\n"
        "Pick\n"
        "    New Browser    chromium    headless=True\n"
        "    New Page    https://the-internet.herokuapp.com/dropdown\n"
        f"    Select Options By    {select}    label    Option 2\n"
        "\n"
        "Check\n"
        f"    ${{selected_option}}=    Get Text    {read}\n"
        f"    {check}\n"
    )


_PICK_OPTION = (
    "*** Keywords ***\n"
    "Pick Option\n"
    "    Select Options By    id=dropdown    label    Option 2\n"
)


def _user_keyword(check: str, keywords_first: bool) -> str:
    """The select sits in the user keyword `Pick Option`. With *** Keywords *** first the pass checker reports the
    check; below the tests it does not (it reads the file top to bottom)."""
    tests = (
        "*** Test Cases ***\n"
        "Generated Test\n"
        "    New Browser    chromium    headless=True\n"
        "    New Page    https://the-internet.herokuapp.com/dropdown\n"
        "    Pick Option\n"
        "    ${selected_option}=    Get Text    id=dropdown\n"
        f"    {check}\n"
        "    Close Browser\n"
    )
    body = _PICK_OPTION + "\n" + tests if keywords_first else tests + "\n" + _PICK_OPTION
    return "*** Settings ***\nLibrary    Browser    timeout=30s\n\n" + body


def _masked(check: str) -> str:
    """Test `A` holds a cannot-fail check the pass checker reports (it gives one finding per shape); test `B` reads
    the same select, then `check`."""
    return (
        "*** Settings ***\n"
        "Library    Browser    timeout=30s\n"
        "\n"
        "*** Test Cases ***\n"
        "A\n"
        "    Select Options By    css=select#dropdown    label    Option 2\n"
        "    ${a}=    Get Text    id=dropdown\n"
        "    Should Contain    ${a}    Option 2    ignore_case=True\n"
        "\n"
        "B\n"
        "    ${selected_option}=    Get Text    id=dropdown\n"
        f"    {check}\n"
    )


# The select is outside the read's test: only a reading of the whole file finds it.
_SELECT_ELSEWHERE = [
    ("another test", _two_tests),
    ("another test, spelled css=select#dropdown", lambda check: _two_tests(check, select="css=select#dropdown")),
    ("a user keyword above the tests", lambda check: _user_keyword(check, keywords_first=True)),
    ("a user keyword below the tests", lambda check: _user_keyword(check, keywords_first=False)),
    ("another test, behind a check the checker reports", _masked),
]
_SELECT_ELSEWHERE_FILES = [
    pytest.param(build(check.values[0]), id=f"{where} / {check.id}")
    for where, build in _SELECT_ELSEWHERE for check in _CHECKS
]

_HEADING_READ = "${t}=    Get Text    id=heading"
_HEADING_CHECK = "Should Contain    ${t}    Products    ignore_case=True"
_PRODUCTS = "(?i)(?<![0-9a-z_])Products(?![0-9a-z_])"
_HEADING_NEW = _new("Products", _PRODUCTS, variable="${t}")
# No select step at all: the file only names the keyword where the reader does not read it as a step.
_SELECT_ONLY_MENTIONED = [
    pytest.param(_suite("# Select Options By is used elsewhere", _HEADING_READ, _HEADING_CHECK), id="a comment line"),
    pytest.param(_suite(_HEADING_READ, _HEADING_CHECK).replace("Auto-generated test case", "uses Select Options By"),
                 id="a documentation line"),
    pytest.param(_suite("Run Keyword If    ${x}    Select Options By    id=a    label    b", _HEADING_READ,
                        _HEADING_CHECK), id="a Run Keyword If argument"),
]

_ANOTHER_ID = _dropdown("id=country", "id=heading", _FLAGGED_CHECK)
_ANOTHER_ID_WIDER_SPELLING = _dropdown("css=select#country", "css=select#dropdown", _FLAGGED_CHECK)
_PRODUCTS_CHECK = "Should Contain    ${selected_option}    Products    ignore_case=True"
_ANOTHER_ID_IN_ANOTHER_TEST = _two_tests(_PRODUCTS_CHECK, select="id=country", read="id=heading")
_ANOTHER_ID_IN_ONE_TEST = _suite("Select Options By    id=country    label    India", _HEADING_READ, _HEADING_CHECK)

# The comparison with the pass checker is the rule that acts here: the checker reads `${loc}` as declared (the
# select's element), the rewrite follows the re-pointed value (another element).
_REPOINTED = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "\n"
    "*** Variables ***\n"
    "${loc}    id=dropdown\n"
    "\n"
    "*** Test Cases ***\n"
    "T\n"
    "    Select Options By    ${loc}    label    Option 2\n"
    "    ${loc}=    Set Variable    id=heading\n"
    "    ${selected_option}=    Get Text    ${loc}\n"
    f"    {_FLAGGED_CHECK}\n"
)

# Every file of this section.
_CHECKER_FILES = [
    *(pytest.param(_dropdown(select, read, check.values[0]), id=f"{select} / {read} / {check.id}")
      for select, read in _SPELLED_DIFFERENTLY for check in _CHECKS),
    pytest.param(_ANOTHER_ID, id="another id"),
    pytest.param(_ANOTHER_ID_WIDER_SPELLING, id="another id in the wider spelling"),
    *_SELECT_ELSEWHERE_FILES,
    *_SELECT_ONLY_MENTIONED,
    pytest.param(_ANOTHER_ID_IN_ANOTHER_TEST, id="another id in another test"),
    pytest.param(_ANOTHER_ID_IN_ONE_TEST, id="another id in the same test, the assembler's file"),
    pytest.param(_REPOINTED, id="a re-pointed locator variable"),
]
# The inputs of the REWRITTEN cases above.
_REWRITTEN_INPUTS = [
    pytest.param(_suite(_READ, _CHECK), id="two cells"),
    pytest.param(_suite(_READ, _CHECK + _FLAG), id="three cells with the flag"),
    pytest.param(_suite(_READ, "BuiltIn.Should Contain    ${status}    Active" + _FLAG), id="the BuiltIn prefix"),
    pytest.param(_suite("${status}=    Browser.Get Text    ${status_locator}", _CHECK), id="a Browser-prefixed read"),
    pytest.param(_suite(_READ, _CHECK + _FLAG).replace("\n", "\r\n"), id="a CRLF file"),
    pytest.param(_suite(_READ, _CHECK, "Should Contain    ${status}    Verified" + _FLAG), id="two checks"),
    *(pytest.param(_suite(_READ, f"Should Contain    ${{status}}    {literal}" + _FLAG), id=f"pattern {literal}")
      for literal, _ in _PATTERNS),
    pytest.param(_suite(_READ, _CHECK + _FLAG + "    # the account status"), id="a trailing comment"),
    pytest.param(_suite(_READ, *(f"Should Contain    ${{status}}    {lit}" for lit, _ in _PATTERNS)),
                 id="every pattern in one file"),
    pytest.param(_suite("Select Options By    id=country    label    India", _READ, _CHECK),
                 id="a select of another element"),
]


def _chain(code: str) -> str:
    """The three read rewrites in the cleanup step's order."""
    return rewrite_text_checks_whole_word(rewrite_typed_value_reads(rewrite_select_text_reads(code)))


def _report(code: str, query: str | None) -> list[tuple[str, str]]:
    """What the pass checker reports on `code`."""
    return [(finding.shape, finding.detail) for finding in check_pass_quality(code, query)]


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


class TestASelectReadInAnotherSpellingIsLeftAlone:
    @pytest.mark.parametrize(("select", "read"), _SPELLED_DIFFERENTLY)
    @pytest.mark.parametrize("check", _CHECKS)
    def test_left_alone_by_the_locator_rule(self, caplog, select, read, check):
        code = _dropdown(select, read, check)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = rewrite_text_checks_whole_word(code)
        assert out == code
        # No `rewrote` line, and no refusal either: the locator rule acts before the checker comparison runs.
        assert _messages(caplog) == []

    @pytest.mark.parametrize(("select", "read"), _SPELLED_DIFFERENTLY)
    @pytest.mark.parametrize("check", _CHECKS[:2])
    def test_the_checker_still_reports_the_check_after_the_chain(self, select, read, check):
        assert SELECT_CHECK_CANNOT_FAIL in hollow_shapes(_chain(_dropdown(select, read, check)), QUERY)

    def test_a_select_of_another_id_does_not_block(self):
        # The control: two different ids are two elements, so the read is a text read and its check is rewritten.
        assert rewrite_text_checks_whole_word(_ANOTHER_ID) == _ANOTHER_ID.replace(_FLAGGED_CHECK, _OPTION_2_NEW)

    def test_another_id_in_the_wider_spelling_does_not_block(self):
        assert rewrite_text_checks_whole_word(_ANOTHER_ID_WIDER_SPELLING) == (
            _ANOTHER_ID_WIDER_SPELLING.replace(_FLAGGED_CHECK, _OPTION_2_NEW))


class TestTheSelectsOfTheWholeFile:
    @pytest.mark.parametrize("code", _SELECT_ELSEWHERE_FILES)
    def test_a_read_of_a_select_set_elsewhere_is_left_alone(self, caplog, code):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = rewrite_text_checks_whole_word(code)
        assert out == code
        # No `rewrote` line, and no refusal either: the whole-file rule acts before the checker comparison runs.
        assert _messages(caplog) == []

    @pytest.mark.parametrize("code", _SELECT_ONLY_MENTIONED)
    def test_a_file_that_only_mentions_the_keyword_is_left_alone(self, caplog, code):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = rewrite_text_checks_whole_word(code)
        assert out == code
        assert _messages(caplog) == []
        # control: the same file naming another keyword there is rewritten
        control = code.replace("Select Options By", "Deselect Options")
        assert rewrite_text_checks_whole_word(control) == control.replace(_HEADING_CHECK, _HEADING_NEW)

    def test_a_select_of_another_id_in_another_test_does_not_block(self):
        # The control for the whole-file rule: two different ids are two elements, in two tests too.
        new = _new("Products", _PRODUCTS, variable="${selected_option}")
        assert rewrite_text_checks_whole_word(_ANOTHER_ID_IN_ANOTHER_TEST) == (
            _ANOTHER_ID_IN_ANOTHER_TEST.replace(_PRODUCTS_CHECK, new))

    def test_a_select_of_another_id_in_the_same_test_does_not_block(self):
        assert rewrite_text_checks_whole_word(_ANOTHER_ID_IN_ONE_TEST) == (
            _ANOTHER_ID_IN_ONE_TEST.replace(_HEADING_CHECK, _HEADING_NEW))


class TestTheRewriteNeverChangesWhatThePassCheckerReports:
    def test_a_select_read_only_the_checker_sees_leaves_the_file_alone(self, caplog):
        assert SELECT_CHECK_CANNOT_FAIL in hollow_shapes(_REPOINTED, QUERY)  # the precondition: the checker reports it
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = rewrite_text_checks_whole_word(_REPOINTED)
        assert out == _REPOINTED
        messages = _messages(caplog)
        refused = [m for m in messages if "signal: text-check-refused" in m]
        assert len(refused) == 1
        assert SELECT_CHECK_CANNOT_FAIL in refused[0] and "→" in refused[0]
        assert not any("rewrote" in m for m in messages)

    def test_one_info_line_says_what_would_have_changed(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            rewrite_text_checks_whole_word(_REPOINTED)
        assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
            "Text check normalizer: left 1 Should Contain line(s) unchanged — the rewrite would change what the "
            "pass checker reports (['SELECT_CHECK_CANNOT_FAIL'] → []) (signal: text-check-refused)"
        ]

    def test_with_the_checker_silent_the_same_file_is_rewritten(self):
        # The refusal above comes from the comparison, not from an earlier rule.
        with patch.object(normalizer, "check_pass_quality", return_value=[]):
            out = rewrite_text_checks_whole_word(_REPOINTED)
        assert out == _REPOINTED.replace(_FLAGGED_CHECK, _OPTION_2_NEW)

    @pytest.mark.parametrize("query", [None, QUERY], ids=["no query", "the q08 query"])
    @pytest.mark.parametrize("source", [*_REWRITTEN_INPUTS, *_CHECKER_FILES])
    def test_the_checker_reports_the_same_before_and_after(self, source, query):
        assert _report(rewrite_text_checks_whole_word(source), query) == _report(source, query)

    @pytest.mark.parametrize("source", _CHECKER_FILES)
    def test_a_second_pass_is_byte_identical(self, source):
        once = rewrite_text_checks_whole_word(source)
        assert rewrite_text_checks_whole_word(once) == once

    def test_the_checker_expands_no_variable_beyond_its_limit(self):
        # The rewrite asks the checker while a test is generated, before `robot --dryrun` can reject the file. A
        # variable naming itself k times grew to k**5 copies there. Never more than 16 references in a test.
        code = (
            "*** Settings ***\nLibrary    Browser\n\n*** Variables ***\n"
            "${loc}    " + " ".join(["${loc}"] * 12) + "\n\n"
            "*** Test Cases ***\nT\n"
            "    New Page    https://example.com\n"
            "    ${v}=    Get Text    ${loc}\n"
            "    Should Contain    ${v}    Example Domain    ignore_case=True\n"
        )
        resolve = pass_quality._resolve
        returned = []

        def recording(text, variables):
            returned.append(resolve(text, variables))
            return returned[-1]

        with patch.object(pass_quality, "_resolve", wraps=recording):
            out = rewrite_text_checks_whole_word(code)
        assert "Should Match Regexp" in out  # no select: the checker was asked and reports nothing either way
        assert returned and max(len(value) for value in returned) <= pass_quality._RESOLVE_LIMIT
