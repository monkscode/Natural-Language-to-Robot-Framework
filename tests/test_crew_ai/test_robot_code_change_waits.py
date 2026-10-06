"""Tests for insert_change_waits / remove_change_waits in
src.backend.crew_ai.robot_code_normalizer (F1, Task 4).

browser-service marks a READ whose text changed because of an earlier action.
insert_change_waits puts two never-failing lines around it — a read above the
first action line and a wait above the read — and remove_change_waits is its
exact inverse (learning must see the test without the pair).

Every positive case is written in the assembler's REAL file shape (Settings +
Variables + Test Cases) and asserts the exact lines and where they sit. Every
"left alone" case has a control — the same file with the one blocking detail
changed gets the pair — so "unchanged" cannot pass vacuously (memory
project_get_element_states_no_wait).
"""

import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backend.crew_ai.robot_code_normalizer import (
    insert_change_waits,
    remove_change_waits,
)

LOGGER = "src.backend.crew_ai.robot_code_normalizer"

# ---------------------------------------------------------------------------
# The assembler's file shape and the two inserted lines
# ---------------------------------------------------------------------------

_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "\n"
    "*** Variables ***\n"
    "${url}    https://example.com\n"
    "${btn_locator}    id=go\n"
    "${other_btn_locator}    id=stop\n"
    "${search_locator}    id=q\n"
    "${pw_locator}    id=pw\n"
    "${total_locator}    css=#total\n"
    "${price_locator}    css=#price\n"
    "${other_locator}    css=#other\n"
    "${row_locator}    css=.row\n"
    "\n"
)

_NAME_LINE = "Generated Test"


def _doc(*body: str, head: str = _HEAD, section: str = "*** Test Cases ***", tail: str = "") -> str:
    """A suite in the assembler's shape: `body` lines, LF, ending in a newline."""
    return head + section + "\n" + _NAME_LINE + "\n" + "\n".join(body) + "\n" + tail


def _before(name: str, cell: str, indent: str = "    ") -> str:
    return f"{indent}${{{name}}}=    Run Keyword And Ignore Error    Get Text    {cell}"


def _wait(name: str, cell: str, indent: str = "    ") -> str:
    return (
        f"{indent}Run Keyword And Ignore Error    Wait Until Keyword Succeeds    30s    250ms"
        f"    Get Text    {cell}    !=    ${{{name}}}[1]"
    )


def _w(read: str = "css=#total", action: str = "id=go") -> dict[str, str]:
    return {"read_locator": read, "action_locator": action}


_W = _w()

_OPEN = ("    New Browser    chromium    headless=True", "    New Page    ${url}")
_CLICK = "    Click    ${btn_locator}"
_READ = "    ${total}=    Get Text    ${total_locator}"
_LOG = "    Log    ${total}"
_CLOSE = "    Close Browser"

_BEFORE_TOTAL = _before("total_locator_before", "${total_locator}")
_WAIT_TOTAL = _wait("total_locator_before", "${total_locator}")

_BASE = _doc(*_OPEN, _CLICK, _READ, _LOG, _CLOSE)
_BASE_INSERTED = _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _LOG, _CLOSE)


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# A. Insert — the pair IS placed
# ---------------------------------------------------------------------------

# (id, original, waits, expected)
_CASES_A: list[tuple[str, str, list, str]] = []


def _case(case_id: str, original: str, waits: list, expected: str) -> None:
    _CASES_A.append((case_id, original, waits, expected))


_case("base", _BASE, [_W], _BASE_INSERTED)

# 2. the variable's value and what browser-service says compare by element, not by spelling
_case(
    "var css=#total, bs css=#total", _BASE, [_w("css=#total")], _BASE_INSERTED,
)
_ID_HEAD = _HEAD.replace("${total_locator}    css=#total", "${total_locator}    id=total")
_case(
    "var id=total, bs css=#total",
    _doc(*_OPEN, _CLICK, _READ, _LOG, _CLOSE, head=_ID_HEAD),
    [_w("css=#total")],
    _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _LOG, _CLOSE, head=_ID_HEAD),
)
_case(
    "var css=#total, bs id=total",
    _BASE, [_w("id=total")], _BASE_INSERTED,
)
_case(
    "bs sends the css= locator without its prefix",
    _doc(*_OPEN, _CLICK, "    ${t}=    Get Text    ${row_locator}", _CLOSE),
    [_w(".row", "id=go")],
    _doc(
        *_OPEN, _before("row_locator_before", "${row_locator}"), _CLICK,
        _wait("row_locator_before", "${row_locator}"), "    ${t}=    Get Text    ${row_locator}", _CLOSE,
    ),
)
_case(
    "bs sends an upper-case CSS= prefix",
    _doc(*_OPEN, "    Click    css=.go", "    ${t}=    Get Text    css=.total", _CLOSE),
    [_w("CSS=.total", "Css=.go")],
    _doc(
        *_OPEN, _before("read_before_1", "css=.total"), "    Click    css=.go",
        _wait("read_before_1", "css=.total"), "    ${t}=    Get Text    css=.total", _CLOSE,
    ),
)

_DOT_HEAD = _HEAD.replace("${total_locator}    css=#total", "${total.locator}    css=#total")
_DOT_READ = "    ${total}=    Get Text    ${total.locator}"
_case(
    "a dotted variable name is not extended with _before",
    _doc(*_OPEN, _CLICK, _DOT_READ, _CLOSE, head=_DOT_HEAD),
    [_W],
    _doc(
        *_OPEN, _before("read_before_1", "${total.locator}"), _CLICK, _wait("read_before_1", "${total.locator}"),
        _DOT_READ, _CLOSE, head=_DOT_HEAD,
    ),
)

# 3. inline locators
_case(
    "inline locators",
    _doc(*_OPEN, "    Click    id=go", "    ${v}=    Get Text    css=#total", _CLOSE),
    [_W],
    _doc(
        *_OPEN, _before("read_before_1", "css=#total"), "    Click    id=go",
        _wait("read_before_1", "css=#total"), "    ${v}=    Get Text    css=#total", _CLOSE,
    ),
)

# 4. two reads after one click
_case(
    "two inline reads after one click",
    _doc(
        *_OPEN, "    Click    id=go", "    ${a}=    Get Text    css=#total", "    ${b}=    Get Text    css=#price", _CLOSE,
    ),
    [_w("css=#total"), _w("css=#price")],
    _doc(
        *_OPEN,
        _before("read_before_1", "css=#total"),
        _before("read_before_2", "css=#price"),
        "    Click    id=go",
        _wait("read_before_1", "css=#total"),
        "    ${a}=    Get Text    css=#total",
        _wait("read_before_2", "css=#price"),
        "    ${b}=    Get Text    css=#price",
        _CLOSE,
    ),
)
_case(
    "two variable reads after one click",
    _doc(
        *_OPEN, _CLICK, _READ, "    ${price}=    Get Text    ${price_locator}", _CLOSE,
    ),
    [_w("css=#total"), _w("css=#price")],
    _doc(
        *_OPEN,
        _BEFORE_TOTAL,
        _before("price_locator_before", "${price_locator}"),
        _CLICK,
        _WAIT_TOTAL,
        _READ,
        _wait("price_locator_before", "${price_locator}"),
        "    ${price}=    Get Text    ${price_locator}",
        _CLOSE,
    ),
)
_case(
    "a skipped wait keeps its position in the inline name",
    _doc(*_OPEN, "    Click    id=go", "    ${b}=    Get Text    css=#price", _CLOSE),
    [_w("css=#gone"), _w("css=#price")],
    _doc(
        *_OPEN, _before("read_before_2", "css=#price"), "    Click    id=go",
        _wait("read_before_2", "css=#price"), "    ${b}=    Get Text    css=#price", _CLOSE,
    ),
)

# 5. shapes
_case(
    "Browser.Click / Browser.Get Text",
    _doc(*_OPEN, "    Browser.Click    ${btn_locator}", "    ${total}=    Browser.Get Text    ${total_locator}", _CLOSE),
    [_W],
    _doc(
        *_OPEN, _BEFORE_TOTAL, "    Browser.Click    ${btn_locator}", _WAIT_TOTAL,
        "    ${total}=    Browser.Get Text    ${total_locator}", _CLOSE,
    ),
)
_case(
    "Fill Secret is the action",
    _doc(*_OPEN, "    Fill Secret    ${pw_locator}    %{PW}", _READ, _CLOSE),
    [_w("css=#total", "id=pw")],
    _doc(*_OPEN, _BEFORE_TOTAL, "    Fill Secret    ${pw_locator}    %{PW}", _WAIT_TOTAL, _READ, _CLOSE),
)
_case(
    "tab separators and a tab indent",
    _doc(*_OPEN, "\tClick\t${btn_locator}", "\t${total}=\tGet Text\t${total_locator}", _CLOSE),
    [_W],
    _doc(
        *_OPEN, _before("total_locator_before", "${total_locator}", "\t"), "\tClick\t${btn_locator}",
        _wait("total_locator_before", "${total_locator}", "\t"), "\t${total}=\tGet Text\t${total_locator}", _CLOSE,
    ),
)
_case(
    "a Tasks section",
    _doc(*_OPEN, _CLICK, _READ, _CLOSE, section="*** Tasks ***"),
    [_W],
    _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _CLOSE, section="*** Tasks ***"),
)
_case(
    "comment lines and a trailing comment on the action line",
    _doc(
        *_OPEN, "    # open the report", "    Click    ${btn_locator}    # the go button", "    # now read",
        _READ, _CLOSE,
    ),
    [_W],
    _doc(
        *_OPEN, "    # open the report", _BEFORE_TOTAL, "    Click    ${btn_locator}    # the go button",
        "    # now read", _WAIT_TOTAL, _READ, _CLOSE,
    ),
)
_case(
    "a read that continues on a ... line",
    _doc(*_OPEN, _CLICK, "    ${total}=    Get Text    ${total_locator}", "    ...    ==    5", _CLOSE),
    [_W],
    _doc(
        *_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, "    ${total}=    Get Text    ${total_locator}", "    ...    ==    5",
        _CLOSE,
    ),
)
_case(
    "a read with its own assertion",
    _doc(*_OPEN, _CLICK, "    Get Text    ${total_locator}    ==    5", _CLOSE),
    [_W],
    _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, "    Get Text    ${total_locator}    ==    5", _CLOSE),
)
_case(
    "click and read of the same locator",
    _doc(*_OPEN, _CLICK, "    ${t}=    Get Text    ${btn_locator}", _CLOSE),
    [_w("id=go", "id=go")],
    _doc(
        *_OPEN, _before("btn_locator_before", "${btn_locator}"), _CLICK,
        _wait("btn_locator_before", "${btn_locator}"), "    ${t}=    Get Text    ${btn_locator}", _CLOSE,
    ),
)
_case(
    "a [Teardown] click plus one body click",
    _doc("    [Teardown]    Click    ${btn_locator}", *_OPEN, _CLICK, _READ, _CLOSE),
    [_W],
    _doc("    [Teardown]    Click    ${btn_locator}", *_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _CLOSE),
)

# 6. a CRLF file whose last line is the read, no trailing newline
_CRLF_LAST_READ = _crlf(_doc(*_OPEN, _CLICK, _READ)).rstrip("\r\n")
_case(
    "CRLF, the last line is the read",
    _CRLF_LAST_READ,
    [_W],
    _crlf(_doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL)) + _READ,
)
_case(
    "CRLF, a trailing newline",
    _crlf(_BASE),
    [_W],
    _crlf(_BASE_INSERTED),
)

# 7. a locator variable re-pointed inside the test
_REPOINT = "    ${total_locator}=    Set Variable    id=other"
_case(
    "a re-pointed locator variable, bs marked the new value",
    _doc(*_OPEN, _REPOINT, _CLICK, _READ, _CLOSE),
    [_w("id=other")],
    _doc(*_OPEN, _REPOINT, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _CLOSE),
)

# 8. several action lines on one element
_FILL = "    Fill Text    ${search_locator}    shoes"
_ENTER = "    Press Keys    ${search_locator}    Enter"
_Q = "id=q"
_case(
    "Fill Text + Press Keys + read",
    _doc(*_OPEN, _FILL, _ENTER, _READ, _CLOSE),
    [_w("css=#total", _Q)],
    _doc(*_OPEN, _BEFORE_TOTAL, _FILL, _ENTER, _WAIT_TOTAL, _READ, _CLOSE),
)
_case(
    "Click X twice + read",
    _doc(*_OPEN, _CLICK, _CLICK, _READ, _CLOSE),
    [_W],
    _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _CLICK, _WAIT_TOTAL, _READ, _CLOSE),
)
_CLICK_Y = "    Click    ${other_btn_locator}"
_case(
    "Click X, Click Y, Click X, read",
    _doc(*_OPEN, _CLICK, _CLICK_Y, _CLICK, _READ, _CLOSE),
    [_W],
    _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _CLICK_Y, _CLICK, _WAIT_TOTAL, _READ, _CLOSE),
)
_case(
    "type, read, Enter, read of the same locator",
    _doc(*_OPEN, _FILL, _READ, _ENTER, "    ${after}=    Get Text    ${total_locator}", _CLOSE),
    [_w("css=#total", _Q)],
    _doc(
        *_OPEN, _BEFORE_TOTAL, _FILL, _READ, _ENTER, _WAIT_TOTAL, "    ${after}=    Get Text    ${total_locator}",
        _CLOSE,
    ),
)

_ORIGINAL_A = {case_id: original for case_id, original, _, _ in _CASES_A}


def _params_a():
    return [pytest.param(o, w, e, id=i) for i, o, w, e in _CASES_A]


class TestInsertPlacesThePair:
    @pytest.mark.parametrize(("original", "waits", "expected"), _params_a())
    def test_exact_lines_at_exact_positions(self, original, waits, expected):
        assert insert_change_waits(original, waits) == expected

    def test_the_two_lines_have_the_exact_text(self):
        out = insert_change_waits(_BASE, [_W]).split("\n")
        i = out.index(_CLICK)
        assert out[i - 1] == (
            "    ${total_locator_before}=    Run Keyword And Ignore Error    Get Text    ${total_locator}"
        )
        assert out[i + 1] == (
            "    Run Keyword And Ignore Error    Wait Until Keyword Succeeds    30s    250ms    "
            "Get Text    ${total_locator}    !=    ${total_locator_before}[1]"
        )
        assert out[i + 2] == _READ

    def test_a_generator_of_waits_is_accepted(self):
        assert insert_change_waits(_BASE, iter([_W])) == _BASE_INSERTED

    def test_one_info_line_per_inserted_pair(self, caplog):
        original, waits, _ = next((o, w, e) for i, o, w, e in _CASES_A if i == "two inline reads after one click")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            insert_change_waits(original, waits)
        signals = [r for r in caplog.records if "signal: change-wait-inserted" in r.getMessage()]
        assert len(signals) == 2
        assert all(r.levelno == logging.INFO for r in signals)
        assert _warnings(caplog) == []


_PROBE_DIR = Path("bench/private/bs31_eval/probe_pr42/exec_check")
_READ_LOCATOR = "css=#tbodyid >> div.col-lg-4 >> nth=0 >> a.hrefch"

_REAL_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "Library    Collections\n"
    "\n"
    "*** Variables ***\n"
    "${browser}    chromium\n"
    "${headless}    True\n"
    "${url}    https://www.demoblaze.com\n"
)
_REAL_VARS_TAIL = "${first_laptop_name_locator}    css=#tbodyid >> div.col-lg-4 >> nth=0 >> a.hrefch\n"
_REAL_BODY_HEAD = (
    "\n*** Test Cases ***\n"
    "Generated Test\n"
    "    [Documentation]    Auto-generated test case\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    ${url}\n"
)
_REAL_BODY_TAIL = (
    "    ${first_laptop_name}=    Get Text    ${first_laptop_name_locator}\n"
    "    Log    @@ARG@@\n"
    "    Close Browser"
)
# The two real files (bench/private/bs31_eval/probe_pr42/exec_check/p03_*.robot): CRLF, no trailing newline.
_REAL_60A0 = _crlf(
    _REAL_HEAD
    + "${laptops_category_locator}    a:has-text('Laptops')\n"
    + _REAL_VARS_TAIL
    + _REAL_BODY_HEAD
    + "    Click    ${laptops_category_locator}\n"
    + _REAL_BODY_TAIL.replace("@@ARG@@", "First Laptop Name: ${first_laptop_name}")
)
_REAL_C84F = _crlf(
    _REAL_HEAD
    + "${laptops_link_locator}    xpath=//div/a[3]\n"
    + _REAL_VARS_TAIL
    + _REAL_BODY_HEAD
    + "    # WARNING: locator for 'Laptops link in the left categories sidebar menu' is positional — "
    "may break in a fresh session\n"
    "    Click    ${laptops_link_locator}\n"
    + _REAL_BODY_TAIL.replace("@@ARG@@", "${first_laptop_name}")
)
_REAL_BEFORE = (
    "    ${first_laptop_name_locator_before}=    Run Keyword And Ignore Error    Get Text    "
    "${first_laptop_name_locator}"
)
_REAL_WAIT = (
    "    Run Keyword And Ignore Error    Wait Until Keyword Succeeds    30s    250ms    Get Text    "
    "${first_laptop_name_locator}    !=    ${first_laptop_name_locator_before}[1]"
)
_REAL_READ_LINE = "    ${first_laptop_name}=    Get Text    ${first_laptop_name_locator}"

_REAL_CASES = [
    ("60a0b526", _REAL_60A0, "a:has-text('Laptops')", "    Click    ${laptops_category_locator}"),
    ("c84f4e64", _REAL_C84F, "xpath=//div/a[3]", "    Click    ${laptops_link_locator}"),
]


class TestTheRealFiles:
    @pytest.mark.parametrize(("name", "text", "action", "click"), _REAL_CASES, ids=[c[0] for c in _REAL_CASES])
    def test_the_pair_is_placed_and_nothing_else_changes(self, name, text, action, click):
        out = insert_change_waits(text, [_w(_READ_LOCATOR, action)])
        lines = text.split("\r\n")
        i = lines.index(click)
        j = lines.index(_REAL_READ_LINE)
        expected = lines[:i] + [_REAL_BEFORE] + lines[i:j] + [_REAL_WAIT] + lines[j:]
        assert out == "\r\n".join(expected)
        assert out.count("\n") == out.count("\r\n")  # CRLF kept: no bare LF
        assert not out.endswith("\n")  # still no trailing newline

    def test_the_comment_stays_above_the_before_read(self):
        out = insert_change_waits(_REAL_C84F, [_w(_READ_LOCATOR, "xpath=//div/a[3]")]).split("\r\n")
        i = out.index(_REAL_BEFORE)
        assert out[i - 1].lstrip().startswith("# WARNING")
        assert out[i + 1] == "    Click    ${laptops_link_locator}"

    @pytest.mark.skipif(not _PROBE_DIR.is_dir(), reason="bench/private is not in this checkout")
    def test_the_inlined_text_is_the_real_file(self):
        for stamp, text in (("60a0b526-0c2b-4f66-9f88-0b82609782d7", _REAL_60A0),
                            ("c84f4e64-5f69-41f3-bf0c-d892bf0214ef", _REAL_C84F)):
            real = (_PROBE_DIR / f"p03_{stamp}.robot").read_bytes().decode("utf-8")
            assert real == text


class TestCrlfLastLineIsTheRead:
    def test_both_inserted_lines_end_with_crlf(self):
        original = _CRLF_LAST_READ
        out = insert_change_waits(original, [_W])
        assert not out.endswith("\n")
        assert re.search(r"(?<!\r)\n", out) is None
        lines = out.split("\r\n")
        assert lines[-1] == _READ
        assert lines[-2] == _WAIT_TOTAL
        assert lines[-4] == _BEFORE_TOTAL
        assert lines[-3] == _CLICK


# ---------------------------------------------------------------------------
# A9. A pair already present plus a second, new wait
# ---------------------------------------------------------------------------

class TestAPairIsAlreadyThere:
    def test_only_the_new_pair_is_added(self):
        original = _ORIGINAL_A["two variable reads after one click"]
        w1, w2 = _w("css=#total"), _w("css=#price")
        once = insert_change_waits(original, [w1])
        assert once.count("Wait Until Keyword Succeeds") == 1
        both = insert_change_waits(once, [w1, w2])
        assert both == insert_change_waits(original, [w1, w2])
        assert both.count("Wait Until Keyword Succeeds") == 2
        assert both.count("Get Text") == 6  # 2 reads, 2 before-reads, 2 waits


# ---------------------------------------------------------------------------
# B. Insert — the file stays byte-identical
# ---------------------------------------------------------------------------

R_PARSE = "could not be read as one suite"
R_KEYWORDS = "could define keywords"
R_COUNT = "not one"
R_FOLLOW = "cannot be followed line by line"
R_ACTION = "no action line"
R_READ = "no Get Text"
R_HALF = "only one of its two lines"


def _blocked(
    blocked: str, control: str, reason: str, case_id: str,
    waits: list | None = None, control_waits: list | None = None,
):
    """A file that must stay as it is, and the control: the same file with the one blocking detail changed."""
    return pytest.param(
        blocked, control, reason,
        waits if waits is not None else [_W],
        control_waits if control_waits is not None else [_W],
        id=case_id,
    )


_NO_ACTION = _doc(*_OPEN, "    Log    nothing to click", _READ, _CLOSE)
_READ_ABOVE = _doc(*_OPEN, _READ, _CLICK, _LOG, _CLOSE)
_OTHER_READ = _doc(*_OPEN, _CLICK, "    ${total}=    Get Text    ${other_locator}", _CLOSE)
_FOR_READ = _doc(
    *_OPEN, _CLICK, "    FOR    ${i}    IN RANGE    1", "        ${total}=    Get Text    ${total_locator}", "    END", _CLOSE,
)
_GROUP_READ = _doc(*_OPEN, _CLICK, "    GROUP    totals", "        ${total}=    Get Text    ${total_locator}", "    END", _CLOSE)
_WITH_KEYWORDS = _BASE + "\n*** Keywords ***\nMy Keyword\n    Log    x\n"
_TWO_TESTS = _BASE + "\nSecond Test\n    Log    other\n"
_ONLY_BEFORE = _BASE_INSERTED.replace(_WAIT_TOTAL + "\n", "")
_ONLY_WAIT = _BASE_INSERTED.replace(_BEFORE_TOTAL + "\n", "")
_PIPE = (
    "| *** Settings *** |\n| Library | Browser |\n\n| *** Test Cases *** |\n| Generated Test |\n"
    "| | Click | id=go |\n| | ${total}= | Get Text | css=#total |\n"
)
_PIPE_CONTROL = (
    "*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nGenerated Test\n"
    "    Click    id=go\n    ${total}=    Get Text    css=#total\n"
)

_BLOCKED_CASES = [
    _blocked(_NO_ACTION, _BASE, R_ACTION, case_id="no action line"),
    _blocked(_READ_ABOVE, _BASE, R_READ, case_id="the read only above the action"),
    _blocked(_OTHER_READ, _BASE, R_READ, case_id="no read of that locator"),
    _blocked(_FOR_READ, _BASE, R_FOLLOW, case_id="the read inside FOR ... END"),
    _blocked(_GROUP_READ, _BASE, R_FOLLOW, case_id="the read inside GROUP ... END"),
    _blocked(_WITH_KEYWORDS, _BASE, R_KEYWORDS, case_id="a *** Keywords *** section"),
    _blocked(_TWO_TESTS, _BASE, R_COUNT, case_id="two test cases"),
    _blocked(_ONLY_BEFORE, _BASE, R_HALF, case_id="only the before-read present"),
    _blocked(_ONLY_WAIT, _BASE, R_HALF, case_id="only the wait present"),
    _blocked(
        _doc(*_OPEN, "    Set Test Variable    ${total_locator}    id=other", _CLICK, _READ, _CLOSE),
        _doc(*_OPEN, "    Log    ${total_locator}", _CLICK, _READ, _CLOSE),
        R_PARSE, case_id="the variable re-pointed by Set Test Variable",
    ),
    _blocked(
        _doc(*_OPEN, _REPOINT, _CLICK, _READ, _CLOSE),
        _doc(*_OPEN, _REPOINT, _CLICK, _READ, _CLOSE),
        R_READ, case_id="the variable re-pointed by Set Variable, bs marked the old value",
        waits=[_w("css=#total")], control_waits=[_w("id=other")],
    ),
    _blocked(
        _doc(*_OPEN, "    VAR    ${total_locator}    css=#total", _CLICK, _READ, _CLOSE),
        _doc(*_OPEN, "    Log    ${total_locator}", _CLICK, _READ, _CLOSE),
        R_PARSE, case_id="the variable re-pointed by VAR",
    ),
    _blocked(
        _doc(*_OPEN, "    ${total_locator}=    Get Variable Value    ${missing}    css=#total", _CLICK, _READ, _CLOSE),
        _doc(*_OPEN, "    ${total_locator}=    Set Variable    css=#total", _CLICK, _READ, _CLOSE),
        R_READ, case_id="a variable assigned from a keyword",
    ),
    _blocked(
        _doc(*_OPEN, _CLICK, _READ, _CLOSE, head=_HEAD.replace("${total_locator}    css=#total", "${total_locator}    #total")),
        _BASE, R_READ, case_id="a bare #total variable value",
    ),
    _blocked(
        _doc(*_OPEN, " Click    ${btn_locator}", _READ, _CLOSE), _BASE, R_FOLLOW, case_id="a step indented by one space",
    ),
    _blocked(
        _doc(*_OPEN, "    IF    ${ready}    Click    ${btn_locator}", _READ, _CLOSE), _BASE, R_FOLLOW,
        case_id="the Click inside an inline IF",
    ),
    _blocked(
        _doc(*_OPEN, "    Run Keyword And Continue On Failure    Click    ${btn_locator}", _READ, _CLOSE),
        _BASE, R_ACTION, case_id="the Click under Run Keyword And Continue On Failure",
    ),
    _blocked(
        _doc(*_OPEN, "    SeleniumLibrary.Click    ${btn_locator}", _READ, _CLOSE),
        _BASE, R_ACTION, case_id="another library's SeleniumLibrary.Click",
    ),
    _blocked(_PIPE, _PIPE_CONTROL, R_PARSE, case_id="a pipe-separated file"),
    _blocked("*** Test Cases ***\n\x00", _BASE, R_ACTION, case_id="garbage"),
]


class TestInsertLeavesTheFileAlone:
    @pytest.mark.parametrize(("blocked", "control", "reason", "waits", "control_waits"), _BLOCKED_CASES)
    def test_unchanged_with_one_warning_naming_the_reason(self, blocked, control, reason, waits, control_waits, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = insert_change_waits(blocked, waits)
        assert out == blocked
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert reason in warnings[0].getMessage()
        assert "Change-wait" in warnings[0].getMessage()

    @pytest.mark.parametrize(("blocked", "control", "reason", "waits", "control_waits"), _BLOCKED_CASES)
    def test_the_control_gets_the_pair(self, blocked, control, reason, waits, control_waits, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = insert_change_waits(control, control_waits)
        assert out != control
        assert out.count("Wait Until Keyword Succeeds") == 1
        assert _warnings(caplog) == []

    def test_no_waits_returns_the_same_object(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert insert_change_waits(_BASE, []) is _BASE
            assert insert_change_waits(_BASE, None) is _BASE
            assert insert_change_waits(_BASE, iter([])) is _BASE
        assert caplog.records == []
        # control: the same file with a wait gets the pair
        assert insert_change_waits(_BASE, [_W]) != _BASE

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_text_returns_the_same_object(self, empty, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert insert_change_waits(empty, [_W]) is empty
        assert caplog.records == []

    def test_a_pair_already_present_is_left_alone_without_a_log(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert insert_change_waits(_BASE_INSERTED, [_W]) == _BASE_INSERTED
        assert caplog.records == []
        # control: the same file without the pair is changed
        assert insert_change_waits(_BASE, [_W]) == _BASE_INSERTED

    def test_half_a_pair_leaves_the_whole_file_even_when_another_wait_could_be_placed(self, caplog):
        original = _ORIGINAL_A["two variable reads after one click"]
        half = insert_change_waits(original, [_w("css=#total")]).replace(_WAIT_TOTAL + "\n", "")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = insert_change_waits(half, [_w("css=#total"), _w("css=#price")])
        assert out == half
        assert len(_warnings(caplog)) == 1
        # control: a file with no half pair gets both
        assert insert_change_waits(original, [_w("css=#total"), _w("css=#price")]).count("Wait Until") == 2

    def test_two_waits_that_would_share_one_variable_never_assign_it_twice(self, caplog):
        original = _doc(*_OPEN, _CLICK, _CLICK_Y, _READ, _CLOSE)
        both = [_W, _w("css=#total", "id=stop")]
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = insert_change_waits(original, both)
        assert out == _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _CLICK_Y, _WAIT_TOTAL, _READ, _CLOSE)
        assert out.count("${total_locator_before}=") == 1
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "already uses its variable" in warnings[0].getMessage()
        # control: each wait alone is placed
        assert insert_change_waits(original, [both[1]]) != original

    def test_one_wait_skipped_the_others_still_placed(self, caplog):
        original = _doc(*_OPEN, _CLICK, _READ, _CLOSE)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            out = insert_change_waits(original, [_w("css=#gone"), _W])
        assert out == _doc(*_OPEN, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _CLOSE)
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert R_READ in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# C. Properties over every case in A
# ---------------------------------------------------------------------------

class TestProperties:
    @pytest.mark.parametrize(("original", "waits", "expected"), _params_a())
    def test_a_second_insert_changes_nothing(self, original, waits, expected):
        first = insert_change_waits(original, waits)
        assert insert_change_waits(first, waits) == first

    @pytest.mark.parametrize(("original", "waits", "expected"), _params_a())
    def test_remove_gives_back_the_original(self, original, waits, expected):
        assert remove_change_waits(insert_change_waits(original, waits)) == original

    @pytest.mark.parametrize(("original", "waits", "expected"), _params_a())
    def test_insert_after_remove_gives_the_inserted_text_again(self, original, waits, expected):
        inserted = insert_change_waits(original, waits)
        assert insert_change_waits(remove_change_waits(inserted), waits) == inserted

    @pytest.mark.parametrize(("original", "waits", "expected"), _params_a())
    def test_remove_twice_equals_once(self, original, waits, expected):
        once = remove_change_waits(insert_change_waits(original, waits))
        assert remove_change_waits(once) == once

    @pytest.mark.parametrize(("name", "text", "action", "click"), _REAL_CASES, ids=[c[0] for c in _REAL_CASES])
    def test_the_real_files_round_trip(self, name, text, action, click):
        waits = [_w(_READ_LOCATOR, action)]
        inserted = insert_change_waits(text, waits)
        assert remove_change_waits(inserted) == text
        assert insert_change_waits(inserted, waits) == inserted


# ---------------------------------------------------------------------------
# D. Remove
# ---------------------------------------------------------------------------

_HAND_WAIT = (
    "    Run Keyword And Ignore Error    Wait Until Keyword Succeeds    30s    250ms    Get Text    css=#x    !=    old"
)
_OWN_IGNORE_CLICK = "    Run Keyword And Ignore Error    Click    ${btn_locator}"

_SWAPPED = _doc(*_OPEN, _WAIT_TOTAL, _BEFORE_TOTAL, _CLICK, _READ, _LOG, _CLOSE)
_OTHER_NAME_WAIT = _wait("price_locator_before", "${total_locator}")
_OTHER_CELL_WAIT = _wait("total_locator_before", "${price_locator}")

_REMOVE_STAYS = [
    pytest.param(_BASE, id="a file without a pair"),
    pytest.param(_ONLY_BEFORE, id="only the before-read"),
    pytest.param(_ONLY_WAIT, id="only the wait"),
    pytest.param(_BASE_INSERTED.replace("30s    250ms", "10s    250ms"), id="the timeout edited to 10s"),
    pytest.param(_BASE_INSERTED.replace("30s    250ms", "30s    1s"), id="the interval edited"),
    pytest.param(_BASE_INSERTED.replace(_WAIT_TOTAL, _OTHER_CELL_WAIT), id="the wait reads another locator"),
    pytest.param(_BASE_INSERTED.replace(_WAIT_TOTAL, _OTHER_NAME_WAIT), id="the wait names another variable"),
    pytest.param(_SWAPPED, id="the wait written above its before-read"),
    pytest.param(_doc(*_OPEN, _OWN_IGNORE_CLICK, _READ, _CLOSE), id="the assembler's own Run Keyword And Ignore Error"),
    pytest.param(_doc(*_OPEN, _CLICK, _HAND_WAIT, _READ, _CLOSE), id="a hand-written wait against another value"),
    pytest.param(_BASE_INSERTED.replace("!=    ${total_locator_before}[1]", "!=    ${total_locator_before}[0]"),
                 id="the wait compares another item"),
    pytest.param(_BASE_INSERTED.replace("    !=    ${total_locator_before}[1]", "    ==    ${total_locator_before}[1]"),
                 id="the wait compares with == instead of !="),
    pytest.param(_BASE_INSERTED.replace(_WAIT_TOTAL, _WAIT_TOTAL + "    # kept"), id="a wait line with a comment"),
    pytest.param("*** Test Cases ***\n\x00", id="garbage"),
]


class TestRemove:
    def test_the_control_pair_is_removed(self):
        assert remove_change_waits(_BASE_INSERTED) == _BASE

    @pytest.mark.parametrize("text", _REMOVE_STAYS)
    def test_stays_byte_identical(self, text):
        out = remove_change_waits(text)
        assert out == text
        assert out is text  # nothing to drop returns the same object

    def test_a_pair_inside_other_edits_drops_only_the_pair(self):
        text = _doc(
            *_OPEN, _OWN_IGNORE_CLICK, _BEFORE_TOTAL, _HAND_WAIT, _CLICK, _WAIT_TOTAL, _READ, _LOG, _CLOSE,
        )
        assert remove_change_waits(text) == _doc(*_OPEN, _OWN_IGNORE_CLICK, _HAND_WAIT, _CLICK, _READ, _LOG, _CLOSE)

    def test_the_inline_name_is_removed_too(self):
        text = insert_change_waits(_ORIGINAL_A["inline locators"], [_W])
        assert "read_before_1" in text
        assert remove_change_waits(text) == _ORIGINAL_A["inline locators"]

    def test_a_pair_with_different_separators_than_ours_is_still_exactly_matched_by_cells(self):
        # tabs between the cells hold the same cells; the cells are what is compared
        before = _BEFORE_TOTAL.replace("    Run", "\tRun")
        text = _doc(*_OPEN, before, _CLICK, _WAIT_TOTAL, _READ, _CLOSE)
        assert remove_change_waits(text) == _doc(*_OPEN, _CLICK, _READ, _CLOSE)

    def test_one_leftover_before_read_stays_when_two_befores_share_one_wait(self):
        text = _doc(*_OPEN, _BEFORE_TOTAL, _BEFORE_TOTAL, _CLICK, _WAIT_TOTAL, _READ, _CLOSE)
        out = remove_change_waits(text)
        assert out.count("Get Text") == 2  # one before-read stays, the read itself
        assert remove_change_waits(out) == out

    def test_crlf_lines_leave_with_their_own_line_ends(self):
        text = _crlf(_BASE_INSERTED)
        assert remove_change_waits(text) == _crlf(_BASE)

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_text_returns_the_same_object(self, empty):
        assert remove_change_waits(empty) is empty

    def test_no_pair_returns_the_same_object(self):
        assert remove_change_waits(_BASE) is _BASE
        # control: a pair is dropped
        assert remove_change_waits(_BASE_INSERTED) is not _BASE_INSERTED


# ---------------------------------------------------------------------------
# E. Neither function ever raises
# ---------------------------------------------------------------------------

class TestNeverRaises:
    @pytest.mark.parametrize("text", ["\x00", "\n\n", "*** Test Cases ***\n\x00", "|", "***", "    Click\n", "\r\n\r\n"])
    def test_garbage_in_both_directions(self, text):
        assert insert_change_waits(text, [_W]) == text
        assert remove_change_waits(text) == text

    @pytest.mark.parametrize(
        "waits",
        [
            [{"read_locator": "css=#total"}],
            [{"action_locator": "id=go"}],
            [None],
            ["css=#total"],
            [{"read_locator": 5, "action_locator": "id=go"}],
            [_W, {"read_locator": "css=#total"}],
        ],
        ids=["no action key", "no read key", "None entry", "a string entry", "a non-text locator", "good then bad"],
    )
    def test_a_bad_wait_leaves_the_file_with_one_warning(self, waits, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert insert_change_waits(_BASE, waits) == _BASE
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        # control: the good wait alone is placed
        assert insert_change_waits(_BASE, [_W]) == _BASE_INSERTED

    def test_a_bug_inside_the_insert_leaves_the_file_with_one_warning(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER), patch(
            "src.backend.crew_ai.robot_code_normalizer._parse_suite", side_effect=RuntimeError("boom")
        ):
            assert insert_change_waits(_BASE, [_W]) == _BASE
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None

    def test_a_bug_inside_the_remove_leaves_the_file_with_one_warning(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER), patch(
            "src.backend.crew_ai.robot_code_normalizer._content_cells", side_effect=RuntimeError("boom")
        ):
            assert remove_change_waits(_BASE_INSERTED) == _BASE_INSERTED
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        # control: without the bug the pair is removed
        assert remove_change_waits(_BASE_INSERTED) == _BASE
