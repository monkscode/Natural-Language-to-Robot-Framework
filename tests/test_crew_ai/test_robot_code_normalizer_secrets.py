"""Tests for rewrite_secret_fills / restore_secret_placeholders (issue #131) and link_secret_fields /
typed_secret_names (issue #166).

A typed `{{NAME}}` becomes `Fill Secret <loc> %NLRF_SECRET_NAME` (the runner injects that env variable);
the repair LLM is handed the `{{NAME}}` form back, never the env name. When the LLM did not keep `{{NAME}}`,
the field the plan typed it into decides: the test's typing step on that field becomes the same Fill Secret line.
"""

import logging

import pytest

from src.backend.crew_ai.robot_code_normalizer import (
    link_secret_fields,
    restore_secret_placeholders,
    rewrite_secret_fills,
    typed_secret_names,
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


# ─── #166: the plan step's field decides how the secret is typed ─────────────
# G1 / G2: the two live generations of 2026-10-24 that did not keep `{{SAUCE_PASSWORD}}` (runs 97873510, 668d9099),
# exactly as stored. `secret_sauce` is saucedemo's public demo password, the LLM's own guess.
G1 = (
    "*** Settings ***\nLibrary    Browser    timeout=30s\nLibrary    BuiltIn\nLibrary    Collections\n\n"
    "*** Variables ***\n${browser}    chromium\n${headless}    True\n${url}    https://www.saucedemo.com\n"
    "${user_name_locator}    id=user-name\n${password_locator}    input[placeholder='Password']\n"
    "${login_button_locator}    id=login-button\n${products_title_locator}    css=.title\n"
    "${username}    standard_user\n${password}    secret_sauce\n\n"
    "*** Test Cases ***\nSauce Demo Login Test\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    ${url}\n"
    "    Fill Text    ${user_name_locator}    ${username}\n"
    "    Fill Text    ${password_locator}    ${password}\n"
    "    Click    ${login_button_locator}\n"
    "    Wait For Elements State    ${products_title_locator}    visible\n"
    "    ${heading_text}=    Get Text    ${products_title_locator}\n"
    "    Should Match Regexp    ${heading_text}    (?i)(?<![0-9a-z_])Products(?![0-9a-z_])    "
    "msg='${heading_text}' does not contain 'Products' (whole words, any letter case)    values=False\n"
    "    Close Browser"
)
G1_FIELDS = (("id=user-name", None), ("input[placeholder='Password']", "SAUCE_PASSWORD"))
G2 = (
    "*** Settings ***\nLibrary    Browser    timeout=30s\nLibrary    BuiltIn\nLibrary    Collections\n\n"
    "*** Variables ***\n${browser}                 chromium\n${headless}                True\n"
    "${url}                     https://www.saucedemo.com\n${user_name_locator}       id=user-name\n"
    "${password_locator}        id=password\n${login_button_locator}    id=login-button\n"
    "${title_heading_locator}   [data-test='title']\n\n"
    "*** Test Cases ***\nSauce Demo Login Test\n"
    "    New Browser    ${browser}    headless=${headless}\n"
    "    New Context    viewport={'width': 1920, 'height': 1080}\n"
    "    New Page    ${url}\n"
    "    Fill Text    ${user_name_locator}    standard_user\n"
    "    Fill Secret    ${password_locator}    %SAUCE_PASSWORD\n"
    "    Click    ${login_button_locator}\n"
    "    Wait For Elements State    ${title_heading_locator}    visible\n"
    "    ${heading_text}=    Get Text    ${title_heading_locator}\n"
    "    Should Match Regexp    ${heading_text}    (?i)(?<![0-9a-z_])Products(?![0-9a-z_])    "
    "msg='${heading_text}' does not contain 'Products' (whole words, any letter case)    values=False"
)
G2_FIELDS = (("id=user-name", None), ("id=password", "SAUCE_PASSWORD"))
_ENV = "%NLRF_SECRET_"

# (label, variables, steps, fields, present, absent)
LINKED = [
    ("two secrets: user and password", "${u}    alice",
     ["Fill Text    id=u    ${u}", "Fill Text    id=p    hunter2"], (("id=u", "USER"), ("id=p", "PW")),
     ["    Fill Secret    id=u    %NLRF_SECRET_USER\n", "    Fill Secret    id=p    %NLRF_SECRET_PW\n"],
     ["alice", "hunter2", "${u}"]),
    ("wrong, then the secret, on one field", "",
     ["Fill Text    id=p    wrong", "Click    id=go", "Fill Text    id=p    guess"], (("id=p", None), ("id=p", "PW")),
     ["    Fill Text    id=p    wrong\n", "    Fill Secret    id=p    %NLRF_SECRET_PW\n"], ["guess"]),
    ("another line reads the variable: its row becomes {{NAME}}", "${pw}    guess",
     ["Fill Text    id=p    ${pw}", "Fill Text    id=c    ${pw}"], (("id=p", "PW"), ("id=c", None)),
     ["${pw}    {{PW}}\n", "    Fill Secret    id=p    %NLRF_SECRET_PW\n", "    Fill Text    id=c    ${pw}\n"],
     ["guess"]),
    ("a multi-line row is left, the step is rewritten", "${pw}    gu\n...    ess",
     ["Fill Text    id=p    ${pw}"], (("id=p", "PW"),),
     ["...    ess\n", "    Fill Secret    id=p    %NLRF_SECRET_PW\n"], []),
    ("a row and a Set Variable: both definitions left", "${pw}    a",
     ["${pw}=    Set Variable    guess", "Fill Text    id=p    ${pw}"], (("id=p", "PW"),),
     ["${pw}    a\n", "Set Variable    guess\n", "    Fill Secret    id=p    %NLRF_SECRET_PW\n"], []),
    ("a variable another keyword assigns: its definitions are left", "${pw}    a",
     ["${pw}=    Get Text    id=x", "Fill Text    id=p    ${pw}"], (("id=p", "PW"),),
     ["${pw}    a\n", "    ${pw}=    Get Text    id=x\n", "    Fill Secret    id=p    %NLRF_SECRET_PW\n"], []),
    ("a lone unread Set Variable is deleted", "",
     ["${pw}=    Set Variable    guess", "Fill Text    id=p    ${pw}"], (("id=p", "PW"),),
     ["    Fill Secret    id=p    %NLRF_SECRET_PW\n"], ["guess", "Set Variable"]),
    ("Type Text keeps delay and clear", "",
     ["Type Text    id=p    guess    delay=50ms    clear=False"], (("id=p", "PW"),),
     ["    Type Secret    id=p    %NLRF_SECRET_PW    delay=50ms    clear=False\n"], ["guess"]),
    ("css=#id and a bare #id name one field", "${loc}    css=#p",
     ["Fill Text    ${loc}    guess"], (("#p", "PW"),),
     ["    Fill Secret    ${loc}    %NLRF_SECRET_PW\n"], ["guess"]),
    ("id=x and css=#x name one field", "",
     ["Fill Text    css=#p    guess"], (("id=p", "PW"),),
     ["    Fill Secret    css=#p    %NLRF_SECRET_PW\n"], ["guess"]),
    ("a frame locator", "",
     ['Fill Text    iframe[id="f"] >>> id=p    guess'], (('iframe[id="f"] >>> id=p', "PW"),),
     ['    Fill Secret    iframe[id="f"] >>> id=p    %NLRF_SECRET_PW\n'], ["guess"]),
    ("a loop test with a literal locator", "",
     ["Fill Text    id=p    guess", "FOR    ${i}    IN RANGE    2", "    Log    ${i}", "END"], (("id=p", "PW"),),
     ["    Fill Secret    id=p    %NLRF_SECRET_PW\n"], ["guess"]),
    ("the Browser. prefix is kept", "",
     ["Browser.Fill Text    id=p    guess"], (("id=p", "PW"),),
     ["    Browser.Fill Secret    id=p    %NLRF_SECRET_PW\n"], ["guess"]),
    ("Fill Secret $pw: its unread row is deleted", "${pw}    guess",
     ["Fill Secret    id=p    $pw"], (("id=p", "PW"),),
     ["    Fill Secret    id=p    %NLRF_SECRET_PW\n"], ["guess", "${pw}"]),
    ("a wrong env name on the field", "",
     ["Fill Secret    id=p    %PW"], (("id=p", "PW"),),
     ["    Fill Secret    id=p    %NLRF_SECRET_PW\n"], ["%PW\n"]),
]

# (label, variables, steps, fields): the file comes back unchanged
UNTOUCHED = [
    ("no fields", "", ["Fill Text    id=p    guess"], ()),
    ("fields without a secret", "", ["Fill Text    id=p    guess"], (("id=p", None),)),
    ("the test types the field more often than the plan", "",
     ["Fill Text    id=p    ${EMPTY}", "Fill Text    id=p    guess"], (("id=p", "PW"),)),
    ("the test never types the field", "", ["Click    id=p"], (("id=p", "PW"),)),
    ("a loop test with a ${var} locator", "${loc}    id=p",
     ["Fill Text    ${loc}    guess", "FOR    ${i}    IN RANGE    2", "    Log    ${i}", "END"], (("id=p", "PW"),)),
    ("bs found no element: the LLM's placeholder locator", "",
     ["Fill Text    xpath=//PLACEHOLDER_FOR_elem_2    guess"], ()),
    ("a hand-written Fill Secret on another field", "",
     ["Fill Secret    id=other    %OTHER_ENV"], (("id=p", "PW"),)),
    ("already linked", "", ["Fill Secret    id=p    %NLRF_SECRET_PW"], (("id=p", "PW"),)),
]


def _linked(variables, steps, fields):
    return link_secret_fields(_suite(*steps, variables=variables), fields)


class TestLinkSecretFields:
    def test_g1_types_the_secret_and_drops_the_guessed_password(self):
        out = link_secret_fields(G1, G1_FIELDS)
        assert "    Fill Secret    ${password_locator}    %NLRF_SECRET_SAUCE_PASSWORD\n" in out
        assert "secret_sauce" not in out
        assert "${password}" not in out
        assert "    Fill Text    ${user_name_locator}    ${username}\n" in out  # the username is not a secret
        assert out.count("\n") == G1.count("\n") - 1  # exactly the one row removed

    def test_g2_gets_the_env_name_the_runner_injects(self):
        out = link_secret_fields(G2, G2_FIELDS)
        assert "    Fill Secret    ${password_locator}    %NLRF_SECRET_SAUCE_PASSWORD\n" in out
        assert "%SAUCE_PASSWORD" not in out
        assert out.replace("%NLRF_SECRET_SAUCE_PASSWORD", "%SAUCE_PASSWORD") == G2  # nothing else moved

    def test_g1_without_its_fields_is_left_as_today(self):
        assert link_secret_fields(G1, ()) is G1
        assert "secret_sauce" in rewrite_secret_fills(G1)  # control: today's rewrite misses G1

    @pytest.mark.parametrize("code, fields", [(G1, G1_FIELDS), (G2, G2_FIELDS)], ids=["G1", "G2"])
    def test_the_repair_llm_sees_the_placeholder_and_a_repair_round_links_again(self, code, fields):
        linked = link_secret_fields(code, fields)
        restored = restore_secret_placeholders(linked)
        assert "{{SAUCE_PASSWORD}}" in restored
        assert _ENV not in restored
        assert link_secret_fields(restored, fields) == linked

    @pytest.mark.parametrize("label, variables, steps, fields, present, absent", LINKED, ids=[c[0] for c in LINKED])
    def test_linked(self, label, variables, steps, fields, present, absent):
        out = _linked(variables, steps, fields)
        for text in present:
            assert text in out, text
        for text in absent:
            assert text not in out, text
        assert link_secret_fields(out, fields) == out  # idempotent

    @pytest.mark.parametrize("label, variables, steps, fields", UNTOUCHED, ids=[c[0] for c in UNTOUCHED])
    def test_untouched(self, label, variables, steps, fields):
        code = _suite(*steps, variables=variables)
        assert link_secret_fields(code, fields) is code

    def test_a_typing_step_inside_a_user_keyword_is_not_linked(self):
        code = _suite("Login") + "\n*** Keywords ***\nLogin\n    Fill Text    id=p    guess\n"
        assert link_secret_fields(code, (("id=p", "PW"),)) is code

    def test_an_unreadable_suite_is_returned_unchanged(self):
        code = "*** Test Cases ***\n| T |\n| | Fill Text | id=p | guess |\n"
        assert link_secret_fields(code, (("id=p", "PW"),)) is code

    def test_line_endings_survive(self):
        crlf = _suite("Fill Text    id=p    ${pw}", variables="${pw}    guess").replace("\n", "\r\n")
        out = link_secret_fields(crlf, (("id=p", "PW"),))
        assert "    Fill Secret    id=p    %NLRF_SECRET_PW\r\n" in out
        assert "guess" not in out
        assert out.count("\r\n") == out.count("\n")

    def test_the_link_logs_a_count_never_a_value(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.robot_code_normalizer"):
            link_secret_fields(G1, G1_FIELDS)
        assert "signal: secret-field-linked" in caplog.text
        assert "secret_sauce" not in caplog.text


class TestTypedSecretNames:
    def test_names_typed_by_a_secret_keyword(self):
        code = _suite("Fill Secret    id=a    %NLRF_SECRET_A", "Type Secret    id=b    ${b}",
                      variables="${b}    %NLRF_SECRET_B")
        assert typed_secret_names(code) == {"A", "B"}

    def test_forms_that_do_not_type_the_secret_are_not_names(self):
        # Browser 19.14.2 types these literally or refuses them (bench/private/issue166_eval/RESULT.md §1)
        code = _suite("Fill Text    id=a    %NLRF_SECRET_A", "Fill Secret    id=b    %{NLRF_SECRET_B}",
                      "Fill Secret    id=c    $c", "Log    %NLRF_SECRET_D", variables="${c}    %NLRF_SECRET_C")
        assert typed_secret_names(code) == set()
