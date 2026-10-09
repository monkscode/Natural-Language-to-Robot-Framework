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

import pytest

from src.backend.crew_ai.robot_code_normalizer import rewrite_text_checks_whole_word

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
