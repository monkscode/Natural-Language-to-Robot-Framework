"""Tests for src/backend/core/pass_quality.py — the static hollow-pass checker.

Every snippet is hand-written to reproduce one captured bench shape (the run it
mirrors is named); no bench data is committed. The corpus replay that shows the
same verdicts on the real captures is bench/private/gate/pass_quality_corpus_replay.py.
"""

import json
import subprocess
import sys
import time
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
    pass_suggestions,
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
                     "all the 20 books", id="the-number-is-in-another-clause"),
        pytest.param("Go to https://books.toscrape.com and get the titles of all the 20 books",
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
        query = "Open https://shop.example.com, get all the error messages and verify there are 0 errors"
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

    def test_a_number_too_long_to_be_a_count_is_no_number_and_raises_nothing(self):
        # Python refuses int() of a 5,000-digit string: the number must never reach int()
        query = Q10.replace("20 books", "9" * 5000 + " books")
        code = books(GET_LENGTH + "    Should Be True    ${elements_count} == 20\n")
        assert shapes(code, query) == {READ_NOT_PERFORMED}

    def test_a_whole_number_has_at_most_eighteen_digits(self):
        assert pass_quality._whole_numbers("there are " + "1" * 18 + " books") == {int("1" * 18)}
        assert pass_quality._whole_numbers("there are " + "1" * 19 + " books") == set()
        assert pass_quality._whole_numbers("there are " + "1" * 5000 + " books") == set()


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

    @pytest.mark.parametrize("keyword", [
        "Run Keyword If Test Failed", "Run Keyword If Test Passed", "Run Keyword If Timeout Occurred",
        "Run Keyword If All Tests Passed", "Run Keyword If Any Tests Failed"])
    def test_a_teardown_keyword_that_tests_the_run_is_no_assertion(self, keyword):
        code = robot(self.SAUCE, self.LOGIN
                     + "    ${heading}=    Get Text    css=.title\n"
                     "    Log    Retrieved: ${heading}\n"
                     f"    [Teardown]    {keyword}    Take Screenshot\n")
        assert shapes(code, Q07) == {VERIFY_WITHOUT_ASSERTION}

    def test_run_keyword_if_on_a_value_is_still_an_assertion(self):
        code = robot(self.SAUCE, self.LOGIN
                     + "    ${heading}=    Get Text    css=.title\n"
                     "    Run Keyword If    '${heading}' != 'Products'    Fail    Wrong heading\n")
        assert shapes(code, Q07) == set()

    def test_run_keyword_if_on_a_count_still_checks_the_count(self):
        code = books(GET_LENGTH + "    Run Keyword If    ${elements_count} != 20    Fail\n")
        assert shapes(code, Q10) == set()

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


SENTENCE_READ_NOT_PERFORMED = ("The request asks to get a value, and this test does not read one. "
                               "Consider adding a read step.")
SENTENCE_SELECT_CHECK = ("The check reads the whole dropdown's text, which lists every option. "
                         "Consider checking the selected option instead.")
SENTENCE_VERIFY = ("The request asks to verify something, and this test reads values without "
                   "comparing them. Consider adding a check.")
SENTENCE_EMPTY = "This test only opens the page. Consider generating it again with the steps you need."


def locator_answer_sentence(text: str) -> str:
    return (f'This test finds the element by its text "{text}". '
            "If that text can change, consider locating it by position instead.")


def answer_in_locator(literal: str) -> str:
    """q05's shape: the cell is found by a text literal the query does not contain, then read."""
    return robot("${cell}    text=\"" + literal + "\" >> nth=0\n",
                 "    New Page    https://the-internet.herokuapp.com/tables\n"
                 "    ${value}=    Get Text    ${cell}\n"
                 "    Should Not Be Empty    ${value}\n")


class TestPassSuggestions:
    def test_each_hollow_shape_has_its_sentence(self):
        select = robot("", "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                           "    Select Options By    id=dropdown    label    Option 2\n"
                           "    Get Text    css=#dropdown    contains    Option 2\n")
        verify = robot("", "    New Page    https://www.saucedemo.com\n"
                           "    Click    css=#login-button\n")
        read = robot("", "    New Page    https://books.toscrape.com\n"
                         "    Click    css=h3 a\n")
        empty = robot("", "    New Page    https://the-internet.herokuapp.com/tables\n")
        assert pass_suggestions(select, Q08) == [SENTENCE_SELECT_CHECK]
        assert pass_suggestions(verify, "Go to https://www.saucedemo.com and verify the login button works") == [
            SENTENCE_VERIFY]
        assert pass_suggestions(read, "Go to https://books.toscrape.com and get the title of the first book") == [
            SENTENCE_READ_NOT_PERFORMED]
        assert pass_suggestions(empty, Q04) == [SENTENCE_EMPTY]
        assert pass_suggestions(answer_in_locator("John"), Q05) == [locator_answer_sentence("John")]

    def test_a_clean_test_has_no_sentence(self):
        code = robot("", "    New Page    https://the-internet.herokuapp.com/dropdown\n"
                         "    Select Options By    id=dropdown    label    Option 2\n"
                         "    ${selected}=    Get Selected Options    id=dropdown\n"
                         "    Should Be Equal    ${selected}[0]    Option 2\n")
        assert pass_suggestions(code, Q08) == []

    @pytest.mark.parametrize("literal, shown", [
        pytest.param("X" * 39, "X" * 39, id="39-shown-whole"),
        pytest.param("X" * 40, "X" * 40 + "…", id="40-treated-as-cut"),
        pytest.param("X" * 41, "X" * 40 + "…", id="41-cut-at-40"),
        pytest.param("HOTSTYLE Loafers , Running Shoes For Men (Black)",
                     "HOTSTYLE Loafers , Running Shoes For Men…", id="real-product-title"),
    ])
    def test_the_text_is_cut_at_forty_characters_with_an_ellipsis(self, literal, shown):
        assert pass_suggestions(answer_in_locator(literal), Q05) == [locator_answer_sentence(shown)]

    def test_empty_test_is_shown_alone(self):
        code = robot("", "    New Page    https://books.toscrape.com\n")
        # the checker finds three shapes here; the user is told the one that explains the others
        assert [f.shape for f in check_pass_quality(code, Q10)] == [
            READ_NOT_PERFORMED, VERIFY_WITHOUT_ASSERTION, EMPTY_TEST]
        assert pass_suggestions(code, Q10) == [SENTENCE_EMPTY]

    def test_pasted_code_with_an_empty_test_gets_the_empty_sentence(self):
        code = robot("", "    New Page    https://example.com\n")
        assert pass_suggestions(code, None) == [SENTENCE_EMPTY]
        assert pass_suggestions(code, "") == [SENTENCE_EMPTY]

    def test_pasted_code_that_does_something_has_no_sentence(self):
        code = robot("", "    New Page    https://example.com\n    Click    css=a\n")
        assert pass_suggestions(code, None) == []

    def test_two_shapes_come_in_the_checkers_order(self):
        code = answer_in_locator("John").replace("    Should Not Be Empty    ${value}\n",
                                                 "    Log    ${value}\n")
        query = Q05 + " and verify it"
        assert [f.shape for f in check_pass_quality(code, query)] == [
            READ_LOCATOR_IS_THE_ANSWER, VERIFY_WITHOUT_ASSERTION]
        assert pass_suggestions(code, query) == [locator_answer_sentence("John"), SENTENCE_VERIFY]

    def test_a_read_that_is_missing_comes_before_a_missing_assertion(self):
        code = books(GET_LENGTH + "    Log    ${elements_count}\n")
        assert [f.shape for f in check_pass_quality(code, Q10)] == [
            READ_NOT_PERFORMED, VERIFY_WITHOUT_ASSERTION]
        assert pass_suggestions(code, Q10) == [SENTENCE_READ_NOT_PERFORMED, SENTENCE_VERIFY]

    def test_a_fragile_numeric_id_alone_has_no_sentence(self):
        code = robot("${pinned_project_locator}    id=880667900\n",
                     "    New Page    https://github.com/monkscode\n"
                     "    ${name}=    Get Text    ${pinned_project_locator}\n"
                     "    Log    ${name}\n")
        assert check_pass_quality(code, Q01) == [Finding(FRAGILE_NUMERIC_ID, "id=880667900")]
        assert pass_suggestions(code, Q01) == []

    def test_a_fragile_numeric_id_beside_a_hollow_shape_adds_no_sentence(self):
        code = robot("${pinned_project_locator}    id=880667900\n",
                     "    New Page    https://github.com/monkscode\n"
                     "    ${name}=    Get Text    ${pinned_project_locator}\n"
                     "    Log    ${name}\n")
        query = Q01 + " and verify it"
        assert [f.shape for f in check_pass_quality(code, query)] == [
            VERIFY_WITHOUT_ASSERTION, FRAGILE_NUMERIC_ID]
        assert pass_suggestions(code, query) == [SENTENCE_VERIFY]


class TestWordsThatAreNotInstructions:
    """A URL and a control's name hold words that look like instructions and are not."""

    @staticmethod
    def page(url: str, body: str = "") -> str:
        return robot("", f"    New Page    {url}\n" + body)

    @pytest.mark.parametrize("query, url", [
        pytest.param("Open https://the-internet.herokuapp.com/login",
                     "https://the-internet.herokuapp.com/login", id="login-path"),
        pytest.param("Open https://the-internet.herokuapp.com/upload",
                     "https://the-internet.herokuapp.com/upload", id="upload-path"),
        pytest.param("Open www.example.com/login", "https://www.example.com/login", id="www-host"),
        pytest.param("Open example.com/login", "https://example.com/login", id="host-and-path"),
        pytest.param("Open https://example.com/get-started", "https://example.com/get-started",
                     id="read-word-in-the-path"),
        pytest.param("Open https://example.com/verify-email", "https://example.com/verify-email",
                     id="verify-word-in-the-path"),
        pytest.param("Open (https://example.com/login).", "https://example.com/login",
                     id="url-in-brackets-then-a-full-stop"),
    ])
    def test_a_word_in_a_url_is_not_an_instruction(self, query, url):
        assert shapes(self.page(url), query) == set()
        assert pass_suggestions(self.page(url), query) == []

    def test_a_real_instruction_beside_a_url_is_still_read(self):
        query = "Open https://the-internet.herokuapp.com/login and log in as tomsmith"
        assert shapes(self.page("https://the-internet.herokuapp.com/login"), query) == {EMPTY_TEST}

    def test_a_click_beside_a_url_with_a_word_in_it_is_satisfied(self):
        query = "Go to https://example.com/get-started and click the Sign up button"
        code = self.page("https://example.com/get-started", "    Click    text=Sign up\n")
        assert pass_suggestions(code, query) == []

    def test_a_read_beside_a_url_with_a_word_in_it_is_still_asked_for(self):
        query = "Go to https://example.com/get-started and get the page title"
        code = self.page("https://example.com/get-started", "    Click    text=Go\n")
        assert shapes(code, query) == {READ_NOT_PERFORMED}

    def test_the_literal_check_reads_the_whole_request_url_included(self):
        code = self.page("https://shop.example.com/Nike-Air",
                         '    ${name}=    Get Text    text="Nike-Air" >> nth=0\n')
        assert shapes(code, "Open https://shop.example.com/Nike-Air and get the name of the first product") == set()
        assert shapes(code, "Open https://shop.example.com and get the name of the first product") == {
            READ_LOCATOR_IS_THE_ANSWER}

    @pytest.mark.parametrize("query, body", [
        pytest.param("Go to https://playwright.dev and click the Get started button",
                     "    Click    text=Get started\n", id="get-in-a-button-name"),
        pytest.param("Go to https://example.com/blog and click Read more on the first post",
                     "    Click    text=Read more >> nth=0\n", id="read-in-a-link-name"),
        pytest.param("Go to https://example.com/checkout and click Confirm",
                     "    Click    id=confirm\n", id="confirm-is-a-button-name"),
        pytest.param("Go to https://example.com and clicks the Verify email link",
                     "    Click    text=Verify email\n", id="clicks"),
        pytest.param("Go to https://example.com and press the Get started button",
                     "    Click    text=Get started\n", id="press"),
        pytest.param("Go to https://example.com and presses the Get started button",
                     "    Click    text=Get started\n", id="presses"),
        pytest.param("Go to https://example.com and tap the Get started button",
                     "    Click    text=Get started\n", id="tap"),
        pytest.param("Go to https://example.com and taps the Get started button",
                     "    Click    text=Get started\n", id="taps"),
        pytest.param("Go to https://example.com and hover the Read more link",
                     "    Hover    text=Read more\n", id="hover"),
        pytest.param("Go to https://example.com and hovers the Read more link",
                     "    Hover    text=Read more\n", id="hovers"),
    ])
    def test_a_controls_name_is_not_an_instruction(self, query, body):
        assert shapes(self.page("https://example.com", body), query) == set()

    def test_the_control_verb_still_asks_for_more_than_opening(self):
        query = "Go to https://playwright.dev and click the Get started button"
        assert shapes(self.page("https://playwright.dev"), query) == {EMPTY_TEST}

    @pytest.mark.parametrize("query", [
        pytest.param("Go to https://playwright.dev, click the Get started button and get the page title",
                     id="and"),
        pytest.param("Go to https://playwright.dev, click the Get started button, get the page title",
                     id="comma"),
        pytest.param("Go to https://playwright.dev, click the Get started button then get the page title",
                     id="then"),
    ])
    def test_a_request_after_the_controls_name_is_still_read(self, query):
        code = self.page("https://playwright.dev", "    Click    text=Get started\n")
        assert shapes(code, query) == {READ_NOT_PERFORMED}

    def test_a_verify_after_the_controls_name_is_still_read(self):
        query = "Go to https://example.com, click Login and verify the dashboard is shown"
        assert shapes(self.page("https://example.com", "    Click    text=Login\n"), query) == {
            VERIFY_WITHOUT_ASSERTION}


class TestCheckThatMeansTick:
    """`check` is a verify word unless it ticks a box or means "check out"."""

    BOXES = "https://the-internet.herokuapp.com/checkboxes"

    @staticmethod
    def page(url: str, body: str = "") -> str:
        return robot("", f"    New Page    {url}\n" + body)

    @pytest.mark.parametrize("query, url, body", [
        pytest.param(f"Go to {BOXES} and check checkbox 1", BOXES,
                     "    Check Checkbox    css=#checkboxes input >> nth=0\n", id="checkbox"),
        pytest.param(f"Go to {BOXES} and checks checkbox 1", BOXES,
                     "    Check Checkbox    css=#checkboxes input >> nth=0\n", id="checks"),
        pytest.param("Go to https://example.com/login, check the Remember me box and click Login",
                     "https://example.com/login",
                     "    Check Checkbox    text=Remember me\n    Click    text=Login\n", id="box-then-a-click"),
        pytest.param("Go to https://example.com/form and check the Male radio button",
                     "https://example.com/form", "    Check Checkbox    css=#male\n", id="radio-button"),
        pytest.param("Go to https://www.saucedemo.com, add the backpack to the cart and check out",
                     "https://www.saucedemo.com",
                     "    Click    id=add-to-cart-sauce-labs-backpack\n    Click    id=checkout\n", id="check-out"),
    ])
    def test_checking_a_box_or_checking_out_is_an_action_not_a_verify(self, query, url, body):
        assert shapes(self.page(url, body), query) == set()

    def test_an_action_check_still_asks_for_more_than_opening(self):
        assert shapes(self.page(self.BOXES), f"Go to {self.BOXES} and check checkbox 1") == {EMPTY_TEST}
        assert pass_suggestions(self.page(self.BOXES), f"Go to {self.BOXES} and check checkbox 1") == [
            SENTENCE_EMPTY]

    @pytest.mark.parametrize("query, url, body", [
        pytest.param(f"Go to {BOXES}, check that checkbox 1 is checked", BOXES,
                     "    Click    css=#checkboxes input >> nth=0\n", id="that-and-is"),
        pytest.param(f"Go to {BOXES}, check if checkbox 1 is ticked", BOXES,
                     "    Click    css=#checkboxes input >> nth=0\n", id="if"),
        pytest.param(f"Go to {BOXES}, check whether the box is ticked", BOXES,
                     "    Click    css=#checkboxes input >> nth=0\n", id="whether"),
        pytest.param(f"Go to {BOXES}, check the boxes are ticked", BOXES,
                     "    Click    css=#checkboxes input >> nth=0\n", id="are"),
        pytest.param("Open example.com and check the page title is not empty", "https://example.com",
                     "    ${title}=    Get Title\n    Log    ${title}\n", id="no-box-at-all"),
        pytest.param("Open example.com and check outside links work", "https://example.com",
                     "    Click    text=Go\n", id="outside-is-not-out"),
    ])
    def test_a_check_that_states_something_is_still_a_verify(self, query, url, body):
        assert shapes(self.page(url, body), query) == {VERIFY_WITHOUT_ASSERTION}

    def test_a_box_in_a_later_clause_does_not_make_this_check_an_action(self):
        query = "Go to https://example.com, check the title is not empty and click the Remember me box"
        code = self.page("https://example.com", "    Click    text=Remember me\n")
        assert shapes(code, query) == {VERIFY_WITHOUT_ASSERTION}

    def test_a_verify_word_after_an_action_check_is_still_a_verify(self):
        query = f"Go to {self.BOXES}, check checkbox 1 and verify it is ticked"
        code = self.page(self.BOXES, "    Check Checkbox    css=#checkboxes input >> nth=0\n")
        assert shapes(code, query) == {VERIFY_WITHOUT_ASSERTION}


class TestLongRequests:
    """A request has no length limit: reading it must take time in proportion to its length."""

    CODE = robot("", "    New Page    https://example.com\n    Click    text=Go\n")

    @pytest.mark.parametrize("word", ["get ", "verify ", "check ", "check box ", "click ", "https://a.com/get ",
                                      "(", "a.", "https://a.com/get"])
    def test_a_long_request_of_one_repeated_word_takes_under_two_seconds(self, word):
        query = word * (80_000 // len(word))
        start = time.perf_counter()
        pass_suggestions(self.CODE, query)
        assert time.perf_counter() - start < 2.0

    def test_the_clause_of_each_verify_word_is_what_follows_it_up_to_the_clause_end(self):
        query = "Go to x, verify there are 20 books, then verify 5 items and check the Total is 7. Done"
        assert pass_quality._verify_clauses(query) == [
            " there are 20 books", " 5 items ", " the total is 7"]


def test_importing_the_checker_loads_no_config_database_or_llm_client():
    probe = ("import sys; import src.backend.core.pass_quality; "
             "bad = [m for m in ('src.backend.core.config', 'psycopg', 'litellm', 'crewai', "
             "'requests', 'httpx', 'sqlalchemy') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
