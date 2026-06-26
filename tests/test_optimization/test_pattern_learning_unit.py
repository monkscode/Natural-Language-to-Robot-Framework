"""Unit tests for QueryPatternMatcher (pattern_learning.py).

The matcher sits between the learning store and code generation: a silent
prediction failure here degrades every generated test without any user-visible
error, so the degradation paths (no store, store errors, low confidence) are
the ones that matter. The vector store is mocked — keyword extraction and
confidence math are pure logic.
"""

from unittest.mock import MagicMock

import pytest

from src.backend.crew_ai.optimization.pattern_learning import QueryPatternMatcher

ROBOT_CODE = """*** Settings ***
Library    Browser

*** Test Cases ***
Search For Shoes
    New Browser    chromium    headless=true
    New Page       https://example.com
    ${text}=    Get Text    id=results
    Click    id=submit-btn
"""


# ---------------------------------------------------------------------------
# Keyword extraction from generated code
# ---------------------------------------------------------------------------

class TestExtractKeywords:
    def test_extracts_keywords_not_test_names_or_settings(self):
        kws = set(QueryPatternMatcher()._extract_keywords_from_code(ROBOT_CODE))
        assert kws == {"New Browser", "New Page", "Get Text", "Click"}
        assert "Search For Shoes" not in kws  # test case name skipped
        assert "Library" not in kws  # settings section skipped

    def test_variable_assignment_yields_the_keyword_not_the_variable(self):
        code = ("*** Test Cases ***\nT\n"
                "    ${result}=    Evaluate    1 + 1\n"
                "    ${x}    ${y}\n")  # var-only line: nothing to record
        kws = QueryPatternMatcher()._extract_keywords_from_code(code)
        assert kws == ["Evaluate"]

    def test_comments_blank_lines_and_brackets_are_skipped(self):
        code = ("*** Test Cases ***\nT\n"
                "    # a comment\n\n"
                "    [Documentation]    Something\n"
                "    Click    id=x\n")
        assert QueryPatternMatcher()._extract_keywords_from_code(code) == ["Click"]


# ---------------------------------------------------------------------------
# learn_from_execution: never raises into the learning pipeline
# ---------------------------------------------------------------------------

class TestLearnFromExecution:
    def test_stores_extracted_pattern(self):
        store = MagicMock()
        store.add_pattern.return_value = "pattern_abc"
        QueryPatternMatcher(store).learn_from_execution("search shoes", ROBOT_CODE)
        args = store.add_pattern.call_args[0]
        assert args[0] == "search shoes"
        assert set(args[1]) == {"New Browser", "New Page", "Get Text", "Click"}

    def test_no_keywords_extracted_skips_store(self):
        store = MagicMock()
        QueryPatternMatcher(store).learn_from_execution("q", "*** Settings ***\n")
        store.add_pattern.assert_not_called()

    def test_without_store_logs_and_continues(self):
        QueryPatternMatcher(None).learn_from_execution("q", ROBOT_CODE)  # no raise

    def test_store_failure_is_swallowed(self):
        store = MagicMock()
        store.add_pattern.side_effect = RuntimeError("postgres down")
        QueryPatternMatcher(store).learn_from_execution("q", ROBOT_CODE)  # no raise


# ---------------------------------------------------------------------------
# get_relevant_keywords: prediction + every degradation path
# ---------------------------------------------------------------------------

def _store_with(results, count=None):
    store = MagicMock()
    store.pattern_count.return_value = len(results) if count is None else count
    store.search_patterns.return_value = results
    return store


class TestGetRelevantKeywords:
    def test_returns_empty_without_store(self):
        assert QueryPatternMatcher(None).get_relevant_keywords("q") == []

    def test_returns_empty_when_no_patterns_stored(self):
        store = _store_with([], count=0)
        assert QueryPatternMatcher(store).get_relevant_keywords("q") == []
        store.search_patterns.assert_not_called()

    def test_returns_empty_when_search_finds_nothing(self):
        store = _store_with([], count=3)
        assert QueryPatternMatcher(store).get_relevant_keywords("q") == []

    def test_returns_empty_below_confidence_threshold(self):
        # distance 1.0 -> similarity 0.5, under the 0.7 default threshold
        store = _store_with([{"keywords": ["Click"], "distance": 1.0}])
        assert QueryPatternMatcher(store).get_relevant_keywords("q") == []

    def test_aggregates_keywords_from_confident_patterns_only(self):
        store = _store_with([
            {"keywords": ["Click", "Fill Text"], "distance": 0.1},   # sim ~0.91
            {"keywords": ["Click", "Go To"], "distance": 0.2},       # sim ~0.83
            {"keywords": ["Should Not Appear"], "distance": 9.0},    # sim 0.1
        ])
        out = QueryPatternMatcher(store).get_relevant_keywords("q")
        assert out[0] == "Click"  # seen twice -> ranked first
        assert set(out) == {"Click", "Fill Text", "Go To"}

    def test_caps_predictions_at_ten_keywords(self):
        store = _store_with([
            {"keywords": [f"KW {i}" for i in range(15)], "distance": 0.1},
        ])
        assert len(QueryPatternMatcher(store).get_relevant_keywords("q")) == 10

    def test_search_caps_top_k_at_pattern_count(self):
        store = _store_with([{"keywords": ["Click"], "distance": 0.1}], count=2)
        QueryPatternMatcher(store).get_relevant_keywords("q")
        store.search_patterns.assert_called_once_with("q", top_k=2, org_id=None)

    def test_store_failure_degrades_to_empty(self):
        store = MagicMock()
        store.pattern_count.side_effect = RuntimeError("postgres down")
        assert QueryPatternMatcher(store).get_relevant_keywords("q") == []
