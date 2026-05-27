"""
Unit tests for _parse_conflict_json — no API calls, run fully offline.

Tests every format the conflict-detection LLM may emit (clean JSON,
markdown fences with and without language specifier, whitespace variants,
empty inputs). These run in CI without any credentials.

For live integration tests against the real Vertex AI / Gemini API see
tests/test_integration/test_conflict_detection_live.py.
"""

import json
import pytest


def _parse_conflict_json(content: str) -> dict:
    """Parse JSON from the LLM response, tolerating any preamble/postamble.

    The model wraps its JSON in ```json fences despite the prompt saying
    "ONLY valid JSON".  json.loads('```json...') raises:
        JSONDecodeError: Expecting value: line 1 column 1 (char 0)
    because backtick is not a valid JSON start character.

    raw_decode() starts at the first '{' and stops when the JSON object
    closes, so markdown fences, leading text, and trailing text are all
    ignored without any regex or string manipulation.
    """
    idx = content.find('{')
    if idx == -1:
        raise json.JSONDecodeError("No JSON object found in LLM response", content, 0)
    return json.JSONDecoder().raw_decode(content, idx)[0]


class TestParseConflictJson:
    """Validate _parse_conflict_json against every format the model may emit."""

    def test_plain_json(self):
        result = _parse_conflict_json('{"flag": [1, 2], "reason": "bad hint"}')
        assert result == {"flag": [1, 2], "reason": "bad hint"}

    def test_json_code_fence_with_language(self):
        content = '```json\n{"flag": [1], "reason": "viewport caused failure"}\n```'
        result = _parse_conflict_json(content)
        assert result["flag"] == [1]
        assert "viewport" in result["reason"]

    def test_json_code_fence_without_language(self):
        content = '```\n{"flag": [], "reason": "no conflicts"}\n```'
        result = _parse_conflict_json(content)
        assert result["flag"] == []

    def test_fence_with_uppercase_language(self):
        content = '```JSON\n{"flag": [3], "reason": "hint 3 was harmful"}\n```'
        result = _parse_conflict_json(content)
        assert result["flag"] == [3]

    def test_fence_with_leading_trailing_whitespace(self):
        content = '  \n```json\n{"flag": [], "reason": "ok"}\n```\n  '
        result = _parse_conflict_json(content)
        assert result["flag"] == []

    def test_fence_trailing_whitespace_after_closing_fence(self):
        content = '```json\n{"flag": [2], "reason": "x"}\n```   '
        result = _parse_conflict_json(content)
        assert result["flag"] == [2]

    def test_empty_flag_list(self):
        plain = '{"flag": [], "reason": "no conflicts detected"}'
        fenced = '```json\n{"flag": [], "reason": "no conflicts detected"}\n```'
        assert _parse_conflict_json(plain)["flag"] == []
        assert _parse_conflict_json(fenced)["flag"] == []

    def test_truly_empty_content_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_conflict_json("")

    def test_empty_fence_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_conflict_json("```json\n```")
