"""
DAY_02 Pytest Tests

Validates all acceptance criteria for the Failure Analyzer module.
Uses synthetic output.xml via tmp_dir fixture -- does NOT modify real data.

Migrated from: scripts/verify_day02.py
"""

import os
import textwrap

import pytest

from src.backend.crew_ai.optimization.failure_analyzer import (
    KeywordResult,
    FailureAnalysis,
    OutputXmlParser,
    FailureClassifier,
    CompositeFailureDetector,
    FailureAnalyzer,
)


# ---------------------------------------------------------------------------
# Synthetic XML Constants
# ---------------------------------------------------------------------------

PASSING_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="New Browser" owner="Browser">
        <arg>${browser}</arg>
        <status status="PASS" start="2026-02-18T17:00:01" elapsed="0.300"/>
    </kw>
    <kw name="New Page" owner="Browser">
        <arg>${url}</arg>
        <status status="PASS" start="2026-02-18T17:00:02" elapsed="1.500"/>
    </kw>
    <kw name="Click" owner="Browser">
        <arg>${locator}</arg>
        <status status="PASS" start="2026-02-18T17:00:04" elapsed="0.050"/>
    </kw>
    <kw name="Close Browser" owner="Browser">
        <status status="PASS" start="2026-02-18T17:00:05" elapsed="0.030"/>
    </kw>
    <status status="PASS" start="2026-02-18T17:00:01" elapsed="4.000"/>
    </test>
    <status status="PASS" start="2026-02-18T17:00:00" elapsed="5.000"/>
    </suite>
    <statistics><total><stat pass="1" fail="0" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")

FAILING_XML_TIMEOUT = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="New Browser" owner="Browser">
        <status status="PASS" start="2026-02-18T17:00:01" elapsed="0.300"/>
    </kw>
    <kw name="New Page" owner="Browser">
        <arg>${url}</arg>
        <status status="PASS" start="2026-02-18T17:00:02" elapsed="1.500"/>
    </kw>
    <kw name="Get Text" owner="Browser">
        <msg time="2026-02-18T17:00:12" level="FAIL">TimeoutError: locator.elementHandle: Timeout 10000ms exceeded.
Call log:
  - waiting for locator('id=892238218')
</msg>
        <var>${text}</var>
        <arg>${locator}</arg>
        <status status="FAIL" start="2026-02-18T17:00:02" elapsed="10.135">TimeoutError: locator.elementHandle: Timeout 10000ms exceeded.
Call log:
  - waiting for locator('id=892238218')
</status>
    </kw>
    <kw name="Log" owner="BuiltIn">
        <arg>Text: ${text}</arg>
        <status status="NOT RUN" start="2026-02-18T17:00:12" elapsed="0.000"/>
    </kw>
    <kw name="Close Browser" owner="Browser">
        <status status="NOT RUN" start="2026-02-18T17:00:12" elapsed="0.000"/>
    </kw>
    <status status="FAIL" start="2026-02-18T17:00:01" elapsed="11.364">TimeoutError: locator.elementHandle: Timeout 10000ms exceeded.
Call log:
  - waiting for locator('id=892238218')
</status>
    </test>
    <status status="FAIL" start="2026-02-18T17:00:00" elapsed="12.000"/>
    </suite>
    <statistics><total><stat pass="0" fail="1" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")

FAILING_XML_KEYWORD_NOT_FOUND = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="Input Text" owner="Browser">
        <msg time="2026-02-18T17:00:01" level="FAIL">No keyword with name 'Input Text' found. Did you mean:
    Browser.Fill Text</msg>
        <status status="FAIL" start="2026-02-18T17:00:01" elapsed="0.001">No keyword with name 'Input Text' found.</status>
    </kw>
    <status status="FAIL" start="2026-02-18T17:00:01" elapsed="0.002">No keyword with name 'Input Text' found.</status>
    </test>
    <status status="FAIL" start="2026-02-18T17:00:00" elapsed="0.003"/>
    </suite>
    <statistics><total><stat pass="0" fail="1" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")

NO_TEST_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <status status="FAIL" start="2026-02-18T17:00:00" elapsed="0.001"/>
    </suite>
    <errors/>
    </robot>
""")


def _write_xml(tmp_dir: str, content: str, name: str = "output.xml") -> str:
    """Write XML content to a file inside tmp_dir and return the path."""
    path = os.path.join(tmp_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.strip())
    return path


# ---------------------------------------------------------------------------
# Test: KeywordResult Dataclass
# ---------------------------------------------------------------------------


class TestKeywordResult:
    """Tests for the KeywordResult dataclass."""

    def test_has_all_expected_fields(self):
        """Verify KeywordResult dataclass has all expected fields."""
        kw = KeywordResult(
            name="Click", library="Browser", status="PASS",
            message="", elapsed_ms=50, arguments=["${locator}"],
        )
        expected_fields = {"name", "library", "status", "message", "elapsed_ms", "arguments"}
        actual_fields = set(kw.__dataclass_fields__.keys())
        assert expected_fields == actual_fields, (
            f"KeywordResult has all expected fields: fields={sorted(actual_fields)}"
        )

    def test_stores_values_correctly(self):
        """Verify KeywordResult stores values correctly."""
        kw = KeywordResult(
            name="Click", library="Browser", status="PASS",
            message="", elapsed_ms=50, arguments=["${locator}"],
        )
        assert kw.name == "Click" and kw.library == "Browser" and kw.elapsed_ms == 50, (
            "KeywordResult stores values correctly"
        )


# ---------------------------------------------------------------------------
# Test: FailureAnalysis Dataclass
# ---------------------------------------------------------------------------


class TestFailureAnalysis:
    """Tests for the FailureAnalysis dataclass."""

    def test_has_all_expected_fields(self):
        """Verify FailureAnalysis dataclass has all expected fields and defaults."""
        fa = FailureAnalysis(
            category="B1", specific_type="wrong_keyword_name",
            confidence=0.95, source="regex",
        )
        expected_fields = {
            "category", "specific_type", "confidence", "source",
            "failed_keyword", "failed_keyword_args", "error_message", "keyword_chain",
        }
        actual_fields = set(fa.__dataclass_fields__.keys())
        assert expected_fields == actual_fields, (
            f"FailureAnalysis has all expected fields: fields={sorted(actual_fields)}"
        )

    def test_optional_fields_default_to_none(self):
        """Verify FailureAnalysis optional fields default to None."""
        fa = FailureAnalysis(
            category="B1", specific_type="wrong_keyword_name",
            confidence=0.95, source="regex",
        )
        assert (
            fa.failed_keyword is None
            and fa.failed_keyword_args is None
            and fa.error_message is None
            and fa.keyword_chain is None
        ), "FailureAnalysis optional fields default to None"

    def test_accepts_optional_fields(self):
        """Verify FailureAnalysis accepts optional fields."""
        fa_with_optionals = FailureAnalysis(
            category="C1", specific_type="element_not_found",
            confidence=0.95, source="regex",
            failed_keyword="Click", error_message="Could not find element",
        )
        assert fa_with_optionals.failed_keyword == "Click", (
            "FailureAnalysis accepts optional fields"
        )


# ---------------------------------------------------------------------------
# Test: OutputXmlParser
# ---------------------------------------------------------------------------


class TestOutputXmlParser:
    """Tests for the OutputXmlParser."""

    def test_passing_test_status(self, tmp_dir):
        """Verify OutputXmlParser correctly parses a passing test."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, PASSING_XML, "passing.xml")

        result = parser.parse(xml_path)
        assert result["test_status"] == "passed", (
            f"Parser: passing test -> test_status='passed': got '{result['test_status']}'"
        )

    def test_passing_test_no_error_message(self, tmp_dir):
        """Verify passing test has no error_message."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, PASSING_XML, "passing_no_err.xml")

        result = parser.parse(xml_path)
        assert result.get("error_message") is None, (
            "Parser: passing test -> no error_message"
        )

    def test_passing_test_keyword_chain_length(self, tmp_dir):
        """Verify passing test has 4 keywords in chain."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, PASSING_XML, "passing_chain.xml")

        result = parser.parse(xml_path)
        assert len(result["keyword_chain"]) == 4, (
            f"Parser: passing test -> keyword_chain has 4 keywords: got {len(result['keyword_chain'])}"
        )

    def test_failed_test_status(self, tmp_dir):
        """Verify OutputXmlParser correctly parses a failed test with timeout."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "failing_timeout.xml")

        result = parser.parse(xml_path)
        assert result["test_status"] == "failed", (
            f"Parser: failed test -> test_status='failed': got '{result['test_status']}'"
        )

    def test_failed_test_error_message_contains_timeout(self, tmp_dir):
        """Verify failed test error_message contains 'Timeout'."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "failing_timeout_msg.xml")

        result = parser.parse(xml_path)
        assert "Timeout" in (result.get("error_message") or ""), (
            f"Parser: failed test -> error_message contains 'Timeout': "
            f"msg='{(result.get('error_message') or '')[:60]}...'"
        )

    def test_failed_test_failed_keyword(self, tmp_dir):
        """Verify failed test identifies failed_keyword='Get Text'."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "failing_timeout_kw.xml")

        result = parser.parse(xml_path)
        assert result.get("failed_keyword") == "Get Text", (
            f"Parser: failed test -> failed_keyword='Get Text': got '{result.get('failed_keyword')}'"
        )

    def test_failed_test_not_run_keywords_in_chain(self, tmp_dir):
        """Verify NOT RUN keywords are included in chain."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "failing_not_run.xml")

        result = parser.parse(xml_path)
        chain = result["keyword_chain"]
        not_run = [kw for kw in chain if kw.status == "NOT RUN"]
        assert len(not_run) == 2, (
            f"Parser: NOT RUN keywords included in chain: got {len(not_run)} NOT RUN keywords"
        )

    def test_failed_test_elapsed_ms_conversion(self, tmp_dir):
        """Verify elapsed seconds to ms conversion."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "failing_elapsed.xml")

        result = parser.parse(xml_path)
        chain = result["keyword_chain"]
        failed_kw = [kw for kw in chain if kw.status == "FAIL"][0]
        assert failed_kw.elapsed_ms == 10135, (
            f"Parser: elapsed seconds -> ms conversion: got {failed_kw.elapsed_ms}ms"
        )

    def test_owner_mapped_to_library(self, tmp_dir):
        """Verify XML 'owner' attribute is mapped to KeywordResult.library."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, PASSING_XML, "owner_mapping.xml")

        result = parser.parse(xml_path)
        first_kw = result["keyword_chain"][0]
        assert first_kw.library == "Browser", (
            f"Parser: owner='Browser' -> library='Browser': got '{first_kw.library}'"
        )

    def test_missing_file_returns_error(self):
        """Verify OutputXmlParser handles missing file gracefully."""
        parser = OutputXmlParser()
        result = parser.parse("/nonexistent/output.xml")
        assert result["test_status"] == "error", (
            "Parser: missing file -> test_status='error'"
        )

    def test_malformed_xml_returns_error(self, tmp_dir):
        """Verify OutputXmlParser handles malformed XML gracefully."""
        parser = OutputXmlParser()
        path = os.path.join(tmp_dir, "malformed.xml")
        with open(path, "w") as f:
            f.write("NOT VALID XML <<<")

        result = parser.parse(path)
        assert result["test_status"] == "error", (
            "Parser: malformed XML -> test_status='error'"
        )

    def test_no_test_element_returns_error(self, tmp_dir):
        """Verify OutputXmlParser handles no <test> element gracefully."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, NO_TEST_XML, "no_test.xml")

        result = parser.parse(xml_path)
        assert result["test_status"] == "error", (
            "Parser: no <test> element -> test_status='error'"
        )

    def test_msg_fail_extraction(self, tmp_dir):
        """Verify error message is extracted from <msg level='FAIL'> first."""
        parser = OutputXmlParser()
        xml_path = _write_xml(tmp_dir, FAILING_XML_KEYWORD_NOT_FOUND, "kw_not_found.xml")

        result = parser.parse(xml_path)
        assert "Did you mean" in (result.get("error_message") or ""), (
            f"Parser: error from <msg level='FAIL'> (full message): "
            f"msg='{(result.get('error_message') or '')[:80]}...'"
        )


# ---------------------------------------------------------------------------
# Test: FailureClassifier -- Seed Regex Patterns
# ---------------------------------------------------------------------------


class TestFailureClassifier:
    """Tests for the FailureClassifier."""

    @pytest.mark.parametrize("error_msg,expected_cat,expected_type,test_name", [
        ("Variable '${text}' not found", "A2", "missing_variable_assignment", "variable_not_found"),
        ("Resolving variable '${count}' failed", "A2", "variable_resolution_error", "variable_resolution"),
        ("Robot Framework reports were not generated", "A5", "robot_crash_no_output", "robot_not_generated"),
        ("No keyword with name 'Input Text' found", "B6", "library_mismatch", "browser_library_mismatch (Input Text)"),
        ("No keyword with name 'Open Browser' found", "B6", "library_mismatch", "browser_library_mismatch (Open Browser)"),
        ("No keyword with name 'Click Element' found", "B6", "library_mismatch", "browser_library_mismatch (Click Element)"),
        ("No keyword with name 'Close All Browsers' found", "B6", "library_mismatch", "browser_library_mismatch (Close All Browsers)"),
        ("No keyword with name 'Do Something' found", "B1", "wrong_keyword_name", "wrong_keyword (generic)"),
        ("Expected 2 arguments, got 3", "B4", "wrong_argument_count", "wrong_arg_count"),
        ("'enabled' is not a valid state", "B3", "invalid_parameter_value", "invalid_state"),
        ("Got unexpected keyword argument 'force'", "B4", "unexpected_parameter", "unexpected_param"),
        ("Could not find element with selector '#btn'", "C1", "element_not_found", "element_not_found"),
        ("strict mode violation: locator resolved to 3 elements", "C2", "multiple_elements_found", "strict_mode"),
        ("Element is not attached to the DOM", "C3", "stale_element", "not_attached"),
        ("Element is not visible in the page", "C1", "element_not_visible", "not_visible"),
        ("TimeoutError: locator.elementHandle: Timeout 10000ms exceeded.", "D1", "page_load_timeout", "timeout"),
        ("Navigation timeout exceeded", "D1", "page_load_timeout", "page_load_timeout"),
        ("Element click intercepted by another element", "D4", "popup_overlay_blocking", "click_intercepted"),
        ("Element is not interactable at this point", "D3", "animation_or_disabled", "not_interactable"),
        ("'Active Status' should contain 'Inactive'", "E1", "assertion_contain_failed", "should_contain"),
        ("'hello' should be equal to 'world'", "E1", "assertion_equality_failed", "should_be_equal"),
        ("'False' should be true", "E1", "assertion_truth_failed", "should_be_true"),
    ], ids=lambda val: val if isinstance(val, str) and "(" not in val else "")
    def test_seed_pattern(self, error_msg, expected_cat, expected_type, test_name):
        """Verify each seed regex pattern classifies correctly."""
        classifier = FailureClassifier()
        result = classifier.classify(error_msg)
        assert result.category == expected_cat and result.specific_type == expected_type, (
            f"Regex: {test_name} -> {expected_cat}: "
            f"got cat={result.category}, type={result.specific_type}"
        )

    def test_empty_message_returns_unknown(self):
        """Verify classifier handles empty error message."""
        classifier = FailureClassifier()
        result = classifier.classify("")
        assert result.category == "unknown" and result.specific_type == "no_error_message", (
            f"Classifier: empty message -> unknown/no_error_message: "
            f"got cat={result.category}, type={result.specific_type}"
        )

    def test_empty_message_confidence(self):
        """Verify empty message returns confidence=0.1."""
        classifier = FailureClassifier()
        result = classifier.classify("")
        assert result.confidence == 0.1, (
            f"Classifier: empty message -> confidence=0.1: got {result.confidence}"
        )

    def test_unmatched_error_returns_unknown(self):
        """Verify unmatched error returns unknown/unclassified."""
        classifier = FailureClassifier()
        result = classifier.classify("Some completely novel error we've never seen")
        assert result.category == "unknown" and result.specific_type == "unclassified", (
            "Classifier: unmatched error -> unknown/unclassified"
        )

    def test_unmatched_error_confidence(self):
        """Verify unmatched error returns confidence=0.3."""
        classifier = FailureClassifier()
        result = classifier.classify("Some completely novel error we've never seen")
        assert result.confidence == 0.3, (
            "Classifier: unmatched error -> confidence=0.3"
        )

    def test_regex_match_confidence(self):
        """Verify seed regex match returns confidence=0.95."""
        classifier = FailureClassifier()
        result = classifier.classify("Variable '${x}' not found")
        assert result.confidence == 0.95, (
            "Classifier: regex match -> confidence=0.95"
        )

    def test_b6_before_b1_input_text(self):
        """Verify B6 (library mismatch) takes priority over B1 (wrong keyword)."""
        classifier = FailureClassifier()
        result = classifier.classify("No keyword with name 'Input Text' found")
        assert result.category == "B6", (
            f"Pattern order: 'Input Text' -> B6 (not B1): got {result.category}"
        )

    def test_b1_for_generic_keyword(self):
        """Verify generic unknown keyword is B1."""
        classifier = FailureClassifier()
        result = classifier.classify("No keyword with name 'FooBar' found")
        assert result.category == "B1", (
            f"Pattern order: 'FooBar' -> B1 (generic): got {result.category}"
        )

    def test_layer2_placeholder_returns_none(self):
        """Verify Layer 2 (learned patterns) returns None in Phase 1."""
        classifier = FailureClassifier()
        result = classifier._check_learned_patterns("any error message")
        assert result is None, (
            "Layer 2: returns None (Phase 2 placeholder)"
        )


# ---------------------------------------------------------------------------
# Test: CompositeFailureDetector
# ---------------------------------------------------------------------------


class TestCompositeFailureDetector:
    """Tests for the CompositeFailureDetector."""

    def test_missing_iteration_a1(self):
        """Verify composite detects missing iteration (A1)."""
        detector = CompositeFailureDetector()
        result = detector.detect("verify all rows show 'Active'", "linear")
        assert result is not None and result.category == "A1", (
            f"Composite: 'all rows' + linear -> A1: got {result.category if result else 'None'}"
        )

    def test_missing_conditional_a7(self):
        """Verify composite detects missing conditional (A7)."""
        detector = CompositeFailureDetector()
        result = detector.detect("click only if the button exists", "linear")
        assert result is not None and result.category == "A7", (
            "Composite: 'only if' + linear -> A7"
        )

    def test_no_false_positive_all_alone(self):
        """Verify 'all' alone does NOT trigger false positive."""
        detector = CompositeFailureDetector()
        result = detector.detect("click all the submit button", "linear")
        assert result is None, (
            "Composite: 'all' alone does NOT trigger (multi-word safety): "
            "Should return None -- no multi-word phrase matched"
        )

    def test_no_detection_with_for_loop(self):
        """Verify no detection when code has FOR loop."""
        detector = CompositeFailureDetector()
        result = detector.detect("verify all rows show 'Active'", "for_loop")
        assert result is None, (
            "Composite: 'all rows' + for_loop -> no detection: "
            "Should NOT fire when code already has a FOR loop"
        )

    def test_empty_query_no_detection(self):
        """Verify empty query produces no detection."""
        detector = CompositeFailureDetector()
        result = detector.detect("", "linear")
        assert result is None, (
            "Composite: empty query -> no detection"
        )


# ---------------------------------------------------------------------------
# Test: FailureAnalyzer
# ---------------------------------------------------------------------------


class TestFailureAnalyzer:
    """Tests for the FailureAnalyzer end-to-end."""

    def test_passing_xml_category_none(self, tmp_dir):
        """Verify FailureAnalyzer correctly identifies passing test."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, PASSING_XML, "analyzer_pass.xml")

        result = analyzer.analyze(xml_path, user_query="click a button")
        assert result.category == "none" and result.specific_type == "test_passed", (
            "Analyzer: passing XML -> category='none'"
        )

    def test_passing_xml_confidence(self, tmp_dir):
        """Verify passing XML returns confidence=1.0."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, PASSING_XML, "analyzer_pass_conf.xml")

        result = analyzer.analyze(xml_path, user_query="click a button")
        assert result.confidence == 1.0, (
            "Analyzer: passing XML -> confidence=1.0"
        )

    def test_timeout_failure_category_d1(self, tmp_dir):
        """Verify FailureAnalyzer classifies timeout failure correctly."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "analyzer_timeout.xml")

        result = analyzer.analyze(xml_path, user_query="get text of element")
        assert result.category == "D1", (
            f"Analyzer: timeout failure -> D1: got {result.category}"
        )

    def test_timeout_failure_source_regex(self, tmp_dir):
        """Verify timeout failure source is 'regex'."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "analyzer_timeout_src.xml")

        result = analyzer.analyze(xml_path, user_query="get text of element")
        assert result.source == "regex", (
            f"Analyzer: timeout failure -> source='regex': got {result.source}"
        )

    def test_timeout_failure_failed_keyword(self, tmp_dir):
        """Verify timeout failure identifies failed_keyword='Get Text'."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "analyzer_timeout_kw.xml")

        result = analyzer.analyze(xml_path, user_query="get text of element")
        assert result.failed_keyword == "Get Text", (
            f"Analyzer: timeout failure -> failed_keyword='Get Text': got {result.failed_keyword}"
        )

    def test_timeout_failure_keyword_chain_populated(self, tmp_dir):
        """Verify timeout failure has keyword_chain populated."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "analyzer_timeout_chain.xml")

        result = analyzer.analyze(xml_path, user_query="get text of element")
        assert result.keyword_chain is not None and len(result.keyword_chain) > 0, (
            "Analyzer: timeout failure -> keyword_chain populated"
        )

    def test_no_xml_returns_a5(self):
        """Verify FailureAnalyzer degrades gracefully when output.xml is missing."""
        analyzer = FailureAnalyzer()
        result = analyzer.analyze(None, exit_code=1)
        assert result.category == "A5" and result.specific_type == "robot_crash_no_output", (
            "Analyzer: no XML -> A5/robot_crash_no_output"
        )

    def test_no_xml_confidence(self):
        """Verify no XML returns confidence=0.5."""
        analyzer = FailureAnalyzer()
        result = analyzer.analyze(None, exit_code=1)
        assert result.confidence == 0.5, (
            "Analyzer: no XML -> confidence=0.5"
        )

    def test_no_xml_source(self):
        """Verify no XML returns source='exit_code_only'."""
        analyzer = FailureAnalyzer()
        result = analyzer.analyze(None, exit_code=1)
        assert result.source == "exit_code_only", (
            "Analyzer: no XML -> source='exit_code_only'"
        )

    def test_nonexistent_path_returns_a5(self):
        """Verify non-existent path returns A5."""
        analyzer = FailureAnalyzer()
        result = analyzer.analyze("/nonexistent/path/output.xml", exit_code=2)
        assert result.category == "A5", (
            "Analyzer: non-existent path -> A5"
        )

    def test_composite_override(self, tmp_dir):
        """Verify composite result overrides regex when confidence is higher."""
        analyzer = FailureAnalyzer()

        unclassified_xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
            <suite id="s1" name="Test" source="/app/test.robot">
            <test id="s1-t1" name="Generated Test" line="12">
            <kw name="Get Text" owner="Browser">
                <msg time="2026-02-18T17:00:01" level="FAIL">Some novel error nobody has seen before</msg>
                <arg>${locator}</arg>
                <status status="FAIL" start="2026-02-18T17:00:01" elapsed="1.000">Some novel error</status>
            </kw>
            <status status="FAIL" start="2026-02-18T17:00:00" elapsed="2.000">Some novel error</status>
            </test>
            <status status="FAIL" start="2026-02-18T17:00:00" elapsed="3.000"/>
            </suite>
            <errors/>
            </robot>
        """)
        xml_path = _write_xml(tmp_dir, unclassified_xml, "composite_override.xml")

        result = analyzer.analyze(
            xml_path,
            user_query="verify all rows in the table",
            robot_code="Click    id=submit",  # linear code
        )
        assert result.category == "A1", (
            f"Analyzer: composite A1 overrides unknown (0.8 > 0.3): "
            f"got {result.category} (confidence={result.confidence})"
        )

    def test_args_extraction(self, tmp_dir):
        """Verify failed keyword arguments are extracted correctly."""
        analyzer = FailureAnalyzer()
        xml_path = _write_xml(tmp_dir, FAILING_XML_TIMEOUT, "args_extraction.xml")

        result = analyzer.analyze(xml_path)
        assert result.failed_keyword_args is not None and len(result.failed_keyword_args) > 0, (
            f"Analyzer: failed_keyword_args extracted: args={result.failed_keyword_args}"
        )
