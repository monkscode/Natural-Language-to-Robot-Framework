"""Tests for rewrite_should_be_true_messages in src.backend.crew_ai.robot_code_normalizer.

`Should Be True    'undefined' not in $delivery_text` fails with `'undefined' not in $delivery_text should be true.`:
a bare `$name` is Robot's evaluation-namespace form and Robot does not put its value in the message, so the tester
never sees the text the page showed. The rewrite appends one `msg=` cell that names each bare `$name`'s value.
A braced `${x}` is substituted before evaluation, so its failure already shows the value: left alone.

Every "left alone" case has a control: the base file IS rewritten, and each case differs from it in one line.
"""

import logging

import pytest

from src.backend.crew_ai.robot_code_normalizer import rewrite_should_be_true_messages

LOGGER = "src.backend.crew_ai.robot_code_normalizer"

_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "\n"
    "*** Test Cases ***\n"
    "Check Delivery\n"
    "    New Page    https://example.com\n"
    "    ${delivery_text}=    Get Text    id=delivery\n"
)
_COND = "'undefined' not in $delivery_text"
_LINE = f"    Should Be True    {_COND}"
_MSG = f"msg='{_COND}' should be true. delivery_text: ${{delivery_text}}"
_REWRITTEN = f"{_LINE}    {_MSG}"
_TAIL = "\n    Close Browser\n"


def _file(step: str) -> str:
    return _HEAD + step + _TAIL


BASE = _file(_LINE)


def test_control_the_base_file_is_rewritten():
    assert rewrite_should_be_true_messages(BASE) == _file(_REWRITTEN)


@pytest.mark.parametrize("condition, names", [
    ("'undefined' not in $delivery_text and len($delivery_text.strip()) > 0", ["delivery_text"]),
    ("any(char.isdigit() for char in $delivery_text)", ["delivery_text"]),
    ("$a in $b and $a != $c and len($b) > 0", ["a", "b", "c"]),
    ("'x' in $a + $b", ["a", "b"]),
])
def test_names_are_deduplicated_in_first_seen_order(condition, names):
    out = rewrite_should_be_true_messages(_file(f"    Should Be True    {condition}"))
    values = "; ".join(f"{n}: ${{{n}}}" for n in names)
    assert out == _file(f"    Should Be True    {condition}    msg='{condition}' should be true. {values}")


@pytest.mark.parametrize("step", [
    "    Should Be True    ${count} == 20",                         # braced: Robot already shows the value
    "    Should Be True    '$nope' == '$nope'",                     # `$` inside a string literal is no variable
    "    Should Be True    '$x' in 'a$y'",
    "    Should Be True    $a == 1    Custom message",              # a second positional argument
    f"    Should Be True    {_COND}    msg=mine",                   # msg= present
    f"    Should Be True    {_COND}    msg=mine    html=True",
    f"    Run Keyword If    ${{ok}}    Should Be True    {_COND}",
    f"    Wait Until Keyword Succeeds    5x    1s    Should Be True    {_COND}",
    f"    Should Be True    {_COND}\n    ...    msg=mine",          # continued line
    "    Should Be True    'abc in $x",                             # the tokenizer refuses it
    "    Should Be True    $",                                      # a `$` with no name
    f"    ${{r}}=    Should Be True    {_COND}",                    # an assignment
])
def test_a_line_that_does_not_qualify_is_left_alone(step):
    raw = _file(step)
    assert rewrite_should_be_true_messages(raw) == raw


def test_a_builtin_prefix_is_rewritten_and_another_library_is_not():
    out = rewrite_should_be_true_messages(_file(f"    BuiltIn.Should Be True    {_COND}"))
    assert out == _file(f"    BuiltIn.Should Be True    {_COND}    {_MSG}")
    other = _file(f"    Other.Should Be True    {_COND}")
    assert rewrite_should_be_true_messages(other) == other


def test_keyword_spelling_is_matched_the_way_robot_matches_it():
    out = rewrite_should_be_true_messages(_file(f"    should_be_true    {_COND}"))
    assert out == _file(f"    should_be_true    {_COND}    {_MSG}")


def test_a_step_in_a_user_keyword_is_left_alone():
    keyword = f"\n*** Keywords ***\nCheck It\n    Should Be True    {_COND}\n"
    assert rewrite_should_be_true_messages(BASE + keyword) == _file(_REWRITTEN) + keyword
    only_keyword = _file("    Check It") + keyword
    assert rewrite_should_be_true_messages(only_keyword) == only_keyword


def test_a_test_with_a_template_is_left_alone():
    raw = _file(f"    [Template]    Should Be True\n    {_COND}")
    assert rewrite_should_be_true_messages(raw) == raw


@pytest.mark.parametrize("mutate", [
    lambda raw: "language: fi\n" + raw,
    lambda raw: "# LANGUAGE: fi\n" + raw,
    lambda raw: raw.replace("id=delivery", "{{SECRET}}"),
    lambda raw: raw.replace("id=delivery", "%NLRF_SECRET_X"),
    lambda raw: raw.replace("Close Browser", "Fill Secret    id=p    %{X}"),
    lambda raw: raw.replace("Close Browser", "fill_secret    id=p    %{X}"),
    lambda raw: raw.replace("Close Browser", "Type Secret    id=p    %{X}"),
    lambda raw: raw + "\n*** Keywords ***\nShould Be True\n    Log    hi\n",
    lambda raw: raw.replace("    Close Browser", "    TRY\n        Close Browser\n    EXCEPT\n        Log    x\n    END"),
], ids=["language", "language upper", "placeholder", "env secret", "Fill Secret", "fill_secret", "Type Secret",
        "own Should Be True", "TRY"])
def test_a_file_guard_leaves_the_whole_file_alone(mutate):
    raw = mutate(BASE)
    assert "$delivery_text" in raw
    assert rewrite_should_be_true_messages(raw) == raw


def test_the_line_keeps_its_indent_line_end_and_trailing_comment():
    crlf = BASE.replace("\n", "\r\n")
    assert rewrite_should_be_true_messages(crlf) == _file(_REWRITTEN).replace("\n", "\r\n")
    tabbed = _file(f"\tShould Be True\t{_COND}")
    assert rewrite_should_be_true_messages(tabbed) == _file(f"\tShould Be True\t{_COND}\t{_MSG}")
    commented = _file(f"{_LINE}    # why")
    assert rewrite_should_be_true_messages(commented) == _file(f"{_REWRITTEN}    # why")


def test_the_second_pass_changes_nothing():
    once = rewrite_should_be_true_messages(BASE)
    assert rewrite_should_be_true_messages(once) == once


def test_every_qualifying_line_is_rewritten_and_the_others_are_not():
    raw = _file(f"{_LINE}\n    Should Be True    ${{n}} == 2\n    Should Be True    $a == 1")
    out = rewrite_should_be_true_messages(raw)
    assert out == _file(f"{_REWRITTEN}\n    Should Be True    ${{n}} == 2\n"
                        "    Should Be True    $a == 1    msg='$a == 1' should be true. a: ${a}")


def test_a_file_the_reader_refuses_and_empty_input_are_left_alone():
    piped = "*** Test Cases ***\n| T |\n| | Should Be True | $a == 1 |\n"
    assert rewrite_should_be_true_messages(piped) == piped
    assert rewrite_should_be_true_messages("") == ""


def test_a_rewrite_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        rewrite_should_be_true_messages(BASE)
    assert any("Should Be True" in r.getMessage() and "rewrote 1" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("condition", [
    "${q}$undefined_name${q} != 'a'",   # Robot substitutes ${q} first: the name sits inside a string, no variable
    "'${x}' in $y",
    "$y == @{items}[0]",
    "$y == &{pairs}[k]",
    "$y == %{HOME}",
    "'a\\\\b' in $y",
    "$y == 'a\\'b'",
])
def test_a_condition_robot_changes_before_it_reads_is_left_alone(condition):
    raw = _file(f"    Should Be True    {condition}")
    assert rewrite_should_be_true_messages(raw) == raw


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.replace("Close Browser", "Set Variable    %{NLRF_SECRET_PW}"),
    lambda raw: raw.replace("Close Browser", "Set Variable    %NLRF_SECRET_PW"),
    lambda raw: raw.replace("    Close Browser", "    [Teardown]    Log    ${TEST MESSAGE}"),
    lambda raw: raw.replace("    Close Browser", "    [Teardown]    Log    ${test_message}"),
    lambda raw: raw.replace("    Close Browser", "    [Teardown]    Log    ${KEYWORD MESSAGE}"),
], ids=["braced env secret", "bare env secret", "TEST MESSAGE", "test_message", "KEYWORD MESSAGE"])
def test_a_secret_or_message_reading_file_is_left_alone(mutate):
    raw = mutate(BASE)
    assert "$delivery_text" in raw
    assert rewrite_should_be_true_messages(raw) == raw


def test_a_name_robot_reads_as_the_same_variable_is_listed_once_with_its_first_spelling():
    out = rewrite_should_be_true_messages(_file("    Should Be True    $Delivery_Text == $delivery_text"))
    assert out == _file("    Should Be True    $Delivery_Text == $delivery_text"
                        "    msg='$Delivery_Text == $delivery_text' should be true. Delivery_Text: ${Delivery_Text}")


def test_the_message_is_robots_own_default_text_plus_the_values():
    # the classifier and the user-facing sentence read Robot's `'<condition>' should be true`
    from src.backend.core.failure_sentences import failure_category, failure_sentence

    value = "Delivery: Arrives by undefined"
    for condition in (_COND, "$a == $b", "len($a) == 20 and $b"):
        out = rewrite_should_be_true_messages(_file(f"    Should Be True    {condition}"))
        cell = next(line for line in out.split("\n") if "msg=" in line).split("    msg=", 1)[1]
        shown = cell.replace("${delivery_text}", value).replace("${a}", "x").replace("${b}", "y")
        robot_default = f"'{condition}' should be true."
        assert shown.startswith(robot_default)
        assert failure_category(shown) == failure_category(robot_default) == "E1"
        assert failure_sentence(shown) == failure_sentence(robot_default) is not None
