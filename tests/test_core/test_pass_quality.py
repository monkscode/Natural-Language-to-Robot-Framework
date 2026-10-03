"""Tests for src/backend/core/pass_quality.py — the static hollow-pass checker.

Every snippet is hand-written to reproduce one captured bench shape (the run it
mirrors is named); no bench data is committed. The corpus replay that shows the
same verdicts on the real captures is bench/private/gate/pass_quality_corpus_replay.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import src.backend.core.pass_quality as pass_quality
from src.backend.core.pass_quality import (
    EMPTY_TEST,
    FRAGILE_NUMERIC_ID,
    READ_LOCATOR_IS_THE_ANSWER,
    READ_NOT_PERFORMED,
    SELECT_CHECK_CANNOT_FAIL,
    VERIFY_WITHOUT_ASSERTION,
    Finding,
    check_pass_quality,
    hollow_shapes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

Q01 = "Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project"
Q04 = ('Go to https://the-internet.herokuapp.com/tables and click on the first table "edit" link '
       'in the row containing "Smith"')
Q05 = ("Go to https://the-internet.herokuapp.com/tables and get the text from the first row, "
       "second column of Table 1")
Q07 = ("Go to https://www.saucedemo.com, type standard_user in the username field, type "
       "secret_sauce in the password field, click the Login button, and verify the products "
       "page shows the Products heading")
Q08 = ("Go to https://the-internet.herokuapp.com/dropdown, select Option 2 from the dropdown, "
       "and verify Option 2 is selected")
Q09 = ("Go to https://www.w3schools.com/tags/tryit.asp?filename=tryhtml5_global_contenteditable, "
       "type Hello Bench into the editable paragraph inside the iframe, and verify the paragraph "
       "contains Hello Bench")
Q10 = ("Go to https://books.toscrape.com, get the titles of all books on the first page, and "
       "verify there are 20 books")


def robot(variables: str, body: str, extra: str = "") -> str:
    """A generated-test-shaped file: the assembler's fixed header, the given body, Close Browser."""
    return ("*** Settings ***\nLibrary    Browser\n\n*** Variables ***\n"
            "${browser}    chromium\n${headless}    True\n" + variables
            + "\n*** Test Cases ***\nGenerated Test\n"
            "    [Documentation]    Auto-generated test case\n"
            "    New Browser    ${browser}    headless=${headless}\n"
            "    New Context    viewport={'width': 1920, 'height': 1080}\n"
            + body + "    Close Browser\n" + extra)


def shapes(code: str, query: str | None) -> set[str]:
    return {f.shape for f in check_pass_quality(code, query)}


GET_ELEMENTS = "    @{elements}=    Get Elements    ${books_locator}\n"
GET_LENGTH = GET_ELEMENTS + "    ${elements_count}=    Get Length    ${elements}\n"
GET_COUNT = "    ${count}=    Get Element Count    ${books_locator}\n"


def books(body: str, variables: str = "") -> str:
    """The books page opened, then `body` (q10's generated shape)."""
    return robot("${url}    https://books.toscrape.com\n${books_locator}    ol > li\n" + variables,
                 "    New Page    ${url}\n" + body)


class TestReadNotPerformed:
    @pytest.mark.parametrize("body, variables", [
        # mirrors 041606ee-dd82-4dad-9e4b-858f6081b61d (118 of the 124 stored count-only tests)
        pytest.param(GET_LENGTH + "    Should Be True    ${elements_count} == 20\n", "", id="get-length"),
        # mirrors 04dd81c7-6a94-4e6e-8b48-7b75e4652fb1
        pytest.param(GET_ELEMENTS + GET_COUNT + "    Should Be Equal As Integers    ${count}    20\n", "",
                     id="count-as-integers"),
        # mirrors 3a563d81-737c-4888-ac49-4db024a471aa
        pytest.param("    @{books}=    Get Elements    ${books_locator}\n"
                     "    Should Be True    len($books) == 20\n", "", id="python-len"),
        # mirrors 4bab7a83-790c-41c6-a9d4-1146ad2a3dd0: the list variable is also declared, empty
        pytest.param("    ${titles}=    Get Elements    ${books_locator}\n"
                     "    Should Be True    len(${titles}) == 20\n", "${titles}\n", id="declared-list"),
        pytest.param("    Get Element Count    ${books_locator}    ==    20\n", "", id="inline-operator"),
        pytest.param(GET_ELEMENTS + "    Length Should Be    ${elements}    20\n", "", id="length-should-be"),
        pytest.param(GET_COUNT + "    Should Be Equal    ${count}    ${20}\n", "", id="integer-literal"),
        pytest.param(GET_COUNT + "    Should Be Equal As Integers    ${count}    ${expected_count}\n",
                     "${expected_count}    20\n", id="number-in-a-variable"),
        pytest.param(GET_ELEMENTS + "    ${n}=    Evaluate    len($elements)\n"
                     "    Should Be Equal As Integers    ${n}    20\n", "", id="count-through-evaluate"),
    ])
    def test_a_count_request_whose_count_is_asserted_is_not_flagged(self, body, variables):
        assert shapes(books(body, variables), Q10) == set()

    @pytest.mark.parametrize("body, expected", [
        pytest.param(GET_LENGTH + "    Should Be True    ${elements_count} == 10\n",
                     {READ_NOT_PERFORMED}, id="another-number"),
        pytest.param(GET_LENGTH + "    Should Be True    ${elements_count} == 120\n",
                     {READ_NOT_PERFORMED}, id="inside-a-longer-number"),
        pytest.param(GET_LENGTH + "    Should Be True    ${elements_count} > 20.5\n",
                     {READ_NOT_PERFORMED}, id="inside-a-decimal"),
        pytest.param(GET_LENGTH + "    Log    ${elements_count}\n",
                     {READ_NOT_PERFORMED, VERIFY_WITHOUT_ASSERTION}, id="count-only-logged"),
        # the assertion that holds 20 is about an element, not about the count
        pytest.param(GET_COUNT + "    Log    ${count}\n"
                     "    Wait For Elements State    xpath=(//article)[20]    visible    timeout=10s\n",
                     {READ_NOT_PERFORMED}, id="number-in-an-unrelated-assertion"),
        pytest.param("    Get Element Count    css=li:nth-child(20)    >    0\n",
                     {READ_NOT_PERFORMED}, id="number-in-the-counted-locator"),
        pytest.param("    ${n}=    Evaluate JavaScript    ${books_locator}    (all) => all.length"
                     "    all_elements=True\n    Should Be Equal As Integers    ${n}    20\n",
                     {READ_NOT_PERFORMED}, id="no-counting-keyword"),
        pytest.param(GET_COUNT + "    Log    Found ${count} books, expected 20\n",
                     {READ_NOT_PERFORMED, VERIFY_WITHOUT_ASSERTION}, id="number-only-in-a-log-message"),
    ])
    def test_a_count_request_whose_count_is_not_asserted_is_still_flagged(self, body, expected):
        assert shapes(books(body), Q10) == expected

    @pytest.mark.parametrize("query", [
        pytest.param(Q10.replace("20 books", "twenty books"), id="a-number-word"),
        pytest.param(Q10.replace("20 books", "20,000 books"), id="a-longer-number"),
        pytest.param("Go to https://books.toscrape.com, verify the page is open and get the titles of "
                     "the 20 books", id="the-number-is-in-another-clause"),
        pytest.param("Go to https://books.toscrape.com and get the titles of the 20 books",
                     id="no-verify-clause"),
    ])
    def test_only_a_number_the_query_asks_to_verify_waives_the_read(self, query):
        code = books(GET_LENGTH + "    Should Be True    ${elements_count} == 20\n")
        assert shapes(code, query) == {READ_NOT_PERFORMED}

    BASKET_COUNT = ("    ${count}=    Get Element Count    ${basket_locator}\n"
                    "    Should Be Equal As Integers    ${count}    1\n")

    @pytest.mark.parametrize("query, body", [
        pytest.param("Go to https://books.toscrape.com, get the price of the first book and verify "
                     "there is 1 item in the basket", BASKET_COUNT, id="one-book-asked-for"),
        pytest.param("Go to https://the-internet.herokuapp.com/checkboxes, check checkbox 2 and get its label",
                     "    Click    css=#checkboxes input >> nth=1\n"
                     "    ${count}=    Get Element Count    ${boxes_locator}\n"
                     "    Should Be Equal As Integers    ${count}    2\n", id="check-is-the-verify-word"),
        pytest.param("Go to https://books.toscrape.com, accept all cookies, get the price of the first book "
                     "and verify there is 1 item in the basket", BASKET_COUNT,
                     id="collection-word-in-another-clause"),
        pytest.param("Go to https://books.toscrape.com, get the price of the small book and verify there "
                     "is 1 item in the basket", BASKET_COUNT, id="word-that-only-contains-all"),
    ])
    def test_a_count_waives_the_read_only_for_a_request_for_a_whole_collection(self, query, body):
        assert shapes(robot("", "    New Page    https://books.toscrape.com\n" + body), query) == {
            READ_NOT_PERFORMED}

    @pytest.mark.parametrize("word", ["every", "each"])
    def test_every_and_each_ask_for_a_whole_collection_like_all(self, word):
        query = Q10.replace("titles of all books", f"titles of {word} book")
        code = books(GET_LENGTH + "    Should Be True    ${elements_count} == 20\n")
        assert shapes(code, query) == set()

    def test_a_declared_start_value_is_not_the_number_compared(self):
        # ${error_count} is declared as 0 and then assigned at run time: `< 5` does not check 0
        code = robot("${error_count}    0\n",
                     "    New Page    https://shop.example.com\n"
                     "    ${error_count}=    Get Element Count    css=.error\n"
                     "    Should Be True    ${error_count} < 5\n")
        query = "Open https://shop.example.com, get the error messages and verify there are 0 errors"
        assert shapes(code, query) == {READ_NOT_PERFORMED}

    def test_the_same_request_with_no_count_and_no_assertion_is_still_flagged(self):
        # mirrors d6965269-4143-43c4-bb8d-5597690c2c6c: the page is opened, nothing else
        assert shapes(books(""), Q10) == {READ_NOT_PERFORMED, VERIFY_WITHOUT_ASSERTION, EMPTY_TEST}

    def test_a_read_request_with_no_reader_is_still_flagged(self):
        # mirrors 5b33729e-a6ca-496e-86e4-9c425da5439d and 474a0f7c-5ea7-406f-a723-d9e98e05916d
        opened = robot("", "    New Page    https://example.com/\n")
        assert shapes(opened, "Open https://example.com and get the page title") == {
            READ_NOT_PERFORMED, EMPTY_TEST}
        assert shapes(robot("", ""), Q01) == {READ_NOT_PERFORMED, EMPTY_TEST}

    def test_titles_read_in_a_loop_is_not_flagged(self):  # mirrors 34b47637-f785-4a41-9b2a-f6e517f4070a
        code = robot("${url}    https://books.toscrape.com\n${book_titles_locator}    css=h3 a\n",
                     "    New Page    ${url}\n"
                     "    @{books}=    Get Elements    ${book_titles_locator}\n"
                     "    FOR    ${element}    IN    @{books}\n"
                     "        ${text}=    Get Text    ${element}\n"
                     "        Log    Book Title: ${text}\n"
                     "    END\n"
                     "    ${books_count}=    Get Length    ${books}\n"
                     "    Should Be True    ${books_count} == 20\n")
        assert shapes(code, Q10) == set()

    def test_get_title_is_a_read(self):  # first-cut false positive (live execution_records id 126)
        code = robot("", "    New Page    https://example.com\n    ${title}=    Get Title\n")
        assert READ_NOT_PERFORMED not in shapes(code, "Open https://example.com and get the page title")

    def test_get_element_count_is_not_a_read(self):
        code = robot("", "    New Page    https://books.toscrape.com\n"
                         "    Get Element Count    css=h3 a    ==    20\n")
        query = "Go to https://books.toscrape.com and get the titles of all books on the first page"
        assert shapes(code, query) == {READ_NOT_PERFORMED}

    def test_a_query_without_a_read_verb_is_never_flagged(self):
        code = robot("", "    New Page    https://example.com\n    Click    id=go\n")
        assert READ_NOT_PERFORMED not in shapes(code, "Open https://example.com and click Go, target the button")


class TestSelectCheckCannotFail:
    SELECT_VARS = ("${url}    https://the-internet.herokuapp.com/dropdown\n"
                   "${dropdown_trigger}    id=dropdown\n${dropdown_verification}    css=#dropdown\n"
                   "${selected_option}    \n")

    def test_get_text_on_the_whole_select_is_flagged(self):  # mirrors 7c4811db-3a72-4791-b22d-476f6e631507
        code = robot(self.SELECT_VARS,
                     "    New Page    ${url}\n"
                     "    Select Options By    ${dropdown_trigger}    label    Option 2\n"
                     "    ${selected_option}=    Get Text    ${dropdown_verification}\n"
                     "    Should Contain    ${selected_option}    Option 2\n")
        found = check_pass_quality(code, Q08)
        assert [f.shape for f in found] == [SELECT_CHECK_CANNOT_FAIL]
        assert found[0].detail == "id=dropdown"

    def test_get_selected_options_is_not_flagged(self):  # mirrors 8e37b6e1-6450-4c80-888a-c7f56bbc4738
        code = robot("${dropdown_locator}    id=dropdown\n",
                     "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                     "    Select Options By    ${dropdown_locator}    label    Option 2\n"
                     "    ${selected_options}=    Get Selected Options    ${dropdown_locator}\n"
                     "    Should Be Equal    ${selected_options}[0]    Option 2\n")
        assert shapes(code, Q08) == set()

    def test_the_inline_contains_form_is_flagged(self):
        code = robot("", "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                         "    Select Options By    id=dropdown    label    Option 2\n"
                         "    Get Text    css=#dropdown    contains    Option 2\n")
        assert shapes(code, Q08) == {SELECT_CHECK_CANNOT_FAIL}

    def test_reading_another_element_is_not_flagged(self):
        code = robot("", "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                         "    Select Options By    id=dropdown    label    Option 2\n"
                         "    ${msg}=    Get Text    id=result\n"
                         "    Should Contain    ${msg}    Option 2\n")
        assert shapes(code, Q08) == set()

    def test_a_presence_check_of_another_option_is_not_flagged(self):
        code = robot("", "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                         "    Select Options By    id=dropdown    label    Option 2\n"
                         "    ${all}=    Get Text    id=dropdown\n"
                         "    Should Contain    ${all}    Option 1\n")
        query = "Go to the dropdown page, select Option 2, and verify Option 1 is still listed"
        assert shapes(code, query) == set()


class TestReadLocatorIsTheAnswer:
    def test_text_literal_not_in_the_query_is_flagged(self):  # mirrors eb45684f-3d1b-42df-801e-279408398660
        code = robot("${cell_row_1_col_2_locator}    text=\"John\" >> nth=0\n",
                     "    New Page    https://the-internet.herokuapp.com/tables\n"
                     "    ${first_row_second_col_text}=    Get Text    ${cell_row_1_col_2_locator}\n"
                     "    Log    Retrieved: ${first_row_second_col_text}\n")
        found = check_pass_quality(code, Q05)
        assert [f.shape for f in found] == [READ_LOCATOR_IS_THE_ANSWER]
        assert found[0].detail == "John"

    def test_positional_xpath_is_not_flagged(self):  # mirrors 48f7f6f4-6f6e-4010-847b-496cf437b71d
        code = robot("${cell}    xpath=//table[@id='table1']/tbody/tr[1]/td[2]\n",
                     "    New Page    https://the-internet.herokuapp.com/tables\n"
                     "    ${result_text}=    Get Text    ${cell}\n"
                     "    Log    Retrieved: ${result_text}\n")
        assert shapes(code, Q05) == set()

    def test_a_literal_the_query_names_is_not_flagged(self):
        code = robot("", "    New Page    https://www.saucedemo.com\n"
                         "    ${h}=    Get Text    text=Products\n")
        assert shapes(code, "Log in to saucedemo and get the text of the Products heading") == set()

    def test_a_bare_name_attribute_is_not_a_literal(self):  # first-cut FP, 27d6d46c-5719-4de4-844f-912ed3a7f1cc
        code = robot("${p}    css=iframe[name=\"iframeResult\"] >>> p[contenteditable=\"true\"]\n",
                     "    New Page    https://www.w3schools.com/tags/tryit.asp\n"
                     "    ${t}=    Get Text    ${p}\n")
        assert READ_LOCATOR_IS_THE_ANSWER not in shapes(code, "Open the w3schools page and get the paragraph text")

    def test_only_read_queries_are_checked(self):
        code = robot("", "    New Page    https://the-internet.herokuapp.com/tables\n"
                         "    ${t}=    Get Text    text=\"John\" >> nth=0\n"
                         "    Should Be Equal    ${t}    John\n")
        assert shapes(code, "Go to the tables page and verify the first row shows a name") == set()

    def test_reading_the_literals_own_attribute_is_flagged(self):  # mirrors f40300ac-f9d0-47f6-81e3-f2b95c8a2b8c
        code = robot("${url}        https://books.toscrape.com\n"
                     "${book_title_locator}    [title=\"A Light in the Attic\"]\n",
                     "    New Page    ${url}\n"
                     "    ${titles}=    Get Attribute    ${book_title_locator}    title\n"
                     "    Should Be True    len(${titles}) == 20\n")
        assert check_pass_quality(code, Q10) == [Finding(READ_LOCATOR_IS_THE_ANSWER, "A Light in the Attic")]

    def test_reading_another_attribute_of_a_text_located_element_is_not_flagged(self):
        code = robot("", "    New Page    https://shop.example.com\n"
                         "    ${link}=    Get Attribute    text=\"Nike Air Max\"    href\n")
        assert shapes(code, "Open the shop and get the link of the first product") == set()

    def test_reading_the_value_of_an_aria_label_located_input_is_not_flagged(self):
        code = robot("", "    New Page    https://www.amazon.in\n"
                         "    Fill Text    css=input[aria-label=\"Search Amazon\"]    shoes\n"
                         "    ${v}=    Get Property    css=input[aria-label=\"Search Amazon\"]    value\n")
        query = "Open amazon, type shoes in the search box and get the typed value"
        assert shapes(code, query) == set()

    def test_a_literal_on_an_ancestor_part_of_a_chain_is_not_flagged(self):
        code = robot("", "    New Page    https://the-internet.herokuapp.com/tables\n"
                         "    ${t}=    Get Text    css=table:has-text(\"Last Name\") >> tbody tr >> nth=0"
                         " >> td >> nth=1\n")
        assert shapes(code, Q05) == set()

    def test_a_literal_on_the_frame_part_is_not_flagged(self):
        code = robot("", "    New Page    https://editor.example.com\n"
                         "    ${t}=    Get Text    css=iframe[title=\"Rich Text Area\"] >>> p\n")
        assert shapes(code, "Open the editor page and get the paragraph text") == set()


class TestVerifyWithoutAssertion:
    SAUCE = ("${username_locator}    css=#user-name\n${password_locator}    css=input[type='password']\n"
             "${login_button_locator}    css=#login-button\n${products_heading_locator}    css=span.title\n")
    LOGIN = ("    New Page    https://www.saucedemo.com\n"
             "    Fill Text    ${username_locator}    standard_user\n"
             "    Fill Text    ${password_locator}    secret_sauce\n"
             "    Click    ${login_button_locator}\n")

    def test_get_text_and_log_only_is_flagged(self):  # mirrors 03f1f94c-f1c5-4489-932b-0038d341fe7f
        code = robot(self.SAUCE, self.LOGIN
                     + "    ${products_heading}=    Get Text    ${products_heading_locator}\n"
                     "    Log    Retrieved: ${products_heading}\n")
        assert shapes(code, Q07) == {VERIFY_WITHOUT_ASSERTION}

    def heading_read(self, locator: str, reader: str = "Get Text    ${products_heading_locator}") -> str:
        """The saucedemo login, then the heading read by `locator` and only logged."""
        return robot(self.SAUCE.replace("css=span.title", locator), self.LOGIN
                     + "    ${result}=    " + reader + "\n    Log    Retrieved: ${result}\n")

    @pytest.mark.parametrize("build, query", [
        # mirrors d2762c18-8a5f-4714-b6cc-8b372e76bf53
        pytest.param(lambda t: t.heading_read("text=Products"), Q07,
                     id="text-locator-of-the-word-to-verify"),
        pytest.param(lambda t: t.heading_read('text="PRODUCTS"'), Q07,
                     id="same-text-in-another-case"),
        pytest.param(lambda t: t.heading_read(
            "text=Products", "Get Attribute    ${products_heading_locator}    class"), Q07,
            id="get-attribute-on-that-text"),
        pytest.param(lambda t: t.heading_read("text=Swag Labs"), Q07,
                     id="text-the-query-does-not-hold"),
        pytest.param(lambda t: t.heading_read("text=Login"), Q07,
                     id="text-from-another-clause"),
        pytest.param(lambda t: t.heading_read('css=div:has-text("Products") >> span'), Q07,
                     id="text-on-an-ancestor-part"),
        # Get Element States returns `detached` instead of failing, so it proves nothing
        pytest.param(lambda t: t.heading_read(
            "text=Products", "Get Element States    ${products_heading_locator}"), Q07,
            id="get-element-states"),
        pytest.param(lambda t: robot("${dropdown_locator}    id=dropdown\n",
                                     "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                                     "    Select Options By    ${dropdown_locator}    label    Option 2\n"
                                     "    ${selected_option}=    Get Text    text=Option 2\n"
                                     "    Log    Retrieved: ${selected_option}\n"), Q08,
                     id="state-request-read-by-its-text"),
    ])
    def test_a_read_that_is_only_logged_is_flagged_whatever_its_locator(self, build, query):
        assert shapes(build(self), query) == {VERIFY_WITHOUT_ASSERTION}

    def test_a_typed_value_read_and_only_logged_is_flagged(self):
        # mirrors 41f4f78d-7007-42b8-86c4-ddb28e978472 and 5db3e7ed-a987-44df-8a73-411f01084e4a
        typed = robot('${editable_p_locator}    iframe[id="iframeResult"] >>> xpath=//body/p\n',
                      "    New Page    https://www.w3schools.com/tags/tryit.asp\n"
                      "    Fill Text    ${editable_p_locator}    Hello Bench\n"
                      "    ${retrieved_text}=    Get Text    ${editable_p_locator}\n"
                      "    Log    Retrieved: ${retrieved_text}\n")
        assert shapes(typed, Q09) == {VERIFY_WITHOUT_ASSERTION}
        selected = robot("${dropdown_locator}    id=dropdown\n"
                         "${selected_option_locator}    id=dropdown >> option[selected]\n",
                         "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                         "    Select Options By    ${dropdown_locator}    label    Option 2\n"
                         "    ${selected_option}=    Get Text    ${selected_option_locator}\n"
                         "    Log    Retrieved: ${selected_option}\n")
        assert shapes(selected, Q08) == {VERIFY_WITHOUT_ASSERTION}

    def test_should_contain_is_an_assertion(self):  # mirrors c5adb455-79cf-40cb-a1f1-88f6fbf96354
        code = robot(self.SAUCE, self.LOGIN
                     + "    ${heading_text}=    Get Text    ${products_heading_locator}\n"
                     "    Should Contain    ${heading_text}    Products\n")
        assert shapes(code, Q07) == set()

    def test_an_operator_in_the_first_argument_is_an_assertion(self):
        code = robot(self.SAUCE, self.LOGIN + "    Get Title    ==    Swag Labs\n")
        assert shapes(code, Q07) == set()

    def test_a_should_keyword_anywhere_in_the_name_is_an_assertion(self):
        code = robot("", "    New Page    https://books.toscrape.com\n"
                         "    ${titles}=    Get Texts    css=h3 a\n"
                         "    Length Should Be    ${titles}    20\n")
        assert shapes(code, Q10) == set()

    def test_a_then_expression_is_not_an_assertion(self):
        # `then` returns the evaluated expression and never fails.
        code = robot("", "    New Page    https://example.com\n"
                         "    ${h}=    Get Text    css=h1    then    value.strip()\n"
                         "    Log    ${h}\n")
        assert shapes(code, "Open https://example.com and verify the heading text") == {
            VERIFY_WITHOUT_ASSERTION}

    def test_a_word_operator_is_an_assertion(self):
        code = robot("", "    New Page    https://books.toscrape.com\n"
                         "    Get Element Count    css=h3 a    greater than    0\n")
        assert VERIFY_WITHOUT_ASSERTION not in shapes(code, "Open the page and verify there are books")

    def test_should_start_with_is_an_assertion(self):
        code = robot("", "    New Page    https://example.com\n"
                         "    Get Text    css=h1    should start with    Welcome\n")
        assert shapes(code, "Open the page and verify the heading starts with Welcome") == set()

    def test_operators_match_browsers_assertion_operator_enum(self):
        # Drift guard: the committed libdoc is the authority. `then` and `evaluate`
        # are excluded because they return the evaluated expression and never fail.
        libdoc = json.loads((REPO_ROOT / "data" / "libdocs" / "browser.json").read_text(encoding="utf-8"))
        enum = next(t for t in libdoc["typedocs"] if t["name"] == "AssertionOperator")
        can_fail = {m["name"] for m in enum["members"]} - {"then", "evaluate"}
        assert set(pass_quality._OPERATORS) == can_fail


class TestEmptyTest:
    def test_open_and_close_only_is_flagged(self):  # mirrors f8fc9bd8-1379-42c8-bddb-2268d2295b1a
        code = robot("${url}         https://the-internet.herokuapp.com/tables\n", "    New Page    ${url}\n")
        assert shapes(code, Q04) == {EMPTY_TEST}

    def test_an_open_only_query_is_satisfied_by_opening(self):
        code = robot("", "    New Page    https://example.com\n")
        assert shapes(code, "Open https://example.com") == set()

    def test_pasted_open_only_code_is_flagged(self):
        code = robot("", "    New Page    https://example.com\n")
        assert shapes(code, "") == {EMPTY_TEST}


class TestPastedCode:
    def test_only_query_free_rules_run(self):
        code = robot("${sel}    id=dropdown\n",
                     "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                     "    Select Options By    ${sel}    label    Option 2\n"
                     "    ${x}=    Get Text    ${sel}\n"
                     "    Should Contain    ${x}    Option 2\n"
                     "    ${cell}=    Get Text    text=\"John\" >> nth=0\n"
                     "    ${repo}=    Get Text    id=880667900\n")
        assert shapes(code, "") == {SELECT_CHECK_CANNOT_FAIL}
        assert shapes(code, None) == {SELECT_CHECK_CANNOT_FAIL}


class TestFragileNumericId:
    def test_a_numeric_repository_id_is_reported_not_hollow(self):  # the q01 id=880667900 shape
        code = robot("${pinned_project_locator}    id=880667900\n",
                     "    New Page    https://github.com/monkscode\n"
                     "    ${name}=    Get Text    ${pinned_project_locator}\n"
                     "    Log    ${name}\n")
        found = check_pass_quality(code, Q01)
        assert found == [Finding(FRAGILE_NUMERIC_ID, "id=880667900")]
        assert found[0].hollow is False
        assert hollow_shapes(code, Q01) == set()


class TestRobotSyntax:
    def test_library_prefix_underscores_and_variable_spelling(self):
        code = robot("${Title Locator}    css=h3 a\n",
                     "    New Page    https://books.toscrape.com\n"
                     "    ${t}=    Browser.Get_Text    ${title_locator}\n"
                     "    Should Not Be Empty    ${t}\n")
        assert shapes(code, Q10) == set()

    def test_continuation_and_inline_comment(self):
        code = robot("", "    New Page    https://books.toscrape.com\n"
                         "    ${t}=    Get Text    # the first title\n"
                         "    ...    css=h3 a\n"
                         "    Should Be Equal    ${t}    A Light in the Attic\n")
        assert shapes(code, Q10) == set()

    def test_a_read_inside_a_user_keyword_counts(self):
        extra = "\n*** Keywords ***\nRead Titles\n    ${t}=    Get Texts    css=h3 a\n    RETURN    ${t}\n"
        code = robot("", "    New Page    https://books.toscrape.com\n"
                         "    ${titles}=    Read Titles\n"
                         "    Length Should Be    ${titles}    20\n", extra)
        assert shapes(code, Q10) == set()

    def test_findings_follow_shape_order_one_per_shape(self):
        code = robot("", "    New Page    https://books.toscrape.com\n")
        assert [f.shape for f in check_pass_quality(code, Q10)] == [
            READ_NOT_PERFORMED, VERIFY_WITHOUT_ASSERTION, EMPTY_TEST]


def test_importing_the_checker_loads_no_config_database_or_llm_client():
    probe = ("import sys; import src.backend.core.pass_quality; "
             "bad = [m for m in ('src.backend.core.config', 'psycopg', 'litellm', 'crewai', "
             "'requests', 'httpx', 'sqlalchemy') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
