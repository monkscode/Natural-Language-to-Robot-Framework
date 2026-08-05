"""The bench guard for a read query whose generated test never reads anything.

Bench query q10 is "Go to https://books.toscrape.com, get the titles of all
books on the first page, and verify there are 20 books". Across the 146
captured q10 runs in bench/runs, 109 (75%) generated a test that only counts
elements — Get Elements -> Get Length -> Should Be True — and never reads a
title. 102 of those PASSED, so the bench reported a pass for a test that did
half the query.

The dryrun gate cannot catch this: it validates keyword and argument SHAPE,
not semantics. A count-only test is perfectly well-formed Robot Framework.
This guard makes the gap visible at bench time.

The guard warns instead of adding a CSV column — a new column would trip
gate_schema against every existing baseline.
"""

from unittest.mock import patch

import pytest

from bench.bench_lib import code_performs_read, query_requests_read

# Both fixtures below are real captured artifacts, copied keyword-for-keyword.
# The only differences from the files on disk are invisible and cannot affect a
# substring match: CRLF line endings, and three blank lines that carry four
# spaces of indentation in the original.

# From bench/runs/641ff45a-c840-41c2-bbcc-189c77753780/artifacts/test.robot —
# a real captured q10 run. Counts elements, never reads a title.
COUNT_ONLY_Q10 = """*** Settings ***
Library    Browser    timeout=30s
Library    BuiltIn
Library    Collections

*** Variables ***
${browser}    chromium
${headless}    True
${url}    https://books.toscrape.com
${book_list_locator}    ol > li

*** Test Cases ***
Generated Test
    [Documentation]    Auto-generated test case to verify books count on Books to Scrape website
    New Browser    ${browser}    headless=${headless}
    New Context    viewport={'width': 1920, 'height': 1080}
    New Page    ${url}
    @{book_elements}=    Get Elements    ${book_list_locator}
    ${book_count}=    Get Length    ${book_elements}
    Should Be True    ${book_count} == 20
    Close Browser"""

# From bench/runs/70da0e33-ceb6-4f49-80c5-7303d64fc069/artifacts/test.robot —
# a real captured q10 run that DOES read each title (Get Attribute), on top of
# the same count assertion.
READING_Q10 = """*** Settings ***
Library    Browser    timeout=30s
Library    BuiltIn
Library    Collections

*** Variables ***
${browser}    chromium
${headless}    True
${url}    https://books.toscrape.com
${book_list_locator}    ol > li

*** Test Cases ***
Verify Book Count And List Titles
    [Documentation]    Verify book elements on the main page of Books to Scrape website
    New Browser    ${browser}    headless=${headless}
    New Context    viewport={'width': 1920, 'height': 1080}
    New Page    ${url}

    # Get elements and iterate to log titles dynamically
    @{elements}=    Get Elements    ${book_list_locator}
    FOR    ${element}    IN    @{elements}
        ${title_elem}=    Get Element    ${element} >>> h3 > a
        ${title}=    Get Attribute    ${title_elem}    title
        Exit For Loop If    len($title.strip()) == 0
        Log    Book Title: ${title}
    END

    # Get element count and assert
    ${book_count}=    Get Element Count    ${book_list_locator}
    Should Be True    ${book_count} == 20

    Close Browser"""


class TestQueryRequestsRead:
    """Every string below is VERBATIM from bench/queries.json (frozen corpus).

    Locking all ten pins the zero-false-positive claim: the three queries that
    ask for a value are the only three that may match, so a warning fired on
    the standard bench is always about a real gap, never about the predicate
    misreading a click-only or type-only query.
    """

    @pytest.mark.parametrize("query", [
        # q01
        "Navigate to GitHub using url https://github.com/monkscode, and then "
        "get the name of the Pinned project",
        # q05
        "Go to https://the-internet.herokuapp.com/tables and get the text from "
        "the first row, second column of Table 1",
        # q10
        "Go to https://books.toscrape.com, get the titles of all books on the "
        "first page, and verify there are 20 books",
    ])
    def test_read_queries_match(self, query):
        assert query_requests_read(query) is True

    @pytest.mark.parametrize("query", [
        # q02
        'Go to https://www.wikipedia.org/ and search for "Narendra Modi"',
        # q03
        "Open https://nutronsystems.com/ then click on Solutions from the top "
        "right and then click on OEM Solution",
        # q04
        'Go to https://the-internet.herokuapp.com/tables and click on the '
        'first table "edit" link in the row containing "Smith"',
        # q06
        'Go to https://demoqa.com/webtables, type "Cierra" in the search box, '
        'and verify the table rows only have data of Cierra',
        # q07
        "Go to https://www.saucedemo.com, type standard_user in the username "
        "field, type secret_sauce in the password field, click the Login "
        "button, and verify the products page shows the Products heading",
        # q08
        "Go to https://the-internet.herokuapp.com/dropdown, select Option 2 "
        "from the dropdown, and verify Option 2 is selected",
        # q09
        "Go to https://www.w3schools.com/tags/tryit.asp?filename="
        "tryhtml5_global_contenteditable, type Hello Bench into the editable "
        "paragraph inside the iframe, and verify the paragraph contains Hello "
        "Bench",
    ])
    def test_non_read_queries_do_not_match(self, query):
        assert query_requests_read(query) is False

    def test_matching_is_case_insensitive(self):
        assert query_requests_read("GET the product name") is True

    @pytest.mark.parametrize("verb", ["read", "extract", "retrieve", "fetch"])
    def test_the_other_read_verbs_match(self, verb):
        assert query_requests_read(f"Open the page and {verb} the heading") is True

    def test_a_word_that_merely_contains_a_verb_does_not_match(self):
        """Word boundaries, not substrings — "target" contains "get"."""
        assert query_requests_read("Click the target button") is False


class TestTestPerformsRead:
    def test_the_real_count_only_q10_artifact_is_not_a_read(self):
        assert code_performs_read(COUNT_ONLY_Q10) is False

    def test_the_real_title_reading_q10_artifact_is_a_read(self):
        assert code_performs_read(READING_Q10) is True

    @pytest.mark.parametrize("keyword", [
        "Get Element Count", "Get Elements", "Get Element", "Get Length",
    ])
    def test_counting_keywords_alone_are_not_a_read(self, keyword):
        """These are exactly the degenerate shape the guard exists to catch."""
        code = f"*** Test Cases ***\nT\n    ${{n}}=    {keyword}    css=li\n"
        assert code_performs_read(code) is False

    @pytest.mark.parametrize("keyword", [
        "Get Text", "Get Texts", "Get Attribute", "Get Property",
        "Get Selected Options",
    ])
    def test_each_value_reading_keyword_qualifies(self, keyword):
        code = f"*** Test Cases ***\nT\n    ${{v}}=    {keyword}    css=li\n"
        assert code_performs_read(code) is True

    def test_matching_is_case_insensitive(self):
        assert code_performs_read("    ${t}=    get text    css=h1\n") is True

    def test_get_attribute_names_is_a_superset_and_still_qualifies(self):
        """"Get Attribute" is a prefix of "Get Attribute Names", which reads a
        value off the page too — a correct match, not a false positive."""
        code = "    @{a}=    Get Attribute Names    css=input\n"
        assert code_performs_read(code) is True


class TestWarnIfReadQueryNeverReads:
    """Located via bench/runs/<workflow_id>/artifacts/test.robot first, then
    the staging copy — capture_evidence writes the former and detach_run
    deletes the latter, so only the staging copy survives a failed capture."""

    READ_QUERY = ("Go to https://books.toscrape.com, get the titles of all "
                  "books on the first page, and verify there are 20 books")
    NON_READ_QUERY = ("Go to https://the-internet.herokuapp.com/dropdown, "
                      "select Option 2 from the dropdown, and verify Option 2 "
                      "is selected")
    WORKFLOW_ID = "641ff45a-c840-41c2-bbcc-189c77753780"

    @staticmethod
    def _write(root, workflow_id, code, *, artifacts):
        run_dir = root / workflow_id / "artifacts" if artifacts else root / workflow_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "test.robot").write_text(code, encoding="utf-8")

    def _call(self, runs_dir, staging_root, query):
        from bench.run_bench import warn_if_read_query_never_reads

        with patch("bench.run_bench.RUNS_DIR", runs_dir), \
                patch("bench.run_bench.STAGING_ROOT", staging_root):
            warn_if_read_query_never_reads(query, "q10", 2, self.WORKFLOW_ID)

    def test_warns_on_a_read_query_whose_test_only_counts(self, tmp_path, capsys):
        runs, staging = tmp_path / "runs", tmp_path / "staging"
        self._write(runs, self.WORKFLOW_ID, COUNT_ONLY_Q10, artifacts=True)

        self._call(runs, staging, self.READ_QUERY)

        err = capsys.readouterr().err
        # Full workflow id, never truncated (repo rule).
        assert self.WORKFLOW_ID in err
        assert "q10" in err
        assert "repeat 2" in err

    def test_silent_when_the_test_actually_reads(self, tmp_path, capsys):
        runs, staging = tmp_path / "runs", tmp_path / "staging"
        self._write(runs, self.WORKFLOW_ID, READING_Q10, artifacts=True)

        self._call(runs, staging, self.READ_QUERY)

        assert capsys.readouterr().err == ""

    def test_silent_when_the_query_asks_for_no_read(self, tmp_path, capsys):
        """A click-only query legitimately produces a test that reads nothing."""
        runs, staging = tmp_path / "runs", tmp_path / "staging"
        self._write(runs, self.WORKFLOW_ID, COUNT_ONLY_Q10, artifacts=True)

        self._call(runs, staging, self.NON_READ_QUERY)

        assert capsys.readouterr().err == ""

    def test_silent_when_no_test_robot_exists_anywhere(self, tmp_path, capsys):
        """An unmeasurable run must not warn — there is nothing to judge."""
        runs, staging = tmp_path / "runs", tmp_path / "staging"
        runs.mkdir()
        staging.mkdir()

        self._call(runs, staging, self.READ_QUERY)

        assert capsys.readouterr().err == ""

    def test_falls_back_to_the_staging_copy(self, tmp_path, capsys):
        """Capture failed, so bench/runs has no artifacts dir, but detach_run
        did not delete the staging copy either — score it from there."""
        runs, staging = tmp_path / "runs", tmp_path / "staging"
        runs.mkdir()
        self._write(staging, self.WORKFLOW_ID, COUNT_ONLY_Q10, artifacts=False)

        self._call(runs, staging, self.READ_QUERY)

        assert self.WORKFLOW_ID in capsys.readouterr().err

    def test_an_unreadable_file_never_breaks_the_run(self, tmp_path, capsys):
        """A guard that raises would kill a bench mid-flight."""
        runs, staging = tmp_path / "runs", tmp_path / "staging"
        self._write(runs, self.WORKFLOW_ID, COUNT_ONLY_Q10, artifacts=True)

        from pathlib import Path as _Path

        with patch.object(_Path, "read_text", side_effect=OSError("boom")):
            self._call(runs, staging, self.READ_QUERY)

        assert capsys.readouterr().err == ""
