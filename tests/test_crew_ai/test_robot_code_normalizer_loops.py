"""Tests for rewrite_empty_loop_checks in src.backend.crew_ai.robot_code_normalizer.

A generated test does `@{elements}=    Get Elements    <locator>`, then `FOR ${element} IN @{elements}` with a
positive `Should Contain` inside. `Get Elements` waits up to the Browser timeout for the first match and then
returns `[]` without failing, so when nothing matches the loop runs zero times and the test PASSES having checked
nothing. The rewrite inserts one `Should Not Be Empty` line after the `Get Elements` line, for the one exact shape
it can prove (read the element's text, optionally skip a blank row, then only positive `Should Contain` checks).

Every case is written in the assembler's REAL file shape (Settings + Variables + Test Cases; the base is the stored
q06 test of bench run a909c8eb). Each "left alone" case has a control: the base file IS rewritten, and each case
differs from it in one line (or carries its own tail).
"""

import logging

import pytest

from src.backend.core.pass_quality import check_pass_quality
from src.backend.crew_ai.robot_code_normalizer import rewrite_empty_loop_checks

LOGGER = "src.backend.crew_ai.robot_code_normalizer"

Q06_QUERY = (
    'Go to https://demoqa.com/webtables, type "Cierra" in the search box, '
    "and verify the table rows only have data of Cierra"
)

_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "Library    Collections\n"
    "\n"
    "*** Variables ***\n"
    "${browser}    chromium\n"
    "${headless}    True\n"
    "${url}    https://demoqa.com/webtables\n"
    "${search_box_locator}    id=searchBox\n"
    "${table_rows_locator}    tbody tr\n"
    "\n"
    "*** Test Cases ***\n"
    "Verify Web Table Search\n"
    "    [Documentation]    Navigate to DemoQA webtables, search for Cierra, and verify rows\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    ${url}\n"
)

_MSG = "msg=No matching element was found, so nothing was checked"
_INSERTED = f"    Should Not Be Empty    ${{elements}}    {_MSG}"

_PRE = "    Fill Text    ${search_box_locator}    Cierra"
_GE = "    @{elements}=    Get Elements    ${table_rows_locator}"
_FOR = "    FOR    ${element}    IN    @{elements}"
_READ = "        ${text}=    Get Text    ${element}"
_SKIP = "        Exit For Loop If    len($text.strip()) == 0"
_CHECK = "        Should Contain    ${text}    Cierra"
_END = "    END"
_CLOSE = "    Close Browser"

_LOOP = [_GE, _FOR, _READ, _SKIP, _CHECK, _END]


def _code(*lines: str, tail: str = "") -> str:
    """The real file shape: the header, then `lines`, with no trailing newline (as the assembler writes it)."""
    return _HEAD + "\n".join(lines) + tail


_Q06 = _code(_PRE, *_LOOP, _CLOSE)


def _guarded(code: str, ge: str = _GE, inserted: str = _INSERTED) -> str:
    return code.replace(ge, ge + "\n" + inserted)


def _swap(old: str, new: str) -> list[str]:
    """The base loop lines with one line replaced."""
    return [new if line == old else line for line in _LOOP]


# (id, the file, the Get Elements line as written, the inserted line)
_ACTS_ON = [
    ("continue-skip", _code(_PRE, *_swap(_SKIP, "        Continue For Loop If    len($text.strip()) == 0"), _CLOSE),
     _GE, _INSERTED),
    ("no-skip", _code(_PRE, _GE, _FOR, _READ, _CHECK, _END, _CLOSE), _GE, _INSERTED),
    ("ignore-case", _code(_PRE, *_swap(_CHECK, _CHECK + "    ignore_case=True"), _CLOSE), _GE, _INSERTED),
    ("two-checks", _code(_PRE, _GE, _FOR, _READ, _SKIP, _CHECK, "        Should Contain    ${text}    Smith", _END, _CLOSE),
     _GE, _INSERTED),
    ("scalar-assignment", _code(_PRE, *_swap(_GE, "    ${elements}=    Get Elements    ${table_rows_locator}"), _CLOSE),
     "    ${elements}=    Get Elements    ${table_rows_locator}", _INSERTED),
    ("browser-prefix", _code(_PRE, *_swap(_GE, "    @{elements}=    Browser.Get Elements    ${table_rows_locator}"), _CLOSE),
     "    @{elements}=    Browser.Get Elements    ${table_rows_locator}", _INSERTED),
    ("comments", _code(_PRE, _GE, "    # now the rows", _FOR, "        # read one", _READ, _SKIP, _CHECK, _END, _CLOSE),
     _GE, _INSERTED),
    ("tab-separated",
     _code(_PRE, "\t@{elements}=\tGet Elements\t${table_rows_locator}", "\tFOR\t${element}\tIN\t@{elements}",
           "\t\t${text}=\tGet Text\t${element}", "\t\tShould Contain\t${text}\tCierra", "\tEND", _CLOSE),
     "\t@{elements}=\tGet Elements\t${table_rows_locator}",
     f"\tShould Not Be Empty    ${{elements}}    {_MSG}"),
]

_TWO_TESTS = _code(_PRE, *_LOOP, _CLOSE, "Second Test", _PRE, *_LOOP, _CLOSE)


def _verdict(code: str, query: str | None) -> list[tuple[str, str]]:
    return [(finding.shape, finding.detail) for finding in check_pass_quality(code, query)]


def test_q06_loop_gets_the_guard():
    out = rewrite_empty_loop_checks(_Q06)
    assert out == _Q06.replace(_GE, _GE + "\n" + _INSERTED)


def test_crlf_file_keeps_its_line_ends():
    code = _Q06.replace("\n", "\r\n")
    out = rewrite_empty_loop_checks(code)
    assert out != code
    assert out.replace(_INSERTED + "\r\n", "") == code


@pytest.mark.parametrize("code, ge, inserted", [case[1:] for case in _ACTS_ON], ids=[case[0] for case in _ACTS_ON])
def test_acts_on(code, ge, inserted):
    out = rewrite_empty_loop_checks(code)
    assert out != code
    assert out == code.replace(ge, ge + "\n" + inserted)


def test_two_tests_in_one_file_are_both_guarded():
    out = rewrite_empty_loop_checks(_TWO_TESTS)
    assert out.count(_INSERTED) == 2
    assert out == _guarded(_TWO_TESTS)


@pytest.mark.parametrize("code", [_Q06, _TWO_TESTS] + [case[1] for case in _ACTS_ON])
def test_idempotent(code):
    out = rewrite_empty_loop_checks(code)
    assert out != code
    assert rewrite_empty_loop_checks(out) == out


_LEFT_ALONE = {
    "should-not-contain": _code(_PRE, *_swap(_CHECK, "        Should Not Contain    ${text}    Cierra"), _CLOSE),
    "q10-count-and-collect": _code(
        _GE, "    ${n}=    Get Length    ${elements}", "    Should Be True    ${n} == 20",
        _FOR, _READ, "        Append To List    ${texts}    ${text}", _END, _CLOSE,
    ),
    "step-between-get-elements-and-for": _code(_PRE, _GE, "    Log    rows found", _FOR, _READ, _SKIP, _CHECK, _END, _CLOSE),
    "list-used-after-end": _code(_PRE, *_LOOP, "    Log    ${elements}", _CLOSE),
    "second-loop-over-the-list": _code(_PRE, *_LOOP, _FOR, _READ, _CHECK, _END, _CLOSE),
    "extra-log-in-body": _code(_PRE, _GE, _FOR, _READ, "        Log    ${text}", _CHECK, _END, _CLOSE),
    "if-in-body": _code(
        _PRE, _GE, _FOR, _READ, "        IF    '${text}' != ''", "            Should Contain    ${text}    Cierra",
        "        END", _END, _CLOSE,
    ),
    "in-range": _code(_PRE, _GE, "    FOR    ${element}    IN RANGE    @{elements}", _READ, _CHECK, _END, _CLOSE),
    "in-enumerate": _code(
        _PRE, _GE, "    FOR    ${i}    ${element}    IN ENUMERATE    @{elements}", _READ, _CHECK, _END, _CLOSE,
    ),
    "get-text-with-assertion": _code(
        _PRE, *_swap(_READ, "        ${text}=    Get Text    ${element}    contains    Cierra"), _CLOSE,
    ),
    "get-text-sub-locator": _code(_PRE, *_swap(_READ, "        ${text}=    Get Text    ${element} >> td"), _CLOSE),
    "read-variable-differs-from-checked": _code(
        _PRE, _GE, _FOR, "        ${row_text}=    Get Text    ${element}", _CHECK, _END, _CLOSE,
    ),
    "skip-on-empty-string": _code(_PRE, *_swap(_SKIP, "        Exit For Loop If    '${text}' == ''"), _CLOSE),
    "skip-with-or": _code(
        _PRE, *_swap(_SKIP, '        Exit For Loop If    len($text.strip()) == 0 or $text.strip() == " "'), _CLOSE,
    ),
    "lower-case-for": _code(_PRE, _GE, "    for    ${element}    IN    @{elements}", _READ, _SKIP, _CHECK, _END, _CLOSE),
    "mixed-case-in": _code(_PRE, _GE, "    FOR    ${element}    In    @{elements}", _READ, _SKIP, _CHECK, _END, _CLOSE),
    "lower-case-end": _code(_PRE, _GE, _FOR, _READ, _SKIP, _CHECK, "    end", _CLOSE),
    "no-end": _code(_PRE, _GE, _FOR, _READ, _SKIP, _CHECK),
    "continued-get-elements": _code(_PRE, _GE, "    ...    timeout=5s", _FOR, _READ, _SKIP, _CHECK, _END, _CLOSE),
    "loop-in-a-user-keyword": _code(
        _PRE, "    Check Rows", _CLOSE,
        tail="\n\n*** Keywords ***\nCheck Rows\n" + "\n".join(_LOOP),
    ),
    "own-should-not-be-empty": _code(
        _PRE, *_LOOP, _CLOSE, tail="\n\n*** Keywords ***\nShould Not Be Empty\n    Log    mine",
    ),
    "own-shouldnotbeempty": _code(
        _PRE, *_LOOP, _CLOSE, tail="\n\n*** Keywords ***\nShouldNotBeEmpty\n    Log    mine",
    ),
    "try-in-the-file": _code(
        _PRE, *_LOOP, "    TRY", "        Log    x", "    EXCEPT", "        Log    y", _END, _CLOSE,
    ),
    "language-setting": "language: en\n\n" + _Q06,
    # AMENDMENT 1: a line Robot runs that the shared reader does not turn into a step.
    "typed-assignment-in-body": _code(
        _PRE, _GE, _FOR, _READ, _SKIP, "        ${text: str}=    Set Variable    Cierra", _CHECK, _END, _CLOSE,
    ),
    "item-assignment-in-body": _code(
        _PRE, _GE, _FOR, _READ, _SKIP, "        ${text}[0]=    Set Variable    x", _CHECK, _END, _CLOSE,
    ),
    "one-space-line-between-get-elements-and-for": _code(
        _PRE, _GE, " Log    rows", _FOR, _READ, _SKIP, _CHECK, _END, _CLOSE,
    ),
    "unindented-continuation-after-get-elements": _code(
        _PRE, _GE, "...    timeout=5s", _FOR, _READ, _SKIP, _CHECK, _END, _CLOSE,
    ),
    "template-test": _code("    [Template]    Log", _PRE, *_LOOP, _CLOSE),
}


def test_the_base_is_rewritten_so_each_left_alone_case_has_a_control():
    assert rewrite_empty_loop_checks(_Q06) != _Q06


@pytest.mark.parametrize("code", list(_LEFT_ALONE.values()), ids=list(_LEFT_ALONE))
def test_left_alone(code):
    assert rewrite_empty_loop_checks(code) == code


@pytest.mark.parametrize("code", [_Q06, _TWO_TESTS] + [case[1] for case in _ACTS_ON])
def test_checker_verdict_unchanged(code):
    out = rewrite_empty_loop_checks(code)
    assert out != code
    for query in (Q06_QUERY, None):
        assert _verdict(code, query) == _verdict(out, query)


def test_logs_one_signal_line(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        rewrite_empty_loop_checks(_Q06)
    records = [r for r in caplog.records if "signal: empty-loop-guard" in r.getMessage()]
    assert len(records) == 1
    assert "rewrote 1 loop(s)" in records[0].getMessage()


def test_empty_input_comes_back_unchanged():
    assert rewrite_empty_loop_checks("") == ""
