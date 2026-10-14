"""Tests for the Robot Framework log extractor.

The extractor turns output.xml into the human-readable blob the SPA parses
(`result.logs` -> GeneratePage parseRobotSummary). Fixtures mirror real RF 7
output: a <test>'s own <status> is its LAST child, after every <kw>.

Referenced by: services/docker_service.py
Depends on: pytest tmp_path
"""
import os
import textwrap

import pytest

from src.backend.services.docker_service import _extract_robot_framework_logs

FAILING_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="New Browser" owner="Browser">
        <status status="PASS" start="2026-02-18T17:00:01" elapsed="0.300"/>
    </kw>
    <kw name="Click" owner="Browser">
        <msg time="2026-02-18T17:00:12" level="FAIL">TimeoutError: locator.click: Timeout 10000ms exceeded.</msg>
        <status status="FAIL" start="2026-02-18T17:00:02" elapsed="10.135">TimeoutError: locator.click: Timeout 10000ms exceeded.</status>
    </kw>
    <status status="FAIL" start="2026-02-18T17:00:01" elapsed="10.500">TimeoutError: locator.click: Timeout 10000ms exceeded.</status>
    </test>
    <status status="FAIL" start="2026-02-18T17:00:00" elapsed="11.000"/>
    </suite>
    <statistics><total><stat pass="0" fail="1" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")

PASSING_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="New Browser" owner="Browser">
        <status status="PASS" start="2026-02-18T17:00:01" elapsed="0.300"/>
    </kw>
    <status status="PASS" start="2026-02-18T17:00:01" elapsed="4.000"/>
    </test>
    <status status="PASS" start="2026-02-18T17:00:00" elapsed="5.000"/>
    </suite>
    <statistics><total><stat pass="1" fail="0" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")


def _write(tmp_path, xml: str, name: str = "output.xml") -> str:
    # strip(): a multi-line status message has a line at column 0, so dedent
    # leaves the XML declaration indented, and a declaration must come first.
    # Same as _write_xml in tests/test_optimization/test_failure_analyzer.py.
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml.strip())
    return path


def test_failed_test_is_reported_as_fail(tmp_path):
    """A failing test must not be printed as PASS (the <test> status is its LAST child)."""
    out = _extract_robot_framework_logs(
        _write(tmp_path, FAILING_XML), str(tmp_path / "log.html"), 1)

    assert "Test: Generated Test - FAIL" in out, out
    assert "Test: Generated Test - PASS" not in out, out


def test_failure_message_is_included(tmp_path):
    """The Error: line is what the SPA renders; without it the card shows nothing."""
    out = _extract_robot_framework_logs(
        _write(tmp_path, FAILING_XML), str(tmp_path / "log.html"), 1)

    assert "Error: TimeoutError: locator.click: Timeout 10000ms exceeded." in out, out


def test_passing_test_still_reported_as_pass(tmp_path):
    """Regression guard: the fix must not flip passing tests."""
    out = _extract_robot_framework_logs(
        _write(tmp_path, PASSING_XML), str(tmp_path / "log.html"), 0)

    assert "Test: Generated Test - PASS" in out, out


def test_statistics_line_is_unaffected(tmp_path):
    """The Results: line comes from <statistics> and must stay correct."""
    out = _extract_robot_framework_logs(
        _write(tmp_path, FAILING_XML), str(tmp_path / "log.html"), 1)

    assert "Results: 0 passed, 1 failed" in out, out


# RF 7.4.2 status.py: a failing Test Setup makes the test's message
# "Setup failed:\n<error>". The setup keyword is a <kw type="SETUP"> child.
SETUP_FAILING_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.2" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="New Page" owner="Browser" type="SETUP">
        <status status="FAIL" start="2026-02-18T17:00:01" elapsed="30.100">TimeoutError: page.goto: Timeout 30000ms exceeded.</status>
    </kw>
    <kw name="Click" owner="Browser">
        <status status="NOT RUN" start="2026-02-18T17:00:31" elapsed="0.000"/>
    </kw>
    <status status="FAIL" start="2026-02-18T17:00:01" elapsed="30.200">Setup failed:
TimeoutError: page.goto: Timeout 30000ms exceeded.</status>
    </test>
    <status status="FAIL" start="2026-02-18T17:00:00" elapsed="31.000"/>
    </suite>
    <statistics><total><stat pass="0" fail="1" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")


def test_setup_failure_header_is_joined_with_the_real_message(tmp_path):
    """The SPA reads only the first line of Error:, so 'Setup failed:' alone says nothing."""
    out = _extract_robot_framework_logs(
        _write(tmp_path, SETUP_FAILING_XML), str(tmp_path / "log.html"), 1)

    assert "Error: Setup failed: TimeoutError: page.goto: Timeout 30000ms exceeded." in out, out


@pytest.mark.parametrize("message,expected", [
    # RF 7.4.2 errors.py: continuable failures.
    ("Several failures occurred:\n\n1) Error: locator.click: boom\n\n2) second",
     "Several failures occurred: 1) Error: locator.click: boom\n\n2) second"),
    # RF 7.4.2 status.py: a failing Test Teardown on an otherwise passing test.
    ("Teardown failed:\nError: locator.click: boom",
     "Teardown failed: Error: locator.click: boom"),
    # Not a Robot Framework header: a strict-mode first line also ends with ':'.
    ("Error: locator.click: Error: strict mode violation: locator('a') resolved to 2 elements:\n"
     "    1) <a>x</a>",
     "Error: locator.click: Error: strict mode violation: locator('a') resolved to 2 elements:\n"
     "    1) <a>x</a>"),
    ("No keyword with name 'Logs' found. Did you mean:\n    Log",
     "No keyword with name 'Logs' found. Did you mean:\n    Log"),
    # A header with nothing after it is left alone.
    ("Setup failed:", "Setup failed:"),
])
def test_join_failure_header(message, expected):
    from src.backend.services.docker_service import _join_failure_header

    assert _join_failure_header(message) == expected


NESTED_FAILING_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <robot generator="Robot 7.4.1" generated="2026-02-18T17:00:00" rpa="false" schemaversion="5">
    <suite id="s1" name="Test" source="/app/test.robot">
    <test id="s1-t1" name="Generated Test" line="12">
    <kw name="Login Keyword" owner="Resource">
        <kw name="Fill Text" owner="Browser">
            <status status="PASS" start="2026-02-18T17:00:01" elapsed="0.200"/>
        </kw>
        <kw name="Click" owner="Browser">
            <status status="FAIL" start="2026-02-18T17:00:02" elapsed="10.100">TimeoutError: locator.click: Timeout 10000ms exceeded.</status>
        </kw>
        <status status="FAIL" start="2026-02-18T17:00:01" elapsed="10.300">TimeoutError: locator.click: Timeout 10000ms exceeded.</status>
    </kw>
    <status status="FAIL" start="2026-02-18T17:00:01" elapsed="10.500">TimeoutError: locator.click: Timeout 10000ms exceeded.</status>
    </test>
    <status status="FAIL" start="2026-02-18T17:00:00" elapsed="11.000"/>
    </suite>
    <statistics><total><stat pass="0" fail="1" skip="0">All Tests</stat></total></statistics>
    <errors/>
    </robot>
""")


def test_failed_keyword_lines_name_only_failing_keywords(tmp_path):
    """A parent keyword reads its OWN status, not its first child's.

    'Login Keyword' and 'Click' both failed; 'Fill Text' passed and must not
    appear as a failed keyword.
    """
    out = _extract_robot_framework_logs(
        _write(tmp_path, NESTED_FAILING_XML), str(tmp_path / "log.html"), 1)

    assert "Failed Keyword: Login Keyword" in out, out
    assert "Failed Keyword: Click" in out, out
    assert "Failed Keyword: Fill Text" not in out, out
