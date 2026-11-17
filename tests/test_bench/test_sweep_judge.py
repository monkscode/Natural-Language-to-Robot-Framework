"""Tests for bench/sweep_judge.py: where the held-out sweep's judge line goes.

Referenced by: pytest
Depends on: bench.sweep_judge
"""

import pytest

from bench.sweep_judge import JudgeRefused, insert_judge

JUDGE = "Get Text    css=#r    ==    ok"
HEAD = "*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n    New Page    https://x\n"


def test_inserts_before_close_browser():
    out = insert_judge(HEAD + "    Log    x\n    Close Browser\n", JUDGE)
    assert out.split("\n")[6:9] == ["    Log    x", "    " + JUDGE, "    Close Browser"]


def test_appends_after_last_step_without_close_browser():
    code = HEAD + "    Log    x\n\n*** Keywords ***\nK\n    Log    y\n"
    lines = insert_judge(code, JUDGE).split("\n")
    assert lines[6:8] == ["    Log    x", "    " + JUDGE]
    assert lines[8:10] == ["", "*** Keywords ***"]


def test_browser_prefixed_close():
    out = insert_judge(HEAD + "    Browser.Close Browser\n", JUDGE)
    assert out.split("\n")[6:8] == ["    " + JUDGE, "    Browser.Close Browser"]


def test_keeps_crlf():
    out = insert_judge((HEAD + "    Close Browser\n").replace("\n", "\r\n"), JUDGE)
    assert out.count("\n") == out.count("\r\n") == 8
    assert "    " + JUDGE + "\r\n" in out


def test_refuses_two_tests():
    with pytest.raises(JudgeRefused, match="2 tests"):
        insert_judge(HEAD + "    Log    x\nU\n    Log    y\n", JUDGE)


def test_refuses_unparsable():
    with pytest.raises(JudgeRefused, match="parse"):
        insert_judge(HEAD + "| Log | x |\n", JUDGE)


LOOP = "    FOR    ${i}    IN    a\n        Log    ${i}\n    END\n"


def test_loop_test_judged_after_end():
    out = insert_judge(HEAD + LOOP + "    Close Browser\n", JUDGE)
    assert out.split("\n")[8:11] == ["    END", "    " + JUDGE, "    Close Browser"]


def test_close_browser_inside_loop_not_used():
    code = HEAD + "    FOR    ${i}    IN    a\n        Close Browser\n    END\n"
    lines = insert_judge(code, JUDGE).split("\n")
    assert lines[7:10] == ["        Close Browser", "    END", "    " + JUDGE]


def test_inline_if_opens_no_block():
    out = insert_judge(HEAD + "    IF    $x    Log    y\n    Close Browser\n", JUDGE)
    assert out.split("\n")[6:9] == ["    IF    $x    Log    y", "    " + JUDGE, "    Close Browser"]


def test_block_if_with_else_closes_at_end():
    code = HEAD + "    IF    $x\n        Log    y\n    ELSE\n        Log    z\n    END\n    Close Browser\n"
    lines = insert_judge(code, JUDGE).split("\n")
    assert lines[10:13] == ["    END", "    " + JUDGE, "    Close Browser"]


def test_refuses_unbalanced_block():
    with pytest.raises(JudgeRefused, match="block"):
        insert_judge(HEAD + "    FOR    ${i}    IN    a\n        Log    ${i}\n", JUDGE)


def test_refuses_stray_end():
    with pytest.raises(JudgeRefused, match="block"):
        insert_judge(HEAD + "    END\n", JUDGE)


def test_no_trailing_newline():
    out = insert_judge(HEAD + "    Log    x", JUDGE)
    assert out.endswith("    Log    x\n    " + JUDGE)
    crlf = insert_judge((HEAD + "    Log    x").replace("\n", "\r\n"), JUDGE)
    assert crlf.endswith("    Log    x\r\n    " + JUDGE)


def test_empty_body_test_is_refused():
    with pytest.raises(JudgeRefused, match="no body line"):
        insert_judge("*** Test Cases ***\nT\n", JUDGE)


def test_close_browser_inside_user_keyword_is_not_top_level():
    code = HEAD + "    Log    x\n\n*** Keywords ***\nK\n    Close Browser\n"
    lines = insert_judge(code, JUDGE).split("\n")
    assert lines[6:8] == ["    Log    x", "    " + JUDGE]
    assert lines.count("    Close Browser") == 1


def test_refuses_per_test_template():
    code = HEAD + "    [Template]    Log Many\n    a    b\n    Close Browser\n"
    with pytest.raises(JudgeRefused, match="[Tt]emplate"):
        insert_judge(code, JUDGE)


def test_refuses_suite_level_test_template():
    code = HEAD.replace("Library    Browser\n", "Library    Browser\nTest Template    Log Many\n")
    with pytest.raises(JudgeRefused, match="parse"):
        insert_judge(code + "    a    b\n", JUDGE)


@pytest.mark.parametrize("step", [
    "Pass Execution    done",
    "Pass Execution If    ${x}    done",
    "BuiltIn.Pass Execution    done",
    "builtin.pass_execution_if    ${x}    done",
    "PASS_execution    done",
])
def test_refuses_pass_execution(step):
    with pytest.raises(JudgeRefused, match="Pass Execution"):
        insert_judge(HEAD + f"    {step}\n    Close Browser\n", JUDGE)


def test_logging_the_words_pass_execution_is_judged():
    out = insert_judge(HEAD + "    Log    Pass Execution\n    Close Browser\n", JUDGE)
    assert "    " + JUDGE in out
