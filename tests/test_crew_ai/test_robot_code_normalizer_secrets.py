"""Tests for rewrite_secret_fills / restore_secret_placeholders (issue #131).

A typed `{{NAME}}` becomes `Fill Secret <loc> %NLRF_SECRET_NAME` (the runner injects that env variable);
the repair LLM is handed the `{{NAME}}` form back, never the env name.
"""

import pytest

from src.backend.crew_ai.robot_code_normalizer import (
    restore_secret_placeholders,
    rewrite_secret_fills,
)

_HEAD = ("*** Settings ***\nLibrary    Browser    timeout=30s\n\n")
_BODY_OPEN = ("*** Test Cases ***\nLogin\n"
              "    New Browser    chromium    headless=True\n"
              "    New Page    https://www.saucedemo.com\n")


def _suite(*steps: str, variables: str = "") -> str:
    head = _HEAD + (f"*** Variables ***\n{variables}\n\n" if variables else "")
    return head + _BODY_OPEN + "".join(f"    {s}\n" for s in steps) + "    Close Browser\n"


def _fill(keyword: str, value: str) -> str:
    return _suite("Fill Text    id=user-name    standard_user", f"{keyword}    id=password    {value}")


Q07 = _fill("Fill Text", "{{SAUCE_PASSWORD}}")

# (code, name) pairs already in canonical form: exact round trip holds.
CANONICAL = [
    pytest.param(Q07, id="fill text"),
    pytest.param(_fill("Type Text", "{{SAUCE_PASSWORD}}"), id="type text"),
    pytest.param(_fill("Browser.Fill Text", "{{SAUCE_PASSWORD}}"), id="browser prefix"),
    pytest.param(_fill("Browser.Type Text", "{{SAUCE_PASSWORD}}"), id="browser prefix type"),
    pytest.param(_fill("Fill Text", "{{A}}"), id="one letter name"),
]
# Every shape the rewrite touches, including those that cannot round-trip exactly.
TOUCHED = CANONICAL + [
    pytest.param(_fill("Fill Text", "{{ sauce_password }}"), id="lower case, inner spaces"),
    pytest.param(_suite("Fill Text    id=password    ${PW}", variables="${PW}    {{PW}}"), id="variables row"),
]


class TestRewrite:
    def test_q07_shape_becomes_fill_secret_with_the_env_name(self):
        out = rewrite_secret_fills(Q07)
        assert "    Fill Secret    id=password    %NLRF_SECRET_SAUCE_PASSWORD\n" in out
        assert "{{" not in out
        assert "    Fill Text    id=user-name    standard_user\n" in out  # the username is not a secret

    def test_type_text_becomes_type_secret(self):
        out = rewrite_secret_fills(_fill("Type Text", "{{SAUCE_PASSWORD}}"))
        assert "    Type Secret    id=password    %NLRF_SECRET_SAUCE_PASSWORD\n" in out

    def test_any_letter_case_and_inner_spaces_give_the_upper_case_name(self):
        out = rewrite_secret_fills(_fill("Fill Text", "{{ sauce_password }}"))
        assert "    Fill Secret    id=password    %NLRF_SECRET_SAUCE_PASSWORD\n" in out

    def test_a_placeholder_held_in_a_variable_rewrites_the_step_and_leaves_the_row(self):
        code = _suite("Fill Text    id=password    ${PW}", variables="${PW}    {{PW}}")
        out = rewrite_secret_fills(code)
        assert "    Fill Secret    id=password    %NLRF_SECRET_PW\n" in out
        assert "${PW}    {{PW}}\n" in out

    def test_the_browser_prefix_is_kept(self):
        out = rewrite_secret_fills(_fill("Browser.Fill Text", "{{SAUCE_PASSWORD}}"))
        assert "    Browser.Fill Secret    id=password    %NLRF_SECRET_SAUCE_PASSWORD\n" in out

    def test_a_check_line_holding_a_placeholder_is_untouched(self):
        code = _suite("Fill Text    id=user-name    standard_user", "Should Contain    ${msg}    {{X}}")
        assert rewrite_secret_fills(code) == code

    def test_a_placeholder_inside_a_longer_text_is_untouched(self):
        for value in ("pre-{{X}}", "{{X}}-post", "{{X}} {{Y}}", "{{1X}}", "{{}}", "{{" + "A" * 65 + "}}"):
            code = _fill("Fill Text", value)
            assert rewrite_secret_fills(code) == code, value

    def test_a_64_character_name_is_rewritten(self):
        name = "A" * 64
        assert f"%NLRF_SECRET_{name}\n" in rewrite_secret_fills(_fill("Fill Text", "{{" + name + "}}"))

    @pytest.mark.parametrize("code", [pytest.param(c.values[0], id=c.id) for c in TOUCHED])
    def test_idempotent(self, code):
        once = rewrite_secret_fills(code)
        assert once != code
        assert rewrite_secret_fills(once) == once

    def test_code_without_a_placeholder_is_returned_as_is(self):
        code = _fill("Fill Text", "pw_marker_131")
        assert rewrite_secret_fills(code) is code
        assert rewrite_secret_fills("") == ""

    def test_an_unreadable_suite_is_returned_unchanged(self):
        code = "*** Test Cases ***\n| T |\n| | Fill Text | id=password | {{X}} |\n"
        assert rewrite_secret_fills(code) == code

    def test_a_test_with_an_unrelated_if_block_is_still_rewritten(self):
        code = _suite("IF    True", "    Log    x", "END", "Fill Text    id=password    {{PW}}")
        out = rewrite_secret_fills(code)
        assert "    Fill Secret    id=password    %NLRF_SECRET_PW\n" in out
        assert out.count("{{") == 0

    def test_a_fill_inside_a_for_body_with_a_literal_placeholder_is_rewritten(self):
        code = _suite("FOR    ${i}    IN RANGE    2", "    Fill Text    id=password    {{PW}}", "END")
        out = rewrite_secret_fills(code)
        assert "        Fill Secret    id=password    %NLRF_SECRET_PW\n" in out or \
            "    Fill Secret    id=password    %NLRF_SECRET_PW\n" in out
        assert "{{" not in out

    def test_an_unfollowable_test_with_a_variable_argument_is_left_alone(self):
        code = _suite("IF    True", "    Log    x", "END", "Fill Text    id=password    ${PW}",
                      variables="${PW}    {{PW}}")
        assert rewrite_secret_fills(code) == code

    def test_a_template_test_data_row_is_left_alone(self):
        code = _suite("[Template]    Fill Text", "id=password    {{PW}}", "Fill Text    id=password    {{PW}}")
        assert rewrite_secret_fills(code) == code

    @pytest.mark.parametrize("code", [
        _suite("IF    True", "    Log    x", "END", "Fill Text    id=password    {{PW}}"),
        _suite("FOR    ${i}    IN RANGE    2", "    Fill Text    id=password    {{PW}}", "END"),
    ], ids=["if block", "for body"])
    def test_unfollowable_rewrites_restore_and_stay_stable(self, code):
        rewritten = rewrite_secret_fills(code)
        assert rewritten != code
        restored = restore_secret_placeholders(rewritten)
        assert "{{PW}}" in restored and "%NLRF_SECRET_" not in restored
        assert rewrite_secret_fills(restored) == rewritten

    def test_line_endings_survive(self):
        crlf = Q07.replace("\n", "\r\n")
        out = rewrite_secret_fills(crlf)
        assert "    Fill Secret    id=password    %NLRF_SECRET_SAUCE_PASSWORD\r\n" in out
        assert out.count("\r\n") == out.count("\n")


class TestRestore:
    @pytest.mark.parametrize("code", CANONICAL)
    def test_exact_round_trip_for_canonical_input(self, code):
        rewritten = rewrite_secret_fills(code)
        assert rewritten != code
        assert restore_secret_placeholders(rewritten) == code

    @pytest.mark.parametrize("code", TOUCHED)
    def test_every_case_returns_the_placeholder_form_and_stays_stable(self, code):
        rewritten = rewrite_secret_fills(code)
        restored = restore_secret_placeholders(rewritten)
        assert "%NLRF_SECRET_" not in restored
        for line in rewritten.split("\n"):
            if "%NLRF_SECRET_" in line:
                name = line.rsplit("%NLRF_SECRET_", 1)[1].strip()
                assert "{{" + name + "}}" in restored
        assert rewrite_secret_fills(restored) == rewritten

    def test_a_hand_written_secret_line_is_not_ours(self):
        for value in ("$x", "%OTHER_ENV", "%NLRF_SECRET_lower", "%NLRF_SECRET_", "%NLRF_SECRET_A B"):
            code = _suite(f"Fill Secret    id=password    {value}")
            assert restore_secret_placeholders(code) == code, value

    def test_only_secret_keywords_are_restored(self):
        code = _suite("Fill Text    id=password    %NLRF_SECRET_X", "Log    %NLRF_SECRET_X")
        assert restore_secret_placeholders(code) == code

    def test_the_prefix_is_kept(self):
        code = _suite("Browser.Type Secret    id=password    %NLRF_SECRET_X")
        assert restore_secret_placeholders(code) == _suite("Browser.Type Text    id=password    {{X}}")

    def test_no_env_name_means_the_input_object_back(self):
        assert restore_secret_placeholders(Q07) is Q07
        assert restore_secret_placeholders("") == ""

    def test_an_unreadable_suite_is_returned_unchanged(self):
        code = "*** Test Cases ***\n| T |\n| | Fill Secret | id=password | %NLRF_SECRET_X |\n"
        assert restore_secret_placeholders(code) == code
